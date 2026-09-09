import { ErrorNotice } from "@/components/errors/ErrorNotice";

/**
 * 404。**服务端组件。**
 *
 * 复用 `errors.not_found` 的文案 —— 前端不为同一件事写第二套说法。
 *
 * **不传 `params.path`**：知道当前路径的唯一办法是读请求头，而"任何文件都不许
 * import `next/headers`"是本项目的硬规则（见 `lib/api/client.ts` 第一节）。
 * 地址就在浏览器地址栏里，为了在页面上重复它一遍而破一条安全边界不值得。
 *
 * 本轮 `/scans/new`、`/scans/[id]`、`/audit`、`/diagnostics`、`/login` 都会落到这里。
 * 那是**如实的**：这些地址现在确实不存在。所以首页那几个入口是禁用的，
 * 不是链到这里来。
 */
export default function NotFound() {
  return <ErrorNotice code="not_found" />;
}
