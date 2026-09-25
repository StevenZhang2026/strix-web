"""打印版 HTML 报告的纯渲染函数：入参是已取好的数据，出参是一份自包含 HTML 字符串。

为什么只有一个渲染器：前端报告 tab 用 `<iframe sandbox>` 嵌这同一份 HTML，「导出 PDF」也是打开它按 ⌘P。
如果 React 再画一份，两边的结论措辞、排序、证据取舍迟早漂移，而管理层拿到的 PDF 和屏幕上看到的必须是同一份。

为什么零 JS：正文里有目标站点可控的内容（endpoint、evidence、模型转述的响应片段）和 LLM 输出，全部是不可信输入。
不输出任何脚本、外链、内联事件，所有入参只以转义后的文本节点出现、从不进属性和 CSS，
配合 `default-src 'none'` 的 CSP，即使转义漏了一处也没有能执行的东西。

为什么证据不经译文：poc/evidence/code_locations 是复现与修复的依据，翻译会改写请求、代码与行号；
所以它们只从 Strix 原始记录 `raw` 取、原样全文附在每条下面，有没有中文版都附。
"""

from __future__ import annotations

import html
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from app.services.report_zh import ExecutiveZh, FindingZh
from app.services.scan_persist import SEVERITY_BUCKETS


@dataclass(frozen=True, slots=True)
class ReportScan:
    scan_id: str
    targets: tuple[str, ...]
    template_id: str
    status: str
    exit_meaning: str | None
    error_code: str | None
    started_at: str | None
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class ReportFinding:
    finding_id: str
    severity: str
    title: str
    cvss: float | None
    cwe: str | None
    endpoint: str | None
    method: str | None
    raw: Mapping[str, object]
    zh: FindingZh | None


_CSS: Final = """
@page { size: A4; margin: 18mm 16mm; }
body {
  font-family: "PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans CJK SC",sans-serif;
  font-size: 11pt; line-height: 1.6; color: #222; max-width: 960px; margin: 0 auto; padding: 16px;
}
h1 { font-size: 20pt; margin: 0 0 8px; }
h2 { font-size: 14pt; border-bottom: 1px solid #ccc; padding-bottom: 4px; margin-top: 24px; }
h3 { font-size: 12pt; margin: 0 0 8px; }
h4 { font-size: 11pt; margin: 12px 0 4px; }
h5 { font-size: 10pt; margin: 8px 0 2px; color: #555; }
.text, li, td { white-space: pre-wrap; }
pre {
  white-space: pre-wrap; word-break: break-all; background: #f6f6f6;
  padding: 8px; font-size: 9pt; border-radius: 4px;
}
.verdict { padding: 12px; border: 2px solid #999; border-radius: 4px; }
.verdict.incomplete { border-color: #d97706; background: #fffbeb; }
.finding { break-inside: avoid; border-left: 6px solid #999; padding: 8px 12px; margin: 16px 0; }
.sev-critical { border-left-color: #7f1d1d; }
.sev-high { border-left-color: #dc2626; }
.sev-medium { border-left-color: #f59e0b; }
.sev-low { border-left-color: #2563eb; }
.sev-other { border-left-color: #9ca3af; }
.tag { font-size: 9pt; border: 1px solid #999; border-radius: 3px; padding: 0 4px; margin-left: 8px; }
.kv { font-family: monospace; font-size: 9pt; }
.noprint { color: #666; }
@media print { .noprint { display: none; } }
"""

# 严重度 → (class, 中文)。class 只能从这张表取，不在表里的一律 sev-other：入参值绝不进属性。
_SEVERITY: Final = MappingProxyType(
    {
        "critical": ("sev-critical", "严重"),
        "high": ("sev-high", "高危"),
        "medium": ("sev-medium", "中危"),
        "low": ("sev-low", "低危"),
    }
)

EVIDENCE_KEYS: Final = ("poc_description", "poc_script_code", "evidence", "code_locations")

