"""共享夹具。

原则：**不碰真实网络、不碰真实 Docker、不碰真实数据目录**（CLAUDE.md §测试）。
每个用到数据库的测试都拿一个 `tmp_path` 下的全新库 —— 测试之间零共享状态。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import Database
from app.main import create_app
from app.routes.auth import SESSION_COOKIE_NAME
from app.services.allowlist import AllowlistEntry
from app.services.auth import AuthRecord, write_auth_file
from app.settings import Settings

# ---- 单账号登录的测试凭据 ----------------------------------------------------
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

    两个沙箱变量必须非空：T10 的 `assert_sandbox_env()` 在 lifespan 里拒绝空值（缺了
    抓包代理会静默降级），所以"一份能起得来的配置"就得带上它们 —— 生产里由
    `docker-compose.yml` 写死。要测"没配置"的展示分支就自己构造 Settings
    （`test_system_status.py` 就是那么做的），别指望这个夹具。
    """
    return Settings(
        console_data_dir=tmp_path,
        strix_image="strix-sandbox:test",
        strix_docker_sandbox_network="strix_sandbox",
    )


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
def app(settings: Settings, auth_file: Path, restore_logging: None) -> FastAPI:
    """真实应用，什么都不替换。

    两个依赖的存在本身就是不变式：`auth_file` 必须先落盘（lifespan 读不到它就拒绝
    启动），`restore_logging` 必须在场（lifespan 会改 root logger 这个进程级单例）。
    需要替身的文件**覆写 `anonymous`**，在 lifespan 跑完之后往 `app.state` 里塞 ——
    在这里塞会被 lifespan 原地盖掉。
    """
    return create_app(settings)


@pytest.fixture
def anonymous(app: FastAPI) -> Iterator[TestClient]:
    """未登录的客户端。`with` 已经进过了，所以 lifespan 跑完了。

    `base_url` 必须是 https：会话 cookie 带 `Secure`，http 下 TestClient 不回传它。
    """
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client


@pytest.fixture
def client(anonymous: TestClient) -> TestClient:
    """已登录的客户端。与 `anonymous` 是同一个对象，只是多了一个会话 cookie。"""
    anonymous.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
    assert anonymous.cookies.get(SESSION_COOKIE_NAME), "登录没成功，后面的断言会全是 401"
    return anonymous


def make_entry(label: str = "预生产", **overrides: object) -> AllowlistEntry:
    """一条合法的授权清单条目。`hosts` 默认只有 `example.com`。"""
    fields: dict[str, object] = {
        "label": label,
        "owner": "安全组",
        "authorization_ref": "TICKET-1",
        "hosts": ("example.com",),
    }
    fields.update(overrides)
    return AllowlistEntry.model_validate(fields)


def make_run_dir(
    cwd: Path,
    name: str = "strix-run-1",
    status: str = "completed",
    **extra: object,
) -> Path:
    """在 `cwd/strix_runs/<name>/` 下写一个 `run.json`，返回那个 run 目录。

    **不放真实抓下来的 run 目录夹具**（那要连 `agents.db` 一起，是 T13 的事）：
    T10 只读 `run.json` 的一个 `status` 字段，多余的内容只会让测试意图变模糊。
    """
    run_dir = cwd / "strix_runs" / name
    run_dir.mkdir(parents=True, exist_ok=True)
    record: dict[str, object] = {"status": status}
    record.update(extra)
    (run_dir / "run.json").write_text(json.dumps(record), encoding="utf-8")
    return run_dir


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
