"""事件镜像：把一帧投影事件只追加地写进 `scan_events`，内联截图落盘到 `media/<sha256>.png`。

顺序是「先文件、后行」，不是随手写的：**「文件写成功但事务失败 → 磁盘上留一个没人记账的
`<sha>.png`」是接受的不对称** —— 内容地址化的孤儿文件无害，留存清理删整个目录时会带走它。
反过来（先记账后落盘）会留下**指向不存在文件的行**，那是真 bug。

**刻意不做按 `fingerprint` 去重。** 镜像就是「客户端在 `(epoch, seq)` 那一刻看到的那一帧」：
重同步时上层会在 epoch+1 下把整份快照重推一遍，镜像也就照样重写一遍。否则 `replay(epoch=E)`
拿不出完整快照，`resume_from` 也就失去意义。`ix_scan_events_fingerprint` 留给回放与排障。

`seq` 由调用方给（它持有 `ws_envelope.Sequencer`）：WS 帧与镜像行必须是同一个 seq，
否则 `resume_from {epoch, seq}` 回放对不上。本模块不发号、也不持有 epoch。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from app.services.audit import iso_utc
from app.services.event_media import ExtractedImage, extract_media
from app.services.retention import scan_dir_for
from app.services.run_projector import ProjectedEvent, canonical_json

if TYPE_CHECKING:
    import sqlite3

    from app.db import Database

# `scan_events.version` 的值：`data_json` 那个三键信封的结构版本（不是 strix 的 `upstream_version`）。
PAYLOAD_VERSION = 1


@dataclass(frozen=True, slots=True)
class MirroredEvent:
    """镜像一帧的结果。调用方拿 `event` 去发 WS 帧 —— 它的 `data` 已把内联 PNG 换成 media URL。"""

    seq: int
    event: ProjectedEvent
    new_media: tuple[str, ...]
    """本次**新记账**的 sha256（`scan_media` 里早就有行的不列）。"""
    skipped: tuple[str, ...]
    """原样透传 `extract_media` 的机器码。"""


def strix_id_of(key: str) -> int | None:
    """取事件 key 的整数后缀（`"tool_12"` → `12`）；解析不出返回 `None`。纯函数。

    存在的理由是 `scan_events` 是 STRICT 表、`strix_id` 只收 int：投影层的 id 是字符串。
    它只是排障线索 —— 不当主键、不参与去重。
    """
    try:
        return int(key.rsplit("_", 1)[-1])
    except ValueError:
        return None


def media_rel_path(scans_dir: Path, scan_id: str, sha256: str) -> str:
    """`scan_media.rel_path` 的值：相对 `${DATA}` 的路径（`scans_dir` 就是 `${DATA}/scans`）。

    刻意拼字符串而不是 `resolved.relative_to(...)`：`scan_dir_for` 会 `resolve()`，
    而 macOS 上 `/var` 是 `/private/var` 的符号链接 → `relative_to` 直接 `ValueError`。
    """
    return f"{scans_dir.name}/{scan_id}/media/{sha256}.png"


def _write_images(media_dir: Path, images: tuple[ExtractedImage, ...]) -> None:
    """把图落到 `<media_dir>/<sha256>.png`。同步阻塞 IO，由调用方丢进工作线程。"""
    media_dir.mkdir(parents=True, exist_ok=True)
    for image in images:
        target = media_dir / f"{image.sha256}.png"
        if target.exists():
            # 内容地址化：同名即同内容，重写没有意义。
            continue
        # 先写临时文件再原子 rename：半截文件会让 I1（行 ⇒ 文件字节数等于 `bytes` 列）失效，
        # 而下一次 append 看到它「存在」就会跳过重写，错误会一直留着。
        staging = media_dir / f"{image.sha256}.part"
        staging.write_bytes(image.payload)
        staging.replace(target)


class EventMirror:
    """一次扫描的事件镜像器。真正持有状态（库、目录、scan_id），所以是 class。

    构造**不碰磁盘也不碰 DB**（照 `RetentionSweeper` 的先例）。
    """

    def __init__(self, db: Database, scans_dir: Path, scan_id: str) -> None:
        self._db = db
        self._scans_dir = scans_dir
        self._scan_id = scan_id
        # 早失败好过晚失败：非法 scan_id 在构造期就让 `InvalidScanIdError` 抛出去，
        # 不等到第一帧才发现。路径逃逸检查本仓已有，不再写第二遍。
        self._media_dir = scan_dir_for(scans_dir, scan_id) / "media"

    async def append(self, *, epoch: int, seq: int, event: ProjectedEvent) -> MirroredEvent:
        """镜像一帧：落盘 → 一个事务里写 `scan_events` 一行 + 每张新图一条 `scan_media`。

        `OSError` 与 `(scan_id, epoch, seq)` 撞了的 `sqlite3.IntegrityError` 都往上抛：
        同一 epoch 里 seq 重复是调用方的编程错误，PK 就是用来让它响的。
        """
        extraction = extract_media(event.data, self._scan_id)
        # `fingerprint` 原样带过来：它是对**上游原始** payload 取的，重同步时要和内存里的
        # `ProjectionState.fingerprints` 比。换成改写后的值，两边永远对不上。
        mirrored = replace(event, data=extraction.data)
        if extraction.images:
            await asyncio.to_thread(_write_images, self._media_dir, extraction.images)
        new_media = await self._db.run(
            partial(
                self._insert_rows,
                epoch=epoch,
                seq=seq,
                event=mirrored,
                images=extraction.images,
            )
        )
        return MirroredEvent(
            seq=seq, event=mirrored, new_media=new_media, skipped=extraction.skipped
        )

    def _insert_rows(
        self,
        conn: sqlite3.Connection,
        *,
        epoch: int,
        seq: int,
        event: ProjectedEvent,
        images: tuple[ExtractedImage, ...],
    ) -> tuple[str, ...]:
        """同步的事务体（`Database.run` 自带 BEGIN IMMEDIATE / COMMIT / 回滚）。"""
        conn.execute(
            "INSERT INTO scan_events (scan_id, epoch, seq, strix_id, kind, agent_id, ts, version,"
            " fingerprint, data_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._scan_id,
                epoch,
                seq,
                strix_id_of(event.key),
                event.kind,
                event.agent_id,
                event.ts,
                PAYLOAD_VERSION,
                event.fingerprint,
                # 套一层信封而不是直接存 payload：`upstream_version` 混进 payload 会和工具
                # 自己的键撞名；而 `key`（`"tool_12"`）回放时要用，又复原不出来 ——
                # `kind` 存的是上游的 `type` 字段，不保证等于 key 的前缀。
                canonical_json(
                    {
                        "key": event.key,
                        "upstream_version": event.upstream_version,
                        "data": event.data,
                    }
                ),
            ),
        )
        seen_at = iso_utc(datetime.now(UTC))
        new_media: list[str] = []
        for image in images:
            # PK 是 (scan_id, sha256)：撞了就什么都不做，绝不覆盖首见的 agent 与时间。
            # `rowcount` 因此正好区分「本次新插的」与「早就有的」。
            cursor = conn.execute(
                "INSERT INTO scan_media (scan_id, sha256, mime, bytes, rel_path, first_agent_id,"
                " first_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (
                    self._scan_id,
                    image.sha256,
                    image.mime,
                    len(image.payload),
                    media_rel_path(self._scans_dir, self._scan_id, image.sha256),
                    event.agent_id,
                    seen_at,
                ),
            )
            if cursor.rowcount:
                new_media.append(image.sha256)
        return tuple(new_media)
