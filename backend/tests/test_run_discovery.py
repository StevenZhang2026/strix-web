"""`RunDiscovery`：在扫描的 cwd 下找到本次 run 目录、读它的 `status`。

两个函数都显式收 profile —— 这里的测试也照做，免得有人给它加一个"默认当前版本"的
默认参数（那就是模块级可变全局状态的伪装）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.services.run_discovery import (
    discover_run,
    read_coverage_complete,
    read_coverage_gaps,
    read_run_instruction,
    read_run_status,
)
from app.strix_profile import profile_for
from tests.conftest import make_coverage_json, make_run_dir

PROFILE = profile_for("1.6.2")


def test_finds_the_single_run_dir(tmp_path: Path) -> None:
    make_run_dir(tmp_path, name="strix-run-1")
    found = discover_run(tmp_path, PROFILE)
    assert found is not None
    assert found.run_name == "strix-run-1"
    assert found.run_dir == tmp_path / "strix_runs" / "strix-run-1"


def test_no_runs_dir_yet(tmp_path: Path) -> None:
    """还没建 `strix_runs/` 不是错误 —— 它出现在拉镜像之后，可能要等好几分钟。"""
    assert discover_run(tmp_path, PROFILE) is None


def test_empty_runs_dir(tmp_path: Path) -> None:
    (tmp_path / "strix_runs").mkdir()
    assert discover_run(tmp_path, PROFILE) is None


def test_two_run_dirs_picks_newest_by_mtime(tmp_path: Path) -> None:
    """多一个目录不该让一次成功的扫描变成失败，所以取最新的那个（只 warning）。"""
    old = make_run_dir(tmp_path, name="strix-run-old")
    new = make_run_dir(tmp_path, name="strix-run-new")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))
    found = discover_run(tmp_path, PROFILE)
    assert found is not None
    assert found.run_name == "strix-run-new"


def test_finds_run_dir_before_run_json_exists(tmp_path: Path) -> None:
    """run 目录一定先于 `run.json` 出现，而我们要尽早拿到 run name。"""
    (tmp_path / "strix_runs" / "strix-run-1").mkdir(parents=True)
    found = discover_run(tmp_path, PROFILE)
    assert found is not None
    assert found.run_name == "strix-run-1"


def test_status_of_completed_run(tmp_path: Path) -> None:
    run_dir = make_run_dir(tmp_path, status="completed")
    assert read_run_status(run_dir, PROFILE) == "completed"


def test_missing_run_json_is_none(tmp_path: Path) -> None:
    run_dir = tmp_path / "strix_runs" / "strix-run-1"
    run_dir.mkdir(parents=True)
    assert read_run_status(run_dir, PROFILE) is None


def test_truncated_run_json_is_none_not_raise(tmp_path: Path) -> None:
    """`run.json` 每有新发现就被整体重写 —— 轮询时读到半截文件是必然事件。"""
    run_dir = make_run_dir(tmp_path)
    (run_dir / "run.json").write_text('{"status": "comp', encoding="utf-8")
    assert read_run_status(run_dir, PROFILE) is None


def test_run_json_without_status_key_is_none(tmp_path: Path) -> None:
    run_dir = make_run_dir(tmp_path)
    (run_dir / "run.json").write_text('{"run_name": "x"}', encoding="utf-8")
    assert read_run_status(run_dir, PROFILE) is None


def test_interrupted_status_is_returned_verbatim(tmp_path: Path) -> None:
    """信号停止最终留在盘上的就是这个值，它必须原样传出去。"""
    run_dir = make_run_dir(tmp_path, status="interrupted")
    assert read_run_status(run_dir, PROFILE) == "interrupted"


def test_unknown_status_is_returned_verbatim(tmp_path: Path) -> None:
    """取值域外的状态**原样返回**（只 warning）：判死它反而把升级信号丢了。"""
    run_dir = make_run_dir(tmp_path, status="teleported")
    assert read_run_status(run_dir, PROFILE) == "teleported"


def test_instruction_is_read_back_verbatim(tmp_path: Path) -> None:
    run_dir = make_run_dir(tmp_path, instruction="line one\n- role=u username=a password=p")
    assert read_run_instruction(run_dir, PROFILE) == "line one\n- role=u username=a password=p"


def test_instruction_of_missing_run_json_is_none(tmp_path: Path) -> None:
    run_dir = tmp_path / "strix_runs" / "strix-run-1"
    run_dir.mkdir(parents=True)
    assert read_run_instruction(run_dir, PROFILE) is None


def test_non_string_instruction_is_none(tmp_path: Path) -> None:
    run_dir = make_run_dir(tmp_path, instruction=123)
    assert read_run_instruction(run_dir, PROFILE) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param({"completeness": {"complete": True}}, True, id="true"),
        pytest.param({"completeness": {"complete": False}}, False, id="false"),
        pytest.param({"completeness": {"complete": "true"}}, False, id="string-true"),
        pytest.param({"completeness": {"complete": 1}}, False, id="one"),
        pytest.param({"completeness": {"complete": None}}, False, id="null"),
        pytest.param(None, False, id="missing-file"),
        pytest.param("{not json", False, id="broken-json"),
        pytest.param("[]", False, id="top-level-list"),
        pytest.param('{"completeness": true}', False, id="completeness-not-dict"),
    ],
)
def test_coverage_complete_only_on_literal_true(
    tmp_path: Path, raw: dict[str, dict[str, object]] | str | None, expected: bool
) -> None:
    """没有"覆盖完整"的证据就不能说目标没问题 —— 缺失、读坏、形状不对一律 `False`，且不抛。"""
    run_dir = make_run_dir(tmp_path)
    if isinstance(raw, dict):
        make_coverage_json(run_dir, complete=raw["completeness"]["complete"])
    elif isinstance(raw, str):
        (run_dir / "coverage.json").write_text(raw, encoding="utf-8")
    assert read_coverage_complete(run_dir, PROFILE) is expected


def test_coverage_gaps_extracts_safe_surface_labels(tmp_path: Path) -> None:
    """两类 gap 的受控词表字段都取，去重、保序。"""
    run_dir = make_run_dir(tmp_path)
    make_coverage_json(
        run_dir,
        complete=False,
        gaps=[
            {"kind": "unrecorded_risk_class", "risk_area": "sql injection", "detail": "prose"},
            {"kind": "unrecorded_risk_class", "risk_area": "xss", "detail": "prose"},
            {"kind": "agent_recorded_no_coverage", "agent_name": "idor-tester", "detail": "p"},
            {"kind": "unrecorded_risk_class", "risk_area": "sql injection", "detail": "dup"},
        ],
    )
    assert read_coverage_gaps(run_dir, PROFILE) == ("sql injection", "xss", "idor-tester")


def test_coverage_gaps_never_leak_the_detail_prose(tmp_path: Path) -> None:
    """`detail` 是 LLM 散文、可能含口令 —— 绝不进返回值。"""
    run_dir = make_run_dir(tmp_path)
    make_coverage_json(
        run_dir,
        complete=False,
        gaps=[{"kind": "unrecorded_risk_class", "risk_area": "ssrf", "detail": "hunter2 secret"}],
    )
    labels = read_coverage_gaps(run_dir, PROFILE)
    assert labels == ("ssrf",)
    assert all("hunter2" not in label for label in labels)


@pytest.mark.parametrize(
    ("gaps", "expected"),
    [
        pytest.param(None, (), id="no-gaps-key"),
        pytest.param([], (), id="empty-gaps"),
        pytest.param("nope", (), id="gaps-not-a-list"),
        pytest.param([{"kind": "x"}, "junk", 3], (), id="entries-without-usable-fields"),
        pytest.param(
            [{"kind": "unrecorded_risk_class", "risk_area": "  csrf  "}], ("csrf",), id="stripped"
        ),
        pytest.param(
            [{"kind": "unrecorded_risk_class", "risk_area": ""}], (), id="empty-label-skipped"
        ),
        pytest.param(
            [{"kind": "unrecorded_risk_class", "risk_area": 42}], (), id="non-string-skipped"
        ),
    ],
)
def test_coverage_gaps_tolerates_missing_and_malformed(
    tmp_path: Path, gaps: object, expected: tuple[str, ...]
) -> None:
    run_dir = make_run_dir(tmp_path)
    make_coverage_json(run_dir, complete=False, gaps=gaps)
    assert read_coverage_gaps(run_dir, PROFILE) == expected


def test_coverage_gaps_returns_empty_when_file_missing(tmp_path: Path) -> None:
    run_dir = make_run_dir(tmp_path)
    assert read_coverage_gaps(run_dir, PROFILE) == ()
