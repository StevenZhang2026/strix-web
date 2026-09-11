"use client";

import { QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";

import { setUnauthenticatedHandler } from "@/lib/api/client";
import { makeQueryClient } from "@/lib/api/query";
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
 * 它是 `app/` 目录下**唯一**允许带 `"use client"` 的文件（`error.tsx` 除外，那是
 * Next 的硬要求）。其余页面一律是服务端组件，需要交互就下沉到 `components/` 的叶子。
 *
 * T5b 把 `<SessionExpiredMask />` 从这里**移到了** `app/(app)/layout.tsx`。
 * 原来的理由写的是"遮罩要能压住顶栏和页脚，所以得在 AppShell 外面" —— 那句话作为
 * 结果对、作为**理由**不准确：遮罩靠 `position: fixed; inset: 0` 覆盖整个视口，
 * 与它在 DOM 里的位置无关。挪走之后覆盖范围一点没变，但多了一条结构性保证：
 * 遮罩不可能出现在 `/login` 上（一个盖住登录表单的"请重新登录"是自相矛盾的）。
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

  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}
