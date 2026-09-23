"""把「一次扫描的意图」翻成「可以直接 exec 的 argv + env + 工作区」。

首次扫描走 `build_launch_plan`，续跑走 `build_resume_plan`。
**本模块不起进程**（那是 T10 的事），只做纯构造，加上 `prepare_workspace` /
`cleanup_workspace` 这两个碰 IO 的函数。这条边界是刻意的：argv 与 env 是全部安全不变式
的落点，它们必须能在没有 docker、没有网络、没有子进程的单测里逐元素比对。

三件事在这里是**结构性**保证，不是纪律：

- **Key 不进 argv**：`build_argv()` 收不到 `CredentialSet`，它连凭据都看不见。
  `LaunchPlan.argv_preview is argv` 让"预览等于真 argv"成为可断言的事实。
- **env 是白名单不是 `os.environ.copy()`**：`_PASSTHROUGH_ENV` 之外的变量一律不传，
  所以 `STRIX_DEBUG`（把 `strix.log` 拉到 DEBUG，是泄漏面）就算被写进 compose 也到不了
  子进程。`environ` 从参数注入 —— 模块里不读 `os.environ`（CLAUDE.md §Python）。
- **测试账号口令只落在 tmpfs 的 `instruction.txt`**：永远用 `--instruction-file`，
  不用 `--instruction`（后者等于把口令送进 `ps`）。

`--config` 指到 `$HOME/.strix/cli-config.json` 是**双保险**：`--config` 生效时读的是那个
文件；万一它不生效、Strix 的 `persist_current()` 回落到 `$HOME/.strix/`，落点仍是同一个
tmpfs 文件（它会把 `LLM_API_KEY` 明文写进去）。M0 实测跑通的就是这个形状。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from app.errors import BudgetExceedsCeilingError, InvalidRequestError
from app.services.key_vault import CredentialSet
from app.services.llm_client import model_for, spec_for
from app.services.scan_templates import SHARED_TAIL, ScanTemplate, template_for
from app.settings import Settings

# `strix -m` 的 choices。不拦非法值的话，子进程会死在 argparse 上，而那个失败会被
# 归因成"扫描失败"——同理 `STRIX_REASONING_EFFORT`（死在 pydantic 的启动校验上）。
_SCAN_MODES = frozenset({"quick", "standard", "deep"})
_REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})

# 四个**必设**的值（CLAUDE.md §Strix 集成 / §禁区）钉死在代码里，**不走透传**。
# 判据：透传的语义是"环境里没有就跳过"，而这四个缺失时都不报错 ——
# 缺 `STRIX_TELEMETRY` 就是 PostHog + Scarf 外发（§禁区第一条），缺 `STRIX_RUN_TYPE`
# 就是沙箱容器没有 label、`make reap` 回收不到。一条"缺了会静默出事"的不变量
# 不能挂在 compose 上（compose 是可以被改的文件），必须在这里结构性成立。
_PINNED_ENV: Mapping[str, str] = {
    "STRIX_TELEMETRY": "false",
    "STRIX_NO_UPDATE_CHECK": "1",
    "LITELLM_LOG": "ERROR",
    "STRIX_RUN_TYPE": "console",
}

# 允许从本进程环境透传给子进程的变量，**按名字**。这份名单就是"不 copy 环境"的实现：
# 加一个名字要能说出它为什么必须进子进程。
# ⚠️ `STRIX_IMAGE` 与 `STRIX_DOCKER_SANDBOX_NETWORK` 也是"必设"，但它们**没有安全的
# 默认值**（猜错的网络名比报错更难查），所以留在透传里 —— 它们的强制点是 T10 的启动
# 断言，见 PLAN.md T10 行。
_PASSTHROUGH_ENV: tuple[str, ...] = (
    "PATH",
    "STRIX_IMAGE",
    "STRIX_DOCKER_SANDBOX_NETWORK",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "DOCKER_HOST",
    "LANG",
)


@dataclass(frozen=True, slots=True)
class TestCredential:
    """操作者提供的测试账号。**只进 `instruction.txt`**，不进 argv、env、DB。"""

    role: str
    username: str
    password: str


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """调用方（T12）填的意图。`targets` 是已过 `target_guard` 的 normalized 值。

    `max_budget_usd` **没有默认值**：忘了填必须是构造失败，不是悄悄用某个数
    （CLAUDE.md §安全不变式）。
    """

    scan_id: str
    template_id: str
    targets: tuple[str, ...]
    scan_mode: str
    max_budget_usd: float
    max_turns: int
    reasoning_effort: str | None
    extra_instruction: str | None
    test_credentials: tuple[TestCredential, ...]


@dataclass(frozen=True, slots=True)
class ResumeSpec:
    """续跑的意图：同一个 cwd 上 `strix --resume <strix_run_name>`。

    `max_budget_usd` 是新的**总额**，不是增量：Strix 续跑时把上次的 `llm_usage` 读回来，
    再拿本次 `--max-budget-usd` 重算是否超限。无默认值，理由同 `LaunchSpec`。
    """

    scan_id: str
    strix_run_name: str
    scan_mode: str
    max_budget_usd: float
    spent_usd: float
    max_turns: int
    reasoning_effort: str | None


@dataclass(frozen=True, slots=True)
class Workspace:
    """一次扫描的目录布局。`root` 在 tmpfs 上，`cwd` 在 `${DATA}` 上 —— 两者寿命不同。"""

    root: Path
    home: Path
    config_path: Path
    instruction_path: Path
    cwd: Path
    instruction_sha256: str


@dataclass(frozen=True, slots=True)
class LaunchPlan:
    """交给 T10 去 exec 的东西。

    `env` 含明文凭据 —— **绝不整体进日志/DB**；要记就记 `env_var_names`。
    `instruction_sha256` 是正文的唯一留痕：正文随 tmpfs 一起消失。
    两个 instruction 字段为 `None` = 续跑：指令由 Strix 从 `run.json` 读回，
    DB 里那一行保留首次的摘要（T31b 不覆写它）。
    """

    argv: tuple[str, ...]
    argv_preview: tuple[str, ...]
    env: Mapping[str, str]
    env_var_names: tuple[str, ...]
    cwd: Path
    home: Path
    config_path: Path
    instruction_path: Path | None
    instruction_sha256: str | None


def _validate(spec: LaunchSpec, budget_ceiling_usd: float) -> ScanTemplate:
    """全部拒绝路径都在这里，**一个新错误码都不加**。返回解析出来的模板。"""
    template = template_for(spec.template_id)
    if template is None:
        raise InvalidRequestError(field="template_id")
    if not spec.targets:
        raise InvalidRequestError(field="targets")
    _validate_limits(spec, budget_ceiling_usd)
    return template


def _validate_limits(spec: LaunchSpec | ResumeSpec, budget_ceiling_usd: float) -> None:
    """与模板无关的五条，首次与续跑共用。"""
    if spec.scan_mode not in _SCAN_MODES:
        raise InvalidRequestError(field="scan_mode")
    if spec.max_budget_usd <= 0:
        raise InvalidRequestError(field="max_budget_usd")
    if spec.max_budget_usd > budget_ceiling_usd:
        # 超上限不是"请求写错了"而是"这台机器不允许"，所以是 409 而不是 422。
        raise BudgetExceedsCeilingError(
            max_budget_usd=spec.max_budget_usd,
            ceiling_usd=budget_ceiling_usd,
        )
    if spec.max_turns <= 0:
        raise InvalidRequestError(field="max_turns")
    if spec.reasoning_effort is not None and spec.reasoning_effort not in _REASONING_EFFORTS:
        raise InvalidRequestError(field="reasoning_effort")


def _validate_resume(spec: ResumeSpec, budget_ceiling_usd: float) -> None:
    """续跑的全部拒绝路径，同样一个新错误码都不加。"""
    if not spec.strix_run_name:
        raise InvalidRequestError(field="strix_run_name")
    _validate_limits(spec, budget_ceiling_usd)
    # 等于也拒：Strix 的判据是"已花费 >= 上限即停"，总额等于已花费就是一启动即停，
    # 白起一个进程还报"结论不完整"。
    if spec.max_budget_usd <= spec.spent_usd:
        raise InvalidRequestError(field="max_budget_usd")


def _format_usd(value: float) -> str:
    """`25.0` → `"25"`，`12.5` → `"12.5"`。

    不用 `f"{v:g}"`：它在大数上会切成科学计数法（`1e+06`），而 argparse 那边收到的是
    字符串，一旦变形我们自己也看不出 argv 里的预算到底是多少。
    """
    return f"{value:.4f}".rstrip("0").rstrip(".")


def build_argv(
    spec: LaunchSpec,
    *,
    config_path: Path,
    instruction_path: Path,
    budget_ceiling_usd: float,
) -> tuple[str, ...]:
    """纯函数。顺序固定，好让黄金 argv 测试逐元素比对。

    `budget_ceiling_usd` 是必填参数而不是"调用前请先校验"：**构造 argv 是唯一的入口**，
    校验挂在这里就没有绕过它的路径。
    `--scope-mode` / `--diff-base` 一律不发 —— 那是代码目标的参数，v1 没有代码目标。
    """
    _validate(spec, budget_ceiling_usd)
    targets: list[str] = []
    for target in spec.targets:
        targets += ["-t", target]
    return (
        "strix",
        "-n",  # 非交互。有 TUI 的话它会等键盘输入，子进程直接挂住。
        *targets,
        "-m",
        spec.scan_mode,
        "--max-budget-usd",
        _format_usd(spec.max_budget_usd),
        "--max-turns",
        str(spec.max_turns),
        "--instruction-file",
        str(instruction_path),
        "--config",
        str(config_path),
    )


def build_resume_argv(
    spec: ResumeSpec,
    *,
    config_path: Path,
    budget_ceiling_usd: float,
) -> tuple[str, ...]:
    """纯函数。校验挂在这里的理由同 `build_argv`：构造 argv 是唯一的入口。

    没有 `-t`（Strix 拒绝 `--resume` 与 `-t` 同时出现）、没有 `--instruction-file`
    （指令它从 `run.json` 读回）。`-m` 必须显式发：Strix 只在 `-m` 等于默认值 `deep` 时
    才换成持久化的模式，不发的话"原来就是 deep"和"没说"分不开。
    """
    _validate_resume(spec, budget_ceiling_usd)
    return (
        "strix",
        "-n",
        "--resume",
        spec.strix_run_name,
        "-m",
        spec.scan_mode,
        "--max-budget-usd",
        _format_usd(spec.max_budget_usd),
        "--max-turns",
        str(spec.max_turns),
        "--config",
        str(config_path),
    )


def build_env(
    credentials: CredentialSet,
    spec: LaunchSpec | ResumeSpec,
    *,
    home: Path,
    cwd: Path,
    environ: Mapping[str, str],
) -> dict[str, str]:
    """纯函数。子进程的**完整**环境 —— 返回值就是全部，不与任何东西合并。"""
    shape = spec_for(credentials.provider, credentials.auth_shape)
    if shape is None:
        # 防御性分支：`POST /api/keys` 登记时已经校验过 provider + auth_shape 的组合
        # （`spec_for` 是同一份目录），所以这里不该发生。但 vault 里的值是"登记那一刻"
        # 的快照，目录改了就可能对不上 —— 静默拼错模型名比 422 难查得多。
        raise InvalidRequestError(field="auth_shape")

    model = model_for(shape, credentials.strix_llm)
    env: dict[str, str] = {
        "HOME": str(home),
        "TMPDIR": str(cwd / "tmp"),
        # 不设它的话 SIGTERM 会跳过 cleanup 而泄漏沙箱容器，只能靠 label 回收。
        "STRIX_RUN_ID": spec.scan_id,
        # CLI 没有 `--model`，模型只能经这个变量注入。
        "STRIX_LLM": model,
        # 判据是**解析后的路由**而不是 auth_shape：Bedrock 的 invoke/ 路由不支持
        # prompt caching，而用户手打的前缀会被 `model_for()` 剥掉重拼。
        "STRIX_PROMPT_CACHE": "false" if "invoke/" in model else "true",
    }
    if spec.reasoning_effort is not None:
        env["STRIX_REASONING_EFFORT"] = spec.reasoning_effort

    for name, secret in credentials.secrets.items():
        # 键就是子进程要的变量名（`LLM_API_KEY` / `AWS_ACCESS_KEY_ID`…），照注入。
        env[name] = secret.get_secret_value()
    for name, value in credentials.params.items():
        env[name] = value
    if credentials.api_base:
        env["LLM_API_BASE"] = credentials.api_base

    for name in _PASSTHROUGH_ENV:
        value = environ.get(name)
        if value is not None:
            env[name] = value
    # 放在透传**之后**：本进程环境里写了 `STRIX_TELEMETRY=true` 也不许覆盖掉它。
    env.update(_PINNED_ENV)
    return env


def compose_instruction(template: ScanTemplate, spec: LaunchSpec) -> str:
    """纯函数。模板正文 + 目标 + 测试账号 + 操作者补充 + 统一尾巴，全英文骨架。"""
    lines: list[str] = [template.instruction_body, "", "## Targets"]
    lines += [f"- {target}" for target in spec.targets]
    if spec.test_credentials:
        lines += ["", "## Test accounts (use only these; do not touch other accounts)"]
        lines += [
            f"- role={credential.role} username={credential.username} "
            f"password={credential.password}"
            for credential in spec.test_credentials
        ]
    if spec.extra_instruction:
        lines += ["", "## Additional notes from the operator", spec.extra_instruction]
    lines.append(SHARED_TAIL)
    return "\n".join(lines)


def _write_private(path: Path, text: str) -> None:
    """先以 0600 建文件再写。

    不写成"写完再 chmod"：那中间有一瞬间文件是 0644 的，而 `instruction.txt` 里有
    测试账号口令。文件已存在时 `O_CREAT` 的 mode 不生效，所以补一次 `chmod`。
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    path.chmod(0o600)


