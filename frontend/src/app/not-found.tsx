import { ErrorNotice } from "@/components/errors/ErrorNotice";
import { AppShell } from "@/components/layout/AppShell";

/**
 * 404。**服务端组件。**
 *
 * 复用 `errors.not_found` 的文案 —— 前端不为同一件事写第二套说法。
 *
 * =============================================================================
 * 为什么这个文件**留在 `app/` 根上**，而不是挪进路由组 `(app)`（T5b 实测结论）
 *
 * 挪进去它就**不再兜住全站未匹配 URL**了。这是实测的，不是推断的：
 *
 *   1. `mv not-found.tsx '(app)/not-found.tsx'` 之后 `npm run build`
 *      → `.next/app-path-routes-manifest.json` 是
 *        `{"/icon.svg/route":…, "/_not-found/page":"/_not-found", "/(app)/page":"/"}`
 *        注意 `_not-found` 那条**没有** `(app)` 前缀 —— 它是 Next 在 app 根上
 *        **自己合成的默认 404**，我们那个文件根本没被挂上去。
 *   2. `next start` 之后 `curl -s http://127.0.0.1:3111/nope`
 *      → HTTP 404，但正文里**没有**「找不到这个地址」；正文是 Next 自带的
 *        "This page could not be found"（内联 `<style>body{color:#000;background:#fff}`），
 *        套在根布局里。
 *
 * 所以它必须留在根上。代价是它拿不到 `(app)/layout.tsx` 里的 `AppShell`，
 * 于是自己包一层 —— `AppShell` 因此有 3 个调用点，如实记账。
 * 包它的理由是保持 T5 已批准的长相（品牌栏 + 页脚）不变；不包就是一个光秃秃的页面。
 *
 * 注意它**不在** `(app)` 里，所以 404 页上没有 `RequireSession`：一个走错地址的人
 * 会看到"找不到这个地址"，而不是被静默送去登录页。那是更诚实的答复。
 *
 * **不传 `params.path`**：知道当前路径的唯一办法是读请求头，而"任何文件都不许
 * import `next/headers`"是本项目的硬规则（见 `lib/api/client.ts` 第一节）。
 * 地址就在浏览器地址栏里，为了在页面上重复它一遍而破一条安全边界不值得。
 *
 * 现在 `/scans/new`、`/scans/[id]`、`/audit`、`/diagnostics` 都会落到这里
 * （`/login` 从 T5b 起是真页面了）。那是**如实的**：这些地址确实还不存在。
 * 所以首页那几个入口是禁用的，不是链到这里来。
 */
export default function NotFound() {
  return (
    <AppShell>
      <ErrorNotice code="not_found" />
    </AppShell>
  );
}
