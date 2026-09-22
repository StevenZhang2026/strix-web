"""把一轮投影落进 `scan_agents` / `scan_findings` / `scans` 的计数列。

职责边界：本模块只写库，不读 run 目录、不算增量、不发帧（那是 `scan_channel` 编排的三个
纯函数）。实时的 cost 与严重度分布走 `summary` 帧，不依赖这里 —— 这几张表是**回放与报告**
的真源（T16 / T21 要靠它），所以它们的一致性比"早一点看到数"重要。

三条不变式（各有测试守着，见 `tests/test_scan_persist.py`）：

- **P1 计数不许有第二个真相。** `scans` 的四个 `count_*` 与 `agent_count` / `event_count`
  **只从同一个事务里刚写下的行上 `COUNT(*)` 出来**，绝不抄 `snapshot.severity` 或
  `len(snapshot.agents)`：上游一旦给出两条同 `id` 的漏洞，`severity_counts` 数 2 而我们按
  PK 只有 1 行 —— 两个独立计算迟早打架。
- **P2 首见即插，之后一个字都不改。** 同一个 `finding_id` 第二次见到就 `DO NOTHING`：
  帧层的 `vuln.add` 也只推一次，库里偷偷换成新版本就意味着"客户端收到的"与"回放出来的"
  是两份不一样的东西。
- **P3 落库失败不被吞。** `record` 抛出来就让它冒泡把 channel 任务打死（同 `EventMirror`
  的立场）：只追加的真源出洞比断流更糟，扫描终态由 `_run_to_completion` 兜着。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from functools import partial
from typing import TYPE_CHECKING, Final

from app.services.run_projector import RunSnapshot, canonical_json, fingerprint_of
from app.services.scan_frames import vuln_key

if TYPE_CHECKING:
    from app.db import Database

SEVERITY_BUCKETS: Final = ("critical", "high", "medium", "low")
"""四个桶。取值域在 Python 侧（DDL 刻意不给枚举列加 CHECK）。"""

_AGENT_SQL: Final = (
    "INSERT INTO scan_agents (scan_id, agent_id, name, parent_id, status, error_message,"
    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    " ON CONFLICT (scan_id, agent_id) DO UPDATE SET"
    " name = excluded.name, parent_id = excluded.parent_id, status = excluded.status,"
    " error_message = excluded.error_message, updated_at = excluded.updated_at"
)
"""`created_at` 刻意不在 SET 里：首见的时刻不许被后来的 upsert 改写。"""

_FINDING_SQL: Final = (
    "INSERT INTO scan_findings (scan_id, finding_id, severity, title, cvss, cwe, cve, endpoint,"
    " method, confidence, finding_class, first_seen_at, raw_json, input_hash)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING"
)
"""首见即插，第二次见到一个字都不改（P2）：帧层的 `vuln.add` 也只推一次。"""

_SEVERITY_COUNT_SQL: Final = (
    "SELECT severity, COUNT(*) FROM scan_findings WHERE scan_id = ? GROUP BY severity"
)

_COUNTS_SQL: Final = (
    "UPDATE scans SET cost_usd = COALESCE(?, cost_usd),"
    " count_critical = ?, count_high = ?, count_medium = ?, count_low = ?,"
    " agent_count = ?, event_count = ?, current_epoch = ? WHERE id = ?"
)


def severity_bucket(entry: Mapping[str, object]) -> str:
    """折进四个桶。规则与上游 `severity_counts()` 逐字一致 —— 它会产出 `"info"`
    （CVSS 基准分为 none 时），不折的话库里就多出一个上游计数里没有的桶。
    原始 severity 完整留在 `raw_json` 里，没丢。
    """
    severity = str(entry.get("severity") or "").lower().strip()
    if severity not in SEVERITY_BUCKETS:
        return "low"
    return severity


def _cvss(raw: object) -> float | None:
    """脏 CVSS 写成 `None` 而不是让 `CHECK (cvss BETWEEN 0 AND 10)` 抛 —— P3 让异常打死
    整条实时流，一条坏分数不配有那个权力。`bool` 显式排掉（`isinstance(True, int)` 为真）。
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if not 0.0 <= value <= 10.0:
        return None
    return value


