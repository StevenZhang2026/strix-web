"use client";

import { useQuery } from "@tanstack/react-query";
import { useId } from "react";

import { fetchKeyState } from "@/lib/api/client";
import { t } from "@/lib/messages";
import { useKeysStore } from "@/lib/stores/keys";
import {
  isBudgetTooLow,
  isTurnsTooLow,
  SUGGESTED_MIN_BUDGET_USD,
  useWizardStore,
} from "@/lib/stores/wizard";

import { AdvancedPanel } from "./AdvancedPanel";
import steps from "./Steps.module.css";
import { SubmitPanel } from "./SubmitPanel";

/**
 * bearer 形状下"这个上限在 SigV4 下大约相当于花多少"：上限 ÷ 6 ～ 上限 ÷ 4（实测倍数 4～6）。
 * 上限不是正数就回 `null`，不出区间那一截。
 */
function sigv4Equivalent(budgetUsd: string): string | null {
  const ceiling = Number(budgetUsd);
  if (budgetUsd.trim() === "" || !Number.isFinite(ceiling) || ceiling <= 0) {
    return null;
  }
  return `$${(ceiling / 6).toFixed(1)}～$${(ceiling / 4).toFixed(1)}`;
}

/**
 * 第 5 步：费用上限与轮数。
 *
 * **这一步不宣称任何上限数字。** 真上限 `CONSOLE_MAX_BUDGET_CEILING_USD` 没有任何
 * 路由暴露给前端，猜一个数字写上去比不写更糟（它会被当成承诺）；超上限由提交时
 * 后端的 `budget_exceeds_ceiling` 告知。
 *
 * 下限反过来 —— 它是前端自己的判定（用户拍板 $2），所以就地提示、就地拦。
 * 再往上还有一条只提示不拦的建议下限 `SUGGESTED_MIN_BUDGET_USD`（$4）。
 */
export function StepBudget() {
  const budgetUsd = useWizardStore((s) => s.budgetUsd);
  const setBudgetUsd = useWizardStore((s) => s.setBudgetUsd);
  const maxTurns = useWizardStore((s) => s.maxTurns);
  const setMaxTurns = useWizardStore((s) => s.setMaxTurns);

  const uid = useId();
  const handle = useKeysStore((s) => s.handle);
  // 只用来判断凭据形状。handle 为空、查询中、查询失败 → 什么都不出（不猜）。
  const { data: keyState } = useQuery({
    queryKey: ["keyState", handle],
    queryFn: ({ signal }) => fetchKeyState(handle ?? "", signal),
    enabled: handle !== null,
  });
  const bearer = keyState?.auth_shape === "bedrock_bearer";
  const equivalent = sigv4Equivalent(budgetUsd);

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
        {/* 已经过了硬下限才轮到它：两句同时出现，前一句说"不许"、这一句说"可以"，自相矛盾。 */}
        {!isBudgetTooLow(budgetUsd) && Number(budgetUsd) < SUGGESTED_MIN_BUDGET_USD ? (
          <p className={steps.hint}>{t("wizard.budgetBelowSuggested")}</p>
        ) : null}
        {/* 只提示、不改默认预算、不给"一键调高"（用户拍板）。 */}
        {bearer ? (
          <>
            <p className={steps.warn}>
              {t("wizard.costBearer")}
              {equivalent === null ? null : <> {equivalent}</>}
            </p>
            <p className={steps.hint}>{t("wizard.costBearerAdvice")}</p>
          </>
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
      <AdvancedPanel />
      <SubmitPanel />
    </div>
  );
}
