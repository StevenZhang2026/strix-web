-- =============================================================================
-- 001_init.sql —— 初始 schema。对应 PLAN.md §数据模型。
--
-- 全局约定（后续迁移必须沿用）：
--
-- 1. 所有表都是 STRICT。SQLite 默认的动态类型会让 `cost_usd = "abc"` 静默存进去，
--    到出报告时才炸。STRICT 让它在 INSERT 当场失败。
--    代价：列类型只能用 INT/INTEGER/REAL/TEXT/BLOB/ANY，不许写 VARCHAR/DATETIME/BOOLEAN。
--
-- 2. 时间戳一律 TEXT，ISO-8601 UTC 带 `Z`（`2026-09-08T12:34:56.789Z`）。
--    不用 INTEGER epoch：这库要被人 `sqlite3` 直接 grep 排障，可读性值这点空间。
--    不用 SQLite 的 CURRENT_TIMESTAMP 默认值：它产出的是不带 T 也不带 Z 的
--    `YYYY-MM-DD HH:MM:SS`，与 Python 侧格式不一致，混在一列里没法排序比较。
--    时间由 Python 侧统一生成 —— 单一真源。
--
-- 3. 布尔一律 INTEGER + CHECK (x IN (0,1))。STRICT 表没有 BOOLEAN 类型。
--
-- 4. 每个 JSON 列都带 CHECK (json_valid(...))。这是"不变量写进代码不是写进文档"
--    （CLAUDE.md §编码哲学 7）：坏 JSON 的失败点被拉到 INSERT，而不是几小时后
--    读取时的 json.JSONDecodeError。JSON1 是 SQLite 3.38+ 内建，无需扩展。
--
-- 5. 状态/枚举**不写 CHECK 约束**（`status`、`phase`、`severity`、`auth_shape` 等）。
--    取值域归 Python 侧。理由：新增一个状态值不该需要一次 schema 迁移，而 SQLite
--    改 CHECK 约束只能整表重建。CHECK 只用来表达**不随需求变化**的不变量
--    （非负、>0、长度、JSON 合法性、布尔域）。
--
-- 6. 列名不得包含 key / secret / token / password / credential / access / aws /
--    bearer 等词 —— 见 app/db.py 的 assert_no_secret_columns()，启动时强制。
--    这不是命名风格，是安全不变式：**没有豁免口子**。
-- =============================================================================


-- -----------------------------------------------------------------------------
-- authorizations —— 授权声明。每次扫描必须先有一条。
--
-- 这张表存在的唯一理由：让"未经授权的扫描"在结构上不可能发生（见下方 scans）。
-- -----------------------------------------------------------------------------
CREATE TABLE authorizations (
    id                TEXT PRIMARY KEY,
    created_at        TEXT NOT NULL,

    -- 操作者自报的身份与授权凭据编号（工单号 / 合同号 / 邮件主题…）。
    -- 只要求非空字符串，不校验格式 —— 我们无法验证它，假装能验证更糟。
    operator_name     TEXT NOT NULL CHECK (length(trim(operator_name)) > 0),
    authorization_ref TEXT NOT NULL CHECK (length(trim(authorization_ref)) > 0),

    -- 声明时的目标与**当时解析到的 IP**。启动扫描前会重解析并比对，
    -- 不一致即 dns_changed 拒绝（PLAN.md §护栏）。所以这一列是证据，不是缓存。
    targets_json      TEXT NOT NULL CHECK (json_valid(targets_json)),
    resolved_ips_json TEXT NOT NULL CHECK (json_valid(resolved_ips_json)),

    -- 手打确认串（"我确认我有权测试 <target>"）。存原文而不是布尔：
    -- 事后要能证明操作者到底打了什么。
    typed_confirmation TEXT NOT NULL CHECK (length(trim(typed_confirmation)) > 0),

    -- 三条勾选式声明。CHECK 钉死 = 3：条数是需求的一部分，少一条就是漏了一个声明。
    -- 这里刻意用 CHECK（与上面第 5 条不矛盾）—— 它不是取值域，是"必须三条都在"。
    affirmations_json TEXT NOT NULL
        CHECK (json_valid(affirmations_json) AND json_array_length(affirmations_json) = 3),

    -- 操作者主动放宽的护栏项（如允许某个非白名单子域）。空对象 = 没放宽。
    overrides_json    TEXT NOT NULL CHECK (json_valid(overrides_json)),

    -- 命中的白名单条目 id + 当时该条目的完整快照。
    -- 存快照是因为白名单 YAML 会被改：事后审计要知道**当时**批准的是什么。
    allowlist_entry_id TEXT,
    allowlist_snapshot TEXT CHECK (allowlist_snapshot IS NULL OR json_valid(allowlist_snapshot)),

    user_agent        TEXT,
    client_ip         TEXT
) STRICT;


