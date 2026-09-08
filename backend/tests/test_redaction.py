"""脱敏测试。

本文件里最重要的一条是 `test_child_logger_output_is_redacted` ——
它证明"脱敏挂在 Formatter 上"是必须的，而挂在 root logger 的 Filter 上是**静默无效**的。
那条差别已经实测过：`logging.Logger.filter()` 只对直接调用该 logger 的记录生效，
子 logger 的记录经 `callHandlers()` 直接交给各级 handler，**完全绕过父 logger 的
filters**。而凭据泄漏恰恰来自 `litellm` / `httpx` 这些第三方子 logger。

⚠️ 本文件里的"凭据"全是编造的假值（`S105`/`S106` 已在 pyproject 里对 tests/ 关闭）。
"""

from __future__ import annotations

import io
import json
import logging
import sys
import time
from collections.abc import Iterator

import pytest

from app.logging_setup import (
    MIN_SECRET_LENGTH,
    REDACTED,
    RedactingJsonFormatter,
    Redactor,
    configure_logging,
    trace_id_var,
)

# 编造的假凭据。形状照真的来（长度、字符集），值是键盘敲的。
FAKE_ANTHROPIC = "sk-ant-api03-" + "A" * 40
FAKE_OPENAI = "sk-" + "B" * 40
FAKE_GEMINI = "AIza" + "C" * 31
FAKE_POSTMAN = "PMAK-" + "D" * 30
FAKE_AKIA = "AKIA" + "E" * 16
FAKE_ASIA = "ASIA" + "F" * 16
FAKE_ABSK = "ABSK" + "G" * 40
# AWS secret access key：40 个 base64 字符，**没有可识别前缀**。
# 这就是为什么"精确子串"来源不可省 —— 任何能匹配它的正则都会把一半的 sha256
# 也当成凭据。
FAKE_AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def make_redactor(*secrets: str) -> Redactor:
    frozen = frozenset(secrets)
    return Redactor(secret_provider=lambda: frozen)


# =============================================================================
# 一、正则来源：认得出形状的凭据
# =============================================================================
@pytest.mark.parametrize(
    "value",
    [FAKE_ANTHROPIC, FAKE_OPENAI, FAKE_GEMINI, FAKE_POSTMAN, FAKE_AKIA, FAKE_ASIA, FAKE_ABSK],
)
def test_known_shapes_are_redacted_without_any_registered_secret(value: str) -> None:
    """正则来源不依赖 KeyVault。

    这一路的价值：拦得住**我们从没见过**的值 —— 比如用户把 Key 误贴进目标 URL，
    那个值从未经过 POST /api/keys，KeyVault 里没有它。
    """
    redactor = make_redactor()  # 刻意不注册任何秘密
    assert value not in redactor.redact(f"boom: {value} tail")


def test_asia_temporary_credentials_are_covered() -> None:
    """ASIA（STS 临时凭据）极容易被漏掉，而它同样能调 Bedrock。"""
    assert make_redactor().redact(FAKE_ASIA) == REDACTED


def test_absk_bedrock_bearer_is_covered() -> None:
    """ABSK 是 §N1 第三种 auth_shape（AWS_BEARER_TOKEN_BEDROCK）的前缀。"""
    assert make_redactor().redact(FAKE_ABSK) == REDACTED


# =============================================================================
# 二、精确子串来源：没有可识别形状的凭据
# =============================================================================
def test_shapeless_secret_needs_exact_substring_source() -> None:
    """AWS secret access key 只能靠精确子串拦住。

    先证明正则拦不住它（否则下面那半条断言就是空的），再证明注册之后拦住了。
    """
    assert FAKE_AWS_SECRET in make_redactor().redact(FAKE_AWS_SECRET)
    assert FAKE_AWS_SECRET not in make_redactor(FAKE_AWS_SECRET).redact(FAKE_AWS_SECRET)


