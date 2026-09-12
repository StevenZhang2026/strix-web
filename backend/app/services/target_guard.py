"""目标护栏 —— 解析规范化 + 地址分类 + 放行策略。对应 `PLAN.md` §护栏。

# 这个模块零 IO，这是设计要求而不是巧合

不解析 DNS、不读文件、不碰网络、不碰 docker、不看时钟。`getaddrinfo` 的**结果**由调用方
传进来（T12 负责解析与"启动前重解析并比对"）。理由有两层：

1. `PLAN.md:158` 把 TargetGuard 写成「纯函数，好测」，`CLAUDE.md` §Python 也把
   "`target_guard` 等判定逻辑写成无 IO 的纯函数"列为硬要求。
2. 更实际的一层：护栏的每一条规则都是安全不变式。要给"`::ffff:169.254.169.254`
   会不会被拦"写测试，只有在判定逻辑不需要网络时才做得到 —— 否则那条用例要么被跳过，
   要么被 mock 成"测 mock 而不测规则"。

因此本模块**不抛 `ConsoleError`**，也不知道 HTTP 状态码。它只输出判定结果与
（规范化阶段的）拒绝原因，由路由层（T8 `/api/targets/validate`、T12 `POST /api/scans`）
决定映射成 200 的预览、还是 403/409/422。

# 三条"看着像洁癖、其实各有出处"的规则

每一条都对应 Strix 的一处具体行为。注释写清出处，否则以后一定有人把它当成多余的严格性
"简化"掉：

- **拒绝 `user:pass@`** —— Strix 的 `infer_target_type` 会把带凭据的 URL **重分类成
  repository**，于是整个扫描走的是完全另一条代码路径（克隆仓库而不是打 Web）。
  这不是审美问题，是"用户以为在扫网站，实际在扫代码仓库"。
- **拒绝 `.git` 结尾的路径** —— `_is_http_git_repo` 会**发一次真实网络请求**去探测。
  那意味着"还在向导里填目标"这一步就已经产生了对外流量，而此时用户还没做授权声明。
- **IDNA 编码不是为了兼容，是为了让同形字攻击可见** —— `例子.中国` 变成
  `xn--fsqu00a.xn--fiqs8s` 之后，人眼才看得出授权声明里那个域名和自己以为的不是一个。
  `NormalizedTarget` 同时保留 unicode 形与 ASCII 形，就是为了让 UI 能并排显示。

# 两件事刻意分开

`normalize_target()` 只管**字符串形状**（能不能构成一个我们愿意交给 Strix 的 URL），
`evaluate_target()` 只管**地址与策略**（这个目标准不准扫）。混在一起会得到一个
"既可能因为有分号被拒、也可能因为是元数据地址被拒"的返回值，而这两类拒绝在 UI 上是
完全不同的两页（前者是"填错了"，后者是"不许扫"）。

# 白名单只以一个已判定好的结论进来

本模块**不读** `allowlist.yaml`、不做通配匹配、不管热重载 —— 那全是 T8 的
`allowlist.py`。它只消费 `AllowlistDecision`：一个"这个目标命中了什么、命中的条目
授予了哪几项放行"的扁平结论。这样文件格式怎么变都不会波及护栏规则本身。
"""

from __future__ import annotations

import ipaddress
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

# =============================================================================
# 一、分类与策略的取值域
# =============================================================================


class TargetCategory(StrEnum):
    """`PLAN.md` §护栏 那张表的六个类别。

    `MIXED` 与其它五个不是一个层级：它**不是任何单个地址的类别**，只在汇总一批解析
    结果时才可能出现（同一主机名既指向公网又指向非公网）。`classify_address()` 永远
    不会返回它 —— 这一点由 `tests/test_target_guard.py` 断言，而不是靠这段注释。

    为什么六个值放在同一个枚举里而不是拆成"地址类别"和"汇总类别"两个：拆开会让
    `GuardVerdict.category` 需要一个联合类型，而前端拿到的就是一个字符串，它只想按
    六个值分支。两个枚举只是把同一个值域在类型系统里写两遍。
    """

    METADATA = "metadata"
    LOOPBACK = "loopback"
    PRIVATE = "private"
    CARRIER_RESERVED = "carrier_reserved"
    PUBLIC = "public"
    MIXED = "mixed"


class GuardRequirement(StrEnum):
    """某个类别**要满足什么条件**才放行。

    ⚠️ 它描述的是类别的性质，**与本次是否已经满足无关**。"这次放不放行"是
    `GuardVerdict.allowed`。两个字段刻意正交：UI 要靠 `requirement` 决定渲染哪种控件
    （复选框？"加入授权清单"按钮？还是一句"这条不能解除"），而那个控件在用户勾上之后
    仍然要显示出来。把两者合并成一个"状态"字段，勾选之后控件就会自己消失。
    """

    NONE = "none"  # public + advisory 模式：不需要额外条件
    OPERATOR_OPT_IN = "operator_opt_in"  # loopback / private：勾一个复选框
    ALLOWLIST_ENTRY = "allowlist_entry"  # public + enforce 模式：必须命中白名单
    ALLOWLIST_FILE_ONLY = "allowlist_file_only"  # carrier/reserved：只能改文件，UI 不给
    IMPOSSIBLE = "impossible"  # metadata / mixed：没有任何放行途径


class OptInFlag(StrEnum):
    """操作者在向导里能勾的两个"我知道我在做什么"。

    刻意**只有两个**，且与 `PLAN.md:461` 那三个"授权声明"复选框是不同的东西：
    那三个是每次扫描都必勾的法律声明（T12 管），这两个是针对具体地址类别的技术放行。
    """

    LOOPBACK = "loopback"  # "测试本机服务"
    PRIVATE = "private"  # "内网确认"


class AllowlistMode(StrEnum):
    """`allowlist.yaml` 的 `mode`。只影响 `PUBLIC` 一类（见 `PLAN.md` §护栏 那张表）。"""

    ENFORCE = "enforce"
    ADVISORY = "advisory"


