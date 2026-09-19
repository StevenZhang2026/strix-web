"use client";

import { Button } from "@/components/ui/Button";
import { t, type MessageKey } from "@/lib/messages";
import { useWizardStore } from "@/lib/stores/wizard";

import { StepBudget } from "./StepBudget";
import { StepTargets } from "./StepTargets";
import { StepTemplate } from "./StepTemplate";
import styles from "./Wizard.module.css";

/**
 * 五步的名字与顺序。**写死**，与首页工单 `DOCKET_FIELDS` 同序、同文案键 ——
 * 「首页的五步 = 工单的五个字段 = 向导的五步」是一条不变量，所以这里刻意复用
 * `home.field*`，不另起一套 `wizard.step*` 名字（两套名字必然漂移）。
 */
const STEP_TITLES: readonly MessageKey[] = [
  "home.fieldTarget",
  "home.fieldAuthorization",
  "home.fieldOperator",
  "home.fieldTemplate",
  "home.fieldBudget",
];

/**
 * 这一轮还没上线的步（授权依据、操作人，T18b 做）。
 * 它们只渲染占位，且**不拦前进** —— 本轮根本没有提交这个动作，
 * 拦在这里只会让人以为自己填错了什么。真正的闸在 T18b。
 */
const PENDING_STEPS: readonly number[] = [2, 3];

export function Wizard() {
  const step = useWizardStore((s) => s.step);
  const setStep = useWizardStore((s) => s.setStep);

  // 越界不可能（导航只在 1..STEP_TITLES.length 之间走），`??` 只为满足
  // `noUncheckedIndexedAccess`。
  const title = STEP_TITLES[step - 1] ?? "wizard.heading";

  return (
    <div>
      <ol className={styles.rail}>
        {STEP_TITLES.map((key, index) => {
          const number = index + 1;
          const classes = [styles.rung];
          if (number === step) {
            classes.push(styles.current);
          }
          if (PENDING_STEPS.includes(number)) {
            classes.push(styles.pending);
          }
          return (
            <li
              key={key}
              className={classes.join(" ")}
              aria-current={number === step ? "step" : undefined}
              // 未上线的步除了灰掉，还要说出"为什么灰" —— 只灰掉会被当成"已完成"。
              title={PENDING_STEPS.includes(number) ? t("wizard.stepPending") : undefined}
            >
              <span className={styles.number}>{number}</span>
              <span className={styles.rungTitle}>{t(key)}</span>
            </li>
          );
        })}
      </ol>

      <section className={styles.body}>
        <h2 className={styles.stepTitle}>{t(title)}</h2>
        <StepBody step={step} />
      </section>

      <div className={styles.nav}>
        {step === 1 ? null : (
          <Button variant="ghost" onClick={() => setStep(step - 1)}>
            {t("common.back")}
          </Button>
        )}
        {step === STEP_TITLES.length ? null : (
          <Button onClick={() => setStep(step + 1)}>{t("common.next")}</Button>
        )}
      </div>
    </div>
  );
}

/** 分步渲染。第 2／3 步落到占位上。 */
function StepBody({ step }: { readonly step: number }) {
  switch (step) {
    case 1:
      return <StepTargets />;
    case 4:
      return <StepTemplate />;
    case 5:
      return <StepBudget />;
    default:
      return <StepPending />;
  }
}

/** 还没上线的那两步。刻意不画一个空表单 —— 空表单看起来像"可以填"。 */
function StepPending() {
  return (
    <>
      <p className={styles.pendingTitle}>{t("wizard.stepPending")}</p>
      <p className={styles.pendingDetail}>{t("wizard.stepPendingDetail")}</p>
    </>
  );
}
