"""`POST /api/system/pull-image` 与 `WS /ws/system`（T11b）。

# 这个文件盯的是三件事

1. **WS 也在全局鉴权后面。** 匿名连 `/ws/system` 必须在**握手**上就被拒（401），
   而不是连上之后收不到东西。这是验收 27 那张逐路由枚举表里的一格 —— WS 是最容易
   被漏掉的一格，因为 `BaseHTTPMiddleware` 看不见 websocket scope。
2. **路径是 `/ws/system` 这个确切字符串。** 挂在 `prefix="/api/system"` 的 router 上会
   静默变成 `/api/system/ws/system`（不报错、没人发现），所以这条路径要被真的连一次。
3. **拉取只由后端决定拉哪个镜像。** 请求没有正文，镜像引用来自 `Settings.strix_image` ——
   一个带 `{"reference": ...}` 的接口就是"让登录用户从任意 registry 拉任意镜像"。

鉴权本身（cookie / Origin / EXEMPT_PATHS）在 `test_auth.py` 测过，这里只测 WS 这一格。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable, Mapping

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from app.routes.auth import SESSION_COOKIE_NAME
from app.services.audit import EVENT_IMAGE_PULL_STARTED
from app.services.image_puller import ALREADY_PRESENT_PHASE, ImagePuller
from app.settings import Settings
from tests.conftest import FakeClock, FakeTransport, StreamHandler, const

ENVELOPE_KEYS = {"v", "epoch", "seq", "type", "ts", "payload"}

OnLine = Callable[[Mapping[str, object]], None]


def install_puller(
    client: TestClient,
    *,
    present: bool = False,
    stream: StreamHandler | None = None,
) -> tuple[ImagePuller, FakeTransport]:
    """把 lifespan 建的那个（对着真 docker 的）puller 换成一个对着替身的。

    必须在 lifespan 跑完之后换（`client` 夹具进过 `with` 了）：在 `create_app` 之前往
    state 里塞会被 lifespan 原地盖掉（见 conftest 的 `app` 夹具 docstring）。
    """
    transport = FakeTransport(
        routes=[("GET", "/json", const(200 if present else 404, {"Size": 123}))],
        streams=[] if stream is None else [("POST", "/images/create", stream)],
    )
    puller = ImagePuller(transport, clock=FakeClock())
    client.app.state.image_puller = puller
    return puller, transport


def ws_headers(client: TestClient) -> dict[str, str]:
    """把会话 cookie 手工放进握手头。

    **这是 TestClient 的限制，不是产品缺陷**：`websocket_connect` 把 base_url 的
    `https` 换成 `wss`，而标准库 `http.cookiejar` 只认 `https` 是安全 scheme，于是带
    `Secure` 的会话 cookie 不会被自动带上（浏览器对 `wss://` 会带 —— 它就是安全 scheme）。
    不这么做的话，"登录了也连得上"这条测试会永远看到 401，看起来像鉴权坏了。
    """
    return {"Cookie": f"{SESSION_COOKIE_NAME}={client.cookies[SESSION_COOKIE_NAME]}"}


def audit_lines(settings: Settings) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in sorted(settings.audit_dir.glob("*.ndjson")):
        rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    return rows


# =============================================================================
# 鉴权：WS 那一格
# =============================================================================
def test_anonymous_websocket_is_denied_on_handshake(anonymous: TestClient) -> None:
    try:
        with anonymous.websocket_connect("/ws/system"):
            raise AssertionError("匿名居然连上了 /ws/system")
    except WebSocketDenialResponse as denial:
        assert denial.status_code == 401


def test_websocket_origin_must_match(client: TestClient) -> None:
    try:
        with client.websocket_connect(
            "/ws/system", headers={**ws_headers(client), "Origin": "https://evil.example"}
        ):
            raise AssertionError("Origin 不匹配居然连上了")
    except WebSocketDenialResponse as denial:
        assert denial.status_code == 403


def test_health_is_still_exempt(anonymous: TestClient) -> None:
    """新增路由没有顺手改坏免鉴权名单。"""
    assert anonymous.get("/api/health").status_code == 200


# =============================================================================
# 快照帧
# =============================================================================
def test_first_frame_is_a_full_snapshot(client: TestClient) -> None:
    with client.websocket_connect("/ws/system", headers=ws_headers(client)) as socket:
        frame = socket.receive_json()

    assert set(frame) == ENVELOPE_KEYS
    assert frame["v"] == 1
    assert frame["type"] == "pull.snapshot"
    assert frame["seq"] == 0
    assert frame["epoch"] == 0
    assert frame["ts"].endswith("Z")
    assert frame["payload"]["status"] == "idle"


def test_every_subscriber_gets_the_same_frame(client: TestClient) -> None:
    install_puller(client, present=True)

    with (
        client.websocket_connect("/ws/system", headers=ws_headers(client)) as first,
        client.websocket_connect("/ws/system", headers=ws_headers(client)) as second,
    ):
        assert first.receive_json()["type"] == "pull.snapshot"
        assert second.receive_json()["type"] == "pull.snapshot"

        assert client.post("/api/system/pull-image").status_code == 200

        pushed_first = first.receive_json()
        pushed_second = second.receive_json()

    assert pushed_first == pushed_second
    assert pushed_first["type"] == "pull.done"
    assert pushed_first["payload"]["phase"] == ALREADY_PRESENT_PHASE


def test_a_closed_page_leaves_no_task_behind(client: TestClient) -> None:
    """页面关掉之后，服务端那个端点任务必须**自己**结束。

    这条流是单向的（我们从不读客户端消息），所以"断开了"只能靠那个专门的 receive
    任务看见。少了它，每关一个页面就在服务端留一个永远等在 `wait_for_change` 上的
    任务 —— 一天下来几百个，而且每个都握着一个 WebSocket。
    收货 mutation（把 `gone` 从 `asyncio.wait` 的集合里去掉）在补这条之前一条测试都不红。

    `asyncio.all_tasks()` 必须在**跑 app 的那个事件循环里**数，所以经 TestClient 的
    portal 调进去。断开是异步的，给它 2 秒收尾（不是"等固定时长"：一旦回落就立即通过）。
    """

    def live_tasks() -> int:
        return client.portal.call(  # type: ignore[union-attr]  # 夹具已经进过 `with`
            lambda: len([task for task in asyncio.all_tasks() if not task.done()])
        )

    before = live_tasks()
    with client.websocket_connect("/ws/system", headers=ws_headers(client)) as socket:
        socket.receive_json()

    for _ in range(20):
        if live_tasks() <= before:
            return
        time.sleep(0.1)
    raise AssertionError(f"关掉页面之后服务端还多着 {live_tasks() - before} 个没结束的任务")


# =============================================================================
# 停机接线
# =============================================================================
class _RecordingPuller:
    """只记 `shutdown()` 被调过几次。替 lifespan 里那个真 puller 站位。"""

    def __init__(self) -> None:
        self.shutdowns = 0

    async def shutdown(self) -> None:
        self.shutdowns += 1


def test_lifespan_shuts_the_puller_down(app: FastAPI) -> None:
    """停机路径**必须**调 `image_puller.shutdown()`。

    删掉 `main.py` 里那一行，交付时一条测试都不红（收货 mutation 实测）——— 而后果是
    一个正在拉几 GB 镜像的工作线程活到事件循环关掉之后。
    "取消发生在 `shutdown()` 返回之前"那条**顺序**断言在 `test_image_puller.py` 里，
    这里只测接线（§十.4：同一条不变式不在两层各测一遍）。
    """
    recorder = _RecordingPuller()
    with TestClient(app, base_url="https://testserver") as client:
        client.app.state.image_puller = recorder
    assert recorder.shutdowns == 1


# =============================================================================
# POST /api/system/pull-image
# =============================================================================
def test_already_present_is_200_and_pulls_nothing(client: TestClient, settings: Settings) -> None:
    _puller, transport = install_puller(client, present=True)

    response = client.post("/api/system/pull-image")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "done"
    assert body["phase"] == ALREADY_PRESENT_PHASE
    assert body["epoch"] == 0
    assert all("/images/create" not in path for path in transport.paths())
    assert audit_lines(settings) == [], "什么都没发生，不许写审计"


def test_pull_is_accepted_once_and_audited(client: TestClient, settings: Settings) -> None:
    release = threading.Event()

    def blocking(_call: object, on_line: OnLine) -> int:
        on_line(
            {"status": "Downloading", "id": "aaa", "progressDetail": {"current": 1, "total": 9}}
        )
        release.wait(5)
        return 200

    _puller, transport = install_puller(client, stream=blocking)
    try:
        first = client.post("/api/system/pull-image")
        second = client.post("/api/system/pull-image")
    finally:
        release.set()

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["status"] == "pulling"
    assert first.json()["epoch"] == 1
    assert second.json()["epoch"] == 1, "第二次不许起第二次拉取"
    assert len([p for p in transport.paths() if "/images/create" in p]) == 1

    entries = audit_lines(settings)
    assert [entry["event"] for entry in entries] == [EVENT_IMAGE_PULL_STARTED]
    # detail 里只有镜像引用与 epoch。**一个凭据字段都不许有**（这个接口本来也碰不到
    # 凭据，断言在这里是为了让"以后有人往 detail 里加东西"必须先改这一行）。
    assert entries[0]["detail"] == {"reference": settings.strix_image, "epoch": 1}