class RejectionReason(StrEnum):
    """规范化阶段的拒绝原因。

    **这些不是 `app/errors.py` 里的 HTTP 机器码**，是护栏自己的一套。分开的理由：
    `errors.py` 里的码每一个都对应一个 HTTP status 和一条前端错误页文案，而这八条是
    向导第 1 步输入框下方的即时提示 —— 那不是错误页，用户还在打字。给它们各编一个
    HTTP 码会得到八个永远不会被当成 HTTP 响应的"错误"。

    路由层怎么用它们：`/api/targets/validate`（T8）在 200 响应体里原样返回；
    `POST /api/scans`（T12）在服务端重校验时映射成 `InvalidRequestError(field="targets")`
    —— 因为到了那一步，一个形状不合法的目标确实只是"请求体不合法"。
    """

    EMPTY_TARGET = "empty_target"
    FORBIDDEN_CHARACTERS = "forbidden_characters"
    UNSUPPORTED_SCHEME = "unsupported_scheme"
    CREDENTIALS_IN_URL = "credentials_in_url"
    GIT_REPOSITORY_PATH = "git_repository_path"
    INVALID_HOST = "invalid_host"
    INVALID_PORT = "invalid_port"
    QUERY_OR_FRAGMENT_NOT_ALLOWED = "query_or_fragment_not_allowed"


class TargetRejected(Exception):
    """规范化失败。**刻意不是 `ConsoleError` 的子类**，理由见 `RejectionReason`。

    `args` 里只放 `reason.value`：`str(exc)` 因此不会带上用户输入的原文。目标不是凭据，
    但它会进日志，而"日志里原样回显未经校验的输入"本身就是一类坑（日志注入）。
    需要原文的调用方从 `.raw` 取，并自己决定要不要落盘。
    """

    def __init__(self, reason: RejectionReason, raw: str) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.raw = raw


# =============================================================================
# 二、输入与输出的形状
#
# 全部是 frozen dataclass，不是 Pydantic 模型：本模块是内部逻辑层，不是 HTTP 边界
# （CLAUDE.md §Python：边界层用 Pydantic v2，内部传参用 dataclass）。T8 的路由把
# GuardVerdict 翻成自己的响应模型 —— 那一层才需要 JSON schema 与 extra="forbid"。
# =============================================================================


@dataclass(frozen=True, slots=True)
class NormalizedTarget:
    """`normalize_target()` 的产物。所有下游只许用它，不许再碰原始输入。"""

    raw: str
    """用户输入去掉首尾空白之后的原文。只用于审计与 UI 回显。"""

    url: str
    """规范化后的完整 URL。**这一串才是交给 Strix `-t` 的值。**"""

    scheme: str
    """`http` 或 `https`，已小写。"""

    host: str
    """ASCII 形主机名（IDNA 之后、小写）或字面 IP。IPv6 **不带**方括号。"""

    host_unicode: str
    """IDNA 之前的主机名（已小写）。与 `host` 不同时说明发生了同形字折叠。"""

    port: int | None
    """显式端口。`None` = 用 scheme 的默认端口（已把 `:80`/`:443` 规范化掉）。"""

    path: str
    """路径，可能是空串（`http://juice-shop:3000` 这种形式刻意不补尾部斜杠）。"""

    is_ip: bool
    """`host` 是字面 IP。为真时不需要 DNS 解析，也就没有 split-horizon 的可能。"""

    punycode_applied: bool
    """IDNA 改动过主机名 —— 验收 3 要求的"标记"。

    为真的两种情形都值得在 UI 上高亮：真正的 punycode（`例子.中国` →
    `xn--fsqu00a.xn--fiqs8s`），以及全角折叠（`ＥＸＡＭＰＬＥ.com` → `example.com`）。
    两者都是"你看到的字符和实际连接的主机不是一回事"。
    """


@dataclass(frozen=True, slots=True)
class ClassifiedAddress:
    """一个解析出的地址的分类结果。整条记录会进 `authorizations.resolved_ips_json` 与审计。"""

    address: str
    """经 `ipaddress` 规范化的地址字符串（IPv6 压缩形、去掉 zone id）。"""

    version: int
    """4 或 6。UI 用它区分 A / AAAA，审计里也需要。"""

    category: TargetCategory
    """永远不会是 `MIXED`（见 `TargetCategory` 的注释）。"""

    rule: str
    """命中的规则名，机器可读、进审计，**不是文案**。

    取值域是本模块下方各常量表的键名（`aws_imds_v4`、`rfc1918`、`cgnat`…）。
    刻意用 `str` 而不是再开一个枚举：它是给人排查用的标签，不参与任何分支判断，
    而一个十八个成员的枚举只会让每次加一条黑名单都要改两处。
    """

    embedded_ipv4: str | None
    """这个 IPv6 地址里嵌着的 IPv4 —— 分类实际用的是它。

    非 `None` 意味着有人（或某个 DNS）用 IPv6 的外衣包了一个 IPv4 目标：
    `::ffff:169.254.169.254`（IPv4-mapped）、`2002:a9fe:a9fe::`（6to4）、
    `64:ff9b::a9fe:a9fe`（NAT64 众所周知前缀）。这三种都是真实存在的绕过形状，
    不是假想 —— 已实测 `64:ff9b::a9fe:a9fe` 在 Python 里 `is_global == True`。
    """


@dataclass(frozen=True, slots=True)
class AllowlistDecision:
    """白名单对这个目标的结论。**由 T8 的 `allowlist.py` 产出，本模块只消费。**

    为什么是"结论"而不是整份白名单：护栏需要知道的只有五件事，而白名单文件里还有
    `label/owner/expires/hosts/cidrs/max_budget_usd/forbidden_paths` 等等 —— 那些属于
    T8 与 T12。把整份配置传进来，护栏就会长出"最长后缀优先"这类匹配逻辑的副本。

    `no_allowlist()` 是给"还没有白名单文件"和单元测试用的中性默认值。
    """

    mode: AllowlistMode
    matched: bool
    """命中了某个条目。只对 `PUBLIC` + `ENFORCE` 有意义。"""

    allow_loopback: bool
    """命中条目的 `allow_loopback: true`。与操作者勾选是**或**的关系。"""

    allow_private: bool
    """命中条目的 `allow_private: true`。"""

    allow_reserved: bool
    """运营商/保留地址的放行。**UI 永不提供这一项** —— 只能手改 `allowlist.yaml`。"""

    entry_label: str | None
    """命中条目的 `label`，进审计。没命中时为 `None`。不是文案，不做插值。"""


