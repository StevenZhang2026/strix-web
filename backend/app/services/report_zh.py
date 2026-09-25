"""中文人话报告的纯判定层：喂给模型什么、prompt 怎么写、模型吐回来的 JSON 收不收。

无 IO、无模块级可变状态。调模型、查缓存、并发在 T21b，路由在 T21c。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields

LANG_ZH = "zh-CN"
EXECUTIVE_ID = "__executive__"

# 模型输入白名单。endpoint/method/cwe 只作上下文，不要求模型翻译它们。
# 必须是白名单不是黑名单：上游将来新增的未知键（可能就是原始请求/响应）默认不外发。
# poc_script_code / evidence / code_locations / poc_description 故意不在内：译错证据 = 伪造证据，
# 报告里的证据由渲染层直接取原文；顺带省掉最占 token 的那几块。
FINDING_INPUT_KEYS: tuple[str, ...] = (
    "title", "severity", "description", "impact", "technical_analysis", "remediation_steps",
    "cvss", "cvss_breakdown", "confidence", "assumptions", "counterevidence",
    "severity_change_conditions", "fix_effort", "cwe", "endpoint", "method",
)  # fmt: skip

# 词表：英文术语 → 中文。prompt 里给模型看，解析器用它拒绝未译输出。
GLOSSARY: tuple[tuple[str, str], ...] = (
    ("IDOR", "越权访问"), ("SSRF", "服务端请求伪造"), ("RCE", "远程命令执行"),
    ("SSTI", "模板注入"), ("CSRF", "跨站请求伪造"), ("BFLA", "接口权限缺失"),
    ("mass assignment", "参数批量赋值"),
)  # fmt: skip

# 边界不用 `\b`：`/api/csrf-token` 这种路径原样出现在正文里是合法的，`\b` 会误拒、白白重试。
_TERM_RE = re.compile(
    r"(?<![A-Za-z0-9_/.-])(?:"
    + "|".join(re.escape(en) for en, _ in GLOSSARY)
    + r")(?![A-Za-z0-9_/.-])",
    re.IGNORECASE,
)

Message = dict[str, str]


@dataclass(frozen=True, slots=True)
class FindingZh:
    title_zh: str
    what_zh: str
    impact_zh: str
    fix_zh: str
    severity_zh_label: str
    severity_reason_zh: str
    effort_zh: str
    who_fixes_zh: str
    confidence_zh: str
    layman_analogy_zh: str


@dataclass(frozen=True, slots=True)
class ExecutiveZh:
    summary_zh: str
    risk_verdict_zh: str
    top3_actions_zh: tuple[str, ...]
    scope_zh: str
    coverage_zh: str
    not_tested_zh: tuple[str, ...]


_FINDING_FIELDS: frozenset[str] = frozenset(f.name for f in fields(FindingZh))
_EXECUTIVE_FIELDS: frozenset[str] = frozenset(f.name for f in fields(ExecutiveZh))
_SUMMARY_MAX = 300


class PayloadRejected(ValueError):
    """模型输出不合格。`reason` 是稳定机器码（no_json/bad_keys/bad_value/untranslated_term），
    T21b 按它挑修复提示。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_REPAIR_PROMPTS: dict[str, str] = {
    "no_json": "Your reply was not a JSON object. Return ONLY one JSON object, nothing else.",
    "bad_keys": "Your JSON had missing or extra keys. Return exactly the keys requested, no more.",
    "bad_value": "Some values were empty or of the wrong type. Fix them and return the JSON again.",
    "untranslated_term": (
        "Some English terms were left untranslated. Use the given Chinese terms and return the JSON"
        " again."
    ),
}

_GLOSSARY_LINES = "\n".join(f"  - {en} -> {zh}" for en, zh in GLOSSARY)

_FINDING_SYSTEM = f"""\
You rewrite ONE penetration-test finding for non-technical business managers, in Simplified Chinese.
Rules:
- Use only facts present in the input. Never invent impact, data, numbers, systems or fixes.
- Never leave these English terms untranslated; always use the Chinese term given:
{_GLOSSARY_LINES}
- severity_reason_zh must cite the cvss score, cvss_breakdown or confidence from the input.
- If assumptions or counterevidence weaken the finding, confidence_zh must say plainly what still \
needs manual confirmation (需人工确认).
- title_zh: 8 to 20 Chinese characters. severity_zh_label: one of 严重 / 高危 / 中危 / 低危.
- Do not include code, payloads, commands or raw evidence.
Return ONLY one JSON object with exactly these string keys: \
{", ".join(f.name for f in fields(FindingZh))}.
layman_analogy_zh may be "" when no honest analogy exists; every other value must be non-empty."""

