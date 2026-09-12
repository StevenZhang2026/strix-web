"""进程内存 LLM 凭据保管库。

# 这个模块的全部存在理由：Key 绝不落盘

它**不写文件、不写 DB、不写审计、不 log 任何值**。`__init__` 连一个路径参数都不收 ——
所以"哪天加个落盘缓存"不是一个疏忽能造成的事，得先改签名。凭据只经
`POST /api/keys` 的 JSON body 进来，出去只有两条路：注入子进程的 env（T9）、
以及 `secret_values()` 喂给日志脱敏（`logging_setup.Redactor`）。

代价是诚实的：`api` 重启后全部 handle 失效，前端拿到 `409 key_required` 重新索要。
那不是缺陷，是"绝不落盘"的直接后果（`PLAN.md` §已确认决策）。

# 一个 handle 指向一**组**凭据，不是一个字符串（§N1）

Bedrock 打破了"一个供应商 = 一个 Key"这个假设，而且打破两次（SigV4 三个值、
bearer 两个值）。所以 `secrets{}` 是 env 名 → 值的映射，非机密的参数（区域）走
`params{}` 分开放 —— 后者不进 `secret_values()`，否则 `us-east-1` 这种到处都出现的
字符串会把日志替换成一片 `[REDACTED]`。

**本模块不判断形状合不合法**（"这个 auth_shape 该有哪几个键"是 T7b 的
`unexpected_secret_key`）。它只按传进来的数据存取。

# 为什么这里有后台任务，而 `auth.SessionStore` 只做懒判定

`SessionStore` 论证过"只为清理 ≤16 条内存记录而起一个定时任务不值得"。vault 不同：
会话过期了留在内存里只是一条无用记录，而**凭据留在内存里就是攻击面本身** ——
"凭据在这个进程里待了多久"就是这个模块的安全属性。没人再来取用的凭据不会被懒判定
碰到，所以必须有人主动去清。懒判定仍是主防线（取用即判），sweeper 只兜这一种。

# 生命周期判定表（`ref_count` 只影响 idle，永不影响 hard）

| 情形 | 行为 | 理由 |
|---|---|---|
| `ref_count > 0`，idle 到 | 不过期 | 一次扫描可能几小时不碰 vault（acquire 一次、结束 release）。让 idle 清掉它 = 扫描还在跑就要求重新输 Key |
| `ref_count > 0`，hard 到 | 照删 | hard 的意义就是"不管多忙最多 24h"。给 ref_count 开豁免 → 一个忘了 release 的任务能让凭据永驻 |
| 显式 `drop`，`ref_count > 0` | 照删 | `DELETE /api/keys/{h}` 是用户按下的"现在忘掉我的 Key"，是安全动作；"有人在用所以不删"把它降级成建议 |

后两条都不打断正在跑的扫描：子进程早已从 env 里拿到了凭据，删 handle 只影响
"续跑"与"报告翻译"这两件还没发生的事，它们会拿到 `409 key_required`。
"""

from __future__ import annotations

import asyncio
import logging
import secrets as secrets_module
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from pydantic import SecretStr

logger = logging.getLogger(__name__)

# 返回单调递增的秒数。注入式（照 `auth.Clock`）：默认 `time.monotonic` 而不是
# `time.time`，因为过期不该受系统时钟跳变影响（NTP 校准、容器暂停恢复都会让
# 墙上时钟往回走，而往回走的时钟会让"8 小时后过期"变成"永不过期"）。
Clock = Callable[[], float]

# 取用后多久没人再碰就清掉。`ref_count == 0` 时才计时，见模块 docstring 的判定表。
IDLE_TTL_SECONDS = 8 * 60 * 60
# 从存入那一刻起的绝对上限。**任何取用都不续它**，`ref_count > 0` 也不豁免。
HARD_TTL_SECONDS = 24 * 60 * 60
# sweeper 的周期。60 秒相对于 8 小时的 TTL 是个无关紧要的精度，但它决定了
# "凭据最迟多久从内存里消失" —— 那才是这个数字要小的理由。
SWEEP_INTERVAL_SECONDS = 60.0

# handle 的随机字节数。32 字节 = 256 bit，与 `auth.SESSION_ID_BYTES` 同量级：
# handle 是"谁能用这组凭据发起扫描"的唯一凭证，猜中它等于借用别人的 Key。
HANDLE_BYTES = 32

