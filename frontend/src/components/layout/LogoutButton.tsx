"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { fetchSession, logout } from "@/lib/api/client";
import { t } from "@/lib/messages";

import styles from "./AppShell.module.css";

/**
 * 顶栏里的「退出登录」。**客户端叶子**，未登录时渲染 `null`。
 *
 * 与 `SessionBadge` **共用** `["session"]` 这个 query key，所以两个组件一起挂载
 * 只发一个请求（react-query 按 key 去重）。
 *
 * =============================================================================
 * 为什么不用 `ui/Button`
 *
 * `Button` 是 `--fs-3`(16px) / 600 / `padding: 10px var(--s5)`，放进 `--fs-2`(13px)
 * 的顶栏会让顶栏高度差不多翻倍；而它的基态是填色的 `--gate`，而 `--gate` 的语义是
 * "真的会发生事情的动作"（发起扫描、越过一道授权边界），登出不属于那一类 ——
 * 串用会稀释那个颜色的分量。
 * 也**不给 `Button` 加一个 `.small` 变体**：那是为一个用例改一个全站组件。
 * 所以这里是一个文字型控件，长相与页面上的链接同构（`globals.css` 的 `a`）。
 *
 * =============================================================================
 * 失败时**不清缓存、不跳转**
 *
 * cookie 只能由后端响应的 `Set-Cookie` 删除。后端没通就本地清掉再宣布"你已登出"，
 * 是一句谎话 —— 服务端会话还活着。所以只在成功之后才 `clear()` + 跳转，
 * 失败就原地说一句 `session.logoutFailed` 并让按钮恢复可点。
 * 顺序纪律与 `stores/keys.ts` 同源：**先请后端清，再清本地**。
 *
 * 后端 `logout` 是幂等的、永远 200（在 `EXEMPT_PATHS` 里），所以这条失败分支只在
 * 网络真的断了时才走到。
 */
export function LogoutButton() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [pending, setPending] = useState(false);
  const [failed, setFailed] = useState(false);
  const { data } = useQuery({
    queryKey: ["session"],
    queryFn: ({ signal }) => fetchSession(signal),
  });

  async function handleClick(): Promise<void> {
    if (pending) {
      return;
    }
    setPending(true);
    setFailed(false);
    try {
      await logout();
      // `clear()` 而不是 `invalidateQueries()`：后者会**保留旧数据**并在后台重取，
      // 于是"已登出"的界面上仍然渲染着上一次会话的内容，直到重取失败。
      // 登出这一侧比登录那一侧更要紧 —— 缓存里可能有扫描记录、发现详情、审计行。
      queryClient.clear();
      router.replace("/login");
    } catch {
      // 不记任何东西：这条路径上没有秘密，但也没有任何值得记的信息 ——
      // "后端连不上"这件事在别处已经会露出来（页脚的版本号也会消失）。
      setFailed(true);
    } finally {
      setPending(false);
    }
  }

  if (data === undefined || !data.authenticated) {
    return null;
  }

  return (
    <>
      <button
        type="button"
        className={styles.logout}
        onClick={() => {
          void handleClick();
        }}
        disabled={pending}
      >
        {t("nav.logout")}
      </button>
      {failed ? (
        <span className={styles.logoutFailed} role="status">
          {t("session.logoutFailed")}
        </span>
      ) : null}
    </>
  );
}
