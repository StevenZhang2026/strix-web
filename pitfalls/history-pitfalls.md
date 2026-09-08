# 历史坑记录（二层：低频 / 特定场景）

**本文件不被 `@import`，是按需读取的。** 一旦 `@` 进 CLAUDE.md 就变成常驻，分流失去意义。
触发条件索引在 `CLAUDE.md` §规则分流 —— 新增条目**必须同步加一行触发条件**，否则这条坑永远不会被读到。

规则：只写**实测过**的坑。推测、听说、"可能会"不进本文件。每条给出现象、根因、处置、实测日期。

---

## 1. `/tmp` 会被 macOS 定期清空，且清空得很隐蔽

**现象**：`/tmp/strix_src`（Strix 源码副本，用于复核 `PLAN.md` 的 `file:line` 引用）目录树和 `.git/` 都还在，但 `find -type f` = **0 个文件**。看目录结构完全正常，实际一个文件都没有。

**处置**：任何需要留存的东西不放 `/tmp`、`/private/tmp`、`/var/folders`。`setup.sh` 的 C11 把这三条路径前缀做成阻断项。Strix 源码复核改成**从 wheel 里读**（`site-packages/strix/…`，在 api 容器内），版本严格对得上，比外部副本更可靠。

**实测**：2026-09-07。

---

## 2. Docker Desktop 的配置文件读不出 File sharing 列表

**现象**：`~/Library/Group Containers/group.com.docker/settings-store.json` 只有 303 字节、9 个键，**没有 `filesharingDirectories`** —— 它只记录非默认项。

**处置**：判断某路径能否挂载**只能真跑一次挂载**：写哨兵文件 → `docker run --rm -v "$D:$D" <image> cat "$D/哨兵"` → 比对内容。这就是 `setup.sh` C16 的做法。默认已共享的根是 `/Users`、`/Volumes`、`/private`、`/tmp`。

**实测**：2026-09-07（T0 调研）。

---

## 3. 宿主 3000 端口已被占用

**现象**：`lsof` 显示 `*.3000` 已被占用，而 juice-shop 靶场的常规映射就是 `-p 3000:3000`。

**处置**：`PLAN.md` 的 `make verify-e2e` 与 M0 步骤统一改用 `-p 13000:3000`，目标 URL 为 `http://localhost:13000`。

**实测**：2026-09-07。

---

## 4. `--require-hashes` 不阻止 pip 选 sdist

**现象**：直觉上"锁了哈希就安全"，但 `pip-compile --generate-hashes` **一定会把 sdist 的哈希也写进 lock**（`strix-agent` 1.5.3 的 sdist 哈希 `a5babe4e…3be18` 就在里面）。缺 `--only-binary` 时 pip 完全可以合法地挑 sdist、通过哈希校验、然后死在 hatch 的 Go 1.24 构建钩子上。

**根因**：两个开关正交 —— 哈希锁管"装进来的字节对不对"，`--only-binary` 管"选中的候选是哪一类"。

**处置**：三层都要：lock 的 `--require-hashes` + 命令行 `--only-binary=:all:` + 镜像级 `ENV PIP_ONLY_BINARY=:all:`（堵住"人工 exec 进容器手动 pip"）。

**实测**：2026-09-07（PyPI JSON API 核对 6 个发布文件的哈希）。

---

## 5. 「禁止 named volume」的精确边界

**现象**：容易过度套用成"本项目一律不用 named volume"。

**根因**：禁令针对的是**会被交给宿主 daemon 去挂载的路径** —— named volume 的宿主路径在 Docker Desktop VM 内部，容器内路径与 daemon 可见路径不一致，会静默重现路径别名 bug。

**处置**：数据卷（`${STRIX_HOST_DATA_DIR}`）必须同路径 bind mount。而**从不进 daemon mounts 参数**的路径用 named volume 完全安全 —— 例如生成 hash lock 时给 pip-tools 当缓存的 `strix_console_lockcache:/root/.cache`。

**实测**：2026-09-07（T0 方案评审时厘清）。

---

## 6. CLAUDE.md 的 `@import` 必须独占一行、结尾不能带标点

**现象**：`见 @agent-rules.md。` 这一行**静默不加载** —— 注入的 CLAUDE.md 块里完全没有 `agent-rules.md` 的正文，且没有任何报错。全角句号很可能被当成路径的一部分。

**处置**：`@路径` 单独成行，前后不接文字、不带标点。

**状态**：✅ **已验证**。2026-09-08 新会话注入的 `claudeMd` 块里出现了
`Contents of /Users/szhang/Documents/claude/Strix/agent-rules.md (project instructions…)`
以及它的完整正文 —— `@` 独占一行的写法确实被解析，导入生效。
（验证方式比 `/context` 更直接：看会话开头注入的 `claudeMd` 里有没有那个文件的正文。）

**实测**：2026-09-07 首次踩，2026-09-08 修复验证通过。

---

## 7. subagent 里拿不到 superpowers 的 skill

**现象**：派发 T0 时指定用 `writing-plans`，子 agent 报告它的可调用列表只有 `archify`/`grilling`/`regression-tests`/`security-scan`，`writing-plans` 不在其中；它误触了 `archify`，之后自己去磁盘读 SKILL.md 才走通流程。

**根因**：superpowers 的 `SessionStart` 钩子只注入**主会话**。

**后果**：`agent-rules.md` §四 模板2/模板3 的「开启 Superpowers」不会自动生效。

**处置**：派发 prompt 里给出 SKILL.md 的**绝对路径**，明确要求子 agent 先读再按其流程执行。例如 `~/.claude/skills/superpowers/skills/writing-plans/SKILL.md`。

**实测**：2026-09-07。

---

## 8. macOS 的 `cat` 没有 `-A`

**现象**：`cat -A file` → `cat: illegal option -- A`（BSD coreutils）。

**处置**：查不可见字符用 `sed -n '3p' f | od -c`，或 `grep -n`。

**实测**：2026-09-07。

---

## 9. Compose tmpfs 长式语法不支持 `noexec`

**现象**：短式 `tmpfs: ["/run/strix:rw,noexec,nosuid,nodev,size=16m,mode=0700"]` 能表达全部挂载选项；长式 `volumes: [{type: tmpfs, tmpfs: {size, mode}}]` **只支持 `size` 和 `mode`**。

**后果**：本机是 Compose `v5.4.0`（远超常见 v2.x 文档的大版本），若它把短式判为弃用并报警告，改写长式就是一次**真实的安全能力回退**（`/run/strix` 上放的是每任务 HOME 与预置 `cli-config.json`，`noexec` 是泄漏矩阵 #2 的一部分）。

**处置**：真遇到时**停下上报**，不许自行接受回退。

**状态**：✅ **本机已实测无此问题** —— Compose `v5.4.0` 对短式 `tmpfs`、YAML 锚点 `<<: *anchor`、`${VAR:-默认}` 插值全部零警告（`docker compose config` 的 stderr 为空字符串），`noexec,nosuid,nodev` 原样保留。条目保留是因为它的失败模式是"看起来只是个警告"，将来 Compose 大版本升级时要重跑这条检查。

**实测**：2026-09-07。

---

## 10. 宿主到 `download.docker.com` 只有 ~45.8 KB/s

**现象**：拉 `docker-29.7.2.tgz`（77.3 MB），300 秒只收到 13.7 MB 就超时中断。

**处置**：不要在构建期 `curl` 这个静态包。改用 `COPY --from=docker:29.7.2-cli@sha256:…` —— 已核实 `docker-library/docker` 的 cli Dockerfile 本身就是 wget 这个 tgz 再 `tar --extract 'docker/docker'`，**产出的是同一个二进制**，而 manifest-list digest 一个值就覆盖 arm64 + amd64，不必自维护 per-arch sha256（该域名**没有 `.sha256` 旁文件**，实测 404）。

**实测**：2026-09-07（T0 调研，curl 超时输出）。

---

## 11. `pip-compile --generate-hashes` 会为算哈希下载整棵依赖树 × 全部平台

**现象**：`make lock` 跑了 30 分钟后死于
`piptools/repositories/pypi.py:372 → _get_file_hash` →
`urllib3.ReadTimeoutError: files.pythonhosted.org: Read timed out.`

**根因**（读了 pip-tools 源码后**两次修正**，最终版）：`_get_req_hashes` 确实是"JSON API 优先"：

