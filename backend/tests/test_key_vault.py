"""内存 KeyVault（T7a）。

# 这个文件里没有一个测试需要等待真实时间

全部生命周期断言都注入 `FakeClock`。理由不只是"快"：`KeyVault` 用的是
`time.monotonic`，而 monotonic 的绝对值没有意义 —— 只有差值有。注入一个能任意推进
的时钟，等于把"8 小时之后"变成一个可断言的事实，而不是一个只能靠 sleep 近似的事。

唯一的例外是 `run_sweeper` 那条（它测的就是"周期真的会到"），那里把 interval 注入成
0.01 秒并轮询。`asyncio.run` 而不是 `async def test_`：本仓刻意没有 `pytest-asyncio`
（见 `test_audit.py` 的同一段论证）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.logging_setup import REDACTED, RedactingJsonFormatter, Redactor
from app.main import create_app
from app.services.key_vault import (
    HARD_TTL_SECONDS,
    IDLE_TTL_SECONDS,
    LABEL_ELLIPSIS,
    CredentialSet,
    KeyVault,
    assert_single_worker,
    labels_for,
    secret_label,
)
from app.settings import Settings

# ---- 测试用的凭据值 ----------------------------------------------------------
# `AWS_SECRET` 是**没有可识别形状**的（40 个 base64 字符）—— 任何能匹配它的正则都会
# 把一半的 sha256 也当成凭据，所以 `logging_setup.DEFAULT_PATTERNS` 刻意不含这种形状。
# 用它做脱敏断言，测到的就只能是"精确子串"这条路，也就是 KeyVault 提供的那条。
AWS_SECRET = "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY01"
AWS_ID = "AKIAIOSFODNN7EXAMPLE"
REGION = "us-east-1"


class FakeClock:
    """可任意推进的单调时钟。`KeyVault(clock=...)` 的注入点。"""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_single(secret: str = AWS_SECRET) -> CredentialSet:
    return CredentialSet(
        provider="anthropic",
        auth_shape="single",
        strix_llm="anthropic/claude-sonnet-4-5",
        api_base=None,
        secrets={"LLM_API_KEY": SecretStr(secret)},
        params={},
    )


def make_sigv4() -> CredentialSet:
    """Bedrock SigV4：三个键里两个是机密、一个（区域）不是（§N1）。"""
    return CredentialSet(
        provider="bedrock",
        auth_shape="bedrock_sigv4",
        strix_llm="bedrock/anthropic.claude-sonnet-4-5-v1:0",
        api_base=None,
        secrets={
            "AWS_ACCESS_KEY_ID": SecretStr(AWS_ID),
            "AWS_SECRET_ACCESS_KEY": SecretStr(AWS_SECRET),
        },
        params={"AWS_REGION_NAME": REGION},
    )


# =============================================================================
# 安全不变式
# =============================================================================
def test_secret_values_contains_secrets_but_not_params() -> None:
    vault = KeyVault(clock=FakeClock())
    vault.store(make_sigv4())
    assert vault.secret_values() == frozenset({AWS_ID, AWS_SECRET})
    assert REGION not in vault.secret_values()


def test_stored_secret_is_redacted_out_of_log_lines() -> None:
    """把 vault 当 `SecretProvider` 接进 Formatter，明文就不该出现在日志行里。

    刻意用第三方 logger 名 + `%s` 参数 + `extra` 三条路径：它们是凭据实际进日志的
    三种方式，而 Formatter 是唯一的收口点（见 `logging_setup` 模块 docstring）。
    """
    vault = KeyVault(clock=FakeClock())
    vault.store(make_single())
    formatter = RedactingJsonFormatter(Redactor(secret_provider=vault.secret_values))

    record = logging.LogRecord(
        name="litellm.utils",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="request failed with key=%s",
        args=(AWS_SECRET,),
        exc_info=None,
    )
    record.request_body = AWS_SECRET  # extra= 那条路径

    line = formatter.format(record)
    assert AWS_SECRET not in line
    assert line.count(REDACTED) == 2


def test_vault_never_touches_the_filesystem(tmp_path: Path) -> None:
    """跑完全部生命周期动作，工作目录里一个文件都不许多出来。

    `KeyVault.__init__` 不收任何路径参数，所以这条在结构上就成立；这个测试是那条
    结构性事实的看守 —— 有人哪天加了个"落盘缓存"，它会立刻红。
    """
    vault = KeyVault(clock=FakeClock())
    handle = vault.store(make_sigv4())
    vault.get(handle)
    vault.acquire(handle)
    vault.release(handle)
    vault.sweep()
    vault.drop(handle)
    assert list(tmp_path.iterdir()) == []


def test_repr_does_not_leak_plaintext() -> None:
    """`SecretStr` 保证 repr 里是掩码。这条防的是"有人把字段类型改回 str"。"""
    creds = make_sigv4()
    assert AWS_SECRET not in repr(creds)
    assert AWS_SECRET not in str(creds)

    vault = KeyVault(clock=FakeClock())
    vault.store(creds)
    assert AWS_SECRET not in repr(vault)


def test_labels_for_gives_one_label_per_secret_value() -> None:
    """一组值 → 一组标签，不是一组值一条标签（`PLAN.md:556`）。"""
    labels = labels_for(make_sigv4())
    assert labels == {
        "AWS_ACCESS_KEY_ID": secret_label(AWS_ID),
        "AWS_SECRET_ACCESS_KEY": secret_label(AWS_SECRET),
    }
    assert "AWS_REGION_NAME" not in labels


def test_secret_label_keeps_only_head_and_tail() -> None:
    assert secret_label(AWS_ID) == "AKIA" + LABEL_ELLIPSIS + "MPLE"


def test_secret_label_masks_short_values_entirely() -> None:
    """短值一个字符都不许露：它可能是整个凭据（用户填了占位符 `test`）。"""
    assert secret_label("short-key") == LABEL_ELLIPSIS


def test_store_copies_the_mappings() -> None:
    """调用方之后改自己那个 dict，改不动 vault 里的内容。"""
    secrets: dict[str, SecretStr] = {"LLM_API_KEY": SecretStr(AWS_SECRET)}
    params: dict[str, str] = {"AWS_REGION_NAME": REGION}
    vault = KeyVault(clock=FakeClock())
    handle = vault.store(
        CredentialSet(
            provider="anthropic",
            auth_shape="single",
            strix_llm="anthropic/claude-sonnet-4-5",
            api_base=None,
            secrets=secrets,
            params=params,
        )
    )

    secrets["LLM_API_KEY"] = SecretStr("hijacked")  # 改值
    secrets["AWS_BEARER_TOKEN_BEDROCK"] = SecretStr("smuggled")  # 增键
    params["AWS_REGION_NAME"] = "eu-west-1"

    stored = vault.get(handle)
    assert stored is not None
    assert dict(stored.secrets) == {"LLM_API_KEY": SecretStr(AWS_SECRET)}
    assert dict(stored.params) == {"AWS_REGION_NAME": REGION}
    assert vault.secret_values() == frozenset({AWS_SECRET})


# =============================================================================
# 生命周期
# =============================================================================
def test_handles_are_unguessable_and_unique() -> None:
    vault = KeyVault(clock=FakeClock())
    first = vault.store(make_single())
    second = vault.store(make_single())
    assert first != second
    assert len(first) >= 40  # token_urlsafe(32)


def test_get_unknown_handle_returns_none() -> None:
    assert KeyVault(clock=FakeClock()).get("nope") is None


def test_idle_ttl_expires_and_removes_the_entry() -> None:
    clock = FakeClock()
    vault = KeyVault(clock=clock)
    handle = vault.store(make_single())
    clock.advance(IDLE_TTL_SECONDS + 1)
    assert vault.get(handle) is None
    assert vault.count() == 0


def test_get_refreshes_the_idle_deadline() -> None:
    clock = FakeClock()
    vault = KeyVault(clock=clock)
    handle = vault.store(make_single())
    clock.advance(IDLE_TTL_SECONDS - 1)
    assert vault.get(handle) is not None
    clock.advance(IDLE_TTL_SECONDS - 1)
    assert vault.get(handle) is not None


def test_hard_ttl_is_not_refreshed_by_use() -> None:
    """一直在用也逃不过 24h：hard TTL 从 `created_at` 起算。"""
    clock = FakeClock()
    vault = KeyVault(clock=clock)
    handle = vault.store(make_single())
    step = IDLE_TTL_SECONDS // 2
    for _ in range(HARD_TTL_SECONDS // step):
        clock.advance(step)
        vault.get(handle)
    clock.advance(step)
    assert vault.get(handle) is None


def test_idle_ttl_does_not_apply_while_referenced() -> None:
    """扫描可能几小时不碰 vault，idle 不许在它跑着的时候清掉凭据。"""
    clock = FakeClock()
    vault = KeyVault(clock=clock)
    handle = vault.store(make_single())
    assert vault.acquire(handle) is not None
    clock.advance(IDLE_TTL_SECONDS + 1)
    assert vault.get(handle) is not None


def test_idle_ttl_restarts_after_release() -> None:
    clock = FakeClock()
    vault = KeyVault(clock=clock)
    handle = vault.store(make_single())
    vault.acquire(handle)
    clock.advance(IDLE_TTL_SECONDS + 1)
    vault.release(handle)
    clock.advance(IDLE_TTL_SECONDS + 1)
    assert vault.get(handle) is None


def test_hard_ttl_applies_even_while_referenced() -> None:
    """`ref_count > 0` 不豁免 hard TTL —— 否则一个忘了 release 的任务能让凭据永驻。"""
    clock = FakeClock()
    vault = KeyVault(clock=clock)
    handle = vault.store(make_single())
    vault.acquire(handle)
    clock.advance(HARD_TTL_SECONDS + 1)
    assert vault.sweep() == 1
    assert vault.get(handle) is None


def test_drop_removes_a_referenced_entry() -> None:
    """ "现在忘掉我的 Key"是一个安全动作，不许因为有人在用就降级成建议。"""
    vault = KeyVault(clock=FakeClock())
    handle = vault.store(make_single())
    vault.acquire(handle)
    assert vault.drop(handle) is True
    assert vault.get(handle) is None


def test_drop_is_idempotent() -> None:
    vault = KeyVault(clock=FakeClock())
    handle = vault.store(make_single())
    assert vault.drop(handle) is True
    assert vault.drop(handle) is False


def test_release_is_forgiving() -> None:
    """release 未知 handle 不抛；多余的 release 不会把 ref_count 压到负数。

    后半条是有后果的：若 ref_count 变成 -1，之后一次正常的 acquire 只会把它抬到 0，
    于是那次扫描跑着的时候 idle TTL 仍然生效 —— 凭据会在它眼皮底下消失。
    """
    clock = FakeClock()
    vault = KeyVault(clock=clock)
    vault.release("nope")

    handle = vault.store(make_single())
    vault.release(handle)
    vault.release(handle)
    vault.acquire(handle)
    clock.advance(IDLE_TTL_SECONDS + 1)
    assert vault.get(handle) is not None


def test_sweep_reports_count_and_shrinks_the_redaction_surface() -> None:
    """被清掉的值必须同时从 `secret_values()` 里消失。

    这条把生命周期和脱敏面连起来：脱敏用的是"当前活跃凭据的每一个值"，如果过期项
    留在集合里，那个值就会一直被当成秘密（无害），且一直留在内存里（有害）。
    """
    clock = FakeClock()
    vault = KeyVault(clock=clock)
    vault.store(make_single())
    kept = vault.store(make_single("sk-ant-api03-0123456789abcdefghij"))
    clock.advance(IDLE_TTL_SECONDS - 1)
    vault.get(kept)
    clock.advance(2)

    assert vault.sweep() == 1
    assert vault.count() == 1
    assert AWS_SECRET not in vault.secret_values()


def test_run_sweeper_cleans_up_periodically_and_propagates_cancellation() -> None:
    clock = FakeClock()
    vault = KeyVault(clock=clock)
    vault.store(make_single())
    clock.advance(IDLE_TTL_SECONDS + 1)

    async def scenario() -> None:
        task = asyncio.create_task(vault.run_sweeper(interval=0.01))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if vault.count() == 0:
                break
        task.cancel()
        # cancel 之后必须真的抛 CancelledError：吞掉它的 sweeper 在 lifespan 关闭时
        # 会永远 await 不完，api 停不下来。
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert vault.count() == 0


# =============================================================================
# 启动校验
# =============================================================================
@pytest.mark.parametrize("value", [None, "", "1"])
def test_assert_single_worker_accepts_single_worker(value: str | None) -> None:
    environ = {} if value is None else {"WEB_CONCURRENCY": value}
    assert_single_worker(environ)


@pytest.mark.parametrize("value", ["2", "abc"])
def test_assert_single_worker_rejects_anything_else(value: str) -> None:
    """非整数也拒绝：值拼错说明有人在配它，而我们无法确认结果是 1。"""
    with pytest.raises(RuntimeError, match="WEB_CONCURRENCY"):
        assert_single_worker({"WEB_CONCURRENCY": value})


# =============================================================================
# 接线（main.py）
# =============================================================================
def test_lifespan_wires_the_vault_into_the_redactor(app: FastAPI, anonymous: TestClient) -> None:
    """行为断言而不是"字段是同一个对象"：能被脱敏掉才算接上了。"""
    vault = app.state.key_vault
    vault.store(make_single())
    assert app.state.redactor.redact(f"boom {AWS_SECRET}") == f"boom {REDACTED}"


def test_multi_worker_env_blocks_startup(
    settings: Settings, auth_file: Path, restore_logging: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`WEB_CONCURRENCY=4` 的机器必须起不来，而不是随机 404。"""
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    with pytest.raises(RuntimeError, match="WEB_CONCURRENCY"), TestClient(create_app(settings)):
        pass
