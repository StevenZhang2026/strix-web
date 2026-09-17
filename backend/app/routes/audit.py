"""`GET /api/audit.csv` —— 把 `audit_log` 整表导出成 CSV。

# 为什么是 CSV 而不是 JSON 接口 + 一个页面

T25 砍掉了审计页面：日常查询用 `grep`。一个能被 `grep`／`awk`／`sqlite3` 直接吃掉的
文本流比一个分页表格有用得多，而且它不需要前端跟着 `audit_log` 的列一起改。

# 刻意**不加** UTF-8 BOM

BOM 的唯一用途是让 Excel 认出编码，而一旦宣称"给 Excel 用"，就连带必须处理 CSV 公式
注入（`actor` / `user_agent` 都是客户端可控的，`=cmd|...` 这类值会被 Excel 当公式执行）。
而防注入的常规做法是给可疑字段加前缀 —— **那会篡改审计原文**。审计原文的保真度优先于
Excel 的便利：这里输出的每个字段都与库里的字节一致，消费方是 `grep` 和脚本。

# 这个接口自己**不写审计**

判据照抄 `services/audit.py` 里 `/api/targets/validate` 的那一条：会被反复调用的只读
操作不记审计。另外导出还有一个自指问题 —— 记下"谁导出了审计"会污染下一次导出的内容。
所以这里**没有**也不许有 `audit.exported` 之类的新事件码。

# 鉴权

什么都不加：全局路由依赖（T4b，`main.py` 的 `dependencies=[...]`）已经覆盖它，
而且**没有**往 `routes/auth.EXEMPT_PATHS` 加任何路径。
"""

from __future__ import annotations

import csv
import io
import sqlite3
from collections.abc import Iterable, Sequence

from fastapi import APIRouter, Request, Response

from app.db import Database

# 路径里带一个点（`.csv`），做成 `prefix` 只会得到一个空字符串路由，所以这个 router
# 不带 prefix，装饰器上写全路径。
router = APIRouter(tags=["audit"])

# 一次导出最多回多少行。**不做分页参数**：本机单账号、每次扫描十几条审计，到不了 10 万。
# 真到了那天，判据是实测行数而不是现在猜（agent-rules §十.1／§十.2）。
_MAX_ROWS = 100_000

# 表头 = `audit_log` 的全部 9 列，顺序照 DDL（`migrations/001_init.sql`）。
_COLUMNS: tuple[str, ...] = (
    "id",
    "at",
    "event",
    "scan_id",
    "authorization_id",
    "actor",
    "detail_json",
    "client_ip",
    "user_agent",
)

# `ORDER BY id`（而不是 `at`）：`at` 是毫秒精度的字符串，同一毫秒内的两条会并列，
# 拿它当排序键得到的顺序是不确定的。`id` 是 AUTOINCREMENT，就是发生顺序。
# `DESC LIMIT ?` 是为了在超限时保留**最近**那批（然后在内存里反转回升序）。
_SELECT_RECENT = (
    "SELECT id, at, event, scan_id, authorization_id, actor, detail_json, client_ip, user_agent "
    "FROM audit_log ORDER BY id DESC LIMIT ?"
)


def _db(request: Request) -> Database:
    db = getattr(request.app.state, "db", None)
    if not isinstance(db, Database):
        raise RuntimeError(
            "app.state.db 不存在。本接口要求 lifespan 已经跑过 —— "
            "测试里请用 `with TestClient(app):`。"
        )
    return db


def _to_csv(rows: Iterable[Sequence[object]]) -> str:
    """表头 + 数据行。用标准库 `csv` 而不是手拼字符串。

    `lineterminator="\\r\\n"` 是 RFC 4180 的行结束符；引用策略用默认的 `QUOTE_MINIMAL`，
    含逗号／双引号／换行的字段由它负责正确引用（`user_agent` 就是这样一个字段）。
    `None`（SQL 的 NULL）由 `csv` 写成**空字段**，不是字符串 `"None"`。
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(_COLUMNS)
    writer.writerows(rows)
    return buffer.getvalue()


@router.get("/api/audit.csv")
async def export_audit_csv(request: Request) -> Response:
    """整表导出。恒 200（空表也回一行表头），响应体原样透传库里的字段。

    返回 `Response` 而不是 Pydantic 模型：正文不是 JSON。先例见 `routes/keys.py` 的
    204。SQL 走 `db.run()`（sqlite3 是同步库，直接在 async 函数里查会阻塞事件循环）。

    `X-Audit-Truncated` 只在**取到了 `_MAX_ROWS` 行**时出现。正好等于上限时也会出现
    （宁可多报一次也不漏报）；没有 UI 消费它 —— 这个头是给脚本看的。
    """
    limit = _MAX_ROWS

    def query(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
        return [tuple(row) for row in conn.execute(_SELECT_RECENT, (limit,))]

    newest_first = await _db(request).run(query)
    headers = {"Content-Disposition": 'attachment; filename="audit.csv"'}
    if len(newest_first) >= limit:
        headers["X-Audit-Truncated"] = "true"
    return Response(
        content=_to_csv(reversed(newest_first)),
        media_type="text/csv; charset=utf-8",
        headers=headers,
    )
