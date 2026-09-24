"""续跑准入：一张判定表，无 IO。

只回答"这次扫描的状态允不允许续跑"。它不管的事各有自己的码与位置：
凭据（`key_required`，KeyVault）、产物已清理（`artifacts_purged`，路由查磁盘）、
DNS 复核与预算（启动前护栏）。事实由路由收集（DB 行、supervisor、磁盘）后传进来，
拒绝时由路由抛 `409 resume_unavailable{reason}`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.errors import assert_scan_failure_code

RESUMABLE_STATUSES: Final = frozenset({"stopped", "interrupted"})
RESUMABLE_ERROR_CODES: Final = frozenset(
    assert_scan_failure_code(code)  # 拼错码时导入即炸，而不是静默永不匹配
    for code in (
        "scan_incomplete",
        "stopped_by_operator",
        "interrupted_by_restart",
        "coverage_incomplete",
    )
)
RESUME_REFUSAL_REASONS: Final = ("not_resumable", "strix_version_changed", "no_checkpoint")
"""全部 reason。T31b5 的 `zh-CN.json` 覆盖测试拿它对表。"""


@dataclass(frozen=True, slots=True)
class ResumeFacts:
    status: str
    error_code: str | None
    strix_version: str | None  # scans.strix_version：换版本后产物格式与 strix_profile 都不保证兼容
    current_strix_version: str  # 本进程的版本，与上面比对
    is_running: bool  # supervisor 里还有它：同一扫描不能起两个进程
    run_name: str | None  # strix_run_name 或 discover_run 回落：--resume 要靠它定位产物目录
    # <run_dir>/.state/agents.json 存在：缺它 Strix 以退出码 2 拒绝，会被我们误读成"发现漏洞"
    has_checkpoint: bool


def resume_refusal(facts: ResumeFacts) -> str | None:
    """放行返回 `None`，否则返回第一条不满足的 reason（顺序即优先级）。

    `completed`／`failed` 永不可续：前者已跑完，后者的失败原因续跑也不会消失。
    """
    if (
        facts.is_running
        or facts.status not in RESUMABLE_STATUSES
        or facts.error_code not in RESUMABLE_ERROR_CODES
    ):
        return "not_resumable"
    if facts.strix_version != facts.current_strix_version:
        return "strix_version_changed"
    if not facts.run_name or not facts.has_checkpoint:
        return "no_checkpoint"
    return None
