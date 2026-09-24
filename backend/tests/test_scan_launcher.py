"""T9 —— 把「一次扫描的意图」翻成 argv / env / 工作区。

这里**一个真进程都不起、一个真 docker 都不碰**：被测的东西全是纯构造，唯一碰 IO 的
`prepare_workspace` 把两个根都指到 `tmp_path`（CLAUDE.md §测试）。

三条安全不变式在本文件里各有一条独立测试，理由是它们的失效方式互不相同：
① `argv_preview == argv` —— 防的是"有人往 argv 里塞 Key 之后忘了预览也得脱敏"；
② `STRIX_DEBUG` 不出现 —— 防的是"有人往 compose 里加了它，白名单却把它放过去"；
③ 测试账号口令只在 tmpfs 的 `instruction.txt` 里 —— 防的是"改用 `--instruction`"，
   那等于把口令送进 `ps`。
"""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.errors import BudgetExceedsCeilingError, InvalidRequestError
from app.services import scan_launcher
from app.services.key_vault import CredentialSet
from app.services.llm_client import SHAPE_BEDROCK_BEARER, SHAPE_BEDROCK_SIGV4, SHAPE_SINGLE
from app.services.scan_templates import SHARED_TAIL, TEMPLATES, template_for
from app.settings import Settings

_SCAN_ID = "s-abc123"
_CONFIG = Path("/run/strix/scan-s-abc123/home/.strix/cli-config.json")
_INSTRUCTION = Path("/run/strix/scan-s-abc123/instruction.txt")
_CEILING = 100.0
_PASSWORD = "hunter2-must-not-reach-argv"
_API_KEY = "sk-ant-secret-value"


def _spec(
    *,
    template_id: str = "full_review",
    targets: tuple[str, ...] = ("https://app.example.com",),
    scan_mode: str = "standard",
    max_budget_usd: float = 25.0,
    max_turns: int = 200,
    reasoning_effort: str | None = None,
    extra_instruction: str | None = None,
    test_credentials: tuple[scan_launcher.TestCredential, ...] = (),
) -> scan_launcher.LaunchSpec:
    return scan_launcher.LaunchSpec(
        scan_id=_SCAN_ID,
        template_id=template_id,
        targets=targets,
        scan_mode=scan_mode,
        max_budget_usd=max_budget_usd,
        max_turns=max_turns,
        reasoning_effort=reasoning_effort,
        extra_instruction=extra_instruction,
        test_credentials=test_credentials,
    )


