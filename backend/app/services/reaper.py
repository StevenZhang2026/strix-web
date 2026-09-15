"""`Reaper`：按 label 回收泄漏的 Strix 沙箱容器（T11a）。

# 它回收的是哪个泄漏

Strix 的 `session_manager.cleanup` 挂在正常退出路径上。`api` 强杀 strix 子进程时
（停机、或操作者停止后宽限期到点）那段清理跑不完，于是 Strix 创建的**兄弟**沙箱容器
留在宿主上占磁盘和 IP。强杀那一刻 `ScanProcess._stop()` 会打一行
`event=sandbox_cleanup_skipped`（带 `scan_id` / `strix_run_id`）—— **那就是"这次一定
泄漏了"的凭据**，本模块的清扫日志用同一个 `strix_run_id` 键名，T26 诊断页靠这两行对账。

# 三级判据，第二级和第三级都不是可选的

1. daemon 侧 `label=strix-run-type=console`（`ORPHAN_LABEL_SELECTOR`）。
2. 客户端侧再要求 `strix-run-id` 存在且非空（`ORPHAN_REQUIRED_LABEL`）。
   **少了这一级会删掉 M0 靶场**：本机实测过，靶场的启动命令里有我们自己手打的
   `--label strix-run-type=console`，只按第一级过滤时 `count=1`、名字就是它。
   而"要求 run-id 非空"永不误伤真沙箱，因为 Strix **结构上产不出**"有 run-type、
   无 run-id"的容器：`strix/runtime/docker_client.py:113-123` 的 `_apply_run_labels()`
   开头就是 `run_id = os.getenv("STRIX_RUN_ID")` / `if not run_id: return`（早退）。
3. `strix-run-id` 命中**在册扫描**的容器一个都不许删（`STRIX_RUN_ID == scan_id`，
   在册 id 由 `ScanSupervisor.active_scan_ids()` 给）。少了这一级，定时清扫会在扫描
   跑到一半时把它自己的沙箱删掉。

删除**只按 label 选**：不许按"名字像"或"时间早"筛 —— 本机同时跑着另一个 compose 项目。

# 三个触发点，刻意没有第四个

启动一次（`run_forever` 的第一轮）、每 `SWEEP_INTERVAL_S` 一次、每次扫描结束后一次
（`ScanSupervisor(on_scan_finished=...)` → `request_sweep()`）。
**刻意不做停机前的最后一次清扫**：`supervisor.shutdown()` 强杀留下的泄漏由**下次启动
的那一轮**兜住，为停机路径再加一次带超时的 docker 往返不值得（CLAUDE.md §编码哲学 3）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from app.services.docker_probe import (
    DockerApiError,
    DockerProbe,
    DockerTransport,
    OrphanSandbox,
)

logger = logging.getLogger(__name__)

# 定时清扫的间隔。沙箱泄漏是"占资源"而不是"出错"，5 分钟足够小；写成模块常量而不是
# 新增配置项，与 `docker_probe.py` 的立场一致（零新增 env）。
SWEEP_INTERVAL_S = 300.0
# 删一个容器的 docker 往返超时。`force=1` 会先给容器发 KILL 再删，比只读查询慢得多，
# 而 Docker Desktop 的 VM 换页时抖动是真的（同 `PROBE_TIMEOUT_S` 的理由），所以给
# 20 秒；比它更长就会让"docker 卡住"拖住整轮清扫（每个残骸都要等一次）。
REMOVE_TIMEOUT_S = 20.0


@dataclass(frozen=True)
class SweepPlan:
    """一轮清扫的判定结果。**纯数据**，算它的是 `plan_sweep`（无 IO）。"""

    doomed: tuple[OrphanSandbox, ...]
    spared: tuple[OrphanSandbox, ...]


@dataclass(frozen=True)
class SweepReport:
    dry_run: bool
    plan: SweepPlan
    removed: tuple[str, ...]
    failed: tuple[str, ...]


def plan_sweep(sandboxes: tuple[OrphanSandbox, ...], active: frozenset[str]) -> SweepPlan:
    """第三级判据。无 IO 的纯函数 —— "在跑的不许删"是这里唯一要证明的事。"""
    return SweepPlan(
        doomed=tuple(s for s in sandboxes if s.run_id not in active),
        spared=tuple(s for s in sandboxes if s.run_id in active),
    )


def _describe(sandbox: OrphanSandbox) -> dict[str, str]:
    """日志用的一条记录。键名 `strix_run_id` 与 `sandbox_cleanup_skipped` 那行对齐。

    **不许叫 `name`**：这些键会被 `**` splat 进 `logging` 的 `extra`，而 `name` 是
    `LogRecord` 的保留字段，撞上直接 `KeyError`（实测踩到过）。
    """
    return {
        "container_id": sandbox.container_id,
        "container_name": sandbox.name,
        "strix_run_id": sandbox.run_id,
    }


class Reaper:
    """真正持有状态（唤醒事件），所以是 class。"""

    def __init__(
        self, transport: DockerTransport, active_scan_ids: Callable[[], tuple[str, ...]]
    ) -> None:
        # 构造**不跟 docker 说话**：`UnixSocketTransport` 是无状态的，所以 docker 挂着
        # 也不影响 `api` 启动（T3 立下的约束）。
        self._probe = DockerProbe(transport=transport)
        self._transport = transport
        self._active_scan_ids = active_scan_ids
        self._wake = asyncio.Event()

    # ---- 同步侧：全部 docker IO ---------------------------------------------
    def sweep_sync(self, active: frozenset[str], *, dry_run: bool) -> SweepReport:
        """一轮清扫。**同步阻塞**，调用方负责 `asyncio.to_thread`。

        `active` 是参数而不是回调：那个注册表属于事件循环，让工作线程去读它是一个
        只在高并发下发作的竞态。快照在 `sweep()` 里做。
        """
        plan = plan_sweep(self._probe.orphan_sandboxes(), active)
        # **动手之前**先把计划整条打出来（dry_run 与否都打）：破坏性操作必须先可观测。
        logger.info(
            "沙箱清扫计划",
            extra={
                "event": "reaper_sweep_plan",
                "dry_run": dry_run,
                "doomed": [_describe(s) for s in plan.doomed],
                "spared": [_describe(s) for s in plan.spared],
            },
        )
        if dry_run:
            return SweepReport(dry_run=True, plan=plan, removed=(), failed=())
        removed: list[str] = []
        failed: list[str] = []
        for sandbox in plan.doomed:
            outcome = self._remove(sandbox)
            if outcome == "removed":
                removed.append(sandbox.container_id)
            elif outcome == "failed":
                failed.append(sandbox.container_id)
        return SweepReport(dry_run=False, plan=plan, removed=tuple(removed), failed=tuple(failed))

    def _remove(self, sandbox: OrphanSandbox) -> str:
        """删一个。返回 `removed` / `skipped` / `failed`。**单个失败不许中断整轮。**

        `force=1`：泄漏的沙箱可能还在跑。`v=1`：回收它**自己的匿名卷**（具名卷不受
        影响 —— docker 只删这个容器独占的那些）。
        """
        path = f"/containers/{sandbox.container_id}?force=1&v=1"
        try:
            reply = self._transport.request("DELETE", path, timeout_s=REMOVE_TIMEOUT_S)
        except DockerApiError as error:
            # 传输层说不通（socket 断了 / 超时）。**不重抛**：后面那些残骸还要试。
            logger.warning(
                "删沙箱失败",
                extra={
                    "event": "reaper_remove_failed",
                    **_describe(sandbox),
                    "reason": error.reason,
                },
            )
            return "failed"
        if reply.status in (204, 404):
            # 404 也算成功：它已经没了，清扫是幂等的。
            logger.info("已回收泄漏的沙箱", extra={"event": "reaper_removed", **_describe(sandbox)})
            return "removed"
        if reply.status == 409:
            # daemon 说"正在删" —— 下一轮再看，不算失败。
            logger.info(
                "沙箱正在被删除，跳过", extra={"event": "reaper_skipped", **_describe(sandbox)}
            )
            return "skipped"
        logger.warning(
            "删沙箱失败",
            extra={"event": "reaper_remove_failed", **_describe(sandbox), "status": reply.status},
        )
        return "failed"

    # ---- 异步侧：快照 + 调度 -------------------------------------------------
    async def sweep(self, *, dry_run: bool = False) -> SweepReport:
        """在事件循环里快照在册 id，再把 docker IO 整个搬进线程。

        **快照发生在列容器之前，这一点是安全的，但理由是时序而不是结构，所以写在这里**：
        一个容器要想"出现在列表里、却不在这份快照里"，它那次扫描就得在快照与列容器之间
        （一次 `to_thread` 派发 + 一次本机 UDS 往返，毫秒级）**既完成登记又建好沙箱**。
        而登记发生在 `ScanSupervisor.start()` 里 `create_subprocess_exec` 返回后的同一步，
        沙箱要等子进程把 python + strix 跑起来才建，两者差几秒到几十秒。
        —— 如果哪天登记被挪到"子进程已经在跑之后"，这个论证就没了，**那时必须改成
        "先列容器、再快照"**（代价是 `sweep_sync` 那个单一同步入口要拆成两半，而 T30 的
        CLI 正指望它是一整块）。
        """
        active = frozenset(self._active_scan_ids())
        return await asyncio.to_thread(self.sweep_sync, active, dry_run=dry_run)

    def request_sweep(self) -> None:
        """请求尽快清扫一次。同步、非阻塞，可以从 `_forget` 那种回调里调。"""
        self._wake.set()

    async def run_forever(self, interval: float = SWEEP_INTERVAL_S) -> None:
        """启动清扫一次 → 之后「到点」或「被 `request_sweep` 叫醒」任一先到就再清一次。

        `interval` 可注入的唯一理由是测试（同 `key_vault.run_sweeper`）。

        **任何一次清扫失败都不许让这个循环退出**：docker 不可达时 `api` 只该少一个
        后台清理，不该变成半死状态。**刻意不捕获 `CancelledError`** —— 吞掉它的话
        lifespan 关闭时会永远 await 不完。
        """
        while True:
            # 先 clear 再清扫：清扫期间来的 `request_sweep()` 必须留到下一轮，不能被
            # 这一轮"顺手吃掉"（那次请求对应的是一个刚结束、可能刚泄漏的扫描）。
            self._wake.clear()
            try:
                await self.sweep()
            except DockerApiError as error:
                logger.warning(
                    "沙箱清扫失败，等下一轮",
                    extra={"event": "reaper_sweep_failed", "reason": error.reason},
                )
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            except TimeoutError:
                pass  # 到点了，正常的下一轮
