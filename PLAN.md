# Strix Web 控制台 — 实施计划

## 交接（2026-09-25）

> **这一节每次交接整段覆盖，不累积历史。**只写"新会话开工前必须知道、又不在别处的事"。

### 最新（2026-09-26 第四／五段）：**T30b1 已收货提交；T30b2 再拆成 b2a／b2b，b2a 交底已写、待派**

- 交底 `~/Documents/claude/dispatch/T30b1/prompt.md`（模板 2；12 条：1–5、21–24、27、28；预算 ≤30 次、峰值 <90k）。交底时定的：5 的"真 Key + 全盘 grep"挪到 T30b2（b1 全程无真 Key，检查 4 用 `verify:false` 假 Key 拿 handle）；split_horizon 与"原本无白名单文件时的 enforce"输出 `MANUAL` 不算 PASS；27 的路由清单从运行中 app 枚举 + 数量下限；21 只在 `TEARDOWN=1`/`ONLY` 显式含 21 时跑。**唯一不变式：不许假绿**（每个 0 命中断言配阳性对照、4xx 同时断言 `code`）。**收货 mutation 3 处**：M1 库名改 `console.db` → 28 FAIL；M2 无 cacert 那次加上 cacert → 24 FAIL；M3 路由枚举只取 3 条 → 数量下限那条 FAIL。登录部分要用户本人跑（口令只有用户有）。
- **✅ T30b1 收货提交（2026-09-26；用户全量跑：1/2/22/23/24/27/28 PASS，3 仅 split_horizon 一格 MANUAL；随后按用户拍板改两处复跑 `ONLY="4 5"` 全 PASS —— ① 5 的门槛 2s → 后端 `VERIFY_TIMEOUT_SECONDS`（验收 5 原文已改）；② 无白名单文件时 4 临时 PUT 一份 enforce 清单、测完删文件并断言 `file_present:false`）**：`scripts/verify_e2e.sh` 524 行（超 500 线，子 agent 事后才发现）+ `Makefile` +7/−2。`make lint` 绿、`make test` **1543 passed / 0 skipped**；无登录子集 `1 22 23 24 27` 全 PASS（27 枚举 26 条需鉴权路由＋2 条 WS，全 401）；M2 只红 24（5 次复跑基线稳定）、M3 只红 27；M1 要登录，机制已单独实测（库名写错 → `[ -f ]` 拦住，`mode=ro` 不建库）。子 agent 偏离全认（27 豁免多一条 `/api/auth/logout`＝`EXEMPT_PATHS`；4 在有白名单文件时先切 advisory 测 typed_confirmation 再切 enforce，因为 enforce 下白名单先拦）。**主会话改了两处**：① 4 的 NULL INSERT 与 28 的 `.dump` 从宿主 `sqlite3` 挪进 api 容器的 python（库是 WAL，`-shm` 跨不过 Docker Desktop VM 边界，宿主写容器正开着的库有损坏风险；`mode=rw`／`mode=ro` 不建库；INSERT 一律 ROLLBACK）；② 登录体从临时文件改成进程替换 `< <(...)`（不落盘；不用管道是因为子 shell 带不回 `HTTP_STATUS`）。**预算**：31 次、峰值 ≈96k（超 90k 预算、未到 120k 真闸）、0 压缩；**②首写第 24 次 ❌** —— 前 23 次都在探活栈拿响应形状（providers／scan-templates／EXEMPT_PATHS 等），**T30b2 交底要把这些响应样例直接贴进去**。
- 未实测的一支：**已有**白名单文件时 4 的"切 advisory → 切 enforce → 原样 PUT 回去"往返（本机无文件，走的是临时文件那支）。
- **`!` 前缀跑不了要登录的检查**（无 TTY，`read` 读不到）→ 让用户在自己的终端里跑；脚本已加 `[ -t 0 ]` 提示。
- **T30b2 按用户拍板又拆成两条**（b1 的脚本已 577 行，一条塞不下 14 个检查）：**T30b2a**＝一次扫描的前半生
  （5b 真凭据、6 发起、7 实时流、26 SSE、8 argv、9 沙箱、10 Caido、11 全盘卫生、12 优雅停止、25 空闲长连接），
  **T30b2b**＝17 Key 生命周期、18 续跑、13 自然结束、19 审计、20 重启恢复（复用 b2a 的 helper）。
  **钱**：两次扫描、最坏 ≈$5 —— 扫描 A（b2a）预算 `$1` 手动停；扫描 B（b2b）`$0.3` 撞预算停→续跑到总额 `$4`
  试自然结束，仍没跑完则 13 报 MANUAL。金额全参数化，只收 `bedrock_sigv4`。
- **T30b2a 交底已写** `~/Documents/claude/dispatch/T30b2a/prompt.md`（模板 2，164 行；预算 ≤35 次、峰值 <100k）。
  交底时定的：录帧器跑在 api 容器里（宿主 py3.9 没有 `websockets`，容器里有 15.0.1），走 `wss://nginx/...`、
  cookie 从 stdin、`ping_interval=None`；`sweep_all` 五个面各配一个阳性对照 token；真凭据 handle 用新变量
  `REAL_HANDLE`（别复用 b1 检查 4 的 `VAULT_HANDLE`）；`TARGET` 默认 `http://localhost:13000`（PLAN 原文那条
  loopback 改写路径，**从没在真扫描里验过**），备选 `http://juice-shop:3000`，`typed_confirmation` 由 validate
  响应自动导出所以换 target 不必改代码。**收货 mutation 3 处**：M1 把 `sweep_all` 的模式换成确实存在的字符串
  → 11 FAIL；M2 录帧器截到 3 帧 → 7 FAIL；M3 检查 9 的筛选去掉 `strix-run-id` 一半 → 9 FAIL（会命中 m0 靶场）。
- **本轮顺手改了 6 条验收原文**（都是"代码里没有那个值"，一律改验收不改代码）：11 的 containment 在 `bedrock_sigv4`
  下是 0 命中（条 25）、12 的 `exit_meaning: stopped`、13 的 `completed_with_findings`、20 的 `orphaned_running`、
  25 改用 `/ws/system`、19 的 `report.exported` T23 没实现 → **用户拍板补，T23c 已做**（见下条）。
- **✅ T23c（2026-09-26，主会话直接做，与 T30b2a 并行、文件不重叠）**：`EVENT_REPORT_EXPORTED`＋`downloads._audit_exported`，
  四个路由在成功下发前各记一条（`export` 记在 `is_file` 之后）；`test_routes_downloads.py` 参数化 6 格 + 404 不记。
  mutation 2 处：去掉 raw_zip 那条 → 只红 `[raw.zip-raw_zip]`；export 审计挪到 `is_file` 前 → 只红 `missing_file_is_404`。
  **T30b2b 交底里 19 写成 PASS 断言**（见验收 19 ★）。
- **api 镜像里没有 `ps`／`strings`／`pgrep`**（只有 `grep`）→ 验收 8 只能 `python` 读 `/proc/*/cmdline`。
- 之后：T30b2a → T30b2b → T30b3（压缩韧性 14）→ T30c（`make reap`）；15、16 是人工核对清单。

### 上一段（2026-09-26 第三段）：**T30a 收货提交（`9ff3491`）；T19 收货提交（人眼通过）**

- **T30a**：`SECURITY-zh.md` 由主会话写（用户定），其余三份子 agent 写；四份整读、引用名逐个 grep 过。T30a 行 ①–④ 全部落进 SECURITY 第 6／8／9／10 条。
- **T19**（方案 `~/Documents/claude/dispatch/T19/plan.md`，用户已批；砍 skill 自选、bearer 只警示不加按钮）：T19a 后端 `requires_notes`（仅 `pre_release_recheck`）+ launcher 422 + `templates`／`templateNotes` 文案覆盖测试；T19b 前端（子 agent 17 次调用、峰值 ≈68k、0 压缩；②首写在第 11 次，差 1）。`make lint` 绿、`make test` **1543 passed / 0 skipped**；mutation 2 处全对（去 launcher 检查只红那条参数化 3 格；删 `templateNotes.api_surface` 只红 coverage 的 templateNotes 格）。主会话改了一处：测试账号折叠区 `open={needed \|\| rows.length > 0}`（换模板不收起已填的行）。人眼 2026-09-26 通过，已提交。
- **记下的跟进（未做）**：① `CONSOLE_MAX_BUDGET_CEILING_USD` 没经 `docker-compose.yml` 透传，写进 `.env` 不生效；② CLAUDE.md 引用而仓库没有的名字（`make reap`／`make verify-e2e` 待 T30、`test_key_hygiene`、`make check-upstream`），错误形状应为 `{code, trace_id, params}`（`errors.py:65`）；③ 规划中的"启用了额外 CA"UI 警示不存在，文档暂指向 `GET /api/system/status`。
- 之后：T30b；「自」：T15a、断点续扫靶场复跑。

### 上一段（2026-09-26）：**待派只剩 T19 与 T30a，可并行**（文件不重叠：`frontend/src/components/wizard/*`＋`routes/templates.py` ／ `README.md`＋`docs/*`）

- **T19**（模板 3）：下一步由主会话写方案 → 用户审 → 另起实现 agent。**T30a**（模板 1）可以在审 T19 方案的空档里先派，T19 落地后再补一两句。
- 派发清单里 T25／T26／T31c 三行没打 ✅，但已提交（`bd7dbfc`／`6f8a6ea`＋`c9df371`）—— 不是待派项。
- 本日两件拍板已落（未提交时以 `git diff` 为准）：① T21 停机 cancel 补 `report.translated{outcome:"cancelled"}`（见 T21 行末）；② 续跑能力边界按 09-25 推翻改写（T30a 行清单 ④、`zh-CN.json` `coverage_incomplete.action`）—— **"复活后能补齐覆盖"仍是未验证**，靶场复跑（下方 09-25 段）照旧待用户。
- 「自」：T15a（卡 T15b）、断点续扫靶场复跑；T30b 在 T30a 之后。

### 最新（2026-09-25 深夜）：**T27a（`75f749b`）、T20（`243f2fa`）收货提交；T27b 已提交，T27 整行完成**

- T27a 收货：M1 只红 I1 29 格、M2 只红 I2 往返那条；子 agent 13 次调用 ≈66k、0 压缩。T20 收货：整读无缺陷、人眼通过；10 次调用 ≈42k、0 压缩。`make test` 1535 → T27b 后 1537 passed / 0 skipped。
- T27b：`downloads.py` 把 print 的取数提成 `_report_inputs`，新 `GET report/docx`（attachment，`to_thread` 渲染）；测试加 docx 正路一条、「未终态 409」参数化到 print／docx；前端 `reportDocxPath` + ReportPanel「下载 Word 版」链接（`report.downloadDocx`）。用户 2026-09-25 放行提交。**下一步：T19（模板 3，方案主会话写）**。

### 上一段：T27a 与 T20 并行派发

- **T27 拆成 T27a／T27b**：T27a（模板 2）`exporter_docx.render_report_docx` 纯函数 + 把 `exporter_html.py` 的结论句／排序／各段标题／证据取法提成公开函数两边共用（`test_exporter_html.py` 必须一行不改全绿）；交底 `~/Documents/claude/dispatch/T27a/prompt.md`，预算 ≤30 次、峰值 <90k、0 压缩。**两条不变式 → 收货 mutation 2 处**：M1 去掉非法 XML 字符替换（只红 I1 含 `\x01`／`\ud800` 的格）、M2 换行不转 `<w:br/>`（只红 I2）。**T27b**（路由 `GET report/docx` + ReportPanel 下载按钮 + 文案）**串在 T20 之后**（都改 `zh-CN.json`），接线层、主会话可自己做。
- **T20 方案用户确认（2026-09-25）**：`~/Documents/claude/dispatch/T20/plan.md`；交底 `…/T20/prompt.md`（模板 3 实现，≈250 行，预算 ≤30 次、峰值 <80k）。要点：tab「发现」进行中也可点、只显示原文、`<details>` 就地展开、`vuln.add` 只当 `invalidateQueries(["scan", id])` 的信号（store 仍丢弃，09-23 决定的轻微修改，用户已认）。**收货**：纯前端 0 mutation；`make lint`＋`make test`＋整读＋人眼看一次（juice-shop 扫描）。
- 导航栏：用户 2026-09-25 问过，**决定先不做**（T17d 的"不加顶栏导航"维持）。

### 最新（2026-09-25 夜）：**T22a／b／c 全部收货提交，T22 整行完成**

- **T22c 已派发** `~/Documents/claude/dispatch/T22c/prompt.md`（模板 3，方案＝T22 plan.md 已确认，frontend-design skill 不整挂、只抄风格约束进 prompt；6 个文件、≈300 行；预算 ≤30 次调用、峰值 <80k、0 压缩、第 6 次调用前开写）。交底时定的：报告 tab 只在 `canReport`（＝DownloadBar 同判据）时可点，选中后 tablist 下只渲染 `ReportPanel`；按钮两个 —— 主按钮 `force:false`（无译文叫「生成」、有译文叫「补全」，缓存按模型命中不重复付费）＋ ghost「全部重新生成」`force:true`；凭据不传 preset（翻译用哪家都行）；running 时不渲染 iframe、done 后重挂载即刷新（无 key 计数器）；`iframe sandbox=""`。**收货**：纯前端 → mutation 0 处；`make lint`＋`make test`＋读代码＋**人眼看一次页面**。⚠️ **待人眼验证的唯一风险**：`sandbox=""`（opaque origin）的 iframe 导航是否带 `SameSite=Strict` 会话 cookie —— 按规范祖先链同站应该带；若 iframe 里出现 401 JSON，退路是 `sandbox="allow-same-origin"`（仍无脚本，CSP 仍 `default-src 'none'`）。
- **✅ T22c 收货 2026-09-25**：`make lint` 绿、`make test` **1497 passed / 0 skipped**；子 agent **8 次调用、峰值 ≈46k、0 压缩**；整读无功能缺陷。**人眼**：`sandbox=""` 的 iframe 正常带会话 cookie、显示报告本身（风险解除）；「新标签页打开」原为裸链接，主会话补了与 `DownloadBar` 同形的 `.link`（第二份拷贝，第三次再提公共类）。prettier 不合规非本任务引入（无配置、不在闸门、HEAD 已不合规）。**T22 整行完成。**
- **T22b 已派发** `~/Documents/claude/dispatch/T22b/prompt.md`（模板 1 接线层，改 `routes/report.py`（抽 `latest_translations` 公共函数，加 `cost_usd` 列）、`routes/downloads.py`（新 `GET report/print` ＋ `REPORT_CSP`）、`tests/test_routes_downloads.py` 追加 P1–P3；预算 ≤25 次调用、峰值 <80k、0 压缩）。交底时定的：路由放 `downloads.py`（`_finished_scan`／`_db` 就在那，不必提 getter）；费用 = 参与报告的行（最新有效译文＋总述）求和，任一 NULL → None，零行 = 0.0；不写审计；CSP 照方案原样（未加 `frame-ancestors`；nginx 无 `X-Frame-Options`，iframe 同源可嵌）。**收货**：接线层无新不变式 → mutation 0 处强制，按交底 §2 的 W1–W6 抽查；`test_routes_report.py` 必须原样全绿。
- **✅ 2026-09-25 T22b 收货**：`report.py` 抽出 `latest_translations`（GET report/zh 行为不变）、`downloads.py` 新路由 ≈70 行、测试 +3 条；`make lint` 绿、`make test` **1497 passed / 0 skipped**。子 agent **6 次调用、≈45k、0 压缩**，无偏离。抽查 W1／W3／W5 各只红该红那几条；**自选 M9「总述取最旧一行」0 红**（T21c 起就没测，现在两条路由共用）→ 主会话给 `_seed_report` 补一条更早的旧总述行，复跑红 P1＋P2（P2 因旧行 cost 非 NULL，合理）。**T22c 交底要贴**：路由 `GET /api/scans/{id}/report/print`（`text/html`，拒绝码同 export：404／`artifacts_purged`／409 `scan_not_finished`）、`POST`／`GET report/zh` 的请求／响应形状（`routes/report.py` 的 `TranslateReportRequest`／`ReportZhResponse`）、`LivePanel.tsx` 的 tab 位置、`ResumePanel` 的 `forget()` 用法、`zh-CN.json` 已有的 `scan.tabReport`。

- **T22a 交底** `~/Documents/claude/dispatch/T22a/prompt.md`（模板 2，2 个新文件，≈420 行；预算 ≤30 次调用、峰值 <90k、0 压缩、第 5 次调用前开写）。交底时定的：入参是 `ReportScan`／`ReportFinding`（`raw`＝解析后的 `raw_json`、`zh: FindingZh | None`）＋ `ExecutiveZh | None` ＋ `report_cost_usd: float | None`；**属性里只出现固定常量**（severity 走固定映射表，未知值 `sev-other`）→ I1 只剩文本节点转义；证据块多带 `poc_description`（它也不进模型，否则在报告里彻底丢失）；结论 `complete = completed 且 error_code is None`，其余一律「结论不完整」+ 状态/原因码，空列表文案「没有记录到发现条目」。T22b 负责：按 `GET report/zh` 同一"最新有效译文"查询填 `zh`，`report_cost_usd` 任一行 NULL 就传 None。**收货 mutation**：M1 某一字段（如 `endpoint`）绕过 `_e`（只红 I1 那一格）、M2 有 zh 就不附证据块（红 I2 zh 那格）、M3 evidence 截断 `[:10000]`（红 I2 两格）、M4 `complete` 不看 error_code（红 `coverage_incomplete` 那格）、M5 空状态文案改"没有发现漏洞"（红 I3 各格）；自选至少一处。
- **✅ 2026-09-25 T22a 收货**：`exporter_html.py` 234 行＋`test_exporter_html.py` 265 行／50 用例；`make lint` 绿、`make test` **1494 passed / 0 skipped**。子 agent **9 次调用、≈61k、0 压缩**。整读无缺陷。**两处由主会话修**：① 交底自相矛盾（不完整正文「没有完整跑完」含禁用短语「完整跑完」），子 agent 把测试的禁用短语收窄成「扫描完整跑完」→ 改回严格版、正文改「没有跑完」；② **自选 M8「未知 severity 拼进 class 属性」0 红**：PAYLOAD 的 `"` 在末尾，整段困在属性值里 → PAYLOAD 改以 `"'>` 开头，复跑红 1。mutation 8 处全对：M1 红 1、M2 红 5（四个证据字段的 I1 格用 zh=FZH 构造，一并红，合理）、M3 红 2、M4 红 2（多红的 `scan.error_code` 格：completed＋有码时状态行不出现，合理）、M5 红 6、M6 红 1、M7 红 1、M8 红 1。**T22b 交底要贴**：`ReportScan`／`ReportFinding` 定义与 `render_report_html` 签名（`exporter_html.py:27-49,210-215`）、`GET report/zh` 的取数（`routes/report.py:257-297`，提成共用函数）、`downloads.py::_finished_scan`（`:62-76`）；`report_cost_usd` 任一行 NULL 传 None。

- **T22 方案** `~/Documents/claude/dispatch/T22/plan.md`（用户确认）：后端单一渲染器 `exporter_html.render_report_html` → 自包含零 JS HTML（CSP `default-src 'none'`）；报告 tab = `<iframe sandbox>` 嵌 `GET /api/scans/{id}/report/print`，导出 PDF = 新标签页 ⌘P；截图 v1 不放。拆 **T22a**（纯函数，TDD，I1 转义／I2 证据原样只取 raw／I3 结论不越权）→ **T22b**（路由＋CSP＋取数，复用 GET report/zh 的"最新译文"查询）→ **T22c**（前端 tab，模板 3 已由本方案覆盖，确认即派实现）。前置判定复用 `routes/downloads.py::_finished_scan`。
- **记下不追**：`_db`／`_settings` getter 已在 `report.py`、`downloads.py` 各抄一份（T22b 若再要就提到 `routes/_context.py`）。
- 其余待办不变：断点续扫真靶场复跑（先还原 `.bak`、`up -d --build api`）；T30a 文档（含续跑"不会恢复"措辞待改）。

### 最新之后（2026-09-25 晚）：**T21a／b／c 全部收货提交，T21 整行完成；下一步 T22（模板 3，方案主会话写）**，一件待定（停机 cancel 不记审计）在 §派发清单 T21 行末

- 方案、三条拍板、T21a 收货与交给 T21b 的两件都在 §派发清单 T21 行；方案原文 `~/Documents/claude/dispatch/T21/plan.md`（T21b／T21c 规格在里面）。
- T21b 交底要贴：`llm_client._completion_kwargs`／`model_for`／`verify` 的异常纪律（`llm_client.py:285-374`）、`report_zh` 的公开签名、`scan_findings` 与 `report_translations` DDL、`read_run_dir` 取 `run_dir` 的方式、`conftest` 的 `db`／`insert_scan`。

### 最新（2026-09-25）：**断点续扫真的能补测了 —— T32d 记为"接受的限制"那条已被推翻**

- **⚠️ 先读这条：`T32d`／上一段里"被强停的子 agent 永不重启、续跑不会补测、Strix 语义非我方可控、
  用户拍板接受并如实文档"——已被 2026-09-25 推翻。** 那个结论只对"不动盘上状态"成立。
  真开关是 `.state/agents.json` 里的状态字符串（详见 `pitfalls` 条 42，出处逐字核过）：
  `respawn_subagents` 只跳过状态不在 `{"running","waiting"}` 的非根 agent，而 `restore()`
  **原样拷状态、零归一化** → 把子 agent 从 `stopped` 翻成 `running`，headless 续跑就会重建它们。
  不需要 TTY。**已实测复活成功**（3 个子 agent 全起、真的在 `exec_command` 探测）。
- **两条必须一起做，少一条就白干**（这也是 2026-09-25 三次真跑失败的全部原因）：
  ① **翻状态**：`stopped`／`budget_paused` 的**非根**子 agent → `running`，并把
  `budget_stopped`／`reserve_stopped`／`budget_paused` 三个**过期**预算 flag 清成 False ——
  `wait_for_message`（`agents.py:348-352`）在这些 flag 下把子 agent 唤醒**只为走收尾**。
  ⚠️ 手工验证那次这三个 flag 恰好是 false，差点把"复活能用"这个假象写进产品代码：
  **凡是因预算储备停下的扫描（最常见那类）不清 flag 就静默复活失败**。
  ② **拦住 root**：复活后 root 干的是 `list_reports → stop_agent ×3 → finish_scan`，
  **45 秒内**把刚复活的三个孩子掐死在探测中途（花了钱、coverage 一条没加）。所以续跑指令在
  复活时第一要求是 `wait_for_agents` 且**明令禁止 `stop_agent`**；没复活时才退回原来"作废
  stopped agent、去建新的"那套 —— **两段措辞互斥**，复活后再说"图里 stopped 的都是死的"就是假话。
- **root 级 prompting 攻不动这个结构限制，别再往措辞上加钱**：`create_agent` 的 docstring 要求
  先 `view_agent_graph` 去重，而被强停的 agent 正顶着待补测范围的名字 → 两次真跑、两种措辞
  （含 `agent_name` 的、只含 `risk_area` 的）都是 **9 项清单换 0 个新 agent**。
- **✅ 本轮已落地（主会话直接做，未派子 agent）**：新 `backend/app/services/agent_checkpoint.py`
  （纯判定 `revive_snapshot` + 原子写回 `revive_checkpoint`，**零 `strix` import**）；
  `ResumeSpec.revived_agents` + `compose_resume_instruction(revived=…)` 两段互斥措辞；
  路由在 `build_resume_plan` **之前**调 `revive_checkpoint` —— **顺序是硬要求**，Strix 一启动
  就把这个文件读进 `restore()`，之后再改无效。`make test` **1342 passed / 0 skipped**、
  `make lint-api` 绿、**7 处 mutation 全对**（每条只红该红的那几格）。
  `test_agent_checkpoint.py` 末尾四条**读真实 strix 源码文本**钉住上面那几处字面量（不
  `import strix.core.*`）—— 升级 `strix-agent` 时它们红了就是提醒复活逻辑要重验。
- **下一步（待用户放行）：拿真靶场再跑一次续跑**，验的是"复活 + 不被掐死"合起来**真的产出
  coverage 条目**（这是唯一还没被证明的一环：复活已证、掐死已证、"不掐死就会记 coverage"未证）。
  素材：`fc1dce74…`（juice-shop，root `completed` + 3 子 agent `stopped`，`agents.json` 有
  我手工改状态时留的 `.bak-1790307422`，在 `${DATA}` 里、不在仓库）。
  **真跑前必须先把 `.bak` 还原回去**：那次手工翻转已经把三个子 agent 改成 `running` 了，
  不还原就等于让代码去翻一份已经翻好的图 —— 验的不是新代码。还原后起点应是 root `completed`
  + 三个子 agent `stopped`。另外 `up -d --build api`（api 镜像里没有本轮代码）。
- ~~顺带要改的文档~~ **2026-09-26 用户拍板现在就改**（只写已证的，"补齐覆盖"写成未验证）：T30a 行清单 ④ 已重写、UI `coverage_incomplete.action` 已改。
- 其余待办不变：**T17d** `/scans` 列表页（交底还没写）；T30a 文档。

### 上一段（2026-09-24）：**T32 全系列落地（a/b/c 提交、d 真跑得出限制）；下一步 T17d 或 T30a**

- **✅ T32c** 预算建议下限 $4 软提示（发起页 + 续跑页，只提示不拦），主会话直接落地，commit `4b32ed5`。
- **✅ T32d 真跑（sigv4 access key，juice-shop）得出关键限制**：续跑机制全通（判定「结论不完整」、指令注入、预算抬升都对），**但 root 在"子 agent 于 spawn 阶段被预算掐死"的扫描上续跑后不会重新派子 agent 补测** —— Strix 非交互续跑语义，非我方代码可控。加强 `compose_resume_instruction` 措辞（已改，测试绿，随本轮提交）无效。**用户拍板接受、如实文档**：写进 T30a 的 `docs/SECURITY-zh.md` 清单 ④（已加进 PLAN），UI 不得把续跑说成"能补全覆盖"（现有文案「被提前停掉的子任务不会恢复」已如实）。详见派发清单 T32d 行。**T32 系列到此收尾。**
- **下一步**：**T17d** `/scans` 列表页（交底还没写）；或 T30a 文档（含上面清单 ④ + ③ 口令落 run.json）。

### 上上段（2026-09-24）：**T17c 已收货提交；T17 三棒全部落地**

- **2026-09-24 用户拍板恢复续跑**（理由：一轮 ≈$0.25、预算必填 → "钱花完就停、停了只能从头重付"不合理）。已拆成派发清单 **T31a（argv）→ T31b（端点，模板 3，方案主会话写）→ T31c（前端）**，串行；验收 18 与 17 末句已恢复、总数改 28。三个已查到的坑（epoch 撞主键／旧 `run.json` 状态／`RunDiscovery` 看到旧目录）写在 T31b 行。**T31a 交底已写**：`~/Documents/claude/dispatch/T31a/prompt.md`（模板 2，2 个文件，预计 ≈220 行；两条不变式 I1「总额 > 已花费，等于也拒」、I2 黄金续跑 argv）。定下的三件：`总额 <= 已花费` 复用 `invalid_request(field=max_budget_usd)`、不加新码；`LaunchPlan.instruction_path／instruction_sha256` 改可空（续跑为 None，指令由 Strix 从 `run.json` 读回）；续跑重建 `cwd/tmp` 时**不带 `parents`**（cwd 被留存清理掉就抛错，不凭空重建）。**收货 mutation**：M1 `<=`→`<`（只红相等那格）、M2 续跑 argv 删掉 `-m`（只红黄金那条）、自选 M3 `tmp` 的 mkdir 加 `parents=True`（只红 cwd 不存在那条）。**✅ 2026-09-24 T31a 收货**（详见派发清单 T31a 行）。**T31b 方案已由用户拍板（2026-09-24），拆成 T31b1–T31b5（见派发清单）。✅ 2026-09-24 T31b1–b4 并行派发并收货**（`make test` 1268 passed / 0 skipped，7 处 mutation 全对，详见各行）。**✅ T31b5 交底已写**：`~/Documents/claude/dispatch/T31b5/prompt.md`（模板 2，6 个文件，预计 ≈435 行，R1–R7 七条接线测试）。**写交底时比方案多定的四件**：① `artifacts_purged` 查审计**排在 `resume_refusal` 之前**（否则被清理的 scan 先被报成 `no_checkpoint`，该码走不到）；② `try`/`finally cleanup_workspace` 必须从 `acquire` 之后开始 —— 挪到 `resume_refusal` 前会在"正在跑 → not_resumable"时删掉在跑那次的 HOME（R5 钉它）；③ reason 中文放新顶层树 `resumeRefusal`（纯字符串，形状同 `targetGuard.reasons`）+ `test_message_coverage.py` 双向对表；④ 条件 UPDATE 顺带写 `strix_run_name`（补 docker kill 残行的 NULL）。已知接受：续跑时 `start()` 抛 OSError → 行被标 `failed`，从此不可再续（同 create 语义，极少见）。**收货 mutation**：删 register（R1）、`resume=True`→False（R1）、`start_epoch` 写死 0（R2）、删准入重跑（R3）、删两处 `cleanup_workspace`（R4 各一格）、`try` 前移（R5）、删三元一致比较（R6）；自选至少一处。**✅ 2026-09-24 T31b5 收货**（详见派发清单 T31b5 行）—— **T31b 整行完成。下一步：T31c（模板 3 + frontend-design，方案由主会话写、用户确认后再派实现）**；T31c 顺带把 `errors.concurrency_limit.params` 的 `running_scan_id` 改成后端真实的 `active`。**✅ 2026-09-24 T31c 方案用户拍板（照推荐）**：页内展开区（不做弹窗）、新总额预填上次上限 ×2；`CredentialForm` 加可选 `preset`（锁 provider/形状/模型）；`useScanStream(scanId, generation)` 续跑 202 后 generation+1 重连。交底 `~/Documents/claude/dispatch/T31c/prompt.md`（7 文件，≈300 行；预算 ≤40 次工具、峰值 <100k、0 压缩）。**真跑素材已有**：`af609e56…`（`scan_incomplete`，$2.05/$2.00）；真跑前必须 `docker compose -p strix-console up -d --build api web`（两容器都早于 T31b5／648e7f4）。**✅ 2026-09-24 T31c 代码收货**：7 文件 +约 290 行（`ResumePanel.tsx` 164 + css 28）；`make lint` 绿、`make test` **1280 passed / 0 skipped**；纯前端 0 mutation；整读无缺陷。子 agent 4 处偏离全认（最要紧：有 preset 时藏掉模型样例按钮，否则 `setModel` 绕过只读框）。16 次调用、峰值 68k、0 压缩；②首次写码第 12 次（读的是它要改的 5 个文件，不是缺出处）。**✅ 2026-09-24 真跑续跑通过（T31c 整行完成）**：重建 api/web 后在 `af609e56…` 上续跑（用户在页面上操作：展开区、锁定的 provider/形状、预填 $4）→ `completed`，费用 $2.05→$2.45（接着算、非增量）、上限 $4.00、执行记录保留上次开头、结论「完整跑完，没有发现漏洞」（仅 completed 时才出现）。验收 17 末句 + 18 前端半边通过。**2026-09-24 追查 0 发现（产物 `scans/af609e56…/strix_runs/juice-shop-3000_41b8/`）**：① 首段 5.5 分钟花满 $2，三个子 agent 在 90% reserve 线被 Strix 强停，**一个探测都没发出**；② 续跑只恢复 root（`Resume: restored coordinator with 4 agent(s)`，但子 agent 仍 stopped、不重启），root 5 轮只做 `list_coverage`→3×`record_coverage`→`finish_scan`，花 $0.40 什么都没测；③ **发布阻断**：`run.json.status=completed` 但 `coverage.json.completeness.complete=false`（caveats：3 个 agent 没跑完；gaps 6），我们只看 status → 页面说「扫描完整跑完，没有发现漏洞」—— 同条 24 的形状，后端从不读 `coverage.json`。Strix 自己的报告写着 "does not mean the application is secure"。另核：strix.log 在 1.6.2 默认就含 DEBUG 行（未设 `STRIX_DEBUG`），扫过全部 run 无 AWS key 形状串。**用户拍板（2026-09-24）**：① `completed` 但 `coverage.json.completeness.complete != true` → 归「结论不完整」，**且允许续跑**（准入要放宽，任务待调查结论后拆，暂记 T32）；② 先派只读调查：续跑时能否让 root 知道上限已提高、继续测（交底 `~/Documents/claude/dispatch/T32-probe/prompt.md`）；③ $2 下限等②的结论再定。
- **✅ 2026-09-24 T32 只读调查回来（三处源码主会话已抽验，出处准）**：**Q1** `execution.py:391` `if not interactive and status not in {"running","waiting"}: continue` + `agents.py:27` `"stopped"∈TERMINAL_STATUSES` → 非交互续跑下被强停的子 agent **永不重启**（设计如此，只 root 续）。**Q2** root 知道新上限（`runner.py:296-305`、`hooks.py:44-57`），但 system prompt/skill **没有"续跑后继续挖"的指引** → root 回放到"即将收尾"就 `finish_scan`（我们那次 $0.40、5 轮、0 改动）。**唯一杠杆**：续跑时显式传 `--instruction`（`cli_args.py:340`→`cli.py:100 resume_instruction`→`runner.py:565-575` 作 high-priority user message 注入 root，root 可据此**新派**子 agent；旧的仍死）—— 但 T31a 续跑 argv **刻意没传** `--instruction`。**Q3** `coverage.py:348` `complete = not caveats`，`complete==true` 是稳判据（健康扫描不会误判 false）；盲区：它**不看 `gaps`**（正常收尾但有 open gap 时可能 complete=true/gaps>0）。**由此的张力（待用户定 T32 形态）**：光放开 `completed` 续跑、不注入 `--instruction`，续跑只会再花 ≈$0.40 重写一份总结（"骗人的继续"）—— 决策①的意图（补测那 6 个 gap）**达不到**，除非后端在续跑时按 open gaps + 新预算自动拼一条 `--instruction`。**旧 stopped 子 agent 不可复活**（`execution.py:391` 非交互过滤），续跑只有 root 带记忆恢复、注入 instruction 后 root **新派**全新子 agent 补测——不继承旧探测状态，但 root 记得覆盖到哪、不瞎重复。
- **✅ 2026-09-24 T32 形态用户拍板（含续跑机制答疑后）**，四件事，**是模板 3 后端重大改动 + 多不变式 → 待拆多条、方案主会话写、用户确认后再派实现**（下一轮做）：
  1. **结论判定改读 `coverage.json.completeness.complete`**（发布阻断，同条 24）：后端当前**从不读 coverage.json**；`complete != true` → 归「结论不完整」/`scan_incomplete`，不再报「完整跑完没发现漏洞」。`complete==true` 是稳判据（健康扫描不误判），盲区是不看 `gaps`（可留软提示）。
  2. **放开 `completed`-但不完整 续跑准入**（现在只放 stopped/interrupted）。
  3. **续跑自动拼 `--instruction`**：后端按 `coverage.json` 的 open gaps + 新预算拼一条**英文** instruction，经续跑 argv 注入 root（`cli_args.py:340`→`runner.py:565-575`）——**T31a 续跑 argv 刻意没传 `--instruction`，这条要改**（`scan_launcher.py` 续跑分支 + `scan_resume.py`）。没有它续跑对 completed 无意义（"骗人的继续"）。
  4. **发起扫描设最低预算下限**：`max_budget_usd` 低于阈值拦/警告（$2 实测连一个探测都发不出：底座每轮 ≈61k + 子 agent 各自全量写缓存）。**阈值按凭据形状分**（bearer ≈ sigv4 的 3.5 倍，见条 38 实测），具体数实现时定；下限"如 $5"。
