"""`read_run_dir`：读一个真的 run 目录（真 SQLite 的 `agents.db`），翻成 `RunSnapshot`。

按 `agent-rules.md` §十.5 第二层（接线层）写：不 TDD，一条正路 + 一条错误形状。
但**"事件不翻倍"那条不是接线**：它是 `hydrate_from_run_dir` 不清状态这一条实测事实的
唯一守卫（`projection.py` 模块 docstring），复用一个 view 就必须有测试变红。

夹具是现场用 `make_agents_db` / `make_agents_json` 造的真库（`conftest.py`），
镜像里装的是真的 `strix-agent==1.6.2`，所以这些用例跑的是上游的解析路径。
"""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path

import pytest

from app.services.run_projector import (
    NOTICE_CONTEXT_COMPACTED,
    NOTICE_SCREENSHOT_ELIDED,
    RESYNC_PREFIX_UNSTABLE,
    ProjectionState,
    fingerprint_of,
    project,
)
from app.strix_bridge import projection
from app.strix_bridge.projection import read_run_dir
from app.strix_profile import profile_for
from tests.conftest import make_agents_db, make_agents_json, make_run_dir

PROFILE = profile_for("1.6.2")

# 每个 agent 的**第一条 user turn** 会被上游当成"它被派的任务"丢掉
# （`live_view.py:258`，2026-09-17 实测确认与位置无关，只跟"是不是第一条 user"有关）。
# 所以凡是要让某条 user turn 真的出现在投影里，前面都得先垫这一条。
PLACEHOLDER = {"role": "user", "content": "the task this agent was given"}


def assistant(content: str) -> dict[str, object]:
    return {"role": "assistant", "content": content}


def call(call_id: str, name: str = "exec_command", **args: object) -> dict[str, object]:
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(args),
    }


def output(call_id: str, value: object) -> dict[str, object]:
    """`value` 收 `object`：这里要能塞进"上游可能写下的任何 output"，含非 JSON 的裸字符串。"""
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": value if isinstance(value, str) else json.dumps(value),
    }


def build_run(
    tmp_path: Path,
    items: list[tuple[str, dict[str, object]]],
    *,
    statuses: dict[str, str] | None = None,
    name: str = "strix-run-1",
    status: str = "running",
    **record: object,
) -> Path:
    """一个带 `run.json` + `.state/agents.json` + `.state/agents.db` 的 run 目录。"""
    run_dir = make_run_dir(tmp_path, name=name, status=status, **record)
    make_agents_json(run_dir, statuses or {"root": "running"})
    make_agents_db(run_dir, items)
    return run_dir


def rewrite_db(run_dir: Path, items: list[tuple[str, dict[str, object]]]) -> None:
    """整份换掉 `agents.db` —— 模拟上游 `clear_session()` 后重插。"""
    (run_dir / ".state" / "agents.db").unlink()
    make_agents_db(run_dir, items)


SCREENSHOT = [{"type": "image", "image_url": "data:image/png;base64,AAA"}]
ELIDED = PROFILE.image_elision_texts[1]


# --------------------------------------------------------------------------- 事件


def test_an_assistant_turn_becomes_one_chat_event(tmp_path: Path) -> None:
    run_dir = build_run(tmp_path, [("root", assistant("looking at the login form"))])
    snapshot = read_run_dir(run_dir, PROFILE)
    assert len(snapshot.events) == 1
    event = snapshot.events[0]
    assert (event.key, event.kind, event.agent_id) == ("chat_1", "chat", "root")
    assert event.data["role"] == "assistant"
    assert event.data["content"] == "looking at the login form"
    assert event.upstream_version == 0
    assert event.ts is not None


