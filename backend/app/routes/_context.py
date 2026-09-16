"""从 `Request` 里取审计需要的两个上下文字段。

第三个路由（T12c 的 `scans.py`）要用同一份逻辑时才提取（CLAUDE.md §编码哲学 3
「重复第三次才提取」）—— 在此之前它在 `keys.py` 与 `allowlist.py` 里各有一份副本，
`keys.py` 里还留着一条"该被提取"的注释。现在那两处都 import 这里，**不许再出现第四份**。

为什么不放进 `services/audit.py`：那个模块刻意不认识 FastAPI 的 `Request`
（它的入参是 `AuditEntry` 这个纯数据），把 HTTP 层的取值搬进去会让审计服务从此依赖
web 框架。这里是路由层的私有工具，所以文件名带下划线、不注册任何路由。
"""

from __future__ import annotations

import logging

from fastapi import Request

from app.services.auth import Session

logger = logging.getLogger(__name__)


def actor(request: Request) -> str | None:
    """操作者用户名，来自全局鉴权依赖挂上的 `request.state.session`。

    取不到就记 `None` 而不是抛异常：审计记录**宁可缺一个字段也不能丢一整条**。
    写审计的路由都在鉴权之后，所以取不到意味着鉴权机制被改坏了 —— 那时更需要留下
    这条记录。
    """
    session = getattr(request.state, "session", None)
    if isinstance(session, Session):
        return session.username
    logger.warning("审计缺少操作者：request.state.session 不存在", extra={"path": request.url.path})
    return None


def client_ip(request: Request) -> str | None:
    """`X-Real-IP`，**不是** `X-Forwarded-For`。

    `nginx.conf` 用 `proxy_set_header X-Real-IP $remote_addr` —— 它**覆盖**客户端送来的
    同名头，所以是可信的。`X-Forwarded-For` 用的是 `$proxy_add_x_forwarded_for`，
    那是"客户端给的值 + 真实地址"的拼接，前半段完全由客户端控制。往审计里写一个
    可伪造的地址比不写更糟。

    直连（没经过 nginx，比如单测或本机排障）时回落到 `client.host`。
    """
    real_ip = request.headers.get("x-real-ip")
    if real_ip is not None:
        return real_ip
    return None if request.client is None else request.client.host
