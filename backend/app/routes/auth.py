"""登录三个接口 + 全局会话依赖。

# 为什么鉴权是**全局依赖**而不是中间件

`BaseHTTPMiddleware` 只看得见 http scope —— WebSocket 握手根本不经过它。本项目里有
**活证据**：`main.py` 的 trace-id 中间件在 WS 下完全不跑，于是 WS 相关日志的
`trace_id` 恒为默认值 `-`。如果鉴权写成中间件，`/ws/...` 就是一条**完全没有鉴权的
后门**，而它在浏览器里工作正常、在测试里也工作正常，只有攻击者会注意到。

`FastAPI(dependencies=[Depends(require_session)])` 覆盖 `@router.websocket`
（含经 `include_router` 挂上去的），已实测。

# ⚠️ 不要写"数一数路由上挂了几个依赖"的结构性测试

已实测：本版本（fastapi 0.141.1 / starlette 1.6.0）里经 `include_router` 挂上的路由
在 `app.routes` 里会**塌缩成一个** `_IncludedRouter` 对象，`path == '?'`，而且**根本
没有 `dependencies` 属性**。HTTP 与 WS 路由一样不可见。也就是说任何这类断言都是
零信息量的 —— 它会在机制被删掉之后**继续通过**。

鉴权的测试只能是**行为式**的：发真请求，断言状态码。见 `tests/test_auth.py`。

# 为什么依赖的签名是 `HTTPConnection` 而不是 `Cookie(...)`

两个理由，第一个是硬的：全局依赖的签名里出现 `Cookie("sid")` 时，只要**任何**路由有
一个同名路径参数（`/api/scans/{sid}` 这种），应用**构建期**就会
`AssertionError: Cannot use \\`Cookie\\` for path param 'sid'`。全局依赖会被复制到每条
路由上，于是一条路由的命名就能把整个应用炸掉。自己从 `conn.cookies` 读没有这个耦合。

第二个：`HTTPConnection` 是 `Request` 与 `WebSocket` 的共同基类，一个签名同时服务两种
scope。写 `Request` 会在 WS 上拿不到对象。
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response
from pydantic import Field, SecretStr
from starlette.requests import HTTPConnection

from app.errors import OriginMismatchError, UnauthenticatedError
from app.models import BoundaryModel
from app.services.auth import (
    MAX_PASSWORD_LENGTH,
    MAX_USERNAME_LENGTH,
    AuthService,
    Session,
)

logger = logging.getLogger(__name__)

# cookie 名。短且无信息量是刻意的：它会出现在每一个请求头里，没必要在里面写
# "strix_console_session" 去告诉扫描器这是什么。
SESSION_COOKIE_NAME = "sid"

# =============================================================================
# 免鉴权路径 —— **精确匹配** `scope["path"]`，不是前缀匹配
#
# 前缀匹配（`path.startswith("/api/auth")`）会让以后新增的 `/api/auth/anything`
# 自动免鉴权，那是一个"加一个路由就悄悄开一个洞"的设计。
#
# 为什么用 `scope["path"]` 而不是路由对象：已实测 `Route.matches()` 返回的
# child_scope 只有 `{"endpoint", "path_params"}`，**没有** `"route"` 键 ——
# 依赖里拿不到"我挂在哪条路由上"。scope["path"] 是唯一稳定可得的标识。
#
# 四条各自的理由：
#   /api/health       —— compose 的 healthcheck 与 nginx 要打它，它们没有 cookie。
#                        它刻意只返回版本号，不碰 DB、不碰 docker（见 HealthResponse）。
#   /api/auth/login   —— 不登录就拿不到 cookie，鉴权它等于死锁。
#   /api/auth/logout  —— 登出**必须幂等**：会话已经失效（进程重启过）时用户点登出
#                        应当拿到 200 并清掉 cookie，而不是 401 卡在一个登不出去的
#                        界面上。它自己读 cookie，见下方 `logout`。
#   /api/auth/me      —— 它的**全部作用**就是回答"我登录了吗"，永远 200。
# =============================================================================
EXEMPT_PATHS: frozenset[str] = frozenset(
    {
        "/api/health",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/me",
    }
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


# =============================================================================
# 边界模型
# =============================================================================
class LoginRequest(BoundaryModel):
    """`POST /api/auth/login` 的请求体。

    口令是 `SecretStr`：它的 `__repr__` 是 `SecretStr('**********')`，所以这个模型
    被塞进任何日志/异常/pydantic 校验报告里都不会带出明文。裸 `str` 在
    `RequestValidationError` 的 `input` 字段里会原样出现（见 main.py 的校验处理器 ——
    那里刻意不把 `errors()` 放进响应，正是因为这个）。

    `max_length` 而不是 `min_length`：口令**下限只在设置口令时校验**
    （`AuthRecord.create`）。在登录接口上加下限会让"太短的口令"拿到 422 而不是 401，
    从而告诉攻击者"这个口令没进到比对环节" —— 一个免费的过滤条件。
    上限存在的理由与安全无关：拦住 10 MB 的请求体。
    """

    username: str = Field(min_length=1, max_length=MAX_USERNAME_LENGTH)
    password: SecretStr = Field(max_length=MAX_PASSWORD_LENGTH)


class SessionStateResponse(BoundaryModel):
    """`/api/auth/login` 与 `/api/auth/me` 的响应体。

    刻意**不含**会话 id、不含过期时间戳：
      · 会话 id 只走 `HttpOnly` cookie，进了响应体就等于交给了 JS（见 CLAUDE.md
        §安全不变式：前端只把 opaque handle 存 sessionStorage）。
      · 过期时间会诱使前端做本地倒计时，而真正的失效原因大多是"api 重启了"，
        本地倒计时对此一无所知 —— 一个总是说谎的 UI。前端应当靠 401 反应。
    """

    authenticated: bool
    username: str | None


# =============================================================================
# 全局依赖
# =============================================================================
def _auth_service(conn: HTTPConnection) -> AuthService:
    """从 `app.state` 取服务实例。

    它由 lifespan 装上（`main.py`）。取不到说明 lifespan 没跑 —— 那是编程错误，
    不是业务错误，所以抛 `RuntimeError`（→ 500 internal_error），**不是** 401。
    把它当成"没登录"会把一个配置故障伪装成用户问题。
    """
    service = getattr(conn.app.state, "auth", None)
    if not isinstance(service, AuthService):
        raise RuntimeError(
            "app.state.auth 不存在。鉴权依赖要求 lifespan 已经跑过 —— "
            "测试里请用 `with TestClient(app):`。"
        )
    return service


def _origin_matches_host(origin: str, host: str) -> bool:
    """比对 `Origin` 与 `Host` 的**主机名**，刻意忽略端口与 scheme。

    忽略端口不是偷懒：nginx 用 `proxy_set_header Host $host`，`$host` **不含端口**，
    而浏览器发出的 `Origin` 在非默认端口下**含端口**。逐字符比这两个值会让
    `https://127.0.0.1:8443` 这种部署永远 403 —— 一条只在改了端口之后才出现的故障。

    Host 头前面补 `//` 是必须的：`urlsplit("127.0.0.1:443")` 会把 `127.0.0.1`
    当作 **scheme** 解析，`hostname` 返回 None，于是"合法请求"被判成不匹配。
    """
    origin_host = urlsplit(origin).hostname
    header_host = urlsplit("//" + host).hostname
    return origin_host is not None and origin_host == header_host


def _check_websocket_origin(conn: HTTPConnection) -> None:
    """WS 握手的 `Origin` 必须与 `Host` 同主机名。

    **这是纵深防御，不是唯一防线。** 真正挡住"任意网页对着我们发 WS 握手"的是
    `SameSite=Strict` —— 跨站发起的握手带不上会话 cookie，所以它在到这里之前就已经
    没有身份了。这一层的意义是"cookie 属性哪天被人改松了"时还剩一道，**不是**
    可以放心去改 SameSite 的理由。

    没有 `Origin` 头时**放行**：浏览器对 WS 握手总会带它，所以缺它意味着请求来自
    非浏览器客户端（curl、我们自己的测试）。而非浏览器客户端不存在"被诱导携带
    环境 cookie"这种攻击 —— 它得先有 cookie，那时它已经是登录用户了。
    在这里 403 只会挡住运维排障，挡不住任何人。
    """
    origin = conn.headers.get("origin")
    if origin is None:
        return
    host = conn.headers.get("host", "")
    if not _origin_matches_host(origin, host):
        # **不记 origin 的原值**：它由攻击者完全可控，是一个日志注入点。
        # 只记"发生了不匹配"，路径足以定位。
        logger.warning("WS 握手 Origin 与 Host 不一致", extra={"path": conn.scope["path"]})
        raise OriginMismatchError()


async def require_session(conn: HTTPConnection) -> None:
    """全局鉴权依赖。放行则把会话挂到 `conn.state.session`。

    `conn.state` 背后是 `scope["state"]` 这个共享 dict，所以路由函数里
    `request.state.session` 读到的就是这里写进去的对象 —— T9 的 `audit_log.actor`
    靠它取操作者。**T4b 自己不写任何 audit_log 行**（那是 T9 的交付物）。

    返回 `None` 而不是返回 `Session`：全局依赖的返回值 FastAPI 会丢掉，没有路由能
    通过 `Depends` 拿到它。写成 `-> Session` 会让人以为可以，然后在别处再解一遍。
    """
    if conn.scope["path"] in EXEMPT_PATHS:
        return

    # 顺序：先看请求形状（Origin），再看身份。反过来也能工作，但那样一个跨站发来的
    # 握手会因为"没 cookie"报 401，掩盖掉真正的问题是它跨站。
    if conn.scope["type"] == "websocket":
        _check_websocket_origin(conn)

    service = _auth_service(conn)
    session = service.resolve(conn.cookies.get(SESSION_COOKIE_NAME))
    if session is None:
        # 不记有没有带 cookie、不记 cookie 的值。"带了但过期"与"没带"对运维是同一件
        # 事（重新登录），而记下 cookie 值等于把会话 id 写进日志。
        raise UnauthenticatedError()

    conn.state.session = session


# =============================================================================
# 接口
# =============================================================================
def _set_session_cookie(response: Response, session: Session) -> None:
    """四个属性每一个都是必需的，别删。

    · `httponly` —— JS 读不到它。XSS 之后仍然拿不到可外带的凭据。
    · `secure`   —— 只在 https 上发送。本项目对外只经 nginx 终止 TLS，
                    绑 `127.0.0.1:443` 且**不监听 80**，所以这一条没有兼容性代价。
    · `samesite="strict"` —— 这是本项目**唯一**的 CSRF 防护（刻意不做 CSRF token）。
                    跨站发起的请求（含 WS 握手）一律带不上它。
    · `path="/"` —— `/api` 与 `/ws` 都要用。

    刻意**没有** `max_age` / `expires` —— 这是一个**会话 cookie**，关掉浏览器即消失。
    给它一个 7 天的过期时间只会造成"cookie 还在但服务端会话早没了"的错觉，而服务端
    会话真正的寿命是"api 进程活多久"。
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session.id,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )


