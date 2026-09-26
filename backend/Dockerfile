# =============================================================================
# Strix Web 控制台 —— api 镜像
#
# 刻意不写 `# syntax=docker/dockerfile:1.x`：本文件只用到 多阶段 + FROM --platform
# + COPY --from，内置 dockerfile frontend 全部支持。加 syntax 指令会额外拉一个
# frontend 镜像（多一个联网依赖、多一个要 pin 的东西），换不到任何能力。
# 用到 `ADD --checksum` 时再加。
# =============================================================================

# -----------------------------------------------------------------------------
# 阶段 1：docker CLI 供体（只要 CLI，绝不要 dockerd）
#
# 为什么是 docker:<ver>-cli 而不是自己 curl 静态包：
#   docker-library/docker 的 cli Dockerfile 本身就是
#     wget https://download.docker.com/linux/static/stable/<arch>/docker-<ver>.tgz
#     tar --extract --file docker.tgz --strip-components 1 \
#         --directory /usr/local/bin/ 'docker/docker'
#   所以这里 COPY 出来的就是 Docker 官方静态构建的同一个二进制，只是下载与解包
#   已由官方镜像完成，并被 manifest-list digest 内容寻址。
#   收益：一个 digest 同时覆盖 arm64 与 amd64 —— 不需要维护 TARGETARCH → sha256
#   的映射表（download.docker.com 没有 .sha256 旁文件，实测 404），也不需要一个
#   装了 curl 的构建阶段。且宿主到该域名实测只有 ~45.8 KB/s，77 MB 拉不动。
#   详见 pitfalls/history-pitfalls.md 条 10。
#
# 版本选择：29.7.2 == 本机 daemon 版本，API 协商零风险。
# 只 COPY /usr/local/bin/docker，不要 cli-plugins（buildx/compose 我们不用），
# 也不要该镜像里的 git/openssh。
#
# 刻意**不写** `--platform=$TARGETPLATFORM`：它就是 FROM 的默认值，写出来 BuildKit 会
# 报 RedundantTargetPlatform 警告。而"这个二进制必须和最终镜像同架构"由默认行为本身
# 保证，用这行注释记下来就够了 —— 不值得为一句文档换一条每次构建都刷屏的警告。
# -----------------------------------------------------------------------------
FROM docker:29.7.2-cli@sha256:3f4743208d2338c934d7b8bcfbe1bb54c0b2355c510ad5e0f31c0c4a54bd704e \
     AS dockercli


# -----------------------------------------------------------------------------
# 阶段 2：运行镜像（生产交付物）
#
# ⚠️ 本阶段刻意**不是**最后一个阶段（后面还有 test）。因此 `docker build` 不带
#    `--target` 时构建的是 test 阶段。docker-compose.yml 的 api 服务**必须**写
#    `build: target: runtime`，否则生产容器里会带上 pytest 与 ruff。
#    这条不靠人记住 —— T30 的 verify-e2e 有一条断言：
#      docker run --rm strix-console/api:1.0.0 python -c "import pytest"  必须失败
#
# 为什么 Python 依赖是单阶段、不做 builder/venv 拆分：
#   全部依赖走 --only-binary=:all:，构建期不需要任何编译器或 -dev 头文件，
#   因此单阶段里没有"要丢掉的构建产物"。加一个 venv 复制阶段只会：
#   ①让 --require-hashes 的审计链多一跳；②让 site-packages 的位置偏离官方镜像
#   约定（验收命令要写死非常规路径）；③省下 0 字节。
#
# 基础镜像按 digest pin：本项目的立场是"版本升级必须是显式动作"（PLAN.md R4）。
#   digest 顺带把 pip 自身版本也钉住了 —— 这很重要，因为我们不升级 pip
#   （升级 pip 本身是一次无哈希安装，会在 --require-hashes 的审计链上开个洞）。
#   代价：基础镜像的安全更新需要手动 bump digest。已裁定接受。
# -----------------------------------------------------------------------------
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea \
     AS runtime

