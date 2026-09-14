"""就绪判定与 `GET /api/system/status`（T3）。

# 这个文件盯的是一件事：**这个接口不许说谎**

`PLAN.md` 验收 #2 要的不是"首页有几个绿点"，而是"绿点为真"。所以本文件里最重要的
不是"全好的时候返回 ready"（那条也在），而是下面这几条：

  · 探测失败（`None`）和探测出问题（`False`）在**结论上等价** —— 都是阻断项。
    「在没有探测的情况下把状态画成绿色就是编造」，而 `is not True` 是这句话的代码形式。
  · `STRIX_TELEMETRY` 没设 = 遥测**开着**（Strix 侧默认 `True`）。把"变量不存在"
    读成"已关闭"是这个接口最容易犯、也最难被发现的谎。
  · `tls.cert_trusted` 恒为 `None`。它有一条专门的测试，见
    `test_cert_trusted_is_always_unknown` 的 docstring。
  · docker 挂掉时**只报一条** `docker_unreachable`，不把四个派生结论一起喷出来。

# 传输层替身从 test_docker_probe 复用

`from tests.conftest import ...` 已是本仓的既有做法（test_no_secret_columns.py）。
复用 `FakeTransport` 而不是再写一个：两份替身会各自漂移，而"探测层怎么被替身"这件事
只该有一个答案。
"""

from __future__ import annotations

import ipaddress
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.errors import ALL_ERRORS
from app.models import (
    DataDirSection,
    DockerSection,
    NetworkSection,
    SandboxImageSection,
    SystemStatusResponse,
    TelemetrySection,
)
from app.routes.auth import EXEMPT_PATHS
from app.services.docker_probe import DockerProbe
from app.services.system_status import (
    ALL_BLOCKER_CODES,
    BLOCKER_API_NOT_ON_SANDBOX_NETWORK,
    BLOCKER_DOCKER_UNREACHABLE,
    BLOCKER_SAME_PATH_UNVERIFIED,
    BLOCKER_SANDBOX_IMAGE_MISSING,
    BLOCKER_SANDBOX_NETWORK_MISSING,
    BLOCKER_TELEMETRY_NOT_DISABLED,
    CERT_TRUST_NOT_OBSERVABLE,
    DOCKER_DERIVED_BLOCKERS,
    collect_extra_ca_section,
    collect_system_status,
    collect_tls_section,
    compute_blockers,
    parse_flag_enabled,
    parse_strix_telemetry,
)
from app.settings import Settings
from tests.test_docker_probe import (
    NETWORK,
    SANDBOX_IMAGE,
    SELF_REF,
    FakeTransport,
    const,
    happy_routes,
    sandbox_item,
)

STATUS_PATH = "/api/system/status"


# =============================================================================
# 一、两个环境变量解析器
# =============================================================================
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # **空串（= 环境里没有这个变量）必须是 True。**
        # Strix 的 `strix/config/settings.py`：`enabled: bool = Field(default=True,
        # alias="STRIX_TELEMETRY")`。忘了设 = 遥测开着 = 每次扫描往外发数据，
        # 而那是本项目存在的理由之一（CLAUDE.md §禁区）。
        ("", True),
        ("false", False),
        ("False", False),
        ("FALSE", False),
        ("0", False),
        ("no", False),
        ("off", False),
        # 前后空格是 .env 里最常见的手误，不该改变含义。
        ("  false  ", False),
        ("true", True),
        ("1", True),
        # 认不出来的值 → True（保守方向）。见 parse_strix_telemetry 的 docstring。
        ("maybe", True),
        ("disabled", True),
    ],
)
def test_parse_strix_telemetry(raw: str, expected: bool) -> None:
    assert parse_strix_telemetry(raw) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", False), ("1", True), ("true", True), ("TRUE", True), ("0", False), ("maybe", False)],
)
def test_parse_flag_enabled(raw: str, expected: bool) -> None:
    """ "设了就算开"的标志位：默认方向与遥测**相反**，所以是两个函数。"""
    assert parse_flag_enabled(raw) is expected


# =============================================================================
# 二、compute_blockers —— 纯函数，每种组合直接断言
# =============================================================================
def docker_section(*, reachable: bool = True) -> DockerSection:
    return DockerSection(
        reachable=reachable,
        unknown_reason=None if reachable else "socket_missing",
        server_version="29.7.2" if reachable else None,
        api_version="1.53" if reachable else None,
    )


