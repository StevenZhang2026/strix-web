# 安全说明

本文说明控制台保护什么、怎么保护，以及**哪些风险是已知且接受的**。最后一节和前面同样重要：
一份只写"我们做了什么"的安全文档，会让人以为没写到的地方也是安全的。

## 合规声明

Strix 会**真实攻击**你指向的目标。仅限对**自己拥有或已获书面授权**的系统使用，并严格遵守约定范围。
未授权测试在多数司法辖区违法。授权与合规责任完全由使用者承担。
本工具只能**记录**你的授权声明，**无法验证**授权真实性。

## 适用场景与信任边界

控制台为**单人、单机**使用设计：只经 `nginx` 绑在 `127.0.0.1:443`，不监听 80，`api`／`web`
不对外发布端口。本文的所有判断都以此为前提 —— 把它暴露到局域网或公网不在设计范围内。

信任边界上最重要的一条事实：**`api` 容器挂载了 `docker.sock`**（Strix 要靠它创建沙箱容器），
而能访问 `docker.sock` 约等于宿主 root。因此**本机管理员账号不在防御范围内**，下文每一处
"挡不住本机管理员"都源于此。

## 模型凭据（Key）不落盘

"谁用谁的 Key、Key 绝不落盘"是本项目的核心约束。Key 的完整生命周期：

1. **浏览器输入**：密文框 `type=password`，随机 `name` 并关闭自动填充（Key 被浏览器存进密码库是净损失）。
   Key 只放在 `POST /api/keys` 的 JSON 请求体里，**绝不进 URL、query、path**。
2. **后端内存**：存进 `api` 进程内的 KeyVault，返回一个不透明的 `vault_handle`。前端只把这个 handle
   存在 `sessionStorage`（关标签页即失效）；不用 localStorage、不进 cookie、不进 URL。
   KeyVault 空闲 8 小时、最长 24 小时过期；`DELETE /api/keys/{handle}` 立即清除。
   `api` 必须单 worker 运行（`--workers 1`，启动时校验 `WEB_CONCURRENCY`），因为 KeyVault 就是进程内存。
3. **子进程**：发起扫描时，Key 只经**环境变量**交给 Strix 子进程，**不进 argv**（有测试断言，`ps` 看不到）。
   每次扫描有独立的临时 `HOME`，放在 tmpfs `/run/strix`（`noexec,nosuid,nodev`、16 MB、`0700`）上，
   并显式 `--config` 指向该目录下预置为空的 `cli-config.json`。Strix 会把 Key 明文写进这个文件 ——
   所以它只能存在于内存盘上，扫描结束时整个目录删除。
4. **沙箱容器**：Strix 创建沙箱时不转发任何 LLM 环境变量，`docker inspect` 沙箱是干净的。
5. **`api` 重启**：所有 handle 与会话一起消失。进行中的扫描不受影响（子进程已持有 Key），
   但报告翻译等后续操作需要重新提供 Key（`409 key_required`）。这是"不落盘"的直接代价，是正确行为。

逐个泄漏面的对策：

| 泄漏面 | 对策 |
|---|---|
| 数据库 | **任何表都没有 Key／secret／token／password 列**。`api` 启动时 `assert_no_secret_columns()` 检查全部列名，命中即拒绝启动，CI 有对应测试 |
| 应用日志 | 全程 `SecretStr`；脱敏挂在 root handler 的 Formatter（`RedactingJsonFormatter`）上：常见 Key 格式的正则，**加上**当前每一组活跃凭据里每一个值的精确子串（一组凭据可能有 2–3 个值），以及每次扫描的测试账号口令（`ScanSecretRegistry`）。uvicorn 的日志被收拢到同一出口；生产环境 `--no-access-log` |
| Strix 自己的日志 | 绝不设 `STRIX_DEBUG`；日志推给前端和写入本地镜像**之前**先过同一个脱敏器 |
| 错误响应 | 统一 `{code, trace_id, params}`，不带 traceback；traceback 脱敏后才记日志 |
| 外发 | `STRIX_TELEMETRY=false`、`STRIX_NO_UPDATE_CHECK=1`、`LITELLM_LOG=ERROR`；不代理 Strix 自带的网页（它有外发中继）；没有任何向第三方发送数据的代码路径 |
| 配置与镜像 | Key 不进 `docker-compose.yml`、`.env`、镜像，只在运行时经 HTTPS 到达内存 |
| 扫描失败信息 | 数据库里的失败说明只由我们自己的错误码与数字拼成，一个字节都不取自 Strix 的输出 |

## 授权护栏

控制台无法验证授权的真假，它能做的是让"误扫"在结构上难以发生，并把每一次声明记下来：

- **每次扫描必须挂一条授权记录**（`scans.authorization_id` 在数据库层 `NOT NULL`）：授权编号、操作人、
  三条逐条勾选的声明、逐字手打的目标 host。多个目标还要额外确认"每一个都已授权"。
- **授权清单**（`allowlist.yaml`）：不在清单里的目标被拒（`not_in_allowlist`）。环回与内网地址需要显式勾选才放行。
- **DNS 重解析**：发起前重新解析目标，与声明时看到的地址比对，不一致即拒（`dns_changed`）。
- **云元数据地址永久硬拦、不可覆盖**：`169.254.169.254`、`fd00:ec2::254`、`metadata.google.internal`、
  `100.100.100.200` 等，包括它们的各种 IPv6 映射写法。
