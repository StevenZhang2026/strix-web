"""6 个扫描模板 —— 向导上那几个按钮到底给 Strix 下了什么指令。

**为什么是 Python 常量而不是 YAML**：这份数据每一项都被代码直接消费（`scan_mode` 进
`-m`、预算与轮数进 argv、`instruction_body` 进 tmpfs 的指令文件），改它必须同时改
`test_scan_templates.py` 的黄金表。放进 YAML 只会多一层"运行时才发现拼错了"的解析，
换不来任何可配置性 —— 用户并不改这些（PLAN.md §向导→CLI 映射）。

`instruction_body` **全英文**：Strix 的 system prompt 与 ~90 个 skill 都是英文，中文正文
会让它在两种语言之间来回翻译（CLAUDE.md §错误与文案）。模板的**中文名与说明**在
`frontend/messages/zh-CN.json`（T19），本文件一个中文字面量都不出。

`SHARED_TAIL` 逐字附在每一份指令末尾。它刻意**不写**"不得扩大授权范围"之类的话：
权威 scope 由 Strix 自己的 `build_scope_context` 注入，我们再写一遍只会让人误以为
护栏在 prompt 里 —— 而 prompt 是模型可以忽略的东西。
"""

from __future__ import annotations

from dataclasses import dataclass

# 6 份指令的共同结尾。中英双写是为了让报告可以直接给非英语的业务方看，
# 而禁测清单是硬要求：这些测试会造成不可逆后果，出了事没法"回滚一次扫描"。
SHARED_TAIL = """
## Reporting and safety rules (apply to every finding)

- For every finding, write `description`, `impact` and `remediation_steps` in BOTH
  Chinese and English, Chinese first, separated by a blank line.
- Keep `poc_script_code`, raw evidence, endpoints and code locations in their original
  form. Never translate or paraphrase them.
- Do NOT run denial-of-service, resource-exhaustion, data-destruction or
  account-lockout tests. That includes password spraying against real accounts,
  mass-deleting records, and flooding any endpoint to measure limits.
"""


@dataclass(frozen=True, slots=True)
class ScanTemplate:
    """一个模板的全部内容。

    预算与轮数是**默认值**，向导允许改（`LaunchSpec` 里带的才是最终值）；
    `scan_mode` 同理。放在这里是为了让"推荐配置"有一个单一出处。
    """

    template_id: str
    scan_mode: str
    default_budget_usd: float
    default_max_turns: int
    recommended: bool
    instruction_body: str


