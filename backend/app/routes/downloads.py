"""`GET /api/scans/{id}/export/{kind}` 与 `GET /api/scans/{id}/raw.zip`（T23＋T24）。

export 把 Strix 在 run 目录根下写好的 md / csv / sarif 原样下发；raw.zip 把整个扫描 cwd
打包（排除规则见 `services/raw_export.py`）。两者前置判定与中文报告 POST 同序同义：
行不在 → 404；purged → `artifacts_purged`；未到终态 → `scan_not_finished`；无 run_dir → 404。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response

from app.db import Database
from app.errors import ArtifactsPurgedError, NotFoundError, ScanNotFinishedError
from app.services import audit, raw_export
from app.services.retention import TERMINAL_STATUSES
from app.settings import Settings
from app.strix_profile import StrixProfile, profile_for

router = APIRouter(prefix="/api/scans", tags=["downloads"])

ExportKind = Literal["md", "csv", "sarif"]


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


def _export_spec(profile: StrixProfile, kind: ExportKind, scan_id: str) -> tuple[str, str, str]:
    """(run 目录下的文件名, media_type, 下载文件名)。"""
    specs: dict[str, tuple[str, str, str]] = {
        "md": (
            profile.report_markdown_name,
            "text/markdown; charset=utf-8",
            f"strix-{scan_id}-report.md",
        ),
        "csv": (
            profile.vulnerabilities_csv_name,
            "text/csv; charset=utf-8",
            f"strix-{scan_id}-vulnerabilities.csv",
        ),
        "sarif": (profile.sarif_name, "application/sarif+json", f"strix-{scan_id}.sarif"),
    }
    return specs[kind]


async def _finished_scan(request: Request, scan_id: str) -> sqlite3.Row:
    def load(conn: sqlite3.Connection) -> tuple[sqlite3.Row | None, bool]:
        scan = conn.execute("SELECT status, run_dir FROM scans WHERE id = ?", (scan_id,)).fetchone()
        return scan, audit.is_scan_purged(conn, scan_id)

    scan, purged = await _db(request).run(load)
    if scan is None:
        raise NotFoundError()
    if purged:
        raise ArtifactsPurgedError()
    if scan["status"] not in TERMINAL_STATUSES:
        raise ScanNotFinishedError()
    if scan["run_dir"] is None:
        raise NotFoundError()
    return scan


@router.get("/{scan_id}/export/{kind}")
async def export_file(scan_id: str, kind: ExportKind, request: Request) -> FileResponse:
    scan = await _finished_scan(request, scan_id)
    profile = profile_for(str(request.app.state.strix_version))
    name, media_type, filename = _export_spec(profile, kind, scan_id)
    path = Path(scan["run_dir"]) / name
    if not await asyncio.to_thread(path.is_file):
        raise NotFoundError()  # 例如没有发现时上游可能不写 csv
    return FileResponse(path, media_type=media_type, filename=filename)


@router.get("/{scan_id}/raw.zip")
async def raw_zip(scan_id: str, request: Request) -> Response:
    await _finished_scan(request, scan_id)
    scan_dir = _settings(request).scans_dir / scan_id  # 即扫描的 cwd（scan_launcher.py:507）
    body = await asyncio.to_thread(raw_export.build_raw_zip, scan_dir)
    return Response(
        content=body,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="strix-{scan_id}-raw.zip"'},
    )
