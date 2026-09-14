# Strix Web 控制台 — 实施计划

## 交接（2026-09-14）

> **这一节每次交接整段覆盖，不累积历史。**只写"新会话开工前必须知道、又不在别处的事"。

**代码状态**：**T0–T9 已完成并全部提交**（最后一次是 `1a04e8e`「T9：ScanLauncher + 6 个模板 +
GET /api/scan-templates」）。T9 的落地内容、我自己做的 8 次 mutation 复验、以及那个"声明了却没有任何
一处强制"的真缺陷，全部记在 **T9 行**，本节不复述。
**工作区**：只有一个未跟踪文件 **`plan-t10.md`（T10 方案，待审）** 与未跟踪的 `pitfalls/local-env.md`
（在 `.gitignore` 里，别提交它）；`PLAN.md` 有本轮的两处纠正（见下）与 §Strix 版本升级 那一节未提交。
**除此之外干净 —— 2026-09-14 第二段会话只做评估、零代码改动。**
**闸门（2026-09-14 亲自跑过快闸门）**：**682 passed / 0 skipped / 13.8 秒**。命令（`agent-rules.md` §九.4
要的秒级闸门，**已实测，别改**）：

```
docker run --rm -v "$PWD/backend/app:/app/app:ro" -v "$PWD/backend/tests:/app/tests:ro" \
  -v "$PWD/frontend/messages:/messages:ro" -e CONSOLE_MESSAGES_JSON=/messages/zh-CN.json \
  strix-console/api-test:0.1.0 pytest
```

那个镜像里装的是**真的 `strix-agent==1.5.3`**，所以核 Strix 源码不用 clone：
`docker run --rm --entrypoint sh strix-console/api-test:0.1.0 -c 'python -c "import strix,pathlib;p=pathlib.Path(strix.__file__).parent;print((p/\"interface/cli.py\").read_text())"'`
—— T10 方案里那四段源码就是这么读出来的，比 `/tmp/strix_src`（早没了）可靠。

## 下一步：T10 方案待审（**代码侧唯一的待办**；另有 §Strix 版本升级 的 4 件待决）

方案在未跟踪的 **`plan-t10.md`**，照 `agent-rules.md` §四 模板 3 写的（主会话写方案、人审通过后
**另起一个 agent 只做实现**）。方案第 8 节已写好派发预算（≤60 次调用、第 6 次前开始写代码、不拆），
第 6 节 13 组测试每条都给了"能让它变红的生产代码改动"，第 9 节列了落地后要改 `PLAN.md` 的 6 处。

**等用户裁决 5 件事（D1–D5，方案 §5 有推荐与备选）**：
D1 启动断言放 `main.py`（推荐，代价是 `system_status` 两个"未配置"分支在生产变得不可达）；
D2 矩阵 #16 只做「正文永不回显」、口令喂 Redactor 推给 T12（推荐，因为 `LaunchPlan` 刻意不带凭据）；
D3 加 3 个归因码 `stopped_by_operator`/`interrupted_by_restart`/`scan_failed_unknown` 换来不变量
「`status != completed` ⟺ `error_code` 非空」（推荐；`scan_incomplete` 的文案写死了"费用或轮次先用完了"，
拿它解释"你点了停止"是撒谎）；D4 `${DATA}/scans/<id>/tmp` 归 T10 删、T11 与 T28 都不许碰（推荐）；
D5 顺手改 `scan.stopHint` 那句被源码证伪的「agent 会先把手里的发现写完再退出」（推荐）。

**已落进 `PLAN.md` §后端接口 的两处纠正**（读 1.5.3 源码得出，与 D1–D5 无关，属纯事实纠错）：
① 「`-15`/`-9`→已手动停止」是错的 —— Strix 自己装了信号处理器，收到 TERM 后**退出码是 1**；
② `run.json.status` 取值域多一个 **`interrupted`**（信号停止的终值），`stopped` 才是预算耗尽。
**方案里还发现一个原矩阵没有的泄漏面**（`error_message` 若是 stdout 摘录，操作者的测试账号口令会进 DB
——agent 拿它登录过，而它不在 KeyVault 里所以不在脱敏集合里）。解法是"`error_message` 一个字节都不来自
stdout"，残余部分记成矩阵 #17 归 T12。**这三条目前只写在 `plan-t10.md` 里，别把那个文件弄丢。**

## Strix 版本升级（2026-09-14 第二段会话评估；**零代码改动，4 件待决**）

**目标变更（用户口述，尚未落进 §已确认决策 —— 要改那张表得先放行）**：从「`1.5.3` 锁死、**不许**升级」
改成「**保持随时能升到最新**」。注意 pin 本身不变松：精确 pin + `--only-binary=:all:` + hash lock 全留
（它们买的是可复现构建，不是"永不升级"），变的是这条 pin 从**冻结**变成**主动推进**。
**明确不做多版本共存**（用户已否决）：N 版本 = N 套夹具 + N 次 M0 真跑 + N 份 Key 卫生复验，粗估 +15 工时
且每次发版重付，还破坏「一个镜像一个 pin」这个让整件事可验证的前提。

### 一、PyPI 实测事实（2026-09-14 直连 `pypi.org/pypi/strix-agent/json` 查得）

- **`1.5.3` 落后两个版本。最新是 `1.6.2`（2026-09-05 发布）**，中间有 `1.6.1`（09-02）；`1.6.0` 不存在（应是被 yank）
- **`1.6.1`/`1.6.2` 没有发 sdist**，只有 5 个 wheel，含 `manylinux_2_17_aarch64` + `manylinux_2_17_x86_64`
  → `CLAUDE.md` 第 62 行「不许升级（sdist 的 hatch 钩子缺 Go 1.24 会硬失败）」这条理由**对 1.6.x 不成立**
  （`--only-binary=:all:` 仍要留，它防的是未来某版又发 sdist）
- **依赖差异只有一条**：`1.6.2` 多了 `markdown-it-py>=3.0.0`，而它**已经在我们 lock 里**（`pins.txt:130`
  `markdown-it-py==4.2.0`，经 `rich` 带进来）。`cryptography<49,>=48.0.1` 这条约束没变
  → 升 1.6.2 的入场费里最贵的"依赖冲突"这一项**几乎为零**
- **`requires_python` 仍是 `>=3.12`**，`python:3.12-slim` 不用换
- **T0 提交是 2026-09-08，比 1.6.2 发布晚 3 天** —— 锁 1.5.3 时 1.6.x 已在。仓库里（决策表 / `pitfalls` /
  commit message）**没有任何一处记录"为什么选 1.5.3 而不是 1.6.x"**。最可能是 `PLAN.md` 写作期定的、之后没重查

### 二、发布节奏 → 建议的跟版策略

实测 8 个版本 / 45 天（1.3.1 `07-22` → 1.4.0 `07-27` → 1.4.1 同日 → 1.5.1 `08-07` → 1.5.2 `08-09` →
1.5.3 `08-10` → 1.6.1 `09-02` → 1.6.2 `09-05`）：**平均 5.6 天一个**，minor 约 2–3 周一个，
且 **`x.y.1` 经常不稳**（1.5.1 三天内被 1.5.2/1.5.3 追打；1.6.1 三天后即 1.6.2）。

> **「始终能升」≠「始终跑在最新」。** 字面执行后者就是每周升一次，而每次有**半天不可压缩的墙钟**。
> 建议：**跟 minor、不追 patch；新 minor 发布后等 7–10 天再升，除非有 CVE。** 落地约每月一次。

### 三、评估结论：**架构不用改**，缺的是检测层

已有决策**已经**买到版本容错，别去动它们：① 走 `subprocess` CLI 而非内嵌 → argv/env/退出码是公开接口，
比 Python 内部 API 稳一个量级；② `strix_bridge/` 是唯一 import 点且 import-linter 强制 → 爆炸半径已围起来；
③ tmpfs HOME + 显式 `--config` + `finally rmtree` 是**正向防线**不是补丁 → `persist_current()` 怎么变都不用改；
④ `fixtures/run_dirs/` 已是一等公民。**实测佐证**：Strix 的 env 名字面量全仓库只 **18 处 / 6 个文件**
（`scan_launcher.py` 10、`settings.py` 3、`llm_client.py` 2、`system_status`/`docker_probe`/`models` 各 1），
其中 10 处**已经是 `scan_launcher.py:48/60` 顶部那两张表**；**生产逻辑里没有任何版本号字面量**（只在注释）。

**已评估并否决的更激进方案**：把 Strix 隔到独立 `strix-runner` 镜像、让 `api` 不装 strix-agent。
不成立 —— 实时流（T13/T14，产品核心）必须 import `live_view` 的游标 API 与 `transcript`；不 import 就只能
自己解析 `agents.db`，那是**比 Python API 更不稳定的面**（还带 `clear_session()` 重插 + id 重排语义）。
**围墙已经在正确的位置，再往外挪反而更耦合。**

### 四、四个"现在做才便宜"的追加项（合计 **5–5.5 工时**）

窗口就在 **T10 派发之前 / T13 动工之前** —— 这两块写完再回来改就是返工 + 重采夹具（唯一要真跑扫描的成本）。

| # | 项 | 工时 | 为什么现在 |
|---|---|---|---|
| 1 | **`strix_profile.py`**：按版本键的 dataclass，收 18 个 env 名 + 退出码语义 + run 目录布局 + `agents.db` 关键列名 + CLI 参数白名单。今天只有一个条目 | **1.5–2** | ⚠️ 这踩 §编码哲学 第 3 条「不要过早抽象」。**正当理由不是"以后可能有第二版"，而是"契约测试需要一个可枚举的断言清单"**（T29 行那句"不许自己发明断言"的实现手段）。只认后者就该砍掉本项，只做 2–4（合计 1 工时） |
| 2 | **T29 契约测试改成遍历 profile 断言，且必须能对着任意版本跑**（被测版本从 env 读、profile 按被测版本选，**不能硬绑镜像内版本**） | **+0.5** | 否则下面阶段 2 的只读勘查根本不成立 |
| 3 | **T10 的退出码映射天生按 profile 写** | **0** | 还没写。写完再改 = 1 工时返工 + 一次 mutation 复验 |
| 4 | **T13 `RunProjector` 按 `scans.strix_version` 选 profile 解析旧 run 目录** + 夹具按版本分层 `fixtures/run_dirs/1.5.3/` | **+0.5** | `scans` 表**已有 `strix_version` / `sandbox_image` 两列**，溯源不缺，但**没有一处说明升级后怎么用它** —— projector 硬用"当前 profile" ⇒ 升级当天**所有历史扫描详情页解析失败**，用户视角是"升级弄坏了我以前的报告" |
| 附 | 迁移里 `UPDATE scans SET resume_available=0` | **0** | `--resume` 收 run name、要读旧格式 `.state/`，跨版本几乎不可能兼容 |
| 附 | **`make check-upstream`**（~40 行 Python，手动目标、**不进 CI 不进应用**，依赖出网） | **0.5** | 现在**没有任何机制**会告诉你上游发版了（`STRIX_NO_UPDATE_CHECK=1` 必须保留，它防外发）。指望人记得就是本轮这个结果。做四件事：①比对 `check_lock.py:26` 的 `REQUIRED_PINS`；②查 manylinux aarch64+x86_64 两个 wheel 是否齐（**缺则硬否决**）；③查有没有发 sdist；④diff 新版 `requires_dist` 与 `pins.txt` |

### 五、升级 runbook（六阶段，任一阶段失败就停在那里）

**阶段 0 发现**（1 分钟）`make check-upstream`，每月一次或收到 CVE 通报时。
**阶段 1 判定**（10 分钟，不动仓库）看阶段 0 四项 + 上游 release notes；**wheel 那项是硬否决**。
**阶段 2 只读勘查**（10 分钟–1 工时，**不改 pin、不动 lock**）一次性容器装新版、对着它跑契约测试：
`docker run --rm -v $PWD/backend:/src:ro python:3.12-slim sh -c 'pip install --only-binary=:all: strix-agent==<新版> pytest && pytest /src/tests/test_strix_contract.py'`
→ 产出 **R4 那 9 个耦合点碎了哪几条**。**这一步是整个流程的价值所在**：把"升级"从"跑跑看"变成"改这 3 条"。
**阶段 3 落地**（0.5–4 工时）profile 加/改条目 → 改 7 处 pin 落点（`pyproject.toml`、`pins.txt`、`pins-dev.txt`、
两个 `.lock`、`Dockerfile:113` 断言、`check_lock.py:26`）→ `make lock` → 重建镜像（6–15 分钟）→ 改碎掉的代码 →
**官方闸门 `make lint` + `make test`（必须 0 skipped）** → 按变动面重采夹具（run 目录或 `agents.db` 变了则
**T15a 压缩夹具要真跑一次扫描**，这是阶段 3 最贵的单项）。
**阶段 4 真跑复验**（**半天墙钟，一项都不能跳** —— 这四类都是"变了不报错、只静默给错答案"，测试碰不到）：
① `./scripts/m0_probe.sh` 断言日志出现**容器 IP** 而非 `127.0.0.1`（两个未文档化 env）；② **重验 `completion_cost`**
（litellm 跟着升 ⇒ **预算护栏静默变 0**）；③ Key 卫生：镜像 env 扫 + 任务目录外全盘扫 `LLM_API_KEY`
（`persist_current()` 写盘路径可能变）；④ `make verify-e2e` 28 条。
**阶段 5 可回滚**：**升级必须是一个单独的 commit，不与任何业务改动混** —— 有 pin + hash lock，回滚 =
`git revert` + 重建镜像；一混进业务改动，回滚就变成手术。（拟写进 `CLAUDE.md` §禁区，**待放行**。）

**停止跟版的条件**（写进流程，免得每次重新讨论）：新版无 manylinux aarch64+x86_64 wheel → 不升（硬否决）；
上游删掉 `live_view` 游标 API 或 `transcript` → 不升，先评估重写 T13 的代价；`requires_python` 超出 3.12 →
先换基础镜像，独立任务；**上游加了向第三方外发数据的默认行为且无 env 可关 → 永久不升**（违反 §禁区第一条）。

### 六、稳态账单

一次性 **5–5.5 工时**；每次 minor 升级期望 **2–3 工时 + 半天墙钟**（乐观只动 argv/env 是 1–2；悲观
`agents.db` 或 `live_view` 游标 API 变了是 4–8，因为要重跑 T15a + 重验 T13 重同步）。按每月一个 minor
≈ **25–35 工时/年**。**不跟版的替代方案不是"省下这些"，而是"某天因 CVE 被迫升级时一次付掉积压的全部差异"，
而积压越久越贵**（1.5.3 → 今天已积两个版本）。

### 七、待决 4 件（都要用户拍板，**agent 不许自己动**）

1. **§已确认决策 表里那条「不许升级或放宽 pin」要不要改成「跟 minor」** —— 连带改 `CLAUDE.md` 第 62 行
2. **第四节第 1 项 `strix_profile.py` 建不建** —— 建（5–5.5 工时，检测有清单）／不建（1 工时，只做 2–4，等真升级再说）
3. **第 3 项要不要并进 T10 方案** —— 0 成本窗口**就在 T10 派发之前**
4. **要不要先把 pin 推到 1.6.2 再继续 T10** —— 现在推最便宜（T13 未写），但 §Strix 集成面 那 100 行
   `file:line` 全是对着 1.5.3 逐行读的，**换版本等于那份地图要重核一遍**（复核方式见本节开头那条 docker 命令）

## T7b 留下的两个缺口（不阻塞，别丢）

① **`secrets` 的值没有长度下限**，`verify=false` 时空串会被存下来（`verify=true` 走不到，真实请求会失败）。
没修是因为 `logging_setup.MIN_SECRET_LENGTH=8` 本来就不脱敏短值，两处判据要一起定。
② **`providers.*` 文案树没有任何测试守着** —— `test_message_coverage.py` 只覆盖 `errors`/`scanFailures`/
`targetGuard` 三棵。bearer 的成本警告（要求给出 4～6 倍的量级对比、不许只说"可能更贵"）目前**靠人看**。T18 做前端时补。

## 别再提的事

- **`agent-rules.md` 不拆**（2026-09-14 算完账定的）：拟摘掉的部分只占子agent上下文 3%、总账单 1%～1.5%，
  而代价是 §九（派发纪律）与 §八.3（收货要逐条 mutation）从"每次调用都在场"退化成"主会话得记得去读"。
- **§编码哲学 前三条与 §安全不变式 论证句的裁剪**：提过，用户选择只做纯冗余。
- **`plan-t9.md` 删不删由用户定**（已随 `a7458fa` 被跟踪，删掉 git 历史里也还在）。

## 每次 push 前

照 §禁区 扫一遍新增行：`git grep -iE "bytedance|palo ?alto|ngfw"` 应为空、新增的美元数字必须是产品侧实测
而非 API 账单 —— **仓库是 public**，这一步不是形式。commit 身份是仓库级 GitHub noreply 邮箱，别改回全局那个。

---

## Context

要做一个渗透测试平台，底层复用开源 AI 渗透测试 agent **Strix**（`strix-agent` 1.5.3, Apache-2.0）。
Strix 只有命令行 + 一个 Go TUI，需要懂参数、懂 prompt、懂 Docker；它自带的 `strix view` Web 页面是
**只读的**、**没有任何发起扫描的接口**、**不能选模型**，而且会向 `app.strix.ai` 发数据（邮箱验证门 + PDF 外发中继）。

因此在 Strix 之上做一层 **Web 控制台（FastAPI + Next.js）**，交付四件事：

1. **向导式发起扫描** — 填目标 + 选场景模板，不接触命令行和 prompt
2. **实时进度可视化** — 子 agent 树、事件流、终端输出、浏览器截图、实时花费
3. **人话版中文报告** — 分级 + 白话解释（是什么/什么后果/怎么修），可导出给领导
4. **授权与安全护栏** — 强制授权确认、目标分类校验、云元数据地址永久禁扫、审计日志

以及 **LLM 可切换、谁用谁的 Key**：使用者在界面选供应商+模型并粘贴自己的 API Key，
Key **仅存活于内存与本次任务的子进程环境中，绝不落盘**。

### 已确认决策