def network_section(*, present: bool | None = True, attached: bool | None = True) -> NetworkSection:
    return NetworkSection(name=NETWORK, present=present, api_attached=attached, unknown_reason=None)


def data_dir_section(*, ok: bool | None = True) -> DataDirSection:
    return DataDirSection(
        path="/data",
        identical_path_ok=ok,
        unknown_reason=None,
        probe_image=None,
        probe_container_name=None,
        probe_duration_ms=None,
    )


def telemetry_section(*, telemetry_on: bool = False) -> TelemetrySection:
    return TelemetrySection(
        strix_telemetry=telemetry_on,
        strix_no_update_check=True,
        raw_strix_telemetry="true" if telemetry_on else "false",
        raw_strix_no_update_check="1",
    )


def image_section(*, present: bool | None = True) -> SandboxImageSection:
    return SandboxImageSection(
        reference=SANDBOX_IMAGE, present=present, size_bytes=1, unknown_reason=None
    )


def blockers(
    *,
    docker: DockerSection | None = None,
    network: NetworkSection | None = None,
    data_dir: DataDirSection | None = None,
    telemetry: TelemetrySection | None = None,
    sandbox_image: SandboxImageSection | None = None,
) -> list[str]:
    """全好的那一套，按需覆盖某一节。"""
    return compute_blockers(
        docker=docker if docker is not None else docker_section(),
        network=network if network is not None else network_section(),
        data_dir=data_dir if data_dir is not None else data_dir_section(),
        telemetry=telemetry if telemetry is not None else telemetry_section(),
        sandbox_image=sandbox_image if sandbox_image is not None else image_section(),
    )


def test_everything_ok_means_no_blockers() -> None:
    assert blockers() == []


@pytest.mark.parametrize("value", [False, None])
def test_unknown_is_treated_exactly_like_broken(value: bool | None) -> None:
    """`None`（没探到）与 `False`（探到坏了）产出**同一条**阻断项。

    这是本任务最核心的一条断言。两者的 `unknown_reason` 不同（前端可以据此说
    "检测失败"而不是"配置不对"），但**结论**必须一样 —— 否则"探测超时"就会
    变成一条通往"可以开始扫描"的路。
    """
    assert blockers(network=network_section(present=value)) == [BLOCKER_SANDBOX_NETWORK_MISSING]
    assert blockers(network=network_section(attached=value)) == [BLOCKER_API_NOT_ON_SANDBOX_NETWORK]
    assert blockers(data_dir=data_dir_section(ok=value)) == [BLOCKER_SAME_PATH_UNVERIFIED]
    assert blockers(sandbox_image=image_section(present=value)) == [BLOCKER_SANDBOX_IMAGE_MISSING]


def test_network_present_but_api_not_attached_is_its_own_blocker() -> None:
    """网络在、api 没接进去 —— 这是必须单独报的一种。

    合并成一条"网络没配好"会让人去 `docker network create`（已经有了、无事发生），
    而真正要改的是 compose 里 `api` 的 networks 段。不设它的后果是 Caido 抓包代理
    **静默降级**（CLAUDE.md §Strix 集成），扫描照跑、结果偷偷变差。
    """
    assert blockers(network=network_section(present=True, attached=False)) == [
        BLOCKER_API_NOT_ON_SANDBOX_NETWORK
    ]


def test_docker_unreachable_reports_one_root_cause_only() -> None:
    """docker 挂了只报一条，不报那四个派生结论。

    五条阻断项里有四条是同一个根因的回声，而用户只需要修一件事（启动 Docker
    Desktop / 挂上 docker.sock）。把回声也列出来会让首页看起来像"到处都坏了"。
    """
    got = blockers(
        docker=docker_section(reachable=False),
        network=network_section(present=None, attached=None),
        data_dir=data_dir_section(ok=None),
        sandbox_image=image_section(present=None),
    )
    assert got == [BLOCKER_DOCKER_UNREACHABLE]
    assert DOCKER_DERIVED_BLOCKERS.isdisjoint(got)


def test_telemetry_blocker_survives_docker_being_down() -> None:
    """遥测是本进程的环境变量，docker 挂了也照样判得出来 —— 所以它不在压制范围内。"""
    got = blockers(
        docker=docker_section(reachable=False), telemetry=telemetry_section(telemetry_on=True)
    )
    assert got == [BLOCKER_DOCKER_UNREACHABLE, BLOCKER_TELEMETRY_NOT_DISABLED]


