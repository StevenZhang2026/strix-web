"""`Reaper`：按 label 回收泄漏的沙箱容器（T11a）。

这里没有真实 Docker（CLAUDE.md §测试），接缝仍然是最低层的 `DockerTransport`：
被替身掉的只是"字节怎么送到 daemon"，而选择器的三级判据、URL 拼装、状态码归类
**全都真的被执行了**。`FakeTransport` 在没有匹配路由时抛 `AssertionError`，所以
"dry-run 一个 DELETE 都没发"这类断言是**机械**成立的，不靠人读代码。

`asyncio.run(scenario())` 而不是 `async def test_`：本仓刻意没有 `pytest-asyncio`。
每个等待都套 `wait_for` —— 没有超时的死锁测试会挂住整个测试集。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import Database
from app.main import create_app
from app.services import reaper as reaper_module
from app.services.docker_probe import (
    ORPHAN_REQUIRED_LABEL,
    DockerApiError,
    DockerProbe,
    DockerReply,
    OrphanSandbox,
)
from app.services.reaper import REMOVE_TIMEOUT_S, Reaper, SweepReport, plan_sweep
from app.settings import Settings
from tests.conftest import FakeTransport, Handler, RecordedCall, const, reply

LEAKED_ID = "aa" * 32
RUNNING_ID = "bb" * 32


def item(
    name: str, *, container_id: str | None = None, run_id: str | None = "scan-1"
) -> dict[str, object]:
    """一条 `GET /containers/json` 的元素。`run_id=None` = 没有 `strix-run-id` 这个 label。"""
    labels: dict[str, str] = {"strix-run-type": "console"}
    if run_id is not None:
        labels[ORPHAN_REQUIRED_LABEL] = run_id
    body: dict[str, object] = {"Names": [f"/{name}"], "Labels": labels}
    if container_id is not None:
        body["Id"] = container_id
    return body


def listing(*items: dict[str, object]) -> tuple[str, str, Handler]:
    return ("GET", "/containers/json?", const(200, list(items)))


def deletes(transport: FakeTransport) -> list[RecordedCall]:
    return [call for call in transport.calls if call.method == "DELETE"]


async def wait_until(predicate: Callable[[], bool], limit: float = 5.0) -> None:
    """轮询等一件由**工作线程**做完的事（清扫的 docker IO 在 `to_thread` 里）。

    这里没有可 await 的 asyncio 事件 —— 跨线程 set 一个 `asyncio.Event` 才是错的。
    """
    deadline = time.monotonic() + limit
    while not predicate():
        assert time.monotonic() < deadline, "等的那件事一直没发生"
        await asyncio.sleep(0.01)


def sweep(transport: FakeTransport, *active: str, dry_run: bool = False) -> SweepReport:
    reaper = Reaper(transport, lambda: active)

    async def scenario() -> SweepReport:
        return await asyncio.wait_for(reaper.sweep(dry_run=dry_run), timeout=10)

    return asyncio.run(scenario())


# =============================================================================
# A. `orphan_sandboxes()`：`orphan_sandbox_names()` 的底座
# =============================================================================
def test_orphan_sandboxes_carries_id_name_and_run_id() -> None:
    transport = FakeTransport([listing(item("strix-sandbox-x", container_id=LEAKED_ID))])
    assert DockerProbe(transport=transport).orphan_sandboxes() == (
        OrphanSandbox(container_id=LEAKED_ID, name="strix-sandbox-x", run_id="scan-1"),
    )


def test_orphan_sandbox_without_an_id_field_falls_back_to_its_name() -> None:
    """`Id` 缺失时用名字当删除地址 —— docker 的 `/containers/{id}` 也接受名字。

    丢弃它才是错的：它**是**一条真残骸（两个 label 都在），丢弃会让它永远不被回收，
    还会让 `/api/system/status` 的孤儿计数少报。
    """
    transport = FakeTransport([listing(item("strix-sandbox-noid"))])
    assert DockerProbe(transport=transport).orphan_sandboxes() == (
        OrphanSandbox(
            container_id="strix-sandbox-noid", name="strix-sandbox-noid", run_id="scan-1"
        ),
    )


def test_orphan_sandbox_names_is_a_thin_wrapper() -> None:
    """名字视图必须与结构化视图同源 —— 过滤逻辑只许有一处。"""
    transport = FakeTransport([listing(item("a", container_id=LEAKED_ID), item("b"))])
    assert DockerProbe(transport=transport).orphan_sandbox_names() == ("a", "b")


# =============================================================================
# B. 三级判据（纯函数 + 端到端各一次）
# =============================================================================
def test_plan_sweep_is_a_pure_split_on_active_run_ids() -> None:
    leaked = OrphanSandbox(container_id=LEAKED_ID, name="leaked", run_id="scan-old")
    live = OrphanSandbox(container_id=RUNNING_ID, name="live", run_id="scan-now")
    plan = plan_sweep((leaked, live), frozenset({"scan-now"}))
    assert plan.doomed == (leaked,)
    assert plan.spared == (live,)


def test_m0_juice_shop_is_neither_doomed_nor_deleted() -> None:
    """靶场只有 `strix-run-type=console`、没有 `strix-run-id` —— 本机实测过的假阳性。

    daemon 侧的过滤器**一定**会把它捞回来（它那个 label 是我们自己手打的），所以
    "不删靶场"只能由客户端第二级判据保证。删它是不可逆的。
    """
    transport = FakeTransport(
        [
            listing(
                {
                    "Names": ["/m0-juice-shop"],
                    "Labels": {"strix-run-type": "console", "strix-console-role": "m0-target"},
                },
                item("strix-sandbox-leaked", container_id=LEAKED_ID),
            ),
            ("DELETE", "/containers/", const(204, {})),
        ]
    )
    report = sweep(transport)
    assert [sandbox.name for sandbox in report.plan.doomed] == ["strix-sandbox-leaked"]
    assert report.plan.spared == ()
    assert [call.path for call in deletes(transport)] == [f"/containers/{LEAKED_ID}?force=1&v=1"]


def test_sandbox_of_a_running_scan_is_spared_with_zero_deletes() -> None:
    """`STRIX_RUN_ID == scan_id`：在册扫描的沙箱一个都不许删。

    少了这一级，定时清扫会在扫描跑到一半时把它自己的沙箱删掉。
    """
    transport = FakeTransport([listing(item("live", container_id=RUNNING_ID, run_id="scan-now"))])
    report = sweep(transport, "scan-now")
    assert report.plan.doomed == ()
    assert [sandbox.name for sandbox in report.plan.spared] == ["live"]
    assert deletes(transport) == []
    assert report.removed == () and report.failed == ()


def test_active_scan_ids_is_snapshotted_on_the_event_loop_thread() -> None:
    """`active` 必须在异步侧快照 —— 让工作线程去读事件循环的那个 dict 是竞态。"""
    threads: list[int] = []
    transport = FakeTransport([listing()])

    def active_scan_ids() -> tuple[str, ...]:
        threads.append(threading.get_ident())
        return ()

    reaper = Reaper(transport, active_scan_ids)

    async def scenario() -> int:
        await asyncio.wait_for(reaper.sweep(), timeout=10)
        return threading.get_ident()

    loop_thread = asyncio.run(scenario())
    assert threads == [loop_thread]


# =============================================================================
# C. dry-run 与删除应答
# =============================================================================
def test_dry_run_plans_everything_and_sends_zero_deletes() -> None:
    transport = FakeTransport([listing(item("leaked", container_id=LEAKED_ID))])
    report = sweep(transport, dry_run=True)
    assert report.dry_run is True
    assert [sandbox.name for sandbox in report.plan.doomed] == ["leaked"]
    assert report.removed == () and report.failed == ()
    assert deletes(transport) == []


def test_sweep_plan_is_logged_before_anything_is_deleted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """破坏性操作必须先可观测。字段名用 `strix_run_id`，与强杀时那行 warning 对得上。"""
    transport = FakeTransport(
        [
            listing(
                item("leaked", container_id=LEAKED_ID),
                item("live", container_id=RUNNING_ID, run_id="scan-now"),
            ),
            ("DELETE", "/containers/", const(204, {})),
        ]
    )
    with caplog.at_level("INFO", logger=reaper_module.logger.name):
        sweep(transport, "scan-now")
    records = [r for r in caplog.records if getattr(r, "event", None) == "reaper_sweep_plan"]
    assert len(records) == 1
    # 键名不许叫 `name` —— 它是 `LogRecord` 的保留字段，splat 进 `extra` 会 KeyError。
    assert records[0].doomed == [
        {"container_id": LEAKED_ID, "container_name": "leaked", "strix_run_id": "scan-1"}
    ]
    assert records[0].spared == [
        {"container_id": RUNNING_ID, "container_name": "live", "strix_run_id": "scan-now"}
    ]


def test_delete_replies_are_classified_and_one_failure_never_aborts_the_sweep() -> None:
    """204 成功 / 404 也算成功（幂等）/ 409 跳过 / 其余算失败，**且后面那个照删**。"""
    names = ("ok", "gone", "busy", "boom", "after")
    ids = {name: f"{index}{'0' * 63}" for index, name in enumerate(names)}
    codes = {"ok": 204, "gone": 404, "busy": 409, "boom": 500, "after": 204}

    def handler(call: RecordedCall) -> DockerReply:
        for name, container_id in ids.items():
            if container_id in call.path:
                return reply(codes[name], {})
        raise AssertionError(call.path)  # pragma: no cover

    transport = FakeTransport(
        [
            listing(*(item(name, container_id=ids[name]) for name in names)),
            ("DELETE", "/containers/", handler),
        ]
    )
    report = sweep(transport)
    assert report.removed == (ids["ok"], ids["gone"], ids["after"])
    assert report.failed == (ids["boom"],)
    assert len(deletes(transport)) == 5
    assert deletes(transport)[0].timeout_s == REMOVE_TIMEOUT_S


# =============================================================================
# D. `run_forever`
# =============================================================================
def test_listing_failure_raises_but_run_forever_lives_into_the_next_round() -> None:
    """docker 挂了不许让 `api` 变成半死状态：记日志、等下一轮。"""
    transport = FakeTransport([("GET", "/containers/json?", const(500, {"message": "nope"}))])
    with pytest.raises(DockerApiError) as excinfo:
        sweep(transport)
    assert excinfo.value.reason == "bad_status"

    rounds: list[int] = []

    def always_failing(_call: RecordedCall) -> DockerReply:
        rounds.append(1)
        return reply(500, {"message": "nope"})

    reaper = Reaper(FakeTransport([("GET", "/containers/json?", always_failing)]), lambda: ())

    async def scenario() -> None:
        task = asyncio.create_task(reaper.run_forever(interval=0.01))
        await wait_until(lambda: len(rounds) >= 3)  # 三轮 = 它真的从失败里回到了循环顶部
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(scenario(), timeout=20))


def test_request_sweep_wakes_run_forever_before_the_interval_is_due() -> None:
    """扫描结束后要立刻清扫一次，不能等到 5 分钟后的那一轮。"""
    sweeps: list[int] = []

    def counting(_call: RecordedCall) -> DockerReply:
        sweeps.append(1)
        return reply(200, [])

    reaper = Reaper(FakeTransport([("GET", "/containers/json?", counting)]), lambda: ())

    async def scenario() -> None:
        # 间隔取 1000 秒：醒不过来的话是 `wait_for` 超时失败，不是慢慢地通过。
        task = asyncio.create_task(reaper.run_forever(interval=1000.0))
        await wait_until(lambda: bool(sweeps))  # 启动的那一轮
        reaper.request_sweep()
        await wait_until(lambda: len(sweeps) >= 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(scenario(), timeout=10))


def test_request_sweep_is_sync_and_needs_no_running_loop() -> None:
    """它由 `ScanSupervisor._forget` 这个**同步**回调调用 —— 只许 set 一个 Event。"""
    transport = FakeTransport([listing()])
    reaper = Reaper(transport, lambda: ())
    reaper.request_sweep()  # 循环还没起，也不许炸
    assert transport.calls == []


# =============================================================================
# E. lifespan 接线
# =============================================================================
@pytest.fixture
def wired_transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeTransport]:
    """把 lifespan 里的 `UnixSocketTransport()` 换成替身 —— 单测不碰真实 docker.sock。

    容器列表刻意是空的：这条测试要证明的是"清扫真的跑了一轮"，删除本身由 C 组测。
    """
    transport = FakeTransport([listing()])
    monkeypatch.setattr("app.main.UnixSocketTransport", lambda: transport)
    yield transport


def test_lifespan_sweeps_once_at_startup(app: FastAPI, wired_transport: FakeTransport) -> None:
    """`create_task(reaper.run_forever())` 真的被建了 —— 只挂在 `app.state` 上不算接线。"""
    with TestClient(app, base_url="https://testserver") as client:
        assert isinstance(app.state.reaper, Reaper)
        for _ in range(200):
            if wired_transport.calls:
                break
            client.get("/api/health")  # 把控制权交回那个循环所在的事件循环
            time.sleep(0.02)
    assert [call.method for call in wired_transport.calls] == ["GET"], "启动后一轮清扫都没发生"


class _RecordingReaper:
    """记下"我是在 lifespan 的 `finally` 里被 cancel 的"。刻意不继承 `Reaper`。

    **只断言"最后 cancelled 为真"是不够的**（实测：删掉那两行后测试照绿）——
    `asyncio.run` 收尾时会把剩下的任务统统 cancel 并 await 一遍，所以"被取消过"这件事
    无论如何都会发生。有区别的是**时机**：我们的 `finally` 在 `db.close()` **之前**取消，
    而事件循环收尾发生在它之后。所以这里记的是顺序。
    """

    events: ClassVar[list[str]] = []
    instances: ClassVar[list[_RecordingReaper]] = []

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        _RecordingReaper.instances.append(self)

    def request_sweep(self) -> None:
        """`ScanSupervisor` 要拿这个回调 —— 录音机也得有它，接线才成立。"""

    async def run_forever(self) -> None:
        try:
            await asyncio.Event().wait()  # 永远等 —— 只有 cancel 能结束它
        except asyncio.CancelledError:
            _RecordingReaper.events.append("cancelled")
            raise


def test_lifespan_cancels_and_awaits_the_reaper_task(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`finally` 里必须 `cancel()` **并 await**，而且要在 `db.close()` 之前做完。

    只 cancel 不 await 的话，"cancelled" 那条记录会晚到事件循环收尾时才出现，
    也就是排在 `db_closed` 后面 —— 所以这一条断言同时盯着那两行。
    """
    _RecordingReaper.events.clear()
    _RecordingReaper.instances.clear()
    original_close = Database.close

    def recording_close(self: Database) -> None:
        _RecordingReaper.events.append("db_closed")
        original_close(self)

    monkeypatch.setattr(Database, "close", recording_close)
    monkeypatch.setattr("app.main.Reaper", _RecordingReaper)
    monkeypatch.setattr("app.main.UnixSocketTransport", lambda: FakeTransport([]))
    with TestClient(app, base_url="https://testserver"):
        pass
    assert len(_RecordingReaper.instances) == 1
    assert _RecordingReaper.events == ["cancelled", "db_closed"]


def test_reaper_construction_never_talks_to_docker(tmp_path: Path) -> None:
    """构造不许有 docker 往返：docker 挂着也不该影响 `api` 启动（T3 立下的约束）。"""
    create_app(Settings(console_data_dir=tmp_path))  # 建 app 也不许有往返
    transport = FakeTransport([])  # 没有任何路由 → 任何请求都会 AssertionError
    Reaper(transport, lambda: ())
    assert transport.calls == []
