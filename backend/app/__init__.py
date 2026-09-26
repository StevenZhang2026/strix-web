"""Strix Web 控制台后端。

`__version__` 必须与 `pyproject.toml` 的 `[project] version` 一致。
两处并存的理由：`app` 包不是 pip 安装的（Dockerfile 只 `COPY app ./app`），
`importlib.metadata.version("strix-console-backend")` 拿不到它。
一致性由 `tests/test_settings.py::test_version_matches_pyproject` 强制 ——
不靠人记得改两处。
"""

from __future__ import annotations

__version__ = "1.0.0"
