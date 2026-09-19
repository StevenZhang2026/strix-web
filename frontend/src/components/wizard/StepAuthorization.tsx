"use client";

import { useQuery } from "@tanstack/react-query";
import { useId } from "react";

import { Button } from "@/components/ui/Button";
import { StatusDot } from "@/components/ui/StatusDot";
import { fetchAllowlist, type AllowlistEntryView, type TargetValidation } from "@/lib/api/client";
import { t, type MessageKey } from "@/lib/messages";
import {
  AFFIRMATION_IDS,
  confirmationMatches,
  expectedConfirmation,
  useWizardStore,
} from "@/lib/stores/wizard";

import steps from "./Steps.module.css";
import styles from "./StepAuthorization.module.css";

/**
 * 第 2 步：授权声明。
 *
 * **没有第 1 步的校验快照就什么都不画。** 两个理由：
 *   · `resolved_ips_seen` 只能从那份快照构造（后端起扫描前会重新解析一遍比对），
 *     所以"先校验再声明"是结构性要求，不是 UI 礼貌；
 *   · 空表单看起来像"可以填"，填完了才发现签的是一份没有对象的声明。
 * 这一步也**不自己去调 `validateTargets`** —— 校验会做真实 DNS 查询，它只能是
 * 第 1 步那个明确的按钮。
 */
export function StepAuthorization() {
  const validation = useWizardStore((s) => s.validation);
  const affirmations = useWizardStore((s) => s.affirmations);
  const setAffirmation = useWizardStore((s) => s.setAffirmation);
  const multiTargetAffirmed = useWizardStore((s) => s.multiTargetAffirmed);
  const setMultiTargetAffirmed = useWizardStore((s) => s.setMultiTargetAffirmed);
  const typedConfirmation = useWizardStore((s) => s.typedConfirmation);
  const setTypedConfirmation = useWizardStore((s) => s.setTypedConfirmation);
  const authorizationRef = useWizardStore((s) => s.authorizationRef);
  const setAuthorizationRef = useWizardStore((s) => s.setAuthorizationRef);

  const uid = useId();

  if (validation === null) {
    return <p className={steps.hint}>{t("wizard.needsValidation")}</p>;
  }

  const expected = expectedConfirmation(validation);
  // 空框**不提示**：一进这一步就骂人一句、还指着一个空框。
  const mismatch =
    typedConfirmation.trim() !== "" && !confirmationMatches(typedConfirmation, expected);

  // 命中的清单条目 label，去重。`allowlist_entry` 是条目的 label，不是编号。
  const hitLabels = [
    ...new Set(
      validation.results
        .map((item) => item.allowlist_entry)
        .filter((label): label is string => label !== null),
    ),
  ];

  return (
    <div>
      {/* ---------------------------------------- (a) 这次声明覆盖的目标 */}
      {/* 只读复述，**不是第 1 步那张卡的复制品**：没有 punycode 提示块、没有放行条件、
          没有 reason/note/ErrorNotice。那些讲的是"怎么才能放行"，在签字这一屏只会
          让它变成又一份诊断报告。用户在这里要读的只有"我正在为哪几个目标签字"。 */}
      <section className={styles.declared}>
        <p className={steps.label}>{t("wizard.declaredHeading")}</p>
        <ul className={styles.list}>
          {validation.results.map((item, index) => (
            <li className={styles.card} key={`${index}-${item.raw}`}>
              <DeclaredTarget item={item} />
            </li>
          ))}
        </ul>
      </section>

      {/* ---------------------------------------- (b) 三条声明 */}
      {/* 三个**独立**复选框，各自勾一次。刻意**没有**"全选" —— 一键勾完三条声明
          等于把三次确认压成一次点击，而这三句话正是这一屏存在的理由。 */}
      <fieldset className={steps.checks}>
        <legend className={steps.label}>{t("wizard.affirmHeading")}</legend>
        {AFFIRMATION_IDS.map((id) => (
          <label className={steps.check} key={id}>
            <input
              type="checkbox"
              checked={affirmations[id]}
              onChange={(event) => setAffirmation(id, event.target.checked)}
            />
            <span>{t(`wizard.affirm.${id}` as MessageKey)}</span>
          </label>
        ))}
        {/* 只有一个目标时这一条**不渲染**（画成禁用的会让人以为自己漏了一项）。
            后端对多目标自己也强制同一条，这里拦只为少发一次没意义的请求。 */}
        {validation.targets.length <= 1 ? null : (
          <label className={steps.check}>
            <input
              type="checkbox"
              checked={multiTargetAffirmed}
              onChange={(event) => setMultiTargetAffirmed(event.target.checked)}
            />
            <span>{t("wizard.multiAffirm")}</span>
          </label>
        )}
      </fieldset>

      {/* ---------------------------------------- (c) 逐字确认 */}
      <div className={steps.row}>
        <label className={steps.label} htmlFor={`${uid}-typed`}>
          {t("wizard.typedLabel")}
        </label>
        {/* 期望串渲染成键值行，**绝不进 `placeholder`**：某些浏览器会把 placeholder
            的内容参与自动填充，放里面就不再是一次确认了。 */}
        <dl className={steps.pairs}>
          <dt className={steps.pairKey}>{t("wizard.typedExpectedLabel")}</dt>
          <dd className={`${steps.pairValue} ${steps.mono}`}>
            {expected ?? t("common.unknown")}
          </dd>
        </dl>
        <input
          className={steps.input}
          id={`${uid}-typed`}
          type="text"
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          value={typedConfirmation}
          onChange={(event) => setTypedConfirmation(event.target.value)}
        />
        <p className={steps.hint}>{t("wizard.typedHint")}</p>
        {!mismatch ? null : <p className={steps.warn}>{t("wizard.typedMismatch")}</p>}
      </div>

      {/* ---------------------------------------- (d) 授权依据 */}
      {hitLabels.length === 0 ? null : (
        <AllowlistHits labels={hitLabels} onUse={setAuthorizationRef} />
      )}
      <div className={steps.row}>
        <label className={steps.label} htmlFor={`${uid}-ref`}>
          {t("home.fieldAuthorization")}
        </label>
        <input
          className={steps.input}
          id={`${uid}-ref`}
          type="text"
          autoComplete="off"
          spellCheck={false}
          value={authorizationRef}
          onChange={(event) => setAuthorizationRef(event.target.value)}
        />
        <p className={steps.hint}>{t("wizard.authRefHint")}</p>
      </div>
    </div>
  );
}

