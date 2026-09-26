# 架构说明

本文讲清这个控制台由哪几块组成、一次扫描从头到尾经过哪些环节，以及几个关键设计背后的理由。
与 Strix 本身的集成细节（每个环境变量为什么必须设、升级时看什么）见 `docs/STRIX-INTEGRATION.md`。

## 1. 组件图

```
浏览器 https://localhost ── sessionStorage 只存不透明的 vault_handle
   │  REST + WebSocket(wss)；模型 Key 只经 JSON POST body 传输
   ▼
nginx（TLS 终止，只绑 127.0.0.1:443，不监听 80）
   ├ /          ──▶ web（Next.js，只 expose 不发布端口；Key 不经过它）
   └ /api, /ws  ──▶ api（FastAPI，python:3.12-slim + docker-cli + strix-agent，单 worker）
                     ├ key_vault       内存 {handle → SecretStr}，带 TTL，绝不持久化
                     ├ target_guard    目标解析/规范化/DNS/分类（无 IO 纯函数）
                     ├ scan_launcher   拼 argv + env + 临时 HOME + 预置 --config + cwd/TMPDIR
                     ├ scan_supervisor 管理子进程，退出码与 run.json 状态 → 中文结论
                     ├ scan_channel    每个扫描一个轮询器，向 N 个 WebSocket 订阅者推帧
                     ├ run_projector   重投影 Strix 产物 + epoch 重同步
                     ├ event_mirror    只追加事件镜像；截图首次见到即落地
                     ├ log_tailer      续读 strix.log，脱敏后推送
                     ├ reaper          按容器 label 回收孤儿沙箱
                     ├ translator      中文白话报告（用用户自己的 Key 直连模型）
                     └ SQLite console.sqlite —— 任何表都没有 Key 字段
   │ 子进程：strix -n -t <目标> -m <模式> --max-budget-usd N --max-turns N
   │          --instruction-file <文件> --config <tmpfs>/cli-config.json
   │ env：STRIX_LLM / LLM_API_KEY / STRIX_RUN_ID=<scan_id> / STRIX_TELEMETRY=false …
   │ cwd=<数据目录>/scans/<id>   HOME=/run/strix/scan-<id>/home（tmpfs）
   ▼
strix 进程 ──docker.sock──▶ 沙箱容器（宿主上的兄弟容器，内含 Caido 抓包代理 :48080）
                             api 与沙箱都在固定名网络 strix_sandbox 上 → api 能直达沙箱 IP
```

几条结构性决定：

- **对外只有 nginx 一个入口**，只绑 loopback 的 443。`api` 与 `web` 只在 compose 内部网络 `expose`，
  防火墙配错也暴露不出去。
- **`web` 进程碰不到 Key**：浏览器提交 Key 的请求直接由 nginx 转给 `api`。
- **`api` 必须单 worker**：`key_vault` 是进程内字典，多 worker 时请求落到别的进程会随机找不到 handle。
- **沙箱是"兄弟"而非"子"容器**：`api` 挂载宿主的 `docker.sock`（DooD），由 Strix 在宿主 Docker 上创建沙箱。
  代价是挂 `docker.sock` 约等于宿主 root 权限，这一残余风险在 `docs/SECURITY-zh.md` 中如实说明。

## 2. 一次扫描的全生命周期

1. **向导**：用户选模板（见第 6 节）、填目标、预算与轮数，并在本会话录入模型凭据
   （`POST /api/keys`，只返回不透明的 `vault_handle` 和脱敏标签，Key 留在 `api` 内存里）。
   录入时会发一次极小的验证请求，Key 错了一秒内就知道，而不是扫到一半才失败。
2. **目标校验**：`POST /api/targets/validate` 对每个目标做规范化、DNS 解析与地址分类，
   私网/回环要显式勾选；云元数据地址永久硬拦、不可覆盖。
3. **授权准入**：`POST /api/scans` 必须带授权声明（操作人、授权依据、手打确认、三项确认勾选、
   用户看到的解析 IP）。服务端在启动前**重新解析 DNS** 并与声明时比对，不一致即拒（`dns_changed`）。
   授权记录先入 `authorizations` 表，`scans.authorization_id` 是 `NOT NULL` 外键 ——
   "没有授权记录的扫描"在数据库层面就写不进去。预算必填，超出天花板配置
   `console_max_budget_ceiling_usd`（默认 100）即拒（`budget_exceeds_ceiling`）。
4. **启动子进程**：`scan_launcher` 为本次扫描准备独立 cwd、tmpfs 上的临时 HOME 和预置的
   `cli-config.json`，把模型路由与 Key 只放进子进程 env，然后以 `strix -n` 非交互模式启动。
   argv 里没有任何 Key。
