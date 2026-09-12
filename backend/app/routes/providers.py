"""`GET /api/providers` —— 前端渲染凭据输入框的唯一依据。

本文件里**没有一条目录数据**：谁支持哪几种形状、每种要哪几个键，全在
`services/llm_client.CATALOG`。理由是那份知识与"怎么用这些键发一次请求"必须放在一起
（见该模块 docstring）；这里只做"内部 dataclass → 对外 JSON"这一层转换。

响应**全是机器码**（`bedrock` / `bedrock_sigv4` / `AWS_REGION_NAME`），中文在
`frontend/messages/zh-CN.json` 的 `providers.*` 下。后端一旦回中文，同一个名字就会有
两份文案，而后端那份还绕过脱敏（CLAUDE.md §错误与文案）。

`model_prefix` 刻意**不对外**：前缀由后端按形状补（`llm_client.model_for()`），前端知道
它只会诱使前端自己去拼，然后两处拼法在下一次改动里分叉。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.models import BoundaryModel
from app.services.llm_client import CATALOG, ProviderSpec, ShapeSpec

router = APIRouter(prefix="/api/providers", tags=["providers"])


class ShapeView(BoundaryModel):
    """一种凭据形状的对外形状。

    `secret_keys` 与 `param_keys` 分开给，前端据此决定哪几个框是 `type=password`
    （凭据）、哪几个是普通文本（区域）。合成一个列表就没法区分了。
    """

    auth_shape: str
    secret_keys: tuple[str, ...]
    param_keys: tuple[str, ...]


class ProviderView(BoundaryModel):
    provider: str
    shapes: tuple[ShapeView, ...]
    models: tuple[str, ...]
    """建议列表，**不是白名单**（见 `ProviderSpec.models`）。可以是空的。"""

    api_base_allowed: bool


class ProvidersResponse(BoundaryModel):
    """信封而不是裸数组 —— 顶层是数组就没法再加字段（同 `routes/targets.py` 的理由）。"""

    providers: tuple[ProviderView, ...]


def _shape_view(spec: ShapeSpec) -> ShapeView:
    return ShapeView(
        auth_shape=spec.auth_shape,
        secret_keys=spec.secret_keys,
        param_keys=spec.param_keys,
    )


def _provider_view(spec: ProviderSpec) -> ProviderView:
    return ProviderView(
        provider=spec.provider,
        shapes=tuple(_shape_view(shape) for shape in spec.shapes),
        models=spec.models,
        api_base_allowed=spec.api_base_allowed,
    )


@router.get("", response_model=ProvidersResponse)
async def list_providers() -> ProvidersResponse:
    """无参、恒 200。目录是进程内常量，没有 IO。"""
    return ProvidersResponse(providers=tuple(_provider_view(spec) for spec in CATALOG))
