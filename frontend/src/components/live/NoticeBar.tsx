import { t, type MessageKey } from "@/lib/messages";
import type { LiveNotice } from "@/lib/stores/scanLive";

import styles from "./NoticeBar.module.css";

/** 只认这三个码；`stream_lagged` 由连接层变成 `reconnecting`，不会走到这里。 */
const KNOWN_CODES: ReadonlySet<string> = new Set([
  "screenshot_elided",
  "context_compacted",
  "stream_resynced",
]);

export function NoticeBar({ notices }: { readonly notices: readonly LiveNotice[] }) {
  // Set 保留插入顺序 → 每个码只显示一次，按首次出现。
  const codes = [...new Set(notices.map((n) => n.code))].filter((c) => KNOWN_CODES.has(c));
  if (codes.length === 0) return null;
  return (
    <div className={styles.bar} role="status">
      {codes.map((c) => (
        <p key={c} className={styles.notice}>
          {t(`wsNotices.${c}` as MessageKey)}
        </p>
      ))}
    </div>
  );
}
