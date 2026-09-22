"""`POST /api/scans`（起扫描）、`POST /api/scans/{id}/stop`（停扫描）与两个读取端点。

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
from app.services.scan_channel import ChannelRegistry
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

# 列表一次最多回多少行。**不做分页参数**：本机单账号 + 有留存清理，到不了 500 次扫描。
# 真到了那天，判据是实测行数而不是现在猜（同 `routes/audit.py` 的 `_MAX_ROWS`）。
_MAX_SCANS = 500

# 读取端**只点名这些列**。`scans` 还有 vault_handle / cwd / argv_json / … —— 那些是泄漏面
# 或者没有消费者，所以这两个清单就是白名单本身（`tests/test_routes_scans.py` 盯着它）。
_SUMMARY_COLUMNS = (
    "id, created_at, started_at, finished_at, status, template_id, targets_json, scan_mode, "
    "max_budget_usd, cost_usd, count_critical, count_high, count_medium, count_low, "
    "agent_count, event_count, exit_code, exit_meaning, error_code"
)
_DETAIL_EXTRA_COLUMNS = (
    "error_message, max_turns, reasoning_effort, provider, strix_llm, current_epoch, "
    "authorization_id"
)
# `ORDER BY created_at DESC, id DESC`（不是只按 `created_at`）：它是毫秒精度的字符串，同一
# 毫秒内的两条会并列，拿它单独当排序键得到的顺序是不确定的（判据同 `routes/audit.py`）。
_SELECT_SCANS = f"SELECT {_SUMMARY_COLUMNS} FROM scans ORDER BY created_at DESC, id DESC LIMIT ?"  # noqa: S608
_SELECT_SCAN = f"SELECT {_SUMMARY_COLUMNS}, {_DETAIL_EXTRA_COLUMNS} FROM scans WHERE id = ?"  # noqa: S608
_SELECT_SCAN_AGENTS = (
    "SELECT agent_id, name, parent_id, status, created_at, updated_at, error_message "
    "FROM scan_agents WHERE scan_id = ? ORDER BY created_at, agent_id"
)
# `ORDER BY first_seen_at, finding_id` = 当初推 `vuln.add` 帧的顺序。
_SELECT_SCAN_FINDINGS = (
    "SELECT raw_json FROM scan_findings WHERE scan_id = ? ORDER BY first_seen_at, finding_id"
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


class ScanSummary(BoundaryModel):
    """列表页的一行。刻意**没有** `error_message`（可能很长）与 `current_epoch`（用不上）。"""

    id: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    status: str
    template_id: str
    targets: list[str]
    """`targets_json` 解析后的结果 —— 这是整个响应里唯一一处改名。"""

    scan_mode: str
    max_budget_usd: float
    cost_usd: float
    count_critical: int
    count_high: int
    count_medium: int
    count_low: int
    agent_count: int
    event_count: int
    exit_code: int | None
    exit_meaning: str | None
    error_code: str | None


class ScanDetail(ScanSummary):
    """详情页的那一行 = 列表的 19 键 + 这 7 键。"""

    error_message: str | None
    max_turns: int | None
    """可空：`scans.max_turns` 的 DDL 是 `INTEGER CHECK (… IS NULL OR … > 0)`。"""

    reasoning_effort: str | None
    provider: str
    strix_llm: str
    current_epoch: int
    authorization_id: str


class ScanAgentRow(BoundaryModel):
    """**与 `scan_frames._agent_row` 逐字段同形状**（`agent_id` → `id` 是唯一的改名）。

    快照与直播帧形状不一致，前端就要为"从 WS 收到的"和"从 REST 拿到的"各写一套解析。
    """

    id: str
    name: str | None
    parent_id: str | None
    status: str | None
    created_at: str
    updated_at: str
    error_message: str | None


class ScanListResponse(BoundaryModel):
    scans: list[ScanSummary]
    truncated: bool
    """⟺ 真取到了 `_MAX_SCANS` 行。放**正文**而不是响应头：顶层是信封，加字段不破坏契约。"""


class ScanDetailResponse(BoundaryModel):
    scan: ScanDetail
    agents: list[ScanAgentRow]
    findings: list[dict[str, object]]
    """每一项就是 `scan_findings.raw_json` 原样，也就是当初 `vuln.add` 帧里那个
    `payload["vulnerability"]`。穷举漏洞条目的形状等于让本模块认识上游每一个字段
    （判据同 `ws_envelope.Envelope.payload`）。"""


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


def _channels(request: Request) -> ChannelRegistry:
    """**同 `_supervisor`，这也是单测的注入点。**

    刻意不做 `isinstance`：真的 registry 会起轮询任务、经 `EventMirror` 写库，而有些
    用例要断言的正是"它的哪个方法在什么时候被调"，所以测试里挂的是鸭子类型替身。
    """
    registry: ChannelRegistry | None = getattr(request.app.state, "channels", None)
    if registry is None:
        raise RuntimeError(
            "app.state.channels 不存在。本接口要求 lifespan 已经跑过 —— "
            "测试里请用 `with TestClient(app):`。"
        )
    return registry


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
                    channels=_channels(request),
                    # 显式传 cwd 而不是从 `process` 上取：`ScanProcess` 把 plan 存成私有的
                    # `self._plan`，为这一个值去开放一个属性比多传一个参数贵。
                    cwd=plan.cwd,
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
# 读取
# =============================================================================
@router.get("", response_model=ScanListResponse)
async def list_scans(request: Request) -> ScanListResponse:
    """列表页的一次性快照。恒 200 —— 空库回空表，那不是错误。

    只读端点**不写审计**（判据在 `routes/audit.py`：会被反复调用的只读操作不记）。
    """

    def query(conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute(_SELECT_SCANS, (_MAX_SCANS,)).fetchall()

    rows = await _db(request).run(query)
    return ScanListResponse(
        scans=[_summary_of(row) for row in rows], truncated=len(rows) == _MAX_SCANS
    )


@router.get("/{scan_id}", response_model=ScanDetailResponse)
async def get_scan(request: Request, scan_id: str) -> ScanDetailResponse:
    """一次扫描的当前快照 —— **结论（状态／归因／漏洞数）的唯一出处**。

    直播流的 `done` 帧刻意不带结论（`scan_frames.done_spec`），而 WS 只推增量：半途连上来
    的客户端没有别的办法知道"现在是什么样"（agents 树与发现列表不在事件镜像里）。
    """

    def query(
        conn: sqlite3.Connection,
    ) -> tuple[sqlite3.Row | None, list[sqlite3.Row], list[sqlite3.Row]]:
        """三条 SELECT 在**同一个事务**里（`Database.run` 包了 BEGIN IMMEDIATE）。

        拆成三次 `db.run()` 就是三个事务，客户端可能拿到"计数是新的、发现是旧的"这种
        自相矛盾的快照。
        """
        scan = conn.execute(_SELECT_SCAN, (scan_id,)).fetchone()
        if scan is None:
            return None, [], []
        agents = conn.execute(_SELECT_SCAN_AGENTS, (scan_id,)).fetchall()
        findings = conn.execute(_SELECT_SCAN_FINDINGS, (scan_id,)).fetchall()
        return scan, agents, findings

    scan, agents, findings = await _db(request).run(query)
    if scan is None:
        # 不区分"没有过"与"被留存清理了"：`scans` 那一行留存清理不删，所以 404 就是真的没有过。
        raise NotFoundError()
    return ScanDetailResponse(
        scan=_detail_of(scan),
        agents=[_agent_row_of(row) for row in agents],
        # `raw_json` 原样回去。DDL 有 `CHECK (json_valid(raw_json))`，所以**不接**
        # `JSONDecodeError`：坏 JSON 进不了库，真炸了就是库被人手工改坏了，该 500。
        findings=[json.loads(row["raw_json"]) for row in findings],
    )


# =============================================================================
# 后台任务：把子进程的结局落库
# =============================================================================
async def _run_to_completion(
    *,
    db: Database,
    audit_dir: Path,
    process: ScanProcess,
    channels: ChannelRegistry,
    cwd: Path,
    scan_secrets: ScanSecretRegistry,
    vault: KeyVault,
    vault_handle: str,
    entry_actor: str | None,
    entry_client_ip: str | None,
    entry_user_agent: str | None,
) -> None:
    """`running` → 等结果 → 终态 → 审计 → 关 channel → 放掉凭据。

    异常**不重抛**：这是一个 detached task，重抛只会在 GC 时打一句
    "Task exception was never retrieved"（连 traceback 都可能丢），比这里 log 一次更差。
    代价是那一行可能卡在 `running` —— 由启动时的 `_reconcile_interrupted_scans` 兜住。
    """
    scan_id = process.scan_id
    try:
        # 起 channel 是 `try` 的第一件事（比 `mark_running` 还早）：越早开，越早捞到 Strix
        # 写下的第一批产物。"开"与"关"落在同一个函数的 try/finally 两端 —— 中间任何一行
        # 抛异常都仍然会关。
        channels.open(scan_id, cwd)

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
        # **这一行必须排在下面两句之前，这是安全约束不是风格**：`close()` 的最后一次 tick
        # 会把进程退出前写下的最后一批日志行推成帧，而脱敏靠的是此刻**还在册**的凭据值。
        # 先 forget/release 再关 channel，最后那批帧就是没脱敏的。
        await channels.close(scan_id)
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


def _summary_of(row: sqlite3.Row) -> ScanSummary:
    """`scans` 的一行 → 列表那 19 个键。

    **显式点名每个字段**而不是 `ScanSummary(**dict(row))`：`targets_json` → `targets` 这一处
    改名让后者不成立，硬凑出来的那一版反而看不出响应到底有哪些键。
    """
    return ScanSummary(
        id=row["id"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        status=row["status"],
        template_id=row["template_id"],
        targets=json.loads(row["targets_json"]),
        scan_mode=row["scan_mode"],
        max_budget_usd=row["max_budget_usd"],
        cost_usd=row["cost_usd"],
        count_critical=row["count_critical"],
        count_high=row["count_high"],
        count_medium=row["count_medium"],
        count_low=row["count_low"],
        agent_count=row["agent_count"],
        event_count=row["event_count"],
        exit_code=row["exit_code"],
        exit_meaning=row["exit_meaning"],
        error_code=row["error_code"],
    )


def _detail_of(row: sqlite3.Row) -> ScanDetail:
    """同一行 → 详情那 26 个键。前 19 个借 `_summary_of`，**列表与详情不许各写一遍**。"""
    return ScanDetail(
        **_summary_of(row).model_dump(),
        error_message=row["error_message"],
        max_turns=row["max_turns"],
        reasoning_effort=row["reasoning_effort"],
        provider=row["provider"],
        strix_llm=row["strix_llm"],
        current_epoch=row["current_epoch"],
        authorization_id=row["authorization_id"],
    )


def _agent_row_of(row: sqlite3.Row) -> ScanAgentRow:
    """`scan_agents` 的一行 → 与 `agents` 帧同形状的那 7 个键。"""
    return ScanAgentRow(
        id=row["agent_id"],
        name=row["name"],
        parent_id=row["parent_id"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        error_message=row["error_message"],
    )


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
