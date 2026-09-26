#!/bin/bash
# =============================================================================
# 端到端验收（PLAN.md §端到端验收）—— 宿主侧脚本。**请由你本人在自己的终端里运行。**
#
#   make verify-e2e                      —— 跑本段全部（不含 21）
#   ONLY="22 23 27" ./scripts/verify_e2e.sh   —— 只跑列出的几条
#   TEARDOWN=1 make verify-e2e           —— 额外跑 21（会 `compose down` 拆掉栈，永远最后跑）
#   PAID=1 make verify-e2e               —— 再加第二段：真凭据 + 一次真扫描（5b 6 26 7–12 25）
#   PAID=1 ONLY="5b 6 7" ./scripts/verify_e2e.sh —— 要花钱的那几条不带 PAID=1 直接拒跑
#     可调：TARGET（缺省 http://localhost:13000；连不上就试 http://juice-shop:3000）、
#     BUDGET_A（缺省 1 美元）、EXEC_WAIT（等 exec_command 的秒数，缺省 300）、
#     MODEL（缺省 = 目录里 bedrock 的第一个；账号没开那个模型时 Bedrock 回 403 = key_verify_failed）
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

# ---- 第二段（PAID=1）的状态 -------------------------------------------------------
TARGET="${TARGET:-http://localhost:13000}"
BUDGET_A="${BUDGET_A:-1}"
MODEL="${MODEL:-}"
EXEC_WAIT="${EXEC_WAIT:-300}"
IDLE_SECONDS=90
REAL_HANDLE=""  # 真凭据（bedrock_sigv4）。与检查 4 的假凭据 VAULT_HANDLE 分开：两条都可能被选中
REAL_TRIED=""   # 非空 = 已经问过凭据（失败也不再问）
REAL_META=""    # {provider, auth_shape, strix_llm, names:[两个 secret env 名], params}
REAL_STATUS=""
REAL_BODY=""
SEC1=""         # 两个秘密只在 shell 变量里，经 secrets_stdin 喂给 stdin，绝不进 argv
SEC2=""
SCAN_ID=""
SCAN_TRIED=""   # 非空 = 已经尝试过发起（失败不重试，免得重复花钱）
SCAN_ENDED=""   # 非空 = 已经停过（检查 12 或 cleanup），cleanup 不再强停
LAUNCH_BODY=""
LAUNCH_TS=0
NORM_URL=""
ALLOWLIST_ENTRY="" # 非空 = enforce 模式下临时加过的条目 label，cleanup 要删
WS_INSTALLED=""
REC_PID=""
REC_OUT=""
WS_SCRIPT="/tmp/verify_e2e_ws.py"

# ---- 选择要跑的检查 ----------------------------------------------------------
PAID_CHECKS="5b 6 7 8 9 10 11 12" # 要真凭据、花真钱
if [ -n "${ONLY:-}" ]; then SELECTED="${ONLY}"
elif [ "${PAID:-}" = "1" ]; then SELECTED="${DEFAULT_CHECKS} ${PAID_CHECKS} 26 25"
else SELECTED="${DEFAULT_CHECKS}"; fi
if [ "${TEARDOWN:-}" = "1" ]; then SELECTED="${SELECTED} 21"; fi
selected() { case " ${SELECTED} " in *" $1 "*) return 0 ;; esac; return 1; }
if [ "${PAID:-}" != "1" ]; then
    for n in ${PAID_CHECKS}; do
        selected "${n}" && { rm -rf "${TMP}"; die "检查 ${n} 要真凭据、会花钱：请加 PAID=1"; }
    done
fi

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

# ---- 第二段 helper：真凭据、全盘卫生、一次真扫描、录帧 ------------------------------
# 两个真秘密，一行一个。只经管道 / 进程替换交给 grep -f 或 python 的 stdin（printf 是内建，
# 不产生进程）。任一为空时 grep -f 会匹配每一行 → 命中数暴涨 → FAIL，不会假绿。
secrets_stdin() { printf '%s\n%s\n' "${SEC1}" "${SEC2}"; }
sid_of_jar() { awk '$6 == "sid" {print $7}' "${JAR}"; }