| 维度 | 决策 |
|---|---|
| 部署形态 | 本地单机工具：`docker compose up` + 浏览器访问 `https://localhost`，只绑 loopback。**「本机」= 部署这套系统的那台机器**，浏览器始终在同一台机器上 —— 不做跨机器远程访问。（**注意**：这条推不出"不需要登录" —— loopback 在 macOS 上是全机共享的，见 §单账号登录）|
| 支持平台 | **macOS + Linux，均要求 Docker Desktop**。Windows 原生**不支持**（`C:\` 含冒号，同路径挂载不成立；请用 WSL2）。Docker Engine 待扩展，差异点见「待扩展：Docker Engine」 |
| 机器规格 | **可变** —— 小笔记本到大服务器都要能跑。沙箱四个限额**不得硬编码**，由 `setup.sh` 按 `docker info` 的 `MemTotal`/`NCPU` 算出写进 `.env` |
| **最小主机要求** | **Docker VM 内存 ≥ 4 GB**（推荐 8）、**Docker VM CPU ≥ 2 核**（推荐 4）、**数据目录可用空间 ≥ 10 GB**（推荐 20）、宿主物理内存 ≥ VM 配额 + 2 GB。低于阻断线 `setup.sh` 直接 `exit 1`，不给"要不要继续"的选项 —— 推导见下方「最小主机要求的推导」 |
| 传输与证书 | nginx 反代终止 TLS，对外只开 `127.0.0.1:443`（**不监听 80**）；`openssl` 单张自签证书（**SAN + EKU=serverAuth**，10 年，私钥 0600）；信任由用户手动导入（macOS `security add-trusted-cert`；Linux `update-ca-certificates`），脚本**不**自动改信任库 |
| **凭据形状** | **采纳 N1（2026-09-08 拍板）** —— `auth_shape` 是**独立于 provider 的一个维度**，不是它的属性；一个 handle 指向一**组**凭据（Bedrock SigV4 要 3 个值）。`POST /api/keys` 收 `auth_shape` + `secrets{env名→值}` + `params`；`/api/providers` 声明每家支持哪几种形状、每种要哪几个键；后端**只接受所选形状声明的键，多余的一律 `400 unexpected_secret_key`**。`auth_shape` 还决定模型名怎么拼（bearer 必须补 `invoke/`）。细则见 §N1 |
| **企业 CA** | **采纳 N2（2026-09-08 拍板）** —— compose 上一对**可选**变量，运行期由操作者显式挂 CA bundle，默认关闭；**构建期烧进镜像永久禁止**。细则见 §N2 |
| **登录** | **单账号登录，实现（2026-09-08 用户拍板，撤销原「延后」）** —— 一个用户名 + 口令；散列存 `${DATA}/auth.json`(0600)、**不进 SQLite**；会话是服务端不透明 id，存 api 进程内存、**不用 JWT**；cookie `HttpOnly; Secure; SameSite=Strict`。**不做**多用户、不做数据隔离（`scans`/`authorizations` 不加 owner 列）。细则见 §单账号登录 |
| API Key | 仅会话内不落盘：浏览器只存 opaque handle（sessionStorage），明文 Key 只在后端内存 + 子进程 env |
| 技术栈 | FastAPI 后端 + Next.js/React 前端 + nginx 反代，WebSocket 推增量（SSE 兜底）|
| 易用性 | 上述四项全要 |

### 本机环境（已核实）

Docker Desktop `29.7.2` / Compose `v5.4.0` ✅ ｜ 宿主 Python 仅 `3.9.6`，无 brew/pipx/uv ｜ Node `v24.19.0`
→ Strix 要求 `>=3.12`，**后端与 Strix 必须容器化**，挂 docker socket 让 Strix 创建兄弟沙箱容器（DooD）。

---

## Strix 集成面（已逐行读源码核实；v1.5.3。**下面所有 `file:line` 的复核方式见 §交接** ——
原来的 clone `/tmp/strix_src`（HEAD `0a6e8b01`）**已不存在**，改从测试镜像里读已安装的包）

### 硬约束，直接决定架构

| 事实 | 出处 | 影响 |
|---|---|---|
| CLI **无 `--model`**，模型只能从 env 读（`STRIX_LLM`）| `cli_args.py` | 每任务独立子进程 + 独立 env 注入模型与 Key |
| CLI **无 `--run-name`/`--output-dir`**，产物固定写 `$CWD/strix_runs/<自动名>/` | `core/paths.py:11-13` | 给每任务独立 CWD，则 `strix_runs/` 下唯一子目录即本次 run |
| 大量模块级全局可变状态（`loader._cached`、`_SESSION_CACHE`、`report/state` 全局、`configure_sdk_model_defaults` 改 `os.environ`）| `config/loader.py:26`、`runtime/session_manager.py:34`、`config/models.py:604` | **一进程一次扫描**；同进程并发扫描会导致两个用户的 Key 互相污染 |
| `-n` 模式下 `persist_current()` 把 **`LLM_API_KEY` 写进 `~/.strix/cli-config.json`**（0600）| `main.py:398` → `config/loader.py:56-74` | ⚠️ 直接违反"不落盘"，见下方缓解 |
| 退出码 `0` 正常 / `1` 错误 / **`2` = 发现漏洞**（仅 `-n`）| `main.py` 尾部 | `2` 必须当成功 |
| 失败是 `sys.exit(1)` + Rich 面板打到 stdout | `main.py`、`environment.py` | 抓 stdout/stderr，按面板标题映射成中文结构化错误 |
| viewer 无启停接口；`POST /api/agents/steer` 仅 TUI 内进程可用 | `viewer/server.py` | v1 **不支持运行中干预** |

### ⚠️ 两个未文档化但必须用的环境变量（最高风险项，已验证）

**1. `STRIX_DOCKER_SANDBOX_NETWORK` —— DooD 能否正常工作的关键**
`runtime/docker_client.py:54-66`：设了它就 `create_kwargs["network"]=<name>` 并 `pop("ports")`；
同时 `StrixDockerSandboxSession._resolve_exposed_port`（同文件 `129-160`）改为返回**沙箱容器在该网络上的 IP**。
**不设**它则 `ports={48080/tcp: ("127.0.0.1", None)}` 发布到 **Docker 宿主**的 loopback，
而 SDK 默认解析返回 `127.0.0.1:<随机端口>` —— 从我们的后端容器看，`127.0.0.1` 是**它自己**，
于是 `bootstrap_caido()` 打到死端口，**抓包代理能力静默降级**。
→ compose 里建固定名网络 `strix_sandbox`，后端容器也加入，并设该变量。
→ **已实测通过**（2026-09-08，M0 断言 4）：`Caido host endpoint resolved: http://172.19.0.4:48080`
  —— 容器 IP，读源码得出的行为与实际一致。R1 关闭。**注意"后端容器也加入"是这个结论的前提**，
  不是可选的优化；哪次重构把它丢了，这里就会静默退回 `127.0.0.1`。

**2. `STRIX_RUN_ID` / `STRIX_RUN_TYPE` —— 孤儿容器回收的抓手**
`docker_client.py:110-124` 会把它们打成容器 label `strix-run-id` / `strix-run-type`。
我们设 `STRIX_RUN_ID=<我们的 scan_id>`，即可 `docker ps --filter label=strix-run-id=<id>` 定位回收。
**必须回收**：`interface/cli.py` 的 SIGTERM handler 里 `sys.exit(1)` 会让 `SystemExit` 穿出 `asyncio.run`，
`finally: await session_manager.cleanup(...)` **永远不会跑完** → **SIGTERM 会泄漏沙箱容器**。

### Key 不落盘的缓解方案（已逐行验证）

`persist_current()` 写的是 `_override or _DEFAULT_PATH`（`config/loader.py:59`）；`_override` 由 `--config`
经 `apply_config_override()` 设置，调用点在 `parse_arguments()` 内（`cli_args.py:293`），
**早于** `_bootstrap_scan()`（`main.py:445`）。所以传 `--config <tmpfs>/cli-config.json` 就能把 Key 重定向到 tmpfs。
`validate_config_file()`（`interface/utils.py:1679-1706`）要求该文件**必须已存在、后缀 `.json`、含 `env` 对象**
→ 预先写入 `{"env": {}}`。

**但 `--config` 单独不够** —— 还有 5 处 `Path.home()/".strix"/*`：
`update-check.json`、`viewer-auth.json`、`mcp-servers.json`、`subscription-auth.json`、telemetry `.seen`。
**两层都做**：每任务独立 `HOME=<tmpfs>/scan-<id>/home` **且** 显式 `--config`（可审计、且能挡住未来新增的写路径）。

另注：`config/models.py:625-641` 的 `_mirror_api_key_to_provider_env()` 会把 Key 再 `setdefault` 到
`ANTHROPIC_API_KEY` 等供应商变量里 —— 影响 `/proc/<pid>/environ` 这一行的风险评估（见泄漏矩阵 #12）。

### 可复用的只读投影层（**注意导入路径**）

- `from strix.interface.tui.backend.live_view import TuiLiveView` —— **子类**，才有游标 API
  `event_snapshot(limit=)` / `event_changes_since(cursor)`（`backend/live_view.py:120,124`）。
  父类 `strix/interface/tui/live_view.py:20` **只有** `.agents` / `.events` / `hydrate_from_run_dir()`。
- `from strix.interface.viewer.transcript import read_run_summary, read_vulnerabilities, read_report_markdown, severity_counts, primary_target`
- **不要** import `strix.core.*` / `strix.runtime.*` 进 web 进程 —— 会连带把 agents SDK、litellm 和
  `configure_sdk_model_defaults` 的 `os.environ` 改写拖进来。用 import-linter 契约强制。

### ⚠️ `agents.db` 不是只追加的 —— 朴素游标增量会出错

`core/sessions.py:101-153` 的 `_rewrite_session()` 先 `clear_session()` 再 `add_items(rebuilt)`，
**行被删掉重新插入、id 重排**。触发者：
- `llm/compaction.py` 上下文压缩 —— **默认开启**（`STRIX_CONTEXT_AUTO_COMPACT=True`）
- `sessions.py::enforce_image_budget` —— 只保留最近 **3** 张截图（`STRIX_MAX_CONTEXT_IMAGES=3`），
  更老的替换为字面量 `[older screenshot elided to bound context memory]`

因为 `TuiLiveView` 的事件 id 是按读取顺序生成的，压缩后 **id 会重排、已推送的事件会被破坏性改写（截图消失）**。
Strix 自己的 viewer 也有这个问题，只是它 500ms 全量重渲染盖过去了。
→ 我们需要 **epoch + 重同步检测 + 自己的只追加镜像**（见"实时流"）。
三个 elision 字面量在 `sessions.py:60-62`，用于识别"这是压缩而非乱序"。

### ✅ 截图不用 docker cp

`tui/live_view.py:507-527` 的 `_normalize_image_result()` / `_image_url_from_result()` 直接产出
`{"type":"image","image_url":"data:image/png;base64,..."}`，**截图以 data URL 内联在事件里**。
`/workspace/.agent-browser-screenshots/` 只是沙箱内部约定，无需从宿主访问。
代价：只有最近 3 张 —— **首次见到就落地到我们自己的 media 目录**，压缩后依然能显示。

### DooD 路径别名：完整触发点

任何交给 `docker.containers.create(mounts=...)` 的路径必须在**宿主**上有效：

| 触发点 | 路径来源 | 由什么触发 |
|---|---|---|
| `build_bind_mounts` | 解析后的本地目录 | `-t ./dir` |
| `build_bind_mounts` | `$TMPDIR/strix_repos/<run>/` | `-t https://github.com/...` |
| `_metadata_mounts` | `<tree>/.git` 等（只读覆盖）| 带 git 的本地目标 |
| `stage_api_specs` | `$TMPDIR/strix_api_specs/<run>` | `-t ./openapi.yaml`、`postman://` |
| `build_extra_file_bind_mounts` | `<cwd>/strix_runs/<run>/.state/extra_files/<i>/` | ~~`--workspace-file`~~ **1.5.3 的 CLI 没有这个参数**（2026-09-14 `strix --help` 实测）—— 这一行只在内嵌 API 上可达，对我们等于不存在。API spec 要进沙箱只有一条路：`-t <spec 文件>`（`-t` 明写接受 OpenAPI/Swagger `.json/.yaml` 与 Postman 导出），走的是上一行的 `stage_api_specs` |

→ 解法：**同路径挂载** `${STRIX_HOST_DATA_DIR}:${STRIX_HOST_DATA_DIR}`，且 `TMPDIR` 也指到该卷下。
**不能用 named volume**（其宿主路径在 Docker Desktop VM 内，两侧不一致，会静默重现此 bug）。

### 其他需要关掉的外联

`STRIX_TELEMETRY=false`（PostHog `us.i.posthog.com` + Scarf）、`STRIX_NO_UPDATE_CHECK=1`（GitHub/PyPI）。
遥测本身不发 Key（只发 model/scan_mode 等元数据），但本地工具应默认静默。
`LITELLM_LOG=ERROR`；**绝不设 `STRIX_DEBUG`**（会把 `strix.log` 拉到 DEBUG，是泄漏面 #5）。

### 运行产物（`strix_runs/<run>/`，每次有新发现整体重写，可实时读）

`run.json`（status/targets/scan_mode/`llm_usage`含 cost/scan_results）、`penetration_test_report.md`、
`vulnerabilities.json`（+`.csv`+ 单条 md）、`findings.sarif`、`coverage.json`（**"哪些没测"的依据，报告里很值钱**）、
`strix.log`、`.state/agents.json`、`.state/agents.db`（SQLite WAL，表 `agent_messages`）。

---

## 架构

```
浏览器 https://localhost ── sessionStorage 只存 opaque vault_handle
   │  REST + WebSocket(wss)；Key 只经 JSON POST body
   ▼
nginx (TLS 终止，只绑 127.0.0.1:443，不监听 80)
   ├ /        ──▶ web (Next.js，不发布端口 —— Key 不经过它)
   └ /api,/ws ─────────────────────▶  api (FastAPI, python:3.12 + docker-cli + strix-agent==1.5.3)
                                       ├ KeyVault      内存 {handle→SecretStr}，TTL，绝不持久化
                                       ├ TargetGuard   解析/规范化/DNS/分类/白名单（纯函数，好测）
                                       ├ ScanLauncher  argv + env + tmpfs HOME + 预置 --config + cwd/TMPDIR
                                       ├ ScanSupervisor asyncio 子进程，退出码→中文，优雅停止
                                       ├ RunProjector  TuiLiveView 重投影 + epoch 重同步
                                       ├ EventMirror   只追加镜像 + 截图落地到 media/
                                       ├ LogTailer     strix.log + 子进程输出，脱敏
                                       ├ Reaper        按 label 清理孤儿沙箱
                                       ├ Translator    中文白话报告（用用户 Key，httpx 直连）
                                       └ SQLite console.sqlite —— 任何表都没有 Key 字段
   │ subprocess: strix -n -t <target> -m <mode> --max-budget-usd N --instruction-file F --config <tmpfs>
   │ env: STRIX_LLM / LLM_API_KEY / STRIX_RUN_ID=<scan_id> / STRIX_TELEMETRY=false
   │ cwd=$DATA/scans/<id>   HOME=<tmpfs>/scan-<id>/home   TMPDIR=$DATA/scans/<id>/tmp
   ▼
strix 进程 ──docker.sock──▶ 沙箱（宿主兄弟容器, strix-sandbox:1.3.0, NET_ADMIN/NET_RAW, Caido:48080）
                              两者都在固定名网络 strix_sandbox 上 → api 可达沙箱 IP:48080
```

### 为什么选 subprocess CLI 而非内嵌 `run_strix_scan()`

1. **内嵌的工作量远超签名所示**：`run_strix_scan` 不做目标推断、仓库克隆、spec 暂存、diff scope、
   run 记录持久化、`ReportState` 接线 —— 要自己重实现 `build_targets_info` + `prepare_run` + `run_cli`
   的 ReportState/signal/atexit 块，约 200 行 Strix 内部逻辑从此由我们维护。
2. **一进程一扫描对内嵌是致命的**：全局状态 + `os.environ` 改写意味着同进程两个并发扫描
   会**互相污染模型配置和 API Key** —— 这是跨用户密钥泄漏。最终还是要 fork，那就等于做了个更差的 `strix -n`。
3. **Key 卫生更好**：Key 只在子进程 env 和堆里；uvicorn 进程从不 import litellm、从不设 `OPENAI_API_KEY`。
4. 唯一真损失是**亚秒级 token 流**（`response.output_text.delta` 仅在内存、从不持久化）。
   一个 40 分钟的渗透测试，整条消息级 ~1s 延迟完全够用。

**v1 只支持 URL / 域名 / IP 目标。** 理由不是 DooD（同路径挂载已解决），而是本地目录被
**读写**挂载进一个持有 `NET_ADMIN` 的自主 agent 容器（`session_manager.py:62` `read_only: False`）——
交给非专业人员用是最大的脚下之雷。v2 开放白盒源码扫描时只需改 UI + 护栏，基础设施已就位。
**例外**：API 场景允许上传 OpenAPI spec 走 `--workspace-file`（**只读**挂载，安全）。

---

## 仓库结构（全新创建）

```
Strix/
├── README.md  Makefile  setup.sh          # setup.sh 校验并生成 .env（含路径合法性检查）
├── docker-compose.yml  docker-compose.override.example.yml  .env.example
├── docs/{SECURITY-zh.md,OPERATIONS-zh.md,STRIX-INTEGRATION.md,ARCHITECTURE.md}
│   # ARCHITECTURE.md 是本文件的**简明提取**（架构图 / 数据流 / 目录职责），不引入新决策；冲突时以本文件为准
├── pitfalls/history-pitfalls.md            # 二层：低频/特定场景的实测坑，按需读取，不被 @import
│   # docs/ 是给人看的中文交付文档；pitfalls/ 是 agent 的工作记忆。受众不同，故分两个根
├── backend/
│   ├── Dockerfile              # python:3.12-slim + docker-cli + strix-agent==1.5.3 --only-binary=:all:
│   ├── pyproject.toml  importlinter.ini
│   └── app/
│       ├── main.py  settings.py  db.py  models.py  logging_setup.py
│       ├── migrations/{001_init.sql,002_report_translations.sql}
│       ├── routes/{health,system,providers,keys,templates,targets,allowlist,scans,stream,reports,audit}.py
│       ├── services/
│       │   ├── key_vault.py        target_guard.py    allowlist.py
│       │   ├── scan_launcher.py    scan_supervisor.py run_discovery.py
│       │   ├── run_projector.py    event_mirror.py    log_tailer.py   channel.py
│       │   ├── reaper.py           docker_probe.py    llm_client.py
│       │   └── translator.py       exporter_html.py   exporter_docx.py  audit.py
│       └── strix_bridge/           # 唯一允许 import strix.* 的地方（import-linter 强制）
│           ├── projection.py  paths.py  catalogue.py
│   └── tests/{test_target_guard,test_key_hygiene,test_scan_launcher,test_projector_resync,
│              test_no_secret_columns,test_strix_contract}.py + fixtures/run_dirs/
└── frontend/  (Next.js 15 + React 19 + CSS Modules + zustand + react-query)
    # 2026-09-09 拍板不引 Tailwind：设计刻意窄（圆角只有 2 个值、零 box-shadow、
    # 3 个语义色各不串用），而 Tailwind 的红利来自接受它的宽刻度 —— 我们的字阶与圆角
    # 都不在它的默认刻度上，每个值都要写成 `[]` 转义，付税却用不到它卖的东西。
    # 另一层理由：Tailwind 的默认长相（rounded-lg shadow-sm）正是这轮花力气排掉的
    # 品类脸，给下游子任务 Tailwind 等于给漂移开一条回去的路。
    ├── messages/zh-CN.json         # 全部用户可见文案集中在此
    └── src/{app,components/{wizard,live,findings,report,expert},lib}
```

`data` 目录**不放在仓库里** —— 放在 `.env` 指定的绝对宿主路径（同路径挂载要求）。
`setup.sh` 拒绝 macOS 未共享的路径，也拒绝 `~/.config`、`~/.ssh`、`~/.aws`、`~/.docker`、`~/.kube`
下的路径（`interface/utils.py:1413` 的 `check_mountable_dir` 会拒绝挂载这些）。

---

## Key 不落盘：泄漏矩阵

| # | 泄漏面 | 对策 |
|---|---|---|
| 1 | 我们的 DB | **任何表都无 Key 字段**；`db.assert_no_secret_columns()` 启动时遍历 `PRAGMA table_info` 拒绝可疑列名；CI 有对应测试。测试账号密码**只**进 tmpfs 上的 instruction 文件，DB 只存 `instruction_sha256` |
| 2 | `~/.strix/cli-config.json`（`persist_current()`）| 每任务 `HOME=<tmpfs>/scan-<id>/home` **且** `--config <同目录>/cli-config.json` 预置 `{"env":{}}`；tmpfs 为 `/run/strix`（`noexec,nosuid,size=16m,mode=0700`）；`finally` 里 `rmtree` |
| 3 | 我们的应用日志 | 全程 `SecretStr`；脱敏挂在 **root handler 的 Formatter** 上：正则（`sk-`/`sk-ant-`/`AIza`/`PMAK-`/`AKIA`/`ASIA`/`ABSK`）**外加 KeyVault 中每个活跃凭据里的每一个值的精确子串**（一个 handle 可能有 2–3 个值，见 §N1）—— 覆盖我们不认识的供应商格式。**本行原写"root logger 挂 `RedactionFilter`"，2026-09-08 T2 实测证明那样静默无效**（logger 的 filter 不作用于子 logger 传播上来的记录，而我们的日志全是 `app.*`）；选 Formatter 而非 Filter 是因为它是唯一的序列化出口，`msg`+`args`+`formatException()` 的整条 traceback 一次覆盖。另需显式把 `uvicorn`/`uvicorn.error`/`uvicorn.access` 的 `propagate` 改回 `True` 并清掉它们自带的 handler，否则 uvicorn 打的东西完全不脱敏 |
| 4 | uvicorn access log | Key **只**出现在 JSON POST body，绝不进 query/path；生产 `--no-access-log`；请求体大小上限防 413 回显 |
| 5 | `strix.log` | `_NOISY_LIBS` 已被 Strix 压到 WARNING，除非设 `STRIX_DEBUG` → **绝不设**；此外 `LogTailer` 在推给前端和写镜像**之前**跑同一个脱敏过滤器 |
| 6 | 遥测外发 | `STRIX_TELEMETRY=false`、`STRIX_NO_UPDATE_CHECK=1`；设置页只读展示"遥测：已关闭" |
| 7 | litellm 调试 | `LITELLM_LOG=ERROR`；从不设 `set_verbose` |
| 8 | 异常栈 / 500 body | 全局异常处理器返回 `{code, trace_id}`，traceback 走脱敏后再记；**Strix 的 `LLM CONNECTION FAILED` 文本也要脱敏后再展示**（litellm 的鉴权错误有时带 Key 尾部）|
| 9 | 沙箱容器 env | **已验证安全**：`session_manager.create_or_reuse` 显式构造 `Environment`，只有 `PYTHONUNBUFFERED`/`HOST_GATEWAY`/代理变量/`NO_PROXY`/可选 UID-GID，**不转发任何 LLM env** → `docker inspect 沙箱` 干净（验收 step 9 实测）|
| 10 | 我们容器的 env / compose | Key **绝不**进 compose、`.env`、镜像；只在运行时经 HTTP 到达内存与子进程 |
| 11 | 前端持久化 | `POST /api/keys` 换回 opaque `vault_handle`，**只**把 handle 存 `sessionStorage`（关标签即失效）；不用 localStorage、不进 cookie、不进 URL；输入框 `type=password` + 随机 `name` 破自动填充；React state 在 POST 后 `finally` 清空 |
| 12 | 子进程 `/proc/<pid>/environ` | **设计使然、不可消除**（Strix 只收 env，且 `_mirror_api_key_to_provider_env` 还会复制到供应商变量）。边界：仅该容器内 root 可读、`no-new-privileges`、进程随任务结束。**Key 不进 argv**（有断言测试，`ps` 干净）。写入 `docs/SECURITY-zh.md` 作为已知可接受属性 |
| 13 | 网络传输 | 浏览器 → nginx **全程 TLS**（自签证书，SAN+EKU）；Key 只在 JSON POST body，绝不进 query/path。反代**不经 Next.js** → Node 进程不再持有 Key。**已知且接受的残余风险**：`nginx → api` 走 Docker 桥网**明文**，有 `docker.sock` 权限者（本机管理员）可被动抓包拿到 Key。判断依据：仅本机使用、其他本地账号非对抗性。**必须写进 `docs/SECURITY-zh.md`**，不得让人误以为"上了 HTTPS 所以 Key 全程加密" |
| 14 | **TLS 私钥本身**（2026-09-08 T4 落地时发现，原矩阵漏项）| `${DATA}/tls/key.pem` 是 `0600`、以 `:ro` 挂进 nginx。**但残余风险不在 nginx 一侧**：`${DATA}` 整个目录以**读写**方式挂进以 `user: "0:0"` 运行的 `api` 容器 —— **`api` 被拿下即等于私钥泄漏**，而 `api` 恰恰是攻击面最大的那个容器（它跑 LLM 驱动的子进程）。不修的理由：能拿到 `api` 内 root 的人本来就已经能读 `docker.sock`（≈ 宿主 root），私钥不是那时最值钱的东西 —— 这是**风险不升级**，不是"私钥安全"。补偿：证书只签 `localhost`/`127.0.0.1`（SAN 里没有别的名字，偷去了也冒充不了任何真实域名）、`CA:FALSE`（不能拿它给别的域名签证书）。**写进 `docs/SECURITY-zh.md`；将来若把 `${DATA}` 收成只读或换非 root 用户，重新评估这一行** |
| 15 | **nginx 的 error log**（同上，T4 实测）| `access_log` 用 `$uri` 只管住访问日志；**error log 会打完整 `$request` 与上游 URL（含 query），且格式不可配置**（`pitfalls` 条 30）。所以"敏感值不进日志"在这一层**没有**结构性保证，靠的是后端侧"Key 只经 JSON POST body、绝不进 query/path"这条约束。**验收 #11 扫日志必须把 `nginx` 的 stderr 一起扫**，只扫 `api` 是漏的 |