def test_all_values_of_one_handle_are_redacted() -> None:
    """一个 vault_handle 装一**组**凭据，每一个值都要脱敏。

    Bedrock SigV4 要 id + secret 两个值（§N1）。只脱敏"主 Key"是一个很自然的错误 ——
    在改名之前，`key_handle` 这个名字正是在诱导这个错误。
    """
    redactor = make_redactor(FAKE_AKIA, FAKE_AWS_SECRET)
    line = f"AWS_ACCESS_KEY_ID={FAKE_AKIA} AWS_SECRET_ACCESS_KEY={FAKE_AWS_SECRET}"
    out = redactor.redact(line)
    assert FAKE_AKIA not in out
    assert FAKE_AWS_SECRET not in out


def test_degenerate_short_value_does_not_nuke_the_log() -> None:
    """用户在凭据框里填了个占位符时，日志不能被毁掉。

    `test` 会被当成秘密注册进来。若不设长度下限，日志里每一个 `test`
    （含 `pytest`、`latest`、`fastest`）都会变成 [REDACTED] —— 日志彻底不可读，
    而不可读的日志等于没有日志。
    """
    placeholder = "test"
    assert len(placeholder) < MIN_SECRET_LENGTH
    redactor = make_redactor(placeholder)
    assert redactor.redact("image=strix-sandbox:latest") == "image=strix-sandbox:latest"


def test_value_at_length_threshold_participates() -> None:
    """负面覆盖：刚好达到下限的值必须被脱敏。

    没有这条的话，把 MIN_SECRET_LENGTH 误设成 100 也能让上面那条测试通过。
    """
    borderline = "A" * MIN_SECRET_LENGTH
    assert make_redactor(borderline).redact(f"v={borderline}") == f"v={REDACTED}"


def test_longest_secret_replaced_first() -> None:
    """两个秘密互为前缀时，必须先替长的。

    反过来的话，短的先替会把长的切成两半，两半都不再匹配任何规则 —— 结果是
    "注册了更多秘密反而泄漏了"。这个失败模式极不直观，所以要有测试钉住。
    """
    long_secret = "ZZZZZZZZZZZZZZZZ-tail-part"
    short_secret = "ZZZZZZZZZZZZZZZZ"
    redactor = make_redactor(long_secret, short_secret)
    out = redactor.redact(f"v={long_secret}")
    assert "tail-part" not in out
    assert out == f"v={REDACTED}"


# =============================================================================
# 三、Formatter：唯一的序列化出口
# =============================================================================
@pytest.fixture
def captured() -> Iterator[tuple[io.StringIO, logging.Handler]]:
    """一个把 JSON 行写进内存的 handler，用我们真正的 Formatter。"""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingJsonFormatter(make_redactor(FAKE_AWS_SECRET)))
    yield stream, handler
    handler.close()