# ---- 脱敏标签 ----------------------------------------------------------------
# 标签是给人看的"我填的是哪个 Key"，不是给机器校验的。所以只露首尾各 4 个字符。
LABEL_HEAD = 4
LABEL_TAIL = 4
LABEL_ELLIPSIS = "…"
# 短于此一律整体打码。理由与 `logging_setup.MIN_SECRET_LENGTH` 同源：用户可能在
# 凭据框里填了占位符（`test`、`abc`），那种值露出 8 个字符就等于露出全部。
LABEL_MIN_LENGTH = 16


@dataclass(frozen=True, slots=True)
class CredentialSet:
    """一组凭据 + 它的非机密元数据。

    `frozen=True` 且 `store()` 会把两个映射包成只读副本，所以从 `get()` 拿到的东西
    是真快照 —— 调用方既改不动自己那份原始 dict 的影响，也改不动 vault 里的内容。

    四个元数据字段（provider / auth_shape / strix_llm / api_base）留在这里是因为
    `POST /api/scans` 的 body 只送 `vault_handle`（`PLAN.md:575`），而 `scans` 表有
    这四列且 `auth_shape NOT NULL`（`PLAN.md:536`）—— vault 是它们的唯一来源。
    本模块**照存不判断**。
    """

    provider: str
    auth_shape: str
    strix_llm: str
    api_base: str | None
    # 键就是要注入子进程的 env 变量名（`LLM_API_KEY` / `AWS_ACCESS_KEY_ID` …）。
    secrets: Mapping[str, SecretStr]
    # 非机密参数（区域等）。**不进 `secret_values()`**，见模块 docstring。
    params: Mapping[str, str]


@dataclass(slots=True)
class _Entry:
    """vault 内部的一条记录。私有且可变 —— 它从不被交出去。

    与 `auth.Session` 的 frozen + replace 写法不同：`ref_count` 会被频繁增减，
    每次都造一个新对象只是为了满足一条与本类无关的纪律。真正需要不可变的是**交出去
    的那个快照**，而那正是 `CredentialSet` 在做的事。
    """

    credentials: CredentialSet
    created_at: float
    last_used_at: float
    ref_count: int


def secret_label(value: str) -> str:
    """`'AKIAIOSFODNN7EXAMPLE'` → `'AKIA…MPLE'`。纯函数。

    放在本模块而不是 `logging_setup`：它服务的是 UI（`POST /api/keys` 的 201 响应里
    每个值一条标签，`PLAN.md:556`），与日志脱敏是两件事 —— 后者要的是"替换掉"，
    这里要的是"留一点让人认得出"。
    """
    if len(value) < LABEL_MIN_LENGTH:
        return LABEL_ELLIPSIS
    return f"{value[:LABEL_HEAD]}{LABEL_ELLIPSIS}{value[-LABEL_TAIL:]}"


def labels_for(credentials: CredentialSet) -> dict[str, str]:
    """每个 secret 一条标签。**一组值给一组标签**，不是一组值一条。

    做成函数而不是让路由层自己 `secret_label(v.get_secret_value())`：那样明文接触点
    会从这个模块扩散到路由层去。`params` 不参与 —— 它们本来就不是机密，UI 直接显示。
    """
    return {
        name: secret_label(value.get_secret_value()) for name, value in credentials.secrets.items()
    }


