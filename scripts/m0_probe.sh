#!/bin/bash
# =============================================================================
# M0 契约实测 —— 宿主侧外壳。**请由你本人在自己的终端里运行。**
#
#   ./scripts/m0_probe.sh
#
# 可选覆盖（都不是机密，走环境变量即可）：
#   M0_BUDGET=0.1 ./scripts/m0_probe.sh   —— 小预算，用来实测 --max-budget-usd 真拦截
#   M0_TURNS=6    ./scripts/m0_probe.sh   —— 轮次上限，是预算失效时唯一的兜底
#
# 本脚本是凭据唯一的入口，也是唯一需要人在场的一步。它做三件事：
#   1. 用 `read -rs` 读凭据 —— 不回显、不进 shell 历史、不进 argv
#   2. 把凭据从 **stdin** 灌进 api 容器里的 m0_probe_inner.sh
#   3. 打印容器内生成的报告路径（报告里没有凭据）
#
# 为什么凭据必须走 stdin，而不是 `docker exec -e LLM_API_KEY=...`：
#   `-e` 的值会出现在**宿主** docker 客户端的命令行里，`ps aux` 全机可见。
#   stdin 不出现在任何进程表里。
#
# 为什么不接受凭据作为参数、不读文件、不读环境变量：
#   参数进 argv 且进 shell 历史；读文件要求凭据先落盘；读环境变量会被 `env` 打印出来。
#   三条都与本项目「凭据绝不落盘 / 绝不进 argv」的不变式冲突。
#
# 本脚本**从不**把凭据写进任何文件、从不 echo 它、也从不把它传给 agent。
# =============================================================================
set -euo pipefail

# 中文提示里 ${VAR} 一律带花括号 —— macOS 自带 bash 3.2 会把全角标点吞进变量名，
# `set -u` 下直接崩。见 pitfalls/history-pitfalls.md 条 14。
API_CONTAINER="strix-console-api-1"
TARGET_DEFAULT="http://juice-shop:3000"
INNER_LOCAL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/m0_probe_inner.sh"
INNER_REMOTE="/tmp/m0_probe_inner.sh"

die() { printf '✗ %s\n' "$*" >&2; exit 1; }

# --- 前置检查（都不需要凭据，先做完，别让人输了凭据才发现环境没起来）----------
[ -f "${INNER_LOCAL}" ] || die "找不到 ${INNER_LOCAL}"
docker info >/dev/null 2>&1 || die "Docker 守护进程不可达"

docker ps --format '{{.Names}}' | grep -qx "${API_CONTAINER}" \
  || die "容器 ${API_CONTAINER} 没在跑。先执行：docker compose -p strix-console up -d"

docker ps --format '{{.Names}}' | grep -qx "m0-juice-shop" \
  || die "靶场 m0-juice-shop 没在跑。先执行 PLAN.md §里程碑 M0 里那条 docker run"

docker exec "${API_CONTAINER}" python3 -c "
import sys, urllib.request
try: urllib.request.urlopen('${TARGET_DEFAULT}', timeout=8)
except Exception as e: sys.exit(str(e))
" >/dev/null 2>&1 || die "从 ${API_CONTAINER} 访问 ${TARGET_DEFAULT} 失败 —— 检查 strix_sandbox 网络与靶场别名"

printf '前置检查通过：api 容器在跑、靶场 %s 可达\n\n' "${TARGET_DEFAULT}"

# --- 选服务商 -----------------------------------------------------------------
# 本网络（企业防火墙选择性解密）实测：Anthropic / OpenAI / OpenRouter / DeepSeek
# 四家端点**都被解密**，容器内没有企业根 CA，TLS 握手就失败，跟凭据无关。
# Bedrock 与 Gemini 未被拦截。详见 pitfalls/history-pitfalls.md 条 19。
cat <<'EOF'
第 1 步／3　选鉴权方式。本网络实测未被 TLS 解密的只有 Bedrock 与 Gemini 两家；
Bedrock 自己又有两种凭据形状（互斥，一次只用一种）：

  1) Bedrock API key（bearer）—— **推荐**。控制台 Bedrock → API keys 生成，
     形如 ABSK…。要 1 个 token + 区域。litellm 见到它就发 Authorization: Bearer，
     整段跳过 SigV4（base_aws_llm.py:1554-1564）。
  2) Bedrock SigV4 —— IAM 长期凭据。要 access key id + secret + 区域，共 3 个值。
  3) Google Gemini —— 单 Key。Google AI Studio 免费申请、有免费额度。
     代价：Strix 未按 Gemini 调优，工具调用行为可能有差异。

  1 与 2 后面都是 Anthropic Claude，而 Strix 的 system prompt 与 ~90 个 skill
  都是按 Claude 调的，行为最接近设计意图 —— 所以优先 Bedrock。

  （Anthropic / OpenAI / OpenRouter / DeepSeek 在本网络都会 TLS 握手失败，
    除非把企业根 CA 挂进容器 —— 那等于让凭据明文过一遍解密设备，不建议。）
