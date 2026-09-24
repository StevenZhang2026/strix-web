"""扫描 channel 的轮询循环（W2b：`app/services/scan_channel.py`）。

# 这个文件盯的四条不变式

1. **慢订阅者被摘掉，且不拖住轮询循环**
   （`test_full_queue_subscriber_is_dropped_and_the_loop_keeps_going`）：队列满 → 摘掉它
   并标记，**绝不** `await queue.put` —— 一个卡在后台标签页里的 WS 客户端不许让整条扫描
   的实时流停摆，也不许让别的订阅者漏帧。被摘掉与正常结束必须能分开：前者要客户端带
   `resume_from` 从镜像补齐。
2. **镜像写失败不被吞**（`test_mirror_write_failure_kills_the_channel`）：
   `EventMirror.append` 抛出来就必须一路冒泡把 channel 任务打死。只追加的真源出洞比
   断流更糟（回放会永久缺那一帧），而扫描本身与 `scans` 终态由 `routes/scans.py` 的
   `_run_to_completion` 兜着，不靠 channel。
3. **终态或停机之后 channel 一定被关掉、不泄漏 asyncio 任务**（W2c，`ChannelRegistry`
   那一节的六条）：`close` 幂等、`shutdown` 一个不剩、即使 channel 已经死在镜像写上，
   它的轮询任务也必须被回收。接线层那一半（谁在什么时候调 close）在
   `test_routes_scans.py`。
4. **每一轮都真的落了库，而落库失败同样不被吞**（W3，
   `test_every_tick_persists_the_projection` / `test_persist_failure_kills_the_channel`）：
   四张表是回放与报告的真源，删掉 `_tick` 里那句 `record` 必须有测试变红。落库**本身**的
   正确性（计数从自己的行上数、首见即插）在 `test_scan_persist.py`，这里只测接线。

# 为什么用 `asyncio.run` 而不是 `async def test_`

同 `test_image_puller.py`：本仓刻意没有 `pytest-asyncio`。循环里的"时间"靠构造参数注入的
`sleep`（下面的 `Sleeper`）拨，所以一次真实的 250ms 都不会睡；`Sleeper` 也是唯一能把
`run_forever` 拆出来的出口（`finish()` 那条出口有它自己的测试）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from app.services import scan_channel as channel_module
from app.services.event_mirror import MirroredEvent
from app.services.run_projector import ProjectedEvent, RunSnapshot
from app.services.scan_channel import (
    BASE_INTERVAL_S,
    MAX_INTERVAL_S,
    SUBSCRIBER_QUEUE_SIZE,
    ChannelRegistry,
    ScanChannel,
    Subscriber,
)
from app.services.scan_frames import FrameState
from app.strix_profile import StrixProfile, profile_for
from app.ws_envelope import Envelope
from tests.conftest import make_run_dir

MEDIA_URL = "/api/scans/scan-1/media/deadbeef.png"


class StopLoop(Exception):
    """把 `run_forever` 从注入的 `sleep` 里拆出来。

    刻意不用 `CancelledError`：那个会被 `asyncio.run` 当成"外部取消了这次运行"，
    而我们要的是一个普通异常，`pytest.raises` 能明确地断言它。
    """


@dataclass
class Sleeper:
    """注入的 `sleep`：记下每次被要求睡多久，第 `limit` 次把循环拆出来。

    `on_sleep` 在两次 tick 之间跑 —— 它就是"Strix 又往 run 目录写了一笔"那个外部事件，
    没有它 stat 门会（正确地）把后面每一轮都整轮跳过。
    """

    limit: int
    on_sleep: Callable[[int], None] | None = None
    seen: list[float] = field(default_factory=list)

    async def __call__(self, seconds: float) -> None:
        self.seen.append(seconds)
        if self.on_sleep is not None:
            self.on_sleep(len(self.seen))
        if len(self.seen) >= self.limit:
            raise StopLoop


@dataclass
class FakeMirror:
    """镜像替身。两件真镜像会做、而本层必须跟着做对的事：

    ① `append` 把事件里内联的 base64 PNG 换成 media URL —— 所以帧的 payload 只能用
       **返回值**算；
    ② 写失败就抛 —— 真库很难按需抛，所以这里给一个 `error`（I2 只能这么测）。
    """

    error: Exception | None = None
    calls: list[tuple[int, int, str]] = field(default_factory=list)

    async def append(self, *, epoch: int, seq: int, event: ProjectedEvent) -> MirroredEvent:
        if self.error is not None:
            raise self.error
        self.calls.append((epoch, seq, event.key))
        rewritten = replace(event, data={**event.data, "screenshot": MEDIA_URL})
        return MirroredEvent(seq=seq, event=rewritten, new_media=(), skipped=())


@dataclass
class FakePersist:
    """落库替身。记下每一轮的 `(epoch, snapshot, at)`；`error` 是 I3 唯一的测法
    （真库很难按需抛）。落库**内容**对不对是 `test_scan_persist.py` 的事。
    """

    error: Exception | None = None
    calls: list[tuple[int, RunSnapshot, str]] = field(default_factory=list)

    async def record(self, *, snapshot: RunSnapshot, epoch: int, at: str) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append((epoch, snapshot, at))


def an_event(key: str = "tool_1", **overrides: object) -> ProjectedEvent:
    fields: dict[str, object] = {
        "key": key,
        "kind": "tool",
        "agent_id": "agent-1",
        "ts": "2026-09-20T00:00:00.000Z",
        "upstream_version": 1,
        "fingerprint": f"fp-{key}",
        "data": {"screenshot": "data:image/png;base64,AAAA"},
    }
    fields.update(overrides)
    return ProjectedEvent(**fields)  # type: ignore[arg-type]


def a_snapshot(**overrides: object) -> RunSnapshot:
    fields: dict[str, object] = {
        "agents": (),
        "events": (),
        "run_status": "running",
        "finished": False,
        "cost_usd": None,
        "severity": {},
        "vulnerabilities": (),
        "report_markdown": "",
    }
    fields.update(overrides)
    return RunSnapshot(**fields)  # type: ignore[arg-type]


@dataclass
class Harness:
    channel: ScanChannel
    run_dir: Path
    mirror: FakeMirror
    persist: FakePersist
    sleeper: Sleeper
    reads: list[Path]
    """`read_run_dir` 的每一次调用 —— stat 门"整轮跳过"就是看这个列表没长。"""


def make_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    snapshots: list[RunSnapshot] | None = None,
    sleeper: Sleeper | None = None,
    mirror: FakeMirror | None = None,
    persist: FakePersist | None = None,
    with_run_dir: bool = True,
    start_epoch: int = 0,
) -> Harness:
    """一个只对着替身说话的 channel。

    `discover_run` 与 stat 门走**真** tmp_path：它们要测的正是那几个 syscall 的结果。
    只有 `read_run_dir` 换成替身 —— 真的那个要 `strix` 的 `TuiLiveView` 去解析
    `agents.db`，那是 W2a／T13 的测试范围，在这里只会让意图变模糊。
    """
    queued = list(snapshots or [a_snapshot()])
    reads: list[Path] = []

    def fake_read_run_dir(run_dir: Path, profile: StrixProfile) -> RunSnapshot:
        reads.append(run_dir)
        # 最后一份留着重复给：被 stat 门跳过的轮次不消耗清单。
        return queued.pop(0) if len(queued) > 1 else queued[0]

    monkeypatch.setattr(channel_module, "read_run_dir", fake_read_run_dir)
    run_dir = make_run_dir(tmp_path, status="running") if with_run_dir else tmp_path / "missing"
    the_mirror = mirror if mirror is not None else FakeMirror()
    the_persist = persist if persist is not None else FakePersist()
    the_sleeper = sleeper if sleeper is not None else Sleeper(limit=1)
    channel = ScanChannel(
        scan_id="scan-1",
        cwd=tmp_path,
        profile=profile_for("1.6.2"),
        mirror=the_mirror,  # type: ignore[arg-type]
        persist=the_persist,  # type: ignore[arg-type]
        redact=lambda text: text,
        sleep=the_sleeper,
        start_epoch=start_epoch,
    )
    return Harness(
        channel=channel,
        run_dir=run_dir,
        mirror=the_mirror,
        persist=the_persist,
        sleeper=the_sleeper,
        reads=reads,
    )


def bump(run_dir: Path, marker: str) -> None:
    """让 stat 门看见变化。改 **size** 而不是指望 mtime：mtime 的分辨率不是我们能保证的。"""
    (run_dir / "run.json").write_text(
        json.dumps({"status": "running", "bump": marker}), encoding="utf-8"
    )


def drive(channel: ScanChannel) -> None:
    """跑循环，直到注入的 `sleep` 把它拆出来。"""
    with pytest.raises(StopLoop):
        asyncio.run(channel.run_forever())


def drain(subscriber: Subscriber) -> list[Envelope]:
    """取出已经到货的帧。`None` 是"没有更多帧了"的哨兵 —— 到它就停。"""
    frames: list[Envelope] = []
    while not subscriber.queue.empty():
        item = subscriber.queue.get_nowait()
        if item is None:
            break
        frames.append(item)
    return frames


def filler_frame() -> Envelope:
    return Envelope(epoch=0, seq=0, type="log", ts="2026-09-20T00:00:00.000Z", payload={})


# =============================================================================
# 1. I1 —— 慢订阅者被摘掉，且不拖住轮询循环
# =============================================================================
def test_full_queue_subscriber_is_dropped_and_the_loop_keeps_going(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_channel(
        tmp_path,
        monkeypatch,
        snapshots=[a_snapshot(), a_snapshot(cost_usd=1.0)],
        sleeper=Sleeper(limit=2),
    )
    h.sleeper.on_sleep = lambda n: bump(h.run_dir, f"bump-{n}")
    slow = h.channel.subscribe()
    healthy = h.channel.subscribe()
    # 塞满慢订阅者的队列：它就是那个"页面在后台标签页里、久久不读"的客户端。
    for _ in range(SUBSCRIBER_QUEUE_SIZE):
        slow.queue.put_nowait(filler_frame())

    drive(h.channel)

    # ① 被摘掉并**标记**：客户端靠这一位决定"带 resume_from 重连"而不是"正常收尾"。
    assert slow.dropped is True
    # ② 读端不会永远挂在 `get()` 上：队列被腾空，只剩一个哨兵。
    assert slow.queue.qsize() == 1
    assert slow.queue.get_nowait() is None
    # ③ 别的订阅者**一帧都不许漏，尤其是"摘掉"动作发生的那一帧**（这里是 `agents`）：
    #    扇出时边迭代边从列表里摘，会让紧跟其后的订阅者被跳过，而且**只跳那一帧** ——
    #    所以只数 `summary` 的话这个 bug 是隐形的（2026-09-21 收货 MX5 实测 0 红）。
    types = [frame.type for frame in drain(healthy)]
    assert types == ["agents", "summary", "summary"], f"健康订阅者漏帧了：{types}"
    # ④ 循环真的转到了第二轮，没被那个满队列卡住。
    assert len(h.reads) == 2, "循环被那个满队列卡住了"


# =============================================================================
# 2. I2 —— 镜像写失败不被吞
# =============================================================================
def test_mirror_write_failure_kills_the_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boom = OSError("media 目录写不进去")
    h = make_channel(
        tmp_path,
        monkeypatch,
        snapshots=[a_snapshot(events=(an_event(),))],
        sleeper=Sleeper(limit=5),
        mirror=FakeMirror(error=boom),
    )

    with pytest.raises(OSError, match="media 目录写不进去"):
        asyncio.run(h.channel.run_forever())

    # 中途抛异常不许留下**半更新**的状态：留了的话，重跑那一轮会把已经发过的帧
    # 当成"推过了"而永远不再发。访问私有字段是刻意的（同 conftest 的 `conn` 夹具）。
    assert h.channel._frames == FrameState.empty()
    assert h.channel._gate is None


# =============================================================================
# 2b. I3 —— 每一轮都落库，落库失败不被吞
# =============================================================================
def test_every_tick_persists_the_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删掉 `_tick` 里那句 `record` 就没有任何人写 `scan_agents` / `scan_findings` /
    `scans` 的计数列了 —— 而那是 T16 回放与 T21 报告的唯一真源。"""
    snapshots = [a_snapshot(), a_snapshot(cost_usd=4.0)]
    h = make_channel(tmp_path, monkeypatch, snapshots=snapshots, sleeper=Sleeper(limit=2))
    h.sleeper.on_sleep = lambda n: bump(h.run_dir, f"bump-{n}")
    sub = h.channel.subscribe()

    drive(h.channel)

    assert [call[0] for call in h.persist.calls] == [0, 0], "没有每一轮都落库"
    # 落库的 epoch 必须与帧的 epoch 是同一个（重同步那一轮它俩一起 +1）。
    assert {frame.epoch for frame in drain(sub)} == {0}
    # 拿到的是这一轮真正读出来的那份投影，不是上一轮的。
    assert [call[1] for call in h.persist.calls] == snapshots
    assert all(call[2].endswith("Z") for call in h.persist.calls)