- **✅ 2026-09-24 T32 方案用户确认**（N2 接受、照实显示；**下限改软提示**：单一 $4、前端提示不拦——用户可能用免费模型；续跑按 `新总额−已花费` 提示）。**下一步：写 T32a 交底**（判定），b 串 a 后，c 纯前端最后。方案：`~/Documents/claude/dispatch/T32/plan.md`（拆 T32a 判定／T32b 续跑指令＋准入／T32c 预算下限／T32d 真跑）。**✅ 2026-09-24 T32a 交底已写并派发**：`~/Documents/claude/dispatch/T32a/prompt.md`（模板 2，9 个文件，预计 ≈120 行；I1 `read_coverage_complete` 只认字面 `true`、I2 completed+不完整→`stopped`/`coverage_incomplete`）。**交底时改的一件**：`LivePanel.tsx` 可续码表**挪到 T32b**，与后端 `RESUMABLE_ERROR_CODES` 同一个提交改（两边取值必须一致，否则 a、b 之间按钮会显示但准入 409）。**收货 mutation**：M1 删 completed 分支里的 coverage 判断（只红 I2 三格 + 接线那条）、M2 `is True`→`bool(...)`（只红 `"true"`／`1` 两格）、M3 `_collect` 写死 `coverage_complete=True`（只红接线那条）。**✅ 2026-09-24 T32a 收货**：9 文件 +155/−14；`make lint` 绿、`make test` **1293 passed / 0 skipped**；M1 红 4 格（I2 三格＋接线）、M2 红 2 格（`"true"`／`1`）、M3 红 1 格（接线），全对；整读无缺陷、无偏离。子 agent 收尾时撞 API 403（Bedrock 鉴权）没交报告，但产物完整、闸门自测已绿 —— TDD 红输出因此没有，由 mutation 直接测量替代（§八.3②）；预算未量。**✅ 2026-09-24 T32b 交底已写并派发**：`~/Documents/claude/dispatch/T32b/prompt.md`（7 文件，≈130 行；I1 `compose_resume_instruction` 往返＋不叠加＋strip 不变，I2 黄金续跑 argv 带 `--instruction-file`）。**交底时多定的三件**：① Strix 读 `--instruction-file` 时 `.strip()`（`cli_args.py:329`）→ 续跑节不许有尾随空白，否则再续跑剥不干净；② 首次扫描 `_validate` 拒 `extra_instruction` 含 `## Resumed scan`（否则剥旧节会从操作者那段剪，连 `SHARED_TAIL` 安全规则一起剪掉）；③ `LaunchPlan.instruction_path／sha256` 改回非空（续跑也写文件了）。已知：续跑时 Strix 把**整份**指令（含原任务）作 resume_instruction 注入 root，不只是追加那节。**收货 mutation**：M1 不剥旧节（`base = original`，只红 I1(b)）、M2 argv 删 `--instruction-file`（只红黄金＋路由那条）、M3 路由不传 `instruction`（改成 `""` → 只红路由那条与 I1？看实际）、M4 `RESUMABLE_ERROR_CODES` 删 `coverage_incomplete`（只红 `scan_resume` 那格）。写方案时新核出三件：**N1** 续跑带新指令会覆写 `run.json.instruction`（`cli_args.py:428`）→ 续跑指令必须"原文＋追加一节"，否则下次续跑口令明文进 DB；**N2** 子 agent 被强停后 `complete` 永远回不到 true（`coverage.py:334` 只看 agent 状态）→ 结论不完整是粘住的；**N3** completed 的 root 续跑时是否真的会动，未实测，放 T32d。 **✅ 2026-09-24 T32b 收货**：`make lint` 过、`make test` 1303 passed 0 skipped；mutation M1 只红 I1(b)、M2 红黄金＋路由那条、M3 只红路由那条、M4 只红 `scan_resume` 那格；预算全绿（0 压缩、第 4 次调用开写）。**✅ 2026-09-24 T32c 主会话直接做了**（26 行，交底比代码贵）：`wizard.ts` 加 `SUGGESTED_MIN_BUDGET_USD = 4`；发起页在过了 $2 硬下限后 `< 4` 出提示（两句不同时出现）、续跑页 `新总额−已花费 < 4` 出提示，都不拦；`make lint` 绿、`make test` 1303 passed / 0 skipped；api/web 已重建，**人眼看页面待用户**。**下一步：T32d 真跑**（bearer 小预算 juice-shop → `coverage_incomplete` → 续跑 → 验 N3：root 是否新派子 agent 并发出探测）。
- **✅ 2026-09-24 用户拍板接受（残余风险，不修）**：Strix 的 `set_scan_config()`（`report/state.py:623`）把 `user_instructions` 原文写进 `${DATA}/scans/<id>/strix_runs/<run>/run.json` 的 `"instruction"` 字段 —— **测试账号口令明文落在 `${DATA}` 上**；续跑正是靠这个字段读回指令，所以不去擦它。我方不变式收窄为「口令不进 argv／env／DB／日志，我方只写 tmpfs 的 `instruction.txt`」（`scan_launcher.py` 模块 docstring 第三个要点的措辞还没跟着改，**不塞进 T31a**，留给 T30a 写 SECURITY 文档时一起改）。**已加进 T30a 行的 `docs/SECURITY-zh.md` 清单 ③**；与留存清理的关系：删 `scans/<id>/` 时口令随之删除，默认不开清理 = 默认一直留着。`agents.db` 的 LLM 历史大概率也有，未查；磁盘上未实测（那次真扫描没填测试账号）。
- 其余待办：**T17d** `/scans` 列表页（交底还没写）；SigV4 缓存实测（见下 ②，要用户给 IAM access key）。
- **2026-09-24 第一次真扫描**（`436d5fa3…`，quick_triage，上限 $5）：`stopped` + `exit_meaning=no_vulnerabilities_found` → 页面正确显示「结论不完整」、费用尺溢出段正确。镜像 37 条事件形状与前端判据一致（12 条 `exec_command` 带 `args.cmd` + 字符串 `result`；0 截图 —— 浏览器走 `agent-browser` CLI，不是截图工具）。**用户人眼看过整页（2026-09-24）：推理过程、工具调用、终端内容都正常** —— T17 前端半边验收通过。
  由此冒出的三件：① ✅ **已修（2026-09-24）**：当时续跑已砍，`scan_incomplete`／`stopped_by_operator` 的 action 改成"重新发起"，删掉无人引用的 `scan.resume`／`scan.resumeNeedsKey` —— **续跑恢复后由 T31c 把 action 改回、键按需重建**；② ✅ **已核（2026-09-24），估算没估高，是真贵**：`run.json` 23 次请求、输入 1,536,439、**cached 0**、输出 4,117；按 litellm 价目表 `us.` 区域 profile（$3.3/M 入、$16.5/M 出，比 `global.`／裸名贵 10%）复算 = `$5.1382`，与 `llm_usage.cost` 逐位相同。**钱 99% 花在输入上**：Strix 每轮底座 ≈61k token（system prompt + skill），`bearer → invoke/ → STRIX_PROMPT_CACHE=false`（条 22／23，`scan_launcher.py:223`）让每轮全价重发。估算：开缓存同样 23 轮约 $0.8–1.0（≈1/5）。**现成的出路是 `bedrock_sigv4` 凭据形状**（走 converse、缓存不关）—— **✅ 2026-09-24 已实测**（sigv4 + `us.anthropic.claude-sonnet-4-6`，juice-shop quick）：`cached_tokens > 0` 且 `cost > 0`，条 38 成立；`llm_usage.cost` 与按价目表（读 0.1×、写 1.25×）逐请求复算逐位相同。**同样前 23 轮 ≈ bearer 的 29%**（≈1/3.5，不到估算的 1/5）：差在 cache **写入**约占总价一半 —— 子 agent 各有自己的前缀，每个新 agent 首轮全量写一次（35 次请求里 2 次整轮未命中）。数字在 `pitfalls/local-env.md`。次要：`global.` profile 再省 10%（价目表有键、护栏不断；要先确认数据驻留可接受 + 该端点没被解密）。这两条做不做、UI 要不要提示"bearer 形状贵约 5 倍"，**待用户定**；③ 验活把 `endpoint_unreachable`（区域填错时实测）也显示成"凭据被拒"，用户会去查 key 而不是查区域。
  另：本机 `api`／`web` 镜像一度停在 09-19（T17 之前），已 `up -d --build api web` 重建 —— **收货后要看页面就得先重建镜像**。
- **记下不追**：`StampBar` 在 `/scans/[id]` 上用不起来 —— `GET /api/scans/{id}` 不带操作人／授权依据／声明时间／解析地址（只有 `authorization_id`）。要用得给 T16e 响应加一个 `authorization` 子对象，另起一条。

### 上一段（2026-09-23 晚）：T17-0、T17a 已收货提交

- 方案原文与用户四条放行在 `~/Documents/claude/dispatch/T17/plan.md`，要点已进 §派发清单 T17 行。
- T17a 交底 `~/Documents/claude/dispatch/T17a/prompt.md`（WS 客户端 + `applyFrame` + `fetchScan`／`stopScan`）。
  **交底时补的两条语义**：浏览器 `WebSocket` 看不到握手 401 → 没 `open` 过就关时先问 `/api/auth/me`；
  `close(1000)` 而没收到 `done`（刚提交、channel 还没开）→ 拉一次 `GET /api/scans/{id}`，仍 `starting|running` 就退避重连。
- T17a 已收货（见派发清单 T17a 行）。**T17b 交底要贴给它的**：`useScanStream(scanId)`（只接线）+ `useScanLiveStore` 的 `live: LiveState`（`agents: AgentRow[]|null` null=用 REST 快照、`summary.cost_usd`、`done`、`connection` 五态、`notices`）、`fetchScan`／`stopScan`／`AgentRow`／`ScanDetail` 签名、`ErrorNotice` 与 `scanFailureCopy()` 的用法、`budget.*`／`live.*`／`status.*` 键、`--ruler-*` token、`SubmitPanel.tsx:155-186` 成功块原文、`page.tsx:44-49` 那条 404 按钮判据。**结论判据**：「未发现漏洞」要求 `status==="completed"` 且 `exit_meaning==="no_vulnerabilities_found"`。
- 收货后 T17b 交底要带：`wizard.livePending` 键随 SubmitPanel 改动一起删（主会话删 json）。

### 上一段（2026-09-23）：T16a–T16e 全部收货

W2a／W2b／W2c／W3／T16a／T16e／T16b 都已实现 + 收货三件事做完，逐条在 §派发清单 对应行，本节不复述。

**现在的实际状态**：帧的**生产端与镜像端都通了**。每起一次扫描，`_run_to_completion` 的 `try`
第一行 `channels.open`、`finally` 第一行 `await channels.close`（排在 `forget`／`release` 之前，
那是安全约束），lifespan 收残留；`ScanPersist` 每一轮把投影落进 `scan_agents`／`scan_findings`／
`scans` 的计数列。`ChannelRegistry` 的公开面只有 `open`／`get`／`close`／`shutdown`，
**`get(scan_id) -> ScanChannel | None` 就是 WS 路由的入口**（它只有 scan_id）。
`services/event_replay.py::replay_batch(db, scan_id, *, resume_from, ts, limit)` 的生产调用方就是
T16b 的 `routes/stream.py::stream_frames`。
**当前闸门基线：`make lint` 干净、`make test` 1213 passed / 0 skipped。**

**出口开了第一个：`WS /ws/scans/{id}`（T16b，commit `e3cca4d`）。**
`routes/stream.py` 的形状是 **与传输无关的 async generator `stream_frames` + 一层薄传输** ——
**T16c 的 SSE 只换传输层、复用同一个 generator，不许另写一份产帧逻辑**；它抛的两个类型化异常
`ScanNotFound`／`SubscriberLagged` 就是给"每种传输自己翻成各自的错误形状"用的（WS 那边翻成
`error{not_found}`+1000 与 `error{stream_lagged}`+1011）。三条别再重新发明：**订阅必须先于回放
查询**；**去重只对 `event.add`／`event.update` 生效**；**去重游标是"这条连接的回放实际交出去了
什么"，绝不是客户端自报的 `resume_from`**（三条的理由都写在那个文件的 docstring 里）。
`ws_router` 是第二个、**无 prefix** 的 router（挂到 `prefix="/api/scans"` 上会静默变成
`/api/scans/ws/scans/{id}`，而 nginx 的 upgrade 指令在 `location /ws/` 下）—— T16c 的 SSE 路径
在 `/api/` 下，那条**不能**挂这个 router。

**出口开了第二个：`GET /api/scans/{id}/stream`（T16c，SSE 兜底）**，翻译规则与交给 T17 的前端契约在 §派发清单 T16c 行。
**T16d（media 路由 + `artifacts_purged`）也已收货**，T16 全部四条出口到齐；下一棒按 §派发清单 顺序（T17 前端实时面板，吃上面三笔债）。
**T17 的三笔债**（细节在 §派发清单 T16b 行）：① 4 个 WS 帧码（`screenshot_elided`、
`context_compacted`、`stream_resynced`、`stream_lagged`）还没有文案树与双向覆盖测试，T17 建第五棵；
**帧类型新增了一个 `error`，它不在 `services/scan_frames.py` 的 8 种里**，任何"帧类型全清单"的
地方要带上它；② T17 存 `resume_from` 必须存**见过的最大** `(epoch,seq)`，不是最后一帧的 seq；
③ `_db` 已经第三份，提取归属地 `routes/_context.py`，留给下一条本来就要碰那几个文件的任务。快照那一半已经有了（T16e）：形状是 **REST 快照 + WS 只管增量** ——
事件能从镜像回放，但 agents 树／summary／发现列表不在镜像里，而 channel 只在指纹变了才推，
半途连上来的客户端只能从 `GET /api/scans/{id}` 拿"现在是什么样"，WS 只负责之后的增量。
（另一条路"新订阅者到达就清 `FrameState` 重推一轮"已否决：会把上百 KB 的报告扇给所有在线订阅者。）

**T16e 的响应形状是已落地的契约**（T16b／T17 按它写，别再另发明一套）：
① 列表 `{scans:[19 键], truncated}`，`ORDER BY created_at DESC, id DESC`，**不做分页参数**、
`_MAX_SCANS=500` + `truncated`（判据照抄 `routes/audit.py:44-46`）；② 详情
`{scan:{19 键+error_message,max_turns,reasoning_effort,provider,strix_llm,current_epoch,
authorization_id}, agents:[7 键], findings:[raw_json 原样]}`，未知 id → 404 `not_found`；
③ **agents 行与 `agents` 帧逐字段同形状**（对照 `scan_frames._agent_row`）、**findings 就是当初
`vuln.add` 的 `payload["vulnerability"]`**（`ORDER BY first_seen_at, finding_id` = 当初推的顺序）
—— 快照与直播只许有一套解析；**agents 顺序刻意不承诺与帧一致**（库里没存上游顺序，前端按
`parent_id` 建树）；④ 15 个列**禁止出现在任何响应里**：`vault_handle` `argv_json`
`env_var_names_json` `instruction_sha256` `cwd` `run_dir` `strix_run_name` `pid`（收窄泄漏面）
+ `api_base` `auth_shape` `phase` `scope_mode` `resume_available` `strix_version` `sandbox_image`
（没有消费者）；⑤ 详情的三条 SELECT 在**同一个 `db.run`** 里（分三个事务会让客户端拿到
"计数是新的、发现是旧的"自相矛盾快照）。

**2026-09-22 另两条用户拍板**（都已改进正文，这里只留指针）：**`phase` 帧删除**
（§实时流设计 的类型清单 + §端到端验收 第 7 条都已改，理由：一直没有生产者，而"到哪一步了"
已有 `/ws/system` 拉取进度与 `agents` 帧两个真出处）；**stat 门保持四文件、不加 `strix.log`**
（代价是"卡在某一步只刷日志"时日志帧要等四文件里任一个再动一次才随下一轮发出，最长 ≈2s 退避 +
下一次文件变动 —— 把一直在长的日志纳入门会让门几乎永不生效、退避形同废止。**这个滞后是已知且接受的**，
T16b／T17 做日志面板时不要再当 bug 查）。

**交底形状（W3 与 T16a 连着两次验证有效，继续用）**：任务书 + **附录内联进同一份文件**、
附录只摘点名用到的那几段，落盘在仓库外的持久目录 `~/Documents/claude/dispatch/<任务>/`
（`/tmp` 活不过一次清理）。T16a 那份 330 行、子 agent 28 次调用 0 压缩，是目前最好的一次。
方法学教训在 §派发方法学 10。**收货的判据 ④（"靶子是字面值吗"）已连抓五次**：
每条任务都要专门看一眼"有没有哪个常量／字面量没人盯着"。

⚠️ **一件不阻塞但别丢的**：`CLAUDE.md` 里"单测用 `fixtures/run_dirs/` 夹具"是**错的**
（那个目录不存在，真家伙是 `conftest.make_run_dir`），改它要**单独一个 commit**，不混进业务改动。

⚠️ **教训（2026-09-18 那份 W2 方案没落盘就丢了）：模板 3 的方案原文与交底任务书都必须当场落盘**
—— `/tmp` 活不过一次清理，所以 W2c 起改用仓库外的持久目录 `~/Documents/claude/dispatch/<任务>/`。

### W2 方案（2026-09-20 用户放行，四点全按推荐；**这一节就是权威**）

**目标**：`ScanChannel` —— 一个扫描一个轮询器、N 个 WS 订阅者，把 run 目录的变化变成 §实时流设计
那套帧，并同步写进 `EventMirror`（T16 回放的真源）。

**四条已拍板的语义**（前三条是 2026-09-18 记下的待放行点，第四条是重写时发现原记录没覆盖的范围问题）：

1. **删掉 `events.snapshot` 帧类型。** 重同步 = `_epoch += 1` 后在新 epoch 下**逐条 `event.add`**，
   一帧一个 seq、与镜像行 **1:1 同 seq** → 回放与直播是同一个帧形状（T16 直接从 `scan_events` 重放，
   前端只有一套解析）。**连带接受的代价**：非事件帧（`summary`／`log`／`notice`／`agents`…）也从同一个
   计数器取 seq，所以**从镜像回放会有 seq 空洞** → `seq` 只用于排序与 `resume_from` 游标，
   **前端不许把空洞当丢帧**；"作废本地状态"全靠 `epoch`（`ws_envelope.py:3-8` 的规则不变）。
2. **`done` 帧不带结论。** 状态／归因／漏洞数只从 `GET /api/scans/{id}`（T16/T17）取 —— 结论的唯一
   出处是 `_run_to_completion` 写的那一行 `scans`，归因活在 `ScanOutcome` 里；channel 自己再推一次
   等于造第二个可能互相矛盾的判决，而 CLAUDE.md 禁止只凭退出码报"未发现漏洞"。
   在那个端点上线前，`done` 就是一个纯信号。
3. **背压与失败：摘订阅者、杀 channel。** 订阅者队列满 → 关掉那条 WS（客户端带 `resume_from` 重连），
   **绝不阻塞轮询循环**；`EventMirror.append` 抛异常 → **让它冒泡杀掉 channel 任务**，不静默丢帧
   （只追加的真源出洞比断流更糟；扫描本身与 `scans` 终态不受影响，由 `_run_to_completion` 兜）。
   这与 W1 交回的第 1 件（`run_forever` 只接 `OSError`）是同一类决定，**这里刻意不开"记日志继续"的口子**。
4. **`scan_agents`／`scan_findings`／`scans` 的计数与 `cost_usd`／`current_epoch` 落库不算在 W2 里** ——
   单独一条 **W3**（已进派发清单）。理由：W2b 已经有 stat 门 + 投影 + 镜像 + 扇出 + 收尾，再压四张表必然
   超 500 行也超 1 条不变式。W2 交完的垂直切片是完整的（帧 → 镜像；实时 cost 走 `summary` 帧）。
   **W3 必须现在就在清单里**：验收第 1702 行要求"`severity_counts` 与 `scan_findings` 行数一致"，
   而在此之前**没有任何一行任务拥有这几张表的写入** —— 和 T7c 那次漏派同一个形状。

**拆三条串行**（每条一个交付物 + 一条能独立变红的不变式，`agent-rules.md` §三.1）：

- **W2a** `services/scan_frames.py`（纯函数，无 IO）：
  `plan_frames(previous, projection, snapshot, log_lines) -> (tuple[FrameSpec, ...], 新状态)`
  + `envelope_for(spec, *, epoch, seq, ts) -> Envelope`。`FrameSpec` 带 `type` + `payload`
  + 可选的 `event: ProjectedEvent`（只有它要进镜像）。**seq 不在这里发**（发号要和 `await append`
  交错，见 W2b）。事件帧的 payload 形状 = **镜像行的纯函数**（`{key, kind, agent_id, ts,
  upstream_version, data}`，与 `EventMirror` 的三键信封同源）—— T16 回放要靠这一条。
  非事件维度（`agents`／`vuln.add`／`summary`／`report`）自带"上次推的是什么"的签名，内容没变不重推。
  **不变式：帧与镜像行 1:1 同 seq、epoch 变则 seq 归零、同一内容不重推。** 估 ~200 + ~300 测试。
- **W2b** `services/scan_channel.py`：stat 门（四个文件的 `(mtime,size)` 全没变就整轮跳过）→
  `read_run_dir`（同步 IO，走 `asyncio.to_thread`）→ `project()` → 按 spec 顺序
  `Sequencer.next(epoch)` → 需要镜像的先 `await mirror.append(...)` **再用返回的 `MirroredEvent.event`
  建帧**（它的 `data` 已把内联 PNG 换成 media URL，顺序反了 WS 帧里就会带 base64、且与回放不一致）→
  扇出给订阅者。自适应轮询 250ms、空闲 ×2 退避到 2s 上限。
  **不变式：慢订阅者被摘掉且不拖住循环；镜像写失败不被吞。** 估 ~250 + ~300 测试。
- **W2c** `ChannelRegistry` + `routes/scans.py` 起停 + `main.py` lifespan 关停。
  **不变式：扫描终态或停机后 channel 一定被关掉、不泄漏任务。** 估 ~150 + ~200 测试。

**「谁判定跑完了」按单一权威定**：**channel 自己不看进程**。W2c 在 `process.wait()` 返回后调
`await channel.finish()`（最后一次 tick → `done` → 停循环），不让两处各判一次"跑完了没"。

**W2 不含 WS 路由**（`/ws/scans/{id}` 是 T16 `routes/stream.py` 的地盘）：W2 只产帧 + 落镜像。
订阅接口照 `/ws/system` 的先例（`routes/system.py:148-192`）设计，但 `ImagePuller` 那套
「一个 `current_frame` + `wait_for_change(seen)`」**不能照抄** —— 进度是幂等快照，事件是不可重建的序列，
所以这里必须是**每订阅者一个 `asyncio.Queue`**。

**接线约束（来自下面"交回的五件"，交底任务书里必须原样带上）**：`EventMirror(db, settings.scans_dir,
scan_id)` 第二参必须是 `settings.scans_dir` **本身**（别名目录会静默写错 `rel_path`）；`LogTailer`
偏移活在实例里 → **一次扫描一个长命实例**，`min_level` 默认 `INFO`、**不与 `LOG_LEVEL` 共用**；
`strix.log` 在 run 目录里 → tailer 只能等 `RunDiscovery` 抢到 run 目录之后再建，不是扫描 cwd。
stat 门的四个文件名走 `StrixProfile`（`run_record_name` 已有，缺的三个在 W2b 补进去），
**不在循环里硬编码** —— 升级时要能一处改完。

**W2a 的交底形状已验证有效，W2b／W2c 照抄**（任务书 `/tmp/W2a-prompt.md` 372 行：附录贴全
`ProjectedEvent`／`ProjectionResult`／`Envelope`／`Sequencer`／`MirroredEvent`／`LogLine` 的原文
+ 六个签名 + 帧顺序 + 两个判据 → 子 agent **一个别的源码文件都没读、8 次调用、0 次压缩**）。
**快闸门实测 1.45s**（`docker run --rm -v app:ro -v tests:ro strix-console/api-test:0.1.0 pytest
tests/test_scan_frames.py -q`）→ §十.5 第一层的 TDD 前提成立；**W2b 是 async + 假时钟，快闸门要重测**。

**W2b 已派发（2026-09-20）。交底产物（假定随时会没，`/tmp` 活不过一次清理）**：任务书
`/tmp/W2b-prompt.md`（298 行）+ 附录 `/tmp/W2b-appendix-{1,2,3,4}.md`（1778 行，逐字原文
A1–A14：`scan_frames` `ws_envelope` `run_projector` `read_run_dir` `EventMirror` `LogTailer`
`run_discovery` `StrixProfile` `image_puller`＋`routes/system.py` `Settings` `conftest` 全部夹具
async 测试范本 pytest／ruff 配置）。**附录改成拆 4 个文件是被迫的**：一个摘录 agent 攒到最后一次性
`Write`，在那一次响应里被 API `server_error` 打断 → 15 分钟、120k token、**0 字节落盘**。
教训：**派摘录活必须要求分批落盘**（写完一节确认一节），别让产物只活在它的上下文里。
**快闸门已重测**：只读挂载 + 已建好的 `api-test:0.1.0`，`pytest tests/test_scan_frames.py
tests/test_image_puller.py -q -p no:warnings` = **1.47s / 42 passed**（含 `image_puller` 那种
后台循环用例）→ async 形状照样在秒级，按文件过滤的 `ruff check`／`format --check` 也已验证可用。
**摘录带回的两条事实（W2b 任务书已按它们订正）**：① **本仓刻意没装 `pytest-asyncio`**，async 场景
在同步测试函数里 `asyncio.run(...)` 跑（`test_image_puller.py:15` 有理由），`conftest` 已有 `FakeClock`；
② ⚠️ **`backend/tests/fixtures/run_dirs/` 这个目录不存在** —— 现成路子是 `conftest` 的
`make_run_dir`／`make_agents_json`／`make_agents_db`。**`CLAUDE.md` §测试与日志 那句"用
`fixtures/run_dirs/` 夹具"是错的，待改成 `conftest.make_run_dir`**（单独一次，不混进 W2b 的 commit）。
③ `StrixProfile` 现只有 `runs_dir_name`／`run_record_name`，stat 门要的另外三个（`.state/agents.json`
`.state/agents.db` `vulnerabilities.json`）确实都还是隐式约定，`agents.db` 只在测试辅助函数里以字面量出现。

**派发预算（§九.6）**：调用 ≤ 40 次、峰值 < 120k、**0 次压缩**、首次写代码不晚于第 10 次调用。

**收货判据（主会话自己做，一条自陈都不采信）**：① 官方闸门 `make lint` + `make test`，
基线 **1106 passed / 0 skipped**；② mutation 数 = 该行的不变式条数，**且至少三处靶子自己想** ——
W2a 实测：照清单的 3 处全部被现有测试抓住，**抓到本轮唯一真缺陷的那一处是自选的**；
③ 产物文件**整读**；④ 先问那一句：**"这条不变式的靶子是一个字面值吗？如果是，有测试盯着那个
字面值本身吗？"**（帧 `type` 字符串有测试盯着，`PROTOCOL_VERSION` 当时没有 → 已补）。

### 上一轮（2026-09-18 第三段）：**W1（留存清理接线）已收货**

**收货三件事全做完**（§八.3，一条自陈都没采信）：① 官方闸门 `make lint` 全绿、
`make test` **1079 passed / 0 skipped**（基线 1072，+7）、`docker compose -p strix-console config -q`
通过；② **5 处 mutation 全部我自选、自己重跑**（不采信子 agent 报的那 7 条）；③ 六个产物文件整读。
逐条结论在 §派发清单 T28 行，本节不复述。

**⚠️ MX1 抓到本轮唯一的真缺陷**：把 `console_retention_days` 的默认值从 `0` 改成 `30`，
**全量 1078 条测试无一变红** —— "破坏性操作默认必须关"当时没有任何测试守着（子 agent 的
F6 钉住的是"这个值来自 `Settings`"，钉不住**那个值是几**）。已补
`test_settings.py::test_retention_is_off_by_default`，重跑 MX1 → 只红它一条。
**这是 T10 的 `assert_sandbox_env`、T7b 的 `params`、T9 的 `_PINNED_ENV` 之后同一形状的第四次。**
下一棒收货先问这一句：**"这条不变式的靶子是一个字面值吗？如果是，有测试盯着那个字面值本身吗？"**
（"参数从配置里来"与"配置的默认值是什么"是两条不变式，一条测试钉不住两条。）

**另外四处 mutation**（都只红该红的那一条）：MX2 `create_task(run_forever())` 换成一个立刻结束的
空任务 → 红 F6；MX3 审计写入短路 → 红 F4；MX4 往 `detail` 里塞一个宿主路径 → 红 F4
（证明"审计正文里没有路径"真的有承重）；MX5 `cancel+await` 挪到 `db.close()` 之后 → 红 F6。

**当前闸门基线：`make lint` 干净、`make test` 1086 passed / 0 skipped**（1079 + `test_llm_client.py` 的 7 条）。

### 交回、必须带进 W2（`ScanChannel`）的五件

1. **`run_forever` 只接 `OSError`** → `_delete_detail_rows`／`audit.record` 抛 `sqlite3.Error` 时
   任务仍会带异常死掉，并在停机 `await` 时把异常重抛进 lifespan 的 `finally`（reaper 那处实测过：
   会连带弄红上百个无关测试）。**收货时刻意没有顺手扩成 `(OSError, sqlite3.Error)`** ——
   它和下面那条 `failed` 字段是同一个决定的两半（"一个 scan 失败要不要把整轮跑完"），
   要做就一起做、单独一条任务，不许混进接线的 commit。
   同理 **`RetentionOutcome` 契约里没有 `failed` 字段**，现在的语义是"这一轮到此为止，下一轮再来"。
2. **`InvalidScanIdError` 刻意不继承 `ConsoleError`**（`retention.py:53`）—— `test_message_coverage.py`
   的 `_backend_http_codes()` 取 `ConsoleError.__subclasses__()` 并要求每个码在 `zh-CN.json` 里有
   中文文案。**谁要把它暴露成 HTTP 错误，必须同时补文案。**
3. **`conftest.insert_scan` 不接受 `status`／`finished_at`**（只写 `queued`），而留存判定读的正是这
   两列 → `test_retention.py` 有本地 `seed_scan()`。**第 2 个要用它的任务出现时才提取**（§十.3）。
4. ⚠️ **`EventMirror(db, scans_dir, scan_id)` 的第二个参数必须传 `settings.scans_dir` 本身**：
   `rel_path` 列的值是 `f"{scans_dir.name}/{scan_id}/media/<sha>.png"`（不能用 `relative_to`，
   理由见 §派发清单 T14 行），传一个别名目录会**静默**写出错的 `rel_path`。**结构上防不住，只能靠这条。**
5. **`EventMirror.append(epoch=, seq=, event=)` 的 `seq` 由调用方给** —— 必须与那一帧 WS 信封
   （`ws_envelope.Sequencer.next(epoch)`）**用同一个 seq**，否则 `resume_from {epoch, seq}` 回放对不上。
   `EventMirror` 不发号、不持有 epoch，也**没有** `replay()`：回放的读取端还没人写。

### 本批之后的顺序（别乱序）

原"一条串行接线任务"**已按 §三.2 拆开**（它一条同时压上 `main.py`＋`settings.py`＋`ScanChannel`，
超 500 行也超 1 条不变式）：**W1 = 留存那一半，已收货**（见上）。剩下的是 W2。

1. **W2（`ScanChannel`）：方案 2026-09-20 已放行，全文在 §W2 方案**（帧形状／`done`／背压三条已落进
   §实时流设计，那一节现在与它一致）。**W2a → W2b → W2c 串行**，之后接 **W3**（四张表落库）。
   ⚠️ 要接的都只吃构造参数：`LogTailer(path, redact, min_level=)`、`EventMirror(db, scans_dir, scan_id)`
   （`extract_media` 已被它包在里面，接线层**不要再直接调**）。
2. **前端"产物已按留存策略清理"的 404 文案：随 T16 做，不在 W1／W2 范围**（2026-09-18 决定）。
   理由：现在没有任何一条路由会因为产物被清理而 404（媒体／报告读取路由属 T16／T21）。
   先声明一个没有生产者的机器码＋文案，正是本仓已经烧过四次的"声明了却没有任何一处强制"。
