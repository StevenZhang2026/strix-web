import { t, type MessageKey } from "@/lib/messages";

import styles from "./Findings.module.css";

/** 严重度 → 文案键与徽标样式。不在表里的原值照原样显示、用默认灰色。 */
const SEVERITY: Readonly<Record<string, { readonly label: MessageKey; readonly cls: string }>> = {
  critical: { label: "severity.critical", cls: styles.sevCritical },
  high: { label: "severity.high", cls: styles.sevHigh },
  medium: { label: "severity.medium", cls: styles.sevMedium },
  low: { label: "severity.low", cls: styles.sevLow },
  info: { label: "severity.info", cls: "" },
};

/** 展开后的字段，顺序固定；`code` 为真的用 `<pre>`（证据/代码要保留原始排版）。 */
const FIELDS: readonly { readonly key: string; readonly label: MessageKey; readonly code: boolean }[] = [
  { key: "description", label: "findings.field.description", code: false },
  { key: "impact", label: "findings.field.impact", code: false },
  { key: "technical_analysis", label: "findings.field.technicalAnalysis", code: false },
  { key: "poc_description", label: "findings.field.pocDescription", code: false },
  { key: "poc_script_code", label: "findings.field.pocScriptCode", code: true },
  { key: "evidence", label: "findings.field.evidence", code: true },
  { key: "remediation_steps", label: "findings.field.remediationSteps", code: false },
  { key: "code_locations", label: "findings.field.codeLocations", code: true },
  { key: "confidence", label: "findings.field.confidence", code: false },
  { key: "assumptions", label: "findings.field.assumptions", code: false },
  { key: "counterevidence", label: "findings.field.counterevidence", code: false },
];

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** 缺失、空串、空数组/空对象都算"没有"，那一段不渲染。 */
function isEmpty(value: unknown): boolean {
  if (value === undefined || value === null) return true;
  if (typeof value === "string") return value.trim() === "";
  if (Array.isArray(value)) return value.length === 0;
  if (typeof value === "object") return Object.keys(value).length === 0;
  return false;
}

/**
 * 一条 Strix 原始发现。内容来自扫描器（间接来自目标站点），**不可信**：
 * 只以 React 文本节点出现，不渲染 Markdown、endpoint 不做成链接（点了就是对目标发请求）。
 * 展开状态交给原生 `<details>`，不自己管。
 */
export function FindingItem({ finding }: { readonly finding: Readonly<Record<string, unknown>> }) {
  const severity = str(finding.severity).toLowerCase();
  const known = SEVERITY[severity];
  const endpoint = [str(finding.method), str(finding.endpoint)].filter((s) => s !== "").join(" ");
  const cvss = finding.cvss;
  const cwe = str(finding.cwe);

  return (
    <li className={styles.item}>
      <details>
        <summary className={styles.summary}>
          <span className={known?.cls ? `${styles.badge} ${known.cls}` : styles.badge}>
            {known ? t(known.label) : str(finding.severity) || t("common.unknown")}
          </span>
          <span className={styles.title}>{str(finding.title)}</span>
          {endpoint !== "" ? <code className={styles.endpoint}>{endpoint}</code> : null}
          {typeof cvss === "number" ? (
            <span className={styles.meta}>
              {t("findings.cvss")} {cvss}
            </span>
          ) : null}
          {cwe !== "" ? <span className={styles.meta}>{cwe}</span> : null}
        </summary>
        <div className={styles.body}>
          {FIELDS.map(({ key, label, code }) => {
            const value = finding[key];
            if (isEmpty(value)) return null;
            const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
            return (
              <section key={key} className={styles.section}>
                <h4 className={styles.label}>{t(label)}</h4>
                {code || typeof value !== "string" ? (
                  <pre className={styles.code}>{text}</pre>
                ) : (
                  <p className={styles.text}>{text}</p>
                )}
              </section>
            );
          })}
        </div>
      </details>
    </li>
  );
}
