import { Button } from "@/components/ui/Button";
import { type DocketField } from "@/components/ui/Field";
import { Panel } from "@/components/ui/Panel";
import { Rows, type StatusRow } from "@/components/ui/Rows";
import { StatusDot } from "@/components/ui/StatusDot";
import { t } from "@/lib/messages";

import styles from "./page.module.css";

/**
 * 首页。**服务端组件，静态输出。**
 *
 * 概念：界面主体是一张**纸**（`--sheet` 的 `<article>`），不是 dashboard（卡片墙）。
 *
 * =============================================================================
 * 为什么首屏**不是**那份待填的工单本身（2026-09-10 改）
 *
 * 原先首屏直接是「新建授权工单」+ 五个空白栏位（虚线 + 灰提示）。用户的判语是
 * "上来就是没头没尾的工单"，准确 —— 那张纸在向一个还不知道这工具是什么、也不知道
 * "授权工单"是个什么东西的人**要求输入**。
 *
 * 改法不是加个 hero，而是把同一批信息**换一种语气**：
 *   · 标题从"新建授权工单"（一个动作）变成"每次扫描都从一份授权工单开始"（一条规则）；
 *   · 两段介绍：这工具是什么 / 工单为什么是硬前提；
 *   · 五个栏位从 `<dl>` 的**空白态**（读作"这里等你填"）变成 `<ol>` 的**编号步骤**
 *     （读作"接下来会问你五件事"）。信息一字未改，性质从命令变成预告。
 *
 * 为什么在 T5 就改而不是留给后面某个任务：`PLAN.md` 的派发清单里 `src/app/*` 只有
 * T5 一个 owner（`PLAN.md:873`）。后面碰前端的四个任务各有地盘 —— T17 是
 * `components/live/*`、T18/T19 是 `components/wizard/*`、T25 是 `app/audit/*`、
 * T26 是 `app/diagnostics/*`；T3 只会把侧栏那四行的值换成真值。**没有任何后续任务
 * 会来加这段介绍**，现在不改就一直是原样。
 *
 * `PLAN.md` 对首屏只有一条硬要求：`PLAN.md:1144`「合规声明（README 与 UI 首屏都要有）」。
 * 那三段条款原地保留，位置也没动 —— 它仍然紧挨在按钮上方，让人在按下去之前读到。
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

/**
 * 工单的五个字段。顺序即填写顺序（T18 的向导按同一顺序走五步）。
 *
 * 首页现在把它们渲染成编号步骤，但**类型仍然用 `DocketField`**：这五条就是工单的
 * 那五栏，让「首页的五步 = 工单的五个字段 = 向导的五步」这条不变量在类型层面就是
 * 同一样东西，而不是两处需要手工对齐的副本。`blank`（"尚未填写时的灰色提示"）在
 * 这里当步骤说明用 —— 两处要说的是同一句话（"这一栏要填什么"），不是巧合的复用。
 */
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
          <h1 className={styles.pageTitle}>{t("home.introTitle")}</h1>
        </div>

        <div className={styles.docketBody}>
          <p className={styles.lede}>{t("home.introLede")}</p>

          {/* 五步在左、批注在右，为的是把首屏压回一屏（2026-09-11）。

              DOM 顺序 = 视觉顺序，刻意**不用** grid 的 `order` 把批注挪位 ——
              那会让屏幕阅读器和 Tab 焦点走出和眼睛不一样的路线。代价是批注从"紧跟
              正文"变成"读完五步再读"，可以接受：它讲的是"没有工单发不起扫描"，
              在五步之后读同样通顺。

              `<aside>` 不是为了排版好看才用的标签 —— 它确实是对正文的**旁注**，
              而批注写在页边是纸质文书本来的做法。 */}
          <div className={styles.mid}>
            {/* 用侧栏那个 `Panel`，不在这里手写一套"带表头的框" ——
                那会是同一个东西的第二份实现。它带来的是：1px 边框 + 一条表头下划线，
                表头刻意不填底色（`Panel.module.css` 里写了理由：填色表头堆起来就是
                「卡片套件」那张品类脸）。 */}
            <Panel title={t("home.stepsHeading")}>
              {/* `<ol>` 而不是 `<dl>`：这里讲的是**有序的五步**，不是"字段名—字段值"。
                  同一批文字在 T18 的工单里会回到 `<dl>`（`ui/FieldTable`），那时它们
                  真的是字段。语义跟着含义走，不跟着文字走。

                  每个 `<li>` 刻意**不设** `display: grid/flex` —— 那会把
                  `display: list-item` 顶掉，编号会整排消失（浏览器不会报错）。
                  两列对齐靠里面多套的这一层 `.stepRow`（grid 在子元素上是安全的）。
                  多这一层 div 不是装饰：没有它，提示折行后会回到行首撞在标签底下。 */}
              <ol className={styles.stepList}>
                {DOCKET_FIELDS.map((field) => (
                  <li key={field.id} className={styles.step}>
                    <div className={styles.stepRow}>
                      <span className={styles.stepLabel}>{field.label}</span>
                      <span className={styles.stepHint}>{field.blank}</span>
                    </div>
                  </li>
                ))}
              </ol>
            </Panel>

            <aside className={styles.note}>{t("home.introRule")}</aside>
          </div>

          {/* 条款是工单的一部分，不是弹窗。用 `<section>` + `<h2>` 让它在
              辅助技术里也是一个可跳转的分节。

              位置**通栏、在最下、紧挨按钮**（2026-09-10 拍板）。曾试过把它挪到五步
              右边以换取"首屏不超过一屏"，被否 —— 它是要在按下按钮之前被读到的东西，
              省高度不是把它挪去侧位的理由。 */}
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