ensure_real_handle() {
    [ -n "${REAL_HANDLE}" ] && return 0
    [ -z "${REAL_TRIED}" ] || return 1
    REAL_TRIED=1
    ensure_login
    [ -t 0 ] || die "要真凭据的检查必须在交互终端里跑"
    api GET /api/providers
    REAL_META="$(jq -c --arg m "${MODEL}" '.providers[] | select(.provider == "bedrock") as $p
        | $p.shapes[] | select(.auth_shape == "bedrock_sigv4")
        | {provider: $p.provider, auth_shape, strix_llm: (if $m == "" then $p.models[0] else $m end),
           names: .secret_keys, param_keys}' <<<"${BODY}")"
    [ -n "${REAL_META}" ] || { bad "GET /api/providers 里没有 bedrock/bedrock_sigv4"; return 1; }
    [ "$(jq '.names | length' <<<"${REAL_META}")" = "2" ] ||
        { bad "bedrock_sigv4 的 secret_keys 不是 2 个：${REAL_META}"; return 1; }
    local region
    printf '\n接下来要一份真的 Bedrock 凭据（不回显，不进 argv、不落盘，跑完即从 vault 删除）。\n'
    read -r -s -p "请粘贴 $(jq -r '.names[0]' <<<"${REAL_META}"): " SEC1 || die "读不到输入"
    printf '\n'
    read -r -s -p "请粘贴 $(jq -r '.names[1]' <<<"${REAL_META}"): " SEC2 || die "读不到输入"
    printf '\n'
    read -r -p "区域（直接回车 = us-east-1）: " region || die "读不到输入"
    [ -n "${SEC1}" ] && [ -n "${SEC2}" ] || die "凭据不能为空"
    REAL_META="$(jq -c --arg r "${region:-us-east-1}" \
        '.params = (reduce .param_keys[] as $k ({}; .[$k] = $r)) | del(.param_keys)' <<<"${REAL_META}")"
    api POST /api/keys --data-binary @- < <(printf '%s\n%s\n%s' "${REAL_META}" "${SEC1}" "${SEC2}" | python3 -c '
import json, sys
meta, first, second = sys.stdin.read().split("\n", 2)
body = json.loads(meta)
names = body.pop("names")
body["secrets"] = {names[0]: first, names[1]: second}
body["verify"] = True
sys.stdout.write(json.dumps(body))
')
    REAL_STATUS="${HTTP_STATUS}"
    REAL_BODY="${BODY}"
    REAL_HANDLE="$(jq -r '.vault_handle // empty' <<<"${BODY}" 2>/dev/null)"
    [ "${HTTP_STATUS}" = "201" ] && [ -n "${REAL_HANDLE}" ] || {
        bad "登记真凭据失败：${HTTP_STATUS} $(code_of)（模型 $(jq -r .strix_llm <<<"${REAL_META}")；key_verify_failed 多半是账号没开这个模型 → 换 MODEL=）"
        return 1
    }
}

# face NAME HITS CTRL —— 全盘卫生的一个面。对照 0 命中 = 这一面根本没扫到东西 → FAIL。
SWEEP_HITS=0
SWEEP_BAD=""
face() {
    local name="$1" hits="${2:-}" ctrl="${3:-}"
    case "${hits}:${ctrl}" in
        *[!0-9:]* | :* | *:) bad "${name}：计数取不到（hits=${hits} ctrl=${ctrl}）"; SWEEP_BAD="${SWEEP_BAD} ${name}"; return ;;
    esac
    SWEEP_HITS=$((SWEEP_HITS + hits))
    if [ "${ctrl}" -eq 0 ]; then bad "${name}：阳性对照 0 命中 —— 这一面没扫到东西"; SWEEP_BAD="${SWEEP_BAD} ${name}"
    elif [ "${hits}" -eq 0 ]; then ok "${name}：0 处明文（对照命中 ${ctrl}）"
    else bad "${name}：${hits} 处明文"; SWEEP_BAD="${SWEEP_BAD} ${name}"; fi
}

# sweep_all TOKEN [沙箱容器 id…] → SWEEP_HITS（五面命中总数）/ SWEEP_BAD（失败的面）。
# TOKEN 是 DATA 树与 compose 日志两面的阳性对照（扫描中给 scan_id，扫描前给审计事件名）。
sweep_all() {
    local token="$1" db="${DATA}/${DB_NAME}" ctrl_tok="CONSOLE_DATA_DIR"
    shift
    [ "$#" -gt 0 ] && ctrl_tok="strix-run-id"  # 沙箱的 label；api 容器上没有
    SWEEP_HITS=0
    SWEEP_BAD=""
    face "DATA 全树" "$(grep -rl -a -F -f <(secrets_stdin) "${DATA}" 2>/dev/null | grep -c .)" \
        "$(grep -rl -a -F -e "${token}" "${DATA}" 2>/dev/null | grep -c .)"
    # 在容器里 grep（同检查 4：宿主跨 VM 边界读正开着的 WAL 库不可靠）
    face "console.sqlite + wal" \
        "$(secrets_stdin | dc exec -T api sh -c 'grep -h -c -a -F -f - -- "$1" "$1-wal" 2>/dev/null' _ "${db}" | awk '{s += $1} END {print s + 0}')" \
        "$(dc exec -T api sh -c 'grep -h -c -a -F -e "CREATE TABLE scans" -- "$1" "$1-wal" 2>/dev/null' _ "${db}" | awk '{s += $1} END {print s + 0}')"
    face "compose logs api web nginx" "$(dc logs --no-color api web nginx 2>&1 | grep -c -a -F -f <(secrets_stdin))" \
        "$(dc logs --no-color api web nginx 2>&1 | grep -c -a -F -e "${token}")"
    local ids
    ids="$(dc ps -q api) $*"
    # shellcheck disable=SC2086 # ids 是空格分隔的容器 id，要按词拆
    face "docker inspect api + 沙箱" "$(docker inspect ${ids} 2>/dev/null | grep -c -F -f <(secrets_stdin))" \
        "$(docker inspect ${ids} 2>/dev/null | grep -c -F -e "${ctrl_tok}")"
    face "仓库目录" \
        "$(grep -rl -a -F -f <(secrets_stdin) . --exclude-dir=.git --exclude-dir=node_modules 2>/dev/null | grep -c .)" \
        "$(grep -rl -a -F -e "${PROJECT}" . --exclude-dir=.git --exclude-dir=node_modules 2>/dev/null | grep -c .)"
    printf '  五面命中合计 %s%s\n' "${SWEEP_HITS}" "${SWEEP_BAD:+；失败的面：${SWEEP_BAD}}"
}

