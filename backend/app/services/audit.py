"""审计双写 —— `audit_log` 表 + `${DATA}/audit/YYYY-MM.ndjson`。

# 为什么双写（`PLAN.md` §护栏 末段）

两份不是冗余，是两种用法：表用来在界面上翻页和导出 CSV，ndjson 用来在**数据库已经丢
了或打不开**的时候还能 `grep`。这也是 `audit_log` 刻意没有外键的同一个思路 ——
审计记录必须比它描述的对象活得久（见 `migrations/001_init.sql` 那段注释）。

# 事件是一串平铺的常量，没有基类、没有注册表

`PLAN.md` 列了十来个事件，这里刻意**不为它们预留任何东西** —— 没有事件基类、没有
注册表、没有"通用 detail 构造器"（CLAUDE.md §编码哲学 3「不要过早抽象」）。加一个
事件就是加一行常量 + 在抛出方写一次 `detail`。

T12c 按预期扩展了两处：`AuditEntry` 多了 `scan_id` / `authorization_id`（两个落点都
真的填它们），以及 `record()` 被剖成 `prepare` / `insert` / `mirror` 三块 —— 那是为了
让"同一事务里既写 `scans` 行又写审计行"有入口（`record()` 自己起事务，那个场景需要
复用调用方的事务），而列清单仍然只有一份。

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
EVENT_KEY_REGISTERED = "key.registered"
EVENT_KEY_DROPPED = "key.dropped"
EVENT_TARGET_REJECTED = "target.rejected"
EVENT_TARGET_DNS_CHANGED = "target.dns_changed"
EVENT_AUTHORIZATION_AFFIRMED = "authorization.affirmed"
EVENT_SCAN_LAUNCHED = "scan.launched"
EVENT_SCAN_STOPPED = "scan.stopped"
EVENT_SCAN_FINISHED = "scan.finished"
EVENT_SCAN_RESUMED = "scan.resumed"
EVENT_IMAGE_PULL_STARTED = "image.pull_started"
EVENT_SCAN_PURGED = "scan.purged"
EVENT_REPORT_TRANSLATED = "report.translated"
"""事件名。**字面串只出现在这里**，路由层 import 它们。

