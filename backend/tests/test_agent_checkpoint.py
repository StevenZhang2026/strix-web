"""续跑前改写 `.state/agents.json`：复活被中断的子 agent、清过期预算闸。

分两层测：纯判定 `revive_snapshot`（安全不变式，先看它红）与 IO 包装 `revive_checkpoint`
（读→改→原子写回的接线，测形状）。

**为什么复活 + 清 flag 是一起的（2026-09-25 实测锚定）**：headless 下 `respawn_subagents`
（`core/execution.py`）只复活 `status in {"running","waiting"}` 的非根 agent，所以要把
`stopped`/`budget_paused` 的子 agent 翻成 `running`；但 `AgentCoordinator.wait_for_message`
（`core/agents.py:348-352`）在 `_budget_stopped` 或（子 agent 且 `_reserve_stopped`）时**立即
返回、只为收尾**，所以复活的同时必须把那三个过期预算 flag 清成 False，否则子 agent 一醒就走
收尾路径、不会真的补测（上次手工验证成功只因当时那三个 flag 恰好都是 False）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.agent_checkpoint import (
    REVIVABLE_STATUSES,
    REVIVED_STATUS,
    revive_checkpoint,
    revive_snapshot,
)
from app.strix_profile import profile_for
from tests.conftest import make_agents_json, make_run_dir

PROFILE = profile_for("1.6.2")

# 真实抓下来的图（2026-09-25 juice-shop run）：root 已 completed，三个子 agent 被强停。
_REAL_PARENT_OF = {
    "root": None,
    "auth": "root",
    "access": "root",
    "xss": "root",
}


def _snapshot(statuses: dict[str, str], **flags: object) -> dict[str, object]:
    snap: dict[str, object] = {"statuses": dict(statuses), "parent_of": dict(_REAL_PARENT_OF)}
    snap.update(flags)
    return snap


def test_revives_stopped_children_to_running() -> None:
    out = revive_snapshot(
        _snapshot({"root": "completed", "auth": "stopped", "access": "stopped", "xss": "stopped"})
    )
    assert out.snapshot["statuses"] == {
        "root": "completed",
        "auth": "running",
        "access": "running",
        "xss": "running",
    }
    assert set(out.revived) == {"auth", "access", "xss"}


def test_root_is_never_revived_even_if_stopped() -> None:
    """root 的 `parent_of` 是 None —— respawn 无论如何都跳过它，翻它只会误导。"""
    out = revive_snapshot(_snapshot({"root": "stopped", "auth": "stopped"}))
    assert out.snapshot["statuses"]["root"] == "stopped"
    assert out.snapshot["statuses"]["auth"] == "running"
    assert "root" not in out.revived


def test_completed_and_crashed_children_are_left_alone() -> None:
    """只复活被中断的（stopped/budget_paused）—— 干净跑完的 completed、循环崩的 crashed 不碰。"""
    out = revive_snapshot(
        _snapshot({"root": "completed", "auth": "completed", "access": "crashed", "xss": "failed"})
    )
    assert out.snapshot["statuses"] == {
        "root": "completed",
        "auth": "completed",
        "access": "crashed",
        "xss": "failed",
    }
    assert out.revived == ()


def test_budget_paused_children_are_revived() -> None:
    """`budget_paused` 也不在 respawn 的白名单里，同样要翻成 running。"""
    out = revive_snapshot(_snapshot({"root": "completed", "auth": "budget_paused"}))
    assert out.snapshot["statuses"]["auth"] == "running"
    assert out.revived == ("auth",)


def test_clears_stale_budget_flags() -> None:
    """三个预算 flag 会让复活的子 agent 一醒就走收尾 —— 必须清掉，且如实报告清了哪几个。"""
    out = revive_snapshot(
        _snapshot(
            {"root": "completed", "auth": "stopped"},
            budget_stopped=True,
            reserve_stopped=True,
            budget_paused=False,
        )
    )
    assert out.snapshot["budget_stopped"] is False
    assert out.snapshot["reserve_stopped"] is False
    assert set(out.cleared_flags) == {"budget_stopped", "reserve_stopped"}


def test_does_not_mutate_the_input_snapshot() -> None:
    """纯函数：调用后原对象一个字节不动（否则 IO 层原子写回会被架空）。"""
    original = _snapshot({"root": "completed", "auth": "stopped"}, budget_stopped=True)
    frozen = json.dumps(original, sort_keys=True)
    revive_snapshot(original)
    assert json.dumps(original, sort_keys=True) == frozen


def test_reports_no_change_when_nothing_to_revive() -> None:
    out = revive_snapshot(_snapshot({"root": "completed", "auth": "completed"}))
    assert out.changed is False


@pytest.mark.parametrize(
    "snap",
    [
        pytest.param({"parent_of": {"a": None}}, id="statuses-missing"),
        pytest.param({"statuses": {"a": "stopped"}}, id="parent_of-missing"),
        pytest.param({"statuses": "nope", "parent_of": {}}, id="statuses-not-dict"),
        pytest.param({"statuses": {}, "parent_of": "nope"}, id="parent_of-not-dict"),
    ],
)
def test_noop_when_statuses_or_parent_of_malformed(snap: dict[str, object]) -> None:
    """认不出谁是根就一个都不翻：宁可续跑退回旧行为，也不冒险改错状态。"""
    out = revive_snapshot(snap)
    assert out.revived == ()
    assert out.changed is False


# ---- IO 包装（接线层）----


def test_revive_checkpoint_round_trips(tmp_path: Path) -> None:
    run_dir = make_run_dir(tmp_path, status="stopped")
    make_agents_json(
        run_dir,
        statuses={"root": "completed", "auth": "stopped", "xss": "stopped"},
        parent_of={"root": None, "auth": "root", "xss": "root"},
        names={"root": "Root", "auth": "Auth Agent", "xss": "XSS Agent"},
        budget_stopped=True,
        reserve_stopped=True,
    )
    revived = revive_checkpoint(run_dir, PROFILE)
    assert set(revived) == {"auth", "xss"}

    written = json.loads((run_dir / ".state" / "agents.json").read_text(encoding="utf-8"))
    assert written["statuses"] == {"root": "completed", "auth": "running", "xss": "running"}
    assert written["budget_stopped"] is False
    assert written["reserve_stopped"] is False
    # 旁挂表原样保留：只动 statuses 与 flag。
    assert written["names"] == {"root": "Root", "auth": "Auth Agent", "xss": "XSS Agent"}
    assert written["parent_of"] == {"root": None, "auth": "root", "xss": "root"}


def test_revive_checkpoint_missing_file_returns_empty(tmp_path: Path) -> None:
    run_dir = make_run_dir(tmp_path, status="stopped")
    assert revive_checkpoint(run_dir, PROFILE) == ()


def test_revive_checkpoint_malformed_json_returns_empty_and_leaves_file(tmp_path: Path) -> None:
    run_dir = make_run_dir(tmp_path, status="stopped")
    state = run_dir / ".state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "agents.json").write_text("{not json", encoding="utf-8")
    assert revive_checkpoint(run_dir, PROFILE) == ()
    # 读坏不改写：留着原文让人能排查，别用一份"修好的"盖掉证据。
    assert (state / "agents.json").read_text(encoding="utf-8") == "{not json"


def test_revive_checkpoint_no_children_does_not_rewrite(tmp_path: Path) -> None:
    """没有可复活的子 agent 就不落盘：避免无谓改动 mtime 与文件。"""
    run_dir = make_run_dir(tmp_path, status="stopped")
    path = make_agents_json(
        run_dir,
        statuses={"root": "completed"},
        parent_of={"root": None},
    )
    before = path.stat().st_mtime_ns
    raw_before = path.read_text(encoding="utf-8")
    assert revive_checkpoint(run_dir, PROFILE) == ()
    assert path.read_text(encoding="utf-8") == raw_before
    assert path.stat().st_mtime_ns == before


# ---- 升级预警线：本模块赖以成立的三条上游事实 ----
#
# 这三条是 `revive_snapshot` 的**前提**。任一条在升级后失效，复活就会**静默**失灵：续跑
# 照常退 0，只是又回到"0 个子 agent 补测、coverage 一条不加"。所以宁可在这里响亮地红。
# 读源码文本而不是 `import strix.core.*`：`core` 会把 agents SDK 与 litellm 拖进来
# （模块级全局可变状态，正是 CLAUDE.md §Strix 集成 禁止 web 进程 import 它的理由），
# 而我们要核对的只是几个字面量。


def _strix_source(rel_path: str) -> str:
    strix = pytest.importorskip("strix")
    assert strix.__file__ is not None
    return (Path(strix.__file__).parent / rel_path).read_text(encoding="utf-8")


def test_upstream_respawn_still_filters_on_running_or_waiting() -> None:
    """headless 复活的闸门。这句一变（比如加进 `stopped`，或改成读别的字段），
    翻 `stopped→running` 这件事就要么多余、要么不够。
    """
    source = _strix_source("core/execution.py")
    assert 'if not interactive and status not in {"running", "waiting"}:' in source
    # root 被跳过的那两句：我们据此**不**翻 root。
    assert "if coordinator.parent_of.get(aid) is None or aid == root_id:" in source


def test_upstream_restore_copies_statuses_verbatim() -> None:
    """`restore` 一旦开始归一化状态（例如把 `running` 读成 `stopped`），盘上那个字符串
    就不再是开关，本模块整体失效。
    """
    source = _strix_source("core/agents.py")
    assert 'self.statuses = dict(snap.get("statuses", {}))' in source
    # 三个预算 flag 确实是从快照读回来的 —— 清它们才有意义。
    for flag in ("budget_stopped", "reserve_stopped", "budget_paused"):
        assert f'snap.get("{flag}", False)' in source


def test_upstream_wait_for_message_still_short_circuits_on_budget_flags() -> None:
    """复活后子 agent 会不会真干活，取决于这个门。它就是"必须清 flag"的唯一理由。"""
    source = _strix_source("core/agents.py")
    assert (
        "reserve_exit = self._reserve_stopped and self.parent_of.get(agent_id) is not None"
        in source
    )
    assert "if self._budget_stopped or reserve_exit or pending_ready:" in source


def test_our_status_vocabulary_matches_upstreams() -> None:
    """我们只认 7 个状态。上游加一个新的"被中断"状态时，`REVIVABLE_STATUSES` 要跟着补
    —— 这条红了就是提醒去看那个新状态该不该复活。
    """
    source = _strix_source("core/agents.py")
    assert (
        'Status = Literal["running", "waiting", "completed", "stopped", "crashed", "failed",'
        ' "budget_paused"]' in source
    )
    assert REVIVABLE_STATUSES == {"stopped", "budget_paused"}
    assert REVIVED_STATUS == "running"
