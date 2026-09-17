"""`project()`：把一份 run 快照跟"上次见过什么"比对，得出增量、重同步与提示。

这是本项目里最典型的"无 IO 纯判定函数"，所以按 `agent-rules.md` §十.5 第一层 TDD：
每条测试都先看着它红过一次。三条不变式各有一组穷举用例（`test_i1_*` / `test_i2_*` /
`test_i3_*`），它们就是收货时的 mutation 靶子。

夹具全在本文件里手搓 `ProjectedEvent`（**不经 `strix_bridge`**）：被测函数零 IO、
零 `strix`，测试也就不该需要一个 run 目录。桥那一侧在 `test_strix_bridge_projection.py`。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from app.services import run_projector
from app.services.run_projector import (
    NOTICE_CONTEXT_COMPACTED,
    NOTICE_SCREENSHOT_ELIDED,
    NOTICE_STREAM_RESYNCED,
    RESYNC_MUTATED,
    RESYNC_PREFIX_UNSTABLE,
    RESYNC_SHRANK,
    Notice,
    ProjectedAgent,
    ProjectedEvent,
    ProjectionResult,
    ProjectionState,
    RunSnapshot,
    fingerprint_of,
    project,
)
from app.strix_profile import profile_for

PROFILE = profile_for("1.6.2")
TS = "2026-01-01T00:00:00+00:00"

ELISION_TEXTS = PROFILE.image_elision_texts
CHECKPOINT = PROFILE.compaction_checkpoint_tag


def ev(
    key: str,
    *,
    data: dict[str, object] | None = None,
    version: int = 0,
    kind: str | None = None,
) -> ProjectedEvent:
    """一条投影事件。`fingerprint` 由 `data` 算出，跟桥里用的是同一个函数。"""
    payload: dict[str, object] = {"result": "ok"} if data is None else dict(data)
    return ProjectedEvent(
        key=key,
        kind=kind or ("chat" if key.startswith("chat") else "tool"),
        agent_id="root",
        ts=TS,
        upstream_version=version,
        fingerprint=fingerprint_of(payload),
        data=payload,
    )


def changed(
    source: ProjectedEvent,
    *,
    data: dict[str, object],
    version: int | None = None,
) -> ProjectedEvent:
    """同一个 key、换了 `data`（可选地换 `upstream_version`）。"""
    return ev(
        source.key,
        kind=source.kind,
        data=data,
        version=source.upstream_version if version is None else version,
    )


def snap(*events: ProjectedEvent) -> RunSnapshot:
    """一份只关心 `events` 的快照 —— 其余字段 `project()` 不看。"""
    return RunSnapshot(
        agents=(),
        events=events,
        run_status="running",
        finished=False,
        cost_usd=None,
        severity={"critical": 0, "high": 0, "medium": 0, "low": 0},
        vulnerabilities=(),
        report_markdown="",
    )


def seen(*events: ProjectedEvent) -> ProjectionState:
    """造出"上一轮就是这些事件"的状态 —— 用 `project()` 自己产出，免得手写状态跑偏。"""
    return project(ProjectionState.empty(), snap(*events), PROFILE).state


def keys(events: tuple[ProjectedEvent, ...]) -> tuple[str, ...]:
    return tuple(event.key for event in events)


def notice_codes(result: ProjectionResult) -> tuple[str, ...]:
    return tuple(notice.code for notice in result.notices)


BASE = (
    ev("chat_1", kind="chat", data={"role": "assistant", "content": "hello"}),
    ev("tool_2", data={"tool_name": "browser", "result": "ok"}, version=1),
    ev("tool_3", data={"tool_name": "exec", "result": "done"}, version=1),
)


# --------------------------------------------------------------------------- 首次投影


def test_first_projection_puts_everything_in_added() -> None:
    """首次投影不是重同步：前端还什么都没有，`added` 就是全量。"""
    result = project(ProjectionState.empty(), snap(*BASE), PROFILE)
    assert result.resync is False
    assert result.resync_reason is None
    assert keys(result.added) == ("chat_1", "tool_2", "tool_3")
    assert result.updated == ()
    assert result.snapshot == ()
    assert result.notices == ()
    assert result.state.epoch == 0


def test_first_projection_of_an_empty_run() -> None:
    """run 刚起来、一条事件都还没有：什么都不发，也不算重同步。"""
    result = project(ProjectionState.empty(), snap(), PROFILE)
    assert result.resync is False
    assert result.added == ()
    assert result.state.order == ()
    assert result.state.epoch == 0


def test_state_records_order_fingerprints_and_versions() -> None:
    result = project(ProjectionState.empty(), snap(*BASE), PROFILE)
    assert result.state.order == ("chat_1", "tool_2", "tool_3")
    assert dict(result.state.fingerprints) == {e.key: e.fingerprint for e in BASE}
    assert dict(result.state.versions) == {"chat_1": 0, "tool_2": 1, "tool_3": 1}


def test_empty_state_is_empty() -> None:
    state = ProjectionState.empty()
    assert state.epoch == 0
    assert state.order == ()
    assert dict(state.fingerprints) == {}
    assert dict(state.versions) == {}


# --------------------------------------------------------------------------- 增量


def test_unchanged_snapshot_emits_nothing() -> None:
    result = project(seen(*BASE), snap(*BASE), PROFILE)
    assert (result.added, result.updated, result.notices, result.resync) == ((), (), (), False)
    assert result.state.epoch == 0


def test_appended_event_is_added() -> None:
    tail = ev("tool_4", data={"tool_name": "exec", "result": "new"})
    result = project(seen(*BASE), snap(*BASE, tail), PROFILE)
    assert keys(result.added) == ("tool_4",)
    assert result.updated == ()
    assert result.resync is False


def test_several_events_added_at_once() -> None:
    extra = (ev("tool_4"), ev("tool_5"), ev("chat_6", kind="chat", data={"role": "assistant"}))
    result = project(seen(*BASE), snap(*BASE, *extra), PROFILE)
    assert keys(result.added) == ("tool_4", "tool_5", "chat_6")


def test_version_bump_with_new_payload_is_an_update() -> None:
    """工具 output 到达：`_record_tool_output_data` 写 `result` 再 `_bump_event`。"""
    updated = changed(BASE[1], data={"tool_name": "browser", "result": "later"}, version=2)
    result = project(seen(*BASE), snap(BASE[0], updated, BASE[2]), PROFILE)
    assert keys(result.updated) == ("tool_2",)
    assert result.added == ()
    assert result.notices == ()
    assert result.resync is False
    assert result.state.epoch == 0
    assert result.state.versions["tool_2"] == 2


def test_added_and_updated_in_the_same_call() -> None:
    updated = changed(BASE[2], data={"tool_name": "exec", "result": "later"}, version=2)
    tail = ev("tool_4")
    result = project(seen(*BASE), snap(BASE[0], BASE[1], updated, tail), PROFILE)
    assert keys(result.added) == ("tool_4",)
    assert keys(result.updated) == ("tool_3",)


def test_a_first_seen_event_carrying_an_elision_text_is_just_added() -> None:
    """从没推过的 key 只能是 `added` —— 它没有"上一版"，谈不上淘汰。"""
    fresh = ev("tool_4", data={"result": ELISION_TEXTS[1]}, version=1)
    result = project(seen(*BASE), snap(*BASE, fresh), PROFILE)
    assert keys(result.added) == ("tool_4",)
    assert result.notices == ()


# --------------------------------------------------------------------------- 重同步


def test_shrank_forces_resync() -> None:
    """事件变少只可能是上游 `clear_session()` 重插过 —— 朴素游标在这里必错。"""
    result = project(seen(*BASE), snap(BASE[0], BASE[1]), PROFILE)
    assert result.resync is True
    assert result.resync_reason == RESYNC_SHRANK
    assert keys(result.snapshot) == ("chat_1", "tool_2")
    assert result.added == ()
    assert result.updated == ()
    assert result.state.epoch == 1
    assert result.state.order == ("chat_1", "tool_2")


def test_prefix_unstable_forces_resync() -> None:
    """长度没变但 key 顺序换了：同一个位置现在是另一条事件。"""
    result = project(seen(*BASE), snap(BASE[1], BASE[0], BASE[2]), PROFILE)
    assert result.resync is True
    assert result.resync_reason == RESYNC_PREFIX_UNSTABLE
    assert keys(result.snapshot) == ("tool_2", "chat_1", "tool_3")
    assert result.state.epoch == 1


def test_prefix_unstable_even_when_the_stream_grew() -> None:
    """压缩后又跑了一会儿：事件总数可能反超，但开头已经被换掉了。"""
    rebuilt = (BASE[1], BASE[2], ev("tool_4"), ev("tool_5"))
    result = project(seen(*BASE), snap(*rebuilt), PROFILE)
    assert result.resync is True
    assert result.resync_reason == RESYNC_PREFIX_UNSTABLE


def test_shrank_wins_over_prefix_unstable() -> None:
    """两个信号同时成立时报靠前的那个 —— 判据表是有序的，不许"两个都报"。"""
    result = project(seen(*BASE), snap(ev("tool_9")), PROFILE)
    assert result.resync_reason == RESYNC_SHRANK


def test_everything_gone_is_a_resync_with_an_empty_snapshot() -> None:
    result = project(seen(*BASE), snap(), PROFILE)
    assert result.resync is True
    assert result.resync_reason == RESYNC_SHRANK
    assert result.snapshot == ()
    assert notice_codes(result) == (NOTICE_STREAM_RESYNCED,)
    assert result.state.epoch == 1


def test_mutation_without_a_version_bump_forces_resync() -> None:
    """内容变了而 `version` 没动 → 不是上游的正常更新路径，只能重同步。"""
    mutated = changed(BASE[1], data={"tool_name": "browser", "result": "rewritten"})
    result = project(seen(*BASE), snap(BASE[0], mutated, BASE[2]), PROFILE)
    assert result.resync is True
    assert result.resync_reason == RESYNC_MUTATED
    assert result.added == ()
    assert result.updated == ()
    assert len(result.snapshot) == 3
    assert result.state.epoch == 1


def test_version_going_backwards_forces_resync() -> None:
    mutated = changed(BASE[1], data={"tool_name": "browser", "result": "x"}, version=0)
    result = project(seen(*BASE), snap(BASE[0], mutated, BASE[2]), PROFILE)
    assert result.resync is True
    assert result.resync_reason == RESYNC_MUTATED


def test_resync_snapshot_is_the_whole_stream_not_the_tail() -> None:
    grown = (*BASE, ev("tool_4"), ev("tool_5"))
    mutated = changed(BASE[1], data={"result": "rewritten"})
    result = project(seen(*grown), snap(BASE[0], mutated, BASE[2], grown[3], grown[4]), PROFILE)
    assert result.resync is True
    assert keys(result.snapshot) == ("chat_1", "tool_2", "tool_3", "tool_4", "tool_5")


# --------------------------------------------------------------------------- 重同步的提示码


def test_resync_after_compaction_reports_context_compacted() -> None:
    """压缩把整段历史换成 `[checkpoint, *recent]` —— 第一条就是那个 checkpoint。"""
    compacted = (
        ev("chat_4", kind="chat", data={"role": "user", "content": f"{CHECKPOINT}\nsummary"}),
        ev("tool_5", data={"result": "ok"}, version=1),
    )
    result = project(seen(*BASE), snap(*compacted), PROFILE)
    assert result.resync is True
    assert notice_codes(result) == (NOTICE_CONTEXT_COMPACTED,)
    assert result.notices[0].keys == ("chat_4",)


@pytest.mark.parametrize(
    ("kind", "data"),
    [
        ("chat", {"role": "assistant", "content": f"{CHECKPOINT} quoted"}),
        ("chat", {"role": "user", "content": "no tag here"}),
        ("chat", {"role": "user"}),
        ("tool", {"result": f"{CHECKPOINT} in a tool result"}),
    ],
    ids=["assistant-quotes-the-tag", "user-without-tag", "user-without-content", "tool-first"],
)
def test_resync_without_a_checkpoint_head_reports_stream_resynced(
    kind: str,
    data: dict[str, object],
) -> None:
    """只有"第一条是带 checkpoint 的 user chat"才算压缩，别的重排都是普通重同步。"""
    head = ev("chat_9" if kind == "chat" else "tool_9", kind=kind, data=data)
    result = project(seen(*BASE), snap(head), PROFILE)
    assert result.resync is True
    assert notice_codes(result) == (NOTICE_STREAM_RESYNCED,)
    assert result.notices[0].keys == ()


def test_mutated_resync_also_carries_a_notice() -> None:
    """任何重同步都要带一条提示 —— 前端得知道"你手上那份要整份换掉"。"""
    mutated = changed(BASE[1], data={"result": "rewritten"})
    result = project(seen(*BASE), snap(BASE[0], mutated, BASE[2]), PROFILE)
    assert notice_codes(result) == (NOTICE_STREAM_RESYNCED,)


# --------------------------------------------------------------------------- 截图淘汰


@pytest.mark.parametrize("text", ELISION_TEXTS, ids=["rejected", "elided", "inherited"])
@pytest.mark.parametrize("wrap", ["bare", "list", "nested"])
def test_image_elision_is_a_notice_not_a_resync(text: str, wrap: str) -> None:
    """`enforce_image_budget` **原位**替换靠前的截图：等长同序，只有内容变了。

    首见即落地了截图镜像（T14），所以没有任何东西需要重同步 —— epoch 不许动。
    """
    payloads: dict[str, object] = {
        "bare": text,
        "list": [{"type": "text", "text": text}],
        "nested": {"content": [text]},
    }
    shot = {"result": {"type": "image", "image_url": "data:image/png;base64,AAA"}}
    before = ev("tool_2", data=shot, version=1)
    after = changed(before, data={"result": payloads[wrap]})
    result = project(seen(BASE[0], before), snap(BASE[0], after), PROFILE)
    assert result.resync is False
    assert result.resync_reason is None
    assert notice_codes(result) == (NOTICE_SCREENSHOT_ELIDED,)
    assert result.notices[0].keys == ("tool_2",)
    assert result.updated == ()
    assert result.added == ()
    assert result.state.epoch == 0


def test_elision_updates_the_recorded_fingerprint() -> None:
    """提示发过就算处理过：下一轮同一份内容不许再报一次。"""
    before = ev("tool_2", data={"result": "shot"}, version=1)
    after = changed(before, data={"result": ELISION_TEXTS[1]})
    first = project(seen(BASE[0], before), snap(BASE[0], after), PROFILE)
    second = project(first.state, snap(BASE[0], after), PROFILE)
    assert notice_codes(second) == ()
    assert second.resync is False


def test_two_events_elided_in_one_tick_get_two_notices() -> None:
    a = ev("tool_2", data={"result": "shot-a"}, version=1)
    b = ev("tool_3", data={"result": "shot-b"}, version=1)
    result = project(
        seen(a, b),
        snap(
            changed(a, data={"result": ELISION_TEXTS[1]}),
            changed(b, data={"result": ELISION_TEXTS[1]}),
        ),
        PROFILE,
    )
    assert notice_codes(result) == (NOTICE_SCREENSHOT_ELIDED, NOTICE_SCREENSHOT_ELIDED)
    assert tuple(notice.keys for notice in result.notices) == (("tool_2",), ("tool_3",))
    assert result.state.epoch == 0


def test_changing_an_already_elided_event_forces_resync() -> None:
    """已经是淘汰占位符了还再变一次，就不是淘汰了 —— 那是改写。"""
    before = ev("tool_2", data={"result": "shot"}, version=1)
    elided = changed(before, data={"result": ELISION_TEXTS[1]})
    first = project(seen(before), snap(elided), PROFILE)
    again = changed(before, data={"result": f"{ELISION_TEXTS[1]} and more"})
    second = project(first.state, snap(again), PROFILE)
    assert second.resync is True
    assert second.resync_reason == RESYNC_MUTATED
    assert second.state.epoch == 1


def test_version_bump_beats_elision_detection() -> None:
    """判据表是有序的：`version` 递增了就是正常更新，哪怕内容正好是淘汰占位符。

    上游走 `_record_tool_output_data` 时**一定**会 `_bump_event`；只有会话被重写
    （`_rewrite_session`）才会出现"内容变了而 version 没动"。
    """
    before = ev("tool_2", data={"result": "shot"}, version=1)
    after = changed(before, data={"result": ELISION_TEXTS[1]}, version=2)
    result = project(seen(before), snap(after), PROFILE)
    assert keys(result.updated) == ("tool_2",)
    assert result.notices == ()
    assert result.resync is False


def test_elision_texts_come_from_the_profile() -> None:
    """三条字面量是 profile 的数据，不是模块里的硬编码 —— 升级时只改对照表。"""
    fake = dataclasses.replace(PROFILE, image_elision_texts=("[gone]",))
    before = ev("tool_2", data={"result": "shot"}, version=1)
    invented = project(seen(before), snap(changed(before, data={"result": "[gone]"})), fake)
    assert notice_codes(invented) == (NOTICE_SCREENSHOT_ELIDED,)
    real = project(seen(before), snap(changed(before, data={"result": ELISION_TEXTS[1]})), fake)
    assert real.resync is True


def test_checkpoint_tag_comes_from_the_profile() -> None:
    fake = dataclasses.replace(PROFILE, compaction_checkpoint_tag="<<cut>>")
    head = ev("chat_9", kind="chat", data={"role": "user", "content": "<<cut>> summary"})
    result = project(seen(*BASE), snap(head), fake)
    assert notice_codes(result) == (NOTICE_CONTEXT_COMPACTED,)


# --------------------------------------------------------------------------- I1：epoch


def i1_scenarios() -> list[tuple[str, tuple[ProjectedEvent, ...], int]]:
    """(用例名, 新快照, 期望的 epoch 增量)。**只有三种情况允许 +1**。"""
    bumped = changed(BASE[1], data={"result": "later"}, version=2)
    mutated = changed(BASE[1], data={"result": "rewritten"})
    elided = changed(BASE[1], data={"result": ELISION_TEXTS[0]})
    return [
        ("unchanged", BASE, 0),
        ("added", (*BASE, ev("tool_4")), 0),
        ("updated", (BASE[0], bumped, BASE[2]), 0),
        ("elided", (BASE[0], elided, BASE[2]), 0),
        ("added-and-updated", (BASE[0], bumped, BASE[2], ev("tool_4")), 0),
        ("shrank", (BASE[0],), 1),
        ("emptied", (), 1),
        ("prefix-unstable", (BASE[2], BASE[1], BASE[0]), 1),
        ("mutated", (BASE[0], mutated, BASE[2]), 1),
    ]


@pytest.mark.parametrize(
    ("events", "delta"),
    [(events, delta) for _name, events, delta in i1_scenarios()],
    ids=[name for name, _events, _delta in i1_scenarios()],
)
def test_i1_epoch_only_moves_on_the_three_resync_signals(
    events: tuple[ProjectedEvent, ...],
    delta: int,
) -> None:
    previous = seen(*BASE)
    result = project(previous, snap(*events), PROFILE)
    assert result.state.epoch - previous.epoch == delta
    assert result.resync is (delta == 1)


def test_i1_epoch_never_decreases_over_a_long_sequence() -> None:
    """epoch 是单调不减的：它是"你手上那份历史属于哪一代"的唯一凭据。"""
    state = ProjectionState.empty()
    epochs = []
    for events in [
        BASE,
        (*BASE, ev("tool_4")),
        (BASE[0],),
        (BASE[0], ev("tool_2", data={"result": "x"})),
        (),
        (ev("chat_9", kind="chat", data={"role": "assistant", "content": "again"}),),
    ]:
        result = project(state, snap(*events), PROFILE)
        state = result.state
        epochs.append(state.epoch)
    assert epochs == [0, 0, 1, 1, 2, 2]


def test_i1_resync_never_carries_added_or_updated() -> None:
    """重同步就是"整份换掉"，再夹带增量只会让前端把同一条事件插两次。"""
    for name, events, delta in i1_scenarios():
        if delta == 0:
            continue
        result = project(seen(*BASE), snap(*events), PROFILE)
        assert result.added == (), name
        assert result.updated == (), name
        assert result.snapshot == events, name


# --------------------------------------------------------------------------- I2：不许静默丢变化


def i2_mutations() -> list[tuple[str, ProjectedEvent]]:
    """一批"已推送过的 key 的内容发生了变化"，每条都必须被报出去。"""
    source = BASE[1]
    return [
        ("version-bump", changed(source, data={"result": "later"}, version=2)),
        ("version-bump-many", changed(source, data={"result": "later"}, version=9)),
        ("elision-rejected", changed(source, data={"result": ELISION_TEXTS[0]})),
        ("elision-elided", changed(source, data={"result": ELISION_TEXTS[1]})),
        ("elision-inherited", changed(source, data={"result": ELISION_TEXTS[2]})),
        ("plain-rewrite", changed(source, data={"result": "rewritten"})),
        ("key-added", changed(source, data={"tool_name": "browser", "result": "ok", "x": 1})),
        ("key-removed", changed(source, data={"result": "ok"})),
        ("emptied", changed(source, data={})),
        ("version-backwards", changed(source, data={"result": "y"}, version=0)),
        ("fuzzy-elided-word", changed(source, data={"result": "elided"})),
        ("bump-and-elision", changed(source, data={"result": ELISION_TEXTS[1]}, version=2)),
    ]


@pytest.mark.parametrize(
    "replacement",
    [event for _name, event in i2_mutations()],
    ids=[name for name, _event in i2_mutations()],
)
def test_i2_every_fingerprint_change_is_reported_somehow(replacement: ProjectedEvent) -> None:
    """`updated` / `notices` / `resync` **三者必居其一**，不许"什么都不发"。

    这条是穷举的：任何一种内容变化，只要它没落进这三者，前端手上那份就悄悄错了。
    """
    result = project(seen(*BASE), snap(BASE[0], replacement, BASE[2]), PROFILE)
    reported = (
        replacement.key in keys(result.updated)
        or any(replacement.key in notice.keys for notice in result.notices)
        or result.resync
    )
    assert reported, f"{replacement.data!r} 的变化没有出现在 updated / notices / resync 里"


@pytest.mark.parametrize(
    "replacement",
    [event for _name, event in i2_mutations()],
    ids=[name for name, _event in i2_mutations()],
)
def test_i2_exactly_one_channel_reports_each_change(replacement: ProjectedEvent) -> None:
    """而且**只**由一个渠道报：同时进 `updated` 和 `resync` 会让前端重复渲染。"""
    result = project(seen(*BASE), snap(BASE[0], replacement, BASE[2]), PROFILE)
    channels = [
        bool(result.updated),
        bool(result.notices) and not result.resync,
        bool(result.resync),
    ]
    assert sum(channels) == 1, channels


def test_i2_a_change_is_still_reported_after_a_resync() -> None:
    """重同步之后 state 换代了，紧接着的一次普通更新照样要报出来。"""
    after_resync = project(seen(*BASE), snap(BASE[0]), PROFILE)
    bumped = changed(BASE[0], data={"role": "assistant", "content": "more"}, version=1)
    result = project(after_resync.state, snap(bumped), PROFILE)
    assert keys(result.updated) == ("chat_1",)
    assert result.state.epoch == after_resync.state.epoch


# --------------------------------------------------------------------------- I3：淘汰识别是排他的


NOT_ELISION_TEXTS = (
    "elided",
    "screenshot",
    "older screenshot elided to bound context memory",
    "[older screenshot elided to bound context memory!]",
    "[OLDER SCREENSHOT ELIDED TO BOUND CONTEXT MEMORY]",
    "[older screenshot elided]",
    "[image rejected]",
    "[screenshot omitted]",
    "screenshot elided to bound context",
    "",
)


@pytest.mark.parametrize("text", NOT_ELISION_TEXTS, ids=[str(i) for i in range(10)])
def test_i3_only_the_three_literals_count_as_elision(text: str) -> None:
    """光是"像是淘汰"不算淘汰：模糊匹配会把真正的改写吞掉，那是静默数据错。"""
    before = ev("tool_2", data={"result": "shot"}, version=1)
    result = project(seen(before), snap(changed(before, data={"result": text})), PROFILE)
    assert result.resync is True
    assert result.resync_reason == RESYNC_MUTATED
    assert notice_codes(result) == (NOTICE_STREAM_RESYNCED,)
    assert result.state.epoch == 1


@pytest.mark.parametrize("text", ELISION_TEXTS, ids=["rejected", "elided", "inherited"])
def test_i3_the_three_literals_do_count(text: str) -> None:
    before = ev("tool_2", data={"result": "shot"}, version=1)
    result = project(seen(before), snap(changed(before, data={"result": text})), PROFILE)
    assert result.resync is False
    assert notice_codes(result) == (NOTICE_SCREENSHOT_ELIDED,)


def test_i3_a_literal_plus_another_change_still_counts_as_elision() -> None:
    """已知取舍：判定只看"新 data 里出现了字面量、旧的没有"，不比对其余字段。

    要区分"只换了 output"和"顺手改了别的"就得把整份旧 payload 留在状态里（每个 run
    上万条事件）。而上游 `_elided_output` 是**原位**替换、不动同一条事件的其他字段，
    所以这个输入实际不会出现 —— 为一个不会发生的情形付那份内存不值。
    """
    before = ev("tool_2", data={"tool_name": "browser", "result": "shot"}, version=1)
    after = changed(before, data={"tool_name": "renamed", "result": ELISION_TEXTS[1]})
    result = project(seen(before), snap(after), PROFILE)
    assert notice_codes(result) == (NOTICE_SCREENSHOT_ELIDED,)


# --------------------------------------------------------------------------- fingerprint


def test_fingerprint_is_sha256_of_canonical_json() -> None:
    data = {"b": 1, "a": [1, 2]}
    expected = hashlib.sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    assert fingerprint_of(data) == expected


def test_fingerprint_ignores_key_order() -> None:
    assert fingerprint_of({"a": 1, "b": 2}) == fingerprint_of({"b": 2, "a": 1})


def test_fingerprint_separates_different_payloads() -> None:
    assert fingerprint_of({"a": 1}) != fingerprint_of({"a": "1"})


def test_fingerprint_keeps_non_ascii_verbatim() -> None:
    """`ensure_ascii=False` 是定义的一部分：中文正文不该让指纹变成一串转义。"""
    assert fingerprint_of({"a": "漏洞"}) != fingerprint_of({"a": "漏"})
    assert fingerprint_of({"a": "漏洞"}) == hashlib.sha256('{"a":"漏洞"}'.encode()).hexdigest()


def test_timestamp_alone_never_looks_like_a_change() -> None:
    """`_bump_event` 每次都重写 `timestamp` —— 指纹含它就会把"什么都没变"报成变了。"""
    before = ev("tool_2", data={"result": "ok"}, version=1)
    after = dataclasses.replace(before, ts="2099-12-31T23:59:59+00:00")
    result = project(seen(before), snap(after), PROFILE)
    assert (result.added, result.updated, result.notices, result.resync) == ((), (), (), False)


# --------------------------------------------------------------------------- 形状


@pytest.mark.parametrize(
    "cls",
    [ProjectedEvent, ProjectedAgent, RunSnapshot, ProjectionState, ProjectionResult, Notice],
)
def test_dataclasses_are_frozen(cls: type) -> None:
    """状态由调用方持有并逐轮替换 —— 可变的话 T14 的 tick 循环就会就地改历史。"""
    assert dataclasses.fields(cls)
    assert cls.__dataclass_params__.frozen is True  # type: ignore[attr-defined]


def test_notice_codes_are_the_agreed_machine_codes() -> None:
    """前端按码分支、不匹配文案，所以这三个字符串是接口的一部分。"""
    assert NOTICE_SCREENSHOT_ELIDED == "screenshot_elided"
    assert NOTICE_CONTEXT_COMPACTED == "context_compacted"
    assert NOTICE_STREAM_RESYNCED == "stream_resynced"


def test_resync_reasons_are_the_agreed_machine_codes() -> None:
    assert (RESYNC_SHRANK, RESYNC_PREFIX_UNSTABLE, RESYNC_MUTATED) == (
        "shrank",
        "prefix_unstable",
        "mutated",
    )


def test_project_does_not_mutate_the_state_it_was_given() -> None:
    previous = seen(*BASE)
    before = (previous.epoch, previous.order, dict(previous.fingerprints))
    project(previous, snap(BASE[0]), PROFILE)
    assert (previous.epoch, previous.order, dict(previous.fingerprints)) == before


def test_run_projector_never_imports_strix() -> None:
    """服务层零 `strix` import —— 唯一的桥是 `app.strix_bridge`（T29 用 import-linter 强制）。"""
    source = Path(run_projector.__file__).read_text(encoding="utf-8")
    assert "import strix" not in source
    assert "from strix" not in source