# 录帧 / 空闲探活脚本，跑在 api 容器里（宿主 python 3.9 没有 websockets，容器里有 15.x）。
# 用法：record URI CERT SECONDS OUT ｜ idle URI CERT SECONDS；会话 sid 从 stdin 读，不进 argv。
install_ws_script() {
    [ -n "${WS_INSTALLED}" ] && return 0
    dc exec -T api sh -c "cat > ${WS_SCRIPT} && grep -q ^MAX_FRAMES ${WS_SCRIPT} && echo installed" > "${TMP}/ws_install.out" 2>&1 <<'EOF'
import asyncio, json, ssl, sys
from websockets.asyncio.client import connect

MAX_FRAMES = 100000  # 收货 mutation M2 把它截到 3

print("inner-ok", flush=True)
mode, uri, cert, seconds = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
sid = sys.stdin.readline().strip()
ctx = ssl.create_default_context(cafile=cert)
ctx.check_hostname = False  # SAN 只有 localhost，这里按服务名连；信任链由验收 24 覆盖


def dial():
    # ping_interval=None：默认每 20 秒自动 ping，pong 会一直重置 nginx 的读超时，检查 25 就等于没测
    return connect(uri, ssl=ctx, origin="https://nginx", additional_headers={"Cookie": f"sid={sid}"},
                   ping_interval=None, open_timeout=10, max_size=None)


async def record(out):
    async with dial() as ws:
        await ws.send(json.dumps({"type": "hello"}))
        with open(out, "a") as f:
            for _ in range(MAX_FRAMES):
                msg = await ws.recv()
                f.write(msg.rstrip("\n") + "\n")
                f.flush()
                if json.loads(msg).get("type") == "done":
                    print("done", flush=True)
                    return
            print("max-frames", flush=True)


async def idle():
    async with dial() as ws:
        await asyncio.wait_for(ws.recv(), 10)
        print("snapshot", flush=True)
        await asyncio.sleep(seconds)
        extra = 0
        while True:
            try:
                await asyncio.wait_for(ws.recv(), 0.2)
            except TimeoutError:
                break
            extra += 1
        pong = await ws.ping()
        await asyncio.wait_for(pong, 10)
        print("alive", extra, flush=True)


try:
    if mode == "record":
        asyncio.run(asyncio.wait_for(record(sys.argv[5]), seconds))
    else:
        asyncio.run(idle())
except TimeoutError:
    print("timeout", flush=True)
EOF
    [ "$(cat "${TMP}/ws_install.out")" = "installed" ] ||
        { bad "录帧脚本没写进容器：$(cat "${TMP}/ws_install.out")"; return 1; }
    WS_INSTALLED=1
}

start_recorder() {
    install_ws_script || return 1
    REC_OUT="/tmp/verify_e2e_frames_${SCAN_ID}.jsonl"
    dc exec -T api python "${WS_SCRIPT}" record "wss://nginx/ws/scans/${SCAN_ID}" "${CERT}" 1800 "${REC_OUT}" \
        < <(sid_of_jar) > "${TMP}/rec.log" 2>&1 &
    REC_PID=$!
}

# frames_jq FILTER —— 对录到的帧（数组）跑 jq；写到一半的末行由 fromjson? 丢掉
frames_jq() {
    dc exec -T api cat "${REC_OUT}" > "${TMP}/frames.jsonl" 2>/dev/null
    jq -n -R "[inputs | fromjson? // empty] | $1" "${TMP}/frames.jsonl"
}
# wait_frame PRED TIMEOUT —— 每秒轮询，任一帧满足 jq 谓词 PRED 即返回 0，超时返回 1
wait_frame() {
    local deadline=$(($(date +%s) + $2))
    while :; do
        frames_jq "any(.[]; $1)" 2>/dev/null | grep -q '^true$' && return 0
        [ "$(date +%s)" -ge "${deadline}" ] && return 1
        sleep 1
    done
}