```python
pypi_hashes_by_link = self._get_hashes_from_pypi(ireq)      # 一次 JSON 请求拿全量
... if candidate.link.url in pypi_hashes_by_link:  用 JSON 里的哈希      # 不下载
... else:                                          self._get_file_hash(link)  # 下载整个文件
```

关键在 `_get_hashes_from_pypi` **请求失败时静默 `return {}`**。于是在"能连通但会中途断流"的网络上，
**一次**元数据请求失败 → 该包的所有候选全部落到 `_get_file_hash`。而 `allow_all_wheels()` 把
所有平台的 wheel 都变成候选（`resolver.py:168` → `repositories/pypi.py:454`，注释原文
"Ignore current platform. Support everything."），于是下载量 = 整棵依赖树 × 全部平台。
所以这不是"JSON API 覆盖不全"的设计问题，是**一个静默降级**：慢网络上它退化成最坏路径且不报错。

**处置**：**不再用 `pip-compile --generate-hashes`。** 把这一步拆成两半，各用擅长的工具（`make lock` 现在就是这样）：

1. **解析** —— `pip-compile`（**不加** `--generate-hashes`）+ `PIP_ONLY_BINARY=:all:`。它停在解析阶段，
   靠 PEP 658 只取 `.whl.metadata`（每个 2–110 kB），产出权威的 `name==version`。
2. **算哈希** —— `scripts/gen_lock.py`，同一个数据源（JSON API 的 `digests.sha256`）、
   同一套文件筛选（`sdist` + `bdist_wheel`），但**显式重试 8 次、失败即 `raise`**，绝不退化成下载。

实测：两步合计约 4 分钟，产出 84 包 / 2084 哈希；而原方案 45 分钟仍未跑完。
**不需要**引入 `uv`（曾被列为备选 6）—— 标准库 120 行就够，不必为此多背一个包管理器。

**顺带记下 `pip install --dry-run` 不能用来做这件事**：它的解析阶段同样是 PEP 658 元数据、能跑完，
但解析完**仍会进入真实 wheel 下载阶段**（日志里从 `Downloading x.whl.metadata (7.5 kB)`
变成 `Downloading pillow-….whl (6.3 MB)`），加 `--report` 也一样。所以它永远跑不到写出 report.json。

**实测**：2026-09-07 首次踩，2026-09-08 定案。

---

## 12. `cmd | tail` 会把退出码换成 `tail` 的退出码

**现象**：`make lock 2>&1 | tail -40` 在后台跑完，上报 `exit code 0`，而日志末尾明明是 `make: *** [lock] Error 1`。据此错误地宣布"锁生成完了"。

**根因**：管道的退出码是**最后一个命令**的退出码。`tail` 总是成功。

**处置**：需要真实退出码时**不要管道**。重定向到文件再读：`cmd > /tmp/x.log 2>&1; echo $?`。
另一个副作用：管进 `tail` 还会**吞掉全部增量输出**（`tail` 要等管道关闭才输出），长任务看起来像卡死。

**实测**：2026-09-07。

---

## 13. 本机 agent 无法读写任何 `.env*` 文件，且项目级 `allow` 改不了

**现象**：`Write(.env.example)` 报 `File is covered by a Read deny rule in your permission settings`。往 `.claude/settings.local.json` 的 `permissions.allow` 里加 `Read(.env.example)` + `Write(.env.example)` 后**重试仍然被拒**。

**根因**：deny 不在项目设置里，而在 **`/Library/Application Support/ClaudeCode/managed-settings.json`**（`root:admin`、0644、当前用户无写权限）：

```json
"deny": ["Read(~/.ssh/*)", "Read(~/.aws/*)", "Read(./.env)", "Read(./.env.*)",
         "Read(./secrets/**)", "Read(./build)"]
```

managed settings 的优先级高于项目设置，且 deny 天然压过 allow —— 所以**项目级 allow 在结构上不可能覆盖它**。改它需要 sudo 去动一个机器级安全策略文件。

**处置**：顺从，不绕过。这条 deny 与本项目自己的「Key 绝不进 `.env`」不变式同向，削弱它得不偿失。示例文件命名为 **`env.example`（无前导点）** —— 它不匹配 `./.env` 也不匹配 `./.env.*`。`.gitignore` 相应改为忽略 `.env` 与 `.env.*`，示例文件天然在版本库里。

**推论**：`.env` 只能由 `setup.sh` 生成、由用户自己查看。任何需要 agent 校验 `.env` 内容的设计（比如密钥卫生扫描）都必须**做在脚本里**而不是靠 agent 读文件 —— `setup.sh` 的 C18 正是如此。

**实测**：2026-09-07。

---

## 14. macOS 的 bash 3.2 会把全角标点吞进变量名，`set -u` 下直接崩

**现象**：`setup.sh` 第一次完整运行就死在
`./setup.sh: line 97: D_VER<?>: unbound variable`。
出问题的那行是 `ok "Docker 守护进程可达（$D_VER，$D_OS）"` —— 变量名拼写完全正确。

**根因**：macOS 自带 `GNU bash 3.2.57`，它在解析 `$名字` 时把 **≥ 0x80 的字节也当作合法标识符字符**。
`$D_VER，` 里紧跟的全角逗号 `，` 是 `EF BC 8C`，于是 bash 认为变量名是 `D_VER\xEF\xBC\x8C`，
那个名字当然没定义，`set -u` 立刻退出。最小复现：

```bash
bash -c 'set -u; X=hello; echo "值是 $X，结束"'   # → bash: X<?>: unbound variable
bash -c 'set -u; X=hello; echo "值是 ${X}，结束"' # → 值是 hello，结束
```

**为什么这个项目一定会反复踩**：本项目所有脚本的提示信息都是中文，`$VAR` 后面接
`，。（）：、` 是最自然的写法。`setup.sh` 一次就有 **9 处**。

**处置**：中文脚本里**一律写 `${VAR}` 而不是 `$VAR`**。落地检查（应当无输出）：

```bash
grep -nP '\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7F]' setup.sh
```

这条 grep 该进 T30 的 `make verify-e2e`。已核对 `Makefile` / `docker-compose.yml` 无此问题
（compose 的 `${VAR}` 本来就带花括号，Make 用 `$(VAR)`）。

**顺带确认**：`set -euo pipefail` 下 `[ 假条件 ] && warn "…"` **不会**提前退出 ——
失败的命令不是 `&&` 后的最后一条，属于 bash 的豁免范围。已实测，这个惯用法可以放心用。

**实测**：2026-09-08。

---

## 15. 本网络的企业防火墙**选择性**解密 TLS —— PyPI 镜像因此不可用

> 解密设备与企业根 CA 的具体归属见 `pitfalls/local-env.md`（未跟踪）。本条只留与
> 工程决策相关的部分 —— 任何做 TLS 解密的企业网络都可能是这个形状。

**现象**：容器内 `pip install -i https://pypi.tuna.tsinghua.edu.cn/simple` 报
`SSLError: [SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate in certificate chain`。
而同一个 URL 在**宿主** `curl` 下 HTTP 200 —— 因为 macOS 系统钥匙串里装了那张企业根 CA，容器里没有。

**实测的拦截矩阵**（容器内严格校验 + 失败后解析对端证书）：

| 主机 | 签发者 | 结论 |
|---|---|---|
| `pypi.org` | GlobalSign Atlas R3 DV TLS CA 2025 Q4 | **未拦截** |
| `files.pythonhosted.org` | 同上 | **未拦截** |
| `ghcr.io` | Sectigo Public Server Authentication CA DV R36 | **未拦截** |
| `pypi.tuna.tsinghua.edu.cn` | 企业解密代理（签发者见 `local-env.md`） | **被解密** |
| `mirrors.cloud.tencent.com` | 同上 | **被解密** |

**后果与裁定**：**不引入任何 PyPI 镜像。** 用镜像就必须把企业 MITM 根 CA 装进 api 镜像，
那是把一张能签任意域名的证书焊进交付物 —— 对一个"Key 绝不落盘"的项目是净损失。
PyPI 直连未被拦截，且有 `--require-hashes` 保底，是唯一正确的路。

**顺带解释了两件旧事**：
① 之前测出"清华 93 KB/s、腾讯 40 KB/s、阿里云 11 KB/s 且在 1.00 MB 处截断" ——
   那些数字全是**经过解密代理**测的，慢和截断都是代理造成的，不是镜像本身慢。