def assert_single_worker(environ: Mapping[str, str]) -> None:
    """多 worker 就拒绝启动。

    为什么这条约束属于本模块：vault 是**进程内 dict**。多 worker 时
    `POST /api/keys` 落在 worker A 的内存里，紧接着的 `POST /api/scans` 可能被路由到
    worker B —— 那里查不到这个 handle，于是用户看到一个随机出现、刷新一下又好了的
    `key_required`。那种 bug 极难从现象反推到原因，所以要在启动时就断死。

    取 `environ` 参数而不是读 `os.environ`：纯函数，好测（照 `settings.py` 的
    `assert_no_credential_env`）。

    **这个断言检得出什么、检不出什么，说清楚，否则下一个人会认定它没用而删掉它：**
      - 真正的防线是 `backend/Dockerfile:160-162` 的 `CMD [... "--workers", "1" ...]`
        （`docker-compose.yml` 的 `api` 段**没有** `command:`，所以镜像的 CMD 就是
        实际 argv）。而 uvicorn 的 argv `--workers` **覆盖** `WEB_CONCURRENCY`。
      - 所以本断言的价值**不是**"检出多 worker"（argv 那条路它看不见），而是兜住
        另一条真实存在的路径：**有人在 compose 里加了 `command:` 覆盖掉镜像 CMD，
        于是 `WEB_CONCURRENCY` 重新生效**。那时环境里的这个变量说话算数，而它是
        我们唯一能在进程内观测到的东西 —— 进程内没有可移植的办法查真实 worker 数。
    """
    raw = environ.get("WEB_CONCURRENCY")
    # 缺失或空 = 没人配过它，uvicorn 默认单 worker。
    if not raw:
        return
    if raw == "1":
        return
    # 非整数（拼错的值）同样拒绝：有人在配它，而我们无法确认结果是 1。
    raise RuntimeError(
        f"WEB_CONCURRENCY={raw!r} 不被支持：api 必须 --workers 1。"
        "内存 KeyVault 是进程内 dict，多 worker 会让 vault_handle 随机 404。"
        "请移除该变量（镜像 CMD 已写死 --workers 1）。"
    )