-- -----------------------------------------------------------------------------
-- scans —— 一次扫描 = 一个 Strix 子进程。
--
-- authorization_id NOT NULL 是本项目最重要的一条 DB 约束：
-- 它让"没有授权记录的扫描行"在结构上无法插入。等价的 Python 侧检查随时可能被
-- 一个新的代码路径绕过；NOT NULL 不会。ON DELETE RESTRICT 补上另一半 ——
-- 授权记录不许在还有扫描引用它时被删掉。
-- -----------------------------------------------------------------------------
CREATE TABLE scans (
    id               TEXT PRIMARY KEY,
    created_at       TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT,

    -- 生命周期状态（queued/preparing/running/finished/failed/stopped…）与细分阶段。
    -- 取值域在 Python 侧，见第 5 条。
    status           TEXT NOT NULL,
    phase            TEXT,

    authorization_id TEXT NOT NULL
        REFERENCES authorizations(id) ON DELETE RESTRICT,

    -- ---- 扫描参数（决定 argv）--------------------------------------------
    template_id      TEXT NOT NULL,
    targets_json     TEXT NOT NULL CHECK (json_valid(targets_json)),
    scan_mode        TEXT NOT NULL,
    scope_mode       TEXT,

    -- --max-budget-usd 强制必填（CLAUDE.md §安全不变式）。> 0 而不是 >= 0：
    -- 预算 0 的扫描会被 Strix 立刻掐死并宣称"未发现漏洞"，是最坏的一种假阴性。
    max_budget_usd   REAL NOT NULL CHECK (max_budget_usd > 0),
    max_turns        INTEGER CHECK (max_turns IS NULL OR max_turns > 0),
    reasoning_effort TEXT,

    -- ---- 模型与凭据形状 --------------------------------------------------
    provider         TEXT NOT NULL,

    -- auth_shape 是**独立于 provider 的一个维度**，不是它的属性（PLAN.md §N1，
    -- 2026-09-08 拍板）。NOT NULL 无默认值，理由同 max_budget_usd：
    -- "不知道用了哪种凭据形状的扫描"不该能存在。
    --
    -- 为什么必须落盘：vault_handle 指向内存 KeyVault，api 一重启就失效。续跑
    -- （POST /api/scans/{id}/resume）要重新索要凭据，而"索要哪几个键、前端渲染
    -- 1／2／3 个输入框"完全由形状决定 —— 重启后这一列是唯一线索。审计上也要能
    -- 回答"这次用的是哪种形状"。
    --
    -- 刻意不加 CHECK 取值域：新增供应商形状是预期会发生的事（见第 5 条）。
    auth_shape       TEXT NOT NULL,

    -- 交给子进程的 STRIX_LLM 值（模型名，bearer 形状会含 `invoke/`）。存的是
    -- 模型标识符，不是凭据。
    strix_llm        TEXT NOT NULL,
    api_base         TEXT,

    -- 指向内存 KeyVault 的 opaque UUID。**不是凭据**，api 重启后即失去意义。
    --
    -- 这一列原名 key_handle，2026-09-08 改名。改名的真正理由不是"绕开列名黑名单"
    -- （那只是顺带好处），而是**原名基于一个已被 PLAN.md §N1 推翻的假设**：
    -- 一个 handle 从来不是"一个 Key 字符串"，它指向一**组**凭据 ——
    -- Bedrock SigV4 要 id + secret + region 三个值。叫 key_handle 会让读代码的人
    -- 以为取出来是个字符串，然后写出只取一个值的错误代码。
    -- DB 列与 HTTP 字段统一用这个名字，不做映射（映射本身就是下一个 bug 的温床）。
    vault_handle     TEXT,

    -- ---- 子进程实况 ------------------------------------------------------
    -- Strix 产物固定写 $CWD/strix_runs/<自动名>/（CLI 无 --output-dir），
    -- 所以每任务必须有独立 cwd。三列合起来才能定位产物目录。
    cwd              TEXT,
    run_dir          TEXT,
    strix_run_name   TEXT,
    pid              INTEGER,

    argv_json        TEXT CHECK (argv_json IS NULL OR json_valid(argv_json)),

    -- 只存**变量名**，绝不存值。原名 env_keys_json，随 vault_handle 一起改名：
    -- 新名字直说了它是"变量名的列表"。
    env_var_names_json TEXT
        CHECK (env_var_names_json IS NULL OR json_valid(env_var_names_json)),

    -- instruction 正文可能含操作者提供的测试账号，只进 tmpfs 文件（0600，随任务
    -- 目录删除）。DB 只留摘要，用于证明"跑的是这份指令"。
    instruction_sha256 TEXT CHECK (instruction_sha256 IS NULL OR length(instruction_sha256) = 64),

    -- ---- 结束态 ----------------------------------------------------------
    -- exit_code 与 exit_meaning 分开存：2 = 发现漏洞 = 成功，0 = 未发现漏洞，
    -- 1 = 失败。而 0 **不代表扫描跑完了** —— 预算耗尽被掐死的扫描同样退 0。
    -- 归因的权威是 run.json.status（写进 status 列），退出码只是原始事实。
    -- 两者都存，才能在归因逻辑出错时事后复盘。
    exit_code        INTEGER,
    exit_meaning     TEXT,

    -- 稳定机器码（见 app/errors.py）。文案在前端，这里绝不存中文。
    error_code       TEXT,
    -- 已脱敏的英文摘要，用于排障。RedactingJsonFormatter 之外的第二处脱敏点。
    error_message    TEXT,

    cost_usd         REAL NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),

    count_critical   INTEGER NOT NULL DEFAULT 0 CHECK (count_critical >= 0),
    count_high       INTEGER NOT NULL DEFAULT 0 CHECK (count_high >= 0),
    count_medium     INTEGER NOT NULL DEFAULT 0 CHECK (count_medium >= 0),
    count_low        INTEGER NOT NULL DEFAULT 0 CHECK (count_low >= 0),

    agent_count      INTEGER NOT NULL DEFAULT 0 CHECK (agent_count >= 0),
    event_count      INTEGER NOT NULL DEFAULT 0 CHECK (event_count >= 0),

    resume_available INTEGER NOT NULL DEFAULT 0 CHECK (resume_available IN (0, 1)),

    -- 记下当时的 strix 版本与沙箱镜像：升级后回看老扫描要能知道跑的是哪一版。
    strix_version    TEXT,
    sandbox_image    TEXT,

    -- agents.db 不是只追加的 —— 上下文压缩会 clear_session() 后重插、id 重排。
    -- epoch 每检测到一次重排就 +1，配合 scan_events 的 PK 做重同步（见 PLAN.md）。
    current_epoch    INTEGER NOT NULL DEFAULT 0 CHECK (current_epoch >= 0)
) STRICT;