EOF
printf '  选 1 / 2 / 3：'
read -r CHOICE

case "${CHOICE}" in
  1) AUTH="bedrock-apikey" ;;
  2) AUTH="bedrock"        ;;
  3) AUTH="single"         ;;
  *) die "只能选 1 / 2 / 3" ;;
esac

# --- 模型名与区域（都不是机密，可以明文提示）---------------------------------
printf '\n第 2 步／3　模型与目标\n'
if [ "${AUTH}" != "single" ]; then
  if [ "${AUTH}" = "bedrock-apikey" ]; then
    cat <<'EOF'
  ⚠️ 用 Bedrock API key（bearer）时，模型名**必须带 invoke/ 段**：
       bedrock/invoke/us.anthropic.claude-opus-5
       bedrock/invoke/us.anthropic.claude-sonnet-4-5-20250929-v1:0
     不带 invoke/ 走的是 converse 路由，它在这个 litellm 版本里**不认 bearer token** ——
     会崩在 `'NoneType' object has no attribute 'access_key'`（已实测，见 pitfalls 条 22）。
     invoke 路由已实测认 bearer，且成本计算与 converse 逐位相同，预算护栏不受影响。
     容器内脚本见到 invoke/ 会自动设 STRIX_PROMPT_CACHE=false —— 该路由不接受
     converse 专用的缓存标记，不关掉会被 Bedrock 400（已实测，见 pitfalls 条 23）。
     代价只是失去缓存折扣，功能不受影响。
EOF
  else
    cat <<'EOF'
  Bedrock SigV4 的模型名带 bedrock/ 前缀即可，跨区推理带 us./eu./apac. 前缀：
    bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0
EOF
  fi
  cat <<'EOF'
  用 `aws bedrock list-foundation-models --region <区域>` 可以列出你账号能用的。
  区域必填 —— 即便走 bearer token，litellm 也要用它拼出端点 URL。
EOF
  printf '  区域 [回车用 us-east-1]：'
  read -r M0_AWS_REGION
  M0_AWS_REGION="${M0_AWS_REGION:-us-east-1}"
else
  cat <<'EOF'
  Gemini 模型名例如：
    gemini/gemini-2.5-pro
    gemini/gemini-2.5-flash
EOF
fi

printf '  模型名（STRIX_LLM）：'
read -r STRIX_LLM
[ -n "${STRIX_LLM}" ] || die "模型名不能为空"

# 模型名形状校验 —— **必须在问凭据之前**。2026-09-14 实测：输入 `us.anthropic.sonnet 4.6`
# 这类裸名，Strix 会在起飞前 `sys.exit(1)`（interface/main.py:186，面板 UNKNOWN MODEL NAME，
# 因为裸名默认路由到 OpenAI）。放到凭据后面才发现，等于白输一次凭据、白跑一轮。
# 三种形状的要求都是已实测的事实，不是偏好：bearer 必须走 invoke 路由（条 22）。
case "${AUTH}" in
  bedrock-apikey)
    case "${STRIX_LLM}" in
      bedrock/invoke/*) ;;
      *) die "bearer 形状的模型名必须是 bedrock/invoke/<model>（converse 路由不认 bearer，见 pitfalls 条 22）。收到：${STRIX_LLM}" ;;
    esac ;;
  bedrock)
    case "${STRIX_LLM}" in
      bedrock/*) ;;
      *) die "SigV4 形状的模型名必须带 bedrock/ 前缀。收到：${STRIX_LLM}" ;;
    esac ;;
  *)
    case "${STRIX_LLM}" in
      */*) ;;
      *) die "模型名必须是 <provider>/<model> 形状，裸名会被默认路由到 OpenAI。收到：${STRIX_LLM}" ;;
    esac ;;