def test_a_tool_call_and_its_output_are_one_event_bumped_once(tmp_path: Path) -> None:
    """`_record_tool_output_data` 更新的是**同一条**事件，末尾 `_bump_event` → version 1。"""
    run_dir = build_run(
        tmp_path,
        [
            ("root", PLACEHOLDER),
            ("root", call("c1", command="ls")),
            ("root", output("c1", "total 0")),
        ],
    )
    snapshot = read_run_dir(run_dir, PROFILE)
    assert len(snapshot.events) == 1
    event = snapshot.events[0]
    assert (event.key, event.kind, event.upstream_version) == ("tool_1", "tool", 1)
    assert event.data["tool_name"] == "exec_command"
    assert event.data["args"] == {"command": "ls"}
    assert event.data["status"] == "completed"
    assert event.data["result"] == "total 0"


def test_events_keep_the_upstream_order(tmp_path: Path) -> None:
    run_dir = build_run(
        tmp_path,
        [
            ("root", PLACEHOLDER),
            ("root", assistant("first")),
            ("root", call("c1")),
            ("root", output("c1", "done")),
            ("root", assistant("second")),
        ],
    )
    snapshot = read_run_dir(run_dir, PROFILE)
    assert tuple(event.key for event in snapshot.events) == ("chat_1", "tool_2", "chat_3")


def test_the_first_user_turn_is_dropped(tmp_path: Path) -> None:
    """上游把每个 agent 的第一条 user turn 当成"它被派的任务"，不进 transcript。"""
    run_dir = build_run(
        tmp_path,
        [("root", {"role": "user", "content": "scan https://example.com"})],
    )
    assert read_run_dir(run_dir, PROFILE).events == ()


def test_a_later_user_turn_survives(tmp_path: Path) -> None:
    run_dir = build_run(
        tmp_path,
        [("root", PLACEHOLDER), ("root", {"role": "user", "content": "also try the API"})],
    )
    events = read_run_dir(run_dir, PROFILE).events
    assert len(events) == 1
    assert events[0].data["content"] == "also try the API"


def test_internal_agent_turns_are_dropped(tmp_path: Path) -> None:
    """`_INTERNAL_TURN_PREFIXES`：协调者转发的消息不是"用户说的话"。"""
    run_dir = build_run(
        tmp_path,
        [
            ("root", PLACEHOLDER),
            ("root", {"role": "user", "content": "[Message from scout] found something"}),
            ("root", assistant("ok")),
        ],
    )
    events = read_run_dir(run_dir, PROFILE).events
    assert tuple(event.data["content"] for event in events) == ("ok",)


def test_each_event_fingerprint_is_the_hash_of_its_own_data(tmp_path: Path) -> None:
    run_dir = build_run(tmp_path, [("root", assistant("hello"))])
    event = read_run_dir(run_dir, PROFILE).events[0]
    assert event.fingerprint == fingerprint_of(event.data)


# --------------------------------------------------------------------------- 不翻倍


def test_reading_twice_does_not_double_the_events(tmp_path: Path) -> None:
    """`hydrate_from_run_dir` **不清状态**：复用一个 view 就会把整段历史再放一遍。

    所以 `read_run_dir` 每次都必须新建 `TuiLiveView()`。这条是那个事实的唯一守卫。
    """
    run_dir = build_run(
        tmp_path,
        [("root", PLACEHOLDER), ("root", assistant("hello")), ("root", assistant("again"))],
    )
    first = read_run_dir(run_dir, PROFILE)
    second = read_run_dir(run_dir, PROFILE)
    assert len(first.events) == 2
    assert len(second.events) == 2
    assert tuple(e.key for e in second.events) == ("chat_1", "chat_2")


def test_two_reads_project_to_nothing_new(tmp_path: Path) -> None:
    """读两次之间什么都没发生，投影就该一条不发 —— 翻倍会在这里表现成 `added`。"""
    run_dir = build_run(tmp_path, [("root", assistant("hello")), ("root", call("c1"))])
    first = project(ProjectionState.empty(), read_run_dir(run_dir, PROFILE), PROFILE)
    second = project(first.state, read_run_dir(run_dir, PROFILE), PROFILE)
    assert len(first.added) == 2
    assert (second.added, second.updated, second.resync) == ((), (), False)


# --------------------------------------------------------------------------- agent 图