def no_allowlist() -> AllowlistDecision:
    """没有白名单（或还没加载）时的中性结论：advisory、未命中、什么都不放行。

    为什么默认 `ADVISORY` 而不是 `ENFORCE`：`enforce` 的语义是"只有清单里的才准扫"，
    在没有清单的情况下那等于"什么都不准扫"，控制台第一次启动就是死的。真正的默认值
    由 `allowlist.yaml` 的 `mode` 说话（T8）；这里只负责"文件缺席时不要假装很严"。
    非公网类别不受 mode 影响，仍然要勾选，所以这个默认值并没有放宽任何一条护栏。
    """
    return AllowlistDecision(
        mode=AllowlistMode.ADVISORY,
        matched=False,
        allow_loopback=False,
        allow_private=False,
        allow_reserved=False,
        entry_label=None,
    )


@dataclass(frozen=True, slots=True)
class OperatorOptIn:
    """操作者在向导里勾了哪几项技术放行。两个字段默认 `False`。"""

    loopback: bool = False
    private: bool = False


NO_OPT_IN = OperatorOptIn()
""""什么都没勾"。

存在的唯一理由是 ruff 的 B008 不许在参数默认值里调构造函数 —— 对 frozen dataclass
来说那条规则的原始担心（可变默认值被共享修改）并不成立，但让 lint 闭嘴的正确做法是
给它一个模块级单例，而不是加 `# noqa`。它是不可变的，所以这**不是**可变全局状态
（CLAUDE.md §Python「模块级不得有可变全局状态」）。
"""


@dataclass(frozen=True, slots=True)
class GuardVerdict:
    """`evaluate_target()` 的产物。T8 的响应模型、T12 的准入判断、审计记录都从这里取。"""

    target: NormalizedTarget
    addresses: tuple[ClassifiedAddress, ...]
    """解析出的每一个地址各一条。顺序与调用方传入的顺序一致（审计要可复现）。"""

    category: TargetCategory
    """汇总类别，UI 的标题就用它。可能是 `MIXED`。"""

    requirement: GuardRequirement
    """`category` 的放行条件。与 `allowed` 正交，见 `GuardRequirement` 的注释。"""

    overridable: bool
    """这个类别**存在**从 UI 放行的途径吗。同样与 `allowed` 正交。

    验收 3 点名要 `metadata` 的这一项是 `false`。它是 `category` 的纯函数：
    勾选之后它不会变（变的是 `allowed`）。`carrier_reserved` 也是 `false` ——
    它能放行，但只能手改 `allowlist.yaml`，UI 上没有那个入口。
    """

    allowed: bool
    """结合本次的白名单结论与勾选状态，现在放不放行。"""

    required_opt_in: tuple[OptInFlag, ...]
    """还缺哪几项勾选。已满足的不列在这里，所以 `allowed` 为真时它一定是空的。

    为什么要这个字段而不是让 UI 自己从 `category` 推：一个主机名可以同时解析到
    环回和内网地址（`localhost` 在某些 `/etc/hosts` 里就是），那时两项勾选都要。
    从汇总类别推只能推出一项。
    """

    error_code: str | None
    """不放行、且调用方决定就此拒绝时该抛的 `app/errors.py` 机器码。

    只有"缺勾选"这一种不放行是 `None` —— 那不是错误，是向导还没走完。T12 在
    服务端重校验时如果发现 `required_opt_in` 非空，抛的是 `InvalidRequestError`
    （请求体少了一个必填的确认标志），而不是护栏码。
    """

    note_code: str | None
    """需要当场向用户解释清楚的那句话的**机器码**。目前只有 `loopback` 有（验收 3）。

    这个字段一度叫 `note_zh`，直接返回中文正文 —— 理由是"内容是一条代码级事实
    （`scan_setup.py:51` 把环回改写成 `host.docker.internal`），跟着 Strix 版本变、
    不跟着界面措辞变"。已否掉：`errors.py` 的模块 docstring 把规矩连理由一起写死了
    ——「同一个码在不同界面位置需要不同措辞，后端硬编码一句就把前端锁死了」。这句话
    恰好正中该理由：T8 要把它塞进输入框下方一行行内提示，T18 的向导有整段的篇幅，
    审计与报告里又是第三种口径。后端返回一句定稿中文，那三处只能共用最长的那一版。
    「跟着 Strix 版本变」成立的是**这条提示该不该出现**（由本模块判定，仍在后端），
    不是它的措辞。

    取值域固定为下方 `_LOOPBACK_NOTE_CODE` 一个值。它**不属于** `errors.*` 那棵树
    （不是错误，是解释），前端在 `targetGuard.notes.*` 下查文案 —— 那棵子树连同
    `RejectionReason` 的文案一起由 T8 落地（见 `PLAN.md` T8 行）。
    """


# =============================================================================
# 三、规范化
# =============================================================================

# 除了空白与不可打印字符之外，额外拒绝的单字符。
#
# PLAN.md §护栏 点名的是「空白 / `;` / 反引号 / `$(` / 不可打印字符」。这里是它的超集：
# `$` 整个拒掉（覆盖 `$(` 与 `${` 两种形状），并补上其余 shell 元字符。
#
# 为什么值得比规格更严一点：目标字符串最终会作为 `-t <url>` 进 argv。我们走
# `subprocess` 的列表形式、**不经 shell**，所以这些字符本来就注入不了 —— 但 Strix 内部
# 会把目标拼进它自己的沙箱命令、提示词和文件名里，那些路径不在我们的控制范围内。
# 一个合法的扫描目标里不需要任何一个这些字符，所以这层限制的成本是零。
_FORBIDDEN_CHARS: frozenset[str] = frozenset(";`$|&<>\"'\\{}^")