class KeyVault:
    """handle → 一组凭据。进程内存，只此一份，挂在 `app.state.key_vault` 上。

    状态挂在实例上、由 lifespan 显式持有，**不是模块级 dict**（CLAUDE.md §Python）。
    这条规则在本模块尤其不能破：Strix 本体的模块级全局可变状态就是"同进程并发扫描会
    跨用户污染 API Key"那个 bug 的成因，也正是本项目走 subprocess 的原因。
    """

    def __init__(self, clock: Clock = time.monotonic) -> None:
        self._clock = clock
        self._entries: dict[str, _Entry] = {}

    def store(self, credentials: CredentialSet) -> str:
        """存入一组凭据，返回不可猜的 handle。

        两个映射各拷一份并包成 `MappingProxyType`。拷贝是必须的（否则调用方之后改自己
        那个 dict 会穿透进 vault，而"凭据被谁改过"是一个没人会去查的问题）；额外包只读
        代理是因为 `get()` 会把同一个 `CredentialSet` 交给多个调用方 —— 其中任何一个
        改了映射，另一个就拿到了被篡改的凭据。两层都便宜，都留着。
        """
        now = self._clock()
        self._prune(now)
        handle = secrets_module.token_urlsafe(HANDLE_BYTES)
        frozen = CredentialSet(
            provider=credentials.provider,
            auth_shape=credentials.auth_shape,
            strix_llm=credentials.strix_llm,
            api_base=credentials.api_base,
            secrets=MappingProxyType(dict(credentials.secrets)),
            params=MappingProxyType(dict(credentials.params)),
        )
        self._entries[handle] = _Entry(
            credentials=frozen, created_at=now, last_used_at=now, ref_count=0
        )
        return handle

    def get(self, handle: str) -> CredentialSet | None:
        """取用并刷新 idle 期限。不存在 / 已过期一律返回 None。

        **不抛异常**：`409 key_required` 的翻译是路由层（T7b）的事。这里返回 None 是
        因为"handle 失效"对本模块不是错误，是一个正常的、预期会大量发生的状态。
        """
        entry = self._resolve(handle)
        if entry is None:
            return None
        entry.last_used_at = self._clock()
        return entry.credentials

    def acquire(self, handle: str) -> CredentialSet | None:
        """取用并登记一个引用（扫描启动、报告翻译开始时调）。

        `ref_count > 0` 期间 idle TTL 不生效 —— 见模块 docstring 的判定表。
        必须与 `release()` 配对，配对靠调用方的 `finally`。忘了 release 的后果被
        hard TTL 兜住（最多多留到 24h），所以这里不做超时自动回收。
        """
        entry = self._resolve(handle)
        if entry is None:
            return None
        entry.last_used_at = self._clock()
        entry.ref_count += 1
        return entry.credentials

    def release(self, handle: str) -> None:
        """归还一个引用。handle 已被删掉也不报错 —— release 必须幂等。

        同时刷新 `last_used_at`：这样 idle 期限是从"最后一次放手"起算的，而不是从
        `acquire` 那一刻。一个跑了 6 小时的扫描结束后，凭据还能再留 8 小时给"看报告、
        点翻译"用，而不是立刻就差 2 小时过期。

        `max(0, ...)` 不只是防御性写法：ref_count 若变成 -1，之后一次正常的 acquire
        只把它抬到 0，于是那次扫描跑着的时候 idle TTL 仍然生效，凭据会在它眼皮底下
        消失。多余的 release 必须是无害的。
        """
        entry = self._entries.get(handle)
        if entry is None:
            return
        entry.ref_count = max(0, entry.ref_count - 1)
        entry.last_used_at = self._clock()

    def drop(self, handle: str) -> bool:
        """立即清除。返回是否真的删掉了一条（幂等，第二次返回 False）。

        **不看 `ref_count`** —— 理由见模块 docstring 的判定表第三行。
        """
        return self._entries.pop(handle, None) is not None

    def secret_values(self) -> frozenset[str]:
        """当前全部凭据的每一个明文值。直接作为 `logging_setup.SecretProvider`。

        刻意**不过滤已过期但还没被清掉的条目**：多脱敏一会儿是无害的，而漏脱敏是
        泄漏。sweeper 会在 60 秒内把它们清走，集合自然收缩。

        **先 `tuple()` 取一份快照再遍历，这一层不是防御性写法。** 本方法是本类唯一
        会在**别的线程**里被调用的入口：`Redactor.redact()` 每格式化一条日志就调它一次
        （`logging_setup.py:143`），而日志会在 `asyncio.to_thread` 的工作线程里打出来
        （例如 `allowlist.current()` 里的 warning，由 `routes/targets.py:352` 调起）。
        同一时刻 sweeper 在事件循环线程里 `del self._entries[...]`。直接遍历
        `self._entries.values()` 会在那个瞬间抛
        `RuntimeError: dictionary changed size during iteration`，而 `Handler.handleError`
        会把它吞掉 —— 后果是**那一行日志整条消失**，且只在凭据正好过期的那一刻发生。
        `tuple(...)` 在 C 层一次取完，对其它 Python 线程是原子的。内层的
        `entry.credentials.secrets` 不需要同样处理：它在 `store()` 之后再不会被改。
        """
        return frozenset(
            value.get_secret_value()
            for entry in tuple(self._entries.values())
            for value in entry.credentials.secrets.values()
        )

    def count(self) -> int:
        """条目数（含尚未被清理的过期项）。给测试与诊断用。"""
        return len(self._entries)

    def sweep(self) -> int:
        """清一遍过期条目，返回清掉的条数。同步、纯内存、不做任何 IO。

        与 `get()` 里的懒判定用的是同一个 `_expired()`，所以两条路径不可能对
        "什么算过期"有分歧。
        """
        now = self._clock()
        before = len(self._entries)
        self._prune(now)
        return before - len(self._entries)

    async def run_sweeper(self, interval: float = SWEEP_INTERVAL_SECONDS) -> None:
        """后台清理循环。由 lifespan `create_task` 起、关闭时 `cancel`。

        `interval` 可注入的唯一理由是测试（不然一条断言要等 60 秒），与 `clock` 同理。

        **刻意不捕获 `CancelledError`**：吞掉它的 sweeper 在 lifespan 关闭时会永远
        await 不完，`api` 就停不下来。日志里只有条数，**没有任何值**。
        """
        while True:
            await asyncio.sleep(interval)
            removed = self.sweep()
            if removed:
                logger.info("清理过期凭据", extra={"removed": removed})

    def _resolve(self, handle: str) -> _Entry | None:
        """查一条并做懒过期判定。过期的当场删掉。"""
        entry = self._entries.get(handle)
        if entry is None:
            return None
        if self._expired(entry, self._clock()):
            del self._entries[handle]
            return None
        return entry

    def _expired(self, entry: _Entry, now: float) -> bool:
        if now - entry.created_at > HARD_TTL_SECONDS:
            return True
        return entry.ref_count == 0 and now - entry.last_used_at > IDLE_TTL_SECONDS

    def _prune(self, now: float) -> None:
        for handle in [h for h, e in self._entries.items() if self._expired(e, now)]:
            del self._entries[handle]
