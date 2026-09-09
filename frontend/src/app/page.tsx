import { Button } from "@/components/ui/Button";
import { FieldTable, type DocketField } from "@/components/ui/Field";
import { Panel } from "@/components/ui/Panel";
import { Rows, type StatusRow } from "@/components/ui/Rows";
import { StatusDot } from "@/components/ui/StatusDot";
import { t } from "@/lib/messages";

import styles from "./page.module.css";

/**
 * 首页。**服务端组件，静态输出。**
 *
 * 概念：界面主体是一份**正在被填写的授权工单**（纸），不是 dashboard（卡片墙）。
 * 所以首屏放的是"要你填的那张纸"，而不是 hero 标题 + 指标卡。
 *
 * =============================================================================
 * 本轮它是**骨架**，三处入口是禁用的
 *
 * 后端到 T5 只有四个路由：`/api/health` 与 `/api/auth/{login,logout,me}`。
 * `/api/scans`、`/api/keys`、`/ws/*` 全是 404。
 *
 * 于是「开始填写工单」（T18 的向导）与「现在提供凭据」（T7 的表单）都点不动。
 * 做成**禁用 + 一句说明**，而不是链到一个不存在的地址 ——
 * 点下去 404 的按钮比没有按钮更糟：它把"这一步还没做"变成"这个工具坏了"。
 * （同一条判据也管着 `SessionExpiredMask` 为什么本轮没有按钮。）
 *
 * 「本机就绪状态」四行同理一律是**尚未检测**（空心点）。样张里写着
 * 「Docker 可达 / Desktop 29.7.2」那种具体值 —— 那要 T3/T26 的
 * `/api/system/status`。在没有探测的情况下把它们画成绿点是编造，
 * 而这种 UI 在真的不就绪时也照样说就绪。
 */

/** 工单的五个字段。顺序即填写顺序（T18 的向导按同一顺序走五步）。 */
const DOCKET_FIELDS: readonly DocketField[] = [
  { id: "target", label: t("home.fieldTarget"), blank: t("home.fieldTargetBlank") },
  {
    id: "authorization",
    label: t("home.fieldAuthorization"),
    blank: t("home.fieldAuthorizationBlank"),
  },
  { id: "operator", label: t("home.fieldOperator"), blank: t("home.fieldOperatorBlank") },
  { id: "template", label: t("home.fieldTemplate"), blank: t("home.fieldTemplateBlank") },
  { id: "budget", label: t("home.fieldBudget"), blank: t("home.fieldBudgetBlank") },
];

/**
 * 就绪状态四项。`tone` 全是 `idle`（空心 = 还不知道），值全是「尚未检测」。
 * T3/T26 接上 `/api/system/status` 之后，这个常量会变成一次 `useQuery` 的结果，
 * 行数、行高、点的位置都不变 —— 那时不会有布局跳动。
 */
const READY_ROWS: readonly StatusRow[] = [
  { id: "docker", label: t("home.readyDocker"), value: t("home.readyNotProbed"), tone: "idle" },
  {
    id: "sandboxImage",
    label: t("home.readySandboxImage"),
    value: t("home.readyNotProbed"),
    tone: "idle",
  },
  { id: "samePath", label: t("home.readySamePath"), value: t("home.readyNotProbed"), tone: "idle" },
  {
    id: "telemetry",
    label: t("home.readyTelemetryOff"),
    value: t("home.readyNotProbed"),
    tone: "idle",
  },
];

export default function HomePage() {
  return (
    <div className={styles.two}>
      <article className={styles.docket}>
        <div className={styles.docketHead}>
          <h1 className={styles.docketTitle}>{t("home.docketTitle")}</h1>
          <div className={styles.docketNo}>{t("home.docketNumberPending")}</div>
        </div>

        <div className={styles.docketBody}>
          <FieldTable fields={DOCKET_FIELDS} />

          {/* 条款是工单的一部分，不是弹窗。用 `<section>` + `<h2>` 让它在
              辅助技术里也是一个可跳转的分节。 */}
          <section className={styles.terms}>
            <h2 className={styles.termsHeading}>{t("compliance.heading")}</h2>
            <p className={styles.termsText}>{t("compliance.p1")}</p>
            <p className={styles.termsText}>{t("compliance.p2")}</p>
            <p className={styles.termsText}>{t("compliance.p3")}</p>
          </section>

          <div className={styles.actions}>
            <Button disabled ariaDescribedBy="start-not-ready">
              {t("home.start")}
            </Button>
            <span className={styles.hint}>{t("home.startHint")}</span>
            <span className={styles.hint} id="start-not-ready">
              {t("common.notReadyYet")}
            </span>
          </div>
        </div>
      </article>

      <aside>
        <Panel title={t("home.readyTitle")}>
          <Rows rows={READY_ROWS} />
        </Panel>

        <Panel title={t("credentials.panelTitle")}>
          {/* 「凭据随时会没」写在明面上。这句话是产品陈述，不是错误提示 ——
              方块点（`warn`）表示"需要你处理"，而不是"出了故障"。 */}
          <p className={styles.credentialsNote}>
            <StatusDot tone="warn" />
            {t("credentials.none")}
          </p>
          <div className={styles.panelActions}>
            <Button variant="ghost" disabled ariaDescribedBy="credentials-not-ready">
              {t("credentials.provide")}
            </Button>
            <span className={styles.hint} id="credentials-not-ready">
              {t("common.notReadyYet")}
            </span>
          </div>
        </Panel>

        <Panel title={t("home.recentTitle")}>
          <p className={styles.empty}>{t("home.recentEmpty")}</p>
        </Panel>
      </aside>
    </div>
  );
}
