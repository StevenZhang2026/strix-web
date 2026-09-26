# 运维手册

面向部署并使用本控制台的人。所有 `docker compose` 命令都带 `-p strix-console`：
这台机器上可能还有别的 compose 项目，不带项目名的命令会作用到错误的对象上。

---

## 1. 安装：`./setup.sh` 做了什么

`./setup.sh` 可以反复运行，已有的东西**不会被静默覆盖**。它依次：

1. **校验环境**：工具链、操作系统（macOS / Linux，且 `docker info` 显示为 Docker Desktop，否则阻断）、
   数据目录 `STRIX_HOST_DATA_DIR`（绝对路径、不含冒号、不含符号链接、不在 `/tmp` 等会被清空的位置、
   不在本仓库内），并**真跑一次** `docker run -v "$D:$D"` 确认同路径挂载成立。
2. **校验资源并算出沙箱限额**（见第 2 节），写进 `.env`。
3. **检查对外端口**（`CONSOLE_WEB_PORT`，默认 `443`）是否被占用 —— 只查不占，不会去停任何进程。
4. **签发自签 TLS 证书**到 `${STRIX_HOST_DATA_DIR}/tls/`：单张自签，带 SAN 与 EKU=serverAuth，私钥 0600。
   已有且合法的证书原样保留（覆盖会让你已导入的信任失效）；已有但不可用（过期、缺 SAN/EKU、私钥不配对）则阻断。
5. **可选的企业 CA bundle**（`STRIX_EXTRA_CA_FILE`，默认关闭，见第 9 节）。
6. **创建控制台登录账号**：不存在时交互式创建（用户名 + 口令输两遍），存到 `${STRIX_HOST_DATA_DIR}/auth.json`；
   已存在则不改也不问；损坏则阻断并提示手动删除后重跑。`api` 在缺少 `auth.json` 时拒绝启动。
7. **扫描已有 `.env` 的密钥卫生**：命中疑似密钥字段即阻断。`.env` 里永远不放 API Key。
8. **写出 `.env`**，并在结尾打印信任证书的命令（脚本不会替你改系统信任库）。

`.env` 各项的含义见仓库根的 `env.example`。**不要手抄它**，真实的 `.env` 由 `setup.sh` 生成。

## 2. 主机要求

门槛常量在 `setup.sh` 顶部的 `REQ_*` / `REC_*`，与本文不一致时以脚本为准。

| 资源 | 阻断线 | 推荐值 | 理由 |
|---|---|---|---|
| Docker VM 内存 | 4 GB | 8 GB | 单个沙箱下限 2048 MB（浏览器 + 抓包代理 + 各类扫描器同时在跑）+ api 容器约 512 MB + VM 开销。低于此线沙箱会被 OOM kill，而报错会指向"agent 崩了"而不是"内存不够"，极难归因 |
| Docker VM CPU | 2 核 | 4 核 | 沙箱下限 2 核。VM 恰好 2 核时沙箱吃满，扫描期间界面会卡 |
| 数据目录可用空间 | 10 GB | 20 GB | 镜像合计约 8 GB（沙箱镜像是大头）；单次扫描产物量级很小 |
| 宿主物理内存 | — | VM 配额 + 2 GB | 浏览器与 Docker Desktop 本体跑在 VM 之外 |

**"Docker VM 内存"是 `docker info` 的 `MemTotal`，不是宿主物理内存**，所有限额的分母都是它。

沙箱限额**按主机算出写进 `.env`，不要手填**：内存与 CPU 各取 Docker VM 配额的一半，分别不低于
2048 MB / 2 核；`/dev/shm` 取沙箱内存的四分之一、上限 1 GB。对应变量是 `STRIX_SANDBOX_MEM_LIMIT`、
`STRIX_SANDBOX_CPUS`、`STRIX_SANDBOX_SHM_SIZE`、`STRIX_SANDBOX_PIDS_LIMIT`。
调整了 Docker Desktop 的资源配额后，重跑 `./setup.sh`。

## 3. 启停与升级

```sh
docker compose -p strix-console up -d api web nginx     # 启动
docker compose -p strix-console down                    # 停止
```

- `api` 停机时会先给运行中的扫描一个优雅收尾窗口（`stop_grace_period` 为 45s）。停机后正在跑的扫描会结束，
  **内存里的 API Key 随进程消失**，重启后要在网页里重新输入。
- 升级本控制台：拉取新代码后重建并重启 `docker compose -p strix-console build api` →
  `docker compose -p strix-console up -d api web nginx`。
- 升级 Strix 本身见第 10 节。

## 4. 日志在哪

