"""把各路探测结果装成 `GET /api/system/status` 的应答。

# 分工

`docker_probe.py` 只管"跟 docker 说话"，本模块管"这些事实合起来意味着什么"，
外加两件不需要 docker 的探测：TLS 证书事实、N2 企业 CA 是否生效。
判定逻辑（`compute_blockers`）是**无 IO 的纯函数**，所以每一种组合都能被直接断言，
不必先把 docker 摆成那个样子（CLAUDE.md §Python：纯函数优先，好测是硬要求）。

# 三条贯穿本模块的规则

1. **未探测 ≠ 通过。** 每个 `bool | None` 字段的 `None` 都是"我没能确认"，而
   `compute_blockers` 把 `is not True` 一律算作阻断项。也就是说"探测失败"和
   "探测出问题"在**结论**上等价 —— 唯一的区别在 `unknown_reason`。
2. **本接口永远返回 200。** 诊断结果是正文（见 `models.SystemStatusResponse`）。
3. **只读。** 唯一的写操作是同路径探测的哨兵文件（写完即删）与它那个一次性容器
   （`AutoRemove`）。不删任何容器、不拉任何镜像、不改任何配置。

# 为什么本模块直接构造 Pydantic 响应模型，而不是先造一份 dataclass

CLAUDE.md §Python 要求"内部传参用 dataclass"。这里的判断是：`SystemStatusResponse`
有九节、四十来个字段，而它们**没有任何内部消费者** —— 唯一的去处就是 HTTP 响应。
再造一份 dataclass 镜像，等于写四十行纯转录代码，而转录代码漂移时没有任何东西会
报错（两边都能编译、都能跑，只是某个字段永远是 `None`）。本模块从 `docker_probe`
拿到的**是** dataclass，边界模型在边界处组装 —— 这正是那条约定要的形状。

本模块仍然遵守 `services/` 的硬约定：**不 import fastapi**。import pydantic 是可以的
（`app/models.py` 也只依赖 pydantic），HTTP 那一层完全在 `routes/system.py`。
"""

from __future__ import annotations

import logging
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

from app.models import (
    DataDirSection,
    DockerSection,
    ExtraCaSection,
    NetworkSection,
    OrphanSandboxSection,
    SandboxImageSection,
    SystemStatusResponse,
    TelemetrySection,
    TlsSection,
    VersionsSection,
)
from app.services.docker_probe import (
    MAX_ORPHAN_NAMES,
    DockerApiError,
    DockerProbe,
    SelfContainer,
)
from app.settings import Settings

logger = logging.getLogger(__name__)

# N2 的挂载点。**必须**与 docker-compose.yml 的
# `${STRIX_EXTRA_CA_FILE:-/dev/null}:/etc/strix/extra-ca.pem:ro` 字面一致。
EXTRA_CA_MOUNT_PATH = "/etc/strix/extra-ca.pem"

# 读挂载点时的上限。它本该是几 KB 的 PEM；挂错成一个大文件时不该把它整个读进内存。
_PEM_SNIFF_BYTES = 256 * 1024
_PEM_MARKER = "BEGIN CERTIFICATE"

# `cert_trusted` 恒为 None 时给出的机器码。理由见 models.TlsSection 的 docstring。
CERT_TRUST_NOT_OBSERVABLE = "not_observable_from_container"

# =============================================================================
# 就绪阻断码
#
# 这些码**刻意不进** `app/errors.py`：那张表是"HTTP 错误响应的码"，这些是"就绪状态
# 的原因码"。两棵树混在一起，前端就不知道该用哪一棵文案树取词
# （test_message_coverage.py::test_two_code_trees_stay_disjoint 是同一条理由的机械版）。
# 中文文案由主会话合进 frontend/messages/zh-CN.json 的 systemStatus 段。
# =============================================================================
BLOCKER_DOCKER_UNREACHABLE = "docker_unreachable"
BLOCKER_SANDBOX_NETWORK_MISSING = "sandbox_network_missing"
BLOCKER_API_NOT_ON_SANDBOX_NETWORK = "api_not_on_sandbox_network"
BLOCKER_SAME_PATH_UNVERIFIED = "same_path_mount_unverified"
BLOCKER_SANDBOX_IMAGE_MISSING = "sandbox_image_missing"
BLOCKER_TELEMETRY_NOT_DISABLED = "telemetry_not_disabled"

