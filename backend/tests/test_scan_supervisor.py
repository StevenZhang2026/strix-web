"""`ScanSupervisor`：归因映射（纯函数）+ 进程生命周期（真子进程）。

进程类测试用 `/bin/sh -c <脚本>` 当假 strix —— 它是**真**子进程（真信号、真管道、真
退出码、真进程组），只是不是 strix。真实 Docker 与真实网络一个都不碰。

`asyncio.run(scenario())` 而不是 `async def test_`：本仓刻意没有 `pytest-asyncio`
（同 `test_key_vault.py`）。每个等待都套 `wait_for` —— 没有超时的死锁测试会挂住整个
测试集，而"抽干 stdout"那条正是靠死锁来失败的。

**不重测 T9 已有的东西**（argv 逐元素、Key 不进 argv、`_PINNED_ENV`、instruction 权限位）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.errors import assert_scan_failure_code
from app.main import create_app
from app.services import scan_supervisor as supervisor_module
from app.services.scan_launcher import LaunchPlan
from app.services.scan_supervisor import (
    EXIT_MEANINGS,
    SCAN_STATUSES,
    STDOUT_TAIL_BYTES,
    Attribution,
    ScanOutcome,
    ScanSupervisor,
    assert_sandbox_env,
    classify_stdout,
    compile_rules,
    resolve_attribution,
)
from app.settings import Settings
from app.strix_profile import PROFILES, profile_for
from tests.conftest import make_coverage_json, make_run_dir

PROFILE = profile_for("1.6.2")
RULES = compile_rules(PROFILE)


def resolve(
    *,
    exit_code: int = 1,
    run_status: str | None = None,
    coverage_complete: bool = True,
    stdout_tail: str = "",
    stopped_by: str | None = None,
) -> Attribution:
    return resolve_attribution(
        exit_code=exit_code,
        run_status=run_status,
        coverage_complete=coverage_complete,
        stdout_tail=stdout_tail,
        stopped_by=stopped_by,
        profile=PROFILE,
        rules=RULES,
    )


# ---- A. 归因黄金表 -----------------------------------------------------------
# (标签, exit_code, run_status, stopped_by, status, exit_meaning, error_code)
# 标签开头的数字是判定表的行号，**顺序即优先级**。
_GOLDEN: tuple[tuple[str, int, str | None, str | None, str, str, str | None], ...] = (
    ("1 跑完了", 0, "completed", None, "completed", "no_vulnerabilities_found", None),
    ("1 跑完了+有发现", 2, "completed", None, "completed", "vulnerabilities_found", None),
    (
        "2 操作者停止",
        1,
        "running",
        "operator",
        "stopped",
        "no_vulnerabilities_found",
        "stopped_by_operator",
    ),
    (
        "3 停机",
        1,
        "running",
        "shutdown",
        "interrupted",
        "no_vulnerabilities_found",
        "interrupted_by_restart",
    ),
    (
        "4 run 记录说 interrupted",
        1,
        "interrupted",
        None,
        "interrupted",
        "no_vulnerabilities_found",
        "interrupted_by_restart",
    ),
    # 退出码 0 + stopped：**发布阻断项**。只看退出码会报成"未发现漏洞"。
    ("5 预算耗尽", 0, "stopped", None, "stopped", "no_vulnerabilities_found", "scan_incomplete"),
    (
        "5 预算暂停+有发现",
        2,
        "budget_paused",
        None,
        "stopped",
        "vulnerabilities_found",
        "scan_incomplete",
    ),
    ("6 crashed", 1, "crashed", None, "failed", "failed", "scan_failed_unknown"),
    ("6 failed", 1, "failed", None, "failed", "failed", "scan_failed_unknown"),
    # 退出码 0 + 根本没有 run 目录 = 什么都没跑（§2.7），绝不能报"未发现漏洞"。
    ("7 没有 run 记录", 0, None, None, "failed", "failed", "scan_failed_unknown"),
    ("8 记录还写着在跑", 1, "running", None, "failed", "failed", "scan_failed_unknown"),
    ("9 兜底", 1, "waiting", None, "failed", "failed", "scan_failed_unknown"),
)


@pytest.mark.parametrize(
    ("exit_code", "run_status", "stopped_by", "status", "exit_meaning", "error_code"),
    [row[1:] for row in _GOLDEN],
    ids=[row[0] for row in _GOLDEN],
)
def test_attribution_golden_table(
    exit_code: int,
    run_status: str | None,
    stopped_by: str | None,
    status: str,
    exit_meaning: str,
    error_code: str | None,
) -> None:
    got = resolve(exit_code=exit_code, run_status=run_status, stopped_by=stopped_by)
    assert got == Attribution(status=status, exit_meaning=exit_meaning, error_code=error_code)


def test_golden_table_values_are_all_registered() -> None:
    """黄金表里出现的每个值都必须在各自的登记表里（拼错立刻响亮失败）。"""
    assert _GOLDEN, "黄金表是空的，下面的遍历什么都没检查"
    for row in _GOLDEN:
        _, _, _, _, status, exit_meaning, error_code = row
        assert status in SCAN_STATUSES
        assert exit_meaning in EXIT_MEANINGS
        if error_code is not None:
            assert_scan_failure_code(error_code)


def test_completed_is_exactly_the_case_without_a_reason() -> None:
    """`status == "completed"` **等价于** `error_code is None`。

    没有一个非完成态可以静默地没有原因 —— 那会把"不许暗示目标干净"推给展示层。
    """
    for row in _GOLDEN:
        _, exit_code, run_status, stopped_by, _, _, _ = row
        got = resolve(exit_code=exit_code, run_status=run_status, stopped_by=stopped_by)
        assert (got.status == "completed") == (got.error_code is None)


@pytest.mark.parametrize(
    "run_status", [None, "completed", "stopped", "budget_paused", "crashed", "running"]
)
def test_exit_code_2_always_means_vulnerabilities_found(run_status: str | None) -> None:
    """两个正交的轴：「找到漏洞」和「跑没跑完」互不推导，退出码 2 只回答前者。"""
    assert resolve(exit_code=2, run_status=run_status).exit_meaning == "vulnerabilities_found"


def test_completed_wins_over_a_stop_we_sent() -> None:
    """判定表第 1 行排在第 2/3 行前面是刻意的。

    `completed` 一旦写下就再也不会被覆盖（§2.3），所以它是真的跑完了 —— 哪怕操作者
    在进程即将正常退出的一瞬点了停止。
    """
    got = resolve(exit_code=0, run_status="completed", stopped_by="operator")
    assert got == Attribution("completed", "no_vulnerabilities_found", None)


@pytest.mark.parametrize(
    ("exit_code", "stopped_by", "exit_meaning"),
    [
        (0, None, "no_vulnerabilities_found"),
        (2, None, "vulnerabilities_found"),
        (0, "operator", "no_vulnerabilities_found"),
    ],
)
def test_completed_with_incomplete_coverage_is_not_a_clean_verdict(
    exit_code: int, stopped_by: str | None, exit_meaning: str
) -> None:
    """Strix 自己收尾了（`completed`），但覆盖记录说有子任务没测完 —— 不能说"没问题"。"""
    got = resolve(
        exit_code=exit_code,
        run_status="completed",
        coverage_complete=False,
        stopped_by=stopped_by,
    )
    assert got == Attribution("stopped", exit_meaning, "coverage_incomplete")


# ---- B/B2/C2. 规则表的顺序 ----------------------------------------------------
def test_prompt_cache_beats_validation_exception() -> None:
    """两个字面量会同时出现在同一次失败的输出里。

    归成 `model_access_denied` 会让人跑去 AWS 控制台申请权限，而鉴权其实已经通过了。
    """
    text = "litellm.BadRequestError: ValidationException ... cache_control_injection_points ..."
    assert classify_stdout(text, RULES) == "prompt_cache_unsupported_on_route"


def test_unknown_model_name_beats_model_not_found() -> None:
    text = "UNKNOWN MODEL NAME\nMODEL NOT FOUND: check the name"
    assert classify_stdout(text, RULES) == "model_name_not_provider_qualified"


def test_no_rule_matches_is_none() -> None:
    assert classify_stdout("everything is fine", RULES) is None


def test_compile_rules_preserves_order() -> None:
    """编译只是把字符串换成 Pattern，**不许重排**（顺序就是优先级）。"""
    declared = PROFILES["1.6.2"].attribution_rules
    compiled = compile_rules(PROFILES["1.6.2"])
    assert declared, "规则表是空的，下面的比对什么都没检查"
    assert [pattern.pattern for pattern, _ in compiled] == [source for source, _ in declared]
    assert [code for _, code in compiled] == [code for _, code in declared]


# ---- L. 启动断言 -------------------------------------------------------------
def test_assert_sandbox_env_requires_image(tmp_path: Path) -> None:
    settings = Settings(
        console_data_dir=tmp_path,
        strix_image="",
        strix_docker_sandbox_network="strix_sandbox",
    )
    with pytest.raises(RuntimeError) as excinfo:
        assert_sandbox_env(settings)
    assert "docker-compose.yml" in str(excinfo.value)


def test_assert_sandbox_env_requires_network(tmp_path: Path) -> None:
    settings = Settings(
        console_data_dir=tmp_path,
        strix_image="strix-sandbox:test",
        strix_docker_sandbox_network="",
    )
    with pytest.raises(RuntimeError) as excinfo:
        assert_sandbox_env(settings)
    assert "docker-compose.yml" in str(excinfo.value)


def test_assert_sandbox_env_passes_when_both_set(tmp_path: Path) -> None:
    assert_sandbox_env(
        Settings(
            console_data_dir=tmp_path,
            strix_image="strix-sandbox:test",
            strix_docker_sandbox_network="strix_sandbox",
        )
    )


# ---- K. 进程生命周期（真 subprocess）-----------------------------------------
@pytest.fixture
def sandbox_settings(tmp_path: Path) -> Settings:
    """tmpfs 根指到 `tmp_path` 下 —— 单测不碰真实的 `/run/strix`。"""
    ephemeral = tmp_path / "run"
    ephemeral.mkdir()
    return Settings(
        console_data_dir=tmp_path,
        console_ephemeral_home_root=ephemeral,
        strix_image="strix-sandbox:test",
        strix_docker_sandbox_network="strix_sandbox",
    )


def make_plan(
    settings: Settings,
    scan_id: str,
    script: str,
    env: Mapping[str, str] | None = None,
) -> LaunchPlan:
    """一个指向 `/bin/sh -c <script>` 的假 LaunchPlan，目录布局与 `prepare_workspace` 一致。

    刻意不调 `build_launch_plan`：那要凭据与模板（T9 已经测透了），而这里要测的是
    进程生命周期与目录清理。
    """
    root = settings.console_ephemeral_home_root / f"scan-{scan_id}"
    home = root / "home"
    config_dir = home / ".strix"
    cwd = settings.scans_dir / scan_id
    for directory in (root, home, config_dir, cwd, cwd / "tmp"):
        directory.mkdir(parents=True, exist_ok=True)
    resolved_env = dict(env if env is not None else {"PATH": "/usr/bin:/bin"})
    return LaunchPlan(
        argv=("/bin/sh", "-c", script),
        argv_preview=("/bin/sh", "-c", script),
        env=resolved_env,
        env_var_names=tuple(sorted(resolved_env)),
        cwd=cwd,
        home=home,
        config_path=config_dir / "cli-config.json",
        instruction_path=root / "instruction.txt",
        instruction_sha256="0" * 64,
    )


async def wait_for_file(path: Path, limit_seconds: float = 20.0) -> None:
    """等假 strix 说"我起来了"再发信号 —— 否则会有一半的跑测在 trap 装好之前就 TERM。"""
    deadline = time.monotonic() + limit_seconds
    while not await asyncio.to_thread(path.exists):
        assert time.monotonic() < deadline, f"{path} 一直没出现"
        await asyncio.sleep(0.05)


def run_to_outcome(settings: Settings, scan_id: str, plan: LaunchPlan) -> ScanOutcome:
    supervisor = ScanSupervisor(settings, "1.6.2")

    async def scenario() -> ScanOutcome:
        process = await supervisor.start(scan_id, plan)
        return await asyncio.wait_for(process.wait(), timeout=60)

    return asyncio.run(scenario())


def test_clean_exit_cleans_tmpfs_and_tmpdir_but_keeps_products(
    sandbox_settings: Settings,
) -> None:
    plan = make_plan(sandbox_settings, "ok", "exit 0")
    make_coverage_json(make_run_dir(plan.cwd, status="completed"), complete=True)

    outcome = run_to_outcome(sandbox_settings, "ok", plan)

    assert outcome.status == "completed"
    assert outcome.exit_meaning == "no_vulnerabilities_found"
    assert outcome.error_code is None
    assert outcome.error_message is None
    assert outcome.run_status == "completed"
    assert outcome.strix_run_name == "strix-run-1"
    assert outcome.run_dir == plan.cwd / "strix_runs" / "strix-run-1"
    # tmpfs 那棵树（口令 + 可能被回落写入的 Key）必须没了。
    assert not (sandbox_settings.console_ephemeral_home_root / "scan-ok").exists()
    # TMPDIR 也必须没了，但**同级的产物目录还在** —— T13 还要读它。
    assert not (plan.cwd / "tmp").exists()
    assert (plan.cwd / "strix_runs" / "strix-run-1" / "run.json").is_file()


def test_completed_without_coverage_record_is_incomplete(sandbox_settings: Settings) -> None:
    """与上一条一起钉住 `_collect` 真的读了 coverage、读的是对的文件。"""
    plan = make_plan(sandbox_settings, "nocov", "exit 0")
    make_run_dir(plan.cwd, status="completed")

    outcome = run_to_outcome(sandbox_settings, "nocov", plan)

    assert outcome.status == "stopped"
    assert outcome.error_code == "coverage_incomplete"
    assert outcome.exit_meaning == "no_vulnerabilities_found"
    assert outcome.run_status == "completed"


def test_finished_scan_notifies_the_reaper_after_leaving_the_registry(
    sandbox_settings: Settings,
) -> None:
    """T11a 的接线缝：扫描结束必须**通知一次** —— 强杀路径必定泄漏沙箱。

    断言里带上"那一刻已经不在册"：清扫要按新名单算，否则刚结束的那次扫描会把自己的
    沙箱 spare 掉一整轮（5 分钟）。
    """
    seen: list[tuple[str, ...]] = []
    supervisor = ScanSupervisor(
        sandbox_settings,
        "1.6.2",
        on_scan_finished=lambda: seen.append(supervisor.active_scan_ids()),
    )
    plan = make_plan(sandbox_settings, "notify", "exit 0")
    make_coverage_json(make_run_dir(plan.cwd, status="completed"), complete=True)

    async def scenario() -> None:
        process = await supervisor.start("notify", plan)
        await asyncio.wait_for(process.wait(), timeout=60)

    asyncio.run(scenario())

    assert seen == [()]


def test_exit_code_2_is_success_with_findings(sandbox_settings: Settings) -> None:
    plan = make_plan(sandbox_settings, "found", "exit 2")
    make_coverage_json(make_run_dir(plan.cwd, status="completed"), complete=True)

    outcome = run_to_outcome(sandbox_settings, "found", plan)

    assert outcome.status == "completed"
    assert outcome.exit_meaning == "vulnerabilities_found"
    assert outcome.exit_code == 2


def test_two_megabytes_of_output_does_not_deadlock(sandbox_settings: Settings) -> None:
    """管道缓冲区满了子进程就阻塞在 write 上，表现是"扫描卡住、没有任何报错"。"""
    plan = make_plan(
        sandbox_settings,
        "loud",
        'i=0; while [ $i -lt 2048 ]; do printf "%01024d" "$i"; i=$((i+1)); done; exit 0',
    )
    make_coverage_json(make_run_dir(plan.cwd, status="completed"), complete=True)
    supervisor = ScanSupervisor(sandbox_settings, "1.6.2")

    async def scenario() -> tuple[ScanOutcome, int]:
        process = await supervisor.start("loud", plan)
        outcome = await asyncio.wait_for(process.wait(), timeout=60)
        # 访问私有属性是刻意的：尾巴不对外暴露（它只该进日志），而"只留最后 64 KiB"
        # 是内存上限的不变量，必须有测试。
        return outcome, len(process._tail)

    outcome, tail_size = asyncio.run(scenario())
    assert outcome.status == "completed"
    assert tail_size == STDOUT_TAIL_BYTES


def test_error_message_never_quotes_stdout(sandbox_settings: Settings) -> None:
    """**泄漏矩阵的守卫**：操作者填的测试账号口令会出现在 stdout 里（agent 拿它登录是
    预期行为），而它不在 KeyVault 里、因此不在精确子串脱敏集合里。所以
    `error_message` 一个字节都不许来自 stdout。
    """
    sentinel = "SENTINEL-PASSWORD-9182"
    plan = make_plan(sandbox_settings, "leak", f'echo "{sentinel}"; exit 1')
    supervisor = ScanSupervisor(sandbox_settings, "1.6.2")

    async def scenario() -> tuple[ScanOutcome, str]:
        process = await supervisor.start("leak", plan)
        outcome = await asyncio.wait_for(process.wait(), timeout=60)
        return outcome, bytes(process._tail).decode("utf-8")

    outcome, tail = asyncio.run(scenario())
    # 前提：哨兵真的被抓到了。不断言这一条，下面那句"不在里面"可能只是因为没抓到。
    assert sentinel in tail
    assert outcome.status == "failed"
    assert outcome.error_code == "scan_failed_unknown"
    assert outcome.error_message is not None
    assert sentinel not in outcome.error_message
    assert "scan_failed_unknown" in outcome.error_message


def test_operator_stop_is_stopped_not_failed(sandbox_settings: Settings) -> None:
    """Strix 自己装了 SIGTERM 处理器并 `sys.exit(1)` —— 退出码是 1，不是 -15。

    所以"是不是被人停的"唯一可靠判据是"我们自己发过信号"这个事实。
    """
    plan = make_plan(
        sandbox_settings,
        "stop",
        'trap "exit 1" TERM; : > ready; while true; do sleep 0.05; done',
    )
    supervisor = ScanSupervisor(sandbox_settings, "1.6.2")

    async def scenario() -> ScanOutcome:
        process = await supervisor.start("stop", plan)
        await wait_for_file(plan.cwd / "ready")
        await process.stop()
        return await asyncio.wait_for(process.wait(), timeout=60)

    outcome = asyncio.run(scenario())
    assert outcome.exit_code == 1
    assert outcome.status == "stopped"
    assert outcome.error_code == "stopped_by_operator"


def test_stop_escalates_to_sigkill_after_grace(
    sandbox_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """赖着不走的子进程必须被强杀（否则一个卡死的扫描能永久占住一个槽位）。"""
    monkeypatch.setattr(supervisor_module, "STOP_GRACE_SECONDS", 0.2)
    plan = make_plan(sandbox_settings, "stubborn", 'trap "" TERM; : > ready; sleep 60')
    supervisor = ScanSupervisor(sandbox_settings, "1.6.2")

    async def scenario() -> ScanOutcome:
        process = await supervisor.start("stubborn", plan)
        await wait_for_file(plan.cwd / "ready")
        await process.stop()
        return await asyncio.wait_for(process.wait(), timeout=60)

    outcome = asyncio.run(scenario())
    assert outcome.exit_code == -9
    assert outcome.status == "stopped"
    assert outcome.error_code == "stopped_by_operator"


def test_shutdown_marks_scans_interrupted_and_empties_the_registry(
    sandbox_settings: Settings,
) -> None:
    plan = make_plan(sandbox_settings, "restart", ": > ready; sleep 60")
    supervisor = ScanSupervisor(sandbox_settings, "1.6.2")

    async def scenario() -> tuple[ScanOutcome, tuple[str, ...]]:
        process = await supervisor.start("restart", plan)
        await wait_for_file(plan.cwd / "ready")
        await asyncio.wait_for(supervisor.shutdown(), timeout=60)
        outcome = await asyncio.wait_for(process.wait(), timeout=60)
        return outcome, supervisor.active_scan_ids()

    outcome, active = asyncio.run(scenario())
    assert outcome.status == "interrupted"
    assert outcome.error_code == "interrupted_by_restart"
    assert active == ()
    assert not (sandbox_settings.console_ephemeral_home_root / "scan-restart").exists()


def test_cleanup_happens_even_if_nobody_awaits(sandbox_settings: Settings) -> None:
    """清理挂在监控任务的 `finally` 上，与有没有人来 `await` 无关。

    写在 `wait()` 里的话，没人 await 的扫描就永远不清 tmpfs —— 那里面有可能被回落
    写入的明文 Key。
    """
    plan = make_plan(sandbox_settings, "orphan", "exit 0")
    supervisor = ScanSupervisor(sandbox_settings, "1.6.2")

    async def scenario() -> None:
        await supervisor.start("orphan", plan)
        deadline = time.monotonic() + 30
        while supervisor.active_scan_ids():
            assert time.monotonic() < deadline, "监控任务一直没收尾"
            await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert not (sandbox_settings.console_ephemeral_home_root / "scan-orphan").exists()
    assert not (plan.cwd / "tmp").exists()


def test_subprocess_env_is_exactly_plan_env(
    sandbox_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`env=plan.env` 是**整份替换**，绝不 `os.environ | plan.env`。

    T9 的 `_PASSTHROUGH_ENV` 白名单就是靠这个成立的 —— 否则 compose 里写一个
    `STRIX_DEBUG=1` 就能把 `strix.log` 拉到 DEBUG（一个泄漏面）。
    """
    monkeypatch.setenv("STRIX_DEBUG", "1")
    plan = make_plan(
        sandbox_settings,
        "env",
        'printf %s "${STRIX_DEBUG-unset}" > seen; exit 0',
        env={"PATH": "/usr/bin:/bin"},
    )

    outcome = run_to_outcome(sandbox_settings, "env", plan)

    assert outcome.exit_code == 0
    assert (plan.cwd / "seen").read_text(encoding="utf-8") == "unset"


