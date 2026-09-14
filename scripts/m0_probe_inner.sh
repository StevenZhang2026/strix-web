#!/bin/sh
# =============================================================================
# M0 契约实测 —— **容器内**执行体。由 scripts/m0_probe.sh 送进 api 容器。
#
# 为什么整套探测都在容器内跑，而不是宿主脚本编排：
#   五条断言里有三条（2/3，以及 4 的一部分）必须**拿着凭据去 grep**。
#   如果宿主脚本持有凭据，它就得把凭据传给每一条 docker 命令 —— 那正是泄漏点。
#   而 api 容器本来就同时具备三样东西：凭据（本脚本从 stdin 读入）、docker CLI、
#   docker.sock。所以把凭据的生命周期整个关在这一个 shell 里，宿主侧永不持有。
#
# 凭据的输入方式：**stdin，一行一个**。刻意不用参数、不用 docker exec -e：
#   - 参数 → 进 argv，宿主 `ps` 可见
#   - docker exec -e KEY=... → 值进宿主 docker 客户端的 argv，同样可见
#   stdin 不出现在任何进程表里。
#
# 三种鉴权形状（M0_AUTH）—— 这三种形状本身就是产品需求 N1 的实证：
# 「一个供应商 = 一个 Key 字符串」是错的，凭据是一**组**值，且组的形状因供应商而异。
#
#   single         —— stdin 第 1 行 = LLM_API_KEY。Anthropic / OpenAI / Gemini / DeepSeek 等
#   bedrock        —— SigV4。stdin 第 1 行 = AWS_ACCESS_KEY_ID，第 2 行 = AWS_SECRET_ACCESS_KEY，
#                     外加区域（M0_AWS_REGION，不是机密）→ 共 3 个值
#   bedrock-apikey —— Bedrock API key（bearer token）。stdin 第 1 行 = AWS_BEARER_TOKEN_BEDROCK，
#                     外加区域 → 共 2 个值
#
# 为什么 Bedrock 要单独两路，不能塞进 single：
#   ① Strix 的 _mirror_api_key_to_provider_env（config/models.py:590-592）只填名字以
#      _API_KEY 结尾的变量，AWS_* 三个都不符合，它帮不上忙。
#   ② 但 LLM_API_KEY 对 Strix 是**可选**的（interface/environment.py:43-44 进的是
#      missing_optional_vars，只有 STRIX_LLM 必填），所以直接喂 AWS_* 就能跑。
#   ③ 两种 Bedrock 形状**互斥**：litellm 的 base_aws_llm.py:1554-1564 先看
#      AWS_BEARER_TOKEN_BEDROCK，有值就发 `Authorization: Bearer …` 并**整段跳过 SigV4**。
#      同时给两套只会让错误难归因，所以这里刻意分成两个模式，一次只导出一套。
#
# sh（不是 bash）：api 镜像基于 python:3.12-slim，不为这个脚本多装一个 shell。
# 中文提示里一律 ${VAR} 而非 $VAR —— 见 pitfalls 条 14（那条讲 bash 3.2，这里是 dash，
# 但统一写法成本为零，且这个文件也会被人拷去 bash 里跑）。
# =============================================================================
set -u

RUN_ID="probe"
HOME_DIR="/run/strix/${RUN_ID}"
CFG_DIR="${HOME_DIR}/.strix"
CFG="${CFG_DIR}/cli-config.json"
WORK="${CONSOLE_DATA_DIR}/scans/${RUN_ID}"
REPORT="${WORK}/m0-report.txt"

PASS=0
FAIL=0
SKIP=0

# 报告是给人和给 agent 看的，**绝不能含凭据**。所有写报告的路径只写判定，不写命中内容。
say()  { printf '%s\n' "$*" | tee -a "${REPORT}"; }
ok()   { PASS=$((PASS+1)); say "  ✓ $*"; }
bad()  { FAIL=$((FAIL+1)); say "  ✗ $*"; }
skip() { SKIP=$((SKIP+1)); say "  – $*（跳过）"; }

