"""稳定机器码 —— 前端按码分支，绝不匹配文案。

⚠️ **这些码是契约，"未被使用"不等于死代码。**
CLAUDE.md §编码哲学 第 6 条说"删代码是进步"，那条规则**不适用于本文件**。
本文件里绝大多数码在 T2 阶段还没有 `raise` 点（它们的抛出方在 T3/T6/T7/T9），
但它们已经是 `PLAN.md` §护栏、§后端接口、§端到端验收里写定的对外契约，也是前端
`zh-CN.json`（T5）文案表的键。一次"清理未使用代码"就会把契约删掉，然后每个抛出方
各自现编一个码，前端分支全线失配。**要删任何一条，先去改 PLAN.md。**

# 两类码，刻意分开

1. **HTTP 错误码** —— `ConsoleError` 的子类，会被 `raise`，有对应 HTTP status。
2. **扫描归因码** —— `SCAN_FAILURE_CODES`，只写进 `scans.error_code` 列、经
   `GET /api/scans/{id}` 读出来给前端。它们**从来不是 HTTP 错误**：请求本身成功了，
   失败的是那次扫描。

为什么不合成一套：给 `llm_tls_intercepted` 编一个 HTTP status 是假的 —— 没有任何
接口会用它做响应码。假字段最终一定会被人当真用。

# 刻意不做的事

- **没有 `__init_subclass__` 自动注册，没有装饰器注册表。** `ALL_ERRORS` 是手写的
  扁平元组。魔法注册需要论证（CLAUDE.md §编码哲学 2），而这里唯一的收益是"新增子类
  时少写一行"，代价是读代码的人无法靠 grep 知道有哪些码。`tests/` 里用
  `ConsoleError.__subclasses__()` 反查漏登记，把那一行的遗忘变成测试失败。
- **文件里没有一个中文字符出现在返回值里。** 中文文案在
  `frontend/messages/zh-CN.json`（T5）。后端只返回 `{code, trace_id, params}`。
  理由：同一个码在不同界面位置需要不同措辞，后端硬编码一句就把前端锁死了。
  （本文件的注释是中文，那是给人读的，不进响应。）
- **`params` 里绝不放凭据、绝不放中文。** 它是给前端做文案插值的
  （例如 `{"target": "example.com"}`），会原样出现在 HTTP 响应里。
"""

from __future__ import annotations

from typing import ClassVar

# params 只允许 JSON 原生标量。刻意不允许 dict / list：嵌套结构会诱使人往里塞
# 整个请求体，那就迟早塞进凭据。
ParamValue = str | int | float | bool | None


class ConsoleError(Exception):
    """所有可经 HTTP 返回的业务错误的基类。

    子类只做一件事：填 `code` 与 `status` 两个 ClassVar。没有自定义 `__init__`，
    没有必填参数 —— 抛出点写 `raise DnsChangedError(target=t)` 即可。
    """

    code: ClassVar[str] = "internal_error"
    status: ClassVar[int] = 500

    def __init__(self, **params: ParamValue) -> None:
        # 传给 Exception 的 args 只有码：任何 str(exc) 都不会泄漏 params 内容。
        # 这一点在日志里很重要 —— logging 默认会 str() 异常。
        super().__init__(self.code)
        self.params: dict[str, ParamValue] = params

    def to_payload(self, trace_id: str) -> dict[str, object]:
        """HTTP 响应体。三个字段是全部对外契约，不许再加。

        返回类型是 `dict[str, object]` 而不是 `Any`：`params` 的值域是
        `ParamValue`，但整个 dict 的值域含 dict 本身，`object` 是最窄的诚实标注。
        """
        return {"code": self.code, "trace_id": trace_id, "params": self.params}

    def response_headers(self) -> dict[str, str]:
        """要附加到错误响应上的 HTTP 头。默认没有。

        为什么需要这个口子：`429` 按 RFC 9110 §15.6.4 应当带 `Retry-After`，而那是
        一个**头**，塞不进 `{code, trace_id, params}` 那三个字段里。让前端从 params
        里读一个自己拼的 `retry_after` 也行，但那就等于我们发明了一个平行于 HTTP 的
        约定，代理和浏览器都看不懂它。

        只有 `AuthLockedError` 覆盖它。刻意做成方法而不是 `__init__` 参数：
        头的内容由错误类型决定（是它语义的一部分），不该由每个抛出点各自决定。
        """
        return {}


