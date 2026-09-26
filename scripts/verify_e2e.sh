#!/bin/bash
# =============================================================================
# 端到端验收（PLAN.md §端到端验收）—— 宿主侧脚本。**请由你本人在自己的终端里运行。**
#
#   make verify-e2e                      —— 跑本段全部（不含 21）
#   ONLY="22 23 27" ./scripts/verify_e2e.sh   —— 只跑列出的几条
#   TEARDOWN=1 make verify-e2e           —— 额外跑 21（会 `compose down` 拆掉栈，永远最后跑）
#
# 前提：栈已由你起好（`docker compose -p strix-console up -d api web nginx`）。
# 本脚本不跑 setup.sh、不 build、不起栈 —— 没起就直接报错退出。
#
# 结果只有四态：PASS / FAIL / MANUAL（脚本判不了，要人看）/ SKIP（未被选中）。
# 有 FAIL → 退出码 1。MANUAL 不算通过，表下会写明有几条待人工核对。
#
# 秘密（登录口令、会话 cookie）**绝不进任何进程的 argv**（`ps` 全机可见）：
#   · 口令用 `read -rs` 读进 shell 变量，只经 stdin 管道交给 python3/curl/grep；
#     `printf` 是 bash 内建，管道左边用它不产生进程。
#   · cookie 只在 mktemp 出来的 0600 文件里（curl -b/-c 的参数是文件路径，不是值）。
#   · 只跑 1/22/23/24/27 时不需要登录，也就不会问口令。
#
# 本脚本的硬不变式是「不许假绿」：每个"0 命中 / 必须失败"的断言都配一个同语料的
# 阳性对照（对照不成立 → FAIL）；每个 4xx 断言同时断言稳定机器码 `code`。
# =============================================================================
set -uo pipefail
# 不用 `set -e`：一条检查里某个命令失败应当让**这一条** FAIL，而不是让整张表消失。

# 中文提示里 ${VAR} 一律带花括号 —— macOS 自带 bash 3.2 会把全角标点吞进变量名，
# `set -u` 下直接崩。见 pitfalls/history-pitfalls.md 条 14。
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

PROJECT="strix-console"
BASE="https://127.0.0.1"
DEFAULT_CHECKS="1 2 3 4 5 22 23 24 27 28"
DB_NAME="console.sqlite" # backend/app/settings.py 的 db_path；写错 sqlite3 会新建空库

die() { printf '✗ %s\n' "$*" >&2; exit 1; }
dc() { docker compose -p "${PROJECT}" "$@"; }

for tool in docker curl jq openssl python3 nc; do
    command -v "${tool}" >/dev/null 2>&1 || die "缺少命令 ${tool}"
done

DATA="$(dc exec -T api printenv CONSOLE_DATA_DIR 2>/dev/null | tr -d '\r')"
[ -n "${DATA}" ] || die "取不到 api 容器的 CONSOLE_DATA_DIR —— 栈没起？先 docker compose -p ${PROJECT} up -d api web nginx"
[ -d "${DATA}" ] || die "${DATA} 在宿主上不存在 —— 同路径挂载不成立"
CERT="${DATA}/tls/cert.pem"
[ -f "${CERT}" ] || die "找不到证书 ${CERT}"

TMP="$(mktemp -d)" || die "mktemp 失败"
chmod 700 "${TMP}"
JAR="${TMP}/cookies"
: > "${JAR}" && chmod 600 "${JAR}"
RESULTS="${TMP}/results"
: > "${RESULTS}"

LOGGED_IN=""
PW=""
VAULT_HANDLE=""
ALLOWLIST_BACKUP="" # 非空 = 改过白名单，cleanup 要 PUT 回去
ALLOWLIST_CREATED="" # 非空 = 检查 4 临时建了清单文件，cleanup 要删掉
ALLOWLIST_MADE_DIR=""
STACK_DOWN=""

# ---- 选择要跑的检查 ----------------------------------------------------------
SELECTED="${ONLY:-${DEFAULT_CHECKS}}"
if [ "${TEARDOWN:-}" = "1" ]; then SELECTED="${SELECTED} 21"; fi
selected() { case " ${SELECTED} " in *" $1 "*) return 0 ;; esac; return 1; }

