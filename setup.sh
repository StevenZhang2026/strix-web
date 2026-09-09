#!/usr/bin/env bash
# =============================================================================
# Strix Web 控制台 —— 环境校验与 .env 生成
#
# 范围：环境校验 + 生成 .env + 签发 TLS 自签证书（T0 + T4）。
#   沙箱镜像预拉（带进度）由 T11 追加。
#
# 本脚本绝不接受、绝不写入任何 API Key。密钥只在网页界面运行时输入。
# 本脚本也**绝不**自动修改系统信任库 —— 证书导入是用户的显式动作，只打印指引。
#
# 可用环境变量覆盖的输入（全部可选）：
#   CONSOLE_WEB_PORT=8443     换掉对外的 https 端口（默认 443，只绑 127.0.0.1）
#   STRIX_EXTRA_CA_FILE=/abs/corp-ca.pem   启用 N2 企业 CA（默认关闭）
#   STRIX_EXTRA_CA_FILE=      （显式给空）关掉已启用的 N2 企业 CA
#   STRIX_REGEN_CERT=1        显式要求重签 TLS 证书（默认永不覆盖已有证书）
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd -P)"
cd "$REPO_ROOT"

BLOCKED=0

# ---------------------------------------------------------------------------
# 最小主机要求 —— 唯一真源，C17 / C17b 都引用这里的常量
#
# 内存：分母是 Docker **VM** 的配额（docker info 的 MemTotal），不是宿主物理内存。
#   沙箱下限 2048 MB（浏览器 + Caido 抓包代理 + 各类扫描器同时在跑）
#   + api 容器约 512 MB + VM 自身开销 → 4096 MB 是能跑起来的绝对底线。
#   宿主物理内存还需 ≥ VM 配额 + 2 GB，因为浏览器和 Docker Desktop 本体在 VM 外。
#
# CPU：沙箱下限 2 核。VM 只有 2 核时沙箱会吃满，api 与 daemon 抢不到 → 2 核阻断、
#   4 核以下警告。
#
# 磁盘：实测的压缩下载量 —— strix-sandbox:1.3.0 为 1.31 GB / 35 层，
#   python:3.12-slim 为 45 MB。压缩→落盘的倍率参照本机 python:3.11-slim
#   （45 MB → 215 MB，约 4.8×），故沙箱落盘估 4–6 GB、api 镜像约 1–1.5 GB、
#   juice-shop 靶场约 1 GB → 镜像合计约 8 GB。再加扫描产物（agents.db + 截图
#   media/ + transcript，单次量级待 M0 实测）。
#   → 10 GB 阻断、20 GB 警告。
# ---------------------------------------------------------------------------
REQ_VM_MEM_MB=4096      # Docker VM 内存阻断线
REC_VM_MEM_MB=8192      # Docker VM 内存推荐值
REQ_VM_CPUS=2           # Docker VM 核数阻断线
REC_VM_CPUS=4           # Docker VM 核数推荐值
REQ_DISK_GB=10          # 数据目录所在卷可用空间阻断线
REC_DISK_GB=20          # 数据目录所在卷可用空间推荐值
REQ_SANDBOX_MEM_MB=2048 # 单个沙箱容器的内存下限
REQ_SANDBOX_CPUS=2      # 单个沙箱容器的核数下限

# TLS 自签证书（PLAN.md §已确认决策「传输与证书」是硬约束，改前先问）
CERT_DAYS=3650          # 10 年
CERT_MIN_DAYS_LEFT=30   # 剩余天数低于此值即判定证书需要重签

# 控制台登录口令的长度下限（C17f）。
# 与 backend/app/services/auth.py 的 MIN_PASSWORD_LENGTH 是同一个数字，**两处都要有**：
# 这里是为了让用户当场重输（体验），那里是为了让"绕过本脚本直接调那个模块"也拦得住
# （不变量）。两边不一致时坏的方式是良性的 —— 容器侧会拒绝，本脚本会如实报错。
AUTH_MIN_PASSWORD_LEN=12

die()  { printf '\033[31m✗ 阻断\033[0m  %s\n' "$1" >&2; BLOCKED=1; }
fatal() { printf '\033[31m✗ 阻断\033[0m  %s\n' "$1" >&2; exit 1; }
warn() { printf '\033[33m! 警告\033[0m  %s\n' "$1" >&2; }
ok()   { printf '\033[32m✓\033[0m       %s\n' "$1"; }
info() { printf '        %s\n' "$1"; }

# 从 backend/Dockerfile 抠出基础镜像 digest 当探测镜像 —— 单一真源，且探测顺手把
# 构建要用的镜像拉好，零额外拉取。
PROBE_IMAGE="$(grep -oE 'python:3\.12-slim@sha256:[0-9a-f]{64}' backend/Dockerfile | head -1 || true)"
[ -n "$PROBE_IMAGE" ] || fatal "无法从 backend/Dockerfile 解析基础镜像 digest，仓库可能不完整。"

echo "== Strix Web 控制台 环境校验 =="
echo
# 先把门槛摆出来，再逐项检查 —— 让"我这台机器够不够"在第一屏就有答案，而不是
# 等某一条阻断信息里才第一次出现数字。
cat <<REQEOF
最小主机要求（阻断线 / 推荐值）：
  Docker VM 内存      ≥ $(( REQ_VM_MEM_MB / 1024 )) GB  / 推荐 ≥ $(( REC_VM_MEM_MB / 1024 )) GB
  Docker VM CPU       ≥ ${REQ_VM_CPUS} 核 / 推荐 ≥ ${REC_VM_CPUS} 核
  数据目录可用空间    ≥ ${REQ_DISK_GB} GB / 推荐 ≥ ${REC_DISK_GB} GB
  宿主物理内存        ≥ Docker VM 配额 + 2 GB
  操作系统            macOS 或 Linux，且为 Docker Desktop（Windows 需在 WSL2 内）
注：VM 内存指 Docker Desktop 分给虚拟机的配额，不是宿主物理内存。
    单个沙箱容器至少要 ${REQ_SANDBOX_MEM_MB} MB 内存 / ${REQ_SANDBOX_CPUS} 核，限额由本脚本按实测配额算出。

REQEOF

