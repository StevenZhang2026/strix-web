"""共享夹具。

原则：**不碰真实网络、不碰真实 Docker、不碰真实数据目录**（CLAUDE.md §测试）。
每个用到数据库的测试都拿一个 `tmp_path` 下的全新库 —— 测试之间零共享状态。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.db import Database
from app.services.auth import AuthRecord, write_auth_file
from app.settings import Settings

# ---- 单账号登录的测试凭据 ----------------------------------------------------
# 三个文件要用（test_auth / test_system_status / test_allowlist）。第三次出现时按
# CLAUDE.md §编码哲学 3 提取到这里 —— test_system_status.py 的 app 夹具上那句
# 「等第三次出现再提取」就是指这一刻。
#
# **只提取 `auth_file` 这一个夹具。** 三处的 `app` / `client` 夹具已经真的分叉了
# （一处要挂两条探针路由、一处要在 lifespan 之后替换 docker 传输层、一处要原样的
# 应用），合并它们只会造出一个带三个开关的夹具 —— 那比重复三遍更难读。
USERNAME = "operator"
PASSWORD = "correct-horse-battery-staple"

# ---- 一行合法的 authorizations / scans 需要填的列 ----------------------------
# 抽出来是因为 NOT NULL 的列有十来个，每个测试各写一遍会让"这个测试到底在测什么"
# 埋在样板里。改 schema 时也只有这一处要跟。
_AUTH_COLUMNS = (
    "id, created_at, operator_name, authorization_ref, targets_json, resolved_ips_json, "
    "typed_confirmation, affirmations_json, overrides_json"
)
_SCAN_COLUMNS = (
    "id, created_at, status, authorization_id, template_id, targets_json, scan_mode, "
    "max_budget_usd, provider, auth_shape, strix_llm"
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """一个指向 tmp_path 的 Settings。

    显式传 `console_data_dir` 而不是改 `os.environ`：改环境变量会在测试之间泄漏，
    而且 `Settings` 是 frozen 的，注入构造参数是它设计好的入口。
    """
    return Settings(console_data_dir=tmp_path)


@pytest.fixture
def auth_file(tmp_path: Path) -> Path:
    """一个真的 `auth.json`，经 `write_auth_file` 落盘（所以权限位也是真的）。

    **必须在 `create_app` 之前建好**：lifespan 会读它，读不到就拒绝启动 ——
    那条语义本身也有测试（`test_auth.py::test_missing_auth_file_blocks_startup`），
    所以这里刻意不加"文件已存在就跳过"之类的宽容。
    """
    path = tmp_path / "auth.json"
    write_auth_file(path, AuthRecord.create(USERNAME, PASSWORD).to_json_text())
    return path


@pytest.fixture
def db(tmp_path: Path, settings: Settings) -> Iterator[Database]:
    """一个已连接、已跑完全部迁移的空库。"""
    database = Database(tmp_path / "console.sqlite")
    database.connect()
    database.migrate(settings.migrations_dir)
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def conn(db: Database) -> sqlite3.Connection:
    """直连，供需要手写 SQL 的测试用。

    刻意暴露原始连接而不是包一层 helper：这些测试要断言的正是"SQL 层的约束会不会
    拒绝这条语句"，多一层封装只会让人怀疑是封装挡下来的。

    注意：这里没有事务包裹（`Database.run()` 才有）。测试里每条语句自动提交，
    库本来就是一次性的。
    """
    # 访问私有方法是刻意的：Database 对外只暴露 run()（见 db.py 模块 docstring），
    # 而测试需要绕过它去直接验证 schema。这是测试的特权，不是 API 缺口。
    return db._require_conn()


def insert_authorization(conn: sqlite3.Connection, auth_id: str = "auth-1") -> str:
    conn.execute(
        f"INSERT INTO authorizations ({_AUTH_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?)",  # noqa: S608
        (
            auth_id,
            "2026-09-08T00:00:00.000Z",
            "测试操作者",
            "TICKET-1",
            '["https://example.com"]',
            '["93.184.216.34"]',
            "我确认我有权测试 example.com",
            '["a","b","c"]',
            "{}",
        ),
    )
    return auth_id


def insert_scan(
    conn: sqlite3.Connection,
    scan_id: str = "scan-1",
    authorization_id: str | None = "auth-1",
    auth_shape: str = "single",
    max_budget_usd: float = 2.0,
) -> None:
    conn.execute(
        f"INSERT INTO scans ({_SCAN_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",  # noqa: S608
        (
            scan_id,
            "2026-09-08T00:00:00.000Z",
            "queued",
            authorization_id,
            "web-quick",
            '["https://example.com"]',
            "quick",
            max_budget_usd,
            "anthropic",
            auth_shape,
            "anthropic/claude-sonnet-4-5",
        ),
    )


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """把 logging 的全局状态存下来再还原。

    必须有这个夹具：`configure_logging()` 改的是 **root logger** —— 进程级的全局
    状态。不还原的话，一个测试装上的 handler 会跟着后面所有测试跑，
    pytest 自己的日志捕获也会被搅乱。
    这正是 CLAUDE.md「模块级不得有可变全局状态」那条规则的反面教材：
    logging 是标准库里的全局单例，我们只能围着它做防护。
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_children = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).propagate,
            logging.getLogger(name).level,
        )
        # 这份名单必须覆盖 configure_logging() 会动到的**每一个** logger，
        # 否则漏掉的那个会把 WARNING 级别泄漏给后面所有测试。
        for name in (
            "uvicorn",
            "uvicorn.error",
            "uvicorn.access",
            "litellm",
            "httpx",
            "httpcore",
            "docker",
            "urllib3",
        )
    }
    try:
        yield
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        for name, (handlers, propagate, level) in saved_children.items():
            logger = logging.getLogger(name)
            logger.handlers = handlers
            logger.propagate = propagate
            logger.setLevel(level)