def test_the_agent_graph_carries_names_parents_and_errors(tmp_path: Path) -> None:
    run_dir = make_run_dir(tmp_path)
    make_agents_json(
        run_dir,
        {"root": "completed", "scout": "failed"},
        names={"root": "协调者", "scout": "侦察"},
        parent_of={"scout": "root"},
        errors={"scout": "boom"},
    )
    make_agents_db(run_dir, [])
    agents = {agent.id: agent for agent in read_run_dir(run_dir, PROFILE).agents}
    assert agents["root"].name == "协调者"
    assert agents["root"].parent_id is None
    assert agents["root"].status == "completed"
    assert agents["root"].error_message is None
    assert agents["scout"].parent_id == "root"
    assert agents["scout"].error_message == "boom"
    assert agents["scout"].created_at and agents["scout"].updated_at


def test_only_agents_listed_in_statuses_get_their_events_read(tmp_path: Path) -> None:
    """`load_session_history` 按 `statuses` 的 key 过滤 session_id —— 漏登记就没有事件。"""
    run_dir = build_run(
        tmp_path,
        [("root", assistant("mine")), ("ghost", assistant("not listed"))],
        statuses={"root": "running"},
    )
    snapshot = read_run_dir(run_dir, PROFILE)
    assert tuple(agent.id for agent in snapshot.agents) == ("root",)
    assert tuple(event.data["content"] for event in snapshot.events) == ("mine",)


# --------------------------------------------------------------------------- run.json


def test_run_status_and_finished_come_from_the_run_record(tmp_path: Path) -> None:
    run_dir = build_run(
        tmp_path,
        [],
        status="completed",
        end_time="2026-01-01T00:00:00+00:00",
        llm_usage={"cost": 1.25, "input_tokens": 10},
    )
    snapshot = read_run_dir(run_dir, PROFILE)
    assert snapshot.run_status == "completed"
    assert snapshot.finished is True
    assert snapshot.cost_usd == 1.25


def test_a_running_scan_is_not_finished(tmp_path: Path) -> None:
    """`finished` 要求终态**且**有 `end_time` —— 少了后者就是"还在写"。"""
    snapshot = read_run_dir(build_run(tmp_path, [], status="completed"), PROFILE)
    assert snapshot.finished is False


def test_an_unknown_status_is_passed_through(tmp_path: Path) -> None:
    """取值域的判定不在桥里（`read_run_status` 才管），这里只如实带出来。"""
    snapshot = read_run_dir(build_run(tmp_path, [], status="teleported"), PROFILE)
    assert snapshot.run_status == "teleported"


def test_a_half_written_run_json_is_not_an_error(tmp_path: Path) -> None:
    """`run.json` 每有新发现就被整体重写，轮询读到半截文件是必然事件。"""
    run_dir = build_run(tmp_path, [("root", assistant("hello"))])
    (run_dir / "run.json").write_text('{"status": "run', encoding="utf-8")
    snapshot = read_run_dir(run_dir, PROFILE)
    assert snapshot.run_status is None
    assert snapshot.finished is False
    assert len(snapshot.events) == 1


@pytest.mark.parametrize(
    "llm_usage",
    [None, {}, {"cost": "1.25"}, {"cost": None}, {"cost": True}, "nope"],
    ids=["absent", "empty", "string", "null", "bool", "not-a-dict"],
)
def test_cost_is_none_when_it_cannot_be_read(tmp_path: Path, llm_usage: object) -> None:
    """读不到就 `None`，**不是** 0 —— "花了 0 块"会被当成"扫描没起来"。"""
    record: dict[str, object] = {} if llm_usage is None else {"llm_usage": llm_usage}
    snapshot = read_run_dir(build_run(tmp_path, [], **record), PROFILE)
    assert snapshot.cost_usd is None


def test_an_integer_cost_becomes_a_float(tmp_path: Path) -> None:
    snapshot = read_run_dir(build_run(tmp_path, [], llm_usage={"cost": 3}), PROFILE)
    assert snapshot.cost_usd == 3.0
    assert isinstance(snapshot.cost_usd, float)


# --------------------------------------------------------------------------- 漏洞与报告


