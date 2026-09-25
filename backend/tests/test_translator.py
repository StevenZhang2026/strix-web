"""Translator 服务：调模型、查缓存、并发、费用核算。

守两条不变式：
- I3 缓存：同一 (scan_id, finding_id, input_hash, model, lang) 已有行就不调模型。
  靠 test_first_run_translates_everything / test_second_run_makes_zero_calls /
  test_model_change_recalls_and_keeps_old_rows / test_force_recalls_and_overwrites 变红。
- I4 失败不毒化缓存、钱照算：不合格或调用失败的条目不写行，但已发生的调用费用照算；
  任一次费用算不出，合计就是 None（不是 0）。靠 test_rejected_twice_writes_no_row_but_counts_cost /
  test_repair_success_row_sums_both_calls / test_completion_failed_writes_no_row_but_counts_cost /
  test_unknown_cost_makes_total_none 变红。
另有并发上限（test_peak_concurrency_is_exactly_the_limit）与 needs_not_tested 参数表。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.db import Database
from app.services import report_zh
from app.services.key_vault import CredentialSet
from app.services.llm_client import Completion, CompletionFailed
from app.services.translator import (
    EXECUTIVE_MAX_TOKENS,
    FINDING_MAX_TOKENS,
    MAX_CONCURRENT_CALLS,
    TranslateResult,
    needs_not_tested,
    translate_scan,
)
from app.strix_profile import profile_for
from tests.conftest import EXEC_OK, FINDING_OK, insert_authorization, insert_scan

FINDING_UNTRANSLATED = FINDING_OK.replace("订单接口可越权查看他人订单", "IDOR 漏洞")

CREDS = CredentialSet(
    provider="anthropic",
    auth_shape="single",
    strix_llm="anthropic/claude-sonnet-4-5",
    api_base=None,
    secrets={"LLM_API_KEY": SecretStr("sk-x")},
    params={},
)
SCAN_ID = "scan-1"


def _ok(text: str, cost: float | None = 0.01) -> Completion:
    usage = None if cost is None else 10
    return Completion(text=text, usage_prompt=usage, usage_completion=usage, cost_usd=cost)


class FakeCompleter:
    """按 max_tokens 区分发现/总述；各自一条脚本队列，排空后回合格样本。"""

    def __init__(
        self,
        finding: list[Completion | CompletionFailed] | None = None,
        executive: list[Completion | CompletionFailed] | None = None,
        cost: float = 0.01,
        delay: float = 0.0,
    ) -> None:
        self.queues = {
            FINDING_MAX_TOKENS: list(finding or []),
            EXECUTIVE_MAX_TOKENS: list(executive or []),
        }
        self.defaults = {FINDING_MAX_TOKENS: FINDING_OK, EXECUTIVE_MAX_TOKENS: EXEC_OK}
        self.cost = cost
        self.delay = delay
        self.calls: list[tuple[list[dict[str, str]], int]] = []
        self.in_flight = 0
        self.peak = 0

    async def __call__(
        self, credentials: CredentialSet, messages: list[dict[str, str]], max_tokens: int
    ) -> Completion:
        self.calls.append((messages, max_tokens))
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1
        queue = self.queues[max_tokens]
        reply = queue.pop(0) if queue else _ok(self.defaults[max_tokens], self.cost)
        if isinstance(reply, CompletionFailed):
            raise reply
        return reply


def _insert_finding(conn: sqlite3.Connection, scan_id: str, finding_id: str, raw: dict) -> None:
    conn.execute(
        "INSERT INTO scan_findings (scan_id, finding_id, severity, title, first_seen_at, "
        "raw_json, input_hash) VALUES (?,?,?,?,?,?,?)",
        (
            scan_id,
            finding_id,
            "high",
            "t",
            "2026-09-08T00:00:00.000Z",
            json.dumps(raw, ensure_ascii=False, sort_keys=True),
            finding_id.ljust(64, "0")[:64],
        ),
    )


def _setup(conn: sqlite3.Connection, tmp_path: Path, n_findings: int) -> Path:
    insert_authorization(conn)
    insert_scan(conn)
    for i in range(n_findings):
        _insert_finding(conn, SCAN_ID, f"f{i}", {"title": f"finding {i}", "severity": "high"})
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "penetration_test_report.md").write_text("# Report\nIDOR on /orders", "utf-8")
    (run_dir / "coverage.json").write_text(
        json.dumps({"completeness": {"complete": False}, "gaps": ["upload"]}), "utf-8"
    )
    return run_dir


def _run(
    db: Database,
    run_dir: Path,
    completer: FakeCompleter,
    *,
    creds: CredentialSet = CREDS,
    force: bool = False,
) -> TranslateResult:
    return asyncio.run(
        translate_scan(db, SCAN_ID, run_dir, profile_for("1.6.2"), creds, completer, force=force)
    )


def _rows(conn: sqlite3.Connection) -> list[tuple]:
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT finding_id, model, cost_usd, usage_prompt, usage_completion "
            "FROM report_translations ORDER BY finding_id, model"
        )
    ]


# ---- I3 缓存 ----


def test_first_run_translates_everything(
    db: Database, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    run_dir = _setup(conn, tmp_path, 2)
    fake = FakeCompleter()
    result = _run(db, run_dir, fake)
    assert (result.translated_findings, result.cached_findings, result.failed_findings) == (2, 0, 0)
    assert result.executive == "translated"
    assert result.cost_usd == pytest.approx(0.03)
    assert len(fake.calls) == 3
    ids = [r[0] for r in _rows(conn)]
    assert ids == sorted([report_zh.EXECUTIVE_ID, "f0", "f1"])
    payload = conn.execute(
        "SELECT payload_json FROM report_translations WHERE finding_id = 'f0'"
    ).fetchone()[0]
    assert json.loads(payload) == json.loads(FINDING_OK)


def test_second_run_makes_zero_calls(
    db: Database, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    run_dir = _setup(conn, tmp_path, 2)
    _run(db, run_dir, FakeCompleter())
    fake = FakeCompleter()
    result = _run(db, run_dir, fake)
    assert fake.calls == []
    assert (result.translated_findings, result.cached_findings, result.failed_findings) == (0, 2, 0)
    assert result.executive == "cached"
    assert result.cost_usd == 0.0


def test_model_change_recalls_and_keeps_old_rows(
    db: Database, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    run_dir = _setup(conn, tmp_path, 2)
    _run(db, run_dir, FakeCompleter())
    fake = FakeCompleter()
    other = dataclasses.replace(CREDS, strix_llm="bedrock/other-model")
    result = _run(db, run_dir, fake, creds=other)
    assert len(fake.calls) == 3
    assert result.translated_findings == 2
    models = [r[1] for r in _rows(conn)]
    assert models.count(CREDS.strix_llm) == 3
    assert models.count("bedrock/other-model") == 3


def test_force_recalls_and_overwrites(
    db: Database, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    run_dir = _setup(conn, tmp_path, 2)
    _run(db, run_dir, FakeCompleter())
    fake = FakeCompleter(cost=0.05)
    result = _run(db, run_dir, fake, force=True)
    assert len(fake.calls) == 3
    assert (result.translated_findings, result.cached_findings) == (2, 0)
    rows = _rows(conn)
    assert len(rows) == 3
    assert [r[2] for r in rows] == [pytest.approx(0.05)] * 3


# ---- I4 失败不毒化缓存、钱照算 ----


def test_rejected_twice_writes_no_row_but_counts_cost(
    db: Database, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    run_dir = _setup(conn, tmp_path, 1)
    fake = FakeCompleter(finding=[_ok("not json"), _ok(FINDING_UNTRANSLATED)])
    result = _run(db, run_dir, fake)
    assert (result.translated_findings, result.failed_findings) == (0, 1)
    assert result.cost_usd == pytest.approx(0.03)  # 发现的两次 + 总述一次
    assert [r[0] for r in _rows(conn)] == [report_zh.EXECUTIVE_ID]
    finding_calls = [m for m, t in fake.calls if t == FINDING_MAX_TOKENS]
    assert len(finding_calls) == 2
    expected = report_zh.repair_messages(finding_calls[0], "not json", "no_json")
    assert finding_calls[1] == expected
    assert finding_calls[1][-2] == {"role": "assistant", "content": "not json"}


def test_repair_success_row_sums_both_calls(
    db: Database, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    run_dir = _setup(conn, tmp_path, 1)
    fake = FakeCompleter(finding=[_ok("not json", 0.02), _ok(FINDING_OK, 0.03)])
    result = _run(db, run_dir, fake)
    assert (result.translated_findings, result.failed_findings) == (1, 0)
    row = next(r for r in _rows(conn) if r[0] == "f0")
    assert row[2] == pytest.approx(0.05)
    assert row[3:] == (20, 20)


def test_completion_failed_writes_no_row_but_counts_cost(
    db: Database, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    run_dir = _setup(conn, tmp_path, 2)
    fake = FakeCompleter(
        finding=[_ok("not json", 0.02), CompletionFailed("timeout"), CompletionFailed("timeout")],
        executive=[CompletionFailed("auth")],
    )
    result = _run(db, run_dir, fake)
    assert (result.translated_findings, result.failed_findings) == (0, 2)
    assert result.executive == "failed"
    assert result.cost_usd == pytest.approx(0.02)
    assert _rows(conn) == []


def test_unknown_cost_makes_total_none(
    db: Database, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    run_dir = _setup(conn, tmp_path, 1)
    fake = FakeCompleter(finding=[_ok(FINDING_OK, None)])
    result = _run(db, run_dir, fake)
    assert result.translated_findings == 1
    assert result.cost_usd is None
    row = next(r for r in _rows(conn) if r[0] == "f0")
    assert row[2:] == (None, None, None)


# ---- 并发上限 ----


def test_peak_concurrency_is_exactly_the_limit(
    db: Database, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    run_dir = _setup(conn, tmp_path, 6)
    fake = FakeCompleter(delay=0.01)
    result = _run(db, run_dir, fake)
    assert result.translated_findings == 6
    assert fake.peak == MAX_CONCURRENT_CALLS == 4


# ---- needs_not_tested ----


@pytest.mark.parametrize(
    ("coverage", "expected"),
    [
        ({"completeness": {"complete": True}}, False),
        ({"completeness": {"complete": True}, "gaps": []}, False),
        ({"completeness": {"complete": "true"}}, True),
        ({"completeness": {"complete": True}, "gaps": ["upload"]}, True),
        ({}, True),
    ],
)
def test_needs_not_tested(coverage: dict[str, object], expected: bool) -> None:
    assert needs_not_tested(coverage) is expected