def test_output_is_one_line_of_valid_json(captured: tuple[io.StringIO, logging.Handler]) -> None:
    stream, handler = captured
    record = logging.LogRecord("t", logging.INFO, "f.py", 1, "hello %s", ("world",), None)
    handler.emit(record)
    line = stream.getvalue().strip()
    assert "\n" not in line
    payload = json.loads(line)
    assert payload["msg"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "t"
    assert payload["ts"].endswith("Z")


def test_message_args_are_redacted(captured: tuple[io.StringIO, logging.Handler]) -> None:
    """凭据经 `%s` 参数传进来时也要脱敏。

    这是最常见的写法（`logger.info("key=%s", value)`），也最容易被"只脱敏 msg
    模板"的实现漏掉 —— 模板里根本没有凭据。
    """
    stream, handler = captured
    record = logging.LogRecord("t", logging.INFO, "f.py", 1, "key=%s", (FAKE_AWS_SECRET,), None)
    handler.emit(record)
    assert FAKE_AWS_SECRET not in stream.getvalue()


def test_traceback_is_redacted(captured: tuple[io.StringIO, logging.Handler]) -> None:
    """traceback 是凭据泄漏概率最高的一处。

    litellm 会把请求参数（含凭据）塞进异常信息。`formatException()` 的输出必须和
    message 一起过脱敏 —— 这是最容易漏的一处，因为它不在 `record.msg` 里。
    """
    stream, handler = captured
    try:
        raise ValueError(f"upstream said: {FAKE_AWS_SECRET}")
    except ValueError:
        record = logging.LogRecord("t", logging.ERROR, "f.py", 1, "boom", None, sys.exc_info())
    handler.emit(record)
    out = stream.getvalue()
    assert FAKE_AWS_SECRET not in out
    assert "ValueError" in out  # 负面覆盖：traceback 本身没被整段丢掉


def test_extra_fields_are_redacted(captured: tuple[io.StringIO, logging.Handler]) -> None:
    """经 `extra=` 传进来的任意字段也要脱敏。

    这一条正是"先 json.dumps 再脱敏整个字符串"的理由：逐字段脱敏要求我们枚举出
    所有可能含凭据的字段名，而那份名单永远不完整 —— 下一个人加个
    `extra={"request_body": ...}` 就漏了。
    """
    stream, handler = captured
    record = logging.LogRecord("t", logging.INFO, "f.py", 1, "req", None, None)
    record.request_body = f"secret={FAKE_AWS_SECRET}"
    handler.emit(record)
    out = stream.getvalue()
    assert FAKE_AWS_SECRET not in out
    assert "request_body" in out


def test_trace_id_is_included(captured: tuple[io.StringIO, logging.Handler]) -> None:
    stream, handler = captured
    token = trace_id_var.set("abc123")
    try:
        handler.emit(logging.LogRecord("t", logging.INFO, "f.py", 1, "x", None, None))
    finally:
        trace_id_var.reset(token)
    assert json.loads(stream.getvalue())["trace_id"] == "abc123"


def test_ts_is_really_utc_even_under_a_non_utc_tz(
    captured: tuple[io.StringIO, logging.Handler],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ts` 缀了 `Z`，那它必须真的是 UTC。

    这条测试**刻意把进程时区改成非 UTC**。不改的话它在容器里是恒真的（容器默认
    `TZ` 未设 = UTC），等于没测 —— 而 `logging.Formatter.converter` 默认恰恰是
    `time.localtime`，那个 bug 在 UTC 环境下完全看不出来。

    对账依赖这一点：DB 时间戳一律 UTC（001_init.sql 约定 2），`audit_log` 还要与
    `${DATA}/audit/YYYY-MM.ndjson` 双写。日志与 DB 差一个时区偏移且没人知道，
    是排查时最费人的一类问题。
    """
    stream, handler = captured

    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    monkeypatch.setattr(time, "tzset", lambda: None, raising=True)  # 防被别处改回去
    # 自证前提：这个时区确实与 UTC 有偏移，否则本测试是空的。
    created = 1_757_000_000.0  # 固定值，避免"刚好跨秒"的抖动
    assert time.localtime(created)[3] != time.gmtime(created)[3], "选的时区没偏移，测试无意义"

    record = logging.LogRecord("t", logging.INFO, "f.py", 1, "x", None, None)
    record.created = created
    record.msecs = 0.0
    handler.emit(record)

    ts = json.loads(stream.getvalue())["ts"]
    expected = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(created)) + ".000Z"
    assert ts == expected, f"ts 不是 UTC：得到 {ts}，UTC 应为 {expected}"


# =============================================================================
# 四、configure_logging：真正把 handler 装到 root 上
# =============================================================================
def test_child_logger_output_is_redacted(restore_logging: None) -> None:
    """**本文件最重要的一条。**

    从一个**子** logger 打日志（模拟 litellm），断言输出被脱敏。

    这条测试的存在意义是它的反面：如果脱敏被实现成
    `logging.getLogger().addFilter(RedactionFilter())`，这条测试会**失败** ——
    因为子 logger 的记录经 `callHandlers()` 沿 parent 链直接交给各级 handler，
    从不看父 logger 的 filters（CPython `logging/__init__.py`）。
    而那种写法的可怕之处在于它"看起来能过测试"：只要测试里直接
    `logging.info(...)`，就永远发现不了问题。所以这里刻意用子 logger。
    """
    configure_logging("INFO", make_redactor(FAKE_AWS_SECRET))
    root = logging.getLogger()
    assert len(root.handlers) == 1
    stream = io.StringIO()
    handler = root.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    handler.setStream(stream)

    logging.getLogger("litellm.utils").warning("upstream: %s", FAKE_AWS_SECRET)

    out = stream.getvalue()
    assert FAKE_AWS_SECRET not in out
    assert REDACTED in out


def test_configure_logging_is_idempotent(restore_logging: None) -> None:
    """调两次不应留下两个 handler。

    `create_app()` 在测试里会被调多次。不幂等的话日志行数会翻倍 ——
    一个只在集成测试里才显形、又很难归因的现象。
    """
    redactor = make_redactor()
    configure_logging("INFO", redactor)
    configure_logging("INFO", redactor)
    assert len(logging.getLogger().handlers) == 1


def test_uvicorn_loggers_are_rewired(restore_logging: None) -> None:
    """`uvicorn` / `uvicorn.error` 必须被接管。

    它们默认自带 handler 且 `propagate=False`。不动它们的话，uvicorn 的输出会绕过
    我们的 formatter —— 也就是**绕过脱敏**，同时日志里会同时出现 JSON 行和
    uvicorn 的彩色纯文本两种格式。
    """
    configure_logging("INFO", make_redactor())
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        assert logger.handlers == [], f"{name} 还留着自己的 handler"
        assert logger.propagate is True, f"{name} 的 propagate 没打开"


def test_access_log_is_silenced_not_rewired(restore_logging: None) -> None:
    """`uvicorn.access` 必须被**静音**，不是被接管。

    这条是一个实测过的回归：`--no-access-log` 的全部实现就是把 `uvicorn.access` 的
    handlers 清空 + `propagate = False`（uvicorn/config.py）。而 `configure_logging()`
    在 lifespan 里跑，**晚于** uvicorn 的配置 —— 早先的版本在这里把 propagate 改回
    True，于是 CMD 里写着 `--no-access-log`、日志里照样出现
    `"GET /api/health HTTP/1.1" 200`。访问日志记 URL，URL 是泄漏面。

    这条测试的价值全在它的反面：把 propagate 改回 True 时，它会红。
    """
    configure_logging("INFO", make_redactor())
    access = logging.getLogger("uvicorn.access")
    assert access.handlers == []
    assert access.propagate is False


def test_access_log_record_reaches_no_handler(restore_logging: None) -> None:
    """负面覆盖：uvicorn.access 打出来的记录不能落到 root 的 handler 上。

    只断言 `propagate is False` 是在测实现细节；这条测的是**行为** ——
    换一种静音手段（比如 `disabled = True`）也必须让它继续通过。
    """
    configure_logging("INFO", make_redactor())
    root = logging.getLogger()
    handler = root.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    stream = io.StringIO()
    handler.setStream(stream)

    logging.getLogger("uvicorn.access").info('127.0.0.1 - "GET /api/scans HTTP/1.1" 200')

    assert stream.getvalue() == ""


def test_noisy_third_party_loggers_are_quieted(restore_logging: None) -> None:
    """降噪与脱敏是两件事，别把前者当后者。

    这几个 logger 被压到 WARNING 只是为了日志可读；它们的记录**仍然**经过同一个
    handler、同一个脱敏。把"我把它关小声了"当成安全措施是一个典型的错误结论。
    """
    configure_logging("INFO", make_redactor())
    for name in ("litellm", "httpx", "docker"):
        assert logging.getLogger(name).level == logging.WARNING