# ---------------------------------------------------------------------------
# C1–C5：工具链
# ---------------------------------------------------------------------------
# v1 支持范围：macOS + Linux，且都要求 Docker Desktop。
#   Windows 原生不支持 —— 同路径挂载在 C:\ 形态下不成立（compose 用冒号分隔卷，
#   且 Linux 容器里不可能存在名为 C:\Users\x 的挂载点）。Windows 用户请在 WSL2
#   里克隆并运行，此时宿主路径是 /home/...，等价于 Linux。
#   Docker Engine（无 Desktop）暂不支持，后续扩展 —— 差异点见下方阻断信息。
case "$(uname -s)" in
  Darwin) OS_KIND=macos; ok "操作系统 macOS $(sw_vers -productVersion 2>/dev/null || echo '')" ;;
  Linux)  OS_KIND=linux; ok "操作系统 Linux $(uname -r)" ;;
  *)      fatal "不支持的操作系统：$(uname -s)。v1 只支持 macOS 与 Linux。Windows 请在 WSL2 内运行（原生 Windows 路径含冒号，同路径挂载不成立）。" ;;
esac

command -v docker >/dev/null 2>&1 \
  || fatal "未找到 docker 命令。请先安装并启动 Docker Desktop。"

# 一次 docker info 取全部需要的字段（这个调用不便宜，不要反复调）。
DOCKER_INFO="$(docker info --format '{{.ServerVersion}}|{{.NCPU}}|{{.MemTotal}}|{{.OSType}}|{{.OperatingSystem}}' 2>/dev/null || true)"
[ -n "$DOCKER_INFO" ] \
  || fatal "无法连接 Docker 守护进程。请启动 Docker Desktop，等鲸鱼图标停止转动后重试。"

IFS='|' read -r D_VER D_NCPU D_MEM D_OSTYPE D_OS <<EOF
$DOCKER_INFO
EOF
ok "Docker 守护进程可达（${D_VER}，${D_OS}）"

# v1 只支持 Docker Desktop。Docker Engine 的差异点是实打实的，不是换个提示就行：
#   ① 没有 File sharing 概念，C16 的探测语义不同
#   ② 没有 host.docker.internal，compose 必须加 extra_hosts: host-gateway
#   ③ docker.sock 属主是 root:docker，GID 处理方式不同
# 与其现在写一套没法测的分支，不如明确阻断。这三条已记进 PLAN.md 待扩展项。
case "$D_OS" in
  *"Docker Desktop"*) ok "Docker Desktop 已确认" ;;
  *) fatal "检测到非 Docker Desktop 环境（${D_OS}）。v1 暂只支持 Docker Desktop；Docker Engine 需要额外处理 File sharing 探测、host.docker.internal 与 docker.sock 属组，尚未实现与测试。" ;;
esac

[ "$D_OSTYPE" = "linux" ] \
  || fatal "Docker 守护进程的容器类型是 ${D_OSTYPE}，需要 linux。若在 Windows 上，请切换到 Linux 容器模式并改用 WSL2 运行本项目。"

docker compose version >/dev/null 2>&1 \
  || fatal "未找到 docker compose 插件（v2 及以上）。请升级 Docker Desktop。"
ok "Compose $(docker compose version --short)"

# 符号链接算通过：宿主上它通常指向 ~/.docker/run/docker.sock。
[ -e /var/run/docker.sock ] \
  || fatal "未找到 /var/run/docker.sock。请在 Docker Desktop → Settings → Advanced 中勾选 \"Allow the default Docker socket to be used\"，然后重启 Docker Desktop。Strix 需要它来创建沙箱容器。"
ok "/var/run/docker.sock 存在"

echo

# ---------------------------------------------------------------------------
# C6：取值 —— 命令行参数 → 已有 .env → 交互提示
# ---------------------------------------------------------------------------
DATA_DIR="${1:-}"
if [ -z "$DATA_DIR" ] && [ -f .env ]; then
  DATA_DIR="$(sed -n 's/^STRIX_HOST_DATA_DIR=//p' .env | head -1)"
  [ -n "$DATA_DIR" ] && info "沿用已有 .env 中的路径：$DATA_DIR"
fi
if [ -z "$DATA_DIR" ]; then
  DEFAULT_DIR="$HOME/strix-console-data"
  printf '扫描数据目录（宿主绝对路径）[%s]: ' "$DEFAULT_DIR"
  read -r DATA_DIR || true
  DATA_DIR="${DATA_DIR:-$DEFAULT_DIR}"
fi

