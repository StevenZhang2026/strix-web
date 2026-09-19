import { CredentialForm } from "@/components/credentials/CredentialForm";
import { t } from "@/lib/messages";

import styles from "./page.module.css";

/**
 * `/credentials`。**服务端组件**（`app/` 下只有 `providers.tsx` 与 `error.tsx`
 * 许可带 `"use client"`），交互全部下沉到 `components/credentials/CredentialForm.tsx`
 * 这个客户端叶子。
 *
 * 这一页在路由组 `(app)` 里面，所以外壳、会话遮罩、登录检查都由
 * `(app)/layout.tsx` 给了 —— 这里一行都不用重复。
 *
 * 刻意**不设** `export const metadata`：根布局的 `title` 是 `app.name`，理由与
 * `/login` 那页同（要做成"模型凭据 · Strix 控制台"就得在根布局引 `title.template`）。
 */
export default function CredentialsPage() {
  return (
    <div className={styles.page}>
      <h1 className={styles.pageTitle}>{t("credentials.panelTitle")}</h1>
      {/* 这一页**整块**都是状态相关的，所以壳里只剩标题（2026-09-19 收货时改）：
          原先这里还有一句 `credentials.none` 作为主文、外面套一个标题为
          `credentials.provide`（"现在提供凭据"）的 `Panel` —— 两样东西对**已登记**
          的人都是假话："还没有提供凭据"会压在那份已登记凭据的摘要上面。
          判据和首页那个按钮是同一条：宁可少一层框，也不要在屏幕上写错事实。 */}
      <CredentialForm />
    </div>
  );
}