_VERDICT_FOUND: Final = "扫描完整跑完，发现了漏洞，逐条见下文。"
_VERDICT_CLEAN: Final = "扫描完整跑完，没有发现漏洞。"
INCOMPLETE_TITLE: Final = "结论不完整"
INCOMPLETE_BODY: Final = (
    "这次扫描没有跑完，或它的覆盖记录显示有部分没测完。已经找到的问题都还在，"
    "但没测到的部分无从判断 —— 这次结果不能当作「目标没有问题」。"
)
EMPTY_FINDINGS: Final = "这次扫描没有记录到发现条目。"
NO_ZH_TAG: Final = "未生成中文版"
TECH_HEADING: Final = "技术细节（原文，未翻译）"

# 总述的小标题：HTML 与 Word 两个渲染器共用，措辞只有这一份源头。
EXEC_TITLE: Final = "总述"
EXEC_MISSING: Final = "尚未生成中文总述。"
EXEC_SUMMARY: Final = "总述"
EXEC_RISK: Final = "风险判断"
EXEC_TOP3: Final = "优先做的三件事"
EXEC_SCOPE: Final = "测试范围"
EXEC_COVERAGE: Final = "覆盖情况"
EXEC_NOT_TESTED: Final = "未测试／需跟进"
EXEC_NOT_LISTED: Final = "（未列出）"


def verdict_sentence(scan: ReportScan) -> str | None:
    """完整跑完才给结论句；返回 None 时调用方必须渲染「结论不完整」。"""
    # 必须同时看 status 与 error_code：coverage_incomplete 是 status=completed 但覆盖记录显示没测完，
    # 只看 status 会把它说成「完整跑完、没有发现漏洞」—— 这是把没测到的部分当成没问题（发布阻断）。
    complete = scan.status == "completed" and scan.error_code is None
    if complete and scan.exit_meaning == "vulnerabilities_found":
        return _VERDICT_FOUND
    if complete and scan.exit_meaning == "no_vulnerabilities_found":
        return _VERDICT_CLEAN
    return None


def severity_label(severity: str) -> str:
    entry = _SEVERITY.get(severity)
    return entry[1] if entry is not None else ""


def _sort_key(f: ReportFinding) -> tuple[int, tuple[int, float], str]:
    rank = (
        SEVERITY_BUCKETS.index(f.severity)
        if f.severity in SEVERITY_BUCKETS
        else len(SEVERITY_BUCKETS)
    )
    cvss = (0, -f.cvss) if f.cvss is not None else (1, 0.0)
    return (rank, cvss, f.finding_id)


def sorted_findings(findings: Sequence[ReportFinding]) -> list[ReportFinding]:
    return sorted(findings, key=_sort_key)


def finding_sections(f: ReportFinding) -> list[tuple[str, str]]:
    """每条发现的 (小标题, 正文)：有译文用译文，没有就用原文里非空的那几项。"""
    zh = f.zh
    if zh is not None:
        sections = [
            ("问题是什么", zh.what_zh),
            ("影响", zh.impact_zh),
            ("怎么修", zh.fix_zh),
            ("严重度依据", zh.severity_reason_zh),
            ("修复工作量", zh.effort_zh),
            ("谁来修", zh.who_fixes_zh),
            ("可信度", zh.confidence_zh),
        ]
        if zh.layman_analogy_zh:
            sections.append(("打个比方", zh.layman_analogy_zh))
        return sections
    sections = []
    for key, heading in (
        ("description", "描述"),
        ("impact", "影响"),
        ("remediation_steps", "修复"),
    ):
        value = f.raw.get(key)
        if isinstance(value, str) and value:
            sections.append((heading, value))
    return sections


def evidence_blocks(f: ReportFinding) -> list[tuple[str, str]]:
    """证据只从 raw 取、全文不截断；非字符串（如 code_locations）按 JSON 缩进展开。"""
    blocks = []
    for key in EVIDENCE_KEYS:
        value = f.raw.get(key)
        if value is None or value == "":
            continue
        body = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        blocks.append((key, body))
    return blocks


def cost_line(report_cost_usd: float | None) -> str:
    if report_cost_usd is None:
        return "报告生成费用：无法计算（有调用没有返回计费信息）"
    return f"报告生成费用：${report_cost_usd:.4f}"


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _or_dash(value: object | None) -> str:
    return "—" if value is None else _e(value)


def _text(out: list[str], label: str, value: str) -> None:
    out.append(f'<h4>{label}</h4><div class="text">{_e(value)}</div>')


