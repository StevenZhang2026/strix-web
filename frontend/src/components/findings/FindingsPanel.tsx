import { t } from "@/lib/messages";

import { FindingItem } from "./FindingItem";
import styles from "./Findings.module.css";

type Finding = Readonly<Record<string, unknown>>;

const SEVERITY_RANK: Readonly<Record<string, number>> = { critical: 0, high: 1, medium: 2, low: 3 };

function rank(f: Finding): number {
  const s = typeof f.severity === "string" ? f.severity.toLowerCase() : "";
  return SEVERITY_RANK[s] ?? 4;
}

function cvss(f: Finding): number {
  // 非数字排在同级最后。
  return typeof f.cvss === "number" && Number.isFinite(f.cvss) ? f.cvss : -Infinity;
}

/** critical→high→medium→low→其它；同级按 CVSS 降序。 */
function sortFindings(findings: readonly Finding[]): Finding[] {
  return [...findings].sort((a, b) => rank(a) - rank(b) || cvss(b) - cvss(a));
}

/** tab 上的条数：单独元素渲染数字，组件里不出现括号等字面量。 */
export function FindingsCount({ count }: { readonly count: number }) {
  return <span className={styles.count}>{count}</span>;
}

/**
 * 扫描页「发现」tab：Strix 原文，扫描进行中也可看。数据由 `LivePanel` 从 `GET /api/scans/{id}`
 * 传入（不新增请求）；`vuln.add` 帧只触发该查询刷新，不在前端维护第二份列表。
 */
export function FindingsPanel({
  findings,
  inProgress,
}: {
  readonly findings: readonly Finding[] | undefined;
  readonly inProgress: boolean;
}) {
  return (
    <div className={styles.root}>
      <p className={styles.hint}>{t("findings.hint")}</p>
      {findings === undefined ? (
        <p className={styles.hint}>{t("common.loading")}</p>
      ) : findings.length === 0 ? (
        <p className={styles.hint}>{t(inProgress ? "findings.emptyRunning" : "findings.emptyDone")}</p>
      ) : (
        <ul className={styles.list}>
          {sortFindings(findings).map((f, i) => (
            // 原始 `id` 不保证存在/唯一，拼上下标兜底。
            <FindingItem key={`${String(f.id)}-${i}`} finding={f} />
          ))}
        </ul>
      )}
    </div>
  );
}