# =============================================================================
# 框架级 —— 这四个在 T2 阶段就真的被用到（main.py 的四个异常处理器）
# =============================================================================
class InvalidRequestError(ConsoleError):
    """请求体/参数不合法。Pydantic 校验失败统一映射到这里。

    为什么是 422 而不是 400：FastAPI 的默认行为就是 422，改成 400 只会让"我们的
    422"和"框架的 422"两种形态并存。参数缺失/类型错误 → 422；语义上合法但被业务
    规则拒绝（不在白名单、超预算）→ 409/403，见下方各码。
    """

    code = "invalid_request"
    status = 422


class NotFoundError(ConsoleError):
    code = "not_found"
    status = 404


class MethodNotAllowedError(ConsoleError):
    """路径存在但方法不对。**只由 `main.py` 的 HTTPException 处理器产出**。

    为什么要给它单独一个码，而不是让它并进 `not_found`：这两种情况的成因完全不同 ——
    `not_found` 通常是拼错了路径或用了老版本的前端，`method_not_allowed` 几乎总是
    前端代码写错了动词（把 POST 写成 GET）。合成一个码会让后者在排查时伪装成前者。
    """

    code = "method_not_allowed"
    status = 405


class InternalError(ConsoleError):
    """兜底。**响应体里绝不带异常信息** —— traceback 只进日志（脱敏后）。"""

    code = "internal_error"
    status = 500


# =============================================================================
# 护栏（T6：target_guard）—— PLAN.md §护栏
# =============================================================================
class NotInAllowlistError(ConsoleError):
    code = "not_in_allowlist"
    status = 409


class BlockedMetadataError(ConsoleError):
    """云元数据地址。403 而不是 409：**不可覆盖**，没有"确认后继续"这一步。

    409 在本项目里的含义是"当前状态冲突，你可以改状态再来"（加白名单、重新解析
    DNS、补确认串）。元数据地址永久硬拦，用 403 把这个区别编码进状态码里。
    """

    code = "blocked_metadata"
    status = 403


class DnsChangedError(ConsoleError):
    """启动前重解析 DNS 与授权声明时不一致。"""

    code = "dns_changed"
    status = 409


class SplitHorizonError(ConsoleError):
    """同一主机名同时解析到公网与内网 —— 这就是 DNS rebinding 的形状。"""

    code = "split_horizon"
    status = 409


class MissingTypedConfirmationError(ConsoleError):
    code = "missing_typed_confirmation"
    status = 409


class BudgetExceedsCeilingError(ConsoleError):
    code = "budget_exceeds_ceiling"
    status = 409


class AllowlistFileBrokenError(ConsoleError):
    """`allowlist.yaml` 当前读不懂，所以**增量编辑**（加一条 / 删一条）没有基准。

    T8 新增。409 而不是 500：这不是 bug，是一个操作者能自己解决的状态冲突 ——
    去修那个文件，或者用"整份替换"（`PUT /api/allowlist`）覆盖掉它。

    为什么只挡增量编辑、不挡整份替换：增量编辑的语义是"在现有配置上改一处"，而现在
    没有可读的现有配置 —— 硬做的话会拿一份**空配置**当基准，于是"加一条"实际上等于
    "删掉文件里其它所有条目"，一次静默的数据丢失。整份替换本来就要覆盖全部内容，
    它正是这个状态的恢复手段。

    `params` 里带 `file_error`（`AllowlistFileErrorCode` 的值）让前端能直接指出是
    语法错还是字段错。但文案表里**刻意没有**把它列进 `params` 数组：那是一个机器码，
    `ErrorNotice` 的键值行会把 `allowlist_syntax_error` 原样显示出来。要把它翻成人话
    是清单编辑界面的事（`targetGuard` 之外另开一棵 `allowlistFile.*`），那时它会有
    行号一起显示，而不是孤零零一个码。
    """

    code = "allowlist_file_broken"
    status = 409


# =============================================================================
# 凭据（T7：key_vault / routes/keys）—— PLAN.md §N1
# =============================================================================
class KeyRequiredError(ConsoleError):
    """vault_handle 缺失或已失效（内存 KeyVault，api 重启即失效）。

    409 是**正确且诚实**的行为：我们确实无法在重启后还原凭据，那正是"Key 绝不落盘"
    的直接后果。前端据此重新索要凭据 —— 索要哪几个键由 `scans.auth_shape` 决定。
    """

    code = "key_required"
    status = 409


