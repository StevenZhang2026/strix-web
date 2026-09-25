"""Word 版报告：I1 任意输入都产出良构文档、I2 证据原样，外加接线。"""

from __future__ import annotations

import io
import json
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, replace

import pytest

from app.services.exporter_docx import render_report_docx
from app.services.exporter_html import ReportFinding, ReportScan
from app.services.report_zh import ExecutiveZh, FindingZh

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

_VERDICT_FOUND = "扫描完整跑完，发现了漏洞，逐条见下文。"
_VERDICT_CLEAN = "扫描完整跑完，没有发现漏洞。"

# `"` 放在结构串之后：code_locations 经 json.dumps 会把它转义成 `\"`，前半段仍须原样出现。
_INJECT = '</w:t></w:r></w:p><w:p>&<]]>"\x01\x0b\ud800'
_STRUCT = "</w:t></w:r></w:p><w:p>&<]]>"
_SANITIZED = '</w:t></w:r></w:p><w:p>&<]]>"\ufffd\ufffd\ufffd'


def _scan(**kw: object) -> ReportScan:
    base = ReportScan(
        scan_id="scan-1",
        targets=("http://juice-shop:3000",),
        template_id="quick",
        status="completed",
        exit_meaning="vulnerabilities_found",
        error_code=None,
        started_at="2026-09-01T00:00:00Z",
        finished_at="2026-09-01T01:00:00Z",
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def _raw(**kw: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "description": "desc",
        "impact": "imp",
        "remediation_steps": "fix",
        "poc_description": "poc desc",
        "poc_script_code": "print(1)",
        "evidence": "GET / HTTP/1.1",
        "code_locations": [{"file": "routes/login.ts", "start_line": 34}],
    }
    raw.update(kw)
    return raw


def _zh(**kw: str) -> FindingZh:
    base = FindingZh(
        title_zh="SQL 注入",
        what_zh="什么",
        impact_zh="影响",
        fix_zh="修复",
        severity_zh_label="严重",
        severity_reason_zh="依据",
        effort_zh="低",
        who_fixes_zh="后端",
        confidence_zh="高",
        layman_analogy_zh="比方",
    )
    return replace(base, **kw)


def _finding(**kw: object) -> ReportFinding:
    base = ReportFinding(
        finding_id="vuln-0001",
        severity="critical",
        title="SQL injection",
        cvss=9.1,
        cwe="CWE-89",
        endpoint="/rest/user/login",
        method="POST",
        raw=_raw(),
        zh=None,
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def _executive(**kw: object) -> ExecutiveZh:
    base = ExecutiveZh(
        summary_zh="总述正文",
        risk_verdict_zh="高风险",
        top3_actions_zh=("修注入",),
        scope_zh="范围",
        coverage_zh="覆盖",
        not_tested_zh=("未测",),
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def _paragraphs(data: bytes) -> list[str]:
    """按 Word 的读法抽回每个段落的文本。"""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))  # noqa: S314 自己刚生成的文档，无外部实体
    out = []
    for p in root.iter(f"{_W}p"):
        parts = []
        for el in p.iter():
            if el.tag == f"{_W}t":
                text = el.text or ""
                # Word 会把 w:t 里的字面换行／制表折成空格、没有 preserve 时吞首尾空格：出现即等于改写了证据。
                assert el.get(_XML_SPACE) == "preserve", text
                assert not {"\n", "\r", "\t"} & set(text), repr(text)
                parts.append(text)
            elif el.tag == f"{_W}br":
                parts.append("\n")
            elif el.tag == f"{_W}tab":
                parts.append("\t")
        out.append("".join(parts))
    return out


Case = Callable[[str], tuple[ReportScan, list[ReportFinding], ExecutiveZh | None]]


def _raw_case(key: str) -> Case:
    return lambda v: (_scan(), [_finding(raw=_raw(**{key: v}))], None)


def _zh_case(key: str) -> Case:
    return lambda v: (_scan(), [_finding(zh=_zh(**{key: v}))], None)


def _exec_case(key: str, as_tuple: bool = False) -> Case:
    return lambda v: (_scan(), [_finding()], _executive(**{key: (v,) if as_tuple else v}))


_CASES: dict[str, tuple[Case, bool]] = {
    **{f"raw.{k}": (_raw_case(k), False) for k in _raw() if k != "code_locations"},
    "raw.code_locations": (
        lambda v: (_scan(), [_finding(raw=_raw(code_locations=[{"f": v}]))], None),
        True,
    ),
    **{f"zh.{k}": (_zh_case(k), False) for k in FindingZh.__dataclass_fields__},
    "exec.summary_zh": (_exec_case("summary_zh"), False),
    "exec.risk_verdict_zh": (_exec_case("risk_verdict_zh"), False),
    "exec.scope_zh": (_exec_case("scope_zh"), False),
    "exec.coverage_zh": (_exec_case("coverage_zh"), False),
    "exec.top3_actions_zh": (_exec_case("top3_actions_zh", as_tuple=True), False),
    "exec.not_tested_zh": (_exec_case("not_tested_zh", as_tuple=True), False),
    "target": (lambda v: (_scan(targets=(v,)), [_finding()], None), False),
    "scan.status": (lambda v: (_scan(status=v), [_finding()], None), False),
    "scan.error_code": (lambda v: (_scan(error_code=v), [_finding()], None), False),
    "title": (lambda v: (_scan(), [_finding(title=v)], None), False),
    "endpoint": (lambda v: (_scan(), [_finding(endpoint=v)], None), False),
    "method": (lambda v: (_scan(), [_finding(method=v)], None), False),
}


@pytest.mark.parametrize("name", list(_CASES))
def test_i1_injection_stays_text_and_document_stays_well_formed(name: str) -> None:
    case, is_json = _CASES[name]
    baseline = _paragraphs(render_report_docx(*case("x"), None))
    paragraphs = _paragraphs(render_report_docx(*case(_INJECT), None))
    assert len(paragraphs) == len(baseline)
    text = "\n".join(paragraphs)
    assert _STRUCT in text
    assert "\ufffd" in text
    assert not {"\x01", "\x0b", "\ud800"} & set(text)
    if not is_json:
        assert _SANITIZED in text


_POC = "```python\r\n    import  requests\r\n\tx =\t1  \rend\n  trailing  \n```"


def test_i2_evidence_round_trips_verbatim() -> None:
    locations = [{"file": "routes/login.ts", "start_line": 34, "end_line": 36}]
    evidence = 'POST /rest/user/login HTTP/1.1\r\nHost: x\r\n\r\n{"email": "\' OR 1=1--"}'
    f = _finding(
        endpoint="/a  b\tc",
        raw=_raw(poc_script_code=_POC, evidence=evidence, code_locations=locations),
    )
    paragraphs = _paragraphs(render_report_docx(_scan(), [f], None, None))
    assert _POC.replace("\r\n", "\n").replace("\r", "\n") in paragraphs
    assert evidence.replace("\r\n", "\n") in paragraphs
    assert json.dumps(locations, ensure_ascii=False, indent=2) in paragraphs
    assert any("endpoint：/a  b\tc　" in p for p in paragraphs)


@dataclass(frozen=True)
class _ZhWithFakeEvidence(FindingZh):
    poc_script_code: str = "FAKE_FROM_TRANSLATION"
    evidence: str = "FAKE_EVIDENCE_FROM_TRANSLATION"


def test_i2_evidence_never_taken_from_translation() -> None:
    zh = _ZhWithFakeEvidence(**{k: "z" for k in FindingZh.__dataclass_fields__})
    text = "\n".join(_paragraphs(render_report_docx(_scan(), [_finding(zh=zh)], None, None)))
    assert "FAKE" not in text
    assert "print(1)" in text


def test_package_has_exactly_three_parts_and_is_deterministic() -> None:
    args = (_scan(), [_finding()], _executive(), 0.5)
    data = render_report_docx(*args)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert sorted(zf.namelist()) == ["[Content_Types].xml", "_rels/.rels", "word/document.xml"]
        types = zf.read("[Content_Types].xml").decode()
    assert (
        'PartName="/word/document.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"'
    ) in types
    assert render_report_docx(*args) == data


@pytest.mark.parametrize(
    "scan",
    [_scan(status="stopped"), _scan(error_code="coverage_incomplete")],
    ids=["stopped", "coverage_incomplete"],
)
def test_incomplete_scan_never_states_a_verdict(scan: ReportScan) -> None:
    text = "\n".join(_paragraphs(render_report_docx(scan, [_finding()], None, None)))
    assert "结论不完整" in text
    assert _VERDICT_FOUND not in text
    assert _VERDICT_CLEAN not in text


def test_translated_finding() -> None:
    f = _finding(zh=_zh())
    paragraphs = _paragraphs(render_report_docx(_scan(), [f], _executive(), None))
    text = "\n".join(paragraphs)
    assert _VERDICT_FOUND in paragraphs
    assert "SQL 注入" in text and "[严重]" in text
    assert "比方" in paragraphs and "总述正文" in paragraphs
    assert "未生成中文版" not in text


def test_untranslated_finding() -> None:
    text = "\n".join(_paragraphs(render_report_docx(_scan(), [_finding()], None, None)))
    assert "SQL injection" in text and "[严重]" in text and "[未生成中文版]" in text
    assert "尚未生成中文总述。" in text


@pytest.mark.parametrize(
    ("cost", "expected"),
    [
        (None, "报告生成费用：无法计算（有调用没有返回计费信息）"),
        (0.01234, "报告生成费用：$0.0123"),
    ],
)
def test_cost_line(cost: float | None, expected: str) -> None:
    assert _paragraphs(render_report_docx(_scan(), [], None, cost))[-1] == expected