3. **T29** 单独跑（契约断言要读整个工作区，有半成品就没意义）
4. **前端这一串可以插在任何位置**（与 W2／T29 无文件重叠），2026-09-19 拍板的顺序是
   **T7c（凭据表单）→ T18a（向导骨架＋目标步＋模板占位＋预算步）→ T18b（授权步＋操作人＋提交）**。
   T7c 排最前的理由：`CreateScanRequest.vault_handle` 必填，而**全仓没有任何取 handle 的 UI**
   （首页「现在提供凭据」至今禁用），没它向导第 5 步永远发不出去。三条同目录／同 store，**必须串行**。
   **T7c 已实现完（2026-09-19，交底任务书 `/tmp/T7c-prompt.md` 427 行）：官方闸门 `make lint`＋`make test`
   全绿（1079 passed / 0 skipped），纯前端 mutation 0 处，六个新文件已整读。`--workers 1` 的内存 vault
   到这一步才第一次有前端生产者。**`web`／`api` 已重建并起来，两家供应商都已在真界面上验活通过
   （2026-09-19）**；`web` 与 `api` 都是 `target: runtime`、**没挂源码**，改完必须
   `docker compose -p strix-console build web api` 再 `up -d web api`，否则跑的还是旧镜像
   （重启 `api` 会清空内存 vault → 已登记的凭据要重填一次，这是预期行为不是 bug）。
   收货读代码改掉的三处（子 agent 自陈全绿，这三处闸门抓不到）：① `/credentials` 页壳原先无条件渲染
   `credentials.none`（"**还没有提供凭据**…"）并把 `Panel` 标题设成"现在提供凭据" —— 对已登记的人是
   **屏幕上的假话**，现在那句话只在未登记分支里，页壳只剩标题；② `resetInputs()` 不清 `failure`，换供应商后
   上一家的 `ErrorNotice` 还挂着；③ 提交按钮文案改用新键 `credentials.register`="登记并验活"（按下去会真的
   拿 Key 发一次请求，动作名要说出这件事；"现在提供凭据"留给首页那个链接）。文案键共新增 3 个，都在
   `credentials.*` 下（自由文本子树，`test_message_coverage` 不管）。
   拍板的六件事：① `/credentials` **独立路由页**（服务端外壳 + 客户端叶子），侧栏面板只留状态 + 一个
   文字链接（`Button` 是 `<button>` 没有 `href`，不为它造第二份按钮样式）；② 模型名是**自由文本框**，
   `models` 只作为"可以抄的值"（`providers.modelsEmpty` 那句话已经定了这件事）；③ `verify` 恒 `true`
   不给开关；④ 「立即忘记凭据」顺序写死 **先 `DELETE /api/keys/{h}` 再 `forget()`**，后端 404 也算成功；
   ⑤ 刷新用 `GET /api/keys/{handle}` 恢复显示，**404 就清本地 handle**（后端重启过，内存 vault 空了）；
   ⑥ **密文绝不进 react-query 缓存** → 提交走手写 `async`+`useState`（照 `LoginForm`），只有两个 GET 用
   `useQuery`。**顺手发现并交给它修的一个真 bug**：`apiFetch` 结尾无条件 `await response.json()`，
   碰上 `DELETE /api/keys/{h}` 的 **204 空正文会抛 `SyntaxError`** —— 它是全前端第一个 204 消费者。
   **人眼验收当场撞到两次真实验活失败，两条都已归因（2026-09-19）：**
   · `deepseek`／`single`，11 毫秒 → **模型名少了 litellm 的供应商前缀**（`LLM Provider NOT provided`，
     请求根本没发出去）。根因是**我们自己的提示文案**没说要带前缀 → 已改写 `providers.modelHint`
     与 `providers.modelsEmpty`（明写 `deepseek/deepseek-chat`、`openai/gpt-4o`）；用户补上前缀后
     **这一家已验活通过**。
   · `bedrock`／`bedrock_bearer`，30 毫秒 → **归因是首尾空白（最可能在区域框），已由修复验证、但不是
     直接证据**：重建镜像（`.trim()` 在场）之后同一个人再填一次就通过了。剩下的不确定只有一件 ——
     没法排除他同时也重打了内容。已排除的三件：TLS（容器里看签发者是**真 Amazon**，没被解密）、
     `invoke/` 路由（后端补的，日志里 `model=invoke/us.anthropic.claude-opus-5`）、`api_key` kwarg
     被忽略（假 token 换回 AWS 的**真 HTTP 响应**、922 毫秒）。旁证：区域多一个空格 → litellm 本地
     9 毫秒就拒（与 30 毫秒同量级，请求都没发出去）。
     **排障手法值得留着**：在 `api` 容器里用**假 token** 做对照矩阵，真凭据一次都不必出现。
     **但下次不必再做了** —— T18a 收货时已 `build api` 把 `classify_failure` 装进镜像（原先那次重建
     发生在写它**之前**，这行一度写错），`failure_kind` 现在是活的：
     `docker compose -p strix-console logs api | grep failure_kind` 直接给码。
   **由此加的后端小改（llm_client.py，已完成）**：`classify_failure()` —— 5 条白名单子串 → 归因码，
   不命中回 `unclassified:<异常类名>`；只多一个日志字段 `failure_kind`，**`VerifyOutcome` 契约不变**
   （仍只有 `ok`/`latency_ms`）。不变式是「出口 = 有限集合 ∪ 静态类名，`str(exc)` 只读不转发」，
   新 `backend/tests/test_llm_client.py` 7 条用例 + 1 处 mutation（改兜底为回正文 → 只有该红的 2 条红）。
   **没做**：把这个码作为 `params.hint` 回给前端让文案直接说"区域格式不对" —— 等用户单独拍板。
   **T18a 已派发（2026-09-19，模板 3 的实现阶段，交底任务书 `/tmp/T18a-prompt.md`）。方案由主会话写、
   用户「按推荐」逐条批准四件事**：① 首页「开始填写工单」**这一轮不开**，留给 T18b（验收靠直接敲
   `https://127.0.0.1/scans/new`）→ `src/app/(app)/page.tsx` 因此**从文件列里去掉了**；② 预算下限 **$2**，
   低于它就地提示且禁用「下一步」；③ 多目标 = **一个 textarea，每行一个**；④ **换模板重置**预算与轮数为
   新模板默认值（不加"用户改过没有"的标记位）。其余方案要点：步进条按首页 `DOCKET_FIELDS` 顺序写死，
   第 2／3 步是"随下一步上线"的占位且**不拦前进**；步号在 store **不进 URL**；`stores/wizard.ts` **纯内存**
   （不 persist、不碰 sessionStorage），**只声明第 1／4／5 步用到的字段**；第 1 步**手动按钮触发校验、
   不做防抖自动校验**（服务端每次都做真实 `getaddrinfo`）；`blocked_metadata`／`split_horizon` **不给覆盖入口**；
   `recommended` 只作文字标记（不造新语义色）；**UI 不宣称任何预算上限数字**（`CONSOLE_MAX_BUDGET_CEILING_USD`
   没有任何路由暴露，超限由 T18b 的 `budget_exceeds_ceiling` 告知）；`client.ts` 只加 `validateTargets()`
   ＋`fetchScanTemplates()`，**不加 `createScan`**。
   **文案由主会话先写完了（32 键：`wizard.*` 26 + `templates.*` 6，`zh-CN.json` 当前未提交）** ——
   这是刻意把"文案"这一维从子 agent 的文件列里拿掉（§九.5）。
   **T18a 已收货（2026-09-19）**：官方闸门 `make lint` 干净、`make test` **1086 passed / 0 skipped**（纯前端，
   不变）；mutation 0 处（前端无测试）；**十个产物文件整读**（含四个 CSS module）；子 agent 26 次调用、
   无压缩，预算达标。它自陈的 13 条偏离逐条看过，**四件拍板全部落对**。
   **读代码改掉的四处**（闸门与它的自陈都抓不到）：
   ① **第 5 步的空输入框被说成"太低了"** —— 一进这一步（还没选模板时预算/轮数都是空串）就亮两句
   "费用上限太低了…钱花了，结论没有"，指着一个空框。判定函数保持把空串算作不合格（T18b 的提交闸要），
   但**屏幕上只在非空时才说"太低"**。这是"屏幕上的假话"同形状的第二次（T7c 是 `credentials.none`）。
   ② **`requirement` 没有任何消费者** → `targetGuard.requirements.*` 五个键悬空，而
   `routes/targets.py:176-182` 加这个字段就是为了这里。已渲染，并**替掉重复的 `wizard.optInMissing`**
   （两句话意思一样；那个键已从 JSON 删掉）。关键收益是 `allowlist_file_only`：那种目标的 `code` 是通用的
   `not_in_allowlist`，只有 `requirement` 说得出"界面上没有这个开关，只能改清单文件"。
   ③ **硬编码等宽字体栈三处** → `var(--mono)`。**这是我交底的漏**：任务书 §5.3 的 token 清单漏了
   `--font`/`--mono`，它按"不许造新 token"的规矩只能写字面量 —— 下次抄 token 表照根 `CLAUDE.md` 全抄。
   ④ `wizard.templateDefaults` "默认上限" + 数字**没有货币单位** → 改成"默认上限（美元）"。
   **后端契约已由主会话逐字核对**（子 agent 按规矩没读 `backend/`）：`ValidateTargetsRequest`／
   `TargetValidation`／`NormalizedTargetView`／`ResolvedAddressView`／信封字段**一字不差**，路由确实是
   `POST /api/targets/validate` 与 `GET /api/scan-templates`。另**核了一条不变式**：`ok=false` 不可能三个
   解释字段全空 —— `_error_code_for()` 只对 `loopback`/`private` 回 `None`，而那两种必有 `required_opt_in`，
   所以"警告灯亮着却不说为什么"在结构上不会出现。
   **子 agent 提出、我拍板的一处**：第 5 步是末步、没有"下一步"按钮，所以"低于下限禁用下一步"无处可禁；
   闸以纯函数 `isBudgetTooLow()`／`isTurnsTooLow()`（在 `stores/wizard.ts`，含空串与 NaN）留给 T18b 的提交
   按钮消费 —— **不写一条永远走不到的 `disabled`**。
   **T18b 必须接着做的三件**：① 提交前把 `budgetUsd`／`maxTurns` 从字符串 `Number()` 成数字（store 存的是
   输入框原文，因为 `Number("")` 是 0 会把"没填"显示成"填了 0"）；② 提交按钮 `disabled` 消费上面那两个纯
   函数；③ 校验结果只在 `StepTargets` 的本地 state 里（离开这一步就消失，结构上防"说着另一批目标的结论"），
   **T18b 需要 `targets[0].normalized.host` 做逐字确认串 → 要么在同一步里拿，要么显式把那一个值提进 store**。
   **人眼验收（用户，2026-09-19）**：第 4 步选模板后第 5 步默认值**跟着变，✅**；另外**抓到两处，都已修**
   （闸门抓不到，`web` 已重建）：
   ⑤ **`requirement` 在目标已放行时还在说"需要你勾选…"** —— 两个框都勾上、`ok=true` 了，那句祈使句还挂着。
   **根因是我上面第 ② 处的修复本身**：`requirement` 与 `ok` 正交（`routes/targets.py:177-178`：已满足条件的
   目标 `ok=true` 而 `requirement` 仍非空，说的是"它**为什么**能过"），而 `targetGuard.requirements.*` 五句
   全是"你需要做 X"的祈使句。现在 **`ok` 为真时一个字都不说**。教训：**字段正交 ≠ 文案正交** ——
   把一个后端字段接上一句现成文案之前，先问"这句话在这个字段的每一种取值组合下都是真的吗"。
   ⑥ **预算框填不进 1 → 下限提示永远读不到**：`min={2}` 在控件层就把值夹住了（箭头到不了、输入判
   `:invalid`），而我们那句"费用上限太低了…"才是要让人读到的东西。**两个输入框的 `min` 都已去掉**，
   下限只在 `isBudgetTooLow()`／`isTurnsTooLow()` 里。判据：**控件属性静默拦住的东西，用户学不到为什么。**
   ⑦ **⑤ 那一刀切得过头了**（同一轮人眼继续看出来的）：公网目标 `12.0.0.1` 是**绿点 +「没有命中授权清单里的
   任何条目」，没有任何一句说明"那为什么还能扫"** —— 唯一说得出口的那句正是 `requirements.none`
   （"公网目标，当前是提示模式，可以直接扫。"），它是五句里唯一的**陈述句**，被 ⑤ 一起压掉了。
   规则改成 `tellRequirement = requirement !== null && (!ok || requirement === "none")`：
   `ok=false` 必说；`ok=true` 且命中清单条目 → 不说（"命中授权清单"那一行已经说了它为什么能过）；
   `ok=true` 且 `none` → 必说。**比字面量 `"none"` 是刻意的** —— "哪一句是陈述句"只存在于文案里，
   没有别的地方读得出来。**这一处的教训比 ⑤ 本身重要：一次"统一压掉"的修复要把枚举的每一个取值都过一遍，
   否则就是用一条新的沉默换掉一句假话。**
   ⑧ **占位那一屏（第 2 步）说的"随下一步上线"撞上了按钮「下一步」**：同一屏底下就有一个深色的
   「下一步」按钮，于是"授权声明…随下一步上线"读起来像"按那个按钮就出来了"。改成"还没做，会在
   后面一批改动里上线"。判据：**文案里不许出现与同屏控件同名的词** —— 这一句还同时出现在第 5 步底部，
   两处都是同一个 `wizard.stepPendingDetail`。同一轮顺手补掉 ⑥ 的另一半：那句"费用上限太低了"
   **没说下限是多少**（`turnsTooLow` 反而说了"至少填 1"），填 1 的人读完仍然不知道该填什么 →
   改成"费用上限至少填 2 美元…"，并在 `MIN_BUDGET_USD` 上注明"改这个数要同时改那句文案"
   （`t()` 刻意没有占位符）。判据：**一句"不合格"必须带着"合格是多少"，否则它只是拒绝，不是提示。**
   ⑨ **「能不能扫」这一位原先只由一个色块表达**（人眼看 `dns_not_found` 那一屏时发现）：不存在的域名
   那张卡上只有一个橙色方块 + 「这个名字解析不出来，检查有没有打错。」，**没有一句话说"所以它现在
   过不了"**。判它错的不是审美，是 `StatusDot` 自己的 docstring —— 它 `aria-hidden`，并写明"这个点
   旁边一定有一句成句的中文说明它是什么状态，出现只有点、没有文字的用法就是那处用法错了"，而这里
   点旁边是**目标原文**，不是状态。修法：`.head` 里补一句 `wizard.verdictOk`／`verdictBlocked`
   （"现在就能扫"／"现在还不能扫"，对齐 `ok` 字段自己的 docstring「现在就能扫吗」），不给语义色 ——
   颜色已经在点上，这一句要的是黑白打印下也读得到。判据：**组件自己声明的前置条件，接它的人要当契约核一遍**
   （与 ⑤⑦ 同源：字段／组件的约定都写在它们自己的 docstring 里，跳过它就等于自己发明一套）。
   第 1 步已过人眼的形态：缺勾选、公网提示、**解析失败**（`dns_not_found`）。**规范化被拒**
   （`RejectionReason`，例如 `user:pass@host`）那一支还没人看过 —— 它走的是 `reason` 而不是
   `resolution_error`，同一处渲染、不同字段。
   首页入口按拍板仍禁用（只能敲 URL）。

### 长期有效的操作事实（每次开工都用得上）

**Docker Desktop 的"文稿"权限故障**（2026-09-17 由人解决；macOS TCC 丢授权 → 仓库路径挂不进容器
→ 全部官方闸门瘫掉）：归因与复现判据在 `pitfalls/local-env.md`（未跟踪），**再遇到同样报错先读那一节**。

快闸门命令（`agent-rules.md` §九.4 要的秒级闸门，**已实测，别改**；给子 agent 时**必须按文件过滤**，
末尾改成 `pytest tests/<本任务的那个文件>`）：

```
docker run --rm -v "$PWD/backend/app:/app/app:ro" -v "$PWD/backend/tests:/app/tests:ro" \
  -v "$PWD/frontend/messages:/messages:ro" -e CONSOLE_MESSAGES_JSON=/messages/zh-CN.json \
  strix-console/api-test:0.1.0 pytest
```

**给子 agent 的 ruff 快闸门必须带 `--no-cache`**（2026-09-18 三个 agent 各自撞到同一处）：
源码是 `:ro` 挂进去的，ruff 想在 `/app/.ruff_cache` 建缓存 → `Read-only file system` 直接 exit 2,
看起来像"代码有问题"其实是闸门自己坏了。官方 `make lint` 没这个问题（它在镜像内的可写目录跑）。

那个镜像里装的是**真的 `strix-agent==1.6.2`**（升级后已重建），所以核 Strix 源码不用 clone：

```
docker run --rm --entrypoint sh strix-console/api-test:0.1.0 -c \
  'python -c "import strix,pathlib;p=pathlib.Path(strix.__file__).parent;print((p/\"interface/cli.py\").read_text())"'
```

未跟踪但**不提交**的只剩 `pitfalls/local-env.md`（在 `.gitignore` 里）。
靶场 `m0-juice-shop` **刻意没删**（`CLAUDE.md` 说它已就绪、重跑 M0 要用它）。

**剩余用例总量目标**：砍范围后剩 20 行，新增用例目标 250–350（前 18 行 838，现 985 —— T13 一行
就占了 147）。这不是封顶，真有不变式的行该占多少就占多少。

## 派发方法学（历次收货实测出来的，写新 prompt 前必读）

> 这一节是**跨任务**的经验，不随进度变。单条任务的落地细节一律在 §派发清单 对应行。

1. **T11b 留给 T14／T16 的三条约定**：
   · **`app/ws_envelope.py` 是全项目 WS 信封的定义方，T14／T16 继承它**（`Envelope` 的
   `v/epoch/seq/type/ts/payload` ＋ `Sequencer.next(epoch)` 在 epoch 变化时把 `seq` 归零）。
   `/ws/system` **刻意不实现 `hello`／`resume`**：进度是幂等快照，回放没意义；T14 的
   `/ws/scans/{id}` 推的是不可重建的事件序列，`resume` 是**在那里**才要造的东西。
   · **`DockerTransport` 有第二个方法 `stream_ndjson`**（回调式 + 调用方 `to_thread`），与 `request()`
   共用 `AF_UNIX` 连接和那 6 个 `DockerApiError.reason` 映射。再有流式 docker API 要用
   （`/events`、`/containers/{id}/logs`）就加在它旁边，**不许新建第二个 transport**。
   · WS 上的 cookie 鉴权 T4b 就做完了（`require_session(conn: HTTPConnection)` 覆盖 websocket scope，
   匿名握手产真 401 + JSON body），`nginx.conf` 的四条 WS 指令与 `location /ws/` 也早已齐 ——
   **T14／T16 都不需要再动这两处**。
2. **每一份新 prompt 都要抄进去的五条仓库事实**（T10 那个 agent 花了 4 次调用自己发现）：
   本仓刻意**没有** `pytest-asyncio`（用 `asyncio.run(scenario())`）、共享夹具的 import 形式是
   `from tests.conftest import`、`zh-CN.json` 的 `scanFailures` 布局、`Settings` 的字段名，
   以及 **把测试要断言的那些类型的字段名／构造参数一起贴上**（`ScanOutcome` 的 8 个字段、
   `CredentialSet` 的构造参数…）。判据是 §九.1 的变种：**只给生产代码的摘录、不给它要断言的那个
   类型的字段名，agent 必然去读原文** —— T12b 为这一件事花掉 3 次调用。
   顺带：`tests/conftest.py` 里**哪些夹具可以被别的测试文件请求**也要说。
   **T11a 复盘再加四段必须直接贴原文的**：`key_vault.run_sweeper()` 的 cancel+await 先例、
   `conftest.py` 的**全貌**（只贴 `settings` 夹具不够）、`test_scan_supervisor.py` 里 `/bin/sh -c`
   当假 strix 那段写法、**改动点周边的既有测试**。
3. **"禁止再读原文"这类禁令基本无效**（三次复现），只有"把内容贴够"有效。所以交底文件里
   **只出现它要写的那几个文件的真路径**，摘录一律只标附录行号（§九.1）。
4. **每一处"接线"都必须有一条会因为删掉那次调用而变红的测试**（T10 立、T11a 验证有效），
   并在 prompt 里要求它自己先删一次、跑一遍、把红的测试名贴进报告。T7b 的 `params`、T9 的
   `_PINNED_ENV`、T10 的两次 `main.py` 调用都是"声明了、接线了、没有任何一处强制"。
   配套：**"停机顺序"类断言不要断言状态，要断言顺序** —— `asyncio.run` 收尾本来就会 cancel
   剩余任务，所以"cancelled 为真"恒成立、那条测试是空的（T11a 第一版栽在这里）。
5. **方法学结论（T12c 实测，四次复现）**：闸门全绿 + 清单上那几条 mutation 全对，仍然漏了三处
   "接线没有任何测试"与一处会让 CI 随机变红的竞态。**自己设计的 mutation（不止跑清单上那几条）
   是唯一抓到它们的手段**；"没人写过的测试不会失败"（§八.3）。
6. **并发期间的跨 agent 发现要等工作区静止后再核**，别直接当缺陷修 —— T12a 报的
   "`test_scan_secrets.py:118` 过不了 `ruff format`"是它读到并发同伴半成品时的瞬时态。
7. **粒度**：T13 一条任务 3 条不变式／7 个文件／1575 行 → 必然压缩（`pitfalls` 条 41b）。
   规则已同步进 `agent-rules.md` §三.2（新增行数超 ~500 必拆、不变式超 1 条 = 超 1 条任务）
   与 §九.4（快闸门必须按文件过滤，全量与 mutation 一律主会话自己跑）。
   **2026-09-18 反向验证成立**：T14a／T14b／T28a 三条并发，各 2 个文件／2 条不变式／
   374–511 行，**三条全部 0 压缩**（工具调用 20／22／37）。**这个形状是模板，别回退。**
8. **mutation 至少要有一处靶子是自己想的**（2026-09-18 又一次验证）：本轮 6 处里 4 处照子 agent
   的清单、2 处我自选（`rfind`→`find`、`_delete_detail_rows` 顺手删 `scans` 行），**两处自选都被
   现有测试抓住了** —— 这才叫"清单之外也有覆盖"。顺带发现**子 agent 的 mutation 预测会算错血溅
   范围**（T14b 报告说红 8、实测红 6：`in_unparsed_line` 那两格对"只脱 msg"没有区分力，因为
   未解析行的整行原文本来就进了 `msg`）。→ **预测数字一律要自己跑一遍才算数**，它连自己写的
   测试都会数错。

9. **跨模块的事实冲突要把两边都读完再下结论**（2026-09-18 花掉一整轮交接才撤销）：`001_init.sql` 的
   DDL 注释与 `run_projector.py` 的 docstring 对 `scan_events.strix_id` 说了两件不一样的话，而上一轮
   只读了前者，就把 T14c 上报成"卡住整条实时流的待拍板阻断项"。**尤其当其中一边明写着"这是已知的
   措辞偏差、不许为它改迁移"的时候** —— 那句话正是为了防止这次误判才写在那里的。

10. **交底拆成「任务书 + N 份附录文件」之后，§九.6 的判据②要换算**（W2c 实测）：那次
   **首次写代码在第 18 次调用**，但它不是"在自己找出处" —— 任务书表里给了 5 个文件 9 个行区间、
   另有 4 份附录，**光把交底读完就 13 次**。判据②（晚于第 10 次 = 出处没给够）只在
   "原文全在 prompt 正文里"的形状下成立；拆成附录文件时判据要改成 **"读交底的次数 + 3"**，
   真正的闸仍然是①（峰值／压缩）。
   **而①这次又是同一个病**：峰值 130k（超 120k），归因是 **附录整包给多了** —— `conftest` 全文
   409 行 + async 测试风格 549 行，实现真正用到的只有 `make_run_dir` 和"用 `asyncio.run` 跑场景"。
   W2b 收货时已经写下"下次只摘点名用到的那几个 fixture"，**这次没做到就又超了一次** →
   **附录的判据改成硬的：一份附录超过 ~150 行就必须先问"实现会用到其中哪几段"，只摘那几段。**

**仍待你拍板的一件旧事**（不阻塞任何派发）：`--workspace-file` 在 1.6.2 里存在了，
T9 行 ③「spec 上传 v1 不做」的**理由**作废，结论要不要跟着变是产品决策。
**顺带定过的一件小事**（§Strix 版本升级 §八 副产品 ②）：1.6.2 新增的 `EXA_API_KEY` /
`STRIX_WEB_SEARCH_PROVIDER` / 两个 `STRIX_EXA_*` **都不加进 `_PINNED_ENV`／`_PASSTHROUGH_ENV`** ——
env 是白名单式的，没在名单里就到不了子进程，那个新凭据面已经关着。

## Strix 版本升级（2026-09-14 第二段会话评估；**零代码改动，4 件待决**）

**目标变更（2026-09-14 已放行，并已落进 §已确认决策 与 `CLAUDE.md`）**：从「`1.5.3` 锁死、**不许**升级」
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
比 Python 内部 API 稳一个量级；② `strix_bridge/` 是唯一 import 点且 `test_strix_contract.py` 强制 → 爆炸半径已围起来；
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
**阶段 2 只读勘查**（10 分钟–1 工时，**不改 pin、不动 lock**）**T29 落地之前这一步只能用 wheel 源码 diff
代替**（契约测试还不存在，做法与实测见 §八）。T29 之后：一次性容器装新版、对着它跑契约测试：
`docker run --rm -v $PWD/backend:/src:ro python:3.12-slim sh -c 'pip install --only-binary=:all: strix-agent==<新版> pytest && pytest /src/tests/test_strix_contract.py'`
→ 产出 **R4 那 9 个耦合点碎了哪几条**。**这一步是整个流程的价值所在**：把"升级"从"跑跑看"变成"改这 3 条"。
**阶段 3 落地**（0.5–4 工时）profile 加/改条目 → 改 **3 处手写** pin 落点（`pyproject.toml`、
`Dockerfile:113` 断言、`check_lock.py:26`）→ **`make lock` 和 `make lock-dev` 两个都跑**（前者不含 dev lock，
漏了就是"装 1.5.3 却按 1.6.2 断言"，见 §八）→ **重建生产镜像和测试镜像两个**
（`docker compose -p strix-console build api` + `up -d api` 重建容器；`make build-test`。
只重建测试镜像 ⇒ 阶段 4 在旧版本容器里空跑，实测踩过，见 §九）→ 改碎掉的代码 →
**官方闸门 `make lint` + `make test`（必须 0 skipped）** → 按变动面重采夹具（run 目录或 `agents.db` 变了则
**T15a 压缩夹具要真跑一次扫描**，这是阶段 3 最贵的单项）。
**阶段 4 真跑复验**（**半天墙钟，一项都不能跳** —— 这四类都是"变了不报错、只静默给错答案"，测试碰不到）：
① `./scripts/m0_probe.sh` 断言日志出现**容器 IP** 而非 `127.0.0.1`（两个未文档化 env）；② **重验成本估算**
（strix **或** litellm 升级都可能让**预算护栏静默变 0**，条 38 已实测发生过）：先离线跑一遍候选名 →
`completion_cost`，再 `M0_BUDGET=0.1 ./scripts/m0_probe.sh` 看断言 6 与拦截；③ Key 卫生：镜像 env 扫 + 任务目录外全盘扫 `LLM_API_KEY`
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

### 七、四件待决已裁决（2026-09-14 用户拍板）

1. **跟 minor、不追 patch，但仍不许放宽成 `>=`／`~=`** —— 已改 `CLAUDE.md` 第 62 行与 §禁区 那条；
   §已确认决策 表同步改（放行范围就是这次裁决）
2. **`strix_profile.py` 建，但只建最小形态、并进 T10**，不立 5–5.5 工时的独立项（增量 0.3–0.5 工时）。
   **✅ 2026-09-14 已落地**（`app/strix_profile.py`，122 行）：收的就是下面这 5 项 —— `runs_dir_name`、
   `run_record_name`、`run_statuses`（**8 个：`core/agents.py:25` 那 7 个 + `interrupted`**）、
   `exit_code_{ok,failed,vulnerabilities_found}`、`attribution_rules`（10 条，**存正则源码字符串**
   而不是编译好的 `Pattern` —— 本模块是**数据**，T29 要遍历它跟另一个版本逐项比对）。
   `PROFILES` 今天只有 `"1.6.2"` 一个键；`profile_for()` 对未知版本**立刻抛并点名 runbook，绝不回落**
   （回落 = 拿旧对照表解释新输出，静默归错因比启动失败难查得多）。**唯一调用点是 `ScanSupervisor.__init__`**。
   等 T29／T13 各自补第二、第三项时才谈扩表。
   只收 T10 今天真要写的 5 项**上游事实**：`RUNS_DIR_NAME`、`RUN_RECORD_NAME`、`RUN_STATUSES`、
   退出码 0/1/2 的语义、`_ATTRIBUTION_RULES`（stdout 异常类名→归因码，最脆的一项）+ 一个版本键。
   **不预写** 18 个 env 名全表、CLI 参数白名单、`agents.db` 列名 —— 今天零调用点，等 T29（遍历断言）
   与 T13（按 `scans.strix_version` 解析旧 run 目录）各自补，那才算第二、第三次使用。
   **不进 profile** 的是我们自己的词汇：`SCAN_STATUSES`、`EXIT_MEANINGS`、`SCAN_FAILURE_CODES`、四个超时常量
3. **第 3 项并进 T10**（同上）
4. **先把 pin 推到 1.6.2 再做 T10** —— 用户推翻了我"先做 T10、pin 留到 T13 之前再推"的建议。**已执行，见 §八**

### 八、1.5.3 → 1.6.2 升级实录（2026-09-14，按 §五 runbook 走）

**阶段 0/1**：5 个 wheel，含 `manylinux_2_17_aarch64` + `manylinux_2_17_x86_64`（**硬否决项通过**），
**无 sdist**，`requires_python>=3.12` 未变。`requires_dist` **零新增依赖**，现有 pin 全部落在新约束内
（`cryptography 48.0.1<49`、`openai 2.54.0<3`、`openai-agents[litellm] 0.19.4<0.20`、`pydantic 2.13.5>=2.11.3`）。
两个 extra（`bedrock`→boto3、`vertex`→google-auth）我们都不装，boto3 本来就经 litellm 进来。

**⚠️ 阶段 2 现在不能照 runbook 字面执行**：那一步要跑 `test_strix_contract.py`，而它是 **T29、还没写**。
本次改用 **wheel 源码 diff**，这也是 T29 落地之前唯一可行的做法（**比"跑跑看"更准**，把升级变成"改这几条"）：
`docker run --rm -v /tmp/strixdiff:/out python:3.12-slim sh -c 'pip download --no-deps --only-binary=:all: -d /out strix-agent==1.6.2 && cd /out && python -m zipfile -e strix_agent-1.6.2-*.whl /out/new/'`
解开后与 api-test 镜像里那份**真 1.5.3**（取法见 §交接 那条 docker 命令）逐文件比。
**结论：R4 的 8 个耦合点碎 0 条。**

| 耦合点 | 1.6.2 实测 |
|---|---|
| `viewer/transcript.py` | **字节完全相同** |
| `core/paths.py`（run 目录布局） | **字节完全相同**，`strix_runs` 字面值未变 |
| `tui/backend/live_view.py` + `projection.py` 游标 API | 函数体有改动，**公开签名零差异**（`grep -E "^\s*(class \|def \|    def )"` 逐条比） |
| 退出码 0/1/2 | `interface/cli.py` 的退出码 diff **为空**；信号处理器仍是 `report_state.cleanup(status="interrupted")` + `sys.exit(1)`（位置 `cli.py:133`→`139`）→ `plan-t10.md` §2.1／§2.3 的结论**在 1.6.2 上仍然成立** |
| `run.json.status` 取值域 | `core/agents.py` 的 `Status` 那 7 个字面量一字未动（新增一个 `TERMINAL_STATUSES` frozenset，**我们不 import**）；`report/state.py:640` 那个把 `stopped` 挡回 `interrupted` 的守卫还在 |
| Rich 面板标题 | `cli.py`／`main.py` 的 `title=` 集合**无差异** |
| 18 个 env 名 | **只增不改不删**。新增 4 个：`EXA_API_KEY`、`STRIX_WEB_SEARCH_PROVIDER`（默认 `auto`）、`STRIX_EXA_NUM_RESULTS`、`STRIX_EXA_SEARCH_TYPE`（`config/settings.py:135-144`）|
| `agents.db` 结构 | `interface/tui/history.py` **字节相同**；全树无 `CREATE TABLE` —— 建表方是 `openai-agents` 的 `SQLiteSession`，而 1.6.2 的约束 `<0.20,>=0.19.0` 被现有 `0.19.4` 满足 → **这个风险面不随 strix 升级而动，它挂在 openai-agents 的 pin 上** |
| CLI 长选项 | **只增不删**：`--mcp-config`／`--mcp-exclude`／`--mcp-server`／**`--workspace-file`** |
| §禁区 外发红线 | 新增的 `interface/cloud/`（app.strix.ai 托管平台）只在 `interface/main.py:458` 的 `sys.argv[1] == "cloud"` 硬门后 import，我们的 argv 进不去；`STRIX_TELEMETRY` 仍是 `config/settings.py:120` 的 alias（默认 True，我们设 false）、`STRIX_NO_UPDATE_CHECK` 仍在 `update_check.py:49`。`start_import_warmup()`（`main.py:462`，新增）是 import 预热、无外发端点 |

**两个副产品（不改本次升级的结论，但要落到 T9/T10）**：
① **`--workspace-file` 在 1.6.2 里存在了** → T9 行 ③「spec 上传 v1 不做」的**理由**（"1.5.3 的 CLI 里没有"）作废；
结论要不要跟着变是产品决策，**待你拍板**，别让实现 agent 自己决定。
② `EXA_API_KEY` 是**新的可选凭据面**（web search provider）。不设即无行为变化，但按 `pitfalls` 条 20/25，
加供应商要配凭据卫生断言；`STRIX_WEB_SEARCH_PROVIDER` 要不要进 T9 的 `_PINNED_ENV` 是 T10 派发前的小决定。

**阶段 3 实测（两处 runbook 要改）**：
① **7 处 pin 落点里只需手改 3 处**：`backend/pyproject.toml:10`、`scripts/check_lock.py:26` 的 `REQUIRED_PINS`、
`backend/Dockerfile:113` 的构建期断言。其余 4 处（`pins.txt`／`pins-dev.txt`／两个 `.lock`）**由 lock 目标生成**，
手改它们等于和生成器打架。
② **`make lock` 不含 dev lock** —— 它是 `lock-resolve + lock-hash + lock-check`，只管 `requirements.lock`；
`requirements-dev.lock` 要**另跑 `make lock-dev`**。漏了这一步的后果很隐蔽：测试镜像装的是 dev lock，
于是 `make test` 会装着 1.5.3 却对着 1.6.2 的断言跑。实测结果：prod 86 包/2047 哈希、dev 90 包/2071 哈希，
两次 `check_lock` 都通过，且 `lock-dev` 自带的交叉校验报「dev lock 未改动任何运行时 pin」。
**pins 的真实改动只有两处**：`strix-agent` 那一行，加上 `markdown-it-py` 多了一行 `# via strix-agent`
（1.6.2 把它写进了 `requires_dist`，版本号没动）。
③ 顺手订正了三处**论证依赖"永不升级"**的注释（`app/main.py:97`、`services/system_status.py:43`、
`services/allowlist.py:64`）：结论不变（1.6.2 的 `requires_dist` 实测仍含 `cryptography<49,>=48.0.1` 与
`pyyaml>=6.0`），但理由改成"pin 不许放宽 + 升级走 runbook，阶段 0 就会 diff `requires_dist`"。
`tests/test_system_status.py` 那两处 `1.5.3` 是**注入的假值**（测的是原样透出，不读真实版本），一并改成 1.6.2。
④ 官方闸门：`make lint`（ruff + eslint + tsc）干净，`make test` **682 passed / 0 skipped / 13.6 秒**（与升级前同数）。

**阶段 2 补扫（R4 那 8 个点之外，两个安全相关的改动文件）—— 这是我第一遍漏掉的，别照抄那份清单**：
- `config/loader.py:26` 的 `_DEFAULT_PATH` **仍是 `Path.home()/".strix"/"cli-config.json"`**，`persist_current()`
  仍在 `loader.py:63` → **tmpfs HOME + 显式 `--config` + `finally rmtree` 这条唯一防线继续有效**（`pitfalls` 条 25、
  §Key 不落盘）。新增的 `_read_env_block()`／`_drop_stale_llm_connection()` 只改"怎么合并"，不改写盘位置。
- `utils/secret_files.py` **变严了**：原来临时文件删不掉是 `contextlib.suppress(OSError)` 静默吞掉，现在改成
  抛 `OSError("could not store the secret, and the temporary file <path> still holds it...")`。**这条消息含路径、
  不含密钥**，无新泄漏面；但它是一个**新的失败模式**，归因兜底要接得住（T10 的 `_ATTRIBUTION_RULES`）。
- **`litellm` 没有跟着升**（lock 里仍 `1.100.0`，因为 1.6.2 对它的约束是无上下界的 `litellm`）
  → 阶段 4 的 ②「重验 `completion_cost`，litellm 跟着升 ⇒ 预算护栏静默变 0」**这一次因果机制不存在**，
  降级为一次快速确认。**但 Strix 新增了 `report/pricing.py`**（它自己怎么算钱变了），所以 `--max-budget-usd`
  的**实际拦截行为**仍要在阶段 4 真跑时看一眼。

### 九、阶段 4 复验（2026-09-14 第四段会话；**准备工作已做完，真跑那一步待用户**）

**先修了一个会让整个阶段 4 变成空跑的前提**：上一段会话只重建了 `api-test` 镜像，
**生产镜像与 `strix-console-api-1` 容器里仍是 1.5.3**（容器 `Up 5 days`，起于 09-09）。
在那个容器里跑 `m0_probe.sh` 测的是旧版本，结论会是假的。已 `docker compose -p strix-console build api`
（全部层命中 15:31 那次构建的缓存，秒级）+ `up -d api` 重建容器 → 容器内实测 `strix-agent 1.6.2`、
healthy、双网络 `strix-console_default` + `strix_sandbox`、`http://juice-shop:3000` 返回 200。
**教训：阶段 3 的"重建镜像"必须包含生产镜像，只重建测试镜像会让阶段 4 沉默地测错版本**（已写进 §五 阶段 3）。

**② 的一半可以不用凭据就查完，而且查出了真问题** —— 见 `pitfalls` **条 38**：1.6.2 新增的
`report/pricing.py` 把 Bedrock 模型名解析成 `bedrock_converse/…`，而 litellm 1.100.0 的
`completion_cost` 不认这个前缀 → **本地成本兜底对我们要用的每一个 Bedrock 名字都返回 `None`**
（1.5.3 同样输入算得出 `$0.0825`；Gemini 名字不受影响）。后果：预算护栏现在**完全**依赖
litellm 自己回的 `response_cost`（`report/state.py:866-887`）。它若也缺，`llm_usage.cost` 恒 0、
`core/hooks.py:55` 永假、`--max-budget-usd` 不拦任何东西且**不打日志**。
→ 真跑时唯一要看的数就是 **`run.json` 的 `llm_usage.cost > 0`**。

**为此改了 `scripts/m0_probe*.sh`（两处，都是阶段 4 每次都要用的能力，不是顺手扩展）**：
① 预算与轮次可由环境变量覆盖（`M0_BUDGET` 默认 2、`M0_TURNS` 默认 20）—— 不能覆盖就没法拿一个
必然撞上的小预算去实测"真拦截"；② 新增**断言 6**：读 `run.json.llm_usage` 的 `cost/total_tokens/requests`，
`cost>0` 记 PASS、`cost==0` 而 tokens>0 记 **FAIL（发布阻断）**、一次 LLM 调用都没完成记「无从判定」，
并按 `status` 打印拦截判读。已用三份合成 `run.json` 覆盖三个分支、并在容器内 `sh -n` 过语法。

**顺带核实**：`STRIX_IMAGE=ghcr.io/usestrix/strix-sandbox:1.3.0` 与 1.6.2 自己的默认值
（`config/settings.py:109`）**一致** —— 沙箱镜像 tag 是 R4 那 8 个耦合点之外的一个耦合点，这次没碎。
生产镜像 env 干净（只有已知假阳性 `GPG_KEY`，条 17b）、`import pytest` 仍然失败、api 容器 env 无凭据形状的键。

**真跑结果（2026-09-14 08:40 UTC，用户执行 `M0_BUDGET=0.1 M0_TURNS=6 ./scripts/m0_probe.sh`，
`bedrock-apikey` + `bedrock/invoke/us.anthropic.claude-sonnet-4-6` + juice-shop）：
`通过 10 / 失败 0 / 跳过 0`，退出 0。①②③ 全部实测通过**：

- **① Caido**（最高风险项）：`Caido host endpoint resolved: http://172.19.0.4:48080` —— 容器 IP，
  两个未文档化 env 在 1.6.2 上仍然生效。沙箱 `networks=strix_sandbox(172.19.0.4)`、
  `labels` 含 `strix-run-id=probe` + `strix-run-type=console`（`make reap` 的回收前提成立）、跑完无残留。
- **② 预算护栏**：`llm_usage.cost=0.2024088`、`requests=1`、`status=stopped`，
  strix.log 写下 `Token budget of $0.10 exceeded (spent $0.2024)` —— **护栏真的掐住了扫描**。
  即：条 38 那条断掉的本地兜底**目前没有让护栏失效**，因为 litellm 的 `observed` response_cost
  对 `bedrock/invoke/…` 是回得来的。**但它现在是唯一的一条路**，所以断言 6 要长期留着。
  同一次实测把"软上限只超一点"推翻了 —— 见 §后端接口 的「预算的真实语义」（超 102%）。
- **③ Key 卫生**：2a 数据目录全树 0 命中、2b `/root` 0 命中、2d 七个落盘面 0 命中、
  3 沙箱 inspect `KEYSCAN=clean`。**2c 有一个 1.6.2 的新观察**：预置 `cli-config.json`
  从 11 字节被写到 **209 字节**，内含 `STRIX_LLM STRIX_PROMPT_CACHE STRIX_IMAGE STRIX_TELEMETRY`
  四个键 —— `persist_current()` 现在回写的 env 块比以前宽（§八 那条 `_read_env_block()` 的后果）。
  本次凭据形状是 bearer，四个 `AWS_*` 都不在 alias 表里所以没落盘；**换成 `single` 形状，
  `LLM_API_KEY` 就会明文进这个文件** —— tmpfs HOME + 显式 `--config` + `finally rmtree`
  仍然是唯一防线（条 25），且这次是**看着它被写了**才确认防线有效，不再是推断。
- **④ `make verify-e2e` 28 条**：待 T30b，**仍然记账为"跳过"，不是"通过"**。

**第一次真跑失败了，那次失败本身查出两个缺陷（都已修）**：模型名输成了裸名 `us.anthropic.sonnet 4.6`
→ Strix 在 `interface/main.py:186` 直接 `sys.exit(1)`（面板 `UNKNOWN MODEL NAME`，裸名默认路由到 OpenAI），
`run.json` 都没生成。
① **探测器的早期失败分类器不认这个面板**，于是断言 1/3/4 被记成「未通过」而不是「无从判定」——
正是脚本注释里警告过的那种误导（人会去查挂载，真凶是模型名）。已加分支，机器码
`model_name_not_provider_qualified`。
② **形状校验缺在凭据之前**：已在 `m0_probe.sh` 里按三种鉴权形状校验模型名（bearer 必须
`bedrock/invoke/*`），**在问凭据之前** die，不再白输一次凭据。

