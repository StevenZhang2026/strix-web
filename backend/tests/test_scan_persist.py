"""四张表的落库（W3：`app/services/scan_persist.py`）。

# 这个文件盯的两条不变式（第三条 P3 在 channel 层，见 `test_scan_channel.py`）

1. **计数不许有第二个真相**（`test_duplicate_finding_ids_collapse_and_the_counts_follow_the_rows`）：
   `scans` 的四个 `count_*` 与 `agent_count` / `event_count` 必须从我们刚写下的行上
   `COUNT(*)` 出来。上游给出两条同 `id` 的漏洞时，`severity_counts` 数 2 而我们按 PK 只有
   1 行 —— 抄上游的数就当场打架（端到端验收第 13 条要求两者恒等）。
2. **首见即插，之后一个字都不改**（`test_a_second_sighting_never_rewrites_the_first`）：
   帧层的 `vuln.add` 只推一次，所以库里也不许悄悄换成新版本 —— 否则"客户端收到的"与
   "回放出来的"是两份不一样的东西。

# 为什么用 `asyncio.run` 而不是 `async def test_`

同 `test_event_mirror.py`：本仓刻意没有 `pytest-asyncio`。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Mapping

from app.db import Database
from app.services.run_projector import ProjectedAgent, RunSnapshot
from app.services.scan_persist import ScanPersist
from tests.conftest import insert_authorization, insert_scan

AT = "2026-09-22T01:02:03.456789Z"
"""调用方给的"我们见到它的时刻"。本模块不许自己取时钟，所以它在测试里是个常量。"""

LATER = "2026-09-22T09:09:09.999999Z"


def _persist(db: Database, conn: sqlite3.Connection) -> ScanPersist:
    """建好 FK 依赖的 authorizations / scans 两行，返回被测对象。"""
    insert_authorization(conn)
    insert_scan(conn, "scan-1")
    return ScanPersist(db, "scan-1")


def _record(persist: ScanPersist, snapshot: RunSnapshot, *, epoch: int = 0, at: str = AT) -> None:
    async def scenario() -> None:
        await persist.record(snapshot=snapshot, epoch=epoch, at=at)

    asyncio.run(scenario())


def a_snapshot(**overrides: object) -> RunSnapshot:
    fields: dict[str, object] = {
        "agents": (),
        "events": (),
        "run_status": "running",
        "finished": False,
        "cost_usd": None,
        "severity": {},
        "vulnerabilities": (),
        "report_markdown": "",
    }
    fields.update(overrides)
    return RunSnapshot(**fields)  # type: ignore[arg-type]


def an_agent(agent_id: str = "agent-1", **overrides: object) -> ProjectedAgent:
    fields: dict[str, object] = {
        "id": agent_id,
        "name": "recon",
        "parent_id": None,
        "status": "running",
        "created_at": "2026-09-22T00:00:00.000000Z",
        "updated_at": "2026-09-22T00:00:01.000000Z",
        "error_message": None,
    }
    fields.update(overrides)
    return ProjectedAgent(**fields)  # type: ignore[arg-type]


def a_vuln(**overrides: object) -> Mapping[str, object]:
    entry: dict[str, object] = {
        "id": "vuln-1",
        "title": "SQL 注入",
        "severity": "critical",
        "cvss": 9.8,
        "cwe": "CWE-89",
        "cve": None,
        "endpoint": "/rest/user/login",
        "method": "POST",
        "confidence": "high",
        "finding_class": "injection",
        "timestamp": "2026-09-21T00:00:00Z",
    }
    entry.update(overrides)
    return entry


def _scan_row(conn: sqlite3.Connection) -> dict[str, object]:
    row = conn.execute(
        "SELECT cost_usd, count_critical, count_high, count_medium, count_low,"
        " agent_count, event_count, current_epoch FROM scans WHERE id = 'scan-1'"
    ).fetchone()
    keys = (
        "cost_usd",
        "count_critical",
        "count_high",
        "count_medium",
        "count_low",
        "agent_count",
        "event_count",
        "current_epoch",
    )
    return {key: row[index] for index, key in enumerate(keys)}


def _insert_event(
    conn: sqlite3.Connection, *, epoch: int, seq: int, scan_id: str = "scan-1"
) -> None:
    conn.execute(
        "INSERT INTO scan_events (scan_id, epoch, seq, strix_id, kind, agent_id, ts, version,"
        " fingerprint, data_json) VALUES (?, ?, ?, '12', 'tool', 'agent-1',"
        " '2026-09-22T00:00:00.000Z', 1, 'fp', '{}')",
        (scan_id, epoch, seq),
    )


# =============================================================================
# 1. 正路
# =============================================================================
def test_one_record_writes_the_agents_the_findings_and_the_eight_numbers(
    db: Database, conn: sqlite3.Connection
) -> None:
    persist = _persist(db, conn)
    _insert_event(conn, epoch=0, seq=0)
    snapshot = a_snapshot(
        agents=(an_agent(), an_agent("agent-2", parent_id="agent-1")),
        cost_usd=0.75,
        vulnerabilities=(a_vuln(), a_vuln(id="vuln-2", severity="medium")),
    )

    _record(persist, snapshot)

    agents = conn.execute(
        "SELECT agent_id, name, parent_id, status, created_at, updated_at FROM scan_agents"
        " WHERE scan_id = 'scan-1' ORDER BY agent_id"
    ).fetchall()
    created, updated = "2026-09-22T00:00:00.000000Z", "2026-09-22T00:00:01.000000Z"
    assert [tuple(row) for row in agents] == [
        ("agent-1", "recon", None, "running", created, updated),
        ("agent-2", "recon", "agent-1", "running", created, updated),
    ]
    finding = conn.execute(
        "SELECT finding_id, severity, title, cvss, cwe, cve, endpoint, method, confidence,"
        " finding_class, first_seen_at, input_hash FROM scan_findings"
        " WHERE scan_id = 'scan-1' AND finding_id = 'vuln-1'"
    ).fetchone()
    assert tuple(finding) == (
        "vuln-1",
        "critical",
        "SQL 注入",
        9.8,
        "CWE-89",
        None,
        "/rest/user/login",
        "POST",
        "high",
        "injection",
        AT,
        finding[11],
    )
    assert len(finding[11]) == 64
    # `raw_json` 是原文，上游自己的 `timestamp` 留在里面（不占列）。
    raw = conn.execute("SELECT raw_json FROM scan_findings WHERE finding_id = 'vuln-1'").fetchone()[
        0
    ]
    assert json.loads(raw)["timestamp"] == "2026-09-21T00:00:00Z"
    # `input_hash` 就是 `raw_json` 的 sha256 —— T21 拿它当"原文没变就不重复付费翻译"的缓存键，
    # 所以它必须随**整条原文**变。只对 `id`（或任何子集）取的实现在这里红：两条内容不同、
    # id 相同的记录会算出同一个键，跨扫描就会取到别人的译文。
    assert finding[11] == hashlib.sha256(raw.encode()).hexdigest()
    assert _scan_row(conn) == {
        "cost_usd": 0.75,
        "count_critical": 1,
        "count_high": 0,
        "count_medium": 1,
        "count_low": 0,
        "agent_count": 2,
        "event_count": 1,
        "current_epoch": 0,
    }


# =============================================================================
# 2. P1 —— 计数只能来自我们自己的行
# =============================================================================
def test_duplicate_finding_ids_collapse_and_the_counts_follow_the_rows(
    db: Database, conn: sqlite3.Connection
) -> None:
    """上游一轮里给出两条同 `id`：`severity_counts` 会数 2，我们按 PK 只有 1 行。

    抄 `snapshot.severity` 的实现在这里立刻打架（`count_critical` 变成 2，而
    `scan_findings` 只有 1 行）—— 端到端验收第 13 条要求两者恒等。
    """
    persist = _persist(db, conn)
    snapshot = a_snapshot(
        vulnerabilities=(
            a_vuln(),
            a_vuln(severity="high", title="同一个 id 的第二份"),
            a_vuln(id="vuln-info", severity="info"),
        ),
        # 上游自己的计数：3 条记录、critical/high/low 各 1。刻意与我们的行数不一致。
        severity={"critical": 1, "high": 1, "medium": 0, "low": 1},
    )

    _record(persist, snapshot)

    rows = conn.execute(
        "SELECT finding_id, severity FROM scan_findings WHERE scan_id = 'scan-1'"
        " ORDER BY finding_id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [("vuln-1", "critical"), ("vuln-info", "low")]
    scan = _scan_row(conn)
    assert scan["count_critical"] == 1, "抄了上游的 severity_counts（它数 2）"
    assert scan["count_high"] == 0, "第二份被折进 critical 那一行了，high 不该有数"
    # `info` 折进 `low`（与上游 severity_counts 同规则），四个桶之和恒等于行数。
    assert scan["count_low"] == 1
    total = sum(int(scan[f"count_{bucket}"]) for bucket in ("critical", "high", "medium", "low"))
    assert total == len(rows)


# =============================================================================
# 3. P2 —— 首见即插，之后一个字都不改
# =============================================================================
def test_a_second_sighting_never_rewrites_the_first(db: Database, conn: sqlite3.Connection) -> None:
    """帧层的 `vuln.add` 只推一次，库里也不许换版本 —— 否则客户端收到的与回放出来的不是
    同一个东西。"""
    persist = _persist(db, conn)
    _record(persist, a_snapshot(vulnerabilities=(a_vuln(),)))
    first = conn.execute(
        "SELECT severity, title, first_seen_at, raw_json, input_hash FROM scan_findings"
        " WHERE finding_id = 'vuln-1'"
    ).fetchone()

    _record(
        persist,
        a_snapshot(vulnerabilities=(a_vuln(severity="low", title="改过的标题", cvss=1.0),)),
        at=LATER,
    )

    again = conn.execute(
        "SELECT severity, title, first_seen_at, raw_json, input_hash FROM scan_findings"
        " WHERE finding_id = 'vuln-1'"
    ).fetchone()
    assert tuple(again) == tuple(first)
    assert again[0] == "critical"
    assert again[2] == AT
    assert conn.execute("SELECT COUNT(*) FROM scan_findings").fetchone()[0] == 1


# =============================================================================
# 4. agent upsert：状态会变，`created_at` 不会
# =============================================================================
def test_agent_upsert_keeps_the_first_created_at(db: Database, conn: sqlite3.Connection) -> None:
    persist = _persist(db, conn)
    _record(persist, a_snapshot(agents=(an_agent(),)))

    _record(
        persist,
        a_snapshot(
            agents=(
                an_agent(
                    status="finished",
                    created_at="2099-01-01T00:00:00.000000Z",
                    updated_at="2026-09-22T00:00:09.000000Z",
                    error_message="超时",
                ),
            )
        ),
        at=LATER,
    )

    row = conn.execute(
        "SELECT status, error_message, created_at, updated_at FROM scan_agents"
        " WHERE agent_id = 'agent-1'"
    ).fetchone()
    assert tuple(row) == (
        "finished",
        "超时",
        "2026-09-22T00:00:00.000000Z",
        "2026-09-22T00:00:09.000000Z",
    )
    assert _scan_row(conn)["agent_count"] == 1


def test_missing_agent_timestamps_fall_back_to_the_sighting_time(
    db: Database, conn: sqlite3.Connection
) -> None:
    """两列 NOT NULL，而上游缺字段时投影层给的是空串。"""
    persist = _persist(db, conn)

    _record(persist, a_snapshot(agents=(an_agent(created_at="", updated_at=""),)))

    row = conn.execute(
        "SELECT created_at, updated_at FROM scan_agents WHERE agent_id = 'agent-1'"
    ).fetchone()
    assert tuple(row) == (AT, AT)


# =============================================================================
# 5. event_count 只数当前 epoch
# =============================================================================
def test_event_count_only_counts_the_current_epoch(db: Database, conn: sqlite3.Connection) -> None:
    """这个数正好等于"一个此刻重连的客户端会被回放多少条事件"。压缩后 epoch +1、
    从头算，是正确行为不是 bug。"""
    persist = _persist(db, conn)
    _insert_event(conn, epoch=0, seq=0)
    _insert_event(conn, epoch=0, seq=1)
    _insert_event(conn, epoch=1, seq=0)
    _insert_event(conn, epoch=1, seq=1)
    _insert_event(conn, epoch=1, seq=2)

    _record(persist, a_snapshot(), epoch=1)

    scan = _scan_row(conn)
    assert scan["event_count"] == 3
    assert scan["current_epoch"] == 1


# =============================================================================
# 6. 脏值容错：一条坏记录不许打死整条实时流
# =============================================================================
def test_dirty_values_land_as_null_instead_of_blowing_up_the_stream(
    db: Database, conn: sqlite3.Connection
) -> None:
    persist = _persist(db, conn)
    snapshot = a_snapshot(
        vulnerabilities=(
            a_vuln(id="v-text", cvss="高", cwe="  ", title=None),
            a_vuln(id="v-range", cvss=11.5),
            # 下界与上界都要有：`CHECK (cvss BETWEEN 0 AND 10)` 两头都会抛。
            a_vuln(id="v-negative", cvss=-0.1),
            a_vuln(id="v-bool", cvss=True),
        )
    )

    _record(persist, snapshot)

    rows = conn.execute(
        "SELECT finding_id, cvss, cwe, title FROM scan_findings ORDER BY finding_id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("v-bool", None, "CWE-89", "SQL 注入"),
        ("v-negative", None, "CWE-89", "SQL 注入"),
        ("v-range", None, "CWE-89", "SQL 注入"),
        ("v-text", None, None, ""),
    ]


# =============================================================================
# 7. 同一个库里总有别的扫描：一处 `WHERE scan_id = ?` 都不许少
# =============================================================================
def test_counts_never_borrow_another_scans_rows(db: Database, conn: sqlite3.Connection) -> None:
    """三个计数查询各有一处 `scan_id` 过滤，少任何一处都是把别人的行数记到我们名下 ——
    而这种错在单扫描的测试里是完全静默的（收货 mutation MX5 实测 0 红）。
    """
    persist = _persist(db, conn)
    insert_scan(conn, "scan-2")
    conn.execute(
        "INSERT INTO scan_agents (scan_id, agent_id, name, parent_id, status, error_message,"
        " created_at, updated_at) VALUES ('scan-2', 'other-agent', 'x', NULL, 'running', NULL,"
        " ?, ?)",
        (AT, AT),
    )
    conn.execute(
        "INSERT INTO scan_findings (scan_id, finding_id, severity, title, first_seen_at,"
        " raw_json, input_hash) VALUES ('scan-2', 'other-finding', 'high', 'x', ?, '{}', ?)",
        (AT, "0" * 64),
    )
    _insert_event(conn, epoch=0, seq=0)
    _insert_event(conn, epoch=0, seq=1, scan_id="scan-2")
    _insert_event(conn, epoch=0, seq=2, scan_id="scan-2")

    _record(persist, a_snapshot(agents=(an_agent(),), vulnerabilities=(a_vuln(),)))

    assert _scan_row(conn) == {
        "cost_usd": 0.0,
        "count_critical": 1,
        "count_high": 0,
        "count_medium": 0,
        "count_low": 0,
        "agent_count": 1,
        "event_count": 1,
        "current_epoch": 0,
    }
    # 别人那一行一个字都不许动（`UPDATE … WHERE id = ?` 那一处过滤）。
    other = conn.execute(
        "SELECT count_high, agent_count, event_count FROM scans WHERE id = 'scan-2'"
    ).fetchone()
    assert tuple(other) == (0, 0, 0)


# =============================================================================
# 8. cost_usd：读不到 ≠ 变成 0
# =============================================================================
def test_a_round_without_cost_does_not_wipe_the_known_one(
    db: Database, conn: sqlite3.Connection
) -> None:
    persist = _persist(db, conn)
    _record(persist, a_snapshot(cost_usd=1.25))

    _record(persist, a_snapshot(cost_usd=None))
    assert _scan_row(conn)["cost_usd"] == 1.25

    # 负数按"读不到"处理：列上有 CHECK >= 0，让它抛就等于一个脏值打死实时流。
    _record(persist, a_snapshot(cost_usd=-3.0))
    assert _scan_row(conn)["cost_usd"] == 1.25
