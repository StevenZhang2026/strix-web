"""配置、错误码登记表、版本一致性。

这三样都是"约定"层面的东西，测试的作用是把约定变成会失败的断言 ——
下游任务（T3/T6/T7/T9）都建立在它们之上。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import __version__
from app.errors import (
    ALL_ERRORS,
    SCAN_FAILURE_CODES,
    ConsoleError,
    InternalError,
    assert_scan_failure_code,
)
from app.settings import FORBIDDEN_ENV_NAMES, Settings, assert_no_credential_env


# =============================================================================
# 一、版本一致性
# =============================================================================
def test_version_matches_pyproject() -> None:
    """`app.__version__` 必须等于 pyproject 的 `[project] version`。

    两处并存的理由：`app` 包不是 pip 安装的（Dockerfile 只 `COPY app ./app`），
    `importlib.metadata` 拿不到它。既然无法消除第二处，就用测试消除不一致 ——
    "记得改两处"不是一个能长期成立的约定。

    刻意用正则而不是 tomllib：只需要一行，而 tomllib 会把这条测试变成"顺便验证
    整个 pyproject 能被解析"，失败时的报错指向就不再是版本不一致。
    """
    text = (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    assert match is not None, "pyproject.toml 里找不到 version"
    assert match.group(1) == __version__


# =============================================================================
# 二、Settings
# =============================================================================
def test_data_dir_is_required() -> None:
    """`CONSOLE_DATA_DIR` 无默认值。

    它是同路径挂载的宿主绝对路径，因部署机而异。任何默认值都会在某台机器上静默
    指向错误位置 —— 而"静默指向错位置"是本项目最想避免的那一类 bug
    （见 CLAUDE.md §技术约束「同路径挂载」）。
    """
    # 显式传 None 而不是靠"环境里没有这个变量"：后者会让这条测试的结果取决于
    # 它在哪跑（在 api 容器里跑就会有 CONSOLE_DATA_DIR）。
    with pytest.raises(ValueError, match="console_data_dir"):
        Settings(console_data_dir=None)


def test_relative_data_dir_is_rejected() -> None:
    """相对路径在同路径挂载下没有意义：容器内外 cwd 不同，同一个相对路径指向两处。"""
    with pytest.raises(ValueError, match="绝对路径"):
        Settings(console_data_dir=Path("relative/path"))


def test_derived_paths(tmp_path: Path) -> None:
    """派生路径集中在 Settings 上，各处不许自己拼。"""
    settings = Settings(console_data_dir=tmp_path)
    assert settings.db_path == tmp_path / "console.sqlite"
    assert settings.scans_dir == tmp_path / "scans"
    assert settings.audit_dir == tmp_path / "audit"
    assert settings.config_dir == tmp_path / "config"
    assert (settings.migrations_dir / "001_init.sql").is_file()


def test_settings_is_frozen(tmp_path: Path) -> None:
    """配置是启动期事实。运行期改它会让"日志里的配置"与"实际行为"不一致。"""
    settings = Settings(console_data_dir=tmp_path)
    with pytest.raises(ValueError, match="frozen"):
        settings.console_log_level = "DEBUG"


def test_bad_log_level_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="CONSOLE_LOG_LEVEL"):
        Settings(console_data_dir=tmp_path, console_log_level="LOUD")


def test_budget_ceiling_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="greater than"):
        Settings(console_data_dir=tmp_path, console_max_budget_ceiling_usd=0)


# =============================================================================
# 三、进程环境里不许有凭据
# =============================================================================
@pytest.mark.parametrize(
    "name",
    [
        "LLM_API_KEY",
        "ANTHROPIC_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_ACCESS_KEY_ID",
        "STRIX_LLM",
        "STRIX_DEBUG",
    ],
)
def test_credential_env_is_rejected(name: str) -> None:
    with pytest.raises(RuntimeError, match=name):
        assert_no_credential_env({name: "任意值"})


def test_error_message_contains_no_value() -> None:
    """报错里**只有变量名，没有值**。

    这条很容易在"为了好排障"时被破坏 —— 而破坏它的那次改动会把凭据写进
    容器启动日志，那是最容易被完整复制粘贴出去的一段文本。
    """
    with pytest.raises(RuntimeError) as excinfo:
        assert_no_credential_env({"ANTHROPIC_API_KEY": "sk-ant-绝密"})
    assert "sk-ant-绝密" not in str(excinfo.value)


def test_strix_llm_is_forbidden_although_not_a_credential() -> None:
    """`STRIX_LLM` 不是凭据，但同样不许出现在 api 进程环境里。

    Strix 从 env 读它，一旦在 api 进程环境里存在就会被**每个子进程继承**，
    变成"所有用户共用同一个模型"的隐式默认 —— 而本项目的核心约束是
    「LLM 可切换、谁用谁的 Key」。它必须由 ScanLauncher 每任务显式注入。
    """
    assert "STRIX_LLM" in FORBIDDEN_ENV_NAMES


def test_clean_env_passes() -> None:
    """负面覆盖：干净的环境必须通过。

    容器环境里本来就有 `GPG_KEY`（Docker Hub 官方 python 镜像的构建产物）。
    如果这个断言按"名字里带 KEY"模糊匹配，它会永远启动失败 ——
    见 pitfalls 条 17b：按值的形状判，别按变量名。这里选择了更简单的路：
    精确名单。所以 GPG_KEY 必须通过。
    """
    # TMPDIR 写成数据卷下的路径而不是 `/tmp`：既符合 CLAUDE.md 的实际约定
    # （TMPDIR 指到同路径挂载的卷下），也顺带避开 ruff 的 S108（硬编码 /tmp）。
    assert_no_credential_env(
        {
            "PATH": "/usr/bin",
            "GPG_KEY": "ABCDEF0123456789",
            "TMPDIR": "/srv/strix-data/tmp",
        }
    )


# =============================================================================
# 四、错误码登记表
# =============================================================================
def test_all_error_subclasses_are_registered() -> None:
    """每个 `ConsoleError` 子类都必须在 `ALL_ERRORS` 里。

    `ALL_ERRORS` 是手写的扁平元组 —— 刻意没有 `__init_subclass__` 自动注册
    （魔法注册需要论证，而这里唯一的收益是少写一行）。代价是"可能忘了登记"，
    这条测试就是把那个代价转成一次红色的测试失败。
    """
    assert set(ConsoleError.__subclasses__()) == set(ALL_ERRORS)


def test_error_codes_are_unique() -> None:
    """码重复会让前端的分支静默走错一支。"""
    codes = [e.code for e in ALL_ERRORS]
    assert len(codes) == len(set(codes)), f"重复的机器码：{codes}"


def test_error_codes_are_snake_case() -> None:
    """码是对外契约的一部分，风格必须统一，否则前端要记两套写法。"""
    for err in ALL_ERRORS:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", err.code), err.code


def test_error_payload_shape() -> None:
    """响应体只有 `code` / `trace_id` / `params` 三个字段。

    刻意没有 `message`：中文文案在前端 `zh-CN.json`。后端一旦提供 message，
    就一定会有人直接把它显示出来，于是我们同时维护两份文案，而后端那份还会
    绕过脱敏。
    """
    payload = InternalError(hint="x").to_payload("trace-1")
    assert set(payload) == {"code", "trace_id", "params"}
    assert payload["code"] == "internal_error"
    assert payload["trace_id"] == "trace-1"
    assert payload["params"] == {"hint": "x"}


def test_str_of_error_does_not_leak_params() -> None:
    """`str(exc)` 只有码。

    logging 默认会 `str()` 异常，params 里若不小心带了敏感内容，那条路径会绕过
    我们对响应体的所有约束。
    """
    assert str(InternalError(target="example.com")) == "internal_error"


def test_scan_failure_codes_are_not_http_errors() -> None:
    """扫描归因码与 HTTP 错误码是**两套**，不许交叉。

    给 `llm_tls_intercepted` 编一个 HTTP status 是假的 —— 没有任何接口会用它做
    响应码，而假字段最终一定会被人当真用。
    """
    http_codes = {e.code for e in ALL_ERRORS}
    assert not (http_codes & SCAN_FAILURE_CODES)


def test_scan_incomplete_is_registered() -> None:
    """`scan_incomplete` 是发布阻断项那一条。

    退出码 0 **不代表扫描跑完了** —— 预算耗尽被掐死的扫描同样退 0 并宣称
    "未发现漏洞"。一个渗透测试控制台在钱花光时报"目标干净"，比不报任何结论危险得多。
    """
    assert "scan_incomplete" in SCAN_FAILURE_CODES


def test_typo_in_failure_code_is_caught() -> None:
    """拼错的码不会静默通过。

    归因逻辑是一串正则匹配，很容易在新增分支时把码拼错（`llm_tls_intercept`
    少个 `ed`）。拼错的码只会让前端落到"未知错误"分支 —— 一个静默的失败模式。
    """
    assert assert_scan_failure_code("llm_tls_intercepted") == "llm_tls_intercepted"
    with pytest.raises(ValueError, match="未登记"):
        assert_scan_failure_code("llm_tls_intercept")
