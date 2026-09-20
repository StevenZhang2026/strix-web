"""扫描 channel 的轮询循环（W2b：`app/services/scan_channel.py`）。

# 这个文件盯的两条不变式

1. **慢订阅者被摘掉，且不拖住轮询循环**
   （`test_full_queue_subscriber_is_dropped_and_the_loop_keeps_going`）：队列满 → 摘掉它
   并标记，**绝不** `await queue.put` —— 一个卡在后台标签页里的 WS 客户端不许让整条扫描
   的实时流停摆，也不许让别的订阅者漏帧。被摘掉与正常结束必须能分开：前者要客户端带
   `resume_from` 从镜像补齐。
2. **镜像写失败不被吞**（`test_mirror_write_failure_kills_the_channel`）：
   `EventMirror.append` 抛出来就必须一路冒泡把 channel 任务打死。只追加的真源出洞比
   断流更糟（回放会永久缺那一帧），而扫描本身与 `scans` 终态由 `routes/scans.py` 的
   `_run_to_completion` 兜着，不靠 channel。

# 为什么用 `asyncio.run` 而不是 `async def test_`

同 `test_image_puller.py`：本仓刻意没有 `pytest-asyncio`。循环里的"时间"靠构造参数注入的
`sleep`（下面的 `Sleeper`）拨，所以一次真实的 250ms 都不会睡；`Sleeper` 也是唯一能把
`run_forever` 拆出来的出口（`finish()` 那条出口有它自己的测试）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from app.services import scan_channel as channel_module
from app.services.event_mirror import MirroredEvent
from app.services.run_projector import ProjectedEvent, RunSnapshot
from app.services.scan_channel import (
    BASE_INTERVAL_S,
    MAX_INTERVAL_S,
    SUBSCRIBER_QUEUE_SIZE,
    ScanChannel,
    Subscriber,
)
from app.services.scan_frames import FrameState
from app.strix_profile import StrixProfile, profile_for
from app.ws_envelope import Envelope
from tests.conftest import make_run_dir

MEDIA_URL = "/api/scans/scan-1/media/deadbeef.png"


class StopLoop(Exception):
    """把 `run_forever` 从注入的 `sleep` 里拆出来。

    刻意不用 `CancelledError`：那个会被 `asyncio.run` 当成"外部取消了这次运行"，
    而我们要的是一个普通异常，`pytest.raises` 能明确地断言它。
    """


@dataclass
class Sleeper:
    """注入的 `sleep`：记下每次被要求睡多久，第 `limit` 次把循环拆出来。

    `on_sleep` 在两次 tick 之间跑 —— 它就是"Strix 又往 run 目录写了一笔"那个外部事件，
    没有它 stat 门会（正确地）把后面每一轮都整轮跳过。
    """

    limit: int
    on_sleep: Callable[[int], None] | None = None
    seen: list[float] = field(default_factory=list)

    async def __call__(self, seconds: float) -> None:
        self.seen.append(seconds)
        if self.on_sleep is not None:
            self.on_sleep(len(self.seen))
        if len(self.seen) >= self.limit:
            raise StopLoop


@dataclass
class FakeMirror:
    """镜像替身。两件真镜像会做、而本层必须跟着做对的事：

    ① `append` 把事件里内联的 base64 PNG 换成 media URL —— 所以帧的 payload 只能用
       **返回值**算；
    ② 写失败就抛 —— 真库很难按需抛，所以这里给一个 `error`（I2 只能这么测）。
    """

    error: Exception | None = None
    calls: list[tuple[int, int, str]] = field(default_factory=list)

    async def append(self, *, epoch: int, seq: int, event: ProjectedEvent) -> MirroredEvent:
        if self.error is not None:
            raise self.error
        self.calls.append((epoch, seq, event.key))
        rewritten = replace(event, data={**event.data, "screenshot": MEDIA_URL})
        return MirroredEvent(seq=seq, event=rewritten, new_media=(), skipped=())


def an_event(key: str = "tool_1", **overrides: object) -> ProjectedEvent:
    fields: dict[str, object] = {
        "key": key,
        "kind": "tool",
        "agent_id": "agent-1",
        "ts": "2026-09-20T00:00:00.000Z",
        "upstream_version": 1,
        "fingerprint": f"fp-{key}",
        "data": {"screenshot": "data:image/png;base64,AAAA"},
    }
    fields.update(overrides)
    return ProjectedEvent(**fields)  # type: ignore[arg-type]


def a_snapshot(**overrides: object) -> RunSnapshot:
    fields: dict[str, object] = {
        "agents": (),
        "events": (),
        "run_status": "running",
        "finished": False,
        "cost_usd": None,
        "severity": {},
        "vulnerabilities": (),
        "report_markdown": "",
    }
    fields.update(overrides)
    return RunSnapshot(**fields)  # type: ignore[arg-type]


@dataclass
class Harness:
    channel: ScanChannel
    run_dir: Path
    mirror: FakeMirror
    sleeper: Sleeper
    reads: list[Path]
    """`read_run_dir` 的每一次调用 —— stat 门"整轮跳过"就是看这个列表没长。"""


def make_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    snapshots: list[RunSnapshot] | None = None,
    sleeper: Sleeper | None = None,
    mirror: FakeMirror | None = None,
    with_run_dir: bool = True,
) -> Harness:
    """一个只对着替身说话的 channel。

    `discover_run` 与 stat 门走**真** tmp_path：它们要测的正是那几个 syscall 的结果。
    只有 `read_run_dir` 换成替身 —— 真的那个要 `strix` 的 `TuiLiveView` 去解析
    `agents.db`，那是 W2a／T13 的测试范围，在这里只会让意图变模糊。
    """
    queued = list(snapshots or [a_snapshot()])
    reads: list[Path] = []

    def fake_read_run_dir(run_dir: Path, profile: StrixProfile) -> RunSnapshot:
        reads.append(run_dir)
        # 最后一份留着重复给：被 stat 门跳过的轮次不消耗清单。
        return queued.pop(0) if len(queued) > 1 else queued[0]

    monkeypatch.setattr(channel_module, "read_run_dir", fake_read_run_dir)
    run_dir = make_run_dir(tmp_path, status="running") if with_run_dir else tmp_path / "missing"
    the_mirror = mirror if mirror is not None else FakeMirror()
    the_sleeper = sleeper if sleeper is not None else Sleeper(limit=1)
    channel = ScanChannel(
        scan_id="scan-1",
        cwd=tmp_path,
        profile=profile_for("1.6.2"),
        mirror=the_mirror,  # type: ignore[arg-type]
        redact=lambda text: text,
        sleep=the_sleeper,
    )
    return Harness(
        channel=channel,
        run_dir=run_dir,
        mirror=the_mirror,
        sleeper=the_sleeper,
        reads=reads,
    )


def bump(run_dir: Path, marker: str) -> None:
    """让 stat 门看见变化。改 **size** 而不是指望 mtime：mtime 的分辨率不是我们能保证的。"""
    (run_dir / "run.json").write_text(
        json.dumps({"status": "running", "bump": marker}), encoding="utf-8"
    )


def drive(channel: ScanChannel) -> None:
    """跑循环，直到注入的 `sleep` 把它拆出来。"""
    with pytest.raises(StopLoop):
        asyncio.run(channel.run_forever())


def drain(subscriber: Subscriber) -> list[Envelope]:
    """取出已经到货的帧。`None` 是"没有更多帧了"的哨兵 —— 到它就停。"""
    frames: list[Envelope] = []
    while not subscriber.queue.empty():
        item = subscriber.queue.get_nowait()
        if item is None:
            break
        frames.append(item)
    return frames


def filler_frame() -> Envelope:
    return Envelope(epoch=0, seq=0, type="log", ts="2026-09-20T00:00:00.000Z", payload={})


# =============================================================================
# 1. I1 —— 慢订阅者被摘掉，且不拖住轮询循环
# =============================================================================
def test_full_queue_subscriber_is_dropped_and_the_loop_keeps_going(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_channel(
        tmp_path,
        monkeypatch,
        snapshots=[a_snapshot(), a_snapshot(cost_usd=1.0)],
        sleeper=Sleeper(limit=2),
    )
    h.sleeper.on_sleep = lambda n: bump(h.run_dir, f"bump-{n}")
    slow = h.channel.subscribe()
    healthy = h.channel.subscribe()
    # 塞满慢订阅者的队列：它就是那个"页面在后台标签页里、久久不读"的客户端。
    for _ in range(SUBSCRIBER_QUEUE_SIZE):
        slow.queue.put_nowait(filler_frame())

    drive(h.channel)

    # ① 被摘掉并**标记**：客户端靠这一位决定"带 resume_from 重连"而不是"正常收尾"。
    assert slow.dropped is True
    # ② 读端不会永远挂在 `get()` 上：队列被腾空，只剩一个哨兵。
    assert slow.queue.qsize() == 1
    assert slow.queue.get_nowait() is None
    # ③ 别的订阅者**一帧都不许漏，尤其是"摘掉"动作发生的那一帧**（这里是 `agents`）：
    #    扇出时边迭代边从列表里摘，会让紧跟其后的订阅者被跳过，而且**只跳那一帧** ——
    #    所以只数 `summary` 的话这个 bug 是隐形的（2026-09-21 收货 MX5 实测 0 红）。
    types = [frame.type for frame in drain(healthy)]
    assert types == ["agents", "summary", "summary"], f"健康订阅者漏帧了：{types}"
    # ④ 循环真的转到了第二轮，没被那个满队列卡住。
    assert len(h.reads) == 2, "循环被那个满队列卡住了"


# =============================================================================
# 2. I2 —— 镜像写失败不被吞
# =============================================================================
def test_mirror_write_failure_kills_the_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boom = OSError("media 目录写不进去")
    h = make_channel(
        tmp_path,
        monkeypatch,
        snapshots=[a_snapshot(events=(an_event(),))],
        sleeper=Sleeper(limit=5),
        mirror=FakeMirror(error=boom),
    )

    with pytest.raises(OSError, match="media 目录写不进去"):
        asyncio.run(h.channel.run_forever())

    # 中途抛异常不许留下**半更新**的状态：留了的话，重跑那一轮会把已经发过的帧
    # 当成"推过了"而永远不再发。访问私有字段是刻意的（同 conftest 的 `conn` 夹具）。
    assert h.channel._frames == FrameState.empty()
    assert h.channel._gate is None


# =============================================================================
# 3. stat 门：四个文件都没变就整轮跳过
# =============================================================================
def test_stat_gate_skips_the_whole_tick_when_the_four_files_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_channel(tmp_path, monkeypatch, sleeper=Sleeper(limit=3))

    drive(h.channel)

    assert len(h.reads) == 1, "stat 门没挡住：run 目录一个字节都没变，却又读了一遍"


# =============================================================================
# 4. 事件帧的 payload 来自 `append` 的返回值
# =============================================================================
def test_event_frame_payload_comes_from_the_mirror_not_the_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_channel(
        tmp_path,
        monkeypatch,
        snapshots=[a_snapshot(events=(an_event(),))],
        sleeper=Sleeper(limit=1),
    )
    sub = h.channel.subscribe()

    drive(h.channel)

    events = [frame for frame in drain(sub) if frame.type == "event.add"]
    assert len(events) == 1
    # 顺序反了这里就是那串 base64（帧巨大，而且与回放出来的帧不一致）。
    assert events[0].payload["data"] == {"screenshot": MEDIA_URL}
    # 帧与镜像行必须同一个 seq，否则回放对不上。
    assert h.mirror.calls == [(0, events[0].seq, "tool_1")]


# =============================================================================
# 5. finish()：最后一次 tick + 唯一一个 done
# =============================================================================
def test_finish_does_a_last_tick_then_exactly_one_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_channel(tmp_path, monkeypatch)
    sub = h.channel.subscribe()

    async def scenario() -> None:
        await h.channel.finish()
        await h.channel.finish()

    asyncio.run(scenario())

    frames = drain(sub)
    assert [frame.type for frame in frames] == ["agents", "summary", "done"]
    # `done` 有自己的 seq（前端靠 seq 判漏帧，复用上一帧的号等于少了一帧）。
    assert [frame.seq for frame in frames] == [0, 1, 2]
    assert len(h.reads) == 1, "finish() 要做最后一次 tick，但第二次调用不许再做"


# =============================================================================
# 5b. finish() 之后循环必须停，而且不许再发一轮帧
# =============================================================================
def test_the_loop_stops_after_finish_and_never_ticks_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_finished` 的判据与置位必须在**同一把锁**里。

    否则有一个真的顺序洞：`finish()` 握着锁做最后一次 tick 时，循环可能已经通过了
    `while` 检查、正卡在锁上 —— 等它拿到锁就会再发一整轮帧，**落在 `done` 之后**。
    这里手工冒充那个交错：先替 `finish()` 把锁占住，让循环去锁上排队，再在锁内置位、放锁。
    （2026-09-21 收货 MX6 实测：`while not self._finished` 换成 `while True` 时全绿 ——
    "finish 之后循环会停"当时没有任何测试守着。）
    """
    h = make_channel(tmp_path, monkeypatch, sleeper=Sleeper(limit=1))

    async def scenario() -> None:
        await h.channel._lock.acquire()  # 冒充"finish() 正握着锁做最后一次 tick"
        task = asyncio.create_task(h.channel.run_forever())
        await asyncio.sleep(0)  # 循环：过了 while 检查，卡在锁上
        h.channel._finished = True  # finish() 在锁内做完了它该做的
        h.channel._lock.release()
        try:
            await asyncio.wait_for(task, timeout=1)
        except StopLoop:
            # 循环没停，一路跑到把注入的 sleep 用光。真正的判据是下面那条。
            pass

    asyncio.run(scenario())

    assert h.reads == [], "循环拿到锁之后又 tick 了一轮 —— 那些帧会落在 done 之后"


