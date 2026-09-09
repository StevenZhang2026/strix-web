import { StatusDot, type DotTone } from "./StatusDot";
import styles from "./Rows.module.css";

export interface StatusRow {
  /** React 的 key。用调用方稳定的标识，不要用数组下标。 */
  readonly id: string;
  /** 已经成句的中文。 */
  readonly label: string;
  /** 右侧的值。`null` = 这一行只有标签（例如「遥测已关闭」自己就是结论）。 */
  readonly value: string | null;
  readonly tone: DotTone;
}

interface RowsProps {
  readonly rows: readonly StatusRow[];
}

/**
 * 面板里的状态行列表。**服务端组件。**
 *
 * 刻意**不用** `<dl>`：这些行不是"术语—释义"，而是"检查项—结果"，
 * 而且每行前面还有一个状态点。用 `<ul>` 更贴近它真实的语义（一组同类条目）。
 * 工单里那种真正的字段表在 `Field.tsx`，那个才是 `<dl>`。
 */
export function Rows({ rows }: RowsProps) {
  return (
    <ul className={styles.rows}>
      {rows.map((row) => (
        <li key={row.id} className={styles.row}>
          <StatusDot tone={row.tone} />
          <span>{row.label}</span>
          {row.value === null ? null : <span className={styles.value}>{row.value}</span>}
        </li>
      ))}
    </ul>
  );
}