TEMPLATES: tuple[ScanTemplate, ...] = (
    ScanTemplate(
        template_id="quick_triage",
        scan_mode="quick",
        default_budget_usd=5.0,
        default_max_turns=60,
        recommended=False,
        instruction_body="""\
## Goal: time-boxed triage

You have a very small budget. Spend it on findings that are directly exploitable and
high impact, and stop as soon as the budget is nearly gone.

- Cover only these six classes: authentication bypass, broken access control on
  obvious object identifiers, SQL/NoSQL injection, reflected and stored XSS,
  server-side request forgery, and unauthenticated exposure of sensitive data.
- Skip subdomain enumeration and directory/parameter brute forcing entirely. They
  burn the whole budget before any request is analysed.
- Prefer one careful probe per candidate over broad fuzzing.
- If you run out of budget with work left, say so explicitly in the summary and list
  what was not covered.""",
    ),
    ScanTemplate(
        template_id="full_review",
        scan_mode="standard",
        default_budget_usd=25.0,
        default_max_turns=200,
        recommended=True,
        instruction_body="""\
## Goal: systematic review of the whole web surface

Work in two phases and do not skip the first one.

1. Enumerate the application: every page, form, API endpoint and file upload, plus the
   roles the application supports (anonymous, authenticated user, admin, tenant).
   Write that inventory down before you start testing.
2. Walk the OWASP Top 10 against every entry point you found: broken access control,
   cryptographic failures, injection, insecure design, security misconfiguration,
   vulnerable components, authentication failures, integrity failures, logging
   failures, and SSRF.

- Track coverage as you go: for each entry point record which classes you tested, so
  the final report can state what was and was not covered.
- When a finding is confirmed, capture a minimal reproducible proof of concept before
  moving on.""",
    ),
    ScanTemplate(
        template_id="deep_audit",
        scan_mode="deep",
        default_budget_usd=80.0,
        default_max_turns=500,
        recommended=False,
        instruction_body="""\
## Goal: deep audit with attack chains

- Split the application by functional domain (authentication, billing, file handling,
  admin tooling, integrations) and delegate each domain to a subagent so that domains
  are explored in parallel and in depth.
- Do not stop at isolated primitives. Chain them: use an information leak to find an
  identifier, use that identifier for an access-control bypass, use the bypass to
  reach an internal service, and so on.
- For every chain, report the full path with the concrete requests that make each hop
  work, and state the impact of the end of the chain, not of the first step.
- Revisit anything the earlier phases marked as "probably not exploitable" once you
  have new primitives. Most real chains come from re-reading old notes.
- Prefer depth on a promising lead over starting a new domain when the budget is
  running low.""",
    ),
    ScanTemplate(
        template_id="auth_and_access",
        scan_mode="standard",
        default_budget_usd=15.0,
        default_max_turns=120,
        recommended=False,
        instruction_body="""\
## Goal: authentication, session and access control only

Cover, in this order: login, registration, password reset and recovery, email or phone
change, multi-factor enrolment and verification, session lifecycle (fixation,
invalidation on logout and on password change), token handling (JWT algorithm and
signature confusion, expiry, audience), and both horizontal and vertical privilege
escalation.

- Do NOT spend turns on XSS, injection or other input-validation classes. If you
  stumble on one, note it in one line and move on.
- Create your own accounts for every role you need. Never test against a real user's
  account, and never repeat failed logins against an account you did not create:
  lockout is a denial of service.
- For every access-control finding, show the request that succeeds as the low
  privilege identity and the field or record it should not have reached.""",
    ),
    ScanTemplate(
        template_id="api_surface",
        scan_mode="standard",
        default_budget_usd=25.0,
        default_max_turns=200,
        recommended=False,
        instruction_body="""\
## Goal: API surface coverage

- Discover the API surface from the running application: client-side bundles, network
  traffic, well-known specification paths, error messages and version prefixes.
- Enumerate every operation you can identify (method plus path plus parameters) and
  test each one for authentication, authorisation, input validation and mass
  assignment.
- Report two lists explicitly, because they are what the API owner cannot get from a
  normal scan: operations that are documented or referenced but not reachable, and
  operations that exist but are not documented anywhere.
- Pay attention to object identifiers in paths and bodies: enumerable identifiers plus
  a missing ownership check is the most common real finding here.""",
    ),
    ScanTemplate(
        template_id="pre_release_recheck",
        scan_mode="standard",
        default_budget_usd=12.0,
        default_max_turns=100,
        recommended=False,
        instruction_body="""\
## Goal: pre-release regression check

- The operator's notes below describe what changed and which findings were supposedly
  fixed. Focus on exactly that.
- For every previously reported issue, actually re-run the proof of concept. Report
  "fixed" only when the original request no longer works, and say how you verified it.
  A code change you cannot observe from the outside is not a verified fix.
- Check whether the fix moved the problem instead of removing it: a filter added on
  one endpoint while a second endpoint still reaches the same code path.
- Test the changed features for new issues too, but keep the scope to the change. Do
  not start a full review.""",
    ),
)


def template_for(template_id: str) -> ScanTemplate | None:
    """按 id 查模板。查不到返回 `None`，**不抛异常**。

    与 `llm_client.spec_for()` 同样的选择：只有调用方知道"没查到"该映射成哪个错误码
    （launcher 把它变成 422 `invalid_request`，路由层可能有别的处理）。
    """
    for template in TEMPLATES:
        if template.template_id == template_id:
            return template
    return None