# ---- 结果记录 -----------------------------------------------------------------
CUR=""
CUR_STATE=""
begin() { CUR="$1"; CUR_STATE="PASS"; printf '\n== 检查 %s ==\n' "${CUR}"; }
ok() { printf '  ✓ %s\n' "$*"; }
bad() { CUR_STATE="FAIL"; printf '  ✗ %s\n' "$*"; }
manual() { [ "${CUR_STATE}" = "FAIL" ] || CUR_STATE="MANUAL"; printf '  ? %s\n' "$*"; }
finish() { printf '%s|%s\n' "${CUR}" "${CUR_STATE}" >> "${RESULTS}"; }

# ---- HTTP -------------------------------------------------------------------
# req <auth|anon> METHOD PATH [curl 参数…] → HTTP_STATUS / HTTP_TIME / BODY
# 请求体一律由调用方从 stdin 喂（`--data-binary @-`），不进 argv。
HTTP_STATUS=""
HTTP_TIME=""
BODY=""
req() {
    local mode="$1" method="$2" path="$3" out tail
    shift 3
    local cookies=(-b /dev/null)
    if [ "${mode}" = "auth" ]; then cookies=(-b "${JAR}" -c "${JAR}"); fi
    out="$(curl -sS --cacert "${CERT}" "${cookies[@]}" -X "${method}" \
        -H 'Content-Type: application/json' --max-time 30 \
        -w '\n%{http_code} %{time_total}' "$@" "${BASE}${path}" 2>/dev/null)"
    tail="${out##*$'\n'}"
    HTTP_STATUS="${tail%% *}"
    HTTP_TIME="${tail##* }"
    if [ "${out}" = "${tail}" ]; then BODY=""; else BODY="${out%$'\n'*}"; fi
}
api() { req auth "$@"; }
code_of() { jq -r '.code // empty' <<<"${BODY}" 2>/dev/null; }

# expect LABEL STATUS CODE —— 4xx 一律同时断言 code（只看状态码会把
# concurrency_limit 的 409 当成 missing_typed_confirmation 的 409）。
expect() {
    local got_code
    got_code="$(code_of)"
    if [ "${HTTP_STATUS}" = "$2" ] && [ "${got_code}" = "$3" ]; then
        ok "$1 → $2 ${got_code}"
    else
        bad "$1：期望 $2 $3，实得 ${HTTP_STATUS} ${got_code:-<无 code>}"
    fi
}

