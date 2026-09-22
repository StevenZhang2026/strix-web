"""`WS /ws/scans/{scan_id}`：扫描帧的出口（T16b）。

先把已经落进镜像的历史帧回放给客户端，然后转成直播。

# 唯一的不变式：回放与直播的接缝上，既不丢帧、也不重帧

它有两个半边，缺一个就不成立：

1. **订阅必须先于回放查询。** 先 `subscribe()` 拿到队列，**再**查库回放。反过来的话，
   「回放查询已经读完」到「订阅生效」之间产出的事件帧**永久消失** —— 它既没进回放的
   那一批，也没进队列。（反之，订阅早于回放只会造成重帧，而重帧是能去掉的 —— 那是②。）
2. **去重规则 `should_forward`。** 订阅早于回放 ⇒ 队列里会有一批已经被回放过的帧，
   必须丢掉；但**不能一刀切**，理由写在那个函数上。

# 形状：与传输无关的 async generator + 一层薄传输

产帧是 `stream_frames`，WS 端点只做传输。T16c（SSE 兜底）复用**同一个** generator、
只换传输层；这也是把接缝去重逻辑单独测住的形状。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Final

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.requests import HTTPConnection

from app.db import Database
from app.services.event_replay import ReplayCursor, replay_batch
from app.services.scan_channel import ChannelRegistry, Subscriber
from app.ws_envelope import Envelope, now_ts

# **绝不许挂到 `prefix="/api/scans"` 的那个 router 上** —— 会静默变成
# `/api/scans/ws/scans/{id}`：不报错、不告警，只是前端永远连不上。全部 WS 路径在
# nginx 的 `location /ws/` 下（四条 upgrade 指令在那里），所以它们必须真的以 `/ws/`
# 开头。`test_routes_stream.py` 里有一条测试连的就是 `/ws/scans/{id}` 这个确切字符串。
ws_router = APIRouter(tags=["scans"])

STREAM_LAGGED: Final = "stream_lagged"
"""客户端跟不上、已经被 channel 摘掉。