**顺带纠正 §八 的一个无效检查**：那张表里"Rich 面板标题无差异"是**用错了度量** ——
`interface/{main,cli}.py` 里 8 个 Panel 的 `title=` **全都是同一个字面量** `[bold white]STRIX`，
所以那一项恒成立、什么都没测。真正有区分度的是**面板正文首行**，1.6.2 里共 5 个：
`LLM CONNECTION FAILED`／`MODEL NOT AVAILABLE ON SUBSCRIPTION`／`MODEL QUALITY WARNING`／
`SESSION ENDED`／`UNKNOWN MODEL NAME`。**T10 的 `_ATTRIBUTION_RULES` 兜底要匹配这 5 个正文首行，
不要匹配 title**；T29 的契约测试同理。

**还发现一个会让护栏静默失效的输入面（记在这里给 T9/T10，本轮不实现）**：
模型名**拼错但仍能被 litellm 认成有效前缀**时，价目表查不到、`completion_cost` 返回 **`0.0` 而不报错**
（实测 `bedrock/invoke/us.anthropic.claude-sonnet-4-6-20260219-v1:0` → `0.0`，
而正确的 `bedrock/invoke/us.anthropic.claude-sonnet-4-6` → `0.0495`）。
`observed` 也走同一张表 ⇒ **cost 恒 0 ⇒ 预算护栏形同不存在，只有 `--max-turns` 拦得住花钱**。
→ 建议 `ScanLauncher` 在构造 argv 时就断言"这个模型名算得出非零成本"，否则拒绝启动。

**结论：这次升级已从"静态全绿"升为"①②③ 实测通过、④ 待 T30b"。**

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
| **Strix 版本策略** | **跟 minor、不追 patch（2026-09-14 拍板，撤销原「`1.5.3` 锁死、不许升级」）** —— 精确 pin + `--only-binary=:all:` + hash lock 全留（它们买的是**可复现构建**，不是"永不升级"），**不许放宽成 `>=`／`~=`**；新 minor 发布后等 7–10 天再升（除非 CVE），走 §Strix 版本升级 六阶段 runbook、**必须单独一个 commit**。当前 **`1.6.2`**（2026-09-14 从 1.5.3 升上来，实录见 §八）。停止跟版的四个硬条件见 §五 末段 |
| API Key | 仅会话内不落盘：浏览器只存 opaque handle（sessionStorage），明文 Key 只在后端内存 + 子进程 env |
| 技术栈 | FastAPI 后端 + Next.js/React 前端 + nginx 反代，WebSocket 推增量（SSE 兜底）|
| 易用性 | 上述四项全要 |

### 本机环境（已核实）

Docker Desktop `29.7.2` / Compose `v5.4.0` ✅ ｜ 宿主 Python 仅 `3.9.6`，无 brew/pipx/uv ｜ Node `v24.19.0`
→ Strix 要求 `>=3.12`，**后端与 Strix 必须容器化**，挂 docker socket 让 Strix 创建兄弟沙箱容器（DooD）。

---

## Strix 集成面（已逐行读源码核实；**原文对着 v1.5.3 读，2026-09-14 已升到 v1.6.2**。
**下面所有 `file:line` 的复核方式见 §交接** —— 原来的 clone `/tmp/strix_src`（HEAD `0a6e8b01`）
**已不存在**，改从测试镜像里读已安装的包）

> **⚠️ 这份地图的「结论」经 1.6.2 源码 diff 复核仍然成立（§八 那张表逐条列了），但「行号」会漂移**
> —— 实测 `cli.py:133`→`139`。所以**引用某个 `file:line` 之前先用 §交接 那条命令核一次行号**，
> 别把行号当契约；契约是那条结论。

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
  `configure_sdk_model_defaults` 的 `os.environ` 改写拖进来。由 `test_strix_contract.py` 的 AST 扫描强制（T29 未引 import-linter）。

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
| `build_extra_file_bind_mounts` | `<cwd>/strix_runs/<run>/.state/extra_files/<i>/` | `--workspace-file` **1.5.3 的 CLI 没有它，1.6.2 里有了**（2026-09-14 源码 diff 实测，见 §八）—— 这一行在 1.6.2 上**变成可达的**，但用不用是产品决策、**待拍板**；拍板之前 v1 仍不做。API spec 要进沙箱只有一条路：`-t <spec 文件>`（`-t` 明写接受 OpenAPI/Swagger `.json/.yaml` 与 Postman 导出），走的是上一行的 `stage_api_specs` |

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
   └ /api,/ws ─────────────────────▶  api (FastAPI, python:3.12 + docker-cli + strix-agent==1.6.2)
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
│   ├── Dockerfile              # python:3.12-slim + docker-cli + strix-agent==1.6.2 --only-binary=:all:
│   ├── pyproject.toml  importlinter.ini
│   └── app/
│       ├── main.py  settings.py  db.py  models.py  logging_setup.py  errors.py
│       ├── strix_profile.py       # 上游事实对照表，按版本索引；**零 strix import 所以不在 strix_bridge/**
│       ├── migrations/{001_init.sql,002_report_translations.sql}
│       ├── routes/{health,system,providers,keys,templates,targets,allowlist,scans,stream,reports,audit}.py
│       ├── services/
│       │   ├── key_vault.py        target_guard.py    allowlist.py
│       │   ├── scan_launcher.py    scan_supervisor.py run_discovery.py
│       │   ├── run_projector.py    event_mirror.py    log_tailer.py   channel.py
│       │   ├── reaper.py           docker_probe.py    llm_client.py
│       │   └── translator.py       exporter_html.py   exporter_docx.py  audit.py
│       └── strix_bridge/           # 唯一允许 import strix.* 的地方（`test_strix_contract.py` 强制）
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
| 16 结案 | 同上 | **2026-09-14 T10 选 (a)：正文永不回显。** `ScanSupervisor` 不读 `instruction.txt`，而且更强的一条落进了结构里 —— `error_message` **只由我们自己的词表与整数构成**（`f"{error_code}; exit=…; run_status=…; matched=…"`，见 `_format_error_message` 的 docstring），**一个字节都不来自 stdout**。这不是风格选择：agent 会拿那个口令去登录，**它出现在 stdout 里是预期行为**，做成这个形状那条通往 DB 的路在结构上就不存在，而不是靠谁记得脱敏。有一条带哨兵口令的测试钉着（先断言哨兵真的被抓到，再断言它不在 `error_message` 里）|
| 17 | **诊断日志里的 stdout 尾巴**（2026-09-14 T10 新增，owner **T12b**）| 归因失败（`scan_failed_unknown`）时 `scan_supervisor` 打一行带 `stdout_tail`（末 4 KiB）的 warning —— 没有它，未归类的失败就永远无法归类。**它经 root handler 的 `RedactingJsonFormatter`，所以 Key 类的值被脱敏，但测试账号口令不在精确子串集合里（就是第 16 行那个洞）**。T10 刻意不在此处再挂一个 Redactor 副本（脱敏点只有一处是本项目的设计）。**T12b 必须做的两件**：① 把 `spec.test_credentials` 的值注册进 Redactor（`ScanSecretRegistry`），口令与 Key 走同一个精确子串通道；② 给这个日志字段加一条脱敏测试 —— 现在**没有**测试钉住"那行日志真的被脱敏了"。<br>**2026-09-16 T12b 收货：两件都做了，但本行只闭合了一半。** 已闭合的是"机制存在且被证明有效"：`test_stdout_tail_warning_is_redacted` 起真子进程走完整条路（回显口令 → 归因 `scan_failed_unknown` → 那行 warning 经真的 `RedactingJsonFormatter`），并且**先断言捕获到的 JSON 行里真有 `stdout_tail` 字段**再断言它的值（pitfalls 条 23）。**未闭合的是运行时**：`register()`／`forget()` 还没有生产调用方，所以注册表在生产里恒为空、这个洞在运行时**还开着** —— **接线在 T12c，本行到那时才算结案**。（附：agent 在接线之前跑过一次，那行 warning 里两个口令都是明文 —— 这个洞是实测存在的，不是理论上的。）<br>**2026-09-16 T12c 结案。** 接线已存在：`routes/scans.py` 起扫描时把**每一条** `TestCredential` 的口令 `register()` 进注册表，终态的 `finally` 里 `forget()`，所以注册表在生产里不再恒为空、这个洞在运行时**关了**。**结案凭据是 mutation 不是声明**：删掉那次 `register()`（M3）→ `test_test_credentials_are_registered_for_redaction` 变红；删掉 `finally` 里的 `forget()`（M4）→ `test_credentials_are_forgotten_when_the_scan_ends` 与 `test_a_failure_to_spawn_marks_the_row_failed` 的 `scan_secrets.count() == 0` 变红。两个方向都有"删掉那次调用就变红"的测试钉着，符合 T10 立的收货清单第一条。|

**KeyVault 生命周期**：`IDLE_TTL=8h` / `HARD_TTL=24h` / 60s sweeper；`ref_count` 跟踪活跃扫描与报告任务；
`DELETE /api/keys/{h}` 立即清除。`api` **必须 `--workers 1`**（vault 是进程内 dict，多 worker 会随机 404）——
启动时校验 `WEB_CONCURRENCY`。不做 `mlock`（容器无 `IPC_LOCK` 会失败，且是虚假安全感）。

**`api` 重启后**：所有 handle 消失。运行中的扫描继续（子进程已持有 Key），但**报告翻译需重新输入 Key** ——
这是正确且诚实的行为。失效的 handle 一律 `409 key_required`，UI 弹窗预填 provider/model、Key 框留空，
文案说明"我们从不保存密钥"。
（**`POST /api/scans/{id}/resume` 2026-09-16 随 T28 砍掉、2026-09-24 恢复（T31b）**；这条"必须带
`vault_handle`、失效即 `409 key_required`"的规则原样适用。）

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

**差分与重同步**（应对 `agents.db` 重写）：维护 `_sent{event_key→fingerprint}`、`_order[]`、`_epoch`。
判据**按顺序、命中即停**：`shrank`（变短）/ `prefix_unstable`（同位置的 key 被换掉）任一成立 →
`_epoch += 1` 并在新 epoch 下逐条重发 `event.add`（`events.snapshot` 已删，见本节末）；`version` **递增**（如 `data.status` `running→completed/failed`）
是**正常更新**，发 `event.update`、**不动 epoch**。

**截图淘汰与上下文压缩是两件事**（2026-09-17 T13 读源码核实；本节原文把它们当成了一件）：

1. **截图淘汰保序保长度、不该 bump epoch。** `core/sessions.py:177-199 enforce_image_budget` 的
   `_transform` 只把靠前的 image output **原位**换成 `core/sessions.py:60-62` 那三个字面量之一，
   `rebuilt` 与 `items` **等长同序** —— 所以它既不 `shrank` 也不 `prefix_unstable`，只表现为
   fingerprint 变而 `version` 没变。而这件事**根本不需要重同步**：我们首见即落地了 media 镜像，
   前端照样显示。→ 发 `screenshot_elided` notice，epoch 不动。
2. **真正的重排是压缩。** `llm/compaction.py:389` `new_items = [_checkpoint_item(summary), *recent]`
   —— 头部 N 条塌成 1 条 `role=user`、content 带 `llm/compaction.py:34` 的
   `<conversation-checkpoint>`。它**同时**命中 `shrank` 与 `prefix_unstable` → 必须 `_epoch += 1`。
   重同步时若首条事件带那个 tag，就把它当**正证据**报 `context_compacted`（人话「早期对话已被摘要
   替代」），否则只报 `stream_resynced`。
3. fingerprint 变、`version` 没变、又**不是**那三条字面量之一 → 这才是可疑的 `mutated`，`_epoch += 1`。

把 ① 认成 ② 的代价是：每淘汰一张截图就给前端一次全量重同步 + 一句不真的「上下文已压缩」。
这一整套（尤其把 ① 与 ② 分开）是我们比 Strix 自带 viewer 强的地方。
**elision 识别必须排他**：只认那三条字面量，不许用"包含 `elided`"之类的模糊匹配（T13 的 I3）。

**EventMirror（只追加真源）**：每个 snapshot/delta/update 写 `scan_events(scan_id, epoch, seq, ...)`；
`data:image/...;base64` 解码一次落到 `<data>/scans/<id>/media/<sha256>.png`，事件里改写成
`/api/scans/{id}/media/<sha>.png`。收益：WS 帧小、截图能抗 elision、报告可内嵌、重连可从镜像回放。

**自适应轮询**：running 且近 3 tick 有变化 → 250ms；空闲 ×2 退避到 2s 上限；
`read_run_summary(...)["finished"]` 为真且子进程已退出 → 停。

**日志**：按字节偏移续读 `strix.log`，按 `telemetry/logging.py:45-46` 的格式解析成
`{ts, level, scan_id, agent_id, logger, msg}`（**五个字段，2026-09-18 从装了 1.6.2 的测试镜像里
逐字核过；原来写的 `:47` 与四字段列表都是错的**）：

```
_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(scan_id)s %(agent_id)s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
```

`levelname` 是 `-7s` **左对齐补空格**（级别后空格数不固定）；`scan_id`／`agent_id` 未设时
`_StrixContextFilter` 渲染成字面量 `-`（要解析成 `None`）；消息正文里可能含 `": "`，别用第一个冒号切。
`--resume` 是 append 模式所以偏移跨续跑仍有效；默认只推 `INFO+`。
**用户看到的"终端"其实不是 `strix.log`**，而是 `tool_name == exec_command` 事件的 `args.cmd` / `result`（`result` 是一段拼好的字符串；2026-09-24 T17c 按 1.6.2 源码更正，原写 `args.command`）
—— 复用 `tui/backend/projection.py::sanitize_terminal_text` 去 ANSI/控制字符。

**WS 信封**：`{v, epoch, seq, type, ts, payload}`，`type ∈ agents|event.add|event.update|
vuln.add|summary|log|report|report.progress|notice|error|done`。
**`phase` 帧已于 2026-09-22 由用户拍板删除**（原先列在第一位）：它一直没有生产者，而"现在到哪一步了"
这件事已经有两个真出处 —— 镜像拉取有 `/ws/system` 的进度帧（T11b），扫描开跑之后有 `agents` 帧里
每个 agent 自己的状态。再发一个 `phase` 等于让前端在两份可能互相矛盾的判决里挑一个。
`scans.phase` 这一列同理不写（见 §派发清单 W3 行）。
`notice` 的 payload 带机器码（`screenshot_elided` / `context_compacted` / `stream_resynced`，T13 定义），
中文文案在 `zh-CN.json`。原先那个 `compaction_notice` 类型名把"码"写进了"类型"，加第二种通知就得加
第二个类型 —— 而 T13 一上来就有三种。
客户端 `{"type":"hello","resume_from":{epoch,seq}}`，epoch 匹配则从镜像回放，否则给新 epoch 的全量快照。

**`events.snapshot` 已删（W2 方案，2026-09-20 用户放行）**：重同步就是"新 epoch 下把每一条事件当
`event.add` 再发一遍"，**一帧一行一个 seq**。这样回放（从 `scan_events` 读）与直播是同一个帧形状，
前端只有一套解析。连带两条：**① `seq` 只用于排序与 `resume_from` 游标，不是丢帧探测器** —— 非事件帧
（`summary`／`log`／`notice`／`agents`…）与事件帧共用一个计数器，而只有事件帧进镜像，所以回放必然有
空洞；"作废本地状态"全靠 `epoch`。**② `done` 帧不带结论**：状态／归因／漏洞数只从 `GET /api/scans/{id}`
取（唯一出处是 `_run_to_completion` 写的那行 `scans`，归因活在 `ScanOutcome` 里；channel 再推一份就是
第二个可能互相矛盾的判决，而 CLAUDE.md 禁止只凭退出码报"未发现漏洞"）。
**背压**：订阅者各自一个队列，满了就摘掉那个订阅者（让它带 `resume_from` 重连），**绝不阻塞轮询循环**；
`EventMirror` 写失败**让异常冒泡杀掉 channel 任务**，不静默丢帧 —— 只追加的真源出洞比断流更糟。

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
| API 接口测试 | `standard` | $25 / 200 | 枚举 spec 每个 operation；额外报告"spec 里有但不可达"与"未文档化但存在"的接口。**⚠️ 上传 spec 在 v1 不做**（2026-09-14 T9 方案审阅时定）：原写的 `--workspace-file` 在 1.5.3 的 CLI 里**不存在**（见 §DooD 路径别名 那张表）—— **注意：1.6.2 里它存在了，所以这条「不做」的理由已作废、结论待重新拍板，在那之前保持不做**。当时唯一的路是把 spec 当成第二个 `-t` 目标，而那要先让 `target_guard` 接受"文件路径"这一类目标（v1 只放 URL/域名/IP，§架构）。所以 v1 这个模板 = 指令正文 + 普通 URL 目标；开放它是 T18/T19 的事，不是 T9 的 |
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

**强制授权声明**（向导**第 2 步**，不可跳过、不可预填。**2026-09-19 用户拍板**：步序以首页已实现的
`DOCKET_FIELDS`（`frontend/src/app/(app)/page.tsx:66-76`：目标 → 授权依据 → 操作人 → 场景模板 → 费用上限）
为准 —— 那里明写"首页的五步 = 工单的五个字段 = 向导的五步"是一条不变量，改向导就要一并改首页。
本行原写"第 4 步"**作废**，下面那五条子要求一条不动。操作人姓名单独成第 3 步，但它与 `authorization_ref`
同属 `ScanAuthorizationInput` 一个请求对象）：
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
`key.dropped`。**导出只有 CSV**（`GET /api/audit.csv`，审计 UI 已于 2026-09-16 砍掉）。

**原先这里还列了 `override.private_used`／`override.loopback_used`，2026-09-17 用户拍板去掉**：
放行之后 `GuardVerdict.required_opt_in` 必为空，要说出"哪个勾选被用上"就得写 `target_guard` 判定
逻辑的第二份副本 —— 改为记进 `authorization.affirmed` 的 detail（§护栏 那条 ③ 与
`services/audit.py` 的常量块 docstring 早已这么写，这次只是把设计段和验收 19 对齐到实现）。
同一次拍板把 `key.forgotten` 更名为代码里实际的 `key.dropped`（**事件名是已落盘的数据**，
改代码会让老行新行双名，grep 审计从此要查两个名字）。

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

## 专家模式：**不代理 Strix 自带 SPA**（2026-09-16：自己做的那个 tab 已砍，降级成 zip 下载，见 T24）

> **1–3 条讲的是"为什么不代理它"，那是禁区、永久有效；末尾的"原生查看器逃生门"也保留（M7）。**
> 砍掉的只是中间那段「所以专家 tab 用自己的 store 渲染」—— 替代品是 `GET /api/scans/{id}/raw.zip`
> 一个下载按钮（T24）。**那段做法原样留着，将来要恢复直接照抄**（它挂在 T13 的 `RunProjector` 上，不受影响）。

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

**~~所以专家 tab 用自己的 store 渲染~~（2026-09-16 砍，见本节顶部；以下内容留作将来恢复的蓝图）**：原始事件 JSON（带复制按钮）、完整 agent 拓扑与每个 agent 的
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
**2026-09-14 已落地**：本节的判定逻辑现在是 `services/scan_supervisor.py` 的
`resolve_attribution()`（纯函数、零 IO、**顺序即优先级**，9 行判定表逐行有测试），三个输入分别是
`exit_code` / `run.json.status` / 我们自己记的 `stopped_by`，**各自只能回答一个问题**（见该模块 docstring）。
`exit_meaning` 与 `status` 是**两个正交的轴**：退出码 `2` 恒为 `vulnerabilities_found`，与"跑完了没有"无关。
⚠️ **原写的「`-15`/`-9`→已手动停止」是错的，2026-09-14 读 1.5.3 源码证伪**：`interface/cli.py:124-135`
给 SIGTERM／SIGINT／SIGHUP **都装了处理器**，里面 `report_state.cleanup(status="interrupted")` 之后
`sys.exit(1)` —— 进程是**正常退出**的，`returncode` 是 **`1`**，不是 `-15`。所以"是不是被人停的"
**唯一可靠判据是「我们自己发过信号」这个事实**（`ScanSupervisor` 自己记的 `stopped_by`），
按退出码判会把"用户点了停止"报成"扫描失败"、还会去 stdout 里瞎归因。只有 SIGKILL 才给 `-9`。
同一条源码还说明**没有"更优雅的信号"可选**（两个信号同一个处理器），优雅停止只能是
「发 TERM → 等宽限 → SIGKILL」，且 `interface/cli.py:201-205` 那个 async 的
`session_manager.cleanup` 跑不完 → **强杀一定泄漏沙箱容器，只能靠 T11 按 label 回收**。
**同一条源码也证伪了停止的 UI 文案**（D5，2026-09-14 已改 `zh-CN.json` 的 `scan.stopHint`）：
原文"agent 会先把手里的发现写完再退出"是**编的** —— 信号处理器只做 `cleanup(status="interrupted")` 就
`sys.exit(1)`，没有任何"写完手头工作"的语义。现文案只说三件源码支持的事：不会再开新的测试步骤、
**已经拿到的发现和报告不会丢**（`run.json` 与 `report/` 是边跑边写的）、沙箱容器在随后的清扫里回收。
**别把它改回去** —— 这条文案的每一句都必须能指到一行源码。

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
| `UNKNOWN MODEL NAME` | `model_name_not_provider_qualified`（指引：模型名要带供应商前缀）。**必须排在 `model_not_found` 之前**，两者的字面量会同时出现；裸名会默认路由到 OpenAI，Strix 在 `interface/main.py:186` 起飞前就 `sys.exit(1)`、`run.json` 根本不生成 |
| `NotFound` / `model_not_found` / `MODEL NOT FOUND` | `model_not_found` |
| `MISSING REQUIRED ENVIRONMENT VARIABLES` | `missing_required_env` |
| `docker` + `permission denied` | `docker_permission_denied` |
| 只剩面板标题 `LLM CONNECTION FAILED` | `llm_connection_failed`（兜底） |
| `DOCKER NOT INSTALLED` / `FAILED TO PULL IMAGE` / `SCAN PREPARATION FAILED` | 同名机器码 |

`llm_tls_intercepted` 的中文指引必须指向"你在企业 TLS 解密后面"这条真因与 CA bundle 挂载口子
（见 §M0 带出的两条产品需求），而不是笼统的"检查网络"。
**2026-09-14 落地**：这张表的权威副本现在是 `app/strix_profile.py` 的 `_ATTRIBUTION_RULES_1_6_2`
（10 条，存的是**正则源码字符串**，编译在 `scan_supervisor.compile_rules`）—— 本表是它的人话版，
两者不一致时以代码为准；上面两条"必须排在 X 之前"各有一条顺序测试钉着。
最后三行（`DOCKER NOT INSTALLED` / `FAILED TO PULL IMAGE` / `SCAN PREPARATION FAILED`）**刻意没进代码**：
面板标题在 `interface/{main,cli}.py` 里 8 个 Panel 的 `title=` 全是同一个字面量 `[bold white]STRIX`，
没有任何区分度，所以只匹配正文（`docker_permission_denied` 那条就是从两个 `grep -i` 的逻辑与
翻成一条带 400 字窗口的正则，无窗口会把相隔 50 KiB 的两个无关错误撞成一条）。
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
**2026-09-14 落地**：这 8 个字面量现在是 `strix_profile.py` 的 `run_statuses`；读取在
`services/run_discovery.py:read_run_status()`，它**任何情况都不抛**（`run.json` 每有新发现就被整体重写，
轮询读到半截文件是必然事件），取到表外的值**原样上报 + warning**（判死它反而丢掉"上游加了新状态"这个升级信号）。
同一段源码还给出"冲突时以 run 记录为准"的精确落点：`completed` 一旦写下就**再也不会被覆盖**
（`save_run_data` 的 `elif` 守卫），所以「操作者在进程即将正常退出那一瞬点了停止」应判 `completed`。
一个渗透测试控制台在钱花光时报"目标干净"，比不报任何结论危险得多。

**预算的真实语义（同次实测，UI 文案必须照这个写，不许自己编）**：
`--max-budget-usd` 是**软上限** —— 给 `2` 实花 `$2.0572`（超 2.9%），因为
`core/hooks.py:55` 的 `cost >= max_budget_usd` 是**每轮结束后**才判，必然超一轮的量。
**⚠️ 2026-09-14（strix 1.6.2、sonnet 4.6）实测把"超一点"这个说法推翻了**：给 `0.1` 实花
**`$0.2024`（102%）**，且 `llm_usage.requests = 1` —— **第一次调用就 60k input tokens**，
一次就把上限顶穿一倍。所以超出量的量级是「**一次调用的成本**」，不是一个百分比：
上限越小，超得越离谱。派生两条产品要求（**待做，不在任何已落地任务里**）：
① 向导的预算**下限**必须显著高于一次调用的成本（给 `0.1` 等于只跑一轮，钱花了、结论没有）；
② `zh-CN.json` 的 `budget.overflowNote` 原写"顶出上限**一点**"，已改成不承诺量级的说法。
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
| M7 | 原始 run 目录 zip 下载按钮（**专家 tab 已砍**）；审计 CSV 导出接口（**审计 UI 已砍**）；首页诊断区块（M3 每个失败模式都有中文修复指引，**独立诊断页已砍**）；原生 viewer 按钮（开关后） | 1 |
| M8 | DOCX 导出（**保留**，用户 2026-09-16 明确要"能导 Word"）；留存清理任务（**并发队列已砍**；续跑 2026-09-24 恢复，见派发清单 T31a–T31c）；`test_strix_contract.py`（**升级预警线**）；`README.md` + `docs/` 四份文档；`make verify-e2e` | 2 |

