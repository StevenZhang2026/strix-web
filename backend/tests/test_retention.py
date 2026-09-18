"""留存清理（T28a）：判定 + 破坏性执行器。

这里没有真实 `${DATA}` —— `scans_dir` 是 `tmp_path` 下的一棵真目录树，删的也是真文件。
接缝是**构造参数**（`db` / `scans_dir` / `retention_days`），所以"该删谁"与"删了什么"
两件事都真的被执行了，不靠替身。

`asyncio.run(scenario())` 而不是 `async def test_`：本仓刻意没有 `pytest-asyncio`。
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.db import Database
from app.services.retention import (
    PURGED_TABLES,
    SKIP_TMP_DIR_PRESENT,
    InvalidScanIdError,
    RetentionOutcome,
    RetentionSweeper,
    ScanRecord,
    plan_retention,
    scan_dir_for,
)
from tests.conftest import insert_authorization, insert_scan

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


def test_purged_tables_is_exactly_the_four_detail_tables() -> None:
    """`scans` / `authorizations` / `audit_log` 永不在内；`report_translations`
    是 T21 的 002 迁移才建的表，现在把它写进来会直接 `no such table`。"""
    assert PURGED_TABLES == ("scan_events", "scan_media", "scan_agents", "scan_findings")


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
    """一行 `scans` + 4 张明细表各一行。

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


def detail_rows(conn: sqlite3.Connection, scan_id: str) -> dict[str, int]:
    """4 张明细表里属于这次扫描的行数。表名来自模块常量，不是入参。"""
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
    db: Database, scans_dir: Path, *, retention_days: int = 1, dry_run: bool = False
) -> RetentionOutcome:
    sweeper = RetentionSweeper(db, scans_dir, retention_days=retention_days)
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