② PyPI JSON API 响应在 448 KB 处 `Unterminated string` ——
   当时有两个容器在抢同一条带宽，属于拥塞，不是拦截。

**排查手法**（值得复用）：严格校验连一次；失败了把 `verify_mode=CERT_NONE` 再连一次，
从 DER 里 `re.findall(r'[\x20-\x7e]{5,}')` 抠可读串就能看出签发者，不需要 openssl。

**实测**：2026-09-08。

---

## 16. 从 pip 日志里抠 pin，会抠到解析器**回溯丢弃**的版本

**现象**：两种方式各自解析同一份 `pyproject.toml`，都得到 84 个包，只差一个：

```
pip-compile 权威:  websockets==15.0.1
从 pip 日志抠的:   websockets==16.1.1
```

`gql==4.0.0` 要求 `websockets<16,>=14.2`，所以 **16.1.1 是非法的** —— 日志派生的那份 lock 是错的。

**根因**：pip 的解析器会**回溯**。它先试 `websockets` 的最新版 16.1.1、为此下载了
`websockets-16.1.1-….whl.metadata`（于是日志里留下一行 `Downloading …`），
发现与 `gql` 的 `<16` 冲突后丢弃它、退到 15.0.1。
日志记录的是**尝试过的候选**，不是**最终解集**；两者只在没有冲突时才碰巧相等。

**为什么当时没被发现**：`pip install --dry-run` 解析完还会进入真实 wheel 下载阶段并卡死
（见条 11），所以永远拿不到它最后那句权威的 `Would install …`，只剩日志可抠。
84 个包里只有 1 个错、且错的是个不起眼的传递依赖 —— 肉眼过一遍 diff 根本发现不了。

**处置**：**永远不要从人类可读的日志里提取机器要消费的事实。** 解析结果只从解析器的
**正式产物**取：`pip-compile` 的输出文件（本项目现在的 `make lock-resolve`），
或 `pip install --report` 的 JSON。日志是给人看的，它连"这个候选最后有没有被采用"都没表达。

**这条坑之所以被抓到**：换用 `pip-compile` 之后，刻意把新旧两份 pin 做了 `diff` 而不是直接覆盖。
两条独立推导互相对账是廉价的 —— 值得成为习惯。`scripts/check_lock.py` 现在把
"pins 与 lock 逐项相等"变成了断言，正是为了让这类不一致再也不能安静地存在。

**实测**：2026-09-08。

---

## 17. `PIP_ONLY_BINARY` 是默认值而非强制门；顺带：镜像 env 里的 `GPG_KEY` 会误报

两条都是做 T0 镜像验收时实测出来的，都属于"以为已经防住了，其实没有"。

**17a. 命令行盖得掉 `PIP_ONLY_BINARY=:all:`**

镜像里设了 `ENV PIP_ONLY_BINARY=:all:`，`printenv` 确认是 `:all:`。但：

```
$ pip download --no-deps --no-binary=:all: --dest /tmp/z cvss==3.6
Saved /tmp/z/cvss-3.6.tar.gz          # ← sdist 照样拿到了
Successfully downloaded cvss
```

**根因**：pip 的配置优先级是 命令行 > 环境变量 > 配置文件。`--no-binary` 显式给出时直接盖掉
`PIP_ONLY_BINARY`。所以这个 ENV 挡的是**手滑与隐式回退**，挡不住刻意绕过的人。
原先 Dockerfile 注释写的"任何人（含日后 exec 进容器手动 pip 的人）都拿不到 sdist 路径"
是**过度承诺**，已改。真正不可被环境影响的门是安装命令行上**显式**写的 `--only-binary=:all:`。

**处置**：保留这个 ENV（它确实消掉了默认路径上的 sdist 回退），但不要把它当安全边界记账。
凡是"防刻意绕过"的诉求，都必须落在显式命令行参数或构建期断言上。

**17b. `GPG_KEY` 让"env 里不许有 key/secret"的扫描误报**

`python:3.12-slim` 基础镜像自带 `GPG_KEY=7169605F62C751356D054A26A821E680E5FA6305`
（用来验证 Python 源码包签名的**公钥指纹**，不是机密）。于是按关键词
`grep -iE 'key|secret|token|password'` 扫镜像 env 必然命中它。

**后果**：T30 的密钥卫生扫描如果按**名字**匹配，第一次跑就会红，然后人会去放宽正则 ——
一旦开始放宽，真正的泄漏也会被一起放过去。

**处置**：扫描要按**值的形状**判定（长度 + 熵 + 已知 Key 前缀如 `sk-`/`sk-ant-`/`AKIA`），
而不是按变量名；若确实要按名字，就为 `GPG_KEY` 写一条**精确**豁免（全等匹配，不是子串）
并注明理由。同理，`find / -name '*.pem'` 会大量命中 `/etc/ssl/certs/` 的 CA 信任库 ——
那是访问 PyPI 与 LLM API 必需的，扫描必须排除该目录而不是排除 `*.pem`。

**实测**：2026-09-08（T0 镜像验收 A11b / A13 / A14）。

---

## 18. 核查 Strix 内部时，两种"假结论"

做 T0 镜像验收（A25：回到 wheel 里复核 `PLAN.md` 的每条出处）时各踩了一次。
两者的共同点：**检查本身出了错，却输出了一个看起来很确定的结论。**

**18a. `cd` 进 `site-packages/strix` 会让 `import agents` 被自家子包遮蔽**

```
$ cd .../site-packages/strix && python -c "import strix.interface.tui.backend.live_view"
ModuleNotFoundError: No module named 'agents.tool'
```

差点据此报告"强制的 import 边界在本镜像里根本 import 不了"。实际是：cwd 在
`site-packages/strix/` 下，而该目录里有 `strix/agents/` 子包；Python 把 `''`（cwd）
放在 `sys.path` 前面，于是 `import agents` 命中 `strix/agents/`，而不是真正的
`openai-agents`。换到 `-w /app` 再跑，一切正常，且 `agents.__file__` 指向
`site-packages/agents/__init__.py`。

**处置**：核查 Strix 内部时**永远用 `-w /app`（或任何不在 `site-packages` 里的 cwd）**。
要看某个 `import` 到底解析到哪，别只看成败，打印 `模块.__file__`。

**18b. `grep 模式 文件 2>/dev/null || echo 通过` —— 文件不存在也会"通过"**

写了 `grep -- "--model" cli_args.py 2>/dev/null || echo "✓ 不存在"`，输出了 `✓`。
但 `cli_args.py` 的真实路径是 `interface/cli_args.py` —— grep 报的是
"No such file or directory"（被 `2>/dev/null` 吞掉），非零退出触发了 `||`，
于是"文件找不到"被打印成了"检查通过"。

**处置**：`||` 分支只在**确认目标存在**之后才等于"通过"。断言不存在时要么先 `test -f`，
要么用 `grep -c` 看计数（本次改成 `grep -c` 后拿到确定的 `0`）。别把 stderr 丢掉。

**顺带产出的确定结论**（都已核实，可直接引用）：
`interface/cli_args.py` 的全部选项是 `--config --diff-base --instruction
--instruction-file --max-budget --max-budget-usd --max-turns --non-interactive
--resume --scan-mode --scope-mode --target --target-list --update --version` ——
确认**没有** `--model` 和 `--output-dir`；且 `--max-budget` 与 `--max-budget-usd`
是**同一个参数的两个拼写**（`dest="max_budget_usd"`, `default=None`），
所以"必填"是本项目自己加的约束，Strix 本身默认不限预算。

**实测**：2026-09-08。

---

## 19. 三家主流 LLM 端点**全部**被同一台企业解密设备解密 —— M0 因此在本网络跑不动

> 同条 15：解密设备归属见 `pitfalls/local-env.md`（未跟踪）。**下面的端点清单本身保留** ——
> 它是本项目的设计依据（为什么开发走 Bedrock、为什么必须有 N2），去掉它会让 `PLAN.md`
> 的论证悬空。清单不指向任何组织。

**现象**：M0 干跑（刻意用假 Key）时，`strix` 退出码 1，Rich 面板标题 `LLM CONNECTION FAILED`：