ALL_BLOCKER_CODES: tuple[str, ...] = (
    BLOCKER_DOCKER_UNREACHABLE,
    BLOCKER_SANDBOX_NETWORK_MISSING,
    BLOCKER_API_NOT_ON_SANDBOX_NETWORK,
    BLOCKER_SAME_PATH_UNVERIFIED,
    BLOCKER_SANDBOX_IMAGE_MISSING,
    BLOCKER_TELEMETRY_NOT_DISABLED,
)

# docker 不可达时，这四项**必然**也探不出来。把它们一起报出来会让一个根因变成
# 五条阻断项，而用户只需要修一件事 —— 所以 `compute_blockers` 在那种情形下一条都不报。
# 这个集合是那条规则的可断言形式（test_system_status.py 用它）。
DOCKER_DERIVED_BLOCKERS: frozenset[str] = frozenset(
    {
        BLOCKER_SANDBOX_NETWORK_MISSING,
        BLOCKER_API_NOT_ON_SANDBOX_NETWORK,
        BLOCKER_SAME_PATH_UNVERIFIED,
        BLOCKER_SANDBOX_IMAGE_MISSING,
    }
)


# =============================================================================
# 纯函数
# =============================================================================
def parse_strix_telemetry(raw: str) -> bool:
    """`STRIX_TELEMETRY` 的**生效值**。空串（= 没设）返回 `True`。

    这条反直觉，但它是 Strix 侧的事实：`strix/config/settings.py` 里是
    `enabled: bool = Field(default=True, alias="STRIX_TELEMETRY")` —— **不设就是开着**。
    所以"环境里没有这个变量"绝不能报成"遥测已关闭"。这正是本文件那条总原则
    （未探测 ≠ 通过）在一个具体字段上的样子。

    认不出来的值也返回 `True`：我们不知道 Strix 的 Pydantic 会把 `"maybe"` 解析成
    什么（大概率是启动失败），而在"不确定"和"说已关闭"之间，只有前者是诚实的。
    """
    return raw.strip().lower() not in {"false", "0", "no", "off", "f", "n"}


def parse_flag_enabled(raw: str) -> bool:
    """`STRIX_NO_UPDATE_CHECK` 这类"设了就算开"的标志位。

    与上面那个函数刻意分开：它们的**默认方向相反**（遥测不设是开，更新检查不设是关），
    合成一个带 `default=` 参数的函数只会让调用点看起来一样、行为不一样。
    """
    return raw.strip().lower() in {"1", "true", "yes", "on", "t", "y"}


def compute_blockers(
    *,
    docker: DockerSection,
    network: NetworkSection,
    data_dir: DataDirSection,
    telemetry: TelemetrySection,
    sandbox_image: SandboxImageSection,
) -> list[str]:
    """哪些前置条件不满足。空列表 = 可以发起扫描。

    判据一律是 `is not True` —— `None`（没探到）与 `False`（探到坏了）都算阻断。
    顺序即修复顺序：docker 在最前面，因为它是其余四项的前提。

    **刻意不算进阻断项的两件事**：
      · TLS 证书 —— 证书过期只影响浏览器怎么连控制台，不影响扫描能不能跑。把它算成
        阻断项会让"证书还剩 3 天"变成"不能扫描"，而那不是真的。
      · 孤儿沙箱 —— 它们是要清理的垃圾，不是发起新扫描的障碍。
    两者都在正文里如实回报，由前端自己决定怎么提示。
    """
    blockers: list[str] = []
    if not docker.reachable:
        blockers.append(BLOCKER_DOCKER_UNREACHABLE)
    else:
        if network.present is not True:
            blockers.append(BLOCKER_SANDBOX_NETWORK_MISSING)
        if network.api_attached is not True:
            blockers.append(BLOCKER_API_NOT_ON_SANDBOX_NETWORK)
        if data_dir.identical_path_ok is not True:
            blockers.append(BLOCKER_SAME_PATH_UNVERIFIED)
        if sandbox_image.present is not True:
            blockers.append(BLOCKER_SANDBOX_IMAGE_MISSING)
    # 遥测与 docker 无关：它是本进程的环境变量，docker 挂了也照样能判。
    if telemetry.strix_telemetry:
        blockers.append(BLOCKER_TELEMETRY_NOT_DISABLED)
    return blockers


