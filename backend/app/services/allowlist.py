"""授权清单 —— 加载 / 校验 / 热重载 / 匹配。对应 `PLAN.md` §护栏 的「白名单」一段。

# 职责边界

本模块产出 `target_guard.AllowlistDecision`（一个扁平结论），**护栏本身不认识这个文件
格式**。反过来本模块也不做任何分类与策略判断 —— `mode` 只影响 `PUBLIC` 那一类，那条
规则写在 `target_guard._policy_for()` 里，不在这里复制一份。

# 三条"不许静默放宽"的规则，每条都在代码里有执行点

`enforce` 的含义是「只有清单里的目标才准扫」。它一旦被误降成 `advisory`，界面上什么都
不会变 —— 用户看不到区别，只是从此公网随便扫。所以这三种情形分别有明确的落点：

1. **文件解析失败**（YAML 语法错、字段不合法、读不出来）→ **保留内存里最后一次成功
   加载的配置**，并在快照上挂 `error`，让 `GET /api/allowlist` 能把"当前文件是坏的"
   如实说出来。绝不因为解析失败就退回 advisory。
2. **冷启动就解析失败**（内存里根本没有"上一次成功"）→ **失败关闭**：
   `mode=ENFORCE, matched=False`，也就是所有公网目标一律 `not_in_allowlist`。
   这一条比第 1 条更容易被写错成 advisory —— 那时"手滑把文件写坏"和"故意删掉限制"
   的效果完全一样。**刻意不选"拒绝启动"**：操作者需要那个还活着的界面才能看见
   `GET /api/allowlist` 里的错误码去改文件，而 `auth.json` 缺席时拒绝启动的理由
   （删掉它就绕过登录）在这里不成立 —— 现在坏文件是更严，不是更松。
3. **文件不存在** → `no_allowlist()`（advisory，什么都不放行）。这是**合法状态**，
   不是错误：控制台第一次启动时就是这样，而 `enforce` + 空清单等于什么都不准扫，
   界面开箱即死（理由完整写在 `target_guard.no_allowlist()` 的 docstring 里）。
   注意它与 2 的区别 —— 文件**缺席**是"我还没用这个功能"，文件**存在但坏**是
   "我在用，但它此刻不可读"。

`mode` 的默认值是 `ENFORCE`（见 `AllowlistConfig.mode`），与 3 不矛盾：文件缺席时我们
根本不构造 `AllowlistConfig`。

# 热重载：为什么指纹是 `(st_mtime_ns, st_size)`

`PLAN.md` 的原话是「按 mtime 热重载」，本模块比它严一点。`st_mtime` 是**秒**粒度，
在编辑器里改两次的间隔通常小于一秒 —— 第二次改动会被当成"文件没变"而完全不生效，
症状是"我把条目删了但还是能扫"。`st_mtime_ns` 是纳秒粒度，再叠一个 `st_size` 兜住
时间戳被 `touch -t` 之类的东西按回去的情形。

为什么不用内容 sha256：算它必须先把文件读出来，而"避免读文件"正是指纹存在的唯一目的。
文件只有几 KB，读一次也不贵 —— 真正的差别在于这条检查跑在**每一次**目标校验和每一次
扫描启动之前，一个纯 `stat()` 与一次 `open+read` 的差距在那个位置上是可以感知的。
**残余风险如实记录**：时间戳粒度粗（某些网络文件系统）+ 改动前后大小相同 + 改动发生在
同一个时间戳内，三者同时成立才会漏一次重载。我们自己经界面写文件时会**立即**把新配置
装进内存（`AllowlistStore.write`），所以那条路径根本不依赖指纹。
"""

from __future__ import annotations

import ipaddress
import logging
import os
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path