def test_vulnerabilities_and_severity_buckets(tmp_path: Path) -> None:
    run_dir = build_run(tmp_path, [])
    (run_dir / "vulnerabilities.json").write_text(
        json.dumps(
            [
                {"title": "SQLi", "severity": "CRITICAL"},
                {"title": "XSS", "severity": "medium"},
                {"title": "Info", "severity": "informational"},
            ]
        ),
        encoding="utf-8",
    )
    snapshot = read_run_dir(run_dir, PROFILE)
    assert len(snapshot.vulnerabilities) == 3
    assert snapshot.vulnerabilities[0]["title"] == "SQLi"
    # `informational` 折进 `low`（上游 `severity_counts` 的语义，我们不另立一套）。
    assert dict(snapshot.severity) == {"critical": 1, "high": 0, "medium": 1, "low": 1}


def test_non_dict_vulnerability_entries_are_dropped(tmp_path: Path) -> None:
    """一条不是对象的记录不是漏洞 —— 留着它会让 `low` 桶凭空多一个。"""
    run_dir = build_run(tmp_path, [])
    (run_dir / "vulnerabilities.json").write_text(
        json.dumps([{"severity": "high"}, "garbage", None]), encoding="utf-8"
    )
    snapshot = read_run_dir(run_dir, PROFILE)
    assert len(snapshot.vulnerabilities) == 1
    assert dict(snapshot.severity) == {"critical": 0, "high": 1, "medium": 0, "low": 0}


def test_the_report_markdown_is_read_verbatim(tmp_path: Path) -> None:
    run_dir = build_run(tmp_path, [])
    (run_dir / "penetration_test_report.md").write_text("# 报告\n\n正文", encoding="utf-8")
    assert read_run_dir(run_dir, PROFILE).report_markdown == "# 报告\n\n正文"


def test_missing_products_are_empty_not_errors(tmp_path: Path) -> None:
    snapshot = read_run_dir(build_run(tmp_path, []), PROFILE)
    assert snapshot.vulnerabilities == ()
    assert snapshot.report_markdown == ""
    assert dict(snapshot.severity) == {"critical": 0, "high": 0, "medium": 0, "low": 0}


# --------------------------------------------------------------------------- 错误形状


def test_a_missing_run_dir_is_an_empty_snapshot(tmp_path: Path) -> None:
    """run 目录先于 `run.json` 出现，轮询早于两者 —— 这不是错误，是"还没到"。"""
    snapshot = read_run_dir(tmp_path / "strix_runs" / "not-there", PROFILE)
    assert snapshot.agents == ()
    assert snapshot.events == ()
    assert snapshot.run_status is None
    assert snapshot.finished is False
    assert snapshot.cost_usd is None
    assert snapshot.report_markdown == ""


def test_a_run_dir_without_state_has_no_events(tmp_path: Path) -> None:
    """`.state/agents.json` 还没写出来：agent 图与事件都是空的，其余照常读。"""
    run_dir = make_run_dir(tmp_path, status="running")
    snapshot = read_run_dir(run_dir, PROFILE)
    assert (snapshot.agents, snapshot.events) == ((), ())
    assert snapshot.run_status == "running"


def test_an_unreadable_agents_db_is_not_an_error(tmp_path: Path) -> None:
    run_dir = build_run(tmp_path, [("root", assistant("hello"))])
    (run_dir / ".state" / "agents.db").write_bytes(b"not a database")
    snapshot = read_run_dir(run_dir, PROFILE)
    assert snapshot.events == ()
    assert tuple(agent.id for agent in snapshot.agents) == ("root",)


# --------------------------------------------------------------------------- profile 驱动