# ---------------------------------------------------------------------------
# C7–C13：纯字符串校验（不碰文件系统）
# ---------------------------------------------------------------------------
case "$DATA_DIR" in
  /*) ;;
  *) fatal "数据目录必须是绝对路径，当前为 \"$DATA_DIR\"。同路径挂载要求容器内外字面一致，相对路径无法满足。" ;;
esac

case "$DATA_DIR" in
  *:*) fatal "数据目录路径不能包含冒号。compose 的卷挂载语法用冒号分隔宿主路径与容器路径，含冒号的路径会被解析成完全不同的挂载。" ;;
esac

case "$DATA_DIR" in
  *[[:space:]]*) warn "数据目录路径含空格。虽然加引号能工作，但 Strix 会把该路径拼进传给 docker daemon 的参数，空格是已知的静默故障源，建议换成不含空格的路径。" ;;
esac

# Strix 自身的 check_mountable_dir 会拒绝挂载这些目录。
for blocked in "$HOME/.config" "$HOME/.ssh" "$HOME/.aws" "$HOME/.docker" "$HOME/.kube"; do
  case "$DATA_DIR" in
    "$blocked"|"$blocked"/*)
      die "数据目录不能位于 $blocked 之下。Strix 自身的 check_mountable_dir 会拒绝挂载这些目录，届时报错会出现在扫描中途、信息含糊。建议改为 \$HOME/strix-console-data。" ;;
  esac
done

# macOS 会定期清理这些位置。已有实证：/tmp/strix_src 下的文件被清空，只剩空目录树。
for vol in /tmp /private/tmp /var/folders; do
  case "$DATA_DIR" in
    "$vol"|"$vol"/*)
      die "数据目录不能放在 ${vol}：macOS 会定期清理这些位置，扫描历史、SQLite 数据库和截图会静默消失。" ;;
  esac
done

case "$DATA_DIR" in
  "$REPO_ROOT"|"$REPO_ROOT"/*)
    die "数据目录不能位于项目仓库 $REPO_ROOT 内。扫描产物含目标信息、截图与报告，放进仓库随时可能被误提交。" ;;
esac

case "$DATA_DIR" in
  /|"$HOME"|/Users|/Volumes)
    die "数据目录不能是 $DATA_DIR 这样的大目录：它会被读写挂载进一个持有 NET_ADMIN 的自主 agent 容器。请指定一个专用子目录。" ;;
esac

[ "$BLOCKED" -eq 0 ] || exit 1
ok "路径形态校验通过"

# ---------------------------------------------------------------------------
# C14–C15：创建并规范化
# ---------------------------------------------------------------------------
mkdir -p "$DATA_DIR" "$DATA_DIR/scans" "$DATA_DIR/tmp" \
  || fatal "无法创建数据目录 ${DATA_DIR}（权限不足或路径被占用）。"
chmod 700 "$DATA_DIR"

REAL_DIR="$(cd "$DATA_DIR" && pwd -P)"
[ "$REAL_DIR" = "$DATA_DIR" ] \
  || fatal "数据目录含符号链接，真实路径是 ${REAL_DIR}。同路径挂载要求两侧字面完全一致，请直接填写 ${REAL_DIR}。"
ok "数据目录就绪：$DATA_DIR"

# ---------------------------------------------------------------------------
# C16：同路径挂载主动探测 —— 这是唯一权威的判定方式
#
# 不能靠读 Docker Desktop 的 settings-store.json 来枚举 File sharing 列表：
# 实测该文件只有 303 字节、根本没有 filesharingDirectories 键（只存非默认项）。
# 详见 pitfalls/history-pitfalls.md 条 2。
# ---------------------------------------------------------------------------
PROBE_FILE="$DATA_DIR/.setup-probe.$$"
PROBE_TOKEN="strix-console-probe-$$"
printf '%s' "$PROBE_TOKEN" > "$PROBE_FILE"
# shellcheck disable=SC2064
trap "rm -f '$PROBE_FILE'" EXIT

info "正在探测同路径挂载（首次会拉取 python:3.12-slim，需要几分钟）…"
PROBE_OUT="$(docker run --rm -v "$DATA_DIR:$DATA_DIR" "$PROBE_IMAGE" cat "$PROBE_FILE" 2>/dev/null || true)"
if [ "$PROBE_OUT" != "$PROBE_TOKEN" ]; then
  fatal "Docker Desktop 未共享该路径，或同路径挂载不可用。请在 Docker Desktop → Settings → Resources → File sharing 中加入 ${DATA_DIR}（或其父目录）后重试。默认已共享的根目录是 /Users、/Volumes、/private、/tmp。"
fi
rm -f "$PROBE_FILE"
trap - EXIT
ok "同路径挂载可用（容器内 $DATA_DIR 与宿主一致）"

# ---------------------------------------------------------------------------
# C17：磁盘余量
# ---------------------------------------------------------------------------
# df -g 是 macOS 专有；-Pk 是 POSIX，macOS 与 GNU 都支持，且 -P 保证不折行。
AVAIL_GB="$(df -Pk "$DATA_DIR" | awk 'NR==2 {printf "%d", $4/1024/1024}')"
if [ "${AVAIL_GB:-0}" -lt "$REQ_DISK_GB" ]; then
  fatal "数据目录所在卷可用空间只有 ${AVAIL_GB} GB，低于阻断线 ${REQ_DISK_GB} GB。仅镜像就约需 8 GB（沙箱镜像 1.31 GB 压缩、落盘 4–6 GB；api 镜像约 1–1.5 GB；靶场约 1 GB），再加扫描产物。空间不足时的表现是构建或扫描中途失败，报错指向别处。"
elif [ "$AVAIL_GB" -lt "$REC_DISK_GB" ]; then
  warn "数据目录所在卷可用空间 ${AVAIL_GB} GB，低于推荐值 ${REC_DISK_GB} GB。能跑，但 run 目录与截图堆积几轮后就会告急，建议先清理。"
else
  ok "可用空间 ${AVAIL_GB} GB"
fi

# ---------------------------------------------------------------------------
# C17b：按宿主实际资源算沙箱限额
#
# 为什么必须算而不能硬编码：部署机可能是小笔记本也可能是大服务器。沙箱内存超过
# Docker VM 容量时，容器会被 OOM kill，而 Strix 的报错会指向"agent 崩了"而不是
# "内存不够"，极难归因。宁可在这里用一个数字挡住。
#
# 注意分母是 Docker **VM** 的内存（docker info 的 MemTotal），不是宿主物理内存 ——
# Docker Desktop 下 VM 只拿到宿主的一部分。
# ---------------------------------------------------------------------------
VM_MEM_MB=$(( D_MEM / 1048576 ))
info "Docker VM 配额：${D_NCPU} 核 / $(( VM_MEM_MB / 1024 )) GB"

if [ "$VM_MEM_MB" -lt "$REQ_VM_MEM_MB" ]; then
  fatal "Docker VM 只有 $(( VM_MEM_MB / 1024 )) GB 内存，低于阻断线 $(( REQ_VM_MEM_MB / 1024 )) GB（沙箱下限 ${REQ_SANDBOX_MEM_MB} MB + api 容器约 512 MB + VM 开销）。请在 Docker Desktop → Settings → Resources → Memory 中提高到至少 $(( REQ_VM_MEM_MB / 1024 )) GB（推荐 $(( REC_VM_MEM_MB / 1024 )) GB）。"
fi
[ "$VM_MEM_MB" -lt "$REC_VM_MEM_MB" ] \
  && warn "Docker VM 内存 $(( VM_MEM_MB / 1024 )) GB 低于推荐值 $(( REC_VM_MEM_MB / 1024 )) GB。能跑，但沙箱只能分到 $(( VM_MEM_MB / 2 )) MB，深度扫描时浏览器可能被 OOM kill。"

if [ "$D_NCPU" -lt "$REQ_VM_CPUS" ]; then
  fatal "Docker VM 只有 ${D_NCPU} 核，低于阻断线 ${REQ_VM_CPUS} 核（沙箱下限 ${REQ_SANDBOX_CPUS} 核）。请在 Docker Desktop → Settings → Resources → CPUs 中提高。"
fi
[ "$D_NCPU" -lt "$REC_VM_CPUS" ] \
  && warn "Docker VM 只有 ${D_NCPU} 核（推荐 ≥ ${REC_VM_CPUS} 核）。沙箱会拿走全部 ${REQ_SANDBOX_CPUS} 核，api 容器与 daemon 只能和它抢，扫描期间界面会卡。"

# 沙箱拿 VM 内存的一半，下限 REQ_SANDBOX_MEM_MB。留一半给 api 容器、Strix 自身与 VM 开销。
SANDBOX_MEM_MB=$(( VM_MEM_MB / 2 ))
[ "$SANDBOX_MEM_MB" -lt "$REQ_SANDBOX_MEM_MB" ] && SANDBOX_MEM_MB="$REQ_SANDBOX_MEM_MB"

# CPU 拿一半，下限 REQ_SANDBOX_CPUS。沙箱是 CPU 密集的（浏览器 + Caido + 各类扫描器）。
SANDBOX_CPUS=$(( D_NCPU / 2 ))
[ "$SANDBOX_CPUS" -lt "$REQ_SANDBOX_CPUS" ] && SANDBOX_CPUS="$REQ_SANDBOX_CPUS"

# /dev/shm 给 Chromium 用。取沙箱内存的四分之一，上限 1 GB —— 小机器上给 1g
# 会挤掉浏览器真正需要的堆空间。
SANDBOX_SHM_MB=$(( SANDBOX_MEM_MB / 4 ))
[ "$SANDBOX_SHM_MB" -gt 1024 ] && SANDBOX_SHM_MB=1024

ok "沙箱限额：内存 ${SANDBOX_MEM_MB}m / CPU ${SANDBOX_CPUS} / shm ${SANDBOX_SHM_MB}m"

echo

# ---------------------------------------------------------------------------
# C17c：对外 https 端口 —— 只查不占
#
# 编号跟着执行顺序走（C17c/C17d/C17e 是 T4 插进来的），刻意不重排 C18 及之后 ——
# PLAN.md 与 pitfalls 里都按编号引用了 C16/C17/C17b/C18，重排会让那些引用全错。
#
# 为什么必须查：compose 里 nginx 是唯一发布端口的服务，端口被占时 `docker compose
# up` 的报错是 daemon 抛出的 "address already in use"，看不出是谁占的。
# 只查不占 —— 绝不去 kill 任何进程。
# ---------------------------------------------------------------------------
WEB_PORT="${CONSOLE_WEB_PORT:-}"
if [ -z "${WEB_PORT}" ] && [ -f .env ]; then
  WEB_PORT="$(sed -n 's/^CONSOLE_WEB_PORT=//p' .env | head -1)"
fi
WEB_PORT="${WEB_PORT:-443}"
case "${WEB_PORT}" in
  ''|*[!0-9]*) fatal "CONSOLE_WEB_PORT 必须是纯数字，当前为 \"${WEB_PORT}\"。" ;;
esac
if [ "${WEB_PORT}" -lt 1 ] || [ "${WEB_PORT}" -gt 65535 ]; then
  fatal "CONSOLE_WEB_PORT 必须在 1–65535 之间，当前为 ${WEB_PORT}。"
fi

# 我们只绑 127.0.0.1，所以只有监听在 *、0.0.0.0、127.0.0.1、::、::1 上的进程会冲突；
# 监听在某张具体网卡地址上的进程不冲突，不该误报。
PORT_HOLDERS=""
if command -v lsof >/dev/null 2>&1; then
  PORT_HOLDERS="$(lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null \
    | awk -v p="${WEB_PORT}" 'NR>1 && $9 ~ ":"p"$" {print $9" ← "$1" (pid "$2")"}' || true)"
elif command -v ss >/dev/null 2>&1; then
  PORT_HOLDERS="$(ss -ltnH 2>/dev/null \
    | awk -v p="${WEB_PORT}" '$4 ~ ":"p"$" {print $4}' || true)"
else
  warn "本机既没有 lsof 也没有 ss，跳过 ${WEB_PORT} 端口占用检查。若 docker compose up 报 \"address already in use\"，那就是这个端口被占了。"
fi
PORT_CONFLICT="$(printf '%s\n' "${PORT_HOLDERS}" \
  | grep -E '^(\*|0\.0\.0\.0|127\.0\.0\.1|localhost|\[::\]|\[::1\]):' || true)"
if [ -n "${PORT_CONFLICT}" ]; then
  # 本项目自己的 nginx 在跑不算冲突 —— 否则装好之后就再也不能重跑 setup.sh 了。
  OUR_NGINX="$(docker compose -p strix-console ps --services --status running 2>/dev/null \
    | grep -cx nginx || true)"
  if [ "${OUR_NGINX:-0}" -ge 1 ]; then
    info "端口 ${WEB_PORT} 由本项目的 nginx 占用，属正常情况。"
  else
    printf '%s\n' "${PORT_CONFLICT}" | while IFS= read -r l; do info "  ${l}"; done
    fatal "端口 ${WEB_PORT} 已被占用（见上）。本脚本不会去停任何进程 —— 请自己停掉它，或用 CONSOLE_WEB_PORT=8443 ./setup.sh 换一个端口（届时访问地址是 https://localhost:8443）。"
  fi
else
  ok "端口 ${WEB_PORT} 未被占用"
fi

# ---------------------------------------------------------------------------
# C17d：TLS 自签证书
#
# 硬约束（PLAN.md §已确认决策「传输与证书」）：单张自签、**SAN + EKU=serverAuth**、
# 10 年、私钥 0600。SAN 和 EKU 都不是"保险起见" —— 缺 SAN 时 Chrome 报
# ERR_CERT_COMMON_NAME_INVALID，缺 EKU 时同样直接拒连，TLS 等于白做（验收 #22）。
#
# 已有证书时的行为，三条，都是刻意的：
#   1) 不存在        → 签发。此时没有任何已导入的信任会被打破。
#   2) 存在且合法    → **原样保留，绝不覆盖**。覆盖会让用户已经 security
#                      add-trusted-cert 过的信任突然失效，而浏览器只会说"不安全"，
#                      没人能把这个现象联想到"我刚跑了一次 setup.sh"。
#   3) 存在但不可用（过期 / 快过期 / 缺 SAN / 缺 EKU / 私钥不配对）→ **阻断**，
#                      打印原因和重签命令，仍然不自动覆盖。理由同 2：继续跑下去的
#                      结果是浏览器拒连，那也是失败；在这里失败至少说清了为什么。
#                      重签要显式：STRIX_REGEN_CERT=1 ./setup.sh（会先备份旧文件）。
#
# basicConstraints 刻意是 CA:FALSE：这张证书会被用户导入成信任锚点，如果给它
# CA:TRUE，那么谁拿到 key.pem 就能给**任意域名**签出被这台机器信任的证书 ——
# 那正是本项目在 pitfalls 条 15 里否掉企业 CA 烧进镜像的同一个理由。CA:FALSE 下
# 泄露 key.pem 的后果被限制在"冒充 localhost"。
# 现代 Chrome/Safari 对"手动信任的自签叶证书"走 trusted-leaf 路径，CA:FALSE 可用。
# ---------------------------------------------------------------------------
command -v openssl >/dev/null 2>&1 \
  || fatal "未找到 openssl 命令，无法签发 TLS 证书。macOS 自带 LibreSSL；Linux 请安装 openssl 包。"
# -addext 是 OpenSSL 1.1.1+ / LibreSSL 3.x 才有的。没有它就没法在自签证书上写 SAN
# 和 EKU（改用临时 openssl.cnf 也行，但那是一条谁都不会去测的分支）。
#
# `|| true` 不是偷懒：macOS 的 LibreSSL 3.3.6 把 `req -help` 当用法错误处理，
# **退出码 1**（已实测）。`set -o pipefail` 会把这个 1 带出整条管道，于是"检出了
# -addext"反而判成"不支持"。踩过一次，别再删。
{ openssl req -help 2>&1 || true; } | grep -q -- '-addext' \
  || fatal "本机 openssl 太旧，不支持 req -addext（$(openssl version)）。没有它就写不进 SAN 与 EKU=serverAuth，Chrome 会直接拒连。请升级 openssl。"

TLS_DIR="${DATA_DIR}/tls"
CERT_FILE="${TLS_DIR}/cert.pem"
KEY_FILE="${TLS_DIR}/key.pem"
mkdir -p "${TLS_DIR}" || fatal "无法创建 ${TLS_DIR}。"
chmod 700 "${TLS_DIR}"

CERT_STATE=missing
CERT_REASON=""
if [ -e "${CERT_FILE}" ] || [ -e "${KEY_FILE}" ]; then
  CERT_STATE=broken
  if [ ! -s "${CERT_FILE}" ] || [ ! -s "${KEY_FILE}" ]; then
    CERT_REASON="cert.pem 与 key.pem 必须成对存在且非空，当前只有其中一个（或为空文件）"
  elif ! openssl x509 -in "${CERT_FILE}" -noout >/dev/null 2>&1; then
    CERT_REASON="cert.pem 不是合法的 X.509 证书"
  elif [ "$(openssl x509 -in "${CERT_FILE}" -noout -pubkey 2>/dev/null | openssl sha256)" \
       != "$(openssl pkey -in "${KEY_FILE}" -pubout 2>/dev/null | openssl sha256)" ]; then
    CERT_REASON="key.pem 与 cert.pem 不是一对（公钥不一致），nginx 会拒绝启动"
  elif ! openssl x509 -in "${CERT_FILE}" -noout -text 2>/dev/null | grep -q 'DNS:localhost'; then
    CERT_REASON="证书缺少 SAN（需要 DNS:localhost）—— Chrome 会直接拒连"
  elif ! openssl x509 -in "${CERT_FILE}" -noout -text 2>/dev/null \
       | grep -q 'TLS Web Server Authentication'; then
    CERT_REASON="证书缺少 EKU=serverAuth —— Chrome 会直接拒连"
  elif ! openssl x509 -in "${CERT_FILE}" -noout \
       -checkend "$(( CERT_MIN_DAYS_LEFT * 86400 ))" >/dev/null 2>&1; then
    CERT_REASON="证书已过期或将在 ${CERT_MIN_DAYS_LEFT} 天内过期（$(openssl x509 -in "${CERT_FILE}" -noout -enddate 2>/dev/null | sed 's/^notAfter=/notAfter /')）"
  else
    CERT_STATE=ok
  fi
fi

if [ "${STRIX_REGEN_CERT:-0}" = "1" ] && [ "${CERT_STATE}" != "missing" ]; then
  CERT_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
  mv "${CERT_FILE}" "${CERT_FILE}.bak.${CERT_STAMP}"
  mv "${KEY_FILE}"  "${KEY_FILE}.bak.${CERT_STAMP}"
  warn "按 STRIX_REGEN_CERT=1 重签证书。旧文件已备份为 *.bak.${CERT_STAMP}（不删除）。旧证书如果已导入系统信任库，请按文末的删除命令手动移除 —— 本脚本不会去改你的信任库。"
  CERT_STATE=missing
fi

case "${CERT_STATE}" in
  ok)
    ok "TLS 证书已存在且合法，保持不变（notAfter $(openssl x509 -in "${CERT_FILE}" -noout -enddate | sed 's/^notAfter=//')）"
    ;;
  broken)
    fatal "${TLS_DIR} 下的 TLS 证书不可用：${CERT_REASON}。本脚本不会静默覆盖它（那会让你已导入的信任莫名失效）。确认要重签就跑：STRIX_REGEN_CERT=1 ./setup.sh"
    ;;
  missing)
    # 子 shell 里改 umask，别污染后续步骤。两个文件先都是 0600，再把公开的证书放宽。
    (
      umask 077
      # ⚠️ 下面那行 SAN 只含 localhost / 127.0.0.1 / ::1。这**不只是** Chrome 兼容性
      # 要求，它是一条安全控制：DNS rebinding（攻击者域名先解析到自己的服务器、再改
      # 成 127.0.0.1）在这里过不了 TLS 校验，因为证书对不上攻击者的域名。
      # **不许为了"方便"往里加本机主机名、`*.local` 或局域网 IP** —— 那会打开
      # rebinding 路径。真要局域网访问，先去 PLAN.md 改 §已确认决策 的部署形态。
      openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days "${CERT_DAYS}" \
        -keyout "${KEY_FILE}" -out "${CERT_FILE}" \
        -subj "/O=Strix Console/CN=localhost" \
        -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1" \
        -addext "basicConstraints=critical,CA:FALSE" \
        -addext "keyUsage=critical,digitalSignature,keyEncipherment" \
        -addext "extendedKeyUsage=serverAuth" \
        -addext "subjectKeyIdentifier=hash" \
        >/dev/null 2>&1
    ) || fatal "openssl 签发自签证书失败。"
    chmod 600 "${KEY_FILE}"
    chmod 644 "${CERT_FILE}"
    # 签完立刻回读验证，而不是"我写了这些参数所以它一定在里面"。
    openssl x509 -in "${CERT_FILE}" -noout -text | grep -q 'DNS:localhost' \
      || fatal "刚签发的证书里没有 SAN，本机 openssl 行为异常，请上报。"
    openssl x509 -in "${CERT_FILE}" -noout -text | grep -q 'TLS Web Server Authentication' \
      || fatal "刚签发的证书里没有 EKU=serverAuth，本机 openssl 行为异常，请上报。"
    ok "已签发 TLS 自签证书（SAN + EKU=serverAuth，${CERT_DAYS} 天，私钥 0600）"
    ;;
esac

CERT_FP256="$(openssl x509 -in "${CERT_FILE}" -noout -fingerprint -sha256 | sed 's/^.*=//')"
# security delete-certificate -Z 要的是不带冒号的大写 SHA-1。
CERT_FP1_PLAIN="$(openssl x509 -in "${CERT_FILE}" -noout -fingerprint -sha1 \
  | sed 's/^.*=//; s/://g')"
info "证书指纹 SHA-256：${CERT_FP256}"

# ---------------------------------------------------------------------------
# C17e：N2 —— 企业 CA bundle（可选，默认关闭）
#
# 边界（PLAN.md §N2，2026-09-08 拍板）：只允许**运行期由操作者显式挂载**；
# 构建期把任何 CA 烧进镜像永久禁止。本脚本**不自动探测、不自动填** ——
# 沉默就是关闭。
# ---------------------------------------------------------------------------
# 取值优先级：环境变量**是否被设置**（不是"是否非空"）> 已有 .env > 关闭。
# 用 ${VAR+x} 而不是 ${VAR:-} 判断，是为了让 `STRIX_EXTRA_CA_FILE= ./setup.sh`
# 成为**关掉 N2** 的正规手段 —— 否则一旦写进 .env 就只能手改文件才能关，
# 而"想关掉一个把凭据暴露给解密设备的开关时必须手动编辑配置文件"是坏设计。
if [ -n "${STRIX_EXTRA_CA_FILE+x}" ]; then
  EXTRA_CA="${STRIX_EXTRA_CA_FILE}"
elif [ -f .env ]; then
  EXTRA_CA="$(sed -n 's/^STRIX_EXTRA_CA_FILE=//p' .env | head -1)"
else
  EXTRA_CA=""
fi
CA_BUNDLE_IN_CONTAINER=""   # 空 = 用镜像自带的系统 bundle（compose 里的 :- 兜底）

if [ -n "${EXTRA_CA}" ]; then
  case "${EXTRA_CA}" in
    /*) ;;
    *) fatal "STRIX_EXTRA_CA_FILE 必须是宿主绝对路径，当前为 \"${EXTRA_CA}\"。" ;;
  esac
  case "${EXTRA_CA}" in
    *:*) fatal "STRIX_EXTRA_CA_FILE 不能含冒号（compose 用冒号分隔卷）。" ;;
  esac
  [ -f "${EXTRA_CA}" ] || fatal "STRIX_EXTRA_CA_FILE 指向的文件不存在：${EXTRA_CA}"
  grep -q 'BEGIN CERTIFICATE' "${EXTRA_CA}" \
    || fatal "${EXTRA_CA} 里没有 PEM 证书块。需要的是 PEM 格式的 CA bundle（-----BEGIN CERTIFICATE-----），不是 .der / .p12。"
  openssl x509 -in "${EXTRA_CA}" -noout >/dev/null 2>&1 \
    || fatal "${EXTRA_CA} 的第一段不是合法证书，openssl 解析失败。"
  # 容器内真读一次：既验 Docker Desktop 的 File sharing 覆盖了这个路径，
  # 也验挂进去之后内容确实在（挂载失败时 Docker 会静默给一个空目录/空文件）。
  docker run --rm -v "${EXTRA_CA}:/tmp/probe-ca.pem:ro" "${PROBE_IMAGE}" \
    grep -q 'BEGIN CERTIFICATE' /tmp/probe-ca.pem >/dev/null 2>&1 \
    || fatal "把 ${EXTRA_CA} 挂进容器后读不到证书内容。请确认它（或其父目录）已加入 Docker Desktop → Settings → Resources → File sharing。"
  CA_BUNDLE_IN_CONTAINER="/etc/strix/extra-ca.pem"
  warn "已启用企业 CA（N2）：${EXTRA_CA}"
  info "  这意味着你**接受**凭据与全部 LLM 流量明文经过企业解密设备一次。"
  info "  它只在运行期挂载，绝不进镜像。要关掉：STRIX_EXTRA_CA_FILE= ./setup.sh"
else
  ok "企业 CA（N2）未启用 —— 容器内用镜像自带的系统根证书"
fi

# compose 里 SSL_CERT_FILE 的兜底值是 /etc/ssl/certs/ca-certificates.crt。
# 这条断言把"镜像换了、这个路径没了"从静默故障（OpenSSL 加载 0 张根证书**且不报错**，
# 容器内所有 TLS 全部失去信任链）变成此处的硬失败。
docker run --rm "${PROBE_IMAGE}" test -s /etc/ssl/certs/ca-certificates.crt \
  || fatal "基础镜像里没有 /etc/ssl/certs/ca-certificates.crt。docker-compose.yml 用它当 SSL_CERT_FILE 的兜底值，缺失会让容器内 TLS 静默失去全部根证书。请同步修改 compose 里的兜底路径。"
ok "容器内系统根证书 bundle 存在"

echo

# ---------------------------------------------------------------------------
# C17f：控制台登录账号（T4b）
#
# 编号继续按执行顺序插在 C17e 之后，**刻意不重排 C18 及之后** ——
# PLAN.md 与 pitfalls 里都按编号引用，重排会让那些引用全错（同 C17c 的注释）。
#
# 三态机，与 C17d（TLS 证书）同一套语义：
#   1) 不存在        → 交互式创建（用户名 + 口令输两遍）
#   2) 存在且可解析  → 打一行 ok，**什么都不改，也不问口令**
#   3) 存在但坏了    → 阻断，打印原因和"手动删除后重跑"的指引
# **绝不静默覆盖**：覆盖等于把用户的登录口令悄悄换掉，而症状是"我的口令不管用了"，
# 没人会把它联想到"我刚跑了一次 setup.sh"。
#
# 口令散列必须在容器里算：宿主 Python 是 3.9.6 且链接 LibreSSL 2.8.3，
# `hasattr(hashlib, "scrypt")` 为 **False**；宿主 openssl 是 LibreSSL 3.3.6，
# 没有 `kdf` 子命令。也就是说**宿主根本算不了 scrypt**，容器不是为了整洁。
#
# 口令只经 **stdin** 进容器，绝不用 `docker run -e` 或 argv：
#   那两条路的值都会进**宿主** docker 客户端的进程参数，`ps aux` 全机可见；
#   而 `docker inspect` 还能从**已退出**的容器里读回 `Config.Cmd` / `Config.Env` ——
#   也就是说它不止暴露一瞬间，是留了个副本。
#   `printf` 是 shell 内建命令，所以管道左边这一段也不会出现在 `ps` 里。
#
# 散列由 `app/services/auth.py` 的 `python3 -m` 入口算，校验由**同一个模块**的
# `AuthRecord.load()` 做 —— 于是"坏了"的定义精确等于"api 进程读不出它"，
# 而不是本脚本另写一遍格式判断然后跟产品代码慢慢漂移。
# ---------------------------------------------------------------------------
AUTH_FILE="${DATA_DIR}/auth.json"

# 读文件权限位。macOS 是 BSD stat，Linux 是 GNU stat，参数完全不同。
auth_file_mode() {
  if [ "${OS_KIND}" = "macos" ]; then
    stat -f '%Lp' "$1"
  else
    stat -c '%a' "$1"
  fi
}

# 用产品自己的 loader 校验。成功时 stdout 只有用户名；失败时把原因留在输出里。
# `-i` 是必需的：`docker run` 不带 `-i` 时 stdin 不连通，heredoc 会静默空跑并退 0
# （pitfalls 条 29）。
auth_file_probe() {
  docker run --rm -i \
    -v "${REPO_ROOT}/backend:/work:ro" \
    -v "${AUTH_FILE}:${AUTH_FILE}:ro" \
    -w /work "${PROBE_IMAGE}" python3 - "${AUTH_FILE}" 2>&1 <<'AUTHPROBEEOF'
import pathlib
import sys

sys.path.insert(0, "/work")
from app.services.auth import AuthFileError, AuthRecord

try:
    print(AuthRecord.load(pathlib.Path(sys.argv[1])).username)
except AuthFileError as exc:
    sys.stderr.write(f"{exc}\n")
    sys.exit(1)
AUTHPROBEEOF
}

AUTH_STATE=missing
AUTH_REASON=""
AUTH_USERNAME=""
if [ -e "${AUTH_FILE}" ]; then
  AUTH_STATE=broken
  AUTH_MODE="$(auth_file_mode "${AUTH_FILE}")"
  if [ ! -s "${AUTH_FILE}" ]; then
    AUTH_REASON="文件为空"
  elif [ "${AUTH_MODE}" != "600" ]; then
    # 权限位不对也算"坏了"而不是"顺手 chmod 修好"：0644 的散列文件意味着同机的其它
    # 账号已经能读到它，改回 600 不能撤销那件事。要让人知道发生过什么。
    AUTH_REASON="权限位是 ${AUTH_MODE}，必须是 600（同机其它账号可能已经读过它）"
  elif ! AUTH_USERNAME="$(auth_file_probe)"; then
    AUTH_REASON="api 进程读不出它 —— ${AUTH_USERNAME}"
    AUTH_USERNAME=""
  else
    AUTH_STATE=ok
  fi
fi

case "${AUTH_STATE}" in
  ok)
    ok "控制台登录账号已存在（用户名 ${AUTH_USERNAME}），保持不变"
    info "  忘了口令？删掉 ${AUTH_FILE} 再跑一次 ./setup.sh 重设（这会作废旧口令）。"
    ;;
  broken)
    fatal "${AUTH_FILE} 不可用：${AUTH_REASON}。本脚本不会静默覆盖它（那等于悄悄换掉你的登录口令）。确认要重设账号就手动删除该文件，再跑一次 ./setup.sh。"
    ;;
  missing)
    [ -t 0 ] || fatal "还没有控制台登录账号，需要交互式创建，但当前 stdin 不是终端。请在终端里直接运行 ./setup.sh（不要用管道或 CI 任务）。"
    info "还没有控制台登录账号。现在创建一个 —— 之后打开控制台要用它登录。"
    info "  它只挡住「本机另一个账号顺手打开浏览器」和「浏览器里的其它标签页」。"
    info "  本机管理员能读进程内存与 docker.sock，登录页对他不构成边界。"

    AUTH_TRIES=0
    while : ; do
      AUTH_TRIES=$(( AUTH_TRIES + 1 ))
      [ "${AUTH_TRIES}" -le 5 ] || fatal "连续 5 次未能设置口令，已放弃。请重新运行 ./setup.sh。"

      printf '用户名 [admin]: '
      read -r AUTH_USER || fatal "读取用户名失败。"
      AUTH_USER="${AUTH_USER:-admin}"

      # `read -rs` 关回显。两次输入必须一致 —— 打错的口令会让人以为"登录坏了"。
      printf '口令（至少 %s 位，不回显）: ' "${AUTH_MIN_PASSWORD_LEN}"
      read -rs AUTH_PASS || fatal "读取口令失败。"
      printf '\n'
      printf '再输一次: '
      read -rs AUTH_PASS2 || fatal "读取口令失败。"
      printf '\n'

      if [ "${AUTH_PASS}" != "${AUTH_PASS2}" ]; then
        # 只说"不一致"，绝不回显任何一次输入的内容。
        warn "两次输入的口令不一致，请重来。"
        continue
      fi
      if [ "${#AUTH_PASS}" -lt "${AUTH_MIN_PASSWORD_LEN}" ]; then
        warn "口令至少要 ${AUTH_MIN_PASSWORD_LEN} 位，请重来。"
        continue
      fi
      break
    done

    # 子 shell 里改 umask，别污染后续步骤（同 C17d 的写法）。
    # 临时文件 + mv：中途失败（容器起不来、磁盘满）时不会留下半截的 auth.json ——
    # 那种文件下一次跑会被判成 broken，而真正的原因是上次写崩了。
    AUTH_TMP="${AUTH_FILE}.tmp.$$"
    if ! (
      umask 077
      printf '%s\n%s\n' "${AUTH_USER}" "${AUTH_PASS}" \
        | docker run --rm -i \
            -v "${REPO_ROOT}/backend:/work:ro" \
            -w /work "${PROBE_IMAGE}" python3 -m app.services.auth > "${AUTH_TMP}"
    ); then
      rm -f "${AUTH_TMP}"
      unset AUTH_PASS AUTH_PASS2
      fatal "计算口令散列失败（容器内 python3 -m app.services.auth 非零退出）。上面一行是它的错误信息。"
    fi
    unset AUTH_PASS AUTH_PASS2

    chmod 600 "${AUTH_TMP}"
    mv "${AUTH_TMP}" "${AUTH_FILE}"

    # 写完立刻回读验证，而不是"我按对的参数写了所以它一定对"（同 C17d 的回读）。
    AUTH_MODE="$(auth_file_mode "${AUTH_FILE}")"
    [ "${AUTH_MODE}" = "600" ] \
      || fatal "刚写出的 ${AUTH_FILE} 权限位是 ${AUTH_MODE} 而不是 600。请检查数据目录所在文件系统是否支持 Unix 权限位。"
    AUTH_USERNAME="$(auth_file_probe)" \
      || fatal "刚写出的 ${AUTH_FILE} 读不回来：${AUTH_USERNAME}。请上报。"
    grep -q '"hash_b64"' "${AUTH_FILE}" \
      || fatal "刚写出的 ${AUTH_FILE} 里没有 hash_b64 字段，格式异常，请上报。"
    ok "已创建控制台登录账号（用户名 ${AUTH_USERNAME}，scrypt 散列，0600）"
    info "  口令**只有散列**落盘，明文既没进磁盘也没进 docker 的参数与环境变量。"
    ;;
esac

echo

# ---------------------------------------------------------------------------
# C18：已有 .env 的密钥卫生 —— 发布阻断类不变式
# ---------------------------------------------------------------------------
if [ -f .env ]; then
  HITS="$(grep -nEi '(api[_-]?key|secret|token|password)[[:space:]]*=|=[[:space:]]*sk-' .env || true)"
  if [ -n "$HITS" ]; then
    echo "$HITS" | while IFS= read -r line; do
      info "  .env:${line%%:*}  $(printf '%s' "${line#*:}" | sed -E 's/=.*/=***已掩码***/')"
    done
    fatal "检测到 .env 中可能含密钥字段（见上）。本项目严禁把 API Key 写入 .env / compose / 镜像 —— 密钥只在运行时经网页界面输入，仅存活于内存与扫描子进程。请删除这些行后重试。"
  fi
  ok ".env 密钥卫生检查通过"
