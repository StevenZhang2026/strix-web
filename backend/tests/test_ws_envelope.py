"""WS 帧信封（T11b：`app/ws_envelope.py`）—— 全部 `/ws/*` 路由共用的外层形状。

这里只测那条**跨任务的约定**：epoch 变了 `seq` 归零。信封本身的字段集合在
`test_routes_system_pull.py` 里对着真发出去的一帧断言过（§十.4：同一条不变式不在两层
各测一遍），而发号器是纯逻辑、又是 T14／T16 要继承的东西，值得自己一条。

T11b 收货 mutation：让 `Sequencer.next` 换 epoch 也只是 `+1` —— 补这条之前一条测试都不红。
"""

from __future__ import annotations

from app.ws_envelope import PROTOCOL_VERSION, Sequencer


def test_protocol_version_is_pinned() -> None:
    """改这个数字就是改协议：前端按 `v` 决定怎么解析一帧，所以它只能是一次刻意的改动。

    `test_scan_frames.py` 只断言了"信封里的 `v` 取自这个常量"，钉不住"这个常量是几"。
    """
    assert PROTOCOL_VERSION == 1


def test_seq_restarts_at_zero_when_the_epoch_changes() -> None:
    """epoch 变了 = 客户端手上的状态作废，编号也必须跟着重来。

    不归零的后果不是"号不好看"：前端靠 `seq` 连续判"我漏帧了吗"，而新 epoch 的第一帧
    带着一个接着上一代的号，看起来就是"没漏帧"，于是那次重画不会发生。
    """
    seq = Sequencer(epoch=-1)  # -1 = 还没发过任何帧，所以第一帧是 0
    assert [seq.next(0), seq.next(0), seq.next(0)] == [0, 1, 2]
    assert seq.next(1) == 0
    assert seq.next(1) == 1
