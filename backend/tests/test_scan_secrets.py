"""`ScanSecretRegistry`：测试账号口令的脱敏来源，以及它在 `main.py` 的接线。

钉住的是泄漏矩阵第 17 行：测试账号口令**不在 KeyVault 里**，所以除非有人把它喂进那个
唯一的 `Redactor`，`ScanSupervisor` 归因失败时打的那行带 `stdout_tail` 的 warning 就会
把它明文写进日志。最后一条测试起真子进程走完那条路。
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.logging_setup import REDACTED, RedactingJsonFormatter, Redactor, SecretProvider

# `scan_launcher` 用模块限定名而不是 `from ... import TestCredential`：pytest 会把测试模块
# 命名空间里任何 `Test*` 类当测试类去收集，然后为它有 `__init__` 打一条
# PytestCollectionWarning。`tests/test_scan_launcher.py` 已经是这个写法。
from app.services import scan_launcher
from app.services.key_vault import CredentialSet, KeyVault
from app.services.scan_secrets import ScanSecretRegistry
from app.services.scan_supervisor import ScanOutcome, ScanSupervisor
from app.settings import Settings

# 编造的假凭据。形状照真的来（长度、字符集），值是键盘敲的。
# 刻意**不**让它们撞上 `DEFAULT_PATTERNS` 里任何一条形状 —— 否则测的就是正则那一路，
# 而这个文件要测的是精确子串那一路。
ADMIN_PASSWORD = "Trombone-Sunset-4417"
VIEWER_PASSWORD = "Kettle-Marigold-8823"
# 4 个字符，短于 `MIN_SECRET_LENGTH`。
SHORT_PASSWORD = "test"
# 40 个 base64 字符、无前缀 —— 只有精确子串能拦住它，正则拦不住。
FAKE_AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

ADMIN = scan_launcher.TestCredential(role="admin", username="alice", password=ADMIN_PASSWORD)
VIEWER = scan_launcher.TestCredential(role="viewer", username="bob", password=VIEWER_PASSWORD)


def empty_base() -> frozenset[str]:
    """一个什么都不提供的 base。测"包住"这一层自己的行为时用它。"""
    return frozenset()


def make_registry(base: SecretProvider = empty_base) -> tuple[ScanSecretRegistry, Redactor]:
    """一个注册表 + 一个只以它为来源的 Redactor（生产里就是这个形状）。"""
    registry = ScanSecretRegistry(base)
    return registry, Redactor(secret_provider=registry.secret_values)


# ---------------------------------------------------------------- 登记与注销


def test_every_credential_password_is_redacted() -> None:
    """**两条**测试账号的口令都要被脱敏，不是只有第一条。"""
    registry, redactor = make_registry()
    registry.register("scan-1", (ADMIN, VIEWER))

    line = f"- role=admin username=alice password={ADMIN_PASSWORD}\n" + (
        f"- role=viewer username=bob password={VIEWER_PASSWORD}"
    )
    redacted = redactor.redact(line)

    assert ADMIN_PASSWORD not in redacted
    assert VIEWER_PASSWORD not in redacted
    assert redacted.count(REDACTED) == 2
    assert registry.count() == 1


def test_role_and_username_are_not_secrets() -> None:
    """`role` / `username` **刻意**不进脱敏集合 —— 报告里要说"用这个账号登录时…"。

    这条测试的作用是让那个决定是**明写的**，免得下一个人以为是漏了。
    """
    registry, redactor = make_registry()
    registry.register("scan-1", (ADMIN, VIEWER))

    assert registry.secret_values() == frozenset({ADMIN_PASSWORD, VIEWER_PASSWORD})
    assert redactor.redact("role=admin username=alice") == "role=admin username=alice"


def test_forget_stops_redacting_that_password() -> None:
    registry, redactor = make_registry()
    registry.register("scan-1", (ADMIN,))
    assert redactor.redact(ADMIN_PASSWORD) == REDACTED

    assert registry.forget("scan-1") is True

    assert redactor.redact(ADMIN_PASSWORD) == ADMIN_PASSWORD
    assert registry.count() == 0


def test_forget_is_idempotent() -> None:
    """终态可能被多条路径观察到，重复注销不能报错。"""
    registry, _ = make_registry()
    registry.register("scan-1", (ADMIN,))

    assert registry.forget("scan-1") is True
    assert registry.forget("scan-1") is False
    assert registry.forget("scan-never-existed") is False


def test_forget_one_scan_keeps_the_other() -> None:
    registry, redactor = make_registry()
    registry.register("scan-1", (ADMIN,))
    registry.register("scan-2", (VIEWER,))

    registry.forget("scan-1")

    assert redactor.redact(VIEWER_PASSWORD) == REDACTED
    assert redactor.redact(ADMIN_PASSWORD) == ADMIN_PASSWORD


def test_base_provider_still_applies() -> None:
    """ "包住"不能把原来那半弄丢：KeyVault 的 Key 照样要被脱敏。"""
    vault = KeyVault()
    vault.store(
        CredentialSet(
            provider="bedrock",
            auth_shape="sigv4",
            strix_llm="bedrock/anthropic.claude-sonnet-4-5",
            api_base=None,
            secrets={"AWS_SECRET_ACCESS_KEY": SecretStr(FAKE_AWS_SECRET)},
            params={"AWS_REGION": "us-east-1"},
        )
    )
    registry, redactor = make_registry(vault.secret_values)
    registry.register("scan-1", (ADMIN,))

    assert registry.secret_values() == frozenset({FAKE_AWS_SECRET, ADMIN_PASSWORD})
    assert redactor.redact(f"{FAKE_AWS_SECRET} {ADMIN_PASSWORD}") == f"{REDACTED} {REDACTED}"


def test_short_password_is_not_redacted() -> None:
    """短于 `MIN_SECRET_LENGTH` 的口令**不会**被替换 —— 已知且接受的事实。

    理由在 `logging_setup.MIN_SECRET_LENGTH`：一个 4 字符的"秘密"会把日志里每一处那个
    子串都换掉，而不可读的日志等于没有日志。本注册表刻意**不**在自己这一层再挡一次
    （同一条规则的第二份副本）；请求体该不该拒短口令是请求模型（T12c）的事。
    这条测试写在这里是为了让这个事实是明写的，而不是意外。
    """
    registry, redactor = make_registry()
    registry.register(
        "scan-1",
        (scan_launcher.TestCredential(role="admin", username="a", password=SHORT_PASSWORD),),
    )

    assert SHORT_PASSWORD in registry.secret_values()
    assert redactor.redact(f"password={SHORT_PASSWORD}") == f"password={SHORT_PASSWORD}"


# ---------------------------------------------------------------- 接线


def test_main_wires_the_registry_into_the_redactor(app: FastAPI, anonymous: TestClient) -> None:
    """`app.state.redactor` 必须真的以 `app.state.scan_secrets` 为来源。

    `anonymous` 只为了让 lifespan 跑完（`with` 已经进过）。删掉 main.py 里那次接线，
    这条测试就红 —— 本仓三次栽在"声明了、接线了，但没有任何一处强制"上。
    """
    registry: ScanSecretRegistry = app.state.scan_secrets
    redactor: Redactor = app.state.redactor

    registry.register("scan-wired", (ADMIN,))
    try:
        assert redactor.redact(f"password={ADMIN_PASSWORD}") == f"password={REDACTED}"
    finally:
        registry.forget("scan-wired")


# ---------------------------------------------------------------- 真子进程：泄漏矩阵 #17


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


@pytest.fixture
def root_capture(restore_logging: None) -> Iterator[tuple[io.StringIO, ScanSecretRegistry]]:
    """把 root 的输出收进内存，用**真的** `RedactingJsonFormatter` + 一个注册表当来源。

    `restore_logging` 是硬要求：这里动的是 root logger 这个进程级全局状态。
    """
    registry = ScanSecretRegistry(empty_base)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingJsonFormatter(Redactor(secret_provider=registry.secret_values)))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    yield stream, registry
    root.removeHandler(handler)
    handler.close()


def make_plan(settings: Settings, scan_id: str, script: str) -> scan_launcher.LaunchPlan:
    """一个指向 `/bin/sh -c <script>` 的假 LaunchPlan，目录布局与 `prepare_workspace` 一致。

    刻意不调 `build_launch_plan`：那要凭据与模板（T9 已经测透了）。
    """
    root = settings.console_ephemeral_home_root / f"scan-{scan_id}"
    home = root / "home"
    config_dir = home / ".strix"
    cwd = settings.scans_dir / scan_id
    for directory in (root, home, config_dir, cwd, cwd / "tmp"):
        directory.mkdir(parents=True, exist_ok=True)
    env: Mapping[str, str] = {"PATH": "/usr/bin:/bin"}
    return scan_launcher.LaunchPlan(
        argv=("/bin/sh", "-c", script),
        argv_preview=("/bin/sh", "-c", script),
        env=env,
        env_var_names=tuple(sorted(env)),
        cwd=cwd,
        home=home,
        config_path=config_dir / "cli-config.json",
        instruction_path=root / "instruction.txt",
        instruction_sha256="0" * 64,
    )


def run_to_outcome(settings: Settings, scan_id: str, plan: scan_launcher.LaunchPlan) -> ScanOutcome:
    """本仓刻意没有 `pytest-asyncio`，所以自己 `asyncio.run`。"""
    supervisor = ScanSupervisor(settings, "1.6.2")

    async def scenario() -> ScanOutcome:
        process = await supervisor.start(scan_id, plan)
        return await asyncio.wait_for(process.wait(), timeout=60)

    return asyncio.run(scenario())


def test_stdout_tail_warning_is_redacted(
    sandbox_settings: Settings,
    root_capture: tuple[io.StringIO, ScanSecretRegistry],
) -> None:
    """Strix 把指令正文回显进 stdout + 归因失败 → 那行 warning 里的口令必须已被换掉。

    ⚠️ 这条测试极易写成空断言：那行 warning 根本没打出来时，"口令不在输出里"会照样
    通过。所以先断言**捕获到的 JSON 行里有 `stdout_tail` 字段**（证明那件事真的发生了），
    再断言它的值。
    """
    stream, registry = root_capture
    scan_id = "scan-tail"
    registry.register(scan_id, (ADMIN, VIEWER))

    # 假 strix：回显指令正文里那两行凭据，然后 `exit 1`。输出里刻意不含任何已登记的
    # 错误特征，也不建 run 目录 → `run_status is None` → 归因落到 scan_failed_unknown。
    script = (
        f'echo "- role=admin username=alice password={ADMIN_PASSWORD}"; '
        f'echo "- role=viewer username=bob password={VIEWER_PASSWORD}"; '
        "exit 1"
    )
    outcome = run_to_outcome(
        sandbox_settings, scan_id, make_plan(sandbox_settings, scan_id, script)
    )

    assert outcome.error_code == "scan_failed_unknown"

    captured = stream.getvalue()
    records = [json.loads(line) for line in captured.splitlines() if line.strip()]
    tails = [record["stdout_tail"] for record in records if "stdout_tail" in record]
    assert tails, f"带 stdout_tail 的那行 warning 没有打出来，捕获到的是：{captured!r}"

    tail = tails[0]
    assert REDACTED in tail
    assert ADMIN_PASSWORD not in tail
    assert VIEWER_PASSWORD not in tail
    # 整个捕获流都不许出现口令（别的日志字段也不许漏）。
    assert ADMIN_PASSWORD not in captured
    assert VIEWER_PASSWORD not in captured