# PYTHONUNBUFFERED：子进程 stdout/stderr 必须实时可读（ScanSupervisor 靠它抓 Rich 面板）
# PIP_ONLY_BINARY=:all:：把"绝不回退 sdist"从命令行参数升级成镜像级**默认值**。
#   ⚠️ 它是默认值，**不是强制门** —— 已实测 `pip download --no-binary=:all:` 能盖掉它并
#   照样拿到 sdist（命令行优先于环境变量，见 pitfalls 条 17）。所以它挡的是"手滑/隐式
#   回退"，挡不住刻意绕过的人。真正的门是第 94 行安装命令上**显式**写的
#   `--only-binary=:all:`，那一条不可被环境影响。
#   ⚠️ 副作用：会让 `pip install -e .` 这类本地源码安装失败。这是刻意的 ——
#   本项目的 app 是 `uvicorn app.main:app` 直接跑源码，从不 pip install 自己。
#   T2 加 web 栈时若某依赖只发 sdist，报错会指向这个 ENV 而不是那个包，别乱改。
# 刻意不设 PYTHONDONTWRITEBYTECODE：strix 会 import litellm（冷启动很贵），
#   让 pip 在构建期把 .pyc 烧进镜像层，每个扫描子进程都能吃到热字节码。
ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ONLY_BINARY=:all:

# -----------------------------------------------------------------------------
# docker CLI 落地 + 构建期断言
#
# `RUN docker --version` 是本文件里最重要的一行断言：供体镜像是 alpine(musl)，
# 最终镜像是 debian(glibc)。Docker 官方 static 通道的二进制是静态链接的（这也是
# 它能在 alpine 上跑的原因），但这一点未经独立核实。放这一行，是让"静态链接假设
# 不成立"在 build 时炸，而不是在第一次扫描调 Reaper 时才炸。
# -----------------------------------------------------------------------------
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
RUN docker --version

WORKDIR /app

# -----------------------------------------------------------------------------
# 依赖安装
#
# lock 文件留在镜像里是为了可审计：验收命令直接读 /app/requirements.lock，
# 不必信任构建上下文。
#
# ⚠️ 这里刻意**只** COPY requirements.lock，不带 pyproject.toml：安装完全由 lock 决定，
#   pyproject.toml 只是 ruff/pytest 的配置来源（test 阶段要用）。把它一起 COPY 进来
#   会让"调一行 lint 规则"作废这一层，重跑 6 分钟的 pip install。已实测踩过一次。
#
# --require-hashes：有哈希时 pip 本会隐式开启，显式写出来是为了让"lock 里漏了
#   任何一行哈希"变成硬失败而不是静默降级。
# --only-binary=:all:：即使 lock 里含 sdist 的哈希（scripts/gen_lock.py 一定会写进去），
#   pip 也永不选它 —— 这挡的是 sdist 的 hatch 构建钩子缺 Go 1.24 会硬失败。
#   哈希锁负责"装的是不是这个文件"，--only-binary 负责"装的是哪一类文件"，
#   两条正交，都要。详见 pitfalls/history-pitfalls.md 条 4。
# 不加 --upgrade、不 pip install --upgrade pip：见上文 digest pin 的理由。
# -----------------------------------------------------------------------------
COPY requirements.lock ./
RUN pip install --require-hashes --only-binary=:all: -r requirements.lock

# 构建期版本断言：把 R4 的"精确 pin"从约定变成不可绕过的构建门。
# 用 importlib.metadata 而不是解析 `pip show` 文本输出（后者格式不受契约保护）。
RUN python -c "import importlib.metadata as m; v=m.version('strix-agent'); \
assert v == '1.6.2', f'strix-agent 必须精确为 1.6.2，实测 {v}'; print('strix-agent', v)"

