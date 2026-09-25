"use client";

/**
 * `/scans` 的整张表。客户端组件，因为 `lib/format.ts` 按本机时区渲染时间。
 *
 * 两条发布阻断约束：
 * - **列表不写结论句。** 「未发现漏洞」只能由详情页按 `status + exit_meaning` 判定
 *   （`ScanHeader.conclusionOf`）；这里只有计数，任何一句结论都会绕开那道判定。
 * - **进行中的扫描，发现格显示 — 而不是 0。** 进行中的 0 读作「没发现」，而那只是还没跑完。
 *   其他状态照实显示，0 就是 0。
 *
 * 整行可点靠第一格里的 `<Link>`（键盘、中键、右键新标签都照常工作），
 * 行 hover 只是 CSS 反馈，不挂 `onClick`。
 */
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";

import { ErrorNotice } from "@/components/errors/ErrorNotice";
import { statusLabel } from "@/components/live/ScanHeader";
import { ApiError, fetchScans, type ScanSummary } from "@/lib/api/client";
import { formatTimestamp, formatUsd } from "@/lib/format";
import { t } from "@/lib/messages";

import styles from "./ScanList.module.css";

const IN_PROGRESS: ReadonlySet<string> = new Set(["starting", "running"]);

export function targetText(targets: readonly string[]): string {
  const first = targets[0] ?? "";
  return targets.length > 1 ? `${first} +${targets.length - 1}` : first;
}

function Count({ value, sevClass }: { value: number; sevClass: string }) {
  return <span className={value > 0 ? sevClass : styles.zero}>{value}</span>;
}

function Findings({ scan }: { scan: ScanSummary }) {
  if (IN_PROGRESS.has(scan.status)) {
    return <span className={styles.zero}>—</span>;
  }
  return (
    <span className={styles.counts}>
      <Count value={scan.count_critical} sevClass={styles.sev1} />
      <Count value={scan.count_high} sevClass={styles.sev2} />
      <Count value={scan.count_medium} sevClass={styles.sev3} />
      <Count value={scan.count_low} sevClass={styles.sev4} />
    </span>
  );
}

export function ScanList() {
  const query = useQuery({ queryKey: ["scans"], queryFn: ({ signal }) => fetchScans(signal) });

  if (query.isPending) {
    return <p className={styles.note}>{t("common.loading")}</p>;
  }
  if (query.isError) {
    const error = query.error;
    return error instanceof ApiError ? (
      <ErrorNotice code={error.code} traceId={error.traceId} />
    ) : (
      <ErrorNotice code="internal_error" />
    );
  }

  const { scans, truncated } = query.data;
  if (scans.length === 0) {
    return <p className={styles.note}>{t("scans.empty")}</p>;
  }

  return (
    <>
      <table className={styles.table}>
        <thead>
          <tr>
            <th>{t("scans.colCreated")}</th>
            <th>{t("scans.colTarget")}</th>
            <th>{t("scans.colStatus")}</th>
            <th className={styles.num}>
              {t("scans.colFindings")}
              <span className={styles.counts}>
                <span>{t("severity.critical")}</span>
                <span>{t("severity.high")}</span>
                <span>{t("severity.medium")}</span>
                <span>{t("severity.low")}</span>
              </span>
            </th>
            <th className={styles.num}>{t("scans.colCost")}</th>
          </tr>
        </thead>
        <tbody>
          {scans.map((scan) => (
            <tr key={scan.id} className={styles.row}>
              <td>
                <Link href={`/scans/${encodeURIComponent(scan.id)}`} className={styles.rowLink}>
                  {formatTimestamp(scan.created_at)}
                </Link>
              </td>
              <td className={styles.target}>{targetText(scan.targets)}</td>
              <td>{statusLabel(scan.status)}</td>
              <td className={styles.num}>
                <Findings scan={scan} />
              </td>
              <td className={styles.num}>
                {formatUsd(scan.cost_usd)} / {formatUsd(scan.max_budget_usd)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {truncated ? <p className={styles.note}>{t("scans.truncatedNote")}</p> : null}
    </>
  );
}
