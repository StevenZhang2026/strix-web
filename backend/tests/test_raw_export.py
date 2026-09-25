"""`build_raw_zip` 的安全不变式（T24）：zip 里只有 `scan_dir` 下真实存在的普通文件。

排除顶层 `tmp/`、不跟符号链接 —— 跟了就等于新造一条读 `${DATA}` 之外文件的路径。
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

from app.services.raw_export import build_raw_zip


def _names(scan_dir: Path) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(build_raw_zip(scan_dir))) as zf:
        return set(zf.namelist())


def test_regular_files_in_nested_dirs_are_zipped_with_same_content(tmp_path: Path) -> None:
    scan_dir = tmp_path / "scan-1"
    (scan_dir / "strix_runs" / "r1" / ".state").mkdir(parents=True)
    (scan_dir / "strix_runs" / "r1" / "run.json").write_text('{"status": "completed"}')
    (scan_dir / "strix_runs" / "r1" / ".state" / "agents.db").write_bytes(b"\x00\x01db")
    (scan_dir / "cli-output.txt").write_text("hello")

    with zipfile.ZipFile(io.BytesIO(build_raw_zip(scan_dir))) as zf:
        assert set(zf.namelist()) == {
            "strix_runs/r1/run.json",
            "strix_runs/r1/.state/agents.db",
            "cli-output.txt",
        }
        assert zf.read("strix_runs/r1/run.json") == b'{"status": "completed"}'
        assert zf.read("strix_runs/r1/.state/agents.db") == b"\x00\x01db"


def test_top_level_tmp_is_excluded_but_nested_tmp_is_kept(tmp_path: Path) -> None:
    scan_dir = tmp_path / "scan-1"
    (scan_dir / "tmp" / "repo").mkdir(parents=True)
    (scan_dir / "tmp" / "repo" / "secret.txt").write_text("cloned")
    (scan_dir / "strix_runs" / "x" / "tmp").mkdir(parents=True)
    (scan_dir / "strix_runs" / "x" / "tmp" / "keep.txt").write_text("keep")

    assert _names(scan_dir) == {"strix_runs/x/tmp/keep.txt"}


def test_symlink_file_pointing_outside_is_not_zipped(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("host secret")
    scan_dir = tmp_path / "scan-1"
    scan_dir.mkdir()
    (scan_dir / "real.txt").write_text("real")
    os.symlink(outside, scan_dir / "link.txt")

    assert _names(scan_dir) == {"real.txt"}


def test_symlink_dir_is_not_entered(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.txt").write_text("host secret")
    scan_dir = tmp_path / "scan-1"
    scan_dir.mkdir()
    (scan_dir / "real.txt").write_text("real")
    os.symlink(outside, scan_dir / "linkdir", target_is_directory=True)

    assert _names(scan_dir) == {"real.txt"}


def test_missing_scan_dir_returns_empty_zip(tmp_path: Path) -> None:
    assert _names(tmp_path / "nope") == set()
