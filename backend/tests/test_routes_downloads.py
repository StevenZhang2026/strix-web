"""`GET /api/scans/{id}/export/{kind}`、`/raw.zip`、`/report/{print,docx}` 的接线测试（T23＋T24＋T23c）。

接线层：正路 + 错误形状。zip 的排除规则在 `test_raw_export.py`，这里不重复。
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.routes import downloads
from app.services import audit
from app.settings import Settings
from tests.conftest import (
    EXEC_OK,
    FINDING_OK,
    USERNAME,
    insert_authorization,
    insert_scan,
    make_run_dir,
    writable_conn,
)

EXPORT_MD = "/api/scans/scan-1/export/md"


def _seed(settings: Settings, *, status: str = "completed", run_dir: bool = True) -> Path:
    run_path = make_run_dir(settings.scans_dir / "scan-1")
    with writable_conn(settings) as conn:
        insert_authorization(conn)
        insert_scan(conn)
        conn.execute(
            "UPDATE scans SET status = ?, run_dir = ? WHERE id = 'scan-1'",
            (status, str(run_path) if run_dir else None),
        )
    return run_path


def _purge(settings: Settings) -> None:
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
    with writable_conn(settings) as conn:
        audit.insert(conn, prepared)


@pytest.mark.parametrize(
    ("kind", "file_name", "download_name"),
    [
        ("md", "penetration_test_report.md", "strix-scan-1-report.md"),
        ("csv", "vulnerabilities.csv", "strix-scan-1-vulnerabilities.csv"),
        ("sarif", "findings.sarif", "strix-scan-1.sarif"),
    ],
)
def test_export_returns_file_as_attachment(
    client: TestClient, settings: Settings, kind: str, file_name: str, download_name: str
) -> None:
    run_path = _seed(settings)
    (run_path / file_name).write_bytes(f"content of {kind}".encode())

    response = client.get(f"/api/scans/scan-1/export/{kind}")
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert "attachment" in disposition
    assert download_name in disposition
    assert response.content == f"content of {kind}".encode()


def test_export_missing_file_is_404(client: TestClient, settings: Settings) -> None:
    _seed(settings)
    response = client.get("/api/scans/scan-1/export/csv")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert _exported(settings) == []  # 什么都没带走就不记


def _exported(settings: Settings) -> list[tuple[str | None, str | None, dict[str, object]]]:
    with writable_conn(settings) as conn:
        rows = conn.execute(
            "SELECT actor, scan_id, detail_json FROM audit_log "
            "WHERE event = 'report.exported' ORDER BY rowid"
        ).fetchall()
    return [(row[0], row[1], json.loads(row[2])) for row in rows]


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("export/md", "md"),
        ("export/csv", "csv"),
        ("export/sarif", "sarif"),
        ("raw.zip", "raw_zip"),
        ("report/print", "print"),
        ("report/docx", "docx"),
    ],
)
def test_download_records_report_exported(
    client: TestClient, settings: Settings, path: str, kind: str
) -> None:
    run_path = _seed(settings)
    for name in ("penetration_test_report.md", "vulnerabilities.csv", "findings.sarif"):
        (run_path / name).write_text("x")

    assert client.get(f"/api/scans/scan-1/{path}").status_code == 200
    assert _exported(settings) == [(USERNAME, "scan-1", {"kind": kind})]


@pytest.mark.parametrize(
    ("case", "status_code", "code"),
    [
        ("missing", 404, "not_found"),
        ("running", 409, "scan_not_finished"),
        ("no_run_dir", 404, "not_found"),
        ("purged", 404, "artifacts_purged"),
    ],
)
def test_export_rejections(
    client: TestClient, settings: Settings, case: str, status_code: int, code: str
) -> None:
    if case == "running":
        _seed(settings, status="running")
    elif case == "no_run_dir":
        _seed(settings, run_dir=False)
    elif case == "purged":
        _seed(settings)
        _purge(settings)

    response = client.get(EXPORT_MD)
    assert response.status_code == status_code
    assert response.json()["code"] == code


def test_raw_zip_returns_scan_dir(client: TestClient, settings: Settings) -> None:
    _seed(settings)
    response = client.get("/api/scans/scan-1/raw.zip")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert 'filename="strix-scan-1-raw.zip"' in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert "strix_runs/strix-run-1/run.json" in zf.namelist()


def test_raw_zip_rejects_unfinished_scan(client: TestClient, settings: Settings) -> None:
    _seed(settings, status="running")
    response = client.get("/api/scans/scan-1/raw.zip")
    assert response.status_code == 409
    assert response.json()["code"] == "scan_not_finished"


def _seed_report(settings: Settings, *, cost_second: float | None = 0.02) -> None:
    _seed(settings)
    at = "2026-09-08T00:00:00.000Z"
    stale = json.dumps({**json.loads(FINDING_OK), "title_zh": "过期译文-不该出现"})
    old_exec = json.dumps({**json.loads(EXEC_OK), "summary_zh": "旧总述-不该出现"})
    with writable_conn(settings) as conn:
        for fid, title, raw in (
            ("f0", "t0-原文", {"title": "t0", "evidence": "EVIDENCE-f0-原样"}),
            ("f1", "XSS 反射-原文", {"title": "t1"}),
        ):
            conn.execute(
                "INSERT INTO scan_findings (scan_id, finding_id, severity, title, first_seen_at, "
                "raw_json, input_hash) VALUES (?,?,?,?,?,?,?)",
                (
                    "scan-1",
                    fid,
                    "high",
                    title,
                    at,
                    json.dumps(raw, ensure_ascii=False, sort_keys=True),
                    fid.ljust(64, "0"),
                ),
            )
        # 旧总述写在前（created_at 更早）：两条路由都必须取最新那行。
        for fid, input_hash, payload, cost, created in (
            ("f0", "f0".ljust(64, "0"), FINDING_OK, 0.01, at),
            ("f1", "x" * 64, stale, 0.5, at),
            ("__executive__", "d" * 64, old_exec, 0.5, "2026-09-07T00:00:00.000Z"),
            ("__executive__", "e" * 64, EXEC_OK, cost_second, at),
        ):
            conn.execute(
                "INSERT INTO report_translations (scan_id, finding_id, input_hash, model, lang, "
                "payload_json, cost_usd, usage_prompt, usage_completion, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("scan-1", fid, input_hash, "m", "zh-CN", payload, cost, 1, 1, created),
            )


def test_report_print_renders_translated_html(client: TestClient, settings: Settings) -> None:
    _seed_report(settings)
    response = client.get("/api/scans/scan-1/report/print")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-security-policy"] == downloads.REPORT_CSP
    body = response.text
    for expected in (
        "订单接口可越权查看他人订单",
        "XSS 反射-原文",
        "EVIDENCE-f0-原样",
        "发现一处高危越权。",
        "$0.0300",
    ):
        assert expected in body
    assert "过期译文-不该出现" not in body
    assert "旧总述-不该出现" not in body


def test_report_print_cost_unknown_when_any_row_is_null(
    client: TestClient, settings: Settings
) -> None:
    _seed_report(settings, cost_second=None)
    body = client.get("/api/scans/scan-1/report/print").text
    assert "无法计算" in body
    assert "$0.0" not in body


def test_report_docx_is_word_attachment_with_same_content(
    client: TestClient, settings: Settings
) -> None:
    _seed_report(settings)
    response = client.get("/api/scans/scan-1/report/docx")
    assert response.status_code == 200
    assert response.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert "attachment" in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        document = zf.read("word/document.xml").decode()
    for expected in ("订单接口可越权查看他人订单", "EVIDENCE-f0-原样", "$0.0300"):
        assert expected in document
    assert "过期译文-不该出现" not in document


@pytest.mark.parametrize("kind", ["print", "docx"])
def test_report_rejects_unfinished_scan(client: TestClient, settings: Settings, kind: str) -> None:
    _seed(settings, status="running")
    response = client.get(f"/api/scans/scan-1/report/{kind}")
    assert response.status_code == 409
    assert response.json()["code"] == "scan_not_finished"
