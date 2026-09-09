"use client";

import { formatTimestamp } from "@/lib/format";
import { t } from "@/lib/messages";

import styles from "./StampBar.module.css";

/**
 * 授权印记条要显示的五个字段。字段集合是**设计定死的**，不是可配置的 ——
 * 这五项就是「谁在什么时候、依据什么、授权对哪个目标动手」的完整回答，
 * 少一项这条印记就不成立。所以这里是一个具体的 interface，不是 `Field[]`。
 */
export interface StampBarData {
  readonly target: string;
  /** 启动扫描前重新解析到的地址。与声明时不一致会被后端拒绝（`dns_changed`）。 */
  readonly resolvedIp: string;
  readonly authorizationRef: string;
  readonly operator: string;
  /** ISO 8601 字符串，由后端给。渲染成本机时区，**绝不缀 `Z`**（见 `lib/format.ts`）。 */
  readonly declaredAt: string;
}

interface StampBarProps {
  /** `null` = 还在取数。渲染骨架，**不改变任何一栏的位置与整条的高度**。 */
  readonly data: StampBarData | null;
  /** 右侧状态印记，已成句中文（`messages.status.*`）。`null` = 还在取数。 */
  readonly state: string | null;
}

/**
 * 授权印记条。**客户端组件**，因为 `formatTimestamp` 按浏览器时区渲染
 * （在服务端渲染会得到容器时区的字符串，然后 hydration 不匹配）。
 *
 * =============================================================================
 * 给 T17 的用法说明
 *
 * ```tsx
 * const { data } = useQuery({ queryKey: ["scan", id], queryFn: () => fetchScan(id) });
 * <StampBar
 *   data={data === undefined ? null : toStampBarData(data)}
 *   state={data === undefined ? null : t(statusKey(data.status))}
 * />
 * ```
 *
 * **直接这么用就行，不要在外面再包一层 `if (isLoading) return <Skeleton/>`。**
 * 那是这个组件存在的全部理由：换一个骨架组件出来，一定会跳。
 *
 * 「零布局位移」靠三件事共同成立，改任何一件都要重新量：
 *   1. 标签（`t('scan.stampTarget')` 那五个）**两态都渲染**。它们是静态文案，
 *      取数中也早就知道，藏起来只会让条子变矮。
 *   2. 每一栏有 `min-width`（`.fTarget` 等五个类），骨架块撑到那个宽度；
 *      加载完成后真值若更短也不会缩回去。
 *   3. 骨架块高度写成 `calc(var(--fs-3) * 1.4)`，与 `.value` 的行盒精确等高。
 *      那个 `1.4` 在 CSS 里是两处共用的契约，注释已标明。
 *
 * 目标那一栏 `white-space: nowrap` + 省略号：长 URL 换行会同时撑高条子和推走
 * 后面几栏。完整值放在 `title` 里，鼠标停一下能看全。
 */
export function StampBar({ data, state }: StampBarProps) {
  const loading = data === null;

  return (
    <div className={styles.bar} aria-busy={loading}>
      {loading ? <span className={styles.srOnly}>{t("common.loading")}</span> : null}

      <div className={`${styles.field} ${styles.fTarget}`}>
        <span className={styles.key}>{t("scan.stampTarget")}</span>
        {data === null ? (
          <span className={styles.placeholder} aria-hidden="true" />
        ) : (
          <span className={`${styles.value} mono`} title={data.target}>
            {data.target}
          </span>
        )}
      </div>

      <div className={`${styles.field} ${styles.fResolved}`}>
        <span className={styles.key}>{t("scan.stampResolved")}</span>
        {data === null ? (
          <span className={styles.placeholder} aria-hidden="true" />
        ) : (
          <span className={`${styles.value} mono`}>{data.resolvedIp}</span>
        )}
      </div>

      <div className={`${styles.field} ${styles.fAuthorization}`}>
        <span className={styles.key}>{t("scan.stampAuthorization")}</span>
        {data === null ? (
          <span className={styles.placeholder} aria-hidden="true" />
        ) : (
          <span className={styles.value}>{data.authorizationRef}</span>
        )}
      </div>

      <div className={`${styles.field} ${styles.fOperator}`}>
        <span className={styles.key}>{t("scan.stampOperator")}</span>
        {data === null ? (
          <span className={styles.placeholder} aria-hidden="true" />
        ) : (
          <span className={styles.value}>{data.operator}</span>
        )}
      </div>

      <div className={`${styles.field} ${styles.fDeclaredAt}`}>
        <span className={styles.key}>{t("scan.stampDeclaredAt")}</span>
        {data === null ? (
          <span className={styles.placeholder} aria-hidden="true" />
        ) : (
          <span className={styles.value}>{formatTimestamp(data.declaredAt)}</span>
        )}
      </div>

      <span
        className={state === null ? `${styles.state} ${styles.statePlaceholder}` : styles.state}
      >
        {/* 骨架态放一个占位字符而不是留空：空字符串会让这枚印记的行盒塌掉，
            边框跟着变矮 —— 又是一次跳动。字是透明的，`aria-hidden` 让它不被读出来。 */}
        {state === null ? <span aria-hidden="true">—</span> : state}
      </span>
    </div>
  );
}