# --- 读凭据：stdin，一行一个 --------------------------------------------------
# SEC_HARD = 真正的机密，出现在任何落盘面上都是发布阻断项
# SEC_SOFT = 半公开的标识符（AWS access key ID 形如 AKIA…），单独泄漏不可用，
#            但仍要报出来 —— 它落盘说明凭据整体在落盘路径上，是同一个问题的征兆
SEC_SOFT=""
AUTH="${M0_AUTH:-single}"
case "${AUTH}" in
  single)
    IFS= read -r LLM_API_KEY || true
    if [ -z "${LLM_API_KEY:-}" ]; then
      echo "✗ stdin 第 1 行没读到 LLM_API_KEY。用法见 scripts/m0_probe.sh" >&2
      exit 2
    fi
    export LLM_API_KEY
    SEC_HARD="${LLM_API_KEY}"
    CRED_DESC="LLM_API_KEY（${#LLM_API_KEY} 字节）"
    ;;
  bedrock)
    IFS= read -r AWS_ACCESS_KEY_ID || true
    IFS= read -r AWS_SECRET_ACCESS_KEY || true
    if [ -z "${AWS_ACCESS_KEY_ID:-}" ] || [ -z "${AWS_SECRET_ACCESS_KEY:-}" ]; then
      echo "✗ bedrock 模式要 stdin 两行：第 1 行 access key id，第 2 行 secret" >&2
      exit 2
    fi
    AWS_REGION_NAME="${M0_AWS_REGION:?bedrock 模式必须传 M0_AWS_REGION}"
    AWS_REGION="${AWS_REGION_NAME}"
    AWS_DEFAULT_REGION="${AWS_REGION_NAME}"
    export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_REGION_NAME AWS_REGION AWS_DEFAULT_REGION
    SEC_HARD="${AWS_SECRET_ACCESS_KEY}"
    SEC_SOFT="${AWS_ACCESS_KEY_ID}"
    CRED_DESC="AWS SigV4（id ${#AWS_ACCESS_KEY_ID} 字节 / secret ${#AWS_SECRET_ACCESS_KEY} 字节，区域 ${AWS_REGION_NAME}）"
    ;;
  bedrock-apikey)
    IFS= read -r AWS_BEARER_TOKEN_BEDROCK || true
    if [ -z "${AWS_BEARER_TOKEN_BEDROCK:-}" ]; then
      echo "✗ bedrock-apikey 模式要 stdin 一行：Bedrock API key（形如 ABSK…）" >&2
      exit 2
    fi
    AWS_REGION_NAME="${M0_AWS_REGION:?bedrock-apikey 模式必须传 M0_AWS_REGION}"
    AWS_REGION="${AWS_REGION_NAME}"
    AWS_DEFAULT_REGION="${AWS_REGION_NAME}"
    # 刻意**不**导出 AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY：bearer 优先于 SigV4，
    # 同时给两套等于让 litellm 静默忽略一套，失败时归因不了。
    export AWS_BEARER_TOKEN_BEDROCK AWS_REGION_NAME AWS_REGION AWS_DEFAULT_REGION
    SEC_HARD="${AWS_BEARER_TOKEN_BEDROCK}"
    CRED_DESC="Bedrock API key / bearer（${#AWS_BEARER_TOKEN_BEDROCK} 字节，区域 ${AWS_REGION_NAME}）"
    ;;
  *)
    echo "✗ 未知 M0_AUTH=${AUTH}，只支持 single / bedrock / bedrock-apikey" >&2
    exit 2
    ;;
esac

STRIX_LLM="${STRIX_LLM:?必须由外层传入模型名}"
export STRIX_LLM

# --- 预算与轮次上限（都不是机密，可由外层覆盖）---------------------------------
# 默认 2 美元 / 20 轮 = 一次完整的 quick 扫描。
# 故意做成可覆盖，是为了 PLAN.md §Strix 版本升级 阶段 4 的第 ② 项「--max-budget-usd
# 真拦截」：拿一个小到必然撞上的预算（例如 M0_BUDGET=0.1）跑一次，才能看见护栏真的
# 掐住了扫描。轮次上限是这条实验的**兜底** —— 万一成本记账变成恒 0，护栏就永不触发，
# 那时唯一拦住花钱的东西就是 --max-turns。
BUDGET="${M0_BUDGET:-2}"
TURNS="${M0_TURNS:-20}"

