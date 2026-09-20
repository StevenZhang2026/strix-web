"""W2a：一轮投影 → 待发货的帧计划。纯函数，不碰时钟／磁盘／DB。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.services.log_tailer import LogLine
from app.services.run_projector import (
    NOTICE_CONTEXT_COMPACTED,
    NOTICE_SCREENSHOT_ELIDED,
    NOTICE_STREAM_RESYNCED,
    Notice,
    ProjectedAgent,
    ProjectedEvent,
    ProjectionResult,
    ProjectionState,
    RunSnapshot,
    fingerprint_of,
)
from app.services.scan_frames import (
    FrameSpec,
    FrameState,
    done_spec,
    envelope_for,
    event_payload,
    plan_frames,
    vuln_key,
)
from app.ws_envelope import PROTOCOL_VERSION

TS = "2026-09-20T10:00:00Z"


def make_event(
    key: str = "tool_1",
    *,
    kind: str = "tool_call",
    upstream_version: int = 1,
    data: dict[str, object] | None = None,
) -> ProjectedEvent:
    payload: dict[str, object] = {"cmd": key} if data is None else data
    return ProjectedEvent(
        key=key,
        kind=kind,
        agent_id="agent-1",
        ts=TS,
        upstream_version=upstream_version,
        fingerprint=fingerprint_of(payload),
        data=payload,
    )


def make_agent(*, status: str = "running") -> ProjectedAgent:
    return ProjectedAgent(
        id="agent-1",
        name="root",
        parent_id=None,
        status=status,
        created_at="2026-09-20T09:59:00Z",
        updated_at=TS,
        error_message=None,
    )


def make_snapshot(
    *,
    run_status: str | None = "running",
    finished: bool = False,
    cost_usd: float | None = 0.5,
    vulnerabilities: Sequence[Mapping[str, object]] = (),
    report_markdown: str = "",
    agents: Sequence[ProjectedAgent] | None = None,
) -> RunSnapshot:
    return RunSnapshot(
        agents=(make_agent(),) if agents is None else tuple(agents),
        events=(),
        run_status=run_status,
        finished=finished,
        cost_usd=cost_usd,
        severity={"high": 1},
        vulnerabilities=tuple(vulnerabilities),
        report_markdown=report_markdown,
    )


def make_projection(
    *,
    resync: bool = False,
    added: Sequence[ProjectedEvent] = (),
    updated: Sequence[ProjectedEvent] = (),
    snapshot: Sequence[ProjectedEvent] = (),
    notices: Sequence[Notice] = (),
) -> ProjectionResult:
    return ProjectionResult(
        state=ProjectionState.empty(),
        resync=resync,
        resync_reason=NOTICE_STREAM_RESYNCED if resync else None,
        added=tuple(added),
        updated=tuple(updated),
        snapshot=tuple(snapshot),
        notices=tuple(notices),
    )


def make_log_line(msg: str) -> LogLine:
    return LogLine(
        ts=TS, level="INFO", scan_id="s1", agent_id="agent-1", logger="app", msg=msg, parsed=True
    )


def types_of(frames: Sequence[FrameSpec]) -> list[str]:
    return [frame.type for frame in frames]


def test_frame_order_within_one_tick() -> None:
    projection = make_projection(
        resync=True,
        snapshot=[make_event("tool_1")],
        notices=[Notice(code=NOTICE_STREAM_RESYNCED, keys=("tool_1",))],
    )
    snapshot = make_snapshot(vulnerabilities=[{"id": "v1"}], report_markdown="# r")
    frames, _ = plan_frames(FrameState.empty(), projection, snapshot, [make_log_line("x")])
    assert types_of(frames) == [
        "notice",
        "agents",
        "event.add",
        "vuln.add",
        "summary",
        "report",
        "log",
    ]


def test_resync_replays_snapshot_as_adds_after_the_notice() -> None:
    events = [make_event("tool_1"), make_event("tool_2"), make_event("tool_3")]
    projection = make_projection(
        resync=True, snapshot=events, notices=[Notice(code=NOTICE_STREAM_RESYNCED, keys=())]
    )
    frames, _ = plan_frames(FrameState.empty(), projection, make_snapshot(), ())
    assert frames[0].type == "notice"
    assert frames[0].payload == {"code": NOTICE_STREAM_RESYNCED, "keys": []}
    event_frames = [frame for frame in frames if frame.type.startswith("event.")]
    assert types_of(event_frames) == ["event.add"] * 3
    assert [frame.event for frame in event_frames] == events
    assert all(frame.payload is None for frame in event_frames)


def test_added_then_updated_keep_their_upstream_order() -> None:
    added = [make_event("tool_1"), make_event("tool_2")]
    updated = [make_event("tool_0", upstream_version=2)]
    projection = make_projection(added=added, updated=updated)
    frames, _ = plan_frames(FrameState.empty(), projection, make_snapshot(), ())
    event_frames = [frame for frame in frames if frame.type.startswith("event.")]
    assert types_of(event_frames) == ["event.add", "event.add", "event.update"]
    assert [frame.event.key for frame in event_frames if frame.event is not None] == [
        "tool_1",
        "tool_2",
        "tool_0",
    ]


def test_event_frames_never_carry_a_payload() -> None:
    """事件帧的 payload 只能在 `EventMirror.append` **之后**用 `mirrored.event` 算：
    镜像会把内联 PNG 换成 media URL，这里先算好等于把 base64 送上 WS、且与回放不一致。
    resync 那条路径上面已经断言过，这条钉住 `added`／`updated` 这条常规路径。
    """
    projection = make_projection(
        added=[make_event("tool_1")], updated=[make_event("tool_0", upstream_version=2)]
    )
    frames, _ = plan_frames(FrameState.empty(), projection, make_snapshot(), ())
    event_frames = [frame for frame in frames if frame.type.startswith("event.")]
    assert len(event_frames) == 2
    assert all(frame.payload is None and frame.event is not None for frame in event_frames)


def test_every_notice_becomes_its_own_frame() -> None:
    notices = [
        Notice(code=NOTICE_SCREENSHOT_ELIDED, keys=("tool_1", "tool_2")),
        Notice(code=NOTICE_CONTEXT_COMPACTED, keys=()),
    ]
    frames, _ = plan_frames(
        FrameState.empty(), make_projection(notices=notices), make_snapshot(), ()
    )
    assert types_of(frames[:2]) == ["notice", "notice"]
    assert frames[0].payload == {"code": NOTICE_SCREENSHOT_ELIDED, "keys": ["tool_1", "tool_2"]}
    assert frames[1].payload == {"code": NOTICE_CONTEXT_COMPACTED, "keys": []}


def test_second_tick_on_the_same_snapshot_repeats_nothing() -> None:
    snapshot = make_snapshot(vulnerabilities=[{"id": 7, "title": "x"}], report_markdown="# r")
    first, state = plan_frames(FrameState.empty(), make_projection(), snapshot, ())
    assert types_of(first) == ["agents", "vuln.add", "summary", "report"]
    second, _ = plan_frames(state, make_projection(), snapshot, ())
    assert second == ()


def test_only_cost_change_emits_only_summary() -> None:
    _, state = plan_frames(FrameState.empty(), make_projection(), make_snapshot(cost_usd=0.5), ())
    frames, _ = plan_frames(state, make_projection(), make_snapshot(cost_usd=0.9), ())
    assert types_of(frames) == ["summary"]
    assert frames[0].payload == {
        "run_status": "running",
        "finished": False,
        "cost_usd": 0.9,
        "severity": {"high": 1},
    }


def test_agents_frame_carries_every_node_field() -> None:
    frames, _ = plan_frames(FrameState.empty(), make_projection(), make_snapshot(), ())
    agents_frame = next(frame for frame in frames if frame.type == "agents")
    assert agents_frame.payload == {
        "agents": [
            {
                "id": "agent-1",
                "name": "root",
                "parent_id": None,
                "status": "running",
                "created_at": "2026-09-20T09:59:00Z",
                "updated_at": TS,
                "error_message": None,
            }
        ]
    }


def test_agent_status_change_re_sends_the_whole_tree() -> None:
    _, state = plan_frames(FrameState.empty(), make_projection(), make_snapshot(), ())
    changed = make_snapshot(agents=[make_agent(status="finished")])
    frames, _ = plan_frames(state, make_projection(), changed, ())
    assert types_of(frames) == ["agents"]


def test_new_vulnerability_emits_only_its_own_frame() -> None:
    _, state = plan_frames(
        FrameState.empty(), make_projection(), make_snapshot(vulnerabilities=[{"id": "v1"}]), ()
    )
    grown = make_snapshot(vulnerabilities=[{"id": "v1"}, {"id": "v2"}])
    frames, next_state = plan_frames(state, make_projection(), grown, ())
    assert types_of(frames) == ["vuln.add"]
    assert frames[0].payload == {"vulnerability": {"id": "v2"}}
    assert next_state.vuln_keys == frozenset({"v1", "v2"})


def test_vulnerabilities_without_id_dedupe_by_fingerprint() -> None:
    entries: list[Mapping[str, object]] = [{"title": "a"}, {"title": "b"}, {"title": "a"}]
    frames, _ = plan_frames(
        FrameState.empty(), make_projection(), make_snapshot(vulnerabilities=entries), ()
    )
    vuln_frames = [frame for frame in frames if frame.type == "vuln.add"]
    assert [frame.payload for frame in vuln_frames] == [
        {"vulnerability": {"title": "a"}},
        {"vulnerability": {"title": "b"}},
    ]


def test_vuln_key_uses_id_when_present() -> None:
    assert vuln_key({"id": 7, "title": "x"}) == "7"


def test_vuln_key_falls_back_to_fingerprint_without_id() -> None:
    entry: Mapping[str, object] = {"title": "x"}
    assert vuln_key(entry) == fingerprint_of(entry)


def test_empty_report_markdown_is_not_a_report_frame() -> None:
    frames, state = plan_frames(
        FrameState.empty(), make_projection(), make_snapshot(report_markdown=""), ()
    )
    assert "report" not in types_of(frames)
    assert state.report_fingerprint is None
    later, _ = plan_frames(state, make_projection(), make_snapshot(report_markdown="# r"), ())
    assert types_of(later) == ["report"]
    assert later[0].payload == {"markdown": "# r"}


def test_no_log_frame_without_lines() -> None:
    frames, _ = plan_frames(FrameState.empty(), make_projection(), make_snapshot(), ())
    assert "log" not in types_of(frames)


def test_two_log_lines_collapse_into_one_frame() -> None:
    lines = [make_log_line("one"), make_log_line("two")]
    frames, _ = plan_frames(FrameState.empty(), make_projection(), make_snapshot(), lines)
    log_frames = [frame for frame in frames if frame.type == "log"]
    assert len(log_frames) == 1
    payload = log_frames[0].payload
    assert payload is not None
    assert payload["lines"] == [
        {
            "ts": TS,
            "level": "INFO",
            "scan_id": "s1",
            "agent_id": "agent-1",
            "logger": "app",
            "msg": "one",
            "parsed": True,
        },
        {
            "ts": TS,
            "level": "INFO",
            "scan_id": "s1",
            "agent_id": "agent-1",
            "logger": "app",
            "msg": "two",
            "parsed": True,
        },
    ]


def test_event_payload_has_the_six_mirror_columns_only() -> None:
    event = make_event("tool_9", kind="tool_result", upstream_version=3, data={"out": "ok"})
    assert event_payload(event) == {
        "key": "tool_9",
        "kind": "tool_result",
        "agent_id": "agent-1",
        "ts": TS,
        "upstream_version": 3,
        "data": {"out": "ok"},
    }


def test_done_spec_carries_no_conclusion() -> None:
    assert done_spec() == FrameSpec(type="done", payload={}, event=None)


def test_envelope_for_fills_the_six_fields() -> None:
    envelope = envelope_for(type="log", payload={"lines": []}, epoch=2, seq=5, ts=TS)
    assert envelope.v == PROTOCOL_VERSION
    assert (envelope.epoch, envelope.seq, envelope.type, envelope.ts) == (2, 5, "log", TS)
    assert envelope.payload == {"lines": []}