fi

# ---------------------------------------------------------------------------
# C19–C20：写出（不静默覆盖）
# ---------------------------------------------------------------------------
# 本脚本写出的 .env 只含路径与资源限额。绝不含任何密钥 —— 见文件头。
NEW_ENV="$(cat <<ENVEOF
# 由 ./setup.sh 生成，请勿手改。此文件绝不放任何 API Key。
STRIX_HOST_DATA_DIR=$DATA_DIR
STRIX_SANDBOX_MEM_LIMIT=${SANDBOX_MEM_MB}m
STRIX_SANDBOX_CPUS=$SANDBOX_CPUS
STRIX_SANDBOX_SHM_SIZE=${SANDBOX_SHM_MB}m
STRIX_SANDBOX_PIDS_LIMIT=2048
CONSOLE_WEB_PORT=${WEB_PORT}
STRIX_EXTRA_CA_FILE=${EXTRA_CA}
# 下面这行是**派生值**，由本脚本按 STRIX_EXTRA_CA_FILE 算出，不要手改。
# 改了上面那行之后必须重跑 ./setup.sh，否则 CA 挂进去了但没有变量指向它。
CONSOLE_CA_BUNDLE=${CA_BUNDLE_IN_CONTAINER}
ENVEOF
)"
if [ -f .env ] && [ "$(cat .env)" != "$NEW_ENV" ]; then
  BAK=".env.bak.$(date -u +%Y%m%dT%H%M%SZ)"
  cp .env "$BAK"
  info "已有 .env 备份到 $BAK"
