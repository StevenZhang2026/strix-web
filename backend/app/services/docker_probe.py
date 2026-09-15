"""Docker 只读探测 + **同路径主动探测**（`GET /api/system/status` 的事实来源）。

# 一条总原则：探测不到就报"未知"，绝不给乐观默认值

本模块每一个返回值里的 `None` 都是"我没能确认"，**不是** "没问题"。
理由不是防御性编程的洁癖：把一个未经探测的前置条件画成绿色的 UI，会在真的不就绪时
照样说就绪 —— 那种 UI 比没有 UI 更坏，因为它把"去查一下"这个动作也一起省掉了。
所以每个结论都是三态（`True` / `False` / `None`），并且每个 `None` 都带一个
机器可读的 `unknown_reason`。

# 为什么不引 `docker` SDK，而是自己写 40 行 HTTP over UDS

`docker==7.2.0` 确实已经在 lock 里（`strix-agent` 的依赖）。但我们要的只是
"发六种 GET 和三种 POST"，而 SDK 会带来两样不想要的东西：
① 它自己有一套异常层级与重试策略，我们得先把它翻译回本项目的机器码；
② 它在 import 期就会去读环境变量与 `~/.docker/config.json`，而本进程的 `HOME`
   语义是被刻意管控的（泄漏矩阵 #2）。
`http.client` + `AF_UNIX` 的组合是标准库，行为可以逐行读懂（CLAUDE.md §编码哲学 2）。

# 探测镜像：用 api 容器**自己正在跑的那个镜像**

`setup.sh:69` 在宿主上从 `backend/Dockerfile` 抠出 `python:3.12-slim@sha256:…` 当探测
镜像（单一真源）。在**容器内部**拿不到那个 digest —— Dockerfile 不在镜像里。三个候选：

1. compose 注入一个 `CONSOLE_PROBE_IMAGE` —— 会让那个 digest 有**两处**真源
   （Dockerfile 与 compose），而两处写同一个值迟早漂移；还要新增一个必须有安全默认值
   的 env。
2. 自省自己的**基础**镜像 digest —— Docker 不保留这条信息（`.Image` 是最终镜像的 ID，
   不是 `FROM` 那一行）。做不到。
3. **自省自己正在跑的镜像 ID**（本模块采用）。`GET /containers/<自己>/json` 的
   `.Image` 就是一个 `sha256:…` 的本地镜像 ID。它：
     · 一定存在（我们此刻正从它里面执行代码），因此探测**永不触发拉取**；
     · 是内容寻址的 ID 而不是 tag，不会被 tag 重指向骗到；
     · 零新增配置项 —— 本模块**不新增任何环境变量**，也就不可能让正在跑的 `api`
       因为缺一个必填 env 而起不来。

同一次自省顺带给出另两个答案：`.NetworkSettings.Networks` 就是
`network.api_attached` 的判据，`.Id` 用来在日志里定位自己。一次调用三个用途。

# 探测容器的形状（`docker run --rm` 的 API 等价物）

`Entrypoint: ["/bin/cp"]` + `Cmd: [哨兵, 回写]`。**必须显式覆盖 Entrypoint**：
api 镜像现在没有 `ENTRYPOINT`，但哪天有人加了一行，只覆盖 `Cmd` 的探测容器就会去启动
uvicorn —— 一个永不退出的探测容器，表现为"探测超时"而根因完全在别处。

判据是**双向**的，比 `setup.sh` 的单向 `cat` 更强：
  · 探测容器要能在同一个绝对路径下**读到**我们写的哨兵（否则 `cp` 非零退出）；
  · 我们要能在自己这一侧**读到**它写出的回写文件，且内容逐字节相等。
路径别名 bug 的形状恰好是"两侧都成功、但看到的是两个不同的目录"，单向探测在
"容器侧那个目录里恰好也有个同名文件"时会假绿。双向 + 随机 token 让这件事不可能。

刻意**不读容器日志**：`AutoRemove: true` 之下容器一退出就被删，`GET /containers/{id}/logs`
与它是竞态；而且日志是带 8 字节帧头的复用流，还要自己解帧。回写文件把这两个问题
一起去掉了。

# 破坏性操作：本模块一个都没有

只有 `POST /containers/create` + `/start` + `/wait` 三个写操作，且 create 时带
`AutoRemove: true`（= `docker run --rm`）。**不 stop、不 kill、不 rm、不 prune、不 pull。**
本机还跑着别的项目，回收孤儿容器是 T11 `Reaper` 的职责且它按 label 精确过滤 ——
本模块只**数**孤儿，一个都不动。

探测容器带固定前缀的显式名字（`strix-console-samepath-probe-<hex>`）：客户端超时
**不会**停掉容器（`Makefile` 的 `lock-resolve` 注释里记着这条），没名字的孤儿无从定位。
名字带随机后缀是为了让"上一次探测的容器还在"不至于让这一次 `409 Conflict`。
"""

