"""结构化 JSON 日志 + 脱敏。

# 为什么脱敏挂在 Formatter 上，而不是 root logger 的 Filter 上

这是本文件唯一需要论证的设计，也是一个**实测过的坑**：

`logging.Logger.filter()` 只对**直接调用该 logger** 的记录生效。子 logger（比如
`litellm.utils`）的记录是经 `callHandlers()` 沿 parent 链**直接交给各级 handler** 的，
**完全绕过父 logger 的 filters**（CPython `logging/__init__.py`：`callHandlers` 遍历
`c.handlers` 并调 `hdlr.handle(record)`，从不看 `c.filters`）。

所以 `logging.getLogger().addFilter(RedactionFilter())` 只能拦住 `logging.info(...)`
这种直接打到 root 的记录 —— 而凭据泄漏恰恰来自 `litellm` / `httpx` 这些第三方子 logger。
那种写法是**静默无效**的：测试会过（因为测试通常直接打 root），生产会漏。

Handler / Formatter 才是真正的收口点：所有记录最终都要经过 handler 的 formatter 序列化。
把脱敏放在"序列化成字符串"的那一步，等于放在唯一的出口上。

# 三个脱敏来源，缺一不可

1. **正则** —— 认得出形状的凭据（`sk-ant-…`、`AKIA…`）。它拦得住**我们没见过**的值，
   比如用户误把 Key 贴进目标 URL。
2. **精确子串** —— KeyVault 里每个活跃凭据的**每一个值**。一个 `vault_handle` 可能装
   2–3 个值（Bedrock SigV4 要 id + secret + region）。这是唯一能拦住
   **没有可识别形状**的凭据的手段 —— AWS secret access key 就是 40 个 base64 字符，
   任何能匹配它的正则都会把一半的 sha256 也当成凭据。
3. **traceback** —— 异常文本里带凭据的概率最高（litellm 会把请求参数塞进异常信息）。
   `formatException()` 的输出必须和 message 一起过脱敏，这是最容易漏的一处。

# 凭据来源怎么接进来，而不引入模块级可变全局状态

`Redactor` 持有一个 `SecretProvider`（无参可调用，返回当前全部活跃凭据值）。
T2 传 `lambda: frozenset()`；T7 的 KeyVault 落地后传 `key_vault.secret_values`。
**注册表不在本模块里** —— 本模块只知道"有人会告诉我当前的秘密是什么"。
这样就没有模块级 `_SECRETS: set[str]` 那种任何代码都能改、测试之间会互相污染的状态。
`Redactor` 实例挂在 `app.state.redactor` 上，T14 推给前端之前复用同一个实例。
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from collections.abc import Callable
from contextvars import ContextVar

# 返回"当前全部活跃凭据的明文值"。由 KeyVault（T7）实现。
SecretProvider = Callable[[], frozenset[str]]

REDACTED = "[REDACTED]"

# 短于这个长度的值**不参与**精确子串替换。
#
# 这不是为了排除 region 之类的非凭据参数 —— 那些走 `params{}` 而不是 `secrets{}`
# （§N1），根本不会进 `secret_provider()`。它防的是另一件事：
# 用户在凭据输入框里填了一个退化的短值（占位符 `test`、误敲的 `1`）。那个值会被当成
# 秘密注册进来，然后把日志里每一个 `t`、每一个 `1` 都替换成 [REDACTED] ——
# 日志彻底不可读，而不可读的日志等于没有日志。
# 真正的凭据都远长于 8，所以这条下限不会漏掉任何真凭据。
MIN_SECRET_LENGTH = 8

# 形状可识别的凭据。刻意**不**包含"40 个 base64 字符"这类通用形状 —— 见 docstring。
DEFAULT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Anthropic。必须排在 sk- 之前？不必 —— 两条都替换成同一个字符串，顺序无关。
    # 但保留分开写：将来若要按类型给不同标签，就需要区分。
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),  # OpenAI / DeepSeek / OpenRouter
    re.compile(r"AIza[A-Za-z0-9_\-]{20,}"),  # Google / Gemini
    re.compile(r"PMAK-[A-Za-z0-9]{8,}"),  # Postman（Strix 会吃 Postman collection）
    # AWS access key id：AKIA = 长期凭据，ASIA = STS 临时凭据。
    # ASIA 很容易被漏掉，而临时凭据同样能调 Bedrock。
    re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    # Bedrock bearer token（AWS_BEARER_TOKEN_BEDROCK，§N1 的第三种 auth_shape）。
    re.compile(r"ABSK[A-Za-z0-9+/=_\-]{8,}"),
)

# 当前请求的 trace_id。
#
# 这是本项目唯一被批准的"模块级可变状态"例外，理由：ContextVar 的值是**每个异步任务
# 独立**的，不存在跨请求污染 —— 它在语义上是"当前上下文"，不是全局变量。
# 替代方案（把 trace_id 一路当参数传到每个 log 调用）会污染每一个函数签名。
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")

# logging.LogRecord 的内建属性。凡不在此列的都是调用方经 extra= 传进来的，
# 要一并序列化进 JSON。硬编码这份名单比"try 序列化整个 __dict__"可控。
_RECORD_BUILTIN_ATTRS: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class Redactor:
    """把文本里的凭据替换成 `[REDACTED]`。

    `redact()` 是纯函数（除了调 `secret_provider`），所以能不起服务、不碰日志系统
    直接单测 —— 这是"好测是硬要求"（CLAUDE.md §Python）的落地。
    """

    def __init__(
        self,
        secret_provider: SecretProvider,
        patterns: tuple[re.Pattern[str], ...] = DEFAULT_PATTERNS,
    ) -> None:
        self._secret_provider = secret_provider
        self._patterns = patterns

    def redact(self, text: str) -> str:
        if not text:
            return text

        # 精确子串**先于**正则。
        # 顺序有意义：先把已知的完整凭据整体替换掉，剩下的才交给正则。反过来的话，
        # 正则可能只吃掉凭据的一个前缀（比如 `sk-ant-` 后面跟的字符里有个 `.`，
        # 正则停在那里），剩下的尾巴就漏出去了。
        #
        # 按长度**降序**：若两个凭据互为前缀（bearer token 与它的截断形式），
        # 先替长的，否则短的先替会把长的切成两半，两半都不再匹配任何规则。
        secrets = sorted(
            (s for s in self._secret_provider() if len(s) >= MIN_SECRET_LENGTH),
            key=len,
            reverse=True,
        )
        for secret in secrets:
            text = text.replace(secret, REDACTED)

        for pattern in self._patterns:
            text = pattern.sub(REDACTED, text)

        return text


class RedactingJsonFormatter(logging.Formatter):
    """把 LogRecord 序列化成一行 JSON，**整体**过一次脱敏。

    为什么是"先 json.dumps 再脱敏整个字符串"，而不是逐字段脱敏：
    逐字段要求我们枚举出所有可能含凭据的字段，而那份名单永远不完整（下一个人加个
    `extra={"request_body": ...}` 就漏了）。对最终字符串做替换，则**没有任何字段能
    绕过它** —— 这是"默认安全"和"记得加白名单"的区别。

    代价：脱敏会看到 JSON 转义后的文本。若凭据里含 `"` 或 `\\`，转义后精确子串就匹配
    不上了。实际凭据的字符集（base64 / 十六进制 / `-` / `_`）不含这两个字符，所以这个
    代价是零。**若将来支持含引号的凭据形状，这条假设必须重新验。**
    """

    # `logging.Formatter.converter` 默认是 `time.localtime`，于是 `formatTime()` 产出的是
    # **本地时间**。下面那行时间戳后面缀了个 `Z`，宣称自己是 UTC —— 两者不改就是在说谎。
    #
    # 现在测不出来只是因为容器里 `TZ` 恰好没设（默认 UTC）。给 compose 加一个 `TZ`、
    # 或换一台会把 TZ 传进容器的宿主，日志时间就会整体偏移而**标签仍写 Z**。
    # 已实测：`TZ=Asia/Shanghai` 时真实 UTC `04:26:34`，日志写的是 `12:26:34Z`。
    #
    # 这件事的代价不在日志本身，而在对账：DB 的时间戳一律是 UTC ISO-8601
    # （001_init.sql 约定 2），`audit_log` 还要与 `${DATA}/audit/YYYY-MM.ndjson` 双写。
    # 日志与 DB 差一个时区偏移、且没人知道差了，是排查时最费人的一类问题。
    converter = staticmethod(time.gmtime)

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            # 用 UTC 且带 Z，与 DB 里的时间戳格式一致（001_init.sql 约定 2）。
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),  # 已做完 %-格式化，args 一并进来
            "trace_id": trace_id_var.get(),
        }

        # traceback 是凭据泄漏概率最高的一处（litellm 把请求参数塞进异常信息）。
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        for key, value in record.__dict__.items():
            if key not in _RECORD_BUILTIN_ATTRS and not key.startswith("_"):
                payload[key] = value

        # default=str：extra 里可能有 Path / datetime / 自定义对象。让它们变成字符串
        # 而不是让整条日志因 TypeError 消失 —— 日志系统自身抛异常是最糟的失败模式。
        line = json.dumps(payload, ensure_ascii=False, default=str)
        return self._redactor.redact(line)


def configure_logging(level: str, redactor: Redactor) -> None:
    """接管 root logger 与 uvicorn 的三个 logger。

    必须显式处理 uvicorn：它的 `uvicorn` / `uvicorn.error` / `uvicorn.access` 三个
    logger 自带 handler 且 `propagate=False`（见 uvicorn 的默认 LOGGING_CONFIG）。
    不动它们的话，uvicorn 的输出会绕过我们的 formatter —— 也就是**绕过脱敏**，
    而且日志里会同时出现 JSON 行和 uvicorn 的彩色纯文本两种格式。

    做法是清掉它们的 handler 并打开 propagate，让记录汇总到 root 的唯一 handler。
    **但 `uvicorn.access` 是例外，见下方注释 —— 它必须被静音，不是被接管。**

    另：uvicorn 在 lifespan 之前就打了两行
    （`Started server process` / `Waiting for application startup.`），
    那两行**必然**绕过本函数装的 formatter —— 本函数是在 lifespan 里跑的。
    它们是固定字面量、不含任何变量，所以不是泄漏面；但"启动日志的前两行不是 JSON"
    是个已知且接受的事实，别把它当 bug 查。
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactingJsonFormatter(redactor))

    root = logging.getLogger()
    # 幂等：create_app() 在测试里会被调多次，不清就会每次多一个 handler，
    # 日志行数翻倍。
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        for existing in list(logger.handlers):
            logger.removeHandler(existing)
        logger.propagate = True
        # 不设 level：让它继承 root，否则改 CONSOLE_LOG_LEVEL 对它们无效。

    # ⚠️ `uvicorn.access` 必须**静音**，不能像上面两个那样"接管"。这是一个实测过的坑：
    #
    #   uvicorn 的 `--no-access-log` 的**全部**实现就是两行（config.py:421-423）：
    #       logging.getLogger("uvicorn.access").handlers = []
    #       logging.getLogger("uvicorn.access").propagate = False
    #   它靠 `propagate = False` 断掉记录的去路。而本函数在 lifespan 里运行，
    #   **晚于** uvicorn 应用自己的配置 —— 一旦这里把 propagate 改回 True，
    #   访问日志就沿 parent 链走到 root 的 handler 上，**静默恢复**。
    #   已实测：CMD 里明明写了 `--no-access-log`，日志里照样有
    #   `uvicorn.access ... "GET /api/health HTTP/1.1" 200`。
    #
    #   而访问日志记的是 URL，URL 是一个泄漏面（CLAUDE.md §日志：生产 --no-access-log）。
    #
    # 为什么是"我们无条件静音"而不是"尊重 uvicorn 的决定"（读它的 propagate 再决定）：
    #   后者让"访问日志开不开"取决于两方状态的先后顺序 —— 正是上面这个 bug 的形状。
    #   访问日志的开关权收归本函数一家，`--no-access-log` 退化成纵深防御。
    #   需要请求级日志时，由我们自己的中间件打（带 trace_id、经脱敏、不记 query）。
    access_logger = logging.getLogger("uvicorn.access")
    for existing in list(access_logger.handlers):
        access_logger.removeHandler(existing)
    access_logger.propagate = False

    # 这三个是最吵也最容易带出敏感内容的第三方 logger。
    # 它们的记录仍然过脱敏（同一个 handler），压到 WARNING 只是降噪 ——
    # **不是**把它们当成安全措施。降噪与脱敏是两件事，别把前者当后者。
    for name in ("litellm", "httpx", "httpcore", "docker", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)
