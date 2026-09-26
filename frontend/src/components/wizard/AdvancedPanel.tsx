"use client";

import { useId } from "react";

import { t, type MessageKey } from "@/lib/messages";
import { useWizardStore } from "@/lib/stores/wizard";

import steps from "./Steps.module.css";

const SCAN_MODES: readonly string[] = ["quick", "standard", "deep"];
const REASONING_EFFORTS: readonly string[] = [
  "none",
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
];

/**
 * `<select>` 的值只能是字符串，所以"跟随"用空串表示，进 store 前换成 `null`。
 * 空串不是任何一档的合法值，不会和真选项撞。
 */
const FOLLOW = "";

function toOverride(value: string): string | null {
  return value === FOLLOW ? null : value;
}

/**
 * 第 5 步的高级设置：扫描深度覆盖与推理强度。默认收起 —— 两项都有合理的缺省
 * （模板默认、模型默认），不改也能跑。
 */
export function AdvancedPanel() {
  const scanModeOverride = useWizardStore((s) => s.scanModeOverride);
  const setScanModeOverride = useWizardStore((s) => s.setScanModeOverride);
  const reasoningEffort = useWizardStore((s) => s.reasoningEffort);
  const setReasoningEffort = useWizardStore((s) => s.setReasoningEffort);

  const uid = useId();

  return (
    <details className={steps.details}>
      <summary className={steps.summary}>{t("wizard.advancedHeading")}</summary>

      <div className={steps.row}>
        <label className={steps.label} htmlFor={`${uid}-mode`}>
          {t("wizard.scanModeLabel")}
        </label>
        <select
          className={steps.input}
          id={`${uid}-mode`}
          value={scanModeOverride ?? FOLLOW}
          onChange={(event) => setScanModeOverride(toOverride(event.target.value))}
        >
          <option value={FOLLOW}>{t("wizard.scanModeFollow")}</option>
          {SCAN_MODES.map((mode) => (
            <option key={mode} value={mode}>
              {t(`wizard.modes.${mode}` as MessageKey)}
            </option>
          ))}
        </select>
      </div>

      <div className={steps.row}>
        <label className={steps.label} htmlFor={`${uid}-reasoning`}>
          {t("wizard.reasoningLabel")}
        </label>
        <select
          className={steps.input}
          id={`${uid}-reasoning`}
          value={reasoningEffort ?? FOLLOW}
          onChange={(event) => setReasoningEffort(toOverride(event.target.value))}
        >
          <option value={FOLLOW}>{t("wizard.reasoningFollow")}</option>
          {REASONING_EFFORTS.map((effort) => (
            <option key={effort} value={effort}>
              {t(`wizard.reasoning.${effort}` as MessageKey)}
            </option>
          ))}
        </select>
        <p className={steps.hint}>{t("wizard.reasoningHint")}</p>
      </div>
    </details>
  );
}