# 依赖论证（CLAUDE.md §编码哲学 4「依赖是负债，每个新依赖必须论证」）：
#
# 这**不是**新依赖 —— `pyyaml==6.0.3` 已经在 `requirements.lock:1506` 里带 hash pin
# 住了。它也不是可有可无的传递依赖：`strix-agent` 对它的要求是 `pyyaml>=6.0`
# （**非 extra、非可选**，已用 `importlib.metadata.requires` 实测）。而
# `strix-agent==1.5.3` 这个 pin 我们不许升级也不许放宽（CLAUDE.md §Strix 集成 第 1 条），
# 所以它不会从依赖树里消失。直接 import 它引入的额外风险被那条 pin 兜住了，为零。
#
# 为什么不用标准库：标准库里没有 YAML。而配置文件用 JSON 的代价是操作者要手写它 ——
# JSON 不能写注释，而这个文件**必须**能写注释（`allow_reserved` 只能手改，界面上
# 刻意没有那一项，那件事只能靠文件里的一行注释交代）。
#
# 为什么不引 `ruamel.yaml`（它能保住注释）：那是一个真正的新依赖，而它换来的好处只有
# "界面编辑不吃掉手写注释"—— 见下方 `_FILE_HEADER`，我们用一句生成头把这件事说清楚，
# 成本是零。
#
# **只许 `yaml.safe_load`，永远不许 `yaml.load`。** `yaml.load` 的默认 Loader 会执行
# `!!python/object/apply:os.system` 这类标签 —— 也就是说"编辑授权清单"会变成"任意代码
# 执行"。而这个文件是可经 `PUT /api/allowlist` 写入的，鉴权之后的 RCE 依然是 RCE。
# 出方向同理只用 `safe_dump`：`dump` 会把 Python 对象序列化成那种标签。
import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from app.models import BoundaryModel
from app.services.target_guard import AllowlistDecision, AllowlistMode, no_allowlist

logger = logging.getLogger(__name__)

# =============================================================================
# 一、文件格式
#
# 用 Pydantic 而不是手写校验：CLAUDE.md §Python 把"边界层（HTTP 出入参、配置、
# 白名单 YAML）一律 Pydantic v2 模型"列为硬要求。同一组模型同时是 YAML 的格式和
# `GET/PUT /api/allowlist` 的请求/响应体 —— 一份格式定义，两个入口，不可能对不上。
# =============================================================================

# 长度上限。它们防的不是恶意（能写这个文件的人已经过了鉴权），是**手滑**：
# 一个被粘错成整篇日志的 label 会进审计正文与 API 响应，把两处都撑坏。
_MAX_TEXT = 200
_MAX_HOST_PATTERN = 253  # 与 DNS 名字上限一致（target_guard._MAX_HOST_LENGTH）
_MAX_LIST = 200
_MAX_ENTRIES = 500

# host 模式里允许的字符。**只有 ASCII**，见 `_normalize_host_pattern` 的注释。
_HOST_PATTERN_ALLOWED = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-.:*[]")