| 16 | **测试账号口令不在脱敏集合里**（2026-09-14 T9 落地时发现，原矩阵漏项）| 第 3 行的精确子串脱敏取的是 **KeyVault 里活跃凭据的值**，而操作者填的测试账号口令**从不进 KeyVault** —— 它只在 tmpfs 的 `instruction.txt` 里（`compose_instruction` 写成 `role=… username=… password=…` 一行）。目前不泄漏，因为**没有任何代码读那个文件**；但**T10 一旦把指令正文回显进日志或前端，就是明文**。对策二选一、由 T10 定：要么正文永不回显（只回显 `instruction_sha256`），要么把 `spec.test_credentials` 的值也喂给 Redactor。**不许"先回显再说"** |

**KeyVault 生命周期**：`IDLE_TTL=8h` / `HARD_TTL=24h` / 60s sweeper；`ref_count` 跟踪活跃扫描与报告任务；
`DELETE /api/keys/{h}` 立即清除。`api` **必须 `--workers 1`**（vault 是进程内 dict，多 worker 会随机 404）——
启动时校验 `WEB_CONCURRENCY`。不做 `mlock`（容器无 `IPC_LOCK` 会失败，且是虚假安全感）。

**`api` 重启后**：所有 handle 消失。运行中的扫描继续（子进程已持有 Key），但**续跑与报告翻译需重新输入 Key** ——
这是正确且诚实的行为。`POST /api/scans/{id}/resume` 必须带 `vault_handle`，失效则 `409 key_required`，
UI 弹窗预填 provider/model、Key 框留空，文案说明"我们从不保存密钥"。

---

## M0 带出的两条产品需求（**N1 与 N2 均已于 2026-09-08 拍板采纳，并入 §已确认决策**）

两条都不是"开发机不便"，而是**实测发现的设计缺口**。**机理与全部 `file:line` 出处在 `pitfalls/history-pitfalls.md` 条 19／22／23**，本节只留结论与实现后果。

### N1. `vault_handle` 必须指向一**组**凭据，不是一个字符串　✅ **已采纳（2026-09-08）**

原设计假设"一个供应商 = 一个 Key 字符串"。Bedrock 打破了它，**而且打破两次** —— 同一个供应商有两种互斥的凭据形状，所以形状不是 provider 的属性，是一个独立维度。字段名从 `key_handle` 改成 `vault_handle`（DB 列与 HTTP 字段统一）就是因为旧名字来自那个被推翻的假设；顺带解开了"黑名单必须含 `key`、又不许开豁免"的死结（见 §数据模型）。

| 形状 | 要注入的 env | 值的个数 | 模型名约束 |
|---|---|---|---|
| `single`（Anthropic / OpenAI / Gemini / DeepSeek…） | `LLM_API_KEY` | 1 | — |
| Bedrock SigV4 | `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` + `AWS_REGION_NAME` | 3 | `bedrock/<model>` |
| Bedrock API key（bearer） | `AWS_BEARER_TOKEN_BEDROCK` + `AWS_REGION_NAME` | 2 | **必须** `bedrock/invoke/<model>` |

**最后一列不是可选项，是硬约束** —— bearer 在 litellm 里的支持是分路由的：converse 路由无条件先取 SigV4 凭据，只给 bearer 时直接崩，走不到认 bearer 的签名分支；invoke 路由才行（条 22）。→ `auth_shape` 不只决定"要哪几个 secret 键"，还决定**模型名怎么拼**：`ScanLauncher` 按 shape 补 `invoke/` 段，不要求用户自己记。

**而 `invoke/` 段又反过来强制一个 env**：Strix 的 prompt caching 只在 converse 路由上成立，走 invoke 会被 Bedrock 打回 `400`；Strix 自己想防住但候选名不剥 `invoke/`，判定照旧返回 `True`（条 23）。出路是它自带的一等公民开关 **`STRIX_PROMPT_CACHE=false`**，不需要打补丁。
→ **判据是路由，不是 auth_shape**：按"模型名里含 `invoke/`"决定是否注入这个 env；SigV4 用户若自己指定 invoke 路由，同样要关。
→ **`completion_cost` 对两条路由算出的值逐位相同**，预算护栏不受影响 —— 但**任何换路由的改动都必须重验这一点**，否则护栏静默变 0。

两种 Bedrock 形状**必须二选一**：litellm 先取 `AWS_BEARER_TOKEN_BEDROCK`，有值就发 `Authorization: Bearer …` 并整段跳过 SigV4。同时给两套会被静默忽略一套、失败时归因不了 —— **UI 必须让人明确选一种，不能两个框都摆着**。Strix 侧帮不上忙（它只镜像名字以 `_API_KEY` 结尾的变量），但 `LLM_API_KEY` 对 Strix 是**可选**的（只有 `STRIX_LLM` 必填），直接喂 `AWS_*` env 就能跑（M0 已实测打到 AWS 真实响应）。

后果，逐条落到实现上：

- `POST /api/keys` 的 body 从 `api_key: SecretStr` 改为 `auth_shape: str` + `secrets: dict[str, SecretStr]`（键就是要注入子进程的 env 变量名）+ 非机密的 `params: dict[str, str]`（区域等）。**每个供应商声明它支持哪几种 auth_shape、每种要哪几个键**，由 `/api/providers` 一并返回，前端据此渲染 1／2／3 个输入框。后端只注入所选 shape 声明的那几个键，**多余的键一律拒绝**（`400 unexpected_secret_key`）—— 否则用户把两套都填上，会撞进上面那个静默忽略。
- `RedactionFilter` 必须把这一组里的**每个值**都注册为精确子串 —— 只注册"主 Key"会漏掉 secret。
- `label` 的脱敏展示要按值分别算（`AKIA…7Q4F` / `…` 各一条），不能只显示一条。
- 泄漏矩阵每一行的判定对象从"Key"变成"这一组值"；探测脚本按 `SEC_HARD`（真机密）/ `SEC_SOFT`（半公开标识符）分级扫描，**两级都要报**。
- `db.assert_no_secret_columns()` 的黑名单机制不变（本来就按名字拦），但要确认 `aws`／`access`／`credential` 也在名单里，否则 `aws_access_key_id` 能溜过去。
- **成本要如实告知，而且有数字**（M0 第 3 次运行实测）：bearer 形状被迫走 invoke、进而被迫关缓存，13 次请求烧掉 `$2.0572`（`cached_tokens: 0`、`cache_write_tokens: 0`、输入 603K token），**在 juice-shop 上一无所获**。Bedrock 缓存读取是原价 1/10 → 开着缓存大致是 1/4～1/6。
  → **SigV4 不是"也行"，是便宜 4～6 倍。** 选 bearer 形状时 UI 必须给出这个量级对比并说明 SigV4 保留缓存 —— 这是真实取舍，不许悄悄替用户决定，也不许只说"可能更贵"这种没信息的话。
  → 同时意味着**默认预算 $2 对 bearer 形状不够**，向导的预算默认值要按形状区分。

### N2. 运行期由操作者挂载 CA bundle —— **绝不**烧进镜像　✅ **已采纳（2026-09-08）**

本网络实测四家主流 LLM 端点全被同一台企业防火墙解密，容器内没有那张企业根 CA，TLS 握手就失败，与凭据无关。这不是本开发机的怪癖 —— **任何处在企业 TLS 检查后面的用户都会撞上，且现有设计下无解**。

所以留一个口子：`docker-compose.yml` 上一对**可选**变量，`STRIX_EXTRA_CA_FILE`（宿主 PEM 绝对路径，默认空）→ 以 `:ro` 挂进 `api`，并设 `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` 指向它。默认关闭；`setup.sh` 不自动探测、不自动填。**边界必须写清，否则这个口子会变成条 15 那个被否掉的方案**：

- 只允许**运行期由操作者显式挂载**。构建期把任何 CA 烧进镜像 = 把一张能签任意域名的证书焊进交付物，**永久禁止**（条 15 的裁定不变）。
- 启用它意味着"凭据与全部 LLM 流量明文过一遍企业解密设备"。必须在 UI 上**显式警示**并写进 `docs/SECURITY-zh.md` 的已知风险，不能只当一个安静的配置项。
- `GET /api/system/status` 要报出"额外 CA：已启用/未启用"，让人一眼看见自己在哪种模式。

### 由此确定的开发期取舍（不是产品需求，只是本机怎么干活）

本网络未被解密的只有 **AWS Bedrock** 与 **Google Gemini**。开发与 M0 走 Bedrock —— 它后面就是 Anthropic Claude，Strix 的 system prompt 与 ~90 个 skill 都是按 Claude 调的，行为最接近设计意图。

---

## 实时流设计（epoch + 重同步 + 只追加镜像）

每个扫描一个 `ScanChannel`（**一个轮询器、N 个 WebSocket 订阅者**，因为重投影是 O(整条 transcript)）：

```
每 tick:
  1. stat() run.json / .state/agents.json / .state/agents.db / vulnerabilities.json
     四者 (mtime,size) 都没变 → 整轮跳过（空闲时约 4 次系统调用）
  2. view = backend.live_view.TuiLiveView(); view.hydrate_from_run_dir(run_dir)
  3. agents 差分 → 4. events 差分（见下） → 5. read_run_summary → status/cost
  6. read_vulnerabilities 按 id 差分 → 7. read_report_markdown 变了才推
```

**差分与重同步**（应对 `agents.db` 重写）：维护 `_sent{event_id→fingerprint}`、`_order[]`、`_epoch`。
`shrank`（变短）/ `reordered`（前缀不稳定）/ `mutated`（fingerprint 变而 `version` **没**递增）任一成立
→ `_epoch += 1` 并整体 `events.snapshot`。
`version` **递增**且 `data.status` 从 `running→completed/failed` 是**正常更新**，发 `event.update`。
识别 `sessions.py:60-62` 那三个 elision 字面量 → 发 `compaction_notice` 而非吓人的全量重同步，
**并继续从镜像提供那张截图**，用户不会眼前一黑。这是我们比 Strix 自带 viewer 强的地方。

**EventMirror（只追加真源）**：每个 snapshot/delta/update 写 `scan_events(scan_id, epoch, seq, ...)`；
`data:image/...;base64` 解码一次落到 `<data>/scans/<id>/media/<sha256>.png`，事件里改写成
`/api/scans/{id}/media/<sha>.png`。收益：WS 帧小、截图能抗 elision、报告可内嵌、重连可从镜像回放。

**自适应轮询**：running 且近 3 tick 有变化 → 250ms；空闲 ×2 退避到 2s 上限；
`read_run_summary(...)["finished"]` 为真且子进程已退出 → 停。

**日志**：按字节偏移续读 `strix.log`，按 `telemetry/logging.py:47` 的格式解析成
`{ts, level, agent_id, logger, msg}`；`--resume` 是 append 模式所以偏移跨续跑仍有效；默认只推 `INFO+`。
**用户看到的"终端"其实不是 `strix.log`**，而是 `tool_name == exec_command` 事件的 `args.command` / `result`
—— 复用 `tui/backend/projection.py::sanitize_terminal_text` 去 ANSI/控制字符。

**WS 信封**：`{v, epoch, seq, type, ts, payload}`，`type ∈ phase|agents|event.add|event.update|
events.snapshot|vuln.add|summary|log|report|report.progress|compaction_notice|error|done`。
客户端 `{"type":"hello","resume_from":{epoch,seq}}`，epoch 匹配则从镜像回放，否则给新 epoch 的全量快照。

---

## 向导 → CLI 映射

UI 与帮助全中文；`--instruction` 正文用**英文**（Strix 的 system prompt 与 ~90 个 skill 都是英文）。
模板存**进程内 Python 常量** `services/scan_templates.TEMPLATES`（2026-09-14 用户拍板，**推翻原先写的
`config/templates/*.yaml`**）：仓库里已有一个反向先例 —— `llm_client.CATALOG` 是常量、`routes/providers.py`
只做投影，而"改模板不用改代码"这个原始理由**站不住**，YAML 改完照样要重建镜像。YAML 换来的只是
加载器 + Pydantic 校验模型 + 打包路径三层，还把打字错误从 import 期推到运行期。因为 `scan_config["skills"]` 虽被引擎读取但
CLI 从不填充（`interface/cli.py` 构造 `scan_config` 时没有 `skills` 键），改为在指令里点名 skill，
由 agent 自己的 `load_skill` 工具加载。

所有模板追加统一尾巴：要求每个 finding 的 `description`/`impact`/`remediation_steps`
**中英双写（中文在前）**，并禁止 DoS / 资源耗尽 / 数据破坏 / 账号锁定类测试。
（注意：护栏**不能**依赖 prompt 文本 —— 权威 scope 由 `build_scope_context` 注入，用户指令
按 `_compose_root_instructions_override` 明确"不得扩大或削弱授权目标约束"。）

| 模板 | `-m` | 预算/轮数 | 指令要点 |
|---|---|---|---|
| 快速体检 | `quick` | $5 / 60 | 限时分诊；只测 6 类高影响可直接利用的问题；跳过子域枚举与目录爆破 |
| 全面体检（推荐）| `standard` | $25 / 200 | 先枚举功能与角色，再系统性走 OWASP Top 10 全入口；边走边记 coverage |
| 深度审计 | `deep` | $80 / 500 | 按功能域派生子 agent；把发现串成完整攻击链而非孤立原语 |
| 只测登录与权限 | `standard` | $15 / 120 | 登录/注册/找回/改邮箱/MFA/会话/JWT/水平与垂直越权；**不**花轮数在 XSS 与注入上；只用自建账号，绝不锁死真实账号（UI 收集测试账号并附到指令）|
| API 接口测试 | `standard` | $25 / 200 | 枚举 spec 每个 operation；额外报告"spec 里有但不可达"与"未文档化但存在"的接口。**⚠️ 上传 spec 在 v1 不做**（2026-09-14 T9 方案审阅时定）：原写的 `--workspace-file` 在 1.5.3 的 CLI 里**不存在**（见 §DooD 路径别名 那张表），唯一的路是把 spec 当成第二个 `-t` 目标，而那要先让 `target_guard` 接受"文件路径"这一类目标（v1 只放 URL/域名/IP，§架构）。所以 v1 这个模板 = 指令正文 + 普通 URL 目标；开放它是 T18/T19 的事，不是 T9 的 |
| 上线前复检 | `standard` | $12 / 100 | 针对改动说明做回归；已报问题要**实测**是否真修好（v2 加 `--scope-mode diff --diff-base`）|

**预算强制**：Strix 的 `--max-budget-usd` 本是可选（默认无限）。我们**强制必填**，
`ScanLauncher` 缺它就拒绝构造 argv，`CONSOLE_MAX_BUDGET_CEILING_USD`（默认 100）兜底。
Strix 在预算/轮数的 70/85/95% 会提醒 agent 收尾（`core/hooks.py:26-29`），所以上限是**优雅收尾不是硬杀**
—— UI 要写清"不会中途丢失结果"。

"高级模式"折叠面板暴露：scan_mode 覆盖、预算、轮数、`STRIX_REASONING_EFFORT`、追加自由文本、
以及按 skill 勾选自组场景 —— 全部汇入同一个 argv 构造器。

---

## 护栏

**解析规范化**（`target_guard.py`，纯函数好测）：拒绝含空白/`;`/反引号/`$(`/不可打印字符；
无 scheme 补 `https://`；只接受 http/https；**拒绝带 `user:pass@`**（Strix 的 `infer_target_type`
会把它重分类成 *repository*！）；拒绝 `.git` 结尾路径（`_is_http_git_repo` 会发真实网络请求）；
host 小写 + IDNA 编码（让 `例子.中国` 变 punycode，同形字攻击可见）；
`getaddrinfo` 解析 A+AAAA 并对**每个**地址分类，主机名与解析结果一起入审计（这是 DNS rebinding 的可见记录）。

**分类与策略**（不是一张平铺黑名单）：

| 类别 | 例子 | 策略 |
|---|---|---|
| `metadata` | `169.254.169.254`、`fd00:ec2::254`、`metadata.google.internal`、`100.100.100.200`（阿里云）| **永久硬拦，不可覆盖** |
| `loopback` | `127.0.0.0/8`、`::1`、`localhost` | 默认拦；勾"测试本机服务"可放行。UI 必须解释：Strix 会把它改写成 `host.docker.internal`（`scan_setup.py:51`），测的是**你这台电脑**，不是容器内部 |
| `private` | RFC1918、`fc00::/7`、`.local`/`.internal`/`.lan`、单标签主机名 | **可覆盖 —— 这是常见的合法场景**，硬拦会砸掉主要用例。需勾选内网确认 + 标准授权声明，并留痕 |
| `carrier/reserved` | `100.64.0.0/10`、`192.0.2.0/24`、组播、广播 | 拦，只能从白名单文件放行（UI 不给） |
| `public` | 其余 | `enforce` 模式下**必须**命中白名单 |
| `mixed` | 同一主机名同时解析到公网和内网 | 拦，`code: split_horizon`（这就是 rebinding 的形状）；要测须直接填字面 IP |

**白名单** `${DATA}/config/allowlist.yaml`，按 mtime 热重载，Pydantic 校验，UI 可编辑且每次编辑入审计：
`mode: enforce|advisory`、条目含 `label/owner/authorization_ref/expires/hosts（单层通配）/cidrs/
allow_private/allow_loopback/max_budget_usd/forbidden_paths`。最长后缀优先，显式主机胜过通配。
未命中 → `409 not_in_allowlist` + 一键"添加到授权清单"。

**强制授权声明**（向导第 4 步，不可跳过、不可预填）：
1. 展示**规范化后**的目标与解析出的 IP（punycode 与 split-horizon 因此可见）
2. **三个独立必勾**复选框：拥有或已获书面授权 / 非他人生产系统或已知情同意 / 理解会真实发起攻击性请求
3. **逐字输入**注册域名（或字面 IP）；规范化后比对；**期望字符串灰显在输入框旁边而不是 placeholder 里**
   （某些浏览器 placeholder 可被复制，放里面就失去意义）；多目标另加"以上 N 个均已授权"