esac

printf '  目标 URL [回车用 %s]：' "${TARGET_DEFAULT}"
read -r M0_TARGET
M0_TARGET="${M0_TARGET:-${TARGET_DEFAULT}}"

# --- 凭据：唯一需要保密的输入 -------------------------------------------------
cat <<'EOF'

第 3 步／3　输入凭据。
  · 不回显、不进 shell 历史、不进命令行参数
  · 只经 stdin 送进 api 容器，用完随该进程一起消失
  · 预算默认 --max-budget-usd 2 / --max-turns 20，跑不飞
EOF
printf '  本次预算 --max-budget-usd %s，轮次上限 %s\n\n' "${M0_BUDGET:-2}" "${M0_TURNS:-20}"

if [ "${AUTH}" = "bedrock-apikey" ]; then
  printf '  Bedrock API key（形如 ABSK…，输入时看不见，粘贴后按回车）：'
  read -rs BR_TOKEN; printf '\n\n'
  [ -n "${BR_TOKEN}" ] || die "token 不能为空"
  CREDS=$(printf '%s\n' "${BR_TOKEN}")
  unset BR_TOKEN
elif [ "${AUTH}" = "bedrock" ]; then
  printf '  AWS_ACCESS_KEY_ID（形如 AKIA…，输入时看不见）：'
  read -rs AWS_ID; printf '\n'
  printf '  AWS_SECRET_ACCESS_KEY（输入时看不见）：'
  read -rs AWS_SECRET; printf '\n\n'
  [ -n "${AWS_ID}" ] && [ -n "${AWS_SECRET}" ] || die "两个值都不能为空"
  CREDS=$(printf '%s\n%s\n' "${AWS_ID}" "${AWS_SECRET}")
  unset AWS_ID AWS_SECRET
else
  printf '  API Key（输入时看不见，粘贴后按回车）：'
  read -rs LLM_KEY; printf '\n\n'
  [ -n "${LLM_KEY}" ] || die "Key 不能为空"
  CREDS=$(printf '%s\n' "${LLM_KEY}")
  unset LLM_KEY
fi

# --- 送执行体进容器（只送脚本，不送凭据）-------------------------------------
docker cp "${INNER_LOCAL}" "${API_CONTAINER}:${INNER_REMOTE}" >/dev/null

# --- 跑：凭据走 stdin ---------------------------------------------------------
# STRIX_LLM / M0_TARGET / 区域 用 -e 传是安全的（都不是机密）；凭据绝不这样传。
set +e
printf '%s\n' "${CREDS}" | docker exec -i \
  -e "M0_AUTH=${AUTH}" \
  -e "STRIX_LLM=${STRIX_LLM}" \
  -e "M0_TARGET=${M0_TARGET}" \
  -e "M0_AWS_REGION=${M0_AWS_REGION:-}" \
  -e "M0_BUDGET=${M0_BUDGET:-2}" \
  -e "M0_TURNS=${M0_TURNS:-20}" \
  "${API_CONTAINER}" sh "${INNER_REMOTE}"
RC=$?
set -e

# 宿主侧立刻清掉。真正的清除是本进程退出，unset 只是缩短窗口。
unset CREDS

printf '\n=============================================================\n'
case "${RC}" in
  0) printf '✓ M0 五条断言全部实测通过，且扫描正常收尾 —— 可以开工了\n' ;;
  3) printf '△ M0 尚未通过：无失败，但有断言「无从判定」（多半是扫描没起来）。\n  别据此开工 —— 没跑到的断言等于没证明。\n' ;;
  4) printf '△ M0 尚未通过：断言都成立，但**扫描本身失败了**。\n  断言全绿不等于扫描跑起来了 —— 报告里有自动归因，照它改完重跑。\n' ;;
  *) printf '✗ M0 有断言**未通过**（退出码 %s）。\n  若挂的是第 4 条（Caido 端点解析成 127.0.0.1），按 PLAN.md 走 R1 的 Plan B/C，不要继续往下建。\n' "${RC}" ;;
esac

cat <<EOF

完整报告在容器内（不含凭据）：
  docker exec ${API_CONTAINER} sh -c 'cat \$CONSOLE_DATA_DIR/scans/probe/m0-report.txt'

跑完记得收靶场（按 label 精确删，绝不用 prune）：
  docker rm -f m0-juice-shop
EOF
exit "${RC}"
