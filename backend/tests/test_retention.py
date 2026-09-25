"""留存清理（T28a）：判定 + 破坏性执行器。

这里没有真实 `${DATA}` —— `scans_dir` 是 `tmp_path` 下的一棵真目录树，删的也是真文件。
接缝是**构造参数**（`db` / `scans_dir` / `retention_days`），所以"该删谁"与"删了什么"
两件事都真的被执行了，不靠替身。

`asyncio.run(scenario())` 而不是 `async def test_`：本仓刻意没有 `pytest-asyncio`。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import Database
from app.services.audit import EVENT_SCAN_PURGED
from app.services.retention import (
    PURGED_TABLES,
    SKIP_TMP_DIR_PRESENT,
    InvalidScanIdError,
    RetentionOutcome,
    RetentionSweeper,
    ScanRecord,
    _remove_dir,
    plan_retention,
    scan_dir_for,
)
from app.settings import Settings
from tests.conftest import FakeTransport, const, insert_authorization, insert_scan

NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
# `now - 30 天` 那一刻，逐微秒对齐：边界用例的整个意义就在这一微秒上。
CUTOFF_30D = "2026-08-19T12:00:00.000000Z"
JUST_BEFORE_CUTOFF_30D = "2026-08-19T11:59:59.999999Z"
ANCIENT = "2016-01-01T00:00:00.000000Z"
LESS_ANCIENT = "2018-01-01T00:00:00.000000Z"
ONE_HOUR_AGO = "2026-09-18T11:00:00.000000Z"


def rec(scan_id: str, status: str = "completed", finished_at: str | None = ANCIENT) -> ScanRecord:
    return ScanRecord(scan_id=scan_id, status=status, finished_at=finished_at)


# =============================================================================
# I-1 选谁 —— 纯函数，表驱动
# =============================================================================
@pytest.mark.parametrize(
    ("records", "retention_days", "expected"),
    [
        pytest.param((rec("s-1"),), 30, ("s-1",), id="终态且超期就入选"),
        pytest.param(
            tuple(rec(f"s-{s}", s) for s in ("completed", "stopped", "failed", "interrupted")),
            30,
            ("s-completed", "s-failed", "s-interrupted", "s-stopped"),
            id="四种终态都可入选",
        ),
        pytest.param((rec("s-1", "running"), rec("s-2", "starting")), 30, (), id="活的永不入选"),
        pytest.param((rec("s-1"),), 0, (), id="retention_days=0 恒为空"),
        pytest.param((rec("s-1"),), -7, (), id="retention_days 为负恒为空"),
        pytest.param((rec("s-1", finished_at=None),), 30, (), id="finished_at 缺失永不入选"),
        pytest.param((rec("s-1", finished_at=""),), 30, (), id="finished_at 空串永不入选"),
        pytest.param((rec("s-1", finished_at="上周三"),), 30, (), id="finished_at 乱码永不入选"),
        pytest.param((rec("s-1", finished_at=CUTOFF_30D),), 30, (), id="正好等于边界不删"),
        pytest.param(
            (rec("s-1", finished_at=JUST_BEFORE_CUTOFF_30D),),
            30,
            ("s-1",),
            id="早于边界一微秒就删",
        ),
        pytest.param(
            (rec("s-new", finished_at=LESS_ANCIENT), rec("s-old", finished_at=ANCIENT)),
            30,
            ("s-old", "s-new"),
            id="按 finished_at 升序返回",
        ),
    ],
)
def test_plan_retention_picks_only_aged_terminal_scans(
    records: tuple[ScanRecord, ...], retention_days: int, expected: tuple[str, ...]
) -> None:
    """I-1 全部五条。乱码/空串那两行同时也断言了"不许抛"（抛了这个测试就 error）。"""
    assert plan_retention(records, now=NOW, retention_days=retention_days) == expected


# =============================================================================
# I-2① 路径护栏
# =============================================================================
@pytest.mark.parametrize(
    "scan_id",
    [
        pytest.param("", id="空串"),
        pytest.param(".", id="单个点"),
        pytest.param("..", id="两个点"),
        pytest.param("../escape", id="相对逃逸"),
        pytest.param("a/b", id="含斜杠"),
        pytest.param("sub/../../etc", id="绕回上层"),
        pytest.param("scan\\1", id="含反斜杠"),
        pytest.param("/etc", id="绝对路径"),
        pytest.param("/etc/passwd", id="绝对路径带子路径"),
        pytest.param("scan\x001", id="含 NUL"),
    ],
)
def test_scan_dir_for_rejects_every_scan_id_that_is_not_a_direct_child(
    scans_dir: Path, scan_id: str
) -> None:
    with pytest.raises(InvalidScanIdError) as caught:
        scan_dir_for(scans_dir, scan_id)
    assert caught.value.code == "invalid_scan_id"


def test_scan_dir_for_rejects_a_symlink_that_leaves_scans_dir(
    tmp_path: Path, scans_dir: Path
) -> None:
    """字符串黑名单抓不到这一条 —— 判据必须是 `resolve()` 之后的父目录。"""
    outside = tmp_path / "someone-elses-data"
    outside.mkdir()
    (scans_dir / "looks-fine").symlink_to(outside, target_is_directory=True)

    with pytest.raises(InvalidScanIdError):
        scan_dir_for(scans_dir, "looks-fine")


def test_scan_dir_for_returns_the_resolved_direct_child(scans_dir: Path) -> None:
    assert scan_dir_for(scans_dir, "scan-1") == scans_dir.resolve() / "scan-1"


def test_purged_tables_is_exactly_the_five_detail_tables() -> None:
    """`scans` / `authorizations` / `audit_log` 永不在内。"""
    assert PURGED_TABLES == (
        "scan_events",
        "scan_media",
        "scan_agents",
        "scan_findings",
        "report_translations",
    )


# =============================================================================
# I-2②③ 执行器
# =============================================================================
@pytest.fixture
def scans_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "scans"
    path.mkdir(parents=True)
    return path


def seed_scan(
    conn: sqlite3.Connection,
    scan_id: str,
    *,
    status: str = "completed",
    finished_at: str | None = ANCIENT,
) -> None:
    """一行 `scans` + 5 张明细表各一行。

    `insert_scan`（conftest）只会写 `queued` 且不写 `finished_at`，而留存判定读的正是
    这两列 —— 所以补一条 UPDATE。**不改 conftest**：那是本批次之后统一提取的事。
    """
    insert_scan(conn, scan_id=scan_id)
    conn.execute(
        "UPDATE scans SET status = ?, finished_at = ? WHERE id = ?",
        (status, finished_at, scan_id),
    )
    conn.execute(
        "INSERT INTO scan_events (scan_id, epoch, seq, kind, fingerprint, data_json)"
        " VALUES (?, 0, 0, 'tool_call', 'fp-1', '{}')",
        (scan_id,),
    )
    conn.execute(
        "INSERT INTO scan_agents (scan_id, agent_id, created_at, updated_at)"
        " VALUES (?, 'agent-1', ?, ?)",
        (scan_id, ANCIENT, ANCIENT),
    )
    conn.execute(
        "INSERT INTO scan_findings (scan_id, finding_id, severity, title, first_seen_at,"
        " raw_json, input_hash) VALUES (?, 'f-1', 'high', 'XSS', ?, '{}', ?)",
        (scan_id, ANCIENT, "a" * 64),
    )
    conn.execute(
        "INSERT INTO scan_media (scan_id, sha256, mime, bytes, rel_path, first_seen_at)"
        " VALUES (?, ?, 'image/png', 12, 'media/x.png', ?)",
        (scan_id, "b" * 64, ANCIENT),
    )
    conn.execute(
        "INSERT INTO report_translations (scan_id, finding_id, input_hash, model, lang,"
        " payload_json, created_at) VALUES (?, 'f-1', ?, 'm', 'zh-CN', '{}', ?)",
        (scan_id, "a" * 64, ANCIENT),
    )


def detail_rows(conn: sqlite3.Connection, scan_id: str) -> dict[str, int]:
    """5 张明细表里属于这次扫描的行数。表名来自模块常量，不是入参。"""
    return {
        table: conn.execute(
            f"SELECT count(*) FROM {table} WHERE scan_id = ?",  # noqa: S608
            (scan_id,),
        ).fetchone()[0]
        for table in PURGED_TABLES
    }


def make_scan_dir(scans_dir: Path, scan_id: str, *, with_tmp: bool = False) -> Path:
    directory = scans_dir / scan_id
    (directory / "strix_runs" / "run-1").mkdir(parents=True)
    (directory / "strix_runs" / "run-1" / "run.json").write_text("{}", encoding="utf-8")
    if with_tmp:
        (directory / "tmp").mkdir()
    return directory


def sweep(
    db: Database,
    scans_dir: Path,
    *,
    retention_days: int = 1,
    dry_run: bool = False,
    audit_dir: Path | None = None,
) -> RetentionOutcome:
    """`audit_dir` 不传时落在 `scans_dir` 隔壁（`${DATA}/audit`），与生产同形。"""
    resolved_audit_dir = audit_dir if audit_dir is not None else scans_dir.parent / "audit"
    resolved_audit_dir.mkdir(parents=True, exist_ok=True)
    sweeper = RetentionSweeper(
        db, scans_dir, retention_days=retention_days, audit_dir=resolved_audit_dir
    )
    return asyncio.run(sweeper.sweep(now=NOW, dry_run=dry_run))


def test_sweep_purges_the_directory_and_detail_rows_but_keeps_the_scan_row(
    db: Database, conn: sqlite3.Connection, scans_dir: Path
) -> None:
    insert_authorization(conn)
    seed_scan(conn, "scan-old")
    seed_scan(conn, "scan-fresh", finished_at=ONE_HOUR_AGO)
    old_dir = make_scan_dir(scans_dir, "scan-old")
    fresh_dir = make_scan_dir(scans_dir, "scan-fresh")

    outcome = sweep(db, scans_dir, retention_days=1)

    assert outcome == RetentionOutcome(purged=("scan-old",), skipped=(), dry_run=False)
    assert not old_dir.exists()
    assert detail_rows(conn, "scan-old") == dict.fromkeys(PURGED_TABLES, 0)
    # 保留期内的那次扫描一个字节都不许动。
    assert fresh_dir.exists()
    assert detail_rows(conn, "scan-fresh") == dict.fromkeys(PURGED_TABLES, 1)
    # 摘要与审计永不删。
    assert [row[0] for row in conn.execute("SELECT id FROM scans ORDER BY id")] == [
        "scan-fresh",
        "scan-old",
    ]
    assert conn.execute("SELECT count(*) FROM authorizations").fetchone()[0] == 1


def test_dry_run_changes_nothing_on_disk_or_in_the_db_but_still_lists_the_plan(
    db: Database, conn: sqlite3.Connection, scans_dir: Path
) -> None:
    insert_authorization(conn)
    seed_scan(conn, "scan-old")
    old_dir = make_scan_dir(scans_dir, "scan-old")

    outcome = sweep(db, scans_dir, retention_days=1, dry_run=True)

    assert outcome == RetentionOutcome(purged=("scan-old",), skipped=(), dry_run=True)
    assert (old_dir / "strix_runs" / "run-1" / "run.json").exists()
    assert detail_rows(conn, "scan-old") == dict.fromkeys(PURGED_TABLES, 1)


def test_a_leftover_tmp_dir_skips_the_scan_entirely(
    db: Database, conn: sqlite3.Connection, scans_dir: Path
) -> None:
    """`tmp/` 是 `cleanup_scan_tmpdir()` 的地盘。它还在 = 监管者没收尾完（或这次扫描
    其实还活着），这时候删就是抢别人的东西 —— 目录和 DB 行都不许动。"""
    insert_authorization(conn)
    seed_scan(conn, "scan-old")
    old_dir = make_scan_dir(scans_dir, "scan-old", with_tmp=True)

    outcome = sweep(db, scans_dir, retention_days=1)

    assert outcome == RetentionOutcome(
        purged=(), skipped=(("scan-old", SKIP_TMP_DIR_PRESENT),), dry_run=False
    )
    assert (old_dir / "tmp").exists()
    assert detail_rows(conn, "scan-old") == dict.fromkeys(PURGED_TABLES, 1)


def test_a_scan_id_that_is_not_a_direct_child_is_skipped_without_aborting_the_round(
    db: Database, conn: sqlite3.Connection, scans_dir: Path
) -> None:
    """`scans.id` 只是 TEXT —— 一行脏数据不许让此后所有清理都停摆。"""
    insert_authorization(conn)
    seed_scan(conn, "../escape", finished_at=ANCIENT)
    seed_scan(conn, "scan-old", finished_at=LESS_ANCIENT)
    old_dir = make_scan_dir(scans_dir, "scan-old")

    outcome = sweep(db, scans_dir, retention_days=1)

    assert outcome == RetentionOutcome(
        purged=("scan-old",), skipped=(("../escape", "invalid_scan_id"),), dry_run=False
    )
    assert not old_dir.exists()
    assert scans_dir.exists()


# =============================================================================
# F. 接线 —— 配置项、定时任务、审计事件、lifespan
# =============================================================================
@pytest.fixture
def audit_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "audit"
    path.mkdir(parents=True)
    return path


async def _drain_until(predicate: Callable[[], bool], *, ticks: int = 200) -> None:
    """让出控制权直到 `predicate` 成立（或让够 `ticks` 次）。

    不断言"让够了就一定成立" —— 那是各条测试自己的事，这里只负责别死等。
    """
    for _ in range(ticks):
        if predicate():
            return
        await asyncio.sleep(0.01)


async def _cancel(task: asyncio.Task[None]) -> None:
    """cancel **并 await**，形状同 lifespan 的 `finally`。"""
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def test_run_forever_returns_immediately_when_retention_is_disabled(
    db: Database, conn: sqlite3.Connection, scans_dir: Path, audit_dir: Path
) -> None:
    """F1。`retention_days=0` 是默认值，也是这个开关的唯一落点。

    **自己就返回**这件事本身就是断言：进了循环的话它会睡在 `interval` 上。`wait_for`
    只是给那种情况一个有限的失败时间 —— 不然这条测试会挂死而不是变红。
    """
    insert_authorization(conn)
    seed_scan(conn, "scan-old")
    old_dir = make_scan_dir(scans_dir, "scan-old")
    sweeper = RetentionSweeper(db, scans_dir, retention_days=0, audit_dir=audit_dir)

    async def scenario() -> None:
        await asyncio.wait_for(sweeper.run_forever(interval=1000.0), timeout=5.0)

    asyncio.run(scenario())

    assert (old_dir / "strix_runs" / "run-1" / "run.json").exists()
    assert detail_rows(conn, "scan-old") == dict.fromkeys(PURGED_TABLES, 1)
    assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 0


def test_run_forever_sweeps_once_at_startup_without_waiting_for_the_interval(
    db: Database, conn: sqlite3.Connection, scans_dir: Path, audit_dir: Path
) -> None:
    """F2。`interval=1000.0`：要是第一轮排在 sleep 之后，这条测试就等不到删除。"""
    insert_authorization(conn)
    seed_scan(conn, "scan-old")
    old_dir = make_scan_dir(scans_dir, "scan-old")
    sweeper = RetentionSweeper(db, scans_dir, retention_days=1, audit_dir=audit_dir)

    async def scenario() -> None:
        task = asyncio.create_task(sweeper.run_forever(interval=1000.0))
        await _drain_until(lambda: not old_dir.exists())
        await _cancel(task)

    asyncio.run(scenario())

    assert not old_dir.exists()


def test_run_forever_survives_a_permission_error_and_lives_into_the_next_round(
    db: Database,
    conn: sqlite3.Connection,
    scans_dir: Path,
    audit_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F3。`_remove_dir` 对 `PermissionError` 是重抛的 —— 那一次重抛不许把循环带走。"""
    insert_authorization(conn)
    seed_scan(conn, "scan-old")
    old_dir = make_scan_dir(scans_dir, "scan-old")
    attempts: list[Path] = []

    def flaky_remove(directory: Path) -> None:
        attempts.append(directory)
        if len(attempts) == 1:
            raise PermissionError(13, "Permission denied")
        _remove_dir(directory)

    monkeypatch.setattr("app.services.retention._remove_dir", flaky_remove)
    sweeper = RetentionSweeper(db, scans_dir, retention_days=1, audit_dir=audit_dir)

    async def scenario() -> None:
        task = asyncio.create_task(sweeper.run_forever(interval=0.01))
        await _drain_until(lambda: len(attempts) >= 2 and not old_dir.exists())
        await _cancel(task)

    asyncio.run(scenario())

    assert len(attempts) >= 2, "第一轮抛了之后循环就没了"
    assert not old_dir.exists()