4. 操作人姓名 + `authorization_ref`（命中白名单时从条目预填）
5. 授权记录在**启动子进程之前**、与 `scans` 行同一事务写入 `authorizations` + `audit_log`；
   `scans.authorization_id` 是 **NOT NULL** —— 这条 DB 约束让"误扫"在结构上不可能，而不是靠 UI 自觉

**服务端全部重校验**（UI 不可信），并在启动时**重新解析 DNS** 与声明时的结果比对，变了就 `409 dns_changed`。

**并发上限** `CONSOLE_MAX_CONCURRENT_SCANS` 默认 **1**（一次扫描就是一个 Kali 沙箱，笔记本吃不住三个）。

**审计**：`audit_log` 表 + `${DATA}/audit/YYYY-MM.ndjson` 镜像（DB 丢了也在、可 grep）。
事件含 `allowlist.changed`/`target.rejected`/`authorization.affirmed`/`scan.launched`（**含完整 argv 与
env 变量名，值脱敏**）/`scan.stopped`/`scan.finished`/`report.exported`/`key.registered`（仅掩码标签）/
`key.forgotten`/`override.private_used`/`override.loopback_used`。UI 只读展示 + CSV 导出。

---

## 人话版中文报告

`finished` 变真时自动触发，或 `POST /api/scans/{id}/report/zh {vault_handle}` 手动触发。
用用户**同一个** `strix_llm` 路由与 `api_base`，但用 **httpx 直连**（不把 litellm import 进 web 进程）——
为要支持的几个供应商各写约 60 行适配器 + 一个通用 OpenAI 兼容分支。`Semaphore(4)` 并发，JSON 修复重试。

每条 finding 产出：`{title_zh(8-20字), what_zh, impact_zh, fix_zh, severity_zh_label,
severity_reason_zh, effort_zh, who_fixes_zh, confidence_zh, layman_analogy_zh?}`。
Prompt 要点：读者是不懂安全的业务/管理人员；**禁止未翻译术语**（给定词表：IDOR→越权访问、
SSRF→服务端请求伪造、RCE→远程命令执行、SSTI→模板注入、CSRF→跨站请求伪造、BFLA→接口权限缺失、
mass assignment→参数批量赋值）；**禁止编造输入里没有的事实**；`severity_reason_zh` 必须引用
`cvss`/`cvss_breakdown`/`confidence_rationale`。
把 `counterevidence` 与 `assumptions` 一并喂进去，让中文如实写出"需人工确认"——
对一份管理层报告，可信度比漂亮话重要。

再一次 executive 调用（输入 `penetration_test_report.md` + `severity_counts()` + `coverage.json`）
→ `{summary_zh(≤300字), risk_verdict_zh, top3_actions_zh[], scope_zh, coverage_zh, not_tested_zh[]}`。
**`not_tested_zh` 很关键且很便宜** —— `coverage.json` 现成，一份说清"哪些没测"的报告远更站得住。

**缓存**：`report_translations` 主键 `(scan_id, finding_id, input_hash, model, lang)`，
`input_hash = sha256(canonical_json(finding子集))`；新发现只翻新增的，换模型重翻但不丢旧的。
UI 单独显示"报告生成费用 $0.14"（花的是用户自己的 Key，应当可见）。

**导出**，按优先级：
1. **打印 CSS HTML（主力）** —— `GET .../report/print?lang=zh` 返回**自包含**单文件（内联 CSS、
   图片内联为 data URL 取自镜像所以截图不丢）、`@page {size:A4; margin:18mm 16mm}`、
   `.finding{break-inside:avoid}`、`font-family:"PingFang SC","Microsoft YaHei","Noto Sans SC"`。
   点"导出 PDF"新开页并 `window.print()`。**零依赖、中文与断行完美、用户用自己系统的 PDF 引擎。**
2. **DOCX** —— 用**标准库 `zipfile`** 手写最小 WordprocessingML（约 250 行，6 种元素），
   `w:rFonts w:eastAsia="PingFang SC"`。**不用 `python-docx`**（为 6 种元素拖进 lxml C 扩展）；
   **不用 reportlab 出中文**（`viewer/report_pdf.py:69` 自述只用 Helvetica，中文会是豆腐块；
   换 CID 字体后 reportlab 的 CJK 断行与混排度量也很差）。
3. Markdown / `vulnerabilities.csv` / `findings.sarif` 直通下载（免费）。

**不复用** `viewer/report_pdf.py::build_encrypted_report` —— 它是为 `/api/report/send` 外发中继而生的。

---

## 专家模式：**不代理 Strix 自带 SPA，自己做一个 tab**

1. **它会外联且有邮箱门**：`/api/runs` 与任何非本次 run 的数据都要 `auth.is_verified()` 打
   `STRIX_APP_URL`（默认 `https://app.strix.ai`）；`POST /api/report/send` 会把客户渗透测试结果的
   加密 PDF **上传到 Strix 的中继**；`POST /api/event` → PostHog；打开就 `posthog.viewer_opened()`。
   对一个主打"本地、数据不出机器"的工具，挂一个"把报告邮件发给我"按钮接第三方中继是不可接受的。
2. **token→cookie 握手过代理很脆**：cookie 名是 `strix_viewer_session_<绑定端口>`
   （因为浏览器 cookie 不按端口隔离），两个 run 的 viewer 经同一 origin 代理会产生两个不同名 cookie ——
   能用，但重启后端口撞车或用户开两个 run 跨过邮箱门就出问题。约 150 行去守一个我们不控制的 UI。
3. **不需要**：`transcript.build_run_state` 本质就是
   `TuiLiveView().hydrate_from_run_dir(); return {"agents":…, "events":…}` —— 专家 tab 要的东西
   已经全在我们的 WS 流和镜像里了。

**所以专家 tab 用自己的 store 渲染**：原始事件 JSON（带复制按钮）、完整 agent 拓扑与每个 agent 的
status/error、不过滤的 `strix.log`（含 DEBUG）、`run.json`/`vulnerabilities.json`/`findings.sarif`/
`coverage.json` 原文与下载、`llm_usage` 的**按 agent** token 与费用明细（`report/usage.py::to_record`
给了 `agents[].{agent_id,agent_name,model,cost,...}`）、以及本次启动的 argv 与脱敏 env。
比 Strix 自带 viewer 展示得**更多**，且零外联。

**廉价而诚实的逃生门**：设置页一个"在 Strix 原生查看器中打开"按钮，
`docker compose exec -T api strix view <run> --host 0.0.0.0 --port 47800 --no-open`，
打印带 token 的 URL 并中文警示"此界面为 Strix 官方所有，可能访问 app.strix.ai"。
仅当 `CONSOLE_ENABLE_NATIVE_VIEWER=1` 时发布该端口。

---

## 数据模型（SQLite，`${DATA}/console.sqlite`，WAL）

> **任何表都没有存放 API Key / secret / token / password 的列。** `scans.vault_handle` 是内存中的
> opaque UUID，`api` 重启后即失去意义；`scans.env_var_names_json` 只存**变量名**；`audit_log` 只存掩码标签。
> **不存在 `scan_credentials` 表** —— 向导收集的测试账号只进 tmpfs 上的 instruction 文件（0600，随任务目录删除），
> DB 只留 `instruction_sha256`。启动时 `db.assert_no_secret_columns()` 校验，CI 有 `test_no_secret_columns.py`。

- `authorizations(id PK, created_at, operator_name, authorization_ref, targets_json, resolved_ips_json, typed_confirmation, affirmations_json, overrides_json, allowlist_entry_id, allowlist_snapshot, user_agent, client_ip)`
- `scans(id PK, created_at, started_at, finished_at, status, phase, **authorization_id NOT NULL REFERENCES authorizations(id)**, template_id, targets_json, scan_mode, scope_mode, max_budget_usd NOT NULL, max_turns, reasoning_effort, provider, **auth_shape NOT NULL**（§N1 采纳后补：续跑要靠它决定重新索要哪几个凭据键、UI 渲染几个输入框；`api` 重启后 `vault_handle` 失效，这一列是唯一线索）, strix_llm, api_base, vault_handle, cwd, run_dir, strix_run_name, pid, argv_json, env_var_names_json, instruction_sha256, exit_code, exit_meaning, error_code, error_message, cost_usd, count_critical, count_high, count_medium, count_low, agent_count, event_count, resume_available, strix_version, sandbox_image, current_epoch)`
- `scan_events(scan_id, epoch, seq, strix_id, kind, agent_id, ts, version, fingerprint, data_json, PK(scan_id,epoch,seq))` — 只追加镜像
- `scan_agents(scan_id, agent_id, name, parent_id, status, error_message, created_at, updated_at, PK(scan_id,agent_id))`
- `scan_findings(scan_id, finding_id, severity, title, cvss, cwe, cve, endpoint, method, confidence, finding_class, first_seen_at, raw_json, input_hash, PK(scan_id,finding_id))`
- `scan_media(scan_id, sha256, mime, bytes, rel_path, first_agent_id, first_seen_at, PK(scan_id,sha256))`
- `audit_log(id PK AUTOINCREMENT, at, event, scan_id, authorization_id, actor, detail_json, client_ip, user_agent)`
- `report_translations(scan_id, finding_id, input_hash, model, lang, payload_json, cost_usd, input_tokens, output_tokens, created_at, PK(scan_id,finding_id,input_hash,model,lang))`

---

## 后端接口（要点）

```
GET  /api/health · GET /api/system/status      # docker/镜像/网络挂载自检/同路径自检/遥测状态/孤儿数
POST /api/system/pull-image  ·  POST /api/system/reap  ·  POST /api/system/native-viewer

GET  /api/providers                            # 模型目录 + 每家要哪几个凭据键（见 §N1）
POST /api/keys  {provider, auth_shape, strix_llm, api_base?, secrets:{env名→SecretStr}, params:{区域等}, verify=true}
     # auth_shape 必填（§N1 采纳后补）—— 少了它就无法执行"只收该形状声明的键、多余的 400 unexpected_secret_key"，
     # 而那条正是防住"bearer 与 SigV4 都填上被 litellm 静默忽略一套"的唯一手段
     → 201 {vault_handle, labels:["sk-ant-…4f2c"], verified, verify_latency_ms}   # 一组值 → 一组脱敏标签
     → 400 key_verify_failed        # 发一次 1-token 请求，1 秒内就知道 Key 错，而不是烧到 60 秒后
GET/POST-touch/DELETE /api/keys[/{h}]

GET  /api/scan-templates
POST /api/targets/validate  {raw:[…], overrides:{allow_private,allow_loopback}}
     → 每个目标 {ok, normalized, kind, resolved_ips, ip_class, allowlist_entry,
                 code?, overridable?, note_code?}
     ⚠️ **刻意没有 `registrable_domain`**（2026-09-11 用户拍板去掉）。理由见 T8 行。
     T8 落地时另加 4 个字段，逐个的理由在 `routes/targets.py` 的字段 docstring 里：
       `raw`（列表行的 key，按下标对齐会在重构里静默错位）· `requirement`（放行**条件**，
       与 `ok` 正交）· `required_opt_in`（还缺哪几项勾选 —— `localhost` 可同时缺两项，
       从 `ip_class` 推不出来）· `resolution_error`（DNS 失败是一行提示，不是 500）。
     响应是信封 `{targets:[…]}` 而非裸数组：顶层是数组就没法再加字段。**恒返回 200。**
GET/PUT /api/allowlist  ·  POST/DELETE /api/allowlist/entries[/{id}]
     GET → {config|null, effective_mode, file_present, stale, file_error, file_error_line}
     增量改（POST/DELETE）在文件坏掉时 **409 `allowlist_file_broken`**（新增码）——
     否则"加一条"会静默变成"删掉其余全部"。PUT 是恢复通道，它整份覆盖。

POST /api/scans   {vault_handle, template_id, targets[], overrides, scan_mode?, max_budget_usd(必填),
                   max_turns, reasoning_effort?, extra_instruction?, credentials?[], spec_upload_id?,
                   authorization:{operator_name, authorization_ref, typed_confirmation,
                                  affirmed:[3项], resolved_ips_seen}}
     → 202 {scan_id, status:"starting", ws, argv_preview(无 Key), budget_usd}
     → 403 blocked_metadata | 409 not_in_allowlist|dns_changed|key_required|concurrency_limit
            |missing_typed_confirmation|docker_unavailable|budget_exceeds_ceiling
GET  /api/scans · GET /api/scans/{id} · DELETE /api/scans/{id}
POST /api/scans/{id}/stop {mode:graceful|force}   ·  POST /api/scans/{id}/resume {vault_handle}
GET  /api/scans/{id}/{events,findings,artifacts,artifacts/{name},log,media/{sha}.png}
WS   /ws/scans/{id}   ·  WS /ws/system   ·  GET /api/scans/{id}/stream (SSE 兜底)

POST /api/scans/{id}/report/zh {vault_handle, force}  ·  GET /api/scans/{id}/report/zh
GET  /api/scans/{id}/report/{print,docx,markdown,sarif}
GET  /api/audit  ·  GET /api/audit/export.csv
```

**错误响应只有一种形状：`{code, trace_id, params}`，框架自己产出的那些也不例外**（T2 已落地）。
Starlette 默认的 404 是 `{"detail": "Not Found"}` —— 没有 `code`，而前端被要求"按码分支、
不得匹配文案"（`CLAUDE.md` §错误与文案），两种形状就意味着前端要写两个解析器。
更糟的是它只在"出了意料之外的事"时出现（重构后请求了一个拼错的路径），那正是最需要看到
明确错误码的时刻，而那时前端只会显示一片空白。所以 `main.py` 覆盖了
`StarletteHTTPException` 的处理器：**404 → `not_found`，405 → `method_not_allowed`**
（两个码分开：405 几乎总是前端写错了动词，404 通常是路径拼错或前端版本过旧，合成一个码会让
前者伪装成后者），其余 4xx→`invalid_request` / 5xx→`internal_error` 兜底。
响应状态码保留框架原值（只修正响应体形状，不改 HTTP 语义），并转发 `exc.headers` 以保住
405 的 `Allow`。已实测经真链路：`404 {"code":"not_found",…}` / `405 allow: GET` +
`{"code":"method_not_allowed",…}`，且 `trace_id` 与 `X-Trace-Id` 一致。

**退出码映射**（`ScanSupervisor`）：`0`→已完成（未发现漏洞）；`2`→已完成（发现漏洞）；`1`→失败。
⚠️ **原写的「`-15`/`-9`→已手动停止」是错的，2026-09-14 读 1.5.3 源码证伪**：`interface/cli.py:124-135`
给 SIGTERM／SIGINT／SIGHUP **都装了处理器**，里面 `report_state.cleanup(status="interrupted")` 之后
`sys.exit(1)` —— 进程是**正常退出**的，`returncode` 是 **`1`**，不是 `-15`。所以"是不是被人停的"
**唯一可靠判据是「我们自己发过信号」这个事实**（`ScanSupervisor` 自己记的 `stopped_by`），
按退出码判会把"用户点了停止"报成"扫描失败"、还会去 stdout 里瞎归因。只有 SIGKILL 才给 `-9`。
同一条源码还说明**没有"更优雅的信号"可选**（两个信号同一个处理器），优雅停止只能是
「发 TERM → 等宽限 → SIGKILL」，且 `interface/cli.py:201-205` 那个 async 的
`session_manager.cleanup` 跑不完 → **强杀一定泄漏沙箱容器，只能靠 T11 按 label 回收**。

`1` 的归因**不能只看 Rich 面板标题** —— M0 实测：TLS 被中间设备解密、凭据无效、
凭据形状与模型路由不匹配、**以及路由不接受 Strix 注入的某个参数**，这**四**种毫不相干的
故障，Strix 打的是**同一个** `LLM CONNECTION FAILED` 面板。只按标题分类会把后三种都报成
"连不上"，把用户送去查网络而不是查凭据或参数。所以**先匹配正文里的异常类名，标题只做兜底**
（判据顺序已在 `scripts/m0_probe_inner.sh` 实测通过，T3 直接照搬）：

| 正文特征（优先级从高到低） | 机器码 |
|---|---|
| `CERTIFICATE_VERIFY_FAILED` / `SSLCertVerificationError` | `llm_tls_intercepted` |
| `object has no attribute 'access_key'` | `bedrock_route_rejects_bearer`（指引：模型名改成 `bedrock/invoke/<model>`，见 §N1） |
| `cache_control_injection_points` | `prompt_cache_unsupported_on_route`（指引：`invoke/` 路由要 `STRIX_PROMPT_CACHE=false`，见 §N1）。**必须排在 `ValidationException` 之前**，否则被归成 `model_access_denied`，把人送去 AWS 控制台申请模型权限 |
| `AuthenticationError` / `security token included in the request is invalid` / `Incorrect API key` / `Invalid API Key` | `invalid_api_key` |
| `AccessDenied` / `not authorized to perform` / `ValidationException` | `model_access_denied` |
| `NotFound` / `model_not_found` / `MODEL NOT FOUND` | `model_not_found` |
| `MISSING REQUIRED ENVIRONMENT VARIABLES` | `missing_required_env` |
| `docker` + `permission denied` | `docker_permission_denied` |
| 只剩面板标题 `LLM CONNECTION FAILED` | `llm_connection_failed`（兜底） |
| `DOCKER NOT INSTALLED` / `FAILED TO PULL IMAGE` / `SCAN PREPARATION FAILED` | 同名机器码 |

`llm_tls_intercepted` 的中文指引必须指向"你在企业 TLS 解密后面"这条真因与 CA bundle 挂载口子
（见 §M0 带出的两条产品需求），而不是笼统的"检查网络"。
**与 `run.json.status` 交叉校验，冲突时以 run 记录为准**（子进程可能在写完终态后才被 SIGKILL）。

⚠️ **退出码 `0` 不代表扫描跑完了 —— 这条已实测，是产品级安全要求，不是防御性冗余。**
M0 第 3 次运行：预算耗尽被掐死（`run.json.status = "stopped"`），strix 仍**退出 0**、
面板写 `Vulnerabilities 0 (No exploitable vulnerabilities detected)` —— 而目标是 juice-shop，
一个**故意塞满漏洞**的靶场。原因是退出码的唯一来源 `interface/main.py:494-497` 只判
"`report_state.vulnerability_reports` 是否非空"，**完全不携带"扫描是否完成"的信息**：

| 实际发生的事 | 退出码 | `run.json.status` |
|---|---|---|
| 跑完了，目标确实干净 | `0` | `completed` |
| **预算/轮次耗尽，什么都没查到** | `0` | `stopped` |
| 找到漏洞（无论是否跑完） | `2` | `completed` / `stopped` |

→ **只凭退出码 0 展示"未发现漏洞"是发布阻断项。** 必须读 `run.json.status`
（取值域 `core/agents.py:25`：`running/waiting/completed/stopped/crashed/failed/budget_paused`，
**外加一个 `interrupted`** —— 2026-09-14 读源码补：`report/state.py:399` 的 `cleanup()` 被信号处理器
以 `status="interrupted"` 调用，而 `save_run_data`（`382-394`）里 `status == "stopped"` 遇到当前值是
`failed`/`interrupted` 时**保留旧值**，所以信号停止的 run 最终留在盘上的是 `interrupted`。
白名单少了它，最常见的操作者停止路径会被判成"未知状态"），
`stopped` 时中文结论固定为**「扫描因预算耗尽提前结束，结论不完整」**，机器码 `scan_incomplete`。
**`stopped`（预算/轮次耗尽）与 `interrupted`（收到信号）因此是可区分的**，别把两者合并。
同一段源码还给出"冲突时以 run 记录为准"的精确落点：`completed` 一旦写下就**再也不会被覆盖**
（`save_run_data` 的 `elif` 守卫），所以「操作者在进程即将正常退出那一瞬点了停止」应判 `completed`。
一个渗透测试控制台在钱花光时报"目标干净"，比不报任何结论危险得多。

**预算的真实语义（同次实测，UI 文案必须照这个写，不许自己编）**：
`--max-budget-usd` 是**软上限** —— 给 `2` 实花 `$2.0572`（超 2.9%），因为
`core/hooks.py:55` 的 `cost >= max_budget_usd` 是**每轮结束后**才判，必然超一轮的量。
另有一道 **90% 子代理保留线**（`_SUBAGENT_BUDGET_RESERVE = 0.90`，`core/hooks.py:29`）：
子代理花到 90% 就停，留给 root agent 收尾。所以是**子代理 90% 停 / root 100% 停 / 再超一轮**。
UI **不许**承诺"绝不超过 $X"，只能说"达到 $X 后停止"。

---

## Compose 设计要点

