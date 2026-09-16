#!/bin/bash
# =============================================================================
# 派发预算量具 —— 收货时按 `agent-rules.md` §九.6 量一次，别只凭子 agent 自陈。
#
#   ./scripts/agent_budget.sh          —— 最近 3 个子 agent
#   ./scripts/agent_budget.sh 6        —— 最近 6 个
#   ./scripts/agent_budget.sh --main   —— 改量主会话（最近 3 个 session）
#
# 它回答三个问题（§九.6 的三条失败判据，按重要性排）：
#   ① 峰值上下文多少、压缩过几次      —— **这条才是真闸**（实测 108 次调用仍然压缩）
#   ② 第几次工具调用才开始写代码      —— 晚于 10 次 = 出处没给够，它在自己找
#   ③ 工具调用总数                    —— 超 150 次 = 拆得不对
# 附带打印各工具的**输入**字符（Edit/Write 的输入是它自己写的代码，会永久留在上下文里，§十.6）
# 与 tool_result 字符（读进来的量），用来判断"上下文是读来的还是自己产的"。
#
# 为什么要有这个脚本：这些数子 agent 自己看不到，task notification 也不报压缩 ——
# 只有 transcript 里有。手敲 jq 每次都要重新想一遍，写死一次就能在每次收货时无脑跑。
#
# 子 agent 的 transcript 在 `<projects>/<session-id>/subagents/agent-<id>.jsonl`；
# **同一个 agent 会横跨多个文件** —— 主会话 `/clear` 时正在跑的子 agent 会被重挂到新
# session 目录下、文件名不变。所以按文件名归并，否则峰值和压缩次数都会少算。
# =============================================================================
set -euo pipefail

PEAK_LIMIT=120000   # 峰值上下文（token）超这个数就当压缩已经发生
CALLS_LIMIT=150     # 工具调用总数上限
FIRSTCODE_LIMIT=10  # 第几次调用之前必须开始写代码

command -v jq >/dev/null 2>&1 || { echo "缺 jq，装不了就手敲 §九.6 那几条 —— brew install jq" >&2; exit 1; }

REPO="$(git rev-parse --show-toplevel)"
PROJ_DIR="${HOME}/.claude/projects/$(printf '%s' "${REPO}" | sed 's#/#-#g')"
[ -d "${PROJ_DIR}" ] || { echo "没找到 transcript 目录：${PROJ_DIR}" >&2; exit 1; }

MODE=subagent
LIMIT=3
for arg in "$@"; do
  case "${arg}" in
    --main) MODE=main ;;
    [0-9]*) LIMIT="${arg}" ;;
    *) echo "用法：$0 [条数] [--main]" >&2; exit 1 ;;
  esac
done

# jq 程序：吃一个 agent（或 session）的全部记录，打印一段判据。
JQ_PROG='
  ([.[] | select(.type == "assistant")]) as $rows
  # 一次 API 响应在 transcript 里按 content block 记多行且每行都带同一份 usage
  # → **必须按 message.id 去重**，否则调用次数和计费都夸大 2 倍上下（output 取最后一行，它是流式递增的）
  | ($rows | group_by(.message.id) | map(.[0] + {out: (.[-1].message.usage.output_tokens)})) as $a
  | ([$rows[] | .message.content[]? | select(.type == "tool_use")]) as $tu
  # 一次压缩会写两条记录（system 的 compact_boundary + 承接摘要的那条 user），按前者计数才不重复
  | ([.[] | select(.type == "system" and .subtype == "compact_boundary")] | length) as $cb
  | ([.[] | select(.isCompactSummary == true)] | length) as $cs
  | (if $cb > 0 then $cb else $cs end) as $compact
  | (($a | map(.message.usage
      | (.input_tokens + .cache_read_input_tokens + .cache_creation_input_tokens))
      | max) // 0) as $peak
  | ([$tu[] | .name] | to_entries
      | map(select(.value == "Write" or .value == "Edit" or .value == "NotebookEdit"))
      | (.[0].key // null)) as $firstcode
  | ([.[] | select(.timestamp)] | (.[0].timestamp[5:16] + " → " + .[-1].timestamp[5:16])) as $span
  | ([$tu[] | {name, n: (.input | tostring | length)}] | group_by(.name)
      | map("\(.[0].name) \(length)次/入\(map(.n) | add)字")| join("  ")) as $tools
  | ([.[] | select(.type == "user") | .message.content[]? | select(.type == "tool_result")
      | (.content | if type == "string" then . else (map(.text // "") | join("")) end) | length]
      | add // 0) as $res
  | ((($a | map(.message.usage.cache_read_input_tokens) | add) * 0.5
      + ($a | map(.message.usage.cache_write_input_tokens // .message.usage.cache_creation_input_tokens) | add) * 6.25
      + ($a | map(.message.usage.input_tokens) | add) * 5
      + ($a | map(.out) | add) * 25) / 1000000) as $usd
  | "  \($span)  API 调用 \($a | length) 次（transcript \($rows | length) 行），工具 \($tu | length) 次",
    "  ≈ $\(($usd * 100 | round) / 100)（opus-5 单价，**用量数据只许进未跟踪的 local-env.md**）",
    "  ① 峰值 \($peak)　压缩 \($compact) 次" +
      (if ($compact == 0 and $peak <= ($limit | tonumber)) then "　✅"
       elif $mode == "main" then "　❌ 超了一个窗口 —— 交底/收货方式错了（§九.6 末句）"
       else "　❌ 拆得不对（§九.6①）" end),
    (if $mode == "main" then empty else                    # ②③ 只对子 agent 成立
      "  ② 首次写代码：" +
        (if $firstcode == null then "全程没写过代码（只读任务？）"
         else "第 \($firstcode + 1) 次调用" +
           (if ($firstcode + 1) > ($fc | tonumber) then "　❌ 出处没给够（§九.6②）" else "　✅" end) end) end),
    (if $mode == "main" then empty else
      "  ③ 工具调用 \($tu | length) 次" +
        (if ($tu | length) > ($calls | tonumber) then "　❌ 超上限（§九.6③）" else "　✅" end) end),
    "  工具明细：\($tools)",
    "  tool_result 合计 \($res) 字符（读进来的量；Edit/Write 的「入」是它自己产的代码，§十.6）"
'

run_one() {  # $1 = 标题；其余 = 该 agent 的全部 transcript 文件（按时间序）
  local title="$1"; shift
  echo "=== ${title}"
  cat "$@" | jq -rs --arg limit "${PEAK_LIMIT}" --arg calls "${CALLS_LIMIT}" \
    --arg fc "${FIRSTCODE_LIMIT}" --arg mode "${MODE}" "${JQ_PROG}"
}

if [ "${MODE}" = main ]; then
  # 主会话：一个 session 一个文件，直接按 mtime 取最近几个
  while IFS= read -r f; do
    run_one "主会话 $(basename "${f}" .jsonl)" "${f}"
  done < <(ls -t "${PROJ_DIR}"/*.jsonl 2>/dev/null | head -n "${LIMIT}")
else
  # 子 agent：先按 mtime 排出最近的文件，再按**文件名**归并（同一 agent 可能横跨多个 session 目录）
  while IFS= read -r name; do
    # shellcheck disable=SC2046  # 要的就是分词：同名文件可能有多个
    run_one "子 agent ${name}" $(ls -tr "${PROJ_DIR}"/*/subagents/"${name}" 2>/dev/null)
  done < <(ls -t "${PROJ_DIR}"/*/subagents/agent-*.jsonl 2>/dev/null \
             | xargs -n1 basename | awk '!seen[$0]++' | head -n "${LIMIT}")
fi
