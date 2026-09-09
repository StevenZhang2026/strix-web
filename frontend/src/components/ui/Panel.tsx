import type { ReactNode } from "react";

import styles from "./Panel.module.css";

interface PanelProps {
  /** 已经是中文成句的文案，由调用方从 `messages` 取。本组件不认识文案表。 */
  readonly title: string;
  readonly children: ReactNode;
}

/**
 * 侧栏面板：一条表头下划线 + 内容区。**服务端组件**（没有任何交互）。
 *
 * 用 `<section>` + `<h2>` 而不是 `<div>` + `<div>`：这些确实是页面的次级分节，
 * 屏幕阅读器要能按标题跳转。视觉上的小字号由 CSS 给，不改语义层级。
 */
export function Panel({ title, children }: PanelProps) {
  return (
    <section className={styles.panel}>
      <h2 className={styles.head}>{title}</h2>
      <div className={styles.body}>{children}</div>
    </section>
  );
}
