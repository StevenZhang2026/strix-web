"""事件回放的读取端：把 `scan_events` 的行变回当初推出去的那些帧。

只读我们自己那份只追加的镜像，**不读** `scans.current_epoch` —— 那一列由另一个模块写，
一旦两边漂移，回放就会声称一个 epoch 而交出另一个 epoch 的帧，客户端永远等不到它以为
自己在等的那一批。"现在是第几代"只信这张表里同一个 scan_id 的 `MAX(epoch)`。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from functools import partial
from typing import Final

from app.db import Database
from app.ws_envelope import PROTOCOL_VERSION, Envelope

DEFAULT_LIMIT: Final = 500
"""一次回放取多少行。"""


@dataclass(frozen=True, slots=True)
class ReplayCursor:
    """客户端 `hello` 里的 `resume_from`：它最后一帧的 `(epoch, seq)`。"""

    epoch: int
    seq: int


@dataclass(frozen=True, slots=True)
class ReplayBatch:
    frames: tuple[Envelope, ...]
    epoch: int
    next_cursor: ReplayCursor | None
    """非 None = 还有更多，拿它再调一次。"""


async def replay_batch(
    db: Database,
    scan_id: str,
    *,
    resume_from: ReplayCursor | None,
    ts: str,
    limit: int = DEFAULT_LIMIT,
) -> ReplayBatch:
    """补齐客户端漏掉的帧。`ts` 是"这一帧什么时候发出去的"，由调用方传 `now_ts()`。

    `MAX(epoch)` 与取行必须在**同一次** `db.run` 里：`db.run` 一次一个事务，分成两次
    中间可能又写进来一批行，于是返回的 `epoch` 与帧对不上。

    未知 scan_id 不抛异常 —— "这个扫描存不存在"由路由层查 `scans` 表回答。
    """
    epoch, rows = await db.run(
        partial(_read, scan_id=scan_id, resume_from=resume_from, limit=limit)
    )
    frames = tuple(_frame(row, epoch=epoch, ts=ts) for row in rows)
    # 取满了就给游标，不预读一行去判断"真的还有没有" —— 代价只是最后可能多空跑一趟。
    last = rows[-1] if len(rows) == limit else None
    next_cursor = ReplayCursor(epoch=epoch, seq=last["seq"]) if last is not None else None
    return ReplayBatch(frames=frames, epoch=epoch, next_cursor=next_cursor)


def _read(
    conn: sqlite3.Connection,
    *,
    scan_id: str,
    resume_from: ReplayCursor | None,
    limit: int,
) -> tuple[int, list[sqlite3.Row]]:
    """`version` 与 `fingerprint` 两列刻意不取：前者只存在过一个值（`PAYLOAD_VERSION`），
    后者取自上游原始 payload 而帧里的 `data` 已被镜像改写过，进帧会让人以为能拿它校验帧。
    """
    max_epoch: int | None = conn.execute(
        "SELECT MAX(epoch) FROM scan_events WHERE scan_id = ?", (scan_id,)
    ).fetchone()[0]
    if max_epoch is None:
        return 0, []
    # 游标只决定从哪一帧开始。epoch 不匹配（更旧或更新）意味着客户端手上的状态已作废，
    # 它要的是新一代的完整序列，所以 `seq` 不设下界。
    if resume_from is not None and resume_from.epoch == max_epoch:
        rows = conn.execute(
            "SELECT seq, kind, agent_id, ts, data_json FROM scan_events"
            " WHERE scan_id = ? AND epoch = ? AND seq > ? ORDER BY seq LIMIT ?",
            (scan_id, max_epoch, resume_from.seq, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT seq, kind, agent_id, ts, data_json FROM scan_events"
            " WHERE scan_id = ? AND epoch = ? ORDER BY seq LIMIT ?",
            (scan_id, max_epoch, limit),
        ).fetchall()
    return max_epoch, rows


def _frame(row: sqlite3.Row, *, epoch: int, ts: str) -> Envelope:
    """一行 → 一帧。`type` 恒为 `event.add`：前端对 add 与 update 都是按 `payload.key`
    upsert，所以表里不记"当初是 add 还是 update"也不丢信息。

    `data_json` 缺三键之一让 `KeyError` 冒泡：那些行是我们自己写的，缺键是编程错误。
    """
    stored: dict[str, object] = json.loads(row["data_json"])
    return Envelope(
        v=PROTOCOL_VERSION,
        epoch=epoch,
        seq=row["seq"],
        type="event.add",
        ts=ts,
        # 这六个键必须与 `scan_frames.event_payload()` 逐字段一致（回放出来的帧就是当初
        # 直播推出去的那一帧）。刻意不为了复用它去伪造一个 `ProjectedEvent`：那个
        # dataclass 多两个字段，其中 `fingerprint` 我们根本没打算进帧。
        payload={
            "key": stored["key"],
            "kind": row["kind"],
            "agent_id": row["agent_id"],
            "ts": row["ts"],
            "upstream_version": stored["upstream_version"],
            "data": stored["data"],
        },
    )