# 发起那一次真扫描（6 断言它的响应；7–12 都挂在它上面）。失败不重试。
ensure_scan() {
    [ -n "${SCAN_ID}" ] && return 0
    [ -z "${SCAN_TRIED}" ] || return 1
    SCAN_TRIED=1
    ensure_real_handle || return 1
    [ "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:13000/ 2>/dev/null)" = "200" ] ||
        die "靶场 http://127.0.0.1:13000/ 不是 200 —— m0-juice-shop 没在跑？"
    api GET /api/scans
    [ "${HTTP_STATUS}" = "200" ] || { bad "GET /api/scans → ${HTTP_STATUS} $(code_of)"; return 1; }
    local busy
    busy="$(jq -r '[.scans[] | select(.status == "starting" or .status == "running") | .id] | join(" ")' <<<"${BODY}")"
    [ -z "${busy}" ] || { bad "已有扫描在跑（${busy}），并发上限 1 —— 等它结束再跑"; return 1; }
    api GET /api/scan-templates
    local tpl
    tpl="$(jq -c '[.templates[] | select(.scan_mode == "quick")] | first // empty' <<<"${BODY}")"
    [ -n "${tpl}" ] || { bad "没有 scan_mode=quick 的模板：${BODY:0:200}"; return 1; }
    # 先不放行地验一次，按 required_opt_in 补上 overrides 再验；typed_confirmation 与
    # resolved_ips_seen 都从第二次的响应导出（自己编会得 dns_changed）
    jq -n --arg t "${TARGET}" '{raw: [$t], overrides: {allow_loopback: false, allow_private: false}}' > "${TMP}/v1.json"
    api POST /api/targets/validate --data-binary @- < "${TMP}/v1.json"
    local ov target
    ov="$(jq -c '.targets[0].required_opt_in // [] | {allow_loopback: (index("loopback") != null), allow_private: (index("private") != null)}' <<<"${BODY}")"
    jq -n --arg t "${TARGET}" --argjson o "${ov}" '{raw: [$t], overrides: $o}' > "${TMP}/v2.json"
    api POST /api/targets/validate --data-binary @- < "${TMP}/v2.json"
    target="$(jq -c '.targets[0]' <<<"${BODY}")"
    [ "$(jq -r .ok <<<"${target}")" = "true" ] || { bad "目标 ${TARGET} 放行后仍不通过：${target}"; return 1; }
    NORM_URL="$(jq -r .normalized.url <<<"${target}")"
    api GET /api/allowlist
    if [ "$(jq -r .effective_mode <<<"${BODY}")" = "enforce" ]; then
        jq -n --arg h "$(jq -r .normalized.host <<<"${target}")" --argjson o "${ov}" \
            '{label: "verify-e2e", owner: "verify-e2e", authorization_ref: "verify-e2e", hosts: [$h]} + $o' > "${TMP}/entry.json"
        api POST /api/allowlist/entries --data-binary @- < "${TMP}/entry.json"
        case "${HTTP_STATUS}" in 2??) ALLOWLIST_ENTRY="verify-e2e" ;;
            *) bad "enforce 模式下加临时白名单条目失败：${HTTP_STATUS} $(code_of)"; return 1 ;; esac
    fi
    jq -n --arg h "${REAL_HANDLE}" --arg raw "${TARGET}" --arg b "${BUDGET_A}" \
        --argjson tpl "${tpl}" --argjson t "${target}" --argjson o "${ov}" '{
        vault_handle: $h, template_id: $tpl.template_id, targets: [$raw], overrides: $o,
        max_budget_usd: ($b | tonumber), max_turns: $tpl.default_max_turns, credentials: [],
        authorization: {operator_name: "verify-e2e", authorization_ref: "verify-e2e",
            typed_confirmation: $t.normalized.host,
            affirmed: ["owns_or_authorized", "not_third_party_production", "understands_real_attacks"],
            multi_target_affirmed: false, resolved_ips_seen: {($t.normalized.host): [$t.resolved_ips[].address]}}}' > "${TMP}/scan_real.json"
    api POST /api/scans --data-binary @- < "${TMP}/scan_real.json"
    [ "${HTTP_STATUS}" = "202" ] || { bad "发起扫描：期望 202，实得 ${HTTP_STATUS} $(code_of)"; return 1; }
    LAUNCH_BODY="${BODY}"
    LAUNCH_TS="$(date +%s)"
    SCAN_ID="$(jq -r '.scan_id // empty' <<<"${BODY}")"
    [ -n "${SCAN_ID}" ] || { bad "202 但响应里没有 scan_id"; return 1; }
    printf '  已发起扫描 %s（目标 %s，预算 %s 美元）\n' "${SCAN_ID}" "${TARGET}" "${BUDGET_A}"
    start_recorder
}

