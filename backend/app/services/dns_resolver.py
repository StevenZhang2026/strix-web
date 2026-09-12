"""把主机名解析成 A + AAAA 地址串。`target_guard` 零 IO，这一层就是它缺的那只手。

# 为什么这是一个独立模块，而不是写在 `routes/targets.py` 里

两个消费者：T8 的 `POST /api/targets/validate`（向导第 1 步的预览）与 T12 的
「启动前重新解析、与声明时比对，不一致就 `409 dns_changed`」。第二个消费者要的是**同一份
解析行为** —— 两处各写一遍 `getaddrinfo`，参数只要有一处不同（`AF_UNSPEC` 写成
`AF_INET`、少了 `SOCK_STREAM` 去重）就会让"声明时的地址"和"启动时的地址"必然对不上，
于是那条 rebinding 防线变成一个随机报错的功能。让 T12 从路由模块 import 更糟。

# 解析失败是**结果**，不是异常

`resolve()` 不抛异常，返回 `Resolution(addresses=(), error=...)`。理由：
`POST /api/targets/validate` 永远返回 200，每个目标带自己的结论（见那个路由的
docstring）。用户在向导里打了半个域名，或者填了一个已经下线的内网名字 —— 那是他需要
**看见**的一行提示，不是一个 500，也不该让同一批里其它目标的结论一起消失。

# 已知限制：`getaddrinfo` 没有超时参数

被黑洞掉的 DNS 服务器会让这个调用阻塞到系统解析器自己放弃（`/etc/resolv.conf` 的
`timeout` × `attempts`，容器里通常是 5s × 2）。修它需要一个自己发 UDP 包的 DNS 库 ——
那是一个新依赖，而本项目的规矩是"每个新依赖必须论证"（CLAUDE.md §编码哲学 4），
为一个向导预览接口引入 DNS 协议栈论证不过去。控制住的办法是**限制扇出**：
`ValidateTargetsRequest.raw` 上有条数上限，一次请求最多占用那么多线程池线程。
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum


class ResolutionError(StrEnum):
    """解析失败的机器码。

    **刻意不是 `app/errors.py` 里的码**：请求本身没失败（200），这是输入框下方的一行
    提示，和 `target_guard.RejectionReason` 同级。前端在 `targetGuard.resolution.*`
    下查文案。

    只有三个值，且刻意粗：`EAI_*` 有十几个，但它们对用户的意义只有三种 ——
    「这个名字不存在」「暂时解析不出来，等一下再试」「解析器本身出问题了」。
    把 `EAI_SOCKTYPE` 单独编一个码只会多一条永远不会有人读的文案。
    """

    NOT_FOUND = "dns_not_found"
    TIMEOUT = "dns_timeout"
    FAILED = "dns_failed"


RESOLUTION_ERROR_CODES: frozenset[str] = frozenset(item.value for item in ResolutionError)
"""给 `tests/test_message_coverage.py` 做双向比对用的取值域。不可变，不是全局状态。"""

# `EAI_NODATA` 在 macOS 上不存在（已被废弃），在 glibc 上存在且含义是"名字有，但没有
# A/AAAA 记录"。用 `getattr` 取而不是直接写 `socket.EAI_NODATA`：后者会让这个模块在
# 某些平台上 import 就炸。本项目的后端只跑在 python:3.12-slim（glibc）里，但单测有可能
# 在宿主上裸跑，而"import 一个模块就失败"是最难排查的失败形状。
_NOT_FOUND_ERRNOS: frozenset[int] = frozenset(
    code
    for code in (getattr(socket, name, None) for name in ("EAI_NONAME", "EAI_NODATA"))
    if isinstance(code, int)
)


@dataclass(frozen=True, slots=True)
class Resolution:
    """一次解析的结果。`error` 非空时 `addresses` 一定是空的，反之亦然。"""

    addresses: tuple[str, ...]
    error: ResolutionError | None


Resolver = Callable[[str], Awaitable[Resolution]]
"""路由层注入点的类型。

单测禁止碰真实网络（CLAUDE.md §测试），所以"跟 DNS 说话"这一层必须可替身。替身挂在
`app.state.dns_resolver` 上 —— 与 `docker_transport` 完全同构，不引入第二种注入机制
（见 `routes/system.py::_probe` 的注释）。
"""


def resolve_sync(host: str) -> Resolution:
    """**同步阻塞。** 调用方负责 `asyncio.to_thread`（下面的 `resolve()` 就是它）。

    `AF_UNSPEC` + `SOCK_STREAM`：前者要 A 和 AAAA 两族（IPv6-only 的内网目标不能漏），
    后者只是去重 —— 不加它同一个地址会按 stream/dgram/raw 返回三遍。

    返回顺序按 `getaddrinfo` 给的顺序（受 RFC 6724 地址选择影响），**去重但不排序**：
    这份顺序会进审计与 `authorizations.resolved_ips_json`，排序会让"这次解析结果和上次
    一样吗"这个比对丢掉一个真实的变化信号。
    """
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        if exc.errno in _NOT_FOUND_ERRNOS:
            return Resolution(addresses=(), error=ResolutionError.NOT_FOUND)
        if exc.errno == socket.EAI_AGAIN:
            # "暂时失败" —— 递归解析器没在规定时间内给出答案。这是超时的实际形状：
            # getaddrinfo 自己不抛 socket.timeout。
            return Resolution(addresses=(), error=ResolutionError.TIMEOUT)
        return Resolution(addresses=(), error=ResolutionError.FAILED)
    except UnicodeError:
        # IDNA 编码失败（标签超过 63 字节等）。到这一步 host 已经过
        # `normalize_target` 的 IDNA 处理，所以正常不会发生；真发生了它是"这个名字不
        # 能被解析"，归到 NOT_FOUND 比 500 诚实。
        return Resolution(addresses=(), error=ResolutionError.NOT_FOUND)

    seen: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in seen:
            seen.append(address)
    if not seen:
        # 正常的 getaddrinfo 要么抛要么给至少一条。空列表只可能来自替身实现，
        # 而"解析成功但没有地址"会让 evaluate_target 抛 ValueError → 500。
        # 在这里收口成一个可显示的状态。
        return Resolution(addresses=(), error=ResolutionError.NOT_FOUND)
    return Resolution(addresses=tuple(seen), error=None)


async def resolve(host: str) -> Resolution:
    """`resolve_sync` 的 async 包装。生产路径用它，单测用替身。"""
    return await asyncio.to_thread(resolve_sync, host)
