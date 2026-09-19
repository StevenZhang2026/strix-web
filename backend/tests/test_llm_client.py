"""`classify_failure()` —— 验活失败的归因码。

这个文件只测那一个纯函数。`verify()` 本身的行为（400 `key_verify_failed`、正文里没有
任何原因描述）在 `test_keys.py` 测过，不在这里重复（`agent-rules.md` §十.4）。

**这里守的不变式只有一条**：归因码的取值范围是「白名单里的固定串」∪「异常类名」，
**异常正文一个字都不许出现在返回值里**。它值得单独一条测试，是因为破坏它不需要谁犯错 ——
只要有人觉得"带上原文更好查"，凭据就跟着 `str(exc)` 一起进了日志。
"""

from __future__ import annotations

import pytest

from app.services.llm_client import classify_failure


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