单人约 **20 个工作日**（M1 因 TLS 从 2 天增至 3 天；2026-09-16 砍范围后 M7 从 2 天降到 1 天）。**M0 永远第一。**

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
| T8 | 授权清单（加载／校验／热重载）+ `/api/targets/validate` | T6 | `services/allowlist.py` `routes/{allowlist,targets}.py` | 2 ✅ 2026-09-12，**130 个新用例**。四个测试文件各造一份的 `app`／`anonymous`／`client` 夹具与 `make_entry` 收进了 `conftest.py`（需要替身的文件**覆写 `anonymous`**，替身必须在 lifespan 跑完之后才装得住）。**另新建 `services/audit.py`**（`audit_log` 表 T2 就建好了但没有写入方）—— 只有一个 `record()`，DB + ndjson 双写共用同一时刻，**T12a／T25 在它上面加事件，不要造第二个写入方**。`/api/targets/validate` **刻意不写审计**：它是会被反复调用的只读预览。<br>**T6 交回的三件事**：① `zh-CN.json` 的 `targetGuard` 子树（8 个 `RejectionReason` 的中文 + `notes.loopback_rewrite`；**不进 `errors.py`**，它们是输入框下方的行内提示，请求本身没失败）—— 已做；② ~~`registrable_domain`~~ **已定不做**（2026-09-11 用户拍板）：正确实现要 Public Suffix List，而近似实现（取末两段）在 `example.co.uk` 上算出 `co.uk`，这个值要进逐字确认串 —— **一个错的注册域名比没有更糟**，会让用户确认一个不是他想授权的范围。**别再加回来**，要加就先把 PSL 那笔维护账付掉；③ ~~⏳ `TargetRejected` 在 HTTP 层怎么表达~~ **2026-09-12 定完，判据表搬到 T12a 行** |
| T9 | `ScanLauncher`：argv + env + tmpfs HOME + 预置 `--config` + cwd/TMPDIR + `RUN_ID`；6 个模板的黄金 argv 测试 | T7 T8 | `services/{scan_launcher,scan_templates}.py` `routes/templates.py` `tests/test_scan_{launcher,templates}.py` | **3** —— **2026-09-14 已落地并复验**：`services/scan_templates.py`(194) + `services/scan_launcher.py`(350) + `routes/templates.py`(56) + `tests/test_scan_launcher.py`(405，23 条) + `tests/test_scan_templates.py`(62，5 条) + `main.py` 一处接线；官方闸门 **682 passed / 0 skipped**、ruff 干净。施工图 `plan-t9.md`（已随 a7458fa 提交，被跟踪）的用途已尽，可删（删了 git 历史里还在）。<br>**复验（主会话自己做，没采信子 agent 的报告）**：8 次 mutation（argv 塞 `api_base`／改用 `--instruction`／env 变成 copy／不设 `STRIX_RUN_ID`／`STRIX_PROMPT_CACHE` 恒 true／指令文件 0644／`cleanup_workspace` 变 no-op／去掉预算必填），**每次只红该红的那几条**。<br>**两条"负对照"没红 = 一个真缺陷，已修**：env 原先把 `STRIX_TELEMETRY`／`STRIX_NO_UPDATE_CHECK`／`LITELLM_LOG`／`STRIX_RUN_TYPE` 当"容器级透传，缺就跳过"（这是方案里写的），而这四个**缺失时都不报错**：缺 telemetry 就是 PostHog + Scarf 外发（§禁区第一条），缺 `STRIX_RUN_TYPE` 就是沙箱容器没 label、`make reap` 回收不到 —— 从白名单删掉任何一个，**没有一条测试变红**。又是 T7b 那个 `params` 缺陷的形状（声明了却没有任何一处强制）。现改成 `_PINNED_ENV` 写死在代码里、**放在透传之后**（本进程环境写了相反值也不生效），加一条测试。`STRIX_IMAGE` 与 `STRIX_DOCKER_SANDBOX_NETWORK` 刻意仍走透传（**没有安全的默认值**，猜错网络名比报错难查），强制点交给 T10 的启动断言。<br>**子 agent 相对方案的 4 处偏离，全部判定接受**：① 加 `build_launch_plan()` 组合器（方案定义了 `LaunchPlan` 却没有函数产出它，不加它 `argv_preview == argv` 就没有可断言的落点）；② `build_argv` 多一个必填 `budget_ceiling_usd`（把预算上限校验挂在 argv 的唯一入口上，而不是"请调用前先校验"）；③ `prepare_workspace -> Workspace`（方案未定返回类型）；④ 多校验 `scan_mode ∈ {quick,standard,deep}`（与已列的 `reasoning_effort` 同类：非法值会让子进程死在 argparse 上、被归因成"扫描失败"，复用同一个已有码、零新增代码）。<br>**实测代价**：32 次工具调用（预算 ≤55）、0 次压缩、第 8 次调用开始写代码（预算是第 6 次）—— 晚的那 3 次是 `grep` 去查方案摘录没给的四个符号（`SHAPE_*` 的字面值、`TestCredential` 仓库里不存在、`settings.scans_dir` 这个 property 名、`BudgetExceedsCeilingError` 没有调用先例所以 params 键名要自定）。**下次派发把"要用到的符号字面值"也抄进摘录**。<br>**审阅时定的四条**（都已落进本文件对应位置，此处不复述理由）：① 模板存 Python 常量不存 YAML（§向导→CLI 映射）；② `--resume` 不进 T9（`strix_run_name` 要等 T10 抢到才存得下）；③ spec 上传 v1 不做（`--workspace-file` 在 1.5.3 的 CLI 里不存在，见 §向导 那张表与 §DooD 路径别名 那张表）；④ **`zh-CN.json` 不在 T9 范围内** —— `/api/scan-templates` 只回机器码，模板中文名归 T19（不跨文案，所以这一行不用再拆）。<br>**边界**：T9 只构造 argv/env/cwd + 建 tmpfs 上那两个文件，**不起进程**（起进程与退出码归因是 T10；并发闸／DNS 重解析／逐字确认／写 `scans` 行／审计是 T12）。<br>**实测得到的三个 env 名**（`strix.config.settings.LlmSettings`，2026-09-14）：`api_base` 的落点是 **`LLM_API_BASE`**（不注入就静默丢失）；`STRIX_REASONING_EFFORT` 是**枚举** `none\|minimal\|low\|medium\|high\|xhigh\|max`（默认 `high`，非法值会让子进程死在 pydantic 启动校验上、被归因成"扫描失败"，所以要在构造阶段 422）；`STRIX_PROMPT_CACHE` 的判据是 `"invoke/" in model_for(...)`，**不是** `auth_shape == bearer`。`--resume` 收的是 **run name**（`strix_runs/` 下的目录名）不是 `scan_id` —— `scans.strix_run_name` 那一列就是为它准备的 |
| T10 | `ScanSupervisor`（退出码→中文、优雅停止、`finally` 清 tmpfs）+ `RunDiscovery` | T9 | `services/{scan_supervisor,run_discovery}.py` | 2 —— **2026-09-14 已落地并复验**：`app/strix_profile.py`(122) + `services/scan_supervisor.py`(497) + `services/run_discovery.py`(89) + `tests/test_{scan_supervisor,run_discovery,strix_profile}.py`（新增 54 条）+ `errors.py` 四个新码 + `zh-CN.json` 四条文案 + `main.py` 三处接线 + `scan_launcher.cleanup_scan_tmpdir()` + `conftest.py`（`settings` 夹具补两个沙箱变量、新增 `make_run_dir`）；官方闸门 **736 passed / 0 skipped**、ruff/eslint/tsc 干净。施工图 `plan-t10.md`（未跟踪）用途已尽。<br>**T9 交回的四件事全部结清**：① 启动断言 `assert_sandbox_env()` 已落在 `main.py` 的 `assert_single_worker` 之后；② `${DATA}/scans/<id>/tmp` 的清理**归 T10**（`cleanup_scan_tmpdir`，挂在监控任务的 `finally` 上）→ **T11 与 T28 不许再碰它**；③ `strix_run_name` 由 `RunDiscovery` 抢到后放在 `ScanProcess.run`，落库是 T12；④ 泄漏矩阵 #16 **结案（选 (a)：正文永不回显）**，新增 #17 归 T12。<br>**复验（主会话自己做，没采信子 agent 的报告）**：8 次 mutation（判定表四处顺序／`exit_meaning` 恒等／`env` 变成 `os.environ \| plan.env`／清理搬进 `wait()`／删启动断言／删停机 `shutdown`／`error_message` 塞 stdout／`_drain` 换 `readline`），**6 次只红该红的那几条**。<br>**两处全绿 = 两个真缺陷，已修**：删掉 `main.py` 里的 `assert_sandbox_env(resolved)` 与 lifespan `finally` 里的 `await supervisor.shutdown()`，**都没有任何一条测试变红** —— 三条 `test_assert_sandbox_env_*` 和 `test_shutdown_*` 测的都是函数自己的行为，而那两次调用才是它们的全部价值。**这是 T7b 的 `params`、T9 的 `_PINNED_ENV` 之后同一形状的第三次**（声明了、接线了，但没有任何一处强制）。已补两条接线测试（`test_startup_refuses_when_sandbox_env_is_empty` 的 `match="STRIX_IMAGE"` 不是装饰：删掉调用后启动会继续走、最终死在"没有 auth.json"上，只断言异常类型会照绿）。**下一个任务的收货清单第一条：把"每一处接线都有一条会因为删掉那次调用而变红的测试"当硬要求。**<br>**方案外的一处主会话决定**：`model_name_not_provider_qualified` 原先只存在于 `scripts/m0_probe_inner.sh` 与本文件，**不在 `SCAN_FAILURE_CODES` 里** —— 所以新增的是**四**个码不是三个（另三个 `stopped_by_operator`／`interrupted_by_restart`／`scan_failed_unknown`），并补了一条顺序测试。<br>**判定表 6–9 行与"退出码 2 恒为 `vulnerabilities_found`"冲突时判给后者**（`status` 与 `exit_meaning` 正交，一个 `failed` 的扫描仍然可以有发现）。<br>**三条读代码留下的观察（不是缺陷，别当 bug 修）**：① `matched` 无条件计算，stopped/interrupted 的 `error_message` 里也会带一个可能无关的 `matched=…`（误导但不泄漏，它是我们自己的词表）；② `run_status` 原样进 `error_message`，含未登记值（刻意，且不是 stdout）；③ `SCAN_STATUSES`／`EXIT_MEANINGS` 只被黄金表钉住，将来某个分支返回表外的值不会被抓。<br>**遗留给 T12c 的三条**：`ScanProcess.stop(force=True)` 目前**没有调用方也没有测试**（要么 T12c 用上，要么删）；`interrupted_by_restart` 一码两因（我们发的停机信号 / 外部 `docker kill`）而文案只说了前者；`starting`／`running` 两个 `SCAN_STATUSES` 值本模块永不返回（它们是 T12c 落库时的状态）。<br>**实测代价**：57–58 次工具调用（软线 60）、0 次压缩、**第 10 次调用才开始写代码**（预算是第 6 次）—— 晚的那 4 次全花在自己发现仓库事实：本仓刻意没有 `pytest-asyncio`（用 `asyncio.run(scenario())`）、共享夹具的 import 形式是 `from tests.conftest import`、`zh-CN.json` 的 `scanFailures` 布局、`Settings` 的字段名。**这四条以后直接抄进 prompt。**<br>（原始交接，已全部结清）**T9 交回四件事**：① **启动断言 `STRIX_IMAGE` 与 `STRIX_DOCKER_SANDBOX_NETWORK` 非空**（两者仍走 env 透传、缺了静默降级：沙箱网络缺失 → Caido 端口解析成 `127.0.0.1`。`settings.py` 已有这两个字段且默认 `""`，断言挂在 `main.py` 的启动序列上，和 `assert_single_worker` 同一处）；② `cleanup_workspace()` **只删 tmpfs**，`${DATA}/scans/<id>/tmp` 的清理没有归属（T10 或 `make reap` 挑一个，别两边都做）；③ `--resume` 收的是 run name，`RunDiscovery` 抢到之后才写得进 `scans.strix_run_name`；④ 指令正文里测试账号是 `role=… username=… password=…` 一行，**那个口令不在 KeyVault 里因此不在脱敏集合里** —— T10 若把指令正文回显进日志/前端就是明文泄漏（泄漏矩阵新增一行，见 §Key 不落盘）|
| T11 | `Reaper`（启动/定时/每次停止后按 label 清扫）+ 镜像预拉取带 WS 进度 | T10 | `services/reaper.py` | **2026-09-15 已拆成 T11a／T11b，拆法与理由见 §下一步 第 0 条**（T11b 改判模板 3；原定排到 T14 之后，2026-09-16 按用户指令改为先做，见 T11b 行）。下面这一整段规格属于 **T11a**。<br>**2 —— ⚠️ 破坏性操作**（判据见上方「两条容易搞反的」）。prompt 必须写死：**`label=strix-run-type=console` 且 `strix-run-id` 非空**的双条件过滤、先 `--dry-run` 打印、禁止按"名字像"或"时间早"删。⚠️ **run-id 那一半不是可选的** —— M0 靶场被手打了同一个 `strix-run-type=console`（2026-09-11 实测），只按前者会删掉它；加上后者永不误伤真沙箱（`docker_client.py:113` 早退）。直接复用 `docker_probe.ORPHAN_LABEL_SELECTOR`／`ORPHAN_REQUIRED_LABEL`，别再抄字面串。<br>**T10 交回两件**：① 清扫触发点里的"每次停止后立即扫一次"—— 强杀路径会打一行 `event=sandbox_cleanup_skipped`（带 `scan_id` 与 `strix_run_id`，两者相等）的 warning，那是 Reaper 与 T26 诊断页的对账凭据；② **不许碰 `${DATA}/scans/<id>/tmp`** —— 它的清理已归 T10（`cleanup_scan_tmpdir`），而它的**同级** `strix_runs/` 是 T13 还要读的产物，两边都删就会出现"Reaper 删了一半、报告读到半截"的竞态 |
| T11a | `Reaper` 本体（三级判据 + 三触发点 + dry-run） | T10 | `services/reaper.py` | 2 —— **2026-09-15 已落地并复验**：`services/reaper.py`(219) + `tests/test_reaper.py`(385，16 条) + `docker_probe.py` 新增 `OrphanSandbox`／`orphan_sandboxes()`（`orphan_sandbox_names()` 退化成一行推导式，过滤逻辑与两个 label 常量仍只有一处）+ `scan_supervisor.py` 的 `on_scan_finished` 缝（`_forget` 里 **pop 之后**才通知，否则刚结束那次扫描会 spare 掉自己一整轮）+ `main.py` 接线 + `FakeTransport`／`RecordedCall`／`reply`／`const` 从 `test_docker_probe.py` **搬进** `conftest.py`。官方闸门 **753 passed / 0 skipped**、ruff/eslint/tsc 干净。<br>**复验（主会话自己做）：9 次 mutation，9 次都只有该红的那几条红** —— 去掉第二级 run-id 判据（红 3 条，含既有的两条 docker_probe 与新的 `test_m0_juice_shop_is_neither_doomed_nor_deleted`）／`plan_sweep` 忽略 active／忽略 `dry_run`／`sweep()` 不快照在册 id／`run_forever` 不接 `DockerApiError`／把 500 当删除成功／`main.py` 不起 `run_forever` 任务／停机不 cancel+await／`_forget` 不通知。**这是连续四个任务里第一次"接线测试真的有效"** —— 收货清单第一条（每处接线要有会因删掉那次调用而变红的测试）写进 prompt 并**要求它自己先删一次跑一遍**，是有效的做法，继续这么派。<br>**子 agent 报告里值得留的一条**：`reaper_task.cancel()` 那处的第一版测试**没红**（`asyncio.run` 收尾本来就会 cancel 掉剩余任务，所以"cancelled 为真"恒成立）—— 改成断言**顺序**（用 `Database.close` 打点，取消必须在 `db.close()` 之前）才红。写"停机顺序"类断言时别断言状态，断言顺序。<br>**主会话读代码补的一处**：`sweep()` 的快照发生在列容器**之前**，安全但**理由是时序不是结构**（登记先于沙箱创建几秒到几十秒），已把论证与"哪天登记挪位置就必须改成先列后快照"写进 `sweep()` 的 docstring。<br>**mutation 顺带发现**：拿掉 `run_forever` 的 `except DockerApiError` 会让 **108 个无关测试连带报错** —— 清扫任务带异常死掉后，停机时 `await reaper_task` 把它重抛进 lifespan 的 `finally`（`suppress` 只接 `CancelledError`）。那个 except 比注释里写的"只是少一个后台清理"承重得多；`sweeper` 也是同一形状，属既有约定，不改。<br>**三处判断题的结论**：① `Id` 缺失时**退回用容器名当删除地址**（`/containers/{id}` 端点同样接受名字，什么都没降级；丢弃或报错都会让既有夹具红，而那些文件不在白名单里）；② `REMOVE_TIMEOUT_S=20.0`（`force=1` 要先 KILL 再删，比只读查询慢一个量级，不能沿用 8s）；③ `Reaper`↔`ScanSupervisor` 构造成环，用晚绑定闭包 `lambda: supervisor.active_scan_ids()` 破环，理由已就地写明。<br>**交给 T30 的一个安全问题（不是可选项）**：`make reap` 的 CLI 在 `api` 进程外，拿不到在册扫描，`sweep_sync(frozenset(), dry_run=False)` 会让第三级判据整个失效、删掉正在跑的扫描的沙箱。**要么让 CLI 走 `api` 拿在册 id，要么 `api` 在跑时只允许 `--dry-run`。** 另需一个 CLI 入口（拼 `UnixSocketTransport()` + `Reaper(...)` 后直接 `sweep_sync`，同步、不需要事件循环）。<br>**交给 T26 的日志字段**：`reaper_sweep_plan`（`dry_run` + `doomed[]`／`spared[]`，每项 `container_id`／`container_name`／`strix_run_id`）、`reaper_removed`、`reaper_skipped`、`reaper_remove_failed`(+`reason`／`status`)、`reaper_sweep_failed`(+`reason`)。`strix_run_id` 与 `ScanProcess._stop()` 的 `sandbox_cleanup_skipped` 同名，可直接对账；**还没有 HTTP 出口**，程序内读 `app.state.reaper`。<br>**小坑**：`pyproject` 的 addopts 已含 `-q`，快闸门命令后面再加 `-q` 会变 `-qq` 而吞掉最后那行计数 —— 别加。 |
| T11b | 镜像预拉取带 WS 进度 | ~~T14~~（2026-09-16 用户指令改为**先做**，见 §下一步 第 0 条）| `ws_envelope.py` `services/image_puller.py` `routes/system.py`（＋`docker_probe.py` 加流式方法、`models.py`、`audit.py` 一个常量、`main.py` 接线、`conftest.py` 的 `FakeTransport`）| **3**（2026-09-15 由 T11 拆出并从 2 改判为 3）—— **2026-09-16 方案已由主会话写、用户放行、实现 agent 已派发**（方案原文＝派发 prompt 本身）。**主会话在 prompt 里替它拍死的七件**：① `POST /images/create` **失败时 HTTP 仍是 200**、失败只体现为正文一行 `{"error":...}`（与 pitfalls 条 24 同源），必须有专门测试；② **不新增任何错误码、不改 `errors.py`／`zh-CN.json`** —— 失败经 WS 帧带已存在的 `failed_to_pull_image`（文案正好写着"在本机诊断页点重新拉取"）；③ 重复 POST **幂等返回当前状态**，不发 409、不起第二个任务；④ `percent` 在任一层 `total` 未知时**必须是 `None`**，不许猜；⑤ **`/ws/system` 不能挂在 `prefix="/api/system"` 那个 router 上**（会静默变成 `/api/system/ws/system`）→ 同文件第二个无前缀 router ＋ 一条断言确切路径可连的测试；⑥ **不实现 `hello`／`resume`**，也不许预留扩展点；⑦ 审计只在真起了任务时写一条 `image.pull_started`，`already_present` 那一支不写。节流（250ms／1%／阶段变化，终态无条件立即发）要求**时钟可注入**，否则测不了。prompt 里给它的 7 处 mutation：M1 删 error 行检查／M2 把 `/ws/system` 加进 `EXEMPT_PATHS`／M3 `total` 未知时 percent 算 0／M4 节流阈值归零／M5 `already_present` 也起任务／M6 shutdown 不取消／M7 `split_reference` 用朴素 `split(":")`。 它建**全项目第一个真实 WebSocket 端点**，信封约定会被 T14／T16 继承 —— 所以本行是那套约定的**定义方**（原写"排在 T14 之后或与它合并"，2026-09-16 作废）。**WS 上的 cookie 鉴权不在本行范围**：T4b 已完成，本行只补 §端到端验收 27 要求的那一格断言（匿名 `/ws/system` → 401、`/api/health` 仍 200）。`DockerProbe.image_facts()`（T3，只查不拉）是它的现成前置。<br>**✅ 2026-09-16 收货（主会话自己做的三件，§八.3；含修复，修完全部复验）**：交付物 `services/image_puller.py` 497 行 + `ws_envelope.py` 70 行（单独成文件是用户当场拍的）+ `tests/test_image_puller.py` 348 行 + `tests/test_routes_system_pull.py` 181 行；改动 `docker_probe.py`（加 `stream_ndjson`，并把 6 个 `reason` 的映射提成两个方法**共用**的 `_translate`）／`routes/system.py`（第二个无前缀 `ws_router`）／`models.py`／`audit.py`／`main.py`／`conftest.py`（`FakeTransport.streams` 一张独立的表，"没准备就 AssertionError"的立场不变）。**上面那七件全部对着代码逐条核过**（不看它的自陈，§八.3）；偏离只有一处且是好的：`already_present` 落成 `status="done"` + `phase="already_present"`（`status` 保持四个值，导出 `ALREADY_PRESENT_PHASE`）—— 给 `status` 加第五个值会让前端多一条分支而不多一点信息。<br>**① 官方闸门**：`make lint` 全绿（ruff check／format 65 文件、eslint、tsc），`make test` **838 passed / 0 skipped**（基线 804，**+34**。派发 prompt 里写的基线 780 是错的 —— 那是 T12a+b 之后的数，T12c 已经把它推到 804）。<br>**② 自己设计 15 处 mutation（一个脚本一轮跑完，每处跑全量套件）**：8 处被杀且血溅范围精确（删 `include_router(ws_router)`／删 lifespan 建 puller／`_puller` 每次新建实例／审计改成无条件写／审计 `detail` 里塞一个凭据字段／不认正文里的 error 行／用已知层的和冒充总量／节流去掉「没变就不发」），**7 处存活**。其中 **5 处是真空白，已各补一条测试并复验"只红该红的那一条"**：(a) 删 `main.py` 停机那句 `image_puller.shutdown()` → 补 `test_lifespan_shuts_the_puller_down`；(b) WS 端点不再监听客户端断开（后果：每关一个页面就在服务端留一个永远等在 `wait_for_change` 上的任务）→ 补 `test_a_closed_page_leaves_no_task_behind`，经 `TestClient.portal` **在 app 那个事件循环里**数 `asyncio.all_tasks()`、2 秒内回落即通过；(c) 快照帧发一个新 `seq` → 补 `test_snapshot_reuses_the_current_seq`；(d) `stream_ndjson` 自己写一份异常映射（`_translate` 的存在理由一条测试都没守）→ 补 `test_docker_probe.py::test_stream_ndjson_maps_failures_the_same_way`；(e) `Sequencer` 换 epoch 不归零（**这是 T14／T16 要继承的信封约定**）→ 新建 `tests/test_ws_envelope.py`。另 2 处存活**留着不补、记为已知空白**：WS 端点里"先取游标再发快照"调换（丢帧窗口，没有注入点、观测不了）；`wait_for_change` 那两行顺序调换是**等价变异** —— 两行之间没有 await，单线程循环里插不进一次发布，承重的其实是"每次发布换一个新 `Event`"，那段把顺序说成竞态防线的注释已按实测改写。<br>**③ 读代码抓到一处交付里没有、也不在任何 mutation 清单上的真缺陷（已修）**：`ImagePuller.start()` 是 check-then-act —— `is_pulling()` 与 `create_task()` 之间有一次 await（查镜像在不在本地），**两个同时到达的 POST 会各起一次几 GB 的拉取**（实测 `/images/create` 被调 2 次）、各写一条审计，而 `self._task` 只留得下后一个，前一个成孤儿却仍在往状态里写。修法：`start()` 整个函数进一把 `asyncio.Lock`（锁只在"查镜像"那几毫秒里持有，真正的拉取在另一个 task 里，两个请求不互相阻塞），新增 `test_two_simultaneous_starts_launch_only_one_pull`。**同一处的第二半也修了**：路由原本在锁外读 `before = puller.state.epoch` 再比对，第二个 POST 会看到**第一个**推进的 epoch，于是同一次拉取被记两条 `image.pull_started` —— 改成 `start()` 在自己的锁里返回 `(state, started)`，路由只看 `started`（钉死的决定 ⑦ 因此在并发下也成立）。<br>**方法学（同一件事第五次被实测）**：闸门全绿 + 清单上那 7 处 mutation 全对，仍然漏了 **5 处接线空白 + 1 处并发缺陷**。两条新判据：① **"先后 `await` 两次"证明不了幂等** —— 窗口就在那次 await 上，这类测试必须用 `gather`（旧的顺序版在没有锁时也是绿的）；② **"这一次到底做了什么"的布尔不许让调用方自己从状态里推**：推出来的那一份不在锁里，于是审计会记下一件没发生的事。<br>**派发预算复算（`scripts/agent_budget.sh`）**：实现 agent 压缩 1 次 → 按 §九.6① **判失败**，但根因不是这一行的粒度：花费的大头是它自己 50 多轮的思考与散文（其次是反复的 pytest 输出），**在一个不小的地板上重复付费**。真要再拆就按接缝切成 (i) `stream_ndjson` + `ImagePuller` + 服务层测试、(ii) `ws_envelope` + 两个端点 + WS 测试 + 接线；另外 **mutation 自检该派一个用完即弃的 agent**（它不需要任何设计上下文），红条 pytest 用 `-x --tb=line`。 |
| T12a | **`ScanAdmission`**：授权重校验 + DNS 重解析比对 + 逐字确认串比对 + `required_opt_in` 缺勾，四条判定收成**一个无 IO 纯函数**（DNS 结果由调用方注入）→ 「通过」或「稳定机器码 + `params`」（`authorization_id NOT NULL` 由 T2 的表约束兜底）| T9 | `services/scan_admission.py` `services/audit.py` `tests/test_scan_admission.py` | **2** ✅ 2026-09-16（由 T12 拆出，理由见 §下一步）—— **T8 交回三件事**：① `target.rejected`／`dns_changed` 两个审计事件**加在 `services/audit.py` 上**，不要造第二个写入方；② `services/dns_resolver.py` 的 `resolve_sync` errno→码映射（`dns_not_found`／`dns_timeout`／`dns_failed`）**目前没有测试**，真要依赖它就在这里补一个 monkeypatch 用例；③ `GET /api/allowlist` 现在把 `owner`／`authorization_ref` 原样返回 —— **2026-09-16 确认不做字段过滤**（只有一个账号），但**一旦出现只读操作员角色就必须做**。<br>**服务端重校验的拒绝出口（2026-09-12 拍板，T8 交回 ③ 的答案；两条出处已在代码里：`routes/targets.py:5-8` 与 `target_guard.py:122-124`）**：按"性质"分两类，**不新增 `target_rejected` 码** —— 8 个 `RejectionReason` 各编一个 HTTP 码会得到 8 个永不当响应码用的"错误"（`errors.py` 模块 docstring），合成 1 个新码又与 `invalid_request` 语义重叠。**本行只产出「码 + `params`」，码→HTTP status 的透出在 T12c**。<br>· 规范化失败（`RejectionReason`）→ **422 `invalid_request`**，`params = {field:"targets", index:<下标>, reason:<RejectionReason>}`，**遇到第一个就拒（fail-fast）**：`ParamValue` 只许 JSON 标量（`errors.py:38-40` 刻意不许 dict／list，防止有人往里塞整个请求体进而塞进凭据），逐条列全就得先破那条约束或给错误响应加第四个字段，而这条路径本该被向导第 1 步的 `validate` 拦住，不值得为它付这笔账。**`raw` 绝不进 `params`** —— `user:pass@host` 的原文回显只许存在于 `validate` 的 200 正文那一处（T30a 已如实记录那一条残余风险，别扩大它）<br>· 缺勾选（`required_opt_in` 非空）→ 同样 **422**，`params = {field:"overrides", index:<下标>, missing_opt_in:<OptInFlag>}`<br>· 护栏策略拒绝 → 保持 `GuardVerdict.error_code`：`blocked_metadata` **403**（不可覆盖），`not_in_allowlist`／`split_horizon`／`dns_changed` **409**（可以改状态再来）<br>· 前端（T18）：`params.reason` 在时去 `targetGuard.reasons.*` 取那句人话并高亮第 `index` 行，不在时退回 `errors.invalid_request` 的通用文案。<br>**文案不在本行范围**：`zh-CN.json` 的 `errors.invalid_request.params` 与 `paramLabels` 三个新键（`index`／`reason`／`missing_opt_in`）**由主会话在本行收货时自己补** —— 3 行 JSON，派 agent 去读 `zh-CN.json` 更贵；把"文案"这一维拆掉正是 `agent-rules.md` §九.5 的目的。<br>**主会话在 prompt 里定死的七件（2026-09-16 派发时决定，不是 agent 自己选的；收货逐项核）**：① 契约是**联合返回** `AdmissionPassed | AdmissionRejected`，**不是**"一个带 `rejection: X | None` 的大 dataclass" —— 后者有一条"忘了检查 rejection 就拿到 targets"的静默放行路径，而本项目已栽过三次"声明了却没有任何一处强制"；② **本次解析失败复用 `dns_changed`**（不加新码），`params` 多一个 `resolution_error`（`ResolutionError` 的三个值）—— 声明时解析出 N 个、现在 0 个，"与声明不一致"是真的，且处置动作与 rebinding 完全相同（回向导第 1 步）。**因此文案是四键不是三键**；③ **DNS 比对按集合、不按顺序**：`getaddrinfo` 顺序受 RFC 6724 与轮转影响，按元组比会让每个做轮询的域名每次都假报一次 rebinding。有序那份留在 `authorizations.resolved_ips_json` 当证据 —— "存"与"比"是两回事（`dns_resolver.resolve_sync` 那句"排序会丢掉一个真实的变化信号"说的是存）。且**只对 `not is_ip` 的目标比对**；④ **逐字确认串的期望值 = 第一个目标规范化后的 `host`**（`registrable_domain` 已定不做，所以就是 host 本身），比对规则 `strip().casefold()`，`params={}`（期望串前端已灰显在输入框旁边，后端再说一遍是把同一份信息造两份）。**多目标的"以上 N 个均已授权"没有存储列 → 留给 T12c 拍**（v1 由前端强制，还是挤进 `overrides_json`）；⑤ **判定顺序**：规范化 fail-fast → 逐目标按请求顺序（取地址／解析失败／DNS 比对／`verdict.error_code`／`required_opt_in`）第一个失败即返回 → **逐字确认串放最后**（目标本身还没过护栏时要求用户逐字打它是无意义的）。**刻意不做严重度阶梯**（跑全部目标再挑最重的）：metadata 被另一个目标的可修错误"挡住"时那次扫描**照样被拒**，没有不变式被破，而阶梯是多出来的代码；⑥ `resolutions` 缺键 → **抛 `ValueError`**（与 `evaluate_target` 对同一件事的处理一致：静默当成"没地址所以放行"是最糟的失败模式）；`allowed` 为假而 `error_code` 与 `required_opt_in` **都空** → 也抛 `ValueError`（护栏契约被破坏，响亮 500 优于静默放行）；⑦ 审计两个事件名定为 **`target.rejected`／`target.dns_changed`**（§护栏 那张事件清单里原本没有 dns 事件，点分命名与既有三个一致），由 `scan_admission.audit_for(rejection) -> (事件名, detail)` 这个纯函数产出 `detail={"code",…,"host"}`，**真正的 `record()` 调用在 T12c**。`AdmissionRejected.target_host` 是**已规范化的 host**、**只进审计 detail 不进 `params`**（`raw` 的原文回显只许存在于 `validate` 的 200 正文那一处）<br>**✅ 2026-09-16 收货**：`services/scan_admission.py` 236 行 + `tests/test_scan_admission.py` **18 个用例** + `audit.py` 两个事件常量。上面七件**逐项核过、全部落地**。主会话侧 5 处 mutation 全被杀且只红该红的（见 §下一步）。**两处偏离，都收下了**：① 额外为 `raw_targets` 为空抛 `ValueError` + 一条测试 —— 与第 ⑥ 条同型（静默"通过且零个目标"会让 T12c 起一次没有目标的扫描），**因此请求模型的 `min_length=1` 是 T12c 的义务**；② `test_declared_addresses_compare_as_sets` 与 `test_typed_confirmation_is_stripped_and_casefolded` 两条在骨架阶段是**绿**的（它们断言"应当通过"，而骨架恒通过），红是从 M1／M6 那两处 mutation 来的 —— agent 如实报了这件事而不是宣称全程合规，**这类"正向断言"测试天生拿不到功能缺失那种红，它的证据只能是 mutation**（§八.3② 的直接测量正好覆盖这个缺口）。预算：27 次调用、第 9 次开始写代码、无压缩，三项都在 §九.6 之内|
| T12b | **`ScanSecretRegistry`**：把 `spec.test_credentials` 的值注册进 Redactor 的精确子串集合（泄漏矩阵 **#17 结案**）| T9 | `services/scan_secrets.py` `tests/test_scan_secrets.py` | **2** ✅ 2026-09-16（由 T12 拆出）—— **T10 交回 ②**：注册**每一个值**（口令与 Key 走同一个精确子串通道；§N1 那条"一个 handle 可能装 2–3 个值"同理）、扫描终态时注销；并给 `scan_supervisor` 那行带 `stdout_tail` 的 warning 补一条"那行日志真的被脱敏了"的测试 —— 现在**没有**测试钉住它（泄漏矩阵第 17 行就是这个洞）。**脱敏点只有一处**是本项目的设计，别在这里挂第二个 Redactor 副本。<br>**主会话在 prompt 里定死的五件（2026-09-16 派发时决定；收货逐项核）**：① **只登记 `password`，不登记 `role`／`username`** —— 它们不是秘密，而报告里要说"用这个账号登录时发现了…"，把账号名塞进脱敏集合是可读性损失换零安全收益；但要登记**每一条** `TestCredential` 的口令，不是只第一条；② **组合发生在构造处**：`ScanSecretRegistry(base: SecretProvider)` 包住 `key_vault.secret_values`，对外只暴露**一个** provider 交给唯一的 `Redactor`。刻意不在 `main.py` 写 `lambda: a() | b()` —— 那样"两个来源"这件事就没有可单测的落点（`logging_setup` 一行不改）；③ **`secret_values()` 必须先 `tuple(...)` 取快照再遍历**（provider 在工作线程里被调，`register`/`forget` 在事件循环线程；否则 `RuntimeError: dictionary changed size` 会被 `Handler.handleError` 吞掉 → **那一行日志整条消失**）。判据是 `key_vault.secret_values()` 的 docstring，它对同一件事已经这么做了；④ **`MIN_SECRET_LENGTH` 不许动、不许在这里再加一层长度校验**；短口令（<8）不被脱敏是**已知且接受**的事实，要求有一条测试把它**明写**下来，免得下一个人以为是漏了。请求体该不该拒短口令是 T12c 的请求模型的事；⑤ 那条 `stdout_tail` 测试**必须同时断言 JSON 行里有 `stdout_tail` 字段**（证明那行 warning 真的发生了）+ 值里含 `[REDACTED]` 且不含口令原文 —— 只断言"口令不在输出里"的话，warning 根本没打出来时它会照样通过（pitfalls 条 23：「检查都通过」≠「被检查的事真发生了」）。<br>**接线归 T12c**：`register()`（扫描启动）／`forget()`（终态）在本行交付时**没有生产调用方**；接线之前口令会一直留在脱敏集合里<br>**✅ 2026-09-16 收货**：`services/scan_secrets.py` 79 行 + `tests/test_scan_secrets.py` **9 个用例** + `main.py` 只动 lifespan 那 3 行（diff 逐行核过，`logging_setup.py` 一行没改）。上面五件**逐项核过、全部落地**。**收货时主会话补了一行文档**：`main.py` 模块 docstring 的启动顺序表第 2d 步原来只写 `KeyVault()`，现已写成 `KeyVault() + ScanSecretRegistry()` 并说明"包住"—— 那张表是"启动顺序是设计的一部分"的唯一记录处，落后一个组件就等于骗下一个读它的人。**一个已知的测试空白（agent 如实报的，主会话确认）**：把 `secret_values()` 里那次 `tuple(...)` 快照删掉，**没有任何测试变红**。它防的是"事件循环线程 register／forget vs 工作线程 redact"的竞态，确定性地测它要么往生产代码插同步点、要么靠概率性压测，两者都比它防的问题更糟。**`key_vault.secret_values()` 有完全相同的空白**（同一条判据、同样没测），所以这是一条**跨两个模块的已知缺口**，不是本行的疏漏 —— 谁哪天动这两处任一，判据在各自的 docstring 里，别当防御性写法删掉。预算：34 次调用（prompt 给的是 ≤30，**超 4 次**）、第 8 次开始写代码、无压缩 —— §九.6 的三条门槛都没破，但**超支原因值得抄进下一份 prompt**：prompt 贴了 `scan_supervisor` 那行 warning 的原文，却没贴**被断言的那个 dataclass 的字段名**（`ScanOutcome`）、`CredentialSet` 的构造参数、以及"`restore_logging` 能不能被别的测试文件请求"，这三样是写测试的必需品，agent 为它们花了 3 次调用（§九.1「只给文件名而不给摘录，它必然整读」的一个新变种：**只给生产代码的摘录而不给它要断言的那个类型的字段名，它必然去读**）|
| T12c | `POST /api/scans`：并发槽位 + `ScanSupervisor.start()` 调用方 + 状态与 `ScanOutcome` 落库 + 审计 + 停止端点 | T12a T12b | `routes/scans.py` `main.py` `tests/test_routes_scans.py` | **3**（2026-09-16 由 T12 拆出；方案由主会话写，`agent-rules.md` §四 模板 3）—— **三条 2026-09-16 用户拍板**：① **并发槽位 = 1**，且**槽位数参数化进 `.env`、默认 1**，不许写成结构性单例（沙箱四个限额是按本机 VM `11 核 / 7.75 GB` 算出来的，两个并发扫描互相饿死；"一进程一次扫描"约束的是 strix 库、不是我们，>1 技术可行只是这台机器扛不住）；② **`ScanProcess.stop(force=True)` 留**，做成"再点一次立即强杀"（T10 已写好，删了 T26 诊断页就没有兜底手段）；③ **`interrupted_by_restart` 保持一码两因、只改文案**，v1 不拆第二个码（将来要拆，判据是 `stopped_by == "shutdown"` 而非 `run_status`）。<br>**T10 交回的其余三件**：① **把 supervisor 接进路由** —— `app.state.supervisor` 已在 lifespan 里建好、停机 `shutdown()` 已接，本行要做的是 `start()` 的调用方、并发闸、以及 `ScanProcess.wait()` 回来后**把 `ScanOutcome` 的 8 个字段落进 `scans` 行 + 写审计**（T10 只算出它，刻意不落库）；② `starting`／`running` 两个状态值 T10 永不返回，**它们的写入点在这里**；③ `scans.strix_run_name` 由 `RunDiscovery` 抢到后放在 `ScanProcess.run`，**落库也在这里**。<br>**把 T12a 的「码 + `params`」透出成 HTTP status**（422／403／409，映射判据在 T12a 行），本行**不新增错误码**。<br>**T12a／T12b 派发时交回本行的四件（2026-09-16）**：① `scan_admission.audit_for()` 与 `ScanSecretRegistry.register()`／`forget()` 三个函数在那两行交付时**都没有生产调用方**，接线全在这里 —— 按收货清单第一条，**每一处接线都要有一条会因为删掉那次调用而变红的测试**；② 多目标的"以上 N 个均已授权"**没有存储列**（`affirmations_json` 被 CHECK 钉死 = 3），本行要拍：v1 只由前端强制、还是挤进 `overrides_json`；③ 逐字确认串的期望值是**第一个目标的 host**，请求模型的 `targets[]` 顺序因此是有语义的，不许在路由层排序或去重后重排；④ `resolutions` 缺键与"`allowed` 为假而两个原因都空"在 T12a 里都是 `ValueError`（→500），本行调它之前必须自己先把每个主机名解析一遍并填齐 mapping<br>**✅ 2026-09-16 收货（主会话自己做的三件，§八.3；含修复，修完全部复验）**：交付物 `routes/scans.py` 854 行 + `routes/_context.py` 50 行（取 `app.state` 的三个小函数，从 `routes/allowlist.py`／`keys.py` 抽出来共用）+ `tests/test_routes_scans.py` **23 个用例** + `test_audit.py` 补 1 条（双写真的落两处）+ `services/audit.py` 拆成 `prepare()`／`insert(conn, prepared)`／`mirror(prepared)`（**ndjson 镜像只在事务提交之后写**）+ `main.py` 启动步 6b 与停机 drain + `errors.py`／`zh-CN.json`。上面方案阶段那六件、三件旧拍板、T10／T12a／T12b 交回的七件**逐条核过、全部落地**（多目标那件选了"挤进 `overrides_json` 的 `multi_target_affirmed` 且后端强制"）。<br>**① 官方闸门**：`make lint` 全绿（ruff check／format 60 个文件、eslint、tsc），`make test` **804 passed / 0 skipped**（基线 780，**+24**）。<br>**② mutation 11 处，全部被杀且血溅范围逐条精确 —— 但交付时有 3 处是全绿的**：**M4b**（`finally` 里只删 `vault.release`、`forget` 留着）、**M7**（删起进程失败时的 `_mark_failed_to_start`）、**M8**（删事务提交后的 `audit.mirror`）。收货补的三条接线测试：`test_credentials_are_forgotten_when_the_scan_ends` 改成注入 `FakeClock` 的 vault，扫描跑完后**推进 idle TTL 再断言 `get(handle) is None`** —— `get()` 不反映 `ref_count`，"`release()` 真的发生过"唯一可观测的后果就是空闲 TTL 重新起算；新增 `test_a_failure_to_spawn_marks_the_row_failed`，用上了交付时**没有调用方**的 `FakeSupervisor.start_error` 钩子（`pytest.raises(OSError)` 拿得到原异常：Starlette 的 `ServerErrorMiddleware` 出完 500 正文还会重抛）；`test_launch_writes_authorization_scan_and_audit` 补 ndjson 镜像断言（两条事件名按序 + `scan_id` 一致）。另补的 M9／M10 也各红一条。<br>**③ 读代码修了四件**：(a) `test_status_walks_starting_running_terminal` 拿"终态状态"当审计写入信号，而 `mark_finished` 先提交、`audit.record` 后走 —— **实测 13 次里红 2 次**，还让 M1 的血溅范围虚报成 3 条；改成先等 `audit_log` 里出现 `scan.finished` 行，改后**连跑 6 次全绿**、M1 收敛成该红的那 2 条。**竞态在测试侧也是缺陷，交付报告里的"全绿"只代表那一次**；(b) 拍板 ③ "只改文案"那件**漏做了**：`zh-CN.json` 的 `interrupted_by_restart` 补上"控制台重启，或者它的进程被强制终止（docker kill、掐电、机器重启）"，一码两因这件事现在文案里也说了；(c) 槽位数只读 `Settings`，**没进 `docker-compose.yml` 也没进 `.env`** —— "参数化"只做了一半（用户拍板"compose 补一行 + `setup.sh` 写进 `.env`"）：`x-console-env` 加 `CONSOLE_MAX_CONCURRENT_SCANS: "${CONSOLE_MAX_CONCURRENT_SCANS:-1}"`，`setup.sh` 加 C18b（纯数字 + ≥1 校验，取值优先级照 `CONSOLE_WEB_PORT`：env > 已有 `.env` > 默认 1）并写进生成的 `.env`；`docker compose -p strix-console config -q` 通过、渲染出 `"1"`，`bash -n setup.sh` 通过；(d) `allowlist_snapshot` 只存了逐目标结论，而 `001_init.sql:64` 要的是"当时该条目的完整快照"（用户拍板"存完整条目"）：按 `decision.entry_label` 反查存整条 `model_dump(mode="json")` —— 反查是**一次 dict 取值、不是第二份匹配逻辑**（`AllowlistConfig._labels_are_unique` 保证标签唯一）。这一处踩了一次：`_store(request).current` 返回的是 `AllowlistSnapshot` **不是** `AllowlistConfig`，第一版改完 11 条红。<br>**两处结构性收紧（读代码时决定的，不在交付里）**：① `try:` 上移到 `acquire()` 之后第一行，把 `TestCredential` 构造与 `scan_secrets.register()` 也圈进去 —— 否则这两行任一抛异常，那份凭据的 `ref_count` 就一直挂着直到 24h 硬 TTL；② `launched = True` 下移到 `create_task(_run_to_completion(...))` 紧前一行，也就是所有权交接的那一刻 —— 早置一行就意味着某次抛异常时 `finally` 会去 `forget()` 一个**正在跑**的扫描的口令，那是泄漏面而不是清理。<br>**一处提取（§十.3 的"第三次"）**：`FakeClock` 从 `test_key_vault.py`／`test_auth.py` 两份副本提到 `tests/conftest.py`，本行新测试是第三个用户。<br>**方法学结论（同一件事第四次被实测）**：闸门全绿 + 清单上六条 mutation 全对，仍然漏了**三处"接线一条测试都没有"** + 一处会让 CI 随机变红的竞态。抓到它们的唯一手段是**自己补设计 mutation**（M4b／M7／M8 都不在清单上），而 M4b 还给出了一条新判据：M4 删 `forget+release` 会红、只删 `release` 就不红 —— **粗粒度 mutation 会把细粒度的空白掩掉，以后按"每一次调用"设计 mutation，不是按"每个 `finally` 块"**。<br>**验收脚本本身的坑（已记进 `pitfalls` 触发条件 35 一带）**：测试镜像的 addopts 里已有 `-q`，再传一个就成 `-qq`，pytest **不再打那行汇总计数** —— 之前每次 mutation 都报"没拿到汇总行"就是这个原因，循环里必须同时打退出码与计数行。<br>**派发预算复算（2026-09-16，`scripts/agent_budget.sh`）**：这一条任务**填满了三个上下文窗口、压缩 4 次**（交底 1／实现 1／收货 2），而子 agent 只占总花费的三成 —— **粒度不是问题，交底与收货的方式是**（它要 16 个模块的签名才能动手，按层再拆每一半仍要那 16 个）。三条已落成规则：交底不许整读源码（§九.1）、收货整读产物别拼 `sed` 片段（§八.3）、单文件别用几十次小 Edit 堆（§十.6）；根因与量具见 `pitfalls` 条 41 |
| T13 | `RunProjector`：epoch + 三信号重同步 + elision 识别（**全项目最难的一块**）| T10 | `services/run_projector.py` `strix_bridge/{projection,paths,catalogue}.py` | **3** —— **T14／T15／T16／T21／T29 五个任务挂在它后面**，且它定义 `strix_bridge/` 的 import 边界 |
| T14 | `EventMirror`（截图首见即落地 `media/`）+ `LogTailer`（脱敏在推流前）+ `ScanChannel` | T13 | `services/{event_mirror,log_tailer,channel}.py` | 2 —— **2026-09-18 按 §三.2 拆成三条**：**T14a** = 纯函数 `services/event_media.py`（抽 data URL、算 `sha256`、改写成 `/api/scans/{id}/media/<sha256>.png`，**不写库不落盘**，任务书 `/tmp/T14a-prompt.md`）；**T14b** = `services/log_tailer.py`（任务书 `/tmp/T14b-prompt.md`）；**T14c**（未派）= `services/event_mirror.py` 落盘＋写 `scan_events`／`scan_media`，**被 `scan_events.strix_id` 的表结构缺陷卡住，见 §交接**。`ScanChannel` 与 `main.py` 接线归到 T14a/b/T28a 之后的那条串行接线任务。<br>**✅ 2026-09-18 T14a 收货（主会话三件事全做，一条自陈都没采信）**：`services/event_media.py` **151** 行 + `tests/test_event_media.py` **223** 行，新增 **28** 条用例。**2 处 mutation 只红该红的**：递归改成只看顶层 → 红 9（4 个嵌套形状 + 双图 + `non_png` 4）；`sha256` 改成对 base64 原文取 → 红 18（恰好是"有 PNG 被成功抽出"那 18 条，`undecodable` 6 与 `non_png` 4 不红）。<br>**整读之后认下的三处**：① **空／只有空白／只有 padding 的 base64 判 `undecodable_base64`、不产 0 字节图**（任务书没规定，子 agent 自己定的，我认可 —— 否则 T14c 会往 `media/e3b0c442….png` 写一个空文件）；② **正则的 base64 续行只认换行、不认一般空白** —— 收进一般空白**已实测出 bug**：`"a <urlA> b <urlB> c"` 里第一个匹配会吃掉 `= b data`（`b`／`data` 全在 base64 字母表内），于是两张图一张都抽不出来还误报 `undecodable_base64`。理由已写进 `_DATA_URL_RE` 上方，**别去"简化"它**；③ ⚠️ **"序列化后零命中 `data:image`"只对全 PNG 且 base64 合法的 payload 成立** —— `data:image/jpeg;base64,…` 这个字面串本身就含 `data:image`，而契约要求非 PNG **原样保留**。**不许把它当全局后置条件**，否则会照字面"修"出一个把 jpeg 也删掉的实现。<br>**已知不做（都不是缺陷）**：`media_url()` 不提取（只一个调用点；测试里刻意写死那个形状、**不从生产代码 import**，否则改坏 URL 测试会跟着一起改 —— 等 T14c 的 `GET /api/scans/{id}/media/{sha}.png` 出现是第 3 个用处再提取）；递归无深度上限（payload 来自 Strix 自己 `json.dump`，受 json 递归限制约束；要兜 `RecursionError` 就兜在 T14c 的调用点）；单事件抽出的总字节数无上限（护栏放在写库那一半，只有它知道磁盘配额）；只下钻 `Mapping`／`list`（JSON 反序列化产不出 tuple／set）。<br>**✅ 2026-09-18 T14b 收货**：`services/log_tailer.py` **155** 行 + `tests/test_log_tailer.py` **266** 行，新增 **23** 条。**脱敏的落法是「整行原文先过一次 `redact`、再解析」**，这是设计要求不是实现细节：反过来（先解析、只脱几个字段）就要求我们枚举"哪些字段可能含凭据"，而 `strix.log` 里连 logger 名和 `agent_id` 都是 Strix 那边填的字符串。`[REDACTED]` 不含空格，所以整行替换不打乱格式。<br>**2 处 mutation**：① 改成"先解析、只脱 `msg`" → 红 **6**（`in_logger_name`／`in_agent_id`／`in_scan_id` × 两种凭据）。⚠️ **`in_unparsed_line` 那两个用例对这个 mutation 没有区分力** —— 未解析行的整行原文本来就进了 `msg`，所以"只脱 msg"照样把它脱干净了（子 agent 报告里列举的"红 8"是错的，实测 6；它文字里的 6 是对的）；② **`rfind(b"\n")` → `find`**（**我自选的靶子，不在它清单上**）→ 红 4（`append_returns_only_the_new_lines`、`shrunken_file_is_re_read_from_zero`、`min_level[debug]`／`[info]`）。<br>**已知不做**：`__init__` 对非法 `min_level` 抛 `ValueError`（3 行，防"配置拼错导致整条流静默为空"）**刻意没有专门测试** —— 一条编程错误不值一格；偏移不持久化（接线约束见 §交接）。<br>**✅ 2026-09-18 T14c 收货（三件事全做）**：`services/event_mirror.py` **180** 行 + `tests/test_event_mirror.py` **222** 行，新增 **8** 条（`make test` 1072 passed / 0 skipped）。**5 处 mutation 全部只红该红的那一条**：M1 把落盘挪到事务之后 → 红 `test_no_rows_when_image_landing_fails`；M2 `ON CONFLICT DO NOTHING` → `DO UPDATE` → 红 `test_known_sha_is_not_re_accounted`；**M3／M4／M5 是我自选的** —— M3 `fingerprint` 改成对**改写后**的 payload 重算 → 红 1（正路那条）；M4 `rsplit("_", 1)` → `split("_", 1)` → 红 1，⚠️ **这个靶子好使的原因是 `int("1_7") == 17`** —— Python 的数字下划线字面量会让"取第一个 `_` 之后"**静默算出一个合法整数**而不是抛；M5 去掉「临时文件 + 原子 rename」→ **红 0，如预期**。<br>**三条落库语义（交底时我拍板，这里是权威）**：① `data_json` 是三键信封 `{"key", "upstream_version", "data"}`，`version=1` 指的就是这个信封 —— `upstream_version` 按 T13 约定不占列，而直接混进 payload 会和工具自己的键撞名；`key`（`"tool_12"`）必须存得下来，因为 `kind` 存的是上游 `type` 字段（`strix_bridge/projection.py:98`），**不保证**是 key 的前缀，`f"{kind}_{strix_id}"` 复原不出来。② **镜像不按 `fingerprint` 去重，1 帧 1 行**：重同步会在 epoch+1 下整份重推、镜像照样重写一遍 —— 否则 `replay(epoch=E)` 拿不出完整快照，`resume_from` 就失去意义；DDL 注释里"用 fingerprint 判断这条我已经有了"说的是内存里的 `ProjectionState`，**不是这张表**。③ **先文件、后行**：孤儿 `<sha>.png` 是接受的不对称（内容地址化、留存清理删目录时带走），反过来会留下**指向不存在文件的行**。<br>**整读之后认下的两处**：① **落盘走 `<sha>.part` + `replace()` 原子 rename**（任务书只说了"文件已存在就跳过写"，没说怎么保证"存在的那个是完整的"）—— 半截文件会让 `exists()` 永久跳过重写、`bytes` 列与实际字节数永不一致。**M5 实测它没有任何测试兜着**（注入"写一半"太别扭），理由只写在代码注释里，**别去"简化"成 `target.write_bytes`**；② `rel_path` 拼 `f"{scans_dir.name}/..."` 而不用 `relative_to`（`scan_dir_for` 会 `resolve()`，macOS 上 `tmp_path` 的 `/var → /private/var` 符号链接会让它抛 `ValueError`）—— 代价是**依赖"`scans_dir` 就是 `${DATA}/scans`"这个约定**，见 §交接。<br>**已知不做**：`strix_id_of` 不挡"超出 int64 的巨大后缀"（会在 `conn.execute` 抛 `OverflowError`，与 `event_media` 那种"一条坏事件不许搞崩整轮"的处理不一致 —— 但 strix 的事件 id 是自增计数器，产不出这种 key）；一条事件里**两张不同图**、以及 `agent_id`／`ts` 为 `None` 都没有专门用例（`for` 循环 4 行、两列本来就 nullable）；没有 `replay()`／读取接口／`GET media/<sha>.png` 路由 —— 那是接线任务与 T16 的事。 |
| W2a | **`ScanChannel` 的纯函数一半**：`services/scan_frames.py` —— `plan_frames(previous, projection, snapshot, log_lines) -> (tuple[FrameSpec, ...], 新状态)` + `envelope_for(spec, *, epoch, seq, ts)`。**无 IO、不发号**（发号要与 `await mirror.append` 交错，属 W2b）。事件帧 payload = 镜像行的纯函数（`{key, kind, agent_id, ts, upstream_version, data}`，与 `EventMirror` 三键信封同源）；非事件维度自带「上次推了什么」的签名，内容没变不重推 | T13 T14a T14b T14c | `services/scan_frames.py` `tests/test_scan_frames.py` | **2** ✅ 2026-09-20 —— 方案在 §W2 方案（2026-09-20 放行）。**不变式：帧↔镜像行 1:1 同 seq、epoch 变则 seq 归零、同一内容不重推**。交付 185 + 317 行／18 用例，**子 agent 8 次调用、0 压缩**（预算 ≤40，`agent-rules.md` §九.6 的形状又一次成立）。收货：闸门 1086 → **1104 passed / 0 skipped**；**6 处 mutation**（3 处照清单：重同步改成一帧带全部事件红 2、三个签名短路成每轮都推红 5、去重键去掉指纹退路红 2；3 处我自选：summary 的比较串线到 `agents_fingerprint` 红 4、`vuln_keys` 不带上一轮红 2）。**自选的 MX5「事件帧直接带 payload」红 0 —— 本轮唯一真缺陷**：`FrameSpec` 的契约「`event` 非 None ⟺ `payload` 为 None」只在 resync 那条路径被断言过，`added`／`updated` 常规路径上没有任何测试盯着，而破坏它正是设计里写明要防的那个 bug（WS 帧带 base64、与回放不一致）→ 补 `test_event_frames_never_carry_a_payload`。**判据 ④（字面值）另抓一处**：`envelope.v == PROTOCOL_VERSION` 钉不住"这个常量是几"，全仓没有任何测试盯 `PROTOCOL_VERSION == 1` → 补进 `tests/test_ws_envelope.py`（它的归属地）。两条各只红自己一条，终态 **1106 passed / 0 skipped** |
| W2b | **tick 循环**：`services/scan_channel.py` —— stat 门（四文件 `(mtime,size)`）→ `read_run_dir`（`to_thread`）→ `project()` → `Sequencer.next(epoch)` → 要镜像的先 `await EventMirror.append` **再用返回的 `MirroredEvent.event` 建帧**（顺序反了 WS 帧里会带 base64、且与回放不一致）→ 扇出；自适应 250ms → ×2 退避到 2s 上限；`finish()` 做最后一次 tick + `done` + 停循环 | W2a | `services/scan_channel.py` `app/strix_profile.py`（补三个文件名常量）`tests/test_scan_channel.py` | **2** ✅ 2026-09-21 —— **不变式：慢订阅者被摘掉且不拖住循环；镜像写失败不被吞**（异常冒泡杀掉 channel 任务）。交付 282 + 393 行／8 用例，**子 agent 24 次调用、0 压缩、第 8 次就动手，但峰值 143k 超了 120k 门**（§九.6①）—— 归因不是任务太大而是**附录整包给多了**：A11 要了 `conftest` 全部 fixture（669 行那节的大头），实现其实只用到 `make_run_dir` 一个。下次摘录只摘**点名用到的那几个** fixture。收货：闸门 `make lint` 干净、`make test` **1116 passed / 0 skipped**（基线 1106，+9 channel +1 profile）；**6 处 mutation**（2 照清单：`dropped` 置位删掉红 t1、`append` 包进 `try/except OSError: return` 红 t2；4 处自选：`Sequencer(epoch=-1)`→`Sequencer()` 红 t5、状态提交挪到 emit 之前红 t2、`_fanout` 去掉 `list()` 拷贝、`while not self._finished`→`while True`）。**自选的后两处各抓一处「没人盯」**：① **MX5 去掉 `list()` 拷贝 0 红** —— 被摘的订阅者在第 0 位，边迭代边 `remove` 会让紧跟其后的健康订阅者**只漏掉"摘掉动作发生的那一帧"**，而 t1 原来只数 `summary` 出现 2 次，恰好不含那一帧（`agents`）→ 把断言改成整串 `["agents","summary","summary"]`。② **MX6 把 `while not self._finished` 换成 `while True` 也 0 红** —— 没有任何测试盯着"`finish()` 之后循环会停"；顺着它读代码抓到**本轮唯一真缺陷**：`_finished` 的**判据在锁外、置位在锁内**，`finish()` 握着锁做最后一次 tick 时循环可能已通过 `while` 检查并在锁上排队 → 拿到锁后再发一整轮帧，**落在 `done` 之后** → 判据挪进锁内 `if self._finished: break`，补 `test_the_loop_stops_after_finish_and_never_ticks_again`（手工占锁冒充那个交错，不依赖 `to_thread` 调度，无 flake）。**判据 ④（字面值）**：`BASE/MAX_INTERVAL_S` 被退避序列里的 `0.5`／`1.0` 间接钉住、第一帧 `seq == 0` 被 t5 钉住（MX3 证实）、三个新 profile 值有专测 —— 只有 `SUBSCRIBER_QUEUE_SIZE=256` 没钉，刻意不补（调优旋钮，不是不变式）。另：`strix.log` 的文件名子 agent 放在模块常量里并上报请裁决 → **收货时收进 `StrixProfile.log_file_name`**（它同样随上游版本变，归属地是那张对照表；但**刻意不进 stat 门** —— 日志几乎一直在长，盯它等于让门永不生效）|
| W2c | **接线**：`ChannelRegistry`（按 scan_id 起停、停机全关）+ `routes/scans.py` 在 `_run_to_completion` 里起、在 `process.wait()` 返回后 `await channel.finish()` + `main.py` lifespan 关停。**channel 自己不看进程**（单一权威，别两处各判一次「跑完了没」）| W2b | `services/scan_channel.py`（registry）`routes/scans.py` `main.py` `tests/test_scan_channel.py` `tests/test_routes_scans.py` | **2** ✅ 2026-09-22 —— **不变式：扫描终态或停机后 channel 一定被关掉、不泄漏任务**。交付 151 + 280 行／8 用例（registry 6 + 接线 2）。**子 agent 46 次调用、0 压缩，但峰值 130k 超 120k 门**（§九.6①，与 W2b 同一个病：**附录整包给多了** —— `conftest` 全文 409 行 + async 测试风格 549 行，实现真正用到的只有 `make_run_dir` 与「用 `asyncio.run` 跑场景」两处）；**首次写代码第 18 次调用**（判据② ❌，但归因是交底形状不是任务大小 —— 见 §派发方法学 10）；**Edit 23 次／自产 27k 字**（§十.6 又一次）。收货：`make lint` 干净、`make test` **1124 passed / 0 skipped**（基线 1116，+8）；**5 处 mutation**（2 照清单：`finally` 里不关 channel 红 2、`close` 挪到 `forget` 之后红 1；3 处自选，`shutdown` 去掉 key 快照红 1）。**另两处自选 0 红 = 两处「接线没有任何测试」**（§派发方法学 4 立的正是这类）：① **MX3 删掉 lifespan 里的 `await app.state.channels.shutdown()` → 全仓 1124 条全绿** → 补 `test_lifespan_shutdown_closes_the_channels_while_the_database_is_still_open`（`OrderRecordingChannels` 替身，顺带用 `db._conn is not None` 钉住它排在 `db.close()` 之前）；② **MX5 把 `EventMirror` 第二参改成 `scans_dir / scan_id` → 0 红**：任务书两次点名「传错是静默写错 `rel_path`」，却没有任何测试盯那一个参数（测试里的 `EventMirror` 替身是个忽略参数的 lambda）→ 替身改成记参数 + 补 `test_the_mirror_gets_the_scans_dir_itself_not_a_subdirectory`。终态 **1126 passed / 0 skipped**。**子 agent 三处「照做但有疑虑」全部接受**：`close` 里包 `except Exception`（§1.1 原文没写，但 `close` 抛出去会跳过 `forget`／`release` = 凭据泄漏，且这条已被 `test_close_does_not_raise_when_the_channel_died_on_a_mirror_write` 盯住）、`open` 的返回值不绑名（F841）、`_channels` 用 `getattr` + 中文 `RuntimeError` 而 `_supervisor` 是裸读（两者都刻意不做 `isinstance`，否则鸭子替身进不来）|
| W3 | **四张表落库**：`scan_agents` upsert、`scan_findings` 首见即插（含 `input_hash`）、`scans` 的 `cost_usd`／`count_*`／`agent_count`／`event_count`／`current_epoch`／`phase` | W2c | `services/scan_persist.py`（新）`tests/test_scan_persist.py` **+ 加宽：`services/scan_channel.py` `tests/test_scan_channel.py`** | **2** —— **2026-09-20 新增的一行，是一处漏派**（与 T7c 同形状）：验收第 14 组那条「`severity_counts` 与 `scan_findings` 行数一致」要求这几张表有行，而在此之前**没有任何一行任务拥有它们的写入**。刻意不折进 W2b（§三.2：超 500 行也超 1 条不变式）；实时 cost 走 `summary` 帧、不依赖本行。<br>**2026-09-22 派发**（交底产物 `dispatch/W3/prompt.md`，**656 行、附录内联进同一份文件** —— W2b／W2c 连着两次被"任务书 + N 份附录文件"顶过 120k，这次按 §派发方法学 10 只摘点名用到的那几段）。**文件列加宽两个的理由**：`scan_persist.py` 单独交完**没有任何生产调用方**，正是本仓烧过多次的"声明了却没有一处强制"形状 → 调用点定在 `ScanChannel._tick` 的 emit 循环**之后**、三份状态提交**之前**（帧是用户看得见的延迟路径、聚合列是它的衍生品；又必须落在"整轮成功才提交"之内），实例在 `ChannelRegistry.open` 建（registry 已持有 `db`）。**三条不变式**：**P1** 计数只能在同一事务里从我们自己刚写下的行上 `COUNT(*)` 算出来，**不许抄 `snapshot.severity`** —— 上游 `severity_counts()` 把 `info`／`unknown`／缺失全折进 `low`、且它是按记录数而不是按 id 去重后计数，所以"同一个 id 出现两条"时两个独立计算立刻打架（验收第 13 条要的那条一致性必须**结构上**成立）；**P2** 首见即插 `ON CONFLICT DO NOTHING`，`first_seen_at`／`input_hash`／`raw_json`／`severity` 永不改写（与帧层 `vuln.add` 只推一次同源 —— 库里悄悄换新版本 = 客户端收到的与库里存的是两份东西，而 T16 回放靠库）；**P3** 落库失败冒泡杀掉 channel 任务（同 I2，不许 log-and-continue），且 `_tick` 每一轮都真的调了它。**`scans.phase` 这一列不写（2026-09-22 用户拍板）**：它没有取值域、没有生产者、也没有消费者，我们现编的任何值都是 `scans.status`／`ScanOutcome` 已有事实的第二份判决；顺带记下 §实时流设计 的 WS 类型清单里那个 `phase` 帧**同样没有生产者** —— **2026-09-22 用户拍板把它删掉**（清单与验收第 7 条都已改）。其余交底时拍板：`finding_id` = `scan_frames.vuln_key(entry)`（帧层与库层必须同一个身份）、`input_hash` = 整条原始记录的 `fingerprint_of`（T21 还没有字段清单，现在收窄等于猜）、`raw_json` = `canonical_json`、`severity` 按上游同一规则折进四个桶（原始值完整留在 `raw_json`）、坏 `cvss`（非数字／越界／`bool`）与负 `cost_usd` 一律**不写**而不是抛（P3 让异常杀流，一条脏值不许打死实时流）、`cost_usd` 用 `COALESCE(?, cost_usd)`（读不到 ≠ 变成 0）、`event_count` 数的是**当前 epoch** 的 `scan_events` 行数（= 此刻重连会被回放多少条，压缩后从头算是正确行为）、`created_at` 不进 upsert 的 SET。预算 ≤35 次调用／峰值 <120k／0 压缩。**收货要跑 ≥3 处 mutation**（M1 计数改抄 `snapshot.severity`、M2 `DO NOTHING`→`DO UPDATE`、M3 删掉 `_tick` 里那句 `record`）+ 自选。<br>**✅ 2026-09-22 收货（三件事全做）**：`services/scan_persist.py` **189** 行 + `tests/test_scan_persist.py` **363** 行／8 用例，`scan_channel.py` +13 行（docstring 补 **I3**、`persist` 必填 kw、`_tick` 一行、`ChannelRegistry.open` 建实例）、`test_scan_channel.py` +91 行／3 条接线用例。**子 agent 41 次调用、0 压缩、预算 ≤35 略超**（交底改成"附录内联进同一份文件"之后**峰值终于没破 120k**，W2b／W2c 那个病治住了）。闸门 `make lint` 干净、`make test` 1126 → **1137 passed / 0 skipped**。**5 处 mutation**（3 照清单，各只红该红的 2 条：M1 红 P1＋正路、M2 红 P2＋P1 的行断言、M3 红两条接线而服务层 25 条一条没红 —— 接线与内容确实分开守着）。**2 处自选全是 0 红 = 两处没人盯**：① **MX4 `input_hash` 改成只对 `id` 取 → 全仓 1137 条全绿**：`raw_json` 有内容断言，而"缓存键必须随整条原文变"这件事没有任何测试盯着 —— 而 T21 拿它跨扫描查译文缓存，同 id 不同内容会取到别人的译文 → 正路那条补 `input_hash == sha256(raw_json)`（**不写成 `== fingerprint_of(entry)`**，那是同义重复）；② **MX5 `agent_count` 去掉 `WHERE scan_id = ?` → 全仓全绿**：三个计数查询各有一处 scan_id 过滤，而全部测试都只有一条扫描 → 补 `test_counts_never_borrow_another_scans_rows`（库里同时放 scan-2 的 agent／finding／两条事件，断言我们的八个数只数自己的行，且 scan-2 那一行一个字没动）。**判据 ④（字面值）另抓一处**：`_cvss` 的上界被 `11.5` 钉着而**下界没有** → 脏值那条加 `cvss=-0.1`（自选 MX6 去掉下界，补之前 0 红、补之后只红它）。终态 **1138 passed / 0 skipped**。**子 agent 报上来的两处规格边缘都接受、不改**：`canonical_json` 遇到不可序列化的值会抛 `TypeError` 并按 P3 打死 channel（与 `_cvss` 的容错取向不一致，但 `vulnerabilities.json` 本身是 JSON 解析出来的，现在触发不了 —— 若将来 `read_run_dir` 改成从 `agents.db` 拼 entry，这一格要重想）；`COALESCE` 让 `cost_usd` 一旦落过正数就再也写不回 0（刻意的，真需要归零得另给路径）。另：`ruff format` 它用可写挂载跑（`:ro` 下格式化器写不回去），三条闸门仍按 `:ro` + `--no-cache` —— 这条要补进下一份任务书 |
| T15a | **采集压缩夹具**：真跑一次扫描，`STRIX_CONTEXT_BUFFER_TOKENS=1` + `STRIX_MAX_CONTEXT_IMAGES=1` 强制触发压缩与图片淘汰；产物脱敏后入库 | T13 | `tests/fixtures/run_dirs/` | **自** —— 要真凭据、真扫描，派发规则第 3 条禁止把 Key 给子 agent，**结构上不可派发** |
| T15b | 重同步测试（吃 T15a 的夹具）| T15a | `tests/test_projector_resync.py` | 2 |
| T16 | WS + SSE 路由、重连回放 | T14 | `routes/stream.py` | 2 —— **2026-09-22 按 §三.2 拆成四条**（整条同时压着"读镜像 + WS 协议 + SSE + 媒体路由"，必然超 500 行也超 1 条不变式）：**T16a**（已收货）回放读取端 · **T16b** `WS /ws/scans/{id}` · **T16c** `GET /api/scans/{id}/stream`（SSE 兜底，验收 26） · **T16d** `GET /api/scans/{id}/media/{sha}.png`（+ 产物已被留存清理的 404 码与文案，2026-09-18 说的"随 T16 做"就是这一条）。**串行**：T16b 吃 T16a，T16c 与 T16b 共用同一条订阅路径，T16d 独立。**前置改成 T16e**（见下一行，新订阅者的快照出处）|
| T16a | **事件回放的读取端**：`scan_events` 的行 → 当初直播推出去的那些帧（游标 `resume_from{epoch,seq}` + 分页）| T14c W3 | `services/event_replay.py`（新）`tests/test_event_replay.py`（新）+ `tests/conftest.py`／`tests/test_event_mirror.py`（只提取 `make_projected_event`）| 2 —— 交底产物 `~/Documents/claude/dispatch/T16a/prompt.md`（**330 行、附录内联进同一份文件**，W3 那个形状继续有效）。**九条语义交底时拍板**：现在是第几代**只信 `scan_events` 自己的 `MAX(epoch)`**，不读 `scans.current_epoch`（那一列由别人写，漂移了就会交出客户端永远等不到的帧）· 游标只决定从哪一帧开始（epoch 匹配取 `seq >`，其余一切情况给当前 epoch 全量）· 一行一帧、`type` **恒为 `event.add`**（表里不记当初是 add 还是 update，因为**前端对两者都是按 `payload.key` upsert** —— 这条是 T17 要继承的约定）· payload 就是 `event_payload()` 那六个键（`fingerprint`／`strix_id` 不进帧）· **信封 `ts` 是"这一帧什么时候发出去的"**，所以收一个 `ts` 参数而不是在模块里调 `now_ts()`（事件自己的时间在 `payload.ts` 里）· 未知 scan_id **不抛**（"扫描存不存在"由路由查 `scans` 回答）· `MAX(epoch)` 与取行必须在**同一次** `db.run` 里（一次一个事务，分两次中间会被写进新行）· 不读不筛 `version` 列 · `data_json` 缺三键让 `KeyError` 冒泡。<br>**✅ 2026-09-22 收货（三件事全做，一条自陈都没采信）**：`services/event_replay.py` **120** 行 + `tests/test_event_replay.py` **299** 行／14 用例，`conftest.py` +27（`make_projected_event`，第 2 个用处出现所以按 §十.3 提取）、`test_event_mirror.py` -12/+3（`_event` 改成三行包装，断言一条没动）。闸门 `make lint` 干净、`make test` 1138 → **1152 passed / 0 skipped**。子 agent **28 次工具调用、0 压缩、首次写代码第 4 次调用**。<br>**8 处 mutation（4 处自选）**：M1 `seq >`→`seq >=` 红 3 · M2 `MAX(epoch)` 漏 `scan_id` 过滤 红 2 · M3 payload 直接用三键信封 红 2 · **M5（自选）`event.add`→`event.update` 红 2** —— 子 agent 自陈"`type` 只被间接盯着、两边一起改不会红"是**错的**（它说的"两边一起改"不是 mutation，期望帧那侧的字面量写在测试里，生产改了就红）· M6（自选）取满即给游标的判据 红 2 · M7（自选）信封 `ts` 改用行里的事件时间 红 2。**M4（自选）去掉 `ORDER BY seq` → 红 0**：`PRIMARY KEY (scan_id, epoch, seq)` 的索引让这个 WHERE 天然按 seq 出行，所以那条"乱序写入"测试**没有区分力** → 已把真相写进它的 docstring（`ORDER BY` 留着是显式声明，将来加走别的索引的过滤时它是唯一保证），**没有假装补一条测得住的测试**。<br>**判据 ④（靶子是字面值吗）又抓一处**：`DEFAULT_LIMIT = 500` 没有任何测试盯着（改成 100000 全仓全绿）—— 而"页大小没有上界"等于"一个客户端重连能把整条扫描读进内存再塞进一个 send 循环" → 补 `test_default_limit_is_a_bounded_page`（MX8 实测只红它一条）。**这是同一形状的第五次**。<br>**整读之后认下 / 记下的**：① 分页语义是"取满就给游标、不预读下一行"，所以最后可能多空跑一趟（有专门用例钉住，比预读便宜）；② ⚠️ 子 agent 报告里说 `limit=0` 会"返回一个指向空批的游标"，**实际是 `rows[-1]` 抛 `IndexError`** —— 没有生产调用方能传 0，按"编程错误就让它响"保留现状，但**它的自陈不准**；③ "只许一次 `db.run`"只靠注释守着（要盯住得数调用次数或塞假 `Database`，不值一格）。 |
| T16e | **扫描读取端（漏派补登记，2026-09-22）**：`GET /api/scans`（列表）+ `GET /api/scans/{id}`（状态／归因／`cost_usd`／四个 `count_*`／`current_epoch` + agents 树 + findings 列表）| W3 | `routes/scans.py`（加宽）`tests/test_routes_scans.py` | 2 —— **与 T7c／W3 同一个形状的漏派**：`done` 帧刻意不带结论、明写"结论只从 `GET /api/scans/{id}` 取"，而**全仓没有任何一行任务拥有这个端点**（`routes/scans.py` 只有 `POST ""` 与 `POST "/{id}/stop"`，T17 是纯前端文件）。**2026-09-22 用户拍板它是新订阅者快照的唯一出处**：事件能从镜像回放，但 agents 树／summary／发现列表**不在镜像里**，而 channel 只在指纹变了才推 —— 半途连上来的客户端靠这个端点拿"现在是什么样"，WS 只管之后的增量（另一条路"新订阅者到达就清 FrameState 重推一轮"已否决：会把上百 KB 的报告扇给所有在线订阅者，且 report 内容没留在内存里）。**读的正是 W3 已经在写的那几张表**，所以排在 T16b 之前。<br>**2026-09-22 已派发**（任务书 `~/Documents/claude/dispatch/T16e/prompt.md`）：响应形状五条要点见 §交接，本行不复述。文件列仍是那两个（`routes/scans.py` 加宽 + `tests/test_routes_scans.py`），**不新建 service、不动 `main.py`、不写迁移**。<br>**2026-09-23 已收货**（+211／+273 行，闸门 1161 passed / 0 skipped）。mutation 五处，四处只红该红的那一条（`agents` 行的 `status` 取成 `name` → 只红同形状那条；详情多回 `vault_handle` → 红卫生 + 26 键两条；`truncated` 恒 False；未知 id 不再 404），**第五处暴露了一个缺口：改掉 findings 的 `ORDER BY` 全绿** —— "= 当初推帧的顺序"只是个字面值、没人盯（判据④"靶子是字面值"第 7 次命中）。**已补两条**：`test_findings_come_back_in_the_order_they_were_pushed`（三条发现，id 次序与首见次序两个方向都不同，按 `finding_id` 升/降序排都会红）＋ 卫生测试加一句"响应正文里不许出现 `settings.console_data_dir` 的绝对路径"（键名白名单挡不住把 `cwd` 的**值**塞进合法字段，已用 `reasoning_effort=row["cwd"]` 验过会红）。子 agent 三处偏离都成立并已接受：`scans.max_turns` 与 `scan_agents.name`／`status` 可空 → 模型必须 `| None`（否则夹具行让详情 500）。 |
| T16b | **`WS /ws/scans/{id}`**：`hello{resume_from}` → 从只追加镜像回放 → 接成直播 | T16a T16e | `routes/stream.py`（新）`main.py`（接线）`tests/test_routes_stream.py`（新）+ `tests/conftest.py`／`tests/test_routes_system_pull.py`／`tests/test_routes_scans.py`（只搬两个助手）| 2 —— **2026-09-23 已派发**，交底任务书 `~/Documents/claude/dispatch/T16b/prompt.md`（附录 A1–A9 内联进同一份文件，T16a／T16e 那个形状继续有效）。**唯一不变式：回放与直播的接缝上既不丢帧也不重帧。**<br>**交底时拍板的语义（S1–S12，本行只记会被别人继承或容易被"简化"掉的那几条）**：① **订阅必须先于回放查询** —— 反过来的话"回放已读完"到"订阅生效"之间的事件帧**永久消失**（它既不在回放批里也不在队列里）；订阅早于回放只造成重帧，而重帧能去掉。② **去重规则不许写成"只发严格大于 `(epoch,seq)` 的帧"**：回放只产 `event.add`，**只有进过镜像的两个类型**（`event.add`／`event.update`）才可能与回放重叠，`summary`／`log`／`notice`／`agents`／`vuln.add`／`report`／`done` 从不进镜像 → 一刀切会把 `notice(context_compacted)` 这种**一次性**帧永久丢掉。所以三条规则：`epoch <` 丢（陈旧代残帧，放过去会让前端把**更新**的状态清掉）· `epoch ==` 时只对两个事件类型按 `seq` 丢 · `epoch >` 一律发。③ **游标只许被回放帧与事件帧推进**：`epoch` 相等的非事件帧 `seq` 再大也代表不了"镜像覆盖到这儿"。④ 扫描不存在 → **`error` 帧 `{code:"not_found"}` 再 `close(1000)`**，不是握手期 404（WS 握手的状态码前端拿不到正文，而"按码分支不匹配文案"是硬要求）；这条检查**必须在路由做** —— `replay_batch` 明写"扫描存不存在由路由查 `scans` 回答"。⑤ 没有活着的 channel（扫描早结束／api 重启过）→ **只回放然后正常关，绝不自己编一个 `done`**（`done` 只有 `ScanChannel.finish()` 会发，对"跑到一半 api 重启"的扫描造一个是假信号）。⑥ 落后被摘（队列 `None` 哨兵 + `subscriber.dropped`）→ best-effort `error{code:"stream_lagged"}` + `close(1011)`，前端带 `resume_from` 重连；`dropped is False` 才是正常收尾。⑦ 形状是**与传输无关的 async generator + 一层薄传输**（T16c 的 SSE 复用同一个 generator，只换传输层），两个自定义异常 `ScanNotFound`／`SubscriberLagged` 给传输层映射。⑧ `hello` **宽容不超时**（解析不出来一律当 `resume_from=None`）。⑨ 断开检测照 `/ws/system` 先例必须有（我们从不读客户端消息 → 少了那个 receive 任务，每个关掉的页面都留一个永远等在 `queue.get()` 上的任务）。<br>**三笔债，都不在本行的文件列里**：① **`stream_lagged` 与 T13 那三个 `NOTICE_*` 至今没有任何中文文案、也没有覆盖测试** —— `errors.py` 只有两类码（HTTP 码要真 `status`／扫描归因码），WS 帧码在本仓的先例就是 `run_projector.py:42-44` 那三个模块常量，`test_message_coverage.py` 对已有两棵树是**双向**比对（往 `zh-CN.json` 加孤儿码直接红）。**→ T17 做那个面板时建第五棵树 `wsNotices.*`（或同名）+ 两条双向断言**，覆盖 `screenshot_elided`／`context_compacted`／`stream_resynced`／`stream_lagged`。在那之前这 4 个码进前端会被 `t()` 原样渲染成 key。② **T17 的 `resume_from` 必须存"见过的**最大** `(epoch,seq)`"，不是"最后一帧的 seq"** —— 非事件帧透传（规则②）让 `seq` 在接缝上**不单调**，存最后一帧会让重连要求一个比已收到更早的位置、于是重复一批事件。③ `_db` 这个取用函数到 `stream.py` 是**第三份**（`audit.py`／`scans.py` 各一份），提取归属地是 `routes/_context.py`，**留给下一条本来就要碰那三个文件的任务** —— 为它重构会把本行的闸门面撑宽到三个既有路由模块。<br>**收货 mutation 清单（派发前就写下，9 处 + 若干自选）**：订阅挪到回放之后 · 去掉 epoch 比较只比 seq · 对非事件帧也去重 · 无活 channel 时不回放 · `finally` 里不 `unsubscribe` · 拿到 `None` 不看 `dropped` · 删存在性检查 · 转发完 `done` 不结束 · 回放只取第一页。另按本仓规矩要有一条"删掉 `main.py` 的 `include_router` 就红"的接线测试。<br>**✅ 2026-09-23 收货（三件事全做，一条自陈都没采信）**：`routes/stream.py` **315** 行 + `tests/test_routes_stream.py` **461** 行／27 用例，`main.py` +5、`conftest.py` +27（`ws_headers`／`writable_conn` 两个助手按 §十.3 提取，**断言与函数体逐字未改**）、`test_routes_system_pull.py` −14/+1、`test_routes_scans.py` −24/+1。闸门 `make lint` 干净（ruff + eslint + tsc）、`make test` 1161 → **1188 passed / 0 skipped**。子 agent **40 次工具调用、0 压缩、首次写代码第 7 次调用**（比"读完交底 + 3"多 3 次：查 `scan_events` 的真实 9 列、查 ruff 的 select 含不含 `N`(N818 会不会拒 `ScanNotFound`)、读 `/ws/system` 那条泄漏测试的技法 —— **前两件本该进附录，下一份交底带上 schema 与 ruff 的 select 清单**）。<br>**主会话 7 处 mutation（刻意挑它没跑过的），外加一条 BASE 正面对照**（BASE 26 passed，证明"全绿"不是因为一条测试都没跑）：规则①反过来 → 只红 stale-epoch 那条 · `hello` 的 bool 不当整数去掉 → 只红那个参数化用例 · `error` 帧 `epoch/seq` 改成 7/7 → 只红 not_found 那条 · `gone` 从 `asyncio.wait` 集合里去掉 → 只红"关掉的页面不留任务" · cancel 之后不 `await pull` → 也只红那一条。**全部只红该红的。**<br>**读代码读出一处真缺陷（已修）**：`sent_through` 初始化成 `resume_from`，也就是**拿客户端自报的游标当去重判据**。镜像为空时回放什么也交不出来，游标就一直是客户端给的那个值 → 一个 epoch 比镜像大的游标（老前端／被改过的前端／`ProjectorState.initial()` 是 `epoch=0` 所以重启后新 channel 从 0 重发）会让 `should_forward` 的规则①把之后**每一帧**都当成"陈旧代"丢掉，客户端拿到的是 **0 帧 + `close(1000)` 正常关闭** —— 一条永不出帧也永不报错的流，**这个功能最坏的失败形态**。实测补的测试在修之前红成 `assert [] == [('event.add', 0)]`。改法是删掉那次初始化（从 `None` 起）。**这同时修正了我自己在 S5 里写错的语义**：`sent_through` 不是"客户端已经有的东西"，而是"**这条连接的回放实际交出去的最后一帧**" —— 重帧只可能来自「订阅早于回放查询」那个窗口，而那个窗口的范围只由回放自己决定。<br>**删掉 `_advance`（19 行 + 调用）**：把那次调用删掉 26 条测试**一条都不红**（mutation MB 实测），因为它**在任何可达状态下都不可观测** —— 游标的语义是"回放交出去了什么"，那是一个已经定下来的事实；同一代里 seq 由 channel 的**单个**发号器单调发出，所以回放之后到来的事件帧 seq 必然大于它。S5 里"非事件帧不推进游标"那半条语义因此是多余的（它防的是一个不会发生的乱序）。已把这个推导和"别再加回来"写进注释。<br>**未覆盖但接受**：`finally` 里那句显式 `await frames.aclose()` 删掉是全绿 —— 取消 `pull` 时 `CancelledError` 已经会穿进 generator 触发它的 `finally`（`unsubscribe` 在那里），`aclose()` 是第二道；而**同一对里真正承重的那半边有测试**（cancel 之后不 `await pull` 是红的）。<br>**两笔交给后面的**：① **本行给协议新增了一个帧类型 `error`**，它不在 `services/scan_frames.py` 的 8 种里 —— T17 与任何"帧类型全清单"的地方要带上它。② 子 agent 把"关掉的页面不留任务"的观察点从 `asyncio.all_tasks()` 的**条数**换成"`unsubscribe` 被调到"，理由成立且更直接（条数被 lifespan 的 sweeper／reaper／retention 三个后台任务搅得对不齐，它实测 flaky）—— **顺带一个存量风险：`/ws/system` 那条同名测试用的还是条数法，哪天 CI 偶发 `CancelledError` 就是它。**<br>**两处偏离，都记下不追**：① TDD 顺序是"先写码、再用三个桩退回缺失态取红"，不是先测后码；红是真的（ROUND C 正好等于我预告的 M3），而且我自己 7 处 mutation 独立复核过，判据满足。② **macOS 没有 `timeout`（不是 coreutils）** —— 它第一版 mutation 脚本用了 `timeout 240 docker run`，结果"8 个 mutation 全部没有测试变红"，因为一个测试都没跑。这正是 §八.3 末段"检查都通过 ≠ 被检查的事真发生了"，所以我这轮的脚本带了 BASE 正面对照 + "补丁没恰好命中 1 次就退出"。 |
| T16c | **`GET /api/scans/{id}/stream`**（SSE 兜底，验收 26）：复用 `stream_frames`，只写第二层薄传输 | T16b | `routes/stream.py`（加 `sse_router`）`main.py`（接线）`tests/test_routes_stream.py`（追加）| 2 —— **2026-09-23 已派发**，交底 `~/Documents/claude/dispatch/T16c/prompt.md`。**唯一不变式：`EventSource` 的自动重连既不丢帧、也不在流正常结束后无限重连。**拍板的语义：① `id:` = 这条连接**交出去过的最大** `(epoch,seq)`，不是这一帧自己的（接缝上 seq 不单调，T17 那笔债在 SSE 上的对应物，这里浏览器替前端存）；`error` 帧无 `id:` · ② 起点游标 `Last-Event-ID` 头优先于 `?resume_from=epoch:seq`（自动重连时 URL 不变），宽容解析 · ③ WS `close(1000)` ↔ `event: end`（前端据此 `es.close()`）；`close(1011)` ↔ 不发 `end` 直接结束（浏览器自动重连 = 续传）；`not_found` = error 帧 + `end`；**绝不发 `event: error`**（与浏览器连接错误同名）· ④ 首发注释 `: connected`（首字节不依赖扫描状态）+ `X-Accel-Buffering: no` · ⑤ 断开靠 Starlette 1.6 在 ASGI spec<2.4 时监听 `http.disconnect` 并 cancel body（uvicorn 0.52.4 给 2.3，已在镜像核实）—— **升 uvicorn 到 spec 2.4 要回来看**。预算 ≤30 调用 / 0 压缩 / ~300 行；收货 mutation M1–M11 写在交底 §8。<br>**✅ 2026-09-23 收货**：`stream.py` 424 行（+108）、`main.py` +3、`test_routes_stream.py` 665 行（+204／21 用例）。`make lint` 干净、`make test` 1188 → **1209 passed / 0 skipped**。子 agent **14 次调用、峰值 72k、0 压缩**（`agent_budget.sh` 报"没写过代码"是因为它经 Bash 写文件，自陈首次写代码第 4 次）。主会话 5 处自选 mutation + BASE：不把游标传给 generator → 只红续传那 3 格 · 普通帧多一行 `event: message` → 只红正路那条 · lagged 不发 error 帧 → 只红 lagged · 读错头名 → 只红"头优先"那格 · **删 `finally` 的 `aclose()` → 全绿（接受，与 WS 同理：cancel 已穿进 generator 的 finally）**。读代码无缺陷。**偏离一处（成立）**：关页面测试改成"订阅生效后才断开"（`: connected` 早于订阅，按我给的骨架断开会落在订阅之前，什么也测不到）；它另证明了 scope 改 spec 2.4 时这条测试会超时 —— S8 的依赖被测住了。**交给 T17**：SSE 前端契约 = 默认 `message` 按信封 `type` 分支 + `addEventListener("end", () => es.close())`；初次连接带 `?resume_from=` 用的是**见过的最大** `(epoch,seq)`；`parse_sse_cursor` 接受负数与 `+3`（负 epoch 在 `replay_batch` 里落进"给当前 epoch 全量"，无害）|
| T16d | **`GET /api/scans/{id}/media/{sha}.png`**：截图读取端 + 留存清理后的 `artifacts_purged` 码 | T14 W1 | `routes/scans.py`（追加一个路由）`tests/test_routes_media.py`（新）| 1 —— **2026-09-23 已派发**，交底 `~/Documents/claude/dispatch/T16d/prompt.md`。**主会话先落了** `errors.py::ArtifactsPurgedError`（404，进 `ALL_ERRORS`）+ `zh-CN.json` 的 `errors.artifacts_purged`（把文案维度从子 agent 文件列拿掉）。**唯一不变式：只交出 `scan_media` 以 `(scan_id, sha256)` 记过账的文件；记账不存在时 purged 与从没有过两个码。**拍板：路径唯一出处是 `rel_path`（URL 两段只当占位符，不做 sha 正则）· purged 判据是 `audit_log` 的 `scan.purged`（磁盘/行都分不清被删与没落过盘）· 行在文件不在 → `not_found` 不 500 · 头 `Cache-Control: private, max-age=31536000, immutable` + `nosniff`（`extract_media` 不验 PNG 结构）。预算 ≤20 调用 / 0 压缩 / ~200 行；mutation M1–M7 在交底 §8。<br>**✅ 2026-09-23 收货**：`scans.py` +44、`test_routes_media.py` 113 行／4 用例（生命周期那条走真 `EventMirror` → 真 `RetentionSweeper`，URL 取自镜像改写结果而非手拼）。`make lint` 干净、`make test` 1209 → **1213 passed / 0 skipped**。子 agent **9 次调用、峰值 44k、0 压缩、首次写代码第 6 次**。10 处 mutation 主会话自跑：M1 去 `scan_id` 条件 → 只红跨扫描 · M2 不查审计／M3 event 字面量改错／M5 jpeg／M6 去 nosniff／M10 Cache-Control 改 no-store／M9（自选）`artifacts_purged` 状态改 410 → 各只红生命周期那条 · M4 去 `is_file` → 只红文件缺失 · M8（自选）两个码互换 → 红 3 条 · M7 路径改用 URL 拼 → 全绿（预期内，无功能差异）。读代码无缺陷；收货时把 warning 日志正文改成中文（与仓内惯例一致）。**没测住的格子**：文件缺失那条的 warning 日志本身没有断言（接受）。**交给 T17**：截图 404 按码分两支（`not_found`／`artifacts_purged`）|
| T17 | 前端实时面板（子 agent 树 / 事件流 / 终端 / 截图 / CostMeter）| T5 T16 | `frontend/src/components/live/*` | **3**（读 T5 的约定，**不再开** frontend-design）—— **2026-09-23 按 §三.2 拆成 T17a／b／c 串行 + T17d 另起**（整条估 1100–1400 行）。方案原文 `~/Documents/claude/dispatch/T17/plan.md`，**用户放行四条**：① 三条串行；② **前端只做 WS、不接 SSE**（SSE 留给 curl 与验收 26）；③ `/scans` 列表页另起 T17d；④ `log`／`report`／`vuln.add` 帧 v1 不渲染、store 丢弃。**T17-0 主会话已落**：`zh-CN.json` 的 `live.*`（30 键）+ `wsNotices.*`（4 码，T16b 债 ①）+ `test_message_coverage.py` 三条（双向 + 纯字符串；mutation 删码／加孤儿各只红一条）。终端字段已更正（§实时流设计）：strix 1.6.2 的 `exec_command` 参数是 `args.cmd`（`agents/sandbox/capabilities/tools/shell_tool.py:106`，测试镜像里核过）。**结论判据**：`status=stopped` 时 `exit_meaning` 仍可能是 `no_vulnerabilities_found`（`scan_supervisor.resolve_attribution`）→「未发现漏洞」必须同时要求 `status=="completed"`。|
| T17a | 实时面板数据层：`applyFrame` 纯 reducer + WS 客户端（hello／游标取见过的最大 `(epoch,seq)`／1000 不重连／1011 与异常断开退避重连／`not_found` 终态／握手 401）+ `fetchScan`／`stopScan` | T17-0 | `src/lib/api/client.ts` `src/lib/ws/scanStream.ts`（新）`src/lib/stores/scanLive.ts`（新）| 2—— 交底 `~/Documents/claude/dispatch/T17a/prompt.md`。<br>**✅ 2026-09-23 收货**：`scanLive.ts` 283 行、`scanStream.ts` ~175 行、`client.ts` +84。`make lint` 干净、`make test` **1216 passed / 0 skipped**；子 agent **14 次调用、峰值 72k、0 压缩**；纯前端 mutation 0 处。**它抓到交底的一处自相矛盾（已采纳）**：§2 表写 `log`／`report`／`vuln.add`「`return state`」与规则 2「游标对全部帧取最大」冲突 —— 照前者写，这三种帧不推进游标、重连会重帧；它按规则 2 返回推进过游标的 `base`。**读代码改一处**：`error` 帧服务端恒给 `epoch=0, seq=0` 占位（`stream.py::_error_frame`），原实现把它也喂进 reducer —— 结果处处无害但是碰巧的（没有 epoch 时会把 0 当成当前代）→ 改成连接层在 `apply` 之前拦下 `error`。**记下不追**：REST 持续 5xx 时规则 8 会以 15s 间隔无限重连（刻意：别在抖动时判死）。|
| T17b | `/scans/[id]` 页 + 子 agent 树 + CostMeter（80% 软告警 + 一键停止）+ 停止 + 结论 + 成功块链接 | T17a | `src/app/(app)/scans/[id]/page.tsx` `src/components/live/{LivePanel,AgentTree,CostMeter,ScanHeader}` `src/components/wizard/SubmitPanel.tsx` | 3—— 交底 `~/Documents/claude/dispatch/T17b/prompt.md`。<br>**✅ 2026-09-23 收货**：新增 ~570 行（tsx 459 + css 113）+ SubmitPanel ±4。`make lint` 干净、`make test` **1216 passed / 0 skipped**；子 agent **10 次调用、峰值 50k、0 压缩**；纯前端 mutation 0 处。结论判据落在 `ScanHeader.tsx::conclusionOf`（纯函数，「未发现漏洞」只有 `completed`+`no_vulnerabilities_found` 一条路）。**读代码改三处**：① `LivePanel` 首帧会读到**上一个扫描**的 store（`reset` 在 effect 里才发生，客户端导航切扫描时 agents／cost／`not_found` 会串一帧）→ `stored.scanId !== scanId` 时当 `initialLiveState(scanId)`；② 已有快照时后台重取失败会把整页换成错误块 → 只在 `data === undefined` 时渲染错误；③ 它借用 `wizard.acceptedStatus` 当状态标签 → 主会话加 `live.statusLabel`。另：删 `wizard.livePending`、改首页注释里过时的「成功块没有链接」。|
| T17c | 事件流 + 终端（`exec_command` 的 `args.cmd`）+ 截图画廊（404 两码）+ 通知条（`wsNotices`）| T17b | `src/components/live/{EventFeed,Terminal,ScreenshotGallery,NoticeBar}` `LivePanel.tsx`（挂载）| 3 —— **2026-09-24 已收货**：8 个新文件 ≈340 行 + LivePanel +14；子 agent 5 次调用、0 压缩。`make lint` 绿、`make test` 1216 passed 0 skipped、mutation 0 处（纯前端）、整读全部产物。收货只改一处（`React.ReactNode` → 显式 `import type`）。自定项：`asRecord`／`TOOL_STATUS_LABEL` 从 `EventFeed` 导出给另两块用；截图只认 `/api/scans/{id}/media/` 前缀。2026-09-24 用户在真扫描上人眼看过，通过（遗留问题见 §交接）|
| T17d | `/scans` 列表页（`GET /api/scans`，已有）+ 首页入口 | T17b | `src/app/(app)/scans/page.tsx` `src/components/scans/*` | 3 —— 2026-09-23 用户拍板另起：没有它，关掉成功块那一页之后 scan_id 就找不回来。**2026-09-25 方案用户拍板（照推荐）**：`/scans` 一张表（创建时间／目标／状态／四级发现数／费用÷上限，整行可点）+ 首页「最近扫描」取前 5（同一 `queryKey ["scans"]`）；**不加顶栏导航、不轮询**、不分页筛选；进行中发现格显示 `—` 不显示 0，列表**不写任何结论句**。交底 `~/Documents/claude/dispatch/T17d/prompt.md`（7 文件，≈330 行；预算 ≤40 次工具、峰值 <100k、0 压缩；纯前端 mutation 0）。**✅ 2026-09-25 收货**：子 agent 12 次调用、峰值 ≈42k、0 压缩；`make lint-web` 绿、`make test` 1342 passed / 0 skipped；主会话整读 7 个文件无缺陷；用户看过页面后放行提交 |
| T7c | **凭据表单（LLM Key）—— 前端**：`GET /api/providers` 的目录 → 供应商／形状／模型三选 + `api_base` + 按 `secret_keys` 渲染 N 个 `type=password`（随机 `name`，破自动填充）+ 按 `param_keys` 渲染普通文本框 → `POST /api/keys` → `vault_handle` 进 `useKeysStore`；首页「现在提供凭据」按钮从禁用改成真入口 | T5 T7b | `frontend/src/components/credentials/*` `src/lib/api/client.ts` `src/app/(app)/page.tsx` | **3** —— **2026-09-19 新增的一行，不是从任何行拆出来的**：它是一处**漏派**。T7 是纯后端；T19 的"测试账号收集"是**靶标应用**的账号不是 LLM 凭据；首页那两个按钮里「现在提供凭据」从 T5 起就是禁用的，而它不在任何任务行的文件列里。`CreateScanRequest.vault_handle` 必填 → **不做它，T18 交完仍然一次扫描都发不出去**。文案**已经全写好**（`zh-CN.json` 的 `providers.*` 12 键 + `credentials.*` 7 键，T7b 落的），本行原则上**不新增文案键**。<br>**硬约束**：Key 输入框 `type=password` + **随机 `name`**（泄漏矩阵第 11 行；与登录口令框规则**刻意相反**，理由见 T5b 下面那一行）、POST 后 `finally` 清空 React state、**绝不写日志/埋点**（明文还在作用域里，照 `LoginForm.tsx:78` 那条注释）、`vault_handle` 只经 `useKeysStore.setHandle` 落 `sessionStorage`（`src/lib/stores/keys.ts` 是全项目唯一许可碰它的文件，eslint 封死）|
| T18a | **五步向导 —— 骨架 + 第 1／4／5 步**：`/scans/new` 路由 + 步进条 + `stores/wizard.ts` + 第 1 步目标（`POST /api/targets/validate` 预览：规范化 URL／punycode／每个 IP 的 `ip_class`／命中的白名单条目／`required_opt_in` 两个勾选）+ 第 4 步模板**最小选择器**（`GET /api/scan-templates`）+ 第 5 步预算与轮数（默认取模板 `default_*`）；**填但不提交** | T5 T12c T7c | `frontend/src/app/(app)/scans/new/page.tsx` `frontend/src/components/wizard/*` `src/lib/stores/wizard.ts` `src/lib/api/client.ts` `frontend/messages/zh-CN.json` `src/app/(app)/page.tsx` | **3** —— 2026-09-19 由 T18 按 §三.2 拆出（整条估 700–900 行）。**文件列比原 T18 那一行宽**，已拍板：`client.ts:241-245` 自己写着"包装留给它们的第一个调用点（T18 的向导）"。**6 模板文案／费用预估／高级面板／测试账号收集全归 T19**，本行只出按码渲染的 radio。`scan_mode`／`reasoning_effort`／`extra_instruction`／`credentials[]` 四个字段 T18 一律不发（`CreateScanRequest` 的默认值刚好覆盖，`extra="forbid"` 只禁多余字段不禁缺省）<br>**2026-09-19 已实现并收货**（交底任务书 `/tmp/T18a-prompt.md`，模板 3 的实现阶段）：十个新文件 + `client.ts` 追加两个包装；官方闸门 `make lint` 干净、`make test` 1086／0 skipped，纯前端 mutation 0 处。**文件列与原计划的两处差别**：`src/app/(app)/page.tsx` 按用户拍板**没动**（首页入口留给 T18b），`zh-CN.json` 的 32 个新键由**主会话**先写（把「文案」这一维从子 agent 的文件列里拿掉）。收货读代码改掉四处、`requirement` 的渲染、以及 T18b 的三件交办都记在 §交接，本行不复述。 |
| T18b | **五步向导 —— 第 2／3 步 + 提交**：授权依据（`authorization_ref`，命中白名单时从条目预填）+ **三个独立必勾** + **逐字输入 `targets[0].normalized.host`**（期望串灰显**在输入框旁边**、**不进 `placeholder`**）+ 多目标"以上 N 个均已授权" + 第 3 步操作人 + `POST /api/scans` + 全部错误码分支 | T18a | `frontend/src/components/wizard/*` `src/lib/api/client.ts` `frontend/messages/zh-CN.json` | **3** —— **提交成功后就地渲染成功块**（`scan_id`／`status`／生效预算 + 可折叠 `argv_preview`）+ 一句"实时面板随 T17 上线"，**不给任何链接**：`routes/scans.py` 只有 `POST ""` 与 `POST "/{id}/stop"`，**`GET /api/scans/{id}` 不存在**，`/scans/[id]` 页面是 T17 的地盘 —— 判据沿用 `page.tsx:44-49`「点下去 404 的按钮比没有按钮更糟：它把'这一步还没做'变成'这个工具坏了'」。<br>**错误渲染**：`ApiError.code` → `ErrorNotice`；`params.reason` 在时额外去 `targetGuard.reasons.*` 取人话并高亮第 `params.index` 行，不在时退回 `errors.invalid_request`（契约出处 T12a 行）。`blocked_metadata`(403) **不给任何"覆盖"入口**<br>**2026-09-20 已派发**（交底任务书 `/tmp/T18b-prompt.md`）。**文件列比本行原先写的宽两个**，已拍板：`src/components/ui/ButtonLink.tsx`（新，首页 CTA 要一个 `next/link` 版按钮，复用 `Button.module.css`；**不给 `Button` 加 `href`** —— 一个组件渲染两种元素比两个各自直白的组件难读）与 `src/app/(app)/page.tsx`（T18a 按用户拍板没动首页，入口就欠在这一行）。另加 `GET /api/allowlist` 的包装 `fetchAllowlist()` —— 授权依据要从命中的条目取 `authorization_ref`，而 `TargetValidation.allowlist_entry` 只回条目的 **label**，编号只在 `/api/allowlist` 的正文里。**原计划里"命中白名单时从条目预填"改成"展示条目 + 一键填入"**（用户拍板，理由在 §交接）。方案与收货判据在 §交接，本行不复述。<br>**2026-09-20 已实现并收货**：6 新 + 8 改（+ `zh-CN.json` 45 键由主会话先写）。`make lint` 干净、`make test` **1086／0 skipped**，纯前端 mutation 0 处；整读 14 个文件读出三处真缺陷（声明不随目标作废、回执活在本地 state、`.btn` 在 `<a>` 上失效），全部已修。三处逐条：① **改目标之后整份声明仍然勾着** —— 三个 setter 只作废 `validation`，三条声明／"以上 N 个均已授权"／逐字确认串都留着，而逐字确认**只钉住 `targets[0]`**，于是改第二行或加一个 host 之后那些勾全在 → 用户为一份他没签过的清单签了字，**而这一屏存在的理由正是这件事**；现在三个 setter 一起清 `CLEARED_DECLARATION`（校验快照 + 逐字串 + 三个勾 + 多目标那条），`authorizationRef`／`operatorName` **刻意不清**（它们是这次委托的属性，不是某一批目标的属性）。**这一处是子 agent 自己点出来留给我拍板的，它的判断是对的。** ② **202 的回执活在提交面板的本地 state 里** → 翻回第 3 步再回来就没了，而那个 `scan_id` 是**屏幕上唯一一份**（没有扫描列表、`GET /api/scans/{id}` 不存在，丢了连 stop 都调不出来）；已提进 store 的 `accepted`，只有 202 会写、没有置回 `null` 那一路、且不随目标变化作废。③ **`ButtonLink` 渲染 `<a>` 而 `.btn` 既没 `display` 也没 `text-decoration`** → 行内元素吃不住上下 `padding`、且全站 `a` 带下划线（`globals.css:205-209`）→ 一个带下划线的填色按钮；已给 `.btn` 补两行（对 `<button>` 是无害的重复）。另删掉 `common.notReadyYet`（首页三处入口现在全是真的，零引用）。<br>**✅ 人眼验收（用户，2026-09-20）：五处全过、零缺陷** —— 逐字串在输入框旁边不在 `placeholder`、单目标时"以上 N 个均已授权"不渲染、空框不骂人、成功块里没有指向 `/scans/{id}` 的链接、首页「开始填写工单」是按钮样式的链接。**第一次人眼一处都没抓到**（T18a 抓到三处）—— 差别是那三处的教训（控件属性静默拦住的东西学不到、字段正交≠文案正交）这轮在交底任务书里就写成了硬要求。已 commit `68337a1`。 |
| T19 | 6 个场景模板 + 费用预估 + 测试账号收集 + 高级面板 | T18 | `frontend/src/components/wizard/*` `routes/templates.py` | **3** —— "费用预估"要如实展示 bearer 形状贵 4～6 倍这个真实取舍，是产品决策不是填表。读 T5／T18 的约定，**不再开** frontend-design |
| T20 | 发现 tab | T17 | `frontend/src/components/findings/*` | **3** |
| T21 | `Translator`（逐条 + executive、`Semaphore(4)`、JSON 修复、缓存表、费用核算）| T13 | `services/translator.py` `migrations/002_report_translations.sql` | **3** —— 含**新建迁移 = 表结构设计**（`agent-rules.md` §四 明列）；且"绝不翻译 `poc_script_code`／`evidence`／`endpoint`／`code_locations`"是硬约束，译错等于伪造证据<br>**2026-09-25 方案用户拍板（照推荐），拆 T21a→T21b→T21c 串行**，方案 `~/Documents/claude/dispatch/T21/plan.md`：① **复用 litellm**（`llm_client` 验活那条 kwargs 路径，web 进程本来就 import 它）→ §人话版中文报告 的"httpx 直连＋每家适配器"作废；费用 `completion_cost`，算不出存 NULL 不当 0；② **只手动触发**（不做 finished 自动翻）；③ **`poc_script_code`／`evidence`／`code_locations`／`poc_description` 不进模型输入**（白名单）。交底时核出：PLAN 原列名 `input_tokens`／`output_tokens` 含 `token` 会被 `assert_no_secret_columns()` 拒 → 改 `usage_prompt`／`usage_completion`；Strix 发现正文已是中英双语（`scan_templates.py:26`），Translator 实为"给管理层改写"。**T21a 交底** `~/Documents/claude/dispatch/T21a/prompt.md`（迁移 002＋`services/report_zh.py` 纯函数＋`PURGED_TABLES` 加表；I1 输入白名单、I2 解析器恰好键集＋无未译术语；≈460 行）。**收货 mutation**：M1 白名单改黑名单（只红未知键那格）、M2 解析器放行多余键（只红 `bad_keys` 多键那格）、M3 术语边界改 `\b`（只红 `/api/csrf-token` 那格）、M4 `PURGED_TABLES` 去掉新表（红留存那几条）。<br>**✅ 2026-09-25 T21a 收货**：迁移 28 行＋`report_zh.py` ≈240＋测试 242 行／27 用例，`retention` 加表；`make lint` 绿、`make test` **1369 passed / 0 skipped**。子 agent **8 次调用、峰值 ≈56k、0 压缩**，无偏离。mutation：M1 红 2、M2 红 2、M4 红 1（只有字面值那条钉着 —— `detail_rows` 按同一个常量遍历，去掉新表时行为测试跟着一起缩，可接受）、自选 M6 去 `IGNORECASE` 红 2。**两处 0 红，主会话已修**：① 我的 M3 只换了前边界，后边界仍挡住 `/api/csrf-token` → 前边界**没有测试**；正路那条改成 `/api/csrf 与 csrf-token`，M3a／M3b 各红 1；② 去掉剥 json 围栏那段 → 0 红："第一个 `{` 到最后一个 `}`"的回退本来就覆盖了它 → **删掉那段**。交给 T21b：`repair_messages` 遇到未知 reason 抛 `KeyError`（编程错误，不兜）；`parse_executive(require_not_tested=)` 由 T21b 按 `coverage` 有 gaps 或 `complete != true` 传 True。<br>**2026-09-25 T21b 拆两半**：① `llm_client.complete()`＋`Completion`／`CompletionFailed`／`Completer` **主会话直接做了**（+≈60 行＋`test_llm_client.py` 2 条；不变式 I5：`CompletionFailed` 的 args／`__cause__`／`__context__` 都连不回原异常 —— `raise` 必须写在 `except` 块外，块内 `from None` 清不掉 `__context__`；mutation 挪回块内 → 只红那一格，红的输出里正好带着 key）；② `services/translator.py`（I3 缓存、I4 失败不毒化＋钱照算）**已派发**，交底 `~/Documents/claude/dispatch/T21b/prompt.md`（模板 2，2 个新文件，≈450 行；预算 ≤40 次调用、峰值 <100k、0 压缩、第 5 次调用前开写）。交底时定的：`translate_scan(db, scan_id, run_dir, profile, credentials, completer, *, force)` —— run_dir 与 completer 由 T21c 传；报告正文经 `strix_bridge.projection.read_report_markdown`；总述 input_hash = executive user 消息的 sha256；`CompletionFailed` 的调用不计钱（无响应）；`payload_json` 不脱敏（与 `raw_json` 同级）。**收货 mutation**：M1 缓存查询不带 model（红换模型那格）、M2 失败也写行（红 I4 无行）、M3 失败条目的钱不计（红合计）、M4 `None` 当 0（红 None 传播）、M5 Semaphore 5（红并发峰值）、M6 第 2 次不用 `repair_messages`（红修复那格）。<br>**✅ 2026-09-25 T21b 收货**：`translator.py` 202 行＋`test_translator.py` 323 行／14 用例；`make lint` 绿、`make test` **1385 passed / 0 skipped**。子 agent **11 次调用、≈60k、0 压缩**。mutation 7 处全对：M1 红换模型那格、M2 红两次不合格那格、M3（失败条目不计钱）红两格（两条 I4 费用测试）、M4 红 None 传播、M5 红并发峰值、M6 红修复那格、自选 M7 `is not True`→`not complete` 只红 `"true"` 那格。整读无缺陷。**交给 T21c 两件**：① 非 `CompletionFailed`／`PayloadRejected` 的异常从 `TaskGroup` 冒出来是 **`ExceptionGroup`**，后台 task 的兜底要 `except* Exception` 或按 group 处理（否则状态卡在 running）；② 测试没钉"模型输出不进日志"（代码里只记 `failure_kind`）。<br>**2026-09-25 T21c 交底** `~/Documents/claude/dispatch/T21c/prompt.md`（模板 1 接线层，2 新文件＋6 处小改，≈480 行；预算 ≤35 次调用、峰值 <100k、0 压缩）。交底时定的：新路由文件 `routes/report.py`（不往 1450 行的 `scans.py` 里塞）；`app.state.report_jobs: dict[scan_id, ReportJob]`（任务强引用挂在 job 上），停机 **cancel 不等**；`app.state.llm_completer` 注入点；POST 顺序 not_found → artifacts_purged → 新码 `scan_not_finished`（非 `retention.TERMINAL_STATUSES`）→ `run_dir` NULL 并进 not_found → 新码 `report_in_progress` → acquire（**模型不必与扫描一致**）→ 占位建 task，查重到占位之间无 await；后台 `except Exception`（ExceptionGroup 是它的子类，交给 T21c 的 ① 用一个 handler 就接住）只记异常**类名**、不带 traceback（接 ②）；审计 `report.translated` 成功与异常都记（`outcome`）；GET 译文 JOIN 当前 `scan_findings.input_hash`、同 finding 多模型取最新；顶层 `cost_usd` 并进 `result`。app 级测试不能用 caplog（lifespan 清 root handlers）→ 给模块 logger 挂收集 handler。`FINDING_OK`／`EXEC_OK` 搬进 conftest 共用。**收货 mutation**：删 finally release（红 R1/R5）、删在翻判定（红 R3）、删终态判定（红 R4 一格）、force 写死 False（红 R2）、去掉 JOIN 的 input_hash 条件（红 R1）、`exc_types` 换成 `str(exc)`（红 R5）、删停机 cancel（红 R7）；自选至少一处。<br>**✅ 2026-09-25 T21c 收货**：`routes/report.py` 322 行＋`test_routes_report.py` ≈340 行／11 用例，6 处小改；`make lint` 绿、`make test` **1396 passed / 0 skipped**。子 agent **15 次调用、峰值 65k、0 压缩、第 6 次开写**。mutation 9 处：清单 7 处全对（各只红该红那几条）、自选 M9 不写 failed 红 R5；**自选 M8「acquire 挪到查重前」0 红** → 主会话给 R3 补了 FakeClock＋TTL 后 handle 失效的断言（409 那次不许占引用），复跑红 1。偏离全认（审计上下文收进 `_AuditContext`；替身多一个 `sleep_s`）。~~待定一件~~ **2026-09-26 用户拍板补上**：`_translate` 接 `CancelledError` → 写 `outcome="cancelled"` 再重抛（写得进库靠 `main.py` 在 `db.close()` 前 gather）；`test_shutdown_cancels_running_translation` 加审计断言，mutation 去掉该分支只红它。**T21 整行完成，下一步 T22**（导出 + 报告 tab，模板 3）。 |
| T22 | `exporter_html.py` 打印 CSS + 报告 tab | T21 T5 | `services/exporter_html.py` `frontend/src/components/report/*` | **3**  **✅ 2026-09-25 T22a／b／c 全部收货提交**（方案 `~/Documents/claude/dispatch/T22/plan.md`） |
| T23 | md/csv/sarif 直通导出 | T21 | `routes/reports.py` | 1<br>**✅ 2026-09-25 与 T24 合一条收货**：新 `routes/downloads.py`（不叫 `reports.py`）`GET /{id}/export/{md,csv,sarif}`，文件名进 `StrixProfile`（`report_markdown_name`／`vulnerabilities_csv_name`／`sarif_name`），前置判定 `_finished_scan` 同中文报告 POST；`_purged` 挪成 `audit.is_scan_purged`。前端 `DownloadBar`（终态才显示）。 |
| T24 | ~~专家 tab~~ → **降级成「下载原始 run 目录 zip」一个按钮**（2026-09-16 用户拍板砍范围）| T17 | `routes/reports.py`（加 `GET /api/scans/{id}/raw.zip`）| **1** —— 不再有 `components/expert/*`、不再自己维护第二份事件视图；前端只多一个按钮。**§专家模式 那节"不代理 Strix 自带 SPA"仍然有效**（那是禁区，不是范围）；砍掉的是"自己再渲染一份原始事件"。**zip 的内容安全性靠的是既有不变式**（验收 11：`grep -rI "$TEST_KEY" $DATA/` 含 run 目录与 `strix.log` 为 0），**不许为它新造一条读文件路径**：只打包 `${DATA}/scans/<id>/` 下已落盘的产物，**排除 `tmp/`**<br>**✅ 2026-09-25 收货**（与 T23 同一提交）：`services/raw_export.build_raw_zip`（排除**顶层** `tmp/`、不跟 symlink 文件／目录，内存建 zip）；zip 提示"含测试账号"。21 次调用、0 压缩。mutation：不剔 symlink 文件 → 只红该格；不剔顶层 tmp → 只红该格。 |
| T25 | ~~审计 UI~~ 砍掉，**只留 `GET /api/audit.csv` 导出接口**（2026-09-16 用户拍板；`audit_log` 表 + `${DATA}/audit/YYYY-MM.ndjson` 双写 T12c 已落地，日常查询用 `grep`）| T12c | `routes/audit.py` | **1** —— 砍掉的只是页面，**验收 19 的实质不变**（六类事件齐全 + 对 CSV `grep -c "$TEST_KEY"` 为 0）。原话"验收 19 条一字不改"**已作废**：2026-09-17 收货时对出两处事件名与代码不符，用户拍板**只改验收/设计文案、不改代码**（`override.loopback_used` → 查 `authorization.affirmed` 的 detail；`key.forgotten` → `key.dropped`），理由记在验收 19 与 §护栏＋审计 那两段 |
| T26 | ~~系统诊断页~~ → **并进首页一个可展开区块**（2026-09-16 用户拍板）| T3 T10 | `frontend/src/components/system/ReadyRows.tsx`（扩写）`frontend/messages/zh-CN.json` | **1** —— T3 已返回结构化 JSON、首页 `ReadyRows` 已接真值（`5ddede6`），本行只是给每个失败码配中文修复指引（指引文案进 `zh-CN.json`；**失败码清单以 T3／T10 的实现为权威，不许自己发明码**）。**不新建 `app/diagnostics/`** |
| T27 | `exporter_docx.py` —— 手写 WordprocessingML，**不引 `python-docx`** | T22 | `services/exporter_docx.py` | 2 —— **2026-09-25 拆成 T27a（渲染纯函数 + 与 HTML 版共用公共件，已派发）／T27b（`GET report/docx` 路由 + 下载按钮，串在 T20 后）**，见 §交接 |
| T28 | ~~续跑（重新索要 Key）+ 并发队列~~ **v1 不做**，只留**留存清理任务**（2026-09-16 用户拍板砍范围；**续跑已于 2026-09-24 恢复，拆成 T31a–T31c 三行，见本表下方；并发队列仍砍**）| T10 | `services/retention.py` | **2 —— ⚠️ 破坏性操作**（会删用户的扫描产物）。**2026-09-18 拆成 T28a（判定 + 执行器，任务书 `/tmp/T28a-prompt.md`）与接线（`settings.py` 加 `CONSOLE_RETENTION_DAYS`、`main.py` 定时任务、审计事件、`.env.example`、前端 404 文案）两条。**<br>**留存策略已由用户 2026-09-18 拍板（属"已确认决策"，改要先问）**：① **删的范围** = 整个 `${DATA}/scans/<id>/` 目录 + **4 张表**（`scan_events`／`scan_media`／`scan_agents`／`scan_findings`）里该 scan 的行；**保留 `scans` 那一行**（历史列表仍看得到摘要/结论计数/成本）；**保留** `authorizations`／`audit_log`／`${DATA}/audit/*.ndjson`；**表结构不改**（无新列无新迁移），前端拿不到产物时按 404 显示"产物已按留存策略清理"。⚠️ **`report_translations` 也该删，但那张表 `001_init.sql` 里不存在**（T21 的 `002` 才建）→ **T21 落地时必须把它加进清理清单**，否则译文会活过它的原文。② **默认关闭**：`CONSOLE_RETENTION_DAYS` 默认 `0` = 永不自动删，用户在 `.env` 显式写 `30` 才生效（理由：这是渗透测试证据，默认销毁是不可逆的坏默认）。开启后 api 启动跑一次 + 每 24h 一次；每次先 INFO 打印将删清单 → 再删 → 写审计事件。③ 目录里仍有 `tmp/` → **整条跳过**并记 `tmp_dir_present`（说明 supervisor 清理没跑完或 scan 其实还活着）。<br>prompt 必须写死：只删 `${DATA}/scans/<自己创建的 scan_id>/`、先 dry-run 打印再删、绝不递归删 `${DATA}` 下其他任何目录；**不许碰 `${DATA}/scans/<id>/tmp`**（那是 `cleanup_scan_tmpdir()` 的地盘，T10 交回，同 T11 那条禁令）；删的是**整个** `scans/<id>/`，且只在扫描已终态之后。<br>**砍掉那两件的连带影响（别丢）**：① **验收 18 整条删除**、**验收 17 末句**（"重启后点继续扫描会预填 provider/model"）删除；② `--resume` 的三条约束（收 **run name** = `strix_runs/` 下目录名，来源 `ScanProcess.run.run_name`；必须**同一个 cwd**，否则 `interface/utils.py:1561` + `cli_args.py:422` 的校验会拒；必须显式 `-m <持久化的模式>` 且**无 `-t`**）**原样留在此处**，将来要做直接照抄；③ T10 已落进 `scans.strix_run_name` 的字段**保留、暂不使用**，不许为"砍了续跑"去删列（删列要写迁移，比留一个空列贵）；④ **没有队列** = 超过并发槽位上限直接返回 `too_many_running`（T12c 的槽位闸已经就是这个行为），不排队。<br>**✅ 2026-09-18 T28a 收货（判定 + 执行器；接线仍未做）**：`services/retention.py` **229** 行（超 200 行预算 29 行，全是"为什么"的注释，判定不值得再压）+ `tests/test_retention.py` **282** 行，新增 **28** 条。形状照 `reaper.py`（本仓另一个破坏性操作）：判定是无 IO 纯函数（`plan_retention`／`scan_dir_for`）、动手前把整条计划打进日志（`dry_run` 与否都打）、`dry_run=True` 一个字节都不动、**默认不删**。<br>**路径护栏的承重点是 `resolve()` 之后"父目录恰好是 `scans_dir`"，不是字符串黑名单**：黑名单永远漏一种写法、且抓不到符号链接（`scans/looks-fine` 指向别人的目录时字符串完全干净，有一条专门测试钉它）。前置的 `("/", "\\", "\x00")` 检查只是提前挡掉分隔符与 NUL（**反斜杠在 POSIX 上是合法文件名字符，`resolve()` 抓不到它**；NUL 会让 `os.lstat` 抛 `ValueError`，那不是我们的机器码）。<br>**2 处 mutation**：① 删掉 `retention_days <= 0` 的短路 → 红 2（`retention_days=0`／为负那两格）。**全模块只有这一处短路**（`sweep()` 里刻意没有第二处提前返回，否则这个 mutation 杀不死）；② **`_delete_detail_rows` 顺手把 `scans` 那一行也删了**（**我自选的靶子，不在它清单上**）→ 红 1（`test_sweep_purges_..._but_keeps_the_scan_row`）—— 证明"摘要永不删"真的有测试承重。<br>**先删目录后删 DB 行**是刻意的：目录删不掉就整条不动（明细行还在，下一轮再来）；反过来会留下"DB 说没有、磁盘上还占着几百 MB"。中途崩了也自愈（下一轮 `_remove_dir` 吞掉 `FileNotFoundError` 再删 DB 行）。<br>**交回给接线任务的两件**（详见 §交接）：`InvalidScanIdError` 刻意不继承 `ConsoleError`（要暴露成 HTTP 就必须同时补 `zh-CN.json` 文案）；`RetentionOutcome` 没有 `failed` 字段，`_remove_dir` 的其它 `OSError` 会**中断整轮**，要改行为得先改契约。<br>**`test_purged_tables_is_exactly_the_four_detail_tables` 是给 T21 的定时炸弹**：T21 建 `report_translations` 后必须把它加进 `PURGED_TABLES`，那条测试会在那天变红，正好当提醒。<br>**✅ 2026-09-18 W1（接线）收货 —— T28 整行完成**：`settings.py`(+6：`console_retention_days: int = Field(default=0, ge=0)`) + `audit.py`(+1：`EVENT_SCAN_PURGED = "scan.purged"`) + `retention.py`(+67：`audit_dir` 必填构造参数、`sweep()` 里每删一个 scan 写一条审计、`run_forever`) + `main.py`(+25：`app.state.retention_sweeper` + `create_task` + `finally` 里 cancel+await **排在 `db.close()` 之前**) + `docker-compose.yml`／`env.example` + `tests/test_retention.py`(+231，新增 6 条 `# F. 接线`) + 主会话补的 `test_settings.py::test_retention_is_off_by_default`。`make test` **1079 passed / 0 skipped**。<br>**开关只有一个落点：`run_forever` 里的 `if self._retention_days <= 0: return`**（`main.py` 里**不许**再写 `if ... > 0`；`plan_retention` 那处是判定层的第二道，两道都留）。`create_task` 而不是 `await` 一次：第一轮要删几十个目录时 `await` 会把"清理很慢"变成"api 起不来"。`run_forever` 里 `except OSError` 是**必须**的 —— 后台任务带异常死掉会在停机 `await` 它时把异常重抛进 lifespan 的 `finally`。<br>**`audit_dir` 刻意是必填关键字参数**（无默认值）：一个可选的审计目录等于让"忘了传"变成"静默不写审计"，而这个模块删的是渗透测试证据。`detail` 只有 `{retention_days, tables}`，**没有路径**（MX4 钉住了这一点）。<br>**⚠️ MX1 的教训（第四次同形状缺陷）**：默认值 `0`→`30` 曾经**全量无一变红**。"参数从 `Settings` 来"（F6）与"那个默认值是 0"是两条不变式。详见 §交接。<br>**子 agent 三处刻意偏离，都认下**：① F1 用 `asyncio.wait_for(..., timeout=5.0)` 包了 `run_forever` —— 裸 `asyncio.run` 在开关被改坏时是**挂死**而不是变红，mutation 就无法收敛；② F6 的 `FakeTransport` 要带一条 `("GET", "/containers/json", const(200, []))`，否则同一个 lifespan 里真实 `Reaper` 的第一轮清扫会以"替身没准备应答"报错、失败信息指向 reaper；③ 测试直接 `from app.services.retention import _remove_dir`（属性访问会踩 ruff SLF001）。<br>**已知不做**：`docs/SECURITY-zh.md` 还没有"哪些数据留多久"一节（T26 补）；前端 404 文案随 T16（见 §交接）。 |
| T31a | **续跑 argv**：`build_resume_argv`（`strix -n --resume <strix_run_name> -m <持久化模式> --max-budget-usd <总额> --max-turns <n> --config <新 cli-config>`，**无 `-t`、无 `--instruction-file`**）+ 同一 cwd 的 `LaunchPlan` 变体 + 黄金 argv 测试 | T9 | `services/scan_launcher.py` `tests/test_scan_launcher.py` | **2**（无 IO 纯函数 → TDD 第一层）—— 2026-09-24 用户拍板恢复续跑。**Strix 侧已核（1.6.2 源码）**：`interface/cli_args.py:346-360` 带 `-t` 即报错、缺 `.state/agents.json` 即报错；`report/state.py:240` 起的 hydrate 把上次 `llm_usage` 读回来，`core/hooks.py::recomputed_budget_flags` 用新的 `--max-budget-usd` 重算 ⇒ **上限是总额不是增量**，校验必须是 `新总额 > 已花费`（等于也不行，一启动就停）。`--max-budget-usd` 强制必填的不变式对续跑同样成立<br>**✅ 2026-09-24 收货**：`scan_launcher.py` 479 行（+136/−14：`ResumeSpec`、`_validate_limits` 抽出共用、`_validate_resume`、`build_resume_argv`、`_prepare_home` 抽出、`build_resume_plan`；`LaunchPlan` 两个 instruction 字段可空）+ 测试 +101 行、新增 14 格。`make lint-api` 绿、`make test` **1230 passed / 0 skipped**。**3 处 mutation 全对**：M1 `<=`→`<` 只红 `[5.1382-5.1382]`；M2 删续跑 `-m` 只红 `test_golden_resume_argv`；M3 `tmp` 加 `parents=True` 只红 `test_resume_plan_does_not_recreate_a_purged_scan_dir`。子 agent 9 次工具、峰值 58k、0 压缩、第 6 次调用开始写码 —— **粒度对**（交底文件 + 附录源码事实的形状可复用）。 |
| T31b | **拆成 T31b1–T31b5**（2026-09-24 主会话写方案、用户拍板照推荐执行；原行文字保留在本格末尾）。端点 `POST /api/scans/{id}/resume {vault_handle, max_budget_usd}` → 202（复用 `ScanAcceptedResponse`）。**用户拍板两件**：① 续跑的测试账号口令**从 `run.json.instruction` 按我们自己的格式解析回来**再登记脱敏（不让用户重填）；② `provider`／`auth_shape`／`strix_llm` **三者都必须与原行一致**，否则 `key_required{provider, auth_shape}`。**主会话核出的四件（原行没写）**：A 续跑时 Strix 把旧对话（含 `password=`）整份重推进 `scan_events`，而 `scan_secrets` 里没有这次的口令 → **明文进 DB**（→ b3）；B 验活失败 `interface/main.py:406` 早于 `cli.py:105` 把 `run.json` 改回 `running` → 退出码 1 + 旧 `status=stopped` 被判成 `scan_incomplete`（→ b1）；C 缺 `.state/agents.json` 时 Strix 走 `parser.error`（`cli_args.py:354`）= **退出码 2**，会被读成"发现漏洞" → 路由必须先查（→ b4/b5）；D **`create_scan` 的 `finally` 从没调过 `cleanup_workspace`**（`start()` 抛 OSError／写库失败时 tmpfs 上带口令的 `instruction.txt` 一直留着；T31a 交接那句"照 create_scan 调"是错的）→ b5 两条路径一起修。另：`resume_available` 列**从无写入点**（恒 0）→ 准入现场推导、不用该列、不删；`docker kill` 残行 `strix_run_name` 为 NULL → 回落 `discover_run(cwd)`。第一波 b1–b4 文件互不相交、并行；b5 串行在后 | T31a | 见下五行 | 交底 `~/Documents/claude/dispatch/T31b{1..4}/prompt.md`。原行：准入：已终态且 `error_code ∈ {scan_incomplete, stopped_by_operator, interrupted_by_restart}`、`strix_run_name` 非空、`resume_available=1`、`strix_version` 与当前一致、产物未被留存清理、并发槽位、`auth_shape` 与 handle 一致（失效 → `409 key_required`）、**按原授权重解析 DNS 比对**（安全不变式，续跑不豁免）。**已查到的三个坑**：① `ChannelRegistry.open` 新建的 channel 从 epoch 0 起（`scan_channel.py:153` + `run_projector.py:113`），而 `scan_events` 是 `PK(scan_id, epoch, seq)` → 续跑第一帧就撞主键，**必须从 `scans.current_epoch + 1` 起步**；② 续跑启动时 `run.json` 里已经躺着上次的 `status=stopped` —— 新进程若在重写它之前崩了，判定表会读到旧值，要确认退出码 1 的分支优先于它；③ `RunDiscovery` 在 cwd 下会直接看到旧目录（这正是要的，但它不是"新出现的"，测试要覆盖）。`finally` 顺序（`channels.close` 先于 `forget`／`release`）照抄 `create_scan` |
| T31b1 | `run.json` 旧状态识别：`ScanSupervisor.start(..., resume: bool = False)`；`resume=True` 时 exec **之前**记下 `run.json` 的 `(st_ino, st_mtime_ns, st_size)`，收尾时没变过 → 当 `run_status=None`（走 stdout 归因） | T31a | `services/scan_supervisor.py` `tests/test_scan_supervisor.py` | **2**（TDD 第一层），~150 行。**收货 mutation**：删掉那次比较 → 只红"续跑 + exit 1 + 旧 stopped → failed"；自选：`resume` 默认改 True → 应红既有预置 `run.json` 的那批 |<br>**✅ 2026-09-24 收货**：`scan_supervisor.py` +35／测试 +53（3 格）。mutation：删指纹比较只红 `test_resume_ignores_untouched_stale_run_status`；默认改 True 红 4（3 条既有预置 `run.json` 的 + 新守卫）。16 次工具、峰值 67k、0 压缩（中途一次 API 400 断线，原会话续上）
| T31b2 | `ChannelRegistry.open(scan_id, cwd, *, start_epoch)` 必填 → 初始投影 epoch = `start_epoch`；`_run_to_completion(..., start_epoch)` 透传、`create_scan` 传 0 | T31a | `services/scan_channel.py` `tests/test_scan_channel.py` `routes/scans.py`（仅两处） | **2**，~90 行。**收货 mutation**：初始投影不用 `start_epoch` → 只红新增那条 |<br>**✅ 2026-09-24 收货**：+51／−14。mutation 只红 `test_the_first_frame_starts_at_the_given_epoch`。**交底漏了一处**：`test_routes_scans.py:851` 的 `OrderRecordingChannels.open` 替身签名也要加 `start_epoch`，主会话补了一行。12 次工具、峰值 66k、0 压缩
| T31b3 | 口令往返：`recover_test_credentials(instruction)` 纯函数 + `_validate` 拒收"格式化后解析不回原值"的账号（`field=credentials`，任何 IO 之前）+ `run_discovery.read_run_instruction` | T31a | `services/scan_launcher.py` `services/run_discovery.py` 及两份测试 | **2**（TDD 第一层），~160 行。**收货 mutation**：解析改用最后一次出现的分隔符 → 只红"口令里含 ` password=`"那格；自选：删 `_validate` 的往返检查 → 只红含换行那格 |<br>**✅ 2026-09-24 收货**：+15 格。mutation：`rpartition` 红 2（口令含 ` password=` 那格 + username 含 ` password=` 的 `_validate` 那格 —— 后者也该红，`rpartition` 下那种 username 反倒能往返）；**换行与往返是两道检查**（换行在单行往返里是能还原的，往返查不出）：删换行只红换行格、删往返红另两格。留给 b5 的一件：无账号但操作者补充里出现与账号段标题完全相同的一行 → `recover` 会去解析其后的行（抛 `ValueError` = 拒续跑，或多登记几个脱敏值），两种都不漏脱敏，不修。12 次工具、峰值 71k、0 压缩
| T31b4 | 准入判定表：新建 `services/scan_resume.py`：`ResumeFacts` + `resume_refusal(facts) -> str \| None`（`not_resumable`／`strix_version_changed`／`no_checkpoint`） | T31a | `services/scan_resume.py` `tests/test_scan_resume.py` | **2**（TDD 第一层），~120 行。**收货 mutation**：删版本那条 → 只红版本那格 |<br>**✅ 2026-09-24 收货**：`scan_resume.py` 52 行＋19 格。mutation 删版本判断红 3（两格版本 + 优先级 2+3 那格，均为版本格）。10 次工具、峰值 43k、0 压缩（中途一次 DNS 断线，原会话续上）
| T31b5 | 路由 + 新码 `409 resume_unavailable{reason}`（`errors.py`＋`zh-CN.json`）+ `EVENT_SCAN_RESUMED` + D 的修复 + `GET /api/scans/{id}` 补 `auth_shape`（T31c 要） | b1–b4 | `routes/scans.py` `errors.py` `services/audit.py` `zh-CN.json` `tests/test_routes_scans.py` | **2**（接线层 + 两条安全接线测试），~350 行，**交底等 b1–b4 收货后写**。**锁内顺序**：槽位 → 读 `scans`＋`authorizations`（无 → `not_found`）→ `resume_refusal`（run 名 = `strix_run_name or discover_run(cwd)`；`has_checkpoint` = `.state/agents.json` 存在）→ 审计有 `scan.purged` → `artifacts_purged` → `vault.acquire`（无或三者不一致 → release + `key_required`）→ **try**：`read_run_instruction` → `recover_test_credentials`（`None`／`ValueError` → `resume_unavailable(no_checkpoint)`）→ `scan_secrets.register` **先于一切可能打日志的步骤** → 用授权行 `targets_json`(raw)／`resolved_ips_json`(当 declared)／`typed_confirmation`／`overrides_json` + 现场 DNS + **当前**白名单重跑 `evaluate_admission`（拒绝先审计再抛）→ `build_resume_plan`（to_thread；`FileNotFoundError` → `artifacts_purged`）→ 条件更新 `SET status='starting'`、清终态列、新 `vault_handle`／`max_budget_usd`／`argv_json`／`env_var_names_json`（`instruction_sha256` 不动）`WHERE id=? AND status IN ('stopped','interrupted')` → `supervisor.start(..., resume=True)` → `_run_to_completion(start_epoch = COALESCE(MAX(scan_events.epoch), -1) + 1)`（以 `scan_events` 为准，同 `event_replay`）→ 锁外 `scan.resumed`（argv、变量名、pid、新旧预算、`spent_usd`=`scans.cost_usd`、上次 `error_code`）。`finally` 未起：`forget` + `release` + `cleanup_workspace`。**必须能变红的接线测试**：DNS 变了 → 409 且没起进程；`start()` 抛 OSError → tmpfs `scan-<id>/` 不存在（create 与 resume 各一格）；续跑经路由 exit 1 → `failed`（钉 `resume=True` 有传） |<br>**✅ 2026-09-24 收货**：`routes/scans.py` +304（预估 170：docstring 续跑小节 + `_checkpoint_facts` + 两段 SELECT 常量）、测试 +217、errors/audit/zh-CN/覆盖测试 +43。`make lint-api` 绿、`make test` **1280 passed / 0 skipped**。**11 处 mutation 全部只红该红的一条**：删 register、`resume=False`、清 `error_code` 那句（R1）；`start_epoch` 写死 0（R2）；跳过准入拒绝（R3）；删两处 `cleanup_workspace`（R4 各一格）；三元比较（R6）；purged 挪到判定后（R7）；自选：拒绝路径先清 HOME（R5）、**不回落 `discover_run` → 0 红 = 缺口**，主会话补了 `test_resume_finds_the_run_on_disk_when_the_row_has_no_run_name` 后变红 1。主会话另修一处：`_SELECT_RESUME_SCAN` 插在了 `scan_findings` 那句注释与它的常量之间。**粒度信号**：23 次工具、0 压缩，但**峰值 126k（超 120k，§九.6①）** —— 两个要写的文件各 1100+ 行必须整读，这就是地板；下次同形状（改一个千行路由 + 千行测试）要么把测试拆成独立文件、要么接受这个峰值。**子 agent 报的遗留（不阻塞）**：① 续跑 `start()` 抛 OSError → 行 `failed`、不可再续（已知接受）；② `zh-CN.json` 的 `concurrency_limit.params` 写 `running_scan_id`，后端实际抛 `active` —— 文案与参数名不一致，**T31c 顺手改**；③ `mark_finished` 会用 `outcome.strix_run_name` 覆写该列，续跑结局没拿到 run 名时列变 NULL，下次续跑靠 `discover_run` 回落（本次已补测试）。
| T31c | `/scans/[id]` 的「提高上限并继续」：按钮（仅准入码三种且非运行中）+ 对话框（已花费、新总额、handle 失效时按 `auth_shape` 重新索要凭据、预填 provider/model）+ 续跑后重连 WS、把 `scan_incomplete`／`stopped_by_operator` 的 action 文案改回"可以继续" | T31b | `frontend/src/app/scans/[id]/`、`frontend/src/components/live/`、`zh-CN.json` | **3 + frontend-design** —— 纯前端，0 mutation；验收 17 末句 + 验收 18 前端半边 + 一次真跑（juice-shop 小预算停下 → 续跑，约 $1–3）|
| T32a | **coverage 判定**：`run_status=="completed"` 且 `coverage.json.completeness.complete is not True`（含缺失/读坏）→ `status="stopped"`＋新码 `coverage_incomplete`；`strix_profile` 加 `coverage_record_name`、`run_discovery.read_coverage_complete`（不抛）、`resolve_attribution` 加参；码进 `errors.py`＋`zh-CN.json`（`LivePanel.tsx` 可续码表交底时挪到 T32b）。历史行不回溯 | T31 | `strix_profile.py` `run_discovery.py` `scan_supervisor.py` `errors.py` `zh-CN.json` + 测试 | ✅ 2026-09-24 收货（1293 passed，3 mutation 全对）。**2**（TDD 第一层，发布阻断同条 24）。方案 `~/Documents/claude/dispatch/T32/plan.md`。**收货 mutation**：删 coverage 判断 → 只红那格 |
| T32b | **续跑指令＋准入**：`compose_resume_instruction(original, *, spent_usd, budget_usd)` 纯函数（剥旧 `## Resumed scan` 节 + 追加固定英文模板，**不拼 gap 正文**）→ tmpfs `instruction.txt`(0600) + 续跑 argv 加 `--instruction-file`；`RESUMABLE_ERROR_CODES` 加 `coverage_incomplete`。理由 N1：新指令会覆写 `run.json.instruction`（`cli_args.py:428`），只写补测提示会丢账号段 → 下次续跑口令明文进 DB | T32a | `scan_launcher.py` `scan_resume.py` `routes/scans.py`（一处传参）`LivePanel.tsx`（可续码表加 `coverage_incomplete`，从 T32a 挪来）+ 测试 | ✅ 2026-09-24 收货（1303 passed，4 mutation 全对）。**2**。不变式：`recover(compose_resume(x)) == recover(x)` 且多次续跑不叠加；黄金续跑 argv。**收货 mutation**：不剥旧节 → 只红叠加格 |
| T32c | **预算软提示**：前端常量 $4；发起页 `< 4`、续跑页 `新总额−已花费 < 4` 显示提示，仍可提交 | T32b | `StepBudget.tsx` `ResumePanel.tsx` `zh-CN.json` | 前端小改，0 mutation；**✅ 2026-09-24 主会话直接落地**，lint/test 绿，人眼待看 |
| T32d | **真跑验收**：bearer 小预算 juice-shop → `coverage_incomplete`/`scan_incomplete` → 续跑 → root **新派**子 agent 并发出探测（验 N3：completed 的 root 续跑会动） | T32c | — | **自** ✅ 2026-09-24 **部分通过 + 得出限制**：sigv4 access key、$2 首段（3 子 agent 全在 90% reserve 线被掐、0 探测）→ 判定「结论不完整」页面文案正确（T32a 通）→ 续跑抬到 $4/$6、日志确认 `injected new instruction`（T32b 通）→ **但 root 两次续跑（原措辞 + 加强措辞）都直接 `finish_scan`、不新派子 agent**（N3 = 否，Strix 续跑语义所致）。加强措辞反而让第二次 root 跳过 `list_coverage` 直接总结。**用户拍板（2026-09-24）接受此限制、如实文档**（见 T30a 行 `docs/SECURITY-zh.md` 清单 ④）；`compose_resume_instruction` 的强指令措辞保留（干净起点下可能略好、无害）。**预算软提示（T32c）真跑页面已见** **⚠️ 2026-09-25 推翻**：其中「子 agent 不可复活、续跑不会补测」只对不动盘上状态成立 —— 改 `agents.json` 状态字符串即可复活，见 §交接 与 `pitfalls` 条 42。 |
| T29 | `test_strix_contract.py`（升级预警线）+ `importlinter.ini` | T13 | `tests/test_strix_contract.py` `backend/importlinter.ini` | **1** —— 断言清单已被 §Strix 集成面 与 §import 边界 钉死，本任务是照着写。**prompt 必须写死"断言只许来自那两节，不许自己发明"** —— 发明的断言会让升级预警线失效<br>**✅ 2026-09-25 收货**：**未引 import-linter**（不在 dev 锁里），改为 `test_strix_contract.py` 第二部分 AST 扫 `app/`；契约 A–M 共 33 格。**M 在 1.6.2 上本来就红**：两个允许模块传递 import `strix.core.paths`（纯常量）→ 用户拍板精确放行 `{strix.core, strix.core.paths}` + 断言 `core/paths.py` 无 `environ`。J 字面量实为 `url.startswith("data:image/")`（接受）；I 的默认 3 在 `config/settings.py`。11 次调用、0 压缩。mutation：app 内加 `from strix.core import paths` → 只红边界格；上游加 `"--model"` → 只红该格；上游 paths 碰 environ → 只红 stateless 格。 |
| T30a | `README.md` + `docs/` 四份文档 | 全部 | `README.md` `docs/*` | 1 —— **是改写现有的 `README.md`（2026-09-09 提前写的临时版，因为仓库 public 而合规声明不该等到 M8），不是新建；必须保留合规声明原文**（见 §合规声明），其中那张手工维护的状态表到时整段删掉。**两条残余风险必须落进 `docs/SECURITY-zh.md`**：① T5b 那行的口令同步；② `POST /api/targets/validate` 拒绝 `https://user:pass@host` 时会在 200 正文的 `raw` 字段**原样回显一次**（只有这一处，零日志调用，走 TLS 回给刚打出它的人）—— 如实记录，不靠"整理干净再回显"消除，那会擦掉用户唯一的线索；③ **测试账号口令明文留在 `run.json` 的 `instruction` 字段**（Strix 为续跑写的，2026-09-24 用户拍板接受）—— 要写清：它在 `${DATA}` 上活到该扫描被留存清理为止，而清理默认关闭 |<br>**④ 续跑的能力边界（2026-09-26 用户拍板按 09-25 的推翻改写；09-24 那版"root 不会重新派子 agent、续跑不会补测"作废）**：续跑能抬预算、带记忆恢复 root、注入新指令，且**会复活被强停的子 agent**（`agent_checkpoint.revive_checkpoint` 在 Strix 启动前把非根 `stopped`／`budget_paused` 翻成 `running` 并清过期预算 flag，2026-09-25 实测三个子 agent 全部复活并真的在探测；机制见 `pitfalls` 条 42）。**尚未实测证明的一环要如实写成"未验证"**：复活且 root 不掐死它们之后，是否真的把缺口记进 coverage、让结论变完整。文档与 UI **不得承诺"续跑能补全覆盖"**，只许说"会让停掉的子任务接着跑，补没补齐看这次的覆盖记录"（`zh-CN.json` 的 `coverage_incomplete.action` 已于 2026-09-26 按此改）。另写明：复活依赖直接改写 Strix 的 `.state/agents.json`（非公开接口），升级 `strix-agent` 时 `test_agent_checkpoint.py` 末四条是预警线 |
| T30b | `make verify-e2e`（**28 条**）| T30a | `Makefile` `scripts/verify_e2e.sh` | **2** —— 写 shell 断言是本项目**踩过坑**的地方：`pitfalls` 条 18、条 23 末段（"检查都通过" ≠ "被检查的事真发生了"，M0 就这么假绿过一次）。**安全门 6–11、22、25 由我逐条复跑复核，不采信子 agent 的结论**。<br>**2026-09-26 用户拍板拆法**：**T30b1 ✅**（不花钱的 1–5、21–24、27、28）→ **T30b2a**（5b、6–12、25、26）→ **T30b2b**（17–20、13、19）→ **T30b3** 压缩韧性（14）；15、16 做成人工核对清单（脚本只打印步骤）。b2 再拆成两条的理由与钱怎么花见 §交接。要花钱的段**只收 `bedrock_sigv4` 凭据、预算默认 $4**（参数可改）。**`make reap`（T30c）：`api` 在跑时只许 `--dry-run`**，要删得先停 api（T11a 交代的安全问题）。**Key 绝不进任何进程的 argv**：`grep "$TEST_KEY"` 在宿主 `ps` 上可见 → 一律 `grep -F -f -` 从 stdin 喂 |