刻意**不进** `app/errors.py`：那个模块只有两类码（会被 raise 的 HTTP 错误、写进
`scans.error_code` 的扫描归因码），而这是一个 WS 帧里的码，两边都不是。本仓对 WS 帧码
已有先例 —— `services/run_projector.py` 的三个 `NOTICE_*` 常量。中文文案随 T17 那个
面板一起进 `zh-CN.json`。
"""

_NOT_FOUND: Final = "not_found"
"""它同时也是一个 HTTP 码，但这里发的是帧不是响应，所以直接用字面量 ——
`import NotFoundError` 再取 `.code` 会让人以为这条路径会抛 HTTP 异常。"""

_FRAME_ERROR: Final = "error"
_FRAME_DONE: Final = "done"

EVENT_FRAME_TYPES: Final = frozenset({"event.add", "event.update"})
"""**只有这两种帧进镜像**（`services/scan_frames.py` 的全部 type 里）。
`should_forward` 的规则 2 只对它们生效。"""

_CLOSE_NORMAL: Final = 1000
_CLOSE_INTERNAL_ERROR: Final = 1011


class ScanNotFound(Exception):
    """库里没有这个 scan_id。

    `replay_batch` 对未知 scan_id 不抛异常 ——「这个扫描存不存在」由路由层查 `scans`
    表回答，而这个模块就是那个路由层。
    """


class SubscriberLagged(Exception):
    """这个客户端跟不上，channel 已经把它摘掉了。"""


def should_forward(envelope: Envelope, *, sent_through: ReplayCursor | None) -> bool:
    """这一帧该不该发给客户端。无 IO 纯判定函数。

    `sent_through` 是**这条连接的回放实际交出去的最后一帧**（`None` = 一帧都没回放过）。
    它刻意**不是**客户端自报的 `resume_from`：重帧只可能来自「订阅早于回放查询」那个窗口，
    而那个窗口的范围只由回放自己决定。拿客户端自报的游标当判据，一个 epoch 比镜像还大的
    值会让下面的规则①把之后每一帧都当成陈旧代丢掉 —— 一条永不出帧也不报错的流。

    ⚠️ 规则 2 的「只对事件帧」是必须的，**别图省事写成「只发严格大于 `(epoch, seq)` 的
    帧」**：回放只产 `event.add`（`event_replay._frame` 的 `type` 恒为它），所以只有进过
    镜像的类型才可能与回放重叠。`summary` / `log` / `notice` / `agents` / `vuln.add` /
    `report` / `done` 从不进镜像 —— 它们跟回放不可能撞车，丢掉就是**永久**丢掉。而
    `notice(context_compacted)`（「早期对话已被摘要替代」）是**一次性**的，丢了前端永远
    不会再收到第二次。
    """
    if sent_through is None:
        return True
    if envelope.epoch < sent_through.epoch:
        # 陈旧代的残帧（队列里攒着的、重同步之前的帧）。放过去会让前端按「epoch 变了
        # 就丢掉本地状态」的规则把**更新**的状态清掉。
        return False
    if envelope.epoch > sent_through.epoch:
        return True
    if envelope.type not in EVENT_FRAME_TYPES:
        return True
    return envelope.seq > sent_through.seq


def _error_frame(code: str) -> Envelope:
    """一帧 `error`。

    `epoch=0, seq=0`：这两种错误（扫描不存在、掉队）都不属于任何一「代」数据 ——
    seq 的号是 channel 的发号器发的，路由自己编一个号是撒谎。
    """
    return Envelope(epoch=0, seq=0, type=_FRAME_ERROR, ts=now_ts(), payload={"code": code})


async def _scan_exists(db: Database, scan_id: str) -> bool:
    def query(conn: sqlite3.Connection) -> bool:
        row = conn.execute("SELECT 1 FROM scans WHERE id = ?", (scan_id,)).fetchone()
        return row is not None

    return await db.run(query)


async def stream_frames(
    *,
    scan_id: str,
    db: Database,
    channels: ChannelRegistry,
    resume_from: ReplayCursor | None,
) -> AsyncIterator[Envelope]:
    """历史帧 + 直播帧。与传输无关 —— 见模块 docstring。

    `ScanNotFound` / `SubscriberLagged` 由调用方翻成各自的帧与关闭码。
    """
    if not await _scan_exists(db, scan_id):
        raise ScanNotFound(scan_id)

    channel = channels.get(scan_id)
    # ⚠️ **订阅必须先于下面那个回放查询**（模块 docstring 的不变式①）。
    subscriber: Subscriber | None = None if channel is None else channel.subscribe()
    try:
        # ⚠️ 去重游标从 `None` 起，**不是** `resume_from`：客户端自报的游标说明不了
        # 「这条连接的回放交出去了什么」，而重帧只可能来自「订阅早于回放查询」那个窗口。
        # 用它当判据的话，一个 epoch 比镜像还大的值（老前端／api 重启前留下的那个）会让
        # `should_forward` 的规则①把之后每一帧都丢掉 —— 一条永不出帧也不报错的流。
        sent_through: ReplayCursor | None = None
        cursor = resume_from
        while True:
            # `ts` 每批都传一次新的 `now_ts()`：信封 `ts` 的语义是「这一帧什么时候发出去
            # 的」，事件自己的时间在 `payload.ts` 里。
            batch = await replay_batch(db, scan_id, resume_from=cursor, ts=now_ts())
            for frame in batch.frames:
                yield frame
            if batch.frames:
                sent_through = ReplayCursor(epoch=batch.epoch, seq=batch.frames[-1].seq)
            if batch.next_cursor is None:
                break
            cursor = batch.next_cursor

        if subscriber is None:
            # `channels.get()` 返回 None 有两种真实情形：扫描早就结束了；或 api 重启过
            # （`ChannelRegistry` 是进程内的）。两种都一样：只回放，回放完就结束。
            # ⚠️ **不许自己编一个 `done` 帧** —— `done` 只有 `ScanChannel.finish()` 会发，
            # 而结论（状态／归因／漏洞数）的唯一出处是 `GET /api/scans/{id}`。给「跑到
            # 一半 api 重启」的扫描造一个 done 是假信号。
            return

        while True:
            envelope = await subscriber.queue.get()
            if envelope is None:
                # 哨兵。**必须看 `dropped`**：被摘掉的客户端要带 `resume_from` 从镜像
                # 补齐，正常收尾的什么都不用做 —— 两者混起来就是静默丢掉一整段。
                if subscriber.dropped:
                    raise SubscriberLagged(scan_id)
                return
            # 游标**一直不动**：它的语义是「回放交出去了什么」，而那是一个已经定下来的
            # 事实。同一代里 seq 由 channel 的单个发号器单调发出，所以回放之后到来的事件
            # 帧 seq 必然大于它 —— 推进它对任何可达状态都没有影响（实测：把推进删掉，
            # 26 条测试一条都不红）。别再加回来。
            if should_forward(envelope, sent_through=sent_through):
                yield envelope
            if envelope.type == _FRAME_DONE:
                # 发完就结束，不再等第二帧。
                return
    finally:
        # 漏了它 = channel 攒一个永远没人读的队列，攒满 256 帧后还会去「摘」一个早就
        # 走了的订阅者。
        if channel is not None and subscriber is not None:
            channel.unsubscribe(subscriber)


def _db(connection: HTTPConnection) -> Database:
    """照 `routes/scans.py` 原样抄（那边是第二份，这是第三份）。

    提取归属地是 `routes/_context.py`，但那要改三个既有文件、把闸门面撑宽 ——
    这笔债记在 `PLAN.md` 里，留给下一条本来就要碰那几个文件的任务。
    参数类型是 `HTTPConnection` 而不是 `Request`：`WebSocket` 也是它的子类。
    """
    db = getattr(connection.app.state, "db", None)
    if not isinstance(db, Database):
        raise RuntimeError(
            "app.state.db 不存在。本接口要求 lifespan 已经跑过 —— "
            "测试里请用 `with TestClient(app):`。"
        )
    return db


def _channels(connection: HTTPConnection) -> ChannelRegistry:
    """同 `_db`。

    ⚠️ 刻意不做 `isinstance`（照 `routes/scans.py`）：真的 registry 会起轮询任务、经
    `EventMirror` 写库，而单测挂的是只有 `get()` 的鸭子替身 —— 加了 `isinstance`
    替身就进不来。
    """
    registry: ChannelRegistry | None = getattr(connection.app.state, "channels", None)
    if registry is None:
        raise RuntimeError(
            "app.state.channels 不存在。本接口要求 lifespan 已经跑过 —— "
            "测试里请用 `with TestClient(app):`。"
        )
    return registry


def _as_int(value: object) -> int | None:
    # `isinstance(True, int)` 为真，但 `{"epoch": true}` 显然不是一个代号。
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parse_resume_from(hello: object) -> ReplayCursor | None:
    """`{"type":"hello","resume_from":{"epoch":3,"seq":41}}` → 游标。

    **宽容**：解析不出来 / 类型不对 / 缺键 / 值不是整数 → 一律当作 `None`，不关连接、
    不报错。首次连接与老版本前端都是这个形状。
    """
    if not isinstance(hello, dict):
        return None
    raw = hello.get("resume_from")
    if not isinstance(raw, dict):
        return None
    epoch = _as_int(raw.get("epoch"))
    seq = _as_int(raw.get("seq"))
    if epoch is None or seq is None:
        return None
    return ReplayCursor(epoch=epoch, seq=seq)


async def _wait_until_gone(websocket: WebSocket) -> None:
    """等到客户端断开。**客户端发来的消息一律丢掉** —— `hello` 之后这条流是单向的。

    （从 `routes/system.py` 的同名私有函数抄来。跨 route 模块 import 一个下划线私有
    函数比抄这 5 行更糟。）
    """
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


@ws_router.websocket("/ws/scans/{scan_id}")
async def scan_stream(websocket: WebSocket, scan_id: str) -> None:
    """一条扫描的帧：历史回放 + 直播。

    鉴权由 app 级依赖 `require_session` 完成（含 Origin 校验），**本文件不做也不许做**
    任何鉴权判断，更不许往 `EXEMPT_PATHS` 加路径。匿名握手会拿到一个真的 401。

    扫描不存在时发一帧 `error{not_found}` 而**不是**在握手期返回 404：WS 握手的状态码
    前端拿不到响应体，而「前端按码分支、不得匹配文案」是硬要求（CLAUDE.md §错误与文案）。

    `hello` **不设超时**：客户端不发就只是闲着，服务端只挂着一个 `receive`，没有泄漏。
    """
    db = _db(websocket)
    channels = _channels(websocket)
    await websocket.accept()

    try:
        hello: object = await websocket.receive_json()
    except WebSocketDisconnect:
        return
    except (json.JSONDecodeError, KeyError):
        # 不是 JSON（KeyError：连发的都不是文本帧）。按宽容规则当作没要求续传。
        hello = None
    resume_from = _parse_resume_from(hello)

    # 我们从 `hello` 之后就不读客户端消息了，所以「页面关了」只能靠这个专门的 receive
    # 任务看见 —— 少了它，每一个关掉的页面都在服务端留下一个永远等在 `queue.get()`
    # 上的任务（`/ws/system` 的注释就是为这件事写的）。
    gone = asyncio.create_task(_wait_until_gone(websocket))
    frames = stream_frames(scan_id=scan_id, db=db, channels=channels, resume_from=resume_from)
    try:
        while True:
            pull = asyncio.create_task(frames.__anext__())
            done, _pending = await asyncio.wait({pull, gone}, return_when=asyncio.FIRST_COMPLETED)
            if gone in done:
                pull.cancel()
                # ⚠️ cancel 之后**必须 await 它一次**再走到 `frames.aclose()`：不等的话
                # generator 可能还停在「正在运行」的状态里，`aclose()` 会抛
                # `RuntimeError: aclose(): asynchronous generator is already running`。
                await asyncio.gather(pull, return_exceptions=True)
                return
            try:
                envelope = pull.result()
            except StopAsyncIteration:
                break
            await websocket.send_json(envelope.model_dump())
        await websocket.close(_CLOSE_NORMAL)
    except ScanNotFound:
        await websocket.send_json(_error_frame(_NOT_FOUND).model_dump())
        await websocket.close(_CLOSE_NORMAL)
    except SubscriberLagged:
        # best-effort：socket 此刻可能已经不行了，而 1011 比那一帧更重要（前端据此带
        # `resume_from` 重连）。
        with suppress(WebSocketDisconnect, RuntimeError):
            await websocket.send_json(_error_frame(STREAM_LAGGED).model_dump())
        with suppress(WebSocketDisconnect, RuntimeError):
            await websocket.close(_CLOSE_INTERNAL_ERROR)
    except WebSocketDisconnect:
        return
    finally:
        gone.cancel()
        with suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
            await gone
        # ⚠️ 显式 `aclose()`，**不依赖 GC** —— generator 的 `finally` 里有 `unsubscribe`。
        await frames.aclose()
