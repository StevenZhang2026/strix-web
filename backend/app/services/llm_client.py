"""供应商目录 + 凭据形状校验 + 真实验活。

# 为什么这三件事在同一个模块里

"这种凭据形状要哪几个键"和"怎么用这几个键发一次请求"是**同一份知识**。分成两个文件
必然漂移：加一种形状时只改了目录、忘了改发请求的那一半，结果是一个能通过校验、
却永远验不活的形状。目录（`CATALOG`）是手写常量元组，没有注册表、没有装饰器。

# 验活为什么全走 kwargs，一个 env 都不设

litellm 认 `AWS_BEARER_TOKEN_BEDROCK` 之类的环境变量，用 `os.environ` 是最短的写法 ——
**但 `os.environ` 是进程级的**。`api` 进程随后要 `subprocess` 起 Strix（T9），那时环境
会被继承，于是一个用户的凭据出现在另一个用户扫描的 `/proc/<pid>/environ` 里。
所以三种形状全部经参数传入，**本模块不写任何环境变量**。

bearer token 走 `api_key` 这个 kwarg 不是将就：litellm 1.100.0 **没有**
`aws_bearer_token_bedrock` 参数，`llms/bedrock/base_aws_llm.py:1455-1459` 与 `:1554-1557`
的代码是 `if api_key is not None: aws_bearer_token = api_key`。

# 失败时一个字都不外带

`VerifyOutcome` 只有 `ok` 与 `latency_ms`，**没有 `reason`**。模型服务的错误正文可能把
我们刚发过去的凭据回显出来（也可能带上整段请求参数），所以它既不进响应、不进日志、
也不进异常 args。`key_verify_failed` 的 params 刻意只有 `provider`/`auth_shape`/
`latency_ms`，这个契约就是这么来的。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from app.errors import InvalidRequestError, UnexpectedSecretKeyError
from app.services.key_vault import CredentialSet

logger = logging.getLogger(__name__)

# 三种凭据形状的机器码。字面串只出现在这里与 `CATALOG` 里，路由层 import 它们。
SHAPE_SINGLE = "single"
SHAPE_BEDROCK_SIGV4 = "bedrock_sigv4"
SHAPE_BEDROCK_BEARER = "bedrock_bearer"

# 验活请求的超时。8 秒是"人愿意站在那儿等"的上限，也远长于一次 1-token 请求的正常耗时
# （M0 实测百毫秒级）。它的意义是**不让一个连不上的端点把请求挂死**，而不是性能调优。
VERIFY_TIMEOUT_SECONDS = 8

# 验活请求体。1 个 token 的输出、一句最短的输入 —— 目的只是让服务端走完鉴权。
_VERIFY_PROMPT = "ping"
_VERIFY_MAX_TOKENS = 1

# litellm 的 bedrock 路由前缀。`model_for()` 先剥掉用户自己带的，再按形状补回去。
_BEDROCK_INVOKE_PREFIX = "bedrock/invoke/"
_BEDROCK_PREFIX = "bedrock/"


@dataclass(frozen=True, slots=True)
class ShapeSpec:
    """一种凭据形状：要哪几个键、模型名怎么拼。

    `secret_keys` 里的名字**就是**要注入 Strix 子进程的 env 变量名（T9）—— 不是一套
    我们自己发明的字段名再去做映射。一套名字少一层翻译，也就少一个能对不上的地方。

    `param_keys` 是**非机密**参数（区域）。它与 `secret_keys` 必须分开：`params` 不进
    `KeyVault.secret_values()`，否则 `us-east-1` 这种到处都出现的字符串会把日志替换成
    一片 `[REDACTED]`（见 `key_vault` 模块 docstring）。
    """

    auth_shape: str
    secret_keys: tuple[str, ...]
    param_keys: tuple[str, ...]
    model_prefix: str


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """一家供应商支持哪几种形状、有哪些模型名可以抄。

    `models` 是**建议列表，不是白名单**：litellm 认的模型名每周都在变，把它当白名单等于
    每次上游发新模型就要改我们的代码。前端把它渲染成候选项，输入框仍然自由填写。
    """

    provider: str
    shapes: tuple[ShapeSpec, ...]
    models: tuple[str, ...]
    api_base_allowed: bool


# `single` 形状只有一个键，对 Strix 来说它甚至是可选的（只有 `STRIX_LLM` 必填），
# 但对验活是必需的 —— 没有它就没什么可验。
_SINGLE = ShapeSpec(
    auth_shape=SHAPE_SINGLE,
    secret_keys=("LLM_API_KEY",),
    param_keys=(),
    model_prefix="",
)

# 两种 Bedrock 形状**必须二选一**（`PLAN.md:336`）：litellm 先取 bearer token，有值就
# 整段跳过 SigV4。两套都填会被静默忽略一套，事后归因不了 —— 所以 `check_secret_keys()`
# 拒绝多余的键，这是那条静默故障的唯一防线。
_BEDROCK_SIGV4 = ShapeSpec(
    auth_shape=SHAPE_BEDROCK_SIGV4,
    secret_keys=("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
    param_keys=("AWS_REGION_NAME",),
    model_prefix=_BEDROCK_PREFIX,
)

# bearer 形状**必须**走 invoke 路由（`PLAN.md:330`）：converse 路由无条件先取 SigV4
# 凭据，只给 bearer 时崩在 `'NoneType' object has no attribute 'access_key'`。
_BEDROCK_BEARER = ShapeSpec(
    auth_shape=SHAPE_BEDROCK_BEARER,
    secret_keys=("AWS_BEARER_TOKEN_BEDROCK",),
    param_keys=("AWS_REGION_NAME",),
    model_prefix=_BEDROCK_INVOKE_PREFIX,
)

# Bedrock 的模型名**不带前缀**：前缀由形状决定并由 `model_for()` 补上，用户不必记
# 自己该写 `bedrock/` 还是 `bedrock/invoke/`。
_BEDROCK_MODELS = (
    "us.anthropic.claude-opus-5",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
)

# `gemini/` 前缀是 litellm 的供应商路由，不是我们加的形状前缀（所以留在名字里）。
_GEMINI_MODELS = ("gemini/gemini-2.5-pro", "gemini/gemini-2.5-flash")

CATALOG: tuple[ProviderSpec, ...] = (
    # 只有 bedrock 有两种形状 —— 形状是独立维度，不是 provider 的属性（`PLAN.md:322`）。
    ProviderSpec(
        provider="bedrock",
        shapes=(_BEDROCK_SIGV4, _BEDROCK_BEARER),
        models=_BEDROCK_MODELS,
        # Bedrock 的端点由区域拼出来，给一个 api_base 只会让 litellm 的两条路由行为分叉。
        api_base_allowed=False,
    ),
    ProviderSpec(
        provider="gemini",
        shapes=(_SINGLE,),
        models=_GEMINI_MODELS,
        api_base_allowed=True,
    ),
    # 下面四家的 `models` 是空的，这是结论不是 TODO：本项目只列**已经实测过**的模型名
    # （bedrock 与 gemini 由 M0 实测，见 `scripts/m0_probe.sh`）。开发网络把这四家的
    # 端点全解密了，我们无法验证任何一个名字，而一个凭空写进候选列表的模型名会让用户
    # 选中它、验活失败、然后去查自己的凭据 —— 那比没有候选项糟得多。输入框本来就自由填。
    ProviderSpec(provider="anthropic", shapes=(_SINGLE,), models=(), api_base_allowed=True),
    ProviderSpec(provider="openai", shapes=(_SINGLE,), models=(), api_base_allowed=True),
    ProviderSpec(provider="openrouter", shapes=(_SINGLE,), models=(), api_base_allowed=True),
    ProviderSpec(provider="deepseek", shapes=(_SINGLE,), models=(), api_base_allowed=True),
)


def spec_for(provider: str, auth_shape: str) -> ShapeSpec | None:
    """查一种形状。provider 不认识、或它不支持这个形状，都返回 None。

    两种情况合并成一个 None 是刻意的：对调用方它们是同一件事（这个组合不存在），
    而分成两个错误码只会让前端多一个分支去显示同一句"重新选一次"。
    """
    for entry in CATALOG:
        if entry.provider != provider:
            continue
        for shape in entry.shapes:
            if shape.auth_shape == auth_shape:
                return shape
    return None


def check_secret_keys(spec: ShapeSpec, secrets: Mapping[str, object]) -> None:
    """所选形状声明的键，一个不多、一个不少。纯函数：不发请求、不碰 vault。

    值的类型标 `object` 而不是 `SecretStr`：本函数只看键名，标成 `object` 顺带保证了
    它**没有办法**不小心把值带进异常或日志。

    先查多余再查缺失：多余的键意味着用户把两套凭据都填了（那是被 litellm 静默忽略一套
    的形状），比"少填一项"危险得多，应该先说。
    """
    allowed = set(spec.secret_keys)
    for name in secrets:
        if name not in allowed:
            raise UnexpectedSecretKeyError(key_name=name, auth_shape=spec.auth_shape)
    for name in spec.secret_keys:
        if name not in secrets:
            raise InvalidRequestError(field="secrets", key_name=name)


def check_param_keys(spec: ShapeSpec, params: Mapping[str, str]) -> None:
    """`params` 的键也必须一个不多、一个不少。纯函数。

    **两侧都要校验，但理由完全不同，所以是两个函数、两个错误码：**

    `secrets` 多一个键 → `400 unexpected_secret_key`，因为那是"bearer 与 SigV4 都填了"
    的形状，litellm 会静默忽略一套（`PLAN.md:336`），事后归因不了。

    `params` 多一个键 → **这是一条泄漏路径**，不是形状问题。`params` 刻意不进
    `KeyVault.secret_values()`（否则 `us-east-1` 会把日志替成一片 `[REDACTED]`），
    而 `GET /api/keys/{handle}` 又把它原样回显。于是一个把凭据填进 `params` 的调用方
    会同时拿到三件事：存下来、明文回显、日志不脱敏。`param_keys` 这份白名单已经声明
    并经 `/api/providers` 发布出去了 —— **声明了却不强制等于没声明**
    （pitfalls 条 23：检查都通过 ≠ 被检查的事真发生了）。
    码用 `invalid_request` 而不是 `unexpected_secret_key`：这里收到的东西按定义不是
    secret，用那个码会让审计和前端都以为用户填错了凭据框。

    `params` 少一个键 → 同样 422。放过去的代价不是"少一个字段"：`_completion_kwargs`
    会把空区域发给 litellm，而验活失败**按契约不带任何原因**，于是用户拿着一组好凭据
    去排查凭据 —— 与 `model_for()` 那段注释防的是同一个死角。
    """
    allowed = set(spec.param_keys)
    for name in params:
        if name not in allowed:
            raise InvalidRequestError(field="params", key_name=name)
    for name in spec.param_keys:
        if name not in params:
            raise InvalidRequestError(field="params", key_name=name)


def model_for(spec: ShapeSpec, strix_llm: str) -> str:
    """把用户填的模型名拼成 litellm 要的形状。纯函数。

    **路由由 auth_shape 决定，不由用户的打字决定**，所以先剥掉用户自己带的 `bedrock/`
    或 `bedrock/invoke/` 再补。少了这一步，一个照着 SigV4 文档粘贴 `bedrock/xxx` 的
    bearer 用户会得到 `bedrock/invoke/bedrock/xxx`，而验活失败**不会告诉他原因**
    （错误正文一个字都不外带）—— 那是一个无从排查的死角。
    """
    if not spec.model_prefix:
        return strix_llm
    bare = strix_llm
    for prefix in (_BEDROCK_INVOKE_PREFIX, _BEDROCK_PREFIX):
        if bare.startswith(prefix):
            bare = bare[len(prefix) :]
            break
    return f"{spec.model_prefix}{bare}"


@dataclass(frozen=True, slots=True)
class VerifyOutcome:
    """验活结果。**刻意只有两个字段** —— 见模块 docstring 最后一节。"""

    ok: bool
    latency_ms: int


# 验活器。做成可注入的别名（形状与 `dns_resolver.Resolver` 一致）是因为单测禁止碰真实
# 网络（CLAUDE.md §测试），注入点是 `app.state.llm_verifier`。
Verifier = Callable[[CredentialSet], Awaitable[VerifyOutcome]]


# 归因码 ← 异常正文里的固定子串。**这是白名单，不是解析器**：只认实测见过的几种失败，
# 其余一律落到类名。顺序无所谓（五条互斥），但**表必须是有限的** —— 见下面那个函数。
_FAILURE_PATTERNS: tuple[tuple[str, str], ...] = (
    # 区域多一个空格/换行就是这条。litellm 本地就拒，请求根本没发出去（实测 9 毫秒）。
    ("Invalid AWS region format", "bad_region_format"),
    # 密文末尾带换行 → httpx 当成 header 注入。同样是本地拒。
    ("Forbidden control character", "control_char"),
    # 模型名少了 litellm 的供应商前缀（`deepseek/` 之类）。2026-09-19 用户实撞过。
    ("LLM Provider NOT provided", "missing_model_prefix"),
    ("Invalid API Key format", "bad_key_format"),
    # 连不上：DNS、被防火墙掐、api_base 写错都会落这里。
    ("Cannot connect to host", "endpoint_unreachable"),
)


def classify_failure(exc: BaseException) -> str:
    """一个验活异常 → 一个能记进日志的归因码。**纯函数，无 IO。**

    为什么需要它：`verify()` 按契约一个字都不外带，于是"为什么没验过"连**我们自己**
    都查不到 —— 2026-09-19 排两次真实失败时只能在容器里拿假 token 做对照实验才定位到
    原因。这个函数把"能安全说出口的那部分"补回来。

    **不变式：返回值只可能是 `_FAILURE_PATTERNS` 里的码，或 `unclassified:<类名>`。**
    `str(exc)` 在这里只被**读**、绝不被**转发** —— litellm 的错误正文会把整段请求参数
    回显出来，凭据就在里面（模块 docstring 第四节）。所以出口是有限集合 + 静态类名，
    不许任何一条路径把正文的字符搬进返回值，截断版也不行。
    """
    message = str(exc)
    for pattern, code in _FAILURE_PATTERNS:
        if pattern in message:
            return code
    # 类名带前缀，是为了让日志里一眼看出"这条没被归类"，而不是多了一个陌生的码。
    return f"unclassified:{type(exc).__name__}"


async def verify(credentials: CredentialSet) -> VerifyOutcome:
    """发一次 1-token 请求，只回"成了/没成"与耗时。

    为什么值得发这一次请求：凭据错了的下一站是一次几十分钟的扫描，而那次扫描会在
    第一次模型调用时失败 —— 用户等到那时才知道自己多粘了一个换行。
    """
    # `import litellm` 既**不放模块顶层**、也**不在协程里直接写**，两边都有实测理由：
    #   · 顶层：它的 import 是秒级的（要 import 全部供应商实现），那笔钱会落在每一次
    #     `api` 启动和每一次 pytest 收集上，而绝大多数请求根本不验活。
    #   · 协程里直接 import：它在 import 期**去网上拉 model cost map**（本机实测那次
    #     请求撞企业 TLS 解密后要等到失败才回落到本地副本），也就是一次同步阻塞 IO。
    #     `api` 只有 1 个 worker，卡住事件循环几秒会把 `/api/health` 一起卡住 ——
    #     compose 的 healthcheck 一超时就重启 `api`，而重启会清空内存 KeyVault。
    # 所以扔到线程里去 import；Python 的模块缓存保证只有第一次贵。
    # 返回的 `ModuleType` 让 `litellm.acompletion` 变成一次 `Any` 属性访问，这是这个
    # 写法唯一的代价：换来的是"验活不会把所有人的凭据弄丢"。
    litellm = await asyncio.to_thread(importlib.import_module, "litellm")

    spec = spec_for(credentials.provider, credentials.auth_shape)
    if spec is None:
        # 路由层先做 spec_for 再验活（顺序见 `routes/keys.py`），走到这里说明有人绕过了
        # 那一步。args 里只有形状名，没有凭据。
        raise ValueError(f"未知的 provider/auth_shape 组合：{credentials.provider}")

    # kwargs 在 `try` **之外**算好。放进去的话，`_completion_kwargs` 里的 KeyError
    # （= 路由层的形状校验被绕过了，编程错误）会被下面那个宽口径 `except` 吞成
    # "验活未通过"，于是一个 bug 长期伪装成"用户的凭据不对"。
    completion_kwargs = _completion_kwargs(credentials)

    started = time.monotonic()
    try:
        await litellm.acompletion(
            model=model_for(spec, credentials.strix_llm),
            messages=[{"role": "user", "content": _VERIFY_PROMPT}],
            max_tokens=_VERIFY_MAX_TOKENS,
            timeout=VERIFY_TIMEOUT_SECONDS,
            **completion_kwargs,
        )
    except Exception as exc:
        # 捕获面故意开到最宽（`Exception`），有两条理由：
        #   ① 验活的语义就是"任何失败都只是没验过" —— 没有一种失败需要区别对待；
        #   ② litellm 会把底层 botocore/httpx/ssl 的异常原样抛出来，异常类是它的实现
        #      细节而不是契约，列举一串类名只会在下一次升级时漏掉一个，那时 500 会
        #      代替 400 出现在用户面前。
        # **exc 一个字都不许带出去**，也不许 `logger.exception` —— traceback 的每一帧
        # 都带着上面那些 kwargs，其中就有凭据本身。日志里只多一个 `failure_kind`：
        # `classify_failure()` 的出口是有限集合，正文与 traceback 一个字都不经过它。
        # 响应契约不变 —— `VerifyOutcome` 仍然只有 `ok` 与 `latency_ms`，这个码**只进
        # 日志**（要不要也回给前端当提示，是另一个决定，没做）。
        outcome = VerifyOutcome(ok=False, latency_ms=_elapsed_ms(started))
        logger.info(
            "凭据验活未通过",
            extra={
                "provider": credentials.provider,
                "auth_shape": credentials.auth_shape,
                "latency_ms": outcome.latency_ms,
                "failure_kind": classify_failure(exc),
            },
        )
        return outcome
    return VerifyOutcome(ok=True, latency_ms=_elapsed_ms(started))


# 翻译（T21b）一次调用的上限。验活那 8 秒是给 1 个 token 的；一条发现改写成十个字段的
# 中文要几百个 token，慢的模型要几十秒。
COMPLETE_TIMEOUT_SECONDS = 120


@dataclass(frozen=True, slots=True)
class Completion:
    """一次模型调用的产出。`cost_usd` 为 `None` = 算不出（价目表里没这个模型），**不是 0**。"""

    text: str
    usage_prompt: int | None
    usage_completion: int | None
    cost_usd: float | None


class CompletionFailed(Exception):
    """模型调用失败。**只带 `classify_failure()` 的码**，理由同 `verify()` 那段注释。"""

    def __init__(self, failure_kind: str) -> None:
        super().__init__(failure_kind)
        self.failure_kind = failure_kind


# 注入点，形状同 `Verifier`：单测禁止碰真实网络。
Completer = Callable[[CredentialSet, list[dict[str, str]], int], Awaitable[Completion]]


async def complete(
    credentials: CredentialSet, messages: list[dict[str, str]], max_tokens: int
) -> Completion:
    """用用户自己的凭据发一次对话请求。路由、kwargs、import 方式与 `verify()` 完全一致。"""
    litellm = await asyncio.to_thread(importlib.import_module, "litellm")
    spec = spec_for(credentials.provider, credentials.auth_shape)
    if spec is None:
        raise ValueError(f"未知的 provider/auth_shape 组合：{credentials.provider}")
    completion_kwargs = _completion_kwargs(credentials)

    failure_kind: str | None = None
    try:
        response = await litellm.acompletion(
            model=model_for(spec, credentials.strix_llm),
            messages=messages,
            max_tokens=max_tokens,
            timeout=COMPLETE_TIMEOUT_SECONDS,
            **completion_kwargs,
        )
    except Exception as exc:  # 捕获面开到最宽的理由同 `verify()`
        failure_kind = classify_failure(exc)
    if failure_kind is not None:
        # `raise` 写在 `except` 块**外面**：在块里抛（哪怕 `from None`）新异常的
        # `__context__` 仍然挂着原异常 —— 它的正文与 traceback 帧里的 kwargs 就是凭据，
        # 任何一个把异常链打印出来的上层都会把它带出去。
        raise CompletionFailed(failure_kind)

    usage = getattr(response, "usage", None)
    try:
        cost: float | None = float(litellm.completion_cost(completion_response=response))
    except Exception:  # 价目表缺这个模型时 litellm 抛的类型不是契约；算不出就是 None
        cost = None
    return Completion(
        text=response.choices[0].message.content or "",
        usage_prompt=getattr(usage, "prompt_tokens", None),
        usage_completion=getattr(usage, "completion_tokens", None),
        cost_usd=cost,
    )


def _completion_kwargs(credentials: CredentialSet) -> dict[str, str]:
    """一组凭据 → litellm 的 kwargs。**这里不设任何环境变量**，理由见模块 docstring。

    三个分支显式写开，不做"env 名 → kwarg 名"的映射表：映射表读起来要跳两跳才知道
    bearer 到底进了哪个参数，而那一跳恰好是这个模块最容易出错、也最需要解释的地方。
    """
    secrets = {name: value.get_secret_value() for name, value in credentials.secrets.items()}
    # 区域只在两个 Bedrock 分支里读，所以取值就写在分支里 —— 并且**直接下标、不给默认值**：
    # 它在场由 `check_param_keys()` 保证（路由层在验活之前调）。一个 `""` 兜底只会把
    # "少填了区域"变成一次没有原因的验活失败，而那正是用户最查不出来的一种。
    if credentials.auth_shape == SHAPE_BEDROCK_SIGV4:
        return {
            "aws_access_key_id": secrets["AWS_ACCESS_KEY_ID"],
            "aws_secret_access_key": secrets["AWS_SECRET_ACCESS_KEY"],
            "aws_region_name": credentials.params["AWS_REGION_NAME"],
        }
    if credentials.auth_shape == SHAPE_BEDROCK_BEARER:
        # bearer token 就是 `api_key`，见模块 docstring 引的那两处源码。
        return {
            "api_key": secrets["AWS_BEARER_TOKEN_BEDROCK"],
            "aws_region_name": credentials.params["AWS_REGION_NAME"],
        }
    kwargs = {"api_key": secrets["LLM_API_KEY"]}
    if credentials.api_base:
        kwargs["api_base"] = credentials.api_base
    return kwargs


def _elapsed_ms(started: float) -> int:
    """`time.monotonic` 而不是 `time.time`：耗时不该受 NTP 校准影响（同 `key_vault.Clock`）。"""
    return int((time.monotonic() - started) * 1000)