class AllowlistEntry(BoundaryModel):
    """一条授权条目。字段表来自 `PLAN.md` §护栏。

    `label` 是这条条目的**标识**：它进审计、进 `AllowlistDecision.entry_label`，
    也是 `DELETE /api/allowlist/entries/{id}` 里的那个 `id`（见 `AllowlistConfig`
    的唯一性校验）。刻意不引入一个自增 id 或 uuid —— 那会让手写这个文件的人必须
    先想出一个没有意义的字符串，而 label 本来就必须能让人一眼看出这条是什么。
    """

    label: str = Field(min_length=1, max_length=_MAX_TEXT)
    """⚠️ 不许含 `/`、不许含控制字符、不许有首尾空白 —— 见 `_check_label`。"""

    owner: str = Field(min_length=1, max_length=_MAX_TEXT)
    authorization_ref: str = Field(min_length=1, max_length=_MAX_TEXT)

    expires: date | None = None
    """最后一个有效日期，**含当天**。`None` = 不过期。

    过期条目按"没命中"处理（见 `_match`），不是"命中了但被拒" —— 后者会让
    `entry_label` 指向一条已经失效的授权，而那个 label 会进审计。
    """

    hosts: tuple[str, ...] = Field(default=(), max_length=_MAX_LIST)
    """主机名，支持**单层**通配 `*.example.com`。

    单层的含义：`*.example.com` 命中 `a.example.com`，**不命中** `a.b.example.com`
    也不命中 `example.com` 自己。授权是按主机给的，而 `*.example.com` 这种写法在
    多层生效时会把一整棵子域树都授权掉 —— 那通常不是写它的人的意思。
    """

    cidrs: tuple[str, ...] = Field(default=(), max_length=_MAX_LIST)
    """网段，匹配**解析出的地址**（不是主机名）。"""

    allow_loopback: bool = False
    allow_private: bool = False

    allow_reserved: bool = False
    """运营商 / 保留地址（CGNAT、组播、`192.0.2.0/24`…）的放行。

    ⚠️ **界面上永不提供这一项。** `PLAN.md` §护栏 那张表写的是「拦，只能从白名单文件
    放行（UI 不给）」，`target_guard._policy_for()` 因此给 `CARRIER_RESERVED` 返回
    `overridable=False`。它没有出现在 `PLAN.md` 的条目字段清单里，但
    `AllowlistDecision.allow_reserved` 需要一个来源 —— 而"只能改文件"这句话本身就
    要求它是一个**条目字段**。放在条目上而不是文件顶层，是因为"允许扫某个运营商网段"
    这件事必须和某一条具体授权（label/owner/authorization_ref）绑在一起才有意义。
    """

    max_budget_usd: float | None = Field(default=None, gt=0)
    """这条授权允许的预算上限。**T8 不消费它**，T9/T12 会。"""

    forbidden_paths: tuple[str, ...] = Field(default=(), max_length=_MAX_LIST)
    """禁止触碰的路径前缀。**T8 不消费它**，T12 会（进 `--instruction`）。"""

    @field_validator("label", mode="after")
    @classmethod
    def _check_label(cls, value: str) -> str:
        """label 会成为 `DELETE /api/allowlist/entries/{label}` 的**路径段**。

        所以它必须能原样活过一个 URL 路径段：含 `/` 的 label 在那条路由上根本无法被
        表达（`%2F` 在 ASGI 服务器之间的解码时机不一致，这不是可以赌的东西）。
        控制字符与首尾空白同理拦掉 —— 前者进日志是注入面，后者会让"看起来一样的两条
        label"变成两条不同的条目。

        代价是一份手写的清单可能因此被判 `allowlist_invalid_shape`。这比"删不掉某一条
        授权"好：后者只能靠改文件解决，而那时界面上还显示着它。
        """
        if value != value.strip():
            raise ValueError(f"label {value!r} 不能有首尾空白")
        if "/" in value:
            raise ValueError(f"label {value!r} 不能含 `/`（它是删除接口的路径段）")
        if not value.isprintable():
            raise ValueError(f"label {value!r} 不能含控制字符")
        return value

    @field_validator("hosts", mode="after")
    @classmethod
    def _check_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_normalize_host_pattern(item) for item in value)

    @field_validator("cidrs", mode="after")
    @classmethod
    def _check_cidrs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_normalize_cidr(item) for item in value)

    @field_validator("forbidden_paths", mode="after")
    @classmethod
    def _check_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if not item.startswith("/") or len(item) > _MAX_TEXT:
                raise ValueError(f"forbidden_paths 里的 {item!r} 必须是以 / 开头的路径前缀")
        return value

    @model_validator(mode="after")
    def _must_match_something(self) -> AllowlistEntry:
        if not self.hosts and not self.cidrs:
            # 一条既没有 hosts 也没有 cidrs 的条目永远不会命中任何目标，但它长得像一条
            # 生效的授权。留着它等于在清单里放一句谎话。
            raise ValueError(f"条目 {self.label!r} 的 hosts 与 cidrs 不能同时为空")
        return self