| 日志 | 位置 |
|---|---|
| 控制台后端 | `docker compose -p strix-console logs api`（结构化 JSON，输出到容器 stdout；凭据在写出前脱敏） |
| nginx | `docker compose -p strix-console logs nginx` |
| 单次扫描的 Strix 日志 | `${STRIX_HOST_DATA_DIR}/scans/<scan_id>/strix_runs/<运行名>/strix.log` |
| 审计 | 数据库 `audit_log` 表 + `${STRIX_HOST_DATA_DIR}/audit/YYYY-MM.ndjson`（双写；数据库丢了也在，可 grep） |

注意：nginx 的 error log 会打出完整请求行（含 query string），这个格式不可配置。

## 5. 数据目录布局

`STRIX_HOST_DATA_DIR` 是宿主上的一个绝对路径，**以同一路径**挂进 `api` 容器
（`${STRIX_HOST_DATA_DIR}:${STRIX_HOST_DATA_DIR}`）：

```
${STRIX_HOST_DATA_DIR}/
  console.sqlite          # 控制台数据库
  auth.json               # 登录账号（scrypt 散列，0600）
  config/allowlist.yaml   # 目标白名单
  tls/cert.pem, key.pem   # 自签证书（私钥 0600，绝不要复制或分享）
  audit/YYYY-MM.ndjson    # 审计
  scans/<scan_id>/        # 每次扫描的工作目录，Strix 产物在其下 strix_runs/<运行名>/
```

**为什么必须同路径挂载、不能用 named volume**：Strix 在 `api` 容器里通过 docker.sock 创建**兄弟**沙箱容器，
挂载路径是按宿主路径传给 Docker daemon 的。容器内外路径必须字面一致，否则沙箱挂到的是另一个位置。
named volume 的宿主路径在 Docker Desktop 的 VM 内，两侧不一致，会**静默**出现路径错位。
所以数据目录也不能放进仓库、不能含符号链接。

## 6. 留存清理（默认关闭）

扫描产物是渗透测试证据，删了不可逆，所以**默认永不自动删除**（`CONSOLE_RETENTION_DAYS=0`）。

开启：在 `.env` 里写 `CONSOLE_RETENTION_DAYS=30`（天数），重启 `api`。之后 `api` 启动时清一轮、
每 24 小时一轮，删除"已终态且结束超过该天数"的扫描：

- **删除**：`${STRIX_HOST_DATA_DIR}/scans/<scan_id>/` 整个目录，以及该扫描在各明细表里的行
  （事件、截图、子任务、发现、报告译文）。
- **永远保留**：扫描列表里的那一行摘要（结论计数、成本仍可见）、授权记录、审计表与 `audit/*.ndjson`。
  每删一次扫描会多一条 `scan.purged` 审计。
- 每轮先把将删清单打进日志再动手。产物被清理后，详情页会显示"产物已按留存策略清理"。

## 7. 断点续扫的能力边界

扫描因费用上限等原因中途停止时，可以提高费用上限后继续这次扫描。续跑能做到：

- 抬高预算、带着记忆恢复主任务、注入新的指令；
- **让被提前停掉的子任务接着跑**。

续跑**不保证补全覆盖**：子任务复活之后，缺口是否真被补测并记进覆盖记录，**尚未验证**。
补没补齐看这次的覆盖记录 —— 仍有没测完的部分就继续标为「结论不完整」（`coverage_incomplete`）。

子任务复活依赖直接改写 Strix 运行目录里的 `.state/agents.json`，这不是 Strix 的公开接口。
升级 `strix-agent` 时 `backend/tests/test_agent_checkpoint.py` 是这一能力的预警线。

## 8. 清理沙箱容器

`api` 会自动回收泄漏的沙箱容器（启动时、定时、每次扫描结束后各一次），正在运行的扫描的沙箱不会被动。
一般不需要手动清理。

如需手动检查，**只许按 label 过滤**，且必须同时满足两个条件：`label=strix-run-type=console`
**且** `strix-run-id` 非空。先列出、确认，再删：

```sh
# 1. 先看（dry-run）：只列出带 run-type=console 的容器及其 run-id
docker ps -a --filter label=strix-run-type=console \
  --format '{{.ID}}  run-id={{.Label "strix-run-id"}}  {{.Names}}'

# 2. 逐个确认 run-id 非空、且不是正在进行的扫描后，按 ID 删除
docker rm -f <容器ID>
```

- run-id 为空的容器**不是** Strix 沙箱（Strix 结构上不会产出这种容器），不要删。
- **不要**按名字或创建时间筛选，**绝不要**用 `docker system prune`、`docker volume prune`、
  `docker network prune` 之类的批量清理 —— 它们会波及这台机器上与本控制台无关的容器、卷和网络。
