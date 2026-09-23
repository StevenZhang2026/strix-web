# CLAUDE.md — Strix Web 控制台

全局调度规则（Plan 约束、任务拆分、三套派发模板、Skill/Superpowers 开关、人工干预边界）见：
@agent-rules.md

**`PLAN.md` 是设计的唯一权威**（事实出处 `file:line`、泄漏矩阵、里程碑、**T0–T31 派发清单**、28 条端到端验收）；与本文件冲突以它为准，并回头修本文件。
**`PLAN.md` 有 1000+ 行，禁止整读**：先 `grep -n "^## " PLAN.md` 看小节，再按行区间 `Read`（整读一次要在此后每一次调用上重复付费，`agent-rules.md` §九）。
本文件只放长期稳定、每轮都需要的项目事实，**硬上限 130 行且 10,500 字**（现 10,371）（行数管不住长行；本文件进**每一次**调用、**每一个**子 agent）—— 写满就往层二/层三下沉，不许硬塞。

## 规则分流（记录任何新规则/新坑之前先读这一节）

- **层一 常驻高频硬规则**（每轮都可能用到、跨模块、违反即出事）→ 本文件，与 `@agent-rules.md` 同为常驻
- **层二 低频/特定场景的坑**（做某类操作时才需要，且**已实测**）→ `pitfalls/history-pitfalls.md`，**不被 `@import`**，靠下方触发条件唤起 —— **没有触发条件的条目等于没写**
- **层三 只在改某模块时才用到的坑** → 该目录的 `CLAUDE.md`，读写该目录文件时自动加载

**层二的触发条件**（路牌：命中就去读原文，别拿这里的一句话代替它）：

- 动 `backend/Dockerfile` / `requirements.lock` / `docker-compose.yml` → 条 2、4、5、9、10、17；动 `setup.sh` 或挂载路径判定 → 条 1、2、5
- 动 `nginx.conf` / 证书 / compose 的可选 env 与挂载 → 条 27、28、30、31；动日志初始化 / Formatter / 接管 uvicorn logger → 条 33、37
- 动 `STRIX_LLM` 的值 / 换模型路由 / 升级 `strix-agent` 或 `litellm` → 条 22、23、38。**必须重验成本估算**，否则预算护栏静默变 0（1.6.2 的本地兜底对 Bedrock 名字**已经是断的**）
- 跑 `make lock` 或任何长耗时网络任务 → 条 10、11、12、15（**不许引 PyPI 镜像**）、16；跑 `make verify-e2e` / 起靶机 → 条 3
- 写凭据卫生断言（镜像 env / 文件系统 / 全盘扫描）或加新供应商 → 条 17b、20、25
- 写迁移执行器 / 配 ruff / 容器里跑 pytest / 写"命中黑名单就报错"的断言 → 条 32、34、35、36、39；写验收/自检脚本 → 条 23 末段 + 条 39（"检查都通过" ≠ "被检查的事真发生了"）
- 写带中文提示的 shell 脚本 → 条 14（`$VAR` 必须写成 `${VAR}`，否则 macOS bash 3.2 崩）；在 shell 里判断命令成败 / 查不可见字符 / 核查 Strix 源码 → 条 8、12、18、26、29
- 排查"连不上模型" / 设计错误码映射 → 条 19（哪家能用见下方环境表「出网」行）；展示扫描结论 / 做预算 UI → 条 24
- 要用户跑需要输入凭据的交互式脚本 → 条 21（**只给命令，不给逐项输入表**）；想读写 `.env*` 或被 deny 挡住 → 条 13
- 改本文件的 `@import` → 条 6；派发 subagent → 条 7；**拆任务粒度 / 复用子 agent 会话 → 条 40**；
  **写交底任务书 / 给子 agent 写闸门命令 / 收货读产物代码 / 查上次派发花在哪 → 条 41＋41b**
- 要导出企业根 CA / 动 `STRIX_EXTRA_CA_FILE`（N2）/ 跑任何 docker 清理命令 → 读 `pitfalls/local-env.md`（**未跟踪**，只在本机）