# --- invoke 路由必须关掉 prompt cache ------------------------------------------
# Strix 的 prompt caching 只在 Bedrock **converse** 路由上成立：它注入的
# cache_control_injection_points 里有 {"location":"tool_config"}，而只有
# converse_transformation.py 会消费这个参数。invoke 路由不认识它，会原样塞进请求体，
# Bedrock 直接 400：`cache_control_injection_points: Extra inputs are not permitted`。
#
# Strix 自己**想**防住这件事（core/inputs.py:265-282 的 docstring 明说"未映射的 Bedrock
# 模型一个注入点都不给"），但它的防线只预设了 `bedrock/<model>` 一种形状：
# config/models.py:857 的 _prompt_cache_name_candidates 只剥 `litellm/` 和 `bedrock/`，
# **不剥 `invoke/`**，于是第 2、3 个候选名（anthropic.claude-opus-5 / claude-opus-5）
# 照样命中缓存能力表，supports_prompt_caching 返回 True，注入照旧发生。已实测。
#
# 出路是 Strix 自带的一等公民开关 STRIX_PROMPT_CACHE（config/settings.py:51，
# 默认 true）—— 置 false 后 extra_args 只剩 {'timeout': …}，无任何 converse 专用参数残留。
# 代价：失去 Bedrock 缓存读取的折扣。预算护栏**不受影响** —— report/state.py:699
# 的候选名里最后一个是 model.rsplit("/",1)[-1]，天然把 invoke/ 剥掉，两条路由算出的
# 成本逐位相同（10k/1k tokens 均为 $0.0825，已实测）。
case "${STRIX_LLM}" in
  */invoke/*)
    STRIX_PROMPT_CACHE=false
    CACHE_NOTE="已关闭（invoke 路由不接受 converse 专用的缓存标记）"
    ;;
  *)
    STRIX_PROMPT_CACHE=true
    CACHE_NOTE="保持默认开启（converse 路由）"
    ;;
esac
export STRIX_PROMPT_CACHE

# scan_path <路径> —— 在该路径下找凭据。命中的**文件名**打到 stdout，绝不打内容。
# 输出非空 == 有命中。head 限流，避免一个坏结果刷屏。
scan_path() {
  _p="$1"
  [ -e "${_p}" ] || return 0
  grep -rlF -- "${SEC_HARD}" "${_p}" 2>/dev/null | head -20 | sed 's/$/  [机密]/'
  if [ -n "${SEC_SOFT}" ]; then
    grep -rlF -- "${SEC_SOFT}" "${_p}" 2>/dev/null | head -20 | sed 's/$/  [标识符]/'
  fi
  return 0
}

# --- 干净的起点 --------------------------------------------------------------
# 每任务独立 cwd：Strix 把产物固定写「当前 cwd」/strix_runs/<自动名>/（CLI 无 --output-dir）
rm -rf "${WORK}" "${HOME_DIR}"
mkdir -p "${WORK}/tmp" "${CFG_DIR}"
chmod 700 "${HOME_DIR}" "${CFG_DIR}"
# 预置空 env 块：让 Strix 的 persist_current()（config/loader.py:74）写到这里而不是真 HOME
printf '%s\n' '{"env":{}}' > "${CFG}"
chmod 600 "${CFG}"

: > "${REPORT}"
say "===== M0 契约实测 ====="
say "时间       $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
say "鉴权       ${AUTH} —— ${CRED_DESC}"
say "模型       ${STRIX_LLM}"
say "prompt缓存 STRIX_PROMPT_CACHE=${STRIX_PROMPT_CACHE} —— ${CACHE_NOTE}"
say "目标       ${M0_TARGET}"
say "cwd        ${WORK}"
say "HOME       ${HOME_DIR}  (tmpfs)"
say "（本报告不含任何凭据原文）"
say ""

# --- tmpfs 挂载选项前置校验 --------------------------------------------------
# /run/strix 必须是 noexec,nosuid,size=16m,mode=0700。它承载每任务 HOME 与预置
# config，是泄漏矩阵 #2 的一部分；挂错了后面所有"凭据不落盘"的结论都不成立。
say "[前置] /run/strix 挂载选项"
MNT=$(awk '$2=="/run/strix"{print $4}' /proc/mounts)
case "${MNT}" in
  *noexec*) ok "noexec 生效" ;;
  *)        bad "noexec 缺失（实际：${MNT:-未挂载}）—— 这是安全回退，停下别继续" ;;
esac
case "${MNT}" in
  *nosuid*) ok "nosuid 生效" ;;
  *)        bad "nosuid 缺失（实际：${MNT:-未挂载}）" ;;
esac
say ""

# --- 沙箱容器观察器 ----------------------------------------------------------
# 断言 3 要在沙箱**活着的时候**看它的 inspect。扫描结束后容器就没了，所以必须边跑边采。
# 观察器只输出判定（found / clean），绝不回显命中内容。
WATCH_OUT="${WORK}/sandbox-watch.txt"
: > "${WATCH_OUT}"
(
  seen=""
  i=0
  while [ "${i}" -lt 1800 ]; do
    ids=$(docker ps -q --filter "label=strix-run-id=${RUN_ID}" 2>/dev/null || true)
    for id in ${ids}; do
      case " ${seen} " in *" ${id} "*) continue ;; esac
      seen="${seen} ${id}"
      {
        echo "container ${id}"
        docker inspect "${id}" --format '  name={{.Name}} image={{.Config.Image}}' 2>/dev/null
        echo "  labels=$(docker inspect "${id}" --format '{{json .Config.Labels}}' 2>/dev/null)"
        echo "  networks=$(docker inspect "${id}" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}({{$v.IPAddress}}) {{end}}' 2>/dev/null)"
        # 关键：整份 inspect 里找凭据。只记 found/clean。
        _insp=$(docker inspect "${id}" 2>/dev/null)
        _v="clean"
        printf '%s' "${_insp}" | grep -qF -- "${SEC_HARD}" && _v="found-secret"
        if [ -n "${SEC_SOFT}" ]; then
          printf '%s' "${_insp}" | grep -qF -- "${SEC_SOFT}" && _v="${_v},found-id"
        fi
        echo "  KEYSCAN=${_v}"
      } >> "${WATCH_OUT}"
    done
    i=$((i+1))
    sleep 1
  done
) &
WATCHER=$!

# --- 跑扫描 ------------------------------------------------------------------
say "[跑] strix -n -t ${M0_TARGET} -m quick --max-budget-usd ${BUDGET} --max-turns ${TURNS}"
say "     （退出码 2 = 发现漏洞 = 成功，见 CLAUDE.md §Strix 集成）"
SCAN_LOG="${WORK}/strix-stdout.log"
set +e
cd "${WORK}" || exit 1
HOME="${HOME_DIR}" \
TMPDIR="${WORK}/tmp" \
STRIX_RUN_ID="${RUN_ID}" \
strix -n -t "${M0_TARGET}" -m quick \
      --max-budget-usd "${BUDGET}" --max-turns "${TURNS}" \
      --config "${CFG}" > "${SCAN_LOG}" 2>&1
SCAN_RC=$?
set -e
kill "${WATCHER}" 2>/dev/null || true
wait "${WATCHER}" 2>/dev/null || true

say ""
say "退出码 ${SCAN_RC}"
case "${SCAN_RC}" in
  0) say "  → 0 = 进程正常退出，且**没找到漏洞**。注意：这**不**代表扫描跑完了 —— 见下面的 status" ;;
  2) say "  → 2 = 发现漏洞，**按成功计**" ;;
  *) say "  → ${SCAN_RC} = 失败。stdout 尾部见报告末尾" ;;
esac

# 退出码不携带"扫描是否完成"的信息（interface/main.py:494-497 只判有没有漏洞）。
# 预算耗尽被掐死的扫描同样退 0 并宣称"未发现漏洞"—— 在 juice-shop 上那是假的健康证明。
# 唯一的真相来源是 run.json.status。见 pitfalls 条 24。
_RJ=$(find "${WORK}/strix_runs" -mindepth 2 -maxdepth 2 -name run.json 2>/dev/null | head -1)
if [ -n "${_RJ}" ]; then
  _ST=$(sed -n 's/^ *"status": *"\([a-z_]*\)".*/\1/p' "${_RJ}" | head -1)
  say "  run.json.status = ${_ST:-未能解析}"
  case "${_ST}" in
    completed) say "    → 扫描**真的跑完了**，「未发现漏洞」是可信的结论" ;;
    stopped|budget_paused)
      say "    → ⚠️ 扫描**被提前中止**（预算或轮次耗尽），「未发现漏洞」**不是**可信结论。"
      say "       控制台必须把这种情况报成「结论不完整」，机器码 scan_incomplete。"
      grep -iE 'budget|max.turn' "${_RJ%/run.json}/strix.log" 2>/dev/null \
        | grep -iE 'exceed|limit|reserve|stopping' | tail -3 | sed 's/^/       /' | tee -a "${REPORT}" ;;
    crashed|failed) say "    → ⚠️ 扫描崩了／失败了，退出码却是 ${SCAN_RC}" ;;
  esac
