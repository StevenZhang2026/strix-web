"""拉沙箱镜像 + 把 docker 的进度流聚合成一个可推送的状态（T11b）。

# 一条总原则：`POST /images/create` 的 HTTP 状态码是 200 也可能是失败

失败以正文里一行 `{"error": "...", "errorDetail": {"message": "..."}}` 出现（已在
docker API 文档与本机实测确认）。**只看状态码就是静默假成功** —— 与 pitfalls 条 24
（"strix 退出码 0 不代表跑完了"）完全同型的坑，代价也一样：界面说"好了"，而实际上
第一次扫描才会以一个看起来毫不相关的错误失败。所以本模块判定失败的判据有三条，
少一条都不行：传输层异常、非 200 状态码、**正文里的 error 行**。

# 百分比宁可没有，也不许猜

任一层的 `total` 未知时 `percent` 就是 `None`。一个假的 0%（或按已知层算出来的
"75%"）比没有百分比更坏：进度条在动，人就会等下去。
唯一的例外不是猜：`Already exists` 的层永远不带 `total`，而它的真值是"这一层零字节
要下"，不是"未知" —— 那是一条事实，不是乐观默认值。

# 为什么整体状态是"整体替换"而不是原地改字段

`PullState` 是 frozen dataclass，每次都新建一个。推给 N 个订阅者的帧必须是一个
**当时那一刻的快照**：原地可变的状态会让"第二个订阅者收到的 payload 已经变了"
这件事静默发生，而它只在多人同时看着界面时才发作。

# 进度不做回放

订阅者一连上就拿一帧全量快照，之后只推增量；醒来时只推**最新**一帧（中间帧丢掉）。
这对进度是对的 —— 它是幂等快照，回放没有意义。T14 的 `/ws/scans/{id}` 推的是
不可重建的事件序列，那里才需要 `resume`。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Literal
from urllib.parse import urlencode

from app.errors import assert_scan_failure_code
from app.services.docker_probe import DockerApiError, DockerProbe, DockerTransport
from app.ws_envelope import Envelope, Sequencer, now_ts

logger = logging.getLogger(__name__)

PULL_FAILURE_CODE = assert_scan_failure_code("failed_to_pull_image")
"""归因码。**刻意不新增机器码** —— `errors.SCAN_FAILURE_CODES` 里已经有这一个，前端
文案也已经有。用 `assert_scan_failure_code` 取值而不是抄一个字面串：抄错了没人拦，
而这一行在 import 期就会炸。"""

PULL_TIMEOUT_S = 300.0
"""**这是"daemon 沉默多久算挂了"，不是"拉取总时长上限"。**