def test_an_unregistered_event_kind_warns_and_is_projected_anyway(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """事件类型的取值域从 profile 取，不是硬编码 —— 升级时多出一种要看得见。"""
    narrowed = dataclasses.replace(PROFILE, event_kinds=frozenset({"chat"}))
    run_dir = build_run(tmp_path, [("root", PLACEHOLDER), ("root", call("c1"))])
    with caplog.at_level(logging.WARNING, logger=projection.logger.name):
        snapshot = read_run_dir(run_dir, narrowed)
    assert tuple(event.kind for event in snapshot.events) == ("tool",)
    assert "未登记的事件类型" in caplog.text


def test_registered_event_kinds_do_not_warn(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    run_dir = build_run(
        tmp_path,
        [("root", PLACEHOLDER), ("root", assistant("hi")), ("root", call("c1"))],
    )
    with caplog.at_level(logging.WARNING, logger=projection.logger.name):
        read_run_dir(run_dir, PROFILE)
    assert caplog.text == ""


# --------------------------------------------------------------------------- 桥 + 投影


def test_screenshot_elision_shows_up_as_a_notice(tmp_path: Path) -> None:
    """真夹具走一遍：output 从截图换成占位符，事件数与顺序都不变、version 仍是 1。"""
    items = [("root", PLACEHOLDER), ("root", call("c1", name="browser_action"))]
    run_dir = build_run(tmp_path, [*items, ("root", output("c1", SCREENSHOT))])
    before = read_run_dir(run_dir, PROFILE)
    first = project(ProjectionState.empty(), before, PROFILE)
    rewrite_db(run_dir, [*items, ("root", output("c1", ELIDED))])
    after = read_run_dir(run_dir, PROFILE)

    assert tuple(e.key for e in after.events) == tuple(e.key for e in before.events)
    assert after.events[0].upstream_version == before.events[0].upstream_version == 1
    assert after.events[0].fingerprint != before.events[0].fingerprint

    result = project(first.state, after, PROFILE)
    assert result.resync is False
    assert tuple(notice.code for notice in result.notices) == (NOTICE_SCREENSHOT_ELIDED,)
    assert result.state.epoch == 0


def test_context_compaction_shows_up_as_a_resync(tmp_path: Path) -> None:
    """压缩把历史换成 `[checkpoint, *recent]` → 事件重排 → 重同步 + `context_compacted`。

    `PLACEHOLDER` 垫在前面是必需的：checkpoint 自己就是一条 user turn，若它是第一条
    就会被上游丢掉，那 `context_compacted` 的断言会变成空的（2026-09-17 实测）。
    """
    run_dir = build_run(
        tmp_path,
        [
            ("root", PLACEHOLDER),
            ("root", assistant("early work")),
            ("root", call("c1")),
            ("root", output("c1", "done")),
        ],
    )
    first = project(ProjectionState.empty(), read_run_dir(run_dir, PROFILE), PROFILE)
    assert tuple(e.key for e in first.added) == ("chat_1", "tool_2")

    tag = PROFILE.compaction_checkpoint_tag
    rewrite_db(
        run_dir,
        [
            ("root", PLACEHOLDER),
            ("root", {"role": "user", "content": f"{tag}\nsummary of the early work"}),
            ("root", assistant("recent work")),
        ],
    )
    after = read_run_dir(run_dir, PROFILE)
    assert tag in str(after.events[0].data["content"])

    result = project(first.state, after, PROFILE)
    assert result.resync is True
    assert result.resync_reason == RESYNC_PREFIX_UNSTABLE
    assert tuple(notice.code for notice in result.notices) == (NOTICE_CONTEXT_COMPACTED,)
    assert result.state.epoch == 1
    assert len(result.snapshot) == 2


# --------------------------------------------------------------------------- import 边界


def test_the_bridge_only_imports_the_two_allowed_strix_modules() -> None:
    """T29 会用 import-linter 强制这条；在那之前它靠这条测试守着。

    `strix.core.*` / `strix.runtime.*` / `strix.llm.*` 会把 agents SDK、litellm 与
    `configure_sdk_model_defaults` 对 `os.environ` 的改写拖进 web 进程。
    """
    source = Path(projection.__file__).read_text(encoding="utf-8")
    imported = sorted(
        line.split()[1] for line in source.splitlines() if line.startswith("from strix")
    )
    assert imported == [
        "strix.interface.tui.backend.live_view",
        "strix.interface.viewer.transcript",
    ]
    assert "import strix\n" not in source
