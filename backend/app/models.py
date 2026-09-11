"""HTTP 边界模型。

下游任务（T3/T6/T7/T9）的请求与响应模型都从 `BoundaryModel` 继承，
从而自动获得下面那两条配置。提前把 `ScanCreateRequest` 之类写在这里是
"为以后可能"预留（CLAUDE.md §编码哲学 3），不做。

内部传参用 `dataclass` 而不是裸 dict（CLAUDE.md §Python）。T3 的
`services/docker_probe.py` 里那几个 dataclass 就是这条约定的实例：**探测层**产出
dataclass，**边界层**是本文件的 Pydantic 模型。

# 关于 `SystemStatusResponse` 一族的"三态布尔"

它们的判定字段全是 `bool | None`，且每节都带一个 `unknown_reason`。
`None` 的含义是"没能探测"，**不是**"没问题"。这条不是风格偏好：一个把未探测
状态画成绿色的就绪面板，在真的不就绪时也照样说就绪 —— 那时它不只是没用，
而是把"去查一下"这个动作也一起省掉了。所以本文件里**没有任何一个判定字段有
默认值** —— 每个都必须由探测代码显式填。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class BoundaryModel(BaseModel):
    """所有 HTTP 出入参模型的基类。

    `extra="forbid"` 是一条**安全**配置，不只是严格性偏好：
    默认的 `extra="ignore"` 会让 `POST /api/scans {"vault_handle": "...",
    "api_key": "sk-ant-..."}` 静默通过 —— 那个 `api_key` 字段会被丢掉，
    但**它已经进过 HTTP 日志和请求体**了，而客户端会以为它被接受了。
    `forbid` 让这种请求变成 422，前端立刻知道自己发错了。
    §N1 的"多余的凭据键一律 400 unexpected_secret_key"是同一条思路在业务层的版本。

    `frozen=True`：请求模型被校验完就是事实，改它等于让"日志里记的入参"与"实际用的
    入参"不一致。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class HealthResponse(BoundaryModel):
    """`GET /api/health`。

    刻意**只有这三个字段**，且刻意**不查任何东西**（不探 docker、不查 DB、
    不做同路径自检）。它的语义是"这个进程还在、还能响应"，被 compose 的 healthcheck
    和 nginx 用。

    把依赖检查塞进 health 的后果很具体：docker daemon 抖一下，healthcheck 失败，
    compose 重启 `api` —— 而重启 `api` 会清空内存 KeyVault，把所有人的凭据弄丢。
    也就是说，一个"更全面"的健康检查会**制造**故障。
    真正的依赖自检是 `GET /api/system/status`（T3），它由人主动触发，失败也不重启谁。
    """

    status: str
    app_version: str
    strix_version: str


class ErrorResponse(BoundaryModel):
    """所有错误响应的统一形状，对应 `ConsoleError.to_payload()`。

    只有三个字段，前端按 `code` 分支、**绝不匹配文案**（CLAUDE.md §错误与文案）。
    这里没有 `message` 字段：中文文案在 `frontend/messages/zh-CN.json`（T5）。
    后端一旦提供了 message，前端就一定会有人直接把它显示出来，然后我们就同时维护
    两份文案，并且后端那份还会绕过脱敏。

    `params` 只给前端做文案插值（`{"target": "example.com"}`），
    **绝不放凭据、绝不放中文**。
    """

    code: str
    trace_id: str
    params: dict[str, str | int | float | bool | None]


# =============================================================================
# `GET /api/system/status`（T3）
#
# 字段名是**对外契约**：`PLAN.md` 的验收 #2 与前端首页的四个就绪点按名字读它们
# （`docker.reachable` / `network.present` / `network.api_attached` /
# `data_dir.identical_path_ok` / `telemetry.strix_telemetry` /
# `sandbox_image.present`）。改名要同时改 `PLAN.md`、验收脚本和前端 —— 别改。
# =============================================================================
class DockerSection(BoundaryModel):
    """docker daemon 是否可达。

    `reachable` 只有两个真值：`True`（`GET /version` 成功）与 `False`（失败）。
    它**没有** `None` —— "连不上"本身就是一个确定的结论，不是"未知"。
    失败的**原因**放 `unknown_reason`（`socket_missing` / `timeout` / …）。
    """

    reachable: bool
    unknown_reason: str | None
    server_version: str | None
    api_version: str | None


class NetworkSection(BoundaryModel):
    """`strix_sandbox` 网络存在吗？`api` 自己接进去了吗？

    两个字段必须**分开**：网络存在但 api 没接进去，是本项目最隐蔽的故障之一 ——
    Caido 端口会解析成 `127.0.0.1`（在 api 容器里指向它自己），抓包代理**静默降级**，
    扫描照样跑完、照样出报告，只是少了一整类证据。合成一个 `ok` 会把它藏起来。
    """

    name: str
    present: bool | None
    api_attached: bool | None
    unknown_reason: str | None


class DataDirSection(BoundaryModel):
    """同路径挂载的**主动**探测结果（`PLAN.md:832`）。

    `identical_path_ok` 为 `True` 的唯一来源是"真起了一个容器、真读到了哨兵、
    真把它写回来了"。没有任何静态检查能替代它：容器内外路径字符串一致（我们自己
    的配置）与两个路径指向同一个目录（Docker Desktop 的 File sharing 配置）是
    两件事，而路径别名 bug 恰好发生在前者成立、后者不成立的时候。
    """

    path: str
    identical_path_ok: bool | None
    unknown_reason: str | None
    probe_image: str | None
    probe_container_name: str | None
    probe_duration_ms: int | None


