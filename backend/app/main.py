"""FastAPI 应用工厂与启动断言。

# 启动顺序是设计的一部分，不要随手调换

    1. load_settings()                 配置不合法就没有下一步
    2. assert_no_credential_env()      **在打出任何一行日志之前**确认环境干净
    2b. assert_single_worker()         判据在 `key_vault`（那条约束的理由就是 vault 是
                                       进程内 dict）；紧挨第 2 步调，理由同上
    2c. assert_sandbox_env()           两个沙箱变量缺一个，抓包代理就静默降级；纯配置
                                       校验、不打日志，所以排在第 3 步之前
    2d. KeyVault()                     **必须在第 3 步之前** —— Redactor 要在日志配好
      + ScanSecretRegistry()           之前就拿到**全部**凭据来源。不用"先占位再
                                       rebind"：那会造出一个"装上了但还是旧的"中间态，
                                       失败模式是日志照打、只是不脱敏（静默）。注册表
                                       把 vault 那个来源**包住**，所以对外仍然只有一个
                                       provider 交给唯一的 Redactor（T12b）
    3. configure_logging()             从这里起日志才带脱敏
    4. 数据目录可写 + 建子目录         下面每一步都要写这个目录
    5. db.connect() / migrate()        建表
    6. db.assert_no_secret_columns()   **必须在 migrate 之后** —— 要检查的是迁移
                                       实际建出来的 schema，不是迁移文件的文本
    6b. _reconcile_interrupted_scans() 上一次进程留下的 starting/running 残行翻成
                                       interrupted。**这是被 `docker kill` 掐死时唯一的
                                       兜底** —— 那种走法没有任何代码跑得到，所以只能
                                       在下次启动时收拾。必须在 migrate 之后
    7. AuthService.load()              读 auth.json；读不出来就**拒绝启动**
    8. create_task(run_sweeper)        本进程唯一的后台任务。`finally` 里 cancel
                                       **并 await**，否则每次正常停机都会打出
                                       "Task was destroyed but it is pending"
    9. create_task(run_forever)        沙箱回收（reaper）与留存清理（retention）。
                                       两者都**无条件**起：留存清理的那个开关
                                       （`CONSOLE_RETENTION_DAYS`，默认 0 = 不删）
                                       只有一个落点，在 `run_forever` 自己里面。
                                       `finally` 里的 cancel+await 必须排在
                                       `db.close()` **之前** —— 它们都会 `await db.run`

第 2 步排在第 3 步之前是刻意的：如果环境里真有凭据，我们希望在**任何日志代码运行
之前**就失败。日志系统本身不会打凭据，但"启动过程中新增一行 logger.info(...)"是随时
会发生的事，而那行可能记录环境。把断言放最前面，这个风险就不存在。

第 6 步排在第 5 步之后同样是刻意的：CI 里的 `test_no_secret_columns.py` 检查的是
"迁移文件建出来的库"，而这里检查的是"这台机器上的真实库" —— 后者可能被人手工
`ALTER TABLE` 过。两个检查的对象不同，都要有。

第 7 步"读不出来就拒绝启动"是**安全语义**，不是健壮性偏好：如果 `auth.json` 缺失或
损坏时我们退化成"没有账号，先放行"，那么删掉那个文件就是一条绕过登录的路径。
代价是可见的 —— 没跑过 `./setup.sh`（C17f）的机器上 `api` 起不来，而错误信息里写着
要跑什么。

# 刻意不在这里做的事

- **`WEB_CONCURRENCY` / `--workers 1` 的启动校验**的判据不在本文件：它是
  `key_vault.assert_single_worker`（跟 KeyVault 同一个模块，因为那条约束的理由就是
  KeyVault 是进程内 dict）。本文件只在第 2b 步调它。
- **任何 docker 探测。** T3 的 `/api/system/status` 由**人**主动触发，启动期一次都
  不探。启动时探 docker 会让"docker daemon 正在重启"变成"api 起不来"，而
  `api` 起不来就没有任何界面能告诉用户是 docker 的问题。lifespan 里只做一件与
  docker 相关的事：把传输层与"我是哪个容器"这两个**不需要跟 daemon 说话**的事实
  装进 `app.state`。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.db import Database
from app.errors import (
    ConsoleError,
    InternalError,
    InvalidRequestError,
    MethodNotAllowedError,
    NotFoundError,
    assert_scan_failure_code,
)
from app.logging_setup import Redactor, configure_logging, trace_id_var
from app.routes import allowlist as allowlist_routes
from app.routes import audit as audit_routes
from app.routes import auth as auth_routes
from app.routes import health as health_routes
from app.routes import keys as keys_routes
from app.routes import providers as providers_routes
from app.routes import scans as scans_routes
from app.routes import system as system_routes
from app.routes import targets as targets_routes
from app.routes import templates as templates_routes
from app.services import dns_resolver, llm_client
from app.services.allowlist import AllowlistStore
from app.services.audit import iso_utc
from app.services.auth import AuthService
from app.services.docker_probe import UnixSocketTransport, read_self_container_ref
from app.services.image_puller import ImagePuller
from app.services.key_vault import KeyVault, assert_single_worker
from app.services.reaper import Reaper
from app.services.retention import RetentionSweeper
from app.services.scan_channel import ChannelRegistry
from app.services.scan_secrets import ScanSecretRegistry
from app.services.scan_supervisor import ScanSupervisor, assert_sandbox_env
from app.settings import Settings, assert_no_credential_env, load_settings
from app.strix_profile import profile_for

logger = logging.getLogger(__name__)

TRACE_ID_HEADER = "X-Trace-Id"


def _resolve_strix_version() -> str:
    """读已安装的 strix-agent 版本。

    拿不到就拒绝启动，而不是返回 "unknown"：整个项目建立在
    `strix-agent==1.6.2` 的精确行为上（`agents.db` 结构、退出码语义、Rich 面板标题、
    两个未文档化的 env）。一个装不出 strix 的镜像跑起来之后，会在第一次扫描时
    以一种完全无关的错误失败。Dockerfile 构建期已经断言过一次，这里是防"镜像被改动"。
    """
    try:
        return version("strix-agent")
    except PackageNotFoundError as exc:
        raise RuntimeError(
            "未安装 strix-agent。镜像构建不完整 —— 见 backend/Dockerfile 的版本断言。"
        ) from exc


def _prepare_data_dir(settings: Settings) -> None:
    """确认数据目录真的可写，并建出固定的子目录。

    为什么要真写一个文件而不是只看 `os.access(W_OK)`：
    `os.access` 在容器里以 root 运行时几乎总是返回 True（root 绕过权限位），
    但同路径挂载的宿主目录可能是只读挂载、可能不存在、可能满了。真写一次才知道。
    这跟 pitfalls 条 18 是同一类教训 —— 别用一个"通常成立"的间接判据代替直接验证。
    """
    data_dir = settings.console_data_dir
    if not data_dir.is_dir():
        raise RuntimeError(
            f"数据目录 {data_dir} 不存在或不是目录。它必须是同路径挂载进来的宿主绝对路径，"
            "由 ./setup.sh 写进 .env 的 STRIX_HOST_DATA_DIR。"
        )

    probe = data_dir / f".write-probe-{uuid.uuid4().hex}"
    try:
        probe.write_bytes(b"")
    except OSError as exc:
        raise RuntimeError(f"数据目录 {data_dir} 不可写：{exc}") from exc
    finally:
        # missing_ok：写失败时文件本来就不存在，别用一个 FileNotFoundError
        # 掩盖上面那个真正有用的错误。
        probe.unlink(missing_ok=True)

    for sub in (settings.scans_dir, settings.audit_dir, settings.config_dir):
        sub.mkdir(parents=True, exist_ok=True)


_INTERRUPTED_SUMMARY = "The console process exited while this scan was still running."
"""`scans.error_message` 的固定摘要。英文：它与 Strix 自己写进这一列的归因摘要同格式
（UI 文案按 `error_code` 查 `zh-CN.json`，这一列是给排障的人 grep 的）。"""


async def _reconcile_interrupted_scans(db: Database) -> None:
    """把上一次进程留下的 `starting` / `running` 残行翻成 `interrupted`。

    为什么必须有：`docker kill` / 断电这两种走法下 `_run_to_completion` 的 finally 一行
    都跑不到，那些行会**永远**停在 `running`。而"正在跑"这个状态会被并发闸、列表页、
    reaper 当真 —— 一行假的 `running` 会让控制台从此拒绝起新扫描。

    `exit_code` / `exit_meaning` 留 NULL：根本没有进程退出过，编一个值是撒谎。
    """
    code = assert_scan_failure_code("interrupted_by_restart")
    finished_at = iso_utc(datetime.now(UTC))

    def reconcile(conn: sqlite3.Connection) -> int:
        cursor = conn.execute(
            "UPDATE scans SET status = 'interrupted', error_code = ?, error_message = ?, "
            "finished_at = ? WHERE status IN ('starting', 'running')",
            (code, _INTERRUPTED_SUMMARY, finished_at),
        )
        return cursor.rowcount

    reconciled = await db.run(reconcile)
    if reconciled:
        logger.warning(
            "上次停机时还有扫描在跑，已标记为 interrupted",
            extra={"scans": reconciled, "error_code": code},
        )


def console_error_for_http_status(status: int) -> ConsoleError:
    """把一个框架抛出来的 HTTP 状态码翻译成我们自己的错误码。

    写成模块级纯函数（而不是塞在处理器闭包里）的理由是可测：这份映射表是对外契约的
    一部分，值得被直接断言，而不是只能经 TestClient 间接观察。

    它只决定**码**，不决定响应的 HTTP 状态 —— 后者由调用方保留框架的原值。

    只有 404 / 405 有专属码，其余按类别兜底。刻意**不**给 4xx 逐个编码：
    我们自己的代码从不 `raise HTTPException`（业务错误一律走 `ConsoleError`），
    所以这里能收到的状态码实际只有 Starlette 路由层产出的那两个。给"永远不会发生"的
    分支编专属码，只会让人误以为它们有含义（CLAUDE.md §编码哲学 3）。
    """
    if status == 404:
        return NotFoundError()
    if status == 405:
        return MethodNotAllowedError()
    if 400 <= status < 500:
        return InvalidRequestError()
    return InternalError()


def _install_exception_handlers(app: FastAPI) -> None:
    """四个处理器。响应体形状统一由 `ConsoleError.to_payload()` 决定。"""

    async def handle_console_error(request: Request, exc: Exception) -> JSONResponse:
        # 签名标 Exception 是 Starlette 处理器的类型契约要求；实际类型由注册时的
        # 键保证。这里 assert 一下，让类型收窄是显式的而不是靠注释声明。
        assert isinstance(exc, ConsoleError)  # noqa: S101
        trace_id = trace_id_var.get()
        # 业务错误是**预期**会发生的（用户填错、目标不在白名单），用 warning 而不是
        # error，否则告警噪音会盖掉真正的 bug。
        logger.warning(
            "业务错误",
            extra={"code": exc.code, "http_status": exc.status, "path": request.url.path},
        )
        # `response_headers()` 默认返回 `{}`，目前只有 `AuthLockedError` 覆盖它
        # （429 要带 `Retry-After`，RFC 9110 §15.6.4）。它是错误**类型**的一部分，
        # 所以由错误对象决定，而不是让每个抛出点各自往响应里塞头。
        return JSONResponse(
            status_code=exc.status,
            content=exc.to_payload(trace_id),
            headers=exc.response_headers() or None,
        )

    async def handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)  # noqa: S101
        trace_id = trace_id_var.get()
        # **刻意不把 exc.errors() 放进响应**。Pydantic 的错误详情里含 `input` 字段 ——
        # 也就是**用户提交的原值**。请求体里放错了 Key 的话，那个 Key 会原样出现在
        # 响应里，然后进浏览器 devtools、进前端错误上报、进用户贴给我们的截图。
        # 只有字段路径进日志（不含值），响应里只有机器码。
        logger.warning(
            "请求校验失败",
            extra={
                "path": request.url.path,
                "fields": [".".join(str(p) for p in e["loc"]) for e in exc.errors()],
            },
        )
        err = InvalidRequestError()
        return JSONResponse(status_code=err.status, content=err.to_payload(trace_id))

    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        trace_id = trace_id_var.get()
        # exc_info=True 让 traceback 进日志。它会过 RedactingJsonFormatter ——
        # 那正是为什么脱敏必须挂在 Formatter 上：litellm 之类的库会把请求参数
        # （含凭据）塞进异常信息。
        logger.error("未处理异常", exc_info=exc, extra={"path": request.url.path})
        err = InternalError()
        # 响应里绝不带 str(exc)：那是最常见的一种信息泄漏，且用户对它毫无办法 ——
        # 他们能做的只是把 trace_id 报给我们。
        return JSONResponse(status_code=err.status, content=err.to_payload(trace_id))

    async def handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
        """框架自己抛的 HTTPException —— 实际来源只有路由层的 404 与 405。

        为什么必须自己接：Starlette 的默认处理器返回 `{"detail": "Not Found"}`，
        跟本项目的 `{code, trace_id, params}` 是**两种形状**。而前端被要求"按码分支、
        不得匹配文案"（CLAUDE.md §错误与文案）—— 两种形状就意味着前端要写两个解析器。
        更糟的是它偏偏在"出了意料之外的事"时才出现（重构后前端请求了一个拼错的路径），
        那正是最需要看到一个明确错误码的时刻，而那时前端只会显示一片空白。

        响应用 `exc.status_code` 而不是映射结果的 `.status`：我们只修正响应体的形状，
        不改框架对 HTTP 语义的判断。404 / 405 两者本来就一致，兜底分支下保留原状态码
        才是诚实的。
        """
        assert isinstance(exc, StarletteHTTPException)  # noqa: S101
        trace_id = trace_id_var.get()
        err = console_error_for_http_status(exc.status_code)
        logger.warning(
            "路由未匹配",
            extra={
                "code": err.code,
                "http_status": exc.status_code,
                "path": request.url.path,
                "method": request.method,
            },
        )
        # 转发 `exc.headers`：405 必须带 `Allow`（RFC 9110 §15.5.6），那个头是
        # Starlette 路由层填的。本项目自己从不 raise HTTPException，所以这里不存在
        # "把用户可控的头透传出去"的问题。
        return JSONResponse(
            status_code=exc.status_code,
            content=err.to_payload(trace_id),
            headers=exc.headers,
        )

    app.add_exception_handler(ConsoleError, handle_console_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    # 覆盖 Starlette 自带的 HTTPException 处理器。注册顺序无关 —— Starlette 按
    # `type(exc).__mro__` 查表，`StarletteHTTPException` 比下面的 `Exception` 更具体。
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    # Starlette 的 ServerErrorMiddleware 在调完这个处理器之后**仍会重抛**异常，
    # 所以 uvicorn 也会记一条 traceback。两条日志都经过脱敏，重复可以接受 ——
    # 换取的是 500 响应也符合 {code, trace_id, params} 契约。
    app.add_exception_handler(Exception, handle_unexpected)


def _install_trace_id_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def with_trace_id(
        request: Request, call_next: Callable[[Request], Awaitable[JSONResponse]]
    ) -> JSONResponse:
        # 刻意**不**接受客户端传入的 X-Trace-Id：那是一个可控的日志注入点
        # （客户端可以塞进换行、塞进伪造的其他请求 id）。trace_id 由服务端生成，
        # 只出现在响应头里。
        trace_id = uuid.uuid4().hex
        token = trace_id_var.set(trace_id)
        try:
            response = await call_next(request)
        finally:
            # 必须 reset：ContextVar 的值在同一个 task 里会一直留着，
            # 不 reset 会让后续请求（复用同一 task 的情况）带上旧 id。
            trace_id_var.reset(token)
        response.headers[TRACE_ID_HEADER] = trace_id
        return response


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造应用。

    `settings` 可注入是为了测试（不用改 `os.environ` 就能换配置），
    生产路径走 `None` → `load_settings()`。
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved = settings if settings is not None else load_settings()

        # 顺序见模块 docstring。
        assert_no_credential_env(os.environ)
        assert_single_worker(os.environ)
        assert_sandbox_env(resolved)

        # KeyVault 必须**在 configure_logging 之前**构造：Redactor 要在日志配好之前
        # 拿到凭据来源。这一步可行是因为 `KeyVault.__init__` 的唯一依赖是 clock ——
        # 不要 settings、不碰 DB、不碰文件，此刻它必然是空的。
        #
        # 刻意不用"先传占位 provider、建完 vault 再 rebind"：那会造出一个
        # "provider 装上了但还是旧的"的中间态，而它的失败模式是**静默**的
        # （日志照打、只是不脱敏）—— 与 logging_setup docstring 里那个已实测的坑同型。
        # 一次性、不可变的绑定没有那个窗口。
        #
        # 测试账号口令不在 vault 里（它不是 LLM 凭据），所以 `ScanSecretRegistry` 把
        # vault 那个来源**包住**，对外仍然只有一个 provider 交给唯一的 Redactor。
        # 刻意不在这里写 `lambda: key_vault.secret_values() | scan_secrets.values()`：
        # 组合逻辑（快照、并集）要有一个可单测的落点，而 main.py 里的 lambda 测不到。
        # 它必须与 Redactor **同时**构造，理由同上：Redactor 要在日志配好之前拿到
        # **全部**凭据来源，事后再补一个来源就又造出了那个静默的中间态。
        key_vault = KeyVault()
        scan_secrets = ScanSecretRegistry(key_vault.secret_values)
        redactor = Redactor(secret_provider=scan_secrets.secret_values)
        configure_logging(resolved.console_log_level, redactor)

        strix_version = _resolve_strix_version()
        logger.info(
            "启动中",
            extra={
                "app_version": __version__,
                "strix_version": strix_version,
                "data_dir": str(resolved.console_data_dir),
            },
        )

        _prepare_data_dir(resolved)

        db = Database(resolved.db_path)
        db.connect()
        try:
            applied = db.migrate(resolved.migrations_dir)
            if applied:
                logger.info("迁移完成", extra={"applied": applied})
            db.assert_no_secret_columns()
            await _reconcile_interrupted_scans(db)
            # 单账号登录（T4b）。放在这个 try 里面是为了让它失败时也走下面的
            # `db.close()` —— 不然 `auth.json` 不存在的机器上会同时留下一个泄漏的
            # SQLite 连接和一对 -wal/-shm 文件，把"没跑 setup.sh"这件事的现场弄脏。
            #
            # **在 yield 之前**加载，而不是让鉴权依赖懒加载：懒加载会让第一个请求
            # 承担一次可能的 AuthFileError，而那时报错的位置是某个业务接口，
            # 跟真正的原因（没跑过 ./setup.sh 的 C17f）差得很远。
            auth_service = AuthService.load(resolved.auth_path)
        except BaseException:
            # 启动失败也要关连接：WAL 会留下 -wal/-shm 文件，且连接泄漏会让
            # 后续的重启诊断更乱。
            db.close()
            raise

        app.state.settings = resolved
        app.state.redactor = redactor
        app.state.key_vault = key_vault
        app.state.scan_secrets = scan_secrets
        app.state.db = db
        app.state.strix_version = strix_version
        app.state.auth = auth_service
        # T3。两者都**不跟 docker daemon 说话**：传输层是无状态的（每次请求新建连接），
        # 容器自省只读 `/proc/self/mountinfo`。所以 docker 挂着也不影响启动。
        #
        # `self_container_ref` 在这里解析一次而不是每次请求都读 /proc：它是进程生命期
        # 内的常量（容器 id 不会变），而单测要替换它时改一个 state 字段就够了。
        app.state.docker_transport = UnixSocketTransport()
        app.state.self_container_ref = read_self_container_ref()
        # T11b。**一个进程一个 puller**：拉取状态与那个后台任务都挂在它身上，多一个实例
        # 就多一次并行拉取。同样**不跟 daemon 说话** —— 构造它只是记下传输层。
        app.state.image_puller = ImagePuller(app.state.docker_transport)
        # T8。授权清单是**文件**，不是表 —— 操作者可以直接编辑它，所以 store 每次读取都
        # 顺带做一次热重载检查。这里主动加载一次，只为了让"清单是坏的"这件事出现在**启动
        # 日志**里而不是第一个请求的响应里；加载失败不影响启动（没有清单是合法状态，
        # 坏清单则失败关闭，两者都由 `allowlist.decide` 决定，见那个模块的 docstring）。
        allowlist_store = AllowlistStore(resolved.allowlist_path)
        app.state.allowlist = allowlist_store
        snapshot = allowlist_store.current()
        if snapshot.error is not None:
            logger.warning(
                "授权清单当前不可用，按失败关闭处理",
                extra={"file_error": snapshot.error.code.value, "line": snapshot.error.line},
            )
        else:
            logger.info(
                "授权清单已加载",
                extra={
                    "file_present": snapshot.file_present,
                    "entries": 0 if snapshot.config is None else len(snapshot.config.entries),
                },
            )
        # T8。`/api/targets/validate` 的 DNS 解析器。挂在 state 上而不是直接 import ——
        # 单测禁止碰真实网络（CLAUDE.md §测试），这是那条规则的注入点。
        app.state.dns_resolver = dns_resolver.resolve
        # T7b。`POST /api/keys` 的验活器。与上面那个 resolver 同一条理由：单测必须能
        # 换掉它（真实验活会拿凭据往模型端点发一次请求）。
        app.state.llm_verifier = llm_client.verify
        # T10。**必须在本轮就接进 lifespan**：否则 `shutdown()` 没有调用方，那就是一段
        # 没人跑过的代码。路由是 T12 的事，这里只让它有主。
        # T11a。两者互相要一个东西，所以 reaper 先建：它拿的是"到时候去问在册 id"的
        # 闭包（`supervisor` 在下一行就绑好，而第一次调用发生在 `run_forever` 里）。
        reaper = Reaper(app.state.docker_transport, lambda: supervisor.active_scan_ids())
        supervisor = ScanSupervisor(resolved, strix_version, on_scan_finished=reaper.request_sweep)
        app.state.reaper = reaper
        app.state.supervisor = supervisor
        # W2c。实时流的注册表：每次扫描起进程时开一个 channel、进程退出后关掉（起关都在
        # `routes/scans.py::_run_to_completion` 的 try/finally 两端）。**必须在本轮就接进
        # lifespan**，否则 `ScanChannel` 就是一段没人跑过的代码。
        # `scans_dir` 传 `Settings` 的那个 property 本身：`EventMirror` 的 rel_path 取它的
        # `.name`，拼过的子目录会静默写出错的相对路径。
        app.state.channels = ChannelRegistry(
            db=db,
            scans_dir=resolved.scans_dir,
            profile=profile_for(strix_version),
            redact=redactor.redact,
        )
        # T12c。两者都**必须在 lifespan 里**建，不能是模块级常量：`asyncio.Lock` 绑定它被
        # 创建时的事件循环，模块级那一个会在测试里跨多个循环复用（同一个进程起好几个 app），
        # 而"锁看起来在但其实属于另一个循环"是一个静默失效。
        #
        # 这把锁罩住 `POST /api/scans` 的"查槽位 → 起进程"整段，判据在 routes/scans.py
        # 的模块 docstring —— 只罩查槽位那一瞬间的话两个并发请求会双双通过。
        app.state.scan_launch_lock = asyncio.Lock()
        # 后台收尾任务的强引用集合。`create_task` 的返回值没人拿着的话事件循环可以把它
        # GC 掉（标准库明写这一点），而它正是唯一会写终态列的地方。
        scan_tasks: set[asyncio.Task[None]] = set()
        app.state.scan_tasks = scan_tasks
        # T28。破坏性操作，**默认关**：天数为 0 时 `run_forever` 自己就立刻返回，所以
        # 这里刻意不写 `if resolved.console_retention_days > 0` —— 开关有两个落点就
        # 一定会有一天不一致。路径一律用 `Settings` 上已有的 property，不自己拼。
        retention_sweeper = RetentionSweeper(
            db,
            resolved.scans_dir,
            retention_days=resolved.console_retention_days,
            audit_dir=resolved.audit_dir,
        )
        app.state.retention_sweeper = retention_sweeper

        # T7a。本项目的第一个后台任务。凭据的过期是懒判定（取用时判）为主，但没人再来
        # 取用的凭据不会被懒判定碰到，而"凭据在内存里待多久"就是 KeyVault 的安全属性
        # 本身 —— 所以必须有人主动去清。见 key_vault 模块 docstring。
        sweeper = asyncio.create_task(key_vault.run_sweeper())
        # T11a。启动的第一轮清扫就在这个任务里 —— 刻意**不在 yield 之前 await 一次**：
        # docker 挂着的时候那次 await 会把"api 起不来"和"docker 有问题"混成一件事。
        reaper_task = asyncio.create_task(reaper.run_forever())
        # T28。同样是 `create_task` 而不是 `await` 一次：第一轮清理要删的可能是几十个
        # 目录，`await` 一次就把"第一轮清理很慢"变成了"api 起不来"。
        retention_task = asyncio.create_task(retention_sweeper.run_forever())

        logger.info("启动完成")
        try:
            yield
        finally:
            # 必须 cancel **并 await**：只 cancel 不等的话，事件循环关闭时会打出
            # "Task was destroyed but it is pending"，而那行字会出现在每一次正常停机的
            # 日志里，把真问题埋掉。`run_sweeper` 刻意不吞 CancelledError，所以这里
            # 必须接住它。
            # 先让子进程退干净（它们的 tmpfs 清理挂在各自的监控任务上），再撤后台任务
            # 与 DB 连接 —— 反过来的话，监控任务收尾时可能撞上一个已经关掉的库。
            await app.state.supervisor.shutdown()
            # **必须在 `db.close()` 之前 drain。** `_run_to_completion` 在 `wait()` 返回
            # 之后才写终态列，而 `shutdown()` 只保证子进程死了、不保证那几个任务跑完 ——
            # 少了这一步，每一次"停机时还有扫描在跑"都会让后台任务在库关掉之后写库
            # （表现是一句 ProgrammingError，以及一行永远卡在 running 的扫描）。
            # `return_exceptions=True`：一个任务的异常不许挡住其它任务的收尾。
            await asyncio.gather(*tuple(scan_tasks), return_exceptions=True)
            # W2c。**排在 gather 之后**：正常结束的扫描由 `_run_to_completion` 的 finally
            # 自己关掉 channel，这里收的是"那个后台任务被 cancel 掉／死得不正常，没走到
            # finally"留下的残留。**也必须排在 `db.close()` 之前**：`finish()` 的最后一次
            # tick 会经 `EventMirror` 写库，库关了就是一句 ProgrammingError（与上面那条
            # drain 同一个理由）。
            await app.state.channels.shutdown()
            # T11b。拉取任务也要在事件循环关掉之前收干净（形状与下面两个后台任务一样：
            # cancel 并 await）。刻意**不等它拉完**：一个 GB 级镜像会让停机卡上几分钟。
            await app.state.image_puller.shutdown()
            sweeper.cancel()
            with suppress(asyncio.CancelledError):
                await sweeper
            # 清扫任务同理（`run_forever` 也不吞 CancelledError）。**刻意不在这里补一次
            # 清扫**：上面 shutdown() 强杀留下的泄漏由下次启动的第一轮兜住，为停机路径
            # 再加一次带超时的 docker 往返不值得（见 reaper 模块 docstring）。
            reaper_task.cancel()
            with suppress(asyncio.CancelledError):
                await reaper_task
            # T28。留存清理同理，且**必须排在 `db.close()` 之前** —— 它的每一轮都会
            # `await db.run(...)`（选行、删明细行、写审计）。
            retention_task.cancel()
            with suppress(asyncio.CancelledError):
                await retention_task
            db.close()
            logger.info("已停止")

    app = FastAPI(
        title="Strix Web 控制台 API",
        version=__version__,
        lifespan=lifespan,
        # 关掉自带的交互文档：它们是未鉴权的、会把全部接口形状暴露出去。
        # 本项目只经 nginx 绑 127.0.0.1:443 提供服务，文档没有受众。
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        # 全局鉴权（T4b）。**默认拒绝**：新增的路由自动受保护，忘了加装饰器不会开洞。
        # 反过来（每条路由自己加 Depends）会让"漏了一条"变成一个静默的后门。
        # 免鉴权名单在 routes/auth.EXEMPT_PATHS，精确匹配 path。
        #
        # 必须是 app 级依赖而不是中间件：`BaseHTTPMiddleware` 看不见 WebSocket scope
        # （本文件的 trace-id 中间件就是活证据 —— WS 下它完全不跑），写成中间件等于给
        # `/ws/...` 开一条无鉴权后门。app 级依赖覆盖 `@router.websocket`，已实测。
        dependencies=[Depends(auth_routes.require_session)],
    )

    _install_trace_id_middleware(app)
    _install_exception_handlers(app)
    app.include_router(auth_routes.router)
    # `/api/health`（免鉴权，零依赖查询）与 `/api/system/status`（鉴权后，查全部依赖）。
    # 两者的界线写在 `routes/health.py` 的模块 docstring 里 —— 把依赖检查搬进 health
    # 会让 docker 抖一下就重启 api，而重启 api 会清空内存 KeyVault。
    app.include_router(health_routes.router)
    app.include_router(system_routes.router)
    # T11b。`/ws/system`（镜像拉取进度）刻意**不在** `/api/system` 前缀下 —— 见
    # `routes/system.py` 里 `ws_router` 的注释。它同样受全局鉴权保护（app 级依赖能
    # 覆盖 websocket 路由，中间件不能，这就是上面那个 `dependencies=` 的理由）。
    app.include_router(system_routes.ws_router)
    # T8。两者都在全局鉴权之后（**没有**往 EXEMPT_PATHS 加任何路径）：
    # `/api/targets/validate` 会对任意主机名发 DNS 查询，而这个进程在 `strix_sandbox`
    # 网络里能解析内网名字；`/api/allowlist` 写的就是"谁批准扫什么"。
    app.include_router(allowlist_routes.router)
    app.include_router(targets_routes.router)
    # T7b。`/api/providers` 是目录（无凭据），`/api/keys` 收凭据 —— 两者都在全局鉴权
    # 之后：一个未鉴权的 `/api/keys` 等于让任意网页替用户登记（并验活）凭据。
    app.include_router(providers_routes.router)
    app.include_router(keys_routes.router)
    # T9。`/api/scan-templates` 是纯目录（进程内常量、无凭据、无 IO），但同样在全局鉴权
    # 之后：内容全是机器码不等于可以对外裸露 —— 它连带告诉未鉴权的人"这里是 Strix 控制台"。
    app.include_router(templates_routes.router)
    # T12c。`/api/scans` 也在全局鉴权之后，**而且它是这里面最需要鉴权的一个**：一个未鉴权
    # 的 POST 等于让任意网页拿着别人登记的 vault_handle 花别人的钱去打任意（已在清单里的）
    # 目标。挡住"任意网页跨域打它"的是「没有 CORS」+「只把 application/json 喂给 Pydantic」，
    # 所以这条路由的安全性也依赖于**永远不装 CORSMiddleware**（CLAUDE.md §安全不变式）。
    app.include_router(scans_routes.router)
    # T25。`/api/audit.csv` 同样在全局鉴权之后：审计流水里写着谁在什么时候批准扫了什么，
    # 未鉴权导出等于把内网资产清单（主机名 + 解析出来的地址 + 授权人 + 工单号）交出去。
    # 它是只读的，也**不写**自己的审计事件（理由在 `routes/audit.py` 的模块 docstring）。
    app.include_router(audit_routes.router)

    return app


# uvicorn 的入口：`uvicorn app.main:app`。
#
# 这里刻意**不做**任何工作 —— 全部副作用都在 lifespan 里。模块级做初始化会让
# `import app.main` 本身产生副作用，测试里就没法只 import 不启动。
# 环境不合法时的失败点是 lifespan（uvicorn 会打出错误并退出），不是 import。
app = create_app()
