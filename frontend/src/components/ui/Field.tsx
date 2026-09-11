import { Fragment } from "react";

import styles from "./Field.module.css";

export interface DocketField {
  readonly id: string;
  /** 字段名，已成句中文。 */
  readonly label: string;
  /** 尚未填写时显示的灰色提示，已成句中文。 */
  readonly blank: string;
}

interface FieldTableProps {
  readonly fields: readonly DocketField[];
}

/**
 * 工单字段表。**服务端组件。**
 *
 * 用 `<dl>` / `<dt>` / `<dd>`：这里确实是"字段名—字段值"，是 `<dl>` 的本义。
 * grid 布局直接作用在 `<dl>` 上，`<dt>` 与 `<dd>` 是它的格子 —— 中间**不许**
 * 包一层 `<div>`，那会破坏 `dl` 的内容模型（也会破坏 grid）。
 *
 * 本轮只渲染空白态（虚线 + 灰提示）。真正的输入控件是 T18 的向导，
 * 那时这个组件会长出一个"值"分支，而不是被替换掉 —— 字段名、竖线、行高
 * 都必须与本轮完全一致，否则从首页走进向导会看到整张表跳一下。
 *
 * ⚠️ **2026-09-10 起本组件暂时没有调用方。** 首页改成"介绍 + 五步预告"之后，那五条
 * 走的是 `(app)/page.tsx` 里的 `<ol>`（有序步骤），不再是 `<dl>`（字段名—字段值）。
 * 刻意保留它而不是删掉：它是 T5 交付的"工单字段长什么样"这条设计约定本身，T18 直接
 * 消费；首页那个 `<ol>` 的 `104px` 定宽也是逐值照着这里的 grid 抄的，删了就没有对照物。
 * 别因为搜不到调用方就以为它已经被替代了。
 */
export function FieldTable({ fields }: FieldTableProps) {
  return (
    <dl className={styles.form}>
      {fields.map((field) => (
        // `Fragment` 而不是 `<>`：一对 dt/dd 是一条记录，key 必须挂在这一对上。
        // 用简写语法就没地方挂 key，分别给 dt 和 dd 挂 key 又会让 React 把它们
        // 当成两条独立记录来对比 —— 插入一行时会错位复用。
        <Fragment key={field.id}>
          <dt className={styles.label}>{field.label}</dt>
          <dd className={styles.value}>
            <span className={styles.blank}>{field.blank}</span>
          </dd>
        </Fragment>
      ))}
    </dl>
  );
}