from __future__ import annotations

import json
import logging
import re
import socket
import time
import uuid
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlencode

logger = logging.getLogger(__name__)

# compose 把宿主 socket 挂到这个固定路径（`docker-compose.yml` 的 api.volumes）。
DOCKER_SOCKET_PATH = "/var/run/docker.sock"

# 探测容器与哨兵文件的命名。前缀固定、后缀随机，理由见模块 docstring 末段。
PROBE_CONTAINER_PREFIX = "strix-console-samepath-probe-"
SENTINEL_PREFIX = ".samepath-probe-"
READBACK_SUFFIX = ".echo"

# 探测容器的 label。**刻意不是** `strix-run-type=console` —— 那个 label 是 Strix 打在
# 沙箱容器上的、也是 `Reaper`(T11) 与本模块 `count_orphan_sandboxes()` 的选择器。
# 复用它会让每次自检都把自己的探测容器数成一个孤儿沙箱。
PROBE_LABEL_KEY = "strix-console-probe"
PROBE_LABEL_VALUE = "same-path"

# 孤儿沙箱的选择器。与 `make reap`（T11）必须是同一个字面串。
ORPHAN_LABEL_SELECTOR = "strix-run-type=console"
# **但这个 label 单独一个是不够的。** 已在本机实测：`PLAN.md:754` 起 M0 靶场的命令
# 里也写着 `--label strix-run-type=console`，所以只按上面那个选择器数，会把**靶场**
# 数成一个孤儿沙箱（本机实测 `count=1`，名字是 `m0-juice-shop`）。
# 真沙箱一定同时带 `strix-run-id`，而这不是"实测碰巧如此"，是**结构上做不到相反**：
# `strix/runtime/docker_client.py:113-123` 的 `_apply_run_labels()` 开头就是
#     run_id = os.getenv("STRIX_RUN_ID")
#     if not run_id: return          # ← 早退
# 也就是说 `strix-run-type` 这个 label 只可能在 `strix-run-id` 已经被打上之后才写入。
# **Strix 产不出"有 run-type、无 run-id"的容器。** 靶场是我们自己手打 label 造出来的，
# 才落在那个 Strix 到不了的组合里。
#
# 所以要求 run-id 存在是一个**正向**判据（永不误伤真沙箱），而不是"把叫某个名字的
# 排除掉"。docker 的 filters 只支持正向 label 匹配，非空判定只能在客户端做。
# 本机实测：修前 `count=1`（`m0-juice-shop`），修后 `count=0`。
ORPHAN_REQUIRED_LABEL = "strix-run-id"
# 只回报前若干个名字：这是给人看的线索，不是清单。全量清单由 `make reap --dry-run` 给。
MAX_ORPHAN_NAMES = 20

# 超时。写成模块常量而不是新增 env：本模块的立场是"零新增配置项"（见模块 docstring）。
#   只读查询：docker daemon 在本机 UDS 上，8 秒已经是"它挂了"的量级。
#   同路径探测：镜像必定在本地（就是我们自己），create+start+cp 实测 < 1 秒；
#     25 秒留给"Docker Desktop 的 VM 正在换页"这种真实抖动。
QUERY_TIMEOUT_S = 8.0
PROBE_TIMEOUT_S = 25.0

