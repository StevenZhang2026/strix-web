"""`POST`／`GET /api/scans/{id}/report/zh` 的接线测试（T21c）。

接线层：一条正路 + 每处接线一条会因删掉它而变红的测试。翻译本身的判定在 `test_translator.py`。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.services import audit
from app.services.key_vault import IDLE_TTL_SECONDS, CredentialSet, KeyVault
from app.services.llm_client import Completion
from app.services.translator import FINDING_MAX_TOKENS
from app.settings import Settings
from tests.conftest import (
    EXEC_OK,
    FINDING_OK,
    PASSWORD,
    USERNAME,
    FakeClock,
    insert_authorization,
    insert_scan,
    writable_conn,
)

URL = "/api/scans/scan-1/report/zh"
_API_KEY = "sk-ant-report-test-key"


class FakeCompleter:
    def __init__(
        self,
        *,
        gate: threading.Event | None = None,
        raises: Exception | None = None,
        sleep_s: float = 0.0,
    ) -> None:
        self.gate = gate
        self.raises = raises
        self.sleep_s = sleep_s
        self.calls = 0

    async def __call__(
        self, credentials: CredentialSet, messages: list[dict[str, str]], max_tokens: int
    ) -> Completion:
        self.calls += 1
        if self.gate is not None:
            await asyncio.to_thread(self.gate.wait, 5)
        if self.sleep_s:
            await asyncio.sleep(self.sleep_s)
        if self.raises is not None:
            raise self.raises
        text = FINDING_OK if max_tokens == FINDING_MAX_TOKENS else EXEC_OK
        return Completion(text=text, usage_prompt=10, usage_completion=10, cost_usd=0.01)


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _seed(
    settings: Settings, *, status: str = "completed", run_dir: bool = True, n_findings: int = 2
) -> None:
    run_path = settings.console_data_dir / "run"
    run_path.mkdir(exist_ok=True)
    (run_path / "penetration_test_report.md").write_text("# report\n", "utf-8")
    with writable_conn(settings) as conn:
        insert_authorization(conn)
        insert_scan(conn)
        conn.execute(
            "UPDATE scans SET status = ?, run_dir = ? WHERE id = 'scan-1'",
            (status, str(run_path) if run_dir else None),
        )
        for i in range(n_findings):
            fid = f"f{i}"
            conn.execute(
                "INSERT INTO scan_findings (scan_id, finding_id, severity, title, first_seen_at, "
                "raw_json, input_hash) VALUES (?,?,?,?,?,?,?)",
                (
                    "scan-1",
                    fid,
                    "high",
                    "t",
                    "2026-09-08T00:00:00.000Z",
                    json.dumps({"title": f"IDOR {i}"}, ensure_ascii=False, sort_keys=True),
                    fid.ljust(64, "0")[:64],
                ),
            )


def _store_credentials(app: FastAPI) -> str:
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


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("等超时了：后台任务没有做到期望的状态")


def _wait_done(client: TestClient) -> dict[str, object]:
    _wait_until(lambda: client.get(URL).json()["status"] != "running")
    body: dict[str, object] = client.get(URL).json()
    return body


def _audit_details(settings: Settings) -> list[dict[str, object]]:
    with writable_conn(settings) as conn:
        rows = conn.execute(
            "SELECT detail_json FROM audit_log WHERE event = 'report.translated' ORDER BY rowid"
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def test_translate_happy_path(client: TestClient, app: FastAPI, settings: Settings) -> None:
    completer = FakeCompleter()
    app.state.llm_completer = completer
    clock = FakeClock()
    app.state.key_vault = KeyVault(clock=clock)
    handle = _store_credentials(app)
    _seed(settings)
    with writable_conn(settings) as conn:
        conn.execute(
            "INSERT INTO report_translations (scan_id, finding_id, input_hash, model, lang, "
            "payload_json, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                "scan-1",
                "f0",
                "e" * 64,
                "anthropic/claude-sonnet-4-5",
                "zh-CN",
                FINDING_OK.replace("订单接口可越权查看他人订单", "旧译文"),
                "2099-01-01T00:00:00.000Z",
            ),
        )

    response = client.post(URL, json={"vault_handle": handle})
    assert response.status_code == 202
    assert response.json() == {"status": "running"}
    body = _wait_done(client)

    assert body["status"] == "done"
    assert body["result"] == {
        "translated_findings": 2,
        "cached_findings": 0,
        "failed_findings": 0,
        "executive": "translated",
        "cost_usd": pytest.approx(0.03),
    }
    findings = body["findings_zh"]
    assert isinstance(findings, list)
    assert [f["finding_id"] for f in findings] == ["f0", "f1"]
    assert {f["title_zh"] for f in findings} == {json.loads(FINDING_OK)["title_zh"]}
    executive = body["executive_zh"]
    assert isinstance(executive, dict)
    assert executive["summary_zh"] == json.loads(EXEC_OK)["summary_zh"]
    assert completer.calls == 3
    assert [d["outcome"] for d in _audit_details(settings)] == ["done"]
    clock.advance(IDLE_TTL_SECONDS + 1)
    assert app.state.key_vault.get(handle) is None


def test_force_is_forwarded(client: TestClient, app: FastAPI, settings: Settings) -> None:
    completer = FakeCompleter()
    app.state.llm_completer = completer
    handle = _store_credentials(app)
    _seed(settings)

    assert client.post(URL, json={"vault_handle": handle}).status_code == 202
    _wait_done(client)
    assert client.post(URL, json={"vault_handle": handle, "force": True}).status_code == 202
    _wait_done(client)
    assert completer.calls == 6
    assert client.post(URL, json={"vault_handle": handle}).status_code == 202
    body = _wait_done(client)
    result = body["result"]
    assert isinstance(result, dict)
    assert result["cached_findings"] == 2
    assert completer.calls == 6


def test_second_post_while_running_is_rejected(
    client: TestClient, app: FastAPI, settings: Settings
) -> None:
    gate = threading.Event()
    completer = FakeCompleter(gate=gate)
    app.state.llm_completer = completer
    clock = FakeClock()
    app.state.key_vault = KeyVault(clock=clock)
    handle = _store_credentials(app)
    _seed(settings)

    assert client.post(URL, json={"vault_handle": handle}).status_code == 202
    second = client.post(URL, json={"vault_handle": handle})
    assert second.status_code == 409
    assert second.json()["code"] == "report_in_progress"
    assert client.get(URL).json()["status"] == "running"
    gate.set()
    assert _wait_done(client)["status"] == "done"
    assert client.post(URL, json={"vault_handle": handle}).status_code == 202
    _wait_done(client)
    # 409 那一次不许占着凭据引用：查重必须排在 acquire 之前（否则这里 idle TTL 永不生效）。
    clock.advance(IDLE_TTL_SECONDS + 1)
    assert app.state.key_vault.get(handle) is None


def _purge(settings: Settings) -> None:
    with writable_conn(settings) as conn:
        prepared = audit.prepare(
            audit.AuditEntry(
                event=audit.EVENT_SCAN_PURGED,
                actor=None,
                detail={},
                client_ip=None,
                user_agent=None,
                scan_id="scan-1",
            )
        )
        audit.insert(conn, prepared)


@pytest.mark.parametrize(
    ("case", "status_code", "code"),
    [
        ("missing", 404, "not_found"),
        ("running", 409, "scan_not_finished"),
        ("no_run_dir", 404, "not_found"),
        ("purged", 404, "artifacts_purged"),
        ("bad_handle", 409, "key_required"),
    ],
)
def test_rejections(
    client: TestClient,
    app: FastAPI,
    settings: Settings,
    case: str,
    status_code: int,
    code: str,
) -> None:
    completer = FakeCompleter()
    app.state.llm_completer = completer
    handle = _store_credentials(app)
    if case == "running":
        _seed(settings, status="running")
    elif case == "no_run_dir":
        _seed(settings, run_dir=False)
    elif case != "missing":
        _seed(settings)
    if case == "purged":
        _purge(settings)
    if case == "bad_handle":
        handle = "not-a-real-handle"

    response = client.post(URL, json={"vault_handle": handle})
    assert response.status_code == status_code
    assert response.json()["code"] == code
    if case == "bad_handle":
        assert response.json()["params"] == {"provider": "anthropic", "auth_shape": "single"}
        assert client.get(URL).json()["status"] == "idle"
    assert completer.calls == 0


def test_failure_marks_failed_and_logs_only_class_names(
    client: TestClient, app: FastAPI, settings: Settings
) -> None:
    completer = FakeCompleter(raises=RuntimeError("SENTINEL-模型原文-口令"))
    app.state.llm_completer = completer
    clock = FakeClock()
    app.state.key_vault = KeyVault(clock=clock)
    handle = _store_credentials(app)
    _seed(settings)
    collector = _Collect()
    loggers = [logging.getLogger("app.routes.report"), logging.getLogger("app.services.translator")]
    for lg in loggers:
        lg.addHandler(collector)
    try:
        assert client.post(URL, json={"vault_handle": handle}).status_code == 202
        body = _wait_done(client)
    finally:
        for lg in loggers:
            lg.removeHandler(collector)

    assert body["status"] == "failed"
    assert body["result"] is None
    dump = "\n".join(f"{r.getMessage()} {r.__dict__!r}" for r in collector.records)
    assert "SENTINEL" not in dump
    assert any("RuntimeError" in getattr(r, "exc_types", []) for r in collector.records)
    assert [d["outcome"] for d in _audit_details(settings)] == ["error"]
    clock.advance(IDLE_TTL_SECONDS + 1)
    assert app.state.key_vault.get(handle) is None


def test_get_idle_without_translation(client: TestClient, settings: Settings) -> None:
    _seed(settings)
    assert client.get(URL).json() == {
        "status": "idle",
        "result": None,
        "findings_zh": [],
        "executive_zh": None,
    }


def test_shutdown_cancels_running_translation(app: FastAPI, settings: Settings) -> None:
    with TestClient(app, base_url="https://testserver") as c:
        _seed(settings)
        login = c.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
        assert login.status_code == 200
        app.state.llm_completer = FakeCompleter(sleep_s=30)
        handle = _store_credentials(app)
        assert c.post(URL, json={"vault_handle": handle}).status_code == 202
        started = time.monotonic()
    assert time.monotonic() - started < 5
