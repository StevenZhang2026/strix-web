"""在扫描的 cwd 下找到本次 run 目录，并读它的 `status`。

CLI 没有 `--output-dir`，产物固定写 `$CWD/strix_runs/<自动名>/` —— 所以"这次扫描的
产物在哪"只能靠**发现**，不能靠约定。每任务独立 cwd 让这件事是确定的（那个目录下最多
一个 run）。

纯同步、零 `strix` import：异步化由调用方 `asyncio.to_thread`（与 `scan_launcher` 同一
风格）。目录名 / 文件名 / 状态取值域三个字面值**都从 profile 取**，本模块不自己定义。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from app.strix_profile import StrixProfile

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DiscoveredRun:
    """一次扫描的产物目录。`run_name` 就是目录名 —— `--resume` 收的正是它（T28）。"""

    run_dir: Path
    run_name: str


def discover_run(cwd: Path, profile: StrixProfile) -> DiscoveredRun | None:
    """找 `cwd/<runs_dir_name>/` 下的 run 目录。还没出现就返回 `None`。

    **不要求 `run.json` 已存在，按目录自己的 mtime 排**：我们要的是"尽早拿到 run name"
    （它是 T11 回收与 T28 续跑的钥匙），而 run 目录一定先于 `run.json` 出现。
    上游 `core/paths.py:31 latest_run_dir()` 反过来（只认已有 `run.json` 的子目录、按
    `run.json` 的 mtime 排）—— 我们是**刻意**不同，不是抄漏了。

    多于一个子目录时取 mtime 最新的那个并 warning，**不抛**：一个多出来的目录不该让
    一次成功的扫描变成失败。
    """
    runs_dir = cwd / profile.runs_dir_name
    if not runs_dir.is_dir():
        return None
    candidates = sorted(
        (path for path in runs_dir.iterdir() if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
    )
    if not candidates:
        return None
    if len(candidates) > 1:
        logger.warning(
            "扫描 cwd 下有多个 run 目录，取 mtime 最新的那个",
            extra={"cwd": str(cwd), "count": len(candidates)},
        )
    run_dir = candidates[-1]
    return DiscoveredRun(run_dir=run_dir, run_name=run_dir.name)


def read_run_status(run_dir: Path, profile: StrixProfile) -> str | None:
    """读 `run.json` 的 `status`。**任何情况都不抛**，读不出来就是 `None`。

    宽容不是防御性冗余：`run.json` 每有新发现就被 Strix **整体重写**，轮询时读到半截
    文件是必然事件 —— `JSONDecodeError` 在这里只意味着"再读一次"。文件还没建、
    没有 `status` 键同理（run 目录先于它出现）。

    取到的值不在 `profile.run_statuses` 里就**原样返回**并 warning：这是升级预警线，
    判死它反而把"上游加了新状态"这个信号丢掉。
    """
    record_path = run_dir / profile.run_record_name
    try:
        raw = record_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        record = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict):
        return None
    status = record.get("status")
    if not isinstance(status, str):
        return None
    if status not in profile.run_statuses:
        logger.warning(
            "run.json 的 status 不在已登记的取值域里，原样上报",
            extra={"run_dir": str(run_dir), "run_status": status},
        )
    return status