cleanup_api() {
    [ -n "${LOGGED_IN}" ] && [ -z "${STACK_DOWN}" ] || return 0
    [ -n "${ALLOWLIST_CREATED}" ] && remove_temp_allowlist
    if [ -n "${SCAN_ID}" ] && [ -z "${SCAN_ENDED}" ]; then
        jq -n '{mode: "force"}' > "${TMP}/stop_force.json"
        api POST "/api/scans/${SCAN_ID}/stop" --data-binary @- < "${TMP}/stop_force.json"
        case "${HTTP_STATUS}" in
            202) printf '⚠ 扫描 %s 没走到检查 12 的优雅停止，已强停\n' "${SCAN_ID}" >&2 ;;
            # 已结束的扫描回 404（scans.py stop_scan：未知与已结束同一回答），例如预算先用完
            404) printf '扫描 %s 已自行结束（stop → 404），无需强停\n' "${SCAN_ID}" >&2 ;;
            *) printf '⚠ 强停扫描 %s 失败：%s —— 请到控制台手工停止\n' "${SCAN_ID}" "${HTTP_STATUS}" >&2
               printf 'cleanup|FAIL\n' >> "${RESULTS}" ;;
        esac
        SCAN_ENDED=1
    fi
    [ -n "${REC_PID}" ] && kill "${REC_PID}" 2>/dev/null
    REC_PID=""
    if [ -n "${WS_INSTALLED}" ]; then
        dc exec -T api rm -f "${WS_SCRIPT}" ${REC_OUT:+"${REC_OUT}"}
        WS_INSTALLED=""
    fi
    if [ -n "${ALLOWLIST_ENTRY}" ]; then
        api DELETE "/api/allowlist/entries/${ALLOWLIST_ENTRY}"
        [ "${HTTP_STATUS}" = "200" ] || printf '⚠ 临时白名单条目 %s 没删掉（%s），请手工删\n' "${ALLOWLIST_ENTRY}" "${HTTP_STATUS}" >&2
        ALLOWLIST_ENTRY=""
    fi
    if [ -n "${REAL_HANDLE}" ]; then
        api DELETE "/api/keys/${REAL_HANDLE}"
        if [ "${HTTP_STATUS}" != "204" ]; then
            printf '⚠ 删除真凭据失败：%s —— 请到控制台手工删除\n' "${HTTP_STATUS}" >&2
            printf 'cleanup|FAIL\n' >> "${RESULTS}"
        fi
        REAL_HANDLE=""
    fi
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
    SEC1=""
    SEC2=""
    [ -n "${REC_PID}" ] && kill "${REC_PID}" 2>/dev/null
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
# 第二段（PAID=1）：真凭据 + 一次真扫描的前半生。顺序即依赖：5b → 6 发起 → 26/7–11 扫描中 → 12 停止
# =============================================================================
check_5b() {
    ensure_real_handle || { bad "没有真凭据 handle，本条无从断言"; return; }
    if [ "${REAL_STATUS}" = "201" ]; then ok "POST /api/keys verify:true → 201"; else bad "POST /api/keys → ${REAL_STATUS}"; fi
    if [ "$(jq -r .verified <<<"${REAL_BODY}")" = "true" ]; then ok "verified == true"; else bad "verified = $(jq -c .verified <<<"${REAL_BODY}")"; fi
    # 阳性对照：labels 的键恰好是那两个 env 名（证明下面扫的是真 labels）
    if jq -e --argjson n "$(jq -c .names <<<"${REAL_META}")" '(.labels | keys) == ($n | sort)' >/dev/null <<<"${REAL_BODY}"; then
        ok "labels 的键恰好是 $(jq -r '.names | join(" / ")' <<<"${REAL_META}")"
    else bad "labels 的键不对：$(jq -c '.labels | keys' <<<"${REAL_BODY}")"; fi
    local hits
    hits="$(jq -r '.labels[]' <<<"${REAL_BODY}" | grep -c -F -f <(secrets_stdin))"
    if [ "${hits}" = "0" ]; then ok "labels 的值里 0 处明文"; else bad "labels 的值里 ${hits} 处明文"; fi
    api GET "/api/keys/${REAL_HANDLE}"
    [ "${HTTP_STATUS}" = "200" ] || { bad "GET /api/keys/{h} → ${HTTP_STATUS} $(code_of)"; return; }
    if grep -q -F -e "$(jq -r '.names[0]' <<<"${REAL_META}")" <<<"${BODY}"; then ok "GET /api/keys/{h} 含 env 名（对照）"
    else bad "GET /api/keys/{h} 里没有 env 名 —— 对照失败"; fi
    hits="$(printf '%s' "${BODY}" | grep -c -F -f <(secrets_stdin))"
    if [ "${hits}" = "0" ]; then ok "GET /api/keys/{h} 0 处明文"; else bad "GET /api/keys/{h} 有 ${hits} 行明文"; fi
    sweep_all "key.registered"
}

# argv_ok LABEL BUDGET JQ_EXPR —— 对 argv_preview（jq 变量 a）断言；adj(k; v) = k 后面紧跟 v
argv_ok() {
    if jq -e --arg t "${TARGET}" --arg u "${NORM_URL}" --arg b "$2" '.argv_preview as $a
        | def adj($k; $v): any(range(0; ($a | length) - 1); $a[.] == $k and $a[. + 1] == $v);
        '"$3" >/dev/null 2>&1 <<<"${LAUNCH_BODY}"; then ok "$1"; else bad "$1 不成立"; fi
}

check_6() {
    ensure_scan || { bad "扫描没发起成功"; return; }
    ok "POST /api/scans → 202（${SCAN_ID}）"
    printf '  argv_preview: %s\n' "$(jq -c .argv_preview <<<"${LAUNCH_BODY}")"
    # 与 scan_launcher._format_usd 同一规则：保留 4 位再去掉尾 0 与小数点（1 → "1"，1.5 → "1.5"）
    local want hits
    want="$(awk -v v="${BUDGET_A}" 'BEGIN { s = sprintf("%.4f", v); sub(/0+$/, "", s); sub(/\.$/, "", s); print s }')"
    argv_ok "-n 存在" "${want}" 'any($a[]; . == "-n")'
    argv_ok "-t 后紧跟目标" "${want}" 'adj("-t"; $t) or adj("-t"; $u)'
    argv_ok "-m quick 相邻" "${want}" 'adj("-m"; "quick")'
    argv_ok "--max-budget-usd ${want} 相邻" "${want}" 'adj("--max-budget-usd"; $b)'
    hits="$(jq -r '.argv_preview[]' <<<"${LAUNCH_BODY}" | grep -c -F -e --max-budget-usd)"
    [ "${hits}" -ge 1 ] 2>/dev/null || bad "argv_preview 里扫不到 --max-budget-usd —— 对照失败"
    hits="$(jq -r '.argv_preview[]' <<<"${LAUNCH_BODY}" | grep -c -F -f <(secrets_stdin))"
    if [ "${hits}" = "0" ]; then ok "argv_preview 0 处明文"; else bad "argv_preview 有 ${hits} 处明文"; fi
}

# frame_ok LABEL PRED WINDOW —— 发起后 WINDOW 秒内要有一帧满足 PRED
frame_ok() {
    if wait_frame "$2" $((LAUNCH_TS + $3 - $(date +%s))); then ok "$1"; else bad "$1：发起后 $3 秒内没等到"; fi
}

