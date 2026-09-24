"""续跑准入判定表：放行正路、逐条拒绝、拒绝的优先级、reason 清单与测试对表。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.services.scan_resume import RESUME_REFUSAL_REASONS, ResumeFacts, resume_refusal

_ALLOWED = ResumeFacts(
    status="stopped",
    error_code="scan_incomplete",
    strix_version="1.6.2",
    current_strix_version="1.6.2",
    is_running=False,
    run_name="app-example-com_a1b2",
    has_checkpoint=True,
)


def _facts(**overrides: object) -> ResumeFacts:
    return replace(_ALLOWED, **overrides)  # type: ignore[arg-type]  # 覆盖值由各用例保证类型


@pytest.mark.parametrize(
    ("status", "error_code"),
    [
        ("stopped", "scan_incomplete"),
        ("stopped", "stopped_by_operator"),
        ("stopped", "coverage_incomplete"),
        ("interrupted", "interrupted_by_restart"),
        ("interrupted", "scan_incomplete"),
    ],
)
def test_resumable_combinations_pass(status: str, error_code: str) -> None:
    assert resume_refusal(_facts(status=status, error_code=error_code)) is None


_REFUSALS: list[tuple[dict[str, object], str]] = [
    ({"status": "completed"}, "not_resumable"),
    ({"status": "failed"}, "not_resumable"),
    ({"status": "running"}, "not_resumable"),
    ({"status": "starting"}, "not_resumable"),
    ({"error_code": None}, "not_resumable"),
    ({"error_code": "scan_failed_unknown"}, "not_resumable"),
    ({"is_running": True}, "not_resumable"),
    ({"strix_version": "1.6.1"}, "strix_version_changed"),
    ({"strix_version": None}, "strix_version_changed"),
    ({"run_name": None}, "no_checkpoint"),
    ({"run_name": ""}, "no_checkpoint"),
    ({"has_checkpoint": False}, "no_checkpoint"),
    # 优先级：先判能不能续，再判版本，最后判断点
    ({"status": "completed", "strix_version": "1.6.1"}, "not_resumable"),
    ({"strix_version": None, "has_checkpoint": False}, "strix_version_changed"),
]


@pytest.mark.parametrize(("overrides", "reason"), _REFUSALS)
def test_refusal_reason(overrides: dict[str, object], reason: str) -> None:
    assert resume_refusal(_facts(**overrides)) == reason


def test_every_reason_is_tested() -> None:
    assert set(RESUME_REFUSAL_REASONS) == {reason for _, reason in _REFUSALS}