-- 列表页的唯一查询形态：按状态筛 + 按创建时间倒序。
CREATE INDEX ix_scans_status_created ON scans (status, created_at DESC);


-- -----------------------------------------------------------------------------
-- scan_events —— 我们自己的只追加事件镜像。
--
-- 为什么需要它：Strix 的 agents.db 会被 clear_session() 重写、id 重排，
-- 朴素游标增量必然错序或漏事件。这张表是我们控制的、真正只追加的那一份。
-- PK(scan_id, epoch, seq) 让"同一 epoch 内 seq 重复"在结构上不可能。
-- -----------------------------------------------------------------------------
CREATE TABLE scan_events (
    scan_id     TEXT NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    epoch       INTEGER NOT NULL CHECK (epoch >= 0),
    seq         INTEGER NOT NULL CHECK (seq >= 0),

    -- 源库里的 id。重排后会变，所以**不能**当主键，只作排障线索。
    strix_id    INTEGER,

    kind        TEXT NOT NULL,
    agent_id    TEXT,
    ts          TEXT,

    -- 事件 payload 的结构版本。将来改 data_json 形状时靠它区分老数据。
    version     INTEGER NOT NULL DEFAULT 1,

    -- 内容指纹。重同步（epoch+1 后重读整库）时用它判断"这条我已经有了"，
    -- 不靠 strix_id。NOT NULL：没有指纹就没法去重，那这张表就白建了。
    fingerprint TEXT NOT NULL,

    data_json   TEXT NOT NULL CHECK (json_valid(data_json)),

    PRIMARY KEY (scan_id, epoch, seq)
) STRICT;