```yaml
name: strix-console
x-console-env: &console-env
  CONSOLE_DATA_DIR: "${STRIX_HOST_DATA_DIR}"          # 容器内外同一绝对路径
  CONSOLE_EPHEMERAL_HOME_ROOT: "/run/strix"           # tmpfs：每任务 HOME + --config
  STRIX_DOCKER_SANDBOX_NETWORK: "strix_sandbox"       # ← DooD 关键
  STRIX_IMAGE: "ghcr.io/usestrix/strix-sandbox:1.3.0"
  STRIX_TELEMETRY: "false"
  STRIX_NO_UPDATE_CHECK: "1"
  STRIX_RUN_TYPE: "console"                            # RUN_ID 每任务单独设
  STRIX_SANDBOX_MEM_LIMIT: "6g"   # 以下均为 docker_client.py:66-107 的真实变量
  STRIX_SANDBOX_CPUS: "4"
  STRIX_SANDBOX_SHM_SIZE: "1g"
  STRIX_SANDBOX_PIDS_LIMIT: "2048"
  LITELLM_LOG: "ERROR"
  # STRIX_DEBUG 故意不设 —— 它会把 strix.log 拉到 DEBUG（泄漏面 #5）
services:
  api:
    volumes:
      - "${STRIX_HOST_DATA_DIR}:${STRIX_HOST_DATA_DIR}"   # ${X}:${X} 就是全部诀窍
      - /var/run/docker.sock:/var/run/docker.sock
    tmpfs: ["/run/strix:rw,noexec,nosuid,nodev,size=16m,mode=0700"]
    networks: [default, strix_sandbox]
    user: "0:0"                       # 需写 docker socket；Docker Desktop 的 socket 属主不可移植
    security_opt: ["no-new-privileges:true"]
    init: true                        # PID 1 收割 strix 子进程与 docker-cli 僵尸
    stop_grace_period: 45s            # > ScanSupervisor 的 30s 优雅窗口
    expose: ["8000"]                  # 不发布端口，只经 nginx 访问
  web:
    expose: ["3000"]                  # 不发布端口，只经 nginx 访问
    read_only: true
    tmpfs: ["/tmp:size=64m", "/app/.next/cache:size=256m"]
  nginx:
    image: nginx:1.27-alpine          # 唯一发布端口的服务；只做 TLS 终止 + 反代，不跑业务
    ports: ["127.0.0.1:${CONSOLE_WEB_PORT:-443}:443"]     # 绑 loopback；不监听 80
    volumes:
      - "./nginx/nginx.conf:/etc/nginx/nginx.conf:ro"
      - "${STRIX_HOST_DATA_DIR}/tls:/etc/nginx/tls:ro"    # setup.sh 生成，私钥 0600，.gitignore
    read_only: true
    tmpfs: ["/var/cache/nginx:size=64m", "/var/run:size=1m"]   # 见下方「为什么是 64m」
    security_opt: ["no-new-privileges:true"]
    depends_on: [api]                 # T5 落地 web 之后补成 [api, web]；web 不存在时写它 compose 直接报错
networks:
  strix_sandbox:
    name: strix_sandbox               # 固定名：Strix 拿这个字面串传给 containers.create(network=…)
```

刻意的三个"不做"：
- **不用 named volume** —— 其宿主路径在 Docker Desktop VM 内，两侧不一致，会静默重现路径别名 bug
- **不用 docker-socket-proxy** —— Strix 需要 `containers/create` 带 mounts/cap_add/extra_hosts/
  log_config/network，加上 images/pull、exec、archive、inspect、networks/inspect，几乎是全部权限；
  放开这些本就等价于 root，只换来表演性安全和排查噩梦。改为在 `docs/SECURITY-zh.md` 里如实写明
  **本工具具有等同宿主 root 的能力，只应在自己机器上运行，不要暴露到局域网**
- **只有 `nginx` 发布端口** —— `api` 与 `web` 都只 `expose`，防火墙配错也暴露不出扫描发起接口

**`nginx.conf` 有四处必须显式写对**（Caddy 是默认行为，nginx 不是；漏任何一条都会**静默**破坏实时流）：

| # | 指令 | 漏了会怎样 |
|---|---|---|
| 1 | `map $http_upgrade $connection_upgrade` + `proxy_set_header Upgrade/Connection` | WebSocket 握手失败，实时流全灭 |
| 2 | `proxy_buffering off;`（`/api/scans/*/events` 与 `/ws`）| 默认缓冲响应 → **SSE 兜底通道卡死** |
| 3 | `proxy_read_timeout 3600s;` | 默认 60s → 扫描安静超过 1 分钟即被 nginx 掐断（拉镜像阶段常态） |
| 4 | `client_max_body_size 16m;` | 默认 1MB → 截图/报告类请求被 413 拒 |

**为什么 `/var/cache/nginx` 是 64m 而不是 16m**（本文档原写 16m，2026-09-08 T4 实测改正）：
`client_max_body_size` 本身就是 16m，而请求体一旦超过 `client_body_buffer_size` 就会落
`/var/cache/nginx/client_temp` 下的临时文件。tmpfs 与上限同大时，**一次刚好 16 MB 的合法上传正好
把 tmpfs 撑爆**，表现是 500 而不是"磁盘满"，排查方向会被完全带偏。tmpfs 按实际占用吃内存，
64m 只是上限、不预留，代价为零。这两个数字必须一起改：**改 `client_max_body_size` 就要重算它。**

**`access_log` 用 `$uri` 只管住访问日志。** 已实测：nginx 的 **error log 会打完整 `$request` 与上游
URL（含 query string），且格式不可配置**，级别提到 `crit` 才压得住，但那会连 502/504 的归因一起丢掉。
所以「query 里不出现敏感值」**是后端必须自己守的约束，不是 nginx 提供的保证** —— 写进
`docs/SECURITY-zh.md`，并且验收 #11 扫日志时要把 `nginx` 的 stderr 一起扫。

选 nginx 而非 Caddy 的理由：20 年生产史、任何人都能读 `nginx.conf`、排障资料多一个量级。
代价就是上面这四条（Caddy 的自动证书能力我们用不上 —— 证书走 `openssl` 自签，见「已确认决策」）。

---

## 里程碑

**M0 — 契约实测（半天）。写任何业务代码之前必须做。**
先起靶场 —— OWASP 官方故意漏洞靶场，自己机器上，授权链天然成立，**不需要另找目标**。
**必须把靶场接进 `strix_sandbox` 网络并用容器 DNS 名寻址**：
```
docker run -d --name m0-juice-shop \
  --network strix_sandbox --network-alias juice-shop \
  -p 127.0.0.1:13000:3000 \
  --label strix-console-role=m0-target \
  bkimminich/juice-shop:latest
```
⚠️ **原稿这里还有一个 `--label strix-run-type=console`，已删（2026-09-11，T3 实测发现）。**
那个 label 是 Strix 打在**沙箱**容器上的、也是 `make reap`(T11) 的删除选择器 ——
给一个我们永远不想被回收的靶场打上它，等于把它排进了待删清单。当时已经起来的那个
`m0-juice-shop` 仍然带着它（`docker inspect` 实测确认），所以**只改文档不够**，回收侧
必须同时要求 `strix-run-id` 非空（见 T11 行与验收 21）。那道判据永不误伤真沙箱，理由是
`docker_client.py:113` 的早退让 Strix 结构上产不出"有 run-type、无 run-id"的容器。
**不要为此重启 `m0-juice-shop`** —— 重建它没有收益，而 reap 侧的判据已经把它排除了。

⚠️ **目标 URL 不能写 `http://localhost:13000`**（本节原稿的写法，已实测是错的）：扫描发生在
**沙箱容器内**，那里的 `localhost` 就是沙箱自己，宿主的发布端口根本不在那个 netns 里。
2026-09-08 从 `strix_sandbox` 上实测：

| 目标 URL | 结果 |
|---|---|
| `http://juice-shop:3000` | ✓ → `172.19.0.3` HTTP 200 —— **采用这个** |
| `http://host.docker.internal:13000` | ✓ → `192.168.65.254` HTTP 200，但**仅 Docker Desktop 有**，Linux Engine 无此 DNS |
| `http://localhost:13000` | ✗ 解析到 `127.0.0.1`，连接失败 |

选容器 DNS 而不是 `host.docker.internal` 的理由：后者是 Desktop 注入的，而本项目承诺支持 Linux，
把它写进流程会让流程本身不可移植。容器 DNS 在两边行为一致，且不占宿主端口，
顺带还练到了 `authorization_id` 复解析要走的同一条 DNS 路径。`-p 127.0.0.1:13000` 只为人工眼验保留。

构好 `api` 镜像（派发清单 T0），容器内手跑一次：
```
HOME=/run/strix/probe TMPDIR=$CONSOLE_DATA_DIR/scans/probe/tmp STRIX_RUN_ID=probe \
strix -n -t http://juice-shop:3000 -m quick --max-budget-usd 2 --max-turns 20 \
      --config /run/strix/probe/.strix/cli-config.json      # 预置 {"env":{}}
```
Key 只经 `LLM_API_KEY` 注入（已核实 `config/settings.py:27-31`，`validation_alias=AliasChoices("LLM_API_KEY","OPENAI_API_KEY")`），
**绝不用 `docker exec -e`** —— 那会把 Key 写进宿主 `docker` 客户端的 argv。见 `scripts/m0_probe.sh`。
逐条断言：
1. `strix_runs/<slug_hex>/` 出现在该任务 cwd 下
2. `grep -r "$KEY" $CONSOLE_DATA_DIR/` → **0 命中**；`grep -r "$KEY" /root/` → 0；
   而 `/run/strix/probe/.strix/cli-config.json` **里有** Key（证明重定向生效），`rm -rf` 后消失
3. `docker inspect $(docker ps -q --filter label=strix-run-id=probe) | grep -i "$KEY"` → 0
4. `strix.log` 里 `Caido host endpoint resolved:` 是**容器 IP**（`172.*`/`10.*`）**而不是 `127.0.0.1`**
   —— 这是全计划最高风险的未知项。
   出处 `runtime/session_manager.py:180`，是 `logger.debug(...)`。**不需要也绝不许设 `STRIX_DEBUG`**：
   已核实 `telemetry/logging.py:152` 的 file handler 恒为 `DEBUG`、`:166` 把 tracked logger 也设成 `DEBUG`，
   `STRIX_DEBUG` 只改 `:158` 的 **stderr** handler 级别。所以 `strix.log` 本来就有这行。
   **推论（安全）**：`strix.log` 恒为 DEBUG 级、且由 Strix 子进程自己写 —— 我们的 `RedactionFilter`
   在那个进程里并不存在。所以它是一块常驻的落盘泄漏面，断言 2 的全目录 grep 是**实打实的门**，不是形式。
5. 跑完 `docker ps` 无残留沙箱

**过不了第 4 条就走 R1 的 Plan B/C，不要继续往下建。**

**第 6 条断言（2026-09-08 补，血的教训）：strix 自己的退出码必须 ∈ {0, 2}。**
上面五条**全部成立，也可能只是因为什么都没发生** —— 已真实出现过一次：五条断言全绿、
探测脚本打印"M0 达成"并退出 0，而 strix 退出 1、**零个 LLM 轮次完成**。原因是断言 2
（凭据卫生）本就不依赖扫描是否跑起来，而 1/3/5 在早期失败时会因为"目录没建 / 没有沙箱 /
因此也没有残留"而各自成立。**"所有检查都通过"和"被检查的东西真的发生了"是两个命题。**
`scripts/m0_probe_inner.sh` 已加这第二道独立的门（不满足则退出 4）。

**实测进度（2026-09-08，两次真凭据运行）**：
- 断言 1 ✓（`juice-shop-3000_9c0d`，5 个文件，落在任务 cwd）
- 断言 2 ✓（四小条全过）。⚠️ **但通过的原因不是本条设计得好，是我们恰好测了唯一不落盘的那个形状。**
  `persist_current()` 在 `interface/main.py:404` **无条件调用**（预置 config 从 11 字节被写到 223 字节
  就是证据），它把凭据明文写进 `cli-config.json` —— 只是 `AWS_*` 四个变量都不在 Strix 的 alias 表里。
  **`LLM_API_KEY` 在**（`config/settings.py:27-31`），所以 `single` 形状（Anthropic / OpenAI / Gemini /
  DeepSeek，即**大多数**供应商）**一定会明文落盘**。已用假凭据实测，详见 `pitfalls` 条 25。
  → **本节原稿"`cli-config.json` 里**有** Key"是对的**；tmpfs HOME + 显式 `--config` + `finally: rmtree`
    对 `single` 形状而言不是纵深防御，**是唯一一道防线**，三样谁都不许简化。
  → **断言 2 必须按 auth_shape 各跑一遍**，`test_key_hygiene` 要参数化。一次实测只证明被测的那个配置。
- 断言 3 ✓（沙箱 label `strix-run-id` + `strix-run-type` 都在，Reaper 前提成立；inspect 无凭据）
- **断言 4 ✓ —— `Caido host endpoint resolved: http://172.19.0.4:48080`，容器 IP。
  R1 关闭，Plan B/C 不需要了。全计划最高风险的未知项已清。**
- 断言 5 ✓（无残留）
- 断言 6 ✓（第 3 次运行，2026-09-08：`bedrock/invoke/us.anthropic.claude-sonnet-4-5-20250929-v1:0`
  + 自动 `STRIX_PROMPT_CACHE=false`，strix 退出 0，13 次 LLM 请求，96 秒，`$2.0572`）

**→ M0 六条断言全部实测通过。可以开工。**

但这次运行带出两条**改变设计**的发现，都已并入本文档（详见 `pitfalls` 条 24、25）：

1. **退出码 0 不代表扫描跑完了。** 本次是被预算掐死的（`run.json.status = "stopped"`，
   `strix.log`：`Token budget of $2.00 exceeded (spent $2.0572)`），可 strix 退出 **0**、
   面板写 `Vulnerabilities 0 (No exploitable vulnerabilities detected)` —— 在 juice-shop 上。
   退出码的唯一来源是 `interface/main.py:494-497`，**只看有没有找到漏洞**。
   → 见下方 §后端接口的 `run.json.status` 交叉校验；那条不是冗余，是唯一真相来源。
2. **凭据卫生的结论按 auth_shape 而定**（见上方断言 2）。

| # | 目标 | 天 |
|---|---|---|
| M1 | 骨架：compose + 两个 Dockerfile + `setup.sh` 校验器；FastAPI 工厂/设置/脱敏日志/迁移 + 无密钥列断言；`/api/system/status` 含**同路径主动探测**（写哨兵文件 → `docker run --rm -v <p>:<p> alpine cat`）与网络挂载自检；Next.js 外壳 + 全中文文案表；**TLS 全套**：`setup.sh` 生成自签证书（SAN+EKU，私钥 0600）+ 校验 443 未被占用 + 打印 `security add-trusted-cert` 指引、`nginx.conf`（四条必须指令）、compose 加 `nginx` 并把 `web` 改 `expose` | 3 |
| M2 | KeyVault（TTL sweeper、`--workers 1` 校验）；`POST /api/keys` 真实验活；TargetGuard 全分类 + ~80 用例（punycode、split-horizon、`user:pass@`、`.git`、各种元数据地址、IPv6）；白名单加载/校验/热重载/编辑 | 2 |
| M3 | ScanLauncher（argv+env+tmpfs HOME+预置 config+cwd/TMPDIR+RUN_ID）；ScanSupervisor（退出码→中文、优雅停止、`finally` 清 tmpfs）；RunDiscovery（glob+超时+启动失败分类）；Reaper（启动/定时/每次停止后）；镜像预拉取带 WS 进度；`POST /api/scans` 全套授权不变式；6 个模板的黄金 argv 测试 | 3 |
| M4 | RunProjector（epoch/重同步）、EventMirror（截图落地）、LogTailer、ScanChannel、WS+SSE、重连回放；**现在就造压缩夹具**（用 `STRIX_CONTEXT_BUFFER_TOKENS=1` + `STRIX_MAX_CONTEXT_IMAGES=1` 强制触发）；前端实时面板 | 3 |
| M5 | 五步向导（含授权步：三勾选 + 逐字输入、期望串显示在框**旁边**）、6 模板、测试账号收集、高级面板、费用预估；发现 tab；服务端重校验 + DNS 变更检查 | 3 |
| M6 | Translator（逐条 + executive、`Semaphore(4)`、JSON 修复、缓存表、费用核算）；**先只出打印 CSS HTML 导出**；报告 tab；md/csv/sarif 直通 | 3 |
| M7 | 专家 tab（复用同一 store）；审计 UI + CSV；系统诊断页（M3 每个失败模式都有中文修复指引）；原生 viewer 按钮（开关后） | 2 |
| M8 | DOCX 导出；续跑（重新索要 Key）；并发队列；留存清理任务；`test_strix_contract.py`（**升级预警线**）；`README.md` + `docs/` 四份文档；`make verify-e2e` | 2 |

单人约 **21 个工作日**（M1 因 TLS 从 2 天增至 3 天）。**M0 永远第一。**

**已完成任务的复核记录**（派发清单只留 ✅ 与日期，细节记在这里，两处别都写）：

- **T2**（2026-09-08，子 agent 交付 + 我独立复跑）：脱敏挂在 **Formatter** 上（子 logger 实测被脱敏）／`uvicorn.access` 是**静音**不是接管（否则静默作废 `--no-access-log`，`pitfalls` 条 33）／五条结构不变式真的会拦且回滚干净／**投毒验证**：给 `scans` 加 `api_key` 列后 api 退出码 3 报 `SecretColumnError`。**三处由我补**：① compose `target: runtime`（Dockerfile 里 `test` 是最后一个阶段，不写 target 则生产镜像带上 pytest/tests）；② `logging_setup` 的 `converter = staticmethod(time.gmtime)`（`pitfalls` 条 37）+ 自改进程时区的测试（否则该测试恒真）；③ 404/405 的响应形状。
- **T4**（2026-09-08，同上）：验收 23／24、证书 SAN+EKU+`CA:FALSE`、唯一发布端口 `127.0.0.1:443`、`api` 8000 宿主不可达、N2 默认关闭，均已复跑。`setup.sh` 新增 C17c 端口占用（**只查不占，绝不杀进程**）、C17d 证书三态机（**`ok` 态永不覆盖**，`STRIX_REGEN_CERT=1` 才重签且先备份）、C17e N2 校验（真 `docker run` 挂一次，防 Docker Desktop File sharing 未覆盖时静默给空文件）。

---

## 派发清单（`agent-rules.md` §二.5 要求的五要素）

**模板**：`1`=普通 subagent｜`2`=Superpowers 自助闭环｜`3`=Superpowers 先上报方案｜`自`=不可派发，必须手动做。

**判据（2026-09-08 重校一遍时定的，之前是凭感觉标的，错了 5 处）**——
不要按"任务难不难"分，按**改错的代价**和**规格明不明确**分：

| 标 | 判据（一句话） | 反问自己 |
|---|---|---|
| **3** | **本任务定的约定会被下游多个任务继承**，或**改动表结构**，或**是前端页面** | "后面有几个任务要照着这个写？" ≥2 就是 3。方案阶段改一句话，实现阶段改就是几十个调用点 |
| **2** | 规格已明确，但**失败模式隐蔽**、自测本身需要动脑设计 | "我能不能现在就写出可执行的验收断言？能，但断言得设计" |
| **1** | 规格已被上游任务钉死，本任务是**照着写** | "这里面有没有选型？"没有才是 1 |
| **自** | **需要真凭据或真扫描** | 派发规则第 3 条禁止把 Key 给子 agent，所以这类任务结构上不可派发 |

**两条容易搞反的**：
- **破坏性操作要往上抬，不要按逻辑复杂度往下压。** `Reaper`（T11）逻辑上就是"按 label 过滤 + 定时"，
  看着像模板 1，但它**删容器**，而本机还跑着别人的项目 —— 后果不对称，所以是 2 且 prompt 必须写死
  label 精确过滤 + `--dry-run` 先打印。留存清理（T28）同理。
- **`agent-rules.md` §四 明写"前端页面开发必须用模板 3"，这是硬规则，不许因为"这个页面简单"就降。**

