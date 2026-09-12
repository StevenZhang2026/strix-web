"""`/api/keys` 与 `/api/providers` 的路由层测试。

**只测这一层真正新增的转换**：形状矩阵（多键 / 缺键 / 正好）、验活失败之后 vault 里
什么都没有、响应与审计里出现的是掩码标签而不是明文、死 handle、`DELETE` 幂等、鉴权。

`KeyVault` 的生命周期（idle / hard TTL、`ref_count`）与 `secret_label` 的掩码规则已经在
`test_key_vault.py` 测过，这里不重复（CLAUDE.md §测试：同一条不变式只测一遍）。

验活一律用替身：真实验活会拿凭据往模型端点发一次请求（CLAUDE.md §测试 禁止真实网络）。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.key_vault import CredentialSet, secret_label
from app.services.llm_client import VerifyOutcome, model_for, spec_for
from app.settings import Settings

# 假凭据。都 ≥16 字符，否则 `secret_label` 会整体打码成 `…`，"标签保留首尾 4 个字符"
# 这条断言就失去意义（见 `key_vault.LABEL_MIN_LENGTH`）。
_LLM_KEY = "sk-ant-api03-0123456789abcdefghij"
_AWS_ID = "AKIAIOSFODNN7EXAMPLE"
_AWS_SECRET = "wJalrXUtnFEMI-K7MDENG-bPxRfiCYEXAMPLEKEY"
_BEARER = "ABSKQmVkcm9ja0FQSUtleS0wMTIzNDU2Nzg5"

_REGION = {"AWS_REGION_NAME": "us-east-1"}

# 三种形状各一行：(provider, auth_shape, secrets, params)。
_SHAPES = (
    ("gemini", "single", {"LLM_API_KEY": _LLM_KEY}, {}),
    (
        "bedrock",
        "bedrock_sigv4",
        {"AWS_ACCESS_KEY_ID": _AWS_ID, "AWS_SECRET_ACCESS_KEY": _AWS_SECRET},
        _REGION,
    ),
    ("bedrock", "bedrock_bearer", {"AWS_BEARER_TOKEN_BEDROCK": _BEARER}, _REGION),
)
_SHAPE_IDS = tuple(row[1] for row in _SHAPES)

_MODEL = {"gemini": "gemini/gemini-2.5-flash", "bedrock": "us.anthropic.claude-opus-5"}


def _body(
    provider: str,
    auth_shape: str,
    secrets: dict[str, str],
    params: dict[str, str],
    **overrides: object,
) -> dict[str, object]:
    body: dict[str, object] = {
        "provider": provider,
        "auth_shape": auth_shape,
        "strix_llm": _MODEL[provider],
        "secrets": secrets,
        "params": params,
    }
    body.update(overrides)
    return body


class _StubVerifier:
    """验活替身。`calls` 让"校验在验活之前"这条顺序可断言（多填一项时它必须没被调过）。"""

    def __init__(self) -> None:
        self.ok = True
        self.latency_ms = 7
        self.calls: list[CredentialSet] = []

    async def __call__(self, credentials: CredentialSet) -> VerifyOutcome:
        self.calls.append(credentials)
        return VerifyOutcome(ok=self.ok, latency_ms=self.latency_ms)


@pytest.fixture
def verifier() -> _StubVerifier:
    return _StubVerifier()


@pytest.fixture
def anonymous(app: FastAPI, verifier: _StubVerifier) -> Iterator[TestClient]:
    """覆写 conftest 的同名夹具，装上验活替身。

    替身必须在 `with TestClient(app)` **之后**才装得住：lifespan 会把
    `app.state.llm_verifier` 赋成真实现，在进入 `with` 之前塞会被原地盖掉。
    """
    with TestClient(app, base_url="https://testserver") as test_client:
        app.state.llm_verifier = verifier
        yield test_client


def _register(client: TestClient, index: int = 0) -> str:
    """登记一组凭据并返回 handle。默认用 `single` 那一行。"""
    response = client.post("/api/keys", json=_body(*_SHAPES[index]))
    assert response.status_code == 201, response.text
    handle: str = response.json()["vault_handle"]
    return handle


def _audit_text(settings: Settings) -> str:
    return "".join(path.read_text(encoding="utf-8") for path in settings.audit_dir.glob("*.ndjson"))


# =============================================================================
# `GET /api/providers`
# =============================================================================
def test_providers_declares_two_bedrock_shapes(client: TestClient) -> None:
    """Bedrock 有两种互斥形状，而"区域"是参数不是凭据 —— 这两点是前端渲染的全部依据。"""
    payload = client.get("/api/providers").json()
    by_name = {entry["provider"]: entry for entry in payload["providers"]}
    assert {"bedrock", "gemini"} <= set(by_name)

    shapes = {shape["auth_shape"]: shape for shape in by_name["bedrock"]["shapes"]}
    assert set(shapes) == {"bedrock_sigv4", "bedrock_bearer"}
    assert shapes["bedrock_bearer"]["secret_keys"] == ["AWS_BEARER_TOKEN_BEDROCK"]
    assert shapes["bedrock_sigv4"]["secret_keys"] == [
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    ]
    for shape in shapes.values():
        # 区域进了 secret_keys 就会被登记进日志脱敏集合，于是 `us-east-1` 这种到处都
        # 出现的字符串把日志替换成一片 [REDACTED]（见 key_vault 模块 docstring）。
        assert shape["param_keys"] == ["AWS_REGION_NAME"]
        assert "AWS_REGION_NAME" not in shape["secret_keys"]

    assert by_name["gemini"]["shapes"][0]["secret_keys"] == ["LLM_API_KEY"]


# =============================================================================
# 模型名怎么拼（纯函数，`ScanLauncher` 之后也要用它）
# =============================================================================
@pytest.mark.parametrize(
    ("auth_shape", "typed", "expected"),
    [
        # bearer **必须**走 invoke 路由：converse 路由无条件先取 SigV4 凭据，只给
        # bearer 时崩在 `'NoneType' object has no attribute 'access_key'`（`PLAN.md:330`）。
        (
            "bedrock_bearer",
            "us.anthropic.claude-opus-5",
            "bedrock/invoke/us.anthropic.claude-opus-5",
        ),
        # 用户照着 SigV4 的文档粘了个前缀也不许改变路由 —— 否则拼出
        # `bedrock/invoke/bedrock/...`，而验活失败不会告诉他原因。
        ("bedrock_bearer", "bedrock/us.x", "bedrock/invoke/us.x"),
        ("bedrock_sigv4", "us.x", "bedrock/us.x"),
        ("bedrock_sigv4", "bedrock/invoke/us.x", "bedrock/us.x"),
    ],
)
def test_model_name_follows_the_shape_not_the_typing(
    auth_shape: str, typed: str, expected: str
) -> None:
    spec = spec_for("bedrock", auth_shape)
    assert spec is not None
    assert model_for(spec, typed) == expected


def test_single_shape_leaves_the_model_name_alone() -> None:
    spec = spec_for("gemini", "single")
    assert spec is not None
    assert model_for(spec, "gemini/gemini-2.5-flash") == "gemini/gemini-2.5-flash"


# =============================================================================
# `POST /api/keys` —— 形状矩阵
# =============================================================================
@pytest.mark.parametrize(("provider", "auth_shape", "secrets", "params"), _SHAPES, ids=_SHAPE_IDS)
def test_exact_keys_register_and_return_labels(
    client: TestClient,
    verifier: _StubVerifier,
    provider: str,
    auth_shape: str,
    secrets: dict[str, str],
    params: dict[str, str],
) -> None:
    response = client.post("/api/keys", json=_body(provider, auth_shape, secrets, params))

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["verified"] is True
    assert body["verify_latency_ms"] == verifier.latency_ms
    # 一组值一组标签：SigV4 两个值就要两条，只回一条会让人不知道是哪个填错了。
    assert body["labels"] == {name: secret_label(value) for name, value in secrets.items()}
    for value in secrets.values():
        assert value not in response.text
    assert len(verifier.calls) == 1


@pytest.mark.parametrize(("provider", "auth_shape", "secrets", "params"), _SHAPES, ids=_SHAPE_IDS)
def test_extra_secret_key_is_rejected_before_verifying(
    client: TestClient,
    verifier: _StubVerifier,
    app: FastAPI,
    provider: str,
    auth_shape: str,
    secrets: dict[str, str],
    params: dict[str, str],
) -> None:
    """多填一项 → 400，**而且验活没被调过**。

    多余的键就是"bearer 与 SigV4 都填上"那个静默故障的形状（`PLAN.md:340`）。校验必须
    在验活之前：反过来会让这个和网络无关的错误先等满一次超时。
    """
    polluted = {**secrets, "AWS_BEARER_TOKEN_BEDROCK": _BEARER, "LLM_API_KEY": _LLM_KEY}
    response = client.post("/api/keys", json=_body(provider, auth_shape, polluted, params))

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == "unexpected_secret_key"
    assert body["params"]["key_name"] not in secrets
    assert body["params"]["auth_shape"] == auth_shape
    assert verifier.calls == []
    assert app.state.key_vault.count() == 0


@pytest.mark.parametrize(("provider", "auth_shape", "secrets", "params"), _SHAPES, ids=_SHAPE_IDS)
def test_missing_secret_key_is_rejected(
    client: TestClient,
    verifier: _StubVerifier,
    app: FastAPI,
    provider: str,
    auth_shape: str,
    secrets: dict[str, str],
    params: dict[str, str],
) -> None:
    """缺一项 → 422，`params` 指出缺的是哪个键。"""
    dropped = sorted(secrets)[0]
    short = {name: value for name, value in secrets.items() if name != dropped}
    response = client.post("/api/keys", json=_body(provider, auth_shape, short, params))

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_request"
    assert body["params"] == {"field": "secrets", "key_name": dropped}
    assert verifier.calls == []
    assert app.state.key_vault.count() == 0


@pytest.mark.parametrize(("provider", "auth_shape", "secrets", "params"), _SHAPES, ids=_SHAPE_IDS)
def test_undeclared_param_key_is_rejected(
    client: TestClient,
    verifier: _StubVerifier,
    app: FastAPI,
    provider: str,
    auth_shape: str,
    secrets: dict[str, str],
    params: dict[str, str],
) -> None:
    """`params` 里放一个没声明的键 → 422，什么都不存。

    这条**不是**对称性洁癖，它挡的是一条真实泄漏路径：`params` 不进
    `KeyVault.secret_values()`（所以永远不脱敏），又被 `GET /api/keys/{handle}` 原样
    回显。一个把凭据填进 `params` 的客户端会同时得到三件事 —— 存下来、明文回显、
    日志不脱敏。`ShapeSpec.param_keys` 已经声明了白名单并经 `/api/providers` 发布出去，
    **声明了却不强制，等于没声明**。
    """
    smuggled = {**params, "AWS_SECRET_ACCESS_KEY": _AWS_SECRET}
    response = client.post("/api/keys", json=_body(provider, auth_shape, secrets, smuggled))

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_request"
    assert body["params"] == {"field": "params", "key_name": "AWS_SECRET_ACCESS_KEY"}
    assert _AWS_SECRET not in response.text
    assert verifier.calls == []
    assert app.state.key_vault.count() == 0


# 只有声明了 `param_keys` 的形状才谈得上"缺一个 param"（`single` 一个都没声明）。
_PARAM_SHAPES = tuple(row for row in _SHAPES if row[3])


@pytest.mark.parametrize(
    ("provider", "auth_shape", "secrets", "params"),
    _PARAM_SHAPES,
    ids=tuple(row[1] for row in _PARAM_SHAPES),
)
def test_missing_param_key_is_rejected(
    client: TestClient,
    verifier: _StubVerifier,
    app: FastAPI,
    provider: str,
    auth_shape: str,
    secrets: dict[str, str],
    params: dict[str, str],
) -> None:
    """少填区域 → 422。

    放它过去的代价不是"少一个字段"：`_completion_kwargs` 会把一个空区域发给 litellm，
    而验活失败**按契约不带任何原因** —— 用户于是拿着一组好凭据去排查凭据。
    `verify=false` 时更糟：这组注定跑不起来的凭据会被存进 vault，等 T9 真起扫描才炸。
    """
    response = client.post("/api/keys", json=_body(provider, auth_shape, secrets, {}))

    assert response.status_code == 422, response.text
    assert response.json()["params"] == {"field": "params", "key_name": "AWS_REGION_NAME"}
    assert verifier.calls == []
    assert app.state.key_vault.count() == 0


@pytest.mark.parametrize(
    ("provider", "auth_shape"),
    [("nosuchvendor", "single"), ("gemini", "bedrock_sigv4"), ("bedrock", "single")],
)
def test_unknown_provider_or_shape_is_rejected(
    client: TestClient, verifier: _StubVerifier, provider: str, auth_shape: str
) -> None:
    """供应商不认识、或它不支持这个形状 —— 两者都是 422，都不发验活请求。"""
    body_json = _body("gemini", auth_shape, {"LLM_API_KEY": _LLM_KEY}, {})
    body_json["provider"] = provider
    response = client.post("/api/keys", json=body_json)

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_request"
    assert verifier.calls == []


# =============================================================================
# 验活
# =============================================================================
def test_failed_verification_stores_nothing(
    client: TestClient, verifier: _StubVerifier, app: FastAPI
) -> None:
    """400 `key_verify_failed`，**vault 里一条都没有**，正文里没有任何原因描述。

    存一份"已知是错的"凭据会让用户拿着一个能用的 handle 去起扫描，几十分钟后才失败。
    `params` 里刻意没有 `reason`：模型服务的错误正文可能把凭据本身回显出来。
    """
    verifier.ok = False
    response = client.post("/api/keys", json=_body(*_SHAPES[0]))

    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == "key_verify_failed"
    assert body["params"] == {
        "provider": "gemini",
        "auth_shape": "single",
        "latency_ms": verifier.latency_ms,
    }
    assert app.state.key_vault.count() == 0
    assert _LLM_KEY not in response.text


def test_verify_false_skips_the_request_and_says_so(
    client: TestClient, verifier: _StubVerifier, app: FastAPI
) -> None:
    """`verify=false` 合法（离线用），但 `verified` 必须**如实**回 false。"""
    response = client.post("/api/keys", json=_body(*_SHAPES[0], verify=False))

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["verified"] is False
    # `null` 而不是 0 —— 0 会被读成"验得飞快"。
    assert body["verify_latency_ms"] is None
    assert verifier.calls == []
    assert app.state.key_vault.count() == 1


# =============================================================================
# 审计
# =============================================================================
def test_audit_records_labels_not_plaintext(client: TestClient, settings: Settings) -> None:
    """审计里只有掩码标签与机器码。这一份是 `grep` 用的，明文进去就是永久落盘的泄漏。"""
    client.post("/api/keys", json=_body(*_SHAPES[1]))
    text = _audit_text(settings)

    assert "key.registered" in text
    assert "bedrock_sigv4" in text
    assert secret_label(_AWS_ID) in text
    assert _AWS_ID not in text
    assert _AWS_SECRET not in text


# =============================================================================
# `GET` / `DELETE`
# =============================================================================
def test_get_key_returns_metadata_without_plaintext(client: TestClient) -> None:
    handle = _register(client, index=1)
    response = client.get(f"/api/keys/{handle}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"] == "bedrock"
    assert body["auth_shape"] == "bedrock_sigv4"
    assert body["strix_llm"] == _MODEL["bedrock"]
    # 区域是非机密参数，原样显示（它得让人看见才能核对）。
    assert body["params"] == _REGION
    assert _AWS_SECRET not in response.text


def test_dead_handle_asks_for_the_key_again(client: TestClient) -> None:
    """内存 vault 重启即失效，所以"查不到"是预期状态而不是 404。"""
    response = client.get("/api/keys/definitely-not-a-real-handle")

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "key_required"


def test_delete_is_idempotent_and_audits_once(
    client: TestClient, app: FastAPI, settings: Settings
) -> None:
    """第二次 DELETE 同样 204，但**不再写一条审计** —— 那件事没有发生第二次。"""
    handle = _register(client)

    assert client.delete(f"/api/keys/{handle}").status_code == 204
    assert app.state.key_vault.count() == 0
    assert client.delete(f"/api/keys/{handle}").status_code == 204
    assert _audit_text(settings).count("key.dropped") == 1


# =============================================================================
# 鉴权
# =============================================================================
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/providers"),
        ("post", "/api/keys"),
        ("get", "/api/keys/whatever"),
        ("delete", "/api/keys/whatever"),
    ],
)
def test_requires_login(anonymous: TestClient, method: str, path: str) -> None:
    """未登录一律 401。一个不鉴权的 `/api/keys` 等于让任意网页替用户登记并验活凭据。"""
    response = anonymous.request(method, path, json=_body(*_SHAPES[0]))

    assert response.status_code == 401, response.text