_MOUNTINFO_PATH = Path("/proc/self/mountinfo")
_CONTAINER_ID_RE = re.compile(r"/containers/([0-9a-f]{64})/")


# =============================================================================
# 传输层
# =============================================================================
class DockerApiError(RuntimeError):
    """与 docker daemon 的一次交互失败。

    `reason` 是**机器码**，会原样进 `/api/system/status` 的 `unknown_reason` 字段，
    所以取值必须稳定、必须是 ASCII、且不许包含任何路径以外的环境细节。
    异常消息（给日志看的）可以更啰嗦，但同样不含凭据 —— 本模块从不接触凭据。
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason


@dataclass(frozen=True)
class DockerReply:
    """一次 HTTP 应答。刻意只有状态码和原始 body。

    不在这里做 JSON 解码：`/containers/{id}/start` 成功时是 `204` 且 body 为空，
    统一解码会让"成功"变成一个 JSONDecodeError。解码由需要它的调用点显式做。
    """

    status: int
    body: bytes

    def as_object(self) -> dict[str, object]:
        """把 body 当 JSON 对象读。形状不对就是 daemon 变了契约，报错而不是猜。"""
        try:
            loaded = json.loads(self.body)
        except json.JSONDecodeError as exc:
            raise DockerApiError("bad_json", f"应答不是合法 JSON（{len(self.body)} 字节）") from exc
        if not isinstance(loaded, dict):
            raise DockerApiError("bad_json", f"应答顶层不是对象，而是 {type(loaded).__name__}")
        return loaded

    def as_array(self) -> list[dict[str, object]]:
        try:
            loaded = json.loads(self.body)
        except json.JSONDecodeError as exc:
            raise DockerApiError("bad_json", f"应答不是合法 JSON（{len(self.body)} 字节）") from exc
        if not isinstance(loaded, list):
            raise DockerApiError("bad_json", f"应答顶层不是数组，而是 {type(loaded).__name__}")
        return [item for item in loaded if isinstance(item, dict)]


class DockerTransport(Protocol):
    """一次 docker API 往返。**这是本模块唯一的可替身接口。**

    刻意把接缝放在最低层（一个方法）而不是"六个语义化方法"：这样单测里的替身只需要
    实现一个函数，而 URL 拼装、状态码判定、JSON 解码这些真正容易写错的逻辑**全部被
    测试覆盖**。接缝放高一层的话，那些代码就只能靠真 docker 才跑得到 —— 而
    CLAUDE.md §测试 禁止单测碰真实 Docker。

    `payload` 标 `object` 而不是 dataclass：它是 **docker 的线上格式**，字段名与嵌套
    由对方的 API 决定（`HostConfig.Binds` 这种）。为它造一份 dataclass 镜像等于把外部
    契约抄一遍，抄错了还没人拦 —— 这不属于 CLAUDE.md「内部传参用 dataclass」的范围。
    """

    def request(
        self, method: str, path: str, *, payload: object = None, timeout_s: float
    ) -> DockerReply: ...


class _UnixHTTPConnection(HTTPConnection):
    """`http.client` 的连接，但连的是 AF_UNIX socket。

    只覆盖 `connect()`。`HTTPConnection` 的其余部分（请求行、头、chunked 解码）与
    传输方式无关 —— 这正是为什么这里不需要一个 HTTP 客户端依赖。
    """

    def __init__(self, socket_path: str, timeout_s: float) -> None:
        # host 只用来填 `Host:` 头。daemon 不校验它，但必须有一个合法值。
        super().__init__("docker", timeout=timeout_s)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


@dataclass(frozen=True)
class UnixSocketTransport:
    """真实实现。无状态：每次请求新建一条连接，所以它可以被整个进程共享。

    刻意不做连接池：`/api/system/status` 是人主动触发的低频接口，池子换来的是
    "连接在 daemon 重启后变成半开"这种只在排障时才发作的问题。
    """

    socket_path: str = DOCKER_SOCKET_PATH

    def request(
        self, method: str, path: str, *, payload: object = None, timeout_s: float
    ) -> DockerReply:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        conn = _UnixHTTPConnection(self.socket_path, timeout_s)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            return DockerReply(status=response.status, body=response.read())
        except FileNotFoundError as exc:
            # 必须排在 OSError 之前（它是 OSError 的子类）。这一条对应最常见的那种
            # 部署错误：忘了挂 docker.sock。它值得一个自己的机器码。
            raise DockerApiError("socket_missing", f"{self.socket_path} 不存在") from exc
        except PermissionError as exc:
            raise DockerApiError(
                "socket_permission_denied", f"{self.socket_path} 不可访问"
            ) from exc
        except TimeoutError as exc:
            # Python 3.10+ 起 `socket.timeout` 就是 `TimeoutError` 的别名，一条够了。
            raise DockerApiError("timeout", f"{timeout_s} 秒内 daemon 没有应答") from exc
        except (OSError, HTTPException) as exc:
            # 不把 str(exc) 之外的东西带出去，也不 bare except（CLAUDE.md §Python）。
            raise DockerApiError("transport_error", f"{type(exc).__name__}: {exc}") from exc
        finally:
            conn.close()


# =============================================================================
# 纯函数：自我识别
# =============================================================================
def parse_self_container_id(mountinfo_text: str) -> str | None:
    """从 `/proc/self/mountinfo` 里抠出自己的完整容器 id。

    为什么这是**首选**判据而不是 `socket.gethostname()`：
      · mountinfo 里出现的是 daemon 为本容器创建的 `/containers/<64位 id>/hostname`
        等文件的宿主侧路径 —— 那是一个**事实**，容器内改不了；
      · hostname 只是一个可配置的标签（compose 的 `hostname:` 一行就能改），而且只有
        12 位短 id。已实测两者都可用，但前者不会被配置骗到。

    cgroup 那条老路子（`/proc/self/cgroup`）在 cgroup v2 下只有 `0::/`，没有 id。
    """
    match = _CONTAINER_ID_RE.search(mountinfo_text)
    return match.group(1) if match else None


def read_self_container_ref(mountinfo_path: Path = _MOUNTINFO_PATH) -> str:
    """自己的容器标识。mountinfo 拿不到就退回主机名。

    退回而不是失败：主机名在本项目的 compose 下**就是**短 id（已实测），而
    docker API 接受 id 前缀。两条都不通说明我们根本不在容器里跑，那时下面的
    `inspect_self()` 会 404，`network.api_attached` 如实报未知。
    """
    try:
        text = mountinfo_path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    parsed = parse_self_container_id(text)
    return parsed if parsed is not None else socket.gethostname()


# =============================================================================
# 探测结果（内部 dataclass；转成 HTTP 出参在 services/system_status.py）
# =============================================================================
@dataclass(frozen=True)
class DaemonVersion:
    server_version: str
    api_version: str


@dataclass(frozen=True)
class SelfContainer:
    container_id: str
    image_ref: str
    networks: tuple[str, ...]


@dataclass(frozen=True)
class ImageFacts:
    present: bool
    size_bytes: int | None


@dataclass(frozen=True)
class SamePathOutcome:
    """同路径探测的结论。`ok is None` = 没能完成探测，不是"没问题"。"""

    ok: bool | None
    reason: str | None
    probe_image: str | None
    container_name: str | None
    duration_ms: int | None


@dataclass(frozen=True)
class OrphanSandbox:
    """一个通过两级 label 判据认出来的沙箱残骸。

    `container_id` 是**删除时用的地址**（`reaper.py`）；`run_id` 就是 `scan_id`
    （Strix 的 `STRIX_RUN_ID` 由我们设成 scan_id），Reaper 靠它避开在跑的扫描。
    """

    container_id: str
    name: str
    run_id: str


# =============================================================================
# 探测器
# =============================================================================
@dataclass(frozen=True)
class DockerProbe:
    """一组只读 docker 查询 + 一次同路径主动探测。

    frozen dataclass 而不是普通 class：它没有可变状态（CLAUDE.md §Python：模块级不得
    有可变全局状态，状态挂在显式传递的对象上 —— 这里连状态都没有，只有一个传输层）。
    """

    transport: DockerTransport

    # ---- 只读查询 -----------------------------------------------------------
    def version(self) -> DaemonVersion:
        """daemon 版本。**同时充当"docker 是否可达"的判据**。

        用 `/version` 而不是 `/_ping`：两者都能证明可达，但前者顺带回报版本号，而
        "daemon 是 29.7.2 吗"是排障时第一个要问的问题（API 版本协商、`--platform`
        行为都跟它有关）。一次调用两个用途。
        """
        reply = self.transport.request("GET", "/version", timeout_s=QUERY_TIMEOUT_S)
        if reply.status != 200:
            raise DockerApiError("bad_status", f"GET /version 返回 {reply.status}")
        payload = reply.as_object()
        return DaemonVersion(
            server_version=_as_str(payload.get("Version")),
            api_version=_as_str(payload.get("ApiVersion")),
        )

    def inspect_self(self, container_ref: str) -> SelfContainer:
        """自省。一次调用给出探测镜像、已接入的网络、以及自己的完整 id。"""
        reply = self.transport.request(
            "GET", f"/containers/{quote(container_ref)}/json", timeout_s=QUERY_TIMEOUT_S
        )
        if reply.status == 404:
            raise DockerApiError("self_container_not_found", f"daemon 不认识 {container_ref}")
        if reply.status != 200:
            raise DockerApiError("bad_status", f"自省返回 {reply.status}")
        payload = reply.as_object()
        image_ref = _as_str(payload.get("Image"))
        if not image_ref:
            raise DockerApiError("self_image_unknown", "自省结果里没有 Image 字段")
        return SelfContainer(
            container_id=_as_str(payload.get("Id")) or container_ref,
            image_ref=image_ref,
            networks=_network_names(payload),
        )

    def network_present(self, name: str) -> bool:
        reply = self.transport.request("GET", f"/networks/{quote(name)}", timeout_s=QUERY_TIMEOUT_S)
        if reply.status == 200:
            return True
        if reply.status == 404:
            return False
        raise DockerApiError("bad_status", f"查网络返回 {reply.status}")

    def image_facts(self, reference: str) -> ImageFacts:
        """沙箱镜像在不在本地。**不触发拉取** —— 拉取是 `POST /api/system/pull-image`。

        路径里的 `/` 与 `:` 不做转义：docker 的路由是 `/images/{name:.*}/json`，
        转义反而会让 `ghcr.io%2Fusestrix%2F…` 查不到。已实测。
        """
        reply = self.transport.request(
            "GET", f"/images/{reference}/json", timeout_s=QUERY_TIMEOUT_S
        )
        if reply.status == 404:
            return ImageFacts(present=False, size_bytes=None)
        if reply.status != 200:
            raise DockerApiError("bad_status", f"查镜像返回 {reply.status}")
        size = reply.as_object().get("Size")
        return ImageFacts(present=True, size_bytes=size if isinstance(size, int) else None)

    def orphan_sandboxes(self) -> tuple[OrphanSandbox, ...]:
        """按 label 列孤儿沙箱。**只列，不删**（删是 `reaper.py`）。

        `all=1`：已退出的沙箱同样占着磁盘和 IP，它们正是 R8 那个 SIGTERM 泄漏留下的
        残骸。只数运行中的会让"泄漏"看起来不存在。

        两级判据（见 `ORPHAN_REQUIRED_LABEL` 的注释）：daemon 侧按
        `strix-run-type=console` 过滤，客户端侧再要求 `strix-run-id` 非空。
        少了第二级，M0 靶场会被数成孤儿（并被 `Reaper` 删掉）。
        """
        query = urlencode({"all": "1", "filters": json.dumps({"label": [ORPHAN_LABEL_SELECTOR]})})
        reply = self.transport.request(
            "GET", f"/containers/json?{query}", timeout_s=QUERY_TIMEOUT_S
        )
        if reply.status != 200:
            raise DockerApiError("bad_status", f"列容器返回 {reply.status}")
        found: list[OrphanSandbox] = []
        for item in reply.as_array():
            labels = item.get("Labels")
            if not isinstance(labels, dict):
                continue
            run_id = labels.get(ORPHAN_REQUIRED_LABEL)
            if not isinstance(run_id, str) or not run_id:
                # 没有 run-id 的不是沙箱残骸。**刻意不把它算进去也不记日志** ——
                # 靶场是长期运行的正常容器，每次自检都 warn 一行只会训练人忽略日志。
                continue
            raw = item.get("Names")
            if not (isinstance(raw, list) and raw and isinstance(raw[0], str)):
                continue
            # docker 回的名字带前导 `/`，去掉它才是 `docker ps` 里看到的那个。
            name = raw[0].lstrip("/")
            container_id = item.get("Id")
            # `Id` 缺失（或不是字符串）时退回用名字：`/containers/{id}` 这个端点
            # **同样接受名字**，所以退回之后它照样删得掉，没有任何东西被降级 ——
            # 丢弃它才是错的（它两个 label 都在，是真残骸），报错更错（一条畸形元素
            # 会让整个 /api/system/status 的孤儿计数变成 unknown）。
            if not isinstance(container_id, str) or not container_id:
                container_id = name
            found.append(OrphanSandbox(container_id=container_id, name=name, run_id=run_id))
        return tuple(found)

    def orphan_sandbox_names(self) -> tuple[str, ...]:
        """孤儿沙箱的名字视图（`/api/system/status` 只要名字）。过滤逻辑只有一处。"""
        return tuple(sandbox.name for sandbox in self.orphan_sandboxes())

    # ---- 同路径主动探测 -----------------------------------------------------
    def same_path_probe(self, data_dir: Path, probe_image: str) -> SamePathOutcome:
        """写哨兵 → 起一次性容器把它 `cp` 成回写文件 → 我们再读回来逐字节比对。

        全部文件操作都在 `finally` 里清理。清理失败**不改变结论** —— 一个残留的
        `.samepath-probe-*` 文件是垃圾，把它升级成"同路径不可用"会误导人。
        """
        token = uuid.uuid4().hex
        sentinel = data_dir / f"{SENTINEL_PREFIX}{token}"
        readback = Path(f"{sentinel}{READBACK_SUFFIX}")
        container_name = f"{PROBE_CONTAINER_PREFIX}{token}"
        started = _monotonic_ms()

        try:
            sentinel.write_text(token, encoding="utf-8")
        except OSError as exc:
            logger.warning("同路径探测：哨兵文件写不出去", extra={"error": str(exc)})
            return SamePathOutcome(
                ok=None,
                reason="sentinel_write_failed",
                probe_image=probe_image,
                container_name=None,
                duration_ms=None,
            )

        try:
            exit_code = self._run_probe_container(
                name=container_name,
                image=probe_image,
                data_dir=data_dir,
                sentinel=sentinel,
                readback=readback,
            )
            reason = _same_path_verdict(exit_code, readback, token)
            return SamePathOutcome(
                ok=reason is None,
                reason=reason,
                probe_image=probe_image,
                container_name=container_name,
                duration_ms=_monotonic_ms() - started,
            )
        except DockerApiError as exc:
            logger.warning(
                "同路径探测未能完成",
                extra={"reason": exc.reason, "container_name": container_name},
            )
            return SamePathOutcome(
                ok=None,
                reason=exc.reason,
                probe_image=probe_image,
                container_name=container_name,
                duration_ms=_monotonic_ms() - started,
            )
        finally:
            # missing_ok：容器侧没能写出回写文件时它本来就不存在。
            sentinel.unlink(missing_ok=True)
            readback.unlink(missing_ok=True)

    def _run_probe_container(
        self,
        *,
        name: str,
        image: str,
        data_dir: Path,
        sentinel: Path,
        readback: Path,
    ) -> int:
        create = self.transport.request(
            "POST",
            f"/containers/create?name={quote(name)}",
            payload={
                "Image": image,
                # 显式覆盖 Entrypoint，理由见模块 docstring。
                "Entrypoint": ["/bin/cp"],
                "Cmd": [str(sentinel), str(readback)],
                # 探测容器不该继承任何东西：不给 Env、不给网络。
                "Env": [],
                "NetworkDisabled": True,
                "Labels": {PROBE_LABEL_KEY: PROBE_LABEL_VALUE},
                "HostConfig": {
                    # 这一行就是被探测的对象本身：容器内外必须是同一个绝对路径字符串。
                    "Binds": [f"{data_dir}:{data_dir}"],
                    "AutoRemove": True,  # = docker run --rm
                    "NetworkMode": "none",
                    "ReadonlyRootfs": True,  # 它只需要往那个 bind 里写
                    "SecurityOpt": ["no-new-privileges:true"],
                },
            },
            timeout_s=QUERY_TIMEOUT_S,
        )
        if create.status != 201:
            raise DockerApiError("probe_create_failed", f"create 返回 {create.status}")
        container_id = _as_str(create.as_object().get("Id"))
        if not container_id:
            raise DockerApiError("probe_create_failed", "create 应答里没有 Id")

        start = self.transport.request(
            "POST", f"/containers/{quote(container_id)}/start", timeout_s=QUERY_TIMEOUT_S
        )
        if start.status != 204:
            raise DockerApiError("probe_start_failed", f"start 返回 {start.status}")

        wait = self.transport.request(
            "POST",
            f"/containers/{quote(container_id)}/wait?condition=not-running",
            timeout_s=PROBE_TIMEOUT_S,
        )
        if wait.status != 200:
            raise DockerApiError("probe_wait_failed", f"wait 返回 {wait.status}")
        code = wait.as_object().get("StatusCode")
        if not isinstance(code, int):
            raise DockerApiError("probe_wait_failed", "wait 应答里没有 StatusCode")
        return code


# =============================================================================
# 纯函数（无 IO 的部分单独拆出来，好测是硬要求）
# =============================================================================
def _same_path_verdict(exit_code: int, readback: Path, token: str) -> str | None:
    """把"探测容器退出码 + 回写文件内容"翻译成一个机器码，`None` 表示通过。

    三种失败被刻意分开，因为它们的中文修复指引完全不同：
      · `sentinel_unreadable` —— 容器在那个路径下看不到我们的文件。这就是"路径没被
        Docker Desktop 共享"或"同路径挂载被改成了 named volume"的形状。
      · `readback_missing`    —— `cp` 说成功了，但我们这一侧看不到它写的文件。
        两侧看到的是**两个不同的目录** —— 路径别名 bug 的教科书形状。
      · `content_mismatch`    —— 两侧都有文件但内容不同。同名不同物，同样是别名。
    """
    if exit_code != 0:
        return "sentinel_unreadable"
    try:
        echoed = readback.read_text(encoding="utf-8")
    except OSError:
        return "readback_missing"
    return None if echoed == token else "content_mismatch"


def _network_names(inspect_payload: dict[str, object]) -> tuple[str, ...]:
    settings = inspect_payload.get("NetworkSettings")
    if not isinstance(settings, dict):
        return ()
    networks = settings.get("Networks")
    if not isinstance(networks, dict):
        return ()
    return tuple(sorted(str(name) for name in networks))


def _as_str(value: object) -> str:
    """把 daemon 回来的值当字符串读；不是字符串就当空。

    刻意不抛异常：这些字段（`Version`、`Id`）缺失时，调用点各自决定这是致命的
    （`Image` 缺了就没法探测）还是可以降级的（`Version` 缺了只是少显示一行）。
    """
    return value if isinstance(value, str) else ""


def _monotonic_ms() -> int:
    # time.monotonic 而不是 time.time：这是一段耗时，不该被系统时钟调整影响
    # （NTP 校时会让后者倒退，算出负数耗时）。
    return int(time.monotonic() * 1000)
