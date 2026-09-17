"""读一个 run 目录，翻成 `RunSnapshot`。这是桥的唯一入口。

同步函数（异步化是调用方 `asyncio.to_thread` 的事，与 `services/run_discovery.py` 同风格）。

**每次读都新建一个 `TuiLiveView`**：`hydrate_from_run_dir`（`live_view.py:91-121`）**不清
状态**，末尾无条件重放整段 session 历史 —— 同一个 view 上 hydrate 两次 = 事件翻倍。
上游自己的 `transcript.build_run_state`（`transcript.py:48-50`）也是每次新建。
推论：子类的 `event_changes_since(cursor)` 在本项目里**永远用不上**（它的游标是实例内的，
随 view 一起丢），所以只用 `event_snapshot()`，增量交给 `services/run_projector.project()`。

**不再包一层容错**：那五个 transcript 函数已经对缺文件、半截 JSON 宽容（`_load_json` 吞
`OSError` + `JSONDecodeError`），`hydrate_from_run_dir` 对缺 `agents.json` 直接 return。
`run_dir` 整个不存在也一样走得通（2026-09-17 在装了 `strix-agent==1.6.2` 的镜像里实测），
所以这里没有一个 `if not run_dir.exists()` 分支 —— 加它只会多一条没人走的路径。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING

from strix.interface.tui.backend.live_view import TuiLiveView
from strix.interface.viewer.transcript import (
    read_report_markdown,
    read_run_summary,
    read_vulnerabilities,
    severity_counts,
)

from app.services.run_projector import (
    ProjectedAgent,
    ProjectedEvent,
    RunSnapshot,
    fingerprint_of,
)

if TYPE_CHECKING:
    from pathlib import Path

    from app.strix_profile import StrixProfile

logger = logging.getLogger(__name__)


def read_run_dir(run_dir: Path, profile: StrixProfile) -> RunSnapshot:
    """读一次 run 目录的全部可见状态。任何缺文件都不抛。

    `profile` 收显式参数：事件类型的取值域是升级时最先失效的东西之一，硬编码在桥里就
    看不出"该重验哪些字面量"了（`services/run_discovery.py` 对 `run.json` 的 status
    是同一个做法）。
    """
    view = TuiLiveView()
    view.hydrate_from_run_dir(run_dir)
    _cursor, raw_events = view.event_snapshot()
    summary = read_run_summary(run_dir)
    vulnerabilities = tuple(
        entry for entry in read_vulnerabilities(run_dir) if isinstance(entry, dict)
    )
    return RunSnapshot(
        agents=tuple(_agent(raw) for raw in view.agents.values()),
        events=tuple(_event(raw, profile) for raw in raw_events),
        run_status=_optional_text(summary.get("status")),
        finished=bool(summary.get("finished")),
        cost_usd=_cost_usd(summary.get("llm_usage")),
        severity=severity_counts(list(vulnerabilities)),
        vulnerabilities=vulnerabilities,
        report_markdown=read_report_markdown(run_dir),
    )


# 下面几个 helper 的入参都是 `object`（不是 `Any`）：上游给的 dict 里键存不存在、值是什么
# 类型都不保证（`agents.json` 与 `run.json` 都可能是人手改过的），而 `object` 会让"没先
# `isinstance` 就用"变成类型错误 —— 收窄正是这几行的工作，不该被 `Any` 静默放过。


def _agent(raw: Mapping[str, object]) -> ProjectedAgent:
    """一个 agent dict → `ProjectedAgent`。`error_message` 只在出错时才存在。"""
    return ProjectedAgent(
        id=_text(raw.get("id")),
        name=_text(raw.get("name")),
        parent_id=_optional_text(raw.get("parent_id")),
        status=_text(raw.get("status")),
        created_at=_text(raw.get("created_at")),
        updated_at=_text(raw.get("updated_at")),
        error_message=_optional_text(raw.get("error_message")),
    )


def _event(raw: Mapping[str, object], profile: StrixProfile) -> ProjectedEvent:
    """一个事件 dict → `ProjectedEvent`，顺手算指纹。

    类型不在 `profile.event_kinds` 里就 warning 并**原样投影**：这是升级预警线，
    丢掉那条事件反而把信号也丢了（同 `read_run_status` 对未登记 status 的处理）。
    """
    data = raw.get("data")
    payload: dict[str, object] = dict(data) if isinstance(data, dict) else {}
    kind = _text(raw.get("type"))
    if kind not in profile.event_kinds:
        logger.warning(
            "上游出现了未登记的事件类型，原样投影",
            extra={"event_kind": kind, "strix_version": profile.version},
        )
    return ProjectedEvent(
        key=_text(raw.get("id")),
        kind=kind,
        agent_id=_optional_text(raw.get("agent_id")),
        ts=_optional_text(raw.get("timestamp")),
        upstream_version=_version(raw.get("version")),
        fingerprint=fingerprint_of(payload),
        data=payload,
    )


def _cost_usd(raw: object) -> float | None:
    """`run.json` 的 `llm_usage.cost`（`report/usage.py:73` 写的就是这个键）。

    取不到就 `None` 而不是 0：预算护栏与"这次花了多少"是两回事，把"读不到"显示成
    "花了 0 块"会让人以为扫描没起来。`bool` 要单独挡掉（它是 `int` 的子类）。
    """
    if not isinstance(raw, dict):
        return None
    value = raw.get("cost")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _version(raw: object) -> int:
    """事件的 `version`。上游写的一定是 int，读不出来就按 0（= 还没被 bump 过）。"""
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else 0


def _text(raw: object) -> str:
    return raw if isinstance(raw, str) else ""


def _optional_text(raw: object) -> str | None:
    return raw if isinstance(raw, str) else None
