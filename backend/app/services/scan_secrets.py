"""扫描期凭据（操作者提供的测试账号口令）的脱敏来源。

# 这个模块补的洞（泄漏矩阵第 17 行）

测试账号口令**不在 KeyVault 里**（它不是 LLM 凭据，只写进 tmpfs 的 `instruction.txt`），
所以它不在 `key_vault.secret_values()` 那份精确子串集合里。而 `ScanSupervisor` 归因
失败时会打一行带 `stdout_tail` 的 warning —— Strix 一旦把指令正文回显进 stdout，那个
口令就明文进了日志。它也没有可识别的形状，正则拦不住它。

# 为什么是"包住"而不是第二个 Redactor

**脱敏点只有一处**（root handler 的 `RedactingJsonFormatter`）是本项目的设计。所以这里
不新增脱敏点，只新增一个**秘密来源**：本类包住 KeyVault 那个 provider，对外仍然只暴露
**一个** `SecretProvider`，交给唯一的那个 `Redactor`。

状态挂在实例上，不是模块级 dict —— 后者任何代码都能改，且测试之间会互相污染
（CLAUDE.md §Python）。
"""

from __future__ import annotations

from collections.abc import Sequence

from app.logging_setup import SecretProvider
from app.services.scan_launcher import TestCredential


class ScanSecretRegistry:
    """当前在跑的各次扫描登记着的测试账号口令。真正持有状态，所以是 class。"""

    def __init__(self, base: SecretProvider) -> None:
        # `base` 只在 `secret_values()` 里现调，结果并进去。**不缓存**：两边的凭据集合
        # 都随时在变（vault 的 sweeper 在删、扫描在起停），缓存过的那一份必然是旧的。
        self._base = base
        self._passwords: dict[str, tuple[str, ...]] = {}

    def register(self, scan_id: str, credentials: Sequence[TestCredential]) -> None:
        """登记一次扫描的全部测试账号口令。同一个 scan_id 重复登记即覆盖。

        **登记每一条凭据的 password，不是只有第一条** —— 与 `PLAN.md` §N1 那条
        "一个 handle 可能装 2–3 个值"同型：漏掉第二条就是漏掉一个明文口令。

        `role` 与 `username` **刻意不登记**：它们不是秘密，而报告里要说"用这个账号
        登录时发现了…"。把账号名塞进脱敏集合只会让报告与日志里的账号名变成
        `[REDACTED]`，是可读性损失换零安全收益。真正的秘密是口令。

        这里**不做长度校验**：`logging_setup.MIN_SECRET_LENGTH` 已经把退化的短值挡在
        替换之外了，在这里再挡一次是同一条规则的第二份副本。请求体该不该拒短口令是
        请求模型（T12c）的事。
        """
        self._passwords[scan_id] = tuple(credential.password for credential in credentials)

    def forget(self, scan_id: str) -> bool:
        """注销一次扫描的口令，返回"本次真的删掉了东西"。

        必须幂等：终态可能被多条路径观察到（正常结束、被停、进程退出兜底），
        重复注销不能报错。`None` 当得起哨兵：登记的值永远是元组（一条都没有时是空元组）。
        """
        return self._passwords.pop(scan_id, None) is not None

    def secret_values(self) -> frozenset[str]:
        """`base` 那一路 + 自己登记的全部口令。直接作为 `logging_setup.SecretProvider`。

        **先 `tuple()` 取一份快照再遍历，这一层不是防御性写法**（判据与
        `key_vault.secret_values()` 完全相同）：本方法是本类唯一会在**别的线程**里被调用
        的入口 —— `Redactor.redact()` 每格式化一条日志就调它一次，而日志会在
        `asyncio.to_thread` 的工作线程里打出来；同一时刻 `register`/`forget` 在事件循环
        线程里改这个 dict。直接遍历 `self._passwords.values()` 会在那个瞬间抛
        `RuntimeError: dictionary changed size during iteration`，而 `Handler.handleError`
        会把它**吞掉** —— 后果是**那一行日志整条消失**。
        """
        values = set(self._base())
        for passwords in tuple(self._passwords.values()):
            values.update(passwords)
        return frozenset(values)

    def count(self) -> int:
        """登记着的扫描数。给测试与诊断用（"口令有没有被漏在里面"）。"""
        return len(self._passwords)