# 允许的 scheme。**只有两个。** `file://` / `ftp://` / `git://` 都会让 Strix 的
# infer_target_type 走别的分支（见模块 docstring）。
_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}

# 主机名里允许的 ASCII 字符。IDNA 之后才检查。
#
# 这一步不能省：Python 的 `str.encode("idna")` 对**纯 ASCII 标签直接放行**，只查长度
# （已实测：`exa_mple.com`、`-bad.com`、`bad-.com` 全都原样通过）。也就是说 IDNA 编码
# 本身完全不做主机名合法性校验，RFC 1123 那套规则只能由我们自己查。
_HOST_ALLOWED: frozenset[str] = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-.")

_MAX_HOST_LENGTH = 253
_MAX_LABEL_LENGTH = 63


def _has_forbidden_characters(text: str) -> bool:
    """任何空白、任何 Unicode `C*` 类字符、任何 `_FORBIDDEN_CHARS` 成员。

    为什么按 `unicodedata.category` 的 `C` 开头判而不是列一张黑名单：要拦的是
    控制字符（Cc）、格式字符（Cf）、代理项（Cs）、私用区（Co）和未分配码位（Cn），
    而 Cf 里躺着零宽空格 U+200B 和从右向左覆盖 U+202E —— 前者已实测会被
    `encode("idna")` **静默吃掉**（`"example.com\\u200b"` → `"example.com"`），
    也就是说不在这里拦下，它就会消失得无影无踪，而 UI 上显示的仍是带零宽字符的原文。
    """
    for char in text:
        if char.isspace() or char in _FORBIDDEN_CHARS:
            return True
        if unicodedata.category(char).startswith("C"):
            return True
    return False


def _to_ascii_host(host: str, raw: str) -> str:
    """IDNA 编码 + RFC 1123 校验。返回小写 ASCII 主机名。

    # 为什么用标准库的 `str.encode("idna")`

    `CLAUDE.md` §编码哲学 4「依赖是负债」：不为这件事引 `idna` 包。`encodings.idna`
    是 IDNA 2003 + nameprep，它把 `例子。中国`（U+3002 表意句号）也当成标签分隔符
    （其 `Codec.encode` 用 `[\\u002E\\u3002\\uFF0E\\uFF61]` 切分），这正好是我们想要的
    ——同形字的点号不能悄悄变成主机名的一部分。

    # 已知的行为边界（都实测过）

    - **IDNA 2003 ≠ 浏览器的 UTS-46。** `faß.example` 在这里变 `fass.example`，
      而 Chrome 会变 `xn--fa-hia.example`。后果：这一类域名我们规范化出来的主机与
      浏览器实际访问的**不是同一个**。可接受 —— 两个方向都不放宽任何护栏（分类仍按
      解析结果做），只是逐字确认串会与浏览器地址栏不同。真要一致得引 `idna` 包。
    - **不小写纯 ASCII 标签**（`EXAMPLE.COM` 原样返回），所以下面显式 `.lower()`。
    - **不校验主机名合法性**（见 `_HOST_ALLOWED` 的注释），所以下面自己查。
    - 尾部根点（`example.com.`）会原样保留，所以进来之前先剥掉。
    """
    if host.endswith("."):
        host = host[:-1]  # 根域的尾点。留着会让 IDNA 与逐字确认串各自多一个字符。
    if not host:
        raise TargetRejected(RejectionReason.INVALID_HOST, raw)

    # 纯数字与点的主机名必须是合法 IPv4，否则拒绝。
    # 这一条拦的是 `127.1` / `2130706433` 这类简写：`ipaddress` 不认它们（已实测），
    # 于是它们会被当成"域名"走到这里，而 `getaddrinfo` 认，最后真连到 127.0.0.1。
    # 地址级分类仍然会兜住它（解析结果是 127.0.0.1），但在规范化这一步就拒掉更诚实 ——
    # 否则 UI 会把一个 IP 显示成域名，逐字确认串也会要求用户去打一个"注册域名"。
    if all(char in "0123456789." for char in host):
        raise TargetRejected(RejectionReason.INVALID_HOST, raw)

    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except (UnicodeError, UnicodeDecodeError) as exc:
        # 只捕获这两类：空标签、标签过长、nameprep 拒绝的码位都是 UnicodeError。
        # 不写 bare except（CLAUDE.md §Python）。
        raise TargetRejected(RejectionReason.INVALID_HOST, raw) from exc

    if len(ascii_host) > _MAX_HOST_LENGTH:
        raise TargetRejected(RejectionReason.INVALID_HOST, raw)
    labels = ascii_host.split(".")
    for label in labels:
        if not 0 < len(label) <= _MAX_LABEL_LENGTH:
            raise TargetRejected(RejectionReason.INVALID_HOST, raw)
        if label.startswith("-") or label.endswith("-"):
            raise TargetRejected(RejectionReason.INVALID_HOST, raw)
        if any(char not in _HOST_ALLOWED for char in label):
            raise TargetRejected(RejectionReason.INVALID_HOST, raw)
    return ascii_host


