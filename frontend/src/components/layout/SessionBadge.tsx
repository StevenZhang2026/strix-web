"use client";

import { useQuery } from "@tanstack/react-query";

import { fetchSession } from "@/lib/api/client";

/**
 * 顶栏右侧的当前用户名。**客户端叶子组件。**
 *
 * 为什么是客户端取数：服务端彻底不碰 cookie（方案 B），理由见
 * `lib/api/client.ts` 顶部第一节。这一小块就是那个决定的代价的最小形态 ——
 * 一个用户名晚半秒出现。`AppShell` 的 `.topbarRight` 有 `min-height`，
 * 所以它出现时顶栏不会变高。
 *
 * `/api/auth/me` 在后端的 `EXEMPT_PATHS` 里，**永远 200**（未登录时在响应体里说话）。
 * 所以这个组件不会触发 401 遮罩 —— 未登录时它只是什么都不显示。
 */
export function SessionBadge() {
  const { data } = useQuery({
    queryKey: ["session"],
    queryFn: ({ signal }) => fetchSession(signal),
  });

  if (data === undefined || !data.authenticated || data.username === null) {
    return null;
  }

  return <span>{data.username}</span>;
}
