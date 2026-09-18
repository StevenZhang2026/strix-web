"""`LogTailer`（T14b）：按字节偏移续读 `strix.log`，**脱敏在推流之前**。

本文件钉住两条不变式：

- **I-1（安全）**：没有任何字段能绕过脱敏。断言方式只能是"把返回行的**每一个**字段拼起来
  找那个假凭据" —— 逐字段挑着断言，等于在测试里复刻"哪些字段可能含凭据"那份永远不完整的
  名单。`Redactor` 自身的行为已由 `test_redaction.py` 钉住，这里测的是**LogTailer 有没有
  把每个字段都喂进去**。
- **I-2（正确性）**：偏移续读不重发、不漏发、不吐半行；文件变短时从 0 重读。
  第三条与安全是一件事：半行会把一个凭据切成两半，而精确子串脱敏认不出半个凭据。

⚠️ 文件里的"凭据"全是编造的假值（`S105`/`S106` 已在 pyproject 里对 tests/ 关闭）。
`asyncio.run` 而不是 `async def test_`：本仓刻意没有 `pytest-asyncio`（见 `test_audit.py`）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import astuple
from pathlib import Path

import pytest

from app.logging_setup import REDACTED, Redactor
from app.services.log_tailer import LogLine, LogTailer, parse_log_line

# 无形状的假凭据：只有"精确子串"那条路拦得住它（照 test_redaction.py 的同一个形状）。
FAKE_SHAPELESS = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
# 有形状的假凭据：不注册也该被正则拦住 —— 两种都测，才覆盖 Redactor 的两条路。
FAKE_SHAPED = "sk-ant-api03-" + "A" * 40

TS = "2026-09-18 12:34:56.789"

SECRETS = pytest.mark.parametrize(
    "secret", [FAKE_SHAPELESS, FAKE_SHAPED], ids=["shapeless", "shaped"]
)


def make_redact() -> Callable[[str], str]:
    """用**真的** `Redactor`。

    假货（`lambda t: t.replace(FAKE, "[REDACTED]")`）测不出正则那条路，
    于是"凭据从未经过 KeyVault"这个最常见的形状会静默漏过去。
    """
    return Redactor(secret_provider=lambda: frozenset({FAKE_SHAPELESS})).redact


def render(
    *,
    level: str = "INFO",
    scan_id: str = "scan-7f3a",
    agent_id: str = "agent-1",
    name: str = "strix.runtime.tools",
    msg: str = "一段消息正文",
) -> str:
    """按 `strix/telemetry/logging.py:45-46` 的 `_FORMAT` 渲染一行（不含换行符）。

    `%(levelname)-7s` 是**左对齐补空格**，所以级别后面的空格数随级别名长度变化 ——
    `INFO` 后面 5 个、`WARNING` 后面 1 个、`CRITICAL` 后面 1 个。
    """
    return f"{TS} {level:<7} {scan_id} {agent_id} {name}: {msg}"


def append_raw(path: Path, text: str) -> None:
    """原样追加（不补换行符）—— 半行的用例要靠这一点。"""
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)


def all_fields(lines: tuple[LogLine, ...]) -> str:
    """把每一条的每一个字段拼起来。

    用 `astuple` 而不是手列字段名：将来给 `LogLine` 加字段时，这条断言自动覆盖它。
    """
    return "|".join(str(value) for line in lines for value in astuple(line))


# =============================================================================
# 一、parse_log_line：无 IO 的纯函数
# =============================================================================
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            render(),
            (TS, "INFO", "scan-7f3a", "agent-1", "strix.runtime.tools", "一段消息正文", True),
            id="plain_info",
        ),
        pytest.param(
            render(level="WARNING", scan_id="-", agent_id="-", name="strix.llm.compaction"),
            (TS, "WARNING", None, None, "strix.llm.compaction", "一段消息正文", True),
            id="dash_means_none",
        ),
        pytest.param(
            render(level="CRITICAL"),
            (TS, "CRITICAL", "scan-7f3a", "agent-1", "strix.runtime.tools", "一段消息正文", True),
            id="level_name_longer_than_the_pad",
        ),
        pytest.param(
            render(msg="failed: giving up: 3 tries"),
            (
                TS,
                "INFO",
                "scan-7f3a",
                "agent-1",
                "strix.runtime.tools",
                "failed: giving up: 3 tries",
                True,
            ),
            id="colon_inside_the_message",
        ),
        pytest.param(
            '  File "/app/x.py", line 1, in f',
            (None, "ERROR", None, None, "", '  File "/app/x.py", line 1, in f', False),
            id="traceback_continuation_is_unparsed",
        ),
    ],
)
def test_parse_log_line(text: str, expected: tuple[object, ...]) -> None:
    """`-` 必须变成 `None`；匹配不上就整行进 `msg` 且级别取 `fallback_level`。"""
    assert astuple(parse_log_line(text, fallback_level="ERROR")) == expected


# =============================================================================
# 二、I-1：没有任何字段能绕过脱敏
# =============================================================================
POSITIONS: dict[str, Callable[[str], str]] = {
    "in_msg": lambda s: render(msg=f"calling upstream with key={s} ok"),
    "in_logger_name": lambda s: render(name=f"strix.llm.{s}"),
    "in_agent_id": lambda s: render(agent_id=s),
    "in_scan_id": lambda s: render(scan_id=s),
    "in_unparsed_line": lambda s: f'  File "/app/x.py", line 1, in f  # {s}',
}


@SECRETS
@pytest.mark.parametrize("position", list(POSITIONS), ids=list(POSITIONS))
def test_no_field_can_bypass_redaction(tmp_path: Path, position: str, secret: str) -> None:
    """I-1：凭据落在哪个字段里都不许漏出去。"""
    path = tmp_path / "strix.log"
    append_raw(path, POSITIONS[position](secret) + "\n")

    lines = asyncio.run(LogTailer(path, make_redact()).read_new())

    assert len(lines) == 1, "这一行根本没被吐出来，那本条断言就是空的"
    blob = all_fields(lines)
    assert secret not in blob
    assert REDACTED in blob, "负面覆盖：不是靠把整行丢掉来通过的"


# =============================================================================
# 三、I-2：偏移续读
# =============================================================================
def test_missing_file_returns_empty_instead_of_raising(tmp_path: Path) -> None:
    """子进程可能还没开始写 `strix.log`。"""
    tailer = LogTailer(tmp_path / "nope.log", make_redact())

    assert asyncio.run(tailer.read_new()) == ()
    assert tailer.offset == 0


def test_second_read_returns_nothing(tmp_path: Path) -> None:
    """I-2①：同一个文件连读两次，第二次必须为空。"""
    path = tmp_path / "strix.log"
    append_raw(path, render(msg="第一行") + "\n")
    tailer = LogTailer(path, make_redact())

    assert [line.msg for line in asyncio.run(tailer.read_new())] == ["第一行"]
    assert asyncio.run(tailer.read_new()) == ()
    assert tailer.offset == path.stat().st_size


def test_append_returns_only_the_new_lines(tmp_path: Path) -> None:
    """I-2②：追加写入后只返回新增的那些行。"""
    path = tmp_path / "strix.log"
    append_raw(path, render(msg="第一行") + "\n" + render(msg="第二行") + "\n")
    tailer = LogTailer(path, make_redact())
    assert [line.msg for line in asyncio.run(tailer.read_new())] == ["第一行", "第二行"]

    append_raw(path, render(msg="第三行") + "\n")

    assert [line.msg for line in asyncio.run(tailer.read_new())] == ["第三行"]


def test_partial_last_line_is_held_back_until_complete(tmp_path: Path) -> None:
    """I-2③：没有换行符的尾巴不许吐出去。

    这不只是"前端看到半句话"：半行会把凭据切成两半，而精确子串脱敏认不出半个凭据 ——
    所以这里刻意**在凭据中间**截断，断言那半截既没被吐出、补全后也被脱掉了。
    """
    path = tmp_path / "strix.log"
    complete = render(msg="完整的一行")
    pending = render(msg=f"key={FAKE_SHAPELESS}")
    head, rest = pending[:-12], pending[-12:]
    assert FAKE_SHAPELESS not in head, "截断点没落在凭据里，本测试就是空的"
    append_raw(path, complete + "\n" + head)

    tailer = LogTailer(path, make_redact())
    first = asyncio.run(tailer.read_new())
    assert [line.msg for line in first] == ["完整的一行"]
    assert tailer.offset == len((complete + "\n").encode())

    append_raw(path, rest + "\n")
    second = asyncio.run(tailer.read_new())

    assert [line.msg for line in second] == [f"key={REDACTED}"]
    assert FAKE_SHAPELESS not in all_fields(second)


def test_shrunken_file_is_re_read_from_zero(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """I-2④：`size < offset`（被截断／换了文件）→ 从 0 重读并打一条 WARNING。

    接着旧偏移读会从某一行的中间开始，产出乱码行；而乱码行既不可读、又可能把一个凭据
    切成两半从而躲过精确子串脱敏。
    """
    path = tmp_path / "strix.log"
    append_raw(path, render(msg="旧的一行") + "\n" + render(msg="旧的第二行") + "\n")
    tailer = LogTailer(path, make_redact())
    assert len(asyncio.run(tailer.read_new())) == 2

    path.write_text(render(msg="新") + "\n", encoding="utf-8")
    assert path.stat().st_size < tailer.offset, "新内容没比旧偏移短，本测试就是空的"
    with caplog.at_level(logging.WARNING, logger="app.services.log_tailer"):
        again = asyncio.run(tailer.read_new())

    assert [line.msg for line in again] == ["新"]
    assert tailer.offset == path.stat().st_size
    assert [record.levelname for record in caplog.records] == ["WARNING"]


# =============================================================================
# 四、最低级别过滤
# =============================================================================
@pytest.mark.parametrize(
    ("min_level", "expected"),
    [
        pytest.param("DEBUG", ["调试", "错误", "Traceback (most recent call last):"], id="debug"),
        pytest.param("INFO", ["错误", "Traceback (most recent call last):"], id="info"),
        pytest.param("CRITICAL", [], id="critical"),
    ],
)
def test_min_level_filters_and_continuation_inherits_previous_level(
    tmp_path: Path, min_level: str, expected: list[str]
) -> None:
    """未解析的续行继承**上一条已解析行**的级别。

    否则 traceback 的后续行会与它的 ERROR 首行分离：拿 `min_level` 当级别时，
    `min_level="CRITICAL"` 那一格会留下一堆没头没尾的 traceback 行。
    """
    path = tmp_path / "strix.log"
    append_raw(
        path,
        render(level="DEBUG", msg="调试")
        + "\n"
        + render(level="ERROR", msg="错误")
        + "\n"
        + "Traceback (most recent call last):\n",
    )

    lines = asyncio.run(LogTailer(path, make_redact(), min_level=min_level).read_new())

    assert [line.msg for line in lines] == expected