```
litellm.InternalServerError: AnthropicException - Cannot connect to host api.anthropic.com:443
ssl:True [SSLCertVerificationError: (1, '[SSL: CERTIFICATE_VERIFY_FAILED]
certificate verify failed: self-signed certificate in certificate chain')]
```

**这不是 Key 的问题** —— 假 Key 本该收到 401；这里连 TLS 都没握成手，压根没走到鉴权。
**也正因为先用假 Key 干跑，才没白烧用户一次真 Key。** 拿真钱/真凭据跑之前先用假的走一遍全流程，值得成为习惯。

**实测的拦截矩阵**（容器内严格校验 + 失败后从 DER 抠签发者，手法同条 15）：

| 主机 | 严格校验 | 签发者 |
|---|---|---|
| `api.anthropic.com` | **✗ 被解密** | 企业解密代理（签发者见 `local-env.md`） |
| `api.openai.com` | **✗ 被解密** | 同上 |
| `openrouter.ai` | **✗ 被解密** | 同上 |
| `api.deepseek.com` | **✗ 被解密** | 同上 |
| `bedrock-runtime.us-east-1.amazonaws.com` | ✓ 未拦截 | — |
| `bedrock-runtime.us-west-2.amazonaws.com` | ✓ 未拦截 | — |
| `bedrock-runtime.ap-southeast-1.amazonaws.com` | ✓ 未拦截 | — |
| `generativelanguage.googleapis.com`（Gemini） | ✓ 未拦截 | — |
| `pypi.org` / `ghcr.io` | ✓ 未拦截 | — |

**结论：本网络能用的只有 AWS Bedrock 与 Google Gemini。** 取 Bedrock ——
它后面就是 Anthropic Claude，而 Strix 的 system prompt 与 ~90 个 skill 全是按 Claude 调的。
已用假凭据实测到 `BedrockException Invalid Authentication -
{"message":"The security token included in the request is invalid."}` ——
那是 **AWS 返回的真实 HTTP 响应**，证明 TLS 握手与请求投递都通了，只是凭据是假的。
**拿真凭据之前把这条路验到"只差凭据"，是这次干跑最大的收益。**

**Bedrock 打破了"一个 Key 一个字符串"的假设，而且打破两次**（对产品设计有影响，不只是 M0）：

`litellm.validate_environment("bedrock/…")` 报 `missing_keys=['AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY']` ——
**两个值**，且都不以 `_API_KEY` 结尾，所以 Strix 的 `_mirror_api_key_to_provider_env`
（`config/models.py:590-592` 只填 `*_API_KEY`）帮不上忙。
好在 `LLM_API_KEY` 对 Strix 是**可选**的（`interface/environment.py:43-44` 进的是
`missing_optional_vars`，只有 `STRIX_LLM` 必填），直接喂 `AWS_*` 三个 env 就能跑。

**再一次**：Bedrock 还有第二种凭据形状 —— **Bedrock API key（bearer token，`ABSK…`）**。
`litellm/llms/bedrock/base_aws_llm.py:1554-1564`：

```python
if api_key is not None: aws_bearer_token = api_key
else:                   aws_bearer_token = get_secret_str("AWS_BEARER_TOKEN_BEDROCK")
if aws_bearer_token:
    headers["Authorization"] = f"Bearer {aws_bearer_token}"
    return headers, json.dumps(request_data).encode()   # ← 整段跳过 SigV4
```

所以两种形状**互斥**：同时给 bearer 与 SigV4，SigV4 会被**静默忽略**，失败时归因不了。
区域两种都要（用来拼端点 URL）。于是同一个 provider 有两种形状 →
**"形状"不是 provider 的属性，是一个独立维度**，`POST /api/keys` 必须显式带 `auth_shape`，
并拒绝该 shape 没声明的键。

顺带排除一个虚惊：`config/models.py:712` 的 `api_key=llm.api_key or "not-needed"` 看着像会把
`"not-needed"` 当 bearer token 送出去，但它在 `_register_openai_client_with_headers` 里，
而 `_configure_extra_headers` 没有 `extra_headers` 就提前 return —— 不碰 Bedrock 路径。
**看到可疑的一行先看它的调用条件，别直接当成 bug 记账。**

→ 控制台的 KeyVault / `key_handle` 模型必须能装**一组**凭据而不是一个字符串，
   组的形状由 `auth_shape` 决定（1／2／3 个值），且脱敏要按"每个值"注册进 `RedactionFilter`。

条 15 只测了**包与镜像仓库**，据此得出的"直连没问题"**对 LLM 端点不成立**。
选择性解密的策略是按目的地定的，"这个域名没被拦"推不出"那个域名没被拦"。

**`gaierror` 不是判据**：首轮 `api.openai.com` 与 `ghcr.io` 都报 `gaierror`，
但 `ghcr.io` 几分钟前刚成功拉过镜像 —— 那是**偶发 DNS 抖动**。重试后两者分别是"被解密"与"通过"。
**判定拦截必须重试到拿到确定结论**，别把一次 DNS 失败记成一种拦截状态。

**处置（分两层，别混）**：

- **M0 怎么跑通** —— 这是用户的网络、用户的选择，必须问，不许替他决定。三条路见 `PLAN.md` §M0。
- **产品层的真需求** —— 本条暴露的不只是开发机不便：**任何处在企业 TLS 解密后面的用户都用不了这个工具**。
  所以需要一个"运行期挂载、操作者自备 CA bundle"的口子（`-v ca.pem:/…:ro` + `SSL_CERT_FILE`），
  **绝不把任何 CA 焊进镜像** —— 条 15 的立场（不把能签任意域名的证书放进交付物）依旧成立：
  运行期由操作者显式挂载，与构建期烧进交付物，是两件完全不同的事。

**顺带确认**：企业根 CA 在本机是**管理员部署的信任锚**（不是用户自己装的），用
`security find-certificate` 就能导成 PEM —— 具体命令与匹配串见 `pitfalls/local-env.md`。
挂进容器机械上很容易，难点从来不是技术，是要不要。

**顺带产出（重要，直接影响 T3 的错误分类设计）**：
**Strix 的失败面板标题不足以做错误分类。** 已实测两种完全不同的故障 ——
TLS 被中间设备解密、以及凭据无效 —— 打的**都是同一个 `LLM CONNECTION FAILED` 面板**。
只按标题分类会把"凭据错"报成"连不上"，让用户去查网络而不是去查凭据。
所以 T3 的映射**必须先看正文里的异常类名**，标题只做兜底。已落地的判据顺序见
`scripts/m0_probe_inner.sh`：

| 正文特征 | 机器码 |
|---|---|
| `CERTIFICATE_VERIFY_FAILED` / `SSLCertVerificationError` | `llm_tls_intercepted` |
| `AuthenticationError` / `security token included in the request is invalid` | `invalid_api_key` |
| `AccessDenied` / `not authorized to perform` / `ValidationException` | `model_access_denied` |
| `NotFound` / `model_not_found` | `model_not_found` |
| `MISSING REQUIRED ENVIRONMENT VARIABLES` | `missing_required_env` |
| 仅剩面板标题 `LLM CONNECTION FAILED` | `llm_connection_failed`（兜底） |

**实测**：2026-09-08。

---

## 20. 容器内 `grep -r <key> /` 慢到不能当断言用

**现象**：M0 断言 2d（"清理后全文件系统无 Key 残留"）写成
`grep -rlF -- "${KEY}" / --exclude-dir=proc --exclude-dir=sys --exclude-dir=dev`，
在 api 容器里跑了 **6 分钟以上仍未返回**，把整个探测脚本卡住 —— 前面几条断言早就打完了，
人看到的却是"卡死"。

**根因**：镜像解包后 768 MB，`site-packages` 里几十万个小文件，全都要读一遍；
再叠加同路径 bind mount 走 VirtioFS（Docker Desktop 的宿主目录穿透本来就慢）。

**处置**：断言要**按泄漏面枚举目标**，而不是"扫全盘图个心安"。Key 可能落盘的位置是**可枚举的**：
每任务 HOME（tmpfs）、数据目录、真 HOME `/root`、`$TMPDIR`、run 目录里的 `strix.log`。
逐个精确扫，既快又说得清扫了什么；`grep -r /` 反而掩盖了"你到底想防住哪条路径"。
真要兜底扫全盘，就丢给 `make verify-e2e` 那种可以跑十分钟的场合，并且**必须**打进度或超时。

**实测**：2026-09-08。

---

