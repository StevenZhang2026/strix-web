"use client";

import { useId } from "react";

import { t } from "@/lib/messages";
import { useWizardStore } from "@/lib/stores/wizard";

import steps from "./Steps.module.css";

/**
 * 第 3 步：操作人。一个字段，就这些。
 *
 * **不预填**（用户拍板）：登录账号名不等于对这次测试负责的人，而全站也没有任何
 * 取会话用户名的地方 —— 为这一个框去开那条路不划算，替他填一个名字更不划算。
 */
export function StepOperator() {
  const operatorName = useWizardStore((s) => s.operatorName);
  const setOperatorName = useWizardStore((s) => s.setOperatorName);

  const uid = useId();

  return (
    <div className={steps.row}>
      <label className={steps.label} htmlFor={`${uid}-operator`}>
        {t("home.fieldOperator")}
      </label>
      <input
        className={steps.input}
        id={`${uid}-operator`}
        type="text"
        autoComplete="off"
        spellCheck={false}
        value={operatorName}
        onChange={(event) => setOperatorName(event.target.value)}
      />
      <p className={steps.hint}>{t("wizard.operatorHint")}</p>
    </div>
  );
}
