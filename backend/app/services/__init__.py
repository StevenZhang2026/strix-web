"""业务服务层 —— 不碰 HTTP，不碰 FastAPI。

本层的模块只做两件事：持有状态、做判断。它们**不 import fastapi**（`auth.py` 唯一的
非标准库依赖是 `app.errors`，而那个模块自己也只依赖标准库）。理由不是洁癖：
`services/` 里的东西要能在一次性容器里被 `python3 -m` 直接跑（`setup.sh` 的 C17f 就
这么算口令散列），而那个容器里没有 fastapi。

HTTP 边界在 `app/routes/`。
"""

from __future__ import annotations