## 21. 交互式凭据脚本的真实失败模式：人会把凭据贴进聊天框

**现象**：`scripts/m0_probe.sh` 全套设计（`read -rs` 不回显、只经 stdin、绝不进 argv、
绝不 `docker exec -e`）都对，但它要求"人在自己的终端里跑"。实际发生的是：
agent 在对话里列了一张"提示 → 输入什么"的表，用户就**照着表把答案连同真 API key
一起回复到对话里**了。凭据于是落进：agent 上下文、会话 transcript
（`~/.claude/projects/<项目>/*.jsonl`，**明文落盘**）、以及用户的输入历史。
一个技术上无懈可击的凭据通道，被一次完全合理的人类行为绕过了。

**根因（值得记住的部分）**：把交互式脚本的**逐个提示词**渲染成一张表，
读起来就像"请在这里填写"。凭据通道的安全边界不只在脚本里，也在**你怎么描述它**。

**处置**：

1. 描述交互式凭据脚本时，**只给命令，不给逐项输入表**。非机密项（区域、模型名、目标）
   可以先说清楚，机密项一律只说"脚本会问，不回显"，绝不摆成待填字段。
2. 真发生了：**第一句话就是让用户去作废重开**，别先讨论技术。transcript 已经落盘，
   agent 删不掉也不该去删（那是用户的审计记录）。作废是唯一真正生效的补救。
3. 不把它写进任何文件、不用它发任何请求 —— 即便"只是列一下可用模型"也不行。
4. 顺带把泄漏范围说清：`ABSK…` 这类 base64 token 解开后带 AWS 账号 ID，
   不只是"一串随机字符"，用户才好判断要不要查 CloudTrail。

**对产品的推论**：控制台的 Key 输入框同理 —— 已有的 `type=password` + 随机 `name`
挡的是浏览器自动填充，挡不住"用户把 key 贴到别处"。所以 UI 文案要**主动说**
"我们从不保存密钥，也请不要把密钥贴给任何人或任何 AI 助手"。

**实测**：2026-09-08（真实发生）。

---

## 22. Bedrock API key（bearer）在 litellm 的 **converse** 路由上直接崩 —— 必须走 `invoke/`

**现象**：给了合法的 Bedrock API key（`ABSK…`）+ 区域，`STRIX_LLM=bedrock/us.anthropic.claude-opus-5`，
Strix 退出码 1，面板还是那个 `LLM CONNECTION FAILED`，但正文是个 **AttributeError**：

```
litellm.APIConnectionError: 'NoneType' object has no attribute 'access_key'
  litellm/llms/bedrock/chat/converse_handler.py", line 377, in completion
    ("aws_access_key_id", credentials.access_key),
```

**根因**：litellm 里 bearer token 的支持是**分路由的，不是全局的**。

- `base_aws_llm.py:1554-1564` 的 `_sign_request` 确实先看 `AWS_BEARER_TOKEN_BEDROCK`，
  有值就发 `Authorization: Bearer …` 并**整段跳过 SigV4**，压根不碰 credentials。
- 但 `converse_handler.py:334` **无条件**先 `credentials = self.get_credentials(...)`，
  再在 `:370-381` 把 `credentials.access_key/.secret_key/.token` 塞进 rust bridge 的参数
  （注释说"hand down the credentials … so both paths sign as the same principal"）。
  只给 bearer 时 `get_credentials` 一路 fall through 到 `_auth_with_env_vars`，
  boto3 找不到凭据 → 返回 `None` → 崩在 `.access_key`，**走不到那个认 bearer 的签名函数**。

**处置：模型名加一段 `invoke/`** —— `bedrock/invoke/us.anthropic.claude-opus-5`。
`invoke_handler.py` 里 `grep 'credentials\.'` 是**零命中**，签名走
`invoke_transformations/base_invoke_transformation.py:145 → _sign_request`，正好是认 bearer 的那条。

**用假 token 做的对照实验**（零成本、不需要真凭据，这个手法本身值得复用）：

| `STRIX_LLM` | 结果 |
|---|---|
| `bedrock/us.anthropic.claude-opus-5` | `APIConnectionError: 'NoneType' object has no attribute 'access_key'` —— 本地崩，没发出请求 |
| `bedrock/invoke/us.anthropic.claude-opus-5` | `PermissionDeniedError: BedrockException - {"Message":"Invalid API Key format: Base64 decoding failed"}` —— **AWS 的真实 HTTP 响应**，证明 bearer 被采用了 |

**顺带确认预算护栏不受影响**（这是必查项，`--max-budget-usd` 是强制必填的）：
`litellm.completion_cost` 对 `bedrock/…` 与 `bedrock/invoke/…` 算出**完全一样**的值
（1000 in / 500 out 都是 `$0.01925`）。两个名字都不在 `litellm.model_cost` 的键里，
但 `completion_cost` 会剥掉路由前缀去查 `us.anthropic.claude-opus-5`（那个**在**键里）。
**换路由前必须验一遍成本计算**，否则预算护栏会静默变成 0 美元。

**给 T3 的判据**（已加进 `scripts/m0_probe_inner.sh`，优先级紧跟 TLS 之后）：
正文含 `object has no attribute 'access_key'` → `bedrock_route_rejects_bearer`，
中文指引直接说"把模型名改成 `bedrock/invoke/<model>`"。
落到兜底的 `llm_connection_failed` 是**没用的**结论 —— 它会让人去查网络，
而真凶是凭据形状与路由不匹配。这是条 19「面板标题不足以分类」的又一个实例：
**同一个面板标题下，已经躺着三种完全不同的故障了。**

**实测**：2026-09-08（M0 第一次用真 Bedrock API key 跑，崩在这里）。

---

## 23. 走 `invoke/` 之后，Strix 的 prompt caching 反过来把请求打死 —— 必须 `STRIX_PROMPT_CACHE=false`

条 22 让我们把模型名改成 `bedrock/invoke/<model>`，bearer 鉴权就通了。**下一步立刻踩到第二个坑。**

**现象**：`STRIX_LLM=bedrock/invoke/us.anthropic.claude-opus-5`，真 Bedrock API key，Strix 退出码 1：

```
litellm.BadRequestError: BedrockException - {"message":"cache_control_injection_points: Extra inputs are not permitted"}
```

**先读对这条错误**：它是 Bedrock **校验请求体**时报的 —— 能拿到 body 校验错误说明
**鉴权已经通过了**。所以别再去查凭据，问题在参数上。这一点很容易搞反：
条 22 那个错误在本地就崩了（压根没发出请求），这个错误是 AWS 回的。

**根因**：Strix 的 prompt caching 只在 Bedrock **converse** 路由上成立。
`core/inputs.py:265-282` 注入的 `cache_control_injection_points` 里带 `{"location":"tool_config"}`，
而**只有** `litellm/llms/bedrock/chat/converse_transformation.py` 会消费这个参数；
invoke 路由不认识它，原样塞进请求体 → Bedrock 400。

**Strix 自己想防住这件事，但防线只预设了一种模型名形状。** 那个 docstring 写得明明白白：
"``tool_config`` only on Bedrock Converse (the only route whose LiteLLM transform consumes it
— elsewhere it leaks onto the wire) … Unmapped Bedrock models get no points at all."
可 `config/models.py:857` 的 `_prompt_cache_name_candidates` 只剥 `litellm/` 和 `bedrock/`，
**从不剥 `invoke/`**：

```
MODEL bedrock/us.anthropic.claude-opus-5
  cache 候选名            = ['us.anthropic.claude-opus-5', 'anthropic.claude-opus-5', 'claude-opus-5']
MODEL bedrock/invoke/us.anthropic.claude-opus-5
  cache 候选名            = ['invoke/us.anthropic.claude-opus-5', 'anthropic.claude-opus-5', 'claude-opus-5']
                              ^^^^ 第 1 个查不到，但第 2、3 个照样命中缓存能力表
  supports_prompt_caching = True   → 注入照旧发生
```

第 2 个候选是"`anthropic.` + 剥掉区域前缀的尾段"，第 3 个是裸模型名 —— 两个都**与路由无关**，
所以多插一段 `invoke/` 完全绕不过这个判定。**这是个通用教训：靠字符串剥离推断能力的代码，
遇到它没预设的名字形状时不会报错，只会给出自信的错答案。**