# =============================================================================
# 6. 自适应退避
# =============================================================================
def test_idle_backoff_caps_at_two_seconds_and_snaps_back_on_new_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_channel(
        tmp_path,
        monkeypatch,
        snapshots=[a_snapshot(), a_snapshot(cost_usd=2.0)],
        sleeper=Sleeper(limit=6),
    )
    # 只在第 5 次睡眠时"写一笔"：前面几轮纯空转（间隔翻倍到上限），最后一轮必须立刻回落。
    h.sleeper.on_sleep = lambda n: bump(h.run_dir, "late") if n == 5 else None

    drive(h.channel)

    assert h.sleeper.seen == [
        BASE_INTERVAL_S,
        0.5,
        1.0,
        MAX_INTERVAL_S,
        MAX_INTERVAL_S,
        BASE_INTERVAL_S,
    ]


# =============================================================================
# 7. run 目录还没出现的那段空窗
# =============================================================================
def test_no_run_dir_yet_means_a_no_op_tick(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = make_channel(tmp_path, monkeypatch, with_run_dir=False, sleeper=Sleeper(limit=2))
    sub = h.channel.subscribe()

    drive(h.channel)

    assert h.reads == []
    assert drain(sub) == []
    assert h.channel._tailer is None, "run 目录还没出现就建 tailer = 绑到一个不存在的路径上"


# =============================================================================
# 8. LogTailer 是长命的
# =============================================================================
def test_log_tailer_is_long_lived_so_lines_are_not_re_pushed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """每轮重建 tailer 会把整个日志重推一遍（偏移活在实例里）。"""
    h = make_channel(
        tmp_path,
        monkeypatch,
        snapshots=[a_snapshot(), a_snapshot(cost_usd=3.0)],
        sleeper=Sleeper(limit=2),
    )
    log_path = h.run_dir / "strix.log"
    log_path.write_text("first line\n", encoding="utf-8")

    def on_sleep(n: int) -> None:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"line {n}\n")
        bump(h.run_dir, f"bump-{n}")

    h.sleeper.on_sleep = on_sleep
    sub = h.channel.subscribe()

    drive(h.channel)

    log_frames = [frame for frame in drain(sub) if frame.type == "log"]
    rows = [frame.payload["lines"] for frame in log_frames]
    assert [len(row) for row in rows] == [1, 1], f"日志被重推了：{rows}"
