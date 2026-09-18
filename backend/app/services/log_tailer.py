"""按字节偏移续读 Strix 子进程写的 `strix.log`，**脱敏之后**才交出结构化行。

本模块是"`strix.log` → 前端"这条路上唯一的收口点，所以脱敏发生在**离开本模块之前**：
每一行原文整体过一次 `redact`，**再**解析成字段。顺序是反过来的（先解析、再挑几个字段脱敏）
就要求我们枚举"哪些字段可能含凭据"，而那份名单永远不完整 —— `strix.log` 里连 logger 名和
`agent_id` 都是 Strix 那边填的字符串。`[REDACTED]` 不含空格，所以整行替换不会打乱格式。

偏移只在**完整的一行**（以 `\\n` 结尾）被消费后才前进。半行不吐出去：前端会看到半句话，
而且那半句可能把一个凭据切成两半，从而躲过精确子串脱敏。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_LEVEL_RANK: dict[str, int] = {name: rank for rank, name in enumerate(LOG_LEVELS)}

# `strix/telemetry/logging.py:45-46` 的 `_FORMAT` / `_DATEFMT`：
#   "%(asctime)s.%(msecs)03d %(levelname)-7s %(scan_id)s %(agent_id)s %(name)s: %(message)s"
#   "%Y-%m-%d %H:%M:%S"
# `levelname` 是 `-7s` 左对齐补空格 → 级别后面的空格数随级别名长度变化，所以是 ` +`。
# 末尾用 `(?P<logger>\S+): ` 而不是"切第一个冒号"：消息正文里常有 `": "`，
# 而 logger 名不含空格 —— 交给正则回溯挑出最后那个不含空格的 token 就够。
_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) "
    r"(?P<level>" + "|".join(LOG_LEVELS) + r") +"
    r"(?P<scan_id>\S+) (?P<agent_id>\S+) (?P<logger>\S+): (?P<msg>.*)$"
)

# Strix 的 `_StrixContextFilter` 在 scan_id / agent_id 没设时写进 record 的占位符。
_ABSENT = "-"


@dataclass(frozen=True, slots=True)
class LogLine:
    """一行日志。`parsed=False` 表示没匹配上格式（多行 traceback 的后续行就是这种）。"""

    ts: str | None
    level: str
    scan_id: str | None
    agent_id: str | None
    logger: str
    msg: str
    parsed: bool


def _optional(token: str) -> str | None:
    return None if token == _ABSENT else token


def parse_log_line(text: str, *, fallback_level: str) -> LogLine:
    """纯函数。匹配不上就 `parsed=False`、`msg` 是整行原文、`level` 取 `fallback_level`。"""
    match = _LINE_RE.match(text)
    if match is None:
        return LogLine(
            ts=None,
            level=fallback_level,
            scan_id=None,
            agent_id=None,
            logger="",
            msg=text,
            parsed=False,
        )
    return LogLine(
        ts=match["ts"],
        level=match["level"],
        scan_id=_optional(match["scan_id"]),
        agent_id=_optional(match["agent_id"]),
        logger=match["logger"],
        msg=match["msg"],
        parsed=True,
    )


class LogTailer:
    """一个实例跟一次扫描的 `strix.log` 绑定，偏移活在实例里。

    `redact` 从构造参数进来（调用方传 `Redactor.redact`），而不是自己去拿 KeyVault：
    否则测试没法注入假凭据，而"脱敏到底有没有覆盖每个字段"就成了不可测的。
    """

    def __init__(
        self,
        path: Path,
        redact: Callable[[str], str],
        *,
        min_level: str = "INFO",
    ) -> None:
        if min_level not in _LEVEL_RANK:
            raise ValueError(f"unknown min_level: {min_level!r}")
        self._path = path
        self._redact = redact
        self._min_rank = _LEVEL_RANK[min_level]
        self._offset = 0
        # 没解析出格式的行（traceback 续行）继承上一条已解析行的级别，这样 traceback
        # 不会与它的 ERROR 首行分离。开头还没有"上一条"，用 min_level 兜底（= 不丢）。
        self._fallback_level = min_level

    @property
    def offset(self) -> int:
        return self._offset

    async def read_new(self) -> tuple[LogLine, ...]:
        """读出上次之后新增的完整行，脱敏、解析、按最低级别过滤。文件不存在则返回空。"""
        text = await asyncio.to_thread(self._read_complete_lines)
        if not text:
            return ()

        lines: list[LogLine] = []
        # `text` 一定以 `\n` 结尾，`split` 的最后一段必然是空串，丢掉。
        for raw in text.split("\n")[:-1]:
            line = parse_log_line(self._redact(raw), fallback_level=self._fallback_level)
            if line.parsed:
                self._fallback_level = line.level
            if _LEVEL_RANK[line.level] >= self._min_rank:
                lines.append(line)
        return tuple(lines)

    def _read_complete_lines(self) -> str:
        """同步 IO（由 `read_new` 放进线程里跑）。只返回以 `\\n` 结尾的那一截。"""
        try:
            size = self._path.stat().st_size
            if size < self._offset:
                # 被截断或换了文件。接着旧偏移读会从某一行的中间开始，产出乱码行。
                logger.warning(
                    "log file shrank (size=%d < offset=%d), re-reading from 0: %s",
                    size,
                    self._offset,
                    self._path,
                )
                self._offset = 0
            with self._path.open("rb") as handle:
                handle.seek(self._offset)
                data = handle.read()
        except FileNotFoundError:
            # 子进程可能还没开始写。
            return ""

        end = data.rfind(b"\n")
        if end < 0:
            return ""
        consumed = data[: end + 1]
        self._offset += len(consumed)
        # 按字节数记偏移、只消费到最后一个 `\n`：`\n` 这个字节不会出现在 UTF-8 多字节
        # 序列内部，所以切点一定落在字符边界上。坏字节不许让整轮挂掉 → errors="replace"。
        return consumed.decode("utf-8", errors="replace")
