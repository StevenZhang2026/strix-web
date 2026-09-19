"use client";

import Link from "next/link";

import { CredentialSummary } from "@/components/credentials/CredentialForm";
import { StatusDot } from "@/components/ui/StatusDot";
import { t } from "@/lib/messages";
import { useKeysStore } from "@/lib/stores/keys";

import styles from "./CredentialsPanel.module.css";

/**
 * 首页侧栏「模型凭据」那一片。**面板与标题留在服务端组件里**，只有这几行值是
 * 客户端的 —— 和 `components/system/ReadyRows.tsx` 完全同一个形状。
 *
 * 入口是**文字链接**而不是按钮：`Button` 渲染的是 `<button>`，没有 `href`，
 * 为它造第二份按钮样式就是重复实现。
 *
 * 已登记态直接复用 `CredentialSummary` —— 「先 `DELETE` 再清本地 handle」这条
 * 顺序只该有一处实现。
 */
export function CredentialsPanel() {
  const handle = useKeysStore((s) => s.handle);

  if (handle !== null) {
    return <CredentialSummary handle={handle} />;
  }

  return (
    <>
      {/* 「凭据随时会没」写在明面上。这句话是产品陈述，不是错误提示 ——
          方块点（`warn`）表示"需要你处理"，而不是"出了故障"。 */}
      <p className={styles.note}>
        <StatusDot tone="warn" />
        {t("credentials.none")}
      </p>
      <p className={styles.actions}>
        <Link className={styles.link} href="/credentials">
          {t("credentials.provide")}
        </Link>
      </p>
    </>
  );
}
