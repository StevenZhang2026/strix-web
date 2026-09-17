"""把一份 run 快照跟"上次见过什么"比对，得出增量、重同步与提示。**纯函数**。

零 IO、零 `strix` import、零模块级可变状态：状态由调用方（T14 的 tick 循环）持有，
每轮拿 `ProjectionResult.state` 换掉手上那份。这样"投影的判定"可以在毫秒级用手搓的
事件测穷举，不需要一个 run 目录、也不需要装 `strix`。

**为什么需要 epoch 与重同步**：`agents.db` 不是只追加的。上下文压缩
（`llm/compaction.py`）与截图淘汰（`core/sessions.py enforce_image_budget`）都走
`_rewrite_session` → `clear_session()` 后重插 → 上游重放出来的事件 id 会**重排**。
朴素游标（"我已经推到第 N 条了"）在这里必然错位，所以每轮都要拿整段 key 序列比对：
`shrank` / `prefix_unstable` / 无 version 递增的 `mutated` 三种情况一律 epoch+1 +
整份重推，前端按 epoch 丢掉旧的那一代。

**截图淘汰刻意不 bump epoch**：`enforce_image_budget._transform` 只把靠前的 image
output **原位**换成一条占位字面量，重建出来的 items 与原来**等长同序** → 只会表现为
"内容变了而 version 没动"。而我们首见即把截图落地成 `media/<sha256>.png`（T14），
前端照样显示得出来，所以没有任何东西需要重同步 —— 只发一条 `screenshot_elided` 提示。
真正的重排是压缩：`new_items = [checkpoint_item, *recent]`，那会同时命中前两条判据。

**给 T14 的两条落库语义**（写在这里，免得 DDL 注释的措辞把人带偏）：

- `scan_events.strix_id INTEGER` 存的是**事件 key 的整数后缀**（`"tool_12"` → `12`），
  因为 `kind` 已经存了前缀；解析不出整数就存 NULL。（DDL 注释说"源库里的 id"，而投影层
  的 id 是字符串 —— 这是已知的措辞偏差，**不许为它改迁移**。）
- `scan_events.version INTEGER DEFAULT 1` 是**我们的 payload 结构版本**，**不是** strix 的
  `version`。后者叫 `upstream_version`，进 `data_json` 的一个键、不占列。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.strix_profile import StrixProfile


# 稳定机器码。中文文案在 `frontend/messages/zh-CN.json`（T17），本模块不碰文案。
NOTICE_SCREENSHOT_ELIDED = "screenshot_elided"
NOTICE_CONTEXT_COMPACTED = "context_compacted"
NOTICE_STREAM_RESYNCED = "stream_resynced"

RESYNC_SHRANK = "shrank"
RESYNC_PREFIX_UNSTABLE = "prefix_unstable"
RESYNC_MUTATED = "mutated"


@dataclass(frozen=True, slots=True)
class ProjectedEvent:
    """一条投影后的事件。`key` 是 strix 的事件 id（`"tool_12"`，**字符串**）。"""

    key: str
    kind: str
    agent_id: str | None
    ts: str | None
    # strix 事件的 `version`（`_bump_event` 的计数），**不是** `scan_events.version`。
    upstream_version: int
    fingerprint: str
    # 上游 payload 原样带过来：形状由工具决定，投影层不解释它（只算指纹、找字面量）。
    data: dict[str, object]


@dataclass(frozen=True, slots=True)
class ProjectedAgent:
    """agent 图里的一个节点。字段名照 `live_view.upsert_agent` 产出的 dict。"""

    id: str
    name: str
    parent_id: str | None
    status: str
    created_at: str
    updated_at: str
    error_message: str | None


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    """一次"读整个 run 目录"的结果。由 `app.strix_bridge.projection.read_run_dir` 产出。"""

    agents: tuple[ProjectedAgent, ...]
    events: tuple[ProjectedEvent, ...]
    # `run.json` 的 status，**原样**。取值域的判定在 `services/run_discovery.py`，
    # 这里判死只会把"上游加了新状态"这个升级信号丢掉。
    run_status: str | None
    finished: bool
    cost_usd: float | None
    severity: Mapping[str, int]
    vulnerabilities: tuple[Mapping[str, object], ...]
    report_markdown: str


@dataclass(frozen=True, slots=True)
class ProjectionState:
    """记下"上一轮推到前端的是什么" —— 调用方持有，本模块只读它、只产出新的。

    `elided` 是"这个 key 现在的内容里已经有淘汰占位符了"。判据里"旧 data 里没有那三条
    字面量"需要它：整份旧 payload 留在内存里代价太大（一个 run 上万条事件），而一个
    key 集合就够回答那个问题。
    """

    epoch: int
    order: tuple[str, ...]
    fingerprints: Mapping[str, str]
    versions: Mapping[str, int]
    elided: frozenset[str] = frozenset()

    @classmethod
    def empty(cls) -> ProjectionState:
        """首次投影用。空 order + epoch 0 —— 全量走 `added`，不算重同步。"""
        return cls(epoch=0, order=(), fingerprints={}, versions={})


@dataclass(frozen=True, slots=True)
class Notice:
    """一条给前端的提示。`code` 是稳定机器码，`keys` 是相关事件 key（可空）。"""

    code: str
    keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProjectionResult:
    """一轮投影的产出。`resync` 时 `added` / `updated` **必须**是空的。"""

    state: ProjectionState
    resync: bool
    resync_reason: str | None
    added: tuple[ProjectedEvent, ...]
    updated: tuple[ProjectedEvent, ...]
    snapshot: tuple[ProjectedEvent, ...]
    notices: tuple[Notice, ...]


def canonical_json(data: Mapping[str, object]) -> str:
    """指纹与字面量查找共用的规范化文本。键排序、不转义非 ASCII、无多余空格。"""
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint_of(data: Mapping[str, object]) -> str:
    """`sha256(canonical_json(data))`。**只对 `data` 取**。

    不含 `ts` 与 `upstream_version`：`_bump_event` 每次都重写 `timestamp`，含进去会把
    "什么都没变"报成变了（然后每一轮都重推一遍整条历史）。
    """
    return hashlib.sha256(canonical_json(data).encode()).hexdigest()


def project(
    previous: ProjectionState,
    snapshot: RunSnapshot,
    profile: StrixProfile,
) -> ProjectionResult:
    """比对一份新快照与上一轮的状态。判据按顺序、**命中即停**。

    1. `shrank`：事件变少 → 重同步
    2. `prefix_unstable`：某个位置上的 key 换了 → 重同步
    3. 没见过的 key → `added`
    4. 指纹变 **且** `upstream_version` 递增 → `updated`（上游的正常更新路径）
    5. 指纹变、version 没动、内容里新出现了淘汰字面量 → `screenshot_elided` 提示
    6. 指纹变、version 没动、不是淘汰 → `mutated` → 重同步

    首次投影（空 order）不需要特殊分支：`len(events) < 0` 不成立、`min(len, 0) == 0`
    也没有可比的前缀，所以 1、2 都不会命中，全部落到 3。

    `profile` 收显式参数（不许默认值）：三条淘汰字面量与 checkpoint 标签都是**升级时
    最先失效**的东西，藏一个默认值等于把它们钉死在代码里。
    """
    events = snapshot.events
    reason = _resync_reason(previous.order, events)
    if reason is not None:
        return _resync(previous, events, reason, profile)

    added: list[ProjectedEvent] = []
    updated: list[ProjectedEvent] = []
    notices: list[Notice] = []
    for event in events:
        old_fingerprint = previous.fingerprints.get(event.key)
        if old_fingerprint is None:
            added.append(event)
            continue
        if old_fingerprint == event.fingerprint:
            continue
        if event.upstream_version > previous.versions.get(event.key, 0):
            updated.append(event)
            continue
        if event.key not in previous.elided and _has_elision_text(event.data, profile):
            notices.append(Notice(code=NOTICE_SCREENSHOT_ELIDED, keys=(event.key,)))
            continue
        return _resync(previous, events, RESYNC_MUTATED, profile)

    return ProjectionResult(
        state=_state_after(previous.epoch, events, profile),
        resync=False,
        resync_reason=None,
        added=tuple(added),
        updated=tuple(updated),
        snapshot=(),
        notices=tuple(notices),
    )


def _resync_reason(order: tuple[str, ...], events: tuple[ProjectedEvent, ...]) -> str | None:
    """只看 key 序列。两个信号同时成立时报靠前的那个。"""
    if len(events) < len(order):
        return RESYNC_SHRANK
    for index in range(min(len(events), len(order))):
        if events[index].key != order[index]:
            return RESYNC_PREFIX_UNSTABLE
    return None


def _resync(
    previous: ProjectionState,
    events: tuple[ProjectedEvent, ...],
    reason: str,
    profile: StrixProfile,
) -> ProjectionResult:
    """换代：epoch+1、整份重推、`added` / `updated` 一律空。"""
    return ProjectionResult(
        state=_state_after(previous.epoch + 1, events, profile),
        resync=True,
        resync_reason=reason,
        added=(),
        updated=(),
        snapshot=events,
        notices=(_resync_notice(events, profile),),
    )


def _resync_notice(events: tuple[ProjectedEvent, ...], profile: StrixProfile) -> Notice:
    """区分"上下文被压缩了"与"历史被重排了"：只认压缩后那个 checkpoint 头。

    压缩把整段历史换成 `[checkpoint_item, *recent]`，那条 checkpoint 是一条 user turn。
    别的重排（含 `mutated`）都只能说"你手上那份要整份换掉"。
    """
    if events:
        head = events[0]
        content = head.data.get("content")
        if (
            head.kind == "chat"
            and head.data.get("role") == "user"
            and isinstance(content, str)
            and profile.compaction_checkpoint_tag in content
        ):
            return Notice(code=NOTICE_CONTEXT_COMPACTED, keys=(head.key,))
    return Notice(code=NOTICE_STREAM_RESYNCED, keys=())


def _state_after(
    epoch: int,
    events: tuple[ProjectedEvent, ...],
    profile: StrixProfile,
) -> ProjectionState:
    return ProjectionState(
        epoch=epoch,
        order=tuple(event.key for event in events),
        fingerprints={event.key: event.fingerprint for event in events},
        versions={event.key: event.upstream_version for event in events},
        elided=frozenset(event.key for event in events if _has_elision_text(event.data, profile)),
    )


def _has_elision_text(data: Mapping[str, object], profile: StrixProfile) -> bool:
    """`data` 的规范化文本里是否含那三条字面量之一。**排他**，不做模糊匹配。

    在整份 payload 上找而不是只看 `data["result"]`：占位符替换的是 output，而 output
    在不同工具下落在 `result` 的不同嵌套层。模糊匹配（"含 `elided`"）是不许的 ——
    它会把真正的改写吞成一条提示，前端手上那份就悄悄错了。
    """
    text = canonical_json(data)
    return any(literal in text for literal in profile.image_elision_texts)
