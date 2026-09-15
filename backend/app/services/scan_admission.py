"""扫描准入判定（T12a）。**零 IO 纯函数** —— DNS 结果、清单快照、"今天"都由调用方注入。

`POST /api/scans`（T12c）在起子进程之前要连着问四个问题：目标写对了吗、DNS 还是声明时
那几个地址吗、护栏放行吗、操作者逐字打对确认串了吗。这四条收在这里一个函数里，为的是让
"少问了一条"这件事在**一个**地方可查，而不是散在路由的四段 if 里。

本模块**不产出 HTTP 响应、不抛 HTTP 异常、不写 DB、不写审计**：码 → status 的透出与真正的
`audit.record()` 调用都在 T12c。这里只回答"准不准"。

为什么返回联合类型（`AdmissionPassed | AdmissionRejected`）而不是抛异常、也不是一个带
`rejection: X | None` 字段的大 dataclass：后者留着一条"忘了检查 rejection 就直接拿
targets"的静默放行路径，而本项目已经栽过三次"声明了却没有任何一处强制"。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from app.errors import (
    DnsChangedError,
    InvalidRequestError,
    MissingTypedConfirmationError,
    ParamValue,
)
from app.services.allowlist import AllowlistSnapshot, decide
from app.services.audit import (
    EVENT_TARGET_DNS_CHANGED,
    EVENT_TARGET_REJECTED,
    AuditDetailValue,
)
from app.services.dns_resolver import Resolution
from app.services.target_guard import (
    AllowlistDecision,
    GuardVerdict,
    NormalizedTarget,
    OperatorOptIn,
    TargetRejected,
    evaluate_target,
    normalize_target,
)


@dataclass(frozen=True, slots=True)
class AdmissionRequest:
    """一次准入判定的全部输入。**所有 IO 的结果都在这里，函数内不再取任何东西。**"""

    raw_targets: tuple[str, ...]
    """用户原始输入，逐条，顺序即 `params["index"]`。"""

    typed_confirmation: str
    """逐字输入的确认串。"""

    opt_in: OperatorOptIn
    """由请求里的 `overrides.{allow_loopback,allow_private}` 组出。"""

    declared_ips: Mapping[str, tuple[str, ...]]
    """host → 声明授权时记下的地址（`authorizations.resolved_ips_json`）。"""

    resolutions: Mapping[str, Resolution]
    """host → 本次**重新**解析的结果。字面 IP 目标不在内（它没有 DNS 这一步）。"""

    allowlist: AllowlistSnapshot
    """调用方取**一次**快照传进来：一批目标必须按同一份规则判定。"""

    today: date
    """UTC 的今天。同样只取一次，一批目标共用同一个"今天"。"""


@dataclass(frozen=True, slots=True)
class AdmittedTarget:
    target: NormalizedTarget
    verdict: GuardVerdict
    allowlist: AllowlistDecision
    """命中的清单条目决策。T12c 要用 `entry_label` 落 `allowlist_entry_id` 与审计。"""


@dataclass(frozen=True, slots=True)
class AdmissionPassed:
    targets: tuple[AdmittedTarget, ...]


@dataclass(frozen=True, slots=True)
class AdmissionRejected:
    code: str
    """`errors.py` 里**已有**的机器码。本模块不发明新码。"""

    params: Mapping[str, ParamValue]
    """直接进 HTTP 响应体。`errors.py` 的规矩：绝不放凭据、绝不放中文。"""

    target_host: str | None
    """已规范化的 host。**只进审计 detail，绝不进 `params`**（规范化失败时为 None）。"""


AdmissionOutcome = AdmissionPassed | AdmissionRejected


def evaluate_admission(request: AdmissionRequest) -> AdmissionOutcome:
    """四条判定，**顺序即优先级**。第一个失败即返回，不做严重度排序。

    不排严重度阶梯的理由：操作者一次只改一件事，而"先让他改哪一件"最省事的答案就是
    "从头往下第一件"。按严重度重排会让他修完最严重的那条之后又冒出一条更靠前的。
    """
    if not request.raw_targets:
        # 空目标表。静默返回"通过且零个目标"会让 T12c 起一次没有目标的扫描 —— 那是本
        # 模块最糟的失败模式。请求模型本该先挡住它，走到这里就是程序错误。
        raise ValueError("raw_targets 为空：准入判定至少需要一个目标。")

    # ---- 第一步：规范化。遇到第一个失败就返回 --------------------------------
    #
    # 为什么 fail-fast 而不逐条列全：`ParamValue` 只许 JSON 标量（刻意不许 dict/list，
    # 防止有人往里塞整个请求体、进而塞进凭据），列全就得先破那条约束。而这条路径本该被
    # 向导第 1 步的 `POST /api/targets/validate` 拦住，能走到这里已经是异常情况。
    targets: list[NormalizedTarget] = []
    for index, raw in enumerate(request.raw_targets):
        try:
            targets.append(normalize_target(raw))
        except TargetRejected as exc:
            # `raw` 绝不进 params：`user:pass@host` 的原文回显只许存在于 validate 的
            # 200 正文那一处。这里只给机器可读的 reason。
            return AdmissionRejected(
                code=InvalidRequestError.code,
                params={"field": "targets", "index": index, "reason": exc.reason.value},
                target_host=None,
            )

    # ---- 第二步：逐个目标过 DNS 比对与护栏 -----------------------------------
    admitted: list[AdmittedTarget] = []
    for index, target in enumerate(targets):
        if target.is_ip:
            # 字面 IP 没有 DNS 这一步，也就没有 rebinding 的可能。
            addresses: tuple[str, ...] = (target.host,)
        else:
            if target.host not in request.resolutions:
                # 调用方少走了解析这一步。静默当成"没有地址所以放行"会是本模块最糟的
                # 失败模式 —— 与 `evaluate_target` 对同一件事的处理完全一致。
                raise ValueError(
                    f"主机名 {target.host!r} 没有本次解析结果。"
                    "scan_admission 不做 DNS 解析（本模块零 IO），调用方必须先解析。"
                )
            resolution = request.resolutions[target.host]
            if resolution.error is not None:
                # 复用 `dns_changed` 而不加新码：声明时解析出 N 个地址、现在解析出 0 个，
                # "解析结果与声明不一致"这句话是真的；处置动作与 rebinding 也一样 ——
                # 回向导第 1 步重新验证。
                return AdmissionRejected(
                    code=DnsChangedError.code,
                    params={
                        "field": "targets",
                        "index": index,
                        "resolution_error": resolution.error.value,
                    },
                    target_host=target.host,
                )
            addresses = resolution.addresses

            # **按集合比、不按顺序比**：`getaddrinfo` 的返回顺序受 RFC 6724 与 DNS
            # 轮转影响，按元组比会让每一个做轮询的域名每次都报一次假的 rebinding 警报。
            # 有序那份留在 `authorizations.resolved_ips_json` 里当证据 —— 那是"存"，
            # 这里是"比"，两回事。
            if frozenset(addresses) != frozenset(request.declared_ips.get(target.host, ())):
                return AdmissionRejected(
                    code=DnsChangedError.code,
                    params={"field": "targets", "index": index},
                    target_host=target.host,
                )

        # 同一个元组喂给 `decide` 和 `evaluate_target`：两处各拼一遍就会出现"白名单按
        # A 组地址判、护栏按 B 组地址判"（见 `allowlist.decide` 的 docstring）。
        allowlist = decide(
            request.allowlist, host=target.host, addresses=addresses, today=request.today
        )
        verdict = evaluate_target(target, addresses, allowlist=allowlist, opt_in=request.opt_in)
        if verdict.allowed:
            admitted.append(AdmittedTarget(target=target, verdict=verdict, allowlist=allowlist))
            continue

        if verdict.error_code is not None:
            # 护栏自己说了该报哪个码（blocked_metadata / split_horizon /
            # not_in_allowlist）。它优先于"缺勾选"：两者可以同时非空（例如一个名字同时
            # 解析到 CGNAT 与内网），而勾完内网那一项也不会让 CGNAT 那半放行。
            return AdmissionRejected(
                code=verdict.error_code,
                params={"field": "targets", "index": index},
                target_host=target.host,
            )
        if verdict.required_opt_in:
            # 缺勾选不是"目标错了"，是 overrides 少给了 —— field 指向前端该改的地方。
            return AdmissionRejected(
                code=InvalidRequestError.code,
                params={
                    "field": "overrides",
                    "index": index,
                    "missing_opt_in": verdict.required_opt_in[0].value,
                },
                target_host=target.host,
            )
        raise ValueError(
            f"护栏对 {target.host!r} 既不放行、又没给 error_code、也没给 required_opt_in："
            "GuardVerdict 的契约被破坏了。静默放行不可接受。"
        )

    # ---- 第三步：逐字确认串 -------------------------------------------------
    #
    # 放在最后：目标本身还没过护栏的时候要求用户逐字打它是无意义的 —— 他可能得先去改目标。
    #
    # 期望值就是第一个目标规范化后的 host（`PLAN.md` §护栏 第 3 条；`registrable_domain`
    # 已于 2026-09-11 拍板不做）。host 已经是小写 ASCII，所以只需要把用户那侧折一下。
    expected = targets[0].host
    if request.typed_confirmation.strip().casefold() != expected:
        # params 刻意为空：期望串前端已经灰显在输入框旁边，后端再说一遍只是把同一份
        # 信息造两份。
        return AdmissionRejected(
            code=MissingTypedConfirmationError.code,
            params={},
            target_host=expected,
        )

    return AdmissionPassed(targets=tuple(admitted))


def audit_for(rejection: AdmissionRejected) -> tuple[str, dict[str, AuditDetailValue]]:
    """`(事件名, detail)`。真正的 `audit.record()` 调用在 T12c。

    审计 detail 比 HTTP 响应体多一个 `host`：响应体里不需要它（前端知道自己提交了什么），
    而事后查"哪个目标被拦了"只能靠审计。
    """
    event = (
        EVENT_TARGET_DNS_CHANGED
        if rejection.code == DnsChangedError.code
        else EVENT_TARGET_REJECTED
    )
    detail: dict[str, AuditDetailValue] = {"code": rejection.code, "host": rejection.target_host}
    detail.update(rejection.params)
    return event, detail
