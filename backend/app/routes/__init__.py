"""HTTP 边界层 —— 只做四件事：解析入参、调 `services/`、拼响应、设 cookie。

判断逻辑一律不写在这里（那在 `app/services/`）。理由是可测性：
`services/auth.py` 不 import fastapi，于是"8 小时后会话失效""用户名不存在与口令
错误耗时相同"这类断言可以直接对着纯对象写，不用起一个 ASGI 应用。
"""

from __future__ import annotations
