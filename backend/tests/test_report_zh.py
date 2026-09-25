"""中文报告纯判定层：I1 模型输入是白名单、I2 解析器只收恰好合格的 JSON。"""

from __future__ import annotations

import json

import pytest

from app.services.report_zh import (
    FINDING_INPUT_KEYS,
    ExecutiveZh,
    FindingZh,
    PayloadRejected,
    executive_messages,
    finding_input,
    finding_messages,
    parse_executive,
    parse_finding,
    repair_messages,
)

# 真实形状的一条 vulnerabilities.json 记录（Strix 1.6.2，juice-shop 实测的键与类型）。
# 四个证据字段 + 一个白名单外的未知键各放一个哨兵串。
SENTINELS = (
    "SENTINEL-POC",
    "SENTINEL-EVIDENCE",
    "SENTINEL-CODELOC",
    "SENTINEL-POCDESC",
    "SENTINEL-UNKNOWN",
)
RAW: dict[str, object] = {
    "id": "vuln-0001",
    "title": "SQL injection in login",
    "severity": "critical",
    "timestamp": "2026-09-20T00:00:00Z",
    "description": "**[中文]** 登录接口存在 SQL 注入。 SQL injection in login.",
    "impact": "**[中文]** 可绕过登录。 Login bypass.",
    "target": "http://juice-shop:3000",
    "technical_analysis": "The email parameter is concatenated into the query.",
    "poc_description": "SENTINEL-POCDESC",
    "poc_script_code": "```python\nprint('SENTINEL-POC')\n```",
    "remediation_steps": "**[中文]** 使用参数化查询。 Use parameterized queries.",
    "evidence": "POST /rest/user/login ... SENTINEL-EVIDENCE",
    "assumptions": "None.",
    "counterevidence": "None.",
    "confidence": "high",
    "severity_change_conditions": "None.",
    "fix_effort": "low",
    "cvss": 9.1,
    "cvss_breakdown": {"attack_vector": "N", "attack_complexity": "L"},
    "endpoint": "/rest/user/login",
    "method": "POST",
    "cwe": "CWE-89",
    "finding_class": "dynamic",
    "agent_id": "agent-1",
    "agent_name": "root",
    "code_locations": [{"file": "SENTINEL-CODELOC", "line": 1}],
    "exploit_payload": "SENTINEL-UNKNOWN",
}

GOOD_FINDING: dict[str, object] = {
    "title_zh": "登录接口存在注入漏洞",
    "what_zh": "登录框会把用户输入直接拼进数据库查询。",
    "impact_zh": "攻击者无需密码即可登录任意账号。",
    "fix_zh": "改用参数化查询。",
    "severity_zh_label": "严重",
    "severity_reason_zh": "CVSS 9.1，网络可达、无需权限。",
    "effort_zh": "低",
    "who_fixes_zh": "后端开发",
    "confidence_zh": "高",
    "layman_analogy_zh": "像门卫把访客写的字条直接当成命令执行。",
}

GOOD_EXECUTIVE: dict[str, object] = {
    "summary_zh": "本次测试发现 1 个严重问题。",
    "risk_verdict_zh": "高风险",
    "top3_actions_zh": ["修复登录接口的注入漏洞"],
    "scope_zh": "juice-shop 测试环境",
    "coverage_zh": "覆盖了登录与购物车。",
    "not_tested_zh": ["支付流程未测试"],
}


def with_(base: dict[str, object], **changes: object) -> str:
    return json.dumps({**base, **changes}, ensure_ascii=False)


def without(base: dict[str, object], key: str) -> str:
    return json.dumps({k: v for k, v in base.items() if k != key}, ensure_ascii=False)


# =============================================================================
# I1 模型输入是白名单
# =============================================================================
def test_finding_input_keeps_only_whitelisted_keys_and_does_not_fill_missing() -> None:
    raw = {k: v for k, v in RAW.items() if k != "cwe"}
    got = finding_input(raw)
    assert set(got) == set(FINDING_INPUT_KEYS) - {"cwe"}
    assert got["cvss_breakdown"] == RAW["cvss_breakdown"]


def test_finding_messages_contain_no_evidence_or_unknown_values() -> None:
    text = "\n".join(m["content"] for m in finding_messages(RAW))
    assert [s for s in SENTINELS if s in text] == []
    assert json.loads(finding_messages(RAW)[1]["content"]) == finding_input(RAW)


def test_executive_messages_pass_only_whitelisted_coverage_parts() -> None:
    coverage: dict[str, object] = {
        "completeness": {"complete": False, "caveats": ["未测支付"], "extra": "SENTINEL-UNKNOWN"},
        "summary": {"tested": 3},
        "gaps": [
            {
                "kind": "untested",
                "surface": "/pay",
                "risk_area": "payment",
                "detail": "没时间",
                "evidence": "SENTINEL-EVIDENCE",
            }
        ],
        "entries": [{"evidence": "SENTINEL-EVIDENCE"}],
        "other": "SENTINEL-UNKNOWN",
    }
    body = json.loads(executive_messages("# r", {"critical": 1}, coverage)[1]["content"])
    assert body == {
        "report_markdown": "# r",
        "severity_counts": {"critical": 1},
        "coverage": {
            "completeness": {"complete": False, "caveats": ["未测支付"]},
            "summary": {"tested": 3},
            "gaps": [
                {"kind": "untested", "surface": "/pay", "risk_area": "payment", "detail": "没时间"}
            ],
        },
    }


