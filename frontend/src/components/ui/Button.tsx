"use client";

import type { ReactNode } from "react";

import styles from "./Button.module.css";

/**
 * 三个变体，对应三种语义，**不是三种颜色**：
 *   · `primary` —— 主操作，填色的闸门按钮（`--gate` 只用于"越过一道授权边界"）
 *   · `ghost`   —— 次要操作，同色不填
 *   · `stop`    —— 中止正在发生的事
 */
type ButtonVariant = "primary" | "ghost" | "stop";

interface ButtonProps {
  /** 已经成句的中文，由调用方从 `messages` 取。本组件不认识文案表。 */
  readonly children: ReactNode;
  readonly variant?: ButtonVariant;
  readonly type?: "button" | "submit";
  readonly disabled?: boolean;
  readonly onClick?: () => void;
  /**
   * 无障碍补充说明。用在"按钮自身文案不足以说明为什么它是灰的"这种场合 ——
   * 本轮首页那三个指向尚未存在页面的入口就是。
   */
  readonly ariaDescribedBy?: string;
}

// 变体 → 类名。刻意写成一张**显式的表**而不是 `styles[variant]`：
// CSS Modules 的类型是索引签名，`styles[variant]` 拼错了要到运行时才发现样式没生效。
const VARIANT_CLASS: Record<ButtonVariant, string | undefined> = {
  primary: undefined, // 基态就是主操作，不需要额外类名。
  ghost: styles.ghost,
  stop: styles.stop,
};

/**
 * 按钮。
 *
 * `type` 默认 `"button"`：HTML 的默认是 `"submit"`，在表单里会意外提交。
 * 这个项目的表单提交都要经逐字确认，一次意外提交等于一次意外攻击。
 */
export function Button({
  children,
  variant = "primary",
  type = "button",
  disabled = false,
  onClick,
  ariaDescribedBy,
}: ButtonProps) {
  const variantClass = VARIANT_CLASS[variant];
  return (
    <button
      type={type}
      className={variantClass === undefined ? styles.btn : `${styles.btn} ${variantClass}`}
      disabled={disabled}
      onClick={onClick}
      aria-describedby={ariaDescribedBy}
    >
      {children}
    </button>
  );
}