else
  say "  （找不到 run.json，无法判断扫描是否跑完）"
fi

# --- 早期失败识别 ------------------------------------------------------------
# 扫描根本没起来时，断言 1/3/4/5 是**无从判定**，不是**未通过**。
# 把两者混成 FAIL 会掩盖真正的原因：人看到"产物没写到 cwd"就会去查挂载，
# 而真凶其实是 TLS 握手失败。所以先按 Strix 的 Rich 面板标题分类。
# 这份映射表同时是 T3「stdout 面板标题 → 中文错误码」的第一批真实样本。
#
# ⚠️ **面板标题不足以分类。** 已实测：TLS 被中间设备解密、和凭据无效，Strix 打的
#    都是同一个 `LLM CONNECTION FAILED` 面板。只看标题会把"凭据错"报成"连不上"，
#    让人去查网络而不是去查凭据。所以**先看正文里的异常类名**，标题只做兜底。
EARLY=""
CODE=""
if grep -qE 'CERTIFICATE_VERIFY_FAILED|SSLCertVerificationError' "${SCAN_LOG}"; then
  EARLY="TLS 证书校验失败（端点被中间设备解密，容器内无该根 CA）"; CODE="llm_tls_intercepted"
elif grep -qF "object has no attribute 'access_key'" "${SCAN_LOG}"; then
  # litellm 的 bedrock **converse** 路由（`bedrock/<model>`）在解析不出 SigV4 凭据时
  # 会拿着 None 去取 .access_key 而崩。两种成因：①压根没给凭据；
  # ②只给了 bearer token —— converse_handler.py:334-377 先无条件构造 credentials
  #   再喂 rust bridge，走不到 base_aws_llm 里那个认 bearer 的 _sign_request。
  # 出路是换 **invoke** 路由：`bedrock/invoke/<model>`（已实测认 bearer，成本计算一致）。
  EARLY="Bedrock 凭据形状与路由不匹配（converse 路由不认 bearer token）—— 换成 bedrock/invoke/<model>"
  CODE="bedrock_route_rejects_bearer"
