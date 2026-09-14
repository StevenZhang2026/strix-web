"""单账号登录（T4b）。

# 这里的测试**必须是行为式的**，理由是实测出来的

已实测：本版本（fastapi 0.141.1 / starlette 1.6.0）里经 `include_router` 挂上的路由在
`app.routes` 里塌缩成一个 `_IncludedRouter` 对象，`path == '?'`，并且**根本没有
`dependencies` 属性**；HTTP 与 WS 一样不可见。也就是说"遍历 `app.routes` 数一数依赖"
这类结构性断言在本版本上是**零信息量**的 —— 它会在鉴权被删掉之后继续通过。

所以：发真请求，断言状态码与响应体。反向验证（把 `dependencies=[...]` 拆掉，看这里
是否变红）才是这些测试唯一的价值来源。

# TestClient 的 `base_url` 必须是 https

会话 cookie 带 `Secure`，httpx 的 cookie jar **不会**把 Secure cookie 发给 http 源。
用默认的 `http://testserver` 会得到"登录成功了但下一个请求还是 401"这种看起来像
后端 bug 的现象。

# 为什么 app 级测试里不能用 caplog

lifespan 会调 `configure_logging()`，那个函数**清空 root handlers** —— 包括 pytest
装上的捕获 handler。所以"失败登录不许记用户名"这条只能写成单测（不起 app）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI, Request, WebSocket
from starlette.testclient import TestClient, WebSocketDenialResponse

from app.errors import (
    ALL_ERRORS,
    AuthLockedError,
    ConsoleError,
    InvalidCredentialsError,
    OriginMismatchError,
    UnauthenticatedError,
)
from app.main import create_app
from app.routes.auth import EXEMPT_PATHS, SESSION_COOKIE_NAME
from app.services.auth import (
    ABSOLUTE_TIMEOUT_SECONDS,
    IDLE_TIMEOUT_SECONDS,
    LOGIN_FAILURE_LIMIT,
    MAX_SESSIONS,
    AuthFileError,
    AuthRecord,
    AuthService,
    KdfParams,
    LoginRateLimiter,
    SessionStore,
    write_auth_file,
)
from app.settings import Settings
from tests.conftest import PASSWORD, USERNAME

WRONG_PASSWORD = "wrong-horse-battery-staple"

# 一个 UI 上的常见事故：用户把口令粘进了用户名框。下面有一条测试专门确认它不会进日志。
PASTED_PASSWORD_AS_USERNAME = "Tr0ub4dor&3-pasted-in-the-wrong-box"

PROBE_PATH = "/api/_probe"
PROBE_WS_PATH = "/ws/_probe"


# =============================================================================
# 夹具
# =============================================================================
@dataclass
class FakeClock:
    """可手动推进的单调时钟。

    没有它的话，"8 小时后会话失效"这条测试要么真睡 8 小时，要么去 monkeypatch
    `time.monotonic`（那是进程级的全局改动，会影响 pytest 自己）。
    `SessionStore` / `LoginRateLimiter` / `AuthService.load` 都收 `clock` 参数，
    就是为了这个。
    """

    now: float = 10_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def app(settings: Settings, auth_file: Path, restore_logging: None) -> FastAPI:
    """真实应用 + 两条探针路由。

    探针路由是必需的：T4b 之外的业务路由还不存在（都在 T3/T6/T7/T9），而"受保护的
    普通路由会 401"和"WS 握手会 401"正是本任务要证明的两件事。它们挂在 app 上，
    因此和真实路由走**同一条**全局依赖链 —— 不是模拟。

    `auth_file` 必须在 `create_app` 之前建好：lifespan 会读它，读不到就拒绝启动
    （那条语义本身也有测试，见 `test_missing_auth_file_blocks_startup`）。
    """
    application = create_app(settings)

    @application.get(PROBE_PATH)
    async def probe(request: Request) -> dict[str, str]:
        # 读 `request.state.session` 是刻意的：它证明鉴权依赖写进 `conn.state` 的东西
        # 在路由里读得到（两者共享 `scope["state"]`）。T9 的 audit_log.actor 靠这条。
        return {"username": request.state.session.username}

    @application.websocket(PROBE_WS_PATH)
    async def probe_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(websocket.state.session.username)
        await websocket.close()

    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    # base_url 必须是 https，见模块 docstring。
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client


@pytest.fixture
def logged_in(client: TestClient) -> TestClient:
    response = client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
    assert response.status_code == 200
    assert client.cookies.get(SESSION_COOKIE_NAME)
    return client


# =============================================================================
# 一、错误码登记表（要求 10）
# =============================================================================
@pytest.mark.parametrize(
    ("cls", "code", "status"),
    [
        (UnauthenticatedError, "unauthenticated", 401),
        (InvalidCredentialsError, "invalid_credentials", 401),
        (AuthLockedError, "auth_locked", 429),
        (OriginMismatchError, "origin_mismatch", 403),
    ],
)
def test_new_error_codes_are_registered(cls: type[ConsoleError], code: str, status: int) -> None:
    """四个新码都必须进手写的 `ALL_ERRORS`，且是 `ConsoleError` 的**直接**子类。

    直接子类这一条不是风格问题：`test_settings.py` 断言
    `set(ConsoleError.__subclasses__()) == set(ALL_ERRORS)`，而 `__subclasses__()`
    只看一层。中间加一个 `AuthError` 基类会让那条断言在"看起来更整洁"的重构里变红。
    """
    assert cls.code == code
    assert cls.status == status
    assert cls in ALL_ERRORS
    assert cls in ConsoleError.__subclasses__()


def test_auth_locked_carries_retry_after_header() -> None:
    """429 必须带 `Retry-After`（RFC 9110 §15.6.4）。"""
    assert AuthLockedError(retry_after=42).response_headers() == {"Retry-After": "42"}
    # 没带 retry_after 时不许凭空造一个头 —— 那会让前端倒计时到一个假数字。
    assert AuthLockedError().response_headers() == {}
    # 其它错误一个头都不加。
    assert UnauthenticatedError().response_headers() == {}


# =============================================================================
# 二、受保护的路由（要求 1）
# =============================================================================
def test_protected_http_route_without_cookie_is_401(client: TestClient) -> None:
    response = client.get(PROBE_PATH)
    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "unauthenticated"
    # 形状仍然是全局那三个字段 —— 前端的错误解析器只有一个。
    assert set(body) == {"code", "trace_id", "params"}


def test_protected_http_route_with_cookie_sees_the_session(logged_in: TestClient) -> None:
    """带上 cookie 之后放行，**且**路由能读到 `request.state.session`。"""
    response = logged_in.get(PROBE_PATH)
    assert response.status_code == 200
    assert response.json() == {"username": USERNAME}


def test_garbage_cookie_is_401(client: TestClient) -> None:
    """伪造的会话 id 必须当成没登录，不能 500。"""
    client.cookies.set(SESSION_COOKIE_NAME, "not-a-real-session-id", domain="testserver")
    response = client.get(PROBE_PATH)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


# =============================================================================
# 三、免鉴权路径（要求 2）—— 逐条列举
# =============================================================================
# 每条免鉴权路径都要在这张表里，并且都**不能**是 401。
# `/api/auth/login` 不带 body 打过去是 422（请求体不合法），那正说明它进到了路由
# 而不是被鉴权挡下 —— 用 422 而不是 200 做期望值是刻意的。
_EXEMPT_CASES: tuple[tuple[str, str, int], ...] = (
    ("GET", "/api/health", 200),
    ("GET", "/api/auth/me", 200),
    ("POST", "/api/auth/logout", 200),
    ("POST", "/api/auth/login", 422),
)


def test_exempt_case_table_covers_every_exempt_path() -> None:
    """往 `EXEMPT_PATHS` 里加一条却不写测试 —— 这条会红。

    这是本文件里唯一的结构性断言，且它断言的是**我们自己的常量**，不是 fastapi 的
    内部结构（后者已实测不可观察，见模块 docstring）。免鉴权名单是攻击面，
    每次增删都必须被人看见。
    """
    assert {path for _, path, _ in _EXEMPT_CASES} == set(EXEMPT_PATHS)


@pytest.mark.parametrize(("method", "path", "expected"), _EXEMPT_CASES)
def test_exempt_paths_do_not_require_a_cookie(
    client: TestClient, method: str, path: str, expected: int
) -> None:
    response = client.request(method, path)
    assert response.status_code == expected
    assert response.status_code != 401


def test_me_without_cookie_is_200_and_says_not_authenticated(client: TestClient) -> None:
    """`/api/auth/me` **永远 200**。

    如果它未登录时返回 401，前端就无法区分"我没登录"与"服务挂了 / 被别的东西拦了"
    —— 两者都是 401，而正确的 UI 完全不同（跳登录页 vs 显示服务不可用）。
    """
    response = client.get("/api/auth/me")
    assert response.status_code == 200
    assert response.json() == {"authenticated": False, "username": None}


def test_me_with_cookie_reports_the_username(logged_in: TestClient) -> None:
    response = logged_in.get("/api/auth/me")
    assert response.status_code == 200
    assert response.json() == {"authenticated": True, "username": USERNAME}


def test_logout_is_idempotent(client: TestClient) -> None:
    """没有会话时登出也要 200 —— 否则用户会卡在一个登不出去的界面上。"""
    assert client.post("/api/auth/logout").status_code == 200
    assert client.post("/api/auth/logout").status_code == 200


def test_logout_invalidates_the_session(logged_in: TestClient) -> None:
    assert logged_in.get(PROBE_PATH).status_code == 200
    logout = logged_in.post("/api/auth/logout")
    assert logout.status_code == 200
    # 服务端会话真的没了：即使把 cookie 值塞回去也不行。
    assert logged_in.get(PROBE_PATH).status_code == 401


# =============================================================================
# 四、WebSocket（要求 3、9）
#
# ⚠️ WS 握手必须**手工带 cookie 头**，cookie jar 在这条路径上不起作用。
#
# 已实测（starlette 1.6.0，testclient.py:662）：`websocket_connect` 里写的是
# `urljoin("ws://testserver", url)` —— scheme 被**硬编码**成 `ws://`，`base_url` 完全
# 不参与。于是 httpx 的 cookie jar 认为目标不是 https，按 RFC 6265 拒绝发送带
# `Secure` 的 cookie，服务端只会看到一个没有 cookie 的握手。
#
# 这是测试工具的限制，**不是产品缺陷**：真实浏览器对 `wss://` 会正常携带 Secure
# cookie。所以这里显式拼 `cookie` 头 —— 它测的仍然是"服务端能不能从 WS 握手的
# cookie 头里认出会话"，那正是要证明的事。
# =============================================================================
def _cookie_header(client: TestClient) -> dict[str, str]:
    return {"cookie": f"{SESSION_COOKIE_NAME}={client.cookies[SESSION_COOKIE_NAME]}"}


def test_websocket_without_cookie_is_denied_with_401_json(client: TestClient) -> None:
    """WS 握手被拒时返回**真正的 401 + JSON 正文**，不是一个空的 403。

    这一条是 T4b 最重要的测试：鉴权若写成 `BaseHTTPMiddleware`，它只看得见 http
    scope，`/ws/...` 就成了一条**完全无鉴权**的后门 —— 而浏览器和其余测试都表现正常。
    本项目里有活证据：trace-id 中间件在 WS 下不跑，所以 WS 相关日志的 trace_id 恒为
    `-`（下面顺手断言了这一点，它是"中间件确实看不见 WS"的直接证据）。
    """
    with pytest.raises(WebSocketDenialResponse) as excinfo:
        with client.websocket_connect(PROBE_WS_PATH):
            pass  # pragma: no cover —— 握手成功就说明鉴权没生效，走不到这里
    denial = excinfo.value
    assert denial.status_code == 401
    assert denial.headers["content-type"].startswith("application/json")
    body = denial.json()
    assert body["code"] == "unauthenticated"
    assert body["trace_id"] == "-"


def test_websocket_with_cookie_handshakes(logged_in: TestClient) -> None:
    headers = _cookie_header(logged_in)
    with logged_in.websocket_connect(PROBE_WS_PATH, headers=headers) as websocket:
        # 收到用户名说明 `websocket.state.session` 真的被鉴权依赖写上了 ——
        # 全局依赖在 websocket scope 下确实跑了，不是"恰好没拦"。
        assert websocket.receive_text() == USERNAME


def test_websocket_origin_mismatch_is_403(logged_in: TestClient) -> None:
    """跨站 Origin 的握手拒掉。**纵深防御** —— 真正挡住它的是 `SameSite=Strict`。

    刻意带上**有效的** cookie：这样 403 只可能来自 Origin 检查，不会是"顺便没登录"。
    """
    headers = {**_cookie_header(logged_in), "Origin": "https://evil.example"}
    with pytest.raises(WebSocketDenialResponse) as excinfo:
        with logged_in.websocket_connect(PROBE_WS_PATH, headers=headers):
            pass  # pragma: no cover
    assert excinfo.value.status_code == 403
    assert excinfo.value.json()["code"] == "origin_mismatch"


def test_websocket_same_origin_with_port_handshakes(logged_in: TestClient) -> None:
    """`Origin` 带端口、`Host` 不带 —— 这是**生产上的常态**，必须放行。

    nginx 用 `proxy_set_header Host $host`，`$host` 不含端口；而浏览器的 `Origin`
    在非默认端口下含端口。逐字符比这两个值会让改过端口的部署永远 403 —— 一个只在
    换端口之后才出现、且看起来像"证书有问题"的故障。
    """
    headers = {**_cookie_header(logged_in), "Origin": "https://testserver:8443"}
    with logged_in.websocket_connect(PROBE_WS_PATH, headers=headers) as websocket:
        assert websocket.receive_text() == USERNAME


# =============================================================================
# 五、登录（要求 4、5、6）
# =============================================================================
def test_login_sets_a_hardened_cookie(client: TestClient) -> None:
    response = client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
    assert response.status_code == 200
    assert response.json() == {"authenticated": True, "username": USERNAME}

    raw = response.headers["set-cookie"]
    assert raw.startswith(f"{SESSION_COOKIE_NAME}=")
    assert "HttpOnly" in raw
    assert "Secure" in raw
    assert "SameSite=strict" in raw.replace("SameSite=Strict", "SameSite=strict")
    assert "Path=/" in raw
    # 会话 cookie：关掉浏览器就没了。有 Max-Age/Expires 会造成"cookie 还在但服务端
    # 会话早没了"的错觉，而服务端会话真正的寿命是"api 进程活多久"。
    assert "Max-Age" not in raw
    assert "expires" not in raw.lower()
    # 响应体里绝不能出现会话 id —— 它只走 HttpOnly cookie。
    assert client.cookies[SESSION_COOKIE_NAME] not in response.text


def test_wrong_password_and_unknown_username_return_the_same_code(client: TestClient) -> None:
    """两种失败共用 `invalid_credentials`。

    分成两个码等于免费告诉攻击者用户名对不对，而单账号系统里那正是他要猜的一半。
    """
    wrong_password = client.post(
        "/api/auth/login", json={"username": USERNAME, "password": WRONG_PASSWORD}
    )
    unknown_user = client.post("/api/auth/login", json={"username": "nobody", "password": PASSWORD})
    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json()["code"] == "invalid_credentials"
    assert unknown_user.json()["code"] == "invalid_credentials"
    # 失败的响应不许下发任何 cookie。
    assert "set-cookie" not in wrong_password.headers
    assert "set-cookie" not in unknown_user.headers


def test_login_never_echoes_the_submitted_password(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login", json={"username": USERNAME, "password": WRONG_PASSWORD}
    )
    assert WRONG_PASSWORD not in response.text


def test_repeated_failures_lock_with_retry_after(client: TestClient) -> None:
    """连续失败到阈值 → 429 `auth_locked` + `Retry-After`。

    每次失败都真跑一次 scrypt（约 44 ms），所以这条测试比别的慢几百毫秒。
    那不是浪费 —— "失败路径也要花同样的时间"正是它要保护的性质。
    """
    for _ in range(LOGIN_FAILURE_LIMIT - 1):
        response = client.post(
            "/api/auth/login", json={"username": USERNAME, "password": WRONG_PASSWORD}
        )
        assert response.status_code == 401
        assert response.json()["code"] == "invalid_credentials"

    locked = client.post("/api/auth/login", json={"username": USERNAME, "password": WRONG_PASSWORD})
    assert locked.status_code == 429
    assert locked.json()["code"] == "auth_locked"
    assert int(locked.headers["retry-after"]) > 0

    # 锁定期内**正确**的口令也进不去 —— 否则限速只是拖慢而不是拦阻。
    still_locked = client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
    assert still_locked.status_code == 429
    assert still_locked.json()["code"] == "auth_locked"


@pytest.mark.parametrize(
    "payload",
    [
        {"username": USERNAME},  # 缺 password
        {"password": PASSWORD},  # 缺 username
        {"username": "", "password": PASSWORD},  # 空用户名
        {"username": USERNAME, "password": PASSWORD, "extra": "x"},  # 多字段
    ],
)
def test_malformed_login_body_is_422_not_401(client: TestClient, payload: dict[str, str]) -> None:
    """请求体不合法是 422 `invalid_request`，不是 401。

    多字段那条依赖 `BoundaryModel` 的 `extra="forbid"` —— 它挡的正是
    `{"username":..., "password":..., "api_key":"sk-..."}` 这种"多带了一个凭据字段
    却被静默丢掉"的请求（见 models.BoundaryModel）。
    """
    response = client.post("/api/auth/login", json=payload)
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"
    # Pydantic 的错误详情里含 `input`（即用户提交的原值），绝不能进响应。
    assert PASSWORD not in response.text


# =============================================================================
# 六、落盘的 auth.json（要求 7）
# =============================================================================
def test_auth_file_is_0600(auth_file: Path) -> None:
    """权限位必须是 0600，**从创建的那一刻起**。

    `write_auth_file` 用 `os.open(O_CREAT|O_EXCL, 0o600)` 而不是"先写再 chmod"：
    后者留一个几微秒的窗口，文件在那时是 0644 的，同机另一个账号只要在那时打开它
    就够了（拿到 fd 之后权限位怎么改都无所谓）。这条测试看不出那个窗口 ——
    它只保证终态；窗口的事靠代码注释与 review。
    """
    assert auth_file.stat().st_mode & 0o777 == 0o600


def test_auth_file_contains_no_plaintext_password(auth_file: Path) -> None:
    text = auth_file.read_text(encoding="utf-8")
    assert PASSWORD not in text
    # 也不许出现任何看起来像明文口令的片段：散列与 salt 都是 base64。
    for field in ("salt_b64", "hash_b64", "kdf"):
        assert field in text


def test_auth_file_roundtrips(auth_file: Path) -> None:
    record = AuthRecord.load(auth_file)
    assert record.username == USERNAME
    assert record.verify(PASSWORD)
    assert not record.verify(WRONG_PASSWORD)


def test_write_auth_file_leaves_no_temp_file(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    write_auth_file(path, "{}\n")
    write_auth_file(path, "{}\n")  # 第二次要能覆盖（os.replace），不能撞 O_EXCL
    assert [p.name for p in tmp_path.iterdir()] == ["auth.json"]


@pytest.mark.parametrize(
    "content",
    [
        "not json at all",
        "[]",  # 顶层不是对象
        '{"version": 99, "username": "u", "salt_b64": "AAAA", "hash_b64": "AAAA", "kdf": {}}',
        '{"version": 1, "username": "u"}',  # 缺字段
        '{"version": 1, "username": "u", "salt_b64": "!!!", "hash_b64": "AAAA",'
        ' "kdf": {"name": "scrypt", "n": 16384, "r": 8, "p": 1, "dklen": 32}}',
        '{"version": 1, "username": "u", "salt_b64": "AAAA", "hash_b64": "AAAA",'
        ' "kdf": {"name": "bcrypt", "n": 16384, "r": 8, "p": 1, "dklen": 32}}',
        '{"version": 1, "username": "u", "salt_b64": "AAAA", "hash_b64": "AAAA",'
        ' "kdf": {"name": "scrypt", "n": true, "r": 8, "p": 1, "dklen": 32}}',
    ],
)
def test_broken_auth_file_raises_never_degrades_to_no_account(tmp_path: Path, content: str) -> None:
    """损坏的 `auth.json` 必须抛错，**绝不**退化成"没有账号所以放行"。

    这是本任务最重要的失败语义：若读不出来时当作"没设账号"，那么删掉这个文件就是
    一条绕过登录的路径 —— 而删文件比猜口令容易得多。
    """
    path = tmp_path / "auth.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(AuthFileError):
        AuthRecord.load(path)


def test_missing_auth_file_blocks_startup(settings: Settings, restore_logging: None) -> None:
    """没有 `auth.json` 时 lifespan 直接失败 —— 不是"先跑起来再说"。

    用共享的 `settings` 夹具而不是自己构造：T10 起 lifespan 里多了一条
    `assert_sandbox_env()`，自己构造就会撞在它上面，而这个测试要测的是 auth 那一条。
    """
    with pytest.raises(AuthFileError), TestClient(create_app(settings)):
        pass  # pragma: no cover


# =============================================================================
# 七、会话生命周期（要求 8）
# =============================================================================
def _fake_clock_service(auth_file: Path, clock: FakeClock) -> AuthService:
    return AuthService.load(auth_file, clock=clock)


def test_idle_timeout_expires_the_session(auth_file: Path) -> None:
    clock = FakeClock()
    service = _fake_clock_service(auth_file, clock)
    session = service.login(USERNAME, PASSWORD)

    clock.advance(IDLE_TIMEOUT_SECONDS - 1)
    assert service.resolve(session.id) is not None  # 这一次取用会续期

    clock.advance(IDLE_TIMEOUT_SECONDS - 1)
    assert service.resolve(session.id) is not None  # 所以还活着

    clock.advance(IDLE_TIMEOUT_SECONDS + 1)
    assert service.resolve(session.id) is None


def test_absolute_timeout_expires_even_if_active(auth_file: Path) -> None:
    """绝对上限不受续期影响 —— 否则一个一直被用的会话永远不过期。"""
    clock = FakeClock()
    service = _fake_clock_service(auth_file, clock)
    session = service.login(USERNAME, PASSWORD)

    # 步长必须**小于**空闲上限，否则先撞上的是空闲过期，这条测试就测不到绝对上限了
    # （第一版写了 12 小时 > 8 小时的空闲上限，直接测错了对象）。
    elapsed = 0.0
    step = IDLE_TIMEOUT_SECONDS / 2
    while elapsed + step < ABSOLUTE_TIMEOUT_SECONDS:
        clock.advance(step)
        elapsed += step
        assert service.resolve(session.id) is not None

    clock.advance(ABSOLUTE_TIMEOUT_SECONDS)
    assert service.resolve(session.id) is None


def test_expired_session_cookie_gets_401_from_the_real_app(app: FastAPI, auth_file: Path) -> None:
    """过期不只是 store 的内部状态 —— 它必须变成一个真的 401。

    换时钟必须在**进入** TestClient 之后：lifespan 自己会装一个真实时钟的
    `AuthService`，提前装的那个会被它覆盖掉（第一版就是这么写的，于是这条测试拿到
    200 而不是 401 —— 一个"测试自己没生效"的失败，正是反向验证要抓的那种）。
    """
    clock = FakeClock()
    with TestClient(app, base_url="https://testserver") as client:
        app.state.auth = _fake_clock_service(auth_file, clock)
        assert (
            client.post(
                "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
            ).status_code
            == 200
        )
        assert client.get(PROBE_PATH).status_code == 200
        clock.advance(IDLE_TIMEOUT_SECONDS + 1)
        response = client.get(PROBE_PATH)
        assert response.status_code == 401
        assert response.json()["code"] == "unauthenticated"


def test_session_store_evicts_the_oldest_when_full() -> None:
    clock = FakeClock()
    store = SessionStore(clock=clock)
    sessions = []
    for _ in range(MAX_SESSIONS):
        sessions.append(store.create(USERNAME))
        clock.advance(1.0)  # 让 created_at 严格递增，"最老"才有唯一答案
    assert store.count() == MAX_SESSIONS

    newest = store.create(USERNAME)
    assert store.count() == MAX_SESSIONS
    assert store.resolve(sessions[0].id) is None  # 最老的被淘汰
    assert store.resolve(sessions[1].id) is not None
    assert store.resolve(newest.id) is not None


def test_dropping_an_unknown_session_is_a_noop() -> None:
    store = SessionStore()
    store.drop(None)
    store.drop("nope")
    assert store.count() == 0


def test_rate_limiter_unlocks_after_the_window() -> None:
    clock = FakeClock()
    limiter = LoginRateLimiter(clock=clock)
    for _ in range(LOGIN_FAILURE_LIMIT):
        limiter.record_failure()
    locked_for = limiter.retry_after()
    assert locked_for > 0

    clock.advance(locked_for)
    assert limiter.retry_after() == 0


# =============================================================================
# 八、时序与日志（单测，不起 app）
# =============================================================================
def _new_service(record: AuthRecord) -> AuthService:
    return AuthService(record=record, sessions=SessionStore(), limiter=LoginRateLimiter())


def test_unknown_username_still_burns_one_kdf_run() -> None:
    """用户名不匹配时也要跑一次 KDF，否则登录接口就是个**用户名探测器**。

    对比的基准是一次裸 KDF 的耗时（约 44 ms），而"不跑 KDF"的分支只要几微秒 ——
    差三个数量级。所以用 0.5 倍做门槛既宽松又足以区分：真的省掉那次 KDF 时，
    这条断言会以百倍的余量失败。
    """
    record = AuthRecord.create(USERNAME, PASSWORD)

    start = time.perf_counter()
    record.kdf.derive(PASSWORD, record.salt)
    kdf_seconds = time.perf_counter() - start

    service = _new_service(record)
    start = time.perf_counter()
    with pytest.raises(InvalidCredentialsError):
        service.login("definitely-not-the-operator", PASSWORD)
    unknown_seconds = time.perf_counter() - start

    assert unknown_seconds > kdf_seconds * 0.5


def test_failed_login_logs_only_whether_the_username_matched(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """**绝不记提交上来的用户名** —— 用户会把口令粘进用户名框（真实发生过）。

    这条只能是单测：app 级测试里 lifespan 调的 `configure_logging()` 会清空 root
    handlers，包括 pytest 装上的捕获 handler。
    """
    service = _new_service(AuthRecord.create(USERNAME, PASSWORD))
    with caplog.at_level(logging.WARNING), pytest.raises(InvalidCredentialsError):
        service.login(PASTED_PASSWORD_AS_USERNAME, WRONG_PASSWORD)

    dumped = "\n".join(f"{r.getMessage()} {r.__dict__!r}" for r in caplog.records)
    assert PASTED_PASSWORD_AS_USERNAME not in dumped
    assert WRONG_PASSWORD not in dumped
    assert any(r.__dict__.get("username_matched") is False for r in caplog.records)


def test_successful_login_never_logs_the_session_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = _new_service(AuthRecord.create(USERNAME, PASSWORD))
    with caplog.at_level(logging.INFO):
        session = service.login(USERNAME, PASSWORD)

    dumped = "\n".join(f"{r.getMessage()} {r.__dict__!r}" for r in caplog.records)
    assert session.id not in dumped
    assert PASSWORD not in dumped


# =============================================================================
# 九、KDF 参数
# =============================================================================
def test_kdf_params_are_stored_in_the_file_not_taken_from_code(auth_file: Path) -> None:
    """校验用的是**文件里**的参数。

    这决定了以后调参（提高 n）不会废掉已经设好的口令 —— 老口令继续按老参数验过。
    若只信代码里的常量，"改一行常量"就等于所有人都登不进去，而报错是"口令错误",
    没人会把它联想到那次改动。
    """
    record = AuthRecord.load(auth_file)
    weaker = KdfParams(name="scrypt", n=2**10, r=8, p=1, dklen=32)
    assert record.kdf != weaker
    # 用错参数一定验不过 —— 这正是"参数必须跟着文件走"的原因。
    assert record.derived_key != weaker.derive(PASSWORD, record.salt)


def test_kdf_params_reject_absurd_memory() -> None:
    """文件里的 n 多打两个 0 时，报一条说得清的错，而不是被 OOM kill。"""
    with pytest.raises(AuthFileError):
        KdfParams(name="scrypt", n=2**24, r=8, p=1, dklen=32)


def test_default_kdf_fits_in_the_openssl_default_maxmem() -> None:
    """`n = 2**14` 是**默认 maxmem（OpenSSL 3 下 32 MiB）内的最大档**，不是随手挑的数。

    已实测 `n = 2**15` 在默认 maxmem 下直接
    `ValueError: memory limit exceeded`。本模块永远显式传 maxmem，所以这里断言的是
    那个公式仍然算得出 16 MiB —— 有人改了 SCRYPT_R 就会在这里看到后果。
    """
    record = AuthRecord.create(USERNAME, PASSWORD)
    assert record.kdf.maxmem == 16_780_288
    assert record.kdf.maxmem <= 32 * 1024 * 1024


@pytest.mark.parametrize("password", ["short", "", "elevenchars"])
def test_creating_a_record_rejects_short_passwords(password: str) -> None:
    with pytest.raises(AuthFileError):
        AuthRecord.create(USERNAME, password)


@pytest.mark.parametrize("username", ["", " leading", "trailing ", "with space", "a\nb", "x" * 65])
def test_creating_a_record_rejects_bad_usernames(username: str) -> None:
    """换行会让用户名变成一个日志注入点（伪造出一整条日志记录）。"""
    with pytest.raises(AuthFileError):
        AuthRecord.create(username, PASSWORD)