class AllowlistConfig(BoundaryModel):
    """整个 `allowlist.yaml`。也是 `GET/PUT /api/allowlist` 的正文形状。"""

    mode: AllowlistMode = AllowlistMode.ENFORCE
    """`enforce` = 只有命中清单的公网目标才准扫；`advisory` = 公网不设限。

    **默认 `ENFORCE`**，也就是"文件里忘了写 mode"倒向更严的一侧。这与"文件不存在时
    是 advisory"不矛盾：文件存在意味着操作者在用这份清单，此时含糊应当从严；文件
    缺席意味着他还没开始用（模块 docstring 第 3 条）。
    """

    entries: tuple[AllowlistEntry, ...] = Field(default=(), max_length=_MAX_ENTRIES)

    @model_validator(mode="after")
    def _labels_are_unique(self) -> AllowlistConfig:
        seen: set[str] = set()
        duplicated: set[str] = set()
        for entry in self.entries:
            if entry.label in seen:
                duplicated.add(entry.label)
            seen.add(entry.label)
        if duplicated:
            # label 是 DELETE 的 id，也是审计里"改了哪一条"的唯一线索。重名会让
            # 「删掉那条 label 叫 x 的」变成一个没有确定答案的操作。
            raise ValueError(f"label 必须唯一，重复的有：{sorted(duplicated)}")
        return self


def _normalize_host_pattern(pattern: str) -> str:
    """小写、去根域尾点、校验字符集与通配形状。返回规范形。

    **只接受 ASCII。** 匹配的对方是 `NormalizedTarget.host`，那是已经过 IDNA 编码的
    形状（`例子.中国` → `xn--fsqu00a.xn--fiqs8s`）。在这里对 unicode 再做一次 IDNA
    意味着本模块有一份 `target_guard._to_ascii_host` 的副本，而两份实现只要在某个
    边角（全角折叠、尾点、空标签）上分歧，通配就会**静默停止命中** —— 那是最坏的一类
    分歧：清单看起来对，扫描却被拒，或者更糟，被放行。

    所以非 ASCII 直接报错，并在错误里告诉操作者去填 punycode 形 —— 那一串正好就是
    界面在目标旁边并排显示的东西（`NormalizedTarget.host`）。
    """
    cleaned = pattern.strip().lower().rstrip(".")
    if not cleaned:
        raise ValueError("hosts 里有空项")
    if len(cleaned) > _MAX_HOST_PATTERN:
        raise ValueError(f"hosts 里的 {pattern!r} 超过 {_MAX_HOST_PATTERN} 字符")
    if not cleaned.isascii():
        raise ValueError(
            f"hosts 里的 {pattern!r} 含非 ASCII 字符。请填 punycode 形"
            "（界面上目标旁边显示的那一串 xn-- 开头的名字）"
        )
    body = cleaned[2:] if cleaned.startswith("*.") else cleaned
    if not body or set(body) - _HOST_PATTERN_ALLOWED or "*" in body:
        raise ValueError(f"hosts 里的 {pattern!r} 不是合法主机名，通配只支持 `*.` 前缀一种")
    return cleaned


def _normalize_cidr(text: str) -> str:
    """校验网段并返回规范形。

    `strict=True`：`10.0.0.1/8` 会被**拒绝**而不是静默变成 `10.0.0.0/8`。那种写法
    百分之百是手误，而"静默纠正"会让操作者以为自己授权的是那台主机。
    """
    cleaned = text.strip()
    try:
        network = ipaddress.ip_network(cleaned, strict=True)
    except ValueError as exc:
        raise ValueError(f"cidrs 里的 {text!r} 不是合法网段：{exc}") from exc
    return str(network)


# =============================================================================
# 二、匹配 —— 纯函数、零 IO、时钟由调用方传入
#
# `today` 是参数而不是 `date.today()`：过期判断一旦读时钟，"过期条目算没命中"这条就
# 没法写测试了（CLAUDE.md §Python：纯函数优先，好测是硬要求）。
# =============================================================================