def test_persist_failure_kills_the_channel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同 I2：库里少一批行比断流更糟，所以不许 `try/except` 记个日志继续。"""
    h = make_channel(
        tmp_path,
        monkeypatch,
        sleeper=Sleeper(limit=5),
        persist=FakePersist(error=RuntimeError("scans 表写不进去")),
    )

    with pytest.raises(RuntimeError, match="scans 表写不进去"):
        asyncio.run(h.channel.run_forever())

    # 与 I2 同样不许留下半更新的状态：留了的话那一轮永远不会被补。
    assert h.channel._gate is None


# =============================================================================
# 2c. 起始 epoch —— 续跑不撞 `scan_events` 的 `(scan_id, epoch, seq)` 主键
# =============================================================================
def test_the_first_frame_starts_at_the_given_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """续跑的新 channel 把全部历史在**新** epoch 下重推；若首帧仍落在 epoch 0，
    它的 seq 0 必撞上一轮的那一行。帧、镜像行、落库三处必须是同一个 epoch。"""
    h = make_channel(
        tmp_path,
        monkeypatch,
        snapshots=[a_snapshot(events=(an_event(),))],
        start_epoch=3,
    )
    sub = h.channel.subscribe()

    drive(h.channel)

    frames = drain(sub)
    assert (frames[0].epoch, frames[0].seq) == (3, 0)
    assert {frame.epoch for frame in frames} == {3}
    assert [call[0] for call in h.mirror.calls] == [3]
    assert [call[0] for call in h.persist.calls] == [3]


# =============================================================================
# 3. stat 门：四个文件都没变就整轮跳过
# =============================================================================
def test_stat_gate_skips_the_whole_tick_when_the_four_files_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_channel(tmp_path, monkeypatch, sleeper=Sleeper(limit=3))

    drive(h.channel)

    assert len(h.reads) == 1, "stat 门没挡住：run 目录一个字节都没变，却又读了一遍"


# =============================================================================
# 4. 事件帧的 payload 来自 `append` 的返回值
# =============================================================================
def test_event_frame_payload_comes_from_the_mirror_not_the_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_channel(
        tmp_path,
        monkeypatch,
        snapshots=[a_snapshot(events=(an_event(),))],
        sleeper=Sleeper(limit=1),
    )
    sub = h.channel.subscribe()

    drive(h.channel)

    events = [frame for frame in drain(sub) if frame.type == "event.add"]
    assert len(events) == 1
    # 顺序反了这里就是那串 base64（帧巨大，而且与回放出来的帧不一致）。
    assert events[0].payload["data"] == {"screenshot": MEDIA_URL}
    # 帧与镜像行必须同一个 seq，否则回放对不上。
    assert h.mirror.calls == [(0, events[0].seq, "tool_1")]


# =============================================================================
# 5. finish()：最后一次 tick + 唯一一个 done
# =============================================================================
def test_finish_does_a_last_tick_then_exactly_one_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_channel(tmp_path, monkeypatch)
    sub = h.channel.subscribe()

    async def scenario() -> None:
        await h.channel.finish()
        await h.channel.finish()

    asyncio.run(scenario())

    frames = drain(sub)
    assert [frame.type for frame in frames] == ["agents", "summary", "done"]
    # `done` 有自己的 seq（前端靠 seq 判漏帧，复用上一帧的号等于少了一帧）。
    assert [frame.seq for frame in frames] == [0, 1, 2]
    assert len(h.reads) == 1, "finish() 要做最后一次 tick，但第二次调用不许再做"


# =============================================================================
# 5b. finish() 之后循环必须停，而且不许再发一轮帧
# =============================================================================
def test_the_loop_stops_after_finish_and_never_ticks_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_finished` 的判据与置位必须在**同一把锁**里。

    否则有一个真的顺序洞：`finish()` 握着锁做最后一次 tick 时，循环可能已经通过了
    `while` 检查、正卡在锁上 —— 等它拿到锁就会再发一整轮帧，**落在 `done` 之后**。
    这里手工冒充那个交错：先替 `finish()` 把锁占住，让循环去锁上排队，再在锁内置位、放锁。
    （2026-09-21 收货 MX6 实测：`while not self._finished` 换成 `while True` 时全绿 ——
    "finish 之后循环会停"当时没有任何测试守着。）
    """
    h = make_channel(tmp_path, monkeypatch, sleeper=Sleeper(limit=1))

    async def scenario() -> None:
        await h.channel._lock.acquire()  # 冒充"finish() 正握着锁做最后一次 tick"
        task = asyncio.create_task(h.channel.run_forever())
        await asyncio.sleep(0)  # 循环：过了 while 检查，卡在锁上
        h.channel._finished = True  # finish() 在锁内做完了它该做的
        h.channel._lock.release()
        try:
            await asyncio.wait_for(task, timeout=1)
        except StopLoop:
            # 循环没停，一路跑到把注入的 sleep 用光。真正的判据是下面那条。
            pass

    asyncio.run(scenario())

    assert h.reads == [], "循环拿到锁之后又 tick 了一轮 —— 那些帧会落在 done 之后"