# =============================================================================
# TLS 证书
# =============================================================================
@dataclass(frozen=True)
class _CertFacts:
    valid_now: bool
    days_remaining: int
    san_dns: tuple[str, ...]
    san_ip: tuple[str, ...]
    has_server_auth_eku: bool
    is_ca: bool


def _parse_cert(pem: bytes, now: datetime) -> _CertFacts:
    """解析证书。**只取 `setup.sh` 真的签进去的那几项**，不做通用证书查看器。

    每一项都对应一个已知的失败模式：
      · `has_server_auth_eku` / `san_dns` —— 缺任何一个，Chrome 直接拒连
        （CLAUDE.md §安全不变式：「必须带 SAN + EKU=serverAuth」）。这是 R15
        点名的那种"静默失败"：nginx 起得来、`curl -k` 通得过、浏览器打不开。
      · `days_remaining` —— 自签是 10 年期，快到期意味着这套部署活了很久没人管。
      · `is_ca` —— 它必须是 `False`。一张 `CA:TRUE` 的自签证书被用户加进信任库之后，
        能给**任何**域名签发可信证书 —— 那是一个远超"让本地控制台可访问"的授权。
    """
    cert = x509.load_pem_x509_certificate(pem)
    not_before = cert.not_valid_before_utc
    not_after = cert.not_valid_after_utc

    san_dns: tuple[str, ...] = ()
    san_ip: tuple[str, ...] = ()
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        san_dns = tuple(san.get_values_for_type(x509.DNSName))
        san_ip = tuple(str(ip) for ip in san.get_values_for_type(x509.IPAddress))
    except x509.ExtensionNotFound:
        # 没有 SAN 扩展就是空元组 —— 这是一个**结论**（Chrome 会拒连），不是错误。
        pass

    has_eku = False
    try:
        eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        has_eku = ExtendedKeyUsageOID.SERVER_AUTH in eku
    except x509.ExtensionNotFound:
        pass

    is_ca = False
    try:
        is_ca = cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    except x509.ExtensionNotFound:
        # 缺 BasicConstraints 时 X.509 的语义是"非 CA"。
        pass

    return _CertFacts(
        valid_now=not_before <= now <= not_after,
        # 只取整天。剩 3 天半报 3 是保守方向 —— 报 4 会让人以为还有一整天。
        days_remaining=(not_after - now).days,
        san_dns=san_dns,
        san_ip=san_ip,
        has_server_auth_eku=has_eku,
        is_ca=is_ca,
    )


