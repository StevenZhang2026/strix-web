from __future__ import annotations

import dataclasses
import html
import json
from html.parser import HTMLParser

import pytest

from app.services.exporter_html import ReportFinding, ReportScan, render_report_html
from app.services.report_zh import ExecutiveZh, FindingZh

# 以 `"'>` 开头：值若被插进属性，要先闭合引号才会冒出新标签；放在末尾会整段困在属性值里、测不到。
PAYLOAD = "\"'></pre><script>alert(1)</script><img src=x onerror=alert(1)>&"
FORBIDDEN = ("完整跑完", "没有发现漏洞", "未发现漏洞")

RAW: dict[str, object] = {
    "id": "vuln-0001",
    "title": "SQL Injection in login",
    "severity": "critical",
    "description": "**[中文]** 登录接口存在 SQL 注入。\n\n**[English]** SQL injection in login.",
    "impact": "**[中文]** 可绕过登录。\n\n**[English]** Authentication bypass.",
    "target": "http://juice-shop:3000",
    "technical_analysis": "The email parameter is concatenated into SQL.",
    "poc_description": "Send a crafted email field.",
    "poc_script_code": "```python\nimport requests\nrequests.post(url, json={'email': \"' OR 1=1--\"})\n```",
    "remediation_steps": "**[中文]** 使用参数化查询。\n\n**[English]** Use parameterized queries.",
    "evidence": "POST /rest/user/login HTTP/1.1\nHost: juice-shop:3000\n\nHTTP/1.1 200 OK",
    "confidence": "high",
    "cvss": 9.1,
    "endpoint": "/rest/user/login",
    "method": "POST",
    "cwe": "CWE-89",
    "code_locations": [{"file": "routes/login.ts", "start_line": 34, "end_line": 36}],
}

FZH = FindingZh(
    title_zh="登录接口 SQL 注入",
    what_zh="登录框没有过滤输入。",
    impact_zh="攻击者可以不用密码登录任何账号。",
    fix_zh="改用参数化查询。",
    severity_zh_label="严重",
    severity_reason_zh="无需认证即可利用。",
    effort_zh="小：半天。",
    who_fixes_zh="后端开发。",
    confidence_zh="高：已实际复现。",
    layman_analogy_zh="像门卫只看你说的名字就放行。",
)

EXEC = ExecutiveZh(
    summary_zh="本次扫描发现 1 个严重问题。",
    risk_verdict_zh="高风险。",
    top3_actions_zh=("修复登录注入", "复测", "加 WAF"),
    scope_zh="只测了 juice-shop。",
    coverage_zh="主要接口已覆盖。",
    not_tested_zh=("管理后台",),
)


def _scan(**overrides: object) -> ReportScan:
    base = ReportScan(
        scan_id="scan-0001",
        targets=("http://juice-shop:3000",),
        template_id="quick_web",
        status="completed",
        exit_meaning="vulnerabilities_found",
        error_code=None,
        started_at="2026-09-01T10:00:00Z",
        finished_at="2026-09-01T11:00:00Z",
    )
    return dataclasses.replace(base, **overrides)  # type: ignore[arg-type]


def _finding(**overrides: object) -> ReportFinding:
    base = ReportFinding(
        finding_id="f-1",
        severity="critical",
        title="SQL Injection in login",
        cvss=9.1,
        cwe="CWE-89",
        endpoint="/rest/user/login",
        method="POST",
        raw=RAW,
        zh=None,
    )
    return dataclasses.replace(base, **overrides)  # type: ignore[arg-type]


def _render(
    scan: ReportScan | None = None,
    findings: tuple[ReportFinding, ...] = (),
    executive: ExecutiveZh | None = None,
    cost: float | None = 0.0123,
) -> str:
    return render_report_html(scan or _scan(), findings, executive, cost)


class _TagCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.attrs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        self.attrs.extend(name for name, _ in attrs)


def _raw_with(key: str, value: object) -> dict[str, object]:
    return {**RAW, key: value}


def _cases() -> list[object]:
    """每格只往一个字段塞 PAYLOAD；第三项是它在输出里应有的（未转义）文本形式。"""
    cases: list[object] = []
    scan_fields = {
        "targets": ("http://ok", PAYLOAD),
        "template_id": PAYLOAD,
        "status": PAYLOAD,
        "error_code": PAYLOAD,
        "scan_id": PAYLOAD,
        "started_at": PAYLOAD,
        "finished_at": PAYLOAD,
    }
    for name, value in scan_fields.items():
        cases.append(pytest.param(_scan(**{name: value}), (), None, PAYLOAD, id=f"scan.{name}"))
    for name in ("title", "severity", "cwe", "endpoint", "method"):
        f = _finding(**{name: PAYLOAD})
        cases.append(pytest.param(_scan(), (f,), None, PAYLOAD, id=f"finding.{name}"))
    for key in ("description", "impact", "remediation_steps"):
        f = _finding(raw=_raw_with(key, PAYLOAD))
        cases.append(pytest.param(_scan(), (f,), None, PAYLOAD, id=f"raw.{key}"))
    for key in ("poc_description", "poc_script_code", "evidence"):
        f = _finding(raw=_raw_with(key, PAYLOAD), zh=FZH)
        cases.append(pytest.param(_scan(), (f,), None, PAYLOAD, id=f"raw.{key}"))
    locations = [{"file": PAYLOAD, "start_line": 1}]
    f = _finding(raw=_raw_with("code_locations", locations), zh=FZH)
    text = json.dumps(locations, ensure_ascii=False, indent=2)
    cases.append(pytest.param(_scan(), (f,), None, text, id="raw.code_locations"))
    for field in dataclasses.fields(FindingZh):
        f = _finding(zh=dataclasses.replace(FZH, **{field.name: PAYLOAD}))
        cases.append(pytest.param(_scan(), (f,), None, PAYLOAD, id=f"zh.{field.name}"))
    for field in dataclasses.fields(ExecutiveZh):
        value: object = (PAYLOAD,) if field.name.endswith("s_zh") else PAYLOAD
        if field.name == "not_tested_zh":
            value = ("x", PAYLOAD)
        ex = dataclasses.replace(EXEC, **{field.name: value})
        cases.append(pytest.param(_scan(), (), ex, PAYLOAD, id=f"exec.{field.name}"))
    return cases