| # | 任务 | 前置 | 涉及文件 | 模板 |
|---|---|---|---|---|
| T0 | 最小 `api` 镜像 + compose 骨架（`strix-agent==1.5.3` 精确 pin + `--only-binary=:all:` + hash lock + 同路径挂载 + `strix_sandbox` 网络）| — | `backend/Dockerfile` `backend/pyproject.toml` `docker-compose.yml` `.env.example` `setup.sh` | **3** ✅ |
| T1 | **M0 契约实测** | T0 | 无（只跑命令）| **自** ✅ 2026-09-08 六条断言全过（记录在 §里程碑 M0）。**断言 2 只覆盖 `bedrock-apikey` 形状，`single` 形状要在 T7 复跑一遍** |
| T2 | FastAPI 骨架 + `assert_no_secret_columns()` + `RedactionFilter` + 建表迁移 | T1 | `app/{main,settings,db,models,logging_setup}.py` `migrations/001_init.sql` | **3** ✅ 2026-09-08（复核记录见 §里程碑「已完成任务的复核记录」；**404/405 的响应形状**见下方独立一段）|
| T3 | `/api/system/status`：同路径主动探测（哨兵文件 + `docker run -v <p>:<p>`）、网络挂载自检 | T2 | `services/docker_probe.py` `routes/{health,system}.py` | 2 ✅ 2026-09-11。**接口刻意不带缓存**（缓存会在用户刚修好之后继续说没修好）；启动期一次都不探（探不到 docker 就起不来 = 没有界面能告诉用户是 docker 的问题）。<br>**前端收尾 ✅ 2026-09-13（主会话自己做，没派 agent）**：`zh-CN.json` 新增第四棵码树 `systemStatus.blockers.*`（六个阻断码，纯字符串；**刻意不进 `errors.py`**，理由与 `targetGuard` 同）+ 新建 `components/system/ReadyRows.tsx`（客户端叶子，`Panel` 与标题仍在服务端页面上）。三态判定落成一条：**只有 `true` 才画实心点**，`false` 用 `warn`（方块 = 需要你处理，`StatusDot` 已有这一档，不新加红色 token），`null` 与"还没请求"一律 `idle` —— 值那一列分「尚未检测」（还没请求）／「未知」（试过了没答案）两句话。<br>**两处刻意偏离原计划，都已在代码里写清理由**：① **四行变五行**，新增「沙箱网络已就绪」—— `sandbox_network_missing` 与 `api_not_on_sandbox_network` 在首页原先没有任何位置，于是沙箱网络坏掉时那个面板会四行全绿而扫描根本发不起来（`ready_for_scan:false`），一个说"就绪"却不能扫的面板正是这块 UI 要防的东西；② 那次请求**挂在 `["session"]` 查询的 `enabled` 上** —— `/api/system/status` 是全站第一个需要身份的接口，未登录直接打它会 401 → 弹会话失效遮罩，而 `RequireSession` 见到 `expired` 就刻意不再跳转，结果"没登录进首页"会从"静默跳登录页"退化成"停在首页看遮罩"。<br>**新增 3 条测试**（`test_message_coverage.py` 第五节，双向比对 `ALL_BLOCKER_CODES` + 纯字符串形状），**2 次 mutation 验证过它们能变红**（改名一个阻断码 → 只有那两条双向比对红；把它改成 `internal_error` → 再多两条红，其中一条是 `test_system_status.py` 里**早就存在**的同义断言，于是我把自己那条重复的 disjoint 测试删了，§十.4）。<br>**唯一没验的一环，如实记账**：`true`/`false`/`null` 三态在**真浏览器里**长什么样没看过 —— 那要登录（面板在鉴权之后），而口令只有用户有。类型检查 + 生产构建 + 读代码都过了，真机确认留给用户或 `make verify-e2e`（T30b） |
| T4 | TLS 全套：证书生成（SAN+EKU）、`nginx.conf` 四条必须指令、compose 加 `nginx` | T0 | `setup.sh` `nginx/nginx.conf` `docker-compose.yml` `env.example` | 2 ✅ 2026-09-08（复核记录见 §里程碑「已完成任务的复核记录」；带出 `pitfalls` 条 26–31 与泄漏矩阵 #14／#15）|
| T4b | **单账号登录 —— 后端**：`auth.py`（scrypt + 随机 salt + `hmac.compare_digest`）、`${DATA}/auth.json`(0600) 读写、会话 = 进程内存不透明 id、`POST /api/auth/{login,logout}` + `GET /api/auth/me`、**全局路由依赖**（唯一豁免 `/api/health` 与三个 auth 路由本身）、失败限流、`setup.sh` 建初始账号（口令经 `read -rs`）、`audit_log.actor` 落真实用户名 | T2 T4 | `app/services/auth.py` `app/routes/auth.py` `app/main.py` `setup.sh` `tests/test_auth.py` | **3** ✅ 2026-09-08。当时写死的四条硬约束（散列绝不进 SQLite、不引 `argon2-cffi`／`passlib`／`python-jose`／`pyjwt`、不装 `CORSMiddleware`、会话不签名不自包含）已收进 `CLAUDE.md` §安全不变式 与本文 §单账号登录 |
| T5 | **前端视觉方向**（定一次，产出项目设计约定）+ Next.js 外壳 + 全中文文案表 | T2 | `frontend/messages/zh-CN.json` `frontend/src/app/*` `frontend/Dockerfile` `docker-compose.yml`(只加 `web`) | **3 + frontend-design** ✅ 2026-09-09。三条**仍然生效**的禁令（不引 Vite／Rollup／esbuild 作独立构建层、不 `next export`、`web` 只 `expose` 不 `ports`）已搬进 `frontend/CLAUDE.md` |
| T5b | **单账号登录 —— 前端**：登录页 + 未登录重定向 + 401 统一拦截（复用 `key_required` 那套交互，因为会话与 KeyVault 同生共死）+ 登出 | T4b T5 | `frontend/src/app/login/*` `frontend/src/lib/api.ts` `frontend/messages/zh-CN.json` | **3** ✅ 2026-09-10。两条定死的：口令框 `type=password` + **标准 `name` + `autoComplete="current-password"`**（2026-09-10 拍板，**推翻本行原先写的"随机 `name`"**，理由见下行）；**会话 id 由 cookie 承载，前端一行都不许碰它** —— 不读、不存 `sessionStorage`、不放 URL（与 `vault_handle` 刻意相反，理由见 §单账号登录 方案表最后一行）|
| | **↑ 为什么登录口令框与 API Key 输入框的规则相反**（别把这两条并成一条）：「随机 `name` 破自动填充」的出处是**泄漏矩阵第 11 行**，讲的是 `POST /api/keys` 的 **API Key 输入框** —— 对别人家的 provider 密钥，被浏览器存下来是净损失，**那一条不变**。登录口令是相反情形：`setup.sh` 强制 ≥12 位，不让密码管理器帮忙，用户就会挑记得住的弱口令或抄进便签，比让浏览器记住更糟；且要挡的两类人（本机另一个 OS 账号、浏览器里其它标签页）都拿不到自动填充，而坐在你已解锁浏览器前的人早就有 `HttpOnly` 会话 cookie 了。**已知且接受的残余风险**：若开了 iCloud 钥匙串／Chrome 同步，这个口令会**离开本机**，而页脚文案写的是「数据与凭据都不离开这台机器」（该句指扫描数据与 **LLM API Key**，后者从不进浏览器存储）。`autoComplete` **没有**"可填充但不要同步"这种值，所以只能如实记录、不能靠代码消除 —— **必须写进 `docs/SECURITY-zh.md`（T30a）** | | | |
| T6 | `target_guard.py` 纯函数全分类（punycode、split-horizon、`user:pass@`、元数据地址、IPv6）| T2 | `services/target_guard.py` `tests/test_target_guard.py` | 2 ✅ 2026-09-11，**181 个用例**（原估 ~80）。判定核心是 `_policy_for()` 一个函数 = §护栏 那六行的可执行版；**`requirement`／`overridable` 与 `allowed` 正交**，所以勾上之后放行入口不会从界面上消失。三件交回的事见 T8 行 |
| T7 | `key_vault.py`（TTL sweeper、`ref_count`、`--workers 1` 启动校验）+ `POST /api/keys` 真实验活 | T2 | `services/{key_vault,llm_client}.py` `routes/{keys,providers}.py` | **3** —— `auth_shape` + `secrets` + `params` 的契约被 **T9／T18／T19 三个任务继承**，`/api/providers` 声明的"每种形状要哪几个键"是前端渲染 1／2／3 个输入框的唯一依据，改错一处要动三处。<br>**⚠️ 已按"派发前必须拆"执行（2026-09-12 用户放行）**：本行涉及文件跨「服务＋路由＋文案＋测试」，正是 T8 超支的结构原因（`agent-rules.md` §九.5）。拆成 **T7a**=`services/key_vault.py` 内存实现 + 单测 + `main.py` 五处接线（**2026-09-12 已落地并复验，闸门全绿**；实测代价：方案 24 次调用 + 实现 45 次工具调用 / 54 分钟，0 次压缩 —— 拆分有效）、**T7b**=`routes/{keys,providers}.py` + `services/llm_client.py` 真实验活 + `zh-CN.json` + 路由层测试（**2026-09-13 已落地并复验**：`llm_client.py`(332) + `keys.py`(272) + `providers.py`(72) + `test_keys.py`(33 条) + `main.py` 3 处接线 + `zh-CN.json` 的 `providers.*`；闸门 **641 passed / 0 skipped**、ruff 干净）。<br>**T7b 是"方案由主会话写 + 快闸门"的第一次执行，账单降 43%**（44 次调用、0 压缩、平均上下文 97.9k 与 T7a 持平，但 `cache_read==0` 从 5 次降到 1 次 —— 数字在 `pitfalls/local-env.md`）。**这两条改动就此从"推测"变成"实测有效"，以后照做。**<br>**复验查出一个它没报的真缺陷，已修**：`params` 的键**完全没校验**，而 `params` 刻意不进 `KeyVault.secret_values()`（永不脱敏）又被 `GET /api/keys/{h}` 原样回显 —— 把凭据填进 `params` 就同时得到"存下来 + 明文回显 + 日志不脱敏"。`ShapeSpec.param_keys` 声明了白名单并经 `/api/providers` 发布，却没有任何代码强制它（pitfalls 条 23 的形状）。现补 `llm_client.check_param_keys()`：多余键与缺键都 **422 `invalid_request` + `params{field:"params",key_name}`**，码不用 `unexpected_secret_key`（收到的按定义不是 secret，用那个码会让前端说"你凭据填错了"）。顺带把 `_completion_kwargs` 的 `params.get("AWS_REGION_NAME","")` 改成直接下标，并把它挪到 `try` 之外 —— 否则 KeyError 会被那个宽口径 `except` 吞成"验活未通过"，让 bug 长期伪装成用户凭据不对。<br>**T7a 交给 T7b 的东西**：`auth_shape` 声明哪几个键的校验（`400 unexpected_secret_key`）、供应商目录、真实验活**都不在 T7a 里** —— vault 只按传进来的数据存取，不判断形状合不合法 |
| T8 | 授权清单（加载／校验／热重载）+ `/api/targets/validate` | T6 | `services/allowlist.py` `routes/{allowlist,targets}.py` | 2 ✅ 2026-09-12，**130 个新用例**。四个测试文件各造一份的 `app`／`anonymous`／`client` 夹具与 `make_entry` 收进了 `conftest.py`（需要替身的文件**覆写 `anonymous`**，替身必须在 lifespan 跑完之后才装得住）。**另新建 `services/audit.py`**（`audit_log` 表 T2 就建好了但没有写入方）—— 只有一个 `record()`，DB + ndjson 双写共用同一时刻，**T12／T25 在它上面加事件，不要造第二个写入方**。`/api/targets/validate` **刻意不写审计**：它是会被反复调用的只读预览。<br>**T6 交回的三件事**：① `zh-CN.json` 的 `targetGuard` 子树（8 个 `RejectionReason` 的中文 + `notes.loopback_rewrite`；**不进 `errors.py`**，它们是输入框下方的行内提示，请求本身没失败）—— 已做；② ~~`registrable_domain`~~ **已定不做**（2026-09-11 用户拍板）：正确实现要 Public Suffix List，而近似实现（取末两段）在 `example.co.uk` 上算出 `co.uk`，这个值要进逐字确认串 —— **一个错的注册域名比没有更糟**，会让用户确认一个不是他想授权的范围。**别再加回来**，要加就先把 PSL 那笔维护账付掉；③ ~~⏳ `TargetRejected` 在 HTTP 层怎么表达~~ **2026-09-12 定完，判据表搬到 T12 行** |
| T9 | `ScanLauncher`：argv + env + tmpfs HOME + 预置 `--config` + cwd/TMPDIR + `RUN_ID`；6 个模板的黄金 argv 测试 | T7 T8 | `services/{scan_launcher,scan_templates}.py` `routes/templates.py` `tests/test_scan_{launcher,templates}.py` | **3** —— **2026-09-14 已落地并复验**：`services/scan_templates.py`(194) + `services/scan_launcher.py`(350) + `routes/templates.py`(56) + `tests/test_scan_launcher.py`(405，23 条) + `tests/test_scan_templates.py`(62，5 条) + `main.py` 一处接线；官方闸门 **682 passed / 0 skipped**、ruff 干净。施工图 `plan-t9.md`（已随 a7458fa 提交，被跟踪）的用途已尽，可删（删了 git 历史里还在）。<br>**复验（主会话自己做，没采信子 agent 的报告）**：8 次 mutation（argv 塞 `api_base`／改用 `--instruction`／env 变成 copy／不设 `STRIX_RUN_ID`／`STRIX_PROMPT_CACHE` 恒 true／指令文件 0644／`cleanup_workspace` 变 no-op／去掉预算必填），**每次只红该红的那几条**。<br>**两条"负对照"没红 = 一个真缺陷，已修**：env 原先把 `STRIX_TELEMETRY`／`STRIX_NO_UPDATE_CHECK`／`LITELLM_LOG`／`STRIX_RUN_TYPE` 当"容器级透传，缺就跳过"（这是方案里写的），而这四个**缺失时都不报错**：缺 telemetry 就是 PostHog + Scarf 外发（§禁区第一条），缺 `STRIX_RUN_TYPE` 就是沙箱容器没 label、`make reap` 回收不到 —— 从白名单删掉任何一个，**没有一条测试变红**。又是 T7b 那个 `params` 缺陷的形状（声明了却没有任何一处强制）。现改成 `_PINNED_ENV` 写死在代码里、**放在透传之后**（本进程环境写了相反值也不生效），加一条测试。`STRIX_IMAGE` 与 `STRIX_DOCKER_SANDBOX_NETWORK` 刻意仍走透传（**没有安全的默认值**，猜错网络名比报错难查），强制点交给 T10 的启动断言。<br>**子 agent 相对方案的 4 处偏离，全部判定接受**：① 加 `build_launch_plan()` 组合器（方案定义了 `LaunchPlan` 却没有函数产出它，不加它 `argv_preview == argv` 就没有可断言的落点）；② `build_argv` 多一个必填 `budget_ceiling_usd`（把预算上限校验挂在 argv 的唯一入口上，而不是"请调用前先校验"）；③ `prepare_workspace -> Workspace`（方案未定返回类型）；④ 多校验 `scan_mode ∈ {quick,standard,deep}`（与已列的 `reasoning_effort` 同类：非法值会让子进程死在 argparse 上、被归因成"扫描失败"，复用同一个已有码、零新增代码）。<br>**实测代价**：32 次工具调用（预算 ≤55）、0 次压缩、第 8 次调用开始写代码（预算是第 6 次）—— 晚的那 3 次是 `grep` 去查方案摘录没给的四个符号（`SHAPE_*` 的字面值、`TestCredential` 仓库里不存在、`settings.scans_dir` 这个 property 名、`BudgetExceedsCeilingError` 没有调用先例所以 params 键名要自定）。**下次派发把"要用到的符号字面值"也抄进摘录**。<br>**审阅时定的四条**（都已落进本文件对应位置，此处不复述理由）：① 模板存 Python 常量不存 YAML（§向导→CLI 映射）；② `--resume` 不进 T9（`strix_run_name` 要等 T10 抢到才存得下）；③ spec 上传 v1 不做（`--workspace-file` 在 1.5.3 的 CLI 里不存在，见 §向导 那张表与 §DooD 路径别名 那张表）；④ **`zh-CN.json` 不在 T9 范围内** —— `/api/scan-templates` 只回机器码，模板中文名归 T19（不跨文案，所以这一行不用再拆）。<br>**边界**：T9 只构造 argv/env/cwd + 建 tmpfs 上那两个文件，**不起进程**（起进程与退出码归因是 T10；并发闸／DNS 重解析／逐字确认／写 `scans` 行／审计是 T12）。<br>**实测得到的三个 env 名**（`strix.config.settings.LlmSettings`，2026-09-14）：`api_base` 的落点是 **`LLM_API_BASE`**（不注入就静默丢失）；`STRIX_REASONING_EFFORT` 是**枚举** `none\|minimal\|low\|medium\|high\|xhigh\|max`（默认 `high`，非法值会让子进程死在 pydantic 启动校验上、被归因成"扫描失败"，所以要在构造阶段 422）；`STRIX_PROMPT_CACHE` 的判据是 `"invoke/" in model_for(...)`，**不是** `auth_shape == bearer`。`--resume` 收的是 **run name**（`strix_runs/` 下的目录名）不是 `scan_id` —— `scans.strix_run_name` 那一列就是为它准备的 |
| T10 | `ScanSupervisor`（退出码→中文、优雅停止、`finally` 清 tmpfs）+ `RunDiscovery` | T9 | `services/{scan_supervisor,run_discovery}.py` | 2 —— **T9 交回四件事**：① **启动断言 `STRIX_IMAGE` 与 `STRIX_DOCKER_SANDBOX_NETWORK` 非空**（两者仍走 env 透传、缺了静默降级：沙箱网络缺失 → Caido 端口解析成 `127.0.0.1`。`settings.py` 已有这两个字段且默认 `""`，断言挂在 `main.py` 的启动序列上，和 `assert_single_worker` 同一处）；② `cleanup_workspace()` **只删 tmpfs**，`${DATA}/scans/<id>/tmp` 的清理没有归属（T10 或 `make reap` 挑一个，别两边都做）；③ `--resume` 收的是 run name，`RunDiscovery` 抢到之后才写得进 `scans.strix_run_name`；④ 指令正文里测试账号是 `role=… username=… password=…` 一行，**那个口令不在 KeyVault 里因此不在脱敏集合里** —— T10 若把指令正文回显进日志/前端就是明文泄漏（泄漏矩阵新增一行，见 §Key 不落盘）|
| T11 | `Reaper`（启动/定时/每次停止后按 label 清扫）+ 镜像预拉取带 WS 进度 | T10 | `services/reaper.py` | **2 —— ⚠️ 破坏性操作**（判据见上方「两条容易搞反的」）。prompt 必须写死：**`label=strix-run-type=console` 且 `strix-run-id` 非空**的双条件过滤、先 `--dry-run` 打印、禁止按"名字像"或"时间早"删。⚠️ **run-id 那一半不是可选的** —— M0 靶场被手打了同一个 `strix-run-type=console`（2026-09-11 实测），只按前者会删掉它；加上后者永不误伤真沙箱（`docker_client.py:113` 早退）。直接复用 `docker_probe.ORPHAN_LABEL_SELECTOR`／`ORPHAN_REQUIRED_LABEL`，别再抄字面串 |
| T12 | `POST /api/scans` 全套授权不变式（`authorization_id NOT NULL`、DNS 重解析比对、逐字确认）| T9 | `routes/scans.py` `services/audit.py` | **3** —— **T8 交回三件事**：① `target.rejected`／`dns_changed` 两个审计事件**加在 `services/audit.py` 上**，不要造第二个写入方；② `services/dns_resolver.py` 的 `resolve_sync` errno→码映射（`dns_not_found`／`dns_timeout`／`dns_failed`）**目前没有测试**，真要依赖它就在这里补一个 monkeypatch 用例；③ `GET /api/allowlist` 现在把 `owner`／`authorization_ref` 原样返回 —— 今天没问题（只有一个账号），但**一旦出现只读操作员角色就必须做字段过滤**。<br>**服务端重校验的拒绝出口（2026-09-12 拍板，T8 交回 ③ 的答案；两条出处已在代码里：`routes/targets.py:5-8` 与 `target_guard.py:122-124`）**：按"性质"分两类，**不新增 `target_rejected` 码** —— 8 个 `RejectionReason` 各编一个 HTTP 码会得到 8 个永不当响应码用的"错误"（`errors.py` 模块 docstring），合成 1 个新码又与 `invalid_request` 语义重叠。<br>· 规范化失败（`RejectionReason`）→ **422 `invalid_request`**，`params = {field:"targets", index:<下标>, reason:<RejectionReason>}`，**遇到第一个就拒（fail-fast）**：`ParamValue` 只许 JSON 标量（`errors.py:38-40` 刻意不许 dict／list，防止有人往里塞整个请求体进而塞进凭据），逐条列全就得先破那条约束或给错误响应加第四个字段，而这条路径本该被向导第 1 步的 `validate` 拦住，不值得为它付这笔账。**`raw` 绝不进 `params`** —— `user:pass@host` 的原文回显只许存在于 `validate` 的 200 正文那一处（T30a 已如实记录那一条残余风险，别扩大它）<br>· 缺勾选（`required_opt_in` 非空）→ 同样 **422**，`params = {field:"overrides", index:<下标>, missing_opt_in:<OptInFlag>}`<br>· 护栏策略拒绝 → 保持 `GuardVerdict.error_code`：`blocked_metadata` **403**（不可覆盖），`not_in_allowlist`／`split_horizon`／`dns_changed` **409**（可以改状态再来）<br>· 前端（T18）：`params.reason` 在时去 `targetGuard.reasons.*` 取那句人话并高亮第 `index` 行，不在时退回 `errors.invalid_request` 的通用文案。落地要顺手补 `zh-CN.json` 的 `errors.invalid_request.params` 与 `paramLabels` 三个新键（`index`／`reason`／`missing_opt_in`）|
| T13 | `RunProjector`：epoch + 三信号重同步 + elision 识别（**全项目最难的一块**）| T10 | `services/run_projector.py` `strix_bridge/{projection,paths,catalogue}.py` | **3** —— **T14／T15／T16／T21／T29 五个任务挂在它后面**，且它定义 `strix_bridge/` 的 import 边界 |
| T14 | `EventMirror`（截图首见即落地 `media/`）+ `LogTailer`（脱敏在推流前）+ `ScanChannel` | T13 | `services/{event_mirror,log_tailer,channel}.py` | 2 |
| T15a | **采集压缩夹具**：真跑一次扫描，`STRIX_CONTEXT_BUFFER_TOKENS=1` + `STRIX_MAX_CONTEXT_IMAGES=1` 强制触发压缩与图片淘汰；产物脱敏后入库 | T13 | `tests/fixtures/run_dirs/` | **自** —— 要真凭据、真扫描，派发规则第 3 条禁止把 Key 给子 agent，**结构上不可派发** |
| T15b | 重同步测试（吃 T15a 的夹具）| T15a | `tests/test_projector_resync.py` | 2 |
| T16 | WS + SSE 路由、重连回放 | T14 | `routes/stream.py` | 2 |
| T17 | 前端实时面板（子 agent 树 / 事件流 / 终端 / 截图 / CostMeter）| T5 T16 | `frontend/src/components/live/*` | **3**（读 T5 的约定，**不再开** frontend-design）|
| T18 | 五步向导（授权步：三勾选 + 逐字输入，期望串显示在框**旁边**）| T5 T12 | `frontend/src/components/wizard/*` | **3** |
| T19 | 6 个场景模板 + 费用预估 + 测试账号收集 + 高级面板 | T18 | `frontend/src/components/wizard/*` `routes/templates.py` | **3** —— "费用预估"要如实展示 bearer 形状贵 4～6 倍这个真实取舍，是产品决策不是填表。读 T5／T18 的约定，**不再开** frontend-design |
| T20 | 发现 tab | T17 | `frontend/src/components/findings/*` | **3** |
| T21 | `Translator`（逐条 + executive、`Semaphore(4)`、JSON 修复、缓存表、费用核算）| T13 | `services/translator.py` `migrations/002_report_translations.sql` | **3** —— 含**新建迁移 = 表结构设计**（`agent-rules.md` §四 明列）；且"绝不翻译 `poc_script_code`／`evidence`／`endpoint`／`code_locations`"是硬约束，译错等于伪造证据 |
| T22 | `exporter_html.py` 打印 CSS + 报告 tab | T21 T5 | `services/exporter_html.py` `frontend/src/components/report/*` | **3** |
| T23 | md/csv/sarif 直通导出 | T21 | `routes/reports.py` | 1 |
| T24 | 专家 tab（复用同一 store，**不代理 Strix 自带 SPA**）| T17 | `frontend/src/components/expert/*` | **3** |
| T25 | 审计 UI + CSV 导出 | T12 | `services/audit.py` `routes/audit.py` **`frontend/src/app/audit/*`** | **3**（前端页面 → 模板 3）|
| T26 | 系统诊断页（T3/T10 每个失败模式都有中文修复指引）| T3 T10 | `frontend/src/app/diagnostics/*` | **3** |
| T27 | `exporter_docx.py` —— 手写 WordprocessingML，**不引 `python-docx`** | T22 | `services/exporter_docx.py` | 2 |
| T28 | 续跑（重新索要 Key）+ 并发队列 + 留存清理任务 | T10 | `routes/scans.py` `services/scan_supervisor.py` | **2 —— ⚠️ 破坏性操作**（留存清理会删用户的扫描产物）。prompt 必须写死：只删 `${DATA}/scans/<自己创建的 scan_id>/`、先 dry-run、绝不递归删 `${DATA}` 下其他任何目录 |
| T29 | `test_strix_contract.py`（升级预警线）+ `importlinter.ini` | T13 | `tests/test_strix_contract.py` `backend/importlinter.ini` | **1** —— 断言清单已被 §Strix 集成面 与 §import 边界 钉死，本任务是照着写。**prompt 必须写死"断言只许来自那两节，不许自己发明"** —— 发明的断言会让升级预警线失效 |
| T30a | `README.md` + `docs/` 四份文档 | 全部 | `README.md` `docs/*` | 1 —— **是改写现有的 `README.md`（2026-09-09 提前写的临时版，因为仓库 public 而合规声明不该等到 M8），不是新建；必须保留合规声明原文**（见 §合规声明），其中那张手工维护的状态表到时整段删掉。**两条残余风险必须落进 `docs/SECURITY-zh.md`**：① T5b 那行的口令同步；② `POST /api/targets/validate` 拒绝 `https://user:pass@host` 时会在 200 正文的 `raw` 字段**原样回显一次**（只有这一处，零日志调用，走 TLS 回给刚打出它的人）—— 如实记录，不靠"整理干净再回显"消除，那会擦掉用户唯一的线索 |
| T30b | `make verify-e2e`（28 条）| T30a | `Makefile` `scripts/verify_e2e.sh` | **2** —— 写 shell 断言是本项目**踩过坑**的地方：`pitfalls` 条 18、条 23 末段（"检查都通过" ≠ "被检查的事真发生了"，M0 就这么假绿过一次）。**安全门 6–11、22、25 由我逐条复跑复核，不采信子 agent 的结论** |