def _prepare_home(settings: Settings, scan_id: str) -> tuple[Path, Path, Path]:
    """tmpfs 那一半：`root`／`home`／`home/.strix` + `cli-config.json`。首次与续跑共用。"""
    root = settings.console_ephemeral_home_root / f"scan-{scan_id}"
    home = root / "home"
    config_dir = home / ".strix"
    for directory in (root, home, config_dir):
        directory.mkdir(parents=True, exist_ok=True)
        # `mkdir(mode=…)` 会被 umask 削掉，所以显式 chmod。
        directory.chmod(0o700)
    config_path = config_dir / "cli-config.json"
    # 预置 `{"env":{}}`：Strix 读不到配置文件时会去问交互式向导，而 `-n` 下那是挂住。
    _write_private(config_path, json.dumps({"env": {}}))
    return root, home, config_path


def prepare_workspace(settings: Settings, spec: LaunchSpec, instruction_text: str) -> Workspace:
    """建目录、落两个文件。**本模块唯一碰 IO 的地方**（除了 `cleanup_workspace`）。"""
    root, home, config_path = _prepare_home(settings, spec.scan_id)
    cwd = settings.scans_dir / spec.scan_id
    for directory in (cwd, cwd / "tmp"):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)

    instruction_path = root / "instruction.txt"
    _write_private(instruction_path, instruction_text)

    return Workspace(
        root=root,
        home=home,
        config_path=config_path,
        instruction_path=instruction_path,
        cwd=cwd,
        instruction_sha256=hashlib.sha256(instruction_text.encode("utf-8")).hexdigest(),
    )


