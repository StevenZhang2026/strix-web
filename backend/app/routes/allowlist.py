"""授权清单的读写接口（契约在 `PLAN.md:581`）。

    GET    /api/allowlist                    读当前状态（含"文件是坏的"这个状态）
    PUT    /api/allowlist                    整份替换
    POST   /api/allowlist/entries            加一条
    DELETE /api/allowlist/entries/{label}    删一条

# 四件在本文件里被刻意固定下来的事

1. **每一次改动都写审计。** 事件是 `allowlist.changed`，双写 `audit_log` 表与
   `${DATA}/audit/YYYY-MM.ndjson`（见 `services/audit.py`）。授权清单就是"谁批准扫什么"
   的记录，改它而不留痕，等于让这份记录可以被无声地伪造。顺序是**先落盘、再写审计** ——
   反过来会在写文件失败时留下一条描述从未发生过的改动的审计。

2. **坏配置写不进文件。** `PUT` 的请求体类型就是 `AllowlistConfig` 本身，所以校验发生在
   路由函数**被调用之前**（FastAPI → Pydantic → `RequestValidationError` → 422
   `invalid_request`）。也就是说"先写文件再校验"这个顺序在结构上不可能出现，不需要
   任何一行防守代码。

3. **增量编辑要求当前文件可读。** 见 `errors.AllowlistFileBrokenError` —— 拿空配置当基准
   会让"加一条"变成"删掉其它全部"。

4. **正文里没有中文。** 文件错误是机器码（`AllowlistFileErrorCode`），前端查文案。
   条目里的 `label` / `owner` 是操作者自己写的文本，原样往返。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request
from pydantic import ValidationError

from app.errors import AllowlistFileBrokenError, InvalidRequestError, NotFoundError
from app.models import BoundaryModel
from app.services.allowlist import (
    AllowlistConfig,
    AllowlistEntry,
    AllowlistSnapshot,
    AllowlistStore,
    effective_mode,
)
from app.services.audit import EVENT_ALLOWLIST_CHANGED, AuditDetailValue, AuditEntry, record
from app.services.auth import Session
from app.services.target_guard import AllowlistMode
from app.settings import Settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/allowlist", tags=["allowlist"])

# 审计正文里最多列几个 label。
#
# 一次整份替换可以改动 500 条，每个 label 最长 200 字符 —— 全列出来是一行 100 KB 的
# ndjson，而 `grep` 一行 100 KB 没有任何用处（见 `services/audit.py` 里对 user_agent
# 的同一条判断）。超出时给 `labels_truncated: true`，计数字段仍然是准确的。
_MAX_AUDIT_LABELS = 20

_OP_REPLACE = "replace"
_OP_ADD = "add"
_OP_REMOVE = "remove"


class AllowlistStateResponse(BoundaryModel):
    """四个读写接口共用的响应体：**改完之后的完整状态**。

    为什么写操作也返回整份状态而不是 201 + 被创建的那一条：客户端下一步要显示的就是
    整份清单，返回单条会让它必须紧接着再发一个 GET —— 而那两次请求之间清单可能又被
    手工改过（文件是可以直接编辑的），于是界面显示的是一份自相矛盾的拼接结果。
    """

    config: AllowlistConfig | None
    """内存里当前生效的配置。`null` 有两种含义，靠 `file_present` 区分：
    没有文件（还没开始用）/ 冷启动就解析失败（此时按 `effective_mode` 失败关闭）。
    """

    effective_mode: str
    """此刻真正生效的模式。**不等于** `config.mode`，见 `allowlist.effective_mode`。"""

    file_present: bool
    stale: bool
    """正在用内存里的旧配置（磁盘上那份此刻读不懂）。"""

    file_error: str | None
    """`AllowlistFileErrorCode` 的值。前端在 `allowlistFile.*` 下查文案（编辑器任务落地）。"""

    file_error_line: int | None
    """1 起的行号，只有 YAML 语法错才有。"""


def _store(request: Request) -> AllowlistStore:
    store = getattr(request.app.state, "allowlist", None)
    if not isinstance(store, AllowlistStore):
        raise RuntimeError(
            "app.state.allowlist 不存在。本接口要求 lifespan 已经跑过 —— "
            "测试里请用 `with TestClient(app):`。"
        )
    return store


def _settings(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    if not isinstance(settings, Settings):
        raise RuntimeError(
            "app.state.settings 不存在。本接口要求 lifespan 已经跑过 —— "
            "测试里请用 `with TestClient(app):`。"
        )
    return settings


def _view(snapshot: AllowlistSnapshot) -> AllowlistStateResponse:
    error = snapshot.error
    return AllowlistStateResponse(
        config=snapshot.config,
        effective_mode=effective_mode(snapshot).value,
        file_present=snapshot.file_present,
        stale=snapshot.stale,
        file_error=None if error is None else error.code.value,
        file_error_line=None if error is None else error.line,
    )


def _base_config(snapshot: AllowlistSnapshot) -> AllowlistConfig:
    """增量编辑的基准。文件坏了就拒绝，别拿空配置顶上（见模块 docstring 第 3 条）。

    文件**不存在**时给一份空配置：那是合法的起点（第一次往清单里加东西）。
    """
    if snapshot.config is not None:
        return snapshot.config
    if snapshot.file_present:
        error = snapshot.error
        raise AllowlistFileBrokenError(file_error=None if error is None else error.code.value)
    return AllowlistConfig()


def _rebuilt(*, mode: AllowlistMode, entries: tuple[AllowlistEntry, ...]) -> AllowlistConfig:
    """用改动后的条目重建一份配置，**真的跑一遍校验器**。

    唯一可能失败的是 `label` 重名（条目自身的字段校验已经在请求体解析时过了一遍）。
    那是一个"请求内容不合法"，所以 422 `invalid_request` + `field=label`。

    **不把 `ValidationError` 的正文带出去**：它会把出错字段的输入值原样嵌进消息，
    而这条消息会进日志与 HTTP 响应（与 `parse_allowlist_text` 里同一条理由）。
    """
    try:
        return AllowlistConfig(mode=mode, entries=entries)
    except ValidationError as exc:
        logger.warning("授权清单改动被拒：条目集合不合法", extra={"path": "/api/allowlist"})
        raise InvalidRequestError(field="label") from exc


def _capped_labels(labels: list[str]) -> tuple[list[str], bool]:
    """`(前 N 个, 是否被截断)`。见 `_MAX_AUDIT_LABELS`。"""
    if len(labels) <= _MAX_AUDIT_LABELS:
        return labels, False
    return labels[:_MAX_AUDIT_LABELS], True


async def _write_and_audit(
    request: Request,
    *,
    config: AllowlistConfig,
    detail: dict[str, AuditDetailValue],
) -> AllowlistStateResponse:
    """原子写文件 → 写审计 → 返回新状态。**这是四个写入口的唯一落地路径。**

    `to_thread`：`write()` 是同步阻塞（open/write/fsync/replace）。
    """
    store = _store(request)
    snapshot = await asyncio.to_thread(store.write, config)
    await record(
        db=request.app.state.db,
        audit_dir=_settings(request).audit_dir,
        entry=AuditEntry(
            event=EVENT_ALLOWLIST_CHANGED,
            actor=_actor(request),
            detail=detail,
            client_ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        ),
    )
    return _view(snapshot)


def _actor(request: Request) -> str | None:
    """操作者用户名，来自全局鉴权依赖挂上的 `request.state.session`。

    取不到就记 `None` 而不是抛异常：审计记录**宁可缺一个字段也不能丢一整条**。
    本路由在鉴权之后，所以取不到意味着鉴权机制被改坏了 —— 那时更需要留下这条记录。
    """
    session = getattr(request.state, "session", None)
    if isinstance(session, Session):
        return session.username
    logger.warning("审计缺少操作者：request.state.session 不存在", extra={"path": request.url.path})
    return None


def _client_ip(request: Request) -> str | None:
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


# =============================================================================
# 接口
# =============================================================================
@router.get("", response_model=AllowlistStateResponse)
async def get_allowlist(request: Request) -> AllowlistStateResponse:
    """当前状态。**它必须能说出"文件坏了但内存里还有一份旧的"这件事** ——
    那正是 `stale` + `file_error` 的用途（`services/allowlist.py` 模块 docstring 第 1 条）。

    `current()` 顺带做一次热重载检查（stat + 必要时 read），所以它是同步阻塞的。
    """
    snapshot = await asyncio.to_thread(_store(request).current)
    return _view(snapshot)


@router.put("", response_model=AllowlistStateResponse)
async def replace_allowlist(request: Request, payload: AllowlistConfig) -> AllowlistStateResponse:
    """整份替换。**也是文件坏掉之后的恢复手段**（见模块 docstring 第 2、3 条）。

    审计正文记的是**差异**（哪些 label 进来了、哪些不见了）而不是整份配置：
    整份配置在文件里就有，而"这次改了什么"只有这一刻知道。
    """
    store = _store(request)
    previous = await asyncio.to_thread(store.current)
    before = {entry.label for entry in previous.config.entries} if previous.config else set()
    after = {entry.label for entry in payload.entries}
    added, added_truncated = _capped_labels(sorted(after - before))
    removed, removed_truncated = _capped_labels(sorted(before - after))
    detail: dict[str, AuditDetailValue] = {
        "operation": _OP_REPLACE,
        "mode": payload.mode.value,
        "entry_count": len(payload.entries),
        "added": added,
        "removed": removed,
        "labels_truncated": added_truncated or removed_truncated,
    }
    return await _write_and_audit(request, config=payload, detail=detail)


@router.post("/entries", response_model=AllowlistStateResponse)
async def add_entry(request: Request, payload: AllowlistEntry) -> AllowlistStateResponse:
    """加一条。label 重复 → 422 `invalid_request`。

    重复判定不在这里手写，而是交给 `AllowlistConfig` 的 `_labels_are_unique` ——
    构造一份带新条目的配置，校验失败就是重复。同一条不变量只有一个执行点，
    "接口漏判但文件里能出现重名"这种分歧因此不可能发生。
    """
    store = _store(request)
    snapshot = await asyncio.to_thread(store.current)
    base = _base_config(snapshot)
    merged = _rebuilt(mode=base.mode, entries=(*base.entries, payload))
    detail: dict[str, AuditDetailValue] = {
        "operation": _OP_ADD,
        "mode": merged.mode.value,
        "entry_count": len(merged.entries),
        "label": payload.label,
        "host_count": len(payload.hosts),
        "cidr_count": len(payload.cidrs),
        "expires": None if payload.expires is None else payload.expires.isoformat(),
    }
    return await _write_and_audit(request, config=merged, detail=detail)


@router.delete("/entries/{label}", response_model=AllowlistStateResponse)
async def delete_entry(request: Request, label: str) -> AllowlistStateResponse:
    """删一条。找不到 → 404 `not_found`。

    **不是幂等的 200。** "删一条不存在的授权"意味着调用方看到的清单和真实的不一样
    （很可能有人刚刚手工改了文件），而那正是操作者需要知道的事 —— 静默返回成功会让
    界面继续显示一份错误的清单。
    """
    store = _store(request)
    snapshot = await asyncio.to_thread(store.current)
    base = _base_config(snapshot)
    remaining = tuple(entry for entry in base.entries if entry.label != label)
    if len(remaining) == len(base.entries):
        raise NotFoundError(path=request.url.path)
    trimmed = _rebuilt(mode=base.mode, entries=remaining)
    detail: dict[str, AuditDetailValue] = {
        "operation": _OP_REMOVE,
        "mode": trimmed.mode.value,
        "entry_count": len(trimmed.entries),
        "label": label,
    }
    return await _write_and_audit(request, config=trimmed, detail=detail)