def _parse_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """主机名是字面 IP 就返回它，否则 `None`。"""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def normalize_target(raw: str) -> NormalizedTarget:
    """把用户输入的一行文本变成一个我们愿意交给 Strix 的 URL。

    失败时抛 `TargetRejected`。**不做任何 DNS 解析** —— 返回值里的 `host` 是一个待解析
    的名字，分类是 `evaluate_target()` 的事。

    首尾空白先剥掉、内部空白直接拒：粘贴一段网址必然带上尾随换行，那不是注入；
    而网址中间出现空格只有两种可能 —— 打错了，或者有人想塞第二个参数进去。
    """
    stripped = raw.strip()
    if not stripped:
        raise TargetRejected(RejectionReason.EMPTY_TARGET, raw)
    if _has_forbidden_characters(stripped):
        raise TargetRejected(RejectionReason.FORBIDDEN_CHARACTERS, raw)

    # 补 scheme 之前先挡掉 `/path` 与 `//host` 两种形状：前者根本不是网址，后者补上
    # scheme 会变成 `https:////host`。
    if stripped.startswith("/"):
        raise TargetRejected(RejectionReason.INVALID_HOST, raw)

    # 按 `://` 判断有没有 scheme，**不能**直接交给 urlsplit 去看它解析出的 scheme：
    # 已实测 `example.com:8080` 会被解析成 scheme=`example.com`、path=`8080`，
    # 于是"补上默认 scheme"这一步永远不会触发，用户填的端口号变成了路径。
    candidate = stripped if "://" in stripped else f"https://{stripped}"

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        # 括号不配对的 IPv6（`https://[fd00::1/`）会走到这里。
        raise TargetRejected(RejectionReason.INVALID_HOST, raw) from exc

    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise TargetRejected(RejectionReason.UNSUPPORTED_SCHEME, raw)

    # `user:pass@` —— 见模块 docstring 第一条。两种写法都要拦：
    # `admin:pw@host`（urlsplit 给出 username+password）与 `admin@host`（只有 username）。
    # 再顺手拦 netloc 里裸露的 `@`，因为 urlsplit 只认最后一个 `@` 之前的部分。
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise TargetRejected(RejectionReason.CREDENTIALS_IN_URL, raw)

    if parts.query or parts.fragment:
        # 查询串与片段对"扫哪台主机"这个判断没有贡献，却会进入逐字确认串和 argv。
        # 一起拒掉比只拒 `#` 一致：否则 `?a=1` 能过而 `?a=1&b=2` 因为 `&` 被拒，
        # 用户完全看不出规则是什么。
        raise TargetRejected(RejectionReason.QUERY_OR_FRAGMENT_NOT_ALLOWED, raw)

    host_raw = parts.hostname
    if not host_raw:
        raise TargetRejected(RejectionReason.INVALID_HOST, raw)
    if "%" in host_raw:
        # IPv6 的 zone id（`[fe80::1%25eth0]`）。刻意拒绝而不是接受：zone 只对**本机的
        # 某一张网卡**有意义，而扫描跑在一个兄弟容器里 —— 那里没有同名网卡，这个目标
        # 必然测不到。接受它只会换来一次跑十分钟才失败的扫描。
        raise TargetRejected(RejectionReason.INVALID_HOST, raw)

    try:
        port = parts.port
    except ValueError as exc:
        # `:99999` 与 `:abc` 都在访问 `.port` 时才抛（已实测）。
        raise TargetRejected(RejectionReason.INVALID_PORT, raw) from exc

    path = parts.path
    if path.lower().rstrip("/").endswith(".git"):
        # 见模块 docstring 第二条：`_is_http_git_repo` 会发真实网络请求。
        raise TargetRejected(RejectionReason.GIT_REPOSITORY_PATH, raw)

    # urlsplit 的 hostname 已经小写过（含全角字符），这里再取一次是为了让
    # host_unicode / host 的对照关系只依赖本函数，不依赖 urlsplit 的实现细节。
    #
    # 根域尾点在**这里**就剥掉，不能只在 `_to_ascii_host` 里剥：`punycode_applied` 是
    # `host != host_unicode`，两边尾点不一致会让 `example.com.` 被报成"发生了同形字
    # 折叠"，于是 UI 弹一个根本没有的警告。
    host_unicode = host_raw.lower().rstrip(".")
    if not host_unicode:
        raise TargetRejected(RejectionReason.INVALID_HOST, raw)
    literal = _parse_ip_literal(host_unicode)
    if literal is not None:
        # 字面 IP：用 ipaddress 的规范形（IPv6 压缩、大写变小写），不走 IDNA。
        host = str(literal)
        is_ip = True
    else:
        host = _to_ascii_host(host_unicode, raw)
        is_ip = False

    # 默认端口规范化掉。`http://x:80` 与 `http://x` 是同一个目标，而它们会分别出现在
    # 逐字确认串、审计记录和白名单条目里 —— 留两种写法就等于留一处不一致。
    if port is not None and port == _DEFAULT_PORTS[scheme]:
        port = None

    bracketed = f"[{host}]" if is_ip and literal is not None and literal.version == 6 else host
    netloc = bracketed if port is None else f"{bracketed}:{port}"
    url = f"{scheme}://{netloc}{path}"

    return NormalizedTarget(
        raw=stripped,
        url=url,
        scheme=scheme,
        host=host,
        host_unicode=host_unicode,
        port=port,
        path=path,
        is_ip=is_ip,
        punycode_applied=host != host_unicode,
    )


# =============================================================================
# 四、地址分类
#
# 检查顺序就是下面这几张表的顺序，**不能改**。已实测的两处反直觉：
#   · `169.254.169.254`、`fd00:ec2::254`、`fe80::a9fe:a9fe` 在 ipaddress 眼里分别只是
#     link-local 和 ULA —— 元数据地址必须先按精确值查，否则会被归成 private 而变成
#     "勾一下就能扫"。
#   · `224.0.0.1` 与 `ff02::1` 的 `is_global` 都是 **True**（组播地址在 Python 里算全球
#     可路由）—— 组播必须在 `is_global` 兜底之前查掉，否则会被当成公网目标。
# =============================================================================

