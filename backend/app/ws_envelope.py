"""WebSocket 帧信封 —— 全部 `/ws/*` 路由共用的唯一外层形状。

`{v, epoch, seq, type, ts, payload}`（`PLAN.md` §实时流设计钉死）。三个字段值得解释：

- **`epoch`**：第几代数据。变了就是"客户端手上的进度作废，请按新快照重画"。
  T11b 用它记第几次拉取，T13/T14 用它记 `agents.db` 的第几个 epoch（那张表不是只追加
  的，压缩会 `clear_session()` 后重插、id 重排 —— 朴素游标必错）。**同一个字段名承载
  同一个语义**，前端只需要一条规则："epoch 变了就丢掉本地状态"。
- **`seq`**：同一个 epoch 内单调递增。前端靠它判"我漏帧了吗"。
- **`type`**：`pull.progress` / `pull.done` 这种点分名字。**不做注册表** —— 一个字符串
  常量表换来的是"新增一个 type 要改两处"，而它拦不住任何错误（前端仍然按字面匹配）。

本模块刻意只有一个模型 + 一个发号器，**没有插件点、没有为 T14 预留的抽象**。
T14 `import` 它，那就是它的全部价值（CLAUDE.md §编码哲学 3：不要过早抽象）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.models import BoundaryModel
from app.services.audit import iso_utc

PROTOCOL_VERSION = 1
"""信封版本。改这个数字是**破坏性**变更（前端要按它分支），加字段不用改。"""


class Envelope(BoundaryModel):
    """一帧。`payload` 是 `dict` 而不是联合类型：每个 `type` 有自己的正文形状，
    在这里穷举它们会让本模块认识每一个业务模块（T11b 的进度、T14 的工具调用、
    T16 的报告片段），也就是把依赖方向倒过来。形状由发送方的服务模块负责。
    """

    v: int = PROTOCOL_VERSION
    epoch: int
    seq: int
    type: str
    ts: str
    payload: dict[str, object]


def now_ts() -> str:
    """帧时间戳。

    复用 `services/audit.iso_utc`（跨模块）而不是再写一个 `strftime`：那个函数的
    docstring 明写"在这里而不是各调用方各写一遍"，理由是 `Z` 后缀与毫秒位数必须
    全项目一致 —— 审计行与 WS 帧会被同一个人放在一起对时间。
    """
    return iso_utc(datetime.now(UTC))


@dataclass
class Sequencer:
    """给定 epoch 内的发号器。epoch 变了就归零。

    非 frozen：它**就是**那一点可变状态。挂在持有它的服务实例上，不许放模块级
    （CLAUDE.md §Python：模块级不得有可变全局状态）。
    """

    epoch: int = 0
    seq: int = 0

    def next(self, epoch: int) -> int:
        if epoch != self.epoch:
            self.epoch = epoch
            self.seq = 0
        else:
            self.seq += 1
        return self.seq