# =============================================================================
# 接线：这两条测的不是函数，是"main.py 真的调了它"
#
# 为什么单独一节：上面那三条 `test_assert_sandbox_env_*` 和 `test_shutdown_*` 测的都是
# 函数自己的行为，**把 `main.py` 里那两次调用删掉，它们全部照绿**（收货时实测过）。
# 而那两次调用就是它们的全部价值 —— 没有调用方的守卫等于没有守卫。
# =============================================================================
def test_startup_refuses_when_sandbox_env_is_empty(tmp_path: Path, restore_logging: None) -> None:
    """两个沙箱变量为空 → lifespan 拒绝启动。

    `match` 不是装饰：删掉那次调用后启动会继续往下走、最终死在"没有 auth.json"上，
    只断言异常类型的话这条测试会照绿。
    """
    settings = Settings(console_data_dir=tmp_path)  # 两个沙箱变量默认是空串
    with pytest.raises(RuntimeError, match="STRIX_IMAGE"), TestClient(create_app(settings)):
        pass  # pragma: no cover


class _RecordingSupervisor:
    """只记 `shutdown()` 被调了几次。刻意不继承 `ScanSupervisor`。"""

    def __init__(self) -> None:
        self.shutdown_calls = 0

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


def test_lifespan_shutdown_stops_running_scans(app: FastAPI) -> None:
    """停机时 lifespan 的 `finally` 必须 `await supervisor.shutdown()`。

    不调它的后果不是"少收个尾"：在跑的扫描的 tmpfs 清理挂在各自的监控任务上，
    进程直接退出就等于把测试账号口令、以及可能被 `persist_current()` 回落写入的明文
    Key 留到下一次开机。

    先断言真身接上了，再换成录音机 —— `finally` 是在退出时才读 `app.state.supervisor` 的。
    """
    recorder = _RecordingSupervisor()
    with TestClient(app, base_url="https://testserver"):
        assert isinstance(app.state.supervisor, ScanSupervisor)
        app.state.supervisor = recorder
    assert recorder.shutdown_calls == 1