/** 一条被声明覆盖的目标。只读。 */
function DeclaredTarget({ item }: { readonly item: TargetValidation }) {
  return (
    <>
      <p className={styles.head}>
        <StatusDot tone={item.ok ? "ok" : "warn"} />
        <span className={steps.mono}>{item.raw}</span>
        {/* 点旁边必须有成句的中文：`StatusDot` 是 `aria-hidden` 的，而只靠颜色的
            区分在打印与高对比模式下会消失。 */}
        <span className={styles.verdict}>
          {t(item.ok ? "wizard.verdictOk" : "wizard.verdictBlocked")}
        </span>
      </p>

      {item.normalized === null ? null : (
        <dl className={steps.pairs}>
          <dt className={steps.pairKey}>{t("wizard.normalizedLabel")}</dt>
          <dd className={steps.pairValue}>{item.normalized.url}</dd>
          {item.resolved_ips.length === 0 ? null : (
            <>
              <dt className={steps.pairKey}>{t("wizard.resolvedLabel")}</dt>
              <dd className={steps.pairValue}>
                <ul className={styles.addresses}>
                  {item.resolved_ips.map((ip) => (
                    <li className={styles.address} key={ip.address}>
                      <span className={steps.mono}>{ip.address}</span>
                      <span>{t(`targetGuard.categories.${ip.ip_class}` as MessageKey)}</span>
                    </li>
                  ))}
                </ul>
              </dd>
            </>
          )}
        </dl>
      )}
    </>
  );
}

/**
 * 命中的授权清单条目 —— 把它们的授权编号摆出来，让用户**点一下**填进去。
 *
 * **绝不自动预填**（用户拍板）：这个字段是一句法律意义上的声明，静默替他填等于
 * 替他声明；而且"自动填了之后用户改过没有"是一个屏幕上看不出来的隐式状态。
 *
 * 失败只说一句话，**不给 `ErrorNotice`**：这是一个便利功能，不是这一步的主线，
 * 一张错误卡片会让人以为授权填不下去了。
 */
function AllowlistHits({
  labels,
  onUse,
}: {
  readonly labels: readonly string[];
  readonly onUse: (ref: string) => void;
}) {
  const { data, isError } = useQuery({
    queryKey: ["allowlist"],
    queryFn: ({ signal }) => fetchAllowlist(signal),
  });

  if (isError) {
    return <p className={steps.hint}>{t("wizard.allowlistRefUnavailable")}</p>;
  }
  if (data === undefined) {
    return <p className={steps.hint}>{t("common.loading")}</p>;
  }
  const config = data.config;
  if (config === null) {
    return <p className={steps.hint}>{t("wizard.allowlistRefUnavailable")}</p>;
  }

  const entries = labels
    .map((label) => config.entries.find((entry) => entry.label === label))
    .filter((entry): entry is AllowlistEntryView => entry !== undefined);
  if (entries.length === 0) {
    return null;
  }

  return (
    <section className={styles.declared}>
      <p className={steps.label}>{t("wizard.allowlistRefHeading")}</p>
      <ul className={styles.list}>
        {entries.map((entry) => (
          <li className={styles.card} key={entry.label}>
            <p className={styles.entryLabel}>{entry.label}</p>
            <dl className={steps.pairs}>
              <dt className={steps.pairKey}>{t("wizard.allowlistOwner")}</dt>
              <dd className={steps.pairValue}>{entry.owner}</dd>
              <dt className={steps.pairKey}>{t("wizard.allowlistRef")}</dt>
              <dd className={`${steps.pairValue} ${steps.mono}`}>{entry.authorization_ref}</dd>
            </dl>
            <div className={steps.actions}>
              <Button variant="ghost" onClick={() => onUse(entry.authorization_ref)}>
                {t("wizard.useThisRef")}
              </Button>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
