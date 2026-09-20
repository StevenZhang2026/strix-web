"""`strix_profile` 是"上游事实"的单一出处，所以这里测的是**字面值**。

这些断言看起来像"把常量抄一遍"，但它们的作用是升级预警线：`strix-agent` 换版本时，
改了目录名/状态取值域/退出码含义的那一次，必须在这里响亮地失败，而不是在某次真实
扫描里静默归错因（`test_strix_contract.py` 是同一条防线的另一半）。
"""

from __future__ import annotations

import pytest

from app.errors import SCAN_FAILURE_CODES
from app.strix_profile import PROFILES, profile_for


def test_profile_1_6_2_literals() -> None:
    profile = profile_for("1.6.2")
    assert profile.version == "1.6.2"
    assert profile.runs_dir_name == "strix_runs"
    assert profile.run_record_name == "run.json"
    assert profile.exit_code_ok == 0
    assert profile.exit_code_failed == 1
    assert profile.exit_code_vulnerabilities_found == 2


def test_profile_1_6_2_watched_rel_paths() -> None:
    """stat 门盯的三个文件名。**"字段存在"与"它的值是什么"是两条不同的事** ——
    上面那条测的是前者，这条钉住后者：升级时 `.state/` 这一层或者文件名一变，
    轮询会静默地一直看不到变化（整轮跳过），而不是响亮地失败。
    """
    profile = profile_for("1.6.2")
    assert profile.agents_record_rel_path == ".state/agents.json"
    assert profile.agents_db_rel_path == ".state/agents.db"
    assert profile.vulnerabilities_rel_path == "vulnerabilities.json"
    # `log_file_name` 不在 stat 门里，但同样会随上游版本变，所以值也要钉住。
    assert profile.log_file_name == "strix.log"


def test_run_statuses_include_interrupted() -> None:
    """`interrupted` 不在上游的状态枚举里，是信号处理器单独写进 `run.json` 的。

    漏掉它，"操作者停止"就会落到兜底分支被报成"扫描失败"。
    """
    assert profile_for("1.6.2").run_statuses == frozenset(
        {
            "running",
            "waiting",
            "completed",
            "stopped",
            "crashed",
            "failed",
            "budget_paused",
            "interrupted",
        }
    )


def test_unknown_version_refuses_to_guess() -> None:
    """未知版本必须抛，**不许回落**到最近的已知版本。

    pin 是精确的（`strix-agent==1.6.2`），所以"出现未知版本"只可能意味着有人绕过了
    升级 runbook —— 那时拿旧对照表去解释新输出，归因会静默地错。
    """
    with pytest.raises(RuntimeError) as excinfo:
        profile_for("1.5.3")
    assert "runbook" in str(excinfo.value)


def test_every_attribution_code_is_registered() -> None:
    """规则表里每个码都必须在 `SCAN_FAILURE_CODES` 里（拼错一个字母就红）。

    刻意遍历 profile 而不是编译后的元组：T29 的契约断言走的也是这条路。
    """
    rules = PROFILES["1.6.2"].attribution_rules
    assert rules, "规则表是空的，下面的遍历什么都没检查"
    for _pattern, code in rules:
        assert code in SCAN_FAILURE_CODES, f"归因码 {code!r} 没在 errors.py 登记"