**处置：`STRIX_PROMPT_CACHE=false`** —— 这是 Strix 自带的一等公民开关
（`config/settings.py:51`，`LlmSettings.prompt_cache` 的 alias，默认 `true`），
不是补丁、不是 fork。实测：

| `STRIX_PROMPT_CACHE` | `settings.prompt_cache` | `extra_args` |
|---|---|---|
| `true`（默认） | `True` | `['cache_control_injection_points']` ← 就是它把请求打死 |
| `false` | `False` | `[]`（连上 timeout 后只剩 `{'timeout': 300.0}`）|

关掉后 `extra_args` 里**没有任何 converse 专用参数残留**，`extra_headers` 与 `tool_choice` 均为 `None`。

**必查两件事**：

1. **预算护栏仍有效**（`--max-budget-usd` 强制必填，算成 0 就等于没护栏）。
   `report/state.py:699` 的候选名最后一个是 `model.rsplit("/", 1)[-1]`，天然把 `invoke/` 剥掉，
   两条路由算出的成本**逐位相同**（10k in / 1k out 均为 `$0.0825`）。✅
2. **代价**：失去 Bedrock 缓存读取折扣。Strix 每轮都重发 system prompt + 历史，
   这笔钱不小 —— 属于设计取舍，要写进文档，不是能忽略的事。

**顺带排除的两条歧路**（都试过，都不通）：

- **litellm 的 `mantle` 路由**（`bedrock/mantle/…`、`bedrock_mantle/…`，原生认 bearer）：
  端点 `bedrock-mantle.<region>.api.aws` 在本网络**同样被 TLS 解密**（`CERTIFICATE_VERIFY_FAILED`），
  而且它照样会被注入 cache 参数。双重死。**又一次"同一家云不同子域待遇不同"** ——
  `bedrock-runtime.<region>.amazonaws.com` 没被拦，`bedrock-mantle` 被拦了。见条 15、19。
- **换个绕过缓存判定的模型名**（如 inference profile ARN）：能让 `supports_prompt_caching` 返回
  `False`，但**同时会让 `completion_cost` 查不到价** → 预算护栏静默变 0。为躲一个坑掉进更深的坑。

**给 T3 的判据**（已加进 `scripts/m0_probe_inner.sh`，**必须排在 `ValidationException` 那条之前**）：
正文含 `cache_control_injection_points` → `prompt_cache_unsupported_on_route`。
排后面会被归成 `model_access_denied`，让人跑去 AWS 控制台申请模型权限 —— 查错方向。
**同一个 `LLM CONNECTION FAILED` 面板下现在躺着四种故障了**（TLS 解密 / 凭据无效 /
凭据形状与路由不匹配 / 路由不接受的参数）。

**顺带修掉的一个自身缺陷**：本次 `m0_probe_inner.sh` **五条断言全绿、打印了"M0 达成"、退出 0**，
而 strix 退出 1 且**零个 LLM 轮次完成**。原因是断言 2 不依赖扫描是否跑起来，
断言 1/3/5 在早期失败时也可能因为"目录没建 / 没有沙箱 / 因此没有残留"而各自成立 ——
**全绿完全可能是什么都没发生。** 已加第二道独立的门：strix 退出码必须 ∈ {0, 2}，
否则退出 4（宿主脚本同步了这个码）。教训：**写验收脚本时，"所有检查都通过"和
"被检查的东西真的发生了"是两个命题，必须分别断言。**

**实测**：2026-09-08（M0 第二次用真 Bedrock API key 跑，崩在这里；修复方案已用假 token + 直接调
`make_model_settings` 双向验证，未再花真钱）。

---

## 24. `strix` 退出码 **0 不代表扫描跑完了** —— 预算耗尽也退 0，还报"没有漏洞"

**现象**（2026-09-08 M0 第 3 次运行，第一次真跑完）：对 juice-shop —— OWASP 那个**故意塞满漏洞**的
靶场 —— 扫描结束，`strix` **退出 0**，面板写：

```
SESSION ENDED
Target  http://juice-shop:3000
Vulnerabilities  0 (No exploitable vulnerabilities detected)
Input Tokens 603.0K · Output Tokens 4.1K · Cost $2.0572
```

而 `run.json` 里写的是 **`"status": "stopped"`**，`strix.log` 里写的是：

```
agent 8adaacc6 reached the scan budget limit; stopping the scan:
  Token budget of $2.00 exceeded (spent $2.0572)
```

**扫描根本没跑完，是被预算掐死的。** "No exploitable vulnerabilities detected" 在这里是个
**假的健康证明** —— 它只意味着"在钱花光之前没找到"。

**根因**：退出码的**唯一**来源是 `interface/main.py:494-497`：

```python
if args.non_interactive:
    report_state = get_global_report_state()
    if report_state and report_state.vulnerability_reports:
        sys.exit(2)
```

只看"有没有找到漏洞"，**完全不携带"扫描是否完成"的信息**。于是这两种情况的退出码完全一样：

| 实际发生的事 | 退出码 | `run.json.status` |
|---|---|---|
| 跑完了，目标确实干净 | `0` | `completed` |
| **预算/轮次耗尽，什么都没查到** | `0` | `stopped` |
| 找到漏洞（无论是否跑完） | `2` | `completed` / `stopped` |

**处置（这是产品级的安全要求，不是优化项）**：控制台**绝不能**只凭退出码 0 就展示"未发现漏洞"。
必须读 `run.json.status`（`core/agents.py:25` 的取值域是
`running/waiting/completed/stopped/crashed/failed/budget_paused`），`stopped` 时中文结论必须是
**「扫描因预算耗尽提前结束，结论不完整」**，而不是「未发现漏洞」。
一个渗透测试控制台在钱花光时报"目标干净"，比不报任何结论危险得多。
`PLAN.md` §后端接口原本就写了"与 `run.json.status` 交叉校验，冲突时以 run 记录为准" ——
**这次实测证明那不是防御性冗余，而是唯一的真相来源。**

**顺带三条实测数字**：

1. **`--max-budget-usd` 是软上限，不是硬上限**：给 `2`，花了 **`$2.0572`（超 2.9%）**。
   `core/hooks.py:55` 的 `budget_stopped = cost >= max_budget_usd` 是**每轮结束后**才判，
   所以必然超一轮的量。UI **不许**承诺"绝不超过 $X"，只能说"达到 $X 后停止"。
2. **还有一道 90% 的子代理保留线**（`_SUBAGENT_BUDGET_RESERVE = 0.90`，`core/hooks.py:29`）：
   子代理花到 `$1.8877`（≥90%）就被停掉，"让 root agent 能收尾"。所以实际预算结构是
   **子代理 90% 停 / root 100% 停 / 再超一轮**。做预算 UI 时要照这个说，别自己编。
3. **关掉 prompt cache 的代价是可量化的**：13 次请求、603K 输入 token、
   `cached_tokens: 0` / `cache_write_tokens: 0`，每次请求输入约 45K 且逐轮增长
   （同一份 system prompt + 历史重发 13 遍）。**$2 只买到 13 次 LLM 请求，在 juice-shop 上一无所获。**
   Bedrock 缓存读取是原价的 1/10，若开着缓存这笔钱大致是 1/4～1/6。
   → **SigV4 形状不是"也行"，是便宜 4～6 倍**（见条 22、23：bearer 被迫走 `invoke/`，
     `invoke/` 被迫关缓存）。这个取舍必须让用户自己做，UI 要给数字。

**实测**：2026-09-08（M0 第 3 次运行，6 条断言全过）。

---

## 25. `persist_current()` 按 alias 表把凭据明文写进 `cli-config.json` —— **凭据卫生的结论按形状而定，不可迁移**

**现象**：M0 第 3 次运行，断言 2c 报告"预置 config 内**不含**凭据（`persist_current` 未被调用）
→ 比 `PLAN.md` 预期更好"。**这句话是错的，而且错的方向很危险。**

线索就在报告自己里：预置的 `cli-config.json` 写进去时是 `{"env":{}}` = **11 字节**，
跑完变成 **223 字节**。它明明**被写过**。`persist_current()` 在 `interface/main.py:404`
是**无条件调用**的，压根没有"未被调用"这回事。

**根因**（`config/loader.py:56-74`）：它遍历所有 settings 字段的 alias，把**环境里同名的值原样写进文件**：

