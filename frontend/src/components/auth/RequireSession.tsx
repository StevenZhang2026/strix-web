"use client";

import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { fetchSession } from "@/lib/api/client";
import { useSessionStore } from "@/lib/stores/session";

/**
 * 未登录就跳 `/login`。**客户端叶子，永远渲染 `null`。**
 *
 * 由 `app/(app)/layout.tsx` 渲染，所以它覆盖 `(app)` 组里的每一个页面 ——
 * "谁受这道门管"是一个目录事实，不是一张要维护的路径清单。
 *
 * =============================================================================
 * 为什么不是 middleware（T5b 拍板，理由全文在 `lib/api/client.ts` 第二节）
 *
 * 一句话：middleware 只能判断 cookie **在不在**，而这里最常见的失效原因是
 * "api 重启了" —— 那时 cookie 一直都在。它会花掉"前端一行都不许碰会话 cookie"
 * 这条硬不变式，换来的却接近于零。
 *
 * `/api/auth/me` **永远 200**（它在后端的 `EXEMPT_PATHS` 里），未登录时在响应体里
 * 说话。所以这个组件既不会触发 401 遮罩，也能区分"我没登录"和"后端连不上"。
 *
 * =============================================================================
 * 三条判据，每一条都是刻意的
 *
 * 1. `data === undefined` → **什么都不做**。这包含"第一次还在飞"和"后端连不上"
 *    两种。后者尤其不该跳转：`/login` 同样需要后端，把人送到一个打不开的登录页
 *    只是把"连不上"伪装成"你没登录"。
 * 2. `expired === true` → **让位给遮罩**。那时 `SessionExpiredMask` 已经在解释
 *    "会话为什么失效了"，一次静默跳转会把这个解释顶掉。分工：遮罩管"刚刚 401 了"
 *    （反应式，不丢屏幕上的东西），本组件管"进来时压根没登录"（前置式）。
 * 3. `router.replace` 而不是 `push`：`/` 不该留在返回键的路上，否则用户按返回就
 *    又弹回来一次跳转。
 *
 * **刻意不做的事**：不把当前路径塞进 `?next=`。那是一个开放重定向面，也违反
 * "凭据与意图不放 URL"的口径；而现在 `(app)` 里只有 `/` 一个页面，无处可回。
 * T18/T25 建起深链之后真需要它，正确做法是放进内存 store，不是 URL。
 *
 * **未登录时会先闪一下首页骨架**，这是不加 middleware 的代价，如实记账。它不泄漏
 * 任何东西：`(app)/page.tsx` 是纯静态骨架 —— 五个字段全是虚线空白态、就绪四行全是
 * 「尚未检测」、三个入口全是 disabled，**零字节用户数据**。真有数据的接口
 * （`/api/scans`、`/api/keys`、`/ws`）由后端全局依赖挡住（验收 27）。
 * 刻意**不**为它加一个"会话未知时渲染 null"的门 —— 那会让已登录用户每次进站都先看
 * 一次白屏，用所有人都付的代价换一个只有未登录者能看见的观感。
 */
export function RequireSession() {
  const router = useRouter();
  const expired = useSessionStore((state) => state.expired);
  const { data } = useQuery({
    // 与 `layout/SessionBadge.tsx` **共用同一个 key**：react-query 按 key 去重，
    // 两个组件一起挂载只发一个请求（`staleTime: 30_000`）。
    queryKey: ["session"],
    queryFn: ({ signal }) => fetchSession(signal),
  });

  const authenticated = data?.authenticated;

  useEffect(() => {
    // 跳转是副作用，必须在 effect 里做：渲染期调 `router.replace` 会在 React 的
    // 严格模式下跑两次，也会让这个组件的渲染依赖路由的副作用。
    if (expired || authenticated !== false) {
      return;
    }
    router.replace("/login");
  }, [authenticated, expired, router]);

  return null;
}