class KeyVerifyFailedError(ConsoleError):
    """真实验活失败（发一次 1-token 请求）。400：请求内容本身错了。"""

    code = "key_verify_failed"
    status = 400


class UnexpectedSecretKeyError(ConsoleError):
    """提交了所选 auth_shape 未声明的凭据键。

    这条是"bearer 与 SigV4 都填上、被 litellm 静默忽略一套"的唯一防线（§N1）。
    静默忽略比报错危险：用户以为在用 A 凭据，实际用的是 B。
    """

    code = "unexpected_secret_key"
    status = 400


# =============================================================================
# 资源与环境（T3：routes/system；T9：ScanLauncher）
# =============================================================================
class ConcurrencyLimitError(ConsoleError):
    code = "concurrency_limit"
    status = 409


class DockerUnavailableError(ConsoleError):
    """起扫描前的 docker 预检失败（sock 不通 / daemon 没响应）。

    与归因码 `docker_permission_denied` / `docker_not_installed` 的分工：
    这个是**我们主动预检**的结果，那两个是**子进程已经跑起来之后**从 stdout 归因的。
    """

    code = "docker_unavailable"
    status = 409


# =============================================================================
# 单账号登录（T4b：services/auth.py、routes/auth.py）—— PLAN.md §单账号登录
# =============================================================================
class UnauthenticatedError(ConsoleError):
    """没带会话 cookie，或会话已失效（进程重启 / 空闲超时 / 绝对超时）。

    401 而不是 403：这里的语义是"我不知道你是谁"，前端应当跳登录页。403 会让前端
    以为"已登录但没权限"，而本项目没有权限模型 —— 那条分支永远不该出现。

    ⚠️ 这个错误也会在 **WebSocket 握手**上产出。已实测：全局依赖抛出它时，
    握手返回真正的 `401` + 本响应体 + `content-type: application/json`
    （ASGI WebSocket Denial Response 扩展），而不是一个没有正文的 403。
    """

    code = "unauthenticated"
    status = 401


class InvalidCredentialsError(ConsoleError):
    """用户名或口令错误。**两种情况共用这一个码，刻意的。**

    分成 `user_not_found` 与 `wrong_password` 会免费告诉攻击者用户名对不对，
    而单账号系统里那正是他要猜的一半。响应时间也一并拉平了
    （见 `AuthRecord.burn_equivalent_work`）—— 只统一码而不统一耗时是自欺。
    """

    code = "invalid_credentials"
    status = 401


class AuthLockedError(ConsoleError):
    """连续失败达到阈值，登录暂时锁定。

    `Retry-After` 是**批准发送**的：攻击者本来就能靠计时测出锁定窗口，藏起来只对
    正常用户有害（界面只能显示"稍后再试"而说不出稍后是多久）。
    """

    code = "auth_locked"
    status = 429

    def response_headers(self) -> dict[str, str]:
        retry_after = self.params.get("retry_after")
        if isinstance(retry_after, int) and not isinstance(retry_after, bool):
            return {"Retry-After": str(retry_after)}
        return {}


class OriginMismatchError(ConsoleError):
    """WebSocket 握手的 `Origin` 与 `Host` 不一致。

    **这是纵深防御，不是唯一防线**：`SameSite=Strict` 已经让跨站发起的 WS 握手带不上
    会话 cookie，所以真正的攻击在到这一步之前就没有身份了。这条检查存在的意义是
    "cookie 属性哪天被人改松了"时还有一层 —— 别把它当成可以放心改 SameSite 的理由。
    """

    code = "origin_mismatch"
    status = 403


# 手写扁平登记表。见模块 docstring「刻意不做的事」第 1 条。
# 顺序与上方定义顺序一致，方便对读。
ALL_ERRORS: tuple[type[ConsoleError], ...] = (
    InvalidRequestError,
    NotFoundError,
    MethodNotAllowedError,
    InternalError,
    NotInAllowlistError,
    BlockedMetadataError,
    DnsChangedError,
    SplitHorizonError,
    MissingTypedConfirmationError,
    BudgetExceedsCeilingError,
    AllowlistFileBrokenError,
    KeyRequiredError,
    KeyVerifyFailedError,
    UnexpectedSecretKeyError,
    ConcurrencyLimitError,
    DockerUnavailableError,
    UnauthenticatedError,
    InvalidCredentialsError,
    AuthLockedError,
    OriginMismatchError,
)


