"use client";

import { useId } from "react";

import { t } from "@/lib/messages";
import { isBudgetTooLow, isTurnsTooLow, useWizardStore } from "@/lib/stores/wizard";

import steps from "./Steps.module.css";
import { SubmitPanel } from "./SubmitPanel";

/**
 * 第 5 步：费用上限与轮数。
 *
 * **这一步不宣称任何上限数字。** 真上限 `CONSOLE_MAX_BUDGET_CEILING_USD` 没有任何
 * 路由暴露给前端，猜一个数字写上去比不写更糟（它会被当成承诺）；超上限由提交时
 * 后端的 `budget_exceeds_ceiling` 告知。
 *
 * 下限反过来 —— 它是前端自己的判定（用户拍板 $2），所以就地提示、就地拦。
 */
export function StepBudget() {
  const budgetUsd = useWizardStore((s) => s.budgetUsd);
  const setBudgetUsd = useWizardStore((s) => s.setBudgetUsd);
  const maxTurns = useWizardStore((s) => s.maxTurns);
  const setMaxTurns = useWizardStore((s) => s.setMaxTurns);

  const uid = useId();

  return (
    <div>
      <div className={steps.row}>
        <label className={steps.label} htmlFor={`${uid}-budget`}>
          {t("budget.ceilingLabel")}
        </label>
        {/* **两个输入框都刻意不写 `min`。** 浏览器的 `min` 会在控件层把值夹住（上下箭头到不了、
            输入被判 `:invalid`），于是"为什么不能填 1"这句话永远不会被读到 —— 闸是下面那句
            人话提示，不是控件属性。下限本身在 `isBudgetTooLow()`／`isTurnsTooLow()` 里，
            T18b 的提交按钮消费同一对函数。 */}
        <input
          className={steps.input}
          id={`${uid}-budget`}
          type="number"
          inputMode="decimal"
          step={1}
          autoComplete="off"
          value={budgetUsd}
          onChange={(event) => setBudgetUsd(event.target.value)}
        />
        <p className={steps.hint}>{t("wizard.budgetHint")}</p>
        {/* 这两句由后端测试管着（"不许承诺绝不超过、必须说达到后停止"），
            所以复用 `budget.*` 的原句，不在这里另写一段。 */}
        <p className={steps.hint}>{t("budget.stopsAt")}</p>
        <p className={steps.hint}>{t("budget.overflowNote")}</p>
        {/* 空框只说明"还没填"，**不是**"填低了"。判定函数把空串算作不合格（T18b 的提交
            闸需要那样），但屏幕上不许把它说成"太低" —— 一进这一步就先骂人一句，
            而且那句话还指着一个空框。 */}
        {budgetUsd.trim() !== "" && isBudgetTooLow(budgetUsd) ? (
          <p className={steps.warn}>{t("wizard.budgetTooLow")}</p>
        ) : null}
      </div>

      <div className={steps.row}>
        <label className={steps.label} htmlFor={`${uid}-turns`}>
          {t("wizard.turnsLabel")}
        </label>
        <input
          className={steps.input}
          id={`${uid}-turns`}
          type="number"
          inputMode="numeric"
          step={1}
          autoComplete="off"
          value={maxTurns}
          onChange={(event) => setMaxTurns(event.target.value)}
        />
        <p className={steps.hint}>{t("wizard.turnsHint")}</p>
        {maxTurns.trim() !== "" && isTurnsTooLow(maxTurns) ? (
          <p className={steps.warn}>{t("wizard.turnsTooLow")}</p>
        ) : null}
      </div>

      {/* 整份工单在这里提交。按钮在还差东西时是禁用的，但**旁边一定有一张"还差
          这几项"的清单** —— 一个只灰掉、不说原因的按钮才是会被反复去按的那种。 */}
      <SubmitPanel />
    </div>
  );
}