@router.post("/login", response_model=SessionStateResponse)
async def login(
    request: Request, response: Response, payload: LoginRequest
) -> SessionStateResponse:
    """校验口令，建会话，下发 cookie。

    `asyncio.to_thread`：`AuthService.login` 里那次 scrypt 是**同步阻塞约 44 ms**
    （`hashlib.scrypt` 会释放 GIL，所以放线程池是真的并行）。直接 await 它等于把
    整个事件循环冻住 44 ms —— 而这个接口是未鉴权的，任何人都能触发，
    也就是说它会变成一个人人可用的拒绝服务开关。
    """
    service = _auth_service(request)
    session = await asyncio.to_thread(
        service.login, payload.username, payload.password.get_secret_value()
    )
    _set_session_cookie(response, session)
    return SessionStateResponse(authenticated=True, username=session.username)


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict[str, bool]:
    """登出。**幂等**：没带 cookie、cookie 早失效，都返回 200。

    在免鉴权名单里（见 `EXEMPT_PATHS` 的注释）。它自己读 cookie —— 读不到就只是
    少删一条内存记录，没有别的后果。

    `delete_cookie` 的属性必须与 `set_cookie` 时**一致**：浏览器按
    (name, domain, path) 定位 cookie，`path` 不一致会留下一个删不掉的旧 cookie，
    症状是"点了登出，刷新还是登录状态"。
    """
    service = _auth_service(request)
    service.logout(request.cookies.get(SESSION_COOKIE_NAME))
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return {"ok": True}


@router.get("/me", response_model=SessionStateResponse)
async def me(request: Request) -> SessionStateResponse:
    """当前登录态。**永远 200**，从不 401。

    这一条是刻意的：如果未登录时它返回 401，前端就无法区分"我没登录"和"服务挂了 /
    被别的东西拦了"—— 两者都是一个 401。而这两种情况的正确 UI 完全不同（跳登录页
    vs 显示"服务不可用"）。让它永远 200 并在**响应体**里说话，前端就只需要读一个
    布尔值。

    注意它在 `EXEMPT_PATHS` 里，所以 `require_session` 不会跑，这里自己解一次。
    """
    service = _auth_service(request)
    session = service.resolve(request.cookies.get(SESSION_COOKIE_NAME))
    if session is None:
        return SessionStateResponse(authenticated=False, username=None)
    return SessionStateResponse(authenticated=True, username=session.username)
