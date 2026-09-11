"""`GET /api/system/status` —— 依赖自检（`PLAN.md` 验收 #2）。

# 三件在本文件里被刻意固定下来的事

1. **它在全局鉴权之后。** 本文件**没有**、也不许有任何往 `auth.EXEMPT_PATHS` 里加
   路径的代码。理由：它的正文包含数据目录的绝对路径、沙箱镜像名、孤儿容器名、
   docker 版本 —— 一份给攻击者用的侦察报告。而它还会**起一个容器**，那是一个未鉴权
   就能触发的资源消耗面。
   （数据目录路径本身不是秘密 —— 它就在 `.env` 里 —— 但"不是秘密"不等于"该匿名公开"。）

2. **全部阻塞 IO 一次性挪出事件循环。** `collect_system_status()` 是同步函数
   （UDS 上的 `http.client`、`read_bytes`、`ssl` 都是阻塞的），这里用一次
   `asyncio.to_thread` 把它整个搬走。逐个 `await` 包装才是 CLAUDE.md §Python
   那条"async 函数里禁止同步阻塞 IO"真正要防的东西 —— 看起来是 async，实际阻塞。

3. **它永远返回 200。** 诊断结果是正文里的 `blockers`，不是状态码。
   见 `models.SystemStatusResponse` 的 docstring。

# 慢是已知的、可接受的

同路径探测要真起一个容器，实测量级是几百毫秒到一两秒。这是这个接口的**全部价值**
所在：静态检查（"配置里两边路径字符串一样吗"）零成本但证明不了任何事 ——
路径别名 bug 恰好发生在配置正确而 Docker Desktop 的 File sharing 没覆盖该路径的时候。
所以**不加缓存**：一个缓存过的就绪状态会在用户刚刚修好问题之后继续说"没修好"，
或者更糟，在刚刚坏掉之后继续说"没问题"。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from app import __version__
from app.models import SystemStatusResponse
from app.services.docker_probe import DockerProbe, DockerTransport
from app.services.system_status import collect_system_status
from app.settings import Settings

router = APIRouter(prefix="/api/system", tags=["system"])


def _settings(request: Request) -> Settings:
    """从 `app.state` 取配置。取不到 = lifespan 没跑 = 编程错误。

    与 `routes/auth._auth_service()` 同一个形状（连异常文案都对齐）：
    抛 `RuntimeError` → 500 `internal_error`，而不是把配置故障伪装成业务错误。
    """
    settings = getattr(request.app.state, "settings", None)
    if not isinstance(settings, Settings):
        raise RuntimeError(
            "app.state.settings 不存在。本接口要求 lifespan 已经跑过 —— "
            "测试里请用 `with TestClient(app):`。"
        )
    return settings


def _probe(request: Request) -> DockerProbe:
    """构造探测器。传输层来自 `app.state`，**这就是单测的注入点**。

    为什么传输层挂在 state 上而不是在这里 `UnixSocketTransport()`：
    CLAUDE.md §测试 禁止单测碰真实 Docker，所以"跟 docker 说话"这一层必须可替身。
    挂在 state 上让替换点只有一处，且与 `db` / `auth` / `settings` 完全同构 ——
    不需要为它引入 `dependency_overrides` 这套第二种注入机制。

    这里刻意**不** `isinstance` 检查：`DockerTransport` 是 `Protocol`，而对非
    `runtime_checkable` 的 Protocol 做 isinstance 会直接 `TypeError`。给它加
    `@runtime_checkable` 只是为了让这一行能写出来，而它换来的检查是**结构性的**
    （只看有没有 `request` 属性），拦不住任何真实错误。缺失时的 `None` 会在第一次
    调用时以 `AttributeError` → 500 暴露，信息同样清楚。
    """
    transport: DockerTransport = request.app.state.docker_transport
    return DockerProbe(transport=transport)


@router.get("/status", response_model=SystemStatusResponse)
async def system_status(request: Request) -> SystemStatusResponse:
    settings = _settings(request)
    return await asyncio.to_thread(
        collect_system_status,
        settings=settings,
        probe=_probe(request),
        self_container_ref=request.app.state.self_container_ref,
        app_version=__version__,
        strix_version=request.app.state.strix_version,
    )