- 网络 `strix_sandbox` 是固定的全局名称，删除前先 `docker network inspect strix_sandbox` 确认它只被本控制台使用。

## 9. 企业 TLS 解密环境：`STRIX_EXTRA_CA_FILE`

若你的网络做 TLS 解密（企业安全网关一类），容器里没有那张企业根 CA，连模型服务会在 TLS 握手阶段失败，
与凭据无关，界面上的错误码是 `llm_tls_intercepted`。

开启：在 `.env` 里写 `STRIX_EXTRA_CA_FILE=<宿主上 PEM 文件的绝对路径>`（可含多张证书），然后**重跑 `./setup.sh`**。
脚本会校验它、真挂进容器读一次，以只读方式挂到 `api` 的 `/etc/strix/extra-ca.pem`。
关闭：`STRIX_EXTRA_CA_FILE= ./setup.sh`。派生变量 `CONSOLE_CA_BUNDLE` 由脚本算出，不要手改。

- **开启即意味着接受：凭据与全部 LLM 流量会明文经过那台解密设备一次。** `GET /api/system/status` 会报出当前是否启用了额外 CA。
- 只允许运行期挂载。**永远不要把任何 CA 烧进镜像**（那等于把一张能签任意域名的证书焊进交付物）。

## 10. Strix 版本升级（简版）

策略：**跟 minor、不追 patch**；新 minor 发布后等 7–10 天再升，除非有 CVE（`x.y.1` 经常不稳）。
升级必须是**一个单独的 commit**，不与任何业务改动混 —— 有 pin + hash lock，回滚就是 `git revert` + 重建镜像。
任一阶段失败就停在那里。

1. **判定**（不动仓库）：看上游 release notes；新版**必须**同时有 manylinux aarch64 与 x86_64 的 wheel，缺则不升。
2. **只读勘查**（不改 pin、不动 lock）：一次性容器装新版、跑契约测试，看哪些耦合点碎了：
   ```sh
   docker run --rm -v $PWD/backend:/src:ro python:3.12-slim sh -c \
     'pip install --only-binary=:all: strix-agent==<新版> pytest && pytest /src/tests/test_strix_contract.py'
   ```
3. **落地**：改三处 pin（`backend/pyproject.toml`、`backend/Dockerfile` 里的版本断言、`scripts/check_lock.py`
   的 `REQUIRED_PINS`）→ `make lock` **和** `make lock-dev` 都跑 → **生产镜像与测试镜像都重建**
   （`docker compose -p strix-console build api` + `up -d api`；`make build-test`；只重建一个会让后续验证在旧版本上空跑）
   → 修复碎掉的代码 → `make lint` + `make test`（必须 0 skipped）。
4. **真跑复验**（这几类变了不报错、只静默给错答案，测试碰不到，一项都不能跳）：
   对一个你有权测试的靶场真跑一次扫描，确认 ① 抓包代理解析到的是沙箱容器 IP 而非 `127.0.0.1`；
   ② **成本估算不为 0**（`strix-agent` 或 `litellm` 升级都可能让预算护栏静默失效）；
   ③ 镜像环境变量与任务目录之外的磁盘上搜不到 `LLM_API_KEY` 的值。

**停止跟版的条件**：新版缺 manylinux aarch64/x86_64 wheel → 不升；上游删掉控制台依赖的实时视图接口 → 先评估代价；
`requires_python` 超出 3.12 → 先单独换基础镜像；**上游加了向第三方外发数据的默认行为且无法关闭 → 永久不升**。

## 11. 常见故障

**连不上模型**
- 界面报 `llm_tls_intercepted`：网络里有 TLS 解密设备，按第 9 节挂载企业根 CA，或换一个不做解密的网络。
- 界面报 `llm_connection_failed`：更具体的原因都已排除，确认这台机器能访问模型服务的地址后重试。
- 一个模型服务的域名能连通，推不出另一家也能连通 —— 解密设备可能只对部分端点生效。

**Chrome 拒绝证书 / 一直显示"不安全"**
- 没导入信任：按 README 第 3 步（或 `setup.sh` 结尾打印的命令）导入 `${STRIX_HOST_DATA_DIR}/tls/cert.pem`。
  Linux 上系统信任库与 Chrome 的 NSS 库两处都要导入。
- 证书缺 SAN（Chrome 报 `ERR_CERT_COMMON_NAME_INVALID`）或缺 EKU=serverAuth 会被直接拒连；
  `setup.sh` 会检出这类证书并阻断，按提示删掉旧证书后重跑即可重新签发（之后要重新导入信任）。
- 证书只对 `localhost` / `127.0.0.1` / `::1` 有效，用别的主机名访问会被拒，这是刻意的。