# =============================================================================
# 6. 自适应退避
# =============================================================================
def test_idle_backoff_caps_at_two_seconds_and_snaps_back_on_new_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_channel(
        tmp_path,
        monkeypatch,
        snapshots=[a_snapshot(), a_snapshot(cost_usd=2.0)],
        sleeper=Sleeper(limit=6),
    )
    # 只在第 5 次睡眠时"写一笔"：前面几轮纯空转（间隔翻倍到上限），最后一轮必须立刻回落。
    h.sleeper.on_sleep = lambda n: bump(h.run_dir, "late") if n == 5 else None

    drive(h.channel)

    assert h.sleeper.seen == [
        BASE_INTERVAL_S,
        0.5,
        1.0,
        MAX_INTERVAL_S,
        MAX_INTERVAL_S,
        BASE_INTERVAL_S,
    ]


# =============================================================================
# 7. run 目录还没出现的那段空窗
# =============================================================================
def test_no_run_dir_yet_means_a_no_op_tick(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = make_channel(tmp_path, monkeypatch, with_run_dir=False, sleeper=Sleeper(limit=2))
    sub = h.channel.subscribe()

    drive(h.channel)

    assert h.reads == []
    assert drain(sub) == []
    assert h.channel._tailer is None, "run 目录还没出现就建 tailer = 绑到一个不存在的路径上"


# =============================================================================
# 8. LogTailer 是长命的
# =============================================================================
def test_log_tailer_is_long_lived_so_lines_are_not_re_pushed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """每轮重建 tailer 会把整个日志重推一遍（偏移活在实例里）。"""
    h = make_channel(
        tmp_path,
        monkeypatch,
        snapshots=[a_snapshot(), a_snapshot(cost_usd=3.0)],
        sleeper=Sleeper(limit=2),
    )
    log_path = h.run_dir / "strix.log"
    log_path.write_text("first line\n", encoding="utf-8")

    def on_sleep(n: int) -> None:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"line {n}\n")
        bump(h.run_dir, f"bump-{n}")

    h.sleeper.on_sleep = on_sleep
    sub = h.channel.subscribe()

    drive(h.channel)

    log_frames = [frame for frame in drain(sub) if frame.type == "log"]
    rows = [frame.payload["lines"] for frame in log_frames]
    assert [len(row) for row in rows] == [1, 1], f"日志被重推了：{rows}"


