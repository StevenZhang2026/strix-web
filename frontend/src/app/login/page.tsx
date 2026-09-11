import { LoginForm } from "@/components/auth/LoginForm";
import { t } from "@/lib/messages";

import styles from "./page.module.css";

/**
 * `/login`。**服务端组件**（eslint 封死了 `app/` 下的 `"use client"`），
 * 交互全部下沉到 `components/auth/LoginForm.tsx` 这个客户端叶子。
 *
 * =============================================================================
 * 它在路由组 `(app)` **外面**，所以结构上就没有：应用外壳（顶栏/页脚/以后的导航）、
 * 会话失效遮罩、`RequireSession` 的登录检查。这三件事不靠任何人记住 ——
 * 靠的是这个文件放在哪个目录。理由全文见 `app/(app)/layout.tsx`。
 *
 * 因此这一页自己出一行品牌和一句落款，复用 `app.name` / `app.tagline` /
 * `app.footerLocal` 三条已有文案，不为登录页新造品牌文案。
 *
 * 刻意**不设** `export const metadata`：根布局的 `title` 是 `app.name`，把标签页
 * 标题换成「登录」是更差的信息；要做成"登录 · Strix 控制台"就得在根布局引入
 * `title.template`，那是为一个页面动全站元数据。
 */
export default function LoginPage() {
  return (
    <div className={styles.page}>
      <div className={styles.card}>
        <p className={styles.brand}>
          {t("app.name")}
          <em className={styles.tagline}>{t("app.tagline")}</em>
        </p>

        <div className={styles.sheet}>
          <h1 className={styles.heading}>{t("login.heading")}</h1>
          <p className={styles.intro}>{t("login.intro")}</p>
          <div className={styles.formWrap}>
            <LoginForm />
          </div>
        </div>

        {/* `login.forgot` 是**唯一的找回路径**：没有改口令的入口，也没有任何东西
            会替用户记住它（浏览器密码管理器是可选的，不能假定）。所以它不折叠、
            不排到视觉次要位置，就在表单正下方。 */}
        <p className={styles.note}>{t("login.forgot")}</p>
        <p className={styles.note}>{t("login.scope")}</p>
        <p className={styles.foot}>{t("app.footerLocal")}</p>
      </div>
    </div>
  );
}
