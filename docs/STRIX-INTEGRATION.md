# Strix 集成说明

写给两类人：要升级 Strix 版本的人，和要排查"控制台与 Strix 之间哪里没接上"的人。
每一条都按"是什么 → 不这样会怎样"写。下文提到的上游代码只写模块或函数名，不写行号 ——
行号随版本漂移，结论才是契约。

控制台当前 pin 的版本以 `backend/requirements.lock` 中的 `strix-agent` 为准（精确版本 + hash，只装 wheel）。
**不要把它放宽成 `>=` / `~=`**：解析期会为拿元数据去构建 sdist，而它的构建钩子需要 Go 工具链，会直接失败。

## 1. 集成方式总览

控制台把 Strix 当作**命令行程序**用：每次扫描起一个 `strix -n` 子进程，用 argv、env、工作目录和
产物文件与它交互。`api` 进程只 import Strix 的只读投影层，用来读产物。理由见 `docs/ARCHITECTURE.md` 第 7 节；
一句话：Strix 的全局状态让"同进程并发两个扫描"会串 Key，而 argv/env/退出码/产物文件是它最稳定的接口。

## 2. 集成面逐条

### 2.1 模型只能经环境变量注入

- **是什么**：Strix 的 CLI 没有 `--model` 参数，模型路由只从 `STRIX_LLM` 读，Key 从 `LLM_API_KEY`
  （以及各供应商自己的变量）读。控制台为每个扫描单独构造子进程 env，把这些值只放在那里。
- **不这样会怎样**：没有别的注入口。若改用进程内调用，Strix 配置模型时会改写整个进程的 `os.environ`，
  同进程里的另一个扫描会拿到别人的 Key。
- 相关：部分路由需要关闭提示缓存（`STRIX_PROMPT_CACHE`），推理强度走 `STRIX_REASONING_EFFORT`，都由
  `scan_launcher` 按需写入子进程 env。

### 2.2 Key 不落盘：临时 HOME + 显式 `--config`

- **是什么**：非交互模式下，Strix 会把当前配置（**包括 `LLM_API_KEY` 明文**）写回它的配置文件。
  控制台做两件事：每个扫描的 `HOME` 指到 tmpfs（`/run/strix/scan-<id>/home`，`noexec,nosuid,nodev`，
  权限 0700），**并且**显式传 `--config <同目录>/cli-config.json`，该文件预先写入 `{"env":{}}`
  （Strix 要求它必须已存在、后缀 `.json`、含 `env` 对象）。扫描结束后整个目录删除。
- **不这样会怎样**：只传 `--config` 不够 —— Strix 还有好几处写 `~/.strix/` 下的文件（更新检查、viewer
  认证、MCP 配置等），不改 `HOME` 它们就落在持久化的位置上；只改 `HOME` 也不够可审计，且挡不住上游将来新增的写路径。
  两层都做，才不依赖 Strix 以后怎么改这段逻辑。

### 2.3 每任务独立工作目录

- **是什么**：CLI 没有 `--output-dir`，产物固定写在 `$CWD/strix_runs/<自动生成的名字>/`。
  控制台给每个扫描一个独立 cwd（数据目录下的 `scans/<id>`），于是 `strix_runs/` 下唯一的子目录就是本次运行。
- **不这样会怎样**：多个扫描共用 cwd 时无法可靠判断哪个产物目录属于哪次扫描，续跑（`--resume`）也找不到目标目录。

### 2.4 退出码语义与 `run.json.status`

- **是什么**：退出码 `0` = 正常结束且未报告漏洞；`2` = 发现了漏洞（**必须当成功**）；`1` = 失败。
  但退出码只反映"是否有漏洞报告"，**不携带"扫描是否跑完"的信息**。
  预算或轮数耗尽时 Strix 仍然退出 `0`，而 `run.json.status` 是 `stopped`。
  控制台的结论由 `scan_supervisor.resolve_attribution()` 给出，输入是三件各自只回答一个问题的事：
  退出码、`run.json.status`、以及控制台自己是否发过停止信号。
- **不这样会怎样**：只看退出码，预算耗尽、什么都没测出来的扫描会被报成"目标干净"。对一个安全工具，
  这比不给结论危险得多。所以 `stopped` 一律报"扫描提前结束，结论不完整"（机器码 `scan_incomplete`）。
- **状态取值**：`running`、`waiting`、`completed`、`stopped`、`crashed`、`failed`、`budget_paused`，外加
  `interrupted`（收到终止信号时写入）。`stopped`（预算/轮数耗尽）与 `interrupted`（被信号停止）要区分开。
  `completed` 一旦写下就不会被覆盖，因此"进程即将正常退出的一瞬点了停止"应判为完成。
  这组取值收在 `backend/app/strix_profile.py`；读到表外的值会原样上报并记 warning，而不是报错 ——
  那正是"上游新增了状态"的升级信号。`run.json` 每有新发现就整体重写，轮询读到半截文件是正常现象，
  读取函数 `run_discovery.read_run_status()` 任何情况都不抛异常。