- **费用上限必填**：`--max-budget-usd` 缺失时拒绝构造命令，另有全局上限兜底。上限是"达到后停止"，
  不是"绝不超过" —— 判定发生在每轮结束后，所以会超出最后一轮的量。
- **审计**：授权声明、扫描发起、凭据登记与清除等事件同时写入数据库与
  `${STRIX_HOST_DATA_DIR}/audit/YYYY-MM.ndjson`（数据库丢了也还在），可经 `/api/audit.csv` 导出。

## 登录

单账号登录。口令至少 12 位，`scrypt` + 每用户随机 salt 单向散列，存 `${STRIX_HOST_DATA_DIR}/auth.json`
（`0600`，不进数据库）；`auth.json` 缺失时 `api` 拒绝启动（否则删掉它就绕过了登录）。
会话是服务端内存里的不透明随机 id，经 `HttpOnly; Secure; SameSite=Strict` cookie 携带，不用 JWT；
空闲 8 小时过期，与 KeyVault 一样随 `api` 重启失效。连续 5 次登录失败锁定 5 分钟。
除 `/api/health` 外所有接口都要登录。控制台**永远不装 CORS 中间件**。

**它挡什么**：本机另一个账号顺手打开浏览器访问 `https://127.0.0.1/`（macOS 上 loopback 是全机共享的）；
浏览器里其它网页跨站调用这个 API。

**它不挡什么**：**拥有管理员权限的本机账号**。管理员能读进程内存、`docker.sock` 与数据目录 ——
登录页对他们不是边界，不要把它当成边界。

## 已知且接受的残余风险

下列每一条都是有意识的取舍，不是疏漏。它们的共同前提是"单人本机使用、本机其他账号非对抗"。

1. **扫描子进程的 `/proc/<pid>/environ` 里有 Key。** Strix 只从环境变量读取凭据，这无法消除。
   可读者仅限 `api` 容器内的 root，进程随扫描结束。
2. **挂载 `docker.sock` 约等于宿主 root。** Strix 用它创建沙箱容器，无法去掉。缓解只有：
   只绑 `127.0.0.1:443`、`no-new-privileges`、单一用途镜像。拿下 `api` 容器即拿下宿主。
3. **`nginx → api` 在 Docker 桥接网络上是明文。** 浏览器到 `nginx` 全程 TLS，但之后一跳不是。
   有 `docker.sock` 权限的人可以被动抓包拿到 Key。不要把"上了 HTTPS"理解成"Key 全程加密"。
4. **`api` 被拿下即等于 TLS 私钥泄漏。** 私钥 `${STRIX_HOST_DATA_DIR}/tls/key.pem` 是 `0600`、只读挂给 `nginx`，
   但整个数据目录以读写方式挂在以 root 运行的 `api` 里。能拿到 `api` root 的人本来就有 `docker.sock`，
   私钥不是那时最值钱的东西 —— 这是"风险不升级"，不是"私钥安全"。补偿：证书只签 `localhost`／`127.0.0.1`
   （偷去也冒充不了任何真实域名）、`CA:FALSE`（不能拿它签别的证书）。
5. **启用 `STRIX_EXTRA_CA_FILE` 意味着凭据与全部模型流量会明文经过一台 TLS 解密设备。** 这个口子是给
   做 TLS 解密的企业网络用的；启用前要确认你接受该设备看到你的 Key 与扫描内容。是否启用可在
   `GET /api/system/status` 查看。
6. **登录口令可能离开本机。** 登录框刻意**允许**浏览器记住口令（否则用户会挑弱口令或抄在便签上）；
   若开了 iCloud 钥匙串或浏览器同步，这个口令会同步到其他设备。模型 Key 的输入框则相反，禁止自动填充。
7. **登录失败的锁定状态只在内存里，`api` 重启即清零。** 能重启 `api` 的人本来就能读它的内存。
8. **`POST /api/targets/validate` 会原样回显一次带口令的目标。** 输入 `https://user:pass@host` 会被拒，
   拒绝结果的 `raw` 字段原样带回用户刚输入的那一串。只有这一处，不写任何日志，经 TLS 返回给刚输入它的人。
   不做"清理后再回显"，因为那会擦掉用户找出错误的唯一线索。
9. **测试账号口令以明文留在扫描产物里。** 我方保证口令不进命令行参数、环境变量、数据库和日志，只写入
   tmpfs 上的指令文件。但 Strix 会把指令原文存进
   `${STRIX_HOST_DATA_DIR}/scans/<id>/strix_runs/<run>/run.json` 的 `instruction` 字段（续跑要靠它读回指令），
   其中包括测试账号口令。它会一直留到这次扫描被留存清理删除为止，而**留存清理默认关闭**
   （`CONSOLE_RETENTION_DAYS=0`）。所以测试账号请用专门为测试建的账号，测完即删或改口令。
10. **续跑的能力边界。** 续跑可以抬高预算、带着记忆恢复主 agent、注入新指令，并会让被强行停掉的子 agent
    接着跑。**但"续跑后能补齐覆盖、让结论变完整"尚未验证。** 不要把续跑当成补全覆盖的保证 ——
    补没补齐，以那次续跑后的覆盖记录为准。另外，让子 agent 接着跑依赖直接改写 Strix 的
    `.state/agents.json`，这不是 Strix 的公开接口；升级 Strix 时 `backend/tests/test_agent_checkpoint.py`
    是这一处的预警线。