def collect_tls_section(cert_path: Path, now: datetime) -> TlsSection:
    """读证书文件并解析。读不到/解析不了都不抛异常 —— 那是要回报的状态。"""
    unknown = TlsSection(
        cert_path=str(cert_path),
        cert_present=False,
        cert_valid_now=None,
        days_remaining=None,
        san_dns=[],
        san_ip=[],
        has_server_auth_eku=None,
        is_ca=None,
        cert_trusted=None,
        cert_trusted_reason=CERT_TRUST_NOT_OBSERVABLE,
        unknown_reason=None,
    )
    try:
        pem = cert_path.read_bytes()
    except FileNotFoundError:
        # 没跑过 ./setup.sh 的 C18。cert_present=False 已经把这件事说清楚了。
        return unknown
    except OSError as exc:
        logger.warning("读证书失败", extra={"error": str(exc)})
        return unknown.model_copy(update={"unknown_reason": "cert_unreadable"})

    try:
        facts = _parse_cert(pem, now)
    except ValueError as exc:
        # cryptography 用 ValueError 表示"这不是一张证书"。截断的文件、DER 当 PEM 用、
        # 或者有人把 key.pem 拷成了 cert.pem 都会落到这里。
        logger.warning("证书解析失败", extra={"error": str(exc)})
        return unknown.model_copy(
            update={"cert_present": True, "unknown_reason": "cert_unparsable"}
        )

    return TlsSection(
        cert_path=str(cert_path),
        cert_present=True,
        cert_valid_now=facts.valid_now,
        days_remaining=facts.days_remaining,
        san_dns=list(facts.san_dns),
        san_ip=list(facts.san_ip),
        has_server_auth_eku=facts.has_server_auth_eku,
        is_ca=facts.is_ca,
        cert_trusted=None,
        cert_trusted_reason=CERT_TRUST_NOT_OBSERVABLE,
        unknown_reason=None,
    )


