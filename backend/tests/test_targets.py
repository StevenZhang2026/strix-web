"""`POST /api/targets/validate`（T8：`routes/targets.py`）。

# 判据本身不在这里测

护栏矩阵有它自己的 80 行表（`test_target_guard.py`），那个模块是纯函数。本文件测的是
**路由这一层独有的四件事** —— 每一件都是"只看 `evaluate_target` 看不出来"的：

1. **永远 200。** 一批里有一个被拒，另外四个的结论不许跟着消失。
2. **DNS 是注入进来的。** 单测禁止碰真实网络（CLAUDE.md §测试），所以本文件
   一次都不调 `socket.getaddrinfo`；替身还顺带让"同名只解析一次"变得可断言。
3. **解析失败是一个可显示的状态**，不是 500。
4. **正文里没有中文**，且**没有 `registrable_domain`**。

# 替身为什么记录调用

`FakeResolver.calls` 不是为了好看：`dict.fromkeys` 去重那一行如果哪天被删掉，功能上
**看不出任何区别**（两次解析同一个名字通常给同一个答案），只有在轮换 DNS 上才偶尔
产出两行不同地址的诡异结果。能钉住它的只有"你调了几次"。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import create_app
from app.routes.auth import EXEMPT_PATHS, SESSION_COOKIE_NAME
from app.services.allowlist import AllowlistConfig, AllowlistEntry, AllowlistStore
from app.services.dns_resolver import Resolution, ResolutionError
from app.services.target_guard import AllowlistMode
from app.settings import Settings
from tests.conftest import PASSWORD, USERNAME

VALIDATE_PATH = "/api/targets/validate"

# IANA 示例地址（与 test_target_guard.py 同一套，理由见那个文件的模块 docstring：
# 一个渗透测试工具的夹具里绝不能出现真实目标）。
PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


@dataclass
class FakeResolver:
    """`dns_resolver.Resolver` 的替身。**本文件里唯一的"网络"。**

    `answers` 没写到的主机名一律 `dns_not_found` —— 刻意不是"默认给个公网地址"：
    夹具里少写一个名字时，我要看到一条明确的解析失败，而不是一个凭空出现的目标。
    """

    answers: dict[str, Resolution] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def __call__(self, host: str) -> Resolution:
        self.calls.append(host)
        return self.answers.get(host, Resolution(addresses=(), error=ResolutionError.NOT_FOUND))


def public(*addresses: str) -> Resolution:
    return Resolution(addresses=addresses, error=None)


@pytest.fixture
def resolver() -> FakeResolver:
    return FakeResolver(answers={"example.com": public(PUBLIC_V4)})


@pytest.fixture
def app(settings: Settings, auth_file: Path, restore_logging: None) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI, resolver: FakeResolver) -> Iterator[TestClient]:
    # base_url 必须是 https：会话 cookie 带 `Secure`。
    with TestClient(app, base_url="https://testserver") as test_client:
        # **在 lifespan 跑完之后**替换解析器：lifespan 装的是真 `dns_resolver.resolve`，
        # 这里换成替身 —— 就是 `routes/targets.py::_resolver` 那个注入点的实际用法。
        app.state.dns_resolver = resolver
        test_client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
        assert test_client.cookies.get(SESSION_COOKIE_NAME), "登录没成功，后面会全是 401"
        yield test_client


def validate(client: TestClient, *raw: str, **overrides: bool) -> list[dict[str, object]]:
    """发一次请求，断言 200，把 `targets` 数组交出来。

    每个用例都要断言 200（模块 docstring 第 1 条），所以它在这里而不是各写一遍。
    """
    body: dict[str, object] = {"raw": list(raw)}
    if overrides:
        body["overrides"] = overrides
    response = client.post(VALIDATE_PATH, json=body)
    assert response.status_code == 200, f"应当恒为 200，实得 {response.status_code}"
    targets = response.json()["targets"]
    assert isinstance(targets, list)
    return targets


def only(client: TestClient, raw: str, **overrides: bool) -> dict[str, object]:
    targets = validate(client, raw, **overrides)
    assert len(targets) == 1
    return targets[0]


def write_allowlist(app: FastAPI, config: AllowlistConfig) -> None:
    """直接写文件，不走 `PUT /api/allowlist`。

    本文件测的是"清单结论怎么进这个响应"，让它依赖另一个接口会让失败原因变成两个。
    """
    store = app.state.allowlist
    assert isinstance(store, AllowlistStore)
    store.write(config)


def entry(label: str, **overrides: object) -> AllowlistEntry:
    fields: dict[str, object] = {
        "label": label,
        "owner": "安全组",
        "authorization_ref": "TICKET-1",
        "hosts": ("example.com",),
    }
    fields.update(overrides)
    return AllowlistEntry.model_validate(fields)


# =============================================================================
# 一、鉴权
# =============================================================================
def test_validate_requires_a_session(app: FastAPI) -> None:
    """没登录就是 401。

    未鉴权的话这个接口就是一台开放的 DNS 探测器 —— 而 `api` 在 `strix_sandbox`
    网络里，能解析内网名字。请求体故意是合法的：要证明拦下它的是鉴权，不是校验。
    """
    with TestClient(app, base_url="https://testserver") as anonymous:
        response = anonymous.post(VALIDATE_PATH, json={"raw": ["https://example.com"]})
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


def test_validate_is_not_in_the_exempt_list() -> None:
    """上面那条测的是"现在是对的"，这条测的是"以后也别加进去"。"""
    assert VALIDATE_PATH not in EXEMPT_PATHS


def test_unauthenticated_request_does_not_resolve_anything(
    app: FastAPI, resolver: FakeResolver
) -> None:
    """401 必须发生在 DNS 之前。

    先鉴权再解析和先解析再鉴权都会返回 401，但后者已经把内网名字探出去了。
    """
    with TestClient(app, base_url="https://testserver") as anonymous:
        app.state.dns_resolver = resolver
        anonymous.post(VALIDATE_PATH, json={"raw": ["https://example.com"]})
    assert resolver.calls == [], "未鉴权的请求触发了 DNS 查询"


# =============================================================================
# 二、恒 200 与逐条对齐
# =============================================================================
def test_a_rejected_target_does_not_sink_the_batch(client: TestClient) -> None:
    """**本文件最重要的一条。**

    4xx 只有一个正文，用户会看到"请求失败"而不是"第 2 行那个地址不合法"。
    """
    targets = validate(client, "https://example.com", "ht!tp://bad", "https://example.com/app")
    assert [item["ok"] for item in targets] == [True, False, True]
    assert targets[1]["reason"] is not None
    assert targets[0]["normalized"] is not None


def test_order_matches_the_request_one_to_one(client: TestClient) -> None:
    """顺序即对齐关系。前端拿 `raw` 做 key，但顺序错位仍会让它显示错行。

    中间那条是空白（`empty_target`），刻意夹在两条合法目标之间：解析那一步只对
    主机名做，被拒的行不进 `hosts` 元组，所以"结果按 hosts 的顺序拼回去"这种写法
    正好会在这里错位。
    """
    raw = ("https://example.com", "  ", "https://example.com:8443")
    targets = validate(client, *raw)
    assert [item["raw"] for item in targets] == list(raw)
    assert [item["ok"] for item in targets] == [True, False, True]


def test_rejection_reports_a_reason_and_no_normalized_target(client: TestClient) -> None:
    """规范化失败时 `normalized` 是 `null` —— 还没有一个可谈的目标。

    `reason` 属于 `targetGuard.reasons.*`，不是 HTTP 错误码，所以 `code` 是 `null`。
    """
    item = only(client, "https://example.com/repo.git")
    assert item["ok"] is False
    assert item["normalized"] is None
    assert item["kind"] is None
    assert item["ip_class"] is None
    assert item["reason"] == "git_repository_path"
    assert item["code"] is None


def test_credentials_in_the_url_are_rejected_and_only_echoed_once(client: TestClient) -> None:
    """带口令的 URL 被拒，且那串口令在正文里**只出现一次**。

    `raw` 是原样回显的（刻意的，见 `TargetValidation.raw`：它是用户自己刚打的那一行）。
    这里钉住的是"只有那一处"—— `normalized.url` 一旦被填上，同一串口令就会在一份可能被
    截图、被贴进工单的正文里出现第二遍。数出现次数而不是断言"不包含"：后者在这个接口上
    根本不可能成立，写成那样只会让人以为口令被脱敏了。
    """
    secret = "s3cr3t-token"
    response = client.post(VALIDATE_PATH, json={"raw": [f"https://user:{secret}@example.com"]})
    assert response.status_code == 200
    assert response.text.count(secret) == 1
    item = response.json()["targets"][0]
    assert item["ok"] is False
    assert item["reason"] == "credentials_in_url"
    assert item["normalized"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"raw": []},  # 一个目标都没有
        {"raw": ["https://example.com"] * 21},  # 超过 _MAX_TARGETS
        {"raw": [f"https://{'a' * 3000}.com"]},  # 单条超过 _MAX_RAW_LENGTH
        {"raw": "https://example.com"},  # 不是数组
        {"raw": ["https://example.com"], "overrides": {"allow_reserved": True}},
    ],
    ids=["empty", "too-many", "too-long", "not-a-list", "unknown-override"],
)
def test_malformed_request_bodies_are_422(client: TestClient, body: dict[str, object]) -> None:
    """请求体真的不合法 → 422。**这不违反"恒 200"** ——

    那条说的是"每个目标的结论各自成立"，而这些输入根本不是"一批目标"。最后一项
    （`allow_reserved`）尤其重要：`overrides` 是 `extra="forbid"`，界面上永不提供
    那一项，从 API 悄悄塞进来也不行。
    """
    assert client.post(VALIDATE_PATH, json=body).status_code == 422


# =============================================================================
# 三、DNS
# =============================================================================
def test_hostnames_are_resolved_through_the_injected_resolver(
    client: TestClient, resolver: FakeResolver
) -> None:
    item = only(client, "https://example.com")
    assert resolver.calls == ["example.com"]
    addresses = item["resolved_ips"]
    assert isinstance(addresses, list)
    assert [entry_view["address"] for entry_view in addresses] == [PUBLIC_V4]


def test_a_repeated_hostname_is_resolved_once(client: TestClient, resolver: FakeResolver) -> None:
    """去重（见模块 docstring 末段）。同名的两行必须给出**同一组**地址。"""
    targets = validate(client, "https://example.com", "https://example.com:8443/admin")
    assert resolver.calls == ["example.com"]
    first, second = (item["resolved_ips"] for item in targets)
    assert first == second


def test_an_ip_literal_skips_dns_entirely(client: TestClient, resolver: FakeResolver) -> None:
    """字面 IP 没有 DNS 这一步 —— 对它发查询是一次无谓的外发。"""
    item = only(client, f"https://{PUBLIC_V4}")
    assert resolver.calls == []
    assert item["kind"] == "ip"
    addresses = item["resolved_ips"]
    assert isinstance(addresses, list)
    assert [view["address"] for view in addresses] == [PUBLIC_V4]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ResolutionError.NOT_FOUND, "dns_not_found"),
        (ResolutionError.TIMEOUT, "dns_timeout"),
        (ResolutionError.FAILED, "dns_failed"),
    ],
)
def test_resolution_failure_is_reported_faithfully(
    client: TestClient, resolver: FakeResolver, error: ResolutionError, code: str
) -> None:
    """解析失败是**一行提示**，不是 500，也不是"假装它是别的什么错"。

    `normalized` 照样给全：目标本身是合法的，用户要先看到规范化结果才能判断自己
    是不是打错了名字。
    """
    resolver.answers["down.example.com"] = Resolution(addresses=(), error=error)
    item = only(client, "https://down.example.com")
    assert item["ok"] is False
    assert item["resolution_error"] == code
    assert item["reason"] is None, "解析失败不是规范化被拒，两个字段互斥"
    assert item["code"] is None
    assert item["normalized"] is not None
    assert item["ip_class"] is None, "没有地址就没有类别，别编一个"


def test_one_unresolvable_target_does_not_hide_the_others(
    client: TestClient, resolver: FakeResolver
) -> None:
    resolver.answers["down.example.com"] = Resolution(addresses=(), error=ResolutionError.NOT_FOUND)
    targets = validate(client, "https://down.example.com", "https://example.com")
    assert [item["ok"] for item in targets] == [False, True]


# =============================================================================
# 四、护栏结论怎么进响应
# =============================================================================
def test_public_target_with_no_allowlist_is_allowed(client: TestClient) -> None:
    """没有清单文件 = advisory，公网可以直接扫（`allowlist.decide` 第 3 条）。"""
    item = only(client, "https://example.com")
    assert item["ok"] is True
    assert item["ip_class"] == "public"
    assert item["requirement"] == "none"
    assert item["allowlist_entry"] is None
    assert item["code"] is None
    assert item["required_opt_in"] == []


def test_enforce_mode_without_a_match_reports_not_in_allowlist(
    app: FastAPI, client: TestClient
) -> None:
    write_allowlist(app, AllowlistConfig(entries=(entry("别的", hosts=("other.example.org",)),)))
    item = only(client, "https://example.com")
    assert item["ok"] is False
    assert item["code"] == "not_in_allowlist"
    assert item["requirement"] == "allowlist_entry"
    assert item["allowlist_entry"] is None


def test_a_matched_entry_is_named_in_the_response(app: FastAPI, client: TestClient) -> None:
    """命中的 label 要露出来 —— 用户需要知道自己是凭哪一份授权在扫。"""
    write_allowlist(app, AllowlistConfig(entries=(entry("客户预生产"),)))
    item = only(client, "https://example.com")
    assert item["ok"] is True
    assert item["allowlist_entry"] == "客户预生产"
    assert item["requirement"] == "allowlist_entry", "requirement 说的是凭什么能过，不是还差什么"


def test_a_broken_allowlist_fails_closed_on_this_endpoint(
    app: FastAPI, client: TestClient, settings: Settings
) -> None:
    """**清单坏了 → 公网目标也一律拒。**

    这条是整个 T8 的中心：解析失败绝不能静默退回 advisory。它在这个接口上的形状就是
    一句 `not_in_allowlist` —— 界面上看得见，而不是一个只有日志里才有的警告。
    """
    settings.allowlist_path.parent.mkdir(parents=True, exist_ok=True)
    settings.allowlist_path.write_text("entries: [没闭合\n", encoding="utf-8")
    item = only(client, "https://example.com")
    assert item["ok"] is False
    assert item["code"] == "not_in_allowlist"


def test_advisory_mode_in_the_file_lets_an_unmatched_target_through(
    app: FastAPI, client: TestClient
) -> None:
    write_allowlist(
        app,
        AllowlistConfig(
            mode=AllowlistMode.ADVISORY, entries=(entry("别的", hosts=("other.example.org",)),)
        ),
    )
    item = only(client, "https://example.com")
    assert item["ok"] is True
    assert item["allowlist_entry"] is None


def test_metadata_addresses_are_never_overridable(client: TestClient) -> None:
    """验收 3 点名：云元数据地址 `overridable=false`，且勾选也不管用。"""
    item = only(client, "http://169.254.169.254", allow_loopback=True, allow_private=True)
    assert item["ok"] is False
    assert item["ip_class"] == "metadata"
    assert item["code"] == "blocked_metadata"
    assert item["overridable"] is False
    assert item["requirement"] == "impossible"


def test_loopback_needs_an_opt_in_and_says_what_is_missing(client: TestClient) -> None:
    """缺勾选**不是错误**：`code` 是 `null`，缺的那一项在 `required_opt_in` 里。

    没有 `required_opt_in` 的话前端只能从 `ip_class` 猜要显示哪个复选框，而
    `localhost` 可以同时需要两项。
    """
    item = only(client, "http://127.0.0.1:8080")
    assert item["ok"] is False
    assert item["code"] is None, "缺勾选不是 HTTP 错误，向导还没走完"
    assert item["required_opt_in"] == ["loopback"]
    assert item["overridable"] is True
    assert item["requirement"] == "operator_opt_in"


def test_the_loopback_opt_in_actually_allows_it_and_carries_the_note(client: TestClient) -> None:
    """勾上之后放行，并带上 `loopback_rewrite` —— 验收 3 要求当场解释这件事。

    文案里必须点名 `host.docker.internal`，那条断言在 `test_message_coverage.py`
    （码在这里、文案在那里，双向都有守卫）。
    """
    item = only(client, "http://127.0.0.1:8080", allow_loopback=True)
    assert item["ok"] is True
    assert item["required_opt_in"] == []
    assert item["note_code"] == "loopback_rewrite"


def test_overrides_default_to_off(client: TestClient) -> None:
    """不传 `overrides` 等于两项都没勾。

    默认成 `True` 会让一个漏写字段的客户端静默获得内网扫描权限。
    """
    item = only(client, "http://10.0.0.1")
    assert item["ok"] is False
    assert item["required_opt_in"] == ["private"]


# =============================================================================
# 五、正文形状 —— 契约与"刻意没有的字段"
# =============================================================================
def test_registrable_domain_is_absent(client: TestClient) -> None:
    """⚠️ **2026-09-11 拍板去掉的字段，不许"补回来"。**

    正确实现要 Public Suffix List（新依赖 + 要跟着更新的数据），近似实现在
    `example.co.uk` 上就是错的 —— 一个偶尔说谎的授权范围提示比没有这个提示更危险。
    这条断言就是那次决定的执行点：谁"顺手补上"它，这里会红。
    """
    item = only(client, "https://example.com")
    assert "registrable_domain" not in item


def test_the_response_has_exactly_the_agreed_fields(client: TestClient) -> None:
    """字段集合是契约。多一个字段不会让前端崩，但会让下一个人以为它可以依赖。"""
    item = only(client, "https://example.com")
    assert set(item) == {
        "raw",
        "ok",
        "normalized",
        "kind",
        "resolved_ips",
        "ip_class",
        "allowlist_entry",
        "requirement",
        "overridable",
        "required_opt_in",
        "code",
        "reason",
        "resolution_error",
        "note_code",
    }


def test_the_response_body_has_no_chinese(client: TestClient) -> None:
    """**正文里不许有中文**（CLAUDE.md §错误与文案）。

    输入全是 ASCII，所以整个正文必须是 ASCII —— 出现非 ASCII 只可能是后端硬编码了
    一句中文。用 `isascii()` 而不是逐字段翻：硬编码的那句话会出现在哪个字段上，
    恰恰是我事先不知道的。

    这里刻意**不给** `allowlist_entry` 一个中文 label：`label` 是操作者自己写的文本，
    原样往返是对的（`test_a_matched_entry_is_named_in_the_response` 就在测它）。
    """
    response = client.post(
        VALIDATE_PATH,
        json={"raw": ["https://example.com", "ht!tp://bad", "http://169.254.169.254"]},
    )
    assert response.status_code == 200
    assert response.text.isascii(), "响应体里出现了非 ASCII 字符，多半是硬编码的中文文案"


def test_punycode_is_visible_on_both_sides(client: TestClient, resolver: FakeResolver) -> None:
    """`host` 与 `host_unicode` 都要给。

    "你看到的字符和实际连接的主机不是一回事"是验收 3 要求高亮的东西 —— 只给一个
    就没法高亮：给 unicode 那份等于帮同形异义字骗人，只给 punycode 等于让用户对不上
    自己刚打的字。
    """
    resolver.answers["xn--fsqu00a.xn--0zwm56d"] = public(PUBLIC_V4)
    item = only(client, "http://例子.测试")
    normalized = item["normalized"]
    assert isinstance(normalized, dict)
    assert normalized["punycode_applied"] is True
    assert normalized["host"] == "xn--fsqu00a.xn--0zwm56d"
    assert normalized["host_unicode"] == "例子.测试"


def test_ipv6_addresses_come_back_classified(client: TestClient, resolver: FakeResolver) -> None:
    """AAAA 不许被丢掉：`AF_UNSPEC` 要两族，而 IPv6-only 的内网目标漏掉就等于漏判。"""
    resolver.answers["dual.example.com"] = public(PUBLIC_V4, PUBLIC_V6)
    item = only(client, "https://dual.example.com")
    addresses = item["resolved_ips"]
    assert isinstance(addresses, list)
    assert [view["version"] for view in addresses] == [4, 6]
    assert {view["ip_class"] for view in addresses} == {"public"}


def test_mixed_horizon_is_reported_as_mixed(client: TestClient, resolver: FakeResolver) -> None:
    """一个名字同时解析到公网与内网 —— 那就是 DNS rebinding 的形状。

    汇总成 `public` 会让内网那一半悄悄跟着放行；汇总成 `private` 则说不清它为什么
    被拦。`mixed` 是唯一诚实的答案。
    """
    resolver.answers["rebind.example.com"] = public(PUBLIC_V4, "10.1.2.3")
    item = only(client, "https://rebind.example.com")
    assert item["ip_class"] == "mixed"
    assert item["ok"] is False
