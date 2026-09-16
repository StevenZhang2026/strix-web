"""`POST /api/scans`（起扫描）与 `POST /api/scans/{id}/stop`（停扫描）。

这是把 T6–T12b 那一串零件真正接上线的地方：护栏判定、白名单、DNS 重解析、准入、
argv 构造、凭据登记、并发闸、落库、审计。**本文件自己不做任何判定** —— 判定都在
`services/` 里的纯函数或服务里，这里只负责顺序、事务边界与错误映射。

# 一把锁罩住"查槽位 → 起进程"整段，不是只罩并发闸

第 3 步（查槽位）到第 11 步（起进程）之间有五个 `await`（DNS、`to_thread`、DB、start）。
只在查槽位那一瞬间持锁的话，两个并发请求会**双双通过** —— 于是槽位数 1 变成 2。
同一把锁顺带把"重解析 DNS → 起进程"这个 TOCTOU 窗口也焊死了（解析完之后 DNS 记录
可以再变，但那已经不是本进程能观察到的事）。

槽位数**仍然取自 `settings.console_max_concurrent_scans`**：锁只保证判断是原子的，
不代表"结构性单例"。把它写死成 1 会让部署机加内存之后没有任何办法提高并发。

# 起扫描的顺序（编号即代码顺序）

     0  Pydantic 校验                                  → 422 invalid_request
     1  生成 scan_id / authorization_id
     2  进锁
     3  并发闸                                         → 409 concurrency_limit
     4  key_vault.acquire()                            → 409 key_required
     5  scan_secrets.register()   ← **在任何可能打日志的事情之前**
     6  多目标"以上 N 个均已授权"                      → 422
     7  预解析（逐目标 normalize + 主机名去重 + gather）
     8  evaluate_admission()                           → 拒绝时先写审计再抛
     9  build_launch_plan()（to_thread：它写文件）      → 422 / 409
    10  一个事务：authorizations + scans + audit_log
    11  supervisor.start()                             → OSError 时把行标 failed 再重抛
    12  create_task(_run_to_completion)
    13  scan.launched 审计（锁外）
    14  202

第 5 步的位置是安全语义：登记测试账号口令**必须**早于任何可能把它打进日志的代码，
否则脱敏集合里还没有它。配对的 `forget` 由 `launched` 标志 + `finally` 保证 ——
起不起来都不会把口令留在进程里。

# 停止端点不 await 那 20 秒宽限

`ScanProcess.stop()` 里有 SIGTERM → 宽限 → SIGKILL。在请求里等它会顶到 nginx 的
`proxy_read_timeout`，更要紧的是"再点一次立即强杀"要求第一次的宽限还在睡时第二个请求
能进来。所以 stop 只登记一个后台任务并立刻 202。**终态列由 `_run_to_completion` 写**，
停止端点一列都不碰 —— 两个写入点会互相覆盖。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import Field, SecretStr, field_validator

from app.db import Database
from app.errors import (
    ConcurrencyLimitError,
    InvalidRequestError,
    KeyRequiredError,
    NotFoundError,
    assert_scan_failure_code,
    error_for_code,
)
from app.models import BoundaryModel
from app.routes._context import actor, client_ip
from app.services import audit
from app.services.allowlist import AllowlistEntry, AllowlistSnapshot, AllowlistStore
from app.services.dns_resolver import Resolution, Resolver
from app.services.key_vault import CredentialSet, KeyVault
from app.services.scan_admission import (
    AdmissionRejected,
    AdmissionRequest,
    AdmittedTarget,
    audit_for,
    evaluate_admission,
)
from app.services.scan_launcher import LaunchPlan, LaunchSpec, TestCredential, build_launch_plan
from app.services.scan_secrets import ScanSecretRegistry
from app.services.scan_supervisor import ScanProcess, ScanSupervisor
from app.services.scan_templates import template_for
from app.services.target_guard import OperatorOptIn, TargetRejected, normalize_target
from app.settings import Settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scans", tags=["scans"])

# 与 `routes/targets.py` 同一个上限。一次向导最多 20 个目标：`AdmissionRequest` 是
# 逐目标做 DNS 的，而 20 个已经远超"一次授权覆盖的范围"的常见形状。
_MAX_TARGETS = 20

_AUTHORIZATION_COLUMNS = (
    "id, created_at, operator_name, authorization_ref, targets_json, resolved_ips_json, "
    "typed_confirmation, affirmations_json, overrides_json, allowlist_entry_id, "
    "allowlist_snapshot, user_agent, client_ip"
)
_SCAN_COLUMNS = (
    "id, created_at, status, authorization_id, template_id, targets_json, scan_mode, "
    "max_budget_usd, max_turns, reasoning_effort, provider, auth_shape, strix_llm, api_base, "
    "vault_handle, cwd, argv_json, env_var_names_json, instruction_sha256, strix_version, "
    "sandbox_image"
)


# =============================================================================
# 边界模型
# =============================================================================
AffirmationId = Literal[
    "owns_or_authorized",
    "not_third_party_production",
    "understands_real_attacks",
]
"""三条声明的 id。**收 id 串而不是三个 bool**：少勾一个就让数组变短，Pydantic 自己就
拒了（422），路由里不需要再写一遍 `all(...)`。文案在 `zh-CN.json`，后端只认这三个键。"""


class ScanOverridesInput(BoundaryModel):
    """操作者的两个显式勾选。缺省一律 false —— 护栏的默认是拒绝。"""

    allow_loopback: bool = False
    allow_private: bool = False


class ScanCredentialInput(BoundaryModel):
    """一组测试账号。`password` 是 `SecretStr`，所以 Pydantic 的校验消息不会回显它。

    `min_length=8` 挡在这里而不是 `ScanSecretRegistry` 里：太短的口令会让脱敏变成一台
    绞肉机（把 `"a"` 加进脱敏集合，此后每一行日志都成了星号）。判据整段在 T12b 的
    `scan_secrets` docstring 里，它明写"请求体该不该拒短口令是请求模型的事"。
    """

    role: str = Field(min_length=1, max_length=64)
    username: str = Field(min_length=1, max_length=200)
    password: SecretStr = Field(min_length=8, max_length=200)


class ScanAuthorizationInput(BoundaryModel):
    """授权声明。**这一整块就是 `authorizations` 那一行的来源。**"""

    operator_name: str = Field(min_length=1)
    authorization_ref: str = Field(min_length=1)
    typed_confirmation: str = Field(min_length=1)
    affirmed: list[AffirmationId] = Field(min_length=3, max_length=3)
    multi_target_affirmed: bool = False
    """多目标时的"以上 N 个均已授权"。

    挤进 `overrides_json` 而不是加一列、也不动 `affirmations_json` 的
    `CHECK json_array_length = 3`：那条 CHECK 是拍过板的，改它要写迁移。
    **后端强制**（不是只由前端提示）—— "声明了却没有任何一处强制"是本项目已经栽过三次
    的形状。
    """

    resolved_ips_seen: dict[str, list[str]]
    """host → 声明授权时看到的地址，前端从 `POST /api/targets/validate` 的响应里带回来。
    它是 rebinding 比对的"声明侧"，比对本身在 `scan_admission`（按集合比）。"""

    @field_validator("affirmed")
    @classmethod
    def _must_be_distinct(cls, value: list[AffirmationId]) -> list[AffirmationId]:
        """三个 id 互不相同。

        没有这一条的话 `["owns_or_authorized"] * 3` 就能过 —— 长度是 3，但操作者只声明了
        一件事。那正是这个字段要防的。
        """
        if len(set(value)) != len(value):
            raise ValueError("三条声明必须互不相同")
        return value


class CreateScanRequest(BoundaryModel):
    """`POST /api/scans` 的请求体。

    刻意**没有** `spec_upload_id`（上传端点还不存在）。`extra="forbid"` 会让前端提前
    发现自己在传一个不存在的字段，而不是让它被静默忽略。
    """

    vault_handle: str
    template_id: str
    targets: list[str] = Field(min_length=1, max_length=_MAX_TARGETS)
    """**不排序、不去重、不重排。** 逐字确认串的期望值是 `targets[0]` 规范化后的 host，
    重排会静默改变语义。`min_length=1` 是硬要求：空表会让 `evaluate_admission` 抛
    `ValueError`（那是它对"调用方少做了一步"的表达），于是 422 变成 500。"""

    overrides: ScanOverridesInput = ScanOverridesInput()
    scan_mode: str | None = None
    """`None` = 用模板的默认。刻意不在这里给字面默认值：模板改了默认，请求体不用跟着改。"""

    max_budget_usd: float = Field(gt=0)
    """**必填**。`--max-budget-usd` 缺了 `ScanLauncher` 就拒绝构造 argv（CLAUDE.md
    §安全不变式），所以这里不给默认值 —— 一个默认预算等于替用户决定花多少钱。"""

    max_turns: int = Field(ge=1)
    reasoning_effort: str | None = None
    extra_instruction: str | None = None
    credentials: list[ScanCredentialInput] = Field(default_factory=list)
    authorization: ScanAuthorizationInput


class ScanAcceptedResponse(BoundaryModel):
    """`202`。**`argv_preview` 里没有凭据**（那就是它叫 preview 的原因）。"""

    scan_id: str
    status: str
    ws: str
    argv_preview: list[str]
    budget_usd: float


class StopScanRequest(BoundaryModel):
    mode: Literal["graceful", "force"]
    """垃圾值 → 422。刻意用 `Literal` 而不是 str + 手写校验：多一个取值就多一种
    "被当成 graceful 处理"的可能。"""


class StopScanAcceptedResponse(BoundaryModel):
    scan_id: str
    mode: str


# =============================================================================
# 依赖取用 —— 形状与 `routes/keys.py` / `routes/targets.py` 一致
# =============================================================================
def _settings(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    if not isinstance(settings, Settings):
        raise RuntimeError(
            "app.state.settings 不存在。本接口要求 lifespan 已经跑过 —— "
            "测试里请用 `with TestClient(app):`。"
        )
    return settings


def _db(request: Request) -> Database:
    db = getattr(request.app.state, "db", None)
    if not isinstance(db, Database):
        raise RuntimeError(
            "app.state.db 不存在。本接口要求 lifespan 已经跑过 —— "
            "测试里请用 `with TestClient(app):`。"
        )
    return db


def _vault(request: Request) -> KeyVault:
    vault = getattr(request.app.state, "key_vault", None)
    if not isinstance(vault, KeyVault):
        raise RuntimeError(
            "app.state.key_vault 不存在。本接口要求 lifespan 已经跑过 —— "
            "测试里请用 `with TestClient(app):`。"
        )
    return vault


def _scan_secrets(request: Request) -> ScanSecretRegistry:
    registry = getattr(request.app.state, "scan_secrets", None)
    if not isinstance(registry, ScanSecretRegistry):
        raise RuntimeError(
            "app.state.scan_secrets 不存在。本接口要求 lifespan 已经跑过 —— "
            "测试里请用 `with TestClient(app):`。"
        )
    return registry


def _store(request: Request) -> AllowlistStore:
    store = getattr(request.app.state, "allowlist", None)
    if not isinstance(store, AllowlistStore):
        raise RuntimeError(
            "app.state.allowlist 不存在。本接口要求 lifespan 已经跑过 —— "
            "测试里请用 `with TestClient(app):`。"
        )
    return store


def _supervisor(request: Request) -> ScanSupervisor:
    """**这就是单测的注入点。**

    刻意不做 `isinstance`：真的起子进程就等于让单测碰真实 Docker（CLAUDE.md §测试），
    所以测试里挂的是一个鸭子类型的替身。理由与 `routes/targets.py::_resolver` 一致。
    """
    supervisor: ScanSupervisor = request.app.state.supervisor
    return supervisor


def _resolver(request: Request) -> Resolver:
    resolver: Resolver = request.app.state.dns_resolver
    return resolver


def _scan_tasks(request: Request) -> set[asyncio.Task[None]]:
    """在跑的后台任务集合。**必须有人拿着强引用**，否则事件循环会把它们 GC 掉；
    停机时 `main.py` 还要 gather 它们（不然会在 `db.close()` 之后写库）。"""
    tasks: set[asyncio.Task[None]] = request.app.state.scan_tasks
    return tasks


async def _audit(
    request: Request,
    *,
    event: str,
    detail: dict[str, audit.AuditDetailValue],
    scan_id: str | None = None,
    authorization_id: str | None = None,
) -> None:
    """写一条审计（自带事务）。本模块特有的是那两个 id —— 别的路由没有。"""
    await audit.record(
        db=_db(request),
        audit_dir=_settings(request).audit_dir,
        entry=audit.AuditEntry(
            event=event,
            actor=actor(request),
            detail=detail,
            client_ip=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            scan_id=scan_id,
            authorization_id=authorization_id,
        ),
    )


# =============================================================================
# 起扫描
# =============================================================================
@router.post("", status_code=202, response_model=ScanAcceptedResponse)
async def create_scan(request: Request, payload: CreateScanRequest) -> ScanAcceptedResponse:
    """起一次扫描。顺序见模块 docstring，编号即代码顺序。"""
    settings = _settings(request)
    supervisor = _supervisor(request)
    scan_secrets = _scan_secrets(request)
    vault = _vault(request)
    db = _db(request)

    scan_id = uuid.uuid4().hex
    authorization_id = uuid.uuid4().hex

    lock: asyncio.Lock = request.app.state.scan_launch_lock
    async with lock:
        active = supervisor.active_scan_ids()
        limit = settings.console_max_concurrent_scans
        if len(active) >= limit:
            raise ConcurrencyLimitError(limit=limit, active=len(active))

        credentials = vault.acquire(payload.vault_handle)
        if credentials is None:
            # 409 是诚实的：`api` 重启后内存 vault 就空了，那正是"Key 绝不落盘"的后果。
            raise KeyRequiredError()

        launched = False
        # `try` 从 `acquire()` **成功之后的第一行**开始，`register` 也在里面：这样
        # "acquire 之后无论哪一行抛，引用都会被 release" 是结构上的事，而不是靠
        # "register 与 TestCredential 不会抛"这个推理（它们此刻确实不会，但那不是不变式）。
        # 漏一次 release 的后果是这组凭据 24 小时（hard TTL）内都不会过期。
        try:
            test_credentials = tuple(
                TestCredential(
                    role=item.role,
                    username=item.username,
                    password=item.password.get_secret_value(),
                )
                for item in payload.credentials
            )
            # **在任何可能打日志的事情之前**登记，否则脱敏集合里还没有这些口令。
            scan_secrets.register(scan_id, test_credentials)

            if len(payload.targets) > 1 and not payload.authorization.multi_target_affirmed:
                raise InvalidRequestError(field="authorization.multi_target_affirmed")

            template = template_for(payload.template_id)
            if template is None:
                raise InvalidRequestError(field="template_id")
            scan_mode = template.scan_mode if payload.scan_mode is None else payload.scan_mode

            resolutions = await _resolve_hosts(payload.targets, _resolver(request))
            snapshot = await asyncio.to_thread(_store(request).current)

            outcome = evaluate_admission(
                AdmissionRequest(
                    raw_targets=tuple(payload.targets),
                    typed_confirmation=payload.authorization.typed_confirmation,
                    opt_in=OperatorOptIn(
                        loopback=payload.overrides.allow_loopback,
                        private=payload.overrides.allow_private,
                    ),
                    declared_ips={
                        host: tuple(addresses)
                        for host, addresses in payload.authorization.resolved_ips_seen.items()
                    },
                    resolutions=resolutions,
                    allowlist=snapshot,
                    # 一批目标共用同一个"今天"：跨午夜时逐目标各取一次会让同一次请求里
                    # 前几个目标按今天判、后几个按明天判（清单条目可以有 expires）。
                    today=datetime.now(UTC).date(),
                )
            )
            if isinstance(outcome, AdmissionRejected):
                event, detail = audit_for(outcome)
                await _audit(request, event=event, detail=detail)
                # 码 → 错误类由 `errors.error_for_code` 查表，**路由里不抄映射表**：
                # T6/T8 以后新增护栏码不用回来改这里。
                raise error_for_code(outcome.code)(**dict(outcome.params))

            spec = LaunchSpec(
                scan_id=scan_id,
                template_id=payload.template_id,
                # 用规范化后的 `url` 而不是用户原文：交给 Strix `-t` 的就是这一串。
                targets=tuple(admitted.target.url for admitted in outcome.targets),
                scan_mode=scan_mode,
                max_budget_usd=payload.max_budget_usd,
                max_turns=payload.max_turns,
                reasoning_effort=payload.reasoning_effort,
                extra_instruction=payload.extra_instruction,
                test_credentials=test_credentials,
            )
            # `build_launch_plan` 是同步的**而且写文件**（建工作区、写 instruction）。
            plan = await asyncio.to_thread(
                build_launch_plan, settings, spec, credentials, environ=os.environ
            )

            prepared = audit.prepare(
                audit.AuditEntry(
                    event=audit.EVENT_AUTHORIZATION_AFFIRMED,
                    actor=actor(request),
                    detail=_affirmed_detail(payload, outcome.targets),
                    client_ip=client_ip(request),
                    user_agent=request.headers.get("user-agent"),
                    scan_id=scan_id,
                    authorization_id=authorization_id,
                )
            )
            authorization_row = _authorization_row(
                authorization_id=authorization_id,
                created_at=prepared.at,
                payload=payload,
                targets=outcome.targets,
                resolutions=resolutions,
                allowlist=snapshot,
                user_agent=request.headers.get("user-agent"),
                client_addr=client_ip(request),
            )
            scan_row = _scan_row(
                scan_id=scan_id,
                created_at=prepared.at,
                authorization_id=authorization_id,
                payload=payload,
                scan_mode=scan_mode,
                target_urls=spec.targets,
                credentials=credentials,
                plan=plan,
                strix_version=str(request.app.state.strix_version),
                sandbox_image=settings.strix_image,
            )

            def write(conn: sqlite3.Connection) -> None:
                """三条 INSERT 在**同一个事务**里（`Database.run` 包了 BEGIN IMMEDIATE）。

                拆成三次 `db.run()` 的话，中间失败会留下一份"授权声明存在但没有扫描"
                或者反过来 —— 而 `scans.authorization_id` 是 NOT NULL，那半个状态在
                结构上本该不可能出现。
                """
                conn.execute(
                    f"INSERT INTO authorizations ({_AUTHORIZATION_COLUMNS}) "  # noqa: S608
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    authorization_row,
                )
                conn.execute(
                    f"INSERT INTO scans ({_SCAN_COLUMNS}) "  # noqa: S608
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    scan_row,
                )
                audit.insert(conn, prepared)

            await db.run(write)
            # ndjson 镜像**在事务提交之后**：里面出现一条被回滚掉的记录，就是往
            # "审计里写的都真的发生过"这条保证上开一个洞。
            await audit.mirror(prepared, audit_dir=settings.audit_dir)

            try:
                process = await supervisor.start(scan_id, plan)
            except OSError:
                await _mark_failed_to_start(db, scan_id)
                raise

            # 交接点：`_run_to_completion` 的 `finally` 从此刻起负责 forget+release，
            # 所以 `launched` 必须在**这里**就置真，而不是等到本 try 的最后一行。中间那几行
            # （建 202 响应、拼审计 detail）若抛，两边都会清一次：release 幂等无害，但
            # `forget` 会把一次**正在跑**的扫描的口令从脱敏集合里拿掉 —— 那是个泄漏面。
            launched = True
            task = asyncio.create_task(
                _run_to_completion(
                    db=db,
                    audit_dir=settings.audit_dir,
                    process=process,
                    scan_secrets=scan_secrets,
                    vault=vault,
                    vault_handle=payload.vault_handle,
                    entry_actor=actor(request),
                    entry_client_ip=client_ip(request),
                    entry_user_agent=request.headers.get("user-agent"),
                )
            )
            tasks = _scan_tasks(request)
            tasks.add(task)
            task.add_done_callback(tasks.discard)

            accepted = ScanAcceptedResponse(
                scan_id=scan_id,
                status="starting",
                ws=f"/ws/scans/{scan_id}",
                argv_preview=list(plan.argv_preview),
                budget_usd=payload.max_budget_usd,
            )
            launched_detail: dict[str, audit.AuditDetailValue] = {
                # **`plan.env` 一个字节都不许进来**，只有变量名。
                "argv": list(plan.argv_preview),
                "env_var_names": list(plan.env_var_names),
                "pid": process.pid,
                "cwd": str(plan.cwd),
                "max_budget_usd": payload.max_budget_usd,
                "template_id": payload.template_id,
            }
        finally:
            # 没起起来就把这一路拿到的东西全还回去。**凭据卫生不许依赖 happy path。**
            if not launched:
                scan_secrets.forget(scan_id)
                vault.release(payload.vault_handle)

    # 锁外、事务外：这一条审计不该延长临界区，也不该让"审计写失败"回滚掉已经起来的扫描。
    await _audit(
        request,
        event=audit.EVENT_SCAN_LAUNCHED,
        detail=launched_detail,
        scan_id=scan_id,
        authorization_id=authorization_id,
    )
    return accepted


@router.post("/{scan_id}/stop", status_code=202, response_model=StopScanAcceptedResponse)
async def stop_scan(
    request: Request, scan_id: str, payload: StopScanRequest
) -> StopScanAcceptedResponse:
    """请求停止。**立刻 202**，理由见模块 docstring。"""
    process = _supervisor(request).get(scan_id)
    if process is None:
        # 未知 id 与"已经结束了"是同一个回答，刻意不区分：区分它要查 DB，而那只会告诉
        # 探测者哪些 id 存在过。
        raise NotFoundError()

    # 先写审计再动手：审的是**操作者的意图**，即使随后强杀失败也已经发生过。
    await _audit(
        request,
        event=audit.EVENT_SCAN_STOPPED,
        detail={"mode": payload.mode},
        scan_id=scan_id,
    )

    task = asyncio.create_task(process.stop(force=payload.mode == "force"))
    tasks = _scan_tasks(request)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return StopScanAcceptedResponse(scan_id=scan_id, mode=payload.mode)


# =============================================================================
# 后台任务：把子进程的结局落库
# =============================================================================
async def _run_to_completion(
    *,
    db: Database,
    audit_dir: Path,
    process: ScanProcess,
    scan_secrets: ScanSecretRegistry,
    vault: KeyVault,
    vault_handle: str,
    entry_actor: str | None,
    entry_client_ip: str | None,
    entry_user_agent: str | None,
) -> None:
    """`running` → 等结果 → 终态 → 审计 → 放掉凭据。

    异常**不重抛**：这是一个 detached task，重抛只会在 GC 时打一句
    "Task exception was never retrieved"（连 traceback 都可能丢），比这里 log 一次更差。
    代价是那一行可能卡在 `running` —— 由启动时的 `_reconcile_interrupted_scans` 兜住。
    """
    scan_id = process.scan_id
    try:
        started_at = audit.iso_utc(datetime.now(UTC))

        def mark_running(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE scans SET status = 'running', started_at = ?, pid = ? WHERE id = ?",
                (started_at, process.pid, scan_id),
            )

        # `running` 的**唯一**写入点。放在 wait() 之前而不是 start() 成功之后的同一个
        # 事务里：那个事务写的是"这次扫描被受理了"，而"它真的在跑了"是另一件事。
        await db.run(mark_running)

        outcome = await process.wait()

        finished_at = audit.iso_utc(datetime.now(UTC))
        run_dir = None if outcome.run_dir is None else str(outcome.run_dir)

        def mark_finished(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE scans SET status = ?, finished_at = ?, exit_code = ?, exit_meaning = ?, "
                "error_code = ?, error_message = ?, run_dir = ?, strix_run_name = ? WHERE id = ?",
                (
                    outcome.status,
                    finished_at,
                    outcome.exit_code,
                    outcome.exit_meaning,
                    outcome.error_code,
                    outcome.error_message,
                    run_dir,
                    outcome.strix_run_name,
                    scan_id,
                ),
            )

        await db.run(mark_finished)

        await audit.record(
            db=db,
            audit_dir=audit_dir,
            entry=audit.AuditEntry(
                event=audit.EVENT_SCAN_FINISHED,
                actor=entry_actor,
                detail={
                    "status": outcome.status,
                    "exit_code": outcome.exit_code,
                    "exit_meaning": outcome.exit_meaning,
                    "error_code": outcome.error_code,
                    # `run_status` 刻意**没有列**（`scans` 的 DDL 注释：归因的权威是
                    # run.json.status，写进 status 列）。它只活在这里与 error_message。
                    "run_status": outcome.run_status,
                    "strix_run_name": outcome.strix_run_name,
                    "error_message": outcome.error_message,
                },
                client_ip=entry_client_ip,
                user_agent=entry_user_agent,
                scan_id=scan_id,
            ),
        )
    except Exception:
        logger.exception("扫描收尾失败", extra={"scan_id": scan_id})
    finally:
        # **凭据卫生不许依赖 DB 写成功。** 这两句在 finally 里，所以上面任何一步炸了、
        # 或者任务被 cancel 了，口令与凭据引用都还是会被放掉。
        scan_secrets.forget(scan_id)
        vault.release(vault_handle)


async def _mark_failed_to_start(db: Database, scan_id: str) -> None:
    """`supervisor.start()` 抛了 OSError（起不了子进程）。

    刻意不删那一行：它已经带着完整的授权声明与 argv，是"有人试过起这次扫描"的记录。
    `exit_code` 留 NULL —— 根本没有进程退出过，编一个 -1 是撒谎。
    """
    code = assert_scan_failure_code("scan_preparation_failed")
    finished_at = audit.iso_utc(datetime.now(UTC))

    def fail(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE scans SET status = 'failed', error_code = ?, error_message = ?, "
            "finished_at = ? WHERE id = ?",
            (code, "Failed to spawn the strix subprocess.", finished_at, scan_id),
        )

    await db.run(fail)


# =============================================================================
# 私有助手（都是无 IO 的纯函数，除了 `_resolve_hosts`）
# =============================================================================
async def _resolve_hosts(raw_targets: list[str], resolver: Resolver) -> dict[str, Resolution]:
    """把每个**主机名**目标重解析一遍。字面 IP 不进这张表（它没有 DNS 这一步）。

    规范化失败的目标在这里**跳过**而不是报错：`evaluate_admission` 会为它给出带
    `index` 与 `reason` 的机器码，而在这里抢先抛一个就得把那份判定抄第二遍。
    """
    hosts: list[str] = []
    for raw in raw_targets:
        try:
            target = normalize_target(raw)
        except TargetRejected:
            continue
        if not target.is_ip:
            hosts.append(target.host)
    # 去重**保序**：同一个主机名出现两次不该解析两次，而排序会打乱与 `targets` 的对应。
    unique = tuple(dict.fromkeys(hosts))
    outcomes = await asyncio.gather(*(resolver(host) for host in unique))
    return dict(zip(unique, outcomes, strict=True))


def _affirmed_detail(
    payload: CreateScanRequest, targets: tuple[AdmittedTarget, ...]
) -> dict[str, audit.AuditDetailValue]:
    """`authorization.affirmed` 的 detail。

    逐目标记下护栏判出来的类别，加上三个 override 标志 —— 这就是"哪一项勾选被用上了"
    这个问题在放行之后唯一还能诚实回答的形式（`GuardVerdict.required_opt_in` 在
    `allowed` 为真时一定是空的，所以刻意**没有** `override.*_used` 两个事件）。
    """
    return {
        "operator_name": payload.authorization.operator_name,
        "authorization_ref": payload.authorization.authorization_ref,
        "affirmed": list(payload.authorization.affirmed),
        "categories": [
            f"{admitted.target.host}={admitted.verdict.category.value}" for admitted in targets
        ],
        "allow_loopback": payload.overrides.allow_loopback,
        "allow_private": payload.overrides.allow_private,
        "multi_target_affirmed": payload.authorization.multi_target_affirmed,
    }


def _authorization_row(
    *,
    authorization_id: str,
    created_at: str,
    payload: CreateScanRequest,
    targets: tuple[AdmittedTarget, ...],
    resolutions: dict[str, Resolution],
    allowlist: AllowlistSnapshot,
    user_agent: str | None,
    client_addr: str | None,
) -> tuple[object, ...]:
    """`authorizations` 那一行的 13 个值，顺序与 `_AUTHORIZATION_COLUMNS` 一致。

    `allowlist_snapshot` 存**逐目标的判定结论 + 命中条目的完整快照** —— 后半截是
    `001_init.sql:64` 那句 DDL 注释要求的（"当时该条目的完整快照。存快照是因为白名单
    YAML 会被改：事后审计要知道**当时**批准的是什么"）。只存 label 不够：owner、
    `authorization_ref`、`hosts`/`cidrs`、`expires` 全在条目里，而那份文件事后可能已经
    被改过或者那条已经被删了，审计就再也问不出"当时批准的是什么"。

    按 label 反查**不是**把 T8 的匹配逻辑抄第二份：label 由
    `AllowlistConfig._labels_are_unique` 保证唯一，所以这只是一次字典取值 ——
    匹配仍然只在 `decide()` 里发生一次，这里消费它的结论。
    """
    # `config` 为 None = 没有可用清单（文件缺席或坏了），那时 `decide()` 一定给出
    # `matched=False` + `entry_label=None`，所以空表在这里是自洽的、不是兜底。
    entries_by_label: dict[str, AllowlistEntry] = (
        {}
        if allowlist.config is None
        else {entry.label: entry for entry in allowlist.config.entries}
    )
    targets_json = [
        {"raw": admitted.target.raw, "url": admitted.target.url, "host": admitted.target.host}
        for admitted in targets
    ]
    resolved: dict[str, list[str]] = {}
    snapshot: list[dict[str, object]] = []
    for admitted in targets:
        target = admitted.target
        decision = admitted.allowlist
        if target.is_ip:
            # 字面 IP 目标"解析"到它自己 —— 存证要能自证，不能留一个空表。
            resolved[target.host] = [target.host]
        else:
            # 有序那份存证据（`scan_admission` 的注释：比对按集合、存证按有序）。
            resolved[target.host] = list(resolutions[target.host].addresses)
        matched_entry = (
            None if decision.entry_label is None else entries_by_label.get(decision.entry_label)
        )
        snapshot.append(
            {
                "host": target.host,
                "entry_label": decision.entry_label,
                "mode": decision.mode.value,
                "matched": decision.matched,
                "allow_loopback": decision.allow_loopback,
                "allow_private": decision.allow_private,
                "allow_reserved": decision.allow_reserved,
                # 没命中就是 `null`（advisory 下的公网目标、loopback／私网目标都走这条）。
                # 编一条空条目会让审计看起来像"命中了一条什么都没写的授权"。
                "entry": None if matched_entry is None else matched_entry.model_dump(mode="json"),
            }
        )
    # **第一个**目标的 label。`authorizations` 只有一列放它，而逐目标那一份在
    # `allowlist_snapshot` 里 —— 这一列的用途是列表页的一句摘要。
    first_label = targets[0].allowlist.entry_label
    return (
        authorization_id,
        created_at,
        payload.authorization.operator_name,
        payload.authorization.authorization_ref,
        json.dumps(targets_json, ensure_ascii=False, sort_keys=True),
        json.dumps(resolved, ensure_ascii=False, sort_keys=True),
        payload.authorization.typed_confirmation,
        json.dumps(list(payload.authorization.affirmed)),
        json.dumps(
            {
                "allow_loopback": payload.overrides.allow_loopback,
                "allow_private": payload.overrides.allow_private,
                "multi_target_affirmed": payload.authorization.multi_target_affirmed,
            },
            sort_keys=True,
        ),
        first_label,
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
        user_agent,
        client_addr,
    )


def _scan_row(
    *,
    scan_id: str,
    created_at: str,
    authorization_id: str,
    payload: CreateScanRequest,
    scan_mode: str,
    target_urls: tuple[str, ...],
    credentials: CredentialSet,
    plan: LaunchPlan,
    strix_version: str,
    sandbox_image: str,
) -> tuple[object, ...]:
    """`scans` 那一行的 21 个值，顺序与 `_SCAN_COLUMNS` 一致。

    `argv_json` 取的是 **`plan.argv_preview`** 那个名字（它与 `argv` 等值，但让"DB 里
    没有 Key"这件事落在一个被断言的路径上）；`env` 只留**变量名**。
    """
    return (
        scan_id,
        created_at,
        "starting",
        authorization_id,
        payload.template_id,
        # 规范化后的 url，**保序** —— 与 argv 里 `-t` 的那一串是同一批值。
        json.dumps(list(target_urls), ensure_ascii=False),
        scan_mode,
        payload.max_budget_usd,
        payload.max_turns,
        payload.reasoning_effort,
        # 这四列的**唯一**来源是 vault：请求体里没有它们，所以前端改不了。
        credentials.provider,
        credentials.auth_shape,
        credentials.strix_llm,
        credentials.api_base,
        payload.vault_handle,
        str(plan.cwd),
        json.dumps(list(plan.argv_preview)),
        json.dumps(list(plan.env_var_names)),
        plan.instruction_sha256,
        strix_version,
        sandbox_image,
    )
