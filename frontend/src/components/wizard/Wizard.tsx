"use client";

import { Button } from "@/components/ui/Button";
import { t, type MessageKey } from "@/lib/messages";
import { useWizardStore } from "@/lib/stores/wizard";

import { StepAuthorization } from "./StepAuthorization";
import { StepBudget } from "./StepBudget";
import { StepOperator } from "./StepOperator";
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
          return (
            <li
              key={key}
              className={classes.join(" ")}
              aria-current={number === step ? "step" : undefined}
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

/** 分步渲染。五步全在，`step` 恒在 1..5，所以 `default` 只是给类型收尾。 */
function StepBody({ step }: { readonly step: number }) {
  switch (step) {
    case 1:
      return <StepTargets />;
    case 2:
      return <StepAuthorization />;
    case 3:
      return <StepOperator />;
    case 4:
      return <StepTemplate />;
    case 5:
      return <StepBudget />;
    default:
      return null;
  }
}
