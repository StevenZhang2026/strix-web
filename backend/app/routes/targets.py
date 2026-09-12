"""`POST /api/targets/validate` —— 向导第 1 步的目标预览（契约在 `PLAN.md:577`）。

# 四件在本文件里被刻意固定下来的事

1. **它永远返回 200。** 每个目标带自己的 `ok`。理由与 `/api/system/status` 那条一样，
   但在这里更强：一批目标里有一个被拒，不该让另外四个的结论一起消失 —— 而 4xx 只有
   一个正文，用户会看到"请求失败"而不是"第 3 行那个域名不在清单里"。真正的错误
   （没登录、请求体形状不对）仍然走统一的 `{code, trace_id}`。

2. **本接口不写审计。** 它是向导里**每次输入都会被调一遍**的只读预览：一次填 5 个目标
   的向导会产出几十条 `target.rejected`，把审计表冲成噪音，而真正需要留痕的那一刻是
   「他按下了开始扫描」。`target.rejected` 属于 T12（`POST /api/scans` 服务端重校验时
   写），那时它对应一个真实的、被拒绝的**意图**。

3. **正文里没有任何中文。** 全是机器码，前端在 `targetGuard.*` / `errors.*` 下查文案
   （CLAUDE.md §错误与文案）。`raw` 是用户自己刚打进来的字符串，原样回显。

4. **它在全局鉴权之后。** 本文件**没有**、也不许有任何往 `auth.EXEMPT_PATHS` 里加路径
   的代码。它会对任意主机名发 DNS 查询，未鉴权就等于把控制台变成一个开放的 DNS
   探测器（而这个进程在 `strix_sandbox` 网络里，能解析内网名字）。

# DNS 解析在这里，不在护栏里

`evaluate_target` 零 IO，所以"把主机名变成地址"是本层的活。解析器从
`app.state.dns_resolver` 取 —— 单测禁止碰真实网络（CLAUDE.md §测试），所以那是注入点，
形状与 `app.state.docker_transport` 完全一致。

同一批里重复的主机名只解析一次（`dict.fromkeys` 去重）。这不只是省事：两次解析同一个
名字可能拿到不同的轮换结果，那会让同一份请求里的两行显示不同的地址，看起来像 bug。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Request
from pydantic import Field

from app.models import BoundaryModel
from app.services.allowlist import AllowlistStore, decide
from app.services.dns_resolver import Resolution, Resolver
from app.services.target_guard import (
    GuardVerdict,
    NormalizedTarget,
    OperatorOptIn,
    TargetRejected,
    evaluate_target,
    normalize_target,
)

router = APIRouter(prefix="/api/targets", tags=["targets"])

# 一次请求最多校验多少个目标。
#
# 两个理由，都不是"防恶意"（调用方已经过鉴权）：
#   · 每个待解析的主机名会占用一个 `asyncio.to_thread` 的线程池线程，而
#     `getaddrinfo` 没有超时参数（见 `dns_resolver` 的模块 docstring）。默认线程池是
#     `min(32, cpu+4)` 个线程，20 个并发解析不会把它占满到影响其它请求。
#   · 向导的目标列表是人手输入的。一次 20 个已经远超真实用法。
_MAX_TARGETS = 20

# 单个目标字符串的长度上限。主机名上限是 253，剩下的留给 scheme、端口和路径。
_MAX_RAW_LENGTH = 2048

_KIND_IP = "ip"
_KIND_HOSTNAME = "hostname"


# =============================================================================
# 边界模型
# =============================================================================
class TargetOverrides(BoundaryModel):
    """操作者在向导里勾的两个复选框（`PLAN.md:577` 的 `overrides`）。

    刻意**只有这两项**：`allow_reserved` 在界面上永不提供（`PLAN.md` §护栏 那张表写的是
    「只能从白名单文件放行」），所以它不在这里 —— 加进来就等于给它开了一个 UI 入口。
    """

    allow_loopback: bool = False
    allow_private: bool = False


class ValidateTargetsRequest(BoundaryModel):
    raw: tuple[Annotated[str, Field(max_length=_MAX_RAW_LENGTH)], ...] = Field(
        min_length=1, max_length=_MAX_TARGETS
    )
    """未经处理的用户输入，逐条。响应里的顺序与它**一一对应**。

    单条超过 `_MAX_RAW_LENGTH` 会让**整批** 422（`invalid_request`），而不是变成那一行
    自己的 `ok=false`。这是刻意的：2048 字符的"目标"不是人打出来的，它是一次粘贴事故
    或客户端 bug，也就是请求体真的不合法。给它编一个护栏拒绝原因等于为一个不存在的
    用户行为造一条文案。
    """

    overrides: TargetOverrides = Field(default_factory=TargetOverrides)


class NormalizedTargetView(BoundaryModel):
    """`target_guard.NormalizedTarget` 的对外形状。

    `host` 与 `host_unicode` 必须**都**给：前者是实际会被连接的名字，后者是用户看到的
    那一串。`punycode_applied` 为真时两者不同，而"你看到的字符和实际连接的主机不是一回事"
    正是验收 3 要求高亮的东西。
    """

    url: str
    scheme: str
    host: str
    host_unicode: str
    port: int | None
    path: str
    is_ip: bool
    punycode_applied: bool


class ResolvedAddressView(BoundaryModel):
    """一个解析出的地址。字段名对齐 `target_guard.ClassifiedAddress`，
    只把 `category` 改叫 `ip_class` —— 与外层那个汇总字段同名，读起来是同一个概念。
    """

    address: str
    version: int
    ip_class: str
    rule: str
    embedded_ipv4: str | None


class TargetValidation(BoundaryModel):
    """一个目标的完整结论。契约字段（`PLAN.md:577-580`）全在，另有四个补充字段。

    ⚠️ **刻意没有 `registrable_domain`。** 2026-09-11 拍板去掉，理由记在 `PLAN.md` 的
    T8 行 —— 正确实现它需要 Public Suffix List（一个新依赖 + 一份要跟着更新的数据），
    近似实现（"取最后两段"）在 `example.co.uk` 上就是错的，而一个**偶尔说谎**的授权
    范围提示比没有这个提示更危险。别当成漏了的字段补上。

    三个"为什么是可空的"：
      · `normalized` / `kind` / `ip_class` / `requirement` 在规范化就失败时为 `None`
        （那时还没有一个可谈的目标）。
      · `code` 只在"不放行且调用方就此拒绝时该报哪个 HTTP 错误"时非空；缺勾选不算错误
        （见 `GuardVerdict.error_code`）。
      · `reason` / `resolution_error` 互斥且都属于 `targetGuard.*` 那棵树。
    """

    raw: str
    """用户输入的原文。

    规范化成功时是去掉首尾空白的那一串（`NormalizedTarget.raw`），规范化失败时是**一字
    不动的原文**（`TargetRejected.raw`）—— 后者刻意如此：一条因为夹了不可见字符而被拒的
    输入，把它"整理干净"再回显就等于把用户唯一的线索擦掉了。

    契约里没有这个字段，是本文件补的：前端要用它给列表行做 key，而"按下标对齐"会在任何
    一次过滤/排序重构里静默错位。回显未校验输入在这里是安全的 —— 它进的是 JSON 字符串，
    不参与任何拼接，且接收者就是刚刚打出它的那个人。
    """

    ok: bool
    """现在就能扫吗。= `GuardVerdict.allowed`，规范化失败或解析失败时恒 `False`。"""

    normalized: NormalizedTargetView | None
    kind: str | None
    """`ip` = 字面 IP（没有 DNS 这一步），`hostname` = 名字。

    契约字段。刻意**不给它文案**：界面上要显示的是 `normalized.host` 与地址列表本身，
    "这是个 IP 还是个域名"用户看一眼就知道，一句「字面 IP 地址」是纯噪音。
    """

    resolved_ips: tuple[ResolvedAddressView, ...] = ()
    ip_class: str | None
    """汇总类别（`TargetCategory`）。可能是 `mixed`。"""

    allowlist_entry: str | None
    """命中的条目 label。`null` = 没命中（或没有清单）。"""

    requirement: str | None
    """放行**条件**（`GuardRequirement`）。与 `ok` 正交 —— 已经满足了条件的目标
    `ok=true` 而 `requirement` 仍然是 `allowlist_entry`，因为那说的是"它为什么能过"。

    契约里没有它。补它的理由：没有它，前端只能从 `code` 反推该给用户什么操作提示，
    而"缺勾选"这一种根本没有 `code`（见 `GuardVerdict.error_code`）。
    """

    overridable: bool = False
    """这个类别**存在**从界面放行的途径吗。`metadata` 永远是 `false`（验收 3 点名）。"""

    required_opt_in: tuple[str, ...] = ()
    """还缺哪几项勾选（`OptInFlag`）。契约里没有；`overrides` 有两项而汇总类别只有一个，
    "还缺哪个"没法从 `ip_class` 推出来（`localhost` 可以同时是环回和内网）。
    """

    code: str | None
    """`app/errors.py` 的机器码。前端在 `errors.*` 下查文案。"""

    reason: str | None
    """规范化被拒的原因（`RejectionReason`）。前端在 `targetGuard.reasons.*` 下查。"""

    resolution_error: str | None
    """DNS 解析失败（`ResolutionError`）。前端在 `targetGuard.resolution.*` 下查。

    契约里没有这一项，但没有它就只能把 NXDOMAIN 报成 500 或者假装它是别的什么错。
    """

    note_code: str | None
    """需要当场解释的那句话的机器码。目前只有 `loopback_rewrite`（验收 3）。"""


class ValidateTargetsResponse(BoundaryModel):
    """信封。刻意只有一个字段。

    为什么不裸返回数组：JSON 顶层是数组的响应没法在不破坏契约的前提下加字段
    （T18 的向导可能要一个"清单当前是 enforce 还是 advisory"的提示）。
    """

    targets: tuple[TargetValidation, ...]


# =============================================================================
# 依赖取用 —— 形状与 `routes/system.py::_settings` / `_probe` 对齐
# =============================================================================
def _store(request: Request) -> AllowlistStore:
    store = getattr(request.app.state, "allowlist", None)
    if not isinstance(store, AllowlistStore):
        raise RuntimeError(
            "app.state.allowlist 不存在。本接口要求 lifespan 已经跑过 —— "
            "测试里请用 `with TestClient(app):`。"
        )
    return store


def _resolver(request: Request) -> Resolver:
    """DNS 解析器。**这就是单测的注入点**（CLAUDE.md §测试 禁止单测碰真实网络）。

    不做 `isinstance` 检查：`Resolver` 是一个 `Callable` 别名，`isinstance` 对它只能
    查"是不是可调用"，拦不住任何真实错误。缺失时的 `None` 会在第一次调用处以
    `TypeError` → 500 暴露。理由与 `routes/system.py::_probe` 完全一致。
    """
    resolver: Resolver = request.app.state.dns_resolver
    return resolver


# =============================================================================
# 结论组装 —— 纯函数
# =============================================================================
def _address_views(verdict: GuardVerdict) -> tuple[ResolvedAddressView, ...]:
    return tuple(
        ResolvedAddressView(
            address=item.address,
            version=item.version,
            ip_class=item.category.value,
            rule=item.rule,
            embedded_ipv4=item.embedded_ipv4,
        )
        for item in verdict.addresses
    )


def _kind(target: NormalizedTarget) -> str:
    return _KIND_IP if target.is_ip else _KIND_HOSTNAME


def _from_verdict(verdict: GuardVerdict, *, allowlist_entry: str | None) -> TargetValidation:
    target = verdict.target
    return TargetValidation(
        raw=target.raw,
        ok=verdict.allowed,
        normalized=NormalizedTargetView(
            url=target.url,
            scheme=target.scheme,
            host=target.host,
            host_unicode=target.host_unicode,
            port=target.port,
            path=target.path,
            is_ip=target.is_ip,
            punycode_applied=target.punycode_applied,
        ),
        kind=_kind(target),
        resolved_ips=_address_views(verdict),
        ip_class=verdict.category.value,
        allowlist_entry=allowlist_entry,
        requirement=verdict.requirement.value,
        overridable=verdict.overridable,
        required_opt_in=tuple(flag.value for flag in verdict.required_opt_in),
        code=verdict.error_code,
        reason=None,
        resolution_error=None,
        note_code=verdict.note_code,
    )


def _from_rejection(exc: TargetRejected) -> TargetValidation:
    """规范化失败。`ok=false` + `reason`，**不是** 4xx（见模块 docstring 第 1 条）。"""
    return TargetValidation(
        raw=exc.raw,
        ok=False,
        normalized=None,
        kind=None,
        ip_class=None,
        allowlist_entry=None,
        requirement=None,
        code=None,
        reason=exc.reason.value,
        resolution_error=None,
        note_code=None,
    )


def _from_resolution_failure(target: NormalizedTarget, resolution: Resolution) -> TargetValidation:
    """解析不出来。**如实报告，不是 500**（见 `dns_resolver` 的模块 docstring）。

    `normalized` 照样给全 —— 目标本身是合法的，用户需要看到规范化的结果才能判断
    自己是不是打错了名字。
    """
    error = resolution.error
    return TargetValidation(
        raw=target.raw,
        ok=False,
        normalized=NormalizedTargetView(
            url=target.url,
            scheme=target.scheme,
            host=target.host,
            host_unicode=target.host_unicode,
            port=target.port,
            path=target.path,
            is_ip=target.is_ip,
            punycode_applied=target.punycode_applied,
        ),
        kind=_kind(target),
        ip_class=None,
        allowlist_entry=None,
        requirement=None,
        code=None,
        reason=None,
        resolution_error=None if error is None else error.value,
        note_code=None,
    )


# =============================================================================
# 接口
# =============================================================================
@router.post("/validate", response_model=ValidateTargetsResponse)
async def validate_targets(
    request: Request, payload: ValidateTargetsRequest
) -> ValidateTargetsResponse:
    store = _store(request)
    resolver = _resolver(request)

    # 清单快照取**一次**：同一批目标必须按同一份规则判定。逐个取会让"正好在这中间
    # 有人改了文件"产出一份内部矛盾的结论。`current()` 是同步阻塞（stat + 可能的 read），
    # 所以经 to_thread（CLAUDE.md §Python：async 函数里禁止同步阻塞 IO）。
    snapshot = await asyncio.to_thread(store.current)

    # UTC 的今天。用 UTC 而不是本地时区：`expires` 是一个日期，而"过期了吗"的答案不该
    # 取决于容器的 TZ 设置（容器里通常根本没有 TZ，本地时间就是 UTC —— 那种"碰巧对"
    # 才是最危险的）。同样只取一次，一批目标共用同一个"今天"。
    today = datetime.now(UTC).date()

    opt_in = OperatorOptIn(
        loopback=payload.overrides.allow_loopback,
        private=payload.overrides.allow_private,
    )

    # ---- 第一步：规范化。失败的留着异常对象，位置不变 -------------------------
    parsed: list[NormalizedTarget | TargetRejected] = []
    for raw in payload.raw:
        try:
            parsed.append(normalize_target(raw))
        except TargetRejected as exc:
            parsed.append(exc)

    # ---- 第二步：解析。只解析主机名，且同名只解析一次 -------------------------
    hosts = tuple(
        dict.fromkeys(
            item.host for item in parsed if isinstance(item, NormalizedTarget) and not item.is_ip
        )
    )
    resolutions: dict[str, Resolution] = {}
    if hosts:
        outcomes = await asyncio.gather(*(resolver(host) for host in hosts))
        resolutions = dict(zip(hosts, outcomes, strict=True))

    # ---- 第三步：判定 -------------------------------------------------------
    results: list[TargetValidation] = []
    for item in parsed:
        if isinstance(item, TargetRejected):
            results.append(_from_rejection(item))
            continue

        if item.is_ip:
            addresses: tuple[str, ...] = (item.host,)
        else:
            resolution = resolutions[item.host]
            if resolution.error is not None:
                results.append(_from_resolution_failure(item, resolution))
                continue
            addresses = resolution.addresses

        # 同一个元组喂给 `decide` 和 `evaluate_target`：两处各拼一遍就会出现
        # "白名单按 A 组地址判、护栏按 B 组地址判"（见 `allowlist.decide` 的 docstring）。
        allowlist = decide(snapshot, host=item.host, addresses=addresses, today=today)
        verdict = evaluate_target(item, addresses, allowlist=allowlist, opt_in=opt_in)
        results.append(_from_verdict(verdict, allowlist_entry=allowlist.entry_label))

    return ValidateTargetsResponse(targets=tuple(results))