ensure_login() {
    [ -n "${LOGGED_IN}" ] && return 0
    local user
    [ -t 0 ] || die "要登录的检查必须在交互终端里跑（stdin 不是终端，read 读不到输入）；只跑免登录的用 ONLY=\"1 22 23 24 27\""
    printf '\n需要登录控制台（口令不回显，不进 argv、不落盘）。\n'
    read -r -p "用户名: " user || die "读不到用户名"
    read -r -s -p "口令: " PW || die "读不到口令"
    printf '\n'
    # 进程替换而不是管道：管道会让 api 跑在子 shell 里，HTTP_STATUS 带不回来；也不落临时文件
    api POST /api/auth/login --data-binary @- < <(printf '%s\n%s' "${user}" "${PW}" | python3 -c '
import json, sys
user, password = sys.stdin.read().split("\n", 1)
sys.stdout.write(json.dumps({"username": user, "password": password}))
')
    [ "${HTTP_STATUS}" = "200" ] || die "登录失败：${HTTP_STATUS} $(code_of)"
    grep -q $'\tsid\t' "${JAR}" || die "登录 200 但响应没有 Set-Cookie sid"
    LOGGED_IN=1
}

# 检查 4 要一个 vault handle：假值 + verify:false，不出网、不花钱。cleanup 里删。
ensure_handle() {
    [ -n "${VAULT_HANDLE}" ] && return 0
    api GET /api/providers
    local body
    body="$(jq -c '
        .providers[] | select(.provider == "bedrock") as $p
        | $p.shapes[] | select(.auth_shape == "bedrock_sigv4")
        | {provider: $p.provider, auth_shape, strix_llm: $p.models[0],
           secrets: (reduce .secret_keys[] as $k ({}; .[$k] = "verify-e2e-fake")),
           params: (reduce .param_keys[] as $k ({}; .[$k] = "us-east-1")),
           verify: false}' <<<"${BODY}")"
    [ -n "${body}" ] || { bad "GET /api/providers 里没有 bedrock/bedrock_sigv4"; return 1; }
    printf '%s' "${body}" > "${TMP}/key_fake.json"
    api POST /api/keys --data-binary @- < "${TMP}/key_fake.json"
    VAULT_HANDLE="$(jq -r '.vault_handle // empty' <<<"${BODY}")"
    [ "${HTTP_STATUS}" = "201" ] && [ -n "${VAULT_HANDLE}" ] ||
        { bad "登记假 Key 失败：${HTTP_STATUS} $(code_of)"; return 1; }
}

# 删掉检查 4 临时建的清单文件，并断言 api 真的回到了「没有清单」（对照：删了但没生效 = FAIL）
remove_temp_allowlist() {
    [ -n "${ALLOWLIST_CREATED}" ] || return 0
    rm -f "${ALLOWLIST_CREATED}"
    [ -n "${ALLOWLIST_MADE_DIR}" ] && rmdir "${DATA}/config" 2>/dev/null
    ALLOWLIST_CREATED=""
    api GET /api/allowlist
    if [ "$(jq -r .file_present <<<"${BODY}")" = "false" ]; then ok "临时清单已删，api 回到「没有清单」"
    else bad "临时清单删了但 api 仍报 file_present=$(jq -r .file_present <<<"${BODY}")"; fi
}

cleanup_api() {
    [ -n "${LOGGED_IN}" ] && [ -z "${STACK_DOWN}" ] || return 0
    [ -n "${ALLOWLIST_CREATED}" ] && remove_temp_allowlist
    if [ -n "${VAULT_HANDLE}" ]; then
        api DELETE "/api/keys/${VAULT_HANDLE}"
        [ "${HTTP_STATUS}" = "204" ] || printf '⚠ 删除假 Key 失败：%s\n' "${HTTP_STATUS}" >&2
        VAULT_HANDLE=""
    fi
    if [ -n "${ALLOWLIST_BACKUP}" ]; then
        api PUT /api/allowlist --data-binary @- < "${ALLOWLIST_BACKUP}"
        [ "${HTTP_STATUS}" = "200" ] ||
            printf '⚠ 白名单没能恢复（%s）—— 原配置在 %s，请手工 PUT 回去\n' "${HTTP_STATUS}" "${ALLOWLIST_BACKUP}" >&2
        [ "${HTTP_STATUS}" = "200" ] && ALLOWLIST_BACKUP=""
    fi
    api POST /api/auth/logout
    LOGGED_IN=""
}
on_exit() {
    cleanup_api
    PW=""
    # 白名单没恢复成功时保留备份，其余一律删
    if [ -n "${ALLOWLIST_BACKUP}" ]; then rm -f "${JAR}" "${TMP}/dump.sql"; else rm -rf "${TMP}"; fi
}
trap on_exit EXIT
trap 'exit 130' INT TERM

# =============================================================================
# 检查
# =============================================================================
check_1() {
    req anon GET /api/health
    if [ "${HTTP_STATUS}" = "200" ] && [ "$(jq -r .status <<<"${BODY}")" = "ok" ]; then
        ok "/api/health 200 status=ok（$(jq -r '"app \(.app_version) / strix \(.strix_version)"' <<<"${BODY}")）"
    else
        bad "/api/health：${HTTP_STATUS} ${BODY}"
    fi
}

check_2() {
    ensure_login
    api GET /api/system/status
    [ "${HTTP_STATUS}" = "200" ] || { bad "/api/system/status → ${HTTP_STATUS} $(code_of)"; return; }
    local field
    for field in .docker.reachable .network.present .network.api_attached \
        .data_dir.identical_path_ok .sandbox_image.present; do
        if [ "$(jq -r "${field}" <<<"${BODY}")" = "true" ]; then ok "${field} == true"; else
            bad "${field} = $(jq -c "${field}" <<<"${BODY}")，应为 true"; fi
    done
    if [ "$(jq -r .telemetry.strix_telemetry <<<"${BODY}")" = "false" ]; then
        ok ".telemetry.strix_telemetry == false"
    else bad ".telemetry.strix_telemetry = $(jq -c .telemetry <<<"${BODY}")，应为 false"; fi
}

# assert_target IDX LABEL JQ_PREDICATE —— 对 validate 响应的第 IDX 个目标断言
assert_target() {
    local item
    item="$(jq -c ".targets[$1]" <<<"${BODY}")"
    if jq -e "$3" >/dev/null 2>&1 <<<"${item}"; then ok "$2"; else bad "$2 不成立：${item}"; fi
}

check_3() {
    ensure_login
    jq -n '{raw: ["http://169.254.169.254/", "http://metadata.google.internal/",
                  "http://10.20.1.5/", "http://localhost:13000",
                  "https://admin:pw@example.com", "http://例子.中国/"],
            overrides: {allow_loopback: false, allow_private: false}}' > "${TMP}/validate.json"
    api POST /api/targets/validate --data-binary @- < "${TMP}/validate.json"
    [ "${HTTP_STATUS}" = "200" ] || { bad "/api/targets/validate → ${HTTP_STATUS} $(code_of)"; return; }
    [ "$(jq '.targets | length' <<<"${BODY}")" = "6" ] || { bad "响应条数不是 6：${BODY}"; return; }
    local meta='.ok == false and .ip_class == "metadata" and .code == "blocked_metadata" and .overridable == false'
    assert_target 0 "169.254.169.254 硬拦且不可覆盖" "${meta}"
    assert_target 1 "metadata.google.internal 硬拦且不可覆盖" "${meta}"
    assert_target 2 "10.20.1.5 要求 private 确认" '.ok == false and (.required_opt_in | index("private"))'
    assert_target 3 "localhost:13000 要求 loopback 确认且带 loopback_rewrite" \
        '.ok == false and (.required_opt_in | index("loopback")) and .note_code == "loopback_rewrite"'
    assert_target 4 "带 userinfo 的 URL 被拒且有 reason" '.ok == false and (.reason // "") != ""'
    assert_target 5 "IDN 走 punycode" '.normalized.punycode_applied == true and (.normalized.host | startswith("xn--"))'
    local text
    text="$(jq -r '[.. | objects | .loopback_rewrite? // empty | strings] | first // empty' frontend/messages/zh-CN.json)"
    case "${text}" in
        *host.docker.internal*) ok "zh-CN.json 的 loopback_rewrite 提到 host.docker.internal" ;;
        *) bad "zh-CN.json 的 loopback_rewrite 没提 host.docker.internal：${text:-<取不到>}" ;;
    esac
    manual "split_horizon：实机造不出同时解析到公网+内网的名字，由 backend/tests/test_target_guard.py 覆盖"
}

