"use client";

import { useQuery } from "@tanstack/react-query";

import { fetchHealth } from "@/lib/api/client";
import { t } from "@/lib/messages";

/** 版本号还没取到时的占位。破折号不是"未知"，它是"还没读到"。 */
const PENDING = "—";

/**
 * 页脚的两个版本号。**客户端叶子组件。**
 *
 * `strix_version` 是从后端 `GET /api/health` 来的 —— 它读的是容器里
 * `strix-agent` 的实际版本，而不是我们写死的字符串。这一点是刻意的：
 * CLAUDE.md 要求 `strix-agent==1.5.3` 精确 pin，页脚显示实测值才能在
 * 有人放宽 pin 的时候被一眼看见。
 */
export function VersionFooter() {
  const { data } = useQuery({
    queryKey: ["health"],
    queryFn: ({ signal }) => fetchHealth(signal),
  });

  return (
    <>
      <span>
        {t("app.footerConsoleVersion")} {data === undefined ? PENDING : data.app_version}
      </span>
      <span>
        {t("app.footerStrixVersion")} {data === undefined ? PENDING : data.strix_version}
      </span>
    </>
  );
}
