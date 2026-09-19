// 服务端组件。交互全在客户端叶子 `Wizard` 里 —— `app/` 下只有 providers.tsx
// 与 error.tsx 允许 "use client"。
import { Wizard } from "@/components/wizard/Wizard";
import { t } from "@/lib/messages";

import styles from "./page.module.css";

export default function NewScanPage() {
  return (
    <div className={styles.page}>
      <h1 className={styles.pageTitle}>{t("wizard.heading")}</h1>
      <p className={styles.lede}>{t("wizard.lede")}</p>
      <Wizard />
    </div>
  );
}
