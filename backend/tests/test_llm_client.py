"""`classify_failure()` —— 验活失败的归因码；`complete()` —— 翻译用的那次模型调用。

前半只测 `classify_failure()` 那个纯函数。`verify()` 本身的行为（400 `key_verify_failed`、正文里没有
任何原因描述）在 `test_keys.py` 测过，不在这里重复（`agent-rules.md` §十.4）。

**这里守的不变式只有一条**：归因码的取值范围是「白名单里的固定串」∪「异常类名」，
**异常正文一个字都不许出现在返回值里**。它值得单独一条测试，是因为破坏它不需要谁犯错 ——
只要有人觉得"带上原文更好查"，凭据就跟着 `str(exc)` 一起进了日志。
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app.services.key_vault import CredentialSet
from app.services.llm_client import CompletionFailed, classify_failure, complete


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # 五条都是实测见过的失败，出处见 `PLAN.md` §交接 与 `pitfalls` 条 22。
        ("litellm.BadRequestError: Invalid AWS region format: us-east-1 ", "bad_region_format"),
        ("Illegal header value b'Bearer abc\\n': Forbidden control character", "control_char"),
        (
            "LLM Provider NOT provided. Pass in the LLM provider you are trying to call.",
            "missing_model_prefix",
        ),
        ("AuthenticationError: Invalid API Key format", "bad_key_format"),
        ("Cannot connect to host api.deepseek.com:443", "endpoint_unreachable"),
    ],
)
def test_whitelisted_messages_map_to_their_code(message: str, expected: str) -> None:
    """白名单命中就回那个码。"""
    assert classify_failure(RuntimeError(message)) == expected


def test_unknown_failure_falls_back_to_the_exception_class_name() -> None:
    """不认识的失败只回类名 —— 类名是静态常量，不可能带着谁的凭据。"""

    class ServiceUnavailableError(Exception):
        pass

    assert (
        classify_failure(ServiceUnavailableError("whatever"))
        == "unclassified:ServiceUnavailableError"
    )


def test_the_exception_message_never_leaks_into_the_code() -> None:
    """**本文件的不变式。** 正文里有凭据形状的字符串时，返回值里一个字都不许有它。

    用两种异常各测一次：不命中白名单的（走类名分支）、以及**命中**白名单的
    （那条路径上 `str(exc)` 刚被读过，最容易顺手把它拼进返回值）。
    """
    secret = "sk-abcdef0123456789"
    assert secret not in classify_failure(RuntimeError(f"auth failed for {secret}"))
    assert secret not in classify_failure(RuntimeError(f"Invalid AWS region format: {secret}"))


# =============================================================================
# complete()：litellm 由 `sys.modules` 里的替身顶上（`import_module` 先查那里），不碰网络。
# =============================================================================
_SECRET = "sk-abcdef0123456789"
_CREDENTIALS = CredentialSet(
    provider="anthropic",
    auth_shape="single",
    strix_llm="anthropic/claude-sonnet-4-5",
    api_base=None,
    secrets={"LLM_API_KEY": SecretStr(_SECRET)},
    params={},
)
_MESSAGES = [{"role": "user", "content": "hi"}]


def _fake_litellm(acompletion: object, completion_cost: object) -> SimpleNamespace:
    return SimpleNamespace(acompletion=acompletion, completion_cost=completion_cost)


def test_complete_failure_carries_no_trace_of_the_original_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**`complete()` 的不变式。** 异常正文与 traceback 帧都带着凭据 —— 抛出去的
    `CompletionFailed` 不许经 args、`__cause__`、`__context__` 任何一条路连回原异常。
    `__context__` 那一格是这条测试存在的理由：在 `except` 块里抛，`from None` 也清不掉它。
    """

    async def acompletion(**kwargs: object) -> object:
        raise RuntimeError(f"auth failed, request was {kwargs}")

    monkeypatch.setitem(sys.modules, "litellm", _fake_litellm(acompletion, None))
    with pytest.raises(CompletionFailed) as caught:
        asyncio.run(complete(_CREDENTIALS, _MESSAGES, 100))
    exc = caught.value
    assert exc.failure_kind == "unclassified:RuntimeError"
    assert _SECRET not in repr(exc.args)
    assert exc.__cause__ is None
    assert exc.__context__ is None


def test_complete_returns_text_usage_and_none_when_cost_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正路：kwargs 走 `_completion_kwargs`；价目表算不出时费用是 `None` 而不是 0。"""
    seen: dict[str, object] = {}

    async def acompletion(**kwargs: object) -> object:
        seen.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"a": 1}'))],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=3),
        )

    def completion_cost(**_: object) -> float:
        raise ValueError("model not in cost map")

    monkeypatch.setitem(sys.modules, "litellm", _fake_litellm(acompletion, completion_cost))
    result = asyncio.run(complete(_CREDENTIALS, _MESSAGES, 100))
    assert (result.text, result.usage_prompt, result.usage_completion) == ('{"a": 1}', 12, 3)
    assert result.cost_usd is None
    assert seen["api_key"] == _SECRET
    assert seen["max_tokens"] == 100