5. **实时流**：`scan_channel` 轮询 Strix 的产物目录，把 agent 拓扑、事件、漏洞、费用、日志推给
   `WS /ws/scans/{id}`（SSE 兜底：`GET /api/scans/{id}/stream`）。设计细节见第 3 节。
6. **结束与结论**：`scan_supervisor` 综合三件事给出结论：退出码、`run.json.status`、
   以及"是不是我们自己发的停止信号"。退出码 `0` 并不代表跑完了（预算耗尽也退 0），
   所以结论以 `run.json.status` 为准，`stopped` 一律报"结论不完整"（`scan_incomplete`）。
   子进程结束后临时 HOME 整个删除。
7. **报告翻译**：`POST /api/scans/{id}/report/zh` 用用户同一个模型路由把每条发现翻成给业务/管理人员看的
   中文白话，并出一段执行摘要与"哪些没测"。翻译用 httpx 直连模型，不把 litellm 引入 web 进程；
   结果按输入哈希与模型缓存在 `report_translations`，新发现只翻新增部分。
   `poc_script_code` / `evidence` / `endpoint` / `code_locations` 永远原文保留。
8. **导出**：打印版 HTML（自包含单文件、截图内联，浏览器"另存为 PDF"）、Word 版、
   以及 Strix 原始产物（Markdown / CSV / SARIF）和整包 `raw.zip`。

## 3. 实时流设计：epoch + 重同步 + 只追加镜像

### 为什么朴素游标不行

直觉做法是"记住推到第几条事件，下次从那里续"。这在 Strix 上会出错，因为它的
`.state/agents.db` **不是只追加的**：上下文压缩（默认开启）会把早期的若干条对话塌成一条摘要，
实现上是清空该会话后整体重插，行 id 随之重排。游标记住的"第 N 条"指向的已经是另一条消息，
增量推送会漏推、错推，且没有任何报错。截图预算（只保留最近 3 张）则会把较早的截图原位替换成占位字面量。

### 轮询与差分

每个扫描一个 `ScanChannel`：**一个轮询器、N 个订阅者**，因为每次重投影的开销与整条对话长度成正比，
不能每个浏览器标签各算一遍。每一拍：

1. 先 `stat()` `run.json`、`.state/agents.json`、`.state/agents.db`、`vulnerabilities.json`，
   四者都没变就整拍跳过（空闲时几乎零开销）；
2. 用 Strix 的只读投影层重读产物目录，得到 agent 列表与事件列表；
3. 依次对 agent、事件、运行摘要（状态/费用）、漏洞（按 id）、报告正文做差分，只推变化。

轮询频率自适应：有变化时快、空闲时指数退避；扫描结束且子进程已退出后停止。

### 重同步的判据

对事件列表维护"已发送的 key → 指纹"、发送顺序和当前 `epoch`，按顺序判断：

- **列表变短**或**同一位置的 key 被换掉** → 发生了重排（压缩）。`epoch += 1`，在新 epoch 下把每条事件
  当作 `event.add` 重发一遍。若新的首条事件带 Strix 的对话摘要标记，就明确告诉用户
  "早期对话已被摘要替代"（notice 码 `context_compacted`），否则只报 `stream_resynced`。
- **事件自身版本号递增**（如状态从 running 变为 completed）→ 正常更新，发 `event.update`，不动 epoch。
- **指纹变了、版本没变，且变化恰好是那三条截图占位字面量之一** → 这是截图淘汰：保序、保长度，
  **不需要重同步**（我们早已把截图落地），只发 `screenshot_elided` 提示。
- 指纹变了、版本没变、又不是截图淘汰 → 可疑的改写（`mutated`），同样 `epoch += 1`。

把"截图淘汰"和"压缩"分开是刻意的：若把前者当成后者，每淘汰一张截图就会让前端全量重同步一次，
还会给出一句不真实的"上下文已压缩"。识别截图占位**只认那三条精确字面量**，不做"包含某个词"的模糊匹配，
否则正常消息会被误判。

### 只追加镜像

每条推出去的事件帧都写进 `scan_events(scan_id, epoch, seq, …)`。事件里内联的 `data:image/png;base64`
截图解码一次落到 `<数据目录>/scans/<id>/media/<sha256>.png`，事件里改写成
`/api/scans/{id}/media/{sha256}.png`。收益：WebSocket 帧变小；截图不怕被 Strix 淘汰；报告能内嵌截图；
断线重连可以从镜像回放。

镜像写失败时**让整个 channel 任务失败**，而不是跳过这一帧 —— 只追加的真源出现空洞比断流更糟。
某个订阅者消费太慢、队列满了，就把它摘掉让它带游标重连，**绝不阻塞轮询循环**。