# -----------------------------------------------------------------------------
# 刻意不做的三件事
#
# 1) 不建非 root USER：compose 强制 user: "0:0"（要写 docker.sock，而 Docker
#    Desktop 下 socket 属主不可移植）。镜像里声明一个随后被覆盖的 USER 只会
#    误导读者以为跑的是非 root。
# 2) 不装 git：v1 只支持 URL/域名/IP 目标，不克隆仓库；且 strix-agent 的依赖
#    里没有 GitPython（已核实 requires_dist）。装了就是多一个攻击面。
# 3) 不装 curl/wget 给 HEALTHCHECK 用：镜像里已经有 python，探针用标准库
#    urllib 就够了（见下方 HEALTHCHECK）。为一条探针装一个网络客户端是纯攻击面。
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# 应用源码
#
# 放在依赖安装**之后**：源码几乎每次都变、依赖几乎从不变。反过来写会让每次改一行
# 代码都重跑那 6 分钟的 pip install（本机 PyPI 直连约 250 KB/s）。
#
# 只 COPY app，不 COPY tests：生产镜像里不该有测试代码。tests 只进下面的 test 阶段。
# app/migrations/*.sql 随包一起进来 —— db.migrate() 按 __file__ 定位它们，不看 cwd。
#
# pyproject.toml 放在这里而不是依赖安装之前：见上文。它在运行镜像里的用途是可审计
# （能对着看装的是哪一版 strix-agent），在 test 阶段的用途是 ruff / pytest 的配置。
# -----------------------------------------------------------------------------
COPY pyproject.toml ./
COPY app ./app

# 构建期断言：迁移目录不能空。
# 一个 .dockerignore 规则手滑（比如加了 `*.sql`）会让镜像构建成功、启动时才报
# "没有任何 .sql 迁移文件"。在这里炸掉，成本从"部署后"降到"构建时"。
RUN test -f /app/app/migrations/001_init.sql

# HEALTHCHECK 刻意只打 /api/health，而那个端点刻意**不查任何依赖**
# （不探 docker、不碰 DB）。理由：docker daemon 抖一下就让容器 unhealthy，
# 而 unhealthy → 重启 api → 清空内存 KeyVault → 所有人的凭据丢失。
# 一个"更全面"的健康检查会**制造**故障。依赖自检是 /api/system/status（T3），
# 由人主动触发。
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request as u; \
r = u.urlopen('http://127.0.0.1:8000/api/health', timeout=4); \
exit(0 if r.status == 200 else 1)"

# --workers 1 是硬约束：KeyVault 是进程内 dict，多 worker 会随机 404。
# --no-access-log：访问日志会记 URL，而 URL 是一个泄漏面（CLAUDE.md §日志）。
#   我们自己的结构化日志由 app/logging_setup.py 负责，且带脱敏。
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--no-access-log"]


# -----------------------------------------------------------------------------
# 阶段 3：测试镜像（`make test` / `make lint` 用，**绝不发布**）
#
# 为什么必须有这个阶段：宿主 Python 只有 3.9.6，没有 brew/pipx/uv，而后端代码要求
# 3.12 —— 本机根本跑不了 pytest。"跑测试"在本项目里就等于"构建这个阶段再
# docker run --rm"。
#
# 为什么继承 runtime 而不是从基础镜像重来：这样测的**就是**要发布的那套依赖，
# 而不是一套碰巧版本相近的。同时复用 runtime 的层，只多下 pytest/ruff 两个 wheel。
#
# requirements-dev.lock 是运行时依赖的**全集** + pytest/ruff。看着冗余，实则是
# 一条断言：--require-hashes 会逐个核对已装包的版本，于是"dev 解析偷偷动了某个
# 运行时包的版本"会在这里当场失败。Makefile 的 lock-dev 另有一道 pin 比对。
# -----------------------------------------------------------------------------
FROM runtime AS test

COPY requirements-dev.lock ./
RUN pip install --require-hashes --only-binary=:all: -r requirements-dev.lock

COPY tests ./tests

# 单测禁止真实网络与真实 Docker（CLAUDE.md §测试）。这里没有技术强制手段
# （刻意没引 pytest-socket 之类的依赖），靠 review 与夹具保证。
CMD ["pytest"]