def test_docker_first_in_fix_order() -> None:
    """顺序即修复顺序：docker 是其余四项的前提，必须排第一。"""
    got = blockers(
        network=network_section(present=False),
        data_dir=data_dir_section(ok=False),
        sandbox_image=image_section(present=False),
        telemetry=telemetry_section(telemetry_on=True),
    )
    assert got == [
        BLOCKER_SANDBOX_NETWORK_MISSING,
        BLOCKER_SAME_PATH_UNVERIFIED,
        BLOCKER_SANDBOX_IMAGE_MISSING,
        BLOCKER_TELEMETRY_NOT_DISABLED,
    ]


def test_every_emitted_code_is_registered() -> None:
    """凡是能被产出的码都必须在 `ALL_BLOCKER_CODES` 里。

    那个元组是主会话往 zh-CN.json 里补文案时的清单。漏登记一个码 = 前端拿到一个
    没有中文说明的阻断项，只能显示原始英文码。
    """
    emitted = set(
        blockers(
            docker=docker_section(reachable=False), telemetry=telemetry_section(telemetry_on=True)
        )
    ) | set(
        blockers(
            network=network_section(present=False, attached=False),
            data_dir=data_dir_section(ok=False),
            sandbox_image=image_section(present=False),
        )
    )
    assert emitted == set(ALL_BLOCKER_CODES)
    assert DOCKER_DERIVED_BLOCKERS <= set(ALL_BLOCKER_CODES)


def test_blocker_codes_are_disjoint_from_http_error_codes() -> None:
    """阻断码与 HTTP 错误码是**两棵树**，不许有交集。

    理由跟 test_message_coverage.py::test_two_code_trees_stay_disjoint 完全一样：
    同一个码出现在两棵文案树里，前端就无从知道该取哪一条。
    """
    http_codes = {error.code for error in ALL_ERRORS}
    assert http_codes.isdisjoint(ALL_BLOCKER_CODES)


# =============================================================================
# 三、TLS 证书 —— 用真证书测，不用夹具字符串
# =============================================================================
def build_cert(
    *,
    now: datetime,
    not_before_days: int = -1,
    not_after_days: int = 3650,
    dns: tuple[str, ...] = ("localhost",),
    ips: tuple[str, ...] = ("127.0.0.1",),
    server_auth: bool = True,
    is_ca: bool = False,
) -> bytes:
    """现场签一张证书。

    为什么不用固定的 PEM 夹具：过期/未过期这件事必须相对 `now` 才有意义，而写死的
    夹具会在某个未来的日期突然从"有效"变成"过期"，让一条无关的测试变红。
    用 EC 而不是 RSA 只是为了快（每条用例都要签一张）—— 解析逻辑不关心密钥类型。
    """
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "strix-console")])
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + timedelta(days=not_before_days))
        .not_valid_after(now + timedelta(days=not_after_days))
        .add_extension(x509.BasicConstraints(ca=is_ca, path_length=None), critical=True)
    )
    names: list[x509.GeneralName] = [x509.DNSName(name) for name in dns]
    names.extend(x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips)
    if names:
        builder = builder.add_extension(x509.SubjectAlternativeName(names), critical=False)
    if server_auth:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
    return builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)


def test_good_cert_reports_every_fact(tmp_path: Path, now: datetime) -> None:
    path = tmp_path / "cert.pem"
    path.write_bytes(build_cert(now=now))
    section = collect_tls_section(path, now)

    assert section.cert_present is True
    assert section.cert_valid_now is True
    # 到期日是 `now + 3650 天`，所以相对同一个 `now` 正好剩 3650 整天。
    # （签发日在昨天不影响剩余天数 —— 那一项由 cert_valid_now 覆盖。）
    assert section.days_remaining == 3650
    assert section.san_dns == ["localhost"]
    assert section.san_ip == ["127.0.0.1"]
    # 这两条缺任何一条，Chrome 都直接拒连（CLAUDE.md §安全不变式）。
    assert section.has_server_auth_eku is True
    assert section.is_ca is False
    assert section.unknown_reason is None
    assert section.cert_path == str(path)


