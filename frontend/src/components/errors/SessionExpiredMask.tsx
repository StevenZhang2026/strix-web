"use client";

import Link from "next/link";

import { t } from "@/lib/messages";
import { useSessionStore } from "@/lib/stores/session";

import styles from "./SessionExpiredMask.module.css";

/**
 * 会话失效时的全屏遮罩。
 *
 * =============================================================================
 * T5b 补上了「重新登录」按钮，并**修正 T5 留在这里的一句错话**
 *
 * T5 时这里写着"T5b 只需要加一个 `<Link>`，**机制一行都不用改**"。
 * **那句话不成立。** 它描述的触发路径是对的 ——
 *   `lib/api/client.ts` 的 401 → `notifyUnauthenticated()` →
 *   `providers.tsx` 注册的回调 → `useSessionStore.markExpired()` → 本组件出现
 * —— 错的是它的隐含前提「401 只可能意味着会话失效」。
 * `POST /api/auth/login` 打错口令回的也是 401（`InvalidCredentialsError`），
 * 于是这个遮罩会盖住登录表单，对着一个刚刚登录失败的人说"请重新登录"。
 * 修法是给 `apiFetch` 加一个显式的一次性开关 `isAuthAttempt`，只有 `login()` 传它；
 * 判定函数 `isUnauthenticated()` 一个字没改（它回答的是"这个响应是什么"）。
 *
 * 同时 T5 那句"T5b 的 middleware 会把没 cookie 的请求重定向到 /login"也作废了：
 * **本项目不加 middleware**，未登录跳转在 `components/auth/RequireSession.tsx`。
 * 理由全文见 `lib/api/client.ts` 第二节。
 *
 * 本组件现在由 `app/(app)/layout.tsx` 渲染（T5 时在 `providers.tsx`）。
 * 覆盖范围一点没变 —— `.backdrop` 是 `position: fixed; inset: 0`，压住顶栏靠的是
 * fixed 而不是 DOM 位置；换来的是"它结构上不可能出现在 `/login` 上"。
 *
 * =============================================================================
 * 与 `RequireSession` 的分工
 *
 * · 本组件（**反应式**）：刚刚有一个请求 401 了 → 原地告知，不丢掉屏幕上的东西。
 * · `RequireSession`（**前置式**）：进入应用时压根没登录 → 直接跳 `/login`。
 * 它在 `expired === true` 时刻意让位（不跳转），否则这段解释会被一次静默跳转顶掉。
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
        {/* `<Link>` 而不是 `<button onClick={router.push}>`：这是一次导航，
            应当是一个真的可以中键打开、可以看到目标地址的链接。
            **刻意不在这里调 `clearExpired()`** —— 会话此刻确实是失效的，
            清标志的唯一正确时机是"重新登录成功"，那在 `LoginForm` 里。 */}
        <p className={styles.actions}>
          <Link className={styles.action} href="/login">
            {t("session.toLogin")}
          </Link>
        </p>
      </div>
    </div>
  );
}
