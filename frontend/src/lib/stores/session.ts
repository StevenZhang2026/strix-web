"use client";

/**
 * "会话是否已失效"这一个布尔量。
 *
 * 为什么需要一个 store 而不是就地 `useState`：置这个标志的人是
 * `lib/api/client.ts` 里的模块级回调（它不在任何 React 组件里），读它的是全屏遮罩。
 * 两者之间没有父子关系，只能经一个进程级的 store。
 *
 * **刻意不存任何会话信息** —— 不存会话 id（那在 `HttpOnly` cookie 里，前端碰不到
 * 也不许碰）、不存过期时间（后端刻意不返回它：真正的失效原因大多是"api 重启了"，
 * 本地倒计时对此一无所知，会是一个总在说谎的 UI，见 `routes/auth.py`
 * 的 `SessionStateResponse`）。
 *
 * 本文件**不碰 sessionStorage** —— 那是 `stores/keys.ts` 的专属特权。
 * 会话失效这件事重启页面就该重新判定，持久化它只会造成"明明能用却显示已失效"。
 */

import { create } from "zustand";

interface SessionState {
  readonly expired: boolean;
  /** 由 `providers.tsx` 注册给 `lib/api/client.ts` 的那个回调调用。 */
  readonly markExpired: () => void;
  /** 重新登录成功后清掉（T5b 用）。 */
  readonly clearExpired: () => void;
}

export const useSessionStore = create<SessionState>((set) => ({
  expired: false,
  markExpired: () => {
    set({ expired: true });
  },
  clearExpired: () => {
    set({ expired: false });
  },
}));
