#!/usr/bin/env python3
"""从 PyPI JSON API 取哈希，生成 pip 可直接消费的 hash lock。

为什么不直接用 `pip-compile --generate-hashes`：
    pip-tools 的 PyPIRepository._get_req_hashes 本来就是"JSON API 优先"：
        pypi_hashes_by_link = self._get_hashes_from_pypi(ireq)
        ... if candidate.link.url in pypi_hashes_by_link -> 用 JSON 里的哈希
        ... else                                         -> _get_file_hash(link)  # 下载整个文件
    而 _get_hashes_from_pypi 在 JSON 请求失败时 **静默 return {}**。于是本机这种
    "能连通但会中途断流"的网络上，一次元数据请求失败就退化成把整棵依赖树 ×
    全部平台的 wheel 全下一遍（实测跑 45 分钟仍未完成）。
    本脚本做的是同一件事 —— 同一个数据源（JSON API 的 digests.sha256）、同一套
    文件筛选（sdist + bdist_wheel）—— 但**显式重试、失败即报错**，绝不退化成下载。

刻意只用标准库：宿主 Python 是 3.9.6 且无包管理器，容器里是 3.12，两边都要能跑。

用法：
    python3 scripts/gen_lock.py <pins 文件> <输出 lock 文件>
其中 pins 文件每行一个 `name==version`。
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request

# 与 pip-tools 的 HASHABLE_PACKAGE_TYPES 保持一致。
# sdist 的哈希也要收：pip-compile 同样会收，而"不许选 sdist"是由
# --only-binary=:all: 保证的，不是靠 lock 里没有它（见 pitfalls 条 4）。
HASHABLE = {"sdist", "bdist_wheel"}

RETRIES = 8
BACKOFF = 3.0
TIMEOUT = 60


def normalize(name: str) -> str:
    """PEP 503 名称归一化。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def fetch_hashes(name: str, version: str) -> list[str]:
    """取某个 name==version 的全部可哈希发布文件的 sha256（跨全部平台）。

    失败就抛异常 —— 绝不返回空集合，否则会静默产出一个装不上的 lock。
    """
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    last: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
            last = exc
            if attempt < RETRIES:
                time.sleep(BACKOFF * attempt)
    else:
        raise RuntimeError(f"{name}=={version}: JSON API 取了 {RETRIES} 次都失败：{last}")

    digests = sorted(
        f["digests"]["sha256"]
        for f in payload["urls"]
        if f.get("packagetype") in HASHABLE and "sha256" in f.get("digests", {})
    )
    if not digests:
        raise RuntimeError(f"{name}=={version}: JSON API 里没有任何 sdist/wheel 的 sha256")
    return digests


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    pins_path, out_path = sys.argv[1], sys.argv[2]

    # 输入就是 pip-compile 的产物，所以要容忍它的三种非 pin 行：
    # 开头的选项行（`--only-binary :all:`）、空行、缩进的 `# via ...` 溯源注释。
    pins: list[tuple[str, str]] = []
    for lineno, raw in enumerate(open(pins_path, encoding="utf-8"), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        line = line.split("#", 1)[0].strip()  # 行尾注释
        if "==" not in line:
            raise SystemExit(f"{pins_path}:{lineno}: 不是 name==version：{raw.rstrip()!r}")
        name, version = line.split("==", 1)
        pins.append((normalize(name.strip()), version.strip()))
    pins.sort()

    lines = [
        "# 由 scripts/gen_lock.py 生成，请勿手改 —— 改依赖请改 pyproject.toml 后跑 `make lock`。",
        "#",
        "# 哈希来自 PyPI JSON API 的 digests.sha256，覆盖每个版本的**全部平台**发布文件，",
        "# 所以同一份 lock 在 arm64 与 x86_64 宿主上都能装。",
        "# 安装侧必须同时给 --require-hashes 与 --only-binary=:all:（两者正交，见 pitfalls 条 4）。",
        "",
    ]
    total = len(pins)
    for i, (name, version) in enumerate(pins, 1):
        digests = fetch_hashes(name, version)
        print(f"[{i}/{total}] {name}=={version}  {len(digests)} 个哈希", file=sys.stderr)
        entry = f"{name}=={version}"
        for d in digests:
            entry += f" \\\n    --hash=sha256:{d}"
        lines.append(entry)

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"已写出 {out_path}：{total} 个包", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