elif grep -qF 'cache_control_injection_points' "${SCAN_LOG}"; then
  # 这条**必须**排在 ValidationException 之前 —— 否则会被归成"没有模型访问权"，
  # 让人跑去 AWS 控制台申请权限，而真凶是发了个 invoke 路由不认的参数。
  # 注意：这个错误是 Bedrock **校验请求体**时报的，意味着鉴权**已经通过了**。
  EARLY="invoke 路由收到了 converse 专用的 prompt cache 参数 —— 需 STRIX_PROMPT_CACHE=false（鉴权其实已通过）"
  CODE="prompt_cache_unsupported_on_route"
elif grep -qE 'AuthenticationError|security token included in the request is invalid|invalid_api_key|Incorrect API key|Invalid API Key' "${SCAN_LOG}"; then
  EARLY="凭据无效（能连上端点，是鉴权被拒）"; CODE="invalid_api_key"
elif grep -qE 'AccessDenied|not authorized to perform|ValidationException' "${SCAN_LOG}"; then
  EARLY="凭据有效但无权调用该模型（Bedrock 需在该区域申请模型访问权）"; CODE="model_access_denied"
elif grep -qF 'UNKNOWN MODEL NAME' "${SCAN_LOG}"; then
  # 2026-09-14 实测撞到的**新失败模式**：模型名里没有 `/`，Strix 在起飞前就
  # `sys.exit(1)`（interface/main.py:186），run.json 都不会生成。
  # 原来的分类器不认这条，于是断言 1/3/4 被记成「未通过」而不是「无从判定」——
  # 正是本脚本注释里警告过的那种误导（人会去查挂载，真凶是模型名）。
  EARLY="模型名不是 <provider>/<model> 形状（裸名默认路由到 OpenAI，Strix 起飞前直接退出）"
  CODE="model_name_not_provider_qualified"
elif grep -qE 'NotFound|model_not_found|MODEL NOT FOUND' "${SCAN_LOG}"; then
  EARLY="模型名不存在或该区域不提供"; CODE="model_not_found"
elif grep -qF 'MISSING REQUIRED ENVIRONMENT VARIABLES' "${SCAN_LOG}"; then
  EARLY="缺必填环境变量（只有 STRIX_LLM 是必填）"; CODE="missing_required_env"
elif grep -qiF 'docker' "${SCAN_LOG}" && grep -qiF 'permission denied' "${SCAN_LOG}"; then
  EARLY="Docker 权限不足（docker.sock）"; CODE="docker_permission_denied"
elif grep -qF 'LLM CONNECTION FAILED' "${SCAN_LOG}"; then
  EARLY="连不上大模型端点（未能进一步归因）"; CODE="llm_connection_failed"
fi
if [ -n "${EARLY}" ]; then
  say ""
  say "  ⚠️ 识别到**早期失败**：${EARLY}（建议机器码 ${CODE}）"
  say "     扫描压根没起来，所以断言 1/3/4/5 记为「无从判定」而不是「未通过」——"
  say "     把这两者混起来会让人去查错的地方。断言 2 仍然有效且仍然必须过。"
fi
say ""

# =============================================================================
# 断言 1：产物目录出现在任务 cwd 下
# =============================================================================
say "[断言 1] strix_runs/<自动名>/ 出现在 cwd"
RUN_DIR=""
if [ -n "${EARLY}" ] && [ ! -d "${WORK}/strix_runs" ]; then
  skip "1 无从判定：${EARLY} —— 扫描没起来，谈不上产物写哪儿"
elif [ -d "${WORK}/strix_runs" ]; then
  RUN_DIR=$(find "${WORK}/strix_runs" -mindepth 1 -maxdepth 1 -type d | head -1)
  if [ -n "${RUN_DIR}" ]; then
    ok "$(basename "${RUN_DIR}")  （$(find "${RUN_DIR}" -type f | wc -l | tr -d ' ') 个文件）"
  else
    bad "strix_runs/ 存在但里面没有 run 目录"
  fi
else
  bad "${WORK}/strix_runs 不存在 —— 产物没写到任务 cwd，独立 cwd 的假设不成立"
fi
say ""

# =============================================================================
# 断言 2：凭据不在数据目录、不在 /root；预置 config 的情况；清理后彻底消失
#
# 这是本轮最重要的一条，也是唯一**不依赖扫描是否跑起来**的一条。
# 分成四小条各自独立判定 —— 混成一条会掩盖到底哪里漏了。
# =============================================================================
say "[断言 2] 凭据落盘卫生"

