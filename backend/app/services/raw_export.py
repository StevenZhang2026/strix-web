"""把一次扫描的 cwd 打成 zip（T24 原始产物下载）。

不变式：zip 里只有 `scan_dir` 下**真实存在的普通文件** —— 排除顶层 `tmp/`（Strix 的
`TMPDIR`，含克隆的仓库等），且**不跟符号链接**（不打包 symlink 文件，不进入 symlink 目录）。
zip 内容的安全性只靠"`${DATA}/` 下 grep 不到 Key"成立，跟了链接就等于新造一条读
`${DATA}` 之外文件的路径。

取舍：整个 zip 在内存里建好再返回。v1 最简单；run 目录量级几十 MB，扛得住。
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

_TOP_LEVEL_EXCLUDED = "tmp"


def build_raw_zip(scan_dir: Path) -> bytes:
    """`scan_dir` 不存在时返回空 zip（`os.walk` 对不存在的目录什么也不产出）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirnames, filenames in os.walk(scan_dir, followlinks=False):
            here = Path(root)
            # 原地改 dirnames 才能阻止 os.walk 下钻；followlinks=False 只保证不下钻，
            # 剔掉 symlink 目录是为了意图显式、不依赖那个默认值。
            dirnames[:] = [
                d
                for d in dirnames
                if not os.path.islink(here / d)
                and not (here == scan_dir and d == _TOP_LEVEL_EXCLUDED)
            ]
            for name in filenames:
                path = here / name
                if os.path.islink(path) or not path.is_file():
                    continue
                zf.write(path, path.relative_to(scan_dir).as_posix())
    return buf.getvalue()