check_7() {
    ensure_scan || { bad "没有扫描可看"; return; }
    if ! wait_frame 'true' $((LAUNCH_TS + 90 - $(date +%s))); then
        bad "录帧文件是空的（录帧器输出：$(tr '\n' ' ' < "${TMP}/rec.log")）"; return
    fi
    frame_ok "agents 里有 Root Agent（parent_id=null）" \
        '.type == "agents" and any(.payload.agents[]; .parent_id == null and .name == "Root Agent")' 90
    frame_ok "event.add kind=chat" '.type == "event.add" and .payload.kind == "chat"' 90
    frame_ok "event.add kind=tool" '.type == "event.add" and .payload.kind == "tool"' 90
    frame_ok "log 帧" '.type == "log"' 90
    frame_ok "summary 帧" '.type == "summary"' 90
    frame_ok "summary.cost_usd > 0" '.type == "summary" and (.payload.cost_usd // 0) > 0' 90
    local costs n
    costs="$(frames_jq '[.[] | select(.type == "summary") | .payload.cost_usd | numbers]')"
    n="$(jq length <<<"${costs}")"
    if [ "${n:-0}" -ge 2 ]; then
        if jq -e '. as $c | all(range(1; length); $c[.] >= $c[. - 1]) and $c[-1] > $c[0]' >/dev/null <<<"${costs}"; then
            ok "cost_usd 单调不减且末值 > 首值（${n} 帧：$(jq -c '[first, last]' <<<"${costs}")）"
        else bad "cost_usd 不单调或没涨：${costs}"; fi
    elif [ "${n:-0}" = "1" ]; then manual "窗口内只拿到 1 帧非 null 的 cost_usd，判不了单调"
    else bad "没有任何非 null 的 cost_usd"; fi
    frame_ok "event.add 里出现 exec_command" \
        '.type == "event.add" and (.payload.data | objects | .tool_name) == "exec_command"' "${EXEC_WAIT}"
    local url code magic
    url="$(grep -o -E "/api/scans/${SCAN_ID}/media/[0-9a-f]{64}\.png" "${TMP}/frames.jsonl" | head -1)"
    if [ -z "${url}" ]; then
        manual "窗口内没出现截图（取决于 agent 选了哪些工具）—— 截图落地请人看"
        return
    fi
    code="$(curl -sS --cacert "${CERT}" -b "${JAR}" -o "${TMP}/shot.png" -w '%{http_code}' --max-time 30 "${BASE}${url}" 2>/dev/null)"
    magic="$(head -c 8 "${TMP}/shot.png" 2>/dev/null | od -An -tx1 | tr -d ' \n')"
    if [ "${code}" = "200" ] && [ "${magic}" = "89504e470d0a1a0a" ]; then ok "截图 ${url##*/} → 200 且是 PNG"
    else bad "截图 ${url} → ${code}，魔数 ${magic:-<空>}"; fi
}

check_26() {
    ensure_login
    local id="${SCAN_ID}" w t
    # 不许退回去测库里的旧扫描：已结束的流很短、立刻收尾，nginx 缓冲了照样 < 2s —— 假绿
    [ -n "${id}" ] || { bad "没有正在跑的扫描：26 要与 6 一起在 PAID=1 下跑"; return; }
    # 超时退出码 28 是预期的（流不会自己结束）；-w 照样写出
    w="$(curl -sS -N --cacert "${CERT}" -b "${JAR}" --max-time 3 -H 'Accept: text/event-stream' \
        -o "${TMP}/sse.txt" -w '%{http_code} %{time_starttransfer}' "${BASE}/api/scans/${id}/stream" 2>/dev/null)"
    t="${w##* }"
    if [ "${w%% *}" = "200" ]; then ok "GET /api/scans/${id}/stream → 200"; else bad "stream → ${w%% *}"; fi
    if awk -v t="${t}" 'BEGIN { exit !(t > 0 && t < 2) }'; then ok "首字节 ${t}s < 2s"; else bad "首字节 ${t:-<无>}s，应 < 2s"; fi
    # 首行是路由自己发的 SSE 注释（stream.py:377）：只有十几字节，nginx 若缓冲就出不来
    if [ "$(head -n 1 "${TMP}/sse.txt" 2>/dev/null)" = ": connected" ]; then ok "3 秒内收到首行 : connected（对照：是本路由的真 SSE）"
    else bad "首行不是 : connected：$(head -c 200 "${TMP}/sse.txt" 2>/dev/null)"; fi
}

check_8() {
    ensure_scan || { bad "没有扫描"; return; }
    local out
    # 镜像里没有 ps/pgrep/strings：直接读 /proc/*/cmdline（\0 分隔）。秘密从 stdin 进
    out="$(secrets_stdin | dc exec -T api python -c '
import os, sys
secrets = [line for line in sys.stdin.read().split("\n") if line]
print("inner-ok", end=" ")
procs = hits = budget = 0
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    try:
        cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        continue
    procs += 1
    hits += any(s in cmd for s in secrets)
    budget += "--max-budget-usd" in cmd
print(len(secrets), procs, hits, budget)
' 2>&1)"
    local tag nsec procs hits budget
    read -r tag nsec procs hits budget <<<"${out}"
    [ "${tag}" = "inner-ok" ] && [ "${nsec}" = "2" ] || { bad "容器内脚本没跑起来或没收到两个秘密：${out:0:200}"; return; }
    if [ "${budget}" -ge 1 ] 2>/dev/null; then ok "${procs} 个进程里 ${budget} 条 cmdline 含 --max-budget-usd（对照：strix 子进程还活着）"
    else bad "没有一条 cmdline 含 --max-budget-usd —— 对照失败（子进程已退出？）"; fi
    if [ "${hits}" = "0" ]; then ok "所有 cmdline 0 处明文"; else bad "${hits} 个进程的 cmdline 含明文"; fi
}

