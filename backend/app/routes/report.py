"""中文报告（T21c）：`POST`／`GET /api/scans/{id}/report/zh`。

翻 20 条发现要几十秒，所以 POST 只起一个后台任务就返回 202，前端轮询 GET。

POST 的顺序（即代码顺序）：
1. 一次 `db.run`：扫描行 + 有没有 `scan.purged` 审计。
2. 行不在 → 404；purged → `artifacts_purged`；未到终态 → `scan_not_finished`；
   `run_dir` 为空（从没起来过）→ 404。
3. 同一扫描已有 `running` 的任务 → `report_in_progress`。
4. `vault.acquire(handle)`；拿不到 → `key_required`。模型不必与扫描一致（缓存键里有 model）。
5. 占位 `jobs[scan_id]` 并 `create_task`。第 3–5 步之间没有 `await` —— 这就是"查重＋占位"的原子性。
6. 返回 202 `{"status": "running"}`。

后台任务的 `finally` 里 release 凭据；异常只记**类名**（正文可能带模型输出）。
GET 只读、不写审计；只返回 `input_hash` 与当前 `scan_findings` 一致的译文。
任务状态只在进程内（`app.state.report_jobs`）：api 重启后一律是 `idle`，译文仍从库里读。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request

from app.db import Database
from app.errors import (
    ArtifactsPurgedError,
    KeyRequiredError,
    NotFoundError,
    ReportInProgressError,
    ScanNotFinishedError,
)
from app.models import BoundaryModel
from app.routes._context import actor, client_ip
from app.services import audit, report_zh
from app.services.key_vault import CredentialSet, KeyVault
from app.services.llm_client import Completer
from app.services.retention import TERMINAL_STATUSES
from app.services.translator import TranslateResult, translate_scan
from app.settings import Settings
from app.strix_profile import StrixProfile, profile_for

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scans", tags=["report"])

ReportStatus = Literal["running", "done", "failed"]


@dataclass(slots=True)  # 可变：后台任务改它的 status/result
class ReportJob:
    status: ReportStatus
    result: TranslateResult | None = None
    task: asyncio.Task[None] | None = None


class TranslateReportRequest(BoundaryModel):
    vault_handle: str
    force: bool = False


class ReportAcceptedResponse(BoundaryModel):
    status: Literal["running"]


class TranslateResultView(BoundaryModel):
    translated_findings: int
    cached_findings: int
    failed_findings: int
    executive: str
    cost_usd: float | None


class FindingZhView(BoundaryModel):
    finding_id: str
    model: str
    title_zh: str
    what_zh: str
    impact_zh: str
    fix_zh: str
    severity_zh_label: str
    severity_reason_zh: str
    effort_zh: str
    who_fixes_zh: str
    confidence_zh: str
    layman_analogy_zh: str


class ExecutiveZhView(BoundaryModel):
    model: str
    summary_zh: str
    risk_verdict_zh: str
    top3_actions_zh: list[str]
    scope_zh: str
    coverage_zh: str
    not_tested_zh: list[str]


class ReportZhResponse(BoundaryModel):
    status: Literal["idle", "running", "done", "failed"]  # 没有 job = idle（含 api 重启后）
    result: TranslateResultView | None  # 只有 done 时非空
    findings_zh: list[FindingZhView]
    executive_zh: ExecutiveZhView | None


def _db(request: Request) -> Database:
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise RuntimeError("app.state.db 不存在。本接口要求 lifespan 已经跑过 —— 检查启动日志。")
    return db


def _settings(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        raise RuntimeError("app.state.settings 不存在。本接口要求 lifespan 已经跑过。")
    return settings


def _vault(request: Request) -> KeyVault:
    vault = getattr(request.app.state, "key_vault", None)
    if vault is None:
        raise RuntimeError("app.state.key_vault 不存在。本接口要求 lifespan 已经跑过。")
    return vault


def _exc_types(exc: BaseException) -> list[str]:
    if isinstance(exc, BaseExceptionGroup):
        return [type(e).__name__ for e in exc.exceptions]
    return [type(exc).__name__]


@dataclass(frozen=True, slots=True)
class _AuditContext:
    actor: str | None
    client_ip: str | None
    user_agent: str | None
    audit_dir: Path


async def _translate(
    *,
    db: Database,
    vault: KeyVault,
    job: ReportJob,
    scan_id: str,
    run_dir: Path,
    profile: StrixProfile,
    handle: str,
    credentials: CredentialSet,
    completer: Completer,
    force: bool,
    ctx: _AuditContext,
) -> None:
    detail: dict[str, audit.AuditDetailValue]
    try:
        result = await translate_scan(
            db, scan_id, run_dir, profile, credentials, completer, force=force
        )
    except Exception as exc:  # 含 TaskGroup 冒出来的 ExceptionGroup（它是 Exception 的子类）
        job.status = "failed"
        # 只记类名：异常正文可能带模型输出（模型会复述口令）。不许 exc_info、不许 str(exc)。
        logger.error("中文报告生成失败", extra={"scan_id": scan_id, "exc_types": _exc_types(exc)})
        detail = {"outcome": "error", "model": credentials.strix_llm, "force": force}
    else:
        job.status, job.result = "done", result
        detail = {
            "outcome": "done",
            "model": credentials.strix_llm,
            "force": force,
            "translated": result.translated_findings,
            "cached": result.cached_findings,
            "failed": result.failed_findings,
            "executive": result.executive,
            "cost_usd": result.cost_usd,
        }
    finally:
        vault.release(handle)
    await audit.record(
        db=db,
        audit_dir=ctx.audit_dir,
        entry=audit.AuditEntry(
            event=audit.EVENT_REPORT_TRANSLATED,
            actor=ctx.actor,
            detail=detail,
            client_ip=ctx.client_ip,
            user_agent=ctx.user_agent,
            scan_id=scan_id,
        ),
    )


@router.post("/{scan_id}/report/zh", status_code=202)
async def translate_report(
    scan_id: str, payload: TranslateReportRequest, request: Request
) -> ReportAcceptedResponse:
    db = _db(request)
    vault = _vault(request)

    def load(conn: sqlite3.Connection) -> tuple[sqlite3.Row | None, bool]:
        scan = conn.execute(
            "SELECT status, run_dir, provider, auth_shape FROM scans WHERE id = ?", (scan_id,)
        ).fetchone()
        return scan, audit.is_scan_purged(conn, scan_id)

    scan, purged = await db.run(load)
    if scan is None:
        raise NotFoundError()
    if purged:
        raise ArtifactsPurgedError()
    if scan["status"] not in TERMINAL_STATUSES:
        raise ScanNotFinishedError()
    if scan["run_dir"] is None:
        raise NotFoundError()

    # 从这里到 create_task 之间**不许有 await**：单线程事件循环里这就是"查重＋占位"的原子性。
    jobs: dict[str, ReportJob] = request.app.state.report_jobs
    existing = jobs.get(scan_id)
    if existing is not None and existing.status == "running":
        raise ReportInProgressError()
    credentials = vault.acquire(payload.vault_handle)
    if credentials is None:
        raise KeyRequiredError(provider=scan["provider"], auth_shape=scan["auth_shape"])
    completer: Completer = request.app.state.llm_completer
    job = ReportJob(status="running")
    jobs[scan_id] = job
    job.task = asyncio.create_task(
        _translate(
            db=db,
            vault=vault,
            job=job,
            scan_id=scan_id,
            run_dir=Path(scan["run_dir"]),
            profile=profile_for(str(request.app.state.strix_version)),
            handle=payload.vault_handle,
            credentials=credentials,
            completer=completer,
            force=payload.force,
            ctx=_AuditContext(
                actor=actor(request),
                client_ip=client_ip(request),
                user_agent=request.headers.get("user-agent"),
                audit_dir=_settings(request).audit_dir,
            ),
        )
    )
    return ReportAcceptedResponse(status="running")


@router.get("/{scan_id}/report/zh")
async def get_report(scan_id: str, request: Request) -> ReportZhResponse:
    db = _db(request)

    def load(
        conn: sqlite3.Connection,
    ) -> tuple[bool, bool, list[sqlite3.Row], sqlite3.Row | None]:
        exists = conn.execute("SELECT 1 FROM scans WHERE id = ?", (scan_id,)).fetchone()
        if exists is None:
            return False, False, [], None
        findings = conn.execute(
            "SELECT t.finding_id, t.model, t.payload_json FROM report_translations t "
            "JOIN scan_findings f ON f.scan_id = t.scan_id AND f.finding_id = t.finding_id "
            "AND f.input_hash = t.input_hash "
            "WHERE t.scan_id = ? AND t.lang = ? ORDER BY t.finding_id, t.created_at, t.rowid",
            (scan_id, report_zh.LANG_ZH),
        ).fetchall()
        executive = conn.execute(
            "SELECT model, payload_json FROM report_translations "
            "WHERE scan_id = ? AND lang = ? AND finding_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (scan_id, report_zh.LANG_ZH, report_zh.EXECUTIVE_ID),
        ).fetchone()
        return True, audit.is_scan_purged(conn, scan_id), findings, executive

    exists, purged, finding_rows, executive_row = await db.run(load)
    if not exists:
        raise NotFoundError()
    if purged:
        raise ArtifactsPurgedError()

    # 同一 finding_id 多行（换过模型）：按 created_at, rowid 升序，后写的覆盖先写的。
    latest: dict[str, sqlite3.Row] = {row["finding_id"]: row for row in finding_rows}
    findings_zh = [
        FindingZhView(finding_id=fid, model=row["model"], **json.loads(row["payload_json"]))
        for fid, row in sorted(latest.items())
    ]
    executive_zh = (
        None
        if executive_row is None
        else ExecutiveZhView(
            model=executive_row["model"], **json.loads(executive_row["payload_json"])
        )
    )

    job = request.app.state.report_jobs.get(scan_id)
    if job is None:
        return ReportZhResponse(
            status="idle", result=None, findings_zh=findings_zh, executive_zh=executive_zh
        )
    result = (
        None
        if job.status != "done" or job.result is None
        else TranslateResultView(**dataclasses.asdict(job.result))
    )
    return ReportZhResponse(
        status=job.status, result=result, findings_zh=findings_zh, executive_zh=executive_zh
    )
