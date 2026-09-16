"""`/api/keys` —— 凭据登记、查看元数据、立即忘掉。

# 这一层只做四件事，顺序不可换

    1. 边界模型解析（`secrets` 的值一律 `SecretStr`）
    2. 形状校验 —— 未知组合 422、多余的键 400、缺键 422（全在 `llm_client`，纯函数）
    3. 验活（可关，默认开）—— 失败就 400，**且什么都不存**
    4. 存 vault → 写审计 → 201

**校验必须在验活之前**：形状错了就没必要往外发一次请求，而反过来会让"多填了一项"的
用户先等 8 秒才看到一个和网络无关的错误。

# 凭据在本文件里的接触面

只有两处：请求体里的 `SecretStr`（直接原样交给 `KeyVault.store()`），以及
`labels_for()` 算出来的掩码标签。**明文不进日志、不进审计、不进响应**——
掩码这一步刻意放在 `key_vault` 里（`labels_for`），这样"调用 `get_secret_value()`"
这个动作不会扩散到路由层来。

# 为什么 `DELETE` 是 204 幂等，而 `/api/allowlist/entries/{label}` 的删除是 404

两者语义不同。删授权清单条目是"改一份共享的、有权威副本的配置"，删不到就意味着调用方
看到的清单和真实的不一样 —— 那件事操作者必须知道。而 `DELETE /api/keys/{h}` 是
"现在忘掉我的凭据"，`api` 一重启全部 handle 就都没了（内存 vault），**"已经不在了"
正是调用方想要的结果**。让它 404 只会让前端为一个成功状态写一条错误分支。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response
from pydantic import Field, SecretStr

from app.errors import InvalidRequestError, KeyRequiredError, KeyVerifyFailedError
from app.models import BoundaryModel
from app.routes._context import actor, client_ip
from app.services.audit import (
    EVENT_KEY_DROPPED,
    EVENT_KEY_REGISTERED,
    AuditDetailValue,
    AuditEntry,
    record,
)
from app.services.key_vault import CredentialSet, KeyVault, labels_for
from app.services.llm_client import Verifier, check_param_keys, check_secret_keys, spec_for
from app.settings import Settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/keys", tags=["keys"])


# =============================================================================
# 边界模型
# =============================================================================
class KeyRegisterRequest(BoundaryModel):
    """`POST /api/keys` 的请求体（`PLAN.md:567`）。

    `secrets` 的值是 `SecretStr`：它的 `repr` 是 `SecretStr('**********')`，所以任何
    "顺手 log 一下入参"的写法都不会泄漏明文。这不是纵深防御的装饰 —— Pydantic 的
    `ValidationError` 消息会把出错字段的输入值原样嵌进去，而那条消息进过日志。

    `params` 是**非机密**参数（区域）。它与 `secrets` 分开不只是类型问题：`params`
    不进日志脱敏集合，往里放凭据就是直接泄漏（见 `key_vault` 模块 docstring）。
    """

    provider: str = Field(min_length=1)
    auth_shape: str = Field(min_length=1)
    strix_llm: str = Field(min_length=1)
    api_base: str | None = None
    secrets: dict[str, SecretStr]
    params: dict[str, str] = Field(default_factory=dict)
    verify: bool = True
    """默认 **true**。关掉它是给离线自测用的，正常向导一律带着真实验活 ——
    否则错的凭据会在几十分钟后的扫描里才暴露。"""


class KeyRegisteredResponse(BoundaryModel):
    """`201`。**没有任何字段能推回明文**。"""

    vault_handle: str
    labels: dict[str, str]
    """env 名 → 掩码标签。**一组值给一组标签**，不是一组值一条 —— Bedrock SigV4 有两个
    值，只显示一条会让人无法判断是哪一个填错了（`PLAN.md:342`）。"""

    verified: bool
    """**如实**回传。`verify=false` 时这里必须是 `false`，不能因为"存进去了"就写 true。"""

    verify_latency_ms: int | None
    """没验活时是 `null`，而不是 0 —— 0 会被读成"验得飞快"。"""


class KeyStateResponse(BoundaryModel):
    """`GET /api/keys/{handle}`。刷新页面后前端用它恢复"当前用的是哪个凭据"。"""

    provider: str
    auth_shape: str
    strix_llm: str
    api_base: str | None
    labels: dict[str, str]
    params: dict[str, str]


# =============================================================================
# 依赖取用 —— 形状与 `routes/targets.py` 的 `_store` / `_resolver` 对齐
# =============================================================================
def _vault(request: Request) -> KeyVault:
    vault = getattr(request.app.state, "key_vault", None)
    if not isinstance(vault, KeyVault):
        raise RuntimeError(
            "app.state.key_vault 不存在。本接口要求 lifespan 已经跑过 —— "
            "测试里请用 `with TestClient(app):`。"
        )
    return vault


def _settings(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    if not isinstance(settings, Settings):
        raise RuntimeError(
            "app.state.settings 不存在。本接口要求 lifespan 已经跑过 —— "
            "测试里请用 `with TestClient(app):`。"
        )
    return settings


def _verifier(request: Request) -> Verifier:
    """真实验活器。**这就是单测的注入点**（CLAUDE.md §测试 禁止单测碰真实网络）。

    不做 `isinstance` 检查：`Verifier` 是一个 `Callable` 别名，`isinstance` 对它只能查
    "是不是可调用"，拦不住任何真实错误。理由与 `routes/targets.py::_resolver` 一致。
    """
    verifier: Verifier = request.app.state.llm_verifier
    return verifier


async def _audit(request: Request, *, event: str, detail: dict[str, AuditDetailValue]) -> None:
    """写一条审计。`detail` 里**只许有掩码标签与机器码**，明文一个字都不许进。"""
    await record(
        db=request.app.state.db,
        audit_dir=_settings(request).audit_dir,
        entry=AuditEntry(
            event=event,
            actor=actor(request),
            detail=detail,
            client_ip=client_ip(request),
            user_agent=request.headers.get("user-agent"),
        ),
    )


# =============================================================================
# 接口
# =============================================================================
@router.post("", status_code=201, response_model=KeyRegisteredResponse)
async def register_key(request: Request, payload: KeyRegisterRequest) -> KeyRegisteredResponse:
    """登记一组凭据，返回不可猜的 `vault_handle`。顺序见模块 docstring。"""
    spec = spec_for(payload.provider, payload.auth_shape)
    if spec is None:
        # 422 而不是 404：请求体里的两个字段组合不合法，这是"请求内容不合法"。
        # `field` 指 `auth_shape` 而不是 `provider`：provider 名单是前端从
        # `/api/providers` 拿的，它不会写错；能对不上的是形状那一维。
        raise InvalidRequestError(field="auth_shape")

    check_secret_keys(spec, payload.secrets)
    # `params` 也要校验，而且理由与 secrets 不同（那边防静默故障，这边防泄漏）——
    # 判据整段写在 `llm_client.check_param_keys` 的 docstring 里。
    check_param_keys(spec, payload.params)

    credentials = CredentialSet(
        provider=payload.provider,
        auth_shape=payload.auth_shape,
        strix_llm=payload.strix_llm,
        api_base=payload.api_base,
        secrets=payload.secrets,
        params=payload.params,
    )

    latency_ms: int | None = None
    if payload.verify:
        outcome = await _verifier(request)(credentials)
        latency_ms = outcome.latency_ms
        if not outcome.ok:
            # **验活失败就什么都不存。** 存一份"已知是错的"凭据只会让用户在下一步拿着
            # 一个能用的 handle 去起扫描，然后在几十分钟后失败。
            # params 里刻意没有 `reason`：模型服务的错误正文可能回显凭据本身。
            raise KeyVerifyFailedError(
                provider=payload.provider,
                auth_shape=payload.auth_shape,
                latency_ms=latency_ms,
            )

    handle = _vault(request).store(credentials)
    labels = labels_for(credentials)
    await _audit(
        request,
        event=EVENT_KEY_REGISTERED,
        detail={
            "provider": payload.provider,
            "auth_shape": payload.auth_shape,
            # 掩码标签，每个值一条。`AuditDetailValue` 不允许嵌套对象，所以拼成
            # `env=掩码` 的字符串 —— 这一份的用途是 `grep`，那个形状正好好 grep。
            "labels": [f"{name}={label}" for name, label in sorted(labels.items())],
        },
    )
    return KeyRegisteredResponse(
        vault_handle=handle,
        labels=labels,
        verified=payload.verify,
        verify_latency_ms=latency_ms,
    )


@router.get("/{handle}", response_model=KeyStateResponse)
async def get_key(request: Request, handle: str) -> KeyStateResponse:
    """凭据的**元数据**（含掩码标签）。取用会刷新 idle 期限，这是刻意的。

    失效 → 409 `key_required`，**params 是空的**：一个查不到的 handle 我们说不出它原来
    是哪个供应商，而编一个值出来比不给更糟（前端会把它显示成"你的 anthropic 凭据过期了"）。
    """
    credentials = _vault(request).get(handle)
    if credentials is None:
        raise KeyRequiredError()
    return KeyStateResponse(
        provider=credentials.provider,
        auth_shape=credentials.auth_shape,
        strix_llm=credentials.strix_llm,
        api_base=credentials.api_base,
        labels=labels_for(credentials),
        params=dict(credentials.params),
    )


@router.delete("/{handle}", status_code=204)
async def drop_key(request: Request, handle: str) -> Response:
    """立即忘掉。204 且幂等，理由见模块 docstring。

    审计**只在真的删掉一条时才写**：给一个不存在的 handle 记一条 `key.dropped` 是往
    审计里写一件没发生的事，而审计的全部价值就是"里面写的都真的发生过"。
    """
    vault = _vault(request)
    credentials = vault.get(handle)
    if credentials is not None and vault.drop(handle):
        await _audit(
            request,
            event=EVENT_KEY_DROPPED,
            detail={"provider": credentials.provider, "auth_shape": credentials.auth_shape},
        )
    # 204 不许带正文，所以显式返回一个空 Response（返回 None 会让 FastAPI 试着
    # 序列化 `null` 进正文）。
    return Response(status_code=204)
