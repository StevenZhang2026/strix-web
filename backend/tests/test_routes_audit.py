"""`GET /api/audit.csv` —— 接线层测试。

审计的**写入**语义（哪些事件、detail 里许放什么、双写 ndjson）在 `test_audit.py` 里测过，
这里不重测（agent-rules §十.4）。这个文件只回答导出这一层的问题：
鉴权真的挡住了吗、列顺序与 DDL 一致吗、`csv` 的引用真的正确吗、正文是不是**原样**透传。

审计行用**另一个连接**插（同 `test_routes_scans.py::_rows` 的判据）：`app.state.db` 的连接
属于事件循环所在的线程，从测试线程用它就是在测试里制造一个数据竞争。WAL 下另开一个连接
写入并提交，路由那边的连接看得到。
"""

from __future__ import annotations

import csv
import io
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.routes import audit as audit_routes
from app.settings import Settings

_MASKED_LABEL = "sk-ant-…4f2c"


def _insert_audit(
    settings: Settings,
    *,
    at: str = "2026-09-17T00:00:00.000Z",
    event: str = "scan.launched",
    scan_id: str | None = "scan-1",
    authorization_id: str | None = "auth-1",
    actor: str | None = "测试操作者",
    detail_json: str = "{}",
    client_ip: str | None = "127.0.0.1",
    user_agent: str | None = "pytest",
) -> None:
    """插一行 `audit_log`。刻意不放进 `conftest.py`：只有这个文件用它。"""
    connection = sqlite3.connect(settings.db_path)
    try:
        connection.execute(
            "INSERT INTO audit_log (at, event, scan_id, authorization_id, actor, "
            "detail_json, client_ip, user_agent) VALUES (?,?,?,?,?,?,?,?)",
            (at, event, scan_id, authorization_id, actor, detail_json, client_ip, user_agent),
        )
        connection.commit()
    finally:
        connection.close()


def _parse(body: str) -> list[list[str]]:
    """用 `csv.reader` 解析回来 —— 断言解析后的值，而不是断言原始字符串的引号长什么样。"""
    return list(csv.reader(io.StringIO(body, newline="")))


_HEADER = [
    "id",
    "at",
    "event",
    "scan_id",
    "authorization_id",
    "actor",
    "detail_json",
    "client_ip",
    "user_agent",
]


# =============================================================================
# 1 鉴权
# =============================================================================
def test_export_requires_session(anonymous: TestClient) -> None:
    """未登录必须 401：这份流水里写着谁批准扫了哪些内网主机。"""
    assert anonymous.get("/api/audit.csv").status_code == 401


# =============================================================================
# 2 空表
# =============================================================================
def test_empty_table_returns_header_only(client: TestClient) -> None:
    """空表也是 200，正文只有表头一行，行结束符是 RFC 4180 的 CRLF。"""
    response = client.get("/api/audit.csv")

    assert response.status_code == 200
    assert response.text == ",".join(_HEADER) + "\r\n"


# =============================================================================
# 3 顺序、列位置、NULL
# =============================================================================
def test_rows_are_ordered_by_id_with_nulls_as_empty_fields(
    client: TestClient, settings: Settings
) -> None:
    """按 `id` 升序（= 发生顺序），列顺序与表头一致，NULL 是空字段而不是 `"None"`。"""
    _insert_audit(settings, event="key.registered", scan_id=None)
    _insert_audit(
        settings,
        event="allowlist.changed",
        scan_id=None,
        authorization_id=None,
        actor=None,
        client_ip=None,
        user_agent=None,
    )
    _insert_audit(settings, event="scan.stopped")

    rows = _parse(client.get("/api/audit.csv").text)

    assert rows[0] == _HEADER
    assert [row[0] for row in rows[1:]] == ["1", "2", "3"]
    assert [row[2] for row in rows[1:]] == ["key.registered", "allowlist.changed", "scan.stopped"]
    assert rows[2] == [
        "2",
        "2026-09-17T00:00:00.000Z",
        "allowlist.changed",
        "",
        "",
        "",
        "{}",
        "",
        "",
    ]


# =============================================================================
# 4 引用
# =============================================================================
def test_fields_with_commas_quotes_and_newlines_survive_a_round_trip(
    client: TestClient, settings: Settings
) -> None:
    """`user_agent` 是客户端可控的字符串，里面什么都可能有 —— `csv` 负责把它引用回来。"""
    nasty = 'Mozilla/5.0 (X11; Linux), "quoted"\nsecond line,末尾'
    _insert_audit(settings, user_agent=nasty)

    rows = _parse(client.get("/api/audit.csv").text)

    assert len(rows) == 2, "含换行的字段被拆成了多行，说明引用是错的"
    assert rows[1][-1] == nasty


# =============================================================================
# 5 响应头
# =============================================================================
def test_response_is_a_csv_attachment_without_bom(client: TestClient) -> None:
    """`text/csv` + attachment，且**没有** BOM（不加 BOM 是取舍，见路由的模块 docstring）。"""
    response = client.get("/api/audit.csv")

    content_type = response.headers["content-type"]
    assert content_type.startswith("text/csv")
    assert "charset=utf-8" in content_type
    assert response.headers["content-disposition"] == 'attachment; filename="audit.csv"'
    assert not response.content.startswith(b"\xef\xbb\xbf")


# =============================================================================
# 6 原样透传
# =============================================================================
def test_detail_json_is_passed_through_verbatim(client: TestClient, settings: Settings) -> None:
    """掩码标签**就该**原样出现：写入方已经掩过了，导出这一层不许再做二次加工。

    这条测的是"我们没有偷偷改内容"，不是"这里会不会泄漏 Key"——
    库里根本没有明文（`test_no_secret_columns.py`）。
    """
    _insert_audit(
        settings,
        event="key.registered",
        detail_json=f'{{"provider": "anthropic", "masked": "{_MASKED_LABEL}"}}',
    )

    rows = _parse(client.get("/api/audit.csv").text)

    assert rows[1][6] == f'{{"provider": "anthropic", "masked": "{_MASKED_LABEL}"}}'
    assert _MASKED_LABEL in client.get("/api/audit.csv").text


# =============================================================================
# 7 行数上限
# =============================================================================
def test_truncation_keeps_the_newest_rows_and_flags_the_response(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超限时保留**最近**那批（仍按升序输出）并加 `X-Audit-Truncated`；不超限时该头不存在。"""
    for index in range(3):
        _insert_audit(settings, event=f"scan.launched.{index}")

    within_limit = client.get("/api/audit.csv")
    assert len(_parse(within_limit.text)) == 4
    assert "x-audit-truncated" not in within_limit.headers

    monkeypatch.setattr(audit_routes, "_MAX_ROWS", 2)
    truncated = client.get("/api/audit.csv")

    rows = _parse(truncated.text)
    assert [row[0] for row in rows[1:]] == ["2", "3"]
    assert truncated.headers["x-audit-truncated"] == "true"
