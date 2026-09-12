"""审计双写 —— `audit_log` 表 + `${DATA}/audit/YYYY-MM.ndjson`。

# 为什么双写（`PLAN.md` §护栏 末段）

两份不是冗余，是两种用法：表用来在界面上翻页和导出 CSV，ndjson 用来在**数据库已经丢
了或打不开**的时候还能 `grep`。这也是 `audit_log` 刻意没有外键的同一个思路 ——
审计记录必须比它描述的对象活得久（见 `migrations/001_init.sql` 那段注释）。

# 这一版**只有一个事件**：`allowlist.changed`

`PLAN.md` 列了十来个事件（`target.rejected` / `authorization.affirmed` /
`scan.launched` …），它们分别属于 T12、T14、T19。这里刻意**不为它们预留任何东西**
——没有事件基类、没有注册表、没有"通用 detail 构造器"（CLAUDE.md §编码哲学 3
「不要过早抽象」）。**T12 会扩展这里**：加 `scan_id` / `authorization_id` 的填法，
以及一个"同一事务里既写 `scans` 行又写审计行"的入口（本模块现在的 `record()` 自己
起事务，那个场景需要复用调用方的事务）。

# 时间戳只取一次

`at` 与 ndjson 的文件名都来自**同一个** `datetime.now(UTC)`。两次 `now()` 的差别在
月末最后一毫秒会变成"表里记 10 月、文件写进 9 月那份"，而那种不一致要到有人对账的
时候才会被发现 —— 那时已经没法判断哪一份是对的。

# `Z` 后缀不是随手缀的

`datetime.now(UTC).isoformat()` 给的是 `+00:00`，替换成 `Z` 只是形状。真正的保证是
**取值时就带 UTC 时区**，而不是 `datetime.now()` 再缀个 Z —— 后者在非 UTC 时区的机器
上就是一句谎话（pitfalls 条 37 是这条的日志版本，`logging.Formatter.converter` 默认
`time.localtime`）。`tests/test_audit.py` 里有一条把 TZ 设成 `Asia/Shanghai` 的用例
钉住这件事。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.db import Database

logger = logging.getLogger(__name__)

EVENT_ALLOWLIST_CHANGED = "allowlist.changed"
"""唯一一个事件名。字面串只出现在这里，路由层 import 它。"""

AuditDetailValue = str | int | float | bool | None | list[str]
"""`detail` 里允许的值域。

刻意不写 `Any`（CLAUDE.md §Python：`Any` 需注释理由，而这里根本不需要它）：
这个 dict 会被 `json.dumps` 序列化进 DB 的 `detail_json` 列与 ndjson 的一行，
一个不可序列化的值会在**写审计的那一刻**才炸 —— 那时业务操作已经做完了。
"""

# user_agent 截断长度。它完全由客户端控制，是一个**未校验的输入**：整段原样落进 DB 与
# ndjson 会让一行审计变成几十 KB，而 `grep` 一个几十 KB 的行没有任何用处。
# 只截断、不做别的处理：它进的是 JSON 字符串与 DB 的文本列，不参与任何格式化拼接，
# 所以日志注入那类问题在这里不成立（真正危险的是把它塞进 log 的 message 模板）。
_MAX_USER_AGENT = 200


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """一条待写入的审计记录。**正文里不许有凭据**（`detail_json` 只放掩码标签）。"""

    event: str
    actor: str | None
    """操作者用户名，来自 `request.state.session.username`（T4b 的全局依赖挂上去的）。

    `None` 只在免鉴权路径上才可能出现，而免鉴权路径不写审计。留着这个可空是因为
    `audit_log.actor` 列本身可空 —— 以后可能有由定时任务发起的事件（T11 的 reaper）。
    """

    detail: Mapping[str, AuditDetailValue]
    client_ip: str | None
    user_agent: str | None


async def record(*, db: Database, audit_dir: Path, entry: AuditEntry) -> None:
    """双写一条审计。

    顺序是**先业务改动、再审计**（调用方那边），这里则是**先表、再文件**。
    如果 ndjson 那一步失败，异常会一路抛到 500，而业务改动已经落盘了 —— 这是刻意的：
    宁可让操作者看到一个失败去核对，也不要静默丢掉审计记录。两个落点都在同一块本地
    磁盘上，一个失败几乎必然意味着另一个也快了。
    """
    moment = datetime.now(UTC)
    at = moment.isoformat().replace("+00:00", "Z")
    detail_json = json.dumps(dict(entry.detail), ensure_ascii=False, sort_keys=True)
    user_agent = None if entry.user_agent is None else entry.user_agent[:_MAX_USER_AGENT]

    def insert(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO audit_log "
            "(at, event, scan_id, authorization_id, actor, detail_json, client_ip, user_agent) "
            "VALUES (?, ?, NULL, NULL, ?, ?, ?, ?)",
            (at, entry.event, entry.actor, detail_json, entry.client_ip, user_agent),
        )

    await db.run(insert)
    await asyncio.to_thread(
        _append_mirror,
        audit_dir=audit_dir,
        moment=moment,
        at=at,
        entry=entry,
        user_agent=user_agent,
    )
    logger.info("审计已记录", extra={"event": entry.event, "actor": entry.actor})


def _append_mirror(
    *,
    audit_dir: Path,
    moment: datetime,
    at: str,
    entry: AuditEntry,
    user_agent: str | None,
) -> None:
    """追加一行 ndjson。**同步阻塞**，由 `record()` 经 `to_thread` 调用。

    字段名与 `audit_log` 的列名一一对应，**但 `detail` 是嵌套对象而不是 JSON 字符串**
    ——这一份的用途是 `jq` 和 `grep`，而一个被转义进字符串里的 JSON 两者都不好用。
    表里那一列必须是字符串（`detail_json TEXT CHECK (json_valid(...))`）。

    `O_APPEND` + `fsync`：追加是原子的（单次 write 小于 PIPE_BUF 级别的量），
    fsync 让"机器掉电"不会吃掉刚刚那条记录。审计写入很稀疏，这点开销无所谓。
    """
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / f"{moment.strftime('%Y-%m')}.ndjson"
    line = json.dumps(
        {
            "at": at,
            "event": entry.event,
            "scan_id": None,
            "authorization_id": None,
            "actor": entry.actor,
            "detail": dict(entry.detail),
            "client_ip": entry.client_ip,
            "user_agent": user_agent,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