# 具体性的三档。数字越大越具体，`_match` 取最大者。
#
# 为什么显式主机 > 通配 > CIDR：前两条是 `PLAN.md` 的原话（「最长后缀优先，显式主机
# 胜过通配」）。第三条是本模块补的判断 —— 主机名是操作者**写下的意图**，CIDR 是对
# 解析结果的兜底描述。同一个目标同时命中"显式写了这个域名"和"它恰好落在某个网段里"
# 时，前者更能代表那份授权是给谁的（label 会进审计）。
_RANK_EXACT_HOST = 2
_RANK_WILDCARD_HOST = 1
_RANK_CIDR = 0


def _host_specificity(pattern: str, host: str) -> tuple[int, int] | None:
    """`(档位, 同档内的长度)`，不命中返回 `None`。`pattern` 已是规范形。"""
    if pattern == host:
        return _RANK_EXACT_HOST, len(pattern)
    if not pattern.startswith("*."):
        return None
    suffix = pattern[2:]
    if not host.endswith(f".{suffix}"):
        return None
    label = host[: -(len(suffix) + 1)]
    if not label or "." in label:
        # 单层通配：`*.example.com` 不命中 `a.b.example.com`，也不命中 `example.com`。
        return None
    return _RANK_WILDCARD_HOST, len(suffix)


def _cidr_specificity(cidrs: Sequence[str], addresses: Sequence[str]) -> tuple[int, int] | None:
    """命中的最长前缀，不命中返回 `None`。

    地址串非法时抛 `ValueError`（来自 `ipaddress`）—— 与 `classify_address` 同一个约定：
    到这一步的地址应当来自解析器，不是输入框，所以那是调用方的 bug。
    """
    best: int | None = None
    for text in cidrs:
        network = ipaddress.ip_network(text)
        for item in addresses:
            address = ipaddress.ip_address(item)
            if address.version != network.version:
                continue
            if address in network:
                best = network.prefixlen if best is None else max(best, network.prefixlen)
    return None if best is None else (_RANK_CIDR, best)


def _match(
    config: AllowlistConfig,
    *,
    host: str,
    addresses: Sequence[str],
    today: date,
) -> AllowlistEntry | None:
    """命中的那一条，没有则 `None`。

    排序键 `(档位, 长度, -文件里的位置)` 取最大：档位与长度就是「最长后缀优先，显式
    主机胜过通配」，位置让平局**稳定**地落在文件里靠前的那一条上。稳定不是洁癖 ——
    `entry_label` 会进审计，一个随机漂移的 label 会让审计记录无法复现。
    """
    best: tuple[tuple[int, int, int], AllowlistEntry] | None = None
    for index, entry in enumerate(config.entries):
        if entry.expires is not None and today > entry.expires:
            # 过期 = 没命中（见 `AllowlistEntry.expires`）。
            continue
        specificity: tuple[int, int] | None = None
        for pattern in entry.hosts:
            hit = _host_specificity(pattern, host)
            if hit is not None and (specificity is None or hit > specificity):
                specificity = hit
        cidr_hit = _cidr_specificity(entry.cidrs, addresses)
        if cidr_hit is not None and (specificity is None or cidr_hit > specificity):
            specificity = cidr_hit
        if specificity is None:
            continue
        key = (specificity[0], specificity[1], -index)
        if best is None or key > best[0]:
            best = (key, entry)
    return None if best is None else best[1]


def _fail_closed() -> AllowlistDecision:
    """冷启动就读不懂文件时的结论：**最严**（见模块 docstring 第 2 条）。

    与 `target_guard.no_allowlist()` 只差一个 `mode`，但那一个字段的差别就是
    "公网随便扫"与"公网一律拒"。刻意不复用它并在这里加一行 `mode=ENFORCE` ——
    那种写法会让下一个读代码的人以为这里和"没有清单"是同一种情形。
    """
    return AllowlistDecision(
        mode=AllowlistMode.ENFORCE,
        matched=False,
        allow_loopback=False,
        allow_private=False,
        allow_reserved=False,
        entry_label=None,
    )