# 云厂商元数据地址。**永久硬拦、不可覆盖**（CLAUDE.md §安全不变式）。
#
# 这张表是最后一道保险，不是主防线：一个指向元数据地址的主机名，其解析结果本来就会被
# 逐个地址分类拦下。它存在是为了两种情况 —— 解析结果拿不到时（主机名分类），以及
# 有人把元数据 IP 直接填进输入框时（一眼可见的拒绝比"解析后才说不行"好）。
_METADATA_IPV4: dict[str, str] = {
    # AWS / GCP / Azure / OpenStack / Oracle / DigitalOcean / 华为云 共用这一个。
    "169.254.169.254": "imds_v4",
    # AWS ECS 任务元数据与**任务角色凭据**端点。拿到它等于拿到该任务的 AWS 凭据。
    "169.254.170.2": "aws_ecs_task_metadata",
    # 阿里云。
    "100.100.100.200": "alibaba_metadata",
    # 腾讯云。
    "169.254.0.23": "tencent_metadata",
    # Oracle Cloud Classic。
    "192.0.0.192": "oracle_classic_metadata",
}
_METADATA_IPV6: dict[str, str] = {
    # AWS IMDS over IPv6。注意它落在 fd00::/8（ULA）里，不查精确值就会被归成 private。
    "fd00:ec2::254": "aws_imds_v6",
    # GCP / Azure 的 IPv6 元数据，形状是 link-local 里嵌 169.254.169.254。
    "fe80::a9fe:a9fe": "imds_v6_link_local",
}
# 元数据主机名。精确匹配（都已小写、已 IDNA）。
_METADATA_HOSTS: dict[str, str] = {
    "metadata.google.internal": "gcp_metadata_host",
    "metadata": "gcp_metadata_short_host",  # GCP 内部的单标签简写
    "instance-data": "aws_legacy_metadata_host",
    "instance-data.ec2.internal": "aws_legacy_metadata_host",
    "metadata.tencentyun.com": "tencent_metadata_host",
}

_LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost"})
# RFC 6761：`.localhost` 整棵子树都是环回。
_LOOPBACK_SUFFIXES: tuple[str, ...] = (".localhost",)

# PLAN.md §护栏 的 private 行点名这三个后缀。刻意不扩到 `.home.arpa` 之类：
# 没在规格里、也没有实测需求，加了就是"为以后可能"预留（CLAUDE.md §编码哲学 3）。
_PRIVATE_SUFFIXES: tuple[str, ...] = (".local", ".internal", ".lan")

_RFC1918: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
_IPV6_ULA = ipaddress.ip_network("fc00::/7")

# 运营商保留 / 文档 / 基准测试 / 保留段。策略：拦，只能从白名单**文件**放行。
_RESERVED_IPV4: tuple[tuple[ipaddress.IPv4Network, str], ...] = (
    (ipaddress.ip_network("100.64.0.0/10"), "cgnat"),
    (ipaddress.ip_network("192.0.2.0/24"), "documentation"),
    (ipaddress.ip_network("198.51.100.0/24"), "documentation"),
    (ipaddress.ip_network("203.0.113.0/24"), "documentation"),
    (ipaddress.ip_network("198.18.0.0/15"), "benchmark"),
    (ipaddress.ip_network("192.0.0.0/24"), "ietf_protocol_assignments"),
    (ipaddress.ip_network("0.0.0.0/8"), "this_network"),
    (ipaddress.ip_network("169.254.0.0/16"), "link_local"),
    (ipaddress.ip_network("240.0.0.0/4"), "reserved"),
)
# 刻意**没有** 2001::/32（teredo）与 2002::/16（6to4）两行：它们的每一个地址都会被
# `_embedded_ipv4()` 先拆成内层 IPv4，永远走不到这张表 —— 写上去就是死代码。
_RESERVED_IPV6: tuple[tuple[ipaddress.IPv6Network, str], ...] = (
    (ipaddress.ip_network("2001:db8::/32"), "documentation"),
    (ipaddress.ip_network("100::/64"), "discard"),
    (ipaddress.ip_network("fe80::/10"), "link_local"),
)

# NAT64 众所周知前缀（RFC 6052）。Python 没有对应属性，得自己查。
# 为什么值得单列：`64:ff9b::a9fe:a9fe` 的 `is_global` 是 **True**（已实测），
# 而它在启用了 NAT64 的网络里就是 169.254.169.254。这是真实的绕过形状。
_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")


def _embedded_ipv4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """IPv6 里嵌着的 IPv4，没有就 `None`。四种形状，逐个显式查。

    刻意不写成"遍历一张 (前缀, 提取函数) 表"：四个分支的提取方式各不相同
    （三个走 ipaddress 的现成属性、NAT64 要自己算），表格化只会多一层间接。
    """
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address.teredo is not None:
        # teredo 返回 (服务器, 客户端)。我们要的是客户端 —— 那才是流量的终点。
        return address.teredo[1]
    if address in _NAT64_WELL_KNOWN:
        # 低 32 位就是 IPv4 地址（RFC 6052 §2.2 的 /96 情形）。
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return None


def _classify_ipv4(address: ipaddress.IPv4Address) -> tuple[TargetCategory, str]:
    """一个 IPv4 地址的类别与命中规则。顺序见本节开头的注释。"""
    text = str(address)
    if text in _METADATA_IPV4:
        return TargetCategory.METADATA, _METADATA_IPV4[text]
    if address.is_loopback:
        return TargetCategory.LOOPBACK, "loopback"
    if any(address in network for network in _RFC1918):
        return TargetCategory.PRIVATE, "rfc1918"
    if address.is_multicast:
        return TargetCategory.CARRIER_RESERVED, "multicast"
    if text == "255.255.255.255":
        return TargetCategory.CARRIER_RESERVED, "broadcast"
    if address.is_unspecified:
        return TargetCategory.CARRIER_RESERVED, "unspecified"
    for network, rule in _RESERVED_IPV4:
        if address in network:
            return TargetCategory.CARRIER_RESERVED, rule
    if address.is_global:
        return TargetCategory.PUBLIC, "global_unicast"
    # 兜底偏严：ipaddress 说它不是全球可路由，而我们上面没认出来是什么。
    # 归到"只能改文件放行"而不是 public —— 未知的非全球地址不该走公网那条路。
    return TargetCategory.CARRIER_RESERVED, "not_globally_routable"


