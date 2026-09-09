"use client";

import { QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";

import { setUnauthenticatedHandler } from "@/lib/api/client";
import { makeQueryClient } from "@/lib/api/query";
import { SessionExpiredMask } from "@/components/errors/SessionExpiredMask";
import { useKeysStore } from "@/lib/stores/keys";
import { useSessionStore } from "@/lib/stores/session";

/**
 * 整棵树唯一的客户端根。`app/layout.tsx` 用它包住 `{children}`。
 *
 * 三件事，就这三件：
 *   1. 提供 react-query 的 QueryClient；
 *   2. 把"会话失效"的回调注册给 `lib/api/client.ts`（**全站唯一注册点**）；
 *   3. 从 sessionStorage 恢复 `vault_handle`（必须在挂载后做，见 stores/keys.ts）。
 *
 * 它是 `app/` 目录下**唯一**允许带 `"use client"` 的文件（eslint 里有对应的例外）。
 * 其余页面一律是服务端组件，需要交互就下沉到 `components/` 的叶子。
 */
export function Providers({ children }: { readonly children: ReactNode }) {
  // `useState` 的初始化函数，**不是**模块级单例。模块级单例在 dev 的 HMR 下会跨热更新
  // 存活、在多标签下共享同一份缓存 —— 而缓存里装的是"当前登录用户能看到的数据"。
  const [queryClient] = useState(() => makeQueryClient());

  const markExpired = useSessionStore((state) => state.markExpired);
  const hydrateFromSession = useKeysStore((state) => state.hydrateFromSession);

  useEffect(() => {
    setUnauthenticatedHandler(markExpired);
    return () => {
      // 卸载时摘掉。留着一个指向已卸载树的回调，在 dev 的 HMR 下会累积成
      // "同一次 401 触发好几个旧回调"。
      setUnauthenticatedHandler(null);
    };
  }, [markExpired]);

  useEffect(() => {
    hydrateFromSession();
  }, [hydrateFromSession]);

  return (
    <QueryClientProvider client={queryClient}>
      {children}
      <SessionExpiredMask />
    </QueryClientProvider>
  );
}
