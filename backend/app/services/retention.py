"""留存清理（T28a）：挑出"已终态且超过保留期"的扫描，删掉它的产物目录与明细行。

**这个模块会删用户的数据。** 所以形状照 `reaper.py`（本仓另一个破坏性操作）：判定是
无 IO 的纯函数（`plan_retention` / `scan_dir_for`，"该删谁"必须能不碰磁盘直接单测）、
动手之前把整条计划打进日志（`dry_run` 与否都打）、`dry_run=True` 一个字节都不动、
**默认不删**（`retention_days <= 0` = 永不清理，而它就是默认值）。

**保留什么**：`scans` 那一行（历史列表还要看得到摘要、结论计数、成本）、
`authorizations`、`audit_log`、`${DATA}/audit/*.ndjson` —— 审计永不删。

`retention_days` 与 `scans_dir` 都从**构造参数**进来，刻意不 import `Settings`：
定时任务的接线（谁多久调一次、天数从哪个配置读）是另一条任务的事。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import ClassVar

from app.db import Database

logger = logging.getLogger(__name__)

# 后四个是终态，`starting` / `running` 是活的（`scan_supervisor.SCAN_STATUSES`）。
TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "stopped", "failed", "interrupted"})

# 要删行的明细表。**顺序无关，但清单本身是契约**：
# `report_translations`（T21 的 002 迁移才建）现在还不存在，写进来会 `no such table`；
# `scans` / `authorizations` / `audit_log` 则是永不删的。
PURGED_TABLES: tuple[str, ...] = ("scan_events", "scan_media", "scan_agents", "scan_findings")

# `skipped` 的机器码。中文文案是前端的事。
SKIP_TMP_DIR_PRESENT = "tmp_dir_present"

# 见 `scan_dir_for`。反斜杠在 POSIX 上是合法文件名字符，`resolve()` 抓不到它。
_FORBIDDEN_IN_SCAN_ID = ("/", "\\", "\x00")


class InvalidScanIdError(Exception):
    """`scan_id` 没法安全地当成 `scans_dir` 下的一个直接子目录名。

    刻意**不继承 `ConsoleError`**：这个错误永远不经 HTTP 返回（清理是后台任务），
    而 `ConsoleError` 的直接子类会被 `test_message_coverage` 要求配一条中文文案。
    机器码仍然有（`code`），供 `RetentionOutcome.skipped` 记录。
    """

    code: ClassVar[str] = "invalid_scan_id"

    def __init__(self, scan_id: str) -> None:
        # args 里只放码：任何 `str(exc)` 都不会把脏 id 带进日志正文。
        super().__init__(self.code)
        self.scan_id = scan_id


@dataclass(frozen=True, slots=True)
class ScanRecord:
    """判定函数的输入。刻意不吃 `sqlite3.Row` —— 判定要能不碰 DB 直接单测。"""

    scan_id: str
    status: str
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class RetentionOutcome:
    """一次清扫的结果。`skipped` 的第二项是稳定机器码。"""

    purged: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]
    dry_run: bool


def _as_utc(moment: datetime) -> datetime:
    """把 naive 当 UTC。naive 与 aware 相减/比较会抛 `TypeError`，那是一个只在
    "调用方忘了带 tzinfo"时才发作的崩溃 —— 而这里宁可少删也不许崩。"""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _parse_finished_at(raw: str) -> datetime | None:
    """`2026-09-18T12:34:56.789012Z` → aware datetime（3.12 的 `fromisoformat` 吃 `Z`）。

    **不许把解析失败抛出去**：一行坏时间戳不该让整轮清理停摆，而"看不懂的时间戳"
    对留存判定的唯一合理解读是"不知道多老，那就不删"。
    """
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return _as_utc(parsed)


def plan_retention(
    scans: Sequence[ScanRecord], *, now: datetime, retention_days: int
) -> tuple[str, ...]:
    """挑出该清理的 scan_id（按 `finished_at` 升序）。纯函数、无 IO。"""
    if retention_days <= 0:
        # **默认不删。** 这是整个模块唯一的"关掉"开关，也是它的默认值 ——
        # 破坏性操作不许默认开启。
        return ()
    cutoff = _as_utc(now) - timedelta(days=retention_days)
    aged: list[tuple[datetime, str]] = []
    for record in scans:
        if record.status not in TERMINAL_STATUSES:
            # 活着的扫描正在往那个目录里写。
            continue
        if record.finished_at is None:
            # 没有结束时间就算不出年龄。宁可不删。
            continue
        finished = _parse_finished_at(record.finished_at)
        if finished is None:
            continue
        # 严格更早才删：正好等于边界的那一次留着（少删一天没有代价，多删有）。
        if finished < cutoff:
            aged.append((finished, record.scan_id))
    aged.sort()
    return tuple(scan_id for _, scan_id in aged)


def scan_dir_for(scans_dir: Path, scan_id: str) -> Path:
    """算出该扫描的目录，并拒绝任何能逃出 `scans_dir` 的 `scan_id`。纯函数。

    判据是**算完再验**：`resolve()` 之后它的父目录必须**恰好**是 `scans_dir.resolve()`。
    不是字符串黑名单 —— 黑名单永远漏一种写法，而且抓不到符号链接（`scans/looks-fine`
    指向别人的目录时字符串完全干净）。上面那个字符检查只是提前挡掉分隔符与 NUL
    （NUL 会让 `os.lstat` 抛 `ValueError`，那不是我们的机器码）。
    """
    if not scan_id or any(char in scan_id for char in _FORBIDDEN_IN_SCAN_ID):
        raise InvalidScanIdError(scan_id)
    base = scans_dir.resolve()
    candidate = (base / scan_id).resolve()
    if candidate == base or candidate.parent != base:
        raise InvalidScanIdError(scan_id)
    return candidate


def _select_scans(conn: sqlite3.Connection) -> tuple[ScanRecord, ...]:
    """全表扫。`scans` 的量级是"人手工发起过多少次扫描"，不值得为它建索引。"""
    rows = conn.execute("SELECT id, status, finished_at FROM scans").fetchall()
    return tuple(ScanRecord(scan_id=row[0], status=row[1], finished_at=row[2]) for row in rows)


def _delete_detail_rows(conn: sqlite3.Connection, scan_id: str) -> None:
    """删这次扫描的明细行。**不动 `scans` 那一行。**

    表名来自模块常量 `PURGED_TABLES`，不是入参 —— 拼进 SQL 是安全的（S608 的 noqa
    就是为这一点）；`scan_id` 走占位符。
    """
    for table in PURGED_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE scan_id = ?", (scan_id,))  # noqa: S608


def _remove_dir(directory: Path) -> None:
    """删目录。**同步阻塞 IO**，调用方负责 `asyncio.to_thread`。

    目录不存在是合法状态（扫描可能压根没落盘就失败了）；别的 `OSError` 一律重抛 ——
    "删不掉"必须响，不能变成一次静默成功的清理。
    """
    try:
        shutil.rmtree(directory)
    except FileNotFoundError:
        return


class RetentionSweeper:
    """真正持有状态（库、目录、天数），所以是 class。构造**不碰磁盘也不碰 DB**。"""

    def __init__(self, db: Database, scans_dir: Path, *, retention_days: int) -> None:
        self._db = db
        self._scans_dir = scans_dir
        self._retention_days = retention_days

    async def sweep(
        self, *, now: datetime | None = None, dry_run: bool = False
    ) -> RetentionOutcome:
        """跑一轮。`dry_run=True` 时磁盘与 DB 零改动，但计划照样返回。"""
        moment = now if now is not None else datetime.now(UTC)
        records = await self._db.run(_select_scans)
        candidates = plan_retention(records, now=moment, retention_days=self._retention_days)

        skipped: list[tuple[str, str]] = []
        doomed: list[tuple[str, Path]] = []
        for scan_id in candidates:
            try:
                directory = scan_dir_for(self._scans_dir, scan_id)
            except InvalidScanIdError as error:
                # 一行脏数据不许让此后所有清理都停摆（`reaper` 的同一条纪律）。
                skipped.append((scan_id, error.code))
                continue
            if (directory / "tmp").exists():
                # `tmp/` 是 `cleanup_scan_tmpdir()` 的地盘。它还在说明监管者的收尾没跑完
                # ——或者这次扫描其实还活着。这时候删就是抢别人的东西。
                skipped.append((scan_id, SKIP_TMP_DIR_PRESENT))
                continue
            doomed.append((scan_id, directory))

        # **动手之前**先把计划整条打出来（dry_run 与否都打）。
        logger.info(
            "留存清理计划",
            extra={
                "event": "retention_sweep_plan",
                "dry_run": dry_run,
                "retention_days": self._retention_days,
                "doomed": [scan_id for scan_id, _ in doomed],
                "skipped": [f"{scan_id}:{code}" for scan_id, code in skipped],
            },
        )
        if dry_run:
            return RetentionOutcome(
                purged=tuple(scan_id for scan_id, _ in doomed),
                skipped=tuple(skipped),
                dry_run=True,
            )

        purged: list[str] = []
        for scan_id, directory in doomed:
            # 先目录后 DB：目录删不掉就整条不动（明细行还在，下一轮再来）；反过来
            # 会留下"DB 说没有、磁盘上还占着几百 MB"。
            await asyncio.to_thread(_remove_dir, directory)
            await self._db.run(partial(_delete_detail_rows, scan_id=scan_id))
            purged.append(scan_id)
        return RetentionOutcome(purged=tuple(purged), skipped=tuple(skipped), dry_run=False)
