import styles from "./StatusDot.module.css";

/** `ok` 圆点 / `warn` 方块 / `idle` 空心。形状与颜色成对变化，见同名 CSS。 */
export type DotTone = "ok" | "warn" | "idle";

interface StatusDotProps {
  readonly tone: DotTone;
}

const TONE_CLASS: Record<DotTone, string | undefined> = {
  ok: undefined, // 基态就是圆点。
  warn: styles.warn,
  idle: styles.idle,
};

/**
 * 状态点。**服务端组件**（纯装饰性标记，没有交互）。
 *
 * `aria-hidden`：这个点旁边一定有一句成句的中文说明它是什么状态（例如
 * 「Docker 可达 / 尚未检测」）。让屏幕阅读器再读一遍"图形"只会重复。
 * 如果哪天出现了"只有点、没有文字"的用法，那是那处用法错了，不是这里该加 label。
 */
export function StatusDot({ tone }: StatusDotProps) {
  const toneClass = TONE_CLASS[tone];
  return (
    <span
      aria-hidden="true"
      className={toneClass === undefined ? styles.dot : `${styles.dot} ${toneClass}`}
    />
  );
}
