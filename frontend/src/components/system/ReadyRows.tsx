"use client";

import { useQuery } from "@tanstack/react-query";

import { Rows, type StatusRow } from "@/components/ui/Rows";
import { type DotTone } from "@/components/ui/StatusDot";
import { fetchSession, fetchSystemStatus, type SystemStatusSummary } from "@/lib/api/client";
import { t } from "@/lib/messages";

/**
 * 首页侧栏「本机就绪状态」那几行的**真值**。客户端叶子，`Panel` 与标题仍在服务端页面上。
 *
 * =============================================================================
 * 一、三态，且 `null` 绝不画绿（T3 的硬要求）
 *
 * 后端每个 `bool | None` 字段的 `None` 意思是"**我没能确认**"，不是"通过"
 * （`services/system_status.py` 三条总则的第一条）。所以这里只有一条判定：
 * **只有 `true` 才是 `ok`**，`false` 是 `warn`，`null` 与"还没拿到响应"一律 `idle`。
 * 反过来写（`!== false` 当成通过）会得到一个在真的不就绪时也说就绪的面板，
 * 那比没有这个面板更糟。
 *
 * `false` 用 `warn`（方块）而不是新加一档红色：`StatusDot.module.css` 里方块的语义
 * 正是"需要你处理"，而一条阻断项要的就是人去修 —— 为它引一档新颜色，等于让形状和
 * 颜色各说一遍同一件事，还多一个 token。
 *
 * 值那一列刻意分成三种话，而不是复用同一句：
 *   · 还没请求 → 「尚未检测」（未登录时也是这一档，见第二节）
 *   · 请求失败 → 「未知」（我们试过了，没得到答案 —— 与"还没试"是两件事）
 *   · 拿到 `null` → 「未知」（后端试过了，没得到答案）
 *
 * =============================================================================
 * 二、为什么要先看会话
 *
 * `/api/system/status` 是全站第一个**需要身份**的接口（`routes/system.py` 第 1 条：
 * 它的正文是一份侦察报告，而且会起一个容器）。未登录直接打它会 401 →
 * `lib/api/client.ts` 通报会话失效 → `SessionExpiredMask` 弹出，而
 * `RequireSession` 见到 `expired` 就**刻意不再跳转**（它让位给遮罩）。
 * 于是"没登录就进首页"会从"静默跳登录页"退化成"停在首页看一个遮罩"。
 *
 * 所以这里 `enabled` 挂在 `/api/auth/me` 的结果上。那个查询与 `RequireSession`、
 * `SessionBadge` **共用 `["session"]` 这个 key**，react-query 按 key 去重，不多发请求。
 *
 * =============================================================================
 * 三、刻意不做的事
 *
 * · **不轮询、不加 `refetchInterval`。** 这个接口每次都真起一个容器（几百毫秒到一两秒），
 *   而它回答的是"这台机器配好了没有" —— 那是人去改配置才会变的事。
 *   要重新检测的入口在 T26 的诊断页。
 * · **不做骨架屏/加载动画。** 五行的行数与行高在三种状态下完全一样，值从「尚未检测」
 *   变成结论时**零布局位移**，再加一层 loading 只是多一次闪烁。
 * · **不把 `blockers` 数组直接渲染出来。** 那样会得到"没有对应行的码就消失、有对应行的
 *   码显示两次"。这里按行去读它自己那个字段，`blockers` 留给 T26。
 */

interface Verdict {
  readonly value: string;
  readonly tone: DotTone;
}

const UNKNOWN: Verdict = { value: t("common.unknown"), tone: "idle" };
const NOT_PROBED: Verdict = { value: t("home.readyNotProbed"), tone: "idle" };
const READY: Verdict = { value: t("systemStatus.ready"), tone: "ok" };

function blocked(copy: string): Verdict {
  return { value: copy, tone: "warn" };
}

