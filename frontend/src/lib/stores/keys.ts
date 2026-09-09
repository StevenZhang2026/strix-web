"use client";

/**
 * `vault_handle` 的存放处。**全项目唯一允许碰 `sessionStorage` 的文件。**
 *
 * =============================================================================
 * 为什么是 sessionStorage，而 cookie 恰恰相反
 *
 * `vault_handle` 是一个**不透明 handle**，不是凭据本身 —— 凭据在后端进程内存里的
 * KeyVault 里，`api` 一重启就没了（CLAUDE.md §安全不变式）。所以：
 *
 *   · `vault_handle` → sessionStorage：关掉标签页就失效，正好匹配"这一次操作"的
 *     生命周期。**不用 localStorage**（跨会话存活）、**不进 cookie**（会被自动带到
 *     每一个请求上，包括不需要它的）、**不进 URL**（会进日志、会进历史记录）。
 *   · 会话 cookie → **前端一行都不许碰**。它是 `HttpOnly` 的，浏览器自己带，
 *     我们连名字都不需要知道。不读、不写、不存 sessionStorage、不放 URL。
 *
 * 两者刻意相反：一个是"我们要主动出示的票据"，一个是"浏览器代管的身份"。
 *
 * =============================================================================
 * 为什么初值不在模块加载时读 sessionStorage
 *
 * 服务端渲染时没有 `sessionStorage`，客户端首次渲染时有。若在 store 初值里读它，
 * 服务端渲染出的 HTML 与客户端首次渲染的结果会不一致 —— 一次 hydration 不匹配，
 * React 会整棵子树重渲染，控制台报错。所以初值恒为 `null`，由
 * `providers.tsx` 在 `useEffect` 里调一次 `hydrateFromSession()`。
 */

import { create } from "zustand";

// 带前缀是为了在开发者工具里一眼看出这是谁的。刻意**不叫** `token` / `key` /
// `secret` —— 那些名字会让人以为里面装着凭据，而它只是一个 handle。
const STORAGE_KEY = "strix.vaultHandle";

interface KeysState {
  /** `null` = 后端内存里没有我们的凭据，任何要花钱的操作都得先去拿一次。 */
  readonly handle: string | null;
  /** 从 sessionStorage 恢复。只该由 `providers.tsx` 在挂载后调用一次。 */
  readonly hydrateFromSession: () => void;
  readonly setHandle: (handle: string) => void;
  /** 忘记 handle。**不负责调用后端的 `DELETE /api/keys/{h}`** —— 那是 T7 的事，
      而且顺序必须是"先请后端清，再清本地"，否则失败时本地已经没有 handle 可重试了。 */
  readonly forget: () => void;
}

function readStored(): string | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    return window.sessionStorage.getItem(STORAGE_KEY);
  } catch {
    // Safari 的隐私模式下 sessionStorage 会抛。那时"没有 handle"是正确的降级 ——
    // 用户会被要求重新提供一次凭据，而不是看到一个白屏。
    return null;
  }
}

function writeStored(handle: string | null): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    if (handle === null) {
      window.sessionStorage.removeItem(STORAGE_KEY);
    } else {
      window.sessionStorage.setItem(STORAGE_KEY, handle);
    }
  } catch {
    // 写不进去不是致命的：store 里还有值，这一次操作照样能走完，
    // 只是刷新页面之后要重新提供凭据。刻意不向用户报错。
  }
}

export const useKeysStore = create<KeysState>((set) => ({
  handle: null,
  hydrateFromSession: () => {
    set({ handle: readStored() });
  },
  setHandle: (handle: string) => {
    writeStored(handle);
    set({ handle });
  },
  forget: () => {
    writeStored(null);
    set({ handle: null });
  },
}));