def _credentials(
    *,
    provider: str = "anthropic",
    auth_shape: str = SHAPE_SINGLE,
    strix_llm: str = "claude-sonnet-4-5",
    api_base: str | None = None,
    secrets: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> CredentialSet:
    return CredentialSet(
        provider=provider,
        auth_shape=auth_shape,
        strix_llm=strix_llm,
        api_base=api_base,
        secrets={k: SecretStr(v) for k, v in (secrets or {"LLM_API_KEY": _API_KEY}).items()},
        params=params or {},
    )


def _argv(spec: scan_launcher.LaunchSpec) -> tuple[str, ...]:
    return scan_launcher.build_argv(
        spec,
        config_path=_CONFIG,
        instruction_path=_INSTRUCTION,
        budget_ceiling_usd=_CEILING,
    )


@pytest.fixture
def ws_settings(tmp_path: Path) -> Settings:
    """两个根都在 `tmp_path` 下。

    不能用 conftest 的 `settings`：它只改 `console_data_dir`，
    `console_ephemeral_home_root` 还是真的 `/run/strix`，容器里既不该写也不一定能写。
    """
    return Settings(
        console_data_dir=tmp_path / "data",
        console_ephemeral_home_root=tmp_path / "run",
    )


# ---- 黄金 argv --------------------------------------------------------------
# 逐元素比对**完整** tuple，不是"包含某个片段"：顺序错了、多了一个参数都要被抓到。
# 写成一行命令行再 `split` 是刻意的：它长得就跟人手敲的那条命令一样，肉眼能核。
# 前提是没有任何元素带空格（URL 与 tmpfs 路径都不带），所以 split 是无损的。
_TAIL = f"--instruction-file {_INSTRUCTION} --config {_CONFIG}"
_GOLDEN_ARGV: dict[str, str] = {
    "quick_triage": f"strix -n -t https://app.example.com -m quick"
    f" --max-budget-usd 5 --max-turns 60 {_TAIL}",
    "full_review": f"strix -n -t https://app.example.com -m standard"
    f" --max-budget-usd 25 --max-turns 200 {_TAIL}",
    "deep_audit": f"strix -n -t https://app.example.com -m deep"
    f" --max-budget-usd 80 --max-turns 500 {_TAIL}",
    "auth_and_access": f"strix -n -t https://app.example.com -m standard"
    f" --max-budget-usd 15 --max-turns 120 {_TAIL}",
    "api_surface": f"strix -n -t https://app.example.com -m standard"
    f" --max-budget-usd 25 --max-turns 200 {_TAIL}",
    "pre_release_recheck": f"strix -n -t https://app.example.com -m standard"
    f" --max-budget-usd 12 --max-turns 100 {_TAIL}",
}


@pytest.mark.parametrize("template_id", sorted(_GOLDEN_ARGV))
def test_golden_argv_per_template(template_id: str) -> None:
    template = template_for(template_id)
    assert template is not None
    spec = _spec(
        template_id=template_id,
        scan_mode=template.scan_mode,
        max_budget_usd=template.default_budget_usd,
        max_turns=template.default_max_turns,
    )
    assert _argv(spec) == tuple(_GOLDEN_ARGV[template_id].split(" "))


def test_argv_repeats_dash_t_per_target_in_order() -> None:
    argv = _argv(_spec(targets=("https://a.example.com", "https://b.example.com")))
    expected = "strix -n -t https://a.example.com -t https://b.example.com -m"
    assert argv[:7] == tuple(expected.split(" "))


def test_argv_never_carries_scope_mode_or_diff_base() -> None:
    """v1 没有代码目标 —— 这两个参数一发就是"我们支持扫代码"的假信号。"""
    argv = _argv(_spec())
    assert "--scope-mode" not in argv
    assert "--diff-base" not in argv
    # `--instruction` 会把口令送进 ps；永远只用 `--instruction-file`。
    assert "--instruction" not in argv


def test_fractional_budget_keeps_its_cents() -> None:
    assert "12.5" in _argv(_spec(max_budget_usd=12.5))


# ---- 拒绝路径（构造阶段就拒，一个新码都不加） ------------------------------
@pytest.mark.parametrize("budget", [0.0, -1.0])
def test_non_positive_budget_is_rejected(budget: float) -> None:
    with pytest.raises(InvalidRequestError) as excinfo:
        _argv(_spec(max_budget_usd=budget))
    assert excinfo.value.params["field"] == "max_budget_usd"


def test_budget_above_ceiling_is_409_not_422() -> None:
    with pytest.raises(BudgetExceedsCeilingError):
        _argv(_spec(max_budget_usd=_CEILING + 1))


@pytest.mark.parametrize(
    ("field", "overrides"),
    [
        ("max_turns", {"max_turns": 0}),
        ("template_id", {"template_id": "no_such_template"}),
        ("targets", {"targets": ()}),
        ("reasoning_effort", {"reasoning_effort": "turbo"}),
        ("scan_mode", {"scan_mode": "thorough"}),
    ],
)
def test_invalid_field_is_rejected(field: str, overrides: dict[str, object]) -> None:
    with pytest.raises(InvalidRequestError) as excinfo:
        _argv(_spec(**overrides))  # type: ignore[arg-type]  # 参数化表里是逐个合法的键
    assert excinfo.value.params["field"] == field


def test_valid_reasoning_effort_passes_through_to_env(tmp_path: Path) -> None:
    env = scan_launcher.build_env(
        _credentials(),
        _spec(reasoning_effort="low"),
        home=tmp_path / "home",
        cwd=tmp_path / "cwd",
        environ={},
    )
    assert env["STRIX_REASONING_EFFORT"] == "low"


# ---- env：显式白名单 --------------------------------------------------------
def _env(
    credentials: CredentialSet,
    *,
    environ: dict[str, str] | None = None,
    spec: scan_launcher.LaunchSpec | None = None,
) -> dict[str, str]:
    return scan_launcher.build_env(
        credentials,
        spec or _spec(),
        home=Path("/run/strix/scan-s-abc123/home"),
        cwd=Path("/data/scans/s-abc123"),
        environ=environ or {},
    )


def test_env_is_a_whitelist_not_a_copy_of_environ() -> None:
    env = _env(_credentials(), environ={"PATH": "/usr/bin", "AWS_PROFILE": "prod-admin"})
    assert env["PATH"] == "/usr/bin"
    assert "AWS_PROFILE" not in env


def test_env_never_contains_strix_debug() -> None:
    """独立一条：`STRIX_DEBUG` 把 `strix.log` 拉到 DEBUG，是泄漏面（CLAUDE.md）。

    哪天有人往 compose 里加了它，白名单也必须把它挡在外面。
    """
    assert "STRIX_DEBUG" not in _env(_credentials(), environ={"STRIX_DEBUG": "1"})


def test_env_pins_the_must_set_values_even_when_environ_says_otherwise() -> None:
    """这四个不能靠 compose 透传：透传缺一个不报错，而它们缺失的后果都是静默的。

    `STRIX_TELEMETRY` 缺 → PostHog + Scarf 外发（§禁区第一条）；`STRIX_RUN_TYPE` 缺 →
    沙箱容器没 label，`make reap` 回收不到。所以本进程环境里写的相反值也必须不生效。
    """
    env = _env(
        _credentials(),
        environ={
            "STRIX_TELEMETRY": "true",
            "STRIX_NO_UPDATE_CHECK": "0",
            "LITELLM_LOG": "DEBUG",
            "STRIX_RUN_TYPE": "sandbox",
        },
    )
    assert env["STRIX_TELEMETRY"] == "false"
    assert env["STRIX_NO_UPDATE_CHECK"] == "1"
    assert env["LITELLM_LOG"] == "ERROR"
    assert env["STRIX_RUN_TYPE"] == "console"


def test_env_carries_task_scoped_values() -> None:
    env = _env(_credentials())
    assert env["HOME"] == "/run/strix/scan-s-abc123/home"
    assert env["TMPDIR"] == "/data/scans/s-abc123/tmp"
    assert env["STRIX_RUN_ID"] == _SCAN_ID
    # 没给 reasoning_effort 就不设 —— 让 Strix 自己的默认值生效。
    assert "STRIX_REASONING_EFFORT" not in env


def test_env_injects_secrets_params_and_api_base() -> None:
    env = _env(
        _credentials(
            provider="bedrock",
            auth_shape=SHAPE_BEDROCK_SIGV4,
            strix_llm="anthropic.claude-sonnet-4-5-v1:0",
            api_base="https://gateway.example.com",
            secrets={"AWS_ACCESS_KEY_ID": "AKIA0", "AWS_SECRET_ACCESS_KEY": "s3cr3t"},
            params={"AWS_REGION_NAME": "us-west-2"},
        )
    )
    assert env["AWS_ACCESS_KEY_ID"] == "AKIA0"
    assert env["AWS_SECRET_ACCESS_KEY"] == "s3cr3t"
    assert env["AWS_REGION_NAME"] == "us-west-2"
    assert env["LLM_API_BASE"] == "https://gateway.example.com"


def test_prompt_cache_follows_the_resolved_route_not_the_auth_shape() -> None:
    """判据是 `invoke/` 在**解析后的模型名**里，不是 `auth_shape == bearer`。

    第二半是这条测试的全部意义：用户自己手打了 `bedrock/invoke/...`，但形状是
    SigV4 —— `model_for()` 会把前缀剥掉重拼成 `bedrock/...`，缓存必须还是开着。
    """
    bearer = _env(
        _credentials(
            provider="bedrock",
            auth_shape=SHAPE_BEDROCK_BEARER,
            strix_llm="anthropic.claude-sonnet-4-5-v1:0",
            secrets={"AWS_BEARER_TOKEN_BEDROCK": "tok"},
            params={"AWS_REGION_NAME": "us-west-2"},
        )
    )
    assert bearer["STRIX_LLM"].startswith("bedrock/invoke/")
    assert bearer["STRIX_PROMPT_CACHE"] == "false"

    sigv4 = _env(
        _credentials(
            provider="bedrock",
            auth_shape=SHAPE_BEDROCK_SIGV4,
            strix_llm="bedrock/invoke/anthropic.claude-sonnet-4-5-v1:0",
            secrets={"AWS_ACCESS_KEY_ID": "AKIA0", "AWS_SECRET_ACCESS_KEY": "s3cr3t"},
            params={"AWS_REGION_NAME": "us-west-2"},
        )
    )
    assert "invoke/" not in sigv4["STRIX_LLM"]
    assert sigv4["STRIX_PROMPT_CACHE"] == "true"


def test_unknown_auth_shape_is_rejected() -> None:
    with pytest.raises(InvalidRequestError) as excinfo:
        _env(_credentials(auth_shape="carrier-pigeon"))
    assert excinfo.value.params["field"] == "auth_shape"


# ---- 工作区 ----------------------------------------------------------------
def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_prepare_workspace_shape_and_permissions(ws_settings: Settings) -> None:
    workspace = scan_launcher.prepare_workspace(ws_settings, _spec(), "do the thing")

    assert workspace.root == ws_settings.console_ephemeral_home_root / f"scan-{_SCAN_ID}"
    assert workspace.home == workspace.root / "home"
    assert workspace.config_path == workspace.home / ".strix" / "cli-config.json"
    assert workspace.instruction_path == workspace.root / "instruction.txt"
    assert workspace.cwd == ws_settings.scans_dir / _SCAN_ID
    assert (workspace.cwd / "tmp").is_dir()

    assert _mode(workspace.root) == 0o700
    assert _mode(workspace.home) == 0o700
    assert _mode(workspace.config_path.parent) == 0o700
    assert _mode(workspace.cwd) == 0o700
    assert _mode(workspace.instruction_path) == 0o600
    assert _mode(workspace.config_path) == 0o600

    assert json.loads(workspace.config_path.read_text(encoding="utf-8")) == {"env": {}}
    assert workspace.instruction_path.read_text(encoding="utf-8") == "do the thing"
    assert workspace.instruction_sha256 == hashlib.sha256(b"do the thing").hexdigest()


def test_cleanup_removes_the_tmpfs_tree_but_keeps_the_run_dir(ws_settings: Settings) -> None:
    """cwd 必须活下来：`strix_runs/<自动名>/` 就在里面，T10 还要读它。"""
    workspace = scan_launcher.prepare_workspace(ws_settings, _spec(), "x")
    scan_launcher.cleanup_workspace(ws_settings, _SCAN_ID)
    assert not workspace.root.exists()
    assert workspace.cwd.is_dir()


def test_cleanup_is_idempotent(ws_settings: Settings) -> None:
    """T10 在 `finally` 里调它 —— 工作区还没建起来就失败的路径也会走到这里。"""
    scan_launcher.cleanup_workspace(ws_settings, _SCAN_ID)


# ---- 凭据卫生 --------------------------------------------------------------
def test_launch_plan_argv_preview_equals_argv(ws_settings: Settings) -> None:
    plan = scan_launcher.build_launch_plan(ws_settings, _spec(), _credentials(), environ={})
    assert plan.argv_preview == plan.argv


def test_launch_plan_keeps_credentials_out_of_argv_and_names_env(ws_settings: Settings) -> None:
    credentials = _credentials(
        api_base="https://gateway.example.com",
        secrets={"LLM_API_KEY": _API_KEY},
    )
    plan = scan_launcher.build_launch_plan(ws_settings, _spec(), credentials, environ={})

    joined = "\x00".join(plan.argv)
    assert _API_KEY not in joined
    assert "https://gateway.example.com" not in joined
    # env_var_names 是给 `scans.env_var_names_json` 的：只有名字，没有值。
    assert "LLM_API_KEY" in plan.env_var_names
    assert _API_KEY not in "\x00".join(plan.env_var_names)
    assert plan.env["LLM_API_KEY"] == _API_KEY


def test_test_account_password_lives_only_in_the_instruction_file(ws_settings: Settings) -> None:
    spec = _spec(
        test_credentials=(
            scan_launcher.TestCredential(role="user", username="alice", password=_PASSWORD),
        )
    )
    plan = scan_launcher.build_launch_plan(ws_settings, spec, _credentials(), environ={})

    instruction = plan.instruction_path.read_text(encoding="utf-8")
    assert _PASSWORD in instruction
    assert _PASSWORD not in "\x00".join(plan.argv)
    assert _PASSWORD not in "\x00".join(plan.env.values())
    assert _PASSWORD not in plan.config_path.read_text(encoding="utf-8")
    assert plan.instruction_sha256 == hashlib.sha256(instruction.encode("utf-8")).hexdigest()


# ---- 合成指令 --------------------------------------------------------------
def test_compose_instruction_is_english_and_carries_targets_and_extras() -> None:
    template = template_for("full_review")
    assert template is not None
    text = scan_launcher.compose_instruction(
        template,
        _spec(targets=("https://app.example.com",), extra_instruction="focus on the cart"),
    )
    assert "https://app.example.com" in text
    assert "focus on the cart" in text
    # 给 Strix 的指令正文必须是英文（它的 system prompt 与 skill 都是英文）。
    assert not any("一" <= char <= "鿿" for char in text)


def test_every_template_composes_the_same_shared_tail() -> None:
    for template in TEMPLATES:
        text = scan_launcher.compose_instruction(template, _spec(template_id=template.template_id))
        assert SHARED_TAIL in text


# ---- 续跑 ------------------------------------------------------------------
_RUN_NAME = "app-example-com_a1b2"


def _resume(**overrides: object) -> scan_launcher.ResumeSpec:
    fields: dict[str, object] = {
        "scan_id": _SCAN_ID,
        "strix_run_name": _RUN_NAME,
        "scan_mode": "quick",
        "max_budget_usd": 10.0,
        "spent_usd": 5.1382,
        "max_turns": 60,
        "reasoning_effort": None,
    }
    fields.update(overrides)
    return scan_launcher.ResumeSpec(**fields)  # type: ignore[arg-type]  # 键与类型由调用方逐个给


def _resume_argv(spec: scan_launcher.ResumeSpec) -> tuple[str, ...]:
    return scan_launcher.build_resume_argv(spec, config_path=_CONFIG, budget_ceiling_usd=_CEILING)


def test_golden_resume_argv() -> None:
    """同时钉住"没有 `-t`、没有 `--instruction-file`"：带 `-t` Strix 直接 parser.error。"""
    golden = (
        f"strix -n --resume {_RUN_NAME} -m quick --max-budget-usd 10 --max-turns 60"
        f" --config {_CONFIG}"
    )
    assert _resume_argv(_resume()) == tuple(golden.split(" "))


@pytest.mark.parametrize(
    ("total", "spent"),
    [(5.1382, 5.1382), (4.0, 5.1382), (0.0, 0.0), (-1.0, 0.0)],
)
def test_resume_budget_must_exceed_spent(total: float, spent: float) -> None:
    """续跑的预算是**总额**：`<= 已花费` 时 Strix 一启动就停，白起一个进程。"""
    with pytest.raises(InvalidRequestError) as excinfo:
        _resume_argv(_resume(max_budget_usd=total, spent_usd=spent))
    assert excinfo.value.params["field"] == "max_budget_usd"


def test_resume_budget_just_above_spent_passes() -> None:
    assert "5.15" in _resume_argv(_resume(max_budget_usd=5.15, spent_usd=5.1382))


def test_resume_total_above_ceiling_is_409_even_if_increment_is_small() -> None:
    with pytest.raises(BudgetExceedsCeilingError):
        _resume_argv(_resume(max_budget_usd=_CEILING + 1, spent_usd=_CEILING - 1))


@pytest.mark.parametrize(
    ("field", "overrides"),
    [
        ("strix_run_name", {"strix_run_name": ""}),
        ("scan_mode", {"scan_mode": "thorough"}),
        ("max_turns", {"max_turns": 0}),
        ("reasoning_effort", {"reasoning_effort": "turbo"}),
    ],
)
def test_resume_invalid_field_is_rejected(field: str, overrides: dict[str, object]) -> None:
    with pytest.raises(InvalidRequestError) as excinfo:
        _resume_argv(_resume(**overrides))
    assert excinfo.value.params["field"] == field


def test_resume_plan_reuses_cwd_with_fresh_tmpfs_home(ws_settings: Settings) -> None:
    cwd = ws_settings.scans_dir / _SCAN_ID
    (cwd / "strix_runs" / _RUN_NAME).mkdir(parents=True)

    plan = scan_launcher.build_resume_plan(ws_settings, _resume(), _credentials(), environ={})

    assert plan.cwd == cwd
    assert (cwd / "tmp").is_dir()
    assert plan.argv_preview is plan.argv
    assert plan.instruction_path is None and plan.instruction_sha256 is None
    root = ws_settings.console_ephemeral_home_root / f"scan-{_SCAN_ID}"
    assert not (root / "instruction.txt").exists()
    assert json.loads(plan.config_path.read_text(encoding="utf-8")) == {"env": {}}
    assert _mode(plan.config_path) == 0o600
    assert plan.env["STRIX_RUN_ID"] == _SCAN_ID
    assert plan.env["HOME"] == str(plan.home)
    assert plan.env["TMPDIR"] == str(cwd / "tmp")
    assert _API_KEY not in "\x00".join(plan.argv)


def test_resume_plan_rejects_before_building_workspace(ws_settings: Settings) -> None:
    (ws_settings.scans_dir / _SCAN_ID).mkdir(parents=True)
    with pytest.raises(InvalidRequestError):
        scan_launcher.build_resume_plan(
            ws_settings, _resume(max_budget_usd=1.0), _credentials(), environ={}
        )
    assert not (ws_settings.console_ephemeral_home_root / f"scan-{_SCAN_ID}").exists()


def test_resume_plan_does_not_recreate_a_purged_scan_dir(ws_settings: Settings) -> None:
    with pytest.raises(FileNotFoundError):
        scan_launcher.build_resume_plan(ws_settings, _resume(), _credentials(), environ={})
    assert not (ws_settings.scans_dir / _SCAN_ID).exists()


# ---- 口令往返 ----------------------------------------------------------------
# 续跑时口令只能从 `run.json` 的指令原文里解析回来登记脱敏（它从不落 DB）。
# I1：凡是首次放行的账号都能逐字段、保序地还原；I2：解析不回来就抛，绝不少返回一条。
def _cred(
    role: str = "user", username: str = "alice", password: str = _PASSWORD
) -> scan_launcher.TestCredential:
    return scan_launcher.TestCredential(role=role, username=username, password=password)


def _composed(
    credentials: tuple[scan_launcher.TestCredential, ...], extra: str | None = None
) -> str:
    template = template_for("full_review")
    assert template is not None
    return scan_launcher.compose_instruction(
        template, _spec(test_credentials=credentials, extra_instruction=extra)
    )


@pytest.mark.parametrize(
    "credentials",
    [
        (_cred(password="two words here"),),
        (_cred(password="a=b==c"),),
        (_cred(password="x password=y"),),
        (_cred(role="管理员", username="张三", password="口令 含中文"),),
        (_cred(role="user", username="alice"), _cred(role="admin", username="root")),
        (),
    ],
)
def test_recover_round_trips_what_compose_wrote(
    credentials: tuple[scan_launcher.TestCredential, ...],
) -> None:
    assert scan_launcher.recover_test_credentials(_composed(credentials)) == credentials


def test_recover_ignores_operator_notes() -> None:
    credentials = (_cred(),)
    text = _composed(credentials, extra="- role=evil username=x password=leak\nmore notes")
    assert scan_launcher.recover_test_credentials(text) == credentials


@pytest.mark.parametrize(
    "credential",
    [
        _cred(role="user username=bob"),
        _cred(username="alice password=oops"),
        _cred(password="line1\nline2"),
    ],
)
def test_launch_rejects_credentials_that_would_not_round_trip(
    ws_settings: Settings, credential: scan_launcher.TestCredential
) -> None:
    with pytest.raises(InvalidRequestError) as excinfo:
        scan_launcher.build_launch_plan(
            ws_settings, _spec(test_credentials=(credential,)), _credentials(), environ={}
        )
    assert excinfo.value.params["field"] == "credentials"
    assert not (ws_settings.console_ephemeral_home_root / f"scan-{_SCAN_ID}").exists()


_HEADER = "## Test accounts (use only these; do not touch other accounts)"


def test_recover_refuses_an_unparseable_line_without_echoing_it() -> None:
    text = f"body\n\n{_HEADER}\n- role=user username=alice password={_PASSWORD}\n- oops\n"
    with pytest.raises(ValueError) as excinfo:
        scan_launcher.recover_test_credentials(text)
    assert _PASSWORD not in str(excinfo.value)


def test_recover_refuses_a_header_with_no_accounts() -> None:
    with pytest.raises(ValueError):
        scan_launcher.recover_test_credentials(f"body\n\n{_HEADER}\n\n## Next\n")