@pytest.mark.parametrize(("scan", "findings", "executive", "text"), _cases())
def test_every_field_is_escaped_and_rendered(
    scan: ReportScan,
    findings: tuple[ReportFinding, ...],
    executive: ExecutiveZh | None,
    text: str,
) -> None:
    out = _render(scan, findings, executive)
    parser = _TagCollector()
    parser.feed(out)
    assert "script" not in parser.tags
    assert "img" not in parser.tags
    assert not [a for a in parser.attrs if a.startswith("on")]
    assert html.escape(text, quote=True) in out


def test_single_style_block_and_no_script() -> None:
    out = _render(findings=(_finding(zh=FZH), _finding(finding_id="f-2")), executive=EXEC)
    assert out.count("<style>") == 1
    assert out.count("<script") == 0


EVIDENCE = ("HTTP/1.1 200 OK\n<div id='x'>" + "A" * 80 + "</div>\n") * 250 + "TAIL-MARK-9f2c"


@pytest.mark.parametrize("zh", [None, FZH], ids=["no_zh", "with_zh"])
def test_evidence_is_complete_with_or_without_translation(zh: FindingZh | None) -> None:
    assert len(EVIDENCE) >= 20000
    f = _finding(raw=_raw_with("evidence", EVIDENCE), zh=zh)
    out = _render(findings=(f,))
    assert html.escape(EVIDENCE, quote=True) in out
    code_locations = json.dumps(RAW["code_locations"], ensure_ascii=False, indent=2)
    for text in (str(RAW["poc_script_code"]), code_locations, "/rest/user/login", "POST"):
        assert html.escape(text, quote=True) in out
    assert "CWE-89" in out
    assert "9.1" in out


def test_missing_or_empty_evidence_keys_emit_no_pre_block() -> None:
    raw = {k: v for k, v in RAW.items() if k != "code_locations"}
    raw.update(poc_description="", poc_script_code=None)
    out = _render(findings=(_finding(raw=raw),))
    assert out.count("<pre") == 1  # 只剩 evidence 一块


@pytest.mark.parametrize(
    ("status", "error_code", "exit_meaning"),
    [
        ("stopped", "scan_incomplete", "no_vulnerabilities_found"),
        ("completed", "coverage_incomplete", "no_vulnerabilities_found"),
        ("failed", "scan_failed", None),
        ("interrupted", "interrupted_by_restart", None),
        ("completed", None, "failed"),
        ("completed", None, None),
    ],
)
def test_incomplete_scan_never_claims_clean(
    status: str, error_code: str | None, exit_meaning: str | None
) -> None:
    scan = _scan(status=status, error_code=error_code, exit_meaning=exit_meaning)
    out = _render(scan)
    assert "结论不完整" in out
    for phrase in FORBIDDEN:
        assert phrase not in out


def test_complete_clean_scan_says_so() -> None:
    out = _render(_scan(exit_meaning="no_vulnerabilities_found"))
    assert "扫描完整跑完，没有发现漏洞。" in out
    assert "结论不完整" not in out


def test_complete_scan_with_findings_says_so() -> None:
    out = _render(_scan(exit_meaning="vulnerabilities_found"), (_finding(),))
    assert "扫描完整跑完，发现了漏洞" in out
    assert "结论不完整" not in out


def test_findings_sorted_by_bucket_then_cvss_then_id() -> None:
    specs = [
        ("low", 9.0, "t-low"),
        ("critical", None, "t-crit-none"),
        ("info", 10.0, "t-info"),
        ("high", 5.0, "t-high-5"),
        ("critical", 9.8, "t-crit-98"),
        ("high", 7.5, "t-high-75"),
    ]
    findings = tuple(
        _finding(finding_id=f"f-{i}", severity=sev, cvss=cvss, title=title)
        for i, (sev, cvss, title) in enumerate(specs)
    )
    out = _render(findings=findings)
    order = sorted((out.index(title), title) for _, _, title in specs)
    assert [title for _, title in order] == [
        "t-crit-98",
        "t-crit-none",
        "t-high-75",
        "t-high-5",
        "t-low",
        "t-info",
    ]


def test_cost_none_is_not_zero() -> None:
    assert "报告生成费用：无法计算" in _render(cost=None)
    assert "报告生成费用：$0.0123" in _render(cost=0.0123)


def test_empty_layman_analogy_is_hidden() -> None:
    assert "打个比方" in _render(findings=(_finding(zh=FZH),))
    hidden = dataclasses.replace(FZH, layman_analogy_zh="")
    assert "打个比方" not in _render(findings=(_finding(zh=hidden),))
