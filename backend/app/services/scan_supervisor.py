"""起 strix 子进程、抽干它的 stdout、把三个信号源交叉成一个终态。

**一进程一次扫描**（模块级全局可变状态 + `configure_sdk_model_defaults` 改 `os.environ`
→ 同进程并发会跨用户污染 API Key），所以这里走 `subprocess` CLI，不内嵌 `run_strix_scan()`。

# 归因的三个输入，各自只能回答一个问题

- `exit_code`：只能回答"有没有发现漏洞"（`2`）与"是不是失败"。**不能回答"跑完了没有"** ——
  `0` 也可能是预算耗尽被掐死，那时它还宣称"未发现漏洞"（发布阻断项）。
- `run.json` 的 `status`：**"跑完了没有"的唯一权威**。可能是 `None`（早期失败，run 目录
  还没建）。
- `stopped_by`：**我们自己的记录**。这是"是不是被人停的"唯一可靠判据 —— Strix 自己装了
  SIGTERM/SIGINT/SIGHUP 处理器并 `sys.exit(1)`（`interface/cli.py:130-141`），所以被停的
  进程是**正常退出**的，`returncode` 是 `1` 而不是 `-15`。

`stdout` 只在需要给 `failed` 找原因时才用，而且**只进 `error_message` 之外的地方**：
见 `_format_error_message` 的 docstring。

# 刻意不做的事

- **不写 DB、不写审计、不加路由、不做并发闸**（T12）。本模块的产物是一个 `ScanOutcome`。
- **不读 `agents.db`、不做事件差分、不 import 任何 `strix.*`**（T13）。
- **不回收沙箱容器**（T11）：强杀后容器一定泄漏（`interface/cli.py:208-212` 的
  `session_manager.cleanup` 是 async 的，`SystemExit` 穿出 `asyncio.run` 之后跑不完），
  这里只打一行可对账的日志。
- **不注入 `Redactor`**：带 stdout 尾巴的那行日志会经 root handler 的
  `RedactingJsonFormatter` 自动脱敏。**脱敏点只有一处**是本项目的设计，在这里再挂一个
  副本只会造出第二个可能忘记维护的地方。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from app.errors import assert_scan_failure_code
from app.services.run_discovery import DiscoveredRun, discover_run, read_run_status
from app.services.scan_launcher import LaunchPlan, cleanup_scan_tmpdir, cleanup_workspace
from app.settings import Settings
from app.strix_profile import StrixProfile, profile_for

logger = logging.getLogger(__name__)

# 只留 stdout 的尾巴：归因表里每一种面板都是"打完就退"的致命错误，所以尾巴一定包含它。
STDOUT_TAIL_BYTES = 64 * 1024
# 操作者点停止后等多久才强杀。
STOP_GRACE_SECONDS = 20.0
# api 停机时的宽限，刻意比上面短 —— 容器编排给的停机窗口通常只有 10 秒。
SHUTDOWN_GRACE_SECONDS = 5.0
RUN_POLL_SECONDS = 0.5
# 归因失败时进日志的尾巴长度（比 STDOUT_TAIL_BYTES 小：日志是给人读的）。
LOGGED_TAIL_BYTES = 4 * 1024
_READ_CHUNK_BYTES = 8192

# 我们自己的词汇（不是 Strix 的 `run.json` 状态）：`scans.status` 列的取值域。
SCAN_STATUSES: frozenset[str] = frozenset(
    {"starting", "running", "completed", "stopped", "failed", "interrupted"}
)
# 同样是我们自己的词汇：退出码回答的那一个问题。
EXIT_MEANINGS: frozenset[str] = frozenset(
    {"vulnerabilities_found", "no_vulnerabilities_found", "failed"}
)


def assert_sandbox_env(settings: Settings) -> None:
    """启动断言：两个沙箱变量必须非空。缺了就**拒绝启动**。

    为什么值得一条硬断言：`STRIX_DOCKER_SANDBOX_NETWORK` 缺失时 Caido 的端口会被解析成
    `127.0.0.1`（在后端容器里指向它自己），抓包代理**静默降级** —— 扫描照跑、只是少了
    一整类发现，没有任何报错。`STRIX_IMAGE` 缺失则是猜错的镜像名比报错难查。

    断言放在本模块而不是 `settings.py`：断言跟着它保护的不变量走（与
    `assert_single_worker` 在 `key_vault.py` 是同一条判据）。调用点在 `main.py`。
    """
    missing = tuple(
        name
        for name, value in (
            ("STRIX_IMAGE", settings.strix_image),
            ("STRIX_DOCKER_SANDBOX_NETWORK", settings.strix_docker_sandbox_network),
        )
        if not value
    )
    if missing:
        raise RuntimeError(
            f"这些环境变量必须非空：{', '.join(missing)}。"
            "它们在 docker-compose.yml 的 api 服务里写死，缺失会让抓包代理静默降级。"
        )


def compile_rules(profile: StrixProfile) -> tuple[tuple[re.Pattern[str], str], ...]:
    """把 profile 里的正则源码编译一次（一个进程一次）。

    **顺序即优先级，编译不许重排** —— `sorted()` 或者经一次 `dict` 中转都会毁掉它。
    """
    return tuple((re.compile(source), code) for source, code in profile.attribution_rules)


def classify_stdout(text: str, rules: tuple[tuple[re.Pattern[str], str], ...]) -> str | None:
    """第一条命中的规则即结论。没有命中就是 `None`（由调用方落兜底码）。"""
    for pattern, code in rules:
        if pattern.search(text):
            return code
    return None


@dataclass(frozen=True, slots=True)
class Attribution:
    """一次扫描的终态。三个字段刻意分开：它们回答三个不同的问题。"""

    status: str  # ∈ SCAN_STATUSES
    exit_meaning: str  # ∈ EXIT_MEANINGS
    error_code: str | None  # ∈ SCAN_FAILURE_CODES，None **只在** status == "completed"


def _exit_meaning(exit_code: int, profile: StrixProfile, fallback: str) -> str:
    """退出码只回答"有没有发现漏洞"。

    `2` 恒为 `vulnerabilities_found`，**与 status 无关**："找到漏洞"和"跑没跑完"是两个
    正交的轴。其余退出码落到 `fallback`（完成/停止路径是"没找到"，失败路径是"失败"——
    一次失败的扫描没有资格说"没找到"）。
    """
    if exit_code == profile.exit_code_vulnerabilities_found:
        return "vulnerabilities_found"
    return fallback


def resolve_attribution(
    *,
    exit_code: int,
    run_status: str | None,
    stdout_tail: str,
    stopped_by: str | None,
    profile: StrixProfile,
    rules: tuple[tuple[re.Pattern[str], str], ...],
) -> Attribution:
    """纯函数、零 IO。**顺序即优先级**，黄金表测试逐行喂它。

    第一条排在"我们自己发过信号"前面是刻意的：`completed` 一旦写下就再也不会被覆盖
    （`report/state.py:637`），所以它是真的跑完了 —— 哪怕操作者在进程即将正常退出的
    一瞬点了停止。"扫描跑完了"是**单调**的。
    """
    if run_status == "completed":
        return Attribution(
            status="completed",
            exit_meaning=_exit_meaning(exit_code, profile, "no_vulnerabilities_found"),
            error_code=None,
        )
    if stopped_by == "operator":
        return Attribution(
            status="stopped",
            exit_meaning=_exit_meaning(exit_code, profile, "no_vulnerabilities_found"),
            error_code=assert_scan_failure_code("stopped_by_operator"),
        )
    if stopped_by == "shutdown":
        return Attribution(
            status="interrupted",
            exit_meaning=_exit_meaning(exit_code, profile, "no_vulnerabilities_found"),
            error_code=assert_scan_failure_code("interrupted_by_restart"),
        )
    if run_status == "interrupted":
        # 信号停止但不是我们发的（有人 docker kill 了 api，或者宿主发的 SIGHUP）。
        return Attribution(
            status="interrupted",
            exit_meaning=_exit_meaning(exit_code, profile, "no_vulnerabilities_found"),
            error_code=assert_scan_failure_code("interrupted_by_restart"),
        )
    if run_status in {"stopped", "budget_paused"}:
        # 预算或轮次先用完了。**退出码可能是 0** —— 这一条就是发布阻断项。
        return Attribution(
            status="stopped",
            exit_meaning=_exit_meaning(exit_code, profile, "no_vulnerabilities_found"),
            error_code=assert_scan_failure_code("scan_incomplete"),
        )
    # 其余全是失败，含 `run_status is None`（连 run 目录都没建 = 什么都没跑，
    # `interface/main.py:521` 那条 `return` 退的是 0）与 `running`/`waiting`
    # （进程已退但记录还写着在跑）。原因只能从 stdout 来。
    return Attribution(
        status="failed",
        exit_meaning=_exit_meaning(exit_code, profile, "failed"),
        error_code=assert_scan_failure_code(
            classify_stdout(stdout_tail, rules) or "scan_failed_unknown"
        ),
    )


def _format_error_message(
    attribution: Attribution,
    *,
    exit_code: int,
    run_status: str | None,
    matched: str | None,
) -> str | None:
    """`error_message` **只由我们自己的词表与整数构成，一个字节都不来自 stdout**。

    这不是风格选择，是结构性解法：只要它是 stdout 的摘录，操作者填的**测试账号口令**
    就有一条通往 DB 的路 —— agent 会拿那个口令登录，输出里出现它是**预期**行为，
    而它不在 KeyVault 里、因此不在精确子串脱敏集合里。做成这个形状，那条路在结构上
    就不存在，而不是靠谁记得脱敏。
    """
    if attribution.error_code is None:
        return None
    return (
        f"{attribution.error_code}; exit={exit_code}; "
        f"run_status={run_status or '-'}; matched={matched or '-'}"
    )


@dataclass(frozen=True, slots=True)
class ScanOutcome:
    """一次扫描的结论。**谁把它落库是 T12 的事**，本模块只负责算出它。"""

    status: str
    exit_code: int
    exit_meaning: str
    error_code: str | None
    error_message: str | None
    run_status: str | None
    run_dir: Path | None
    strix_run_name: str | None


class ScanProcess:
    """一次扫描的子进程。真正持有状态，所以是 class。

    构造即启动监控任务：清理挂在那个任务的 `finally` 上，**与有没有人来 `await` 无关**。
    写在 `wait()` 里的话，没人 await 的扫描就永远不清 tmpfs —— 那里面有测试账号口令和
    可能被 `persist_current()` 回落写入的明文 Key。
    """

    def __init__(
        self,
        *,
        scan_id: str,
        proc: asyncio.subprocess.Process,
        plan: LaunchPlan,
        settings: Settings,
        profile: StrixProfile,
        rules: tuple[tuple[re.Pattern[str], str], ...],
        on_finish: Callable[[str], None],
    ) -> None:
        self.scan_id = scan_id
        self.pid = proc.pid
        # 发现后填上。T12 靠它写 `strix_run_name`，T28 靠它 `--resume`。
        self.run: DiscoveredRun | None = None
        self._proc = proc
        self._plan = plan
        self._settings = settings
        self._profile = profile
        self._rules = rules
        self._on_finish = on_finish
        self._stopped_by: str | None = None
        self._tail = bytearray()
        self._monitor: asyncio.Task[ScanOutcome] = asyncio.create_task(self._run_monitor())

    async def wait(self) -> ScanOutcome:
        """等这次扫描出结论。可以被多个调用方同时 await。

        `shield`：调用方被取消时不该顺手取消监控任务 —— 清理挂在它的 `finally` 上。
        """
        return await asyncio.shield(self._monitor)

    async def stop(self, *, force: bool = False) -> None:
        """操作者停止。SIGTERM → 宽限 → SIGKILL；`force=True` 直接 SIGKILL。

        没有"更优雅的信号"可选：SIGINT 和 SIGTERM 在 Strix 里走同一个处理器。
        """
        await self._stop("operator", grace=STOP_GRACE_SECONDS, force=force)

    async def stop_for_shutdown(self) -> None:
        """api 停机时的收尾。与 `stop()` 只差两点：更短的宽限、不同的归因码。"""
        await self._stop("shutdown", grace=SHUTDOWN_GRACE_SECONDS, force=False)

    async def wait_quietly(self) -> None:
        """等监控任务把 `finally` 跑完，**不看结果**（谁 await `wait()` 谁看）。"""
        try:
            await asyncio.shield(self._monitor)
        except Exception:
            # 停机路径不该因为某一次扫描的收尾异常而中断其余扫描的收尾。
            logger.exception("扫描收尾时抛了异常", extra={"scan_id": self.scan_id})

    # ---- 内部 ---------------------------------------------------------------
    async def _stop(self, reason: str, *, grace: float, force: bool) -> None:
        if self._monitor.done():
            # 进程已经自己退了 —— 不要把它追认成"人停的"。
            return
        # **先记再发信号**：反了就有一个竞态窗口，监控任务可能先读到 None，
        # 把一次人为停止归成"扫描失败"。
        self._stopped_by = reason
        if not force:
            self._signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=grace)
                return
            except TimeoutError:
                pass
        self._signal(signal.SIGKILL)
        # 强杀 = Strix 的 `session_manager.cleanup` 一定跑不完，沙箱容器泄漏。
        # 这行日志是 T11 Reaper 与 T26 诊断页的对账凭据。`STRIX_RUN_ID` 就是 scan_id。
        logger.warning(
            "宽限期内没退，已强杀；沙箱容器要靠 label 回收",
            extra={
                "event": "sandbox_cleanup_skipped",
                "scan_id": self.scan_id,
                "strix_run_id": self.scan_id,
                "reason": reason,
            },
        )

    def _signal(self, sig: int) -> None:
        """打整个进程组（`start_new_session=True` 让 pgid == pid）。

        只杀领头的会留下孙子进程（docker CLI 之类）。`ProcessLookupError` 一律忽略：
        停止是幂等的，"它已经没了"不是错误。
        """
        with suppress(ProcessLookupError):
            os.killpg(self.pid, sig)

    async def _run_monitor(self) -> ScanOutcome:
        try:
            return await self._collect()
        finally:
            # `rmtree` 是同步阻塞 IO，进线程。两个清理都在这里 —— 无论成功、失败、
            # 还是被强杀，tmpfs 上那棵树都必须消失。
            await asyncio.to_thread(self._cleanup)
            self._on_finish(self.scan_id)

    def _cleanup(self) -> None:
        cleanup_workspace(self._settings, self.scan_id)
        cleanup_scan_tmpdir(self._settings, self.scan_id)

    async def _collect(self) -> ScanOutcome:
        stream = self._proc.stdout
        if stream is None:  # 用 PIPE 起的进程必然有 stdout；这里只是让类型标注诚实。
            raise RuntimeError("子进程没有 stdout 管道")
        # 抽干与轮询并行，**先等抽干到 EOF 再取 returncode** —— 否则会丢最后一段输出，
        # 也就是最可能带错误面板的那一段。
        await asyncio.gather(self._drain(stream), self._poll_for_run())
        exit_code = await self._proc.wait()

        if self.run is None:
            # 进程可能在最后一刻才建出 run 目录，那时轮询循环已经退出了。
            self.run = await asyncio.to_thread(discover_run, self._plan.cwd, self._profile)
        run_status: str | None = None
        if self.run is not None:
            run_status = await asyncio.to_thread(read_run_status, self.run.run_dir, self._profile)

        # 退出时才一次性解码：按块解码会把多字节字符切两半。
        stdout_tail = bytes(self._tail).decode("utf-8", errors="replace")
        attribution = resolve_attribution(
            exit_code=exit_code,
            run_status=run_status,
            stdout_tail=stdout_tail,
            stopped_by=self._stopped_by,
            profile=self._profile,
            rules=self._rules,
        )
        matched = classify_stdout(stdout_tail, self._rules)
        if attribution.error_code == "scan_failed_unknown":
            # 尾巴**只进日志**（经 root handler 的 Formatter 脱敏），绝不进 `error_message`、
            # 绝不返回给调用方。没有这行日志，未归类的失败就永远无法归类。
            logger.warning(
                "扫描失败，但输出里没有我们认识的错误特征",
                extra={
                    "scan_id": self.scan_id,
                    "exit_code": exit_code,
                    "stdout_tail": stdout_tail[-LOGGED_TAIL_BYTES:],
                },
            )
        logger.info(
            "扫描子进程已退出",
            extra={
                "scan_id": self.scan_id,
                "pid": self.pid,
                "exit_code": exit_code,
                "status": attribution.status,
                "exit_meaning": attribution.exit_meaning,
                "error_code": attribution.error_code,
                "run_status": run_status,
                "strix_run_name": None if self.run is None else self.run.run_name,
            },
        )
        return ScanOutcome(
            status=attribution.status,
            exit_code=exit_code,
            exit_meaning=attribution.exit_meaning,
            error_code=attribution.error_code,
            error_message=_format_error_message(
                attribution, exit_code=exit_code, run_status=run_status, matched=matched
            ),
            run_status=run_status,
            run_dir=None if self.run is None else self.run.run_dir,
            strix_run_name=None if self.run is None else self.run.run_name,
        )

    async def _drain(self, stream: asyncio.StreamReader) -> None:
        """持续抽干 stdout，只留最后 `STDOUT_TAIL_BYTES` 字节。

        不抽干的话管道缓冲区满了子进程就阻塞在 `write` 上，表现是"扫描卡住、没有任何
        报错"。用 `read()` 而**不是** `readline()`：Rich 画的横线是超长单行，
        `readline` 会抛 `LimitOverrunError`。
        """
        while True:
            chunk = await stream.read(_READ_CHUNK_BYTES)
            if not chunk:
                return
            self._tail += chunk
            if len(self._tail) > STDOUT_TAIL_BYTES:
                del self._tail[:-STDOUT_TAIL_BYTES]

    async def _poll_for_run(self) -> None:
        """轮询 run 目录，**只要进程还活着就一直轮**，不设固定超时。

        run 目录出现在**拉沙箱镜像之后**（`interface/main.py:472-480`），首次拉镜像是
        分钟级。设个固定超时就会把一次正常的首跑判成失败。
        """
        while self.run is None and self._proc.returncode is None:
            self.run = await asyncio.to_thread(discover_run, self._plan.cwd, self._profile)
            if self.run is not None:
                logger.info(
                    "run 目录已出现",
                    extra={"scan_id": self.scan_id, "strix_run_name": self.run.run_name},
                )
                return
            await asyncio.sleep(RUN_POLL_SECONDS)


class ScanSupervisor:
    """在册扫描的注册表。`api` 是 `--workers 1`，所以这个 dict 就是全局真相。"""

    def __init__(
        self,
        settings: Settings,
        strix_version: str,
        on_scan_finished: Callable[[], None] | None = None,
    ) -> None:
        self._settings = settings
        # 版本只在这里解析一次，规则也只编译一次 —— 别处不重新读版本。
        self._profile = profile_for(strix_version)
        self._rules = compile_rules(self._profile)
        self._processes: dict[str, ScanProcess] = {}
        # T11a：扫描一结束就通知 `Reaper` 清一次（强杀路径必定泄漏沙箱）。它在事件循环
        # 里被**同步**调用，所以实现方只许 set 一个 Event，不许阻塞。
        self._on_scan_finished = on_scan_finished

    async def start(self, scan_id: str, plan: LaunchPlan) -> ScanProcess:
        """起子进程。**`env=plan.env` 是整份替换**，绝不 `os.environ | plan.env` ——
        T9 的 `_PASSTHROUGH_ENV` 白名单就是靠这个成立的。

        `stderr=STDOUT` 合流：Rich 面板走 stdout、Python traceback 走 stderr，而归因要靠
        两者的先后顺序。`start_new_session=True` 让 pgid == pid，停止时能打整组。
        """
        proc = await asyncio.create_subprocess_exec(
            *plan.argv,
            cwd=plan.cwd,
            env=plan.env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        # 构造即起监控任务，所以**这两行之间不许有 await** —— 否则监控任务可能先跑到
        # `_on_finish` 而删掉一个还没登记的 scan_id。
        process = ScanProcess(
            scan_id=scan_id,
            proc=proc,
            plan=plan,
            settings=self._settings,
            profile=self._profile,
            rules=self._rules,
            on_finish=self._forget,
        )
        self._processes[scan_id] = process
        logger.info(
            "扫描子进程已启动",
            extra={"scan_id": scan_id, "pid": proc.pid, "cwd": str(plan.cwd)},
        )
        return process

    def get(self, scan_id: str) -> ScanProcess | None:
        return self._processes.get(scan_id)

    def active_scan_ids(self) -> tuple[str, ...]:
        return tuple(self._processes)

    async def shutdown(self) -> None:
        """api 停机：给所有在册扫描发 TERM、短宽限、KILL，然后**等清理跑完**。

        必须等：`finally` 里那次 `rmtree` 是 tmpfs 上口令与可能被回落写入的 Key 的唯一
        销毁点，进程退出前不等它就等于把它留到下一次开机。
        """
        processes = tuple(self._processes.values())
        if not processes:
            return
        logger.info("停机：正在结束在跑的扫描", extra={"count": len(processes)})
        await asyncio.gather(*(process.stop_for_shutdown() for process in processes))
        await asyncio.gather(*(process.wait_quietly() for process in processes))

    def _forget(self, scan_id: str) -> None:
        self._processes.pop(scan_id, None)
        if self._on_scan_finished is not None:
            # **pop 之后**才通知：清扫要按"已经不在册"的那份名单算，否则刚结束的这次
            # 扫描的沙箱会被自己 spare 掉一轮。
            self._on_scan_finished()