### 帧格式与重连

信封为 `{v, epoch, seq, type, ts, payload}`，`type` 取 `agents`、`event.add`、`event.update`、`vuln.add`、
`summary`、`log`、`report`、`notice`、`error`、`done`。客户端连上后发
`{"type":"hello","resume_from":{"epoch":…,"seq":…}}`：epoch 与服务端一致就从镜像回放，否则收到新 epoch 下的全量。

- 重同步就是"新 epoch 下逐条重发 `event.add`"，所以回放与直播是同一种帧形状，前端只有一套解析逻辑。
- `seq` 只用于排序和重连游标，**不是丢帧探测器**：非事件帧与事件帧共用计数器但不进镜像，回放必然有空洞；
  "作废本地状态"完全由 `epoch` 决定。
- `done` 帧**不带结论**。扫描状态、归因、漏洞数只从 `GET /api/scans/{id}` 取 —— 同一个结论只有一个出处，
  避免两份判决互相矛盾（尤其不能出现只凭退出码就报"未发现漏洞"）。

## 4. 数据模型要点

SQLite 文件 `console.sqlite` 在数据目录下，WAL 模式。表：`authorizations`、`scans`、`scan_events`、
`scan_agents`、`scan_findings`、`scan_media`、`audit_log`、`report_translations`。

- **任何表都没有 Key / secret / token / password 列**。`api` 启动时 `assert_no_secret_columns()` 校验，
  有对应测试。`scans.vault_handle` 只是内存 handle 的不透明 id，`api` 重启后即失效；
  `scans.env_var_names_json` 只存变量名；审计只存脱敏标签。
- 向导里填的测试账号只进 tmpfs 上的 instruction 文件（随任务目录删除），数据库只留 `instruction_sha256`。
- `scans.authorization_id NOT NULL` —— 让"未授权的扫描"在结构上不可能。
- `scans.auth_shape NOT NULL` —— `api` 重启后 handle 失效，续跑时要靠它知道该重新索要哪几个凭据。
- `scan_events` 是只追加镜像，主键 `(scan_id, epoch, seq)`；`scans.current_epoch` 记当前 epoch。
- `report_translations` 主键 `(scan_id, finding_id, input_hash, model, lang)`：内容不变不重翻，换模型重翻但不丢旧译文。
- 审计双写：`audit_log` 表 + 数据目录下按月的 `audit/YYYY-MM.ndjson`，数据库丢了记录也在，可直接 grep。
- 登录账号不在 SQLite 里，而在数据目录的 `auth.json`（0600，scrypt 加盐），因此不需要给
  `assert_no_secret_columns()` 开任何豁免。

## 5. 后端接口一览

除 `/api/health` 外全部需要登录。错误响应统一为 `{code, trace_id, params}`，前端只按 `code` 分支。

| 方法与路径 | 用途 |
|---|---|
| `POST /api/auth/login` · `POST /api/auth/logout` · `GET /api/auth/me` | 单账号登录会话 |
| `GET /api/health` | 健康检查（唯一免登录） |
| `GET /api/system/status` · `POST /api/system/pull-image` · `WS /ws/system` | 环境自检、拉取沙箱镜像及其进度 |
| `GET /api/providers` | 模型目录与每家所需的凭据键 |
| `POST /api/keys` · `GET /api/keys/{handle}` · `DELETE /api/keys/{handle}` | 录入并验证凭据 / 查询 / 删除 |
| `GET /api/scan-templates` | 向导模板 |
| `POST /api/targets/validate` | 目标校验（恒返回 200，逐个目标给结论） |
| `GET/PUT /api/allowlist` · `POST /api/allowlist/entries` · `DELETE /api/allowlist/entries/{label}` | 目标白名单 |
| `POST /api/scans` · `GET /api/scans` · `GET /api/scans/{scan_id}` | 发起 / 列表 / 详情（结论唯一出处） |
| `POST /api/scans/{scan_id}/stop` | 停止（`mode`: `graceful` / `force`） |
| `POST /api/scans/{scan_id}/resume` | 续跑（需重新提供 `vault_handle`） |
| `WS /ws/scans/{scan_id}` · `GET /api/scans/{scan_id}/stream` | 实时流（SSE 为兜底） |
| `GET /api/scans/{scan_id}/media/{sha256}.png` | 已落地的截图 |
| `POST/GET /api/scans/{scan_id}/report/zh` | 生成 / 读取中文报告 |
| `GET /api/scans/{scan_id}/report/print` · `GET /api/scans/{scan_id}/report/docx` | 打印版 HTML / Word 版 |
| `GET /api/scans/{scan_id}/export/{kind}`（`md` / `csv` / `sarif`） · `GET /api/scans/{scan_id}/raw.zip` | Strix 原始产物 |
| `GET /api/audit.csv` | 审计日志导出 |

