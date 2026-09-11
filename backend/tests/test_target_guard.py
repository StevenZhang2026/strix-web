"""目标护栏的全部判据。对应 `PLAN.md` §护栏 与验收 3「护栏矩阵」。

# 为什么整个文件是表驱动的

护栏是一组**判据**，不是一段流程：每一行"这个输入 → 那个类别 + 那个机器码 +
可不可覆盖"都是独立的事实。写成 80 个各自复制粘贴的 `def test_xxx` 之后，加一条黑名单
要新写一个函数、抄一遍断言，而抄错的那一次不会有人看出来。表驱动之后新增一条判据就是
加一行数据，而且整张表可以直接对着 `PLAN.md` §护栏 那张表读。

# 这个文件不碰网络，一次都不碰

`target_guard` 是纯函数（模块 docstring 第一节），所以这里没有 mock、没有 monkeypatch、
没有 fixture。`getaddrinfo` 的结果是参数。这不是"为了测试方便"——反过来才对：正是因为
每一条护栏规则都必须能被断言，那个模块才被设计成不做 IO 的。

# 用例里的地址都是保留段或 IANA 示例地址

`example.com` 及其 A/AAAA（`93.184.216.34` / `2606:2800:220:1:248:1893:25c8:1946`）是 IANA
的示例域名与地址，`192.0.2.0/24` 等是 RFC 5737 文档段。文件里没有任何真实目标、没有任何
真实授权信息 —— 这条规矩比"测试能过"重要：一个渗透测试工具的测试夹具里出现真实主机，
就等于把它写进了公开仓库。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.errors import ALL_ERRORS
from app.services.target_guard import (
    NO_OPT_IN,
    AllowlistDecision,
    AllowlistMode,
    ClassifiedAddress,
    GuardRequirement,
    GuardVerdict,
    NormalizedTarget,
    OperatorOptIn,
    OptInFlag,
    RejectionReason,
    TargetCategory,
    TargetRejected,
    classify_address,
    classify_host,
    evaluate_target,
    no_allowlist,
    normalize_target,
)

# IANA 示例地址。集中在这里，免得每张表各写一遍字面量。
PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


# =============================================================================
# 一、解析规范化 —— 接受
# =============================================================================
@dataclass(frozen=True)
class Accept:
    """一行"这个输入应当规范化成那个样子"。

    只列需要断言的字段，其余留默认值 —— 否则每一行都要写满九个字段，真正在测的那一个
    就埋进样板里了。
    """

    raw: str
    url: str
    host: str
    port: int | None = None
    path: str = ""
    is_ip: bool = False
    punycode_applied: bool = False
    host_unicode: str | None = None  # None = 与 host 相同


ACCEPT_CASES: tuple[Accept, ...] = (
    # ---- 补 scheme -----------------------------------------------------------
    Accept("example.com", "https://example.com", "example.com"),
    Accept("https://example.com", "https://example.com", "example.com"),
    Accept("http://example.com", "http://example.com", "example.com"),
    # scheme 与主机名都要小写。
    Accept("HTTPS://Example.COM/", "https://example.com/", "example.com", path="/"),
    # 首尾空白剥掉（粘贴必然带尾随换行），内部空白另有拒绝用例。
    Accept("  https://example.com \n", "https://example.com", "example.com"),
    # ---- 端口 ---------------------------------------------------------------
    # 没有 scheme 但有端口：`example.com:8080` 会被 urlsplit 当成 scheme=example.com，
    # 所以规范化必须按 `://` 判断有没有 scheme。这一行就是那个坑的回归用例。
    Accept("example.com:8080", "https://example.com:8080", "example.com", port=8080),
    Accept(
        "http://juice-shop:3000",
        "http://juice-shop:3000",
        "juice-shop",
        port=3000,
    ),
    # 默认端口规范化掉：`http://x:80` 与 `http://x` 必须是同一串，否则逐字确认串、
    # 审计记录、白名单条目会出现两种写法。
    Accept("http://example.com:80", "http://example.com", "example.com"),
    Accept("https://example.com:443/", "https://example.com/", "example.com", path="/"),
    # 非默认端口保留。
    Accept(
        "http://example.com:8080/x",
        "http://example.com:8080/x",
        "example.com",
        port=8080,
        path="/x",
    ),
    # ---- IDNA / 同形字 -------------------------------------------------------
    Accept(
        "例子.中国",
        "https://xn--fsqu00a.xn--fiqs8s",
        "xn--fsqu00a.xn--fiqs8s",
        punycode_applied=True,
        host_unicode="例子.中国",
    ),
    # 表意句号 U+3002 也是标签分隔符（encodings.idna 的 Codec.encode 就按它切）。
    # 不处理的话它会变成主机名的一部分，一个同形字域名就能装成子域。
    Accept(
        "https://例子。中国/",
        "https://xn--fsqu00a.xn--fiqs8s/",
        "xn--fsqu00a.xn--fiqs8s",
        path="/",
        punycode_applied=True,
        host_unicode="例子。中国",
    ),
    # 全角字母折叠成 ASCII —— 没有 punycode，但同样是"看到的和连上的不是一回事"，
    # 所以 punycode_applied 也要为真。
    Accept(
        "ＥＸＡＭＰＬＥ.com",
        "https://example.com",
        "example.com",
        punycode_applied=True,
        host_unicode="ｅｘａｍｐｌｅ.com",
    ),
    # 已经是 ACE 形式：原样通过，且**不**标记（没有发生折叠）。
    Accept(
        "xn--fsqu00a.xn--fiqs8s",
        "https://xn--fsqu00a.xn--fiqs8s",
        "xn--fsqu00a.xn--fiqs8s",
    ),
    Accept(
        "münchen.de",
        "https://xn--mnchen-3ya.de",
        "xn--mnchen-3ya.de",
        punycode_applied=True,
        host_unicode="münchen.de",
    ),
    # 根域尾点剥掉。留着 encodings.idna 会原样保留它（已实测），于是逐字确认串会多
    # 一个字符。
    Accept("example.com.", "https://example.com", "example.com"),
    # ---- 字面 IP ------------------------------------------------------------
    Accept("127.0.0.1", "https://127.0.0.1", "127.0.0.1", is_ip=True),
    Accept("https://10.20.1.5", "https://10.20.1.5", "10.20.1.5", is_ip=True),
    Accept(
        "http://localhost:13000",
        "http://localhost:13000",
        "localhost",
        port=13000,
    ),
    # IPv6：host 不带方括号，url 带；大写压缩成小写。
    Accept(
        "https://[FD00:EC2::254]:8443/",
        "https://[fd00:ec2::254]:8443/",
        "fd00:ec2::254",
        port=8443,
        path="/",
        is_ip=True,
    ),
    Accept("https://[::1]/", "https://[::1]/", "::1", path="/", is_ip=True),
    # ---- 路径 ---------------------------------------------------------------
    Accept(
        "https://sub.example.com/a/b",
        "https://sub.example.com/a/b",
        "sub.example.com",
        path="/a/b",
    ),
    # `.git` 的拒绝规则不许过度匹配：`.github` 不是 git 仓库路径。
    Accept(
        "https://example.com/repo.github",
        "https://example.com/repo.github",
        "example.com",
        path="/repo.github",
    ),
)


@pytest.mark.parametrize("case", ACCEPT_CASES, ids=lambda c: c.raw)
def test_normalize_accepts(case: Accept) -> None:
    target = normalize_target(case.raw)
    assert target.url == case.url
    assert target.host == case.host
    assert target.port == case.port
    assert target.path == case.path
    assert target.is_ip == case.is_ip
    assert target.punycode_applied == case.punycode_applied
    assert target.host_unicode == (case.host_unicode or case.host)
    # raw 只剥首尾空白，不做别的加工 —— 审计里要能看到用户到底打了什么。
    assert target.raw == case.raw.strip()


def test_normalize_is_idempotent() -> None:
    """`url` 必须是不动点，且由它重新规范化出的目标标识与第一遍一致。

    为什么这条重要：授权声明（T12）会把 `url` 存进 `authorizations`，启动前再规范化
    一次做比对。不幂等的话第二次会得到不同的串，于是每一次扫描都被自己拦下。

    **三个字段刻意不参与比较**，因为它们描述的是"用户当时打了什么"，不是目标本身：
      · `raw` —— 第二遍的输入本来就是第一遍的输出；
      · `host_unicode` / `punycode_applied` —— 把 ACE 形式（`xn--…`）再喂一遍，
        它当然没发生折叠。这是正确行为：那两个字段是给 UI 提示"你输入的和实际连接的
        不是一回事"用的，而 `url` 里已经没有可折叠的东西了。
    """
    provenance = {"raw", "host_unicode", "punycode_applied"}
    for case in ACCEPT_CASES:
        once = normalize_target(case.raw)
        twice = normalize_target(once.url)
        assert twice.url == once.url, f"{case.raw!r} 的 url 不是不动点"
        for field in NormalizedTarget.__dataclass_fields__:
            if field in provenance:
                continue
            assert getattr(twice, field) == getattr(once, field), (
                f"{case.raw!r} 规范化不幂等：{field}"
            )


# =============================================================================
# 二、解析规范化 —— 拒绝
# =============================================================================
REJECT_CASES: tuple[tuple[str, RejectionReason], ...] = (
    ("", RejectionReason.EMPTY_TARGET),
    ("   ", RejectionReason.EMPTY_TARGET),
    ("\t\n", RejectionReason.EMPTY_TARGET),
    # ---- 空白 / shell 元字符 / 不可打印 --------------------------------------
    ("exa mple.com", RejectionReason.FORBIDDEN_CHARACTERS),
    ("https://example.com/a b", RejectionReason.FORBIDDEN_CHARACTERS),
    ("example.com;ls", RejectionReason.FORBIDDEN_CHARACTERS),
    ("example.com`id`", RejectionReason.FORBIDDEN_CHARACTERS),
    ("example.com$(id)", RejectionReason.FORBIDDEN_CHARACTERS),
    ("example.com${HOME}", RejectionReason.FORBIDDEN_CHARACTERS),
    ("example.com|id", RejectionReason.FORBIDDEN_CHARACTERS),
    ("example.com&&id", RejectionReason.FORBIDDEN_CHARACTERS),
    ("example.com>out", RejectionReason.FORBIDDEN_CHARACTERS),
    ("https://例子.中国\\", RejectionReason.FORBIDDEN_CHARACTERS),
    # 不可打印字符一律写成 `\uXXXX` / `\x00` 转义，**绝不贴字面量**：贴进来之后
    # 这个文件在 grep 眼里就是二进制（ugrep 直接一个匹配都不输出，已实测），
    # 而 review 的人也根本看不到它。
    #
    # 零宽空格**必须在这里拦住**：`encode("idna")` 会把它静默吃掉（已实测
    # `"example.com" + U+200B` → `"example.com"`），于是 UI 上显示的原文与实际
    # 连接的主机不一致，而且不留任何痕迹。
    ("example.com\u200b", RejectionReason.FORBIDDEN_CHARACTERS),
    ("exa\x00mple.com", RejectionReason.FORBIDDEN_CHARACTERS),
    # 从右向左覆盖 —— 让 UI 上的域名倒着显示。
    ("https://example.com\u202e", RejectionReason.FORBIDDEN_CHARACTERS),
    # ---- scheme -------------------------------------------------------------
    ("ftp://example.com", RejectionReason.UNSUPPORTED_SCHEME),
    ("file:///etc/passwd", RejectionReason.UNSUPPORTED_SCHEME),
    # git:// 会让 Strix 的 infer_target_type 走 repository 分支。
    ("git://example.com/x", RejectionReason.UNSUPPORTED_SCHEME),
    ("://example.com", RejectionReason.UNSUPPORTED_SCHEME),
    # ---- user:pass@ ——「否则会被 Strix 当成仓库」（验收 3 点名的那一行）--------
    ("https://admin:pw@example.com", RejectionReason.CREDENTIALS_IN_URL),
    ("http://admin:pw@example.com/", RejectionReason.CREDENTIALS_IN_URL),
    ("https://admin@example.com", RejectionReason.CREDENTIALS_IN_URL),
    # 两个 @：urlsplit 只认最后一个，所以要按 netloc 里有没有 @ 判。
    ("https://a@b@example.com", RejectionReason.CREDENTIALS_IN_URL),
    # 没写 scheme 也要拦。
    ("admin:pw@example.com", RejectionReason.CREDENTIALS_IN_URL),
    # ---- .git ——「_is_http_git_repo 会发真实网络请求」------------------------
    ("https://example.com/repo.git", RejectionReason.GIT_REPOSITORY_PATH),
    ("https://example.com/a/repo.GIT/", RejectionReason.GIT_REPOSITORY_PATH),
    ("example.com/x.git", RejectionReason.GIT_REPOSITORY_PATH),
    # ---- 端口 ---------------------------------------------------------------
    ("https://example.com:99999/", RejectionReason.INVALID_PORT),
    ("https://example.com:abc/", RejectionReason.INVALID_PORT),
    ("https://example.com:-1/", RejectionReason.INVALID_PORT),
    # ---- 主机名 -------------------------------------------------------------
    ("https://", RejectionReason.INVALID_HOST),
    ("https://:8080/", RejectionReason.INVALID_HOST),
    ("/etc/passwd", RejectionReason.INVALID_HOST),
    ("//example.com", RejectionReason.INVALID_HOST),
    # IDNA 对纯 ASCII 标签**完全不校验**（已实测），下面这三行只能靠我们自己的
    # RFC 1123 检查拦下。
    ("https://exa_mple.com", RejectionReason.INVALID_HOST),
    ("https://-bad.com", RejectionReason.INVALID_HOST),
    ("https://bad-.com", RejectionReason.INVALID_HOST),
    ("https://a..b", RejectionReason.INVALID_HOST),
    ("https://" + "a" * 64 + ".com", RejectionReason.INVALID_HOST),
    # IPv4 简写：ipaddress 不认（已实测），getaddrinfo 认。当成域名放过去会让 UI
    # 把一个 IP 显示成域名。
    ("127.1", RejectionReason.INVALID_HOST),
    ("2130706433", RejectionReason.INVALID_HOST),
    ("10.0.1", RejectionReason.INVALID_HOST),
    # IPv6 zone id：只对本机某张网卡有意义，扫描跑在兄弟容器里必然测不到。
    ("https://[fe80::1%25eth0]/", RejectionReason.INVALID_HOST),
    # 方括号不配对，urlsplit 自己抛 ValueError。
    ("https://[fd00::1/", RejectionReason.INVALID_HOST),
    # ---- 查询串与片段 -------------------------------------------------------
    ("https://example.com?a=1", RejectionReason.QUERY_OR_FRAGMENT_NOT_ALLOWED),
    ("https://example.com/#frag", RejectionReason.QUERY_OR_FRAGMENT_NOT_ALLOWED),
)


@pytest.mark.parametrize(("raw", "reason"), REJECT_CASES, ids=lambda v: repr(v))
def test_normalize_rejects(raw: str, reason: RejectionReason) -> None:
    with pytest.raises(TargetRejected) as excinfo:
        normalize_target(raw)
    assert excinfo.value.reason is reason
    # 原文只在 .raw 上，不在 str(exc) 里 —— 未经校验的输入不该原样进日志。
    assert excinfo.value.raw == raw
    assert str(excinfo.value) == reason.value


def test_rejection_message_never_echoes_input() -> None:
    """`str(exc)` 里不许出现用户输入的任何片段。

    单独一条而不是并进上面：上面那条断言的是"等于 reason.value"，这条断言的是
    "不含输入" —— 前者哪天被改成 f-string 加点上下文，后者才是真正会响的那个。
    """
    payload = "example.com;rm-rf"
    with pytest.raises(TargetRejected) as excinfo:
        normalize_target(payload)
    assert "example.com" not in str(excinfo.value)
    assert "rm-rf" not in str(excinfo.value)


# =============================================================================
# 三、地址分类
#
# 每一行：地址串 → (类别, 命中规则, 规范化后的地址, 内层 IPv4)。
# 顺序按类别分组，组内按 IPv4 / IPv6。
# =============================================================================
@dataclass(frozen=True)
class AddrCase:
    text: str
    category: TargetCategory
    rule: str
    address: str | None = None  # None = 与 text 相同
    embedded_ipv4: str | None = None


ADDRESS_CASES: tuple[AddrCase, ...] = (
    # ---- metadata：永久硬拦 -------------------------------------------------
    AddrCase("169.254.169.254", TargetCategory.METADATA, "imds_v4"),
    AddrCase("169.254.170.2", TargetCategory.METADATA, "aws_ecs_task_metadata"),
    AddrCase("100.100.100.200", TargetCategory.METADATA, "alibaba_metadata"),
    AddrCase("169.254.0.23", TargetCategory.METADATA, "tencent_metadata"),
    AddrCase("192.0.0.192", TargetCategory.METADATA, "oracle_classic_metadata"),
    # fd00:ec2::254 落在 fc00::/7 里 —— 不查精确值就会被归成 private（勾一下就能扫）。
    AddrCase("fd00:ec2::254", TargetCategory.METADATA, "aws_imds_v6"),
    AddrCase("fe80::a9fe:a9fe", TargetCategory.METADATA, "imds_v6_link_local"),
    # ---- metadata：IPv6 外衣包 IPv4 的三种绕过形状 ---------------------------
    AddrCase(
        "::ffff:169.254.169.254",
        TargetCategory.METADATA,
        "imds_v4",
        embedded_ipv4="169.254.169.254",
    ),
    AddrCase(
        "2002:a9fe:a9fe::",
        TargetCategory.METADATA,
        "imds_v4",
        embedded_ipv4="169.254.169.254",
    ),
    # NAT64 众所周知前缀。ipaddress 说它 is_global == True（已实测），
    # 所以少了这一条它会被当成公网目标。
    AddrCase(
        "64:ff9b::a9fe:a9fe",
        TargetCategory.METADATA,
        "imds_v4",
        embedded_ipv4="169.254.169.254",
    ),
    # ---- loopback -----------------------------------------------------------
    AddrCase("127.0.0.1", TargetCategory.LOOPBACK, "loopback"),
    AddrCase("127.5.6.7", TargetCategory.LOOPBACK, "loopback"),
    AddrCase("::1", TargetCategory.LOOPBACK, "loopback"),
    AddrCase(
        "::ffff:127.0.0.1",
        TargetCategory.LOOPBACK,
        "loopback",
        embedded_ipv4="127.0.0.1",
    ),
    # ---- private ------------------------------------------------------------
    AddrCase("10.20.1.5", TargetCategory.PRIVATE, "rfc1918"),
    AddrCase("172.16.0.1", TargetCategory.PRIVATE, "rfc1918"),
    AddrCase("172.31.255.255", TargetCategory.PRIVATE, "rfc1918"),
    AddrCase("192.168.1.1", TargetCategory.PRIVATE, "rfc1918"),
    AddrCase("fc00::1", TargetCategory.PRIVATE, "ipv6_ula"),
    AddrCase("fd12:3456::1", TargetCategory.PRIVATE, "ipv6_ula"),
    AddrCase(
        "::ffff:10.0.0.1",
        TargetCategory.PRIVATE,
        "rfc1918",
        embedded_ipv4="10.0.0.1",
    ),
    # 172.16/12 的两侧边界都是公网 —— 掩码写错成 /16 或 /8 会被这两行抓住。
    AddrCase("172.15.255.255", TargetCategory.PUBLIC, "global_unicast"),
    AddrCase("172.32.0.1", TargetCategory.PUBLIC, "global_unicast"),
    # ---- carrier / reserved -------------------------------------------------
    AddrCase("100.64.0.1", TargetCategory.CARRIER_RESERVED, "cgnat"),
    AddrCase("192.0.2.5", TargetCategory.CARRIER_RESERVED, "documentation"),
    AddrCase("198.51.100.7", TargetCategory.CARRIER_RESERVED, "documentation"),
    AddrCase("203.0.113.9", TargetCategory.CARRIER_RESERVED, "documentation"),
    AddrCase("198.18.0.1", TargetCategory.CARRIER_RESERVED, "benchmark"),
    AddrCase("0.0.0.0", TargetCategory.CARRIER_RESERVED, "unspecified"),  # noqa: S104
    AddrCase("255.255.255.255", TargetCategory.CARRIER_RESERVED, "broadcast"),
    # 组播必须在 is_global 兜底之前查掉：224.0.0.1 与 ff02::1 的 is_global 都是 True。
    AddrCase("224.0.0.1", TargetCategory.CARRIER_RESERVED, "multicast"),
    AddrCase("ff02::1", TargetCategory.CARRIER_RESERVED, "multicast"),
    AddrCase("240.0.0.1", TargetCategory.CARRIER_RESERVED, "reserved"),
    AddrCase("169.254.1.1", TargetCategory.CARRIER_RESERVED, "link_local"),
    AddrCase("2001:db8::1", TargetCategory.CARRIER_RESERVED, "documentation"),
    AddrCase("::", TargetCategory.CARRIER_RESERVED, "unspecified"),
    AddrCase("fe80::1", TargetCategory.CARRIER_RESERVED, "link_local"),
    AddrCase("100::1", TargetCategory.CARRIER_RESERVED, "discard"),
    # zone id：解析器真的会返回它。分类前去掉，且输出的 address 也不带。
    AddrCase("fe80::1%eth0", TargetCategory.CARRIER_RESERVED, "link_local", address="fe80::1"),
    # teredo：取内层客户端地址（RFC 5737 文档段）。
    AddrCase(
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",
        TargetCategory.CARRIER_RESERVED,
        "documentation",
        embedded_ipv4="192.0.2.45",
    ),
    # ---- public -------------------------------------------------------------
    AddrCase(PUBLIC_V4, TargetCategory.PUBLIC, "global_unicast"),
    AddrCase(PUBLIC_V6, TargetCategory.PUBLIC, "global_unicast"),
    AddrCase(
        f"::ffff:{PUBLIC_V4}",
        TargetCategory.PUBLIC,
        "global_unicast",
        embedded_ipv4=PUBLIC_V4,
    ),
)


@pytest.mark.parametrize("case", ADDRESS_CASES, ids=lambda c: c.text)
def test_classify_address(case: AddrCase) -> None:
    result = classify_address(case.text)
    assert result.category is case.category
    assert result.rule == case.rule
    assert result.address == (case.address or case.text)
    assert result.embedded_ipv4 == case.embedded_ipv4
    assert result.version == (6 if ":" in result.address else 4)


def test_classify_address_never_returns_mixed() -> None:
    """`MIXED` 是汇总层的类别，单个地址永远不该拿到它。

    这条是 `TargetCategory` 那段注释的执行点：把"不会返回"从一句话变成一个断言。
    """
    for case in ADDRESS_CASES:
        assert classify_address(case.text).category is not TargetCategory.MIXED


def test_classify_address_rejects_non_addresses() -> None:
    """不是地址就抛 `ValueError` —— 那是调用方的 bug，不是用户输入问题。"""
    for junk in ("example.com", "127.1", "", "not an ip"):
        with pytest.raises(ValueError, match="does not appear to be"):
            classify_address(junk)


# =============================================================================
# 四、主机名分类
# =============================================================================
HOST_CASES: tuple[tuple[str, TargetCategory | None, str], ...] = (
    ("metadata.google.internal", TargetCategory.METADATA, "gcp_metadata_host"),
    ("metadata", TargetCategory.METADATA, "gcp_metadata_short_host"),
    ("instance-data", TargetCategory.METADATA, "aws_legacy_metadata_host"),
    ("instance-data.ec2.internal", TargetCategory.METADATA, "aws_legacy_metadata_host"),
    ("metadata.tencentyun.com", TargetCategory.METADATA, "tencent_metadata_host"),
    ("localhost", TargetCategory.LOOPBACK, "loopback_host"),
    ("foo.localhost", TargetCategory.LOOPBACK, "loopback_host"),
    ("printer.local", TargetCategory.PRIVATE, "private_suffix"),
    ("gitlab.internal", TargetCategory.PRIVATE, "private_suffix"),
    ("nas.lan", TargetCategory.PRIVATE, "private_suffix"),
    # 单标签主机名 = docker 网络里的服务别名。CLAUDE.md 规定的靶机目标就是这个形状。
    ("juice-shop", TargetCategory.PRIVATE, "single_label_host"),
    ("gitlab", TargetCategory.PRIVATE, "single_label_host"),
    # 定不了类别的交给地址分类。
    ("example.com", None, ""),
    ("sub.example.com", None, ""),
    ("xn--fsqu00a.xn--fiqs8s", None, ""),
    # 后缀规则不能误伤把这些词当成标签内容的名字 —— `.internal` / `.localhost` 是
    # **后缀**，不是子串。
    ("internal.example.com", None, ""),
    ("localhost.example.com", None, ""),
    ("notlocalhost.example.com", None, ""),
    # 但裸的 `notlocalhost` 仍然是单标签 → 内网。单标签规则不看这个词是什么，
    # 它看的是"没有点"这件事：在 docker 网络里那就是一个服务别名。
    ("notlocalhost", TargetCategory.PRIVATE, "single_label_host"),
)


@pytest.mark.parametrize(("host", "category", "rule"), HOST_CASES, ids=lambda v: str(v))
def test_classify_host(host: str, category: TargetCategory | None, rule: str) -> None:
    result = classify_host(host)
    if category is None:
        assert result is None
        return
    assert result is not None
    assert result == (category, rule)


# =============================================================================
# 五、策略矩阵
# =============================================================================
def _allowlist(
    *,
    mode: AllowlistMode = AllowlistMode.ADVISORY,
    matched: bool = False,
    loopback: bool = False,
    private: bool = False,
    reserved: bool = False,
) -> AllowlistDecision:
    return AllowlistDecision(
        mode=mode,
        matched=matched,
        allow_loopback=loopback,
        allow_private=private,
        allow_reserved=reserved,
        entry_label="测试条目" if matched else None,
    )


# "什么都放行"：白名单给足、两项都勾上。metadata 与 mixed 用它来证明**不可覆盖**。
EVERYTHING_ALLOWED = _allowlist(
    mode=AllowlistMode.ENFORCE,
    matched=True,
    loopback=True,
    private=True,
    reserved=True,
)
BOTH_OPT_INS = OperatorOptIn(loopback=True, private=True)


def _verdict(
    raw: str,
    resolved: tuple[str, ...] = (),
    *,
    allowlist: AllowlistDecision | None = None,
    opt_in: OperatorOptIn = NO_OPT_IN,
) -> GuardVerdict:
    return evaluate_target(
        normalize_target(raw),
        resolved,
        allowlist=allowlist if allowlist is not None else no_allowlist(),
        opt_in=opt_in,
    )


# ---- metadata：永久硬拦、不可覆盖 -------------------------------------------
def test_metadata_literal_ip_is_blocked() -> None:
    verdict = _verdict("http://169.254.169.254/")
    assert verdict.category is TargetCategory.METADATA
    assert verdict.allowed is False
    assert verdict.overridable is False
    assert verdict.requirement is GuardRequirement.IMPOSSIBLE
    assert verdict.error_code == "blocked_metadata"
    assert verdict.required_opt_in == ()


def test_metadata_hostname_is_blocked_even_when_it_resolves_public() -> None:
    """主机名命中元数据名单时，解析结果全是公网也照样拦。

    这条覆盖的是"攻击者把 metadata.google.internal 指到自己的公网 IP"以外的另一面：
    我们不需要知道它解析到哪，这个名字本身在云上就是元数据入口。
    """
    verdict = _verdict("http://metadata.google.internal/", (PUBLIC_V4,))
    assert verdict.category is TargetCategory.METADATA
    assert verdict.error_code == "blocked_metadata"
    assert verdict.overridable is False


@pytest.mark.parametrize(
    "raw",
    [
        "http://169.254.169.254/",
        "http://100.100.100.200/",
        "http://[fd00:ec2::254]/",
        "http://metadata.google.internal/",
    ],
)
def test_metadata_cannot_be_overridden_by_anything(raw: str) -> None:
    """**白名单给足 + 两项都勾上，仍然拦。**

    这是 CLAUDE.md §安全不变式「云元数据地址永久硬拦、不可覆盖」与验收 3
    `overridable:false` 的执行点。任何一次"让白名单能放行元数据"的改动都会让这条红。
    """
    verdict = _verdict(raw, (PUBLIC_V4,), allowlist=EVERYTHING_ALLOWED, opt_in=BOTH_OPT_INS)
    assert verdict.category is TargetCategory.METADATA
    assert verdict.allowed is False
    assert verdict.overridable is False
    assert verdict.error_code == "blocked_metadata"


def test_metadata_via_ipv4_mapped_resolution_is_blocked() -> None:
    """解析结果是 IPv4-mapped 形式的元数据地址 —— 一个普通名字也能指到那里。"""
    verdict = _verdict("https://example.com", ("::ffff:169.254.169.254",))
    assert verdict.category is TargetCategory.METADATA
    assert verdict.error_code == "blocked_metadata"


def test_metadata_wins_over_mixed() -> None:
    """同时解析到公网和元数据地址时报 metadata，不报 split_horizon。

    两个码的处置完全不同：`split_horizon` 让人"改成填字面 IP"，而那正是攻击者
    想要的下一步。元数据必须赢。
    """
    verdict = _verdict("https://example.com", (PUBLIC_V4, "169.254.169.254"))
    assert verdict.category is TargetCategory.METADATA
    assert verdict.error_code == "blocked_metadata"


# ---- loopback：默认拦，勾选可放行，必须带 note_zh ----------------------------
def test_loopback_needs_opt_in_and_carries_note() -> None:
    """验收 3：`http://localhost:13000` → 需 loopback 放行 + `note_zh`。"""
    verdict = _verdict("http://localhost:13000", ("127.0.0.1",))
    assert verdict.category is TargetCategory.LOOPBACK
    assert verdict.allowed is False
    assert verdict.overridable is True
    assert verdict.requirement is GuardRequirement.OPERATOR_OPT_IN
    assert verdict.required_opt_in == (OptInFlag.LOOPBACK,)
    # 缺勾选不是错误，是向导还没走完。
    assert verdict.error_code is None
    assert verdict.note_zh is not None
    # 说明里必须点出改写目标，否则用户不知道扫的是自己这台机器。
    assert "host.docker.internal" in verdict.note_zh


def test_loopback_allowed_after_opt_in() -> None:
    verdict = _verdict(
        "http://localhost:13000",
        ("127.0.0.1",),
        opt_in=OperatorOptIn(loopback=True),
    )
    assert verdict.allowed is True
    assert verdict.required_opt_in == ()
    assert verdict.error_code is None
    # 放行之后说明照旧要给 —— 这时候它才真的有用。
    assert verdict.note_zh is not None


def test_loopback_allowed_by_allowlist_file() -> None:
    """`allow_loopback: true` 与操作者勾选是**或**的关系（验收 6 就靠这条起扫描）。"""
    verdict = _verdict(
        "http://localhost:13000", ("127.0.0.1",), allowlist=_allowlist(loopback=True)
    )
    assert verdict.allowed is True


def test_loopback_ipv6_literal() -> None:
    verdict = _verdict("http://[::1]:13000/")
    assert verdict.category is TargetCategory.LOOPBACK
    assert verdict.required_opt_in == (OptInFlag.LOOPBACK,)


# ---- private：可覆盖，这是常见的合法场景 ------------------------------------
def test_private_needs_opt_in() -> None:
    """验收 3：`10.20.1.5` → 需内网放行。"""
    verdict = _verdict("http://10.20.1.5/")
    assert verdict.category is TargetCategory.PRIVATE
    assert verdict.allowed is False
    assert verdict.overridable is True
    assert verdict.requirement is GuardRequirement.OPERATOR_OPT_IN
    assert verdict.required_opt_in == (OptInFlag.PRIVATE,)
    assert verdict.error_code is None
    assert verdict.note_zh is None


def test_private_allowed_after_opt_in() -> None:
    verdict = _verdict("http://10.20.1.5/", opt_in=OperatorOptIn(private=True))
    assert verdict.allowed is True
    assert verdict.required_opt_in == ()


def test_private_allowed_by_allowlist_file() -> None:
    verdict = _verdict("http://10.20.1.5/", allowlist=_allowlist(private=True))
    assert verdict.allowed is True


def test_private_opt_in_does_not_leak_into_loopback() -> None:
    """勾了"内网确认"不等于放行环回。两个勾选各管一件事。"""
    verdict = _verdict("http://127.0.0.1/", opt_in=OperatorOptIn(private=True))
    assert verdict.allowed is False
    assert verdict.required_opt_in == (OptInFlag.LOOPBACK,)


def test_docker_alias_target_is_private() -> None:
    """CLAUDE.md 规定的靶机目标 `http://juice-shop:3000`：单标签名 → 内网。"""
    verdict = _verdict("http://juice-shop:3000", ("172.18.0.5",))
    assert verdict.category is TargetCategory.PRIVATE
    assert verdict.required_opt_in == (OptInFlag.PRIVATE,)


def test_private_suffix_host_needs_opt_in_even_when_unresolvable_shape() -> None:
    """`.internal` 结尾但解析到内网地址 —— 主机名与地址两层结论一致。"""
    verdict = _verdict("https://gitlab.internal", ("10.0.0.9",))
    assert verdict.category is TargetCategory.PRIVATE
    assert verdict.required_opt_in == (OptInFlag.PRIVATE,)


def test_both_opt_ins_required_when_host_resolves_to_loopback_and_private() -> None:
    """一个名字同时解析到环回和内网 → 两项勾选都要。

    这就是 `required_opt_in` 存在的理由：从汇总类别只能推出一项。
    """
    verdict = _verdict("https://example.com", ("127.0.0.1", "10.20.1.5"))
    assert set(verdict.required_opt_in) == {OptInFlag.LOOPBACK, OptInFlag.PRIVATE}
    assert verdict.allowed is False
    verdict_ok = _verdict("https://example.com", ("127.0.0.1", "10.20.1.5"), opt_in=BOTH_OPT_INS)
    assert verdict_ok.allowed is True


# ---- carrier / reserved：拦，只能从白名单文件放行，UI 不给 -------------------
def test_reserved_is_file_only() -> None:
    verdict = _verdict("http://192.0.2.5/")
    assert verdict.category is TargetCategory.CARRIER_RESERVED
    assert verdict.allowed is False
    assert verdict.overridable is False  # UI 上没有这个入口
    assert verdict.requirement is GuardRequirement.ALLOWLIST_FILE_ONLY
    assert verdict.error_code == "not_in_allowlist"


def test_reserved_allowed_only_by_file() -> None:
    assert _verdict("http://192.0.2.5/", allowlist=_allowlist(reserved=True)).allowed is True
    # 勾选给不了它 —— 这是"UI 不给"那句话的执行点。
    assert _verdict("http://192.0.2.5/", opt_in=BOTH_OPT_INS).allowed is False


def test_cgnat_is_reserved() -> None:
    verdict = _verdict("http://100.64.0.1/")
    assert verdict.category is TargetCategory.CARRIER_RESERVED
    assert verdict.requirement is GuardRequirement.ALLOWLIST_FILE_ONLY


# ---- public：enforce 下必须命中白名单 ---------------------------------------
def test_public_allowed_in_advisory_mode() -> None:
    verdict = _verdict("https://example.com", (PUBLIC_V4,))
    assert verdict.category is TargetCategory.PUBLIC
    assert verdict.allowed is True
    assert verdict.requirement is GuardRequirement.NONE
    assert verdict.overridable is False  # advisory 模式下没有什么要覆盖的
    assert verdict.error_code is None


def test_public_blocked_in_enforce_mode_without_match() -> None:
    verdict = _verdict(
        "https://example.com",
        (PUBLIC_V4,),
        allowlist=_allowlist(mode=AllowlistMode.ENFORCE),
    )
    assert verdict.allowed is False
    assert verdict.requirement is GuardRequirement.ALLOWLIST_ENTRY
    assert verdict.overridable is True  # UI 有"一键加入授权清单"
    assert verdict.error_code == "not_in_allowlist"


def test_public_allowed_in_enforce_mode_when_matched() -> None:
    verdict = _verdict(
        "https://example.com",
        (PUBLIC_V4,),
        allowlist=_allowlist(mode=AllowlistMode.ENFORCE, matched=True),
    )
    assert verdict.allowed is True
    assert verdict.error_code is None


def test_public_ipv6_only_resolution() -> None:
    verdict = _verdict("https://example.com", (PUBLIC_V6,))
    assert verdict.category is TargetCategory.PUBLIC
    assert verdict.allowed is True


def test_punycode_target_still_classified_by_addresses() -> None:
    """验收 3：`例子.中国` → punycode 且标记。分类仍然按解析结果做。"""
    verdict = _verdict("例子.中国", (PUBLIC_V4,))
    assert verdict.target.host == "xn--fsqu00a.xn--fiqs8s"
    assert verdict.target.host_unicode == "例子.中国"
    assert verdict.target.punycode_applied is True
    assert verdict.category is TargetCategory.PUBLIC


# ---- mixed：split_horizon ---------------------------------------------------
def test_split_horizon_public_plus_private() -> None:
    """验收 3：同时解析到公网与内网的主机名 → `split_horizon`。"""
    verdict = _verdict("https://example.com", (PUBLIC_V4, "10.20.1.5"))
    assert verdict.category is TargetCategory.MIXED
    assert verdict.allowed is False
    assert verdict.overridable is False
    assert verdict.requirement is GuardRequirement.IMPOSSIBLE
    assert verdict.error_code == "split_horizon"
    # 硬拦时不给"还缺哪个勾选" —— 勾完也不会放行。
    assert verdict.required_opt_in == ()


def test_split_horizon_cannot_be_overridden() -> None:
    verdict = _verdict(
        "https://example.com",
        (PUBLIC_V4, "10.20.1.5"),
        allowlist=EVERYTHING_ALLOWED,
        opt_in=BOTH_OPT_INS,
    )
    assert verdict.category is TargetCategory.MIXED
    assert verdict.allowed is False


def test_split_horizon_public_plus_loopback_has_no_note() -> None:
    """环回参与了 split-horizon 时不给环回说明 —— 这次根本不会扫。"""
    verdict = _verdict("https://example.com", (PUBLIC_V4, "127.0.0.1"))
    assert verdict.category is TargetCategory.MIXED
    assert verdict.note_zh is None


def test_split_horizon_public_plus_reserved() -> None:
    """公网 + 运营商保留段也是 split-horizon：形状与公网 + 内网完全一样。"""
    verdict = _verdict("https://example.com", (PUBLIC_V4, "100.64.0.1"))
    assert verdict.category is TargetCategory.MIXED
    assert verdict.error_code == "split_horizon"


def test_private_suffix_host_resolving_public_is_split_horizon() -> None:
    """名字说内网、地址说公网 —— 主机名分类与地址分类不一致同样是 split-horizon。"""
    verdict = _verdict("https://gitlab.internal", (PUBLIC_V4,))
    assert verdict.category is TargetCategory.MIXED
    assert verdict.error_code == "split_horizon"


def test_two_public_addresses_are_not_mixed() -> None:
    """一个名字解析到两个公网地址是常态，不是 split-horizon。"""
    verdict = _verdict("https://example.com", (PUBLIC_V4, PUBLIC_V6))
    assert verdict.category is TargetCategory.PUBLIC
    assert verdict.allowed is True


def test_two_private_addresses_are_not_mixed() -> None:
    verdict = _verdict("https://example.com", ("10.0.0.1", "192.168.1.1"))
    assert verdict.category is TargetCategory.PRIVATE
    assert verdict.required_opt_in == (OptInFlag.PRIVATE,)


# ---- 解析结果的传入约定 -----------------------------------------------------
def test_literal_ip_needs_no_resolution() -> None:
    verdict = _verdict("http://10.20.1.5/")
    assert [item.address for item in verdict.addresses] == ["10.20.1.5"]


def test_literal_ip_is_always_classified_even_if_caller_passes_other_addresses() -> None:
    """字面 IP 目标：调用方传进来的地址只能**追加**，不能取代那个字面量。

    这条挡住的是一种很安静的削弱：调用方（T12）如果因为 bug 把别的地址填进
    `resolved_addresses`，护栏的结论就会变成那个地址的结论。字面量必须永远在里面。
    """
    verdict = _verdict("http://169.254.169.254/", (PUBLIC_V4,))
    assert [item.address for item in verdict.addresses] == ["169.254.169.254", PUBLIC_V4]
    assert verdict.category is TargetCategory.METADATA


def test_hostname_without_resolution_is_a_programming_error() -> None:
    """主机名 + 空解析结果 → `ValueError`。

    **绝不能**静默当成"没有地址所以放行" —— 那会是本模块最糟糕的失败模式：
    DNS 抖一下，护栏就全线消失。
    """
    with pytest.raises(ValueError, match="没有传入解析结果"):
        evaluate_target(normalize_target("https://example.com"), (), allowlist=no_allowlist())


def test_address_order_is_preserved() -> None:
    """审计要可复现：`addresses` 的顺序与传入顺序一致，不排序、不去重。"""
    resolved = ("10.0.0.2", "10.0.0.1", "10.0.0.2")
    verdict = _verdict("https://example.com", resolved)
    assert tuple(item.address for item in verdict.addresses) == resolved


# =============================================================================
# 六、跨用例不变式
#
# 这几条不针对某一行输入，而是断言"整张表都不许违反"的性质。它们是最容易在重构里
# 被静默破坏的部分 —— 单条用例改一行就过了，性质不会。
# =============================================================================
_REGISTERED_CODES = frozenset(error.code for error in ALL_ERRORS)

# 把上面所有 evaluate_target 的入参组合收在一处，供不变式遍历。
_MATRIX_INPUTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("http://169.254.169.254/", ()),
    ("http://metadata.google.internal/", (PUBLIC_V4,)),
    ("http://localhost:13000", ("127.0.0.1",)),
    ("http://10.20.1.5/", ()),
    ("http://192.0.2.5/", ()),
    ("http://100.64.0.1/", ()),
    ("https://example.com", (PUBLIC_V4,)),
    ("https://example.com", (PUBLIC_V4, "10.20.1.5")),
    ("https://example.com", ("127.0.0.1", "10.20.1.5")),
    ("http://juice-shop:3000", ("172.18.0.5",)),
    ("例子.中国", (PUBLIC_V4,)),
)
_ALLOWLIST_VARIANTS: tuple[AllowlistDecision, ...] = (
    no_allowlist(),
    _allowlist(mode=AllowlistMode.ENFORCE),
    _allowlist(mode=AllowlistMode.ENFORCE, matched=True),
    EVERYTHING_ALLOWED,
)
_OPT_IN_VARIANTS: tuple[OperatorOptIn, ...] = (
    NO_OPT_IN,
    OperatorOptIn(loopback=True),
    OperatorOptIn(private=True),
    BOTH_OPT_INS,
)


def _all_verdicts() -> list[GuardVerdict]:
    """矩阵全展开：11 × 4 × 4 = 176 个判定。"""
    return [
        _verdict(raw, resolved, allowlist=allowlist, opt_in=opt_in)
        for raw, resolved in _MATRIX_INPUTS
        for allowlist in _ALLOWLIST_VARIANTS
        for opt_in in _OPT_IN_VARIANTS
    ]


def test_every_error_code_is_registered() -> None:
    """护栏产出的每个机器码都必须在 `app/errors.py` 里登记过。

    没有这条，一个拼错的 `"not_in_allowlst"` 会让前端静默落到"未知错误"分支 ——
    而 `test_message_coverage.py` 只看 `errors.py`，看不到护栏这边现编的字符串。
    """
    produced = {v.error_code for v in _all_verdicts()} - {None}
    assert produced <= _REGISTERED_CODES, f"未登记的机器码：{sorted(produced - _REGISTERED_CODES)}"
    # 反面：这三个码必须真的被产出过，否则说明矩阵覆盖漏了。
    assert {"blocked_metadata", "split_horizon", "not_in_allowlist"} <= produced


def test_allowed_verdicts_have_no_error_and_no_pending_opt_in() -> None:
    for verdict in _all_verdicts():
        if verdict.allowed:
            assert verdict.error_code is None
            assert verdict.required_opt_in == ()


def test_overridable_depends_only_on_category_and_mode() -> None:
    """`overridable` 与"这次勾没勾"无关 —— 勾上之后放行入口不该从界面上消失。

    唯一允许它随外部状态变的是白名单 `mode`（advisory 下的 public 无需覆盖）。
    """
    by_key: dict[tuple[TargetCategory, AllowlistMode], set[bool]] = {}
    for raw, resolved in _MATRIX_INPUTS:
        for allowlist in _ALLOWLIST_VARIANTS:
            for opt_in in _OPT_IN_VARIANTS:
                verdict = _verdict(raw, resolved, allowlist=allowlist, opt_in=opt_in)
                key = (verdict.category, allowlist.mode)
                by_key.setdefault(key, set()).add(verdict.overridable)
    for key, values in by_key.items():
        assert len(values) == 1, f"{key} 的 overridable 随勾选状态变了：{values}"


def test_metadata_is_never_allowed_anywhere_in_the_matrix() -> None:
    """整个矩阵里凡是判成 metadata 的，一律 `allowed=False` 且 `overridable=False`。"""
    metadata = [v for v in _all_verdicts() if v.category is TargetCategory.METADATA]
    assert metadata, "矩阵里没有 metadata 判定，这条不变式等于没测"
    for verdict in metadata:
        assert verdict.allowed is False
        assert verdict.overridable is False
        assert verdict.error_code == "blocked_metadata"


def test_mixed_is_never_allowed_anywhere_in_the_matrix() -> None:
    mixed = [v for v in _all_verdicts() if v.category is TargetCategory.MIXED]
    assert mixed, "矩阵里没有 mixed 判定，这条不变式等于没测"
    for verdict in mixed:
        assert verdict.allowed is False
        assert verdict.overridable is False
        assert verdict.error_code == "split_horizon"


def test_dataclasses_are_frozen() -> None:
    """判定结果是事实，改它等于让"审计里记的"与"实际用的"不一致。"""
    target: NormalizedTarget = normalize_target("https://example.com")
    with pytest.raises((AttributeError, TypeError)):
        target.host = "evil.example"  # type: ignore[misc]

    address: ClassifiedAddress = classify_address(PUBLIC_V4)
    with pytest.raises((AttributeError, TypeError)):
        address.category = TargetCategory.PUBLIC  # type: ignore[misc]


def test_no_allowlist_default_does_not_relax_any_guard() -> None:
    """默认值（没有白名单文件）不许放宽任何一条非公网护栏。

    它把 mode 定成 advisory（否则第一次启动什么都不能扫），而 advisory 只影响
    public 那一行 —— 环回、内网、保留段仍然要勾选或改文件。
    """
    default = no_allowlist()
    assert default.mode is AllowlistMode.ADVISORY
    assert (default.allow_loopback, default.allow_private, default.allow_reserved) == (
        False,
        False,
        False,
    )
    assert _verdict("http://127.0.0.1/", allowlist=default).allowed is False
    assert _verdict("http://10.20.1.5/", allowlist=default).allowed is False
    assert _verdict("http://192.0.2.5/", allowlist=default).allowed is False
    assert _verdict("http://169.254.169.254/", allowlist=default).allowed is False