def cleanup_workspace(settings: Settings, scan_id: str) -> None:
    """删掉 tmpfs 上那棵树（口令与可能被回落写入的 Key 都在里面）。

    **只删 tmpfs，不删 `cwd`**：`strix_runs/<自动名>/` 在 cwd 里，T10 还要读产物。
    `ignore_errors=True` 且不抛：T10 在 `finally` 里调它，这里抛异常会盖掉真正的失败原因；
    而"没删掉"本身对应的是宿主 tmpfs 的故障，不是本函数能修的事。
    """
    shutil.rmtree(settings.console_ephemeral_home_root / f"scan-{scan_id}", ignore_errors=True)


def cleanup_scan_tmpdir(settings: Settings, scan_id: str) -> None:
    """删掉 `${DATA}/scans/<id>/tmp`（子进程的 `TMPDIR`）。

    **只删 `tmp`，绝不碰同级的 `strix_runs/`** —— 产物要留给 T13 读、留给 T28 续跑。
    放在本模块而不是 supervisor：这个目录布局是 `prepare_workspace` 造的，**谁造谁删**，
    路径知识不外泄。

    ⚠️ **重新评估条件**：v1 只有 URL 目标，所以现在删它是安全的。哪天支持 git 目标就要
    重新评估 —— 克隆目录是 `tempfile.gettempdir()/strix_repos/<run_name>`
    （`interface/utils.py:1561`），而 `gettempdir()` 读的正是这个 `TMPDIR`；`--resume`
    会校验 `cloned_repo_path` 还在，缺了直接 `parser.error`（`interface/cli_args.py:422`）。
    好消息：`--resume` 真正必需的 `run.json` 与 `.state/agents.json` 都在
    `cwd/strix_runs/` 下，不在 `TMPDIR` 里。
    """
    shutil.rmtree(settings.scans_dir / scan_id / "tmp", ignore_errors=True)


