"""续跑前改写 `.state/agents.json`：复活被中断的子 agent，清掉过期的预算闸。

纯同步、零 `strix` import（异步化交给调用方 `asyncio.to_thread`），风格与
`services/run_discovery.py` 一致：路径字面值从 profile 取，本模块不自己定义。

**为什么复活 + 清 flag 必须一起做（2026-09-25 读 strix 源码 + 实测锚定）**：headless 下
`respawn_subagents`（`core/execution.py`）只复活 `status in {"running","waiting"}` 的**非根**
agent，`restore`（`core/agents.py`）原样拷状态、不做归一化 —— 所以把 `stopped`/`budget_paused`
的子 agent 直接翻成 `running`，续跑时它们就会被 respawn 重建。但 `wait_for_message`
（`core/agents.py:348-352`）在 `_budget_stopped` 为真、或（子 agent 且 `_reserve_stopped`）时
**立即返回、只为走收尾**，所以复活的同时必须把那三个过期预算 flag 清成 False，否则子 agent
一醒就收尾、根本不补测。root（`parent_of` 为 None）永远不碰：respawn 本就跳过它。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from app.strix_profile import StrixProfile

logger = logging.getLogger(__name__)

# respawn 白名单外、又代表"被中断"的两个状态：翻成 running 才会被重建。
# `completed`（干净跑完）/`crashed`（循环崩）/`failed` 一律不碰。
REVIVABLE_STATUSES: Final = frozenset({"stopped", "budget_paused"})
REVIVED_STATUS: Final = "running"
# 复活的子 agent 一醒就会读这三个 flag 决定走不走收尾，全部清成 False。
STALE_BUDGET_FLAGS: Final = ("budget_stopped", "reserve_stopped", "budget_paused")


@dataclass(frozen=True, slots=True)
class RevivedCheckpoint:
    """`revive_snapshot` 的产物：改写后的快照 + 改了什么。`snapshot` 与入参不共享可变子对象。"""

    snapshot: dict[str, Any]
    revived: tuple[str, ...]
    cleared_flags: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return bool(self.revived or self.cleared_flags)


def revive_snapshot(snapshot: dict[str, Any]) -> RevivedCheckpoint:
    """纯函数。把被中断的**非根**子 agent 翻成 `running`、清过期预算 flag。不改入参。

    `statuses` 或 `parent_of` 形状不对（认不出谁是根）就整体 no-op：宁可续跑退回旧行为，
    也不冒险改错状态。
    """
    statuses = snapshot.get("statuses")
    parent_of = snapshot.get("parent_of")
    result = dict(snapshot)
    if not isinstance(statuses, dict) or not isinstance(parent_of, dict):
        return RevivedCheckpoint(snapshot=result, revived=(), cleared_flags=())

    new_statuses = dict(statuses)
    revived: list[str] = []
    for aid, status in statuses.items():
        if parent_of.get(aid) is None:  # root（或没爹的孤儿）—— respawn 本就跳过
            continue
        if status in REVIVABLE_STATUSES:
            new_statuses[aid] = REVIVED_STATUS
            revived.append(aid)
    result["statuses"] = new_statuses

    cleared: list[str] = []
    for flag in STALE_BUDGET_FLAGS:
        if snapshot.get(flag):
            result[flag] = False
            cleared.append(flag)

    return RevivedCheckpoint(snapshot=result, revived=tuple(revived), cleared_flags=tuple(cleared))


def revive_checkpoint(run_dir: Path, profile: StrixProfile) -> tuple[str, ...]:
    """读 `agents.json` → `revive_snapshot` → 有改动才原子写回。返回被复活的 agent id。

    **任何读错都不改写、返回 `()`**：缺文件、半截 JSON、形状不对 —— 续跑指令据此退回旧措辞，
    不因此失败（与 `run_discovery` 同一种"没有证据就退回"的克制）。没有可复活的子 agent 时
    也不落盘：避免无谓改 mtime。
    """
    path = run_dir / profile.agents_record_rel_path
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ()
    try:
        snapshot = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(snapshot, dict):
        return ()

    outcome = revive_snapshot(snapshot)
    if not outcome.changed:
        return ()
    _atomic_write(path, json.dumps(outcome.snapshot))
    logger.info(
        "续跑复活子 agent",
        extra={
            "run_dir": str(run_dir),
            "revived_count": len(outcome.revived),
            "cleared_flags": list(outcome.cleared_flags),
        },
    )
    return outcome.revived


def _atomic_write(path: Path, text: str) -> None:
    """临时文件 + `os.replace`。形状照 `services/allowlist.py::_atomic_write`。

    原子性这里不是为并发（写它时 Strix 进程还没起、supervisor 已确认没在跑），而是崩溃
    安全：中途被 kill 时留下的是完整旧文件、不是半截的 agent 图。`0o600`：与 `${DATA}` 里
    其它文件一致（图里没有凭据，但它是一份现成的攻击面侦察结果）。
    """
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        os.unlink(tmp_path)
        raise
    os.replace(tmp_path, path)