**层三的归属**：建模块时**同时**建该目录的 `CLAUDE.md`，把对应条目搬过去、只留一句结论（已搬 `frontend/`；T9 搬 `services/`，T13 搬 `strix_bridge/`）—— **代码写出来之前一律留在本文件**。

## 项目状态

在 Strix（Apache-2.0）之上做 Web 控制台：向导式发起扫描、实时进度可视化、中文人话报告、授权护栏。核心约束：**LLM 可切换、谁用谁的 Key、Key 绝不落盘。**

**开工先读 `PLAN.md` §交接**（进度、待放行的事、上次留下的问题）。进度与逐条验收记在 `PLAN.md`（派发清单对应行 + §里程碑），归因记在 `pitfalls` —— **本节不复述，不记日期/数量/退出码**。
**现在可以跑**（其余目标见 `Makefile`）：`./setup.sh`（校验环境、生成 `.env`、自签证书）、`make lock`、`make lint`（ruff + **eslint/tsc 跑在宿主 node 上**）、`make test`（**必须 0 skipped —— 不为 0 就说明有守卫没在跑**）、`docker compose -p strix-console up -d api web nginx` → `https://127.0.0.1/`（证书在 `${STRIX_HOST_DATA_DIR}/tls/cert.pem`）。`make verify-e2e` / `make reap` **待 T30**。
`lint-web` 依赖 `frontend/node_modules`，装它必须显式 `npm ci --registry=https://registry.npmjs.org/`（宿主 `~/.npmrc` 的镜像源被 TLS 解密，**不许动那个文件**）。
**`api` 缺 `${DATA}/auth.json` 就拒绝启动**（否则删掉它即绕过登录）—— 用 `./setup.sh` 的 C17f 建账号。
靶场 `m0-juice-shop` 已就绪（在 `strix_sandbox`，别名 `juice-shop`），重跑 M0 用 `./scripts/m0_probe.sh`：**目标 URL 必须写 `http://juice-shop:3000`**，`localhost:13000` 在沙箱内指向沙箱自己（已实测失败）。

## 编码哲学（Karpathy 风格）

1. **短绳原则** —— 小步、可读、可独立验证。看不懂的代码不许留下，"能跑就行"不是理由
2. **显式优于聪明** —— 宁可多写三行直白代码，不要一行要注释才读懂的技巧。魔法（元编程、隐式注册、装饰器链）需论证
3. **不要过早抽象** —— 重复第三次才提取；为"以后可能"预留的扩展点一律不写
4. **依赖是负债** —— 新依赖必须论证"标准库/现有依赖为什么做不到"。先例：手写 250 行 WordprocessingML 而不引 `python-docx`；不用 docker-socket-proxy（要的权限约等于全部，只换表演性安全）
5. **先端到端跑通，再优化** —— 垂直切片打穿全链路优先于任何一层的完备
6. **删代码是进步** —— 死代码、注释掉的代码、"以后可能用"的分支，见到就删
7. **不变量写进代码，不写进文档** —— 用 DB 约束、类型、启动断言（`scans.authorization_id NOT NULL` 让"误扫"结构上不可能；`assert_no_secret_columns()`）

## 技术约束（不可协商）：环境

