"""`GET /api/scans/{id}/export/{kind}` 与 `/raw.zip` 的接线测试（T23＋T24）。

接线层：正路 + 错误形状。zip 的排除规则在 `test_raw_export.py`，这里不重复。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services import audit
from app.settings import Settings
from tests.conftest import insert_authorization, insert_scan, make_run_dir, writable_conn

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
