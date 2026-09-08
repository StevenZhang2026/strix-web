"""HTTP 边界模型。

本文件在 T2 只有两个模型 —— 这是刻意的。它的真正交付物是**约定**：
下游任务（T3/T6/T7/T9）的请求与响应模型都从 `BoundaryModel` 继承，
从而自动获得下面那两条配置。提前把 `ScanCreateRequest` 之类写在这里是
"为以后可能"预留（CLAUDE.md §编码哲学 3），不做。

内部传参用 `dataclass` 而不是裸 dict（CLAUDE.md §Python），但 T2 还没有需要跨模块
传递的内部结构 —— 有了再加，同一个理由。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class BoundaryModel(BaseModel):
    """所有 HTTP 出入参模型的基类。

    `extra="forbid"` 是一条**安全**配置，不只是严格性偏好：
    默认的 `extra="ignore"` 会让 `POST /api/scans {"vault_handle": "...",
    "api_key": "sk-ant-..."}` 静默通过 —— 那个 `api_key` 字段会被丢掉，
    但**它已经进过 HTTP 日志和请求体**了，而客户端会以为它被接受了。
    `forbid` 让这种请求变成 422，前端立刻知道自己发错了。
    §N1 的"多余的凭据键一律 400 unexpected_secret_key"是同一条思路在业务层的版本。

    `frozen=True`：请求模型被校验完就是事实，改它等于让"日志里记的入参"与"实际用的
    入参"不一致。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class HealthResponse(BoundaryModel):
    """`GET /api/health`。

    刻意**只有这三个字段**，且刻意**不查任何东西**（不探 docker、不查 DB、
    不做同路径自检）。它的语义是"这个进程还在、还能响应"，被 compose 的 healthcheck
    和 nginx 用。

    把依赖检查塞进 health 的后果很具体：docker daemon 抖一下，healthcheck 失败，
    compose 重启 `api` —— 而重启 `api` 会清空内存 KeyVault，把所有人的凭据弄丢。
    也就是说，一个"更全面"的健康检查会**制造**故障。
    真正的依赖自检是 `GET /api/system/status`（T3），它由人主动触发，失败也不重启谁。
    """

    status: str
    app_version: str
    strix_version: str


class ErrorResponse(BoundaryModel):
    """所有错误响应的统一形状，对应 `ConsoleError.to_payload()`。

    只有三个字段，前端按 `code` 分支、**绝不匹配文案**（CLAUDE.md §错误与文案）。
    这里没有 `message` 字段：中文文案在 `frontend/messages/zh-CN.json`（T5）。
    后端一旦提供了 message，前端就一定会有人直接把它显示出来，然后我们就同时维护
    两份文案，并且后端那份还会绕过脱敏。

    `params` 只给前端做文案插值（`{"target": "example.com"}`），
    **绝不放凭据、绝不放中文**。
    """

    code: str
    trace_id: str
    params: dict[str, str | int | float | bool | None]
