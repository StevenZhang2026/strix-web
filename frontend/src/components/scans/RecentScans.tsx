"use client";

/**
 * 首页侧栏「最近扫描」：前 5 条，每条 目标 + 状态，链到详情页。
 *
 * 与 `ScanList` 共用 `queryKey: ["scans"]`，从首页点「查看全部」时直接命中缓存。
 * 和整张表一样**不写结论句**、也不显示发现数 —— 结论只由详情页按
 * `status + exit_meaning` 判定（`ScanHeader.conclusionOf`）。
 */
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";

import { ErrorNotice } from "@/components/errors/ErrorNotice";
import { statusLabel } from "@/components/live/ScanHeader";
import { ApiError, fetchScans } from "@/lib/api/client";
import { t } from "@/lib/messages";

import { targetText } from "./ScanList";
import styles from "./RecentScans.module.css";

const LIMIT = 5;

export function RecentScans() {
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

  const recent = query.data.scans.slice(0, LIMIT);
  if (recent.length === 0) {
    return <p className={styles.note}>{t("home.recentEmpty")}</p>;
  }

  return (
    <>
      <ul className={styles.list}>
        {recent.map((scan) => (
          <li key={scan.id}>
            <Link href={`/scans/${encodeURIComponent(scan.id)}`} className={styles.item}>
              <span className={styles.target}>{targetText(scan.targets)}</span>
              <span className={styles.status}>{statusLabel(scan.status)}</span>
            </Link>
          </li>
        ))}
      </ul>
      <Link href="/scans" className={styles.link}>
        {t("scans.viewAll")}
      </Link>
    </>
  );
}
