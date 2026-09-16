"""沙箱镜像拉取（T11b：`app/services/image_puller.py`）。

# 这个文件盯的是四件"只看状态码就会被骗"的事

1. **`POST /images/create` 失败时 HTTP 状态码仍然是 200。** 失败以正文里一行
   `{"error": ...}` 出现。只看状态码 = 静默假成功，与 pitfalls 条 24（strix 退出码 0
   不代表跑完了）同源。这是本文件的第一条测试。
2. **百分比不许猜。** 任一层的 total 未知时 `percent` 必须是 `None` —— 一个假的 0%
   比没有百分比更坏，因为它会让人以为进度条在动。
3. **节流真的在节流。** 没有它，一个几百层的镜像会把每一行进度都推给前端。
4. **停机时拉取任务真的在 `shutdown()` 返回之前就被取消了**（不是"最后总会被取消"）。

# 为什么用 `asyncio.run` 而不是 `async def test_`

同 `test_audit.py`：本仓刻意没有 `pytest-asyncio`。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Mapping

import pytest

from app.services.docker_probe import DockerApiError
from app.services.image_puller import (
    ALREADY_PRESENT_PHASE,
    PULL_FAILURE_CODE,
    ImagePuller,
    LayerTally,
    PullState,
    should_publish,
    split_reference,
)
from tests.conftest import FakeClock, FakeTransport, StreamHandler, const, reply

REF = "ghcr.io/usestrix/strix-sandbox:1.3.0"

OnLine = Callable[[Mapping[str, object]], None]


def feed(*rows: Mapping[str, object], status: int = 200) -> StreamHandler:
    """一个把预置行逐条喂给 `on_line` 的流处理器。"""

    def handler(_call: object, on_line: OnLine) -> int:
        for row in rows:
            on_line(row)
        return status

    return handler


def raises(error: DockerApiError) -> StreamHandler:
    def handler(_call: object, _on_line: OnLine) -> int:
        raise error

    return handler


def make_puller(
    *,
    present: bool = False,
    stream: StreamHandler | None = None,
) -> tuple[ImagePuller, FakeTransport, FakeClock]:
    """一个只对着替身说话的 puller。时钟冻结（节流的判据因此完全可控）。"""
    facts = reply(200, {"Size": 123}) if present else reply(404, {"message": "no such image"})
    transport = FakeTransport(
        routes=[("GET", "/json", const(facts.status, {"Size": 123}))],
        streams=[] if stream is None else [("POST", "/images/create", stream)],
    )
    clock = FakeClock()
    return ImagePuller(transport, clock=clock), transport, clock


async def finish(puller: ImagePuller) -> None:
    """等后台拉取任务自然跑完。

    访问私有字段是刻意的：生产代码只需要 `shutdown()`（取消），"等它自然结束"这个入口
    只有测试要，为它加一个公开方法是给生产 API 加负担（conftest 的 `conn` 夹具同理）。
    """
    task = puller._task
    assert task is not None, "start() 应该起了一个后台任务"
    await task


def layer(status: str, id_: str, current: int | None = None, total: int | None = None) -> dict:
    line: dict[str, object] = {"status": status, "id": id_}
    if current is not None or total is not None:
        detail: dict[str, object] = {}
        if current is not None:
            detail["current"] = current
        if total is not None:
            detail["total"] = total
        line["progressDetail"] = detail
    return line


def a_state(**overrides: object) -> PullState:
    fields: dict[str, object] = {
        "status": "pulling",
        "epoch": 1,
        "reference": REF,
        "phase": "downloading",
        "percent": 0.10,
        "downloaded_bytes": 10,
        "total_bytes": 100,
        "layers_done": 0,
        "layers_total": 1,
        "code": None,
        "reason": None,
    }
    fields.update(overrides)
    return PullState(**fields)  # type: ignore[arg-type]


# =============================================================================
# 1. HTTP 200 但正文里有 error（本文件的第一条，理由见模块 docstring）
# =============================================================================
def test_error_line_in_a_200_response_is_a_failure() -> None:
    stream = feed(
        layer("Downloading", "aaa", 10, 100),
        {"errorDetail": {"message": "toomanyrequests"}, "error": "toomanyrequests"},
        status=200,
    )
    puller, _transport, _clock = make_puller(stream=stream)

    async def scenario() -> None:
        await puller.start(REF)
        await finish(puller)

    asyncio.run(scenario())

    assert puller.state.status == "error"
    assert puller.state.code == PULL_FAILURE_CODE
    assert puller.state.reason == "daemon_error"
    assert puller.current_frame() is not None
    assert puller.current_frame().type == "pull.error"


# =============================================================================
# 2. 引用拆分（纯函数）
# =============================================================================
@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        (REF, ("ghcr.io/usestrix/strix-sandbox", "1.3.0")),
        ("redis", ("redis", "latest")),
        ("registry:5000/foo", ("registry:5000/foo", "latest")),
        ("registry:5000/foo:v2", ("registry:5000/foo", "v2")),
        ("busybox@sha256:abc123", ("busybox", "sha256:abc123")),
    ],
)
def test_split_reference(reference: str, expected: tuple[str, str]) -> None:
    assert split_reference(reference) == expected


# =============================================================================
# 3. 进度聚合与"不猜百分比"
# =============================================================================
def test_progress_sums_every_layer() -> None:
    tally = LayerTally()
    tally.absorb(layer("Downloading", "aaa", 30, 100))
    tally.absorb(layer("Downloading", "bbb", 20, 300))
    tally.absorb(layer("Pull complete", "aaa"))

    # `Pull complete` 的那一层按 total 记满：docker 在那之后不再报它的 current，
    # 而"这一层下完了"就是 current == total。
    assert tally.downloaded_bytes() == 120
    assert tally.total_bytes() == 400
    assert tally.percent() == pytest.approx(0.3)
    assert tally.layers_total() == 2
    assert tally.layers_done() == 1
    # 最落后的那一层决定整体阶段。
    assert tally.phase() == "downloading"


def test_percent_is_none_when_any_layer_total_is_unknown() -> None:
    tally = LayerTally()
    tally.absorb(layer("Downloading", "aaa", 30, 100))
    tally.absorb(layer("Pulling fs layer", "bbb"))

    assert tally.percent() is None
    assert tally.total_bytes() is None
    assert tally.downloaded_bytes() == 30


def test_cached_layers_do_not_poison_percent() -> None:
    """`Already exists` 的层永远不带 total —— 它的真值是"这一层零字节要下"，不是未知。"""
    tally = LayerTally()
    tally.absorb(layer("Already exists", "aaa"))
    tally.absorb(layer("Downloading", "bbb", 50, 100))

    assert tally.percent() == pytest.approx(0.5)
    assert tally.total_bytes() == 100


# =============================================================================
# 4. 节流
# =============================================================================
@pytest.mark.parametrize(
    ("previous", "candidate", "elapsed_s", "expected"),
    [
        # 阶段变化：立即发
        (a_state(phase="queued"), a_state(phase="downloading"), 0.0, True),
        # 一点点字节 + 刚发过：不发
        (a_state(downloaded_bytes=10), a_state(downloaded_bytes=11), 0.01, False),
        # 距上一帧够久：发
        (a_state(downloaded_bytes=10), a_state(downloaded_bytes=11), 0.30, True),
        # percent 跨过 1%：发
        (a_state(percent=0.10), a_state(percent=0.12), 0.01, True),
        # 什么都没变：即使过了很久也不发
        (a_state(), a_state(), 10.0, False),
        # percent 从未知变已知：发（那是一条真信息）
        (a_state(percent=None), a_state(percent=0.10), 0.01, True),
    ],
)
def test_should_publish(
    previous: PullState, candidate: PullState, elapsed_s: float, expected: bool
) -> None:
    assert should_publish(previous, candidate, elapsed_s=elapsed_s) is expected


def test_high_frequency_lines_are_throttled() -> None:
    """6 行进度只发 4 帧：起始 + 两次阶段变化 + 终态。时钟冻结，所以时间那一条不触发。"""
    stream = feed(
        layer("Pulling fs layer", "aaa"),
        layer("Downloading", "aaa", 100, 100_000),
        layer("Downloading", "aaa", 101, 100_000),
        layer("Downloading", "aaa", 102, 100_000),
        layer("Downloading", "aaa", 103, 100_000),
        layer("Downloading", "aaa", 104, 100_000),
    )
    puller, _transport, _clock = make_puller(stream=stream)

    async def scenario() -> None:
        await puller.start(REF)
        await finish(puller)

    asyncio.run(scenario())

    assert puller.version == 4
    assert puller.state.status == "done"
    assert puller.current_frame().type == "pull.done"


# =============================================================================
# 5. 幂等 / 已在本地
# =============================================================================
def test_already_present_starts_nothing() -> None:
    puller, transport, _clock = make_puller(present=True)

    async def scenario() -> tuple[PullState, bool]:
        return await puller.start(REF)

    state, started = asyncio.run(scenario())

    assert started is False, "什么都没拉，不许报告「起了一次拉取」"
    assert state.status == "done"
    assert state.phase == ALREADY_PRESENT_PHASE
    assert state.epoch == 0, "什么都没拉，epoch 不许动"
    assert all("/images/create" not in path for path in transport.paths())


def test_second_start_does_not_launch_a_second_pull() -> None:
    release = threading.Event()

    def blocking(_call: object, on_line: OnLine) -> int:
        on_line(layer("Downloading", "aaa", 1, 10))
        release.wait(5)
        return 200

    puller, transport, _clock = make_puller(stream=blocking)

    async def scenario() -> tuple[tuple[PullState, bool], tuple[PullState, bool]] | None:
        first = await puller.start(REF)
        second = await puller.start(REF)
        try:
            return first, second
        finally:
            release.set()
            await finish(puller)

    result = asyncio.run(scenario())
    assert result is not None
    (first, first_started), (second, second_started) = result

    assert first.epoch == 1
    assert second.epoch == 1
    assert [first_started, second_started] == [True, False]
    assert [p for p in transport.paths() if "/images/create" in p] == [
        "/images/create?fromImage=ghcr.io%2Fusestrix%2Fstrix-sandbox&tag=1.3.0"
    ]


def test_two_simultaneous_starts_launch_only_one_pull() -> None:
    """两个 POST **同时**到达（`gather`，不是先后 `await`）。

    上一条测试在没有那把锁的时候**也是绿的** —— 它的两次 `start()` 是串行的，而窗口
    在 `is_pulling()` 与 `create_task()` 之间那一次 await（查镜像在不在本地）上：
    没有锁时两边都穿过判据，起两次几 GB 的拉取、写两条审计，而 `self._task` 只留得下
    后一个（前一个成孤儿，仍在往状态里写）。这条是收货时实测出来的。
    """
    release = threading.Event()

    def blocking(_call: object, _on_line: OnLine) -> int:
        release.wait(5)
        return 200

    puller, transport, _clock = make_puller(stream=blocking)

    async def scenario() -> list[tuple[PullState, bool]]:
        try:
            return list(await asyncio.gather(puller.start(REF), puller.start(REF)))
        finally:
            release.set()
            await finish(puller)

    outcomes = asyncio.run(scenario())

    assert [p for p in transport.paths() if "/images/create" in p] == [
        "/images/create?fromImage=ghcr.io%2Fusestrix%2Fstrix-sandbox&tag=1.3.0"
    ]
    assert puller.state.epoch == 1, "epoch 每 +1 就是一次拉取，同时来两个也只许 +1"
    # 审计正是靠这个布尔值决定写不写的：同时来两个也只许有一个报告"我起了一次"。
    assert [started for _state, started in outcomes].count(True) == 1


def test_snapshot_reuses_the_current_seq() -> None:
    """快照不是新事件。给它一个新号会让订阅者的"我漏帧了吗"判断出错。"""
    puller, _transport, _clock = make_puller(stream=feed(layer("Pull complete", "aaa")))

    async def scenario() -> None:
        await puller.start(REF)
        await finish(puller)

    asyncio.run(scenario())

    frame = puller.current_frame()
    assert frame is not None
    assert puller.snapshot_frame().seq == frame.seq
    assert puller.snapshot_frame().seq == frame.seq, "连着取两次也不许递增"


# =============================================================================
# 6. 传输层故障 → error 帧带 reason
# =============================================================================
@pytest.mark.parametrize("reason", ["socket_missing", "timeout"])
def test_transport_failure_becomes_an_error_frame(reason: str) -> None:
    puller, _transport, _clock = make_puller(stream=raises(DockerApiError(reason, "…")))

    async def scenario() -> None:
        await puller.start(REF)
        await finish(puller)

    asyncio.run(scenario())

    assert puller.state.status == "error"
    assert puller.state.code == PULL_FAILURE_CODE
    assert puller.state.reason == reason


# =============================================================================
# 7. 停机
# =============================================================================
def test_shutdown_cancels_the_pull_before_returning() -> None:
    """断言的是**顺序**：`shutdown()` 返回时任务已经结束了。

    刻意不断言"最后 task.cancelled() 为真" —— `asyncio.run` 收尾本来就会取消剩下的
    任务，那种断言恒成立（T11a 第一版正是栽在这里）。
    """
    release = threading.Event()
    observed: list[bool] = []

    def blocking(_call: object, on_line: OnLine) -> int:
        on_line(layer("Downloading", "aaa", 1, 10))
        release.wait(5)
        return 200

    puller, _transport, _clock = make_puller(stream=blocking)

    async def scenario() -> None:
        await puller.start(REF)
        # 让后台任务真的进到 to_thread 里。
        await asyncio.sleep(0.05)
        try:
            await puller.shutdown()
            observed.append(puller.is_pulling())
        finally:
            release.set()
        # 被取消之后不许再有人往状态里写东西。
        version_after_cancel = puller.version
        await asyncio.sleep(0.05)
        assert puller.version == version_after_cancel

    asyncio.run(scenario())

    assert observed == [False], "shutdown() 返回时拉取任务必须已经结束"
    assert puller.state.status == "pulling", "取消不是终态，不许伪造 done/error"
