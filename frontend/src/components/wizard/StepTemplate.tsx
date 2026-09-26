"use client";

import { useQuery } from "@tanstack/react-query";
import { useId } from "react";

import { ErrorNotice } from "@/components/errors/ErrorNotice";
import { fetchScanTemplates } from "@/lib/api/client";
import { t, type MessageKey } from "@/lib/messages";
import { useWizardStore } from "@/lib/stores/wizard";

import steps from "./Steps.module.css";
import { TestAccounts } from "./TestAccounts";

/**
 * 第 4 步：场景模板。
 *
 * 六个模板与它们的默认预算／轮数**全部来自 `/api/scan-templates`**，前端不留
 * 第二份表 —— 硬编码那张表会在改后端常量时静默漂移，而漂移的后果是钱。
 */
export function StepTemplate() {
  const templateId = useWizardStore((s) => s.templateId);
  const chooseTemplate = useWizardStore((s) => s.chooseTemplate);
  const extraInstruction = useWizardStore((s) => s.extraInstruction);
  const setExtraInstruction = useWizardStore((s) => s.setExtraInstruction);

  // hook 一律在任何早退之前调用。
  const uid = useId();
  const { data, isError } = useQuery({
    queryKey: ["scanTemplates"],
    queryFn: ({ signal }) => fetchScanTemplates(signal),
  });

  if (isError) {
    return <ErrorNotice code="internal_error" />;
  }
  if (data === undefined) {
    return <p className={steps.hint}>{t("common.loading")}</p>;
  }

  const requiresNotes =
    data.templates.find((item) => item.template_id === templateId)?.requires_notes ?? false;

  return (
    <div>
      {/* 换模板会重置第 5 步的预算与轮数，所以这句话要在**选之前**读到。 */}
      <p className={steps.hint}>{t("wizard.templateResetNote")}</p>

      <div className={steps.choices}>
        {data.templates.map((item) => (
          <label className={steps.choice} key={item.template_id}>
            <input
              type="radio"
              name={`${uid}-template`}
              value={item.template_id}
              checked={item.template_id === templateId}
              onChange={() => {
                // 选模板 = 选模板 **并**把预算与轮数重置成它的默认值（用户拍板）。
                chooseTemplate(
                  item.template_id,
                  item.default_budget_usd,
                  item.default_max_turns,
                );
              }}
            />
            <span>
              <span className={steps.choiceTitle}>
                {t(`templates.${item.template_id}` as MessageKey)}
                {item.recommended ? (
                  <span className={steps.badge}>{t("wizard.templateRecommended")}</span>
                ) : null}
              </span>
              <span className={steps.choiceMeta}>
                {t(`wizard.modes.${item.scan_mode}` as MessageKey)}
              </span>
              <span className={steps.choiceMeta}>
                {t("wizard.templateDefaults")} {item.default_budget_usd}
              </span>
              <span className={steps.choiceMeta}>
                {t("wizard.templateTurns")} {item.default_max_turns}
              </span>
              <span className={steps.choiceMeta}>
                {t(`templateNotes.${item.template_id}` as MessageKey)}
              </span>
            </span>
          </label>
        ))}
      </div>

      {/* 所有模板都可见，不放进高级面板：它是写给扫描 agent 的话，不是一个调参项。 */}
      <div className={steps.row}>
        <label className={steps.label} htmlFor={`${uid}-notes`}>
          {t("wizard.notesLabel")}
        </label>
        <textarea
          className={steps.textarea}
          id={`${uid}-notes`}
          value={extraInstruction}
          onChange={(event) => setExtraInstruction(event.target.value)}
        />
        <p className={steps.hint}>{t("wizard.notesHint")}</p>
        {requiresNotes ? <p className={steps.warn}>{t("wizard.notesRequired")}</p> : null}
      </div>

      <TestAccounts />
    </div>
  );
}