def test_expired_cert_is_reported_as_invalid(tmp_path: Path, now: datetime) -> None:
    path = tmp_path / "cert.pem"
    path.write_bytes(build_cert(now=now, not_before_days=-400, not_after_days=-35))
    section = collect_tls_section(path, now)
    assert section.cert_present is True
    assert section.cert_valid_now is False
    # 负数是刻意的：前端能据此说"已过期 35 天"，而 clamp 到 0 会让"刚过期"和
    # "过期一年"看起来一样。
    assert section.days_remaining == -35


def test_not_yet_valid_cert_is_reported_as_invalid(tmp_path: Path, now: datetime) -> None:
    """签发时间在未来（宿主时钟错了）也是无效 —— 判据是区间，不是只看到期日。"""
    path = tmp_path / "cert.pem"
    path.write_bytes(build_cert(now=now, not_before_days=5, not_after_days=100))
    assert collect_tls_section(path, now).cert_valid_now is False


def test_cert_without_san_or_eku_is_flagged(tmp_path: Path, now: datetime) -> None:
    """缺 SAN / 缺 EKU 是"nginx 起得来但浏览器打不开"的那种静默失败（R15）。

    `curl -k` 通得过，日志里一切正常 —— 只有这两个字段能把它说出来。
    """
    path = tmp_path / "cert.pem"
    path.write_bytes(build_cert(now=now, dns=(), ips=(), server_auth=False))
    section = collect_tls_section(path, now)
    assert section.san_dns == []
    assert section.san_ip == []
    assert section.has_server_auth_eku is False
    # 扩展缺失不是"读不出来"，而是一个确定的结论，所以 unknown_reason 仍为 None。
    assert section.unknown_reason is None


def test_ca_true_cert_is_flagged(tmp_path: Path, now: datetime) -> None:
    """`CA:TRUE` 必须被看见。

    用户会把这张证书**加进系统信任库**。一张可信的 CA 证书能给任何域名签发证书 ——
    那是一个远超"让本地控制台可访问"的授权，而 `setup.sh` 签出来的那张是 CA:FALSE。
    这里报出来是为了让"证书被人换过"这件事有一个可见的出口。
    """
    path = tmp_path / "cert.pem"
    path.write_bytes(build_cert(now=now, is_ca=True))
    assert collect_tls_section(path, now).is_ca is True


def test_missing_cert_file(tmp_path: Path, now: datetime) -> None:
    """没跑过 setup.sh 的 C18。`cert_present=False` 已经说清了，不需要 unknown_reason。"""
    section = collect_tls_section(tmp_path / "nope.pem", now)
    assert section.cert_present is False
    assert section.cert_valid_now is None
    assert section.unknown_reason is None


def test_unparsable_cert_file(tmp_path: Path, now: datetime) -> None:
    """文件在、但不是证书（截断、DER 当 PEM、或者有人把 key.pem 拷成了 cert.pem）。

    `cert_present=True` + `cert_unparsable` 的组合是刻意的：它把用户往"这个文件坏了"
    引，而 `cert_present=False` 会把人往"去生成一张"引 —— 后者会因为文件已存在而困惑。
    """
    path = tmp_path / "cert.pem"
    path.write_text("-----BEGIN CERTIFICATE-----\nnot base64 at all\n", encoding="utf-8")
    section = collect_tls_section(path, now)
    assert section.cert_present is True
    assert section.unknown_reason == "cert_unparsable"
    assert section.cert_valid_now is None


def test_cert_trusted_is_always_unknown(tmp_path: Path, now: datetime) -> None:
    """`cert_trusted` 恒为 `None`，并带一个说明为什么的机器码。

    **这是一个结论，不是一个 TODO。** macOS 的信任状态在宿主的 Keychain 里，
    api 容器（Linux、看不到宿主文件系统、也没有 `security` 命令）**无法**观测它。
    `PLAN.md:939` 列了 `tls.cert_trusted` 这个字段，我们如实把它填成"未知"而不是
    猜一个 `true` —— 猜出来的绿点会在证书其实没被信任时照样说"已信任"，
    而那正是这个字段唯一要防的事。
    """
    good = tmp_path / "cert.pem"
    good.write_bytes(build_cert(now=now))
    for path in (good, tmp_path / "missing.pem"):
        section = collect_tls_section(path, now)
        assert section.cert_trusted is None
        assert section.cert_trusted_reason == CERT_TRUST_NOT_OBSERVABLE


