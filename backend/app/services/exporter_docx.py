"""Word 版报告的纯渲染函数：入参与打印版 HTML 完全相同，出参是一份最小 `.docx` 的字节。

为什么手写而不引 `python-docx`：我们只要段落、加粗、字号、换行、制表这几样，最小包只有 3 个部件，
几十行字符串拼接就够；引库要带上 `lxml`（C 扩展、要进 hash lock、两种架构都得有 wheel），换来的能力用不上。

为什么措辞从 `exporter_html` 取：结论判据、排序、证据取舍、所有小标题都只在那边有一份源头，
Word 版和 PDF 版给管理层的必须是同一份话。

为什么非法字符要替换：正文里有目标站点可控的内容与 LLM 输出，XML 1.0 不允许的字符（控制字符、孤立代理）
一旦进了 `document.xml`，Word 直接打不开文件，孤立代理甚至让 `.encode("utf-8")` 抛异常 —— 一律换成 U+FFFD。
所有入参只经 `_text_xml` 以文本节点出现，绝不进属性。

为什么证据不经译文：poc/evidence/code_locations 是复现与修复的依据，只从 `raw` 取（`evidence_blocks`）；
Word 会把 `w:t` 里的字面换行与制表折成空格、吞掉首尾空格，所以换行写成 `<w:br/>`、制表写成 `<w:tab/>`、
`w:t` 一律 `xml:space="preserve"`，否则请求报文与代码缩进会被悄悄改写。
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Sequence
from typing import Final
from xml.sax.saxutils import escape

from app.services.exporter_html import (
    EMPTY_FINDINGS,
    EXEC_COVERAGE,
    EXEC_MISSING,
    EXEC_NOT_LISTED,
    EXEC_NOT_TESTED,
    EXEC_RISK,
    EXEC_SCOPE,
    EXEC_SUMMARY,
    EXEC_TITLE,
    EXEC_TOP3,
    INCOMPLETE_BODY,
    INCOMPLETE_TITLE,
    NO_ZH_TAG,
    TECH_HEADING,
    ReportFinding,
    ReportScan,
    cost_line,
    evidence_blocks,
    finding_sections,
    severity_label,
    sorted_findings,
    verdict_sentence,
)
from app.services.report_zh import ExecutiveZh

_CONTENT_TYPES: Final = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/'
    'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
)
_RELS: Final = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/officeDocument" Target="word/document.xml"/></Relationships>'
)
_DOC_HEAD: Final = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
)
# A4；页边距 18mm／16mm 与打印版 HTML 的 @page 一致。
_DOC_TAIL: Final = (
    '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1020" w:right="907" '
    'w:bottom="1020" w:left="907" w:header="0" w:footer="0" w:gutter="0"/></w:sectPr>'
    "</w:body></w:document>"
)
# 固定时间戳：同样输入同样字节，也不把生成时间泄进文件。
_ZIP_DATE: Final = (1980, 1, 1, 0, 0, 0)

_ILLEGAL_XML: Final = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
_NEWLINE: Final = re.compile(r"\r\n|\r|\n")

# 字号单位是半磅：40=20pt、28=14pt、24=12pt、22=11pt、18=9pt，与打印版 HTML 的 CSS 对应。
_SZ_H1: Final = 40
_SZ_H2: Final = 28
_SZ_H3: Final = 24
_SZ_BODY: Final = 22
_SZ_H5: Final = 20
_SZ_MONO: Final = 18

_REPORT_TITLE: Final = "渗透测试报告"
_VERDICT_TITLE: Final = "结论"
_FINDINGS_TITLE: Final = "发现列表"


def _text_xml(text: str) -> str:
    """文本进 XML 的唯一入口：先换掉非法字符，再把换行／制表拆成 Word 认的元素。"""
    clean = _ILLEGAL_XML.sub("\ufffd", text)
    lines = []
    for line in _NEWLINE.split(clean):
        cells = [f'<w:t xml:space="preserve">{escape(cell)}</w:t>' for cell in line.split("\t")]
        lines.append("<w:tab/>".join(cells))
    return "<w:br/>".join(lines)


def _run(text: str, *, bold: bool = False, size: int = _SZ_BODY, mono: bool = False) -> str:
    fonts = ' w:ascii="Courier New" w:hAnsi="Courier New"' if mono else ""
    rpr = f'<w:rPr><w:rFonts{fonts} w:eastAsia="PingFang SC"/>{"<w:b/>" if bold else ""}'
    rpr += f'<w:sz w:val="{size}"/></w:rPr>'
    return f"<w:r>{rpr}{_text_xml(text)}</w:r>"


def _para(*runs: str, shade: bool = False, after: int = 80) -> str:
    shd = '<w:shd w:val="clear" w:color="auto" w:fill="F6F6F6"/>' if shade else ""
    return f'<w:p><w:pPr>{shd}<w:spacing w:after="{after}"/></w:pPr>{"".join(runs)}</w:p>'


def _heading(text: str, size: int) -> str:
    return _para(_run(text, bold=True, size=size), after=120)


def _dash(value: object | None) -> str:
    return "—" if value is None else str(value)


def _header(scan: ReportScan, out: list[str]) -> None:
    out.append(_heading(_REPORT_TITLE, _SZ_H1))
    for target in scan.targets:
        out.append(_para(_run(f"目标：{target}")))
    out.append(_para(_run(f"模板：{scan.template_id}")))
    out.append(_para(_run(f"开始时间：{_dash(scan.started_at)}")))
    out.append(_para(_run(f"结束时间：{_dash(scan.finished_at)}")))
    out.append(_para(_run(f"扫描编号：{scan.scan_id}")))


def _verdict(scan: ReportScan, out: list[str]) -> None:
    sentence = verdict_sentence(scan)
    if sentence is not None:
        out.append(_heading(_VERDICT_TITLE, _SZ_H2))
        out.append(_para(_run(sentence)))
        return
    out.append(_heading(INCOMPLETE_TITLE, _SZ_H2))
    out.append(_para(_run(INCOMPLETE_BODY)))
    out.append(_para(_run(f"状态：{scan.status}；原因码：{_dash(scan.error_code)}")))


def _section(out: list[str], heading: str, body: str) -> None:
    out.append(_para(_run(heading, bold=True), after=40))
    out.append(_para(_run(body)))


def _executive(executive: ExecutiveZh | None, out: list[str]) -> None:
    out.append(_heading(EXEC_TITLE, _SZ_H2))
    if executive is None:
        out.append(_para(_run(EXEC_MISSING)))
        return
    _section(out, EXEC_SUMMARY, executive.summary_zh)
    _section(out, EXEC_RISK, executive.risk_verdict_zh)
    out.append(_para(_run(EXEC_TOP3, bold=True), after=40))
    for i, action in enumerate(executive.top3_actions_zh, start=1):
        out.append(_para(_run(f"{i}. {action}")))
    _section(out, EXEC_SCOPE, executive.scope_zh)
    _section(out, EXEC_COVERAGE, executive.coverage_zh)
    out.append(_para(_run(EXEC_NOT_TESTED, bold=True), after=40))
    for item in executive.not_tested_zh or (EXEC_NOT_LISTED,):
        out.append(_para(_run(f"・{item}")))


def _finding(f: ReportFinding, out: list[str]) -> None:
    if f.zh is not None:
        tags = [f.zh.severity_zh_label]
        title = f.zh.title_zh
    else:
        tags = [severity_label(f.severity) or f.severity, NO_ZH_TAG]
        title = f.title
    tag_runs = [_run(f"　[{tag}]", size=_SZ_H5) for tag in tags]
    out.append(_para(_run(title, bold=True, size=_SZ_H3), *tag_runs, after=120))
    for heading, body in finding_sections(f):
        _section(out, heading, body)
    out.append(_para(_run(TECH_HEADING, bold=True), after=40))
    kv = (
        f"endpoint：{_dash(f.endpoint)}　method：{_dash(f.method)}　"
        f"CWE：{_dash(f.cwe)}　CVSS：{_dash(f.cvss)}"
    )
    out.append(_para(_run(kv, size=_SZ_MONO, mono=True)))
    for key, body in evidence_blocks(f):
        out.append(_para(_run(key, bold=True, size=_SZ_H5), after=40))
        out.append(_para(_run(body, size=_SZ_MONO, mono=True), shade=True))


def render_report_docx(
    scan: ReportScan,
    findings: Sequence[ReportFinding],
    executive: ExecutiveZh | None,
    report_cost_usd: float | None,
) -> bytes:
    out: list[str] = [_DOC_HEAD]
    _header(scan, out)
    _verdict(scan, out)
    _executive(executive, out)
    out.append(_heading(_FINDINGS_TITLE, _SZ_H2))
    if not findings:
        out.append(_para(_run(EMPTY_FINDINGS)))
    for f in sorted_findings(findings):
        _finding(f, out)
    out.append(_para(_run(cost_line(report_cost_usd))))
    out.append(_DOC_TAIL)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in (
            ("[Content_Types].xml", _CONTENT_TYPES),
            ("_rels/.rels", _RELS),
            ("word/document.xml", "".join(out)),
        ):
            info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, content.encode("utf-8"))
    return buf.getvalue()