| 项 | 事实 | 后果 |
|---|---|---|
| 支持范围与机型 | **macOS + Linux，都要求 Docker Desktop**（Windows 原生不支持：`C:\` 含冒号，同路径挂载不成立）；部署机规格**可变**，本开发机 Docker VM 仅 `11 核 / 7.75 GB` | `setup.sh` 按 `uname` 分支 + 检测 `docker info` 的 `Docker Desktop` 字样，非 Desktop 阻断；沙箱四个限额**必须参数化**（按 `MemTotal`/`NCPU` 算出写进 `.env`，不许硬编码）；阈值常量在 `setup.sh` 顶部 `REQ_*`/`REC_*`，**改门槛改脚本不改文档**（判据：**"VM 内存"是 `MemTotal`，不是宿主物理内存**）|
| **出网** | 企业防火墙**选择性**解密 LLM 端点，**只有 Bedrock 与 Gemini 可用**（清单在 `pitfalls` 条 19；"这个域名没被拦"推不出"那个也没被拦"）；PyPI 直连慢且会断流（条 10、11、15）| 开发与 M0 走 Bedrock；产品侧留"运行期挂载 CA bundle"的口子（`PLAN.md` §N2）**绝不烧进镜像**；装依赖要能重试、绝不进构建期 `curl` |
| 宿主与版本 | Python `3.9.6`、无 brew/pipx/uv（Strix 要求 `>=3.12`）；Docker Desktop `29.7.2` / Compose `v5.4.0`；Node `v24.19.0`；`arm64` 但**不得假定** | **后端必须容器化**（`python:3.12-slim`），本机不跑 Python 业务代码、hash lock 只能在一次性 Linux 容器里生成（**不写** `--platform`，须含 aarch64 与 x86_64，权威在 `requirements.lock`）；镜像按 manifest-list digest pin；DooD 挂 docker.sock，Strix 创建**兄弟**容器；前端 Next.js 15 + React 19 |

## Strix 集成（不可协商；每条都已读源码核实，出处见 `PLAN.md`）

- **`strix-agent==1.6.2` 精确 pin** + `--only-binary=:all:` + hash lock（**不许放宽**成 `>=`／`~=`：解析期会为拿元数据去构建 sdist，hatch 钩子缺 Go 1.24 硬失败）。**升级是允许的：跟 minor、不追 patch**，走 `PLAN.md` §Strix 版本升级 六阶段 runbook、**单独一个 commit**；`test_strix_contract.py` 是预警线
- **一进程一次扫描。** 模块级全局可变状态 + `configure_sdk_model_defaults` 改 `os.environ` → 同进程并发会**跨用户污染 API Key**。所以走 `subprocess` CLI，**不内嵌** `run_strix_scan()`
- **模型只能经 `STRIX_LLM` env 注入**（CLI 无 `--model`）；产物固定写 `$CWD/strix_runs/<自动名>/`（无 `--output-dir`）→ 每任务独立 cwd
- **退出码 `2` = 发现漏洞，必须当成功**；`0` 正常，`1` 失败。**但 `0` 不代表跑完了** —— **必须读 `run.json.status`**，`stopped` 一律「结论不完整」（`scan_incomplete`）；只凭退出码 0 报"未发现漏洞"是发布阻断项（条 24）。`1` 的归因**看 stdout 正文的异常类名，面板标题只兜底**（条 19）
- **`STRIX_DOCKER_SANDBOX_NETWORK=strix_sandbox` 必须设**，且 `api` 也要加入该网络 —— 不设则 Caido 端口解析成 `127.0.0.1`（在后端容器里指向它自己），抓包代理**静默降级**
- **`STRIX_RUN_ID=<scan_id>` 必须设** —— SIGTERM 会跳过 `session_manager.cleanup` 而**泄漏沙箱容器**，只能靠 label 回收
- **同路径挂载 `${STRIX_HOST_DATA_DIR}:${STRIX_HOST_DATA_DIR}`，绝不用 named volume** —— 它的宿主路径在 VM 内、两侧不一致，会静默重现路径别名 bug。`TMPDIR` 也指到该卷下
- **`agents.db` 不是只追加的** —— 上下文压缩与图片淘汰会 `clear_session()` 后重插、id 重排 → 朴素游标增量必错，要 epoch + 重同步检测 + 自己的只追加镜像
- **截图只保留最近 3 张**（内联 data URL）→ **首次见到就落地**到 `media/<sha256>.png`
- **import 边界（import-linter 强制）**：web 进程只许 import `strix.interface.tui.backend.live_view`（**子类**才有 10k FIFO 上界与 `event_snapshot()`；游标 API 反而用不上）与 `strix.interface.viewer.transcript`，**禁止** `strix.core.*` / `strix.runtime.*`；全部收口在 `backend/app/strix_bridge/`
- **必设** `STRIX_TELEMETRY=false`、`STRIX_NO_UPDATE_CHECK=1`、`LITELLM_LOG=ERROR`；**绝不设 `STRIX_DEBUG`**（把 `strix.log` 拉到 DEBUG，是泄漏面）
- **`api` 必须 `--workers 1`**（KeyVault 是进程内 dict，多 worker 会随机 404），启动时校验 `WEB_CONCURRENCY`

## 安全不变式（违反即发布阻断）

- **任何表都不得有 Key / secret / token / password 列。** 启动 `db.assert_no_secret_columns()`，CI 有对应测试
- **Key 绝不进 argv / query / path / 日志 / DB / compose / `.env` / 镜像**，只经 JSON POST body 到内存与子进程 env
- 每任务 `HOME=<tmpfs>/scan-<id>/home` **且**显式 `--config <同目录>/cli-config.json`（预置 `{"env":{}}`）；tmpfs `/run/strix` 为 `noexec,nosuid,size=16m,mode=0700`；`finally` 里 `rmtree`。**这三样对 `single` 形状是唯一防线，不是纵深防御**（`persist_current()` 把 `LLM_API_KEY` 明文写进该文件，条 25）
- 全程 `SecretStr`；脱敏挂在 **root handler 的 Formatter** 上（正则 + 每个活跃凭据里**每一个值**的精确子串 —— 一个 handle 可能装 2–3 个值，`PLAN.md` §N1）。**挂 root logger 是静默无效的**，uvicorn 的三个 logger 要单独收口（已实测，泄漏矩阵 #3）
- 前端只把 opaque `vault_handle` 存 **`sessionStorage`**；不用 localStorage、不进 cookie、不进 URL
- **单账号登录**（T4b/T5b，`PLAN.md` §单账号登录）：`scrypt` + 每用户随机 salt 存 `${DATA}/auth.json`(0600)，**不进 SQLite、不给 `assert_no_secret_columns()` 开豁免**；会话是内存里的不透明 id + `HttpOnly; Secure; SameSite=Strict` cookie，**不用 JWT**；全局路由依赖只豁免 `/api/health`。**不做多用户、不加 owner 列**
- **永远不许装 `CORSMiddleware`。** 挡住"任意网页跨域打我们 API"的是「没有 CORS」+「FastAPI 只把 `application/json` 喂给 Pydantic」+「证书 SAN 只含 localhost」；`allow_origins=["*"]` 一次作废前两条（判据表见 `PLAN.md` §单账号登录 理由三）
- `scans.authorization_id` **NOT NULL**；启动前重解析 DNS 与声明时比对，不一致即拒。`--max-budget-usd` **强制必填**，缺它 `ScanLauncher` 拒绝构造 argv
- 云元数据地址（`169.254.169.254`、`metadata.google.internal`、`100.100.100.200` 等）**永久硬拦、不可覆盖**
- **对外只经 `nginx` 终止 TLS**，绑 `127.0.0.1:443`（不监听 80）；`api`/`web` 一律只 `expose`。证书是 `openssl` 单张自签，**必须带 SAN + EKU=serverAuth**（缺则 Chrome 拒连）；信任由用户手动 `security add-trusted-cert`，脚本不得自动改信任库
- **已知且接受的残余风险**（三条都如实写进 `docs/SECURITY-zh.md`）：子进程 `/proc/<pid>/environ` 可见 Key；挂 docker.sock ≈ 宿主 root；`nginx → api` 走 Docker 桥网**明文**（有 docker.sock 权限者可被动抓 Key）。缓解只有上一条 + `no-new-privileges`

## 全局编码约定

### Python

- 3.12 / `ruff` + `ruff format`，行宽 100；`from __future__ import annotations`；**完整类型标注**，`Any` 需注释理由；文件名 `snake_case`，一个 service 一个模块，只有真正持有状态才用 class
- 边界层（HTTP 出入参、配置、白名单 YAML）一律 Pydantic v2；内部传参用 `dataclass`，不传裸 `dict`
- **async 优先**，async 函数里**禁止**同步阻塞 IO（走 `asyncio.to_thread`）；**禁止 bare `except`**，捕获必须指明类型并处理或重抛，业务错误用自定义异常 + **稳定机器码**
- 判定逻辑（如 `target_guard`）写成无 IO 的纯函数（好测是硬要求，不是加分项）；模块级**不得**有可变全局状态（Strix 的教训），状态挂在显式传递的对象上

### TypeScript

- `strict: true`、**禁止 `any`**；App Router + 服务端组件优先；样式只用 CSS Modules + `globals.css` 的 token 层（**不引 Tailwind / `clsx` / `cva`**）；文案全进 `frontend/messages/zh-CN.json`，组件里**不得**出现中文字面量
- **细则（eslint 封死的 `next/headers` 与 `sessionStorage` 两条边界、状态选型、Key 输入框）见 `frontend/CLAUDE.md`**

### 错误与文案

- 每个错误有**稳定机器码**（`not_in_allowlist`、`dns_changed`、`key_required`…），前端按码分支、**不得**匹配文案；中文文案与机器码分离、集中维护；后端返回 `{code, trace_id}`，traceback 脱敏后才记
- **UI 与文档全中文；给 Strix 的 `--instruction` 正文用英文**（它的 system prompt 与 ~90 个 skill 都是英文）
- 报告翻译**绝不翻译** `poc_script_code` / `evidence` / `endpoint` / `code_locations`

### 测试与日志

- `pytest`；单测**禁止真实网络与真实 Docker**，用 `fixtures/run_dirs/` 夹具；每个模板一个**黄金 argv 测试**；每条安全不变式一个测试（`test_key_hygiene`、`test_no_secret_columns`、`test_strix_contract`）
- 压缩夹具（`STRIX_CONTEXT_BUFFER_TOKENS=1` + `STRIX_MAX_CONTEXT_IMAGES=1`）是 M4 交付物，**不是事后补**
- 日志结构化 JSON；生产 `--no-access-log`；脱敏过滤器在**推给前端和写镜像之前**执行；`audit_log` 表 + `${DATA}/audit/YYYY-MM.ndjson` 双写（DB 丢了也在、可 grep）

## 禁区

- **不许改** `PLAN.md` 里"已确认决策"表中的条目（用户拍过板，要改先问）；**不许**放宽 `strix-agent` 的 pin，也不许在**不走 runbook** 的情况下换它的版本（换版本要单独 commit，不与业务改动混）；**不许**把 `data` 目录放进仓库（同路径挂载要求它是 `.env` 指定的绝对宿主路径）
- **不许**引入向 `app.strix.ai` 或任何第三方外发数据的代码路径（这是本项目存在的理由之一）；**不许**代理 Strix 自带 SPA（有邮箱门 + PostHog + 报告外发中继），专家 tab 自己渲染
- **仓库是 public**：具名内网信息（解密设备归属、另一个 compose 项目名）与**用量/账单数字只许**留在未跟踪的 `pitfalls/local-env.md`，不许搬回被跟踪的文件；commit 身份是**仓库级** GitHub noreply 邮箱（刻意不用全局那个公司邮箱），别改回去
- **本机有另一个在用的 Docker compose 项目（6 个容器 + 端口 3000/3080/8000/5432/6379；项目名见 `pitfalls/local-env.md`，未跟踪）。**
  - **绝对禁止**任何无过滤的批量清理：`docker system prune`、`docker volume prune`、`docker image prune -a`、`docker network prune`、不带 `-p strix-console` 的 `docker compose down`
  - `make reap` **必须**按 `label=strix-run-type=console` **且 `strix-run-id` 非空**双条件过滤，先 `--dry-run` 打印再删；禁止按"名字像"或"时间早"删。**少了 run-id 那一半会删掉 M0 靶场**（理由与实测见 `local-env.md`）
  - `strix_sandbox` 是**无项目前缀的全局网络名**（Strix 要求字面匹配）。创建前先 `docker network inspect` 确认它不属于别人；删除时同理