# =============================================================================
# 四、N2 企业 CA
# =============================================================================
def test_extra_ca_enabled_when_ssl_cert_file_points_at_the_mount(tmp_path: Path) -> None:
    """判据是容器内可观测的 `SSL_CERT_FILE`，不是 compose 里的宿主侧变量。"""
    mount = tmp_path / "extra-ca.pem"
    mount.write_text("-----BEGIN CERTIFICATE-----\nx\n", encoding="utf-8")
    settings = Settings(console_data_dir=tmp_path, ssl_cert_file=str(mount))

    section = collect_extra_ca_section(settings, mount)
    assert section.enabled is True
    assert section.mounted_looks_like_pem is True
    assert section.bundle_path == str(mount)
    assert section.unknown_reason is None
    # 真的加载了几张根 —— 这一项才能看见"SSL_CERT_FILE 指向一个空文件 → 0 张根、
    # 且不报错"那种静默故障（docker-compose.yml 里记着这条实测）。
    assert section.trusted_root_count is not None


def test_extra_ca_disabled_when_bundle_is_the_system_default(tmp_path: Path) -> None:
    settings = Settings(
        console_data_dir=tmp_path, ssl_cert_file="/etc/ssl/certs/ca-certificates.crt"
    )
    section = collect_extra_ca_section(settings, tmp_path / "extra-ca.pem")
    assert section.enabled is False
    # 路径不存在（N2 未启用时 compose 挂 /dev/null，本地测试里干脆没有这个文件）。
    assert section.mounted_looks_like_pem is None


def test_extra_ca_mount_that_is_not_a_pem(tmp_path: Path) -> None:
    """挂了个文件但里面不是证书 —— 用户挂错了（比如挂了私钥或一个 .der）。"""
    mount = tmp_path / "extra-ca.pem"
    mount.write_text("just some text", encoding="utf-8")
    settings = Settings(console_data_dir=tmp_path, ssl_cert_file=str(mount))
    assert collect_extra_ca_section(settings, mount).mounted_looks_like_pem is False


def test_extra_ca_unknown_when_ssl_cert_file_unset(tmp_path: Path) -> None:
    """compose 一定会设这个变量。空 = 不是 compose 起的 → "N2 是否启用"没有答案。"""
    settings = Settings(console_data_dir=tmp_path, ssl_cert_file="")
    section = collect_extra_ca_section(settings, tmp_path / "extra-ca.pem")
    assert section.enabled is False
    assert section.unknown_reason == "ssl_cert_file_unset"


# =============================================================================
# 五、装配
# =============================================================================
def ready_settings(tmp_path: Path) -> Settings:
    """一台"什么都配对了"的机器。

    每一项都显式传入而不是靠环境：测试容器里 `STRIX_*` 是否存在不由我们决定，
    而"漏了 STRIX_TELEMETRY 会怎样"恰好是下面一条用例要断言的东西 ——
    让它取决于宿主环境等于让那条断言随机通过。
    """
    return Settings(
        console_data_dir=tmp_path,
        strix_docker_sandbox_network=NETWORK,
        strix_image=SANDBOX_IMAGE,
        strix_telemetry="false",
        strix_no_update_check="1",
        ssl_cert_file="",
    )


def collect(settings: Settings, transport: FakeTransport) -> SystemStatusResponse:
    return collect_system_status(
        settings=settings,
        probe=DockerProbe(transport=transport),
        self_container_ref=SELF_REF,
        app_version="0.1.0",
        strix_version="1.6.2",
    )


def test_collect_all_good(tmp_path: Path) -> None:
    settings = ready_settings(tmp_path)
    status = collect(settings, FakeTransport(happy_routes(tmp_path)))

    assert status.blockers == []
    assert status.ready_for_scan is True
    assert status.docker.reachable is True
    assert status.docker.server_version == "29.7.2"
    assert status.network.present is True
    assert status.network.api_attached is True
    assert status.data_dir.identical_path_ok is True
    assert status.data_dir.path == str(tmp_path)
    # 探测镜像来自"我自己在跑的那个镜像"，不是配置项 —— 所以它一定是本地存在的。
    assert (
        status.data_dir.probe_image
        == "sha256:04a3e2ffb413000000000000000000000000000000000000000000000000beef"
    )
    assert status.telemetry.strix_telemetry is False
    assert status.sandbox_image.present is True
    assert status.orphan_sandboxes.count == 0
    assert status.versions.strix_version == "1.6.2"