def test_purging_writes_one_scan_purged_audit_to_both_the_table_and_the_ndjson(
    db: Database, conn: sqlite3.Connection, scans_dir: Path, settings: Settings
) -> None:
    """F4。审计是双写的（DB 丢了 ndjson 还在），`detail` 里只有机器码与数字。"""
    insert_authorization(conn)
    seed_scan(conn, "scan-old")
    make_scan_dir(scans_dir, "scan-old")

    outcome = sweep(db, scans_dir, retention_days=1, audit_dir=settings.audit_dir)

    assert outcome.purged == ("scan-old",)
    expected_detail = {"retention_days": 1, "tables": list(PURGED_TABLES)}
    rows = conn.execute(
        "SELECT event, actor, scan_id, detail_json FROM audit_log WHERE event = ?",
        (EVENT_SCAN_PURGED,),
    ).fetchall()
    assert len(rows) == 1
    # `actor` 为空是契约的一部分：这条事件由定时任务发起，没有操作者。
    assert rows[0][1] is None
    assert rows[0][2] == "scan-old"
    assert json.loads(rows[0][3]) == expected_detail

    files = sorted(settings.audit_dir.glob("*.ndjson"))
    assert len(files) == 1
    mirrored = json.loads(files[0].read_text(encoding="utf-8"))
    assert mirrored["event"] == EVENT_SCAN_PURGED
    assert mirrored["scan_id"] == "scan-old"
    assert mirrored["actor"] is None
    assert mirrored["detail"] == expected_detail