# scan_body JQ_FILTER → 一份**故意错在某处**的 POST /api/scans 请求体写到 scan.json。
# 所有变体的 typed_confirmation 都是错的：即使别的判定意外放行，也起不了扫描。
scan_body() {
    jq -n --arg h "${VAULT_HANDLE}" --arg t "${TEMPLATE_ID}" '{
        vault_handle: $h, template_id: $t, targets: ["http://1.1.1.1/"],
        overrides: {allow_loopback: false, allow_private: false},
        max_budget_usd: 0.01, max_turns: 1, credentials: [],
        authorization: {operator_name: "verify-e2e", authorization_ref: "verify-e2e",
            typed_confirmation: "wrong-host.invalid",
            affirmed: ["owns_or_authorized", "not_third_party_production", "understands_real_attacks"],
            multi_target_affirmed: false, resolved_ips_seen: {}}} | '"$1" > "${TMP}/scan.json"
}
post_scan() {
    api POST /api/scans --data-binary @- < "${TMP}/scan.json"
    if [ "${HTTP_STATUS}" = "202" ]; then
        local id
        id="$(jq -r '.scan_id // empty' <<<"${BODY}")"
        bad "意外 202，扫描被接受了（${id}）—— 立刻强停"
        jq -n '{mode: "force"}' | api POST "/api/scans/${id}/stop" --data-binary @-
        HTTP_STATUS="202(已强停)"
    fi
}
put_allowlist_mode() {
    jq -c --arg m "$1" '.config | .mode = $m' <<<"${ALLOWLIST_ORIG}" > "${TMP}/allowlist_put.json"
    api PUT /api/allowlist --data-binary @- < "${TMP}/allowlist_put.json"
    [ "${HTTP_STATUS}" = "200" ] || { bad "PUT /api/allowlist mode=$1 → ${HTTP_STATUS} $(code_of)"; return 1; }
}

