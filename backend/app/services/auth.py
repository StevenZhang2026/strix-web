"""单账号登录 —— 口令散列（scrypt）、`auth.json` 读写、进程内会话、失败限速。

# 范围（PLAN.md §单账号登录，2026-09-08 用户拍板）

**一个**用户名 + 口令。不做多用户、不做数据隔离、不做应用内改口令（改口令 = 重跑
`./setup.sh`）。这不是"先做简单版以后扩展"，是刻意的终点：多用户会带出 owner 列、
数据隔离、权限模型三件事，而本项目是本机单人工具。

# 它挡什么、不挡什么（写进 docs/SECURITY-zh.md，不许含糊）

挡的是"本机另一个账号顺手打开浏览器"和"浏览器里的其它标签页"。
本机管理员账号能读进程内存、读 docker.sock、读 SQLite 文件 ——
**登录页对拥有管理员权限的本地账号不构成边界**。把它宣传成安全边界就是自欺。

# 三条设计决定

1. **散列存 `${DATA}/auth.json`（0600），绝不进 SQLite。**
   `password_hash` 这个列名会被 `db.assert_no_secret_columns()` 的黑名单拦下，而给
   那个断言开白名单等于承认它有例外 —— 它是本项目最硬的结构性不变量。所以走文件。

2. **`hashlib.scrypt`，不引 `argon2-cffi` / `passlib`。**
   前者是 C 扩展，违反"依赖是负债"（先例：为避开 lxml 手写了 250 行
   WordprocessingML）。scrypt 抗 GPU 的特性与 argon2 同级，对"挡住同机另一个账号"
   这个威胁模型远远够用。

3. **会话 = 服务端不透明随机 id，存进程内存 dict，不用 JWT。**
   JWT 的卖点是服务端无状态；我们要的恰好相反 —— 状态必须由服务端持有**且只在内存
   里**。自包含 token 等于在客户端留一份可离线验证的副本。`pyjwt` 虽然在
   `requirements.lock` 里（`# via mcp`，strix-agent 的传递依赖），我们不 import 它。

# 日志纪律（违反一次就等于把口令写进容器日志）

口令、散列、salt、会话 id **绝不**进日志、argv、query、URL、DB、异常消息。
登录失败只记 `username_matched: bool`，**绝不记提交上来的用户名** ——
用户会把口令粘进用户名框，这是真实发生过的。

# 本模块可以当命令行跑（`python3 -m app.services.auth`）

`setup.sh` 的 C17f 用它算初始口令散列：口令经 **stdin** 进一次性容器，JSON 打到
stdout。宿主 Python 是 3.9.6 且链接 LibreSSL 2.8.3（`hasattr(hashlib, "scrypt")`
为 **False**），宿主 `openssl` 是 LibreSSL 3.3.6（没有 `kdf` 子命令）——
也就是说**宿主根本算不了 scrypt**，容器不是为了整洁，是唯一可行的办法。

让 setup.sh 复用本模块（而不是在 shell heredoc 里另写一遍 scrypt 参数）的收益是
**只有一处知道 `auth.json` 的格式**。格式改了，两边同时改完或者同时坏，不会出现
"脚本写出来的文件 api 读不了"这种只在部署时才暴露的偏差。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from app.errors import AuthLockedError, InvalidCredentialsError

logger = logging.getLogger(__name__)

# =============================================================================
# scrypt 参数
#
# `n = 2**14` 不是随手挑的数，是**默认 maxmem 下的最大档**：
#   `hashlib.scrypt` 的 `maxmem=0`（默认）在 OpenSSL 3 下等价于 32 MiB 上限。
#   n=2**14, r=8 需要 128*8*2**14 + 3072 = 16780288 B = 16.00 MiB —— 默认下通过
#   （本机实测 ~44 ms）。而 **n=2**15 需要 32 MiB，在默认 maxmem 下直接
#   `ValueError: [digital envelope routines] memory limit exceeded`**（已实测）。
#   也就是说想再往上翻一档，必须同时显式抬高 maxmem。
#
# 所以 `maxmem` 在本模块里**永远显式传**，绝不用默认值 0：默认值的含义取决于
# OpenSSL 版本，而"参数在这台机器上能用，换台机器 ValueError"是最难归因的一类故障。
# =============================================================================
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SALT_BYTES = 16

# `maxmem` 的精确下限 = 128 * r * n + 3072。这个 3072 是实测出来的：
# 传 `128*8*2**14 + 3072` 通过，传它 **减一** 就报 memory limit exceeded。
# 写成常量而不是"给个宽松的大数"，是为了让"参数变了但内存没跟上"在这里立刻失败。
SCRYPT_MAXMEM_OVERHEAD = 3072

# 从 `auth.json` 读来的参数会被用来申请内存。这个上限把"文件里的 n 多打了两个 0"
# 从"api 进程被 OOM kill"变成一条说得清的错误。
MAX_KDF_MEMORY_BYTES = 64 * 1024 * 1024

# 口令长度下限。`setup.sh` 的交互式设置里也有同一条检查 —— 两处都要有：
# shell 那处是为了让用户当场重输（体验），这处是为了让"绕过 setup.sh 直接调本模块"
# 也拦得住（不变量）。
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024
MAX_USERNAME_LENGTH = 64

AUTH_FILE_VERSION = 1

# 用户名不匹配时拿它跑一次 scrypt，把响应时间拉平。
# 固定值而不是随机值：随机会让每次调用多一次熵消耗，且这个值**不参与任何校验**，
# 它唯一的作用是让 CPU 干等同样长的时间。
_DUMMY_SALT = b"\x00" * SALT_BYTES


# =============================================================================
# 会话与限速的时间常量
#
# ⚠️ 这些数字**不是安全边界**。真正的上限是"api 进程重启即全部会话失效" ——
# 会话存在进程内存里，跟 KeyVault 同生共死（api `--workers 1`，重启一起蒸发）。
# 8 小时 / 7 天只决定"离开工位一下午回来要不要重新登录"这种舒适度。
# 把它们当成安全参数去调（比如改成 5 分钟）只会换来一个天天要重登的工具，
# 而攻击面一点没变。
# =============================================================================
IDLE_TIMEOUT_SECONDS = 8 * 60 * 60
ABSOLUTE_TIMEOUT_SECONDS = 7 * 24 * 60 * 60

# 会话上限。单账号单人使用，16 个已经覆盖"三台设备 + 几个标签页 + 忘了登出的旧会话"。
# 它的作用是给内存 dict 一个天花板，不是安全控制。
MAX_SESSIONS = 16

SESSION_ID_BYTES = 32

# =============================================================================
# 失败限速 —— **一个全局计数器，刻意不做 per-IP**
#
# 理由是实测的，不是偷懒：Docker Desktop 的 SNAT 会抹掉客户端身份，nginx access log
# 的 `$remote_addr` 恒为 `172.20.0.1`（桥网关）。也就是说 per-IP 的桶只会有**一个**
# 桶，和全局计数器完全等价，只是代码多一倍。
# 而改用 `X-Forwarded-For` 更糟 —— 最左段由客户端可控，等于让攻击者自由换桶，
# 那是一个**看起来更严格、实际上被绕过**的限速器。
#
# 所以：不要"顺手补上 per-IP"。要补，先去掉 SNAT。
#
# ⚠️ 锁定状态也在进程内存，**重启即清零**。这是已知且接受的（能重启 api 的人本来就
# 能读它的内存）。这一条要如实写进 docs/SECURITY-zh.md（T30a），不许含糊。
# =============================================================================
LOGIN_FAILURE_LIMIT = 5
LOGIN_LOCKOUT_SECONDS = 300

# 时钟。默认 `time.monotonic` 而不是 `time.time`：会话过期不该受系统时钟跳变
# （NTP 校准、笔记本休眠唤醒、用户手动改时间）影响。可注入是为了测试 ——
# 否则"验证 8 小时后会话失效"这条测试要真睡 8 小时。
Clock = Callable[[], float]


class AuthFileError(RuntimeError):
    """`auth.json` 缺失或不可解析。

    刻意**不是** `ConsoleError` 的子类（与 `db.SecretColumnError` 同一个理由）：
    它意味着**进程不该启动**，不是某个请求会收到的业务错误。给它一个 HTTP status
    会暗示"某个接口会返回这个码"，而那是假的。
    """


@dataclass(frozen=True, slots=True)
class KdfParams:
    """散列参数。**存在 `auth.json` 里**，校验时用文件里的值而不是本模块的常量。

    为什么要存进文件：以后调参（提高 n）不会废掉已经设好的口令 —— 老口令继续用老
    参数校验通过，新口令用新参数。若只信代码里的常量，那么"改一行常量"就等于让所有
    人都登不进去，而报错会是"口令错误"，没人能把它联想到那次改动。
    """

    name: str
    n: int
    r: int
    p: int
    dklen: int

    def __post_init__(self) -> None:
        if self.name != "scrypt":
            raise AuthFileError(f"不支持的 KDF {self.name!r}，只支持 scrypt。")
        if self.n < 2 or self.r < 1 or self.p < 1:
            raise AuthFileError(f"scrypt 参数不合法：n={self.n} r={self.r} p={self.p}")
        if not 16 <= self.dklen <= 64:
            raise AuthFileError(f"scrypt dklen 必须在 16–64 之间，收到 {self.dklen}")
        if self.maxmem > MAX_KDF_MEMORY_BYTES:
            raise AuthFileError(
                f"scrypt 参数需要 {self.maxmem} 字节内存，超过上限 "
                f"{MAX_KDF_MEMORY_BYTES}。n 是不是多打了几个 0？"
            )

    @property
    def maxmem(self) -> int:
        return 128 * self.r * self.n + SCRYPT_MAXMEM_OVERHEAD

    def derive(self, password: str, salt: bytes) -> bytes:
        """跑一次 KDF。约 44 ms（n=2**14），调用方要考虑别在事件循环里直接调。"""
        return hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=self.n,
            r=self.r,
            p=self.p,
            dklen=self.dklen,
            maxmem=self.maxmem,
        )

    def to_obj(self) -> dict[str, object]:
        return {"name": self.name, "n": self.n, "r": self.r, "p": self.p, "dklen": self.dklen}

    @classmethod
    def from_obj(cls, obj: object) -> KdfParams:
        if not isinstance(obj, dict):
            raise AuthFileError("auth.json 的 kdf 字段必须是对象。")
        try:
            return cls(
                name=_require_str(obj, "kdf.name"),
                n=_require_int(obj, "kdf.n"),
                r=_require_int(obj, "kdf.r"),
                p=_require_int(obj, "kdf.p"),
                dklen=_require_int(obj, "kdf.dklen"),
            )
        except KeyError as exc:
            raise AuthFileError(f"auth.json 的 kdf 缺少字段 {exc.args[0]!r}。") from exc


@dataclass(frozen=True, slots=True)
class AuthRecord:
    """`auth.json` 的内存表示。**单向散列**，没有任何路径能从它还原出口令。

    需求原话曾是"密码在后台加密"，但能解出明文口令的设计是缺陷不是功能：那意味着
    解密材料也在这台机器上，于是"读到文件"就等于"拿到口令"。
    """

    username: str
    salt: bytes
    derived_key: bytes
    kdf: KdfParams

    @classmethod
    def create(cls, username: str, password: str) -> AuthRecord:
        _validate_username(username)
        _validate_password(password)
        kdf = KdfParams(name="scrypt", n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN)
        salt = secrets.token_bytes(SALT_BYTES)
        return cls(username=username, salt=salt, derived_key=kdf.derive(password, salt), kdf=kdf)

    def verify(self, password: str) -> bool:
        """比对用 `hmac.compare_digest`，不用 `==`。

        `==` 对 bytes 是短路比较，逐字节泄漏"猜对了几个字节"的时间差。这条路径的
        输入完全由攻击者控制且可以无限次重放（限速只是把重放变慢），所以短路比较
        是真的可利用，不是理论洁癖。
        """
        return hmac.compare_digest(self.derived_key, self.kdf.derive(password, self.salt))

    def burn_equivalent_work(self, password: str) -> None:
        """用户名不匹配时也跑一次 KDF，把响应时间拉平。

        不做这一步的后果很具体：登录接口会变成一个**用户名探测器** —— 用户名对了
        要 44 ms，错了要 0.05 ms，一次请求就能问出来。而单账号系统里"用户名是什么"
        是攻击者唯一需要猜的另一半。

        用**本记录的参数**跑（不是模块常量），否则文件里存的是老参数时两条分支的
        耗时又对不上了。
        """
        self.kdf.derive(password, _DUMMY_SALT)

    def to_json_text(self) -> str:
        """序列化成 `auth.json` 的内容（含末尾换行）。

        `sort_keys=True` + `indent=2`：这个文件会被人打开看、会被 `diff`，
        稳定的键序让"它变了吗"这个问题有确定答案。
        """
        payload: dict[str, object] = {
            "version": AUTH_FILE_VERSION,
            "username": self.username,
            "salt_b64": base64.b64encode(self.salt).decode("ascii"),
            "hash_b64": base64.b64encode(self.derived_key).decode("ascii"),
            "kdf": self.kdf.to_obj(),
        }
        return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    @classmethod
    def load(cls, path: Path) -> AuthRecord:
        """从磁盘读。任何问题都抛 `AuthFileError`，**绝不**退化成"没有账号"。

        这一条是本模块最重要的失败语义：如果 `auth.json` 读不出来时我们"当作没设
        账号"并放行，那么删掉这个文件就成了一条绕过登录的路径。读不出来 = 拒绝启动。
        """
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise AuthFileError(
                f"未找到 {path}。控制台的登录账号由 ./setup.sh 创建（C17f）—— "
                "请在宿主上跑一次 ./setup.sh 再启动。"
            ) from exc
        except OSError as exc:
            raise AuthFileError(f"读取 {path} 失败：{exc}") from exc

        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AuthFileError(
                f"{path} 不是合法 JSON（第 {exc.lineno} 行）。本进程**不会**静默重建它 —— "
                "确认要重设账号就手动删除该文件并重跑 ./setup.sh。"
            ) from exc

        if not isinstance(obj, dict):
            raise AuthFileError(f"{path} 的顶层必须是 JSON 对象。")

        version = obj.get("version")
        if version != AUTH_FILE_VERSION:
            raise AuthFileError(
                f"{path} 的 version 是 {version!r}，本进程只认 {AUTH_FILE_VERSION}。"
            )

        try:
            username = _require_str(obj, "username")
            salt = _decode_b64(obj, "salt_b64")
            derived_key = _decode_b64(obj, "hash_b64")
            kdf = KdfParams.from_obj(obj["kdf"])
        except KeyError as exc:
            raise AuthFileError(f"{path} 缺少字段 {exc.args[0]!r}。") from exc

        if len(derived_key) != kdf.dklen:
            raise AuthFileError(
                f"{path} 的 hash_b64 解出 {len(derived_key)} 字节，与 kdf.dklen {kdf.dklen} 不符。"
            )
        _validate_username(username)
        return cls(username=username, salt=salt, derived_key=derived_key, kdf=kdf)


def write_auth_file(path: Path, content: str) -> None:
    """原子地写出 `auth.json`，**从创建的那一刻起就是 0600**。

    三个细节都是刻意的：

    1. `os.open(..., O_CREAT | O_EXCL, 0o600)` 而不是"先写再 `os.chmod`"——
       后者留一个竞态窗口：文件在那几微秒里是 0644 的，同机的另一个账号可以在那时
       打开它（拿到 fd 之后权限位怎么改都无所谓了）。而这个窗口的存在与否，
       在任何测试里都看不出来。
    2. `os.fchmod` 兜一手 `umask`：`os.open` 的 mode 会被进程 umask 削减
       （umask 0o022 时 0o600 仍是 0o600，但 umask 是全局状态，不该被信任）。
       刻意**不**用 `os.umask()` 去"临时改一下"—— 那是进程级副作用，会影响同时在
       跑的其它协程写出的每一个文件。
    3. `os.replace()` 原子替换：中途失败（磁盘满、被 kill）时留下的是完整的旧文件，
       而不是一个半截的 `auth.json` —— 后者会让下次启动报"不是合法 JSON"，
       而真正的原因是上次写崩了。

    ⚠️ 生产路径上写这个文件的是 `setup.sh`（C17f，宿主 shell + `umask 077`），
    不是本函数 —— 因为宿主 Python 3.9.6 链接 LibreSSL，连 `hashlib.scrypt` 都没有，
    不可能 import 本模块。本函数是**格式与权限的参考实现**，并且是测试里唯一能真的
    验"落盘就是 0600"的那条路径。两边的等价性由 setup.sh 自己写完后回读权限位来保证。
    """
    tmp_path = path.with_name(path.name + f".tmp.{os.getpid()}")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # 失败时别把半截的临时文件留在数据目录里 —— 下一次 O_EXCL 会撞上它。
        os.unlink(tmp_path)
        raise
    os.replace(tmp_path, path)


# =============================================================================
# 会话
# =============================================================================
@dataclass(frozen=True, slots=True)
class Session:
    """一个登录态。

    `frozen=True` 所以"续期"是**造一个新对象替换 dict 里的旧值**（见
    `SessionStore.resolve`），不是原地改字段。收益：任何持有 Session 引用的代码
    （比如正在处理请求的路由）看到的都是取用那一刻的快照，不会被并发的续期改掉。

    `id` 是不透明随机串。它**绝不**进日志、URL、DB —— 见模块 docstring 的日志纪律。
    """

    id: str
    username: str
    created_at: float
    last_seen_at: float


class SessionStore:
    """进程内会话表。

    状态挂在实例上、由 `AuthService` 显式持有，**不是模块级 dict**
    （CLAUDE.md §Python：模块级不得有可变全局状态）—— 模块级 dict 会让测试之间
    互相污染，也让"这个进程里有几套会话"变成一个说不清的问题。

    过期是**懒判定**（取用时判），不起后台任务。理由：一个只为了清理 ≤16 条内存记录
    而存在的定时任务，是另一个要管生命周期、要在关闭时 cancel、要在测试里 mock 的
    东西。取用时判一次的成本是两次减法。
    """

    def __init__(self, clock: Clock = time.monotonic) -> None:
        self._clock = clock
        self._sessions: dict[str, Session] = {}

    def create(self, username: str) -> Session:
        now = self._clock()
        self._prune(now)
        # 满了就淘汰**最老的**（按创建时间）。用 min 而不是 popitem：dict 的插入序
        # 在有过删除之后不等于创建序。created_at 相同时 min 返回先遍历到的那个，
        # 也就是先插入的 —— 结果仍然确定。
        while len(self._sessions) >= MAX_SESSIONS:
            oldest = min(self._sessions.values(), key=lambda s: s.created_at)
            del self._sessions[oldest.id]
        session = Session(
            id=secrets.token_urlsafe(SESSION_ID_BYTES),
            username=username,
            created_at=now,
            last_seen_at=now,
        )
        self._sessions[session.id] = session
        return session

    def resolve(self, session_id: str | None) -> Session | None:
        """取会话并续期。过期 / 不存在 / 没带 cookie 一律返回 None。

        这里用普通 dict 查找而不是逐个 `compare_digest`：id 是 32 字节
        （256 bit）密码学随机串，字典查找的时间差泄漏不出任何可用信息 ——
        攻击者需要先猜对整个 id 才能观察到"存在"，而那时他已经登录成功了。
        """
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None

        now = self._clock()
        if now - session.created_at > ABSOLUTE_TIMEOUT_SECONDS:
            del self._sessions[session_id]
            return None
        if now - session.last_seen_at > IDLE_TIMEOUT_SECONDS:
            del self._sessions[session_id]
            return None

        renewed = replace(session, last_seen_at=now)
        self._sessions[session_id] = renewed
        return renewed

    def drop(self, session_id: str | None) -> None:
        """登出。不存在也不报错 —— 登出必须幂等。"""
        if session_id:
            self._sessions.pop(session_id, None)

    def count(self) -> int:
        """活跃会话数（含尚未被懒清理掉的过期项）。给测试与 T3 的诊断页用。"""
        return len(self._sessions)

    def _prune(self, now: float) -> None:
        expired = [
            sid
            for sid, s in self._sessions.items()
            if now - s.created_at > ABSOLUTE_TIMEOUT_SECONDS
            or now - s.last_seen_at > IDLE_TIMEOUT_SECONDS
        ]
        for sid in expired:
            del self._sessions[sid]


class LoginRateLimiter:
    """全局失败计数器。per-IP 的理由见模块顶部常量区的注释 —— 不要加。"""

    def __init__(self, clock: Clock = time.monotonic) -> None:
        self._clock = clock
        self._failures = 0
        self._locked_until = 0.0

    def retry_after(self) -> int:
        """还需等待的秒数。0 表示未锁定。

        向上取整：返回 0 就意味着"现在可以试"，而 0.4 秒后才真的解锁的话，
        前端按这个数倒计时会正好撞在锁定窗口的最后一刻上。
        """
        remaining = self._locked_until - self._clock()
        if remaining <= 0:
            return 0
        return int(remaining) + 1

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= LOGIN_FAILURE_LIMIT:
            self._locked_until = self._clock() + LOGIN_LOCKOUT_SECONDS
            self._failures = 0

    def record_success(self) -> None:
        self._failures = 0
        self._locked_until = 0.0


class AuthService:
    """登录、登出、按会话 id 反查登录态。

    实例挂在 `app.state.auth` 上显式传递。它持有的三样东西——账号记录、会话表、
    限速器——都是**这个进程的**状态，随进程一起消失。
    """

    def __init__(
        self,
        record: AuthRecord,
        sessions: SessionStore,
        limiter: LoginRateLimiter,
    ) -> None:
        self._record = record
        self._sessions = sessions
        self._limiter = limiter

    @classmethod
    def load(cls, path: Path, clock: Clock = time.monotonic) -> AuthService:
        record = AuthRecord.load(path)
        return cls(
            record=record,
            sessions=SessionStore(clock=clock),
            limiter=LoginRateLimiter(clock=clock),
        )

    @property
    def sessions(self) -> SessionStore:
        """给 T3 的诊断页读会话数用。**只暴露 store，不暴露 record。**"""
        return self._sessions

    def login(self, username: str, password: str) -> Session:
        """校验口令并建会话。**同步、阻塞约 44 ms**，调用方要 `asyncio.to_thread`。

        错误分两种，且**用户名不存在与口令错误返回同一个码**：
        区分它们等于免费告诉攻击者用户名对不对，而单账号系统里那是他要猜的一半。
        """
        retry_after = self._limiter.retry_after()
        if retry_after > 0:
            logger.warning("登录被限速", extra={"retry_after": retry_after})
            raise AuthLockedError(retry_after=retry_after)

        # 用户名也用 compare_digest 比：它不是秘密，但短路比较会泄漏"前几个字符对了"，
        # 而那能把一次穷举从"猜整个用户名"降成"逐字符猜"。成本是几微秒，不值得省。
        matched = hmac.compare_digest(username.encode("utf-8"), self._record.username.encode())
        if matched:
            password_ok = self._record.verify(password)
        else:
            # 丢弃结果，只为了消耗同样多的时间。见 burn_equivalent_work 的 docstring。
            self._record.burn_equivalent_work(password)
            password_ok = False

        if not password_ok:
            self._limiter.record_failure()
            # **只记 username_matched，绝不记提交上来的用户名** —— 用户会把口令
            # 粘进用户名框（真实发生过）。这条日志一旦记了原值，口令就进了容器日志。
            logger.warning("登录失败", extra={"username_matched": matched})
            retry_after = self._limiter.retry_after()
            if retry_after > 0:
                raise AuthLockedError(retry_after=retry_after)
            raise InvalidCredentialsError()

        self._limiter.record_success()
        session = self._sessions.create(self._record.username)
        # 记的是**配置里的**用户名（不是提交上来的那个字符串），且绝不记会话 id。
        logger.info("登录成功", extra={"username": self._record.username})
        return session

    def logout(self, session_id: str | None) -> None:
        self._sessions.drop(session_id)

    def resolve(self, session_id: str | None) -> Session | None:
        return self._sessions.resolve(session_id)


# =============================================================================
# 小工具 —— JSON 取值。刻意手写而不是引 Pydantic：
# 本模块要能在没有 pydantic 的一次性容器里跑（见 services/__init__.py）。
# =============================================================================
def _require_str(obj: dict[str, object], dotted_name: str) -> str:
    key = dotted_name.rsplit(".", 1)[-1]
    if key not in obj:
        raise KeyError(dotted_name)
    value = obj[key]
    if not isinstance(value, str):
        raise AuthFileError(f"{dotted_name} 必须是字符串，收到 {type(value).__name__}。")
    return value


def _require_int(obj: dict[str, object], dotted_name: str) -> int:
    key = dotted_name.rsplit(".", 1)[-1]
    if key not in obj:
        raise KeyError(dotted_name)
    value = obj[key]
    # 显式排除 bool：它是 int 的子类，`{"n": true}` 会被算成 n=1 然后一路跑到
    # scrypt 里报一个完全看不懂的错。
    if isinstance(value, bool) or not isinstance(value, int):
        raise AuthFileError(f"{dotted_name} 必须是整数，收到 {type(value).__name__}。")
    return value


def _decode_b64(obj: dict[str, object], dotted_name: str) -> bytes:
    raw = _require_str(obj, dotted_name)
    try:
        # validate=True：默认行为是**静默丢掉**非 base64 字符，那会让一个被截断或
        # 被编辑器折行过的字段解出一个短一截却"看起来正常"的散列。
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AuthFileError(f"{dotted_name} 不是合法 base64。") from exc


def _validate_username(username: str) -> None:
    if not username or username.strip() != username:
        raise AuthFileError("用户名不能为空，也不能有前后空白。")
    if len(username) > MAX_USERNAME_LENGTH:
        raise AuthFileError(f"用户名不能超过 {MAX_USERNAME_LENGTH} 个字符。")
    # 控制字符会把用户名变成一个日志注入点（换行伪造出一整条日志记录）。
    if any(ch.isspace() or ord(ch) < 0x20 for ch in username):
        raise AuthFileError("用户名不能包含空白字符或控制字符。")


def _validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        # 报错里**只有长度下限，没有提交上来的值**，也没有实际长度。
        raise AuthFileError(f"口令至少需要 {MIN_PASSWORD_LENGTH} 个字符。")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise AuthFileError(f"口令不能超过 {MAX_PASSWORD_LENGTH} 个字符。")


# =============================================================================
# 命令行入口：`python3 -m app.services.auth`
#
# 读 stdin 的两行（用户名、口令），把 auth.json 的内容打到 stdout。
# 由 setup.sh 的 C17f 在一次性容器里调用。
#
# 为什么口令走 **stdin** 而不是 `docker run -e` 或 argv：
#   `-e` 与 argv 的值都会进**宿主** docker 客户端的进程参数，`ps aux` 全机可见；
#   而 `docker inspect` 还能从**已退出**的容器里把 `Config.Cmd` / `Config.Env`
#   读回来 —— 也就是说它不止暴露一瞬间，是留了个副本。
#
# 为什么不接受"输出文件路径"参数、自己写文件：
#   容器里是 root，写出来的文件在 Linux 宿主上会是 root 所有，用户之后想删掉重设
#   还得 sudo。让宿主 shell 拿着 stdout 自己写，属主就是跑 setup.sh 的那个人。
# =============================================================================
def _main() -> int:
    # 口令里不可能有换行 —— shell 侧是 `read -rs`，一行读到底。
    lines = sys.stdin.read().split("\n")
    if len(lines) < 2:
        sys.stderr.write("stdin 需要两行：第一行用户名，第二行口令。\n")
        return 1

    try:
        record = AuthRecord.create(lines[0], lines[1])
        # 立刻回读校验一次。它挡的是"参数写错了但没人发现"这一类：
        # 一个算得出来却验不过的散列，症状是用户永远登不进去，而错误信息是"口令错误"。
        if not record.verify(lines[1]):
            sys.stderr.write("刚生成的散列自校验失败，本机 hashlib 行为异常，请上报。\n")
            return 1
    except AuthFileError as exc:
        # 异常信息里只有规则（长度下限之类），没有值 —— 见 _validate_password。
        sys.stderr.write(f"{exc}\n")
        return 1

    sys.stdout.write(record.to_json_text())
    return 0


if __name__ == "__main__":
    sys.exit(_main())
