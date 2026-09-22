"""`EventMirror` 的测试。

两条不变式各自能独立变红：
- I1「先文件、后行」：落盘失败时两张表都不许有行（`test_no_rows_when_image_landing_fails`）；
  反向（行 ⇒ 文件存在且字节数等于 `bytes` 列）在正路测试里断言。
- I2「跨事件按 sha256 只记一次账」：`test_known_sha_is_not_re_accounted`。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from app.db import Database
from app.services.event_mirror import (
    PAYLOAD_VERSION,
    EventMirror,
    MirroredEvent,
    media_rel_path,
    strix_id_of,
)
from app.services.retention import InvalidScanIdError
from app.services.run_projector import ProjectedEvent
from tests.conftest import insert_authorization, insert_scan, make_projected_event

# 只要是能解码出的非空字节就够：`extract_media` 不校验 PNG 结构（它的测试兜着这件事）。
_PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-pixels"
_PNG_SHA = hashlib.sha256(_PNG_BYTES).hexdigest()
_PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(_PNG_BYTES).decode()
_LANDED_URL = f"/api/scans/scan-1/media/{_PNG_SHA}.png"


def _event(
    *,
    key: str = "tool_12",
    agent_id: str | None = "agent-1",
    data: dict[str, object] | None = None,
) -> ProjectedEvent:
    """造一条投影事件。默认带一张内联 PNG。"""
    payload: dict[str, object] = {"screenshot": _PNG_DATA_URL} if data is None else data
    return make_projected_event(key=key, agent_id=agent_id, data=payload)


def _mirror(db: Database, conn: sqlite3.Connection, tmp_path: Path) -> EventMirror:
    """建好 FK 依赖的 authorizations / scans 两行，返回被测对象。"""
    insert_authorization(conn)
    insert_scan(conn, "scan-1")
    return EventMirror(db, tmp_path / "scans", "scan-1")


def test_strix_id_of_takes_integer_suffix() -> None:
    assert strix_id_of("tool_12") == 12
    assert strix_id_of("agent_1_7") == 7
    assert strix_id_of("42") == 42
    assert strix_id_of("tool_abc") is None
    assert strix_id_of("tool_") is None
    assert strix_id_of("") is None


def test_media_rel_path_is_relative_to_data_dir() -> None:
    sha = "a" * 64
    assert media_rel_path(Path("/data/scans"), "scan-1", sha) == f"scans/scan-1/media/{sha}.png"


def test_append_lands_image_and_writes_event_row(
    db: Database, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    mirror = _mirror(db, conn, tmp_path)
    event = _event()
    original_data = dict(event.data)

    async def scenario() -> MirroredEvent:
        return await mirror.append(epoch=1, seq=7, event=event)

    result = asyncio.run(scenario())

    assert result.seq == 7
    assert result.new_media == (_PNG_SHA,)
    assert result.skipped == ()
    assert result.event.data == {"screenshot": _LANDED_URL}
    assert result.event.fingerprint == event.fingerprint
    assert event.data == original_data

    landed = tmp_path / "scans" / "scan-1" / "media" / f"{_PNG_SHA}.png"
    assert landed.read_bytes() == _PNG_BYTES

    row = conn.execute("SELECT * FROM scan_events").fetchone()
    assert row["scan_id"] == "scan-1"
    assert row["epoch"] == 1
    assert row["seq"] == 7
    assert row["strix_id"] == 12
    assert row["kind"] == "tool_result"
    assert row["agent_id"] == "agent-1"
    assert row["ts"] == "2026-09-16T03:04:05.678901Z"
    assert row["version"] == PAYLOAD_VERSION
    assert row["fingerprint"] == event.fingerprint
    # 内联 PNG 绝不许进镜像：这一条就是 `extract_media` 那次调用的接线断言。
    assert "data:image/png;base64" not in row["data_json"]
    assert json.loads(row["data_json"]) == {
        "key": "tool_12",
        "upstream_version": 3,
        "data": {"screenshot": _LANDED_URL},
    }

    media = dict(conn.execute("SELECT * FROM scan_media").fetchone())
    first_seen_at = media.pop("first_seen_at")
    assert first_seen_at.endswith("Z")
    assert media == {
        "scan_id": "scan-1",
        "sha256": _PNG_SHA,
        "mime": "image/png",
        # I1 的正向半边：行在 ⇒ 文件在，且字节数对得上。
        "bytes": landed.stat().st_size,
        "rel_path": f"scans/scan-1/media/{_PNG_SHA}.png",
        "first_agent_id": "agent-1",
    }


def test_no_rows_when_image_landing_fails(
    db: Database, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """I1：落盘抛 `OSError` 时，`scan_events` 与 `scan_media` 都不许有行。"""
    mirror = _mirror(db, conn, tmp_path)
    # 用一个同名普通文件占住 media/ 的位置 → `mkdir` 抛 FileExistsError。
    # 刻意不用 chmod：容器里测试以 root 跑，权限位拦不住 root。
    scan_dir = tmp_path / "scans" / "scan-1"
    scan_dir.mkdir(parents=True)
    (scan_dir / "media").write_bytes(b"not a directory")

    async def scenario() -> MirroredEvent:
        return await mirror.append(epoch=0, seq=0, event=_event())

    with pytest.raises(OSError):
        asyncio.run(scenario())

    assert conn.execute("SELECT count(*) FROM scan_events").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM scan_media").fetchone()[0] == 0


def test_known_sha_is_not_re_accounted(
    db: Database, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """I2：同一张图第二次出现只改写 URL，既不新增行也不覆盖首见信息。"""
    mirror = _mirror(db, conn, tmp_path)
    sentinel = {
        "scan_id": "scan-1",
        "sha256": _PNG_SHA,
        "mime": "image/png",
        "bytes": 1,
        "rel_path": "sentinel",
        "first_agent_id": "agent-0",
        "first_seen_at": "1999-01-01T00:00:00Z",
    }
    conn.execute(
        "INSERT INTO scan_media (scan_id, sha256, mime, bytes, rel_path, first_agent_id,"
        " first_seen_at) VALUES (:scan_id, :sha256, :mime, :bytes, :rel_path, :first_agent_id,"
        " :first_seen_at)",
        sentinel,
    )

    async def scenario() -> MirroredEvent:
        return await mirror.append(epoch=0, seq=0, event=_event(agent_id="agent-9"))

    result = asyncio.run(scenario())

    assert result.new_media == ()
    assert result.event.data == {"screenshot": _LANDED_URL}
    rows = conn.execute("SELECT * FROM scan_media").fetchall()
    assert [dict(row) for row in rows] == [sentinel]


def test_skip_codes_are_passed_through(
    db: Database, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    mirror = _mirror(db, conn, tmp_path)
    payload: dict[str, object] = {
        "shot": "data:image/jpeg;base64," + base64.b64encode(b"jpeg").decode()
    }

    async def scenario() -> MirroredEvent:
        return await mirror.append(epoch=0, seq=0, event=_event(data=payload))

    result = asyncio.run(scenario())

    assert result.skipped == ("unsupported_mime",)
    assert result.new_media == ()
    assert result.event.data == payload
    assert conn.execute("SELECT count(*) FROM scan_media").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM scan_events").fetchone()[0] == 1


def test_duplicate_epoch_seq_is_rejected_by_primary_key(
    db: Database, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    mirror = _mirror(db, conn, tmp_path)

    async def scenario() -> None:
        await mirror.append(epoch=0, seq=0, event=_event(data={"note": "first"}))
        await mirror.append(epoch=0, seq=0, event=_event(data={"note": "second"}))

    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(scenario())


def test_invalid_scan_id_is_rejected_at_construction(db: Database, tmp_path: Path) -> None:
    with pytest.raises(InvalidScanIdError):
        EventMirror(db, tmp_path / "scans", "../evil")
