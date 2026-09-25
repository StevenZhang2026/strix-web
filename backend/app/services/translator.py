"""把一次扫描的发现与总述译成中文：查缓存、调模型、校验、失败修复一次、写缓存、算钱。

不碰路由与 vault：凭据和 completer 由调用方传入（T21c）。
模型输出正文一个字都不进日志 —— 它可能复述口令。
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.db import Database
from app.services import report_zh
from app.services.audit import iso_utc
from app.services.key_vault import CredentialSet
from app.services.llm_client import Completer, Completion, CompletionFailed
from app.services.report_zh import ExecutiveZh, FindingZh, Message, PayloadRejected
from app.strix_bridge.projection import read_report_markdown
from app.strix_profile import StrixProfile

logger = logging.getLogger(__name__)

FINDING_MAX_TOKENS = 1500
EXECUTIVE_MAX_TOKENS = 2000
MAX_CONCURRENT_CALLS = 4

_UPSERT = (
    "INSERT INTO report_translations (scan_id, finding_id, input_hash, model, lang, "
    "payload_json, cost_usd, usage_prompt, usage_completion, created_at) "
    "VALUES (?,?,?,?,?,?,?,?,?,?) "
    "ON CONFLICT(scan_id, finding_id, input_hash, model, lang) DO UPDATE SET "
    "payload_json=excluded.payload_json, cost_usd=excluded.cost_usd, "
    "usage_prompt=excluded.usage_prompt, usage_completion=excluded.usage_completion, "
    "created_at=excluded.created_at"
)


@dataclass(frozen=True, slots=True)
class TranslateResult:
    translated_findings: int
    cached_findings: int
    failed_findings: int
    executive: str  # "translated" | "cached" | "failed"
    cost_usd: float | None  # 本轮实际发生的调用之和；None = 至少一次算不出


@dataclass(frozen=True, slots=True)
class _Item:
    finding_id: str
    input_hash: str
    messages: list[Message]
    max_tokens: int
    parse: Callable[[str], FindingZh | ExecutiveZh]


def needs_not_tested(coverage: Mapping[str, object]) -> bool:
    """覆盖不完整（complete 不是字面 True）或有缺口 → 总述必须列出"没测到什么"。"""
    completeness = coverage.get("completeness")
    complete = completeness.get("complete") if isinstance(completeness, Mapping) else None
    gaps = coverage.get("gaps")
    return complete is not True or (isinstance(gaps, list) and len(gaps) > 0)


def _read_coverage(path: Path) -> Mapping[str, object]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _sum_or_none(values: list[float | None] | list[int | None]) -> float | None:
    """任一为 None 则 None（算不出 ≠ 0）；空列表是 0。"""
    if any(v is None for v in values):
        return None
    return sum(v for v in values if v is not None)


async def translate_scan(
    db: Database,
    scan_id: str,
    run_dir: Path,
    profile: StrixProfile,
    credentials: CredentialSet,
    completer: Completer,
    *,
    force: bool,
) -> TranslateResult:
    model = credentials.strix_llm
    lang = report_zh.LANG_ZH

    def read_db(
        conn: sqlite3.Connection,
    ) -> tuple[list[sqlite3.Row], dict[str, int], set[tuple[str, str]]]:
        findings = conn.execute(
            "SELECT finding_id, input_hash, raw_json FROM scan_findings WHERE scan_id = ?",
            (scan_id,),
        ).fetchall()
        counts = conn.execute(
            "SELECT severity, COUNT(*) FROM scan_findings WHERE scan_id = ? GROUP BY severity",
            (scan_id,),
        ).fetchall()
        cached = conn.execute(
            "SELECT finding_id, input_hash FROM report_translations "
            "WHERE scan_id = ? AND model = ? AND lang = ?",
            (scan_id, model, lang),
        ).fetchall()
        return findings, {r[0]: r[1] for r in counts}, {(r[0], r[1]) for r in cached}

    findings, severity_counts, cached = await db.run(read_db)
    if force:
        cached = set()
    report_md = await asyncio.to_thread(read_report_markdown, run_dir)
    coverage = await asyncio.to_thread(_read_coverage, run_dir / profile.coverage_record_name)

    items = [
        _Item(
            finding_id=r[0],
            input_hash=r[1],
            messages=report_zh.finding_messages(json.loads(r[2])),
            max_tokens=FINDING_MAX_TOKENS,
            parse=report_zh.parse_finding,
        )
        for r in findings
    ]
    require_not_tested = needs_not_tested(coverage)
    exec_messages = report_zh.executive_messages(report_md, severity_counts, coverage)
    executive = _Item(
        finding_id=report_zh.EXECUTIVE_ID,
        input_hash=hashlib.sha256(exec_messages[1]["content"].encode("utf-8")).hexdigest(),
        messages=exec_messages,
        max_tokens=EXECUTIVE_MAX_TOKENS,
        parse=lambda t: report_zh.parse_executive(t, require_not_tested=require_not_tested),
    )

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CALLS)

    async def run_item(item: _Item) -> tuple[bool, list[Completion]]:
        calls: list[Completion] = []
        messages = item.messages
        parsed: FindingZh | ExecutiveZh | None = None
        while parsed is None and len(calls) < 2:
            try:
                async with semaphore:
                    completion = await completer(credentials, messages, item.max_tokens)
            except CompletionFailed as e:
                logger.info(
                    "翻译调用失败",
                    extra={
                        "scan_id": scan_id,
                        "finding_id": item.finding_id,
                        "failure_kind": e.failure_kind,
                    },
                )
                return False, calls
            calls.append(completion)
            try:
                parsed = item.parse(completion.text)
            except PayloadRejected as e:
                messages = report_zh.repair_messages(item.messages, completion.text, e.reason)
        if parsed is None:
            return False, calls

        row = (
            scan_id,
            item.finding_id,
            item.input_hash,
            model,
            lang,
            json.dumps(dataclasses.asdict(parsed), ensure_ascii=False),
            _sum_or_none([c.cost_usd for c in calls]),
            _sum_or_none([c.usage_prompt for c in calls]),
            _sum_or_none([c.usage_completion for c in calls]),
            iso_utc(datetime.now(UTC)),
        )
        await db.run(lambda conn: conn.execute(_UPSERT, row))
        return True, calls

    todo = [i for i in [*items, executive] if (i.finding_id, i.input_hash) not in cached]
    async with asyncio.TaskGroup() as tg:
        tasks = {i.finding_id: tg.create_task(run_item(i)) for i in todo}

    all_calls = [c for t in tasks.values() for c in t.result()[1]]
    ok = {fid: t.result()[0] for fid, t in tasks.items()}
    finding_ids = [i.finding_id for i in items]
    exec_ok = ok.get(executive.finding_id)
    cost = _sum_or_none([c.cost_usd for c in all_calls])
    return TranslateResult(
        translated_findings=sum(1 for f in finding_ids if ok.get(f) is True),
        cached_findings=sum(1 for f in finding_ids if f not in ok),
        failed_findings=sum(1 for f in finding_ids if ok.get(f) is False),
        executive="cached" if exec_ok is None else ("translated" if exec_ok else "failed"),
        cost_usd=None if cost is None else float(cost),
    )