# 2a：数据目录全树（含 strix.log —— 它恒为 DEBUG 级且由 Strix 子进程自己写，
#     我们的 RedactionFilter 在那个进程里不存在，所以这条是实打实的门）
H=$(scan_path "${CONSOLE_DATA_DIR}")
if [ -z "${H}" ]; then
  ok "2a 数据目录 ${CONSOLE_DATA_DIR}/ 全树 0 命中"
else
  bad "2a 数据目录有命中 —— 发布阻断。命中文件（仅文件名）："
  printf '%s\n' "${H}" | sed 's/^/        /' | tee -a "${REPORT}"
fi

# 2b：真 HOME。--config 重定向若失效，凭据会落到 /root/.strix/cli-config.json
if [ -e /root ]; then
  H=$(scan_path /root)
  if [ -z "${H}" ]; then
    ok "2b /root/ 全树 0 命中（--config 重定向生效）"
  else
    bad "2b /root/ 有命中 —— --config 重定向失效"
    printf '%s\n' "${H}" | sed 's/^/        /' | tee -a "${REPORT}"
  fi
else
  skip "2b /root 不存在"
fi

# 2c：预置 config 是否被写入凭据。
#     PLAN.md 原稿断言"里面有 Key"，依据是 config/loader.py:56-74 的 persist_current()
#     会把 env 里所有已知 alias 原样写回配置文件。**它是无条件调用的**（interface/main.py:404），
#     所以"没写进凭据"绝不等于"没被调用"——只等于**本次这个凭据的变量名不在 alias 表里**。
#     实测：LLM_API_KEY 在表里（settings.py:27-31）→ single 形状**一定明文落盘**；
#           AWS_* 四个都不在表里 → 两种 Bedrock 形状都不落盘。见 pitfalls 条 25。
#     **两种结果都不算失败**：关键是它必须在 tmpfs 上、且 rm 后消失（看 2d）。
#     所以这里只做信息记录，不计入 PASS/FAIL —— 但要把"结论只对本形状成立"说明白。
if [ -n "$(scan_path "${CFG}")" ]; then
  say "  i 2c 预置 config 内**含**凭据 → 符合 PLAN.md 原稿预期。它在 tmpfs 上，看 2d"
  CFG_HAD=yes
else
  say "  i 2c 预置 config 内**不含**凭据 —— **这只说明 ${AUTH} 形状的变量名不在 Strix 的 alias 表里，**"
  say "        **不说明 persist_current 没跑**（它无条件调用）。换成 single 形状会明文落盘。"
  CFG_HAD=no
fi
# 文件大小是关键证据：预置时是 {"env":{}} = 11 字节，变大就说明它**被写过**。
say "      $(ls -l "${CFG}" | awk '{print "mode="$1" size="$5}')（预置时 11 字节，变大即被写过）"
say "      写进去的 env 键（只列**键名**，键名不是机密）：$(sed -n 's/^ *"\([A-Z_][A-Z0-9_]*\)".*/\1/p' "${CFG}" | tr '\n' ' ')"
say "      路径=${CFG}"

# 2d：清理后凭据不在**任何一个可枚举的落盘面**上。这条是"凭据绝不落盘"的最终判据。
#
# 刻意**不**写 `grep -r / `：实测那样在本镜像里 6 分钟都跑不完（768 MB 解包 +
# site-packages 几十万小文件 + 同路径 bind mount 走 VirtioFS），会把脚本卡成"看起来死了"。
# 见 pitfalls 条 20。凭据可能落盘的位置是**可枚举的**，逐个精确扫既快又说得清扫了什么。
rm -rf "${HOME_DIR}"
if [ -e "${HOME_DIR}" ]; then
  bad "2d ${HOME_DIR} 删不掉"
else
  SURFACES="/run/strix ${CONSOLE_DATA_DIR} /root /tmp /var/tmp /home /etc"
  RESIDUE=""
  for s in ${SURFACES}; do
    [ -n "$(scan_path "${s}")" ] && RESIDUE="${RESIDUE} ${s}"
  done
  if [ -z "${RESIDUE}" ]; then
    ok "2d 全部落盘面 0 命中（2c=${CFG_HAD} 的那份也随 tmpfs 一起没了）"
    say "      已扫：${SURFACES}"
    say "      未扫 /proc —— /proc/<pid>/environ 可见凭据是**已知且接受**的残余风险，不是本条的对象"
  else
    bad "2d 这些落盘面仍有残留 —— 发布阻断：${RESIDUE}"
    for s in ${RESIDUE}; do
      scan_path "${s}" | sed 's/^/        /' | tee -a "${REPORT}"
    done
  fi
fi
say ""

# =============================================================================
# 断言 3：沙箱容器的 inspect 里没有凭据
# =============================================================================
say "[断言 3] 沙箱容器 inspect 无凭据"
if [ ! -s "${WATCH_OUT}" ] && [ -n "${EARLY}" ]; then
  skip "3 无从判定：${EARLY} —— 沙箱压根没被创建"