**可并行组**（不共享文件，同批发出）：`T3∥T4`、`T6∥T7`、`T15b∥T16`、`T19∥T20`、`T23∥T25`、`T27∥T28∥T29`。
**⚠️ 2026-09-16 砍范围后新增一处文件撞车**：T24 的 `raw.zip` 端点也落在 `routes/reports.py`，与 T23 同文件
→ **T23 与 T24 不许同批派发**（§九.7：并发时双改是后写覆盖且无冲突提示）。两行现在都是模板 1、都只往
`routes/reports.py` 加端点，**收货时按提交边界收一次**即可（§八.3 末句）；真要省一次派发就合成一行。
其余全部串行 —— T2 与 T13 是两个瓶颈，几乎所有东西挂在它们后面。同时在跑的 subagent **≤3**。
**T15a 是「自」，不占 subagent 名额**，但它卡着 T15b。

**每个模板 2/3 的 prompt 必须写死**（缺一不发）：
1. **Superpowers 只作用于本子任务** —— 不得触发新一轮 Plan、不得再派生任何子 agent（§六.3/§六.4）
2. 边界：只许改「涉及文件」列里的路径；不许 `git commit`/`push`、不许改 `PLAN.md`/`CLAUDE.md`/`agent-rules.md`。
   **依赖（2026-09-08 修）**：原文写的是"不许装依赖"，但 T2 实测发现镜像里连 `fastapi` 都没有 ——
   那条规则与任务定义本身冲突，按字面执行则 T2 不可能完成。改为：**新增依赖必须先上报并经批准，
   且只能走 `make lock` 进 hash lock**；**绝不许**子 agent 自行 `pip install`、绕过 hash lock、
   或放宽 `strix-agent==1.6.2` 的 pin。改 lock 的代价要一并报（重建镜像约 6–15 分钟，见 `pitfalls` 条 10/11/15）
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
| R4 | Strix 1.5.4+ 破坏我们（依赖 `tui.backend.live_view`、`viewer.transcript`、`core.paths`、run 目录布局、`agents.db` 结构、Rich 面板标题、退出码、两个未文档化 env）| 高 | `strix-agent==1.6.2` 精确 pin + `--only-binary=:all:` + hash lock（**已核实 1.5.3 有 x86_64 与 aarch64 两个 manylinux wheel，无需 Go 工具链**；sdist 的 hatch 钩子缺 Go 1.24 会硬失败）；`test_strix_contract.py` 导入真实包断言每个符号/签名/env 名；import-linter 禁止 `app.*` 越过 `strix_bridge` |
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