check_4() {
    ensure_login
    ensure_handle || return
    api GET /api/scan-templates
    TEMPLATE_ID="$(jq -r '.templates[0].template_id // empty' <<<"${BODY}")"
    [ -n "${TEMPLATE_ID}" ] || { bad "GET /api/scan-templates 取不到模板 id：${BODY:0:200}"; return; }

    scan_body 'del(.authorization)'
    post_scan && expect "缺 authorization" 422 invalid_request
    scan_body '.authorization.affirmed |= .[0:2]'
    post_scan && expect "affirmed 只给 2 项" 422 invalid_request

    # 白名单：typed_confirmation 那一格要 advisory（enforce 下 1.1.1.1 先被白名单拦），
    # 白名单那一格要 enforce。有文件时切模式、事后 PUT 回原样；没文件时临时建一份、测完删掉。
    api GET /api/allowlist
    ALLOWLIST_ORIG="${BODY}"
    local present mode
    present="$(jq -r .file_present <<<"${ALLOWLIST_ORIG}")"
    mode="$(jq -r .effective_mode <<<"${ALLOWLIST_ORIG}")"
    if [ "${present}" = "true" ]; then
        ALLOWLIST_BACKUP="${TMP}/allowlist_orig.json"
        jq -c .config <<<"${ALLOWLIST_ORIG}" > "${ALLOWLIST_BACKUP}"
        put_allowlist_mode advisory && mode=advisory
    fi
    if [ "${mode}" = "advisory" ]; then
        scan_body '.'
        post_scan && expect "typed_confirmation 写错" 409 missing_typed_confirmation
    else
        manual "typed_confirmation：白名单文件不存在且生效模式是 ${mode}，没法在不造文件的前提下测（请手工核对）"
    fi
    if [ "${present}" = "true" ]; then
        if put_allowlist_mode enforce; then
            scan_body '.'
            post_scan && expect "enforce 下目标不在白名单" 409 not_in_allowlist
        fi
    else
        # 原本没有文件：临时 PUT 一份空的 enforce 清单，测完由 cleanup 删文件。
        # 删文件就是「回到没有清单」（services/allowlist.py 的 _reload_locked）。
        [ -d "${DATA}/config" ] || ALLOWLIST_MADE_DIR=1
        ALLOWLIST_CREATED="${DATA}/config/allowlist.yaml"
        jq -n '{mode: "enforce", entries: []}' > "${TMP}/allowlist_put.json"
        api PUT /api/allowlist --data-binary @- < "${TMP}/allowlist_put.json"
        if [ "${HTTP_STATUS}" = "200" ]; then
            scan_body '.'
            post_scan && expect "enforce 下目标不在白名单（临时清单）" 409 not_in_allowlist
        else
            bad "PUT /api/allowlist（临时 enforce 清单）→ ${HTTP_STATUS} $(code_of)"
        fi
        remove_temp_allowlist
    fi

    # DB 结构上拒绝 authorization_id=NULL。其余 NOT NULL 列全部填上，断言错误正文
    # 指名 scans.authorization_id —— 因为别的列缺值而失败不算数。
    # 在 api 容器里做、不用宿主 sqlite3：库是 WAL，-shm 共享内存跨不过 Docker Desktop 的
    # VM 边界，宿主进程写一个容器正开着的 WAL 库有损坏风险。事务最后一律 ROLLBACK。
    local db="${DATA}/${DB_NAME}" err
    [ -f "${db}" ] || { bad "${db} 不存在"; return; }
    err="$(dc exec -T api python - "${db}" <<'EOF' 2>&1
print("inner-ok")
import sqlite3, sys
# mode=rw：文件不存在时报错，而不是新建一个空库
conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=rw", uri=True, isolation_level=None)
conn.execute("BEGIN")
try:
    conn.execute(
        "INSERT INTO scans (id, created_at, status, authorization_id, template_id, targets_json,"
        " scan_mode, max_budget_usd, provider, auth_shape, strix_llm) VALUES ('verify-e2e-null',"
        " '2026-01-01T00:00:00Z', 'queued', NULL, 't', '[]', 'quick', 1.0, 'bedrock',"
        " 'bedrock_sigv4', 'm')"
    )
    print("inserted")
except sqlite3.IntegrityError as exc:
    print(exc)
finally:
    conn.execute("ROLLBACK")
EOF
)"
    case "${err}" in
        inner-ok*"NOT NULL constraint failed: scans.authorization_id"*) ok "DB 拒绝 authorization_id=NULL" ;;
        *) bad "DB 没按预期拒绝（已回滚）：${err}" ;;
    esac
}

