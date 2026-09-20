"""把一轮投影排成「这一轮该发哪些帧」——纯函数：无 IO、无时钟、无随机。

发号（seq）与时间戳（ts）不在这里产生：它们必须与 `EventMirror.append` 交错，那是
`ScanChannel` 的循环该管的事。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.services.log_tailer import LogLine
from app.services.run_projector import (
    ProjectedAgent,
    ProjectedEvent,
    ProjectionResult,
    RunSnapshot,
    fingerprint_of,
)
from app.ws_envelope import PROTOCOL_VERSION, Envelope


@dataclass(frozen=True, slots=True)
class FrameSpec:
    """一帧的"待发货"形状。seq 与 ts 都还没有 —— 它们在发送循环里才产生。

    `event` 非 None ⟺ `payload` 为 None：这种帧的 payload 只能在镜像之后用
    `event_payload(mirrored.event)` 算（镜像会把内联 PNG 换成 media URL，
    顺序反了 WS 帧里就会带着 base64，而且与回放不一致）。
    """

    type: str
    payload: dict[str, object] | None
    event: ProjectedEvent | None = None


@dataclass(frozen=True, slots=True)
class FrameState:
    """上一轮推给前端的**非事件维度**是什么。只存签名，不存内容
    （一个 run 的报告能上百 KB，留在内存里没有意义）。事件维度的状态
    由 `run_projector.ProjectionState` 管，不在这里。
    """

    agents_fingerprint: str | None = None
    summary_fingerprint: str | None = None
    report_fingerprint: str | None = None
    vuln_keys: frozenset[str] = frozenset()

    @classmethod
    def empty(cls) -> FrameState:
        """首次 tick 用：四个维度全是"没推过"，所以第一轮一定是全量。"""
        return cls()


def vuln_key(entry: Mapping[str, object]) -> str:
    """漏洞的去重键。没有 `"id"` 就退回内容指纹 —— 不猜第二个上游键名，
    猜错的后果是同一个漏洞每 tick 重推一次。
    """
    if "id" in entry:
        return str(entry["id"])
    return fingerprint_of(entry)


def event_payload(event: ProjectedEvent) -> dict[str, object]:
    """事件帧的 payload。正好是 `scan_events` 一行能重建出的那六个键，
    因为 T16 回放时手上只有那一行；`fingerprint` 刻意不进帧 —— 它取自上游原始
    payload，而帧里的 `data` 已被镜像改写过，放进去会让人以为能拿它校验帧。
    """
    return {
        "key": event.key,
        "kind": event.kind,
        "agent_id": event.agent_id,
        "ts": event.ts,
        "upstream_version": event.upstream_version,
        "data": event.data,
    }


def envelope_for(
    *, type: str, payload: dict[str, object], epoch: int, seq: int, ts: str
) -> Envelope:
    """`ts` 是显式参数而不是默认 `now_ts()`：收一个时钟进来这个模块就不再是纯的。"""
    return Envelope(v=PROTOCOL_VERSION, epoch=epoch, seq=seq, type=type, ts=ts, payload=payload)


def done_spec() -> FrameSpec:
    """`done` 不带结论：状态／归因／漏洞数只从 `GET /api/scans/{id}` 取，
    免得同一件事有两个真相。由发送循环在最后一次 tick 之后追加。
    """
    return FrameSpec(type="done", payload={})


def _agent_row(agent: ProjectedAgent) -> dict[str, object]:
    return {
        "id": agent.id,
        "name": agent.name,
        "parent_id": agent.parent_id,
        "status": agent.status,
        "created_at": agent.created_at,
        "updated_at": agent.updated_at,
        "error_message": agent.error_message,
    }


def _log_row(line: LogLine) -> dict[str, object]:
    return {
        "ts": line.ts,
        "level": line.level,
        "scan_id": line.scan_id,
        "agent_id": line.agent_id,
        "logger": line.logger,
        "msg": line.msg,
        "parsed": line.parsed,
    }


def plan_frames(
    previous: FrameState,
    projection: ProjectionResult,
    snapshot: RunSnapshot,
    log_lines: Sequence[LogLine],
) -> tuple[tuple[FrameSpec, ...], FrameState]:
    """一个 tick 该发的帧，按 notice → agents → 事件 → vuln → summary → report → log 排序。"""
    frames: list[FrameSpec] = []

    # 重同步的通知必须是新 epoch 的第一帧：客户端看到 epoch 变了要先清本地状态。
    for notice in projection.notices:
        payload: dict[str, object] = {"code": notice.code, "keys": list(notice.keys)}
        frames.append(FrameSpec(type="notice", payload=payload))

    # 整棵树一帧、不做差分：树是几十个节点，差分它换不来任何东西。
    agents_payload: dict[str, object] = {"agents": [_agent_row(a) for a in snapshot.agents]}
    agents_fingerprint = fingerprint_of(agents_payload)
    if agents_fingerprint != previous.agents_fingerprint:
        frames.append(FrameSpec(type="agents", payload=agents_payload))

    # 没有 `events.snapshot` 帧类型：重同步就是在新 epoch 下把每条事件当 `event.add` 再发一遍，
    # 这样回放与直播是同一个帧形状，前端只维护一套解析。
    if projection.resync:
        for event in projection.snapshot:
            frames.append(FrameSpec(type="event.add", payload=None, event=event))
    else:
        for event in projection.added:
            frames.append(FrameSpec(type="event.add", payload=None, event=event))
        for event in projection.updated:
            frames.append(FrameSpec(type="event.update", payload=None, event=event))

    vuln_keys = set(previous.vuln_keys)
    for entry in snapshot.vulnerabilities:
        key = vuln_key(entry)
        if key in vuln_keys:
            continue
        vuln_keys.add(key)
        frames.append(FrameSpec(type="vuln.add", payload={"vulnerability": entry}))

    summary_payload: dict[str, object] = {
        "run_status": snapshot.run_status,
        "finished": snapshot.finished,
        "cost_usd": snapshot.cost_usd,
        "severity": snapshot.severity,
    }
    summary_fingerprint = fingerprint_of(summary_payload)
    if summary_fingerprint != previous.summary_fingerprint:
        frames.append(FrameSpec(type="summary", payload=summary_payload))

    # 空串是"读不到报告"，不是"报告变空了"：不发帧，也不更新签名，
    # 否则报告真出现时会被当成"内容没变"。
    report_fingerprint = previous.report_fingerprint
    if snapshot.report_markdown:
        report_payload: dict[str, object] = {"markdown": snapshot.report_markdown}
        report_fingerprint = fingerprint_of(report_payload)
        if report_fingerprint != previous.report_fingerprint:
            frames.append(FrameSpec(type="report", payload=report_payload))

    # 日志不进镜像，所以不需要一行一帧；一行一帧只会白吃掉大量 seq。
    if log_lines:
        rows = [_log_row(line) for line in log_lines]
        frames.append(FrameSpec(type="log", payload={"lines": rows}))

    return tuple(frames), FrameState(
        agents_fingerprint=agents_fingerprint,
        summary_fingerprint=summary_fingerprint,
        report_fingerprint=report_fingerprint,
        vuln_keys=frozenset(vuln_keys),
    )
