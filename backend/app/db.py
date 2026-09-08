"""SQLite 连接管理、迁移执行、以及"任何表都不得有凭据列"的启动断言。

# 为什么是"一条连接 + 一把锁 + asyncio.to_thread"，而不是 aiosqlite 或连接池

1. `aiosqlite` 不在 `requirements.lock` 里。加一个依赖要论证"标准库做不到什么"
   （CLAUDE.md §编码哲学 4）—— 而 `aiosqlite` 本身就是"一个线程 + 一个队列"，
   跟 `asyncio.to_thread` 是同一个东西，只是包了一层 API。论证不成立。

2. `api` 强制 `--workers 1`（KeyVault 是进程内 dict），所以进程内只有一个事件循环。
   单连接 + `threading.Lock` 就足以序列化全部写入 —— 而 SQLite 的写本来就是全库串行的，
   连接池对写没有任何帮助，只会引入 `database is locked` 这一整类问题。

3. 单连接 + 单锁带来一个白送的性质：**`run()` 里的多表操作天然原子**。
   T12 要在一个事务里更新 `scans` 计数、插 `scan_events`、插 `scan_findings`；
   有了这把锁，"另一个请求插到中间"在结构上不可能，不需要额外的事务协调代码。

代价是所有 DB 访问串行。对本项目是零代价：单用户控制台，写入量是每秒几十条事件。

# 唯一的运行期入口是 `run()`

刻意**没有** `execute()` / `fetch_all()` / `fetch_one()` 这些便利方法。
它们看着方便，但每一个都会诱使调用方把两次调用写成两个事务，从而破坏上面第 3 条。
`run()` 强迫调用方把"一次完整的数据库工作"写成一个函数 —— 事务边界因此在代码里
是可见的。等到第三处出现完全相同的样板再提取（CLAUDE.md §编码哲学 3）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# =============================================================================
# 列名黑名单 —— 安全不变式，**没有豁免口子**
#
# 判定方式：**大小写无关的子串匹配**，同时作用于表名、视图名、列名。
#
# 为什么是子串而不是精确匹配：`aws_access_key_id`、`x_api_token`、`llm_secret_v2`
# 全都要被拦住。精确名单永远追不上下一个人的命名习惯。
#
# 为什么名单里刻意**没有** `auth`：它会拦掉 `authorizations` 表、`authorization_id`
# 列和 `auth_shape` 列 —— 而那三个恰恰是本项目最重要的安全结构（NOT NULL 外键让
# "未授权的扫描"不可能存在）。把它们逼进"改名或开豁免"的死角，才是真正的风险。
#
# 为什么**没有豁免机制**：PLAN.md 自己立的先例是 —— 需要存 `password_hash` 时，
# 把它放进 `${DATA}/auth.json` 而不是给断言开个白名单。一旦有了豁免列表，
# "这个是例外"就会成为绕过断言的标准姿势，断言随即失效。
#
# 遇到"必须存一个名字里带 key 的东西"时，正确的做法有三条，都不含豁免：
#   1. 它不是凭据 → 改个说实话的名字（`key_handle` → `vault_handle` 就是这么来的：
#      handle 指向一**组**凭据，原名基于一个已被 §N1 推翻的假设，本来就是错的）；
#   2. 它只是变量名的列表 → 叫 `env_var_names_json`；
#   3. 它真的是凭据 → **它不该进数据库**。
# =============================================================================
FORBIDDEN_COLUMN_SUBSTRINGS: frozenset[str] = frozenset(
    {
        "key",
        "secret",
        "token",
        "password",
        "passwd",
        "pwd",
        "credential",
        # access / aws 是任务派发里点名要求的两个词：没有它们，`aws_access_key_id`
        # 只被 `key` 拦住，而 `aws_access_id` 就能溜进来。
        "access",
        "aws",
        "bearer",
        "apikey",
        "privkey",
    }
)

# 枚举全库的表/视图与其列。
#
# `pragma_table_info` 是表值函数（SQLite 3.16+），可以直接 JOIN —— 比"先查
# sqlite_master 再对每张表跑一次 PRAGMA"少一层 Python 循环，也顺带避免了把表名
# 拼进 PRAGMA 字符串（那是唯一无法参数化的地方）。
#
# 含 `type = 'view'`：视图能给列改名。只查表的话，
# `CREATE VIEW v AS SELECT vault_handle AS api_key FROM scans` 就能凭空造出一个
# 叫 api_key 的列，断言完全看不见。
_ENUMERATE_COLUMNS_SQL = """
SELECT m.name AS obj_name, ti.name AS col_name
FROM sqlite_master AS m
JOIN pragma_table_info(m.name) AS ti
WHERE m.type IN ('table', 'view')
  AND m.name NOT LIKE 'sqlite_%'