def test_executive_messages_drop_coverage_parts_of_wrong_type() -> None:
    coverage: dict[str, object] = {"completeness": "yes", "summary": [1], "gaps": {"a": 1}}
    body = json.loads(executive_messages("# r", {}, coverage)[1]["content"])
    assert body["coverage"] == {}


# =============================================================================
# I2 解析器只收"恰好这些键、全合格、无未译术语"
# =============================================================================
@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("抱歉，我无法完成。", "no_json"),
        ("[1, 2]", "no_json"),
        (with_(GOOD_FINDING, evidence_zh="原始请求"), "bad_keys"),
        (without(GOOD_FINDING, "who_fixes_zh"), "bad_keys"),
        (with_(GOOD_FINDING, effort_zh=1), "bad_value"),
        (with_(GOOD_FINDING, impact_zh="   "), "bad_value"),
        (with_(GOOD_FINDING, layman_analogy_zh=None), "bad_value"),
        (with_(GOOD_FINDING, what_zh="存在 IDOR 问题"), "untranslated_term"),
        (with_(GOOD_FINDING, fix_zh="防止 Mass Assignment"), "untranslated_term"),
    ],
    ids=[
        "prose",
        "array",
        "extra_key",
        "missing_key",
        "int_value",
        "blank_value",
        "null_analogy",
        "term_upper",
        "term_mixed_case",
    ],
)
def test_parse_finding_rejects(text: str, reason: str) -> None:
    with pytest.raises(PayloadRejected) as exc:
        parse_finding(text)
    assert exc.value.reason == reason


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("no json here", "no_json"),
        (with_(GOOD_EXECUTIVE, evidence_zh="x"), "bad_keys"),
        (without(GOOD_EXECUTIVE, "not_tested_zh"), "bad_keys"),
        (with_(GOOD_EXECUTIVE, scope_zh=""), "bad_value"),
        (with_(GOOD_EXECUTIVE, summary_zh="字" * 301), "bad_value"),
        (with_(GOOD_EXECUTIVE, top3_actions_zh=[]), "bad_value"),
        (with_(GOOD_EXECUTIVE, top3_actions_zh=["a", "b", "c", "d"]), "bad_value"),
        (with_(GOOD_EXECUTIVE, top3_actions_zh="修复"), "bad_value"),
        (with_(GOOD_EXECUTIVE, not_tested_zh=[]), "bad_value"),
        (with_(GOOD_EXECUTIVE, not_tested_zh=[" "]), "bad_value"),
        (with_(GOOD_EXECUTIVE, not_tested_zh=["存在 ssrf 风险"]), "untranslated_term"),
    ],
    ids=[
        "prose",
        "extra_key",
        "missing_key",
        "blank_scalar",
        "summary_too_long",
        "no_actions",
        "four_actions",
        "actions_not_list",
        "not_tested_empty",
        "not_tested_blank",
        "term_in_list",
    ],
)
def test_parse_executive_rejects(text: str, reason: str) -> None:
    with pytest.raises(PayloadRejected) as exc:
        parse_executive(text, require_not_tested=True)
    assert exc.value.reason == reason


def test_parse_finding_accepts_fenced_json_path_term_and_empty_analogy() -> None:
    payload = with_(
        GOOD_FINDING, what_zh="接口 /api/csrf 与 csrf-token 字段可被滥用", layman_analogy_zh=""
    )
    got = parse_finding(f"好的：\n```json\n{payload}\n```")
    assert got == FindingZh(
        **{
            **GOOD_FINDING,
            "what_zh": "接口 /api/csrf 与 csrf-token 字段可被滥用",
            "layman_analogy_zh": "",
        }
    )


def test_parse_executive_accepts_empty_not_tested_when_not_required() -> None:
    got = parse_executive(
        f"```json\n{with_(GOOD_EXECUTIVE, not_tested_zh=[])}\n```", require_not_tested=False
    )
    assert got == ExecutiveZh(
        summary_zh="本次测试发现 1 个严重问题。",
        risk_verdict_zh="高风险",
        top3_actions_zh=("修复登录接口的注入漏洞",),
        scope_zh="juice-shop 测试环境",
        coverage_zh="覆盖了登录与购物车。",
        not_tested_zh=(),
    )


def test_repair_messages_appends_without_mutating_input() -> None:
    original = finding_messages(RAW)
    snapshot = [dict(m) for m in original]
    got = repair_messages(original, "not json", "no_json")
    assert original == snapshot
    assert got[:2] == snapshot
    assert [m["role"] for m in got[-2:]] == ["assistant", "user"]
    assert got[-2]["content"] == "not json"