def build_launch_plan(
    settings: Settings,
    spec: LaunchSpec,
    credentials: CredentialSet,
    *,
    environ: Mapping[str, str],
) -> LaunchPlan:
    """T12 的唯一入口：校验 → 建工作区 → 拼 argv/env。失败时不留半个工作区。

    先 `_validate` 再建目录（`build_argv` 里那次校验是重复的，但它是公开入口的守卫，
    不能省）：非法请求不该在 tmpfs 上留下一个空目录。
    """
    template = _validate(spec, settings.console_max_budget_ceiling_usd)
    workspace = prepare_workspace(settings, spec, compose_instruction(template, spec))
    argv = build_argv(
        spec,
        config_path=workspace.config_path,
        instruction_path=workspace.instruction_path,
        budget_ceiling_usd=settings.console_max_budget_ceiling_usd,
    )
    env = build_env(
        credentials,
        spec,
        home=workspace.home,
        cwd=workspace.cwd,
        environ=environ,
    )
    return LaunchPlan(
        argv=argv,
        # 不是冗余：接口契约写了"无 Key 的预览"，这个别名让"预览等于真 argv"成为
        # 一条可断言的事实，而不是靠人记得没往 argv 里塞 Key。
        argv_preview=argv,
        env=env,
        env_var_names=tuple(sorted(env)),
        cwd=workspace.cwd,
        home=workspace.home,
        config_path=workspace.config_path,
        instruction_path=workspace.instruction_path,
        instruction_sha256=workspace.instruction_sha256,
    )


