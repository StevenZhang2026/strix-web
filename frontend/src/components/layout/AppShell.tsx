import type { ReactNode } from "react";

import { t } from "@/lib/messages";

import { SessionBadge } from "./SessionBadge";
import { VersionFooter } from "./VersionFooter";
import styles from "./AppShell.module.css";

/**
 * 顶栏 + 定宽内容区 + 页脚。**服务端组件**（两个异步的小块下沉成了客户端叶子）。
 *
 * 顶栏刻意**没有导航菜单**：本轮只有首页一个页面，一条只有一项的导航是噪音。
 * `messages.nav.*` 里那几条文案已经就位，T18/T25/T26 把页面建起来时再加。
 *
 * 顶栏右侧也刻意**没有**样张里那句「本机环境就绪」：后端到 T5 只有四个路由
 * （`/api/health`、`/api/auth/{login,logout,me}`），没有任何系统状态接口。
 * 硬写一句"就绪"就是编造 —— 那种 UI 在真的不就绪时也照样说就绪。T3/T26 出
 * `/api/system/status` 之后再把它加回来。
 */
export function AppShell({ children }: { readonly children: ReactNode }) {
  return (
    <>
      <div className={styles.topbar}>
        <div className={styles.topbarIn}>
          <div className={styles.brand}>
            {t("app.name")}
            <em className={styles.tagline}>{t("app.tagline")}</em>
          </div>
          <div className={styles.topbarRight}>
            <SessionBadge />
          </div>
        </div>
      </div>

      <div className={styles.shell}>
        <main>{children}</main>

        <footer className={styles.foot}>
          <VersionFooter />
          <span>{t("app.footerLocal")}</span>
        </footer>
      </div>
    </>
  );
}
