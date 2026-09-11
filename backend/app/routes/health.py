"""`GET /api/health` —— 存活探针。

# 它为什么值得一个自己的文件

它在 T2 里是 `main.py` 里的一个内联函数。搬出来的理由不是整齐，是**它与
`/api/system/status` 的界线需要一个地方写下来**：两个接口看起来都在"检查健康"，
而把依赖检查塞进哪一个，后果差得很远。

  · 本接口：**不查任何依赖。** 不探 docker、不碰 DB、不做同路径自检。
    语义是"这个进程还在、还能响应"。消费者是 nginx 与（将来的）compose healthcheck，
    它们**没有 cookie**，所以它是 `EXEMPT_PATHS` 里唯一的非鉴权业务路径。
  · `/api/system/status`（`routes/system.py`）：查全部依赖，但由**人**主动触发，
    失败也不重启谁。

把依赖检查塞进本接口的后果很具体：docker daemon 抖一下 → healthcheck 失败 →
compose 重启 `api` → 内存 KeyVault 清空 → 所有人的凭据丢失。也就是说，一个"更全面
的健康检查"会**制造**故障。这条不是权衡，是禁令。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app import __version__
from app.models import HealthResponse

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """三个字段，零依赖查询。

    `strix_version` 取 `app.state`（lifespan 启动时解析并断言过），不在这里再读一次
    包元数据：那是一次磁盘 IO，而本接口会被 healthcheck 以秒级频率打。
    """
    return HealthResponse(
        status="ok",
        app_version=__version__,
        strix_version=request.app.state.strix_version,
    )
