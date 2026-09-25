"""Strix 升级预警线（T29）：上游契约 + 我方 import 边界。

第一部分读已安装的 strix 源码**文本**（不 import `strix.core.*` / `strix.runtime.*`），断言
`PLAN.md` §Strix 集成面 钉死的那张清单（A–M）。只断言字面量、不断言行号。任何一条红了 =
升级改了我方依赖的行为，按 §Strix 版本升级 runbook 重验对应模块，**不许**改断言凑绿。

第二部分用 `ast` 扫 `app/**/*.py`，确保 strix 只经 `app/strix_bridge/` 的两个允许模块进入。
"""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

import app
from app.strix_profile import profile_for

_P = profile_for("1.6.2")
_ALLOWED = ("strix.interface.tui.backend.live_view", "strix.interface.viewer.transcript")


def _strix_root() -> Path:
    strix = pytest.importorskip("strix")
    assert strix.__file__ is not None
    return Path(strix.__file__).parent


def _strix_source(rel_path: str) -> str:
    return (_strix_root() / rel_path).read_text(encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    """取 `start` 到其后第一个 `end` 之间的文本；找不到任一端就让测试红。"""
    i = text.index(start)
    return text[i : text.index(end, i)]


# ---------------------------------------------------------------- 第一部分：上游契约


@pytest.mark.parametrize(
    ("rel_path", "literal"),
    [
        # B：产物路径名与我方 profile 一致
        ("core/paths.py", f'RUNS_DIR_NAME = "{_P.runs_dir_name}"'),
        ("core/paths.py", f'RUN_RECORD_FILENAME = "{_P.run_record_name}"'),
        ("report/coverage.py", f'COVERAGE_FILENAME = "{_P.coverage_record_name}"'),
        # A：有 --config
        ("interface/cli_args.py", '"--config"'),
        # C：persist_current 写 override；--config 校验要求 .json 与 env 对象
        ("config/loader.py", "target = _override or _DEFAULT_PATH"),
        ("interface/utils.py", 'path.suffix != ".json"'),
        ("interface/utils.py", 'if "env" not in data or not isinstance(data.get("env"), dict):'),
        # D：发现漏洞退出码
        ("interface/main.py", f"sys.exit({_P.exit_code_vulnerabilities_found})"),
        # E：沙箱网络 env 与回收 label
        ("runtime/docker_client.py", '"STRIX_DOCKER_SANDBOX_NETWORK"'),
        ("runtime/docker_client.py", 'os.getenv("STRIX_RUN_ID")'),
        ("runtime/docker_client.py", 'labels["strix-run-id"] = run_id'),
        ("runtime/docker_client.py", 'os.getenv("STRIX_RUN_TYPE")'),
        ("runtime/docker_client.py", 'labels["strix-run-type"] = run_type'),
        # F：SIGTERM 挂在 signal_handler 上
        ("interface/cli.py", "signal.signal(signal.SIGTERM, signal_handler)"),
        # H：泄漏矩阵 #12 的评估对象
        ("config/models.py", "def _mirror_api_key_to_provider_env("),
        # I：截图上限默认 3
        ("config/settings.py", 'Field(default=3, ge=0, alias="STRIX_MAX_CONTEXT_IMAGES")'),
        # J：截图以 data URL 内联在事件里
        ("interface/tui/live_view.py", 'url.startswith("data:image/")'),
        # K：我们设的两个 env、绝不设的一个 env 仍被读取
        ("config/settings.py", 'alias="STRIX_TELEMETRY"'),
        ("interface/update_check.py", 'os.environ.get("STRIX_NO_UPDATE_CHECK")'),
        ("telemetry/logging.py", 'os.environ.get("STRIX_DEBUG")'),
    ]
    + [("core/sessions.py", f'= "{text}"') for text in _P.image_elision_texts],
)
def test_upstream_literal_present(rel_path: str, literal: str) -> None:
    """上游改名/删掉该字面量即红：对应的 profile 字段、launcher env 或投影层要重验。"""
    assert literal in _strix_source(rel_path)


@pytest.mark.parametrize("flag", ['"--model"', '"--run-name"', '"--output-dir"'])
def test_cli_lacks_flag(flag: str) -> None:
    """A：上游新增该 CLI 参数即红：STRIX_LLM 注入 / 每任务独立 cwd 的设计要重评。"""
    assert flag not in _strix_source("interface/cli_args.py")


def test_sigterm_handler_exits_failed() -> None:
    """F：signal_handler 不再 sys.exit(1) 即红：exit_code_failed 与沙箱回收假设要重验。"""
    body = _between(_strix_source("interface/cli.py"), "def signal_handler", "atexit.register")
    assert f"sys.exit({_P.exit_code_failed})" in body


def test_rewrite_session_clears_then_reinserts() -> None:
    """I：_rewrite_session 不再 clear_session 后重插即红：agents.db 镜像的重同步逻辑要重评。"""
    body = _between(
        _strix_source("core/sessions.py"), "async def _rewrite_session", "except Exception"
    )
    assert body.index("await session.clear_session()") < body.index(
        "await session.add_items(rebuilt_items)"
    )


def test_strix_dot_dir_only_under_home() -> None:
    """G：出现不在 Path.home() 下的 ".strix" 路径即红：每任务独立 HOME 挡不住它，要重评泄漏面。"""
    hits: list[str] = []
    for path in sorted(_strix_root().rglob("*.py")):
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if '".strix"' in line or "'.strix'" in line:
                hits.append(f"{path.name}:{no} {line.strip()}")
    assert hits, "一处 .strix 字面量都没找到 —— 判据失效"
    assert [h for h in hits if 'Path.home() / ".strix"' not in h] == []


def test_allowed_modules_provide_symbols() -> None:
    """L：允许的两个模块删改我们用的符号即红：strix_bridge/projection.py 要跟着改。"""
    live_view = importlib.import_module(_ALLOWED[0])
    transcript = importlib.import_module(_ALLOWED[1])
    assert hasattr(live_view.TuiLiveView, "event_snapshot")
    for name in (
        "read_run_summary",
        "read_vulnerabilities",
        "read_report_markdown",
        "severity_counts",
        "primary_target",
    ):
        assert hasattr(transcript, name), name


# 1.6.2 实测：两个允许的模块都 `from strix.core.paths import ...`（transcript.py:9、
# live_view.py:15）。paths 是只 import pathlib 的常量模块，2026-09-25 用户拍板精确放行它；
# 再多带进任何一个 core/runtime 模块，或 paths 开始碰 environ，都红。
_TRANSITIVE_OK = ["strix.core", "strix.core.paths"]


def test_core_paths_stays_stateless() -> None:
    """paths 一旦读写 os.environ 即红：放行它的前提（纯常量）不再成立。"""
    assert "environ" not in _strix_source("core/paths.py")


def test_allowed_modules_do_not_pull_core_or_runtime() -> None:
    """M：允许的模块传递 import 了 paths 以外的 strix.core/runtime 即红：web 进程会被拖进改
    os.environ 的全局态。"""
    code = (
        "import sys\n"
        + "".join(f"import {m}\n" for m in _ALLOWED)
        + "print('\\n'.join(sorted(m for m in sys.modules"
        " if m.startswith(('strix.core', 'strix.runtime')))))"
    )
    # argv 全是本文件里的常量，不含外部输入
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], check=True, capture_output=True, text=True
    )
    assert result.stdout.split() == _TRANSITIVE_OK


# ---------------------------------------------------------------- 第二部分：我方 import 边界


def _strix_imports() -> list[tuple[str, int, str]]:
    """返回 app/ 下所有 strix 导入：(相对 app 的路径, 行号, 模块名)。相对导入一定是我方模块。"""
    root = Path(app.__file__).parent
    found: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            found += [
                (rel, node.lineno, n) for n in names if n == "strix" or n.startswith("strix.")
            ]
    return found


def test_strix_imports_only_in_bridge_and_allowed() -> None:
    """我方在 strix_bridge/ 外或引了允许外的 strix 模块即红：import 边界被突破。"""
    imports = _strix_imports()
    assert imports, "一处 strix import 都没扫到 —— 扫描器失效"
    bad = [
        f"{rel}:{no} {mod}"
        for rel, no, mod in imports
        if not rel.startswith("strix_bridge/") or mod not in _ALLOWED
    ]
    assert bad == []