集中在本模块而不是各自写在抛出方的路由里：这份名单就是"审计里会出现哪些事件"的全部
答案，而回答这个问题的人（写查询的、写告警的）不该需要先知道有哪几个路由文件。
`key.*` 两个是 T7b 加的，登记与忘掉各一条 —— **`detail` 里只许有掩码标签与机器码**。
`image.pull_started` 是 T11b 加的：拉一个 GB 级镜像是一次真实的资源消耗，而它由人按
按钮触发 —— `detail` 里只有镜像引用与 epoch（**没有凭据可放**），"已经在本地"那一支
什么都没发生所以不记。
`authorization.affirmed` / `scan.*` 三个是 T12c 加的。刻意**没有**
`override.private_used` / `override.loopback_used`：放行之后 `GuardVerdict.required_opt_in`
一定是空的（那个字段的 docstring 写死了这一点），要说出"哪一项勾选被用上了"就得从解析
出来的地址逐个重新分类 —— 那是 `target_guard` 判定逻辑的第二份副本。改为在
`authorization.affirmed` 的 detail 里逐目标记下类别与三个 override 标志。
`scan.purged` 是 T28 加的，也是唯一由**定时任务**发起的事件 → `actor` 为 None（`AuditEntry.actor`
的 docstring 预留了这一种）；`detail` 只有 `retention_days` 与被清空的表名，**没有路径** ——
留存清理删的是产物，而审计本身永不删，它就是"那次扫描曾经存在过"的唯一凭据。
`report.translated` 是 T21c 加的：后台翻译结束时记一条（成功与异常都记，`outcome` 区分），
`detail` 只有模型名、计数与费用，**没有译文**。"""

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

    scan_id: str | None = None
    """这条记录说的是哪次扫描。有默认值，所以 T12c 之前的五个调用点一个字都不用改。"""

    authorization_id: str | None = None
    """哪一份授权声明。与 `scan_id` 一样是可空的：`allowlist.changed` 之类的事件与两者
    都无关，而 `audit_log` 刻意没有外键（审计要比它描述的对象活得久）。"""


def iso_utc(moment: datetime) -> str:
    """`2026-09-16T03:04:05.678901Z`。见模块 docstring「`Z` 后缀不是随手缀的」。

    在这里而不是各调用方各写一遍：`scans` 的时间列（`started_at` / `finished_at`）与
    启动时的 6b 兜底都要写同一种形状，而"哪一种形状"的判据整段写在本模块 docstring 里。
    传进来的 `moment` 必须已经带 UTC 时区 —— 本函数只做格式，不做时区。
    """
    return moment.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class PreparedAudit:
    """算好一切、还没落盘的一条审计。

    为什么要把 `record()` 剖成这样：T12c 要在**同一个事务**里既写 `scans` 行又写审计行
    （准入通过之后 `authorizations` / `scans` / `audit_log` 必须一起成立或一起不成立），
    而 `record()` 自己起事务。剖开之后列清单仍然只有一份（`insert`），调用方只是自己
    决定那条 INSERT 落在哪个事务里。

    `moment` 与 `at` 同时留着是刻意的：表里的 `at` 和 ndjson 的文件名必须来自同一个
    瞬间（见模块 docstring）。
    """

    at: str
    moment: datetime
    entry: AuditEntry
    user_agent: str | None
    detail_json: str


def prepare(entry: AuditEntry) -> PreparedAudit:
    """取一次 `now(UTC)`，算出两个落点都要用的派生值。**不做任何 IO。**"""
    moment = datetime.now(UTC)
    return PreparedAudit(
        at=iso_utc(moment),
        moment=moment,
        entry=entry,
        user_agent=None if entry.user_agent is None else entry.user_agent[:_MAX_USER_AGENT],
        detail_json=json.dumps(dict(entry.detail), ensure_ascii=False, sort_keys=True),
    )


def is_scan_purged(conn: sqlite3.Connection, scan_id: str) -> bool:
    """这次扫描的产物是否已被保留期清理删掉（有 `scan.purged` 审计行即是）。同步，同 `insert`。"""
    row = conn.execute(
        "SELECT 1 FROM audit_log WHERE scan_id = ? AND event = ? LIMIT 1",
        (scan_id, EVENT_SCAN_PURGED),
    ).fetchone()
    return row is not None


def insert(conn: sqlite3.Connection, prepared: PreparedAudit) -> None:
    """写表。**同步**，供调用方在自己的 `db.run()` 回调里调（那就是同一个事务）。"""
    entry = prepared.entry
    conn.execute(
        "INSERT INTO audit_log "
        "(at, event, scan_id, authorization_id, actor, detail_json, client_ip, user_agent) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            prepared.at,
            entry.event,
            entry.scan_id,
            entry.authorization_id,
            entry.actor,
            prepared.detail_json,
            entry.client_ip,
            prepared.user_agent,
        ),
    )


async def mirror(prepared: PreparedAudit, *, audit_dir: Path) -> None:
    """写 ndjson。**必须在表那一侧的事务提交之后调**：镜像里出现一条最终被回滚掉的
    记录，就是往"里面写的都真的发生过"这条保证上开一个洞。"""
    await asyncio.to_thread(_append_mirror, audit_dir=audit_dir, prepared=prepared)


async def record(*, db: Database, audit_dir: Path, entry: AuditEntry) -> None:
    """双写一条审计（自带事务）。**签名不变**，内部由上面三块拼出来。

    顺序是**先业务改动、再审计**（调用方那边），这里则是**先表、再文件**。
    如果 ndjson 那一步失败，异常会一路抛到 500，而业务改动已经落盘了 —— 这是刻意的：
    宁可让操作者看到一个失败去核对，也不要静默丢掉审计记录。两个落点都在同一块本地
    磁盘上，一个失败几乎必然意味着另一个也快了。
    """
    prepared = prepare(entry)
    await db.run(lambda conn: insert(conn, prepared))
    await mirror(prepared, audit_dir=audit_dir)
    logger.info("审计已记录", extra={"event": entry.event, "actor": entry.actor})


def _append_mirror(*, audit_dir: Path, prepared: PreparedAudit) -> None:
    """追加一行 ndjson。**同步阻塞**，由 `mirror()` 经 `to_thread` 调用。

    字段名与 `audit_log` 的列名一一对应，**但 `detail` 是嵌套对象而不是 JSON 字符串**
    ——这一份的用途是 `jq` 和 `grep`，而一个被转义进字符串里的 JSON 两者都不好用。
    表里那一列必须是字符串（`detail_json TEXT CHECK (json_valid(...))`）。

    `O_APPEND` + `fsync`：追加是原子的（单次 write 小于 PIPE_BUF 级别的量），
    fsync 让"机器掉电"不会吃掉刚刚那条记录。审计写入很稀疏，这点开销无所谓。
    """
    entry = prepared.entry
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / f"{prepared.moment.strftime('%Y-%m')}.ndjson"
    line = json.dumps(
        {
            "at": prepared.at,
            "event": entry.event,
            "scan_id": entry.scan_id,
            "authorization_id": entry.authorization_id,
            "actor": entry.actor,
            "detail": dict(entry.detail),
            "client_ip": entry.client_ip,
            "user_agent": prepared.user_agent,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
