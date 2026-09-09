import type { Metadata, Viewport } from "next";

import { AppShell } from "@/components/layout/AppShell";
import { t } from "@/lib/messages";

import { Providers } from "./providers";
import "./globals.css";

export const metadata: Metadata = {
  title: t("app.name"),
  description: t("app.footerLocal"),
  /**
   * 本机工具，不该被任何东西索引。它监听在 `127.0.0.1:443`，理论上爬不到，
   * 但"理论上到不了"不该是唯一的防线。
   */
  robots: { index: false, follow: false },
  /**
   * 不发 Referer。这个控制台的 URL 里会出现扫描 id；哪天页面上有一个外链
   * （报告里的 CVE 链接就是候选），Referer 会把它带出去。
   * 这一行让"以后不会有人不小心泄漏 URL"变成结构性事实。
   */
  referrer: "no-referrer",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
};

/**
 * 根布局。**服务端组件。**
 *
 * `lang="zh-CN"`：UI 全中文（CLAUDE.md §错误与文案）。这不只是元数据 ——
 * 浏览器按它选中日韩字形（同一个码位在 zh 和 ja 下字形不同），
 * 也按它决定断行规则。
 *
 * `Providers` 是整棵树唯一的客户端根，包在 `AppShell` **外面**：
 * 会话失效遮罩要能压住顶栏和页脚，不能只压住主内容区。
 */
export default function RootLayout({ children }: { readonly children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