def test_collect_when_docker_is_down(tmp_path: Path) -> None:
    """daemon 不可达时**其余每一节都要有 unknown_reason**，一个 True 都不许有。

    这是"未探测 ≠ 通过"在整份应答上的样子。
    """
    settings = ready_settings(tmp_path)
    transport = FakeTransport([("GET", "/version", const(500, {"message": "boom"}))])
    status = collect(settings, transport)

    assert status.ready_for_scan is False
    assert status.blockers == [BLOCKER_DOCKER_UNREACHABLE]
    assert status.docker.reachable is False
    for section in (status.network, status.data_dir, status.sandbox_image):
        assert section.unknown_reason == "bad_status"
    assert status.network.present is None
    assert status.network.api_attached is None
    assert status.data_dir.identical_path_ok is None
    assert status.sandbox_image.present is None
    assert status.orphan_sandboxes.count is None
    # docker 挂了也照样能读的两节仍然要有真内容。
    assert status.telemetry.strix_telemetry is False
    assert status.tls.cert_present is False


def test_collect_when_self_inspect_fails(tmp_path: Path) -> None:
    """自省失败（拿不到自己的镜像）→ 同路径探测无从做起，**不猜镜像**。

    退回一个猜的镜像名会触发一次 registry 拉取：几分钟、可能失败、而且拉下来的东西
    跟"我们自己跑的那个"不是一回事。
    """
    settings = ready_settings(tmp_path)
    transport = FakeTransport(
        [
            ("GET", "/version", const(200, {"Version": "29.7.2", "ApiVersion": "1.53"})),
            ("GET", f"/containers/{SELF_REF}/json", const(404, {"message": "No such container"})),
            ("GET", f"/networks/{NETWORK}", const(200, {"Name": NETWORK})),
            ("GET", f"/images/{SANDBOX_IMAGE}/json", const(200, {"Size": 1})),
            ("GET", "/containers/json?", const(200, [])),
        ]
    )
    status = collect(settings, transport)

    assert status.data_dir.identical_path_ok is None
    assert status.data_dir.unknown_reason == "self_container_not_found"
    assert status.data_dir.probe_image is None
    assert status.network.present is True
    assert status.network.api_attached is None
    assert set(status.blockers) == {
        BLOCKER_API_NOT_ON_SANDBOX_NETWORK,
        BLOCKER_SAME_PATH_UNVERIFIED,
    }
    # 数据目录里不许留下哨兵 —— 这条路径上根本没写过它。
    assert list(tmp_path.iterdir()) == []


def test_collect_when_api_is_not_on_the_sandbox_network(tmp_path: Path) -> None:
    settings = ready_settings(tmp_path)
    routes = [
        (
            "GET",
            f"/containers/{SELF_REF}/json",
            const(
                200,
                {
                    "Id": SELF_REF,
                    "Image": "sha256:deadbeef",
                    # 只在默认桥网上 —— 正是"忘了给 api 加 networks"的形状。
                    "NetworkSettings": {"Networks": {"strix-console_default": {}}},
                },
            ),
        ),
        *[r for r in happy_routes(tmp_path) if r[1] != f"/containers/{SELF_REF}/json"],
    ]
    status = collect(settings, FakeTransport(routes))
    assert status.network.present is True
    assert status.network.api_attached is False
    assert status.blockers == [BLOCKER_API_NOT_ON_SANDBOX_NETWORK]


def test_collect_reports_orphan_sandboxes_without_touching_them(tmp_path: Path) -> None:
    """孤儿沙箱只**数**，而且**不算阻断项**。

    它们是要清理的垃圾（`make reap`，T11 的活），不是发起新扫描的障碍。
    把它算成阻断会让"上次扫描留了个容器"变成"现在不能扫描"，而那不是真的。
    删除更不是本接口的事 —— 本模块一个删除调用都没有（见 test_docker_probe.py
    ::test_probe_never_calls_a_destructive_endpoint）。
    """
    settings = ready_settings(tmp_path)
    routes = [
        (
            "GET",
            "/containers/json?",
            const(200, [sandbox_item(f"strix-sandbox-{i}") for i in range(3)]),
        ),
        *[r for r in happy_routes(tmp_path) if r[1] != "/containers/json?"],
    ]
    status = collect(settings, FakeTransport(routes))
    assert status.orphan_sandboxes.count == 3
    assert status.blockers == []
    assert status.ready_for_scan is True