def build_resume_plan(
    settings: Settings,
    spec: ResumeSpec,
    credentials: CredentialSet,
    *,
    environ: Mapping[str, str],
) -> LaunchPlan:
    """续跑的唯一入口：同一个 cwd、新的 tmpfs HOME、同一个 `STRIX_RUN_ID`（沙箱 label 连续）。

    先校验再建目录，理由同 `build_launch_plan`。
    """
    _validate_resume(spec, settings.console_max_budget_ceiling_usd)
    _root, home, config_path = _prepare_home(settings, spec.scan_id)
    cwd = settings.scans_dir / spec.scan_id
    # `tmp` 上次被 `cleanup_scan_tmpdir` 删了，而 `TMPDIR` 指向它，所以重建。
    # 刻意不带 `parents=True`：cwd 已被留存清理删掉时必须抛 `FileNotFoundError`，
    # 不许把一个没有产物的 scan 目录凭空建出来（T31b 准入时先查，这里是结构性兜底）。
    (cwd / "tmp").mkdir(exist_ok=True)
    (cwd / "tmp").chmod(0o700)
    argv = build_resume_argv(
        spec,
        config_path=config_path,
        budget_ceiling_usd=settings.console_max_budget_ceiling_usd,
    )
    env = build_env(credentials, spec, home=home, cwd=cwd, environ=environ)
    return LaunchPlan(
        argv=argv,
        argv_preview=argv,  # 同一个对象，理由见 `build_launch_plan`。
        env=env,
        env_var_names=tuple(sorted(env)),
        cwd=cwd,
        home=home,
        config_path=config_path,
        instruction_path=None,
        instruction_sha256=None,
    )