```python
for alias in _aliases_for(finfo):
    value = os.environ.get(alias.upper())
    if value:
        env_block[alias.upper()] = value
write_secret_text(target, json.dumps({"env": env_block}, indent=2))
```

用四个假凭据同时喂进去实测（零成本），结果是**分形状的**：

| env 变量 | 是否被写进 `cli-config.json` | 为什么 |
|---|---|---|
| `LLM_API_KEY` | **★ 明文落盘 ★** | 是 `LlmSettings.api_key` 的 alias（`config/settings.py:27-31`）|
| `AWS_BEARER_TOKEN_BEDROCK` | 未落盘 | 不是任何 settings 字段的 alias |
| `AWS_ACCESS_KEY_ID` | 未落盘 | 同上 |
| `AWS_SECRET_ACCESS_KEY` | 未落盘 | 同上 |

写出的文件长这样（`STRIX_IMAGE`、`STRIX_TELEMETRY` 这些它也一起写）：

```json
{ "env": { "STRIX_LLM": "bedrock/invoke/…", "LLM_API_KEY": "FAKE-SINGLE-KEY-0002",
           "STRIX_IMAGE": "ghcr.io/usestrix/strix-sandbox:1.3.0", "STRIX_TELEMETRY": "false" } }
```

**结论，两条**：

1. **`PLAN.md` 原稿是对的**，我那句"比预期更好"是错的。原稿断言
   "`cli-config.json` **里有** Key（证明重定向生效）"对 **`single` 形状**（Anthropic / OpenAI /
   Gemini / DeepSeek —— 也就是**大多数**供应商）**完全成立**。M0 恰好测的是那个唯一不落盘的形状。
2. **所以 tmpfs HOME + 显式 `--config` + `finally: rmtree` 不是纵深防御，对 `single` 形状而言
   它是唯一一道防线。** 少任何一环，用户的 API Key 就明文躺在一个持久文件里。
   这三样谁都不许"简化掉"。

**通用教训（这已经是同一个错误的第三次了）**：
条 15 是"`pypi.org` 没被拦推不出 LLM 端点没被拦"，条 23 是"`bedrock-runtime` 没被拦推不出
`bedrock-mantle` 没被拦"，这条是"**bearer 形状不落盘推不出 `single` 形状不落盘**"。
**一次实测只证明被测的那一个配置。** 凭据卫生断言必须**按 auth_shape 各跑一遍**，
`test_key_hygiene` 要参数化，不能只测一种形状就宣布通过。

**实测**：2026-09-08（M0 第 3 次运行后复查报告里的字节数异常发现；修正结论用假凭据直接调
`persist_current()` 验证，零成本）。

---

## 26. `openssl req -help` 的退出码是 **1**，`pipefail` 下能力探测会在 grep 明明命中时判失败

LibreSSL（macOS 自带）与 OpenSSL 的 `-help` 都走"打用法然后报错退出"的路子：

```
$ openssl req -help >/dev/null 2>&1; echo $?
1
```

于是这种写法在 `set -o pipefail` 下必然误判 —— **grep 命中了，整条管道仍然是失败**：

```bash
openssl req -help 2>&1 | grep -q -- '-addext'      # ✗ rc=1，哪怕 -addext 确实支持
{ openssl req -help 2>&1 || true; } | grep -q -- '-addext'   # ✓
```

后果不是"少一个功能"，而是**探测逻辑走进 fallback 分支**：以为不支持 `-addext`，改用临时 openssl.cnf
或干脆不加扩展 → 签出来的证书没有 SAN/EKU → Chrome 拒连，而脚本一路 `EXIT=0`。

**触发条件**：写任何 `<cmd> --help | grep` 形式的**能力探测**。同类命令还有 `docker buildx --help`、
`tar --help`。规则：能力探测一律套 `{ …… || true; } | grep`，别信 `--help` 的退出码。

**实测**：2026-09-08（T4 写 `setup.sh` 的证书签发时踩到）。

---

## 27. Compose 的 `${V:+x}` 求出**空串**时仍然把变量设进容器，而空 `SSL_CERT_FILE` = **0 张根证书且不报错**

想表达"变量非空时才设这个 env"，直觉写法是 `SSL_CERT_FILE: "${STRIX_EXTRA_CA_FILE:+/etc/strix/extra-ca.pem}"`。
变量为空时它求出**空字符串**，而 Compose 会把 `SSL_CERT_FILE=` **设进容器**（不是不设）。实测：

```
$ docker run --rm -e SSL_CERT_FILE= strix-console/api:0.1.0 \
    python -c "import ssl; print(len(ssl.create_default_context().get_ca_certs()))"
0
$ docker run --rm            strix-console/api:0.1.0 ... 同上
150
```

**空 `SSL_CERT_FILE` 让 OpenSSL 加载 0 张根证书，且不抛任何错。** 容器里所有 TLS 静默失去信任链 ——
连 `pypi.org` 都验不过，而故障现象是"证书验证失败"，没人会怀疑到一个空环境变量。

唯一干净写法：`:-` 兜底到一个**真实存在**的 bundle，**并且加断言确认那个路径在镜像里存在且非空**
（基础镜像换版本就可能挪走它）：

```yaml
SSL_CERT_FILE: "${CONSOLE_CA_BUNDLE:-/etc/ssl/certs/ca-certificates.crt}"
```

**触发条件**：在 compose 里用 `:+` 表达"可选 env"；或给容器设任何 `*_CA_BUNDLE` / `SSL_CERT_*` /
`REQUESTS_CA_BUNDLE` / `NODE_EXTRA_CA_CERTS`。

**实测**：2026-09-08（T4 实现 N2 企业 CA 时）。

---

## 28. "整条挂载可选"在 Compose 里**表达不出来** —— 只能挂 `/dev/null`

```yaml
- "${VAR:+${VAR}:/target:ro}"   # ✗ VAR 为空时 Compose v5.4.0：invalid empty volume spec
- "${VAR:-/dev/null}:/target:ro"  # ✓ 唯一干净写法
```

Compose 没有条件挂载语法，`volumes` 列表元素不能求值成空。挂 `/dev/null` 是无害的，**前提是没有任何
变量指向那个目标路径**（否则程序会读到一个空文件，回到条 27 那类静默失败）。

**触发条件**：写任何可选 bind mount。

**实测**：2026-09-08（T4 实现 N2）。

---

## 29. `docker exec <c> python - <<'EOF'` 不带 `-i`，stdin **根本没送进去**，脚本静默空跑且退出码 0

```bash
docker exec  c python - <<'EOF'   # ✗ 什么都没执行，rc=0，看上去"成功了"
docker exec -i c python - <<'EOF' # ✓
```

没有 `-i` 时 `docker exec` 不给容器分配 stdin，heredoc 的内容被丢弃，`python -` 读到 EOF 立即正常退出。
**危险的地方在于退出码是 0** —— 于是"清理脚本已执行""断言已通过"这类判断全是假的。
T4 就靠这个误判了一次清理是否生效。

**触发条件**：用 heredoc 往容器里喂脚本（`python -` / `sh -s` / `psql` 都一样）。
自查：脚本第一行加一条一定会有输出的 `print`/`echo`，看不到输出就说明 stdin 没进去。

**实测**：2026-09-08（T4 验证 nginx 时）。

---

## 30. nginx 的 **error log 含 query string 且格式不可配置**；`log_format` 里用 `$uri` 只管住 access log

`log_format` 只作用于 `access_log`。error log 的格式在 nginx 里是**硬编码**的，会打完整 `$request`
和上游 URL：

```
[error] connect() failed ... request: "GET /api/health?token=XXX HTTP/2.0",
        upstream: "http://172.20.0.2:8000/api/health?token=XXX"
```

把 `error_log` 级别提到 `crit` 能压住它，但会连 502/504 的归因信息一起丢掉 —— 对一个本机自建工具是坏交易。

**结论：「敏感值绝不进 URL」是后端必须自己守的约束，nginx 这层守不住。** 反过来，验收 #11 扫日志时
**必须把 nginx 的 stderr 一起扫**，别只扫 api。

**触发条件**：设计日志脱敏面；写验收 #11 的日志扫描；判断"某一层是否提供了结构性保证"。

**实测**：2026-09-08（T4）。

---

## 31. 容器化跑 `nginx -t` 必须把证书目录一起挂进去，否则报错像是配置语法错

