"""进程配置 —— 从环境变量读，启动时一次性校验。

三条设计决定，后续任务请沿用：

1. **不读 `.env` 文件。** 只读 `os.environ`。`.env` 由 `docker compose` 负责插值成容器
   环境变量，那是它的职责。后端再去找 `.env` 会得到两条真源，并且引出"容器里哪个
   路径才是 `.env`"这种没有正确答案的问题。所以这里刻意**没有** `env_file=`。

2. **`SecretStr` 在本文件出现 0 次，这是结论而不是遗漏。**
   本进程的环境里根本不该存在任何凭据 —— 凭据只走 `POST /api/keys` 的 JSON body
   进内存 KeyVault（T7），再经子进程 env 交给 Strix。`assert_no_credential_env()`
   把这条从"约定"变成"启动断言"：环境里出现凭据就拒绝启动，而不是包上 `SecretStr`
   假装安全。用 `SecretStr` 包一个不该存在的值，只会让它合法化。

3. **不在这里放路径字面量。** `CONSOLE_DATA_DIR` 必填且无默认值。它是同路径挂载的
   宿主绝对路径，因部署机而异；写死任何默认值都会在某台机器上静默指向错误位置
   （见 CLAUDE.md §技术约束「同路径挂载」）。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# =============================================================================
# 凭据环境变量黑名单
#
# 按**精确变量名**匹配，不按子串。理由见 pitfalls 条 17b：按名字形状模糊匹配会误报
# （基础镜像里就有个 `GPG_KEY`，它是 Docker Hub 官方镜像的构建产物，不是凭据）。
# 这份名单是穷举的、可读的，加供应商时手工加一行 —— 比一个会误报的正则可靠。
#
# 为什么 STRIX_LLM 也在名单里（它不是凭据）：
#   它是**进程级**的模型选择。Strix 从 env 读它，一旦在 api 进程环境里存在，就会被
#   每个子进程继承，变成"所有用户共用同一个模型"的隐式默认 —— 而本项目的核心约束是
#   「LLM 可切换、谁用谁的 Key」。它必须由 ScanLauncher 每任务显式注入。
#
# 为什么 STRIX_DEBUG 也在：它把 strix.log 拉到 DEBUG，那是一个泄漏面
# （CLAUDE.md §Strix 集成）。它不是凭据，但后果同级。
# =============================================================================
FORBIDDEN_ENV_NAMES: frozenset[str] = frozenset(
    {
        # 单值形状（auth_shape=single）
        "LLM_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENROUTER_API_KEY",
        # Bedrock SigV4（三值）与 Bedrock bearer（PLAN.md §N1）
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_BEARER_TOKEN_BEDROCK",
        # 不是凭据，但同样必须逐任务注入 / 永不设置。理由见上方注释。
        "STRIX_LLM",
        "STRIX_DEBUG",
    }
)


class Settings(BaseSettings):
    """本进程的全部可配置项。

    `frozen=True`：配置是启动期事实，运行期改它会让"日志里的配置"与"实际行为"
    不一致。模块级也不缓存实例 —— 实例挂在 `app.state.settings` 上显式传递
    （CLAUDE.md §Python：模块级不得有可变全局状态）。
    """

    model_config = SettingsConfigDict(
        env_file=None,  # 见模块 docstring 第 1 条
        extra="ignore",  # 容器环境里有大量与我们无关的变量（PATH、STRIX_SANDBOX_*…）
        frozen=True,
    )

    # ---- 必填 ---------------------------------------------------------------
    # 由 docker-compose.yml 的 x-console-env 注入，值来自 .env 的 STRIX_HOST_DATA_DIR，
    # 且带 `:?` 守卫。这里无默认值 = 少了它启动就失败，而不是退化到某个错路径。
    console_data_dir: Path

    # ---- 有默认值 -----------------------------------------------------------
    # compose 里已显式设为 /run/strix（tmpfs 挂载点）。默认值只是给"手工起容器"兜底，
    # 与 compose 保持一致。
    console_ephemeral_home_root: Path = Path("/run/strix")

    # 一进程一次扫描是 Strix 侧的硬约束（模块级全局状态会跨用户污染 API Key），
    # 但我们走 subprocess，所以**并发多个子进程**是安全的。这个上限管的是机器资源：
    # 每个沙箱按 STRIX_SANDBOX_MEM_LIMIT 吃内存，默认 1 是小笔记本上的安全值。
    # 消费者：T9 ScanLauncher。
    console_max_concurrent_scans: int = Field(default=1, ge=1)

    # 预算硬顶。--max-budget-usd 强制必填，但"必填"防不住手抖多打一个 0。
    # 消费者：T6 护栏（budget_exceeds_ceiling）。
    console_max_budget_ceiling_usd: float = Field(default=100.0, gt=0)

    # 扫描产物过期自动删除的天数。**默认 0 = 永不自动删**：产物是渗透测试证据，
    # 销毁不可逆，而破坏性操作的默认值只能是"关"。
    # `ge=0` 而不是 `ge=1`，因为 `0` 不是"非法的天数"而是那个开关本身。
    # 消费者：T28 RetentionSweeper（`run_forever`）。
    console_retention_days: int = Field(default=0, ge=0)

    # Strix 自带 SPA 默认不代理（有邮箱门 + PostHog + 报告外发中继）。
    # 消费者：T3 /api/system/status、POST /api/system/native-viewer。
    console_enable_native_viewer: bool = False

    console_log_level: str = "INFO"

    # ---- 只读：由 compose 设置，我们不改，只用来自检与写进 scans 行 ----------
    # 它们的权威真源是 docker-compose.yml。这里读进来是为了让 /api/system/status
    # 能回答"我以为的配置是什么"，以及把 sandbox_image 记进扫描行。
    strix_image: str = ""
    strix_docker_sandbox_network: str = ""
    strix_telemetry: str = ""
    strix_no_update_check: str = ""
    # N2：操作者运行期挂载的企业 CA bundle 路径。空 = 未启用。绝不烧进镜像。
    #
    # ⚠️ 在 api 容器里这一项**总是空**：`STRIX_EXTRA_CA_FILE` 只出现在
    # docker-compose.yml 的 volumes 段（宿主侧路径），没有进 x-console-env。
    # 它留在这里是为了"手工起容器时也能自报"，但 `/api/system/status` 判定 N2
    # 是否启用**不看它**，看下面的 `ssl_cert_file` 与那个挂载点的真实形状 ——
    # 那两样是容器内可观测的事实（见 services/system_status.py）。
    strix_extra_ca_file: str = ""

    # 容器内真正生效的 CA bundle 路径。compose 设为
    # `${CONSOLE_CA_BUNDLE:-/etc/ssl/certs/ca-certificates.crt}`，也就是：
    # N2 启用时是 /etc/strix/extra-ca.pem，未启用时是镜像自带的系统 bundle。
    # 默认值给空串而不是那个系统路径：空串的含义是"没人告诉我"，而写死系统路径会让
    # "compose 忘了设它"看起来像"设成了默认值"。判定 N2 时会区分这两种情形。
    ssl_cert_file: str = ""

    @field_validator("console_log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"CONSOLE_LOG_LEVEL 必须是 {sorted(allowed)} 之一，收到 {v!r}")
        return upper

    @field_validator("console_data_dir", "console_ephemeral_home_root")
    @classmethod
    def _must_be_absolute(cls, v: Path) -> Path:
        # 相对路径在同路径挂载下是没有意义的：容器内外的 cwd 不同，
        # 同一个相对路径会指向两个地方 —— 正是我们要避免的路径别名 bug。
        if not v.is_absolute():
            raise ValueError(f"必须是绝对路径，收到 {v!r}")
        return v

    # ---- 派生路径。集中在这里，避免各处自己拼 -------------------------------
    @property
    def db_path(self) -> Path:
        return self.console_data_dir / "console.sqlite"

    @property
    def scans_dir(self) -> Path:
        return self.console_data_dir / "scans"

    @property
    def audit_dir(self) -> Path:
        return self.console_data_dir / "audit"

    @property
    def config_dir(self) -> Path:
        return self.console_data_dir / "config"

    @property
    def allowlist_path(self) -> Path:
        """目标白名单（T8）。**文件不存在是合法状态** —— 首次启动就是这样。

        为什么在 `config/` 而不是数据目录根：它是操作者手写、也可以经
        `PUT /api/allowlist` 改的**配置**，与 `console.sqlite`（运行期数据）分开放，
        备份和 review 的对象因此是一个目录而不是一堆散文件。
        """
        return self.config_dir / "allowlist.yaml"

    @property
    def auth_path(self) -> Path:
        """单账号登录的口令散列文件（0600），由 `./setup.sh` 的 C17f 创建。

        为什么在数据目录里而不是 SQLite 里：`password_hash` 这个列名会被
        `db.assert_no_secret_columns()` 拦下，而给那个断言开豁免等于承认它有例外。
        见 CLAUDE.md §安全不变式「单账号登录」。
        """
        return self.console_data_dir / "auth.json"

    @property
    def tls_cert_path(self) -> Path:
        """nginx 用的自签证书（`./setup.sh` 的 C18 签发，`nginx` 以 `:ro` 挂载）。

        api 进程能读到它，靠的正是同路径挂载：证书在 `${DATA}/tls/` 下，而
        `${DATA}` 在容器内外是同一个绝对路径。**只读它，绝不读 `key.pem`** ——
        私钥对本进程没有任何用途，而"能读到"和"会读"之间的距离就是一次
        `logger.debug(content)` 的距离。
        """
        return self.console_data_dir / "tls" / "cert.pem"

    @property
    def migrations_dir(self) -> Path:
        # 迁移 SQL 随包发布（Dockerfile 的 COPY app ./app 带上它），
        # 所以按本模块位置定位，而不是按 cwd。
        return Path(__file__).parent / "migrations"


def load_settings() -> Settings:
    """从 `os.environ` 构造 `Settings`。

    单独一个函数而不是模块级常量：模块级实例会在 import 时读环境，测试就没法在
    不重载模块的前提下换配置。
    """
    return Settings()  # pydantic-settings 自己读 os.environ


def assert_no_credential_env(environ: Mapping[str, str]) -> None:
    """环境里出现任何凭据变量就拒绝启动。

    取 `environ` 参数而不是直接读 `os.environ`：这样它是纯函数，好测（CLAUDE.md
    §Python：纯函数优先，好测是硬要求）。

    为什么是"拒绝启动"而不是"记一条警告并清掉":
      清掉治不了根因 —— 那个值已经在某个 compose 文件或 shell profile 里了，下次
      还会回来，而且我们无法知道它是否已经被别的进程读走。启动失败会逼人去改真正
      的源头。

    异常信息里**只有变量名，没有值**。这条很容易在"为了好排障"时被破坏。
    """
    found = sorted(name for name in FORBIDDEN_ENV_NAMES if name in environ)
    if found:
        raise RuntimeError(
            "api 进程环境里存在不允许的变量：" + ", ".join(found) + "。"
            "凭据只能经 POST /api/keys 进内存 KeyVault，绝不经进程环境；"
            "STRIX_LLM / STRIX_DEBUG 必须由 ScanLauncher 逐任务注入。"
            "请从 docker-compose.yml / .env / shell profile 中移除。"
        )
