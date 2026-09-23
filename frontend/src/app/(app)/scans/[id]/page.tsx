// 服务端组件。交互全在客户端根 `LivePanel` 里 —— `app/` 下只有 providers.tsx
// 与 error.tsx 允许 "use client"。
import { LivePanel } from "@/components/live/LivePanel";
import { t } from "@/lib/messages";

import styles from "./page.module.css";

export default async function ScanPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <div className={styles.page}>
      <h1 className={styles.pageTitle}>{t("live.pageTitle")}</h1>
      <LivePanel scanId={id} />
    </div>
  );
}