check_5() {
    ensure_login
    api GET /api/providers
    jq -c '.providers[] | select(.provider == "bedrock") as $p
        | $p.shapes[] | select(.auth_shape == "bedrock_sigv4")
        | {provider: $p.provider, auth_shape, strix_llm: $p.models[0],
           secrets: (reduce .secret_keys[] as $k ({}; .[$k] = "AKIAVERIFYE2EGARBAGE")),
           params: (reduce .param_keys[] as $k ({}; .[$k] = "us-east-1")),
           verify: true}' <<<"${BODY}" > "${TMP}/key_garbage.json"
    [ -s "${TMP}/key_garbage.json" ] || { bad "GET /api/providers 里没有 bedrock/bedrock_sigv4"; return; }
    api POST /api/keys --data-binary @- < "${TMP}/key_garbage.json"
    expect "垃圾 Key verify:true" 400 key_verify_failed
    # 门槛 = 后端验活超时（services/llm_client.py 的 VERIFY_TIMEOUT_SECONDS）：证明是被服务端
    # 拒回、不是挂到超时。原先的 2s 取决于到 Bedrock 的网络，本机经防火墙实测 4.1s（2026-09-26 改）。
    local limit
    limit="$(dc exec -T api python -c 'from app.services.llm_client import VERIFY_TIMEOUT_SECONDS as t; print(t)' | tr -d '\r')"
    if awk -v t="${HTTP_TIME}" -v l="${limit}" 'BEGIN { exit !(l > 0 && t > 0 && t < l) }'; then
        ok "${HTTP_TIME}s 内返回（< 验活超时 ${limit}s）"
    else bad "耗时 ${HTTP_TIME}s，应 < 验活超时 ${limit:-<取不到>}s"; fi
    if [ "${HTTP_STATUS}" = "201" ]; then
        api DELETE "/api/keys/$(jq -r .vault_handle <<<"${BODY}")"
    fi
}

check_21() {
    dc down || { bad "docker compose -p ${PROJECT} down 失败"; return; }
    STACK_DOWN=1
    # 阳性对照：down 真的发生了（本项目一个容器都不剩）
    if [ -z "$(dc ps -a -q)" ]; then ok "${PROJECT} 的容器已全部移除"; else bad "down 之后仍有 ${PROJECT} 容器"; fi
    local all leaked
    all="$(docker ps -a --filter label=strix-run-type=console --format '{{.ID}}')" ||
        { bad "docker ps 失败"; return; }
    # 不是"为空"：M0 靶场带同一个 type label 但没有 run-id
    leaked="$(docker ps -a --filter label=strix-run-type=console \
        --format '{{.ID}} {{.Label "strix-run-id"}}' | awk '$2 != ""')"
    if [ -z "${leaked}" ]; then
        ok "没有带 strix-run-id 的沙箱残留（type label 命中 $(printf '%s' "${all}" | grep -c .) 个，均无 run-id）"
    else bad "沙箱容器泄漏：${leaked}"; fi
    if [ -f "${DATA}/${DB_NAME}" ]; then ok "${DB_NAME} 仍在"; else bad "${DATA}/${DB_NAME} 没了"; fi
}

perm_of() {
    if [ "$(uname)" = "Darwin" ]; then stat -f %Lp "$1"; else stat -c %a "$1"; fi
}

check_22() {
    local text perm
    text="$(openssl x509 -noout -text -in "${CERT}" 2>&1)" || { bad "openssl 读不了 ${CERT}：${text}"; return; }
    local want
    for want in "Subject Alternative Name" "DNS:localhost" "IP Address:127.0.0.1" "TLS Web Server Authentication"; do
        case "${text}" in *"${want}"*) ok "证书含 ${want}" ;; *) bad "证书缺 ${want}" ;; esac
    done
    # ::1 在 LibreSSL / OpenSSL 下打印成两种样子
    case "${text}" in
        *"IP Address:0:0:0:0:0:0:0:1"* | *"IP Address:::1"*) ok "证书含 IP ::1" ;;
        *) bad "证书缺 IP ::1" ;;
    esac
    perm="$(perm_of "${DATA}/tls/key.pem")"
    if [ "${perm}" = "600" ]; then ok "key.pem 权限 600"; else bad "key.pem 权限是 ${perm:-<不存在>}，应为 600"; fi
}

