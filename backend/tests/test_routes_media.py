"""`GET /api/scans/{scan_id}/media/{sha256}.png`：只交出 `scan_media` 以 (scan_id, sha256) 记过账的文件。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Database
from app.services.event_mirror import EventMirror
from app.services.retention import RetentionSweeper
from app.settings import Settings
from tests.conftest import insert_authorization, insert_scan, make_projected_event, writable_conn

# 只要是能解码出的非空字节就够：`extract_media` 不校验 PNG 结构（它的测试兜着这件事）。
_PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-pixels"
_PNG_SHA = hashlib.sha256(_PNG_BYTES).hexdigest()
_PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(_PNG_BYTES).decode()


def _seed_scans(settings: Settings, *scan_ids: str) -> None:
    with writable_conn(settings) as conn:
        insert_authorization(conn)
        for scan_id in scan_ids:
            insert_scan(conn, scan_id=scan_id)


def _mirror_screenshot(settings: Settings, scan_id: str) -> str:
    """用真 `EventMirror` 落一张图，返回它改写进事件正文的 URL。"""
    database = Database(settings.db_path)
    database.connect()
    try:
        mirror = EventMirror(database, settings.scans_dir, scan_id)
        event = make_projected_event(data={"screenshot": _PNG_DATA_URL})
        result = asyncio.run(mirror.append(epoch=1, seq=0, event=event))
    finally:
        database.close()
    url = result.event.data["screenshot"]
    assert isinstance(url, str)
    return url


def _media_file(settings: Settings, scan_id: str) -> Path:
    return settings.scans_dir / scan_id / "media" / f"{_PNG_SHA}.png"


def test_mirrored_url_is_served_then_reports_purged(client: TestClient, settings: Settings) -> None:
    """路由没接上 `extract_media` 写出的 URL、头/类型写错、或不查 purged 审计，都会红。"""
    _seed_scans(settings, "scan-1")
    url = _mirror_screenshot(settings, "scan-1")

    response = client.get(url)
    assert response.status_code == 200
    assert response.content == _PNG_BYTES
    assert response.headers["content-type"].startswith("image/png")
    assert response.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert response.headers["x-content-type-options"] == "nosniff"

    with writable_conn(settings) as conn:
        conn.execute(
            "UPDATE scans SET status = 'completed', finished_at = '2026-01-01T00:00:00+00:00'"
            " WHERE id = ?",
            ("scan-1",),
        )
    database = Database(settings.db_path)
    database.connect()
    try:
        sweeper = RetentionSweeper(
            database, settings.scans_dir, retention_days=1, audit_dir=settings.audit_dir
        )
        outcome = asyncio.run(sweeper.sweep(now=datetime(2026, 9, 1, tzinfo=UTC)))
    finally:
        database.close()
    assert outcome.purged == ("scan-1",)

    response = client.get(url)
    assert response.status_code == 404
    assert response.json()["code"] == "artifacts_purged"


def test_sha_of_another_scan_is_not_found(client: TestClient, settings: Settings) -> None:
    """查询去掉 `scan_id = ?` 条件，A 的 URL 就能读到 B 的截图 → 200，红。"""
    _seed_scans(settings, "scan-a", "scan-b")
    _mirror_screenshot(settings, "scan-b")
    assert _media_file(settings, "scan-b").is_file()

    response = client.get(f"/api/scans/scan-a/media/{_PNG_SHA}.png")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_row_without_file_is_not_found(client: TestClient, settings: Settings) -> None:
    """去掉 `is_file` 检查，`FileResponse` 发送时 stat 失败 → 500，红。"""
    _seed_scans(settings, "scan-1")
    url = _mirror_screenshot(settings, "scan-1")
    _media_file(settings, "scan-1").unlink()

    response = client.get(url)
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_unknown_sha_is_not_found(client: TestClient, settings: Settings) -> None:
    """把"没有行"一律当成 `artifacts_purged`（不看审计）会红。"""
    _seed_scans(settings, "scan-1")

    response = client.get(f"/api/scans/scan-1/media/{'0' * 64}.png")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