底层是 socket 超时，作用在每一次 `recv` 上（见 `docker_probe._UnixHTTPConnection`），
而拉取期间 daemon 每秒都在吐进度行 —— 所以一个几 GB 的镜像拉 20 分钟也不会超时，
而真的卡死 5 分钟就会。写成"总时长上限"（比如 1800）反而会在慢网络上误杀。
"""

MIN_FRAME_INTERVAL_S = 0.25
MIN_PERCENT_DELTA = 0.01
"""节流。一个几十层的镜像每秒能吐上百行进度，逐行推给前端只是在浪费两边的 CPU。
终态（`done` / `error`）**不受节流约束**，无条件立即发。"""

IDLE_PHASE = "idle"
STARTING_PHASE = "starting"
ALREADY_PRESENT_PHASE = "already_present"

# docker 进度行的 `status` → 该层所处的阶段。**只认这张表里的**：
# `Pulling from usestrix/strix-sandbox`、`Digest: sha256:…`、`Status: Downloaded …`
# 三种行也带（或不带）id，认下来会凭空多出几个"层"。
_STAGE_BY_STATUS = {
    "pulling fs layer": "queued",
    "waiting": "queued",
    "downloading": "downloading",
    "verifying checksum": "verifying",
    "download complete": "downloaded",
    "extracting": "extracting",
    "pull complete": "complete",
    "already exists": "complete",
}
# 从慢到快。整体阶段取**最落后**的那一层 —— 这样它基本单调不倒退，节流的
# "阶段变化就立即发"才有意义（取最新一行的 status 会在多层交错时来回跳）。
_STAGE_ORDER = ("queued", "downloading", "verifying", "downloaded", "extracting", "complete")

_MAX_DAEMON_MESSAGE = 300


@dataclass(frozen=True)
class PullState:
    """一次拉取的全量状态。**整体替换，不原地改字段**（理由见模块 docstring）。

    `code` / `reason` 只在 `status == "error"` 时非空：`code` 是给前端查文案的稳定
    机器码，`reason` 是 `DockerApiError.reason` 或 `daemon_error` —— 给排障的人分辨
    "socket 没挂上"和"registry 限流"。两者都不含 daemon 原文（那只进日志）。
    """

    status: Literal["idle", "pulling", "done", "error"]
    epoch: int
    reference: str
    phase: str
    percent: float | None
    downloaded_bytes: int
    total_bytes: int | None
    layers_done: int
    layers_total: int
    code: str | None
    reason: str | None


@dataclass
class _Layer:
    stage: str
    current: int = 0
    total: int | None = None


@dataclass
class LayerTally:
    """按层累加 docker 的进度流。**无 IO 的纯累加器**（CLAUDE.md §Python），所以
    "多层怎么求和"、"什么时候不给百分比"这两条判定是被直接单测的。
    """

    layers: dict[str, _Layer] = field(default_factory=dict)
    daemon_message: str | None = None
    """daemon 在正文里报的错误原文。**只用来记日志**，不进 `PullState`。"""

    def absorb(self, line: Mapping[str, object]) -> None:
        message = _error_message(line)
        if message is not None:
            self.daemon_message = message[:_MAX_DAEMON_MESSAGE]
            return

        status = line.get("status")
        if not isinstance(status, str):
            return
        stage = _STAGE_BY_STATUS.get(status.strip().lower())
        if stage is None:
            return
        layer_id = line.get("id")
        if not isinstance(layer_id, str) or not layer_id:
            return

        layer = self.layers.setdefault(layer_id, _Layer(stage=stage))
        layer.stage = stage
        current, total = _progress_detail(line)
        if current is not None:
            layer.current = current
        if total is not None:
            layer.total = total
        if stage == "complete":
            # 完成的层：docker 不再报 current，而"下完了"就是 current == total。
            # total 一直没出现过（`Already exists`）的层记 0 —— 见模块 docstring。
            if layer.total is None:
                layer.total = 0
            layer.current = layer.total

    def phase(self) -> str:
        if not self.layers:
            return STARTING_PHASE
        return min((layer.stage for layer in self.layers.values()), key=_STAGE_ORDER.index)

    def layers_total(self) -> int:
        return len(self.layers)

    def layers_done(self) -> int:
        return sum(1 for layer in self.layers.values() if layer.stage == "complete")

    def downloaded_bytes(self) -> int:
        return sum(layer.current for layer in self.layers.values())

    def total_bytes(self) -> int | None:
        """**任何一层的 total 未知，整体就是未知。** 不用已知层的和冒充总量。"""
        if not self.layers:
            return None
        totals = [layer.total for layer in self.layers.values()]
        if any(total is None for total in totals):
            return None
        return sum(total for total in totals if total is not None)

    def percent(self) -> float | None:
        total = self.total_bytes()
        if total is None:
            return None
        if total == 0:
            # 整个镜像全在本地（每一层都是 `Already exists`）。除法在这里没有意义，
            # 但"全完成了"是已知的。
            return 1.0 if self.layers_done() == self.layers_total() else None
        return round(min(1.0, self.downloaded_bytes() / total), 4)


def split_reference(reference: str) -> tuple[str, str]:
    """`ghcr.io/usestrix/strix-sandbox:1.3.0` → `("ghcr.io/usestrix/strix-sandbox", "1.3.0")`。

    `/images/create` 要求 `fromImage` 与 `tag` 分开传。**只在最后一个 `/` 之后才认 `:`** ——
    否则 `registry:5000/foo` 会被切成主机名加端口。digest 形式（`name@sha256:…`）走
    同一个 `tag` 参数，docker 明确接受。
    """
    prefix, _, last = reference.rpartition("/")

    def rejoin(name: str) -> str:
        return f"{prefix}/{name}" if prefix else name

    if "@" in last:
        name, _, digest = last.partition("@")
        return rejoin(name), digest
    if ":" in last:
        name, _, tag = last.partition(":")
        return rejoin(name), tag
    return reference, "latest"


def should_publish(previous: PullState, candidate: PullState, *, elapsed_s: float) -> bool:
    """要不要为这个新状态发一帧。纯函数，所以节流是可测的（时钟由调用方注入）。

    三个触发条件（任一成立即发）：阶段变了、距上一帧够久、百分比跨过一个整点。
    前置：状态**完全没变**就一定不发 —— 不然"距上一帧够久"会让空帧按 4 fps 刷。
    终态不走这里（无条件发）。
    """
    if candidate.phase != previous.phase:
        return True
    if candidate == previous:
        return False
    if elapsed_s >= MIN_FRAME_INTERVAL_S:
        return True
    return _percent_jumped(previous.percent, candidate.percent)


class ImagePuller:
    """一次拉取的全部状态 + 一个后台任务 + 一个"有变化了"的广播。

    状态挂在实例上（`app.state.image_puller`），**模块级不得有可变全局** ——
    CLAUDE.md §Python，也是 Strix 那条"同进程并发会跨用户污染 API Key"的教训。
    """

    def __init__(
        self,
        transport: DockerTransport,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._probe = DockerProbe(transport=transport)
        self._clock = clock
        self._epoch = 0
        self._reference = ""
        self._tally = LayerTally()
        # epoch=-1 表示"还没发过任何帧"，所以第一帧永远是 seq=0。
        self._sequencer = Sequencer(epoch=-1)
        self._version = 0
        self._last_frame_at = clock()
        self._changed = asyncio.Event()
        self._start_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._frame: Envelope | None = None
        self._state = PullState(
            status="idle",
            epoch=0,
            reference="",
            phase=IDLE_PHASE,
            percent=None,
            downloaded_bytes=0,
            total_bytes=None,
            layers_done=0,
            layers_total=0,
            code=None,
            reason=None,
        )

    # ---- 只读视图 -----------------------------------------------------------
    @property
    def state(self) -> PullState:
        return self._state

    @property
    def version(self) -> int:
        """发过多少帧。订阅者拿它当"我看到哪儿了"的游标。"""
        return self._version

    def is_pulling(self) -> bool:
        return self._task is not None and not self._task.done()

    def current_frame(self) -> Envelope | None:
        """最近发出的那一帧。没拉过就是 `None`。"""
        return self._frame

    def snapshot_frame(self) -> Envelope:
        """给刚连上的订阅者的全量快照。**每次新建**（`ts` 要是此刻），但 `seq` 沿用
        当前那一帧的号 —— 快照不是新事件，给它一个新号会让"我漏帧了吗"的判断出错。
        """
        return Envelope(
            epoch=self._state.epoch,
            seq=self._sequencer.seq if self._frame is not None else 0,
            type="pull.snapshot",
            ts=now_ts(),
            payload=asdict(self._state),
        )

    async def wait_for_change(self, seen: int) -> None:
        """等到 `version` 不再等于 `seen`。

        承重的是**每次发布换一个新 `Event`**（而不是 `set()` + `clear()`）：clear 与
        wait 之间的竞态没有安全的写法，而"换一个新的"让等待方一定持有它比对版本号
        那一刻的那个对象。
        下面两行的顺序（先取 Event、再比版本）在单线程事件循环里其实是**等价**的 ——
        中间没有 await，插不进一次发布（T11b 收货 mutation M8 调换顺序，一条测试都
        没红）。保留这个顺序是为了以后有人在中间加 await 时它仍然对，**但别把它当成
        一条被测试守着的不变式**。
        """
        while True:
            event = self._changed
            if self._version != seen:
                return
            await event.wait()

    # ---- 拉取 ---------------------------------------------------------------
    async def start(self, reference: str) -> tuple[PullState, bool]:
        """幂等入口。返回 `(当前状态, 这一次是不是真起了一次拉取)`。
        已在本地就什么都不做，正在拉就原样返回当前状态。

        **第二个返回值不许让调用方自己算。** 路由靠它决定写不写审计，而"epoch 变没变"
        只有在这把锁里看才是**这一次调用**的结果：两个同时到达的 POST 里的第二个，
        会看到一个由第一个推进的 epoch，于是给同一次拉取写第二条 `image.pull_started`
        （收货时实测）—— 审计里多一条没发生过的事，比少一条更坏。

        **整个函数在一把锁里。** `is_pulling()` 这个判据与 `create_task()` 之间有一次
        await（查镜像在不在本地），两个**同时**到达的 POST 会双双穿过判据、各起一次
        几 GB 的拉取、各写一条审计，而 `self._task` 只留得下后一个 —— 前一个成了孤儿
        任务，仍然在往这里的状态里写。收货时实测确认过（先后 `await` 两次的那条测试
        看不见它，窗口只在并发时张开）。
        锁只在"查镜像"这几毫秒里被持有：真正的拉取在另一个 task 里，两个请求不会互相
        阻塞。
        """
        async with self._start_lock:
            if self.is_pulling():
                return self._state, False

            try:
                facts = await asyncio.to_thread(self._probe.image_facts, reference)
                present = facts.present
            except DockerApiError as exc:
                # 查不到就当"不在本地"往下走：拉取会以同一个 reason 失败，并如实报成一个
                # error 帧。在这里提前抛的话，同一种故障会有两条出口（HTTP 500 与 error 帧）。
                logger.warning("查沙箱镜像是否在本地失败，按不在处理", extra={"reason": exc.reason})
                present = False

            if present:
                self._reference = reference
                self._tally = LayerTally()
                self._publish(
                    PullState(
                        status="done",
                        epoch=self._epoch,
                        reference=reference,
                        phase=ALREADY_PRESENT_PHASE,
                        percent=1.0,
                        downloaded_bytes=0,
                        total_bytes=None,
                        layers_done=0,
                        layers_total=0,
                        code=None,
                        reason=None,
                    ),
                    "pull.done",
                )
                return self._state, False

            self._epoch += 1
            self._reference = reference
            self._tally = LayerTally()
            self._publish(self._compose("pulling"), "pull.progress")
            loop = asyncio.get_running_loop()
            task = asyncio.create_task(self._pull(reference, loop))
            self._task = task
            return self._state, True

    async def shutdown(self) -> None:
        """取消在跑的拉取并等它结束。lifespan 关停时调，形状与 `sweeper` / `reaper_task`
        一致（cancel 然后 await，吞掉 `CancelledError`）。

        **刻意不写终态帧**：进程要停了，"拉取被中断"不是 `done` 也不是 `error`。
        下一次启动的第一次 POST 会重新开始（epoch 因此 +1，前端丢掉旧进度）。
        """
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            # 只吞**这个任务**被取消这一件事。如果是本协程自己被取消，await 会重抛
            # 到这里 —— 那种情况下 task.cancelled() 也为真，两者都已经结束了，
            # 停机路径不需要再区分。
            pass

    async def _pull(self, reference: str, loop: asyncio.AbstractEventLoop) -> None:
        name, tag = split_reference(reference)
        path = f"/images/create?{urlencode({'fromImage': name, 'tag': tag})}"

        def on_line(line: Mapping[str, object]) -> None:
            # **在工作线程里跑。** 绝不直接碰 asyncio 对象（Event / Task / 状态），
            # 一律派回事件循环。`call_soon_threadsafe` 保序，而 `to_thread` 的完成
            # 通知走的是同一个队列 —— 所以下面 await 返回时，前面每一行都已经处理过了。
            loop.call_soon_threadsafe(self._apply, line)

        try:
            status = await asyncio.to_thread(
                self._transport.stream_ndjson,
                "POST",
                path,
                timeout_s=PULL_TIMEOUT_S,
                on_line=on_line,
            )
        except DockerApiError as exc:
            self._fail(exc.reason)
            return

        if self._state.status == "error":
            # 正文里的 error 行已经发过终态帧了，别用一个 done 盖掉它。
            return
        if status != 200:
            logger.warning("拉取沙箱镜像失败", extra={"http_status": status})
            self._fail("bad_status")
            return
        self._publish(self._compose("done"), "pull.done")

    # ---- 帧 -----------------------------------------------------------------
    def _apply(self, line: Mapping[str, object]) -> None:
        """处理一行进度。**在事件循环里跑**（由 `call_soon_threadsafe` 派进来）。"""
        if self._state.status != "pulling":
            # 终态之后（或取消之后）还在路上的行一律丢掉。
            return
        self._tally.absorb(line)
        if self._tally.daemon_message is not None:
            # 这是那条"HTTP 200 也可能是失败"的判据。原文只进日志，不进帧。
            logger.warning(
                "拉取沙箱镜像失败：daemon 在进度流里报错",
                extra={"detail": self._tally.daemon_message},
            )
            self._fail("daemon_error")
            return
        candidate = self._compose("pulling")
        if should_publish(self._state, candidate, elapsed_s=self._clock() - self._last_frame_at):
            self._publish(candidate, "pull.progress")

    def _fail(self, reason: str) -> None:
        self._publish(self._compose("error", code=PULL_FAILURE_CODE, reason=reason), "pull.error")

    def _compose(
        self,
        status: Literal["idle", "pulling", "done", "error"],
        *,
        code: str | None = None,
        reason: str | None = None,
    ) -> PullState:
        return PullState(
            status=status,
            epoch=self._epoch,
            reference=self._reference,
            phase=self._tally.phase(),
            percent=self._tally.percent(),
            downloaded_bytes=self._tally.downloaded_bytes(),
            total_bytes=self._tally.total_bytes(),
            layers_done=self._tally.layers_done(),
            layers_total=self._tally.layers_total(),
            code=code,
            reason=reason,
        )

    def _publish(self, state: PullState, frame_type: str) -> None:
        self._state = state
        self._version += 1
        self._last_frame_at = self._clock()
        self._frame = Envelope(
            epoch=state.epoch,
            seq=self._sequencer.next(state.epoch),
            type=frame_type,
            ts=now_ts(),
            payload=asdict(state),
        )
        # 换一个新 Event 再 set 旧的：等待方持有的一定是它比对版本号那一刻的那个。
        stale = self._changed
        self._changed = asyncio.Event()
        stale.set()


def _error_message(line: Mapping[str, object]) -> str | None:
    """从一行里读出 daemon 报的错误。**这是"200 也可能失败"的唯一判据。**"""
    detail = line.get("errorDetail")
    if isinstance(detail, Mapping):
        message = detail.get("message")
        if isinstance(message, str) and message:
            return message
    error = line.get("error")
    if isinstance(error, str) and error:
        return error
    return None


def _progress_detail(line: Mapping[str, object]) -> tuple[int | None, int | None]:
    detail = line.get("progressDetail")
    if not isinstance(detail, Mapping):
        return None, None
    current = detail.get("current")
    total = detail.get("total")
    # `total: 0` 出现在"这一层没有字节要下"的行里，当未知处理反而会把整体总量抹成未知。
    return (
        current if isinstance(current, int) else None,
        total if isinstance(total, int) and total >= 0 else None,
    )


def _percent_jumped(previous: float | None, candidate: float | None) -> bool:
    if previous is None or candidate is None:
        # 从"未知"变成"已知"（或反过来）本身就是一条真信息。两个都未知则没变化。
        return previous is not candidate
    return abs(candidate - previous) >= MIN_PERCENT_DELTA