_EXECUTIVE_SYSTEM = f"""\
You write the executive summary of a penetration-test report for non-technical business managers, \
in Simplified Chinese.
Rules:
- Use only facts present in the input. Never invent findings, systems or numbers.
- Never leave these English terms untranslated; always use the Chinese term given:
{_GLOSSARY_LINES}
- not_tested_zh lists, one item per entry, what was NOT tested or needs follow-up, taken from \
coverage.gaps and coverage.completeness.caveats. Never omit an item to make the report look better.
- summary_zh: at most 300 Chinese characters. top3_actions_zh: 1 to 3 concrete actions.
Return ONLY one JSON object with exactly these keys: summary_zh, risk_verdict_zh, top3_actions_zh \
(array of strings), scope_zh, coverage_zh, not_tested_zh (array of strings)."""

_GAP_KEYS = ("kind", "surface", "risk_area", "detail")
_COMPLETENESS_KEYS = ("complete", "caveats")


def finding_input(raw: Mapping[str, object]) -> dict[str, object]:
    return {k: raw[k] for k in FINDING_INPUT_KEYS if k in raw}


def finding_messages(raw: Mapping[str, object]) -> list[Message]:
    user = json.dumps(finding_input(raw), ensure_ascii=False, indent=2)
    return [{"role": "system", "content": _FINDING_SYSTEM}, {"role": "user", "content": user}]


def _coverage_input(coverage: Mapping[str, object]) -> dict[str, object]:
    """覆盖度只取总述要用的几块；`entries` 不传 —— 大，且含证据原文。"""
    out: dict[str, object] = {}
    completeness = coverage.get("completeness")
    if isinstance(completeness, dict):
        out["completeness"] = {k: completeness[k] for k in _COMPLETENESS_KEYS if k in completeness}
    summary = coverage.get("summary")
    if isinstance(summary, dict):
        out["summary"] = summary
    gaps = coverage.get("gaps")
    if isinstance(gaps, list):
        out["gaps"] = [
            {k: gap[k] for k in _GAP_KEYS if k in gap} for gap in gaps if isinstance(gap, dict)
        ]
    return out


def executive_messages(
    report_md: str, severity_counts: Mapping[str, int], coverage: Mapping[str, object]
) -> list[Message]:
    body = {
        "report_markdown": report_md,
        "severity_counts": dict(severity_counts),
        "coverage": _coverage_input(coverage),
    }
    user = json.dumps(body, ensure_ascii=False, indent=2)
    return [{"role": "system", "content": _EXECUTIVE_SYSTEM}, {"role": "user", "content": user}]


def repair_messages(messages: list[Message], bad_text: str, reason: str) -> list[Message]:
    return [
        *messages,
        {"role": "assistant", "content": bad_text},
        {"role": "user", "content": _REPAIR_PROMPTS[reason]},
    ]


def _load_object(text: str) -> dict[str, object]:
    # 不单独剥 ```json 围栏：下面"第一个 { 到最后一个 }"的回退已经覆盖它（与前后闲话）。
    body = text.strip()
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        start, end = body.find("{"), body.rfind("}")
        try:
            data = json.loads(body[start : end + 1]) if 0 <= start < end else None
        except json.JSONDecodeError:
            data = None
    if not isinstance(data, dict):
        raise PayloadRejected("no_json")
    return data


def _nonempty_str(value: object) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _str_list(value: object) -> bool:
    return isinstance(value, list) and all(_nonempty_str(item) for item in value)


def _reject_untranslated(values: list[str]) -> None:
    if any(_TERM_RE.search(v) for v in values):
        raise PayloadRejected("untranslated_term")


def parse_finding(text: str) -> FindingZh:
    data = _load_object(text)
    if set(data) != _FINDING_FIELDS:
        raise PayloadRejected("bad_keys")
    for key, value in data.items():
        if not isinstance(value, str) or (key != "layman_analogy_zh" and not value.strip()):
            raise PayloadRejected("bad_value")
    values = {k: str(v) for k, v in data.items()}
    _reject_untranslated(list(values.values()))
    return FindingZh(**values)


def parse_executive(text: str, *, require_not_tested: bool) -> ExecutiveZh:
    data = _load_object(text)
    if set(data) != _EXECUTIVE_FIELDS:
        raise PayloadRejected("bad_keys")
    summary, verdict, scope, cov = (
        data["summary_zh"],
        data["risk_verdict_zh"],
        data["scope_zh"],
        data["coverage_zh"],
    )
    actions, not_tested = data["top3_actions_zh"], data["not_tested_zh"]
    if not (
        isinstance(summary, str)
        and isinstance(verdict, str)
        and isinstance(scope, str)
        and isinstance(cov, str)
        and all(_nonempty_str(v) for v in (summary, verdict, scope, cov))
        and len(summary) <= _SUMMARY_MAX
        and isinstance(actions, list)
        and _str_list(actions)
        and 1 <= len(actions) <= 3
        and isinstance(not_tested, list)
        and _str_list(not_tested)
        and (len(not_tested) >= 1 or not require_not_tested)
    ):
        raise PayloadRejected("bad_value")
    actions_t = tuple(str(a) for a in actions)
    not_tested_t = tuple(str(n) for n in not_tested)
    _reject_untranslated([summary, verdict, scope, cov, *actions_t, *not_tested_t])
    return ExecutiveZh(summary, verdict, actions_t, scope, cov, not_tested_t)
