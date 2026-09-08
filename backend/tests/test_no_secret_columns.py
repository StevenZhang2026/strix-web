"""安全不变式的测试：**任何表都不得有凭据列** + **误扫在结构上不可能**。

这个文件测的不是某个函数的行为，而是两条发布阻断级别的约束
（CLAUDE.md §安全不变式）。它有一条"负面覆盖"要求，很容易被忽略：
不仅要证明脏 schema 被拦住，还要证明**干净的 schema 没被误伤** ——
一个把所有东西都判定为可疑的断言，实际效果等于没有断言，因为下一个人会把它关掉。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app import db as db_module
from app.db import FORBIDDEN_COLUMN_SUBSTRINGS, Database, SecretColumnError
from app.settings import Settings
from tests.conftest import insert_authorization, insert_scan


@pytest.fixture
def migrations_dir(settings: Settings) -> Path:
    """迁移目录。经 Settings 取而不是自己拼路径 —— 那个 property 是单一真源。"""
    return settings.migrations_dir


# =============================================================================
# 一、黑名单本身
# =============================================================================
@pytest.mark.parametrize(
    "word", ["key", "secret", "token", "password", "credential", "access", "aws"]
)
def test_blacklist_contains_mandatory_words(word: str) -> None:
    """这几个词是派发时点名要求的。

    `access` 与 `aws` 单列出来的理由：没有它们，`aws_access_key_id` 只被 `key` 拦住，
    而 `aws_access_id`（同样是凭据的一半）就能溜进来。
    """
    assert word in FORBIDDEN_COLUMN_SUBSTRINGS


def test_blacklist_excludes_auth() -> None:
    """`auth` 刻意**不在**名单里。

    它会拦掉 `authorizations` 表、`authorization_id` 列和 `auth_shape` 列 ——
    而那三个恰恰是本项目最重要的安全结构。把它们逼进"改名或开豁免"的死角
    才是真正的风险。这条测试把这个决定钉住，防止有人"顺手补全"名单。
    """
    assert "auth" not in FORBIDDEN_COLUMN_SUBSTRINGS


def test_no_exemption_mechanism_exists() -> None:
    """断言里**没有豁免口子**。

    这是本文件唯一一条"按名字反射"的测试，值得解释：
    一旦 db.py 里出现 `ALLOWED_SECRET_COLUMNS` 之类的东西，"这个是例外"就会成为
    绕过断言的标准姿势，断言随即失效。而这种改动在 code review 里看起来很无害
    （"就加一个白名单"）。让它变成一条红色的测试，比指望 review 拦住它可靠。

    PLAN.md 自己立的先例是：需要存 `password_hash` 时，把它放进 `${DATA}/auth.json`
    而不是给断言开白名单。
    """
    suspicious = {
        name
        for name in dir(db_module)
        if any(w in name.upper() for w in ("ALLOW", "EXEMPT", "WHITELIST", "IGNORE", "SKIP"))
    }
    assert not suspicious, (
        f"app/db.py 里出现了疑似豁免机制：{sorted(suspicious)}。"
        "列名黑名单不允许有豁免 —— 见 db.py 的三条正确做法。"
    )


# =============================================================================
# 二、正面：干净的 schema 必须通过（负面覆盖）
# =============================================================================
def test_real_schema_passes(db: Database) -> None:
    """仓库里的迁移建出来的库必须通过。

    这条同时也是 CI 里的回归门：以后任何一次迁移引入了脏列名，都会在这里失败，
    而不是等到部署时启动失败。
    """
    db.assert_no_secret_columns()


@pytest.mark.parametrize(
    "clean_name",
    [
        "authorizations",  # 表名
        "authorization_id",  # scans 的外键列
        "auth_shape",  # §N1 的凭据形状维度
        "vault_handle",  # 指向内存 KeyVault 的 opaque UUID
        "env_var_names_json",  # 只存变量名
        "instruction_sha256",
        "typed_confirmation",
        "affirmations_json",
    ],
)
def test_legitimate_names_are_not_flagged(clean_name: str) -> None:
    """这些名字必须**不**命中黑名单。

    `vault_handle` 与 `env_var_names_json` 是 2026-09-08 从 `key_handle` /
    `env_keys_json` 改过来的。改名的真正理由**不是**绕开黑名单（那只是顺带好处），
    而是原名基于一个已被 PLAN.md §N1 推翻的假设：一个 handle 从来不是"一个 Key
    字符串"，它指向一**组**凭据（Bedrock SigV4 要 id + secret + region）。
    叫 `key_handle` 会让读代码的人以为取出来是个字符串，然后写出只取一个值的错误代码。
    """
    hits = [w for w in FORBIDDEN_COLUMN_SUBSTRINGS if w in clean_name.lower()]
    assert not hits, f"{clean_name!r} 被黑名单误伤，命中 {hits}"


# =============================================================================
# 三、反面：脏 schema 必须被拦住
# =============================================================================
@pytest.mark.parametrize(
    "column",
    [
        "api_key",  # 最经典的一个
        "secret_value",
        "access_token",
        "password",
        "aws_access_key_id",  # 只有同时含 aws/access 才拦得住它的各种变体
        "bearer_token",
        "apikey",  # 无下划线
        "user_credential",
        "ANTHROPIC_API_KEY",  # 大写，验证大小写无关
    ],
)
def test_poisoned_column_is_rejected(db: Database, conn: sqlite3.Connection, column: str) -> None:
    """手工 ALTER TABLE 加一个脏列，断言必须拒绝。

    这一条模拟的是**真实事故形态**：有人在生产库上直接 `ALTER TABLE`，或者一次
    没过 review 的迁移。CI 里的 schema 检查看不见这种改动，启动断言看得见。
    """
    # 这里刻意没有 S608 抑制注释：S608（硬编码 SQL 拼接）只盯
    # SELECT/INSERT/UPDATE/DELETE，DDL 不在它的范围内 —— 加了反而会被 RUF100
    # 报成"多余的抑制注释"。（连"抑制注释"这四个字母的英文写法都不能出现在注释里，
    # 否则 ruff 会把它当成一条真的指令去解析，见本文件被 lint 报过的那条 warning。）
    conn.execute(f"ALTER TABLE scans ADD COLUMN {column} TEXT")

    with pytest.raises(SecretColumnError) as excinfo:
        db.assert_no_secret_columns()

    # 报错必须点名到底是哪个对象的哪一列 —— 只说"发现可疑列"没法排障。
    assert column in str(excinfo.value)


def test_poisoned_table_name_is_rejected(db: Database, conn: sqlite3.Connection) -> None:
    """表名本身可疑就够了，无论列名多干净。

    PLAN.md 明确"**不存在 `scan_credentials` 表**" —— 向导收集的测试账号只进 tmpfs
    上的 instruction 文件，DB 只留 `instruction_sha256`。
    """
    conn.execute("CREATE TABLE scan_credentials (scan_id TEXT, value TEXT) STRICT")

    with pytest.raises(SecretColumnError) as excinfo:
        db.assert_no_secret_columns()
    assert "scan_credentials" in str(excinfo.value)


def test_poisoned_view_is_rejected(db: Database, conn: sqlite3.Connection) -> None:
    """视图能给列改名，所以断言必须也扫视图。

    只扫表的话，这一条 CREATE VIEW 就能凭空造出一个叫 api_key 的列，而断言完全
    看不见 —— 而视图恰恰是"为了方便查询"最容易被加进来的东西。
    """
    conn.execute("CREATE VIEW v_leak AS SELECT vault_handle AS api_key FROM scans")

    with pytest.raises(SecretColumnError) as excinfo:
        db.assert_no_secret_columns()
    assert "api_key" in str(excinfo.value)


def test_error_message_deduplicates(db: Database, conn: sqlite3.Connection) -> None:
    """一个列同时命中多个词时，只报一次。

    `aws_secret_access_key` 命中 aws / secret / access / key 四个词。不去重的话
    报错里同一列出现四遍，真正的问题（还有别的列吗）就被淹了。
    """
    conn.execute("ALTER TABLE scans ADD COLUMN aws_secret_access_key TEXT")

    with pytest.raises(SecretColumnError) as excinfo:
        db.assert_no_secret_columns()
    assert str(excinfo.value).count("scans.aws_secret_access_key") == 1


# =============================================================================
# 四、误扫在结构上不可能：authorization_id NOT NULL + 外键
# =============================================================================
def test_scan_without_authorization_is_rejected(conn: sqlite3.Connection) -> None:
    """`authorization_id = NULL` 必须被 DB 拒绝。

    这是本项目最重要的一条 DB 约束。等价的 Python 侧检查随时可能被一个新的代码路径
    绕过（一个新写的 repair 脚本、一次数据修复）；NOT NULL 不会。
    """
    insert_authorization(conn)
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        insert_scan(conn, authorization_id=None)


def test_scan_with_unknown_authorization_is_rejected(conn: sqlite3.Connection) -> None:
    """指向不存在的授权记录也必须被拒绝。

    NOT NULL 只能挡住"没填"，挡不住"随便填一个"。外键才能。
    而外键强制是**每连接**的 PRAGMA 且默认关闭 —— 所以这条测试真正在验的是
    `Database.connect()` 里那行 `PRAGMA foreign_keys = ON` 有没有生效。
    """
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        insert_scan(conn, authorization_id="不存在的授权")


def test_authorization_cannot_be_deleted_while_referenced(conn: sqlite3.Connection) -> None:
    """有扫描引用它时，授权记录不许被删（ON DELETE RESTRICT）。

    否则"删掉授权记录"就成了一条把已完成扫描变成无授权扫描的路径 —— 而审计要求
    的恰恰是反过来：授权证据必须比扫描活得久。
    """
    insert_authorization(conn)
    insert_scan(conn)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute("DELETE FROM authorizations WHERE id = 'auth-1'")


def test_scan_accepts_valid_row(conn: sqlite3.Connection) -> None:
    """负面覆盖：合法的一行必须能插进去。

    没有这条的话，上面三个 pytest.raises 在"任何 INSERT 都失败"时也会全绿。
    """
    insert_authorization(conn)
    insert_scan(conn)
    assert conn.execute("SELECT count(*) FROM scans").fetchone()[0] == 1


# =============================================================================
# 五、其余 CHECK 约束（都是"不随需求变化"的不变量，见 001_init.sql 约定 5）
# =============================================================================
def test_affirmations_must_be_exactly_three(conn: sqlite3.Connection) -> None:
    """三条声明少一条就不该能存。条数是需求的一部分。"""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO authorizations "
            "(id, created_at, operator_name, authorization_ref, targets_json, "
            " resolved_ips_json, typed_confirmation, affirmations_json, overrides_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "auth-bad",
                "2026-09-08T00:00:00.000Z",
                "测试操作者",
                "TICKET-1",
                "[]",
                "[]",
                "确认",
                '["a","b"]',  # 只有两条
                "{}",
            ),
        )


def test_budget_must_be_positive(conn: sqlite3.Connection) -> None:
    """预算 0 的扫描会被 Strix 立刻掐死并宣称"未发现漏洞" —— 最坏的一种假阴性。"""
    insert_authorization(conn)
    with pytest.raises(sqlite3.IntegrityError):
        insert_scan(conn, max_budget_usd=0)


def test_invalid_json_is_rejected(conn: sqlite3.Connection) -> None:
    """坏 JSON 的失败点必须在 INSERT，而不是几小时后读取时的 JSONDecodeError。"""
    insert_authorization(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO scans (id, created_at, status, authorization_id, template_id, "
            " targets_json, scan_mode, max_budget_usd, provider, auth_shape, strix_llm) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "scan-bad",
                "2026-09-08T00:00:00.000Z",
                "queued",
                "auth-1",
                "web-quick",
                "这不是 JSON",
                "quick",
                2.0,
                "anthropic",
                "single",
                "anthropic/claude-sonnet-4-5",
            ),
        )


def test_strict_tables_reject_wrong_type(conn: sqlite3.Connection) -> None:
    """STRICT 表必须拒绝类型不符的值。

    SQLite 默认的动态类型会让 `cost_usd = "abc"` 静默存进去，到出报告时才炸。
    这条验的是 `) STRICT` 那个后缀真的写上了 —— 漏写不会有任何症状，直到某天
    一个字符串被存进了金额列。
    """
    insert_authorization(conn)
    insert_scan(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE scans SET cost_usd = 'abc' WHERE id = 'scan-1'")


# =============================================================================
# 六、迁移账本
# =============================================================================
def test_migration_is_idempotent(db: Database, migrations_dir: Path) -> None:
    """再跑一次不应重复应用。"""
    assert db.migrate(migrations_dir) == []


def test_modified_migration_is_rejected(tmp_path: Path, migrations_dir: Path) -> None:
    """已应用的迁移文件被改动 → 拒绝启动。

    这是一个**静默且致命**的情形：开发机上"顺手补一列"改了 001，其他机器的库里
    没有那一列，代码却假定有。查出来的时候通常已经是生产问题了。
    """
    fake_dir = tmp_path / "migrations"
    fake_dir.mkdir()
    target = fake_dir / "001_init.sql"
    target.write_text(
        (migrations_dir / "001_init.sql").read_text(encoding="utf-8"), encoding="utf-8"
    )

    database = Database(tmp_path / "console.sqlite")
    database.connect()
    try:
        assert database.migrate(fake_dir) == ["001_init.sql"]
        target.write_text(
            target.read_text(encoding="utf-8") + "\n-- 偷偷改一笔\n", encoding="utf-8"
        )
        with pytest.raises(RuntimeError, match="被改动"):
            database.migrate(fake_dir)
    finally:
        database.close()