def _classify_ipv6(address: ipaddress.IPv6Address) -> tuple[TargetCategory, str]:
    """一个 IPv6 地址（已去 zone、未嵌 IPv4）的类别与命中规则。"""
    text = str(address)
    if text in _METADATA_IPV6:
        return TargetCategory.METADATA, _METADATA_IPV6[text]
    if address.is_loopback:
        return TargetCategory.LOOPBACK, "loopback"
    if address in _IPV6_ULA:
        return TargetCategory.PRIVATE, "ipv6_ula"
    if address.is_multicast:
        return TargetCategory.CARRIER_RESERVED, "multicast"
    if address.is_unspecified:
        return TargetCategory.CARRIER_RESERVED, "unspecified"
    for network6, rule in _RESERVED_IPV6:
        if address in network6:
            return TargetCategory.CARRIER_RESERVED, rule
    if address.is_global:
        return TargetCategory.PUBLIC, "global_unicast"
    return TargetCategory.CARRIER_RESERVED, "not_globally_routable"


def classify_address(text: str) -> ClassifiedAddress:
    """给 `getaddrinfo` 返回的一个地址定类别。纯函数，不解析、不查询。

    `text` 可以带 IPv6 的 zone id（`fe80::1%eth0`）—— 解析器确实会返回它。
    zone 只影响"从哪张网卡出去"，不影响这个地址是什么，所以分类前去掉。

    非法地址串抛 `ValueError`（来自 `ipaddress`）：那是调用方的 bug，不是用户输入
    问题 —— 到这一步的字符串应该来自解析器而不是输入框。
    """
    address = ipaddress.ip_address(text)
    if isinstance(address, ipaddress.IPv4Address):
        category, rule = _classify_ipv4(address)
        return ClassifiedAddress(
            address=str(address),
            version=4,
            category=category,
            rule=rule,
            embedded_ipv4=None,
        )

    if address.scope_id is not None:
        address = ipaddress.IPv6Address(text.split("%", 1)[0])
    inner = _embedded_ipv4(address)
    if inner is not None:
        category, rule = _classify_ipv4(inner)
        return ClassifiedAddress(
            address=str(address),
            version=6,
            category=category,
            rule=rule,
            embedded_ipv4=str(inner),
        )
    category, rule = _classify_ipv6(address)
    return ClassifiedAddress(
        address=str(address),
        version=6,
        category=category,
        rule=rule,
        embedded_ipv4=None,
    )


def classify_host(host: str) -> tuple[TargetCategory, str] | None:
    """只看主机名本身能不能定类别。定不了就返回 `None`（交给地址分类）。

    `host` 必须是 `NormalizedTarget.host`（已小写、已 IDNA）。

    这层判断不能替代地址分类，只能**叠加**在它之上：`.internal` 结尾的名字可能解析到
    公网（那就是 split-horizon），而一个普通的公网名字也可能解析到 10.0.0.1。
    """
    if host in _METADATA_HOSTS:
        return TargetCategory.METADATA, _METADATA_HOSTS[host]
    if host in _LOOPBACK_HOSTS or host.endswith(_LOOPBACK_SUFFIXES):
        return TargetCategory.LOOPBACK, "loopback_host"
    if host.endswith(_PRIVATE_SUFFIXES):
        return TargetCategory.PRIVATE, "private_suffix"
    if "." not in host:
        # 单标签主机名。这正是 docker 网络里的服务别名（`juice-shop`）与内网
        # 短名（`gitlab`）的形状 —— 它们只在某一个网络内部有意义，天然是内网目标。
        return TargetCategory.PRIVATE, "single_label_host"
    return None


# =============================================================================
# 五、策略
# =============================================================================

# 汇总时的"谁更严"。数字越大越严，取最大者作为 GuardVerdict.category。
# METADATA 与 MIXED 不参与（它们由更前面的规则直接决定）。
_SEVERITY: dict[TargetCategory, int] = {
    TargetCategory.PUBLIC: 0,
    TargetCategory.PRIVATE: 1,
    TargetCategory.LOOPBACK: 2,
    TargetCategory.CARRIER_RESERVED: 3,
}

# 非公网。用来判 split-horizon：公网 + 任何一个非公网 = mixed。
#
# 为什么把 CARRIER_RESERVED 也算进"非公网"，虽然 PLAN 的原话是"公网和内网"：
# 一个名字同时解析到 93.184.216.34 和 100.64.0.1，形状与解析到 10.0.0.1 完全一样 ——
# 都是"我们无法知道你授权的是哪一个"。往严的一侧靠。
_NON_PUBLIC: frozenset[TargetCategory] = frozenset(
    {TargetCategory.LOOPBACK, TargetCategory.PRIVATE, TargetCategory.CARRIER_RESERVED}
)

# 环回目标必须当场解释清楚的那件事。**只给码，正文在前端**（见 GuardVerdict.note_code）。
# 这个码要说的是：Strix 会把环回地址改写成 host.docker.internal，也就是沙箱访问的是
# 宿主机上的服务，不是沙箱容器内部的服务 —— 出处 `scan_setup.py:51`。
_LOOPBACK_NOTE_CODE = "loopback_rewrite"


def _policy_for(
    category: TargetCategory,
    allowlist: AllowlistDecision,
) -> tuple[GuardRequirement, bool]:
    """`(放行条件, UI 上有没有放行入口)`。**这就是 `PLAN.md` §护栏 那六行的可执行版本。**

    CLAUDE.md §编码哲学 7「不变量写进代码，不是写进文档」—— 改护栏策略改这个函数。

    写成 if 链而不是一张 dict 表，是因为 `PUBLIC` 那一行的条件依赖白名单 mode，
    夹在表里就得给它留一个"占位再覆盖"的特例。六个分支平铺出来反而一眼能对着规格读。
    两个返回值都**只看类别**，不看当前是否已满足（见 `GuardRequirement` 的注释）。
    """
    if category in (TargetCategory.METADATA, TargetCategory.MIXED):
        return GuardRequirement.IMPOSSIBLE, False
    if category is TargetCategory.CARRIER_RESERVED:
        # 能放行，但只能手改 allowlist.yaml —— UI 上没有那个入口，所以 overridable=False。
        return GuardRequirement.ALLOWLIST_FILE_ONLY, False
    if category in (TargetCategory.LOOPBACK, TargetCategory.PRIVATE):
        return GuardRequirement.OPERATOR_OPT_IN, True
    # PUBLIC
    if allowlist.mode is AllowlistMode.ADVISORY:
        return GuardRequirement.NONE, False
    return GuardRequirement.ALLOWLIST_ENTRY, True