-- 重同步时按指纹查重的唯一查询形态。
CREATE INDEX ix_scan_events_fingerprint ON scan_events (scan_id, fingerprint);


-- -----------------------------------------------------------------------------
-- scan_agents —— 子 agent 树。parent_id 为 NULL 即根 agent。
-- -----------------------------------------------------------------------------
CREATE TABLE scan_agents (
    scan_id       TEXT NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    agent_id      TEXT NOT NULL,
    name          TEXT,
    -- 刻意不加自引用 FK：源库里父 agent 可能后到（事件顺序不保证），
    -- 加了 FK 会让插入顺序变成隐式契约。
    parent_id     TEXT,
    status        TEXT,
    error_message TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,

    PRIMARY KEY (scan_id, agent_id)
) STRICT;


-- -----------------------------------------------------------------------------
-- scan_findings —— 漏洞发现。
--
-- raw_json 保留 Strix 的原始结构：报告翻译**绝不翻译** poc_script_code /
-- evidence / endpoint / code_locations（CLAUDE.md §错误与文案），
-- 所以原文必须完整留着。
-- -----------------------------------------------------------------------------
CREATE TABLE scan_findings (
    scan_id       TEXT NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    finding_id    TEXT NOT NULL,

    severity      TEXT NOT NULL,
    title         TEXT NOT NULL,
    cvss          REAL CHECK (cvss IS NULL OR (cvss >= 0 AND cvss <= 10)),
    cwe           TEXT,
    cve           TEXT,
    endpoint      TEXT,
    method        TEXT,
    confidence    TEXT,
    finding_class TEXT,
    first_seen_at TEXT NOT NULL,

    raw_json      TEXT NOT NULL CHECK (json_valid(raw_json)),

    -- 待翻译原文的摘要。report_translations（002，T21）以它做缓存键：
    -- 原文没变就不重复付费翻译。
    input_hash    TEXT NOT NULL CHECK (length(input_hash) = 64),

    PRIMARY KEY (scan_id, finding_id)
) STRICT;


-- -----------------------------------------------------------------------------
-- scan_media —— 截图等媒体。
--
-- Strix 只保留最近 3 张截图（内联 data URL），旧的会被淘汰 —— 所以首次见到就
-- 必须落地到 media/<sha256>.png。sha256 既是内容地址也是天然去重键。
-- -----------------------------------------------------------------------------
CREATE TABLE scan_media (
    scan_id        TEXT NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    sha256         TEXT NOT NULL CHECK (length(sha256) = 64),
    mime           TEXT NOT NULL,
    -- 列名 bytes 指字节数。> 0：零字节文件说明落地那一步失败了，别静默存下来。
    bytes          INTEGER NOT NULL CHECK (bytes > 0),
    -- 相对 ${DATA} 的路径。绝不存绝对路径：${DATA} 因部署机而异。
    rel_path       TEXT NOT NULL,
    first_agent_id TEXT,
    first_seen_at  TEXT NOT NULL,

    PRIMARY KEY (scan_id, sha256)
) STRICT;


-- -----------------------------------------------------------------------------
-- audit_log —— 审计流水。与 ${DATA}/audit/YYYY-MM.ndjson 双写。
--
-- **刻意没有外键。** 审计记录必须比它描述的对象活得久：删掉一次扫描之后，
-- "谁在什么时候删了它"这条记录本身不能跟着消失。加 ON DELETE CASCADE 会把
-- 审计变成"只记录还没被删掉的事"，那就没有审计价值了。
-- 也因此 scan_id / authorization_id 这里只是纯文本引用。
--
-- detail_json 只存掩码标签（如 "sk-ant-…4f2c"），绝不存凭据值本身。
-- -----------------------------------------------------------------------------
CREATE TABLE audit_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    at               TEXT NOT NULL,
    event            TEXT NOT NULL,
    scan_id          TEXT,
    authorization_id TEXT,
    actor            TEXT,
    detail_json      TEXT NOT NULL CHECK (json_valid(detail_json)),
    client_ip        TEXT,
    user_agent       TEXT
) STRICT;

CREATE INDEX ix_audit_at ON audit_log (at DESC);
CREATE INDEX ix_audit_scan ON audit_log (scan_id, at DESC);