def _render_header(scan: ReportScan, out: list[str]) -> None:
    out.append("<h1>渗透测试报告</h1><table>")
    targets = "".join(f"<li>{_e(t)}</li>" for t in scan.targets)
    out.append(f"<tr><th>目标</th><td><ul>{targets}</ul></td></tr>")
    out.append(f"<tr><th>模板</th><td>{_e(scan.template_id)}</td></tr>")
    out.append(f"<tr><th>开始时间</th><td>{_or_dash(scan.started_at)}</td></tr>")
    out.append(f"<tr><th>结束时间</th><td>{_or_dash(scan.finished_at)}</td></tr>")
    out.append(f"<tr><th>扫描编号</th><td>{_e(scan.scan_id)}</td></tr></table>")


def _render_verdict(scan: ReportScan, out: list[str]) -> None:
    sentence = verdict_sentence(scan)
    if sentence is not None:
        out.append(f'<div class="verdict"><h2>结论</h2><p>{sentence}</p></div>')
    else:
        status_line = f"状态：{_e(scan.status)}；原因码：{_or_dash(scan.error_code)}"
        out.append(
            f'<div class="verdict incomplete"><h2>{INCOMPLETE_TITLE}</h2>'
            f"<p>{INCOMPLETE_BODY}</p><p>{status_line}</p></div>"
        )


def _render_executive(executive: ExecutiveZh | None, out: list[str]) -> None:
    out.append(f"<h2>{EXEC_TITLE}</h2>")
    if executive is None:
        out.append(f"<p>{EXEC_MISSING}</p>")
        return
    _text(out, EXEC_SUMMARY, executive.summary_zh)
    _text(out, EXEC_RISK, executive.risk_verdict_zh)
    actions = "".join(f"<li>{_e(a)}</li>" for a in executive.top3_actions_zh)
    out.append(f"<h4>{EXEC_TOP3}</h4><ol>{actions}</ol>")
    _text(out, EXEC_SCOPE, executive.scope_zh)
    _text(out, EXEC_COVERAGE, executive.coverage_zh)
    not_tested = "".join(f"<li>{_e(n)}</li>" for n in executive.not_tested_zh)
    out.append(f"<h4>{EXEC_NOT_TESTED}</h4><ul>{not_tested or f'<li>{EXEC_NOT_LISTED}</li>'}</ul>")


def _render_finding(f: ReportFinding, out: list[str]) -> None:
    sev_class, sev_label = _SEVERITY.get(f.severity, ("sev-other", ""))
    out.append(f'<section class="finding {sev_class}">')
    if f.zh is not None:
        out.append(
            f'<h3>{_e(f.zh.title_zh)}<span class="tag">{_e(f.zh.severity_zh_label)}</span></h3>'
        )
    else:
        label = sev_label or _e(f.severity)
        out.append(
            f'<h3>{_e(f.title)}<span class="tag">{label}</span>'
            f'<span class="tag">{NO_ZH_TAG}</span></h3>'
        )
    for heading, body in finding_sections(f):
        _text(out, heading, body)
    out.append(f'<div class="tech"><h4>{TECH_HEADING}</h4>')
    out.append(
        f'<p class="kv">endpoint：{_or_dash(f.endpoint)}　method：{_or_dash(f.method)}　'
        f"CWE：{_or_dash(f.cwe)}　CVSS：{_or_dash(f.cvss)}</p>"
    )
    for key, body in evidence_blocks(f):
        out.append(f"<h5>{key}</h5><pre>{_e(body)}</pre>")
    out.append("</div></section>")


def render_report_html(
    scan: ReportScan,
    findings: Sequence[ReportFinding],
    executive: ExecutiveZh | None,
    report_cost_usd: float | None,
) -> str:
    out: list[str] = [
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
        f"<title>渗透测试报告</title><style>{_CSS}</style></head><body>",
        '<p class="noprint">按 ⌘P / Ctrl+P 存为 PDF。</p>',
    ]
    _render_header(scan, out)
    _render_verdict(scan, out)
    _render_executive(executive, out)
    out.append("<h2>发现列表</h2>")
    if not findings:
        out.append(f"<p>{EMPTY_FINDINGS}</p>")
    for f in sorted_findings(findings):
        _render_finding(f, out)
    out.append(f"<footer><p>{cost_line(report_cost_usd)}</p></footer></body></html>")
    return "".join(out)