- **停止信号**：Strix 对 SIGTERM / SIGINT / SIGHUP 装了同一个处理器，写 `interrupted` 后以 `1` 退出。
  所以"是不是被人停的"只能靠"我们自己发过信号"这一事实判断，不能看退出码；也没有"更温和的信号"可选，
  优雅停止就是"发 TERM → 等宽限 → SIGKILL"（只有 SIGKILL 才得到 `-9`）。已写出的发现与报告不会丢，
  因为 `run.json` 与报告是边跑边写的。
- **失败归因**：退出码 `1` 时，Strix 对 TLS 被解密、凭据无效、凭据形状与路由不匹配、路由不接受某个参数等
  完全不同的故障打的是同一种错误面板。所以**先匹配输出正文里的异常特征，面板标题只做兜底**。
  规则表也在 `strix_profile.py`（顺序即优先级），对应机器码包括 `llm_tls_intercepted`、
  `bedrock_route_rejects_bearer`、`prompt_cache_unsupported_on_route`、`invalid_api_key`、`model_access_denied`、
  `model_name_not_provider_qualified`、`model_not_found`、`missing_required_env`、`docker_permission_denied`，
  兜底为 `llm_connection_failed`。若你的网络做 TLS 解密，出现的会是 `llm_tls_intercepted`，
  处理方法见 `STRIX_EXTRA_CA_FILE`（由 `setup.sh` 校验并挂载 CA bundle）。

### 2.5 `STRIX_DOCKER_SANDBOX_NETWORK`

- **是什么**：设了它，Strix 创建沙箱时把容器接入该网络、不发布端口，并把沙箱内 Caido 抓包代理的地址解析为
  **沙箱在该网络上的 IP**。控制台设为 `strix_sandbox`，**且 `api` 容器自己也加入这个网络** ——
  后者是前者生效的前提，不是可选优化。
- **不这样会怎样**：不设时，Strix 把代理端口发布到 Docker 宿主的 `127.0.0.1` 上，并把地址解析成
  `127.0.0.1:<随机端口>`。从 `api` 容器里看，`127.0.0.1` 是它自己 —— 代理连不上，抓包能力**静默降级**，
  扫描照样跑但质量变差，没有任何报错。
- `strix_sandbox` 是没有 compose 项目前缀的全局网络名（Strix 要求字面匹配）。创建或删除前先
  `docker network inspect` 确认它不属于别的项目。

### 2.6 `STRIX_RUN_ID` 与按 label 回收

- **是什么**：Strix 把 `STRIX_RUN_ID` / `STRIX_RUN_TYPE` 打成沙箱容器的 label `strix-run-id` / `strix-run-type`。
  控制台设 `STRIX_RUN_ID=<scan_id>`、`STRIX_RUN_TYPE=console`，`reaper` 按
  `label=strix-run-type=console` **且 `strix-run-id` 非空**两个条件定位孤儿沙箱。
- **不这样会怎样**：Strix 的信号处理器直接 `sys.exit`，异步的沙箱清理永远跑不完 —— **每一次停止或强杀都会
  泄漏沙箱容器**。没有 label 就只能按名字或时间去猜，会误删别人的容器。两个条件缺一不可：只按类型过滤，
  会把同类型但不属于任何扫描的容器也算进来。

### 2.7 同路径挂载与 DooD 路径别名

- **是什么**：Strix 在 `api` 容器里调用宿主 Docker 创建沙箱，传给 Docker 的挂载路径由宿主上的 daemon 解析。
  所以凡是会被挂进沙箱的路径（Strix 为目标暂存的仓库、API spec 等，都放在 `$TMPDIR` 或 cwd 下），
  必须在 `api` 容器内和宿主上**是同一个路径**。控制台用
  `${STRIX_HOST_DATA_DIR}:${STRIX_HOST_DATA_DIR}` 同路径挂载数据目录，`TMPDIR` 也指到该目录下。
- **不这样会怎样**：路径两侧不一致时，Docker 会在宿主上挂一个不存在或错误的目录，沙箱里看到的是空目录 ——
  同样静默失败。**不能用 named volume**：它在宿主上的真实路径位于 Docker Desktop 的 VM 内部，两侧不一致，
  会原样重现这个问题。`setup.sh` 还会拒绝 Strix 自己拒绝挂载的目录（`~/.config`、`~/.ssh`、`~/.aws`、
  `~/.docker`、`~/.kube`）。

