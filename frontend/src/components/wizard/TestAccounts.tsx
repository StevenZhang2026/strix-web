"use client";

import { useEffect, useId, useState } from "react";

import { Button } from "@/components/ui/Button";
import { t } from "@/lib/messages";
import { accountRowProblem, MAX_TEST_ACCOUNTS, useWizardStore } from "@/lib/stores/wizard";

import steps from "./Steps.module.css";

/** 这个模板测的就是登录与权限：默认展开，并多说一句为什么需要账号。 */
const ACCOUNTS_NEEDED_TEMPLATE = "auth_and_access";

/**
 * 第 4 步：测试账号 → 请求体 `credentials[]`。
 *
 * 行的判定只在 `accountRowProblem()` 里（提交闸共用），这里只把结论说成人话。
 * `accountsResidual` 是残余风险告知，**始终可见**，不藏进 tooltip。
 */
export function TestAccounts() {
  const templateId = useWizardStore((s) => s.templateId);
  const rows = useWizardStore((s) => s.testAccounts);
  const addTestAccount = useWizardStore((s) => s.addTestAccount);
  const removeTestAccount = useWizardStore((s) => s.removeTestAccount);
  const updateTestAccount = useWizardStore((s) => s.updateTestAccount);

  const uid = useId();
  // 随机 `name` 在**挂载后**才生成（照 `CredentialForm.tsx`）：`crypto.randomUUID()`
  // 放进 `useState` 的惰性初始值会让 SSR 与 hydration 的属性对不上。
  const [nameSalt, setNameSalt] = useState("");
  useEffect(() => {
    setNameSalt(crypto.randomUUID());
  }, []);

  const needed = templateId === ACCOUNTS_NEEDED_TEMPLATE;

  return (
    <details className={steps.details} open={needed || rows.length > 0}>
      <summary className={steps.summary}>{t("wizard.accountsHeading")}</summary>
      {needed ? <p className={steps.warn}>{t("wizard.accountsNeededHint")}</p> : null}
      <p className={steps.hint}>{t("wizard.accountsHint")}</p>
      <p className={steps.hint}>{t("wizard.accountsResidual")}</p>

      {rows.map((row, index) => {
        const problem = accountRowProblem(row);
        return (
          <div className={steps.accountRow} key={index}>
            <div className={steps.accountFields}>
              <label className={steps.label} htmlFor={`${uid}-role-${index}`}>
                {t("wizard.accountRole")}
              </label>
              <input
                className={steps.input}
                id={`${uid}-role-${index}`}
                type="text"
                autoComplete="off"
                placeholder={t("wizard.accountRoleExample")}
                value={row.role}
                onChange={(event) => updateTestAccount(index, "role", event.target.value)}
              />
              <label className={steps.label} htmlFor={`${uid}-username-${index}`}>
                {t("wizard.accountUsername")}
              </label>
              <input
                className={steps.input}
                id={`${uid}-username-${index}`}
                type="text"
                autoComplete="off"
                autoCapitalize="none"
                spellCheck={false}
                value={row.username}
                onChange={(event) => updateTestAccount(index, "username", event.target.value)}
              />
              <label className={steps.label} htmlFor={`${uid}-password-${index}`}>
                {t("wizard.accountPassword")}
              </label>
              <input
                className={steps.input}
                id={`${uid}-password-${index}`}
                // 随机 `name`：破浏览器自动填充。
                name={`a-${nameSalt}-${index}`}
                type="password"
                autoComplete="off"
                autoCapitalize="none"
                spellCheck={false}
                value={row.password}
                onChange={(event) => updateTestAccount(index, "password", event.target.value)}
              />
            </div>
            {problem === "incomplete" ? (
              <p className={steps.warn}>{t("wizard.accountIncomplete")}</p>
            ) : null}
            {problem === "too_short" ? (
              <p className={steps.warn}>{t("wizard.accountPasswordTooShort")}</p>
            ) : null}
            <div className={steps.actions}>
              <Button variant="ghost" onClick={() => removeTestAccount(index)}>
                {t("wizard.accountRemove")}
              </Button>
            </div>
          </div>
        );
      })}

      {rows.length >= MAX_TEST_ACCOUNTS ? null : (
        <div className={steps.actions}>
          <Button variant="ghost" onClick={addTestAccount}>
            {t("wizard.accountAdd")}
          </Button>
        </div>
      )}
    </details>
  );
}
