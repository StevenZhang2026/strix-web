-- 002：中文人话报告（T21）的翻译缓存。沿用 001 的全局约定（STRICT、时间戳 TEXT、JSON 列
-- CHECK(json_valid)、枚举不写 CHECK、列名不含 key/secret/token/…）。
--
-- 缓存键 = (scan_id, finding_id, input_hash, model, lang)：
--   - input_hash 取 scan_findings.input_hash（整条原始记录 canonical JSON 的 sha256）——
--     原文没变、模型没换、语言没换，就不重复付费翻译；任何一项变了自然落到新的一行。
--   - 总述（executive）那一行的 finding_id 用保留值 '__executive__'，input_hash 是总述输入的摘要。
--     因为它没有对应的发现，所以**不对 scan_findings 建外键**，只挂 scans。
--
-- 用量列叫 usage_prompt / usage_completion（对应 litellm 的 usage.prompt_tokens /
-- usage.completion_tokens），**不叫 input_tokens / output_tokens**：列名含 `token` 会让
-- assert_no_secret_columns() 拒绝启动（子串匹配、没有豁免）。
--
-- 不写 BEGIN / COMMIT：迁移执行器自己包事务。

CREATE TABLE report_translations (
    scan_id          TEXT NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    finding_id       TEXT NOT NULL,   -- scan_findings.finding_id；总述那一行用保留值 '__executive__'
    input_hash       TEXT NOT NULL CHECK (length(input_hash) = 64),
    model            TEXT NOT NULL,   -- 用户选的 strix_llm 原值
    lang             TEXT NOT NULL,
    payload_json     TEXT NOT NULL CHECK (json_valid(payload_json)),
    cost_usd         REAL CHECK (cost_usd IS NULL OR cost_usd >= 0),  -- NULL = 算不出，≠ 0
    usage_prompt     INTEGER CHECK (usage_prompt IS NULL OR usage_prompt >= 0),
    usage_completion INTEGER CHECK (usage_completion IS NULL OR usage_completion >= 0),
    created_at       TEXT NOT NULL,
    PRIMARY KEY (scan_id, finding_id, input_hash, model, lang)
) STRICT;