fi
printf '%s\n' "$NEW_ENV" > .env
chmod 600 .env
ok "已写出 .env（0600）"

# ---------------------------------------------------------------------------
# C21：compose 自检 —— stderr 必须为空（警告也算失败）
# ---------------------------------------------------------------------------
CFG_ERR="$(docker compose config 2>&1 >/dev/null || true)"
if [ -n "$CFG_ERR" ]; then
  printf '%s\n' "$CFG_ERR" >&2
  fatal "compose 配置校验未通过（或产生了警告，见上）。T0 的验收要求 \`docker compose config\` 零警告。"
fi
ok "docker compose config 零警告"

# ---------------------------------------------------------------------------
# C22：下一步 + 证书信任导入指引
#
# 只**打印**指引，绝不代替用户执行 —— 自动往系统信任库里塞证书是不可接受的越界
# 行为（那等于替用户决定信任一个新的锚点）。所以下面全是给人抄的命令。
# ---------------------------------------------------------------------------
if [ "${WEB_PORT}" = "443" ]; then
  CONSOLE_URL="https://localhost"
else
  CONSOLE_URL="https://localhost:${WEB_PORT}"
fi

cat <<NEXT

== 校验全部通过。下一步 ==

  1) 构建 api 镜像
       docker compose build api
  2) 启动（nginx 是唯一发布端口的服务，只绑 127.0.0.1:${WEB_PORT}）
       docker compose up -d
  3) 浏览器访问
       ${CONSOLE_URL}

