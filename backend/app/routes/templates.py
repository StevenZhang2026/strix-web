"""`GET /api/scan-templates` —— 向导第一步那几个按钮的唯一依据。

与 `routes/providers.py` 是同一个形状（进程内常量 → 对外 JSON），两条判据也照抄：
① 目录数据在 `services/scan_templates.py`，这里只做"内部 dataclass → 对外 JSON"；
② 响应**全是机器码**（`full_review` / `standard`），中文名与说明在
`frontend/messages/zh-CN.json` 的 `scanTemplates.*` 下（T19）—— 后端一旦回中文，同一个
名字就有两份文案，而后端那份还绕过脱敏。

`instruction_body` 刻意**不对外**：它是"我们怎么驱动 Strix"的实现细节，前端不展示它，
而把它送出去只会诱使有人在前端改一份、后端改另一份。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.models import BoundaryModel
from app.services.scan_templates import TEMPLATES, ScanTemplate

router = APIRouter(prefix="/api/scan-templates", tags=["scan-templates"])


class ScanTemplateView(BoundaryModel):
    """一个模板的对外形状。

    预算与轮数带 `default_` 前缀是刻意的：向导允许改，最终值由 `POST /api/scans` 决定，
    不带前缀会让前端以为这是硬上限（真上限是 `console_max_budget_ceiling_usd`）。
    """

    template_id: str
    scan_mode: str
    default_budget_usd: float
    default_max_turns: int
    recommended: bool


class ScanTemplatesResponse(BoundaryModel):
    """信封而不是裸数组 —— 顶层是数组就没法再加字段（同 `routes/providers.py`）。"""

    templates: tuple[ScanTemplateView, ...]


def _template_view(template: ScanTemplate) -> ScanTemplateView:
    return ScanTemplateView(
        template_id=template.template_id,
        scan_mode=template.scan_mode,
        default_budget_usd=template.default_budget_usd,
        default_max_turns=template.default_max_turns,
        recommended=template.recommended,
    )


@router.get("", response_model=ScanTemplatesResponse)
async def list_scan_templates() -> ScanTemplatesResponse:
    """无参、恒 200。目录是进程内常量，没有 IO。顺序就是向导上的展示顺序。"""
    return ScanTemplatesResponse(templates=tuple(_template_view(t) for t in TEMPLATES))