# ---- 续跑：旧 run.json ----
# 续跑的 cwd 里已经躺着上一轮的 `run.json`（通常 `status=stopped`）。Strix 要到 LLM 验活
# 通过之后才改写它，所以"一启动就失败"的续跑留下的是**旧**状态 —— 它不能进归因。
def _resume_to_outcome(settings: Settings, scan_id: str, plan: LaunchPlan) -> ScanOutcome:
    supervisor = ScanSupervisor(settings, "1.6.2")

    async def scenario() -> ScanOutcome:
        process = await supervisor.start(scan_id, plan, resume=True)
        return await asyncio.wait_for(process.wait(), timeout=60)

    return asyncio.run(scenario())


def test_resume_ignores_untouched_stale_run_status(sandbox_settings: Settings) -> None:
    """I1：验活失败的续跑 = exit 1 + run.json 一个字节没动。旧的 `stopped` 不算数，
    否则会报成"结论不完整，可以继续"，用户会一直重试续跑。"""
    plan = make_plan(sandbox_settings, "resume-fail", "exit 1")
    make_run_dir(plan.cwd, status="stopped")

    outcome = _resume_to_outcome(sandbox_settings, "resume-fail", plan)

    assert outcome.status == "failed"
    assert outcome.run_status is None
    assert outcome.error_code != "scan_incomplete"


def test_resume_counts_run_status_rewritten_this_round(sandbox_settings: Settings) -> None:
    """这一轮真的重写了 run.json（大小变了，三元组必变）→ 新写下的状态照常算数。"""
    plan = make_plan(
        sandbox_settings,
        "resume-rewrite",
        'sleep 0.05; printf \'%s\' \'{"status":"stopped","again":true}\''
        " > strix_runs/strix-run-1/run.json; exit 0",
    )
    make_run_dir(plan.cwd, status="stopped")

    outcome = _resume_to_outcome(sandbox_settings, "resume-rewrite", plan)

    assert outcome.status == "stopped"
    assert outcome.error_code == "scan_incomplete"


def test_first_scan_still_trusts_untouched_run_status(sandbox_settings: Settings) -> None:
    """首次扫描（不传 `resume`）行为不变：同样的预置与脚本仍是 stopped/scan_incomplete。"""
    plan = make_plan(sandbox_settings, "first-stopped", "exit 1")
    make_run_dir(plan.cwd, status="stopped")

    outcome = run_to_outcome(sandbox_settings, "first-stopped", plan)

    assert outcome.status == "stopped"
    assert outcome.error_code == "scan_incomplete"
