"""docker 探测层（T3）。

# 这里没有真实 Docker，但也没有"假装测过"

CLAUDE.md §测试 禁止单测碰真实 Docker。于是本文件把接缝放在**最低层**
（`DockerTransport` 只有一个方法），这样被替身掉的只是"字节怎么送到 daemon"，而
URL 拼装、状态码判定、JSON 解码、探测容器的 create payload、以及同路径判据的
真值表**全都真的被执行了**。替身之上的每一行代码都在测试里跑过。

唯一被替身遮住的是 `UnixSocketTransport` 自己。它由 `test_unix_socket_transport_*`
用一个 **真的 AF_UNIX socket**（tmp_path 下、几十行的假 daemon）覆盖 —— 那不是网络，
也不是 Docker，但它证明我们真的说得出合法的 HTTP。

# 同路径探测的替身怎么才算诚实

假 daemon 必须**真的模仿 `cp`**：从 create payload 的 `Cmd[0]` 读文件、写到 `Cmd[1]`。
如果替身只是回一个 `StatusCode: 0`，那么"哨兵内容真的穿过了挂载"这件事就没被验到，
而那恰好是整个探测的**唯一**目的。所以下面的 `_cp_emulator` 是本文件最重要的十行。
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.services.docker_probe import (
    ORPHAN_LABEL_SELECTOR,
    PROBE_CONTAINER_PREFIX,
    PROBE_LABEL_KEY,
    DockerApiError,
    DockerProbe,
    DockerReply,
    UnixSocketTransport,
    parse_self_container_id,
    read_self_container_ref,
)

SELF_REF = "648249032d81f0aa11bb22cc33dd44ee55ff6677889900aabbccddeeff001122"
SELF_IMAGE = "sha256:04a3e2ffb413000000000000000000000000000000000000000000000000beef"
SANDBOX_IMAGE = "ghcr.io/usestrix/strix-sandbox:1.3.0"
NETWORK = "strix_sandbox"


# =============================================================================
# 替身
# =============================================================================
@dataclass(frozen=True)
class RecordedCall:
    method: str
    path: str
    payload: object
    timeout_s: float


Handler = Callable[[RecordedCall], DockerReply]


@dataclass
class FakeTransport:
    """按 (method, path 子串) 路由到处理器。第一个匹配的生效。

    **没有匹配就 `AssertionError`**，不是静默返回 404：一次意料之外的 docker 调用
    （比如有人在探测里加了 `POST /images/create`，那是拉镜像）必须让测试炸掉，
    而不是被当成"那个东西不存在"。这是本文件对"只读探测"这条约束的机械保证。
    """

    routes: list[tuple[str, str, Handler]]
    calls: list[RecordedCall] = field(default_factory=list)

    def request(
        self, method: str, path: str, *, payload: object = None, timeout_s: float
    ) -> DockerReply:
        call = RecordedCall(method=method, path=path, payload=payload, timeout_s=timeout_s)
        self.calls.append(call)
        for want_method, needle, handler in self.routes:
            if method == want_method and needle in path:
                return handler(call)
        raise AssertionError(f"替身没有为 {method} {path} 准备应答 —— 探测发了一个意料之外的请求")

    def paths(self) -> list[str]:
        return [call.path for call in self.calls]


def reply(status: int, body: object) -> DockerReply:
    return DockerReply(status=status, body=json.dumps(body).encode("utf-8"))


def const(status: int, body: object) -> Handler:
    return lambda _call: reply(status, body)


def boom(reason: str) -> Handler:
    def handler(_call: RecordedCall) -> DockerReply:
        raise DockerApiError(reason, "替身刻意失败")

    return handler


def _payload_dict(call: RecordedCall) -> dict[str, object]:
    assert isinstance(call.payload, dict)
    return call.payload


def _cp_emulator(*, transform: Callable[[str], str] | None = None) -> Handler:
    """模仿探测容器里那次 `cp`：读 `Cmd[0]`，写 `Cmd[1]`。

    `transform` 用来制造"内容不一样"的场景（路径别名 bug 的真实形状之一）。
    读不到源文件时**不写**目标文件、并让 create 之后的 wait 返回非零 —— 与真 `cp` 一致。
    """

    def handler(call: RecordedCall) -> DockerReply:
        payload = _payload_dict(call)
        cmd = payload["Cmd"]
        assert isinstance(cmd, list)
        source, destination = Path(str(cmd[0])), Path(str(cmd[1]))
        if source.is_file():
            content = source.read_text(encoding="utf-8")
            destination.write_text(
                transform(content) if transform is not None else content, encoding="utf-8"
            )
        return reply(201, {"Id": "probe-container-id"})

    return handler


def probe_routes(
    *,
    cp: Handler,
    exit_code: int = 0,
    start_status: int = 204,
) -> list[tuple[str, str, Handler]]:
    """同路径探测那三步（create / start / wait）的替身路由。"""
    return [
        ("POST", "/containers/create", cp),
        ("POST", "/start", const(start_status, {})),
        ("POST", "/wait", const(200, {"StatusCode": exit_code})),
    ]


def happy_routes(data_dir: Path) -> list[tuple[str, str, Handler]]:
    """全部探测都成功的那一套。`data_dir` 只是让签名自解释探测会写它。"""
    assert data_dir.is_dir()
    return [
        ("GET", "/version", const(200, {"Version": "29.7.2", "ApiVersion": "1.53"})),
        (
            "GET",
            f"/containers/{SELF_REF}/json",
            const(
                200,
                {
                    "Id": SELF_REF,
                    "Image": SELF_IMAGE,
                    "NetworkSettings": {"Networks": {NETWORK: {}, "strix-console_default": {}}},
                },
            ),
        ),
        ("GET", f"/networks/{NETWORK}", const(200, {"Name": NETWORK})),
        ("GET", f"/images/{SANDBOX_IMAGE}/json", const(200, {"Size": 1234})),
        ("GET", "/containers/json?", const(200, [])),
        *probe_routes(cp=_cp_emulator()),
    ]


# =============================================================================
# 一、自我识别
# =============================================================================
# 一行真实的 mountinfo（本机 strix-console-api-1 实测，id 已改写）。
_REAL_MOUNTINFO = (
    "1234 1233 254:1 /docker/containers/"
    + SELF_REF
    + "/resolv.conf /etc/resolv.conf rw,relatime - ext4 /dev/vda1 rw\n"
)


def test_parse_self_container_id_reads_mountinfo() -> None:
    assert parse_self_container_id(_REAL_MOUNTINFO) == SELF_REF


def test_parse_self_container_id_ignores_short_ids() -> None:
    """12 位短 id 不匹配。

    正则要求整 64 位十六进制：mountinfo 里出现的一定是完整 id，而"随便一段像 id 的
    十六进制"（镜像层的 sha、卷名）不该被当成容器 id。
    """
    assert parse_self_container_id("/docker/containers/648249032d81/resolv.conf") is None


def test_parse_self_container_id_on_cgroup_v2_style_text() -> None:
    """cgroup v2 下 `/proc/self/cgroup` 只有 `0::/` —— 那条老路子拿不到 id。

    这条测试固定的是"为什么我们读 mountinfo 而不是 cgroup"。
    """
    assert parse_self_container_id("0::/\n") is None


def test_read_self_container_ref_falls_back_to_hostname(tmp_path: Path) -> None:
    """mountinfo 不存在（比如根本不在容器里）时退回主机名，而不是抛异常。

    退回是安全的：拿一个错的 ref 去自省会得到 404，于是 `api_attached` 与同路径探测
    如实报未知 —— 而"未知"在 `compute_blockers` 里等价于阻断。没有任何一条路径会
    因为这个退回而变成假绿。
    """
    assert read_self_container_ref(tmp_path / "nope") == socket.gethostname()


def test_read_self_container_ref_prefers_mountinfo(tmp_path: Path) -> None:
    path = tmp_path / "mountinfo"
    path.write_text(_REAL_MOUNTINFO, encoding="utf-8")
    assert read_self_container_ref(path) == SELF_REF


# =============================================================================
# 二、只读查询
# =============================================================================
def test_version_reports_daemon_version() -> None:
    transport = FakeTransport(
        [("GET", "/version", const(200, {"Version": "29.7.2", "ApiVersion": "1.53"}))]
    )
    version = DockerProbe(transport=transport).version()
    assert (version.server_version, version.api_version) == ("29.7.2", "1.53")


def test_version_rejects_non_200() -> None:
    """daemon 应答了但不是 200 —— 那不是"可达"，是"说不通"。"""
    transport = FakeTransport([("GET", "/version", const(500, {"message": "boom"}))])
    with pytest.raises(DockerApiError) as excinfo:
        DockerProbe(transport=transport).version()
    assert excinfo.value.reason == "bad_status"


def test_version_rejects_non_json_body() -> None:
    """socket 上有个东西在应答，但它不是 docker（比如一个 HTTP 代理的错误页）。"""
    transport = FakeTransport(
        [("GET", "/version", lambda _c: DockerReply(status=200, body=b"<html>nope</html>"))]
    )
    with pytest.raises(DockerApiError) as excinfo:
        DockerProbe(transport=transport).version()
    assert excinfo.value.reason == "bad_json"


def test_inspect_self_extracts_image_and_networks() -> None:
    transport = FakeTransport(
        [
            (
                "GET",
                f"/containers/{SELF_REF}/json",
                const(
                    200,
                    {
                        "Id": SELF_REF,
                        "Image": SELF_IMAGE,
                        "NetworkSettings": {"Networks": {"b": {}, "a": {}}},
                    },
                ),
            )
        ]
    )
    me = DockerProbe(transport=transport).inspect_self(SELF_REF)
    assert me.container_id == SELF_REF
    assert me.image_ref == SELF_IMAGE
    # 排序过：网络名的集合是结论，顺序不是。不排序会让断言依赖 dict 的插入序。
    assert me.networks == ("a", "b")


def test_inspect_self_404_has_its_own_reason() -> None:
    """daemon 不认识我们给的 id —— 这跟"daemon 挂了"是两件事，码必须不同。"""
    transport = FakeTransport(
        [("GET", "/containers/", const(404, {"message": "No such container"}))]
    )
    with pytest.raises(DockerApiError) as excinfo:
        DockerProbe(transport=transport).inspect_self("whatever")
    assert excinfo.value.reason == "self_container_not_found"


def test_inspect_self_without_image_field_is_an_error() -> None:
    """没有 `Image` 就没有探测镜像。**绝不退回一个猜的镜像名** —— 那会触发拉取。"""
    transport = FakeTransport([("GET", "/containers/", const(200, {"Id": SELF_REF}))])
    with pytest.raises(DockerApiError) as excinfo:
        DockerProbe(transport=transport).inspect_self(SELF_REF)
    assert excinfo.value.reason == "self_image_unknown"


def test_inspect_self_tolerates_missing_network_settings() -> None:
    """`NetworkSettings` 缺失/形状不对时给空元组，而不是崩。

    空元组的下游后果是 `api_attached=False` → 阻断项。诚实且安全的方向。
    """
    transport = FakeTransport(
        [("GET", "/containers/", const(200, {"Id": SELF_REF, "Image": SELF_IMAGE}))]
    )
    assert DockerProbe(transport=transport).inspect_self(SELF_REF).networks == ()


@pytest.mark.parametrize(("status", "expected"), [(200, True), (404, False)])
def test_network_present(status: int, expected: bool) -> None:
    transport = FakeTransport([("GET", "/networks/", const(status, {}))])
    assert DockerProbe(transport=transport).network_present(NETWORK) is expected


def test_network_present_raises_on_other_status() -> None:
    """500 不是"网络不存在"。把它当成 False 会让用户去建一个已经存在的网络。"""
    transport = FakeTransport([("GET", "/networks/", const(500, {}))])
    with pytest.raises(DockerApiError):
        DockerProbe(transport=transport).network_present(NETWORK)


def test_image_facts_present() -> None:
    transport = FakeTransport([("GET", "/images/", const(200, {"Size": 4096}))])
    facts = DockerProbe(transport=transport).image_facts(SANDBOX_IMAGE)
    assert (facts.present, facts.size_bytes) == (True, 4096)


def test_image_facts_absent_is_not_an_error() -> None:
    """镜像没拉过是**正常状态**，不是异常。它会变成一条 `sandbox_image_missing`。"""
    transport = FakeTransport([("GET", "/images/", const(404, {"message": "No such image"}))])
    facts = DockerProbe(transport=transport).image_facts(SANDBOX_IMAGE)
    assert (facts.present, facts.size_bytes) == (False, None)


def test_image_reference_is_not_url_escaped() -> None:
    """`/` 与 `:` 必须原样进 URL。

    docker 的路由是 `/images/{name:.*}/json`；把 `ghcr.io/usestrix/...` 转义成
    `%2F` 会让**存在的镜像**查成 404，于是首页显示"沙箱镜像未就绪"而 `docker images`
    里明明有。这条断言就是为了钉住那个 bug。
    """
    transport = FakeTransport([("GET", "/images/", const(200, {}))])
    DockerProbe(transport=transport).image_facts(SANDBOX_IMAGE)
    assert transport.paths() == [f"/images/{SANDBOX_IMAGE}/json"]


def sandbox_item(name: str, *, run_id: str | None = "scan-1") -> dict[str, object]:
    """一个沙箱容器在 `/containers/json` 里的样子。

    `run_id=None` 模仿 M0 靶场：带 `strix-run-type=console`（`PLAN.md:754` 的启动命令
    真的这么写），但**没有** `strix-run-id`。
    """
    labels: dict[str, str] = {"strix-run-type": "console"}
    if run_id is not None:
        labels["strix-run-id"] = run_id
    return {"Names": [f"/{name}"], "Labels": labels}


def test_orphan_names_strip_leading_slash_and_filter_by_label() -> None:
    transport = FakeTransport(
        [
            (
                "GET",
                "/containers/json?",
                const(200, [sandbox_item("strix-sandbox-abc"), sandbox_item("strix-sandbox-def")]),
            )
        ]
    )
    names = DockerProbe(transport=transport).orphan_sandbox_names()
    assert names == ("strix-sandbox-abc", "strix-sandbox-def")

    path = transport.paths()[0]
    # `all=1` 是必需的：已退出的沙箱同样占磁盘和 IP，只数运行中的会让泄漏看起来不存在。
    assert "all=1" in path
    # label 选择器必须与 `make reap`（T11）字面一致，否则两边数的不是同一批容器。
    assert ORPHAN_LABEL_SELECTOR in path.replace("%3D", "=").replace("+", " ")


def test_m0_target_range_is_not_counted_as_an_orphan() -> None:
    """本机实测出来的假阳性，回归测试。

    `PLAN.md:754` 起 M0 靶场时带的正是 `--label strix-run-type=console`，所以 daemon
    侧的过滤器**一定**会把它捞回来。区分靶场与真残骸的是 `strix-run-id`：
    `PLAN.md:812`（M0 断言 3）已实测确认真沙箱两个 label 都有。
    """
    transport = FakeTransport(
        [
            (
                "GET",
                "/containers/json?",
                const(
                    200,
                    [
                        # 靶场：`docker inspect m0-juice-shop` 的真实 label 形状。
                        {
                            "Names": ["/m0-juice-shop"],
                            "Labels": {
                                "strix-run-type": "console",
                                "strix-console-role": "m0-target",
                            },
                        },
                        sandbox_item("strix-sandbox-leaked", run_id="scan-7"),
                    ],
                ),
            )
        ]
    )
    assert DockerProbe(transport=transport).orphan_sandbox_names() == ("strix-sandbox-leaked",)


def test_orphan_names_require_a_nonempty_run_id() -> None:
    """空串 run-id 也不算 —— `labels.get()` 拿到 `""` 时 `if not` 必须是 False 分支。"""
    transport = FakeTransport(
        [("GET", "/containers/json?", const(200, [sandbox_item("s", run_id="")]))]
    )
    assert DockerProbe(transport=transport).orphan_sandbox_names() == ()


def test_orphan_names_tolerate_missing_names_field() -> None:
    labels = {"strix-run-type": "console", "strix-run-id": "scan-1"}
    transport = FakeTransport(
        [
            (
                "GET",
                "/containers/json?",
                # 没有 Labels 键、有 Labels 但没有 Names、Names 是空数组 —— 三种都不该炸。
                const(200, [{}, {"Labels": labels}, {"Names": [], "Labels": labels}]),
            )
        ]
    )
    assert DockerProbe(transport=transport).orphan_sandbox_names() == ()


def test_orphan_list_rejects_object_body() -> None:
    """列容器必须回数组。回对象说明我们打错了端点（或者对方不是 docker）。"""
    transport = FakeTransport([("GET", "/containers/json?", const(200, {"message": "nope"}))])
    with pytest.raises(DockerApiError) as excinfo:
        DockerProbe(transport=transport).orphan_sandbox_names()
    assert excinfo.value.reason == "bad_json"


# =============================================================================
# 三、同路径主动探测 —— 每一种失败模式一条
# =============================================================================
def test_same_path_probe_happy_path(tmp_path: Path) -> None:
    transport = FakeTransport(probe_routes(cp=_cp_emulator()))
    outcome = DockerProbe(transport=transport).same_path_probe(tmp_path, SELF_IMAGE)
    assert outcome.ok is True
    assert outcome.reason is None
    assert outcome.probe_image == SELF_IMAGE
    assert outcome.container_name is not None
    assert outcome.container_name.startswith(PROBE_CONTAINER_PREFIX)
    assert outcome.duration_ms is not None and outcome.duration_ms >= 0


def test_same_path_probe_cleans_up_both_files(tmp_path: Path) -> None:
    """哨兵与回写文件都不许留下。

    数据目录是用户会去看的地方（报告、日志都在那儿）。每点一次"检测"就多两个
    点文件，是一种缓慢的垃圾堆积。
    """
    transport = FakeTransport(probe_routes(cp=_cp_emulator()))
    DockerProbe(transport=transport).same_path_probe(tmp_path, SELF_IMAGE)
    assert list(tmp_path.iterdir()) == []


def test_same_path_probe_detects_unreadable_sentinel(tmp_path: Path) -> None:
    """容器里 `cp` 非零退出 = 它在那个路径下看不到我们的文件。

    这是"Docker Desktop 没共享这个路径"或"同路径挂载被改成 named volume"的形状。
    """

    def cp_fails(_call: RecordedCall) -> DockerReply:
        return reply(201, {"Id": "x"})

    transport = FakeTransport(probe_routes(cp=cp_fails, exit_code=1))
    outcome = DockerProbe(transport=transport).same_path_probe(tmp_path, SELF_IMAGE)
    assert outcome.ok is False
    assert outcome.reason == "sentinel_unreadable"


def test_same_path_probe_detects_missing_readback(tmp_path: Path) -> None:
    """`cp` 说成功了，但我们这一侧看不到它写的文件 —— **两侧是两个不同的目录**。

    这正是路径别名 bug 的教科书形状，也是单向 `cat` 探测（setup.sh 那种）**看不见**的
    那一半：容器那边一切正常，只有我们这边什么都没有。
    """
    transport = FakeTransport(probe_routes(cp=const(201, {"Id": "x"}), exit_code=0))
    outcome = DockerProbe(transport=transport).same_path_probe(tmp_path, SELF_IMAGE)
    assert outcome.ok is False
    assert outcome.reason == "readback_missing"


def test_same_path_probe_detects_content_mismatch(tmp_path: Path) -> None:
    """两侧都有文件，但内容不同 —— 同名不同物，同样是别名。

    随机 token 让这条判据不可能被巧合满足。
    """
    transport = FakeTransport(
        probe_routes(cp=_cp_emulator(transform=lambda _text: "somebody-else"))
    )
    outcome = DockerProbe(transport=transport).same_path_probe(tmp_path, SELF_IMAGE)
    assert outcome.ok is False
    assert outcome.reason == "content_mismatch"


def test_same_path_probe_timeout_is_unknown_not_false(tmp_path: Path) -> None:
    """探测没跑完 → `ok is None`。

    `False` 会让首页说"路径不一致"，而真相是"我们不知道"。两者的修法完全不同
    （一个去改 File sharing，一个去看 docker 为什么不应答），所以不能合并。
    下游的 `compute_blockers` 仍然把 `None` 算成阻断 —— 诚实不等于放宽。
    """
    routes = probe_routes(cp=_cp_emulator())
    routes = [(m, n, boom("timeout") if "/wait" in n else h) for m, n, h in routes]
    transport = FakeTransport(routes)
    outcome = DockerProbe(transport=transport).same_path_probe(tmp_path, SELF_IMAGE)
    assert outcome.ok is None
    assert outcome.reason == "timeout"
    # 超时也要清干净。
    assert list(tmp_path.iterdir()) == []


def test_same_path_probe_reports_create_conflict(tmp_path: Path) -> None:
    """名字撞了（409）也是"没探到"，不是"路径不对"。"""
    transport = FakeTransport(probe_routes(cp=const(409, {"message": "Conflict"})))
    outcome = DockerProbe(transport=transport).same_path_probe(tmp_path, SELF_IMAGE)
    assert outcome.ok is None
    assert outcome.reason == "probe_create_failed"


def test_same_path_probe_reports_start_failure(tmp_path: Path) -> None:
    transport = FakeTransport(probe_routes(cp=_cp_emulator(), start_status=500))
    outcome = DockerProbe(transport=transport).same_path_probe(tmp_path, SELF_IMAGE)
    assert outcome.ok is None
    assert outcome.reason == "probe_start_failed"


def test_same_path_probe_without_writable_dir_never_calls_docker(tmp_path: Path) -> None:
    """哨兵都写不出去时，一个 docker 请求都不该发。

    连数据目录都写不了的时候去起容器，只会用一个"探测容器失败"的报错盖掉真正的原因。
    """
    transport = FakeTransport([])  # 任何调用都会 AssertionError
    outcome = DockerProbe(transport=transport).same_path_probe(tmp_path / "missing", SELF_IMAGE)
    assert outcome.ok is None
    assert outcome.reason == "sentinel_write_failed"
    assert transport.calls == []


# =============================================================================
# 四、探测容器的形状 —— 这些是安全与"不删东西"的机械保证
# =============================================================================
def test_probe_container_payload_shape(tmp_path: Path) -> None:
    transport = FakeTransport(probe_routes(cp=_cp_emulator()))
    DockerProbe(transport=transport).same_path_probe(tmp_path, SELF_IMAGE)

    create = next(call for call in transport.calls if "/containers/create" in call.path)
    payload = _payload_dict(create)
    host_config = payload["HostConfig"]
    assert isinstance(host_config, dict)

    # 同路径：容器内外必须是**同一个绝对路径字符串**。这一行就是被探测的对象本身。
    assert host_config["Binds"] == [f"{tmp_path}:{tmp_path}"]
    # = docker run --rm。没有它，每次自检都会留下一个已退出的容器。
    assert host_config["AutoRemove"] is True
    # 显式覆盖 Entrypoint：哪天 api 镜像加了 ENTRYPOINT，只覆盖 Cmd 的探测容器会去
    # 启动 uvicorn —— 一个永不退出的探测容器，表现为"超时"而根因完全在别处。
    assert payload["Entrypoint"] == ["/bin/cp"]
    # 探测容器不继承任何环境、不上网。
    assert payload["Env"] == []
    assert payload["NetworkDisabled"] is True
    assert host_config["NetworkMode"] == "none"
    assert host_config["ReadonlyRootfs"] is True
    assert host_config["SecurityOpt"] == ["no-new-privileges:true"]
    # 显式名字：客户端超时**不会**停掉容器，没名字的孤儿无从定位。
    assert f"name={PROBE_CONTAINER_PREFIX}" in create.path


def test_probe_container_label_is_not_the_reaper_selector(tmp_path: Path) -> None:
    """探测容器的 label **不能**是 `strix-run-type=console`。

    复用那个 label 会有两个后果：① 每次自检都把自己的探测容器数成一个孤儿沙箱；
    ② `make reap`（T11）会把它当孤儿删 —— 而那时它可能正在跑。
    """
    transport = FakeTransport(probe_routes(cp=_cp_emulator()))
    DockerProbe(transport=transport).same_path_probe(tmp_path, SELF_IMAGE)

    create = next(call for call in transport.calls if "/containers/create" in call.path)
    labels = _payload_dict(create)["Labels"]
    assert isinstance(labels, dict)
    assert PROBE_LABEL_KEY in labels
    assert "strix-run-type" not in labels
    assert ORPHAN_LABEL_SELECTOR.split("=")[0] not in labels


def test_probe_never_calls_a_destructive_endpoint(tmp_path: Path) -> None:
    """一整轮探测里不许出现 delete / kill / stop / prune / 拉镜像。

    这条测试的价值全在**将来**：本机同时跑着用户的另一个 compose 项目，
    "自检顺手清理一下"是一个非常容易被加进来的"改进"。它会在那次提交里变红。
    """
    transport = FakeTransport(happy_routes(tmp_path))
    probe = DockerProbe(transport=transport)
    probe.version()
    me = probe.inspect_self(SELF_REF)
    probe.network_present(NETWORK)
    probe.image_facts(SANDBOX_IMAGE)
    probe.orphan_sandbox_names()
    probe.same_path_probe(tmp_path, me.image_ref)

    for call in transport.calls:
        assert call.method in {"GET", "POST"}, f"{call.method} {call.path}"
        for forbidden in ("/prune", "/kill", "/stop", "/images/create", "/remove"):
            assert forbidden not in call.path, f"探测调了破坏性端点：{call.path}"
    # POST 只许是那三步。
    posts = [call.path for call in transport.calls if call.method == "POST"]
    assert len(posts) == 3
    assert any("/containers/create" in p for p in posts)
    assert any("/start" in p for p in posts)
    assert any("/wait" in p for p in posts)


def test_wait_gets_a_longer_timeout_than_queries(tmp_path: Path) -> None:
    """`/wait` 要等容器跑完，用查询的 8 秒会把慢机器上的正常探测判成超时。"""
    transport = FakeTransport(happy_routes(tmp_path))
    probe = DockerProbe(transport=transport)
    probe.version()
    probe.same_path_probe(tmp_path, SELF_IMAGE)

    query = next(call for call in transport.calls if call.path == "/version")
    wait = next(call for call in transport.calls if "/wait" in call.path)
    assert wait.timeout_s > query.timeout_s


# =============================================================================
# 五、真实 AF_UNIX 传输层（不是网络、不是 Docker）
# =============================================================================
@contextmanager
def fake_daemon(socket_path: Path, response: bytes) -> Iterator[list[bytes]]:
    """一个只接一次连接的假 daemon。收到的原始请求存进列表供断言。"""
    received: list[bytes] = []
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen(1)

    def serve() -> None:
        conn, _ = server.accept()
        with conn:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            head, _, body = data.partition(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
            while len(body) < length:
                body += conn.recv(4096)
            received.append(head + b"\r\n\r\n" + body)
            conn.sendall(response)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield received
    finally:
        server.close()
        thread.join(timeout=5)


def _http_response(body: bytes) -> bytes:
    return (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
        + str(len(body)).encode("ascii")
        + b"\r\n\r\n"
        + body
    )


def test_unix_socket_transport_speaks_http_over_uds(tmp_path: Path) -> None:
    """证明我们自己写的 40 行传输层真的发得出合法 HTTP。

    这一段是唯一无法用替身覆盖的代码（替身就装在它上面），所以它值得一个真 socket。
    断言请求行、Content-Type 和 body 三样 —— 少任何一样，docker 都会用 400 拒绝我们。
    """
    socket_path = tmp_path / "docker.sock"
    body = b'{"Id":"probe-container-id"}'
    with fake_daemon(socket_path, _http_response(body)) as received:
        transport = UnixSocketTransport(socket_path=str(socket_path))
        got = transport.request(
            "POST", "/containers/create?name=x", payload={"Image": "i"}, timeout_s=5.0
        )

    assert got.status == 200
    assert got.body == body
    assert got.as_object() == {"Id": "probe-container-id"}

    request = received[0]
    assert request.startswith(b"POST /containers/create?name=x HTTP/1.1\r\n")
    assert b"Content-Type: application/json\r\n" in request
    assert request.endswith(b'{"Image": "i"}')


def test_unix_socket_transport_get_has_no_content_type(tmp_path: Path) -> None:
    """GET 不带 body，就不该带 `Content-Type` —— 那会让代理与 daemon 都困惑。"""
    socket_path = tmp_path / "docker.sock"
    with fake_daemon(socket_path, _http_response(b"{}")) as received:
        UnixSocketTransport(socket_path=str(socket_path)).request("GET", "/version", timeout_s=5.0)
    assert b"Content-Type" not in received[0]


def test_unix_socket_transport_missing_socket_has_its_own_reason(tmp_path: Path) -> None:
    """忘了挂 docker.sock 是最常见的部署错误，它值得一个自己的机器码。

    这条错误的中文修法（"给 api 加上 /var/run/docker.sock 挂载"）跟 `timeout`
    完全不同 —— 合并成一个 `transport_error` 就等于把修法也一起抹掉。
    """
    transport = UnixSocketTransport(socket_path=str(tmp_path / "nope.sock"))
    with pytest.raises(DockerApiError) as excinfo:
        transport.request("GET", "/version", timeout_s=1.0)
    assert excinfo.value.reason == "socket_missing"


def test_unix_socket_transport_wrong_kind_of_path(tmp_path: Path) -> None:
    """路径存在但不是 socket（比如挂错成了一个普通文件）→ `transport_error`。

    不是 `socket_missing`：文件在那儿，问题是它不是个 socket。区分开是为了让
    "挂载写错了目标"和"没挂"在日志里可辨。
    """
    regular = tmp_path / "not-a-socket"
    regular.write_text("hello", encoding="utf-8")
    with pytest.raises(DockerApiError) as excinfo:
        UnixSocketTransport(socket_path=str(regular)).request("GET", "/version", timeout_s=1.0)
    assert excinfo.value.reason == "transport_error"