## 端到端验收（`make verify-e2e`；**28 条**（18 号续跑 2026-09-24 恢复）；6–11 是 Key 卫生安全门，22 与 25 是 TLS 阻断项，27 与 28 是登录阻断项，全绿才能发布）

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
5. **Key 注册**：垃圾 Key → 400 `key_verify_failed` 且在验活超时（`VERIFY_TIMEOUT_SECONDS`，现 8s）内返回、无 DB 行（**2026-09-26 用户拍板改**：原"2 秒内"取决于到 Bedrock 的网络，本机经防火墙实测 4.1s）；真 Key → 201 带掩码标签；
   `grep -r "$TEST_KEY" $DATA/` → **0 命中**
6. **发起**：把 `localhost` 加入白名单（`allow_loopback:true`）后发起快速体检、预算 $3。
   `argv_preview` 含 `-n -t http://localhost:13000 -m quick --max-budget-usd 3 …` 且**无 Key**
7. **实时流**：90 秒内依次看到 `agents` 出现 Root Agent → `event.add` 的 chat/tool → `log` →
   `summary` 且 `cost_usd` 递增（**2026-09-22 改**：原先第一格要求 `phase: pulling_image|
   starting_sandbox` → `setting_up_proxy`，而 `phase` 帧已删 —— 镜像拉取那一段的可见性由
   `/ws/system` 的拉取进度帧负责，不在本条的 90 秒窗口里）；
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
    **containment 那一格（2026-09-26 改，原文对 `bedrock_sigv4` 是错的）**：原写"应恰好有一处命中：
    `/run/strix/` 下那个预置的 `--config` 文件"—— 但 `persist_current()` 只写自己 settings 字段的 alias，
    `AWS_ACCESS_KEY_ID`／`AWS_SECRET_ACCESS_KEY` **不是**任何 alias（`pitfalls` 条 25 的实测表），
    该文件里根本没有它们。改成：`/run/strix/` 下**也是 0 命中**，阳性对照 = `cli-config.json` 比预置的
    `{"env":{}}`（11 字节）变大了且含 `STRIX_LLM`（证明 `persist_current()` 跑过、看的是对的文件）。
    **`single` 形状（`LLM_API_KEY`）下仍会明文落盘**，所以那三道防线一条都不许撤
