import Link from "next/link";
import type { ReactNode } from "react";

import styles from "./Button.module.css";

/**
 * 长得像按钮的**链接**。样式**复用 `Button.module.css`** —— 不新写一份按钮样式。
 *
 * 为什么是两个组件而不是给 `Button` 加一个 `href`：一个组件按 prop 渲染
 * `<button>` 或 `<a>` 比两个各自直白的组件难读，而调用方要的东西本来就不同
 * （一个是 `onClick`，一个是导航）。
 *
 * 不需要 `"use client"`：纯渲染，`next/link` 在服务端组件里可用。
 */
type ButtonVariant = "primary" | "ghost" | "stop";

interface ButtonLinkProps {
  /** 已经成句的中文，由调用方从 `messages` 取。 */
  readonly children: ReactNode;
  readonly href: string;
  readonly variant?: ButtonVariant;
}

// 与 `Button.tsx` 同一张显式表，理由也同 —— `styles[variant]` 拼错了要到运行时
// 才发现样式没生效。
const VARIANT_CLASS: Record<ButtonVariant, string | undefined> = {
  primary: undefined,
  ghost: styles.ghost,
  stop: styles.stop,
};

export function ButtonLink({ children, href, variant = "primary" }: ButtonLinkProps) {
  const variantClass = VARIANT_CLASS[variant];
  return (
    <Link
      href={href}
      className={variantClass === undefined ? styles.btn : `${styles.btn} ${variantClass}`}
    >
      {children}
    </Link>
  );
}