check_23() {
    if nc -z -w 2 127.0.0.1 443 >/dev/null 2>&1; then ok "443 通（对照）"; else bad "443 不通 —— 对照失败，80 的结论不可信"; fi
    if nc -z -w 2 127.0.0.1 80 >/dev/null 2>&1; then bad "80 端口在监听"; else ok "80 不通"; fi
    local ps published
    ps="$(dc ps --format json)" || { bad "docker compose ps 失败"; return; }
    # 新版 compose 每行一个对象，旧版一个数组 —— 两种都收
    ps="$(jq -s 'if length == 1 and (.[0] | type) == "array" then .[0] else . end' <<<"${ps}")"
    local svc
    for svc in api web nginx; do
        [ "$(jq --arg s "${svc}" '[.[] | select(.Service == $s)] | length' <<<"${ps}")" = "1" ] ||
            bad "compose ps 里没有 ${svc}（对照失败）"
    done
    published="$(jq -r '[.[] | select([.Publishers[]? | select(.PublishedPort > 0)] | length > 0) | .Service] | sort | join(",")' <<<"${ps}")"
    if [ "${published}" = "nginx" ]; then ok "只有 nginx 有发布端口"; else bad "有发布端口的服务：${published:-<无>}，应只有 nginx"; fi
    if [ "$(jq -r '[.[] | select(.Service == "nginx") | .Publishers[] | select(.PublishedPort > 0) | "\(.URL):\(.PublishedPort)"] | join(",")' <<<"${ps}")" = "127.0.0.1:443" ]; then
        ok "nginx 只绑 127.0.0.1:443"
    else bad "nginx 的发布端口不是 127.0.0.1:443"; fi
}

check_24() {
    req anon GET /api/health
    if [ "${HTTP_STATUS}" = "200" ]; then ok "带 --cacert → 200"; else bad "带 --cacert → ${HTTP_STATUS}"; fi
    local rc
    curl -sS -o /dev/null --max-time 10 "${BASE}/api/health" 2>/dev/null
    rc=$?
    # 恰好 60（证书校验失败）。7 是连不上，0 是信任链被放宽 —— 都不算
    if [ "${rc}" = "60" ]; then ok "不带 --cacert → curl 退出码 60"; else bad "不带 --cacert → curl 退出码 ${rc}，应为 60"; fi
}

# 免鉴权路径（routes/auth.py 的 EXEMPT_PATHS，刻意在这里手写一份而不是 import：
# 后端多豁免一条，这里就会多出一条 FAIL，那正是要人看的事）：
#   /api/health       —— healthcheck 用，未登录 200
#   /api/auth/login   —— 未登录才需要它，鉴权它等于死锁
#   /api/auth/logout  —— 必须幂等，会话失效时也 200
#   /api/auth/me      —— 回答"我登录了吗"，未登录 200 {authenticated:false}
EXEMPT=" /api/health /api/auth/login /api/auth/logout /api/auth/me "
MIN_AUTH_ROUTES=20

check_27() {
    req anon GET /api/health
    [ "${HTTP_STATUS}" = "200" ] || { bad "对照 /api/health → ${HTTP_STATUS}，请求没打到 api"; return; }
    ok "/api/health 未登录 200（对照）"
    local routes
    # 从运行中的 app 枚举 —— 手写清单会漏掉新 router。首行是 stdin 进去了的证明。
    routes="$(dc exec -T api python - <<'EOF'
print("inner-ok")
from app.main import app

def walk(routes):
    for r in routes:
        if hasattr(r, "original_router"):  # FastAPI 0.14x 的 _IncludedRouter
            yield from walk(r.original_router.routes)
        else:
            yield r

for r in walk(app.routes):
    methods = sorted(getattr(r, "methods", None) or [])
    if methods:
        for m in methods:
            print("HTTP", m, r.path)
    else:
        print("WS", "-", r.path)
EOF
)"
    [ "${routes%%$'\n'*}" = "inner-ok" ] || { bad "容器内枚举脚本没跑起来：${routes:0:200}"; return; }
    local kind method path url n=0
    while read -r kind method path; do
        case "${EXEMPT}" in *" ${path} "*) continue ;; esac
        url="$(printf '%s' "${path}" | sed 's/{[^}]*}/x/g')"
        if [ "${kind}" = "HTTP" ]; then
            n=$((n + 1))
            req anon "${method}" "${url}"
            [ "${HTTP_STATUS}" = "401" ] && [ "$(code_of)" = "unauthenticated" ] ||
                bad "${method} ${path} 未登录 → ${HTTP_STATUS} $(code_of)，应为 401 unauthenticated"
        else
            req anon GET "${url}" --http1.1 --max-time 3 -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
                -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ=='
            if [ "${HTTP_STATUS}" = "401" ]; then ok "WS ${path} 握手被拒（401）"
            else bad "WS ${path} 未登录握手 → ${HTTP_STATUS}，应被拒"; fi
        fi
    done <<<"$(printf '%s\n' "${routes}" | sed 1d)"
    if [ "${n}" -ge "${MIN_AUTH_ROUTES}" ]; then ok "${n} 条需鉴权 HTTP 路由（≥ ${MIN_AUTH_ROUTES}）已逐条打过"
    else bad "只枚举到 ${n} 条需鉴权 HTTP 路由，应 ≥ ${MIN_AUTH_ROUTES} —— 枚举不全"; fi
    [ "${CUR_STATE}" = "PASS" ] && ok "全部 401"
}