def test_collect_flags_telemetry_left_on(tmp_path: Path) -> None:
    """`.env` 里漏了 `STRIX_TELEMETRY=false` → 阻断，且原值原样回报。

    `raw_*` 字段是为了让用户能看见"我以为我设了"和"实际是什么"的差别 ——
    这两个变量的值不是凭据（它们就在 `.env` 里、也在 compose 里），可以原样显示。
    """
    settings = Settings(
        console_data_dir=tmp_path,
        strix_docker_sandbox_network=NETWORK,
        strix_image=SANDBOX_IMAGE,
        strix_telemetry="",
        strix_no_update_check="",
    )
    status = collect(settings, FakeTransport(happy_routes(tmp_path)))
    assert status.telemetry.strix_telemetry is True
    assert status.telemetry.raw_strix_telemetry == ""
    assert status.telemetry.strix_no_update_check is False
    assert status.blockers == [BLOCKER_TELEMETRY_NOT_DISABLED]
    assert status.ready_for_scan is False


def test_collect_without_configured_network_or_image(tmp_path: Path) -> None:
    """两个变量都没设时**不去查一个猜出来的名字**，如实说"没配"。"""
    settings = Settings(
        console_data_dir=tmp_path,
        strix_telemetry="false",
        strix_docker_sandbox_network="",
        strix_image="",
    )
    # 只有 /version 与自省会被调用；查网络/镜像的请求一旦发出就会 AssertionError。
    routes = [
        r
        for r in happy_routes(tmp_path)
        if r[1] not in (f"/networks/{NETWORK}", f"/images/{SANDBOX_IMAGE}/json")
    ]
    status = collect(settings, FakeTransport(routes))
    assert status.network.unknown_reason == "network_name_unset"
    assert status.sandbox_image.unknown_reason == "image_reference_unset"


def test_probed_at_is_utc_with_a_z(tmp_path: Path) -> None:
    """`Z` 只许缀在真的 UTC 上（pitfalls 条 37）。"""
    status = collect(ready_settings(tmp_path), FakeTransport(happy_routes(tmp_path)))
    assert status.probed_at.endswith("Z")
    assert "+" not in status.probed_at
    # 能被解析回一个 aware 的 UTC 时间。
    assert datetime.fromisoformat(status.probed_at.replace("Z", "+00:00")).tzinfo is not None


def test_response_carries_the_field_paths_the_frontend_reads(tmp_path: Path) -> None:
    """`PLAN.md` 验收 #2 与前端 zh-CN.json 按**名字**读这六条路径。

    改名会让首页那四个就绪点静默退回「尚未检测」—— 静默是因为 TypeScript 那边
    读到的是 `undefined`，没有任何一层会报错。所以这条测试钉的是字段名本身。
    """
    status = collect(ready_settings(tmp_path), FakeTransport(happy_routes(tmp_path)))
    dumped = json.loads(status.model_dump_json())

    for path in (
        "docker.reachable",
        "network.present",
        "network.api_attached",
        "data_dir.identical_path_ok",
        "telemetry.strix_telemetry",
        "sandbox_image.present",
        "orphan_sandboxes.count",
        "tls.cert_trusted",
        "extra_ca.enabled",
        "ready_for_scan",
        "blockers",
    ):
        node = dumped
        for part in path.split("."):
            assert isinstance(node, dict) and part in node, f"缺字段：{path}"
            node = node[part]


def test_response_does_not_dump_the_environment(tmp_path: Path) -> None:
    """应答里只许有被点名的那几项配置，不许出现整张环境表。

    这个接口在鉴权之后，但"鉴权之后"不是"什么都能放"。整张环境表迟早会包含一个
    不该出现在 HTTP 响应里的东西 —— 而它出现的那天不会有人注意到。
    """
    settings = ready_settings(tmp_path)
    status = collect(settings, FakeTransport(happy_routes(tmp_path)))
    dumped = json.loads(status.model_dump_json())
    # 顶层的键是九节 + 三个标量，写死在这里：加一节要有人主动改这一行。
    assert set(dumped) == {
        "ready_for_scan",
        "blockers",
        "docker",
        "network",
        "data_dir",
        "telemetry",
        "sandbox_image",
        "orphan_sandboxes",
        "tls",
        "extra_ca",
        "versions",
        "native_viewer_enabled",
        "probed_at",
    }
    # 数据库路径、auth.json 路径、迁移目录都不该出现在应答里。
    text = json.dumps(dumped)
    for leaked in ("auth.json", "console.sqlite", "migrations"):
        assert leaked not in text


