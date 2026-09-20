"""一个扫描一个轮询循环、N 个 WebSocket 订阅者。

职责边界：把 Strix run 目录的变化变成 WS 帧信封扇出给订阅者，并**同步**把事件帧写进
`EventMirror`（回放的真源）。本层只做"排班与发号"：读目录是 `strix_bridge.projection`，
算增量是 `run_projector.project`，排帧是 `scan_frames.plan_frames`（三者都是纯函数或纯 IO，
在这里只被编排）。

**channel 不看子进程、也不判"扫描跑完了没"** —— `finish()` 由 `routes/scans.py` 那侧在进程
退出后调（单一权威；两处各判一次必然打架）。`done` 帧不带任何结论，状态／归因／漏洞数只由
`GET /api/scans/{id}` 给。channel 也不 own `EventMirror` / `Database` 的生命周期。

两条不变式（各有测试守着，见 `tests/test_scan_channel.py`）：

- **I1 慢订阅者被摘掉，且不拖住轮询循环。** 每个订阅者一个有界队列；满了就摘掉它并
  标记（客户端稍后带 `resume_from` 重连），扇出全程 `put_nowait`，**绝不 await**。
- **I2 镜像写失败不被吞。** `EventMirror.append` 抛出来就让它冒泡把 channel 任务打死：
  只追加的真源出洞比断流更糟，而扫描本身与 `scans` 终态由 `_run_to_completion` 兜着。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.services.event_mirror import EventMirror
from app.services.log_tailer import LogLine, LogTailer
from app.services.run_discovery import discover_run
from app.services.run_projector import ProjectionState, project
from app.services.scan_frames import (
    FrameSpec,
    FrameState,
    done_spec,
    envelope_for,
    event_payload,
    plan_frames,
)
from app.strix_bridge.projection import read_run_dir
from app.strix_profile import StrixProfile
from app.ws_envelope import Envelope, Sequencer, now_ts

logger = logging.getLogger(__name__)

BASE_INTERVAL_S: Final = 0.25
"""有变化时的轮询间隔。"""

MAX_INTERVAL_S: Final = 2.0
"""空闲时的上限。再长会让"扫描开始动了"这件事迟到得能被人眼看见。"""

BACKOFF_FACTOR: Final = 2.0

SUBSCRIBER_QUEUE_SIZE: Final = 256
"""一个订阅者的积压上限。够一次重同步的开场（agents + 若干事件）排进去，
又小到一个不读的客户端不会把整条扫描的帧都攒在内存里。"""


@dataclass(eq=False)
class Subscriber:
    """一个 WS 连接的收帧端。`eq=False` → 按身份比较（它就是一个句柄，不是值）。

    队列里的 `None` 是"没有更多帧了"的哨兵：没有它，消费者会永远挂在 `queue.get()` 上。
    `dropped` 与"正常结束"必须分得开 —— 被摘掉的客户端要带 `resume_from` 从镜像补齐，
    正常结束的什么都不用做。
    """

    queue: asyncio.Queue[Envelope | None]
    dropped: bool = False


@dataclass(frozen=True, slots=True)
class StatGate:
    """四个被监视文件此刻的 `(mtime_ns, size)`。`None` = 那个文件现在不存在。

    "不存在"是一个**稳定值**而不是错误：run 目录刚建出来时 `vulnerabilities.json`
    本来就还没有，让它抛会把整个循环打死。比较交给 dataclass 的 `__eq__`（无 IO、纯）。
    """

    stamps: tuple[tuple[int, int] | None, ...]


def watched_rel_paths(profile: StrixProfile) -> tuple[str, ...]:
    """stat 门盯的四个文件（相对 run 目录）。字面量一个都不在这里 —— 全在 profile。"""
    return (
        profile.run_record_name,
        profile.agents_record_rel_path,
        profile.agents_db_rel_path,
        profile.vulnerabilities_rel_path,
    )


def read_stat_gate(run_dir: Path, profile: StrixProfile) -> StatGate:
    """空闲时的一整轮就是这几次 syscall。**同步 IO —— 调用方必须 `to_thread`。**"""
    return StatGate(stamps=tuple(_stamp(run_dir / rel) for rel in watched_rel_paths(profile)))


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def next_interval(current: float, *, produced_frames: bool) -> float:
    """纯函数：有帧就立刻回落，空转就翻倍到上限。"""
    if produced_frames:
        return BASE_INTERVAL_S
    return min(current * BACKOFF_FACTOR, MAX_INTERVAL_S)


class ScanChannel:
    """一次扫描的轮询循环 + 扇出。真正持有状态（两份投影状态、发号器、订阅者），所以是 class。"""

    def __init__(
        self,
        *,
        scan_id: str,
        cwd: Path,
        profile: StrixProfile,
        mirror: EventMirror,
        redact: Callable[[str], str],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._scan_id = scan_id
        self._cwd = cwd
        self._profile = profile
        self._mirror = mirror
        self._redact = redact
        # 注入是因为本仓刻意没有 pytest-asyncio：测试没法拨事件循环的表，只能拨这个。
        self._sleep = sleep
        self._run_dir: Path | None = None
        # 一次扫描一个长命 tailer（字节偏移活在实例里）。只有本类知道 run 目录哪一刻
        # 才出现，所以它必须在这里建，而不是由调用方传进来。
        self._tailer: LogTailer | None = None
        # `None` = 还没读过（≠ 四个文件都不存在），所以第一轮一定不会被跳过。
        self._gate: StatGate | None = None
        self._projection = ProjectionState.empty()
        self._frames = FrameState.empty()
        # epoch=-1 表示"还没发过任何帧"，所以第一帧的 seq 是 0（同 ImagePuller）。
        self._sequencer = Sequencer(epoch=-1)
        self._subscribers: list[Subscriber] = []
        self._interval = BASE_INTERVAL_S
        self._finished = False
        # `finish()` 的最后一次 tick 与循环里的 tick 是两个任务，同时跑会把两份状态
        # 搅成半更新的。一把锁比"约定谁先谁后"便宜得多。
        self._lock = asyncio.Lock()

    # ---- 订阅 ---------------------------------------------------------------
    def subscribe(self) -> Subscriber:
        subscriber = Subscriber(queue=asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE))
        self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        if subscriber in self._subscribers:
            self._subscribers.remove(subscriber)

    # ---- 生命周期 -----------------------------------------------------------
    async def run_forever(self) -> None:
        """循环：一次 tick + 睡一会儿。

        `CancelledError` 原样传出去（停机路径靠它），但订阅者一定要收尾 —— 少了这一步，
        每个还连着的客户端都会留下一个永远等在 `queue.get()` 上的任务。
        """
        try:
            while True:
                async with self._lock:
                    # 判据与置位**都在锁内**。在锁外检查是一个真的顺序洞：`finish()` 可能
                    # 正握着锁做最后一次 tick，而循环已经通过了 `while` 检查、在锁上排队 ——
                    # 等它拿到锁，就会在 `done` **之后**再发一整轮帧。
                    if self._finished:
                        break
                    produced = await self._tick()
                self._interval = next_interval(self._interval, produced_frames=produced)
                await self._sleep(self._interval)
        finally:
            self._close_subscribers()

    async def finish(self) -> None:
        """扫描进程已经退出（判据在调用方，channel 不看子进程）。

        最后一次 tick 把进程退出前写下的最后一批变化捞干净，再发**唯一**一个 `done`。
        幂等：第二次调用什么都不做（判据与置位都在同一把锁里，两个并发调用不会双双穿过）。
        """
        async with self._lock:
            if self._finished:
                return
            await self._tick()
            await self._emit(done_spec(), self._projection.epoch)
            self._finished = True

    # ---- 一轮 ---------------------------------------------------------------
    async def _tick(self) -> bool:
        """返回"这一轮有没有产出帧"（退避的判据）。"""
        run_dir = self._run_dir
        if run_dir is None:
            discovered = await asyncio.to_thread(discover_run, self._cwd, self._profile)
            if discovered is None:
                # Strix 启动到建出 run 目录之间有一段空窗，那几轮什么都做不了。
                return False
            run_dir = discovered.run_dir
            self._run_dir = run_dir
            self._tailer = LogTailer(run_dir / self._profile.log_file_name, self._redact)

        gate = await asyncio.to_thread(read_stat_gate, run_dir, self._profile)
        if gate == self._gate:
            return False

        snapshot = await asyncio.to_thread(read_run_dir, run_dir, self._profile)
        log_lines: tuple[LogLine, ...] = (
            () if self._tailer is None else await self._tailer.read_new()
        )
        result = project(self._projection, snapshot, self._profile)
        specs, frame_state = plan_frames(self._frames, result, snapshot, log_lines)
        # 重同步那一轮 epoch 已经 +1，所以帧必须落在**新** epoch 上。
        epoch = result.state.epoch
        for spec in specs:
            await self._emit(spec, epoch)
        # 整轮成功走完才提交状态：中途抛异常（I2）留下半更新的状态，会让已经发过一半的
        # 那一轮被当成"推过了"而永远不再补。
        self._gate = gate
        self._projection = result.state
        self._frames = frame_state
        return bool(specs)

    async def _emit(self, spec: FrameSpec, epoch: int) -> None:
        """发号 → 落镜像 → 扇出。**这个顺序错了就是缺陷。**"""
        seq = self._sequencer.next(epoch)
        if spec.event is not None:
            # 必须先 append 再算 payload：它会把内联的 `data:image/...;base64` 落地成
            # media URL 并改写正文。反过来 WS 帧里就带着 base64（帧巨大），而且与从
            # 镜像回放出来的帧不一致（前端只有一套解析）。
            # 抛出来的异常**刻意不接**（I2）。
            mirrored = await self._mirror.append(epoch=epoch, seq=seq, event=spec.event)
            payload = event_payload(mirrored.event)
        else:
            # `FrameSpec` 的契约：`event` 非 None ⟺ `payload` 为 None，所以这一支必有 payload。
            payload = {} if spec.payload is None else spec.payload
        self._fanout(
            envelope_for(type=spec.type, payload=payload, epoch=epoch, seq=seq, ts=now_ts())
        )

    # ---- 扇出 ---------------------------------------------------------------
    def _fanout(self, envelope: Envelope) -> None:
        """同步、非阻塞。一个满队列只影响它自己那个订阅者（`list(...)` 是因为要边发边摘）。"""
        for subscriber in list(self._subscribers):
            self._offer(subscriber, envelope)

    def _offer(self, subscriber: Subscriber, frame: Envelope | None) -> None:
        try:
            subscriber.queue.put_nowait(frame)
        except asyncio.QueueFull:
            self._drop(subscriber)

    def _drop(self, subscriber: Subscriber) -> None:
        """摘掉一个跟不上的订阅者。绝不 `await put` —— 那会让一个卡住的客户端拖停整条扫描。"""
        subscriber.dropped = True
        self.unsubscribe(subscriber)
        # 先腾空再塞哨兵：队列是满的，不腾空就塞不进去，读端会永远挂在 `get()` 上。
        # 那些帧也已经没用了 —— 客户端要带 `resume_from` 从镜像整段补齐。
        _drain(subscriber.queue)
        subscriber.queue.put_nowait(None)
        logger.warning(
            "WS 订阅者跟不上，已摘掉（客户端可带 resume_from 重连）",
            extra={"scan_id": self._scan_id},
        )

    def _close_subscribers(self) -> None:
        """让每个读端都看得出"没有更多帧了"。"""
        for subscriber in list(self._subscribers):
            self._offer(subscriber, None)
        self._subscribers.clear()


def _drain(queue: asyncio.Queue[Envelope | None]) -> None:
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return