def _category_satisfied(
    category: TargetCategory,
    allowlist: AllowlistDecision,
    opt_in: OperatorOptIn,
) -> bool:
    """单个类别当前满不满足放行条件。

    逐类别判而不是只判汇总类别：一个主机名可以同时解析到环回和内网地址，那时两项勾选
    都要有。只看汇总类别会漏掉另一项。
    """
    if category in (TargetCategory.METADATA, TargetCategory.MIXED):
        return False
    if category is TargetCategory.CARRIER_RESERVED:
        return allowlist.allow_reserved
    if category is TargetCategory.LOOPBACK:
        return opt_in.loopback or allowlist.allow_loopback
    if category is TargetCategory.PRIVATE:
        return opt_in.private or allowlist.allow_private
    # PUBLIC
    return allowlist.mode is AllowlistMode.ADVISORY or allowlist.matched


def _error_code_for(category: TargetCategory) -> str | None:
    """不放行时该抛哪个 `app/errors.py` 的码。

    `LOOPBACK` / `PRIVATE` 返回 `None` —— "还没勾选"不是错误，见
    `GuardVerdict.error_code` 的注释。
    """
    if category is TargetCategory.METADATA:
        return "blocked_metadata"
    if category is TargetCategory.MIXED:
        return "split_horizon"
    if category in (TargetCategory.CARRIER_RESERVED, TargetCategory.PUBLIC):
        return "not_in_allowlist"
    return None


def evaluate_target(
    target: NormalizedTarget,
    resolved_addresses: Sequence[str],
    *,
    allowlist: AllowlistDecision,
    opt_in: OperatorOptIn = NO_OPT_IN,
) -> GuardVerdict:
    """对一个已规范化的目标做完整判定。纯函数、零 IO。

    `resolved_addresses` 是 `getaddrinfo` 对 `target.host` 解析出的 A + AAAA 地址串。
    目标是**主机名**而序列为空则抛 `ValueError`：那是调用方少走了解析这一步，属于程序
    错误 —— 静默当成"没有地址所以放行"会是本模块最糟糕的失败模式。

    目标是**字面 IP** 时序列可以为空（没有 DNS 这一步）。此时字面量自己**总是**被分类，
    调用方另外传进来的地址只会追加进去、不会取代它：护栏的强度不该取决于调用方在这个
    参数里填了什么。`getaddrinfo("169.254.169.254")` 本来就返回同一个地址，所以正常
    调用下这里不会多出任何一条。

    元数据主机名（`metadata.google.internal`）即使解析结果全是公网地址，也照样硬拦。
    """
    if target.is_ip:
        texts: tuple[str, ...] = (
            target.host,
            *(text for text in resolved_addresses if text != target.host),
        )
    elif not resolved_addresses:
        raise ValueError(
            f"主机名 {target.host!r} 没有传入解析结果。"
            "evaluate_target 不做 DNS 解析（本模块零 IO），调用方必须先解析。"
        )
    else:
        texts = tuple(resolved_addresses)

    addresses = tuple(classify_address(text) for text in texts)
    categories = {item.category for item in addresses}

    # 主机名级别的判定只对**名字**有意义：`classify_host` 里的单标签规则会把
    # `::1` 这种没有点的字面 IPv6 误判成 docker 服务别名（→ private），于是环回目标
    # 会多要一个"内网确认"勾选。字面 IP 的结论已经由地址分类给全了。
    if not target.is_ip:
        host_hit = classify_host(target.host)
        if host_hit is not None:
            categories.add(host_hit[0])

    # ---- 汇总类别 -----------------------------------------------------------
    if TargetCategory.METADATA in categories:
        # 永久硬拦，不可覆盖。白名单与勾选一概不看 —— 这一点是 CLAUDE.md §安全不变式
        # 里"不可覆盖"那句话的执行点，测试里有对应用例（给足所有放行仍然拦）。
        category = TargetCategory.METADATA
    elif TargetCategory.PUBLIC in categories and categories & _NON_PUBLIC:
        category = TargetCategory.MIXED
    else:
        category = max(categories, key=lambda item: _SEVERITY[item])

    # ---- 策略 ---------------------------------------------------------------
    requirement, overridable = _policy_for(category, allowlist)
    hard_blocked = category in (TargetCategory.METADATA, TargetCategory.MIXED)

    if hard_blocked:
        # 硬拦时逐类别的"还缺哪个勾选"是噪音 —— 勾完也不会放行。
        allowed = False
        required: tuple[OptInFlag, ...] = ()
    else:
        allowed = all(_category_satisfied(item, allowlist, opt_in) for item in categories)
        required = tuple(
            flag
            for flag, needed in (
                (OptInFlag.LOOPBACK, TargetCategory.LOOPBACK),
                (OptInFlag.PRIVATE, TargetCategory.PRIVATE),
            )
            if needed in categories and not _category_satisfied(needed, allowlist, opt_in)
        )

    return GuardVerdict(
        target=target,
        addresses=addresses,
        category=category,
        requirement=requirement,
        overridable=overridable,
        allowed=allowed,
        required_opt_in=required,
        error_code=None if allowed else _error_code_for(category),
        # 硬拦时不给环回说明：那句话是在教用户怎么正确地扫本机，而这次根本不会扫。
        note_code=(
            _LOOPBACK_NOTE_CODE
            if TargetCategory.LOOPBACK in categories and not hard_blocked
            else None
        ),
    )