# =============================================================================
# 三、文件状态
# =============================================================================
class AllowlistFileErrorCode(StrEnum):
    """当前文件为什么用不了。

    **不是 `app/errors.py` 里的码**：它出现在 `GET /api/allowlist` 的 200 正文里
    （和 `system_status` 的 blocker 码同一种东西 —— 诊断结果是内容，不是状态码）。
    三个值分得开是因为三种修法完全不同：改权限 / 改 YAML 语法 / 改字段。
    """

    UNREADABLE = "allowlist_unreadable"
    SYNTAX_ERROR = "allowlist_syntax_error"
    INVALID_SHAPE = "allowlist_invalid_shape"


@dataclass(frozen=True, slots=True)
class AllowlistFileError:
    code: AllowlistFileErrorCode
    line: int | None
    """1 起的行号，只有 YAML 语法错才有。"""


@dataclass(frozen=True, slots=True)
class AllowlistSnapshot:
    """某一时刻的清单状态。**这就是"内存里最后一次成功加载的配置"的载体。**

    `config` 与 `error` 可以同时非空 —— 那正是"文件此刻是坏的，但我们还记得上一次好的"
    这个状态（模块 docstring 第 1 条）。`config is None and error is not None` 是冷启动
    就坏（第 2 条）；`config is None and not file_present` 是还没有文件（第 3 条）。
    """

    config: AllowlistConfig | None
    file_present: bool
    error: AllowlistFileError | None
    fingerprint: tuple[int, int] | None
    """`(st_mtime_ns, st_size)`。只给 `AllowlistStore` 自己用，不进 HTTP 正文。"""

    @property
    def stale(self) -> bool:
        """正在用内存里的旧配置（当前文件读不懂）。"""
        return self.error is not None and self.config is not None


_EMPTY_SNAPSHOT = AllowlistSnapshot(config=None, file_present=False, error=None, fingerprint=None)


def _no_config_baseline(snapshot: AllowlistSnapshot) -> AllowlistDecision:
    """没有可用配置时的结论。**模块 docstring 第 2 条与第 3 条的唯一执行点。**

    `decide()` 与 `effective_mode()` 都从这里取，所以"文件缺席 = advisory、文件坏 =
    失败关闭"这条分流在代码里只有一份。写成两份的风险很具体：界面显示 advisory 而
    实际按 enforce 拒绝（或者反过来），而那种不一致没有任何日志会提到。
    """
    return _fail_closed() if snapshot.file_present else no_allowlist()


def effective_mode(snapshot: AllowlistSnapshot) -> AllowlistMode:
    """此刻**真正生效**的模式。给 `GET /api/allowlist` 用。

    不等于 `snapshot.config.mode`：冷启动就解析失败时内存里没有配置，而生效的是
    `enforce`（失败关闭）。界面必须能说出这一点 —— 否则操作者只看到一个错误码，
    不知道现在到底还能不能扫。
    """
    if snapshot.config is not None:
        return snapshot.config.mode
    return _no_config_baseline(snapshot).mode


def decide(
    snapshot: AllowlistSnapshot,
    *,
    host: str,
    addresses: Sequence[str],
    today: date,
) -> AllowlistDecision:
    """把一份快照 + 一个目标变成护栏要的那个扁平结论。纯函数、零 IO。

    `host` 必须是 `NormalizedTarget.host`（已小写、已 IDNA、无方括号）。
    `addresses` 是分类用的那一组地址串（字面 IP 目标要把它自己包含在内 —— 与
    `evaluate_target` 的入参**完全一致**，两处传同一个元组，别各自拼一遍）。

    三种"没有配置"的分流全在这里，见模块 docstring 那三条。
    """
    if snapshot.config is None:
        return _no_config_baseline(snapshot)
    entry = _match(snapshot.config, host=host, addresses=addresses, today=today)
    if entry is None:
        return AllowlistDecision(
            mode=snapshot.config.mode,
            matched=False,
            allow_loopback=False,
            allow_private=False,
            allow_reserved=False,
            entry_label=None,
        )
    return AllowlistDecision(
        mode=snapshot.config.mode,
        matched=True,
        allow_loopback=entry.allow_loopback,
        allow_private=entry.allow_private,
        allow_reserved=entry.allow_reserved,
        entry_label=entry.label,
    )