check_28() {
    ensure_login
    local auth="${DATA}/auth.json" db="${DATA}/${DB_NAME}" perm hash hits
    perm="$(perm_of "${auth}")"
    if [ "${perm}" = "600" ]; then ok "auth.json 权限 600"; else bad "auth.json 权限是 ${perm:-<不存在>}，应为 600"; fi
    # -f 在前：sqlite3 打开不存在的文件会新建空库，后面 grep 恒 0 就是假绿
    [ -f "${db}" ] || { bad "${db} 不存在"; return; }
    # 在容器里 dump（同检查 4：宿主进程读不到容器那边的 WAL 共享内存）；mode=ro 不会建库
    dc exec -T api python - "${db}" > "${TMP}/dump.sql" 2>&1 <<'EOF' || { bad ".dump 失败"; return; }
import sqlite3, sys
conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
for line in conn.iterdump():
    print(line)
EOF
    if grep -q "CREATE TABLE scans" "${TMP}/dump.sql"; then ok ".dump 含 CREATE TABLE scans（对照：读的是真库）"
    else bad ".dump 里没有 CREATE TABLE scans —— 读的不是控制台的库"; return; fi
    [ -n "${PW}" ] || { bad "口令为空"; return; }
    hits="$(printf '%s\n' "${PW}" | grep -c -F -f - "${TMP}/dump.sql")"
    if [ "${hits}" = "0" ]; then ok "库里 0 处口令明文"; else bad "库里有 ${hits} 行含口令明文"; fi
    hash="$(jq -r '[.. | objects | .hash_b64? // empty | strings] | first // empty' "${auth}" 2>/dev/null)"
    [ -n "${hash}" ] || { bad "从 auth.json 取不到 hash_b64"; return; }
    hits="$(printf '%s\n' "${hash}" | grep -c -F -f - "${TMP}/dump.sql")"
    hash=""
    if [ "${hits}" = "0" ]; then ok "库里 0 处 auth.json 的散列"; else bad "库里有 ${hits} 行含 auth.json 的散列"; fi
    rm -f "${TMP}/dump.sql"
    local out
    out="$(dc exec -T api python - "${db}" <<'EOF' 2>&1
print("inner-ok")
import sys
from pathlib import Path
from app.db import _ENUMERATE_COLUMNS_SQL, Database

path = Path(sys.argv[1])
assert path.is_file(), f"{path} 不存在"  # connect() 会新建空库，那样必然"不抛"
db = Database(path)
db.connect()
db.assert_no_secret_columns()
print("columns", len(list(db._require_conn().execute(_ENUMERATE_COLUMNS_SQL))))
db.close()
EOF
)"
    case "${out}" in
        inner-ok*"columns "[1-9]*) ok "assert_no_secret_columns() 通过，扫过 ${out##*columns } 列" ;;
        *) bad "assert_no_secret_columns() 没通过：${out}" ;;
    esac
}

# =============================================================================
# 执行：按编号跑；21 永远最后，且在它之前把 API 侧的临时改动收拾干净
# =============================================================================
for n in 1 2 3 4 5 22 23 24 27 28; do
    if selected "${n}"; then begin "${n}"; "check_${n}"; finish
    else printf '%s|SKIP\n' "${n}" >> "${RESULTS}"; fi
done
cleanup_api
if selected 21; then begin 21; check_21; finish
else
    printf '\n（21 拆栈：破坏性，缺省不跑；要跑请 TEARDOWN=1 或 ONLY 里写 21）\n'
    printf '21|SKIP\n' >> "${RESULTS}"
fi

printf '\n%-6s %s\n' "检查" "结果"
fails=0
manuals=0
while IFS='|' read -r n state; do
    printf '%-6s %s\n' "${n}" "${state}"
    [ "${state}" = "FAIL" ] && fails=$((fails + 1))
    [ "${state}" = "MANUAL" ] && manuals=$((manuals + 1))
done < "${RESULTS}"
if [ "${manuals}" -gt 0 ]; then printf '\n%s 条待人工核对，不算通过。\n' "${manuals}"; fi
if [ "${fails}" -gt 0 ]; then printf '%s 条 FAIL。\n' "${fails}"; exit 1; fi
exit 0