== 让浏览器信任这张自签证书（手动，一次性）==

证书  ${CERT_FILE}
私钥  ${KEY_FILE}   （0600，绝不要复制或分享它）
指纹  SHA-256 ${CERT_FP256}

不导入的后果：每次访问都是"不安全"警告页，人会被训练成无脑点过 —— 那就等于没上 TLS。

NEXT

if [ "${OS_KIND}" = macos ]; then
  cat <<NEXT
macOS（会要求输入你的管理员密码；这条命令由你执行，本脚本不会代劳）：
     sudo security add-trusted-cert -d -r trustRoot \\
       -k /Library/Keychains/System.keychain "${CERT_FILE}"

  以后想撤销信任 / 换证书前清理旧的：
     sudo security delete-certificate -Z ${CERT_FP1_PLAIN} \\
       /Library/Keychains/System.keychain
NEXT
else
  cat <<NEXT
Linux（系统信任库 + Chrome 自己的 NSS 库，两处都要）：
     sudo cp "${CERT_FILE}" /usr/local/share/ca-certificates/strix-console.crt
     sudo update-ca-certificates
     # Chrome/Chromium 不读 /etc/ssl/certs，它用自己的 NSS 库：
     certutil -d sql:\${HOME}/.pki/nssdb -A -t "P,," -n strix-console -i "${CERT_FILE}"

  以后想撤销：
     sudo rm /usr/local/share/ca-certificates/strix-console.crt && sudo update-ca-certificates
     certutil -d sql:\${HOME}/.pki/nssdb -D -n strix-console
NEXT
fi

cat <<'NEXT'

说明：
  · 本脚本从不接受 API Key。密钥在网页界面运行时输入，只存活于内存与扫描子进程。
  · 本脚本从不修改系统信任库、从不 kill 任何进程。
  · 证书已存在时**不会**被覆盖。确需重签：STRIX_REGEN_CERT=1 ./setup.sh（旧文件会备份）。
  · nginx → api 之间走 Docker 桥网，是**明文**的。这是已知且接受的残余风险，
    详见 docs/SECURITY-zh.md。
NEXT