**可并行组**（不共享文件，同批发出）：`T3∥T4`、`T6∥T7`、`T15b∥T16`、`T19∥T20`、`T23∥T25`、`T27∥T28∥T29`。
其余全部串行 —— T2 与 T13 是两个瓶颈，几乎所有东西挂在它们后面。同时在跑的 subagent **≤3**。
**T15a 是「自」，不占 subagent 名额**，但它卡着 T15b。

**每个模板 2/3 的 prompt 必须写死**（缺一不发）：
1. **Superpowers 只作用于本子任务** —— 不得触发新一轮 Plan、不得再派生任何子 agent（§六.3/§六.4）
2. 边界：只许改「涉及文件」列里的路径；不许 `git commit`/`push`、不许改 `PLAN.md`/`CLAUDE.md`/`agent-rules.md`。
   **依赖（2026-09-08 修）**：原文写的是"不许装依赖"，但 T2 实测发现镜像里连 `fastapi` 都没有 ——
   那条规则与任务定义本身冲突，按字面执行则 T2 不可能完成。改为：**新增依赖必须先上报并经批准，
   且只能走 `make lock` 进 hash lock**；**绝不许**子 agent 自行 `pip install`、绕过 hash lock、
   或放宽 `strix-agent==1.5.3` 的 pin。改 lock 的代价要一并报（重建镜像约 6–15 分钟，见 `pitfalls` 条 10/11/15）
3. **API Key、真实目标、授权信息绝不进 prompt**；需要密钥的验证一律留给手动步骤
4. 返回格式 + 可执行验收命令；**我会自己再跑一次验收**，不盲信子 agent 的结论。
   **给子 agent 的必须是「快闸门」，不是 `make test`**（`agent-rules.md` §九.4）—— `make test` 会重建
   镜像并重装 dev 依赖（测试 stage 是 `FROM runtime`，`COPY app` 一改就让依赖层失效，约 15 分钟），
   子 agent 每跑一次就白烧一次满上下文重写。写进 prompt 的命令是这条（秒级，且不会在仓库里留下
   root-owned 缓存 —— `PYTHONDONTWRITEBYTECODE` + `-p no:cacheprovider` 是必需的）：
   ```
   docker run --rm -v "$PWD/backend/app:/app/app:ro" -v "$PWD/backend/tests:/app/tests:ro" \
     -v "$PWD/frontend/messages:/messages:ro" -e CONSOLE_MESSAGES_JSON=/messages/zh-CN.json \
     -e PYTHONDONTWRITEBYTECODE=1 strix-console/api-test:0.1.0 pytest -p no:cacheprovider
   ```
   前提是镜像已存在（`make build-test` 或此前跑过一次 `make test`）。`make lint-api` / `make test`
   由**我**在收货后跑一次 —— 那才是权威闸门。
   **`backend/tests` 那一半挂载是 2026-09-13 补的，缺它就是一道假闸门**：测试是**烤进镜像**的
   （`/app/tests`），只挂 `app` 的话新写的测试文件根本不会被收集，跑出来的 608 全绿里**一条新测试
   都没有**，而 exit=0 会让人以为通了。实测补上后 11 秒、608 passed。
   **别加 `-q`**：`-q` 下那行 `608 passed` 不打印，只剩一片点，判绿就只能靠数点或退出码
5. 相关安全不变式原文抄进 prompt。**理由订正（2026-09-13 实测）**：原文写的是"子 agent 看不到
   `CLAUDE.md`"，这是**错的** —— 子 agent 首轮就收到 `CLAUDE.md` + `agent-rules.md` + `MEMORY.md`
   的 instructions 附件（在它自己的 transcript 第一个 `attachment.type=="instructions"` 里可复核）。
   所以抄进 prompt 是**刻意的重复**，不是补缺：常驻区那份是"它读到过"，prompt 里那份是"本任务的
   硬约束"，实测后者才被照办（T7b 通篇遵守 prompt 里的约束，而常驻文件当时并没有让它做 TDD）。
   **代价要认**：同一段文字付两遍钱 —— 所以只抄**这个任务真的碰得到**的那几条，不要整节搬。
6. **模板 3 从 T7b 起：方案由主会话写，只派一个实现 agent**（2026-09-12 用户拍板 ——
   当日先定"拆成两个 agent"，同日收紧成本条；起因都是 T7a 的实测）。三步：
   ① **主会话写方案** —— 它为了写派发 prompt 本来就已经读过那些文件，再派一个方案 agent
   等于同一批文件付两次钱；② 用户按 `agent-rules.md` §八.2 审 —— 那条要求的是**人**审方案，
   **没有**要求方案由子 agent 产出；③ 批准后**另起**一个 agent 只做实现，把方案原文 +
   关键出处（`file:line` 形式，别让它自己再找一遍）塞进 prompt。
   理由：模板 3「先上报 → 等人审 → 再放行」必然制造一次 5 分钟以上的停顿，而 prompt cache 过期后
   下一次调用要把**整个上下文**按 12.5 倍单价重写 —— 停顿发生在同一个 agent 身上时，它的上下文
   正好涨到最大，是最贵的时机。方案写在主会话则把这次停顿挪回主会话（本来就要停在这里等用户批），
   实现 agent 从零起步、根本不经历它。**实测数字在未跟踪的 `pitfalls/local-env.md`**（仓库 public）。
   代价不变：实现 agent 没读过那些文件 —— 那份出处清单是必需品，不是可选项。
7. **省"读文件"的四句必须逐条写进 prompt**（2026-09-13 按 T7b 实测加；它第 18 次调用才写第一行代码，
   之前已吃进 8 万字，其中约 1 万字是重复读同一批文件 —— 明细在 `pitfalls/local-env.md`）：
   ① **`PLAN.md` 与 `CLAUDE.md` 都不许读** —— 本 prompt 已含它们的相关全文（`PLAN.md` 1000+ 行，
   `CLAUDE.md` 它本来就已经收到一份，见规则 5）；
   ② **同一个文件不许读第二次**，需要回看就用已经读到的内容或 `grep -n` 定位行区间；
   ③ **凡是 prompt 里点到的文件，我必须同时给出它需要的那 20–40 行** —— 只给文件名它必然整读
   （T7b 因此整读 `key_vault.py` + `models.py` 共 2 万字，真正用到约 1.5k）；
   ④ **测试/依赖的约定由 prompt 直接给**（测试文件命名、`conftest.py` 里有哪些夹具、依赖已在 lock 里），
   否则它会用 `grep "def test_"`、`cat pyproject.toml` 去摸，一次几千字。
8. **TDD 只抄两句进 prompt，不挂那 320 行 skill**（2026-09-13 用户拍板，判据与理由在
   `agent-rules.md` §六.5 / §十.5 / §八.3）。本项目侧只多两件事：
   ① **前提是规则 4 那条快闸门已经存在** —— TDD 每条测试要跑两遍闸门，11 秒可以，15 分钟不行；
   ② **收货侧的 mutation 按 `CLAUDE.md` §安全不变式逐条做**（不是按测试条数），改坏一处 →
   跑快闸门 → 确认**只有该红的那几条红** → 改回来。T7b 实测 8 次，全部命中。
   **闸门全绿 + mutation 全对仍然不等于没缺陷**：T7a（开了 TDD）与 T7b（全程没有 TDD 要求）
   的测试都扛住了全部 mutation，**两个各漏一个真缺陷，两次都是我读代码抓到的** —— T7a 是跨线程
   竞态（无法确定性断言），T7b 是 `params` 白名单**声明了却没有任何一处强制**（`llm_client`
   已经把 `param_keys` 经 `/api/providers` 发布出去，而路由层从不校验它 → 凭据放进 `params`
   会被存下、被 `GET /api/keys/{h}` 明文回显、且永不进脱敏集合）。**所以收货必须读代码。**

---

## 主要风险

| # | 风险 | 级 | 对策 |
|---|---|---|---|
| ~~R1~~ | ~~**`STRIX_DOCKER_SANDBOX_NETWORK` 行为不如所读**（未文档化、零测试、全仓仅一处）→ 每次扫描 Caido 代理都是死的，抓包类能力静默降级~~ | ~~高~~ | **已关闭（2026-09-08 实测）**：M0 第 4 条通过 —— `Caido host endpoint resolved: http://172.19.0.4:48080`，容器 IP 而非 `127.0.0.1`。Plan B（扫描进程也进兄弟容器）与 Plan C（接受降级 + 明确提示）**均不需要**。⚠️ 前提是 `api` 容器本身也加入 `strix_sandbox`；这条前提写在 §Strix 集成，别在后续重构里丢掉 |
| R2 | Key 从我没找到的路径落盘 | 高 | 五层：tmpfs HOME + 重定向 `--config` + 无密钥列 DB + 脱敏过滤器 + `test_key_hygiene.py`（每次 E2E 后 grep 整个数据目录、DB 文件、容器日志、`docker inspect` 输出）。CI 用假 Key 跑；**任何命中都是发布阻断项** |
| R3 | `agents.db` 重写（压缩/图片淘汰）搞坏实时视图：事件重复或消失、截图丢失 | 高 | epoch + 三信号重同步 + 识别 elision 字面量 + 只追加镜像；强制压缩夹具是 M4 的交付物而非事后补 |
| R4 | Strix 1.5.4+ 破坏我们（依赖 `tui.backend.live_view`、`viewer.transcript`、`core.paths`、run 目录布局、`agents.db` 结构、Rich 面板标题、退出码、两个未文档化 env）| 高 | `strix-agent==1.5.3` 精确 pin + `--only-binary=:all:` + hash lock（**已核实 1.5.3 有 x86_64 与 aarch64 两个 manylinux wheel，无需 Go 工具链**；sdist 的 hatch 钩子缺 Go 1.24 会硬失败）；`test_strix_contract.py` 导入真实包断言每个符号/签名/env 名；import-linter 禁止 `app.*` 越过 `strix_bridge` |
| R5 | 挂 docker.sock ≈ 宿主 root | 高 | 无法在 Strix 用 docker-py 的前提下消除。`web` 只绑 `127.0.0.1`、`api` 不发布端口、`no-new-privileges`、单一用途镜像；在 README 与 `docs/SECURITY-zh.md` 显著声明 |
| R6 | 护栏之外仍发生未授权扫描（声明后 DNS rebinding、通配太宽、用户就是撒谎）| 高 | `authorization_id NOT NULL`；启动时重解析 DNS 不一致就拒；只允许单层通配；条目 `expires`；元数据地址不可覆盖；审计留操作人姓名与授权编号。**如实承认残余：工具只能记录声明，无法验证授权** —— UI 里就这么写 |
| R7 | 若有人日后去掉同路径挂载或改用 named volume，路径别名 bug 复现 | 高 | `/api/system/status` 做**主动**同路径探测，失败就拒绝启动任何扫描；compose 那行卷挂载写注释说明为什么 |
| R8 | SIGTERM 泄漏沙箱容器（已确认：`run_cli` 信号处理器的 `sys.exit(1)` 跳过 `session_manager.cleanup`）| 中 | Reaper 在启动时、每 5 分钟、以及每次停止后按 label 清扫；`/api/system/status` 暴露 `orphan_sandboxes`；`make reap` |
| R9 | 费用失控（大应用 + 贵模型的 deep 扫描可以烧掉几百美元）| 中 | 预算强制必填 + 全局上限；向导显示预估区间与生效上限；实时 CostMeter；80% 软告警带一键停止；并发 1。Strix 自身在 70/85/95% 会引导收尾，所以上限是优雅降级 |
| R10 | 中文翻译幻觉出一个漏洞、或把真问题说轻了 | 中 | Prompt 禁止新增事实；喂 `counterevidence`/`assumptions`/`confidence_rationale` 让"需人工确认"如实呈现；**每张发现卡都有「查看原文」切到英文 `vulnerabilities/<id>.md`**；报告页脚注明"中文说明由 AI 依据扫描原始结果生成，技术细节以原文为准"；**绝不翻译** `poc_script_code`/`evidence`/`endpoint`/`code_locations` |
| R11 | 扫描中 `api` 重启 → 子进程被孤立、WS 悬空、key handle 失效 | 中 | 启动时 `ScanSupervisor.recover()` 扫 `status IN (starting,running)` 的行，重新发现 run 目录并挂只读 RunProjector（无子进程也能工作），标 `orphaned_running`；`run.json` 到终态就正常收尾；若 15 分钟不动且无对应沙箱容器则标 `interrupted` 并提供续跑（需新 Key）|
| R12 | `localhost` 有三种含义（用户的 Mac / `api` 容器 / 沙箱）导致误解 | 中 | loopback 放行流程用中文讲清 Strix 的 `host.docker.internal` 改写；`/api/targets/validate` 返回 `note_code`（后端只给码，中文在 `zh-CN.json`，T6 改）；常见问题给完整例子 |
| R13 | Docker Desktop 资源耗尽 | 低 | 上面那组 `STRIX_SANDBOX_*` 限额；并发 1；状态页显示剩余磁盘，低于 20GB 告警（沙箱镜像 + run 目录 + 截图很快堆起来）|
| R14 | base64 截图撑爆 WS 帧与 DB | 低 | EventMirror 首见即抽取到 `media/` 并改写为 URL；`scan_media` 记账；留存清理优先删 media |
| R15 | **TLS 装了但静默失效** —— 三种真实模式：证书缺 SAN/EKU（Chrome 直接 `ERR_CERT_COMMON_NAME_INVALID` 拒连）｜证书未被信任（每次弹警告页，用户被训练成无脑点过）｜`nginx.conf` 漏了 `proxy_buffering off`/`proxy_read_timeout` 导致 SSE 卡死或 WS 60 秒被掐 | 中 | 验收 22（SAN+EKU 断言）与 25（wss + 长时空闲）为**发布阻断**；26 断言 SSE 首字节延迟；`/api/system/status` 暴露证书剩余天数 + SAN/EKU/`CA:FALSE` 的实测结论，未信任时给中文修复指引（`security add-trusted-cert` 原样命令可复制）。⚠️ **`tls.cert_trusted` 在后端恒为 `null`**（2026-09-11 T3 改）：信任判定在宿主钥匙串 / 浏览器 NSS 库里，api 容器既没有 `security` 也看不见 keychain，所以它如实回报 `cert_trusted_reason: "not_observable_from_container"` 而不是猜。**这条改由前端判定**：页面能加载出来本身就是"证书已被接受"的证明（拿不到就根本渲染不了这个状态页）。后端只负责"证书本身合不合格"那一半 |

---

## 端到端验收（`make verify-e2e`；6–11 是 Key 卫生安全门，22 与 25 是 TLS 阻断项，27 与 28 是登录阻断项，全绿才能发布）

