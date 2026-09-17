"use client";

import { useQuery } from "@tanstack/react-query";

import { Rows, type StatusRow } from "@/components/ui/Rows";
import { type DotTone } from "@/components/ui/StatusDot";
import { fetchSession, fetchSystemStatus, type SystemStatusSummary } from "@/lib/api/client";
import { t, type MessageKey } from "@/lib/messages";

import styles from "./ReadyRows.module.css";

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
 *   重新检测的办法就是刷新页面（诊断页已砍掉，所以每条修复指引都以"刷新本页"收尾）。
 * · **不做骨架屏/加载动画。** 五行的行数与行高在三种状态下完全一样，值从「尚未检测」
 *   变成结论时**零布局位移**，再加一层 loading 只是多一次闪烁。
 * · **五行不读 `blockers` 数组。** 按数组渲染那五行会得到"没有对应行的码就消失、有对应
 *   行的码显示两次"。所以每行只读它自己那个字段。
 *
 * =============================================================================
 * 四、`blockers` 在下方的修复指引里（T26）
 *
 * 五行只回答"哪一项没过"，不回答"那我该怎么办"。后者是 `<FixHints>`：**照着
 * `status.blockers` 的顺序一个码一条**，标题用短标签、正文用 `systemStatus.fixes.*`。
 *
 * 这里**不做任何判定**（不排序、不去重、不抑制）—— `compute_blockers` 已经做完了：
 * docker 排在最前，且 docker 不可达时那四条派生项一条都不报（一个根因不该变成五条
 * 待办）。前端再排一遍等于把同一条规则实现两次，而两份实现必然漂移。
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
 * 修法是同一件事（把网络和 api 接上），而下方的 `<FixHints>` 会逐码展开。
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

/**
 * 一个阻断码的两句话：短标签（五行右侧那句）+ 修复指引。
 *
 * `t()` 的参数是**字面量联合类型**，而码是运行期从 JSON 里来的字符串，所以这里必须
 * 断言一次。断言是安全的，而且这条安全性是**被测的**：
 * `backend/tests/test_message_coverage.py` 拿 `ALL_BLOCKER_CODES` 对
 * `systemStatus.blockers.*` 与 `systemStatus.fixes.*` 各做一次双向比对，缺一条或多一条
 * 都会红。真漏了的话 `t()` 也不会静默 —— 它按兜底原样露出 key（`lib/messages.ts`），
 * 一串英文点分路径出现在一片中文里是刺眼且可搜索的。
 *
 * 反过来在这里写一张"码 → key"的表就得把六个码抄进前端，那份副本没有任何测试守着。
 */
function copyFor(code: string): { readonly label: string; readonly fix: string } {
  return {
    label: t(`systemStatus.blockers.${code}` as MessageKey),
    fix: t(`systemStatus.fixes.${code}` as MessageKey),
  };
}

interface FixHintsProps {
  /** `undefined` = 还没拿到响应。 */
  readonly blockers: readonly string[] | undefined;
}

/**
 * 五行下方的修复指引。**没有阻断项时整块不渲染**（返回 `null`）——
 * 全绿的机器上留一个"查看修复指引"的入口，等于让人怀疑面板刚说的"已就绪"。
 */
function FixHints({ blockers }: FixHintsProps) {
  if (blockers === undefined || blockers.length === 0) {
    return null;
  }
  return (
    <details className={styles.fixes}>
      {/* 数量单独一个文本节点，前后两句从文案表取：`t()` 刻意不支持占位符插值
          （`messages/README.md` 约定 4 禁占位符、约定 3 要求"值渲染成自己的节点"），
          而为一个计数去给它加一套插值机制，代价比这里多两个 key 大得多。 */}
      <summary className={styles.summary}>
        {t("systemStatus.fixesSummaryPrefix")}
        {blockers.length}
        {t("systemStatus.fixesSummarySuffix")}
      </summary>
      <ul className={styles.list}>
        {blockers.map((code) => {
          const copy = copyFor(code);
          return (
            <li key={code} className={styles.item}>
              <p className={styles.title}>{copy.label}</p>
              <p className={styles.fix}>{copy.fix}</p>
            </li>
          );
        })}
      </ul>
    </details>
  );
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
  return (
    <>
      <Rows rows={rows} />
      <FixHints blockers={data?.blockers} />
    </>
  );
}