```
nginx: [emerg] cannot load certificate "/etc/nginx/tls/cert.pem":
       BIO_new_file() failed (SSL: error:80000002:system library::No such file or directory)
```

看着像 `ssl_certificate` 那行写错了，其实是**一次性校验容器少挂了一个卷**。`nginx -t` 会真的去加载证书，
不是纯语法检查。

```bash
docker run --rm -v "$PWD/nginx/nginx.conf:/etc/nginx/nginx.conf:ro" \
                -v "${DATA}/tls:/etc/nginx/tls:ro" nginx:1.27-alpine nginx -t
```

**触发条件**：容器化校验 nginx 配置。推广：任何"一次性容器跑配置校验"，都要问一句"这个校验会不会
真的去读运行时才存在的文件"。

**实测**：2026-09-08（T4）。

---

## 32. `sqlite3.executescript()` 会**隐式 COMMIT** 掉挂着的事务

```python
conn.execute("BEGIN IMMEDIATE")
conn.executescript(sql)          # ← 这里已经隐式提交了
conn.execute("COMMIT")           # OperationalError: cannot commit - no transaction is active
```

`executescript` 执行前先隐式提交，所以任何事务控制**必须写在脚本字符串内部**。

顺带的好处：把「记账本」的 INSERT 也塞进同一段脚本，"DDL 建好了但账本没记"这个**需要人工介入的
中间态**就在结构上不存在了。代价是 `executescript` 不接受参数绑定 → 要插值的值必须先过一条形状断言
（迁移文件名走白名单正则，摘要走 `hexdigest()` 所以天然只有十六进制）。

**触发条件**：写 SQLite 迁移执行器，或任何用 `executescript()` 的地方。

**实测**：2026-09-08（T2 写 `db.migrate()`）。

---

## 33. `--no-access-log` 会被"接管 uvicorn logger"的代码**静默作废**

uvicorn 的 `--no-access-log` 的**全部**实现就是 `uvicorn/config.py:421-423` 两行：

```python
logging.getLogger("uvicorn.access").handlers = []
logging.getLogger("uvicorn.access").propagate = False
```

它靠 `propagate = False` **断掉记录的去路**。而"接管 uvicorn 的三个 logger"的标准做法恰恰是
`handlers.clear()` + `propagate = True` —— 如果那段代码在 lifespan 里跑（**晚于** uvicorn 自己的配置），
就把 `propagate` 改回了 `True`，访问日志沿 parent 链走到 root 的 handler 上**静默恢复输出**。
实测：CMD 里明明写着 `--no-access-log`，日志里照样有

```json
{"level":"INFO","logger":"uvicorn.access","msg":"127.0.0.1:45464 - \"GET /api/health HTTP/1.1\" 200"}
```

而访问日志记的是 URL，URL 是泄漏面（`CLAUDE.md` §日志 要求生产 `--no-access-log`）。

**结论**：`uvicorn.access` 必须**静音**（`handlers.clear()` + `propagate = False`），不能像
`uvicorn` / `uvicorn.error` 那样"接管"。**不要**写"读一下 uvicorn 设的 `propagate` 再决定" ——
那让行为取决于两方状态的先后顺序，正是这个 bug 的形状。开关权收归自己一家，`--no-access-log`
退化成纵深防御。需要请求级日志时自己写中间件（带 trace_id、经脱敏、不记 query）。

**附带事实**：uvicorn 在 lifespan **之前**打的两行（`Started server process` /
`Waiting for application startup.`）**必然**绕过 lifespan 里装的 formatter。它们是固定字面量、
不含变量，不是泄漏面 —— 但"启动日志前两行不是 JSON"是已知且接受的，别当 bug 查。

**触发条件**：写日志初始化；接管 uvicorn 的三个 logger；排查"明明关了访问日志却还在打"。

**实测**：2026-09-08（T2 自查时发现自己刚写的 `configure_logging()` 作废了 Dockerfile 里的 CMD 参数）。

---

## 34. ruff 的 `RUF001/002/003` 与中文注释不共存 —— 845 条噪音，真阳性 0

`RUF001/002/003`（ambiguous-unicode）会把中文标点（`，`、`（）`、`——`）逐个报成"疑似误输入的
ASCII 同形字符"。本项目实测 **845 条，真阳性 0**。必须在 `ignore` 里关掉，**并在配置里写明代价**
（真同形字符攻击不再被 lint 拦，落到代码审查上）—— 否则下一个人会以为是漏配又打开。

同一族的两个抑制注释坑：
1. 给**不在 `select` 里**的规则写抑制注释，会被 `RUF100` 反过来报"多余的抑制"。
   例：`S608` 只盯 SELECT/INSERT/UPDATE/DELETE，对 DDL 不生效，给 DDL 加它的抑制注释就是多余的。
2. 在**中文注释正文里提到那个抑制指令的四个字母**，ruff 会把它当一条真指令去解析并 warning。
   讨论抑制注释时只能用中文描述，不能把那个词写出来。

**触发条件**：给一个注释与 docstring 全中文的 Python 项目配 ruff 且选了 `RUF`。

**实测**：2026-09-08（T2 配 `pyproject.toml`）。

---

## 35. `pytest` 控制台入口**不**把 cwd 放进 `sys.path`

只有 `python -m pytest` 会把 cwd 加进 `sys.path`；直接敲 `pytest` 不会。源码不经 pip 安装、
只 `COPY app ./app` 就在容器里跑测试时，`import app.db` 直接 `ImportError`。

靠"记得写 `python -m`"是隐式约定 —— 必须在 `[tool.pytest.ini_options]` 里显式写：

```toml
pythonpath = ["."]
```

**触发条件**：源码不经 pip 安装、在容器里跑 pytest。

**实测**：2026-09-08（T2）。

---

## 36. "一个对象一条报错"要靠**聚合命中词**，不能靠 `set()` 去重

`aws_secret_access_key` 会同时命中黑名单里的 `aws` / `secret` / `access` / `key` 四个词。
每命中一个就 append 一条措辞不同的字符串，`set()` **完全去不掉**重复 —— 实测同一列报了 4 遍，
把"除了它还有别的脏列吗"这个真正要看的信息淹掉了。

做法：先把一个对象的**全部**命中词收集起来，再拼成唯一一条消息：
`scans.aws_secret_access_key（命中 access, aws, key, secret）`。

**触发条件**：写任何"扫一遍名字、命中黑名单就报错"的断言（`assert_no_secret_columns`、
密钥卫生扫描、配置校验）。

**实测**：2026-09-08（T2）。

---

## 37. `logging.Formatter.formatTime()` 默认用 **localtime**，缀个 `Z` 就是在说谎

`logging.Formatter.converter` 的默认值是 `time.localtime`。所以这样写出来的时间戳是**本地时间**，
而后面那个 `Z` 宣称它是 UTC：

```python
"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}Z"   # ✗ 本地时间
converter = staticmethod(time.gmtime)                                              # ✓ 加这一行
```

**在容器里测不出来** —— 容器默认不设 `TZ`，等于 UTC，两者恰好相同。它会在有人给 compose 加一个
`TZ`、或换一台会把 TZ 传进容器的宿主时爆掉。实测 `TZ=Asia/Shanghai`：真实 UTC `04:26:34`，
日志写成 `12:26:34Z`，差 8 小时且**标签仍然是 Z**。

代价不在日志本身，而在**对账**：DB 时间戳一律 UTC ISO-8601，`audit_log` 还要与
`${DATA}/audit/YYYY-MM.ndjson` 双写。日志与 DB 差一个时区偏移、且没人知道差了，是排查时最费人的
一类问题（会得出"事件顺序不可能如此"的错误结论）。

**测试必须自己把时区改成非 UTC**（`monkeypatch.setenv("TZ", …)` + `time.tzset()`），
否则那条测试在容器里是恒真的 —— 等于没测。已实测：撤掉修复后该测试确实报
`ts 不是 UTC：得到 2025-09-04T23:33:20.000Z，UTC 应为 2025-09-04T15:33:20.000Z`。

**触发条件**：自定义 logging Formatter 并输出时间戳；给任何时间戳缀 `Z` / 声称 UTC。
**推广**：任何"格式化时间"的默认值都要查一遍时区，`datetime.now()`、`time.strftime()`、
`date` 命令（`date -u` 才是 UTC）都是同一族。

**实测**：2026-09-08（我复核 T2 交付时查出，T2 未发现）。