class TelemetrySection(BoundaryModel):
    """外联静默开关。

    `strix_telemetry` 是**生效值**而不是原始字符串：Strix 侧的默认值是
    `True`（`strix/config/settings.py` 的 `Field(default=True, alias="STRIX_TELEMETRY")`），
    所以**没设** = 遥测开着。`raw_strix_telemetry` 保留原始字符串，供人核对
    "我明明写了 false 怎么还是 true"（答案通常是 YAML 把它解析成了布尔再转成 `True`）。
    """

    strix_telemetry: bool
    strix_no_update_check: bool
    raw_strix_telemetry: str
    raw_strix_no_update_check: str


class SandboxImageSection(BoundaryModel):
    """沙箱镜像在本地吗。

    刻意**只查不拉**：拉一个 GB 级镜像是个几分钟的操作，不该由一次自检请求触发。
    `present=False` 的修法是让人自己 `docker pull`，文案里会写。
    """

    reference: str
    present: bool | None
    size_bytes: int | None
    unknown_reason: str | None


class OrphanSandboxSection(BoundaryModel):
    """残留的沙箱容器（`PLAN.md` R8）。

    判据是 `label=strix-run-type=console` **且** 带非空 `strix-run-id`。后半条不是
    多余的收紧：M0 靶场也带前一个 label（`PLAN.md:754`），只按它数会把靶场报成孤儿。
    出处与实测见 `docker_probe.ORPHAN_REQUIRED_LABEL`。

    **只报数，不删。** 删是 `make reap`（T11）的职责，而且那件事必须先 `--dry-run`
    再删（本机还跑着别的 compose 项目）。一个自检接口顺手删容器，是"读操作有副作用"
    的最坏形式。
    """

    count: int | None
    names: list[str]
    unknown_reason: str | None


class TlsSection(BoundaryModel):
    """证书事实。

    # `cert_trusted` 永远是 `None`，这是结论而不是 TODO

    `PLAN.md:939`（R15）要求报 `cert_trusted`。但"这张证书是否已进 macOS 钥匙串"
    是**宿主**的状态，而本进程在容器里 —— 钥匙串不在这里，`security(1)` 也不在这里。
    容器里能做的替代品（比如 `openssl verify`）验的是**容器**的信任库，跟浏览器
    会不会报红没有关系；把它填进 `cert_trusted` 就是拿一个不相关的事实冒充答案。
    所以这里如实给 `None` + `cert_trusted_reason`，由 `make verify-e2e`（T30，跑在
    宿主上）去回答那一问。
    """

    cert_path: str
    cert_present: bool
    cert_valid_now: bool | None
    days_remaining: int | None
    san_dns: list[str]
    san_ip: list[str]
    has_server_auth_eku: bool | None
    is_ca: bool | None
    cert_trusted: bool | None
    cert_trusted_reason: str
    unknown_reason: str | None


class ExtraCaSection(BoundaryModel):
    """N2 企业 CA（`PLAN.md:355`）—— 启用/未启用，以及**实际生效的根证书张数**。

    `trusted_root_count` 是这一节里最重要的字段。`docker-compose.yml` 记着一个已实测
    的静默故障：`SSL_CERT_FILE` 为空时 OpenSSL 加载 **0** 张根证书**且不报错**，
    容器内所有 TLS 静默失去信任链。同一个坑的另一半是：操作者挂进来的 PEM 若只含
    企业根而不含公共根，`SSL_CERT_FILE` 指过去之后**公共 TLS 全断**。两种情况都只有
    "数一下真的加载了几张"才看得见。
    """

    enabled: bool
    mounted_path: str
    mounted_looks_like_pem: bool | None
    bundle_path: str
    trusted_root_count: int | None
    unknown_reason: str | None


class VersionsSection(BoundaryModel):
    app_version: str
    strix_version: str


class SystemStatusResponse(BoundaryModel):
    """`GET /api/system/status` —— 人主动触发的依赖自检。

    与 `/api/health` 的分工见 `HealthResponse` 的 docstring：health 不查任何依赖
    （查了会在依赖抖动时重启 api，而重启会清空内存 KeyVault）；本接口查全部依赖，
    但**失败也不重启谁**。

    # 为什么它总是 200，即使一切都坏了

    一个"依赖不就绪就返回 503"的自检接口，在最需要它的时候会变成"前端只知道 503、
    不知道哪一项坏了"。诊断结果是**正文**，不是状态码。真正的错误（凭据不对、
    路由不存在）仍然走统一的 `{code, trace_id}`。

    `blockers` 是**机器码列表**，前端按码取文案、按序显示修复步骤。
    它刻意**不进** `errors.py` 的码表：那张表是"HTTP 错误响应的码"，而这些是
    "就绪状态的原因码"，混在一起会让前端不知道该用哪棵文案树
    （见 `test_message_coverage.py::test_two_code_trees_stay_disjoint` 的同一条理由）。
    """

    ready_for_scan: bool
    blockers: list[str]
    docker: DockerSection
    network: NetworkSection
    data_dir: DataDirSection
    telemetry: TelemetrySection
    sandbox_image: SandboxImageSection
    orphan_sandboxes: OrphanSandboxSection
    tls: TlsSection
    extra_ca: ExtraCaSection
    versions: VersionsSection
    native_viewer_enabled: bool
    probed_at: str