# =============================================================================
# 扫描归因码 —— 只写进 scans.error_code，从不作为 HTTP 状态
#
# 来源（都是 M0 实测得出的，不是推测）：
#   · 子进程 stdout 正文的异常类名 + Rich 面板标题，判据表见 PLAN.md §后端接口。
#     **必须先匹配正文异常类名，面板标题只做兜底** —— 四种毫不相干的故障
#     （TLS 被解密 / 凭据无效 / 形状与路由不匹配 / 路由不接受某参数）打的是同一个
#     `LLM CONNECTION FAILED` 面板。只按标题分类会把三种都报成"连不上"。
#   · run.json.status == "stopped" → scan_incomplete。
#
# scan_incomplete 是发布阻断项的那一条：退出码 0 **不代表扫描跑完了**，预算耗尽被
# 掐死的扫描同样退 0 并宣称"未发现漏洞"。一个渗透测试控制台在钱花光时报"目标干净"，
# 比不报任何结论危险得多。
#
# 这一组码的语义是"**这次扫描为什么不是一次干净的完成**"，不全是故障：操作者自己点了
# 停止、控制台重启把子进程带走，都不是任何东西坏了，但同样意味着"结论不完整"。
#
# 为什么不复用现成的码：`scan_incomplete` 的文案写死了"费用或轮次先用完了"，拿它解释
# "你自己点了停止"是撒谎；而让 `error_code` 为 NULL 则意味着"没跑完但没人说得出为什么"，
# 那就把"不许暗示目标干净"这条安全语义推给了展示层。
#
# 为什么是 frozenset 而不是 Enum：这些值来自对**外部程序输出**的模式匹配，取值域会
# 随 Strix 版本变化（升级预警线是 test_strix_contract.py）。Enum 的收益是穷举性检查，
# 而这里穷举性本来就靠不住 —— T3 的兜底分支必须存在。用 frozenset + 校验函数，把
# "我们只认这些"表达成一个断言，不假装它是封闭类型。
# =============================================================================
SCAN_FAILURE_CODES: frozenset[str] = frozenset(
    {
        # ---- LLM 连接类（同一个面板标题，靠正文异常类名区分）----------------
        "llm_tls_intercepted",  # CERTIFICATE_VERIFY_FAILED / SSLCertVerificationError
        "bedrock_route_rejects_bearer",  # object has no attribute 'access_key'
        "prompt_cache_unsupported_on_route",  # cache_control_injection_points
        "invalid_api_key",  # AuthenticationError / Incorrect API key
        "model_access_denied",  # AccessDenied / not authorized / ValidationException
        "model_not_found",  # NotFound / MODEL NOT FOUND
        "model_name_not_provider_qualified",  # UNKNOWN MODEL NAME（裸名默认路由到 OpenAI）
        "missing_required_env",  # MISSING REQUIRED ENVIRONMENT VARIABLES
        "llm_connection_failed",  # 只剩面板标题时的兜底
        # ---- Docker 与准备阶段（面板标题即码）------------------------------
        "docker_permission_denied",
        "docker_not_installed",
        "failed_to_pull_image",
        "scan_preparation_failed",
        # ---- 结论完整性（来自 run.json.status，不是 stdout）-----------------
        "scan_incomplete",
        # ---- 没跑完但不是故障（来自"我们自己发过信号"这个事实，不是退出码）----
        "stopped_by_operator",  # 操作者点了停止
        "interrupted_by_restart",  # api 停机 / run.json.status == "interrupted"
        "scan_failed_unknown",  # 失败退出，但输出里没有我们认识的特征
    }
)


def assert_scan_failure_code(code: str) -> str:
    """写 `scans.error_code` 之前过一遍。

    为什么需要它：归因逻辑是一串正则匹配，很容易在新增分支时把码拼错
    （`llm_tls_intercept` 少个 `ed`）。拼错的码不会报错，只会让前端落到"未知错误"
    分支 —— 一个静默的失败模式。这个函数把它变成响亮的失败。
    """
    if code not in SCAN_FAILURE_CODES:
        raise ValueError(
            f"未登记的扫描归因码 {code!r}。"
            "新增归因码要同时改 errors.py 的 SCAN_FAILURE_CODES 与前端 zh-CN.json。"
        )
    return code
