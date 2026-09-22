"""`replay_batch` 的测试。

只守一条不变式：**回放出来的帧，就是当初直播推出去的那一帧**（除 `ts` 外逐字段相等）。
所以期望帧一律用直播侧那两个函数现算（`envelope_for(payload=event_payload(...))`），
行也一律用真的 `EventMirror.append()` 写 —— 手抄一份期望 payload 的字面量 dict，
就等于"两边同形状"这件事没人守了。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import sqlite3
from pathlib import Path

from app.db import Database
from app.services.event_mirror import EventMirror
from app.services.event_replay import DEFAULT_LIMIT, ReplayBatch, ReplayCursor, replay_batch
from app.services.run_projector import ProjectedEvent
from app.services.scan_frames import envelope_for, event_payload
from app.ws_envelope import Envelope
from tests.conftest import insert_authorization, insert_scan, make_projected_event

# 发送时刻。回放时由调用方传进来（生产里是 `now_ts()`），所以测试里它是个常量。
_TS = "2026-09-20T12:00:00.000Z"

_PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-pixels"
_PNG_SHA = hashlib.sha256(_PNG_BYTES).hexdigest()
_PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(_PNG_BYTES).decode()


def _mirror(db: Database, tmp_path: Path, scan_id: str = "scan-1") -> EventMirror:
    return EventMirror(db, tmp_path / "scans", scan_id)


def _setup(
    db: Database, conn: sqlite3.Connection, tmp_path: Path, scan_id: str = "scan-1"
) -> EventMirror:
    """建好 FK 依赖的 authorizations / scans 两行，返回写入端。"""
    insert_authorization(conn)
    insert_scan(conn, scan_id)
    return _mirror(db, tmp_path, scan_id)


def _plain(key: str = "tool_12") -> ProjectedEvent:
    """一条不带图的事件。`data` 里放上 key，方便断言"回来的是哪一帧"。"""
    return make_projected_event(key=key, data={"note": key})


def _seqs(batch: ReplayBatch) -> list[int]:
    return [frame.seq for frame in batch.frames]


def _keys(batch: ReplayBatch) -> list[object]:
    return [frame.payload["key"] for frame in batch.frames]


def test_frame_matches_the_live_frame(
    db: Database, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    mirror = _setup(db, conn, tmp_path)

    async def scenario() -> tuple[Envelope, ReplayBatch]:
        mirrored = await mirror.append(epoch=2, seq=5, event=_plain())
        expected = envelope_for(
            type="event.add",
            payload=event_payload(mirrored.event),
            epoch=2,
            seq=5,
            ts=_TS,
        )
        return expected, await replay_batch(db, "scan-1", resume_from=None, ts=_TS)

    expected, batch = asyncio.run(scenario())

    assert batch.frames == (expected,)
    assert batch.epoch == 2
    assert batch.next_cursor is None


def test_frame_with_inline_png_matches_the_live_frame(
    db: Database, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """带图那条尤其要测：镜像把 base64 换成了 media URL，回放只能交付改写后的那份。"""
    mirror = _setup(db, conn, tmp_path)

    async def scenario() -> tuple[Envelope, ReplayBatch]:
        mirrored = await mirror.append(
            epoch=1, seq=0, event=make_projected_event(data={"screenshot": _PNG_DATA_URL})
        )
        expected = envelope_for(
            type="event.add",
            payload=event_payload(mirrored.event),
            epoch=1,
            seq=0,
            ts=_TS,
        )
        return expected, await replay_batch(db, "scan-1", resume_from=None, ts=_TS)

    expected, batch = asyncio.run(scenario())

    assert batch.frames == (expected,)
    frame = batch.frames[0]
    assert frame.payload["data"] == {"screenshot": f"/api/scans/scan-1/media/{_PNG_SHA}.png"}
    # 上一条的一个真后果：base64 正文绝不会经回放回到前端。
    assert "base64" not in repr(frame.payload)


def test_cursor_at_current_epoch_resumes_after_its_seq(
    db: Database, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    mirror = _setup(db, conn, tmp_path)

    async def scenario() -> ReplayBatch:
        for seq in (0, 1, 2):
            await mirror.append(epoch=1, seq=seq, event=_plain(f"tool_{seq}"))
        return await replay_batch(db, "scan-1", resume_from=ReplayCursor(epoch=1, seq=1), ts=_TS)

    batch = asyncio.run(scenario())

    assert _seqs(batch) == [2]
    assert _keys(batch) == ["tool_2"]
    assert batch.epoch == 1


def test_stale_cursor_epoch_replays_the_whole_current_epoch(
    db: Database, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """epoch 不匹配 = 客户端手上的状态已作废，它要的是新一代的完整序列。"""
    mirror = _setup(db, conn, tmp_path)

    async def scenario() -> ReplayBatch:
        for seq in (0, 1):
            await mirror.append(epoch=3, seq=seq, event=_plain(f"tool_{seq}"))
        return await replay_batch(db, "scan-1", resume_from=ReplayCursor(epoch=2, seq=99), ts=_TS)

    batch = asyncio.run(scenario())

    assert _seqs(batch) == [0, 1]
    assert batch.epoch == 3


def test_future_cursor_epoch_replays_the_whole_current_epoch(
    db: Database, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    mirror = _setup(db, conn, tmp_path)

    async def scenario() -> ReplayBatch:
        for seq in (0, 1):
            await mirror.append(epoch=1, seq=seq, event=_plain(f"tool_{seq}"))
        return await replay_batch(db, "scan-1", resume_from=ReplayCursor(epoch=9, seq=0), ts=_TS)

    batch = asyncio.run(scenario())

    assert _seqs(batch) == [0, 1]
    assert batch.epoch == 1


def test_no_cursor_replays_the_whole_current_epoch(
    db: Database, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    mirror = _setup(db, conn, tmp_path)

    async def scenario() -> ReplayBatch:
        for seq in (0, 1, 2):
            await mirror.append(epoch=1, seq=seq, event=_plain(f"tool_{seq}"))
        return await replay_batch(db, "scan-1", resume_from=None, ts=_TS)

    batch = asyncio.run(scenario())

    assert _seqs(batch) == [0, 1, 2]
    assert batch.next_cursor is None


def test_only_the_latest_epoch_is_replayed(
    db: Database, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """上一代的帧永远不回：客户端拿到它们只会把已作废的状态又贴回去。"""
    mirror = _setup(db, conn, tmp_path)

    async def scenario() -> ReplayBatch:
        await mirror.append(epoch=0, seq=0, event=_plain("tool_old"))
        await mirror.append(epoch=1, seq=0, event=_plain("tool_new"))
        return await replay_batch(db, "scan-1", resume_from=None, ts=_TS)

    batch = asyncio.run(scenario())

    assert _keys(batch) == ["tool_new"]
    assert batch.epoch == 1


def test_default_limit_is_a_bounded_page() -> None:
    """默认页大小是一条不变式，不是口味。

    回放的调用方是 WS 路由，它会拿 `next_cursor` 一页页取；**页大小没有上界**就等于
    "一个客户端重连能把整条扫描的事件一次读进内存并塞进一个 send 循环"。
    盯住这个字面值本身：`assert 页大小来自 DEFAULT_LIMIT` 钉不住"那个值是几"
    （本仓同一形状已经烧过五次，见 PLAN.md §交接 的判据 ④）。
    """
    assert 0 < DEFAULT_LIMIT <= 1000


def test_empty_table_returns_an_empty_batch(db: Database) -> None:
    batch = asyncio.run(replay_batch(db, "scan-1", resume_from=None, ts=_TS))

    assert batch == ReplayBatch(frames=(), epoch=0, next_cursor=None)


def test_unknown_scan_id_returns_an_empty_batch(
    db: Database, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """「这个扫描存不存在」由路由层查 `scans` 表回答，这里不抛。"""
    mirror = _setup(db, conn, tmp_path)

    async def scenario() -> ReplayBatch:
        await mirror.append(epoch=1, seq=0, event=_plain())
        return await replay_batch(db, "scan-404", resume_from=None, ts=_TS)

    assert asyncio.run(scenario()) == ReplayBatch(frames=(), epoch=0, next_cursor=None)


def test_rows_of_another_scan_are_never_replayed(
    db: Database, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """另一条扫描的 epoch 刻意更大：`MAX(epoch)` 漏了 scan_id 过滤，这里就会空手而归。"""
    mirror = _setup(db, conn, tmp_path)
    insert_scan(conn, "scan-2")
    other = _mirror(db, tmp_path, "scan-2")

    async def scenario() -> ReplayBatch:
        await mirror.append(epoch=1, seq=0, event=_plain("tool_mine"))
        await other.append(epoch=7, seq=0, event=_plain("tool_theirs"))
        return await replay_batch(db, "scan-1", resume_from=None, ts=_TS)

    batch = asyncio.run(scenario())

    assert _keys(batch) == ["tool_mine"]
    assert batch.epoch == 1


def test_pagination_walks_every_row_exactly_once(
    db: Database, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    mirror = _setup(db, conn, tmp_path)

    async def scenario() -> list[ReplayBatch]:
        for seq in range(5):
            await mirror.append(epoch=1, seq=seq, event=_plain(f"tool_{seq}"))
        batches: list[ReplayBatch] = []
        cursor: ReplayCursor | None = None
        while True:
            batch = await replay_batch(db, "scan-1", resume_from=cursor, ts=_TS, limit=2)
            batches.append(batch)
            if batch.next_cursor is None:
                return batches
            cursor = batch.next_cursor

    batches = asyncio.run(scenario())

    assert [_seqs(batch) for batch in batches] == [[0, 1], [2, 3], [4]]
    assert batches[0].next_cursor == ReplayCursor(epoch=1, seq=1)
    assert batches[1].next_cursor == ReplayCursor(epoch=1, seq=3)


def test_full_batch_sets_a_cursor_even_when_nothing_remains(
    db: Database, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """取满了就给游标（不预读一行判断有没有下一页），所以最后可能多空跑一趟。"""
    mirror = _setup(db, conn, tmp_path)

    async def scenario() -> tuple[ReplayBatch, ReplayBatch]:
        for seq in (0, 1):
            await mirror.append(epoch=1, seq=seq, event=_plain(f"tool_{seq}"))
        first = await replay_batch(db, "scan-1", resume_from=None, ts=_TS, limit=2)
        assert first.next_cursor is not None
        return first, await replay_batch(
            db, "scan-1", resume_from=first.next_cursor, ts=_TS, limit=2
        )

    first, second = asyncio.run(scenario())

    assert first.next_cursor == ReplayCursor(epoch=1, seq=1)
    assert second.frames == ()
    assert second.epoch == 1
    assert second.next_cursor is None


def test_frames_are_ordered_by_seq(db: Database, conn: sqlite3.Connection, tmp_path: Path) -> None:
    """乱序写入后仍按 seq 回放。

    ⚠️ **这条测试盯不住 `ORDER BY seq` 那句 SQL**（2026-09-22 收货实测：删掉它全仓一条不红）——
    `PRIMARY KEY (scan_id, epoch, seq)` 的索引让这个 WHERE 天然就按 seq 出行，所以插入顺序
    改变不了结果。留着 `ORDER BY` 是**显式声明**而不是被测行为：将来加一个走别的索引的
    过滤条件时，它是唯一还站着的那道保证。`strix_id` 重排后会变，永远不许拿它排序。
    """
    mirror = _setup(db, conn, tmp_path)

    async def scenario() -> ReplayBatch:
        await mirror.append(epoch=1, seq=2, event=_plain("tool_second"))
        await mirror.append(epoch=1, seq=1, event=_plain("tool_first"))
        return await replay_batch(db, "scan-1", resume_from=None, ts=_TS)

    batch = asyncio.run(scenario())

    assert _seqs(batch) == [1, 2]
    assert _keys(batch) == ["tool_first", "tool_second"]
