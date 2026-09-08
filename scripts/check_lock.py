#!/usr/bin/env python3
"""校验 backend/requirements.lock 与 pins 文件一致、且每个条目都带合法哈希。

为什么需要这一步：坏 lock 的失败模式**全是安静的**。最现实的一种是
`lock-resolve` 更新了 pins、`lock-hash` 半路网络中断 —— 于是 lock 是上一次的旧内容，
`pip install` 照样成功，装出来的却不是解析结果。所以把这几条当断言跑，不当文档写。

**明确不在这里检查的：跨架构覆盖（验收项 A11）。**
lock 里只有裸 `sha256:`，没有文件名，所以"aarch64 和 x86_64 的哈希都在吗"这个问题
**在离线状态下不可判定**。这条不变量的正确落点是 scripts/gen_lock.py 的文件筛选 ——
它只按 packagetype 过滤（sdist / bdist_wheel），代码里根本没有按平台过滤的分支，
覆盖全平台是构造上保证的。要端到端复核就得回 PyPI 比对，那属于 make verify-e2e。

用法：
    python3 scripts/check_lock.py <pins 文件> <lock 文件>
"""

from __future__ import annotations

import re
import sys

# 禁区：strix-agent 的 pin 不许升级或放宽（CLAUDE.md §Strix 集成）。
# 放宽 pin 的后果不是"版本变了"，而是 sdist 的 hatch 钩子缺 Go 1.24 直接硬失败，
# 且 PLAN.md 里那些 file:line 出处会集体失效。
REQUIRED_PINS = {"strix-agent": "1.5.3"}

SHA256 = re.compile(r"^[0-9a-f]{64}$")


def normalize(name: str) -> str:
    """PEP 503 名称归一化，必须与 gen_lock.py 一致。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def read_pins(path: str) -> dict[str, str]:
    """读 pip-compile 产出的 requirements 文件，取出 name==version。

    pip-compile 的输出里除了 pin 还有缩进的 `# via ...` 注释行，跳过即可。
    """
    pins: dict[str, str] = {}
    for lineno, raw in enumerate(open(path, encoding="utf-8"), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        line = line.split("#", 1)[0].strip()  # 行尾注释
        if "==" not in line:
            raise SystemExit(f"{path}:{lineno}: 不是 name==version：{raw.rstrip()!r}")
        name, version = line.split("==", 1)
        pins[normalize(name.strip())] = version.strip()
    return pins


def read_lock(path: str) -> dict[str, tuple[str, list[str]]]:
    """读 lock，返回 {name: (version, [sha256...])}。"""
    entries: dict[str, tuple[str, list[str]]] = {}
    current: str | None = None
    for lineno, raw in enumerate(open(path, encoding="utf-8"), 1):
        line = raw.strip().rstrip("\\").strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("--hash="):
            if current is None:
                raise SystemExit(f"{path}:{lineno}: --hash 出现在任何包之前")
            algo, _, digest = line[len("--hash=") :].partition(":")
            if algo != "sha256" or not SHA256.match(digest):
                raise SystemExit(f"{path}:{lineno}: 哈希不是合法 sha256：{line!r}")
            entries[current][1].append(digest)
            continue
        if "==" not in line:
            raise SystemExit(f"{path}:{lineno}: 无法解析：{raw.rstrip()!r}")
        name, version = line.split("==", 1)
        current = normalize(name.strip())
        if current in entries:
            raise SystemExit(f"{path}:{lineno}: {current} 重复出现")
        entries[current] = (version.strip(), [])
    return entries


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    pins_path, lock_path = sys.argv[1], sys.argv[2]

    pins = read_pins(pins_path)
    lock = read_lock(lock_path)
    problems: list[str] = []

    # 1. 集合必须完全相等 —— 这条抓的就是"lock 是旧的"。
    missing = sorted(set(pins) - set(lock))
    extra = sorted(set(lock) - set(pins))
    if missing:
        problems.append(f"lock 里缺这些包（共 {len(missing)} 个）：{', '.join(missing)}")
    if extra:
        problems.append(f"lock 里多出这些包（共 {len(extra)} 个）：{', '.join(extra)}")

    # 2. 版本必须逐个对得上。
    for name in sorted(set(pins) & set(lock)):
        if pins[name] != lock[name][0]:
            problems.append(f"{name} 版本不一致：pins={pins[name]} lock={lock[name][0]}")

    # 3. 每个条目至少一个哈希，且同一条目内不许重复。
    for name, (version, digests) in sorted(lock.items()):
        if not digests:
            problems.append(f"{name}=={version} 一个哈希都没有")
        elif len(digests) != len(set(digests)):
            problems.append(f"{name}=={version} 有重复哈希")

    # 4. 禁区 pin。
    for name, want in REQUIRED_PINS.items():
        got = lock.get(name, (None, []))[0]
        if got != want:
            problems.append(f"{name} 必须精确 pin 在 {want}，实际是 {got}（禁区，改它要先问）")

    if problems:
        print(f"lock 校验失败，{len(problems)} 个问题：", file=sys.stderr)
        for p in problems:
            print(f"  ✗ {p}", file=sys.stderr)
        return 1

    total = sum(len(d) for _, d in lock.values())
    print(f"✓ lock 校验通过：{len(lock)} 个包 / {total} 个哈希，与 {pins_path} 逐项一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
