"""`POST /api/scans` 与 `POST /api/scans/{id}/stop` —— **只测路由层多出来的那一层接线**。

判定逻辑各有自己的测试文件（`test_target_guard.py` / `test_allowlist.py` /
`test_scan_admission.py` / `test_scan_launcher.py`），这里不重测。这个文件回答的问题是：
那些零件真的被**接上线**了吗 —— 并发闸真的读了配置、`register`/`forget` 真的被调用、
三条 INSERT 真的在同一个事务里、`plan.argv_preview` 真的落进了 DB 而 `plan.env` 没有。

# 替身只有两个：supervisor 与 DNS

`build_launch_plan` 用**真的**：黄金 argv 在 T9 已经测过，这里要断言的是"它真的被调用了、
它的产物真的落进了 DB"。用假的就把这两句话都测空了。

# 假子进程为什么用 `threading.Event` 而不是 `asyncio.Future`

`TestClient` 的事件循环跑在**另一个线程**里，测试线程造出来的 Future 属于错误的循环。
`await asyncio.to_thread(event.wait)` 两侧都成立：测试线程 `set()`，循环线程被唤醒。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.db import Database
from app.main import create_app
from app.services.allowlist import AllowlistConfig, AllowlistStore
from app.services.dns_resolver import Resolution, ResolutionError, Resolver
from app.services.key_vault import IDLE_TTL_SECONDS, CredentialSet, KeyVault
from app.services.scan_launcher import LaunchPlan
from app.services.scan_supervisor import ScanOutcome
from app.services.target_guard import AllowlistMode
from app.settings import Settings
from tests.conftest import (
    PASSWORD,
    USERNAME,
    FakeClock,
    insert_authorization,
    insert_scan,
    make_entry,
)

_FIRST_ADDRESS = "93.184.216.34"
_SECOND_ADDRESS = "93.184.216.35"
_API_KEY = "sk-ant-this-is-the-model-key"
_TEST_PASSWORD = "hunter2-hunter2"
_AFFIRMED = [
    "owns_or_authorized",
    "not_third_party_production",
    "understands_real_attacks",
]


# =============================================================================
# 替身
# =============================================================================
def _outcome(**overrides: object) -> ScanOutcome:
    fields: dict[str, object] = {
        "status": "completed",
        "exit_code": 0,
        "exit_meaning": "no_vulnerabilities_found",
        "error_code": None,
        "error_message": None,
        "run_status": "finished",
        "run_dir": None,
        "strix_run_name": None,
    }
    fields.update(overrides)
    return ScanOutcome(**fields)  # type: ignore[arg-type]


class FakeProcess:
    """`ScanProcess` 的替身。`wait()` 一直阻塞到测试说"它结束了"。"""

    def __init__(self, scan_id: str, pid: int = 4321) -> None:
        self.scan_id = scan_id
        self.pid = pid
        self.stop_calls: list[bool] = []
        self._done = threading.Event()
        self._outcome = _outcome()

    @property
    def finished(self) -> bool:
        return self._done.is_set()

    def finish(self, outcome: ScanOutcome | None = None) -> None:
        if outcome is not None:
            self._outcome = outcome
        self._done.set()

    async def wait(self) -> ScanOutcome:
        await asyncio.to_thread(self._done.wait)
        return self._outcome

    async def stop(self, *, force: bool = False) -> None:
        self.stop_calls.append(force)
        # 真进程被停掉之后会退出，所以这里也让 `wait()` 醒过来 —— 否则停机 drain 会挂住。
        self.finish(
            _outcome(
                status="stopped",
                exit_code=143,
                exit_meaning="failed",
                error_code="stopped_by_operator",
                error_message="stopped by operator",
            )
        )


class FakeSupervisor:
    """`ScanSupervisor` 的替身。**不起进程**，只记下 plan 并交回一个假进程。"""

    def __init__(self) -> None:
        self.processes: dict[str, FakeProcess] = {}
        self.plans: list[LaunchPlan] = []
        self.start_error: Exception | None = None

    async def start(self, scan_id: str, plan: LaunchPlan) -> FakeProcess:
        if self.start_error is not None:
            raise self.start_error
        self.plans.append(plan)
        process = FakeProcess(scan_id)
        self.processes[scan_id] = process
        return process

    def get(self, scan_id: str) -> FakeProcess | None:
        return self.processes.get(scan_id)

    def active_scan_ids(self) -> tuple[str, ...]:
        return tuple(scan_id for scan_id, process in self.processes.items() if not process.finished)

    async def shutdown(self) -> None:
        for process in self.processes.values():
            process.finish()


# =============================================================================
# 夹具
# =============================================================================
@pytest.fixture
def max_concurrent() -> int:
    """槽位数。要测"槽位数取自配置"的用例用 `parametrize` 覆盖它。"""
    return 1


@pytest.fixture
def settings(tmp_path: Path, max_concurrent: int) -> Settings:
    return Settings(
        console_data_dir=tmp_path,
        strix_image="strix-sandbox:test",
        strix_docker_sandbox_network="strix_sandbox",
        console_max_concurrent_scans=max_concurrent,
    )


@pytest.fixture
def allowlist_file(settings: Settings) -> Path:
    """一份 enforce 模式的清单。没有它，公网目标一律 `not_in_allowlist`（失败关闭）。"""
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    AllowlistStore(settings.allowlist_path).write(
        AllowlistConfig(
            mode=AllowlistMode.ENFORCE,
            entries=(make_entry(hosts=("example.com", "other.example.com")),),
        )
    )
    return settings.allowlist_path


@pytest.fixture
def app(
    settings: Settings, auth_file: Path, allowlist_file: Path, restore_logging: None
) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def supervisor() -> FakeSupervisor:
    return FakeSupervisor()


@pytest.fixture
def resolutions() -> dict[str, Resolution]:
    """host → 本次解析结果。测试可以改它来制造 `dns_changed`。"""
    return {
        "example.com": Resolution(addresses=(_FIRST_ADDRESS,), error=None),
        "other.example.com": Resolution(addresses=(_SECOND_ADDRESS,), error=None),
    }


def _resolver(resolutions: dict[str, Resolution]) -> Resolver:
    async def resolve(host: str) -> Resolution:
        return resolutions.get(host, Resolution(addresses=(), error=ResolutionError.NOT_FOUND))

    return resolve


@contextmanager
def _live_client(
    app: FastAPI, supervisor: FakeSupervisor, resolutions: dict[str, Resolution]
) -> Iterator[TestClient]:
    """进过 `with` 的客户端，替身在 lifespan 之后塞进 `app.state`（在之前塞会被盖掉）。"""
    with TestClient(app, base_url="https://testserver") as test_client:
        app.state.supervisor = supervisor
        app.state.dns_resolver = _resolver(resolutions)
        yield test_client


@pytest.fixture
def anonymous(
    app: FastAPI, supervisor: FakeSupervisor, resolutions: dict[str, Resolution]
) -> Iterator[TestClient]:
    with _live_client(app, supervisor, resolutions) as test_client:
        yield test_client


# =============================================================================
# 助手
# =============================================================================
def _store_credentials(app: FastAPI) -> str:
    """现造一个 vault handle。**明文只在这里出现一次**，其它断言都是"它不该出现在哪"。"""
    handle: str = app.state.key_vault.store(
        CredentialSet(
            provider="anthropic",
            auth_shape="single",
            strix_llm="anthropic/claude-sonnet-4-5",
            api_base=None,
            secrets={"LLM_API_KEY": SecretStr(_API_KEY)},
            params={},
        )
    )
    return handle


def _authorization(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "operator_name": "测试操作者",
        "authorization_ref": "TICKET-1",
        "typed_confirmation": "example.com",
        "affirmed": list(_AFFIRMED),
        "resolved_ips_seen": {"example.com": [_FIRST_ADDRESS]},
    }
    body.update(overrides)
    return body


def _payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "vault_handle": "will-be-replaced",
        "template_id": "quick_triage",
        "targets": ["https://example.com"],
        "max_budget_usd": 2.0,
        "max_turns": 20,
        "authorization": _authorization(),
    }
    body.update(overrides)
    return body


def _launch(client: TestClient, app: FastAPI, **overrides: object) -> dict[str, object]:
    handle = _store_credentials(app)
    response = client.post("/api/scans", json=_payload(vault_handle=handle, **overrides))
    assert response.status_code == 202, response.text
    accepted: dict[str, object] = response.json()
    return accepted


def _rows(settings: Settings, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    """从**另一个连接**读库。

    刻意不复用 `app.state.db` 的连接：那一个属于事件循环所在的线程，从测试线程用它就是
    在测试里制造一个数据竞争。WAL 下另开一个只读连接看得到已提交的数据。
    """
    connection = sqlite3.connect(settings.db_path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("等超时了：后台任务没有做到期望的状态")


def _wait_for_status(settings: Settings, scan_id: str, status: str) -> sqlite3.Row:
    _wait_until(
        lambda: bool(
            _rows(
                settings, "SELECT status FROM scans WHERE id = ? AND status = ?", (scan_id, status)
            )
        )
    )
    return _rows(settings, "SELECT * FROM scans WHERE id = ?", (scan_id,))[0]


def _mirror_lines(settings: Settings) -> list[str]:
    """审计 ndjson 镜像的所有行（月份文件按名排序，行内保序）。"""
    return [
        line
        for path in sorted(settings.audit_dir.glob("*.ndjson"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _database_text(settings: Settings) -> str:
    """整个库的文本形态。用来做"某个明文一个字节都没进去"这种全盘断言。"""
    connection = sqlite3.connect(settings.db_path)
    try:
        tables = [
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        return "\n".join(
            str(tuple(row))
            for table in tables
            for row in connection.execute(f"SELECT * FROM {table}")  # noqa: S608
        )
    finally:
        connection.close()


# =============================================================================
# 1 目标被拒 → 有审计、有 host
# =============================================================================
def test_dns_change_is_audited_with_the_host(
    client: TestClient, app: FastAPI, settings: Settings, resolutions: dict[str, Resolution]
) -> None:
    resolutions["example.com"] = Resolution(addresses=("203.0.113.5",), error=None)
    handle = _store_credentials(app)

    response = client.post("/api/scans", json=_payload(vault_handle=handle))

    assert response.status_code == 409
    assert response.json()["code"] == "dns_changed"
    rows = _rows(
        settings,
        "SELECT detail_json FROM audit_log WHERE event = 'target.dns_changed'",
    )
    assert len(rows) == 1
    assert json.loads(rows[0]["detail_json"])["host"] == "example.com"


# =============================================================================
# 2 / 3 测试账号口令进脱敏集合，扫描结束后被忘掉
# =============================================================================
def test_test_credentials_are_registered_for_redaction(client: TestClient, app: FastAPI) -> None:
    _launch(
        client,
        app,
        credentials=[{"role": "user", "username": "alice", "password": _TEST_PASSWORD}],
    )

    assert app.state.scan_secrets.count() == 1
    assert _TEST_PASSWORD in app.state.scan_secrets.secret_values()


def test_credentials_are_forgotten_when_the_scan_ends(
    client: TestClient, app: FastAPI, settings: Settings
) -> None:
    """收尾要 `forget` 口令，**也要** `release` 凭据引用。

    `release` 那一半断言的是**后果**而不是"函数被调过"：`ref_count > 0` 期间 idle TTL
    不生效（`key_vault.py` 的判定表），所以漏掉那次 release，这组凭据就永远不会过期 ——
    "Key 只活在内存里、会自己过期"这条承诺随之作废。为此换一份注入 `FakeClock` 的
    vault，真实时钟没法在测试里推 8 小时。

    换掉 `app.state.key_vault` 只影响脱敏集合里的 **Key**（`Redactor` 绑的是 lifespan
    里那个实例，见 `main.py:352`），本用例不断言日志；口令那一半仍走真的 `scan_secrets`。
    """
    clock = FakeClock()
    app.state.key_vault = KeyVault(clock=clock)
    handle = _store_credentials(app)

    response = client.post(
        "/api/scans",
        json=_payload(
            vault_handle=handle,
            credentials=[{"role": "user", "username": "alice", "password": _TEST_PASSWORD}],
        ),
    )
    assert response.status_code == 202, response.text
    scan_id = str(response.json()["scan_id"])
    process = app.state.supervisor.get(scan_id)
    assert process is not None

    process.finish()

    _wait_for_status(settings, scan_id, "completed")
    # 等后台任务**整个**跑完：状态到终态只说明 try 里最后一步过了，`finally` 还没跑。
    # 任务在协程彻底结束后才被 `add_done_callback(discard)` 摘出集合。
    _wait_until(lambda: not app.state.scan_tasks)

    assert app.state.scan_secrets.count() == 0
    clock.advance(IDLE_TTL_SECONDS + 1)
    assert app.state.key_vault.get(handle) is None


# =============================================================================
# 4 / 5 并发闸
# =============================================================================
def test_second_scan_is_refused_while_one_is_active(client: TestClient, app: FastAPI) -> None:
    _launch(client, app)
    handle = _store_credentials(app)

    response = client.post("/api/scans", json=_payload(vault_handle=handle))

    assert response.status_code == 409
    assert response.json()["code"] == "concurrency_limit"
    assert response.json()["params"] == {"limit": 1, "active": 1}


@pytest.mark.parametrize("max_concurrent", [2])
def test_slot_count_comes_from_settings(client: TestClient, app: FastAPI) -> None:
    """槽位数**不许在代码里写死 1**。两个都进得去，第三个才被挡。"""
    _launch(client, app)
    _launch(client, app)

    handle = _store_credentials(app)
    third = client.post("/api/scans", json=_payload(vault_handle=handle))

    assert len(app.state.supervisor.active_scan_ids()) == 2
    assert third.status_code == 409


# =============================================================================
# 6 一个事务：三条记录一起成立，或者一条都不成立
# =============================================================================
def test_launch_writes_authorization_scan_and_audit(
    client: TestClient, app: FastAPI, settings: Settings
) -> None:
    accepted = _launch(client, app)
    scan_id = str(accepted["scan_id"])

    scans = _rows(settings, "SELECT * FROM scans WHERE id = ?", (scan_id,))
    assert len(scans) == 1
    scan = scans[0]
    authorizations = _rows(
        settings, "SELECT * FROM authorizations WHERE id = ?", (scan["authorization_id"],)
    )
    assert len(authorizations) == 1
    authorization = authorizations[0]

    assert json.loads(authorization["affirmations_json"]) == _AFFIRMED
    assert json.loads(authorization["overrides_json"]) == {
        "allow_loopback": False,
        "allow_private": False,
        "multi_target_affirmed": False,
    }
    assert json.loads(authorization["resolved_ips_json"]) == {"example.com": [_FIRST_ADDRESS]}
    assert json.loads(authorization["targets_json"]) == [
        {"raw": "https://example.com", "url": "https://example.com", "host": "example.com"}
    ]
    assert authorization["allowlist_entry_id"] == "预生产"
    # 快照里存的是**命中条目的完整形状**，不只是 label —— `001_init.sql:64` 要求的就是
    # "当时该条目的完整快照"：白名单 YAML 事后会被改（甚至那条会被删），只留一个 label
    # 的话"当时批准的是什么"就再也问不出来了。
    entry_snapshot = json.loads(authorization["allowlist_snapshot"])[0]
    assert entry_snapshot["matched"] is True
    assert entry_snapshot["entry"] == make_entry(
        hosts=("example.com", "other.example.com")
    ).model_dump(mode="json")
    assert authorization["typed_confirmation"] == "example.com"

    assert scan["status"] == "starting" or scan["status"] == "running"
    assert scan["template_id"] == "quick_triage"
    assert scan["provider"] == "anthropic"
    assert scan["auth_shape"] == "single"
    assert scan["strix_llm"] == "anthropic/claude-sonnet-4-5"
    assert scan["sandbox_image"] == "strix-sandbox:test"
    assert scan["strix_version"] == app.state.strix_version
    assert json.loads(scan["targets_json"]) == ["https://example.com"]
    assert len(scan["instruction_sha256"]) == 64

    events = [row["event"] for row in _rows(settings, "SELECT event FROM audit_log ORDER BY rowid")]
    assert events == ["authorization.affirmed", "scan.launched"]
    affirmed_row = _rows(
        settings,
        "SELECT scan_id, authorization_id, detail_json FROM audit_log "
        "WHERE event = 'authorization.affirmed'",
    )[0]
    assert affirmed_row["scan_id"] == scan_id
    assert affirmed_row["authorization_id"] == scan["authorization_id"]
    assert json.loads(affirmed_row["detail_json"])["categories"] == ["example.com=public"]
    launched_row = _rows(
        settings, "SELECT scan_id, detail_json FROM audit_log WHERE event = 'scan.launched'"
    )[0]
    assert launched_row["scan_id"] == scan_id
    assert "LLM_API_KEY" in json.loads(launched_row["detail_json"])["env_var_names"]

    # 双写的另一半：`audit_log` 表之外还有 `${DATA}/audit/YYYY-MM.ndjson`（CLAUDE.md
    # §测试与日志：DB 丢了也在、可 grep）。`authorization.affirmed` 的镜像是在事务提交
    # **之后**单独一步，很容易漏掉而表那一半照样绿 —— 所以这里两条都点名。
    mirrored = [json.loads(line) for line in _mirror_lines(settings)]
    assert [record["event"] for record in mirrored] == ["authorization.affirmed", "scan.launched"]
    assert {record["scan_id"] for record in mirrored} == {scan_id}


def test_rejected_admission_writes_no_authorization_row(
    client: TestClient, app: FastAPI, settings: Settings, resolutions: dict[str, Resolution]
) -> None:
    resolutions["example.com"] = Resolution(addresses=("203.0.113.5",), error=None)
    handle = _store_credentials(app)

    assert client.post("/api/scans", json=_payload(vault_handle=handle)).status_code == 409

    assert _rows(settings, "SELECT id FROM authorizations") == []
    assert _rows(settings, "SELECT id FROM scans") == []


def test_a_failure_to_spawn_marks_the_row_failed(
    client: TestClient, app: FastAPI, settings: Settings
) -> None:
    """`supervisor.start()` 起不了子进程：那一行**留着**并翻成 `failed`。

    留着是刻意的：它已经带着完整的授权声明与 argv，是"有人试过起这次扫描"的记录。
    `exit_code` / `exit_meaning` 必须是 NULL —— 根本没有进程退出过，编一个 -1 是撒谎。
    `scan.launched` 也不许有：那条审计说的是"它起来了"。
    """
    app.state.supervisor.start_error = OSError("fork 不出来")
    handle = _store_credentials(app)

    # 500 那条响应由 `handle_unexpected` 给出，但 Starlette 的 ServerErrorMiddleware
    # 随后仍会重抛（`main.py:298` 的注释），于是 TestClient 把它交回给测试。
    with pytest.raises(OSError):
        client.post("/api/scans", json=_payload(vault_handle=handle))

    row = _rows(settings, "SELECT * FROM scans")[0]
    assert row["status"] == "failed"
    assert row["error_code"] == "scan_preparation_failed"
    assert row["error_message"]
    assert row["finished_at"] is not None
    assert row["exit_code"] is None
    assert row["exit_meaning"] is None
    assert [event["event"] for event in _rows(settings, "SELECT event FROM audit_log")] == [
        "authorization.affirmed"
    ]
    # 没起起来就不许留下登记的口令（`finally` 那一路）。
    assert app.state.scan_secrets.count() == 0


# =============================================================================
# 7 状态机：starting → running → 终态
# =============================================================================
def test_status_walks_starting_running_terminal(
    client: TestClient, app: FastAPI, settings: Settings
) -> None:
    accepted = _launch(client, app)
    scan_id = str(accepted["scan_id"])
    assert accepted["status"] == "starting"

    running = _wait_for_status(settings, scan_id, "running")
    assert running["pid"] == 4321
    assert running["started_at"] is not None
    assert running["finished_at"] is None

    process = app.state.supervisor.get(scan_id)
    process.finish(
        _outcome(
            status="stopped",
            exit_code=0,
            exit_meaning="no_vulnerabilities_found",
            error_code="scan_incomplete",
            error_message="run.json.status=stopped",
            run_status="stopped",
            run_dir=settings.scans_dir / "strix-run-7",
            strix_run_name="strix-run-7",
        )
    )

    done = _wait_for_status(settings, scan_id, "stopped")
    assert done["exit_code"] == 0
    assert done["exit_meaning"] == "no_vulnerabilities_found"
    assert done["error_code"] == "scan_incomplete"
    assert done["error_message"] == "run.json.status=stopped"
    assert done["strix_run_name"] == "strix-run-7"
    assert done["run_dir"] == str(settings.scans_dir / "strix-run-7")
    assert done["finished_at"] is not None

    # `scan.finished` 是在终态那次 UPDATE **提交之后**才写的（`routes/scans.py` 里
    # `mark_finished` 在前、`audit.record` 在后），所以"状态已是终态"**不是**"审计写完了"
    # 的信号 —— 直接读会随机读到空表（实测：全量跑 13 次红 2 次）。这里要等它自己出现。
    _wait_until(
        lambda: bool(_rows(settings, "SELECT 1 FROM audit_log WHERE event = 'scan.finished'"))
    )
    finished_audit = _rows(
        settings, "SELECT scan_id, detail_json FROM audit_log WHERE event = 'scan.finished'"
    )
    assert len(finished_audit) == 1
    assert finished_audit[0]["scan_id"] == scan_id
    # `run_status` 刻意没有列，只活在审计 detail 里（见 `scans` 的 DDL 注释）。
    assert json.loads(finished_audit[0]["detail_json"])["run_status"] == "stopped"


# =============================================================================
# 8 停止端点
# =============================================================================
@pytest.mark.parametrize(("mode", "force"), [("graceful", False), ("force", True)])
def test_stop_forwards_the_mode_and_audits_the_intent(
    client: TestClient, app: FastAPI, settings: Settings, mode: str, force: bool
) -> None:
    accepted = _launch(client, app)
    scan_id = str(accepted["scan_id"])

    response = client.post(f"/api/scans/{scan_id}/stop", json={"mode": mode})

    assert response.status_code == 202
    assert response.json() == {"scan_id": scan_id, "mode": mode}
    process = app.state.supervisor.get(scan_id)
    _wait_until(lambda: process.stop_calls == [force])
    rows = _rows(
        settings, "SELECT scan_id, detail_json FROM audit_log WHERE event = 'scan.stopped'"
    )
    assert len(rows) == 1
    assert rows[0]["scan_id"] == scan_id
    assert json.loads(rows[0]["detail_json"]) == {"mode": mode}


def test_stop_of_an_unknown_scan_is_not_found(client: TestClient) -> None:
    response = client.post("/api/scans/nope/stop", json={"mode": "graceful"})

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_stop_rejects_a_bogus_mode(client: TestClient) -> None:
    assert client.post("/api/scans/nope/stop", json={"mode": "kill -9"}).status_code == 422


# =============================================================================
# 9 逐字确认串对的是**第一个**目标（路由不许排序/去重 targets）
# =============================================================================
def test_typed_confirmation_is_checked_against_the_first_target(
    client: TestClient, app: FastAPI
) -> None:
    handle = _store_credentials(app)

    response = client.post(
        "/api/scans",
        json=_payload(
            vault_handle=handle,
            targets=["https://example.com", "https://other.example.com"],
            authorization=_authorization(
                typed_confirmation="other.example.com",
                multi_target_affirmed=True,
                resolved_ips_seen={
                    "example.com": [_FIRST_ADDRESS],
                    "other.example.com": [_SECOND_ADDRESS],
                },
            ),
        ),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "missing_typed_confirmation"


# =============================================================================
# 10 Key 卫生
# =============================================================================
def test_no_secret_reaches_the_db_the_audit_or_the_response(
    client: TestClient, app: FastAPI, settings: Settings
) -> None:
    accepted = _launch(
        client,
        app,
        credentials=[{"role": "user", "username": "alice", "password": _TEST_PASSWORD}],
    )
    scan_id = str(accepted["scan_id"])
    scan = _rows(
        settings, "SELECT argv_json, env_var_names_json FROM scans WHERE id = ?", (scan_id,)
    )[0]

    # argv 里必须真有目标（证明落库的确实是 argv 而不是别的东西）……
    assert "https://example.com" in scan["argv_json"]
    # ……而 env 只留**变量名**。
    assert "LLM_API_KEY" in scan["env_var_names_json"]

    haystack = "\n".join([_database_text(settings), *_mirror_lines(settings), json.dumps(accepted)])
    for secret in (_API_KEY, _TEST_PASSWORD):
        assert secret not in haystack


# =============================================================================
# 11 / 12 请求模型挡下来的那些
# =============================================================================
def test_empty_target_list_is_422_not_500(client: TestClient, app: FastAPI) -> None:
    handle = _store_credentials(app)

    response = client.post("/api/scans", json=_payload(vault_handle=handle, targets=[]))

    assert response.status_code == 422


def test_missing_affirmation_is_422(client: TestClient, app: FastAPI) -> None:
    handle = _store_credentials(app)

    response = client.post(
        "/api/scans",
        json=_payload(vault_handle=handle, authorization=_authorization(affirmed=_AFFIRMED[:2])),
    )

    assert response.status_code == 422


def test_short_test_password_is_422(client: TestClient, app: FastAPI) -> None:
    handle = _store_credentials(app)

    response = client.post(
        "/api/scans",
        json=_payload(
            vault_handle=handle,
            credentials=[{"role": "user", "username": "alice", "password": "short"}],
        ),
    )

    assert response.status_code == 422


def test_multi_target_without_the_extra_affirmation_is_refused(
    client: TestClient, app: FastAPI
) -> None:
    handle = _store_credentials(app)

    response = client.post(
        "/api/scans",
        json=_payload(
            vault_handle=handle,
            targets=["https://example.com", "https://other.example.com"],
            authorization=_authorization(
                resolved_ips_seen={
                    "example.com": [_FIRST_ADDRESS],
                    "other.example.com": [_SECOND_ADDRESS],
                }
            ),
        ),
    )

    assert response.status_code == 422
    assert response.json()["params"] == {"field": "authorization.multi_target_affirmed"}


# =============================================================================
# 13 vault / 预算
# =============================================================================
def test_unknown_vault_handle_is_key_required(client: TestClient) -> None:
    response = client.post("/api/scans", json=_payload(vault_handle="not-a-handle"))

    assert response.status_code == 409
    assert response.json()["code"] == "key_required"


def test_budget_over_the_ceiling_bubbles_up(client: TestClient, app: FastAPI) -> None:
    handle = _store_credentials(app)

    response = client.post(
        "/api/scans", json=_payload(vault_handle=handle, max_budget_usd=10_000.0)
    )

    assert response.status_code == 409
    assert response.json()["code"] == "budget_exceeds_ceiling"
    # 没起成扫描就不许留下登记的口令。
    assert app.state.scan_secrets.count() == 0


# =============================================================================
# 14 停机 drain：退出 client 上下文之后不许有行卡在 running
# =============================================================================
def test_pending_scans_are_drained_at_shutdown(
    app: FastAPI,
    supervisor: FakeSupervisor,
    resolutions: dict[str, Resolution],
    settings: Settings,
) -> None:
    with _live_client(app, supervisor, resolutions) as test_client:
        test_client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
        handle = _store_credentials(app)
        response = test_client.post("/api/scans", json=_payload(vault_handle=handle))
        assert response.status_code == 202, response.text

    row = _rows(settings, "SELECT status, finished_at FROM scans")[0]
    assert row["status"] == "completed"
    assert row["finished_at"] is not None


# =============================================================================
# 15 启动时的 6b：残行翻成 interrupted
# =============================================================================
def test_startup_flips_leftover_running_scans_to_interrupted(
    app: FastAPI,
    supervisor: FakeSupervisor,
    resolutions: dict[str, Resolution],
    settings: Settings,
) -> None:
    database = Database(settings.db_path)
    database.connect()
    try:
        database.migrate(settings.migrations_dir)
        connection = database._require_conn()
        insert_authorization(connection)
        insert_scan(connection)
        connection.execute("UPDATE scans SET status = 'running'")
        connection.commit()
    finally:
        database.close()

    with _live_client(app, supervisor, resolutions):
        pass

    row = _rows(settings, "SELECT * FROM scans WHERE id = 'scan-1'")[0]
    assert row["status"] == "interrupted"
    assert row["error_code"] == "interrupted_by_restart"
    assert row["finished_at"] is not None
    assert row["error_message"]
    # 退出码是"子进程告诉我们的"，我们没等到它 —— 编一个是撒谎。
    assert row["exit_code"] is None
    assert row["exit_meaning"] is None


# =============================================================================
# 16 实时流 channel 的起停（W2c）
# =============================================================================
class OrderRecordingChannels:
    """`ChannelRegistry` 的替身，只记下自己被调的顺序。

    真的那个也能用（lifespan 建的就是真的），但"close 排在 forget 前面还是后面"只能
    从调用顺序上看 —— 后果（最后一批帧没脱敏）在这一层观察不到。
    """

    def __init__(self, order: list[str], db: Database | None = None) -> None:
        self.order = order
        self._db = db

    def open(self, scan_id: str, cwd: Path) -> None:
        self.order.append("open")

    async def close(self, scan_id: str) -> None:
        self.order.append("close")

    async def shutdown(self) -> None:
        self.order.append("shutdown")
        if self._db is not None:
            # 停机兜底的那次 `close()` 还要做最后一次 tick，而那次 tick 经 `EventMirror`
            # 写库 —— 所以这一刻库必须还开着。私有字段是唯一看得见它的地方（同 conftest
            # 的 `conn` 夹具）。
            self.order.append("db-open" if self._db._conn is not None else "db-closed")


def test_the_channel_is_opened_on_launch_and_closed_at_the_terminal_state(
    client: TestClient, app: FastAPI, settings: Settings
) -> None:
    """W2c 的不变式在接线层的那一半：扫描到终态之后 registry 里不许还留着这个 scan_id
    （留着就是一个永远转下去的轮询任务）。用**真**的 registry，替身测不出这件事。"""
    accepted = _launch(client, app)
    scan_id = str(accepted["scan_id"])
    # `open` 在后台任务里，它与本请求的返回没有先后保证。
    _wait_until(lambda: app.state.channels.get(scan_id) is not None)
    process = app.state.supervisor.get(scan_id)
    assert process is not None

    process.finish()

    _wait_for_status(settings, scan_id, "completed")
    # 等后台任务**整个**跑完：终态只说明 try 里最后一步过了，`finally` 还没跑。
    _wait_until(lambda: not app.state.scan_tasks)

    assert app.state.channels.get(scan_id) is None


def test_the_channel_is_closed_before_the_secrets_are_forgotten(
    client: TestClient, app: FastAPI, settings: Settings
) -> None:
    """**这条顺序是安全约束**：`close()` 的最后一次 tick 会把进程退出前写下的最后一批
    日志行推成帧，而脱敏靠的是此刻**还在册**的凭据值。先 `forget` 再关 channel，最后
    那批帧就是没脱敏的。结构上防不住，只有这条测试盯着。
    """
    order: list[str] = []
    app.state.channels = OrderRecordingChannels(order)
    secrets = app.state.scan_secrets
    real_forget = secrets.forget

    def recording_forget(scan_id: str) -> bool:
        order.append("forget")
        return bool(real_forget(scan_id))

    secrets.forget = recording_forget  # type: ignore[method-assign]

    accepted = _launch(
        client,
        app,
        credentials=[{"role": "user", "username": "alice", "password": _TEST_PASSWORD}],
    )
    scan_id = str(accepted["scan_id"])
    process = app.state.supervisor.get(scan_id)
    assert process is not None

    process.finish()

    _wait_for_status(settings, scan_id, "completed")
    _wait_until(lambda: not app.state.scan_tasks)

    assert order == ["open", "close", "forget"]


def test_lifespan_shutdown_closes_the_channels_while_the_database_is_still_open(
    app: FastAPI,
) -> None:
    """不变式的另一半：**停机**之后一个 channel 都不许剩。

    正常结束的扫描由 `_run_to_completion` 的 finally 关掉，这里收的是"那个后台任务被
    cancel 掉、没走到 finally"的残留 —— 删掉 lifespan 里那一句调用，上面两条测试都不会红
    （收货时实测 0 红），所以这一句接线需要自己的守卫。顺带钉住它排在 `db.close()` 之前。
    """
    order: list[str] = []
    with TestClient(app, base_url="https://testserver"):
        # 替身只能在 lifespan **跑完之后**装（同 `test_keys.py` 那条注释：在 `app` 夹具里
        # 塞会被 lifespan 原地盖掉）。
        app.state.channels = OrderRecordingChannels(order, db=app.state.db)

    assert order == ["shutdown", "db-open"]