ORDER BY m.name, ti.cid
"""

# 迁移文件名的合法形状。约束它的唯一理由：文件名会被拼进 SQL 脚本
# （`executescript` 不接受参数绑定，见 migrate() 里的注释）。
_SAFE_MIGRATION_NAME = re.compile(r"[0-9]{3}_[a-z0-9_]+\.sql")

# 迁移账本。它本身也要满足列名黑名单（`filename` / `sha256` / `applied_at` 都干净）。
_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT PRIMARY KEY,
    sha256     TEXT NOT NULL,
    applied_at TEXT NOT NULL
) STRICT
"""


class SecretColumnError(RuntimeError):
    """启动断言失败。刻意不是 `ConsoleError` 的子类。

    `ConsoleError` 是"能经 HTTP 返回给用户的业务错误"。这一条不是业务错误 ——
    它意味着进程不该启动。给它一个 HTTP status 会暗示"某个请求会收到这个错误"，
    而实际上根本没有请求能进来。
    """


class Database:
    """持有唯一那条 SQLite 连接。

    实例挂在 `app.state.db`，显式传递，不做模块级单例
    （CLAUDE.md §Python：模块级不得有可变全局状态）。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None
        # 序列化全部访问。见模块 docstring 第 3 条 —— 这把锁同时是"多表操作原子性"
        # 的实现手段，不只是并发保护。
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    # ---- 生命周期 -----------------------------------------------------------
    def connect(self) -> None:
        if self._conn is not None:
            raise RuntimeError("Database.connect() 被调用了两次")

        self._path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(
            self._path,
            # 连接会被 asyncio.to_thread 派出的**不同**工作线程使用。
            # 安全性由 self._lock 保证（同一时刻只有一个线程在用它），
            # 而不是由 sqlite3 自己的线程检查保证。
            check_same_thread=False,
            # 关掉 sqlite3 的隐式事务管理。它的默认行为（"遇到 INSERT 自动 BEGIN，
            # 遇到 SELECT 自动 COMMIT"）会让事务边界取决于**语句类型**而不是代码结构，
            # 是一个极难排查的 bug 源。我们自己写 BEGIN IMMEDIATE / COMMIT。
            isolation_level=None,
            # 5 秒。WAL 下写锁竞争只发生在两个写事务之间，而我们只有一条连接 ——
            # 这个超时实际只对"外部进程正在 sqlite3 CLI 里写"这种排障场景生效。
            timeout=5.0,
        )
        conn.row_factory = sqlite3.Row

        # WAL：读不阻塞写。对本项目的直接意义是"扫描进行中还能读列表页"。
        # 已在容器里实测过它在同路径挂载的 VirtioFS 卷上确实生效
        # （journal_mode 返回 wal，-wal / -shm 文件出现）—— 这不是理所当然的，
        # 网络文件系统上 WAL 会静默退回 delete 模式。
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if mode.lower() != "wal":
            raise RuntimeError(
                f"无法在 {self._path} 上启用 WAL（PRAGMA journal_mode 返回 {mode!r}）。"
                "数据目录很可能在一个不支持共享内存的文件系统上。"
            )

        # NORMAL 而不是 FULL：WAL 下 NORMAL 只在断电时可能丢**最后几个已提交事务**，
        # 不会损坏数据库。我们丢的最坏情况是几条事件镜像 —— 而 FULL 要求每次提交
        # 都 fsync，在 VirtioFS 上代价很大。
        conn.execute("PRAGMA synchronous = NORMAL")

        # ⚠️ 外键强制是**每连接**的，且默认**关闭**。忘了这行，
        # `scans.authorization_id REFERENCES authorizations(id)` 就只是一句注释 ——
        # 本项目最重要的那条约束会静默失效。所以它必须紧贴 connect()。
        conn.execute("PRAGMA foreign_keys = ON")
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        if fk != 1:
            raise RuntimeError("PRAGMA foreign_keys = ON 未生效，拒绝启动")

        self._conn = conn
        logger.info("SQLite 已连接", extra={"db_path": str(self._path), "journal_mode": mode})

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database 未连接，先调 connect()")
        return self._conn

    # ---- 唯一的运行期入口 ---------------------------------------------------
    async def run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """在工作线程里跑一个同步的数据库函数，整体包成一个事务。

        `fn` 是**同步**的，这是刻意的：sqlite3 是同步库，把它伪装成 async 只会
        让人在里面写出真正阻塞事件循环的代码（CLAUDE.md §Python：async 函数里
        禁止同步阻塞 IO）。

        `BEGIN IMMEDIATE` 而不是 `BEGIN`（DEFERRED）：DEFERRED 到第一条写语句才取锁，
        于是"先 SELECT 再按结果 UPDATE"这种最常见的形态可能在中间被别人插入。
        IMMEDIATE 一开始就拿写锁 —— 我们本来就串行，白拿的正确性。
        """
        return await asyncio.to_thread(self._run_sync, fn)

    def _run_sync(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        with self._lock:
            conn = self._require_conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn)
            except BaseException:
                # BaseException 而不是 Exception：KeyboardInterrupt / CancelledError
                # 也必须回滚，否则连接会卡在一个开着的事务里，之后每次
                # BEGIN IMMEDIATE 都报 "cannot start a transaction within a
                # transaction"，整个进程的数据库访问全死。这不是 bare except
                # （CLAUDE.md 禁的是那个）—— 类型明确，且立即重抛。
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return result

    # ---- 迁移 ---------------------------------------------------------------
    def migrate(self, migrations_dir: Path) -> list[str]:
        """按文件名顺序执行未应用的 `*.sql`，返回本次应用的文件名。

        同步方法，只在启动时调用（此时事件循环还没开始跑请求），所以不走 `run()`。

        为什么记 sha256 而不只记文件名：**已应用的迁移文件被改动**是一个静默且致命的
        情形 —— 开发机上"顺手补一列"改了 001，其他机器的库里没有那一列，代码却假定
        有。查出来的时候通常已经是生产问题了。这里直接拒绝启动。

        为什么不实现回滚 / down 迁移：一个单用户本地控制台，回滚的正确做法是删库重建
        （数据是扫描记录，不是业务主数据）。写一套没人敢在生产上用的 down 脚本
        是纯负债（CLAUDE.md §编码哲学 3）。
        """
        conn = self._require_conn()
        conn.execute(_SCHEMA_MIGRATIONS_DDL)

        applied: dict[str, str] = {
            row["filename"]: row["sha256"]
            for row in conn.execute("SELECT filename, sha256 FROM schema_migrations")
        }

        files = sorted(migrations_dir.glob("*.sql"))
        if not files:
            raise RuntimeError(f"{migrations_dir} 下没有任何 .sql 迁移文件")

        newly_applied: list[str] = []
        for path in files:
            sql = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()

            if path.name in applied:
                if applied[path.name] != digest:
                    raise RuntimeError(
                        f"迁移 {path.name} 在应用之后被改动了"
                        f"（库里记录 {applied[path.name][:12]}…，"
                        f"当前文件 {digest[:12]}…）。"
                        "已应用的迁移是不可变的 —— 请新增一个迁移文件，"
                        "或删掉数据库重建。"
                    )
                continue

            # ⚠️ 事务控制必须写在**脚本内部**，不能用外层的 conn.execute("BEGIN")。
            #
            # 已实测：`executescript()` 在执行前会先隐式 COMMIT 掉挂着的事务
            # （Python 文档明确说明，且"任何事务控制都必须加进 sql_script"）。
            # 所以 `BEGIN IMMEDIATE` → `executescript(...)` → `COMMIT` 这个看起来
            # 最自然的写法会在最后一步抛
            # `OperationalError: cannot commit - no transaction is active`。
            #
            # 账本的 INSERT 也放进同一个脚本，于是"DDL 建好了但账本没记"这个需要
            # 人工介入的中间状态在结构上不存在（重启会重跑 CREATE TABLE 并硬失败）。
            #
            # 两个插值的值都不可能带 SQL：filename 由下面的正则约束，
            # sha256 由 hexdigest() 保证只有 [0-9a-f]。executescript 不接受参数绑定，
            # 所以这是唯一的做法 —— 用断言把"可插值"变成显式前提，而不是默默相信它。
            if not _SAFE_MIGRATION_NAME.fullmatch(path.name):
                raise RuntimeError(
                    f"迁移文件名 {path.name!r} 不符合 {_SAFE_MIGRATION_NAME.pattern}。"
                    "文件名会被拼进 SQL，命名必须受约束。"
                )
            script = (
                "BEGIN IMMEDIATE;\n"
                f"{sql}\n"
                "INSERT INTO schema_migrations (filename, sha256, applied_at) "
                f"VALUES ('{path.name}', '{digest}', "
                "strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));\n"
                "COMMIT;\n"
            )
            try:
                conn.executescript(script)
            except BaseException:
                # 脚本里的 BEGIN 已经开了事务，异常时它还挂着 —— 不回滚的话
                # 后续每次 BEGIN IMMEDIATE 都会报"事务已开始"，整个进程的 DB 全死。
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

            newly_applied.append(path.name)
            logger.info("已应用迁移", extra={"migration": path.name})

        return newly_applied

    # ---- 安全不变式 ---------------------------------------------------------
    def assert_no_secret_columns(self) -> None:
        """全库扫一遍表名/视图名/列名，命中黑名单即拒绝启动。

        这是 CLAUDE.md §安全不变式的第一条，也是"不变量写进代码不是写进文档"的范例：
        一句注释里的"不要建密钥列"会被忽略，一个让进程起不来的断言不会。

        为什么在启动时跑、而不只在 CI 里：CI 只覆盖仓库里的迁移文件。它拦不住
        "有人在生产库上手工 `ALTER TABLE scans ADD COLUMN api_key TEXT`"，
        或者一个第三方库偷偷建表。启动断言拦得住 —— 下次重启就起不来。
        """
        conn = self._require_conn()
        # set 而不是 list：表名命中时，每一列都会重复报同一条；而一个列名可能同时
        # 命中多个词（`aws_secret_access_key` 命中 aws/secret/access/key 四个）。
        # 所以"一个对象一条"要靠**把全部命中词收进同一条消息里**，
        # 而不是每命中一个词就 append 一次 —— 后者的字符串各不相同，set 也去不掉。
        offenders: set[str] = set()

        def hits(name: str) -> list[str]:
            return sorted(bad for bad in FORBIDDEN_COLUMN_SUBSTRINGS if bad in name.lower())

        for row in conn.execute(_ENUMERATE_COLUMNS_SQL):
            obj_name: str = row["obj_name"]
            col_name: str = row["col_name"]

            col_hits = hits(col_name)
            if col_hits:
                offenders.add(f"{obj_name}.{col_name}（命中 {', '.join(col_hits)}）")

            obj_hits = hits(obj_name)
            if obj_hits:
                # 表名本身可疑就够了：一张叫 scan_credentials 的表，无论列名多干净
                # 都是设计错误。PLAN.md 明确"不存在 scan_credentials 表"。
                offenders.add(f"表/视图 {obj_name}（命中 {', '.join(obj_hits)}）")

        if offenders:
            raise SecretColumnError(
                "数据库里存在疑似存放凭据的表/视图/列，拒绝启动：\n  "
                + "\n  ".join(sorted(offenders))
                + "\n凭据绝不落盘（CLAUDE.md §安全不变式）。"
                "本断言**没有豁免机制** —— 请改名（若它不是凭据）"
                "或把它移出数据库（若它是）。"
            )
