/**
 * QueryClient 工厂 + 全局错误策略。
 *
 * **工厂函数，不是模块级单例。** `providers.tsx` 用 `useState(() => makeQueryClient())`
 * 每个 React 树各建一个。模块级单例在 dev 的 HMR 下会跨热更新存活、在多标签下也会
 * 共享同一份缓存 —— 而缓存里装的是"当前登录用户能看到的数据"。
 */

import { QueryClient } from "@tanstack/react-query";

import { ApiError } from "./client";

/**
 * 401 **一律不重试**。
 *
 * 不是性能考虑：会话失效时每次失败都会调一次 `notifyUnauthenticated()`，
 * 重试三次就等于让全屏遮罩闪三次。而重试对一个"内存里的会话已经没了"的后端
 * 也不可能成功 —— 它需要人重新登录，不需要我们再问两遍。
 *
 * 同理 `NetworkError` 是**允许**重试的（后端可能正在重启，几百毫秒后就好了），
 * 所以这里只挑 401 出来，其余走默认次数。
 */
function shouldRetry(failureCount: number, error: Error): boolean {
  if (error instanceof ApiError && (error.status === 401 || error.code === "unauthenticated")) {
    return false;
  }
  // 4xx 里除了 401 也基本没有重试的意义：请求内容不合法、目标不在白名单，
  // 重发一次结果一样。只有 5xx 与网络层值得重试。
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
    return false;
  }
  return failureCount < 2;
}

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: shouldRetry,
        // 本机单用户工具，数据变化由 WebSocket 推（T13/T17），不靠轮询。
        // 关掉窗口聚焦重取：它会在"切回浏览器"这种与数据无关的时刻打一串请求。
        refetchOnWindowFocus: false,
        staleTime: 30_000,
      },
      mutations: {
        // 变更一律不重试。这个项目里的变更包括"发起一次真实攻击"和"停止一次扫描"，
        // 自动重发是不可接受的 —— 宁可让人自己再点一次。
        retry: false,
      },
    },
  });
}
