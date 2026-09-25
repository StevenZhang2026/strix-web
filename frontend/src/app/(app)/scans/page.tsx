// 服务端组件。表格要按本机时区格式化时间，所以整张表在客户端根 `ScanList` 里 ——
// `app/` 下只有 providers.tsx 与 error.tsx 允许 "use client"。
import { ScanList } from "@/components/scans/ScanList";
import { t } from "@/lib/messages";

import styles from "./page.module.css";

export default function ScansPage() {
  return (
    <div className={styles.page}>
      <h1 className={styles.pageTitle}>{t("nav.scans")}</h1>
      <ScanList />
    </div>
  );
}