# =============================================================================
# 9. ChannelRegistry（W2c）—— 终态或停机之后一个 channel 都不许剩
# =============================================================================
@dataclass
class RegistryHarness:
    registry: ChannelRegistry
    cwd: Path
    mirror: FakeMirror
    reads: list[Path]
    mirror_args: list[tuple[object, Path, str]]
    persist_args: list[tuple[object, str]]


def make_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    snapshots: list[RunSnapshot] | None = None,
    mirror: FakeMirror | None = None,
) -> RegistryHarness:
    """一个真 registry：只有 `EventMirror` 与 `read_run_dir` 是替身。

    channel 与它的轮询任务都是**真的** —— 本节要测的就是"任务真的起了、真的被回收"，
    换成替身就把这句话测空了。所以这里也没有注入的 `sleep`（registry 刻意不开那个口子），
    时间靠 `wait_for` 等，而不是靠拨表。
    """
    queued = list(snapshots or [a_snapshot()])
    reads: list[Path] = []

    def fake_read_run_dir(run_dir: Path, profile: StrixProfile) -> RunSnapshot:
        reads.append(run_dir)
        return queued.pop(0) if len(queued) > 1 else queued[0]

    the_mirror = mirror if mirror is not None else FakeMirror()
    mirror_args: list[tuple[object, Path, str]] = []

    def fake_mirror(db: object, scans_dir: Path, scan_id: str) -> FakeMirror:
        mirror_args.append((db, scans_dir, scan_id))
        return the_mirror

    the_persist = FakePersist()
    persist_args: list[tuple[object, str]] = []

    def fake_persist(db: object, scan_id: str) -> FakePersist:
        persist_args.append((db, scan_id))
        return the_persist

    monkeypatch.setattr(channel_module, "read_run_dir", fake_read_run_dir)
    # 同 `EventMirror`：registry 自己建 `ScanPersist`，替身只能从这里换进去（库是 None，
    # 真的那个一落库就会炸）。参数照样记下来 —— 传错 scan_id 是静默写到别人名下。
    monkeypatch.setattr(channel_module, "ScanPersist", fake_persist)
    # registry 自己建 `EventMirror`，所以替身只能从这里换进去。**参数要记下来**：传给它的
    # 目录传错了是静默的（见下面那条测试）。
    monkeypatch.setattr(channel_module, "EventMirror", fake_mirror)
    make_run_dir(tmp_path, status="running")
    registry = ChannelRegistry(
        # 镜像已经是替身，所以库一次都不会被碰到 —— 给一个真库只会让意图变模糊。
        db=None,  # type: ignore[arg-type]
        scans_dir=tmp_path / "scans",
        profile=profile_for("1.6.2"),
        redact=lambda text: text,
    )
    return RegistryHarness(
        registry=registry,
        cwd=tmp_path,
        mirror=the_mirror,
        reads=reads,
        mirror_args=mirror_args,
        persist_args=persist_args,
    )