# =============================================================================
# N2 企业 CA
# =============================================================================
def _sniff_pem(path: Path) -> bool | None:
    """这个挂载点里是不是一份 PEM。`None` = 路径不存在（= 根本没挂）。

    N2 未启用时 compose 会把 `/dev/null` 挂在这里（"整条挂载可选"在 compose 里
    表达不出来，见 docker-compose.yml 那段注释）。读 `/dev/null` 得到空内容，
    于是这里返回 `False` —— 与"挂了个空文件"不可区分，但那两种情况的结论相同。
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(_PEM_SNIFF_BYTES)
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("读 CA 挂载点失败", extra={"error": str(exc)})
        return None
    return _PEM_MARKER in head.decode("ascii", errors="ignore")


def _count_trusted_roots() -> int | None:
    """当前进程**真的**加载了几张根证书。

    这不是装饰性指标。docker-compose.yml 里记着一个已实测的静默故障：
    `SSL_CERT_FILE=`（空）会让 OpenSSL 加载 0 张根证书**且不报错**。数一下是唯一
    能看见它的办法 —— 任何"变量设了吗"式的检查都会说"设了"。
    """
    try:
        return len(ssl.create_default_context().get_ca_certs())
    except (OSError, ssl.SSLError) as exc:
        logger.warning("统计根证书失败", extra={"error": str(exc)})
        return None


def collect_extra_ca_section(settings: Settings, mount_path: Path) -> ExtraCaSection:
    """N2 启用/未启用，以及实际生效的根证书张数。

    判据是**容器内可观测的事实**，不是 `STRIX_EXTRA_CA_FILE`：那个变量只出现在
    compose 的 volumes 段（宿主侧路径），api 容器里读不到它（见 settings.py 的注释）。
    容器内的事实是 `SSL_CERT_FILE` 指向哪里 —— 而它指向这个挂载点，正是
    `setup.sh` 启用 N2 时干的唯一一件事。
    """
    bundle_path = settings.ssl_cert_file
    return ExtraCaSection(
        enabled=bundle_path == str(mount_path),
        mounted_path=str(mount_path),
        mounted_looks_like_pem=_sniff_pem(mount_path),
        bundle_path=bundle_path,
        trusted_root_count=_count_trusted_roots(),
        # compose 一定会设 SSL_CERT_FILE（带 `:-` 兜底）。空串意味着这个进程不是
        # compose 起的 —— 那时"N2 是否启用"这个问题本身没有答案，如实说。
        unknown_reason=None if bundle_path else "ssl_cert_file_unset",
    )


# =============================================================================
# docker 侧各节
# =============================================================================
def _docker_section(probe: DockerProbe) -> DockerSection:
    try:
        version = probe.version()
    except DockerApiError as exc:
        logger.warning("docker 不可达", extra={"reason": exc.reason})
        return DockerSection(
            reachable=False, unknown_reason=exc.reason, server_version=None, api_version=None
        )
    return DockerSection(
        reachable=True,
        unknown_reason=None,
        server_version=version.server_version or None,
        api_version=version.api_version or None,
    )


def _inspect_self(
    probe: DockerProbe, container_ref: str
) -> tuple[SelfContainer | None, str | None]:
    try:
        return probe.inspect_self(container_ref), None
    except DockerApiError as exc:
        logger.warning("自省失败", extra={"reason": exc.reason})
        return None, exc.reason


def _network_section(
    probe: DockerProbe, name: str, me: SelfContainer | None, self_reason: str | None
) -> NetworkSection:
    if not name:
        # compose 一定会设 STRIX_DOCKER_SANDBOX_NETWORK。空 = 手工起的容器，
        # 而那种容器几乎肯定也没接进沙箱网络。不猜一个名字去查。
        return NetworkSection(
            name="", present=None, api_attached=None, unknown_reason="network_name_unset"
        )
    try:
        present: bool | None = probe.network_present(name)
        present_reason: str | None = None
    except DockerApiError as exc:
        present, present_reason = None, exc.reason

    attached = None if me is None else name in me.networks
    return NetworkSection(
        name=name,
        present=present,
        api_attached=attached,
        # 两个子探测各有可能失败，但一节只有一个 unknown_reason。优先报网络查询的
        # 失败原因：`api_attached` 依赖自省，而自省失败时网络查询通常也失败，
        # 前者的原因更接近根因。
        unknown_reason=present_reason or (self_reason if attached is None else None),
    )


def _sandbox_image_section(probe: DockerProbe, reference: str) -> SandboxImageSection:
    if not reference:
        return SandboxImageSection(
            reference="", present=None, size_bytes=None, unknown_reason="image_reference_unset"
        )
    try:
        facts = probe.image_facts(reference)
    except DockerApiError as exc:
        return SandboxImageSection(
            reference=reference, present=None, size_bytes=None, unknown_reason=exc.reason
        )
    return SandboxImageSection(
        reference=reference,
        present=facts.present,
        size_bytes=facts.size_bytes,
        unknown_reason=None,
    )


def _orphan_section(probe: DockerProbe) -> OrphanSandboxSection:
    try:
        names = probe.orphan_sandbox_names()
    except DockerApiError as exc:
        return OrphanSandboxSection(count=None, names=[], unknown_reason=exc.reason)
    return OrphanSandboxSection(
        # count 是**全部**，names 只是前 MAX_ORPHAN_NAMES 个。两者不一致时，
        # count 才是真相 —— 所以数字和名字分开给，而不是让前端去 len(names)。
        count=len(names),
        names=list(names[:MAX_ORPHAN_NAMES]),
        unknown_reason=None,
    )


def _data_dir_section(
    probe: DockerProbe, data_dir: Path, me: SelfContainer | None, self_reason: str | None
) -> DataDirSection:
    if me is None:
        return DataDirSection(
            path=str(data_dir),
            identical_path_ok=None,
            # 没自省成功就不知道该用哪个镜像当探测镜像。**不退回一个猜的镜像名** ——
            # 那会触发一次 registry 拉取（几分钟、可能失败），而且拉下来的东西
            # 跟"我们自己跑的那个"不是一回事。
            unknown_reason=self_reason or "probe_image_unknown",
            probe_image=None,
            probe_container_name=None,
            probe_duration_ms=None,
        )
    outcome = probe.same_path_probe(data_dir, me.image_ref)
    return DataDirSection(
        path=str(data_dir),
        identical_path_ok=outcome.ok,
        unknown_reason=outcome.reason,
        probe_image=outcome.probe_image,
        probe_container_name=outcome.container_name,
        probe_duration_ms=outcome.duration_ms,
    )


# =============================================================================
# 装配
# =============================================================================
def collect_system_status(
    *,
    settings: Settings,
    probe: DockerProbe,
    self_container_ref: str,
    app_version: str,
    strix_version: str,
    now: datetime | None = None,
) -> SystemStatusResponse:
    """跑一遍全部探测。**同步阻塞**，调用方负责 `asyncio.to_thread`。

    写成同步函数而不是 async：里面每一步都是阻塞 IO（UDS 上的 http.client、
    `read_bytes`），把它们逐个包成 async 只会得到"看起来是 async、实际串行阻塞"的
    代码。CLAUDE.md §Python 的要求是"async 函数里禁止同步阻塞 IO" —— 满足它的正确
    做法是让这个函数**明确地是同步的**，由路由层一次 `to_thread` 把它整个挪出事件循环。

    顺序是刻意的：先 `/version`（不可达就没必要再试五次、每次都等超时），
    再自省（同路径探测与 `api_attached` 都要它），最后才是那次真起容器的探测。
    """
    moment = now if now is not None else datetime.now(UTC)

    docker = _docker_section(probe)
    if docker.reachable:
        me, self_reason = _inspect_self(probe, self_container_ref)
        network = _network_section(probe, settings.strix_docker_sandbox_network, me, self_reason)
        sandbox_image = _sandbox_image_section(probe, settings.strix_image)
        orphans = _orphan_section(probe)
        data_dir = _data_dir_section(probe, settings.console_data_dir, me, self_reason)
    else:
        reason = docker.unknown_reason
        network = NetworkSection(
            name=settings.strix_docker_sandbox_network,
            present=None,
            api_attached=None,
            unknown_reason=reason,
        )
        sandbox_image = SandboxImageSection(
            reference=settings.strix_image, present=None, size_bytes=None, unknown_reason=reason
        )
        orphans = OrphanSandboxSection(count=None, names=[], unknown_reason=reason)
        data_dir = DataDirSection(
            path=str(settings.console_data_dir),
            identical_path_ok=None,
            unknown_reason=reason,
            probe_image=None,
            probe_container_name=None,
            probe_duration_ms=None,
        )

    telemetry = TelemetrySection(
        strix_telemetry=parse_strix_telemetry(settings.strix_telemetry),
        strix_no_update_check=parse_flag_enabled(settings.strix_no_update_check),
        raw_strix_telemetry=settings.strix_telemetry,
        raw_strix_no_update_check=settings.strix_no_update_check,
    )

    blockers = compute_blockers(
        docker=docker,
        network=network,
        data_dir=data_dir,
        telemetry=telemetry,
        sandbox_image=sandbox_image,
    )

    return SystemStatusResponse(
        ready_for_scan=not blockers,
        blockers=blockers,
        docker=docker,
        network=network,
        data_dir=data_dir,
        telemetry=telemetry,
        sandbox_image=sandbox_image,
        orphan_sandboxes=orphans,
        tls=collect_tls_section(settings.tls_cert_path, moment),
        extra_ca=collect_extra_ca_section(settings, Path(EXTRA_CA_MOUNT_PATH)),
        versions=VersionsSection(app_version=app_version, strix_version=strix_version),
        native_viewer_enabled=settings.console_enable_native_viewer,
        # 缀 `Z` 之前必须先确认它真是 UTC（pitfalls 条 37：`formatTime` 默认 localtime，
        # 缀 Z 就是说谎）。`moment` 来自 `datetime.now(UTC)` 或调用方显式给的 aware
        # 时间，`isoformat()` 会带 `+00:00`，替换成 `Z` 是等价改写。
        probed_at=moment.isoformat().replace("+00:00", "Z"),
    )
