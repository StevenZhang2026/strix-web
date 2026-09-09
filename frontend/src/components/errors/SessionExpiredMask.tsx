"use client";

import { t } from "@/lib/messages";
import { useSessionStore } from "@/lib/stores/session";

import styles from "./SessionExpiredMask.module.css";

/**
 * 会话失效时的全屏遮罩。
 *
 * =============================================================================
 * 本轮**只有告知，没有按钮**（2026-09-09 拍板）
 *
 * T5 结束时 `/login` 这个页面还不存在。一个点下去 404 的按钮比没有按钮更糟 ——
 * 它把"你需要重新登录"这条正确的信息变成了"这个工具坏了"。
 * 所以这一轮只呈现 `session.expiredTitle` + `session.expiredDetail`，
 * 用户按浏览器刷新即可（T5b 的 middleware 会把没 cookie 的请求重定向到 /login）。
 *
 * **T5b 只需要在下面那个位置加一个 `<Link href="/login">` 按钮**
 * （文案 `session.toLogin` 已经在文案表里了），机制一行都不用改：
 * 触发路径仍然是 `lib/api/client.ts` 的 401 → `notifyUnauthenticated()` →
 * `providers.tsx` 注册的回调 → `useSessionStore.markExpired()` → 本组件出现。
 *
 * =============================================================================
 * 为什么不是 `<dialog>`
 *
 * 这不是一个"可以关掉的对话框"。关掉它之后页面上的一切操作都会立刻再撞一次 401，
 * 于是它又出现 —— 给一个必然回弹的东西加关闭按钮是在骗人。
 * 用普通元素 + `role="alertdialog"` + `aria-live`，让辅助技术立刻读出来。
 */
export function SessionExpiredMask() {
  const expired = useSessionStore((state) => state.expired);

  if (!expired) {
    return null;
  }

  return (
    <div
      className={styles.backdrop}
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="session-expired-title"
      aria-describedby="session-expired-detail"
    >
      <div className={styles.sheet}>
        <p className={styles.title} id="session-expired-title">
          {t("session.expiredTitle")}
        </p>
        <p className={styles.detail} id="session-expired-detail">
          {t("session.expiredDetail")}
        </p>
        {/* T5b：在这里加 `<Link href="/login">{t("session.toLogin")}</Link>`。
            位置刻意留白，不放占位元素 —— 空占位是死代码。 */}
      </div>
    </div>
  );
}