**准备**：自己有权测试的靶场 —— `docker run --rm -d -p 13000:3000 bkimminich/juice-shop`，
（**用 13000 不用 3000** —— 本机 3000 已被占用且监听在所有网卡上，`make verify-e2e` 会起不来；端口在验收脚本里参数化）
目标填 `http://localhost:13000`（一次跑通 loopback 放行 **和** `host.docker.internal` 改写两条路径）。

1. `./setup.sh` → `docker compose up -d --build` → `/api/health` 返回 ok
2. `/api/system/status`：断言 `docker.reachable`、`network.present && api_attached`、
   `data_dir.identical_path_ok`、`telemetry.strix_telemetry == false`、`sandbox_image.present`
3. **护栏矩阵**：`169.254.169.254` 与 `metadata.google.internal` → `blocked_metadata, overridable:false`；
   `10.20.1.5` → 需内网放行；`http://localhost:13000` → 需 loopback 放行 + `note_code:"loopback_rewrite"`
   （并断言 `zh-CN.json` 里那句正文含 `host.docker.internal`）；
   `https://admin:pw@example.com` → 拒绝（否则会被 Strix 当成仓库）；`例子.中国` → punycode 且标记；
   同时解析到公网与内网的主机名 → `split_horizon`
4. **授权强制**：缺 `authorization` → 422；`typed_confirmation` 写错 → 409；只勾 2 项 → 422；
   `enforce` 下目标不在白名单 → 409；**直接 `INSERT` 一行 `authorization_id=NULL` → DB 拒绝**
5. **Key 注册**：垃圾 Key → 400 且 2 秒内中文报错、无 DB 行；真 Key → 201 带掩码标签；
   `grep -r "$TEST_KEY" $DATA/` → **0 命中**
6. **发起**：把 `localhost` 加入白名单（`allow_loopback:true`）后发起快速体检、预算 $3。
   `argv_preview` 含 `-n -t http://localhost:13000 -m quick --max-budget-usd 3 …` 且**无 Key**
7. **实时流**：90 秒内依次看到 `phase: pulling_image|starting_sandbox` → `setting_up_proxy` →
   `agents` 出现 Root Agent → `event.add` 的 chat/tool → `log` → `summary` 且 `cost_usd` 递增；
   几分钟内至少一个 `exec_command` 事件渲染到终端面板，且至少一张截图经 `/api/scans/{id}/media/…` 显示
8. **argv 干净**：`docker compose exec api sh -c 'ps -ww -eo args' | grep -c "$TEST_KEY"` → **0**
9. **沙箱容器干净**：`docker inspect $(docker ps -q --filter label=strix-run-id=<id>) | grep -ic "$TEST_KEY"`
   → **0**；同时断言有 `NET_ADMIN`/`NET_RAW`、在 `strix_sandbox` 网络上、**无发布端口**
10. **Caido 解析（R1 的证据）**：`grep "Caido host endpoint resolved" <run_dir>/strix.log`
    → 必须是 `172.*`/`10.*` 容器 IP，**不是 `127.0.0.1`**
11. **扫描中 Key 卫生大扫除**，以下全部为 0：
    `grep -rI "$TEST_KEY" $DATA/`（含 run 目录、`strix.log`、`run.json`、审计 NDJSON）；
    `strings $DATA/console.sqlite`（含 `-wal`）；`docker compose logs api web nginx`；
    `docker inspect` 两个容器；仓库目录内 grep（compose/.env 卫生）。
    **且应恰好有一处命中以证明containment 生效**：
    `docker compose exec api grep -rl "$TEST_KEY" /run/strix/` → 只有那个预置的 `--config` 文件
12. **优雅停止 + 回收**：45 秒内 WS 收 `done{exit_meaning:stopped}`；`run.json.status ∈ {stopped,interrupted}`；
    `docker ps -a --filter label=strix-run-id=<id>` → 空；容器内 `/run/strix/<该任务目录>` → 已删；
    `orphan_sandboxes: 0`
13. **跑到自然结束**：退出码 `2`、`exit_meaning: completed_with_findings`（juice-shop 必有发现）；
    `vulnerabilities.json` 非空；`findings.sarif` 合法 JSON；`severity_counts` 与 `scan_findings` 行数一致
14. **压缩韧性（R3 验收）**：注入 `STRIX_MAX_CONTEXT_IMAGES=1` + `STRIX_CONTEXT_BUFFER_TOKENS=1` 重跑。
    断言 WS 至少发出一次 `compaction_notice` 或 epoch 递增的 `events.snapshot`；`scan_events` 保留**所有** epoch；
    **截图画廊仍显示每一张截图**，尽管 `agents.db` 里已变成 `[older screenshot elided…]`
15. **中文报告**：每条 finding 的 `title_zh/what_zh/impact_zh/fix_zh/severity_reason_zh` 均非空；
    无词表里的未翻译术语；`not_tested_zh` 非空（来自 `coverage.json`）；重复 POST → `translated_findings: 0`（命中缓存）
16. **导出**：`report/print?lang=zh` 在 Chrome 里 `⌘P` → A4 PDF 中文正常、发现卡不跨页断裂、截图内嵌；
    `report/docx` 在 Word/Pages 打开中文无豆腐块
17. **Key 生命周期**：`DELETE /api/keys/{h}` → 204；用死 handle 请求报告 → 409 `key_required`；
    重启 `api` → 所有 handle 消失，可续跑的扫描点"继续扫描"会预填 provider/model 且 Key 框为空
18. **续跑**：中途停止后续跑，断言 argv 用**同一个 cwd**、`--resume <strix_run_name>`、
    **显式 `-m <持久化的模式>`**（否则会静默回落到默认 `deep`）、**且无 `-t`**（Strix 会报错）；
    transcript 是接续而非重来；`strix.log` 是**追加**（老行还在）
19. **审计**：CSV 含 `authorization.affirmed`/`scan.launched`/`override.loopback_used`/`scan.stopped`/
    `report.exported`/`key.registered`（掩码）/`key.forgotten`；对 CSV `grep -c "$TEST_KEY"` → **0**
20. **重启恢复**：扫描中 `docker compose restart api` → 标 `orphaned_running`、WS 重连并从镜像续流、
    `run.json` 到终态后正常收尾
21. **拆除**：`docker compose down` → `docker ps -a --filter label=strix-run-type=console` 里**没有带非空 `strix-run-id` 的容器**（不是"为空"—— M0 靶场带着同一个 `strix-run-type`，它在跑的时候这条永远不可能为空，2026-09-11 T3 实测）；
    `$DATA` 仍保有 DB 与 run 目录（持久化正常）；`/run/strix` 随容器消失（tmpfs 生效）
22. **证书正确性（发布阻断）**：`openssl x509 -noout -text -in $DATA/tls/cert.pem` 同时含
    `Subject Alternative Name`（`localhost`/`127.0.0.1`/`::1`）与 `TLS Web Server Authentication`；
    缺任一 Chrome 直接拒连 —— TLS 等于白做。同时断言私钥权限为 `600`
23. **无 80 端口**：`nc -z 127.0.0.1 80` 失败（我们刻意不监听）；`docker compose ps` 中仅 `nginx` 有发布端口，
    `api` 与 `web` 的 `PORTS` 列为空
24. **TLS 生效且未退化成"随便信任"**：`curl --cacert $DATA/tls/cert.pem https://localhost/api/system/status`
    → `200`；**不带** `--cacert` 时 curl 报证书校验失败（若这条通过了，说明信任链被放宽了，是缺陷）
25. **wss 与长连接（发布阻断）**：实时流走 `wss://localhost/ws`；启动一次扫描后**空闲 90 秒不发任何消息**，
    连接仍存活（证明 `proxy_read_timeout` 已改，默认 60s 会掐断）；`ws://` 明文端点不存在
26. **SSE 未被缓冲**：关掉 WS 走 SSE 兜底，首字节延迟 < 2s（证明 `proxy_buffering off` 生效；
    默认缓冲下这里会挂到超时）
27. **登录真的是门，不是装饰（发布阻断）**：不带 cookie 请求 `/api/scans`、`/api/keys`、`/api/audit`、
    `/ws` 全部 `401`；**`/api/health` 仍 `200`**（否则 compose healthcheck 会把容器判成不健康）。
    这条必须**逐个路由枚举**跑，不许只测一个就宣布通过 —— 全局依赖漏挂某个 router 是最典型的失败模式
28. **口令散列不落 DB、不可逆（发布阻断）**：`auth.json` 权限为 `600`；
    `sqlite3 $DATA/console.sqlite '.dump' | grep -ci <口令>` 与 `grep -c <散列>` 均为 `0`；
    （**文件名是 `console.sqlite`**，不是 `console.db` —— 写错的话 sqlite3 会去建一个空库然后
    `grep -c` 返回 0，整条断言变成永远通过的假绿。出处 `settings.py:136`）
    `assert_no_secret_columns()` 仍在跑且 41 列零凭据列（即"加登录"没有顺手给它开豁免）

---

## 单账号登录

**2026-09-08 用户拍板：实现。** 原文"我认为应该有登录认证,虽然是本机使用,但也应该有账号登录"。
撤销此前的"方案已定但延后"。**范围限定为单账号** —— 不做多用户、不做数据隔离。

### 为什么需要它（两条真实理由，第三条是我一度夸大的，已纠正）

**理由一：loopback 在 macOS 上是全机共享的，不是每用户隔离。**
原决策写"绑 `127.0.0.1` 所以不需要登录"，这条推理是错的。本机另外两个账号
（`itadmin` 501、`macadmin` 503）登录后可直接访问以 `szhang` 身份绑在 `127.0.0.1` 的控制台，
**发起扫描、花你的预算、以你的授权编号署名**。这两个账号是公司 IT 下发的管控账号、平时无人登录，
所以是非对抗性场景 —— 但"平时无人登录"不是一个可依赖的安全属性。

**理由二：`audit_log.actor` 现在填不出东西来。**
该列已在库里（`backend/app/migrations/001_init.sql:319`），无登录时只能填 `local`。
一个渗透测试控制台的审计日志说不出是谁发起的扫描，那审计就是装饰。

**理由三（纠正）："任何网页都能调这个 API" —— 不成立，但依赖四条隐式行为。**
我一度断言未认证的 localhost API 对浏览器里每个标签页都开放。**实测否证**：

| 攻击路径 | 现状 | 靠什么挡住 |
|---|---|---|
| 跨域 `application/json` POST | 被浏览器拦 | 我们**没有**装任何 CORS 中间件（已核实 `backend/app/` 无 `CORSMiddleware`），预检拿不到 `Access-Control-Allow-Origin` |
| 跨域简单请求 POST（无预检） | **422** | FastAPI 只在 content-type 是 `application/json` 时才把 body 喂给 Pydantic。**已实测**（`api` 容器内 TestClient）：`text/plain` / `x-www-form-urlencoded` / `multipart/form-data` 三种 CORS 安全名单类型全部 422 |
| DNS rebinding | TLS 握手失败 | 证书 SAN 只有 `DNS:localhost,IP:127.0.0.1,IP:::1`（`setup.sh:418`），攻击者域名过不了校验；且**不监听 80**，没有明文降级路径 |
| 跨域 GET | 能发出、读不到响应 | 同源策略。**前提是没有任何 GET 带副作用** |

结论：当前**不可利用**，但这份安全性寄托在四条从未被测试断言过的隐式行为上 —— 其中"证书 SAN 只含
localhost"此前被当成 Chrome 兼容性措施记录，实际上**它是一条安全控制**。
登录 + `SameSite=Strict` 让这四条是否成立都不再要紧，这才是它真正的收益。
**推论（写进代码约束）**：永远不许为了"方便调试"加 `CORSMiddleware`；`allow_origins=["*"]` 会一次性
作废上表第一行和第二行。

### 方案（六条取舍都是刻意的，不要"顺手优化"回去）

| 决定 | 理由 |
|---|---|
| 散列存 `${DATA}/auth.json`（0600），**不进 SQLite** | 不给 `db.assert_no_secret_columns()` 开豁免。`password_hash` 列名会被它拦下（黑名单含 `password`），而给它加白名单等于承认这条不变量有例外，之后每个人都会想加自己的例外 —— 它是本项目最硬的结构性不变量 |
| `hashlib.scrypt`（标准库），**不引 `argon2-cffi`** | 后者是 C 扩展，违反"依赖是负债"（先例：为避开 lxml 手写了 250 行 WordprocessingML）。scrypt 抗 GPU 特性与 argon2 同级，够用 |
| **单向哈希**，不是可逆加密 | 需求原话曾是"密码在后台加密"，但能解出明文口令的设计是缺陷不是功能。每用户随机 salt，`hmac.compare_digest` 比对 |
| 会话 = **服务端不透明随机 id**，存 api 进程内存，**不用 JWT** | JWT 的卖点是服务端无状态；我们要的恰好相反 —— 状态必须由服务端持有**且只在内存里**。自包含 token 等于在客户端留一份可离线验证的副本。`pyjwt` 虽在 lock 里（`# via mcp`，strix-agent 传递依赖）但**我们不 import 它** |
| 会话与 KeyVault **同生共死** | 两者都是进程内 dict，api 重启一起蒸发。这不是缺陷，是"Key 绝不落盘"的直接后果；前端已有 `key_required` 分支在处理同一类事，登录失效复用同一种交互 |
| cookie `HttpOnly; Secure; SameSite=Strict; Path=/`，**不存 sessionStorage** | 与 `vault_handle` 刻意相反：`vault_handle` 要被 JS 读出来放进 POST body，会话 id 不需要，那就别让 JS 碰得到（防 XSS 提权成会话窃取）。`SameSite=Strict` 是上一节四条隐式行为的替代品 |

### 它挡什么、不挡什么（写进 `docs/SECURITY-zh.md`，不许含糊）

挡的是"另一个本机账号顺手打开浏览器"和"浏览器里的其它标签页"。
`itadmin`/`macadmin` 是**本地管理员**，能读进程内存、`docker.sock`、SQLite 文件 ——
**登录页对拥有管理员权限的本地账号不构成边界**。把它宣传成安全边界就是自欺。

### 待实现时确定（依赖登录本身，现在定了也是空谈）

失败限流的具体窗口与阈值；初始账号在 `setup.sh` 里的生成流程（口令必须经 `read -rs`，不进 argv 与 shell 历史）；
会话空闲超时；改口令流程。

---

## 最小主机要求的推导

数字不是拍的。常量集中在 `setup.sh` 顶部的 `REQ_*` / `REC_*`，C17 校磁盘、C17b 校内存与 CPU；
**要改门槛改那里，不要只改文档** —— 文档与常量不一致时，以 `setup.sh` 为准并回头修本节。

| 资源 | 阻断线 | 推荐值 | 推导 |
|---|---|---|---|
| Docker VM 内存 | **4 GB** | 8 GB | 单个沙箱下限 `2048 MB`（浏览器 + Caido 抓包代理 + 各类扫描器**同时**在跑）+ api 容器约 `512 MB` + VM 自身开销。低于此线沙箱会被 OOM kill，而 Strix 的报错指向"agent 崩了"而不是"内存不够"，**极难归因** —— 这正是要在 `setup.sh` 里用一个数字挡住的原因 |
| Docker VM CPU | **2 核** | 4 核 | 沙箱下限 2 核。VM 恰好 2 核时沙箱会吃满，api 容器与 daemon 只能和它抢，扫描期间界面卡死 |
| 数据目录可用空间 | **10 GB** | 20 GB | 镜像合计约 8 GB，见下表；再加扫描产物（单次量级见下方实测，量级很小，不是本项的主要压力） |
| 宿主物理内存 | — | VM 配额 + 2 GB | 浏览器与 Docker Desktop 本体跑在 VM **之外** |

**"Docker VM 内存"是 `docker info` 的 `MemTotal`，不是宿主物理内存。** 这是最容易看错的一项：
本开发机宿主 18 GB，而 Docker VM 只有 7.75 GB。所有限额计算的分母都是后者。

镜像体积（**三项均已实测落盘**，2026-09-08 M0 首次拉取后回填）：

| 镜像 | 压缩下载量 | 落盘实测 | 原估值 |
|---|---|---|---|
| `ghcr.io/usestrix/strix-sandbox:1.3.0` (arm64) | **1.31 GB / 35 层**（最大单层 722 MB） | **5.83 GB** | 4–6 GB ✓ |
| `strix-console/api:0.1.0` | 178 MB | **768 MB** | 1–1.5 GB（高估） |
| `bkimminich/juice-shop:latest` | 114 MB | **518 MB** | 约 1 GB（高估） |
| **合计** | | **7.12 GB** | 约 8 GB ✓ |

落盘倍率**实测 ≈4.3–4.5×**（三个镜像分别 4.45× / 4.3× / 4.5×），原先按 `python:3.11-slim` 推的
≈4.8× 略高但同量级 —— 方法是对的。**10 GB 阻断线维持不变**：实测合计 7.12 GB，留给扫描产物 2.9 GB。

**单次扫描产物（M0 第 3 次运行实测回填，2026-09-08）—— 这是个下限，不是典型值**：

| 文件 | 大小 | 说明 |
|---|---|---|
| `.state/agents.db` | **128 KB** | 主体。T13 的增量镜像与 epoch 检测都围着它 |
| `strix.log` | 32 KB | 恒为 DEBUG 级，是泄漏面（见断言 2 的推论） |
| `run.json` | 12 KB | 含 `request_usage_entries` —— **每次 LLM 请求一条，随轮次线性增长** |
| `.state/{agents,notes,todos}.json` | 各 4 KB | |
| `findings.sarif` | 318 B | 本次 0 漏洞，非空时会大得多 |
| **合计** | **188 KB** | |

**必须按下限读**：本次只跑了 13 次 LLM 请求（96 秒，预算耗尽提前结束），且**没有 `media/` 目录**
—— 压根没走到截图那一步。所以"截图只保留最近 3 张 → 首次见到就落地"那条设计**尚未被实测触发过**，
T13 不能拿这次的产物当夹具就算验证完了。`run.json` 随轮次线性增长这点也要留意：
默认 `max_turns=500`（`config/settings.py` 的 `DEFAULT_MAX_TURNS`）时它会比这里大一到两个数量级。
产物本身量级很小，**磁盘阻断线的压力全在镜像上，不在扫描产物上**。

本次运行的产物留在 `${DATA}/scans/probe/strix_runs/juice-shop-3000_3d9a/`（已验无凭据），
可作 T13 的第一份真实夹具素材；但 M4 的压缩夹具仍需一次**跑到截图与上下文压缩**的运行，那次才算齐。

⚠️ **量取口径**：`docker image inspect --format '{{.Size}}'` 给的是**压缩态内容大小**，
`docker images` 的 SIZE 列和 `docker system df -v` 给的才是**解包后落盘**大小，两者差 4 倍多。
本表"落盘实测"一列取后者。看错这一列会把镜像成本低估 4 倍。

## 待扩展：Docker Engine（无 Desktop）

v1 的 `setup.sh` 检测 `docker info` 的 `Operating System` 是否含 `Docker Desktop`，**不是**则直接阻断。
理由：与其现在写一套没法在本机测试的分支，不如把差异点记清、明确拒绝。三条差异都是真实的，不是提示文案问题：

| # | 差异 | 现在的做法 | 扩展时要改 |
|---|---|---|---|
| 1 | Linux 原生**没有 File sharing 概念** | `setup.sh` C16 用真实 `docker run -v "$D:$D"` 探测 | 探测本身仍然有效（同路径挂载在原生 Linux 上天然成立），但失败时的中文指引要换 —— 不能再让人去 Settings → Resources 里找 |
| 2 | **没有 `host.docker.internal`** | 刻意不加 `extra_hosts`（Desktop 下自动可解析） | 必须加 `extra_hosts: ["host.docker.internal:host-gateway"]`，否则向导里"扫本机靶场"这类目标全部不可达 |
| 3 | `docker.sock` 属主是 `root:docker`（GID 因发行版而异） | `user: "0:0"` 直接以容器内 root 运行 | 可以改成非 root + `group_add: [<宿主 docker GID>]`，比 Desktop 下更干净。但 GID 要从宿主探测后注入 |

另有一条**不打算**扩展：Windows 原生。`C:\Users\x:C:\Users\x` 在 compose 卷语法里无法解析（冒号是分隔符），且 Linux 容器内不可能存在名为 `C:\Users\x` 的挂载点。放弃同路径挂载就等于重现 Strix 的路径别名 bug —— 代价不可接受。Windows 用户走 WSL2，此时宿主路径是 `/home/...`，等价于 Linux。

---

## 合规声明（README 与 UI 首屏都要有）

Strix 会**真实攻击**你指向的目标。仅限对**自己拥有或已获书面授权**的系统使用，并严格遵守约定范围。
未授权测试在多数司法辖区违法。授权与合规责任完全由使用者承担。
本工具只能**记录**你的授权声明，**无法验证**授权真实性。
