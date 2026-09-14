"""T9 —— 6 个扫描模板的目录，以及 `GET /api/scan-templates`。

指令正文本身不在这里逐字断言（那等于把 prompt 抄两遍），只断言两件会真的坏事的事：
① 目录的形状与值（前端与 argv 都依赖它们）；② 对外响应**全是机器码、没有中文**。
`compose_instruction` 把统一尾巴接上去这一条在 `test_scan_launcher.py` 里测过一次，
这里不重复（agent-rules §十.4）。
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.services.scan_templates import TEMPLATES, template_for

_CHINESE = re.compile(r"[一-鿿]")

_EXPECTED = {
    "quick_triage": ("quick", 5.0, 60),
    "full_review": ("standard", 25.0, 200),
    "deep_audit": ("deep", 80.0, 500),
    "auth_and_access": ("standard", 15.0, 120),
    "api_surface": ("standard", 25.0, 200),
    "pre_release_recheck": ("standard", 12.0, 100),
}


def test_catalog_matches_the_approved_table() -> None:
    """预算与轮数是**产品决策**（PLAN.md §向导→CLI 映射），不是随手填的默认值。"""
    actual = {
        t.template_id: (t.scan_mode, t.default_budget_usd, t.default_max_turns) for t in TEMPLATES
    }
    assert actual == _EXPECTED


def test_exactly_one_template_is_recommended() -> None:
    """向导要预选一个 —— 两个"推荐"就等于没有推荐。"""
    assert [t.template_id for t in TEMPLATES if t.recommended] == ["full_review"]


def test_template_for_returns_none_for_unknown_id() -> None:
    """返回 `None` 而不是抛异常：调用方（launcher）才知道该抛哪个码。"""
    assert template_for("no_such_template") is None
    assert template_for("full_review") is not None


def test_instruction_bodies_are_english() -> None:
    """给 Strix 的指令正文用英文（它的 system prompt 与 ~90 个 skill 都是英文）。"""
    for template in TEMPLATES:
        assert not _CHINESE.search(template.instruction_body), template.template_id


def test_list_scan_templates_returns_machine_codes_only(client: TestClient) -> None:
    """一条中文断言：后端一旦回中文，同一个名字就有两份文案，而后端那份还绕过脱敏。"""
    response = client.get("/api/scan-templates")
    assert response.status_code == 200
    body = response.json()
    assert [item["template_id"] for item in body["templates"]] == list(_EXPECTED)
    assert not _CHINESE.search(response.text)
    # 指令正文不对外：它是我们怎么驱动 Strix 的实现细节，前端不需要也不该展示。
    assert "instruction_body" not in body["templates"][0]
