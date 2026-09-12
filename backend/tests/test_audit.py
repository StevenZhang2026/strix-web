"""审计双写（T8：`app/services/audit.py`）。

# 这个文件盯的是三件"事后没人能补救"的事

1. **两个落点都真的写了。** 只写表的话，`PLAN.md` §护栏 里"DB 丢了也能 grep"那条
   就是一句空话；只写文件的话界面翻不出页。
2. **两份记的是同一个瞬间。** 两次 `datetime.now(UTC)` 在月末最后一毫秒会让表里记
   10 月、行写进 9 月那份文件 —— 而对账的人无从判断哪一份是对的。
3. **`Z` 不是谎话。** 进程时区非 UTC 时仍然必须是 UTC（pitfalls 条 37 的审计版本）。

# 为什么用 `asyncio.run` 而不是 `async def test_`

本仓刻意没有 `pytest-asyncio`（`pyproject.toml` 的 dev 依赖只有 pytest 与 ruff，
每一个依赖都要论证）。`record()` 是 async 的唯一原因是它要 `await db.run(...)`，
在测试里用 `asyncio.run` 起一个循环跑它，行为与生产完全一致，且不欠一个依赖。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import stat
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.db import Database
from app.services.audit import EVENT_ALLOWLIST_CHANGED, AuditEntry, record
from app.settings import Settings

ACTOR = "operator"


def entry(**overrides: object) -> AuditEntry:
    """一条典型记录。`detail` 里刻意有中文与非字符串值 —— 两者都要能活着往返。"""
    fields: dict[str, object] = {
        "event": EVENT_ALLOWLIST_CHANGED,
        "actor": ACTOR,
        "detail": {"operation": "add", "entry_count": 2, "label": "客户预生产环境"},
        "client_ip": "192.0.2.9",
        "user_agent": "Mozilla/5.0",
    }
    fields.update(overrides)
    return AuditEntry(**fields)  # type: ignore[arg-type]


def write(db: Database, audit_dir: Path, item: AuditEntry) -> None:
    asyncio.run(record(db=db, audit_dir=audit_dir, entry=item))


def only_row(conn: sqlite3.Connection) -> sqlite3.Row:
    rows = conn.execute("SELECT * FROM audit_log").fetchall()
    assert len(rows) == 1, f"期望恰好一行审计，实得 {len(rows)} 行"
    return rows[0]


def only_line(audit_dir: Path) -> dict[str, object]:
    files = sorted(audit_dir.glob("*.ndjson"))
    assert len(files) == 1, f"期望恰好一个 ndjson 文件，实得 {[f.name for f in files]}"
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, f"期望恰好一行，实得 {len(lines)} 行"
    parsed = json.loads(lines[0])
    assert isinstance(parsed, dict)
    return parsed


# =============================================================================
# 一、两个落点
# =============================================================================
def test_record_writes_the_table(
    db: Database, settings: Settings, conn: sqlite3.Connection
) -> None:
    write(db, settings.audit_dir, entry())
    row = only_row(conn)
    assert row["event"] == EVENT_ALLOWLIST_CHANGED
    assert row["actor"] == ACTOR
    assert row["client_ip"] == "192.0.2.9"
    assert row["user_agent"] == "Mozilla/5.0"
    # 表里那一列**必须是字符串**：schema 上挂着 `CHECK (json_valid(detail_json))`。
    assert isinstance(row["detail_json"], str)
    assert json.loads(row["detail_json"])["label"] == "客户预生产环境"
    # T8 只有一个事件，两列都留空。T12 会填它们。
    assert row["scan_id"] is None
    assert row["authorization_id"] is None


def test_record_writes_the_ndjson_mirror(db: Database, settings: Settings) -> None:
    write(db, settings.audit_dir, entry())
    line = only_line(settings.audit_dir)
    assert line["event"] == EVENT_ALLOWLIST_CHANGED
    assert line["actor"] == ACTOR
    # **这一份的 detail 是嵌套对象**，不是 JSON 字符串 —— 用途是 jq/grep。
    detail = line["detail"]
    assert isinstance(detail, dict)
    assert detail["entry_count"] == 2


def test_ndjson_keeps_chinese_readable(db: Database, settings: Settings) -> None:
    """`ensure_ascii=False`。这一份存在的理由就是能被 `grep` ——
    而 `grep 客户预生产环境` 命中不了 `\\u5ba2\\u6237`。
    """
    write(db, settings.audit_dir, entry())
    raw = next(settings.audit_dir.glob("*.ndjson")).read_text(encoding="utf-8")
    assert "客户预生产环境" in raw


def test_ndjson_is_owner_only(db: Database, settings: Settings) -> None:
    """0600，与 `auth.json` / `allowlist.yaml` 一致。

    里面没有凭据，但它记着一份内网主机名与授权编号的历史 —— 对想横向移动的人来说
    那是现成的侦察结果。
    """
    write(db, settings.audit_dir, entry())
    mode = stat.S_IMODE(next(settings.audit_dir.glob("*.ndjson")).stat().st_mode)
    assert mode == 0o600, f"ndjson 权限是 {mode:o}，应为 600"


def test_records_append_and_do_not_overwrite(db: Database, settings: Settings) -> None:
    write(db, settings.audit_dir, entry())
    write(db, settings.audit_dir, entry(detail={"operation": "remove"}))
    lines = next(settings.audit_dir.glob("*.ndjson")).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_audit_dir_is_created_if_missing(db: Database, tmp_path: Path) -> None:
    """`main.py` 的 `_prepare_data_dir` 会建这个目录，但审计**不能依赖**那一步。

    审计是这个系统里最不该因为"目录恰好不在"而丢记录的东西。
    """
    audit_dir = tmp_path / "brand-new" / "audit"
    assert not audit_dir.exists()
    write(db, audit_dir, entry())
    assert only_line(audit_dir)["event"] == EVENT_ALLOWLIST_CHANGED


# =============================================================================
# 二、同一个瞬间
# =============================================================================
def test_both_sinks_share_one_instant(
    db: Database, settings: Settings, conn: sqlite3.Connection
) -> None:
    """表里的 `at`、文件里的 `at`、以及**文件名里的年月**三者必须一致。

    文件名那一半是关键：它来自同一个 `moment`，所以月末跨秒时不会出现"表里 10 月、
    行写进 9 月那份文件"。两次 `now()` 的实现会在 99.99% 的运行里通过这条断言，
    但它在月末那一毫秒失败 —— 而那正是审计最需要可信的时刻。这里能确定性地测到的
    是"三处取自同一个值"这个结构，靠的是文件名与 `at` 的强一致。
    """
    write(db, settings.audit_dir, entry())
    row_at = only_row(conn)["at"]
    line = only_line(settings.audit_dir)
    assert line["at"] == row_at
    file_name = next(settings.audit_dir.glob("*.ndjson")).name
    assert file_name == f"{str(row_at)[:7]}.ndjson", (
        f"文件名 {file_name} 与 at={row_at} 的年月不一致 —— 多半是取了两次 now()"
    )


def test_at_is_iso_with_z(db: Database, settings: Settings, conn: sqlite3.Connection) -> None:
    write(db, settings.audit_dir, entry())
    at = only_row(conn)["at"]
    assert isinstance(at, str) and at.endswith("Z"), f"at={at!r} 不是 `...Z` 形状"
    parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert abs((datetime.now(UTC) - parsed).total_seconds()) < 60


def test_at_is_really_utc_under_a_non_utc_tz(
    db: Database,
    settings: Settings,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """把进程时区改成 `Asia/Shanghai`，`at` 仍然必须是 UTC。

    不改时区这条测试在容器里恒真（容器默认 `TZ` 未设 = UTC），等于没测 ——
    与 `test_redaction.py::test_ts_is_really_utc_even_under_a_non_utc_tz` 同一条理由。
    审计与日志、与 DB 其它时间戳要能对账，差一个时区偏移且没人知道是最费人的一类问题。
    """
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    monkeypatch.setattr(time, "tzset", lambda: None, raising=True)
    now = time.time()
    # 自证前提：这个时区确实与 UTC 有偏移，否则本测试是空的。
    assert time.localtime(now)[3] != time.gmtime(now)[3], "选的时区没偏移，测试无意义"

    write(db, settings.audit_dir, entry())
    at = str(only_row(conn)["at"])
    hour = datetime.fromisoformat(at.replace("Z", "+00:00")).hour
    assert hour == time.gmtime()[3], f"at={at} 用的是本地时间，不是 UTC"


# =============================================================================
# 三、未校验输入
# =============================================================================
def test_user_agent_is_truncated(
    db: Database, settings: Settings, conn: sqlite3.Connection
) -> None:
    """User-Agent 完全由客户端控制。一行几十 KB 的审计等于一行没法 grep 的审计。"""
    write(db, settings.audit_dir, entry(user_agent="A" * 5000))
    stored = only_row(conn)["user_agent"]
    assert isinstance(stored, str)
    assert len(stored) == 200
    # 两个落点截的是同一份，不许一边全一边短。
    assert only_line(settings.audit_dir)["user_agent"] == stored


def test_missing_actor_and_ip_are_recorded_as_null(
    db: Database, settings: Settings, conn: sqlite3.Connection
) -> None:
    """取不到操作者时**照样写这条记录**（`actor` 列可空）。

    宁可缺一个字段，也不要因为拿不到用户名而丢掉整条"授权清单被改了"。
    """
    write(db, settings.audit_dir, entry(actor=None, client_ip=None, user_agent=None))
    row = only_row(conn)
    assert row["actor"] is None
    assert row["client_ip"] is None
    assert row["user_agent"] is None
