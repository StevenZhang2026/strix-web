import type { ReactNode } from "react";

import { RequireSession } from "@/components/auth/RequireSession";
import { SessionExpiredMask } from "@/components/errors/SessionExpiredMask";
import { AppShell } from "@/components/layout/AppShell";

/**
 * 「已登录的应用」这一段的布局。**服务端组件。**
 *
 * =============================================================================
 * 这个路由组存在的唯一理由（T5b 拍板 2026-09-10）
 *
 * `(app)` 是一个**路由组**：括号让它不出现在 URL 里，所以 `(app)/page.tsx` 仍然是
 * `/`。它买到的东西是一条**结构性**保证 —— `/login` 在这个组**外面**，于是登录页
 * 拿不到应用外壳、拿不到会话遮罩、也不做登录检查。这三件事不需要任何人记住。
 *
 * 在 T5 里外壳挂在根布局上。那时它是无害的（顶栏当时没有导航，`SessionBadge` 未登录
 * 时渲染 `null`，页脚只打免鉴权的 `/api/health`）。但 T18/T25/T26 一加导航，登录页
 * 就会长出一排点进去 401 的链接 —— 而那时拦住它的只能是一句注释。
 * 把它换成"文件放在哪个目录"，是这个项目一贯的做法（eslint.config.mjs 顶部那句
 * "靠人记的规则等于没有规则"）。
 *
 * 代价，如实记账：`AppShell` 从 1 个调用点变成 3 个（这里 + `app/error.tsx` +
 * `app/not-found.tsx`）。那两个文件为什么不能挪进来，见它们各自的 docstring ——
 * 一条是实测结论，一条是 eslint 的硬约束。
 *
 * =============================================================================
 * 三个子元素的分工
 *
 * · `RequireSession` —— **前置式**：进入应用时压根没登录 → 跳 `/login`。
 * · `SessionExpiredMask` —— **反应式**：刚刚有一个请求 401 了 → 原地告知 + 一个
 *   去登录页的按钮，不丢掉屏幕上的东西。它从 `providers.tsx` 挪到这里，覆盖范围
 *   **一点没变**（`.backdrop` 是 `position: fixed; inset: 0`，压住顶栏靠的是 fixed，
 *   不是 DOM 位置），换来的是"遮罩结构上不可能出现在登录页上"。
 * · `AppShell` —— 顶栏 + 定宽内容区 + 页脚。
 */
export default function AppLayout({ children }: { readonly children: ReactNode }) {
  return (
    <>
      <RequireSession />
      <AppShell>{children}</AppShell>
      <SessionExpiredMask />
    </>
  );
}