def test_dry_run_writes_no_audit_at_all(
    db: Database, conn: sqlite3.Connection, scans_dir: Path, settings: Settings
) -> None:
    """F5。`dry_run` 什么都没删，所以"删掉了什么"这条审计也不许存在。"""
    insert_authorization(conn)
    seed_scan(conn, "scan-old")
    make_scan_dir(scans_dir, "scan-old")

    sweep(db, scans_dir, retention_days=1, dry_run=True, audit_dir=settings.audit_dir)

    assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 0
    assert list(settings.audit_dir.glob("*.ndjson")) == []


class _RecordingSweeper:
    """记下构造参数，以及"我是在 `db.close()` **之前**被 cancel 的"。刻意不继承。

    **只断言"最后 cancelled 为真"是无效的**（`asyncio.run` 收尾时会把剩下的任务统统
    cancel 并 await 一遍）。有区别的是**时机** —— 所以这里记的是顺序。
    """

    events: ClassVar[list[str]] = []
    positional: ClassVar[list[tuple[object, ...]]] = []
    keywords: ClassVar[list[dict[str, object]]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        _RecordingSweeper.positional.append(args)
        _RecordingSweeper.keywords.append(kwargs)

    async def run_forever(self) -> None:
        _RecordingSweeper.events.append("started")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            _RecordingSweeper.events.append("cancelled")
            raise


def test_lifespan_starts_the_sweeper_and_cancels_it_before_db_close(
    app: FastAPI, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F6。构造参数、任务真的起了、cancel 排在 `db.close()` 之前，三件一起钉。"""
    _RecordingSweeper.events.clear()
    _RecordingSweeper.positional.clear()
    _RecordingSweeper.keywords.clear()
    original_close = Database.close

    def recording_close(self: Database) -> None:
        _RecordingSweeper.events.append("db_closed")
        original_close(self)

    monkeypatch.setattr(Database, "close", recording_close)
    monkeypatch.setattr("app.main.RetentionSweeper", _RecordingSweeper)
    # 同一个 lifespan 里的 reaper 会真的去列容器（那是它的第一轮清扫），所以替身得给它
    # 一个"一个泄漏都没有"的应答 —— 不给的话失败信息会指向 reaper 而不是本测试。
    monkeypatch.setattr(
        "app.main.UnixSocketTransport",
        lambda: FakeTransport([("GET", "/containers/json", const(200, []))]),
    )
    with TestClient(app, base_url="https://testserver"):
        pass

    assert len(_RecordingSweeper.positional) == 1
    assert _RecordingSweeper.positional[0][0] is app.state.db
    assert _RecordingSweeper.positional[0][1] == settings.scans_dir
    assert _RecordingSweeper.keywords[0] == {
        "retention_days": settings.console_retention_days,
        "audit_dir": settings.audit_dir,
    }
    assert _RecordingSweeper.events == ["started", "cancelled", "db_closed"]