elif [ ! -s "${WATCH_OUT}" ]; then
  bad "3 观察器一个带 label=strix-run-id=${RUN_ID} 的容器都没抓到。两种可能："
  say "        ① 沙箱没起来（看退出码与 stdout）"
  say "        ② STRIX_RUN_ID 没生效 → label 没打上"
  say "           （docker_client.py:113-123 只在该 env 非空时打 label）"
  say "        无论哪种，Reaper 的回收前提都不成立，必须查清楚"
else
  N=$(grep -c '^container ' "${WATCH_OUT}")
  say "      抓到 ${N} 个沙箱容器"
  grep -E '^  (name|labels|networks|KEYSCAN)' "${WATCH_OUT}" | sed 's/^/      /' | tee -a "${REPORT}"
  if grep -q 'KEYSCAN=.*found' "${WATCH_OUT}"; then
    bad "3 沙箱 inspect 里出现了凭据 —— 发布阻断"
  else
    ok "3 全部 ${N} 个沙箱 inspect 均无凭据"
  fi
fi
say ""

# =============================================================================
# 断言 4：Caido 端点解析成容器 IP，不是 127.0.0.1  —— 全计划最高风险项
# =============================================================================
say "[断言 4] Caido host endpoint（最高风险项）"
if [ -n "${EARLY}" ] && [ -z "${RUN_DIR}" ]; then
  skip "4 无从判定：${EARLY} —— **本条是全计划最高风险项，必须在网络问题解决后重跑**"
elif [ -z "${RUN_DIR}" ] || [ ! -f "${RUN_DIR}/strix.log" ]; then
  bad "4 找不到 strix.log，无法判定"
else
  LINE=$(grep -F 'Caido host endpoint resolved' "${RUN_DIR}/strix.log" | tail -1)
  if [ -z "${LINE}" ]; then
    skip "4 strix.log 里没有这行 —— 本次扫描可能没启用抓包代理。**不算通过**，需重跑确认"
    say "      （出处 runtime/session_manager.py:180，恒写入 file handler，与 STRIX_DEBUG 无关）"
  else
    say "      ${LINE}"
    case "${LINE}" in
      *127.0.0.1*|*localhost*)
        bad "4 解析成 127.0.0.1 —— 在后端容器里那指向它自己，抓包代理**静默降级**"
        say "        按 PLAN.md：过不了第 4 条就走 R1 的 Plan B/C，**不要继续往下建**" ;;
      *)
        if echo "${LINE}" | grep -qE '(^|[^0-9.])(172\.|10\.|192\.168\.)[0-9]'; then
          ok "4 解析成容器 IP —— STRIX_DOCKER_SANDBOX_NETWORK 生效，抓包代理可用"
        else
          bad "4 既不是 127.0.0.1 也不像容器 IP，需人工判读上面那行"
        fi ;;
    esac
  fi
fi
say ""

# =============================================================================
# 断言 5：无残留沙箱
# =============================================================================
say "[断言 5] 无残留沙箱容器"
LEFT=$(docker ps -aq --filter "label=strix-run-id=${RUN_ID}" 2>/dev/null | wc -l | tr -d ' ')
if [ "${LEFT}" = "0" ]; then
  ok "5 docker ps -a 无 label=strix-run-id=${RUN_ID} 的残留"
else
  bad "5 残留 ${LEFT} 个 —— 这正是 Reaper 存在的理由，记下来给 T30"
  docker ps -a --filter "label=strix-run-id=${RUN_ID}" \
    --format '        {{.Names}} {{.Status}} {{.Image}}' | tee -a "${REPORT}"
fi
say ""

# =============================================================================
# 断言 6：成本记账不为 0 —— `--max-budget-usd` 能不能拦截，全压在这一个数上
#
# 为什么这条要每次升级都重跑（PLAN.md §Strix 版本升级 阶段 4 第 ② 项）：
#   护栏的判据是 core/hooks.py:55 的 `cost >= max_budget_usd`。cost 由
#   report/usage.py 的 total_cost 给出，而它是 `observed if has_observed else estimated`：
#     · observed  = litellm 自己回的 response_cost（report/state.py:866-887）
#     · estimated = 本地价目表兜底（report/usage.py:203-219）
#   两条都算不出来时 cost 恒为 0，于是护栏**永不触发**，且**不报任何错**。
#   1.6.2 新增的 report/pricing.py 已实测把 Bedrock 名字解析成 `bedrock_converse/…`，
#   而 litellm 1.100.0 的 completion_cost 不认这个前缀（BadRequestError）——
#   也就是说**兜底那条路对我们的模型已经是断的**，护栏现在完全依赖 observed。
say "[断言 6] 成本记账非 0（预算护栏的唯一前提）"
if [ -z "${_RJ}" ]; then
  skip "6 无从判定：找不到 run.json"