async def wait_for(predicate: Callable[[], bool]) -> None:
    """等后台任务做到某个状态。

    真 channel 的一轮里有两次 `to_thread`，`await asyncio.sleep(0)` 让不出足够的时间 ——
    必须真的把控制权交出去若干次。上限写死不做参数：没有哪条用例需要另一个值。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等超时了：channel 的轮询任务没有做到期望的状态")


def task_of(registry: ChannelRegistry, scan_id: str) -> asyncio.Task[None]:
    """拿那个轮询任务。访问私有字段是刻意的（同 conftest 的 `conn` 夹具）：本节的不变式
    是"任务不许泄漏"，而"它被收掉了"只能从任务对象上看出来。"""
    return registry._live[scan_id][1]


def test_open_starts_the_polling_task_and_get_finds_the_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_registry(tmp_path, monkeypatch)

    async def scenario() -> list[Envelope]:
        channel = h.registry.open("scan-1", h.cwd, start_epoch=0)
        subscriber = channel.subscribe()
        # `get` 是 registry 存在的理由：WS 路由只有 scan_id。
        assert h.registry.get("scan-1") is channel
        await wait_for(lambda: subscriber.queue.qsize() >= 2)
        frames = drain(subscriber)
        await h.registry.close("scan-1")
        return frames

    frames = asyncio.run(scenario())

    # 没人 await 那个任务，它却真的转了一轮 —— 强引用没被 GC 掉、循环真的在跑。
    assert [frame.type for frame in frames] == ["agents", "summary"]
    assert h.reads != []


def test_close_finishes_the_channel_and_reaps_the_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_registry(tmp_path, monkeypatch)

    async def scenario() -> tuple[list[Envelope], bool, ScanChannel | None]:
        channel = h.registry.open("scan-1", h.cwd, start_epoch=0)
        subscriber = channel.subscribe()
        task = task_of(h.registry, "scan-1")
        await wait_for(lambda: subscriber.queue.qsize() >= 2)
        await h.registry.close("scan-1")
        return drain(subscriber), task.done(), h.registry.get("scan-1")

    frames, task_done, still_there = asyncio.run(scenario())

    # `finish()` 的最后一次 tick + **唯一**一个 done。
    assert [frame.type for frame in frames] == ["agents", "summary", "done"]
    assert task_done is True, "轮询任务没被回收 —— 它会一直转到事件循环关闭"
    assert still_there is None


def test_close_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """终态可能被多条路径观察到（正常结束、被停、停机兜底），第二次 close 必须是空操作。"""
    h = make_registry(tmp_path, monkeypatch)

    async def scenario() -> tuple[int, int]:
        h.registry.open("scan-1", h.cwd, start_epoch=0)
        await wait_for(lambda: h.reads != [])
        await h.registry.close("scan-1")
        after_first = len(h.reads)
        await h.registry.close("scan-1")
        return after_first, len(h.reads)

    after_first, after_second = asyncio.run(scenario())

    assert after_first == after_second, "第二次 close 又做了一次 tick"


def test_opening_the_same_scan_twice_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """静默替换掉旧的那个 = 泄漏一个还在跑的任务，正是本节的不变式要挡的事。"""
    h = make_registry(tmp_path, monkeypatch)

    async def scenario() -> None:
        first = h.registry.open("scan-1", h.cwd, start_epoch=0)
        try:
            with pytest.raises(RuntimeError, match="已经开着了"):
                h.registry.open("scan-1", h.cwd, start_epoch=0)
            assert h.registry.get("scan-1") is first, "第二次 open 把在册的那个换掉了"
        finally:
            await h.registry.close("scan-1")

    asyncio.run(scenario())


def test_close_does_not_raise_when_the_channel_died_on_a_mirror_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """镜像写失败会把 channel 任务打死（I2）。`close` 是那个异常的**回收点**，不是传播点：
    它往外抛就会跳过 `_run_to_completion` 的 `forget`/`release` —— 那是凭据泄漏。"""
    h = make_registry(
        tmp_path,
        monkeypatch,
        snapshots=[a_snapshot(events=(an_event(),))],
        mirror=FakeMirror(error=OSError("media 目录写不进去")),
    )

    async def scenario() -> tuple[BaseException | None, bool, ScanChannel | None]:
        h.registry.open("scan-1", h.cwd, start_epoch=0)
        task = task_of(h.registry, "scan-1")
        await wait_for(task.done)
        died_of = task.exception()
        await h.registry.close("scan-1")
        return died_of, task.done(), h.registry.get("scan-1")

    died_of, task_done, still_there = asyncio.run(scenario())

    assert isinstance(died_of, OSError)
    assert task_done is True
    assert still_there is None


def test_shutdown_closes_every_open_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_registry(tmp_path, monkeypatch)

    async def scenario() -> tuple[list[bool], list[ScanChannel | None]]:
        h.registry.open("scan-1", h.cwd, start_epoch=0)
        h.registry.open("scan-2", h.cwd, start_epoch=0)
        tasks = [task_of(h.registry, "scan-1"), task_of(h.registry, "scan-2")]
        await wait_for(lambda: len(h.reads) >= 2)
        await h.registry.shutdown()
        return [task.done() for task in tasks], [
            h.registry.get("scan-1"),
            h.registry.get("scan-2"),
        ]

    done, left = asyncio.run(scenario())

    assert done == [True, True]
    assert left == [None, None]


def test_the_mirror_gets_the_scans_dir_itself_not_a_subdirectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`EventMirror` 的 `rel_path` 列取的是 `scans_dir.name`，所以 registry 必须把
    `Settings.scans_dir` **本身**交给它。在这里拼一层子目录是**静默**写出错的相对路径
    （回放时按 rel_path 找落地的截图会找不着），没有任何别的地方盯着这一个参数。
    """
    h = make_registry(tmp_path, monkeypatch)

    async def scenario() -> None:
        h.registry.open("scan-1", h.cwd, start_epoch=0)
        await h.registry.close("scan-1")

    asyncio.run(scenario())

    assert h.mirror_args == [(None, tmp_path / "scans", "scan-1")]


def test_the_persist_gets_the_scan_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """registry 自己建 `ScanPersist`，所以"它拿到的是哪个 scan_id"只有这里盯着 ——
    传错了就是把一条扫描的 agents／findings 与计数静默写到另一条名下。"""
    h = make_registry(tmp_path, monkeypatch)

    async def scenario() -> None:
        h.registry.open("scan-1", h.cwd, start_epoch=0)
        await h.registry.close("scan-1")

    asyncio.run(scenario())

    assert h.persist_args == [(None, "scan-1")]