def _optional_text(raw: object) -> str | None:
    """空串／只有空白在库里等于没有信息，存成 `None` 好让读取端只判一次。"""
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


class ScanPersist:
    """一次扫描的落库器。真正持有状态（库、scan_id），所以是 class。

    构造**不碰 DB**（照 `EventMirror` / `RetentionSweeper` 的先例）。
    """

    def __init__(self, db: Database, scan_id: str) -> None:
        self._db = db
        self._scan_id = scan_id

    async def record(self, *, snapshot: RunSnapshot, epoch: int, at: str) -> None:
        """一轮投影 → 一个事务写三张表。

        `at` 由调用方给（`ScanChannel` 传 `now_ts()`）：自己取时钟就没法确定性地测，
        而且一轮里帧与库应当共用同一个时刻。
        """
        await self._db.run(partial(self._write, snapshot=snapshot, epoch=epoch, at=at))

    def _write(
        self,
        conn: sqlite3.Connection,
        *,
        snapshot: RunSnapshot,
        epoch: int,
        at: str,
    ) -> None:
        """同步的事务体（`Database.run` 自带 BEGIN IMMEDIATE / COMMIT / 回滚）。"""
        for agent in snapshot.agents:
            conn.execute(
                _AGENT_SQL,
                (
                    self._scan_id,
                    agent.id,
                    agent.name,
                    agent.parent_id,
                    agent.status,
                    agent.error_message,
                    # 两列 NOT NULL，而上游缺字段时投影层给的是空串：空时间戳在库里等于
                    # 没有信息，`at`（我们见到它的时刻）至少是句真话。
                    agent.created_at or at,
                    agent.updated_at or at,
                ),
            )
        for entry in snapshot.vulnerabilities:
            conn.execute(
                _FINDING_SQL,
                (
                    self._scan_id,
                    # 与帧层同一个身份函数，否则"推过的"和"存下的"对不上号。
                    vuln_key(entry),
                    severity_bucket(entry),
                    str(entry.get("title") or ""),
                    _cvss(entry.get("cvss")),
                    _optional_text(entry.get("cwe")),
                    _optional_text(entry.get("cve")),
                    _optional_text(entry.get("endpoint")),
                    _optional_text(entry.get("method")),
                    _optional_text(entry.get("confidence")),
                    _optional_text(entry.get("finding_class")),
                    at,
                    canonical_json(entry),
                    # 对**整条**原始记录取：T21 的缓存键还没有字段清单，现在收窄等于猜。
                    fingerprint_of(entry),
                ),
            )
        # 全部计数都从**刚写下的行**上数出来（P1），一个都不许来自 snapshot。
        counts = dict.fromkeys(SEVERITY_BUCKETS, 0)
        for row in conn.execute(_SEVERITY_COUNT_SQL, (self._scan_id,)).fetchall():
            counts[str(row[0])] = int(row[1])
        agent_count = conn.execute(
            "SELECT COUNT(*) FROM scan_agents WHERE scan_id = ?", (self._scan_id,)
        ).fetchone()[0]
        # 只数**当前 epoch**：这个数正好等于"一个此刻重连的客户端会被回放多少条事件"。
        event_count = conn.execute(
            "SELECT COUNT(*) FROM scan_events WHERE scan_id = ? AND epoch = ?",
            (self._scan_id, epoch),
        ).fetchone()[0]
        # `None` 是"这一轮读不到"，不是"变成 0 了"——直接写会把已经知道的花费擦掉。
        # 负数同样按读不到处理（列上有 CHECK >= 0，让它抛就是一个脏值打死实时流）。
        cost_usd = snapshot.cost_usd
        if cost_usd is not None and cost_usd < 0:
            cost_usd = None
        conn.execute(
            _COUNTS_SQL,
            (
                cost_usd,
                counts["critical"],
                counts["high"],
                counts["medium"],
                counts["low"],
                agent_count,
                event_count,
                epoch,
                self._scan_id,
            ),
        )