# sandbox_ids —— 本次扫描的沙箱容器。**必须按 run-id 精确过滤**：只按 type label 会连带
# 命中 m0-juice-shop 靶场（收货 mutation M3 就是删掉这一半）
sandbox_ids() { docker ps -q --filter "label=strix-run-id=${SCAN_ID}"; }

check_9() {
    ensure_scan || { bad "没有扫描"; return; }
    local ids="" i
    for i in $(seq 1 60); do
        ids="$(sandbox_ids)"
        [ -n "${ids}" ] && break
        sleep 1
    done
    [ -n "${ids}" ] || { bad "按 strix-run-id=${SCAN_ID} 取不到沙箱容器（等了 60 秒）"; return; }
    # shellcheck disable=SC2086
    docker inspect ${ids} > "${TMP}/sandbox.json" 2>/dev/null || { bad "docker inspect 失败"; return; }
    ok "沙箱容器 $(jq length "${TMP}/sandbox.json") 个"
    local pred
    for pred in \
        'CapAdd 含 NET_ADMIN 与 NET_RAW|(.HostConfig.CapAdd // []) as $c | any($c[]; . == "NET_ADMIN" or . == "CAP_NET_ADMIN") and any($c[]; . == "NET_RAW" or . == "CAP_NET_RAW")' \
        'Networks 含 strix_sandbox|.NetworkSettings.Networks | has("strix_sandbox")' \
        'NetworkSettings.Ports 没有任何宿主绑定|[(.NetworkSettings.Ports // {})[] | select(. != null)] | length == 0' \
        'HostConfig.PortBindings 为空|(.HostConfig.PortBindings // {}) | length == 0'; do
        if jq -e "all(.[]; ${pred#*|})" >/dev/null "${TMP}/sandbox.json"; then ok "${pred%%|*}"
        else bad "${pred%%|*} 不成立"; fi
    done
    if grep -q -F -e strix-run-id "${TMP}/sandbox.json"; then ok "inspect 含 strix-run-id label（对照）"; else bad "inspect 里没有 strix-run-id —— 对照失败"; fi
    local hits
    hits="$(grep -c -F -f <(secrets_stdin) "${TMP}/sandbox.json")"
    if [ "${hits}" = "0" ]; then ok "inspect 0 处明文"; else bad "inspect 有 ${hits} 行明文"; fi
    rm -f "${TMP}/sandbox.json"
}

# run_dir → RUN_DIR。自动名不可预测，用 glob；0 个或多于 1 个都是 FAIL
run_dir() {
    local d n=0
    RUN_DIR=""
    for d in "${DATA}/scans/${SCAN_ID}/strix_runs/"*/; do
        [ -d "${d}" ] && { n=$((n + 1)); RUN_DIR="${d%/}"; }
    done
    [ "${n}" = "1" ] || { bad "run 目录应恰好 1 个，实得 ${n}"; RUN_DIR=""; return 1; }
}

check_10() {
    ensure_scan || { bad "没有扫描"; return; }
    run_dir || return
    local line addr
    line="$(grep -a 'Caido host endpoint resolved' "${RUN_DIR}/strix.log" 2>/dev/null | tail -1)"
    [ -n "${line}" ] || { bad "strix.log 里没有 Caido host endpoint resolved"; return; }
    addr="$(printf '%s' "${line}" | grep -o -E 'https?://[0-9.]+' | tail -1)"
    addr="${addr#*://}"
    case "${addr}" in
        172.* | 10.* | 192.168.*) ok "Caido 解析到容器 IP ${addr}" ;;
        *) bad "Caido 解析到 ${addr:-<取不到>}，应为容器 IP：${line}" ;;
    esac
}

check_11() {
    ensure_scan || { bad "没有扫描"; return; }
    local ids
    ids="$(sandbox_ids)"
    [ -n "${ids}" ] || bad "取不到沙箱容器 —— inspect 面只剩 api 一半"
    # shellcheck disable=SC2086
    sweep_all "${SCAN_ID}" ${ids}
    # containment：bedrock_sigv4 下 persist_current() 不写 AWS_* → /run/strix 下应 0 命中；
    # 对照 = cli-config.json 比预置的 {"env":{}}（11 字节）变大了且含 STRIX_LLM
    local out tag nsec hits size llm
    out="$(secrets_stdin | dc exec -T api python -c '
import os, sys
secrets = [line for line in sys.stdin.read().split("\n") if line]
hits = 0
for root, _, files in os.walk("/run/strix"):
    for name in files:
        try:
            data = open(os.path.join(root, name), "rb").read()
        except OSError:
            continue
        hits += any(s.encode() in data for s in secrets)
cfg = f"/run/strix/scan-{sys.argv[1]}/home/.strix/cli-config.json"
try:
    body = open(cfg, "rb").read()
except OSError:
    body = b""
print("inner-ok", len(secrets), hits, len(body), int(b"STRIX_LLM" in body))
' "${SCAN_ID}" 2>&1)"
    read -r tag nsec hits size llm <<<"${out}"
    [ "${tag}" = "inner-ok" ] && [ "${nsec}" = "2" ] || { bad "容器内脚本没跑起来：${out:0:200}"; return; }
    if [ "${size}" -gt 11 ] && [ "${llm}" = "1" ]; then ok "cli-config.json ${size} 字节且含 STRIX_LLM（对照：persist_current 跑过）"
    else bad "cli-config.json ${size} 字节、含 STRIX_LLM=${llm} —— 对照失败"; fi
    if [ "${hits}" = "0" ]; then ok "/run/strix 下 0 个文件含明文"; else bad "/run/strix 下 ${hits} 个文件含明文"; fi
}