### 2.8 `agents.db` 不是只追加的

- **是什么**：Strix 的 `.state/agents.db`（SQLite）在上下文压缩时会清空会话再整体重插，行 id 重排；
  上下文压缩默认开启。
- **不这样会怎样**：按"已推到第几条"做增量推送会漏推、错推，而且不报错。控制台因此用 epoch + 重同步检测 +
  自己的只追加镜像（`scan_events` 表）。完整判据见 `docs/ARCHITECTURE.md` 第 3 节。
- 区分"压缩"和"截图淘汰"要用到两类上游字面量：压缩后首条消息里的 `<conversation-checkpoint>` 标记，
  和三条截图占位字面量。它们都收在 `strix_profile.py`，**升级时必须核对**：字面量变了，识别就会失效。

### 2.9 截图只保留最近 3 张

- **是什么**：截图以 `data:image/png;base64,...` 的形式内联在事件里（不需要从沙箱 `docker cp`），
  但 Strix 只保留最近 3 张，更早的会被替换成占位字面量。控制台**首次见到就落地**到
  `media/<sha256>.png`，事件里改写为 `/api/scans/{id}/media/{sha256}.png`。
- **不这样会怎样**：稍长一点的扫描，前端和报告里的截图会陆续消失。

### 2.10 import 边界

- **是什么**：`api` 进程只允许 import 两处 Strix 模块，且全部收口在 `backend/app/strix_bridge/`：
  - `strix.interface.tui.backend.live_view` 的 `TuiLiveView` —— 必须用这个**子类**，它才有有上界的事件缓冲和
    `event_snapshot()`；父类只有基本的 `hydrate_from_run_dir()`。
  - `strix.interface.viewer.transcript` 的 `read_run_summary`、`read_vulnerabilities`、`read_report_markdown` 等只读函数。
  - **禁止** `strix.core.*` 与 `strix.runtime.*`（上游传递带进来的除外）。
- **不这样会怎样**：import 核心模块会把 agent SDK、litellm 和"改写 `os.environ` 配置模型"的逻辑一起带进 web 进程，
  等于把"一进程一扫描"的隔离从内部打穿。这条边界由 `backend/tests/test_strix_contract.py` 用 AST 扫描强制。

### 2.11 必设与禁设的环境变量

| 变量 | 设为 | 原因 |
|---|---|---|
| `STRIX_TELEMETRY` | `false` | 关闭遥测外联。遥测本身不发 Key，但本地工具应默认静默，且本项目不允许任何数据外发 |
| `STRIX_NO_UPDATE_CHECK` | `1` | 关闭启动时的更新检查外联 |
| `LITELLM_LOG` | `ERROR` | 压低模型调用库的日志量，减少泄漏面 |
| `STRIX_DOCKER_SANDBOX_NETWORK` | `strix_sandbox` | 见 2.5 |
| `STRIX_RUN_ID` / `STRIX_RUN_TYPE` | `<scan_id>` / `console` | 见 2.6 |
| `STRIX_DEBUG` | **绝不设置** | 会把 `strix.log` 拉到 DEBUG 级别，模型请求细节进日志，是泄漏面 |

此外还要知道：Strix 自带的 viewer 有邮箱门、PostHog 统计和"把报告发到第三方中继"的功能。控制台**不代理它**，
所有展示自己渲染；它需要的数据（agent 拓扑、事件、产物）本来就全在控制台的实时流和镜像里。

## 3. 升级 Strix 时

架构不需要因升级而改动：走 CLI 让接口面限于 argv/env/退出码/产物文件；Strix import 收口在 `strix_bridge/`；
临时 HOME + 显式 `--config` + 结束即删是正向防线，不依赖上游具体怎么写配置。需要看住的是**检测层**：

- **`backend/tests/test_strix_contract.py` 是预警线**。它钉住控制台依赖的上游事实（import 边界、env 名、
  label 语义等）。升级后它变红，说明某条集成前提变了 —— 先查清是哪条、影响哪一节，再决定怎么改，
  不要为了让它变绿而改测试。
- **上游事实对照表是 `backend/app/strix_profile.py`**：`run.json` 状态取值、失败归因规则、截图与压缩的字面量等
  都按版本收在这里，且它自己不 import Strix。升级时逐项对照新版本源码核一遍。
- 升级要**单独一次提交**，不与业务改动混在一起；跟 minor 版本，不追 patch。
- 若升级同时牵涉模型路由或 litellm 版本，要重新验证费用估算，否则预算护栏可能静默失效。
- 归因规则依赖上游错误输出的措辞。升级后若某类故障开始落到 `llm_connection_failed` 兜底，说明措辞变了，
  要补规则。