# =============================================================================
# 四、加载与写回
# =============================================================================
_FILE_HEADER = """\
# Strix 控制台 —— 授权清单（`PLAN.md` §护栏）
#
# ⚠️ 控制台上的每一次编辑都会**整份重写**这个文件，你写在这里的注释会丢。
# `allow_reserved`（运营商/保留地址放行）只能在这里改 —— 界面上刻意没有那一项。
# 改完直接保存即可，后端按 mtime + 大小热重载，不需要重启。
"""


class AllowlistStore:
    """持有"最后一次成功加载的配置"。**一个实例挂在 `app.state.allowlist` 上。**

    为什么是 class 而不是模块级缓存：CLAUDE.md §Python「模块级不得有可变全局状态」——
    那正是 Strix 让我们记住的教训。实例由 lifespan 构造、显式传递，单测各自一个。

    `threading.Lock`：`current()` 是同步阻塞函数，路由层用 `asyncio.to_thread` 调它，
    于是两个并发请求真的会在**两个线程**里同时进来（`Database` 里那把锁同理）。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._snapshot = _EMPTY_SNAPSHOT

    @property
    def path(self) -> Path:
        return self._path

    def current(self) -> AllowlistSnapshot:
        """**同步阻塞**（一次 `stat`，必要时再 `read`）。调用方负责 `to_thread`。"""
        with self._lock:
            self._snapshot = self._reload_locked(self._snapshot)
            return self._snapshot

    def write(self, config: AllowlistConfig) -> AllowlistSnapshot:
        """原子写回文件，并**立刻**把新配置装进内存。**同步阻塞。**

        立刻装进内存而不是等下一次 `current()` 去发现：写完到读到之间不依赖任何指纹
        比较，也就不受"时间戳粒度"那条残余风险影响（模块 docstring 末段）。
        """
        text = _FILE_HEADER + yaml.safe_dump(
            config.model_dump(mode="json", exclude_none=True),
            allow_unicode=True,  # 中文 label 不要变成 \\uXXXX，这个文件是给人读的
            sort_keys=False,  # 字段顺序按模型定义走，比字母序好读
            default_flow_style=False,
        )
        with self._lock:
            _atomic_write(self._path, text)
            stat = os.stat(self._path)
            self._snapshot = AllowlistSnapshot(
                config=config,
                file_present=True,
                error=None,
                fingerprint=(stat.st_mtime_ns, stat.st_size),
            )
            return self._snapshot

    def _reload_locked(self, previous: AllowlistSnapshot) -> AllowlistSnapshot:
        try:
            stat = os.stat(self._path)
        except FileNotFoundError:
            if previous.config is not None:
                # 文件被删掉了。**不保留内存里的配置** —— 删文件是操作者说"我不用这份
                # 清单了"的合法方式，而继续按一份磁盘上已经不存在的配置放行/拒绝，
                # 会让界面显示的内容和实际生效的规则对不上，没法排查。
                # 这不是绕过限制的口子：能删这个文件的人同样能重写它的内容。
                logger.warning(
                    "授权清单文件已消失，改按「没有清单」处理", extra={"path": str(self._path)}
                )
            return _EMPTY_SNAPSHOT
        except OSError as exc:
            # 权限、IO 错误。保留上一次好的配置（模块 docstring 第 1 条）。
            logger.warning("授权清单读不出来", extra={"path": str(self._path), "error": str(exc)})
            return AllowlistSnapshot(
                config=previous.config,
                file_present=True,
                error=AllowlistFileError(code=AllowlistFileErrorCode.UNREADABLE, line=None),
                fingerprint=None,
            )

        fingerprint = (stat.st_mtime_ns, stat.st_size)
        if previous.fingerprint == fingerprint:
            # 快路径。**错误状态也走它** —— 上一次的结论就是这个指纹的结论，
            # 一个没被改过的坏文件不该每个请求都重读一遍。
            return previous

        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("授权清单读不出来", extra={"path": str(self._path), "error": str(exc)})
            return AllowlistSnapshot(
                config=previous.config,
                file_present=True,
                error=AllowlistFileError(code=AllowlistFileErrorCode.UNREADABLE, line=None),
                fingerprint=fingerprint,
            )

        parsed = parse_allowlist_text(text)
        if isinstance(parsed, AllowlistFileError):
            logger.warning(
                "授权清单解析失败，继续使用内存里最后一次成功加载的配置",
                extra={
                    "path": str(self._path),
                    "code": parsed.code.value,
                    "line": parsed.line,
                    "had_previous_config": previous.config is not None,
                },
            )
            return AllowlistSnapshot(
                config=previous.config,
                file_present=True,
                error=parsed,
                fingerprint=fingerprint,
            )
        return AllowlistSnapshot(
            config=parsed, file_present=True, error=None, fingerprint=fingerprint
        )


def parse_allowlist_text(text: str) -> AllowlistConfig | AllowlistFileError:
    """纯函数：一段文本 → 配置或错误。**返回错误而不是抛异常。**

    抛异常会让调用方在 `except` 里判断"这次失败要不要保留旧配置"，而那正是"静默放宽"
    最容易溜进来的地方（写成 `except Exception: return no_allowlist()` 只需要一行）。
    把失败做成一个**返回值**，编译期就要求调用方处理它。
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.MarkedYAMLError as exc:
        line = None if exc.problem_mark is None else exc.problem_mark.line + 1
        return AllowlistFileError(code=AllowlistFileErrorCode.SYNTAX_ERROR, line=line)
    except yaml.YAMLError:
        return AllowlistFileError(code=AllowlistFileErrorCode.SYNTAX_ERROR, line=None)

    if raw is None:
        # 空文件（或只有注释）。文件存在 = 操作者在用这份清单，所以给它默认值
        # （mode=enforce、零条目），也就是"公网一律拒"。见 `AllowlistConfig.mode`。
        raw = {}
    if not isinstance(raw, dict):
        return AllowlistFileError(code=AllowlistFileErrorCode.INVALID_SHAPE, line=None)

    try:
        return AllowlistConfig.model_validate(raw)
    except ValidationError:
        # **不把 ValidationError 的正文带出去。** 它会把出错字段的输入值原样嵌进消息里，
        # 而这条消息的去向包括日志与 HTTP 正文。文件内容不是凭据，但"原样回显未校验的
        # 输入"是一类坑，且那段文本可能很长。操作者要看细节就看这个文件本身。
        return AllowlistFileError(code=AllowlistFileErrorCode.INVALID_SHAPE, line=None)


def _atomic_write(path: Path, text: str) -> None:
    """临时文件 + `os.replace`。形状照抄 `services/auth.py::write_auth_file`。

    为什么必须原子：热重载会在任意时刻读这个文件。直接 `open(path,"w")` 有一个窗口
    ——文件已被截断、新内容还没写完 —— 而那一刻读到的是一份**空的**清单。空清单在
    `enforce` 下是"什么都不准扫"，在别的写法下可能是"什么都准扫"，两种都不是操作者
    按下保存时想要的。`os.replace` 是原子的，读者只会看到旧的或新的完整内容。

    `0o600`：与 `auth.json` 一致。里面没有凭据，但它记着一份内网主机名清单 ——
    对想横向移动的人来说那是现成的侦察结果。api 容器以 root 跑（compose 的
    `user: "0:0"`），所以手工编辑这个文件在 Linux 上需要 sudo；这是同路径挂载下
    `${DATA}` 里每个文件的共同处境，不是本文件特有的。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # 失败时别留下一个半截的 .tmp 文件 —— 它会让下一次写入的 O_EXCL 直接失败。
        os.unlink(tmp_path)
        raise
    os.replace(tmp_path, path)