tmpfs_state() { dc exec -T api sh -c 'if [ -e "$1" ]; then echo present; else echo gone; fi' _ "/run/strix/scan-${SCAN_ID}"; }

check_12() {
    ensure_scan || { bad "没有扫描"; return; }
    # 对照：停之前沙箱与 tmpfs 目录都在，否则"已消失"证明不了回收
    if [ -n "$(sandbox_ids)" ]; then ok "停止前沙箱容器在（对照）"; else bad "停止前就没有沙箱容器 —— 回收断言无从证明"; fi
    if [ "$(tmpfs_state)" = "present" ]; then ok "停止前 /run/strix/scan-${SCAN_ID} 在（对照）"; else bad "停止前 tmpfs 目录就不在"; fi
    jq -n '{mode: "graceful"}' > "${TMP}/stop.json"
    api POST "/api/scans/${SCAN_ID}/stop" --data-binary @- < "${TMP}/stop.json"
    [ "${HTTP_STATUS}" = "202" ] || { bad "graceful stop → ${HTTP_STATUS} $(code_of)"; return; }
    SCAN_ENDED=1
    ok "graceful stop → 202"
    if wait_frame '.type == "done"' 45; then ok "45 秒内收到 done 帧"; else bad "45 秒内没收到 done 帧"; fi
    local i scan
    for i in $(seq 1 15); do
        api GET "/api/scans/${SCAN_ID}"
        scan="$(jq -c '.scan' <<<"${BODY}" 2>/dev/null)"
        case "$(jq -r .status <<<"${scan}" 2>/dev/null)" in starting | running) sleep 1 ;; *) break ;; esac
    done
    if [ "$(jq -r .status <<<"${scan}")" = "stopped" ]; then ok "scan.status == stopped"; else bad "scan.status = $(jq -r .status <<<"${scan}")"; fi
    if [ "$(jq -r .error_code <<<"${scan}")" = "stopped_by_operator" ]; then ok "error_code == stopped_by_operator"
    else bad "error_code = $(jq -r .error_code <<<"${scan}")"; fi
    case "$(jq -r .exit_meaning <<<"${scan}")" in
        vulnerabilities_found | no_vulnerabilities_found | failed) ok "exit_meaning = $(jq -r .exit_meaning <<<"${scan}")" ;;
        *) bad "exit_meaning = $(jq -r .exit_meaning <<<"${scan}")，不在合法值里" ;;
    esac
    if run_dir; then
        case "$(jq -r .status "${RUN_DIR}/run.json" 2>/dev/null)" in
            stopped | interrupted) ok "run.json status = $(jq -r .status "${RUN_DIR}/run.json")" ;;
            *) bad "run.json status = $(jq -r .status "${RUN_DIR}/run.json" 2>/dev/null)" ;;
        esac
    fi
    local left=""
    for i in $(seq 1 30); do
        left="$(docker ps -a -q --filter "label=strix-run-id=${SCAN_ID}")"
        [ -z "${left}" ] && [ "$(tmpfs_state)" = "gone" ] && break
        sleep 1
    done
    if [ -z "${left}" ]; then ok "沙箱容器已回收"; else bad "沙箱容器残留：${left}"; fi
    if [ "$(tmpfs_state)" = "gone" ]; then ok "/run/strix/scan-${SCAN_ID} 已删"; else bad "/run/strix/scan-${SCAN_ID} 仍在"; fi
    api GET /api/system/status
    if [ "$(jq -r '.orphan_sandboxes.count' <<<"${BODY}")" = "0" ]; then ok "orphan_sandboxes.count == 0"
    else bad "orphan_sandboxes = $(jq -c .orphan_sandboxes <<<"${BODY}")"; fi
}

check_25() {
    ensure_login
    install_ws_script || return
    local out extra
    printf '  经 nginx 连 wss /ws/system，空闲 %s 秒…\n' "${IDLE_SECONDS}"
    out="$(dc exec -T api python "${WS_SCRIPT}" idle "wss://nginx/ws/system" "${CERT}" "${IDLE_SECONDS}" \
        < <(sid_of_jar) 2>&1)"
    case "${out}" in inner-ok*) ;; *) bad "容器内脚本没跑起来：${out:0:300}"; return ;; esac
    case "${out}" in *snapshot*) ok "收到全量快照帧（对照：连上的是真 /ws/system）" ;;
        *) bad "没收到快照帧：${out:0:300}"; return ;; esac
    case "${out}" in
        *"alive "*)
            extra="${out##*alive }"
            ok "空闲 ${IDLE_SECONDS} 秒后 ping 10 秒内有 pong"
            [ "${extra}" = "0" ] || manual "空闲期间服务端推了 ${extra} 帧，「空闲不掉线」的证明力不足" ;;
        *) bad "空闲 ${IDLE_SECONDS} 秒后连接不再存活：${out:0:300}" ;;
    esac
}

# =============================================================================
# 执行：按编号跑；21 永远最后，且在它之前把 API 侧的临时改动收拾干净
# =============================================================================
for n in 1 2 3 4 5 22 23 24 27 28 5b 6 26 7 8 9 10 11 12 25; do
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
