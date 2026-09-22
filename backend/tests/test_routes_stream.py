"""`WS /ws/scans/{scan_id}`（T16b）：回放 → 直播的接缝。

# 这个文件盯的是什么

**接缝上既不丢帧、也不重帧。** 两个半边：

1. **订阅先于回放查询** —— `test_subscribe_happens_before_the_replay_query`。它靠一个
   在 `subscribe()` 里往镜像插一行的替身 channel，把"谁先谁后"变成**可观察**的：
   订阅在前，那一行就会出现在回放里；订阅在后，它两边都不在。
2. **去重规则** —— 纯函数层那一组 `test_seam_*`（`should_forward` 无 IO，必须 TDD）。

路由层只测"接缝真的接上了"与那几个关闭码，**不重复测** `should_forward` 的分支。

另外还有两格是"少了它一条测试都不红"的：路径是 `/ws/scans/{id}` 这个确切字符串
（挂到 `prefix="/api/scans"` 的 router 上会静默变成 `/api/scans/ws/scans/{id}`），
以及关掉的页面必须不留下服务端任务。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse, WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from app.routes.stream import STREAM_LAGGED, should_forward
from app.services.event_replay import DEFAULT_LIMIT, ReplayCursor
from app.services.scan_channel import SUBSCRIBER_QUEUE_SIZE, Subscriber
from app.settings import Settings
from app.ws_envelope import Envelope
from tests.conftest import insert_authorization, insert_scan, writable_conn, ws_headers

ENVELOPE_KEYS = {"v", "epoch", "seq", "type", "ts", "payload"}

SCAN_ID = "scan-1"
PATH = f"/ws/scans/{SCAN_ID}"


def envelope(
    *,
    epoch: int,
    seq: int,
    frame_type: str = "event.add",
    payload: dict[str, object] | None = None,
) -> Envelope:
    return Envelope(
        epoch=epoch,
        seq=seq,
        type=frame_type,
        ts="2026-09-23T00:00:00.000Z",
        payload={} if payload is None else payload,
    )


# =============================================================================
# 去重规则（无 IO 纯判定函数）
# =============================================================================
# 「这一代里 seq ≤ 10 的全部**事件**，客户端都已经有了」。
SENT_THROUGH = ReplayCursor(epoch=3, seq=10)


def test_seam_drops_frames_from_a_stale_epoch() -> None:
    """陈旧代的残帧（队列里攒着的、重同步之前的帧）必须丢。

    放过去会让前端按"epoch 变了就丢掉本地状态"的规则把**更新**的状态清掉。
    """
    assert should_forward(envelope(epoch=2, seq=99), sent_through=SENT_THROUGH) is False


def test_seam_drops_event_frames_the_replay_already_covered() -> None:
    assert should_forward(envelope(epoch=3, seq=10), sent_through=SENT_THROUGH) is False
    assert (
        should_forward(
            envelope(epoch=3, seq=1, frame_type="event.update"), sent_through=SENT_THROUGH
        )
        is False
    )


def test_seam_forwards_new_event_frames_in_the_same_epoch() -> None:
    assert should_forward(envelope(epoch=3, seq=11), sent_through=SENT_THROUGH) is True


@pytest.mark.parametrize(
    "frame_type", ["agents", "vuln.add", "summary", "report", "log", "notice", "done"]
)
def test_seam_never_drops_non_event_frames(frame_type: str) -> None:
    """**只对事件帧去重**，别图省事写成"只发严格大于 (epoch, seq) 的帧"。

    回放只产 `event.add`，所以只有进过镜像的类型才可能与回放重叠。这七种从不进镜像 ——
    它们跟回放不可能撞车，丢掉就是**永久**丢掉。而 `notice(context_compacted)`
    （"早期对话已被摘要替代"）是**一次性**的，丢了前端永远不会再收到第二次。
    """
    got = should_forward(envelope(epoch=3, seq=1, frame_type=frame_type), sent_through=SENT_THROUGH)
    assert got is True


def test_seam_forwards_everything_in_a_newer_epoch() -> None:
    assert should_forward(envelope(epoch=4, seq=0), sent_through=SENT_THROUGH) is True


def test_seam_forwards_everything_when_nothing_was_replayed() -> None:
    """游标是 None = 一帧都没回放过 ⇒ 不存在重帧，一律发。"""
    assert should_forward(envelope(epoch=0, seq=0), sent_through=None) is True


# =============================================================================
# 路由（替身与助手）
# =============================================================================
@dataclass
class FakeChannel:
    """只管"交出一个预先塞好帧的队列"的替身 channel。

    帧在 `subscribe()` 里塞，而**不是**测试线程往队列里 put：`asyncio.Queue` 属于 app
    那个事件循环，从测试线程碰它就是制造数据竞争。`subscribe()` 正好在那个循环里跑。

    `on_subscribe` 是"订阅先于回放查询"那条测试的观察点。
    """

    staged: list[Envelope | None] = field(default_factory=list)
    dropped: bool = False
    on_subscribe: Callable[[], None] | None = None
    subscribed: list[Subscriber] = field(default_factory=list)
    unsubscribed: list[Subscriber] = field(default_factory=list)

    def subscribe(self) -> Subscriber:
        subscriber = Subscriber(
            queue=asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE), dropped=self.dropped
        )
        for frame in self.staged:
            subscriber.queue.put_nowait(frame)
        if self.on_subscribe is not None:
            self.on_subscribe()
        self.subscribed.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        self.unsubscribed.append(subscriber)


@dataclass
class FakeRegistry:
    """鸭子替身。`routes/stream._channels` 刻意不做 `isinstance`，就是为了让它进得来。"""

    channels: dict[str, FakeChannel] = field(default_factory=dict)

    def get(self, scan_id: str) -> FakeChannel | None:
        return self.channels.get(scan_id)

    async def shutdown(self) -> None:
        """lifespan 的 finally 会调它 —— 少了这个方法每个用例都会在拆夹具时炸。"""


def install_channel(
    client: TestClient,
    *,
    staged: list[Envelope | None] | None = None,
    dropped: bool = False,
    on_subscribe: Callable[[], None] | None = None,
) -> FakeChannel:
    """把 lifespan 建的那个真 registry 换成一个只有 `get()` 的替身。

    必须在 lifespan 跑完之后换（`client` 夹具进过 `with` 了）：在 `create_app` 之前往
    state 里塞会被 lifespan 原地盖掉（见 conftest 的 `app` 夹具 docstring）。
    """
    channel = FakeChannel(
        staged=[] if staged is None else staged, dropped=dropped, on_subscribe=on_subscribe
    )
    client.app.state.channels = FakeRegistry({SCAN_ID: channel})
    return channel


def seed_scan(settings: Settings) -> None:
    with writable_conn(settings) as conn:
        insert_authorization(conn)
        insert_scan(conn, scan_id=SCAN_ID)


def insert_event(conn: sqlite3.Connection, *, epoch: int, seq: int) -> None:
    """一行镜像。`version` / `fingerprint` 回放刻意不取，给占位值即可。"""
    conn.execute(
        "INSERT INTO scan_events"
        " (scan_id, epoch, seq, kind, agent_id, ts, version, fingerprint, data_json)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (
            SCAN_ID,
            epoch,
            seq,
            "tool_result",
            "agent-1",
            "2026-09-23T00:00:00.000Z",
            1,
            f"fp-{epoch}-{seq}",
            json.dumps({"key": f"tool_{seq}", "upstream_version": 1, "data": {"n": seq}}),
        ),
    )


def seed_events(settings: Settings, seqs: range, epoch: int = 1) -> None:
    with writable_conn(settings) as conn:
        for seq in seqs:
            insert_event(conn, epoch=epoch, seq=seq)


def drain(socket: WebSocketTestSession) -> tuple[list[dict[str, object]], int]:
    """读到服务端关闭为止，返回 (收到的帧, 关闭码)。"""
    frames: list[dict[str, object]] = []
    while True:
        try:
            frames.append(socket.receive_json())
        except WebSocketDisconnect as closed:
            return frames, closed.code


def stream(
    client: TestClient, hello: dict[str, object] | str
) -> tuple[list[dict[str, object]], int]:
    """连上、发 `hello`、读到关闭。"""
    with client.websocket_connect(PATH, headers=ws_headers(client)) as socket:
        socket.send_json(hello)
        return drain(socket)


# =============================================================================
# 路由（用例）
# =============================================================================
def test_anonymous_websocket_is_denied_on_handshake(anonymous: TestClient) -> None:
    try:
        with anonymous.websocket_connect(PATH):
            raise AssertionError(f"匿名居然连上了 {PATH}")
    except WebSocketDenialResponse as denial:
        assert denial.status_code == 401


def test_unknown_scan_gets_an_error_frame_and_a_normal_close(client: TestClient) -> None:
    """扫描不存在是一帧 `error{not_found}`，**不是**握手期的 404。

    WS 握手的状态码前端拿不到响应体，而"前端按码分支、不得匹配文案"是硬要求。
    `epoch=0, seq=0`：这条连接根本没有"数据代"可言。
    """
    frames, code = stream(client, {"type": "hello"})

    assert [frame["type"] for frame in frames] == ["error"]
    assert frames[0]["payload"] == {"code": "not_found"}
    assert (frames[0]["epoch"], frames[0]["seq"]) == (0, 0)
    assert set(frames[0]) == ENVELOPE_KEYS
    assert code == 1000


def test_history_is_replayed_and_then_the_socket_closes(
    client: TestClient, settings: Settings
) -> None:
    """没有活着的 channel（扫描早结束了／api 重启过）→ 只回放，回放完正常关。

    **刻意不编一个 `done` 帧**：`done` 只有 `ScanChannel.finish()` 会发，而结论的唯一
    出处是 `GET /api/scans/{id}`。给"跑到一半 api 重启"的扫描造一个 done 是假信号。
    """
    seed_scan(settings)
    seed_events(settings, range(3))

    frames, code = stream(client, {"type": "hello", "resume_from": None})

    assert [frame["seq"] for frame in frames] == [0, 1, 2]
    assert {frame["type"] for frame in frames} == {"event.add"}
    assert {frame["epoch"] for frame in frames} == {1}
    assert code == 1000


def test_replay_follows_next_cursor_across_pages(client: TestClient, settings: Settings) -> None:
    """一页 500 行，必须跟着 `next_cursor` 把后面的页也取完。"""
    seed_scan(settings)
    seed_events(settings, range(DEFAULT_LIMIT + 1))

    frames, code = stream(client, {"type": "hello"})

    assert len(frames) == DEFAULT_LIMIT + 1
    assert frames[-1]["seq"] == DEFAULT_LIMIT
    assert code == 1000


def test_resume_from_only_replays_what_the_client_is_missing(
    client: TestClient, settings: Settings
) -> None:
    seed_scan(settings)
    seed_events(settings, range(3))

    frames, _code = stream(client, {"type": "hello", "resume_from": {"epoch": 1, "seq": 1}})

    assert [frame["seq"] for frame in frames] == [2]


@pytest.mark.parametrize(
    "resume_from",
    [
        "epoch-1-seq-1",
        {"epoch": "1", "seq": 1},
        {"epoch": 1},
        {"epoch": True, "seq": True},
    ],
)
def test_a_malformed_hello_is_treated_as_a_fresh_connection(
    client: TestClient, settings: Settings, resume_from: object
) -> None:
    """宽容：解析不出来／类型不对／缺键／值不是整数 → 一律当作没要求续传，
    **不关连接、不报错**（首次连接与老版本前端都是这个形状）。
    """
    seed_scan(settings)
    seed_events(settings, range(3))

    frames, code = stream(client, {"type": "hello", "resume_from": resume_from})

    assert [frame["seq"] for frame in frames] == [0, 1, 2]
    assert code == 1000


def test_subscribe_happens_before_the_replay_query(client: TestClient, settings: Settings) -> None:
    """**订阅必须先于回放查询。**

    反过来的话，"回放查询已经读完"到"订阅生效"之间产出的事件帧**永久消失** ——
    它既没进回放的那一批，也没进队列。

    替身在 `subscribe()` 里往镜像插一行 seq=1：订阅在前，那一行就会被随后的回放取到；
    订阅在后，它两边都不在，本条测试少一帧。
    """
    seed_scan(settings)
    seed_events(settings, range(1))

    def write_one_more() -> None:
        with writable_conn(settings) as conn:
            insert_event(conn, epoch=1, seq=1)

    install_channel(client, staged=[None], on_subscribe=write_one_more)

    frames, code = stream(client, {"type": "hello"})

    assert [frame["seq"] for frame in frames] == [0, 1]
    assert code == 1000


def test_live_frames_follow_the_replay_and_done_ends_the_stream(
    client: TestClient, settings: Settings
) -> None:
    """接缝真的接上了：队列里那一批已经回放过的帧丢掉、新的发出去；拿到 `done` 就
    发完结束（**不再等第二帧**）。
    """
    seed_scan(settings)
    seed_events(settings, range(1))
    install_channel(
        client,
        staged=[
            envelope(epoch=1, seq=0),  # 回放已经给过它
            envelope(epoch=1, seq=1),
            envelope(epoch=1, seq=2, frame_type="done"),
            envelope(epoch=1, seq=3, frame_type="log"),  # done 之后的一律不发
            # 真 channel 在 `done` 之后一定会塞哨兵（`_close_subscribers`），这里照样给 ——
            # 不给的话"转发完 done 不结束"这个改动会让本条用例**挂死**而不是变红。
            None,
        ],
    )

    frames, code = stream(client, {"type": "hello"})

    assert [(frame["type"], frame["seq"]) for frame in frames] == [
        ("event.add", 0),
        ("event.add", 1),
        ("done", 2),
    ]
    assert code == 1000


def test_a_client_cursor_never_blacks_out_the_live_stream(
    client: TestClient, settings: Settings
) -> None:
    """回放一帧都没给的时候，**客户端自报的游标不许拿来去重**。

    去重的唯一判据是"这条连接的回放**实际**交出去了什么"：没回放过任何东西，就没有任何
    帧可能是重帧。拿客户端自报的 `(epoch, seq)` 当判据的话，一个 epoch 比镜像还大的游标
    （老版本前端、被改过的前端、或者 api 重启前留下的那个 —— `ProjectorState.initial()`
    是 `epoch=0`，重启后新 channel 从 0 重新发）会让规则①把之后**每一帧**都当成"陈旧代"
    丢掉：客户端看到的是一条永远不出帧、也永远不报错的流。那是这个功能最坏的失败形态。
    """
    seed_scan(settings)  # 镜像里一行都没有 → 回放什么也交不出来
    install_channel(client, staged=[envelope(epoch=1, seq=0), None])

    frames, code = stream(client, {"type": "hello", "resume_from": {"epoch": 3, "seq": 9}})

    assert [(frame["type"], frame["seq"]) for frame in frames] == [("event.add", 0)]
    assert code == 1000


def test_a_dropped_subscriber_gets_stream_lagged_and_1011(
    client: TestClient, settings: Settings
) -> None:
    """跟不上被 channel 摘掉 → `error{stream_lagged}` + 1011。前端据此带
    `resume_from` 重连；跟"正常收尾"混起来就是静默丢掉一整段。
    """
    seed_scan(settings)
    install_channel(client, staged=[None], dropped=True)

    frames, code = stream(client, {"type": "hello"})

    assert [frame["type"] for frame in frames] == ["error"]
    assert frames[0]["payload"] == {"code": STREAM_LAGGED}
    assert code == 1011


def test_a_normal_sentinel_closes_cleanly_and_releases_the_subscription(
    client: TestClient, settings: Settings
) -> None:
    """哨兵 + `dropped is False` = channel 正常收尾，不是掉队。

    并且 `finally` 里必须 `unsubscribe`：漏了它 channel 就攒一个永远没人读的队列，
    攒满 256 帧之后还会去"摘"一个早就走了的订阅者。
    """
    seed_scan(settings)
    channel = install_channel(client, staged=[None])

    frames, code = stream(client, {"type": "hello"})

    assert frames == []
    assert code == 1000
    assert channel.unsubscribed == channel.subscribed != []


def test_a_closed_page_leaves_no_task_behind(client: TestClient, settings: Settings) -> None:
    """页面关掉之后，端点必须**自己**结束。

    `hello` 之后我们再也不读客户端消息，所以"断开了"只能靠那个专门的 receive 任务
    （`gone`）看见。少了它，每关一个页面就在服务端留一个永远等在 `queue.get()` 上的
    任务，而且每个都握着一个 WebSocket。
    先收一帧回放：那说明服务端已经走过 `hello`、正挂在队列上（替身刻意不给哨兵）——
    不然断开会被 `hello` 那个 `receive` 撞上，这条测试就测不到 `gone` 了。

    **观察点是 `unsubscribe` 而不是 `asyncio.all_tasks()` 的条数**：那个条数会被
    lifespan 里的三个后台任务（sweeper / reaper / retention）搅得忽上忽下，而
    `unsubscribe` 只有端点真的走到 generator 的 `finally` 才会发生 —— 它就是
    "那个任务结束了"的直接证据，且是确定性的。

    `close()` 与等待都在 `with` **里面**：`WebSocketTestSession.__exit__` 先关连接、
    紧接着就 cancel 那个跑 app 的任务，中间不给服务端喘息的机会 —— 端点还没收尾的话
    那次 cancel 会以 `CancelledError` 的形式变成用例失败，而那跟本条测试要问的
    "它会不会自己结束"是两件事。断开是异步的，给它 2 秒（一旦收尾就立即通过）。
    """
    seed_scan(settings)
    seed_events(settings, range(1))
    channel = install_channel(client, staged=[])

    with client.websocket_connect(PATH, headers=ws_headers(client)) as socket:
        socket.send_json({"type": "hello"})
        assert socket.receive_json()["seq"] == 0
        socket.close(1000)
        for _ in range(20):
            if channel.unsubscribed:
                return
            time.sleep(0.1)
    raise AssertionError("关掉页面之后端点没有收尾 —— 它还挂在 queue.get() 上")