else
  # 只读三个数字，不读别的 —— run.json 里没有凭据，但少读一样是少一个泄漏面。
  set -- $(python3 -c 'import json,sys
u = json.load(open(sys.argv[1])).get("llm_usage") or {}
print(u.get("cost") or 0, u.get("total_tokens") or 0, u.get("requests") or 0)' "${_RJ}" 2>/dev/null)
  _COST="${1:-0}"; _TOK="${2:-0}"; _REQ="${3:-0}"
  say "      llm_usage: cost=${_COST} total_tokens=${_TOK} requests=${_REQ}  预算上限=${BUDGET}"
  if [ "${_REQ}" = "0" ] || [ "${_TOK}" = "0" ]; then
    skip "6 无从判定：一次 LLM 调用都没完成，谈不上记账"
  elif python3 -c "import sys; sys.exit(0 if float('${_COST}') > 0 else 1)"; then
    ok "6 cost=${_COST} > 0 —— observed 成本进得来，护栏的判据成立"
  else
    bad "6 cost 恒为 0 而 tokens=${_TOK} —— **预算护栏静默失效**，--max-budget-usd 永不触发"
    say "        这正是 report/pricing.py 那条断掉的兜底暴露出来的后果，发布阻断。"
  fi
  # 拦截行为本身：只有拿一个小到必然撞上的预算跑才看得见（M0_BUDGET=0.1）。
  case "${_ST:-}" in
    stopped|budget_paused)
      say "      i 本次 status=${_ST} 且 cost=${_COST} / 上限 ${BUDGET} → **护栏真的掐住了扫描**（阶段 4 ② 实测到）" ;;
    completed)
      say "      i 本次 status=completed，cost 未触及上限 —— 只证明了记账在跑，**没证明拦截**。" ;;
  esac
fi
say ""

# --- 附：Strix stdout 尾部（供失败归因；先确认不含凭据才摘录）-----------------
say "===== strix stdout 末 40 行（已过凭据过滤）====="
if [ -n "$(scan_path "${SCAN_LOG}")" ]; then
  say "  ⚠️ stdout 里含凭据 —— **不摘录**。这本身是一条发布阻断项，已计入失败。"
  FAIL=$((FAIL+1))
else
  tail -40 "${SCAN_LOG}" | sed 's/^/  /' | tee -a "${REPORT}"
fi
say ""

say "===== 小结：通过 ${PASS} / 失败 ${FAIL} / 跳过 ${SKIP} ====="
say "报告（不含凭据）：${REPORT}"

# 凭据出这个 shell 就没了。unset 只是缩短窗口 —— 进程退出才是真正的清除。
unset LLM_API_KEY AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_BEARER_TOKEN_BEDROCK \
      SEC_HARD SEC_SOFT 2>/dev/null || true

# 退出码四态。两条独立的门，缺一不可：
#
#   ① 五条断言全过。**"跳过"绝不能当成功** —— M0 的意义是把契约**证明**一遍，
#      没跑到的断言就是没证明。尤其断言 4 是全计划最高风险项，记成 0 会让人以为过了。
#   ② 扫描本身跑完了（退出码 ∈ {0, 2}）。
#
# 为什么 ② 必须是独立的门：本脚本第一版只看 ①，结果出现过"五条断言全绿、脚本打印
# M0 达成、而 strix 退出 1 且零个 LLM 轮次完成"。断言 2（凭据卫生）不依赖扫描是否
# 跑起来，断言 1/3/5 在早期失败时也可能因为"目录没建 / 没有沙箱 / 因此没有残留"而
# 各自成立 —— 全绿完全可能是**什么都没发生**。M0 要证的是契约在**真跑一次**时成立。
if [ "${FAIL}" != "0" ]; then
  say ""
  say "→ 退出 1：有 ${FAIL} 条断言**未通过**"
  exit 1
elif [ "${SKIP}" != "0" ]; then
  say ""
  say "→ 退出 3：无失败，但有 ${SKIP} 条**无从判定**。M0 尚未通过，别据此开工。"
  exit 3
elif [ "${SCAN_RC}" != "0" ] && [ "${SCAN_RC}" != "2" ]; then
  say ""
  say "→ 退出 4：断言都成立，但**扫描本身失败了**（strix 退出 ${SCAN_RC}）。"
  say "   断言全绿不等于扫描跑起来了 —— 它们各自成立也可能只是因为什么都没发生。"
  if [ -n "${EARLY}" ]; then
    say "   已归因：${EARLY}（机器码 ${CODE}）"
  else
    say "   未能自动归因 —— 看报告末尾的 stdout 尾部，并把新样本补进本脚本的分类器。"
  fi
  exit 4
fi
say ""
say "→ 退出 0：五条断言全部实测通过，且扫描以 ${SCAN_RC} 正常收尾。M0 达成。"
exit 0