## 6. 向导模板 → CLI 参数

模板是后端的进程内常量（`services/scan_templates.py`）。改模板与改代码同样要重建镜像，
用 YAML 只会多出加载器、校验模型和打包路径三层，还把拼写错误从启动期推迟到运行期。

| 模板 | `template_id` | `-m` | 默认预算 / 轮数 | 指令要点 |
|---|---|---|---|---|
| 快速体检 | `quick_triage` | `quick` | $5 / 60 | 限时分诊，只测少数高影响、可直接利用的问题；跳过子域枚举与目录爆破 |
| 全面体检（推荐） | `full_review` | `standard` | $25 / 200 | 先枚举功能与角色，再系统性覆盖 OWASP Top 10 各入口，边走边记覆盖面 |
| 深度审计 | `deep_audit` | `deep` | $80 / 500 | 按功能域派生子 agent，把发现串成完整攻击链 |
| 只测登录与权限 | `auth_and_access` | `standard` | $15 / 120 | 登录/注册/找回/会话/越权；不花轮数在 XSS 与注入上；只用自建测试账号，绝不锁死真实账号 |
| API 接口测试 | `api_surface` | `standard` | $25 / 200 | 枚举接口，额外报告"文档里有但不可达"与"未文档化但存在"的接口；目标仍是普通 URL（暂不支持上传 spec） |
| 上线前复检 | `pre_release_recheck` | `standard` | $12 / 100 | 针对改动说明回归；已报问题要实测是否真修好 |

所有模板共用的部分：

- 预算映射到 `--max-budget-usd`、轮数映射到 `--max-turns`，模板指令写进 `--instruction-file` 指向的文件。
  **`--max-budget-usd` 强制必填**（Strix 本身默认无限），缺它 `scan_launcher` 拒绝构造 argv。
- 预算是**软上限**：Strix 每轮结束后才比较花费，所以总会超出约"一次模型调用"的量；
  接近上限时它会提醒 agent 收尾。界面只说"达到 $X 后停止"，不承诺"绝不超过 $X"。
- 指令正文用英文（Strix 的 system prompt 与 skill 都是英文），并统一追加：发现的描述/影响/修复要中英双写，
  禁止 DoS、资源耗尽、数据破坏、账号锁定类测试。**授权边界不依赖这段 prompt**：权威的目标范围由 Strix
  自己注入，用户指令不能扩大或削弱它。
- 高级选项可覆盖扫描模式、预算、轮数、推理强度（`STRIX_REASONING_EFFORT`）和追加指令，全部汇入同一个 argv 构造器。
- 目前只支持 URL / 域名 / IP 目标。不支持本地源码目录的原因不是技术上做不到，而是 Strix 会把目录**读写**
  挂进一个有 `NET_ADMIN` 能力的自主 agent 容器，对非专业用户风险过大。

## 7. 为什么走 subprocess CLI，而不在进程内调用 Strix

1. **并发安全**：Strix 有大量模块级全局可变状态，且会改写 `os.environ` 来配置模型。同一进程里跑两个扫描，
   两个用户的模型配置和 API Key 会互相污染 —— 这是跨用户密钥泄漏。所以必须一进程一次扫描。
2. **内嵌的真实工作量远超函数签名所示**：进程内入口不做目标推断、spec 暂存、运行记录持久化、信号与退出处理，
   这些都得照抄 CLI 的内部逻辑，然后由我们长期维护。最终还是要 fork 进程，等于做了个更差的 `strix -n`。
3. **Key 卫生更好**：Key 只存在于子进程 env 和 `api` 的内存里；`api` 进程从不 import litellm、
   也不设任何供应商的 Key 变量。
4. **版本容错**：argv、env、退出码和产物文件是 Strix 的公开接口，比 Python 内部 API 稳定得多。
5. 唯一的损失是亚秒级的逐 token 流（它只在 Strix 内存里、从不落盘）。对一个动辄几十分钟的渗透测试，
   按消息粒度、约一秒的延迟完全够用。

另一个被否决的方案是把 Strix 隔离到独立镜像、让 `api` 完全不装它。这不成立：实时流必须用 Strix 的只读投影层，
否则就得自己解析 `agents.db` —— 那是比 Python API 更不稳定的接口，还带着重插与 id 重排语义。
`api` 对 Strix 的 import 已被收口在 `backend/app/strix_bridge/` 并由测试强制（见 `docs/STRIX-INTEGRATION.md`），
隔离墙已经在正确的位置。