/** 单个可空布尔的三态。`ok` 传进来是因为「遥测已关闭」那一行的通过态另有说法。 */
function tri(flag: boolean | null, ok: Verdict, bad: Verdict): Verdict {
  if (flag === null) {
    return UNKNOWN;
  }
  return flag ? ok : bad;
}

interface ReadyCheck {
  readonly id: string;
  readonly label: string;
  /** 从响应里读出这一行的结论。**每一行只读它自己那个字段。** */
  readonly resolve: (status: SystemStatusSummary) => Verdict;
}

/**
 * 五行。标签在左、结论在右，顺序 = `compute_blockers` 的修复顺序（docker 在最前，
 * 因为其余几项都以它为前提）。
 *
 * **网络那一行盖住两个码。** 后端的六个阻断码里，`sandbox_network_missing` 与
 * `api_not_on_sandbox_network` 原先在首页没有任何位置 —— 于是沙箱网络坏掉时这个
 * 面板会四行全绿，而扫描根本发不起来（`ready_for_scan: false`）。一个说"就绪"却
 * 不能扫的面板正是这块 UI 要防的东西，所以加了这一行。两个码共用一行是因为它们的
 * 修法是同一件事（把网络和 api 接上），而 T26 的诊断页会逐码展开。
 */
const CHECKS: readonly ReadyCheck[] = [
  {
    id: "docker",
    label: t("home.readyDocker"),
    resolve: (status) =>
      status.docker.reachable
        ? READY
        : blocked(t("systemStatus.blockers.docker_unreachable")),
  },
  {
    id: "sandboxNetwork",
    label: t("home.readySandboxNetwork"),
    resolve: (status) => {
      // 顺序即根因顺序：网络不存在时 `api_attached` 必然也是 false，先报前者。
      if (status.network.present === false) {
        return blocked(t("systemStatus.blockers.sandbox_network_missing"));
      }
      if (status.network.api_attached === false) {
        return blocked(t("systemStatus.blockers.api_not_on_sandbox_network"));
      }
      return status.network.present === true && status.network.api_attached === true
        ? READY
        : UNKNOWN;
    },
  },
  {
    id: "sandboxImage",
    label: t("home.readySandboxImage"),
    resolve: (status) =>
      tri(
        status.sandbox_image.present,
        READY,
        blocked(t("systemStatus.blockers.sandbox_image_missing")),
      ),
  },
  {
    id: "samePath",
    label: t("home.readySamePath"),
    resolve: (status) =>
      tri(
        status.data_dir.identical_path_ok,
        READY,
        blocked(t("systemStatus.blockers.same_path_mount_unverified")),
      ),
  },
  {
    id: "telemetry",
    label: t("home.readyTelemetryOff"),
    // 唯一一个通过态另有说法的行：这一行是**产品承诺**（不外发任何数据），
    // 不是一项配置检查，所以它值一句自己的话，而不是通用的「已就绪」。
    // 字段方向也与其余四行相反 —— `strix_telemetry: true` 是"遥测开着"。
    resolve: (status) =>
      status.telemetry.strix_telemetry
        ? blocked(t("systemStatus.blockers.telemetry_not_disabled"))
        : { value: t("home.readyTelemetryOffValue"), tone: "ok" },
  },
];

function toRow(check: ReadyCheck, status: SystemStatusSummary | undefined, failed: boolean) {
  const verdict = status === undefined ? (failed ? UNKNOWN : NOT_PROBED) : check.resolve(status);
  return { id: check.id, label: check.label, value: verdict.value, tone: verdict.tone };
}

export function ReadyRows() {
  const { data: session } = useQuery({
    queryKey: ["session"],
    queryFn: ({ signal }) => fetchSession(signal),
  });
  const { data, isError } = useQuery({
    queryKey: ["systemStatus"],
    queryFn: ({ signal }) => fetchSystemStatus(signal),
    enabled: session?.authenticated === true,
  });

  const rows: readonly StatusRow[] = CHECKS.map((check) => toRow(check, data, isError));
  return <Rows rows={rows} />;
}
