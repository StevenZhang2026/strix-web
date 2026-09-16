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
import logging
from contextlib import suppress
from dataclasses import asdict

from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect

from app import __version__
from app.models import PullImageResponse, SystemStatusResponse
from app.routes._context import actor, client_ip
from app.services.audit import EVENT_IMAGE_PULL_STARTED, AuditEntry, record
from app.services.docker_probe import DockerProbe, DockerTransport
from app.services.image_puller import ALREADY_PRESENT_PHASE, ImagePuller
from app.services.system_status import collect_system_status
from app.settings import Settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/system", tags=["system"])

# **`/ws/system` 不能挂在上面那个 router 上**：它的 prefix 是 `/api/system`，挂上去会
# 静默变成 `/api/system/ws/system` —— 不报错、不告警，只是前端永远连不上。
# 全部 WS 路径在 nginx 的 `location /ws/` 下（四条 upgrade 指令在那里），所以它们必须
# 真的以 `/ws/` 开头。`test_routes_system_pull.py` 里有一条测试连的就是这个确切字符串。
ws_router = APIRouter(tags=["system"])


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


def _puller(request: Request) -> ImagePuller:
    """lifespan 建的那一个。**必须是同一个实例** —— 它持有拉取状态与那个后台任务，
    每次请求新建一个等于每次 POST 都起一次并行拉取（`start()` 的幂等就白写了）。
    """
    puller: ImagePuller = request.app.state.image_puller
    return puller


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


@router.post("/pull-image", response_model=PullImageResponse)
async def pull_image(request: Request, response: Response) -> PullImageResponse:
    """把沙箱镜像拉到本地。**请求没有正文。**

    拉哪个镜像由 `Settings.strix_image`（来源是 compose）决定，**不由请求决定**：
    一个收 `{"reference": ...}` 的接口等于"让登录用户从任意 registry 拉任意镜像到
    宿主 daemon 上"，那是一条越权的资源消耗与供应链路径。

    状态码即"起没起"：`200` 镜像已在本地，`202` 正在拉。进度不在这个响应里 ——
    连 `/ws/system` 看。
    """
    puller = _puller(request)
    settings = _settings(request)

    state, started = await puller.start(settings.strix_image)

    # 只有"这一次真的起了一次拉取"才写审计。重复 POST（上一次还在跑）与"已经在本地"
    # 都不写 —— 那一条会记录一件没发生的事。
    # 这个判据由 `start()` 在它自己的锁里给出，**不能在这里比 epoch**：两个同时到达的
    # POST 里的第二个会看到第一个推进的 epoch，于是同一次拉取被记两条。
    if started:
        await record(
            db=request.app.state.db,
            audit_dir=settings.audit_dir,
            entry=AuditEntry(
                event=EVENT_IMAGE_PULL_STARTED,
                actor=actor(request),
                detail={"reference": state.reference, "epoch": state.epoch},
                client_ip=client_ip(request),
                user_agent=request.headers.get("user-agent"),
            ),
        )

    response.status_code = 200 if state.phase == ALREADY_PRESENT_PHASE else 202
    return PullImageResponse(**asdict(state))


@ws_router.websocket("/ws/system")
async def system_stream(websocket: WebSocket) -> None:
    """镜像拉取进度。**一帧全量快照 + 之后的增量。**

    刻意**不实现 `hello` / `resume`**：进度是幂等快照，重放一遍旧进度没有任何价值。
    被唤醒时也只推**最新**一帧（中间帧丢掉），理由同上。T14 的 `/ws/scans/{id}` 推的是
    不可重建的事件序列，`resume` 是在那里才需要的东西。

    鉴权由 app 级依赖 `require_session` 完成（含 Origin 校验），**本文件不做也不许做**
    任何鉴权判断，更不许往 `EXEMPT_PATHS` 加路径。匿名握手会拿到一个真的 401。
    """
    puller: ImagePuller = websocket.app.state.image_puller
    await websocket.accept()

    # 先记住游标再发快照：反过来的话，两步之间的一次发布会被当成"快照里已经有了"而丢掉。
    seen = puller.version
    await websocket.send_json(puller.snapshot_frame().model_dump())

    # 我们从不读客户端消息，所以断开只能靠一个专门的 receive 任务看见 —— 少了它，
    # 每一个关掉的页面都会在服务端留下一个永远等在 `wait_for_change` 上的任务。
    gone = asyncio.create_task(_wait_until_gone(websocket))
    try:
        while True:
            change = asyncio.create_task(puller.wait_for_change(seen))
            done, _pending = await asyncio.wait({change, gone}, return_when=asyncio.FIRST_COMPLETED)
            if gone in done:
                change.cancel()
                return
            seen = puller.version
            frame = puller.current_frame()
            if frame is not None:
                await websocket.send_json(frame.model_dump())
    except WebSocketDisconnect:
        return
    finally:
        gone.cancel()
        with suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
            await gone


async def _wait_until_gone(websocket: WebSocket) -> None:
    """等到客户端断开。**客户端发来的消息一律丢掉** —— 这条流是单向的。"""
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