# =============================================================================
# 六、路由 —— 鉴权与形状
# =============================================================================
@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """覆写 conftest 的 `settings`：本文件的路由测试都从"一切就绪"出发。

    覆写这一个，`app` / `anonymous` / `client` 三个夹具就都能用 conftest 里的。
    """
    return ready_settings(tmp_path)


@pytest.fixture
def anonymous(anonymous: TestClient, app: FastAPI, tmp_path: Path) -> TestClient:
    """覆写 conftest 的 `anonymous`，只为了替换传输层与容器 ref。

    **必须在 lifespan 跑完之后**（父夹具已经进过 `with`）：lifespan 里装的是真
    UnixSocketTransport，在它之前塞替身会被原地盖掉。这就是 routes/system.py::_probe
    那个注入点的实际用法。
    """
    app.state.docker_transport = FakeTransport(happy_routes(tmp_path))
    app.state.self_container_ref = SELF_REF
    return anonymous


def test_status_requires_a_session(anonymous: TestClient) -> None:
    """没登录就是 401。

    **这个接口绝不能进免鉴权名单**：它的正文是一份侦察报告（数据目录绝对路径、
    镜像名、docker 版本、孤儿容器名），而它还会**起一个容器** —— 那是一个未鉴权
    就能触发的资源消耗面。
    """
    response = anonymous.get(STATUS_PATH)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


def test_status_is_not_in_the_exempt_list() -> None:
    """免鉴权名单里只许有 `/api/health` 和三条登录路由。

    上面那条 401 测的是"现在是对的"，这条测的是"以后也别加进去" —— 名单是精确匹配
    的 frozenset，往里塞一个 `/api/system/status` 只是一行代码的事。
    """
    assert STATUS_PATH not in EXEMPT_PATHS
    assert "/api/health" in EXEMPT_PATHS


def test_status_returns_200_with_diagnostics(client: TestClient) -> None:
    """登录之后 200，而且**即使有阻断项也是 200**（见下一条）。"""
    response = client.get(STATUS_PATH)
    assert response.status_code == 200
    body = response.json()
    assert body["docker"]["reachable"] is True
    assert body["data_dir"]["identical_path_ok"] is True
    assert body["versions"]["strix_version"]


def test_status_stays_200_when_docker_is_down(app: FastAPI, client: TestClient) -> None:
    """docker 挂了也返回 200。

    用 503 会让前端走错误分支，显示一句"服务暂时不可用" —— 而用户真正需要看见的是
    那份写着"哪一项不就绪、怎么修"的正文。诊断结果是内容，不是状态码。
    """
    app.state.docker_transport = FakeTransport(
        [("GET", "/version", const(500, {"message": "boom"}))]
    )

    response = client.get(STATUS_PATH)
    assert response.status_code == 200
    body = response.json()
    assert body["ready_for_scan"] is False
    assert body["blockers"] == [BLOCKER_DOCKER_UNREACHABLE]


def test_health_still_works_and_needs_no_session(anonymous: TestClient) -> None:
    """`/api/health` 从 main.py 搬进 routes/health.py，形状一个字都不许变。

    它的消费者是 nginx 与 compose healthcheck —— 它们没有 cookie，也不会因为
    响应多了一个字段而报错，但**少**一个字段会让 T5 前端的启动检查失效。
    """
    response = anonymous.get("/api/health")
    assert response.status_code == 200
    assert set(response.json()) == {"status", "app_version", "strix_version"}
    assert response.json()["status"] == "ok"


def test_health_does_not_probe_docker(app: FastAPI, anonymous: TestClient) -> None:
    """健康检查不许碰 docker。

    碰了的后果很具体：docker 抖一下 → healthcheck 失败 → compose 重启 `api` →
    内存 KeyVault 清空 → 所有人的凭据丢失。也就是说"更全面的健康检查"会**制造**故障。
    这里把传输层换成"任何调用都炸"的空替身来钉住它。
    """
    exploding = FakeTransport([])
    app.state.docker_transport = exploding
    assert anonymous.get("/api/health").status_code == 200
    assert exploding.calls == []
