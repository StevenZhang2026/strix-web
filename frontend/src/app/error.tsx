"use client";

import { ApiError } from "@/lib/api/client";
import { ErrorNotice } from "@/components/errors/ErrorNotice";
import { Button } from "@/components/ui/Button";
import { t } from "@/lib/messages";

import styles from "./error.module.css";

/**
 * 路由级错误边界。**必须是客户端组件**（Next 的要求：它要接 `reset` 回调）。
 *
 * 三种来源，映射到三个机器码：
 *   · `ApiError`     —— 后端明确回了 `{code, trace_id, params}`，直接用它的码。
 *   · `NetworkError` —— 网络层就没通。归到 `docker_unavailable`？**不**：那是
 *     "Docker 连不上"，不是"后端连不上"。用 `internal_error`，它的文案说的正是
 *     "后端出错了 / 重试一次通常就好"，与实际情形一致。
 *   · 其它           —— 渲染期的真 bug。也归 `internal_error`。
 *
 * **不显示 `error.message`**。它可能带着栈、路径、甚至请求头里的东西 ——
 * 而这个项目的口径是脱敏之后才落日志，页面上一律只出机器码 + 问题编号
 * （CLAUDE.md §错误与文案）。
 */
export default function RouteError({
  error,
  reset,
}: {
  readonly error: Error & { readonly digest?: string };
  readonly reset: () => void;
}) {
  // `NetworkError` 与"其它 Error"目前归同一个码，所以这里没有第三个分支 ——
  // 刻意不写 `if (error instanceof NetworkError) code = "internal_error"`，
  // 那是一行什么都不做的代码。判据的区别记在上面的 docstring 里。
  const isApi = error instanceof ApiError;
  const code = isApi ? error.code : "internal_error";

  return (
    <div className={styles.wrap}>
      <ErrorNotice
        code={code}
        {...(isApi ? { traceId: error.traceId, params: error.params } : {})}
      />
      <Button variant="ghost" onClick={reset}>
        {t("common.retry")}
      </Button>
    </div>
  );
}