12. **优雅停止 + 回收**：45 秒内 WS 收 `done`（**纯信号、不带结论**）**且** `GET /api/scans/{id}` 给
    `status: stopped` + `error_code: stopped_by_operator` + `exit_meaning ∈ EXIT_MEANINGS`
    （2026-09-23 改：原文写的 `done{exit_meaning:stopped}` 与 W2 方案 ②「`done` 帧不带结论、结论唯一
    出处是 `GET /api/scans/{id}`」直接冲突，照字面写断言会永远失败。**2026-09-26 再改**：`stopped`
    根本不是 `exit_meaning` 的合法值 —— `EXIT_MEANINGS` 只有 `vulnerabilities_found` /
    `no_vulnerabilities_found` / `failed` 三个（`scan_supervisor.py:73`），"停了"这件事在 `status`
    与 `error_code` 上。**改验收不改代码**：那套词汇已经落盘成数据，改它要迁移 + 动前端）；
    `run.json.status ∈ {stopped,interrupted}`；
    `docker ps -a --filter label=strix-run-id=<id>` → 空；容器内 `/run/strix/<该任务目录>` → 已删；
    `orphan_sandboxes: 0`
13. **跑到自然结束**：退出码 `2`、`status: completed` + `exit_meaning: vulnerabilities_found`
    （**2026-09-26 改**：原文的 `completed_with_findings` 不是代码里的值，同 12 的理由）（juice-shop 必有发现）；
    `vulnerabilities.json` 非空；`findings.sarif` 合法 JSON；`severity_counts` 与 `scan_findings` 行数一致
14. **压缩韧性（R3 验收）**：注入 `STRIX_MAX_CONTEXT_IMAGES=1` + `STRIX_CONTEXT_BUFFER_TOKENS=1` 重跑。
    **两个 env 各驱动一件不同的事，所以分开断言**（2026-09-17 随 §实时流设计 那处修正改）：
    `STRIX_MAX_CONTEXT_IMAGES=1` → 至少一次 `notice(screenshot_elided)` 且 **epoch 不变**；
    `STRIX_CONTEXT_BUFFER_TOKENS=1` → 至少一次 epoch 递增、且新 epoch 下整份事件被逐条 `event.add`
    重发（`events.snapshot` 已删，见 §实时流设计 末），伴随 `notice(context_compacted)`。`scan_events` 保留**所有** epoch；
    **截图画廊仍显示每一张截图**，尽管 `agents.db` 里已变成 `[older screenshot elided…]`
15. **中文报告**：每条 finding 的 `title_zh/what_zh/impact_zh/fix_zh/severity_reason_zh` 均非空；
    无词表里的未翻译术语；`not_tested_zh` 非空（来自 `coverage.json`）；重复 POST → `translated_findings: 0`（命中缓存）
16. **导出**：`report/print?lang=zh` 在 Chrome 里 `⌘P` → A4 PDF 中文正常、发现卡不跨页断裂、截图内嵌；
    `report/docx` 在 Word/Pages 打开中文无豆腐块
17. **Key 生命周期**：`DELETE /api/keys/{h}` → 204；用死 handle 请求报告 → 409 `key_required`；
    重启 `api` → 所有 handle 消失，可续跑的扫描点"继续扫描"会预填 provider/model 且 Key 框为空
18. **续跑**（2026-09-16 砍、**2026-09-24 用户拍板恢复**，编号沿用原空位）：预算耗尽停下后提高上限续跑，
    断言 argv 用**同一个 cwd**、`--resume <strix_run_name>`、**显式 `-m <持久化的模式>`**（否则会静默回落到
    默认 `deep`）、**且无 `-t`**（Strix 会报错）、`--max-budget-usd` 是**总额**（> 已花费）；
    `run.json` 的 `llm_usage.cost` 接着上次累计而非归零；发现列表保留上次的；`strix.log` 是**追加**（老行还在）；
    续跑的帧落在**新 epoch**（`scan_events` 无主键冲突）
19. **审计**：CSV 含 `authorization.affirmed`（**detail 里含本次被用上的 opt-in**）/`scan.launched`/
    `scan.stopped`/`key.registered`（掩码）/`key.dropped`；
    对 CSV `grep -c "$TEST_KEY"` → **0**
    ★ **`report.exported`（2026-09-26 用户拍板补，T23c）**：脚本对一次已到终态的扫描下一次 `raw.zip`，断言 CSV 里多出恰好
    一行 `report.exported`、`detail` 为 `{"kind":"raw_zip"}`、`scan_id` 对得上（阳性对照即这一行本身）。四个下载路由
    成功下发时各记一条，`kind ∈ {md,csv,sarif,raw_zip,print,docx}`；被拒（4xx）不记
    （**2026-09-17 用户拍板改了两处措辞**，T25 收货时对出来的：① 原先点的 `override.loopback_used`
    改成"查 `authorization.affirmed` 的 detail" —— 代码里刻意不发那个事件，理由见 `services/audit.py`
    常量块与本文件 §护栏 那条 ③：放行后 `GuardVerdict.required_opt_in` 必为空，要说出"哪个勾选被用上"
    就得写 `target_guard` 判定逻辑的第二份副本，两份必然漂移；② `key.forgotten` 改成代码里实际的
    `key.dropped` —— 事件名是**已经落盘的数据**（`audit_log` 行 + NDJSON 镜像 + 现有测试），
    改代码会让老行新行两个名字并存，日后 grep 审计要永远查两个名字。**这两处只改文案、不改代码。**）
20. **重启恢复**：扫描中 `docker compose restart api` → 那行被标成 `status: interrupted` +
    `error_code: interrupted_by_restart`（**2026-09-26 改**：原文的 `orphaned_running` 在代码里不存在 ——
    `_reconcile_interrupted_scans()`（`main.py:174`）把 `starting`/`running` 残行翻成 `interrupted`，
    `exit_code`/`exit_meaning` 留 NULL（"根本没有进程退出过，编一个值是撒谎"）；沙箱由 reaper 回收，
    那行随后可续跑（`interrupted_by_restart ∈ RESUMABLE_ERROR_CODES`））、WS 重连并从镜像续流、
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
25. **wss 与长连接（发布阻断）**：实时流走 `wss://localhost/ws`；**空闲 90 秒不发任何消息**，
    连接仍存活（证明 `proxy_read_timeout` 已改，默认 60s 会掐断）；`ws://` 明文端点不存在（由 23 覆盖）。
    **2026-09-26 改：用 `/ws/system` 而不是 `/ws/scans/{id}`** —— 扫描流每个 tick 都在推帧，结构上证明不了
    "空闲不掉线"；`/ws/system` 是"一帧快照 + 只在变化时推"（`routes/system.py:149-151`），正是要的形状。
    ⚠️ 客户端**必须关掉自动 ping**（`websockets` 默认 20s 一次）：pong 会一直重置 nginx 的读超时，
    不关的话这条断言永远通过 = 等于没测
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
