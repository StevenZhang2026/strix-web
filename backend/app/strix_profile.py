"""`strix-agent` 的**上游事实**：目录名、记录文件名、状态取值域、退出码、归因规则表。

放在 `app/` 顶层而不是 `app/strix_bridge/`：后者的定义是"唯一允许 import `strix.*` 的
地方"（import-linter 强制），而本模块**零 `strix` import** —— 它是一张照着源码抄下来的
对照表，不是桥。让它进 `strix_bridge/` 会稀释那条边界的含义。

为什么按版本索引：pin 是精确的（`strix-agent==1.6.2`），但升级是允许的（跟 minor），
而升级时最先失效的就是这些字面值。把它们收在一处，六阶段 runbook 的第一步就有确切的
diff 对象；`profile_for()` 对未知版本直接抛，所以"有人绕过 runbook 换了版本"是一次
响亮的启动失败，而不是一串静默归错因的扫描。

本模块只放"Strix 说什么"，**不放"我们怎么叫它"** —— `SCAN_FAILURE_CODES`（`errors.py`）、
`SCAN_STATUSES` / `EXIT_MEANINGS` / 四个超时常量（`services/scan_supervisor.py`）都是
我们自己的词汇，混进来就分不清哪一半是升级时要重验的。

升级时**只许往 `PROFILES` 加条目，不许删旧条目**：老扫描行里存着它当时的 `strix_version`，
删掉那份对照表就是"升级弄坏了我以前的报告"（`profile_for()` 会对它直接抛）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StrixProfile:
    """一个 `strix-agent` 版本的对照表。

    `attribution_rules` 存的是**正则源码字符串**而不是编译好的 `re.Pattern`：本模块是
    数据（T29 的契约断言要遍历它、跟另一个版本逐项比对），编译是使用方的事
    （`scan_supervisor.compile_rules`，一个进程只编一次）。
    """

    version: str
    runs_dir_name: str
    run_record_name: str
    run_statuses: frozenset[str]
    exit_code_ok: int
    exit_code_failed: int
    exit_code_vulnerabilities_found: int
    attribution_rules: tuple[tuple[str, str], ...]
    image_elision_texts: tuple[str, ...]
    compaction_checkpoint_tag: str
    event_kinds: frozenset[str]
    terminal_run_statuses: frozenset[str]


# stdout 正文 → 归因码。**顺序即优先级**，来自 `scripts/m0_probe_inner.sh` 里实测通过的
# 分类器，改顺序必须有新的实测样本。
#
# ⚠️ **只匹配正文，不要试图匹配面板标题**：`interface/{main,cli}.py` 里 8 个 Rich Panel
# 的 `title=` 全是同一个字面量 `[bold white]STRIX`，标题没有任何区分度。
_ATTRIBUTION_RULES_1_6_2: tuple[tuple[str, str], ...] = (
    (r"CERTIFICATE_VERIFY_FAILED|SSLCertVerificationError", "llm_tls_intercepted"),
    # litellm 的 bedrock **converse** 路由解析不出 SigV4 凭据时，拿 None 去取
    # `.access_key` 而崩；只给 bearer token 也会崩。出路是换 invoke 路由。
    (r"object has no attribute 'access_key'", "bedrock_route_rejects_bearer"),
    # 必须排在 `ValidationException` 那条**之前**：否则"invoke 路由收到 converse 专用的
    # prompt cache 参数"会被归成"没有模型访问权"，让人跑去 AWS 控制台申请权限，
    # 而鉴权其实**已经通过了**。
    (r"cache_control_injection_points", "prompt_cache_unsupported_on_route"),
    (
        r"AuthenticationError|security token included in the request is invalid"
        r"|invalid_api_key|Incorrect API key|Invalid API Key",
        "invalid_api_key",
    ),
    (r"AccessDenied|not authorized to perform|ValidationException", "model_access_denied"),
    # 必须排在 `model_not_found` 那条**之前**（两者的字面量会同时出现）。
    # 2026-09-14 实测：模型名里没有 `/` → 裸名默认路由到 OpenAI → Strix 在
    # `interface/main.py:186` 起飞前 `sys.exit(1)`，`run.json` 根本不生成。
    (r"UNKNOWN MODEL NAME", "model_name_not_provider_qualified"),
    (r"NotFound|model_not_found|MODEL NOT FOUND", "model_not_found"),
    (r"MISSING REQUIRED ENVIRONMENT VARIABLES", "missing_required_env"),
    # 原分类器是两个 `grep -i` 的**逻辑与**，翻成一条正则必须限个窗口 —— 否则会把相隔
    # 50 KiB 的两个无关错误撞成一条。
    (
        r"(?is)(docker.{0,400}permission denied|permission denied.{0,400}docker)",
        "docker_permission_denied",
    ),
    (r"LLM CONNECTION FAILED", "llm_connection_failed"),
)

_P_1_6_2 = StrixProfile(
    version="1.6.2",
    runs_dir_name="strix_runs",  # core/paths.py:8 RUNS_DIR_NAME
    run_record_name="run.json",  # core/paths.py:10 RUN_RECORD_FILENAME
    # `core/agents.py:25` 的 7 个，**外加 `interrupted`** —— 后者不在那个枚举里，是
    # `interface/cli.py:135` 的信号处理器经 `report_state.cleanup(status="interrupted")`
    # 单独写进 `run.json` 的，而且 `report/state.py:643` 那个 if 保证它不会再被
    # `finally` 里的 `stopped` 盖掉。漏掉它，"操作者停止"会落到兜底分支被报成"失败"。
    run_statuses=frozenset(
        {
            "running",
            "waiting",
            "completed",
            "stopped",
            "crashed",
            "failed",
            "budget_paused",
            "interrupted",
        }
    ),
    exit_code_ok=0,
    # 1 **含"收到信号后自己退出"**：`interface/cli.py:138` 的处理器调 `sys.exit(1)`，
    # 所以进程是正常退出的，returncode 是 1 而**不是** -15。
    exit_code_failed=1,
    # 2 = 有发现（`interface/main.py:533`），**必须当成功**。
    exit_code_vulnerabilities_found=2,
    attribution_rules=_ATTRIBUTION_RULES_1_6_2,
    # core/sessions.py:60-62 —— 上下文里的截图被淘汰时，output 会被**原位**换成这三条之一。
    # 投影层靠它把"截图淘汰"和"事件被改写"区分开（前者不需要重同步）。
    image_elision_texts=(
        "[image rejected by the model]",
        "[older screenshot elided to bound context memory]",
        "[screenshot omitted from inherited context]",
    ),
    # llm/compaction.py:34 _CHECKPOINT_TAG —— 压缩后新的第一条 user turn 以它开头。
    compaction_checkpoint_tag="<conversation-checkpoint>",
    # interface/tui/live_view.py:396 事件 id 的前缀，只有这两种。
    event_kinds=frozenset({"chat", "tool"}),
    # interface/viewer/transcript.py:19 _TERMINAL_STATUSES —— **与上面 run_statuses 的 8 个
    # 不是一回事**：这 4 个是 read_run_summary 判 `finished` 用的。
    terminal_run_statuses=frozenset({"completed", "stopped", "failed", "interrupted"}),
)

PROFILES: Mapping[str, StrixProfile] = {"1.6.2": _P_1_6_2}


def profile_for(version: str) -> StrixProfile:
    """按版本取对照表。未知版本**立刻抛**，绝不回落到最近的已知版本。

    回落是错的答案：pin 是精确的，所以"版本不认识"只能意味着有人绕过了升级 runbook，
    而拿旧对照表去解释新输出会让归因静默地错（比报错难查得多）。
    唯一的调用点是 `ScanSupervisor.__init__` —— 版本在别处不重新读。
    """
    profile = PROFILES.get(version)
    if profile is None:
        raise RuntimeError(
            f"未登记的 strix-agent 版本 {version!r}。换版本必须走 PLAN.md "
            "§Strix 版本升级 的六阶段 runbook，并在 strix_profile.py 里新增一份对照表。"
        )
    return profile
