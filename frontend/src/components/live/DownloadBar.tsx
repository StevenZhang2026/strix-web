import { t } from "@/lib/messages";

import styles from "./DownloadBar.module.css";

/**
 * 终态扫描的下载条：三个直通导出 + 原始产物 zip。
 *
 * 普通同源 `<a download>` 导航：浏览器会带上 `HttpOnly` 会话 cookie，所以不用 fetch/blob。
 * 无 hook，被客户端组件 `LivePanel` 引用也无需 `"use client"`。
 */
export function DownloadBar({ scanId }: { readonly scanId: string }) {
  const base = `/api/scans/${encodeURIComponent(scanId)}`;
  return (
    <section className={styles.root}>
      <div className={styles.links}>
        <span className={styles.label}>{t("scan.downloadsTitle")}</span>
        <a className={styles.link} href={`${base}/export/md`} download>
          {t("scan.downloadReportMd")}
        </a>
        <a className={styles.link} href={`${base}/export/csv`} download>
          {t("scan.downloadCsv")}
        </a>
        <a className={styles.link} href={`${base}/export/sarif`} download>
          {t("scan.downloadSarif")}
        </a>
        <a className={styles.link} href={`${base}/raw.zip`} download>
          {t("scan.downloadRawZip")}
        </a>
      </div>
      <p className={styles.hint}>{t("scan.downloadRawZipHint")}</p>
    </section>
  );
}
