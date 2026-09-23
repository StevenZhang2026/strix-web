import type { ReactNode } from "react";

import { Panel } from "@/components/ui/Panel";
import { StatusDot, type DotTone } from "@/components/ui/StatusDot";
import { t, type MessageKey } from "@/lib/messages";
import type { LiveEvent } from "@/lib/stores/scanLive";

import styles from "./EventFeed.module.css";

/** 只挂最后这么多条进 DOM。改它要同步改文案 `live.eventsTruncated`（里面写死了 500）。 */
const MAX_EVENTS = 500;

/** `data` 的叶子是 `unknown`：是普通对象才当记录读，否则 `null`。 */
export function asRecord(value: unknown): Readonly<Record<string, unknown>> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

const ROLE_LABEL: Readonly<Record<string, MessageKey>> = {
  user: "live.roleUser",
  assistant: "live.roleAssistant",
};

export const TOOL_STATUS_LABEL: Readonly<Record<string, MessageKey>> = {
  running: "live.toolRunning",
  completed: "live.toolCompleted",
  failed: "live.toolFailed",
};

const TOOL_STATUS_TONE: Readonly<Record<string, DotTone>> = {
  running: "ok",
  failed: "warn",
};

function clock(ts: string | null): string | null {
  if (ts === null) return null;
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? null : d.toLocaleTimeString("zh-CN", { hour12: false });
}

function EventRow({ event }: { readonly event: LiveEvent }) {
  const { data } = event;
  let head: ReactNode;
  let body: ReactNode = null;
  if (event.kind === "chat") {
    if (typeof data.content !== "string") return null;
    const role = typeof data.role === "string" ? ROLE_LABEL[data.role] : undefined;
    head = <span className={styles.label}>{t(role ?? "common.unknown")}</span>;
    body = <div className={styles.content}>{data.content}</div>;
  } else if (event.kind === "tool") {
    const status = typeof data.status === "string" ? data.status : "";
    head = (
      <>
        <span className={`${styles.label} mono`}>
          {typeof data.tool_name === "string" ? data.tool_name : t("common.unknown")}
        </span>
        <span className={styles.status}>
          <StatusDot tone={TOOL_STATUS_TONE[status] ?? "idle"} />
          {t(TOOL_STATUS_LABEL[status] ?? "common.unknown")}
        </span>
      </>
    );
  } else {
    return null;
  }
  const time = clock(event.ts);
  return (
    <li className={styles.row}>
      <div className={styles.head}>
        {time !== null && <span className={`${styles.time} num`}>{time}</span>}
        {head}
      </div>
      {body}
    </li>
  );
}

export function EventFeed({ events }: { readonly events: readonly LiveEvent[] }) {
  const shown = events.length > MAX_EVENTS ? events.slice(-MAX_EVENTS) : events;
  return (
    <Panel title={t("live.eventsHeading")}>
      {events.length === 0 ? (
        <p className={styles.empty}>{t("live.eventsEmpty")}</p>
      ) : (
        <>
          {shown !== events && <p className={styles.empty}>{t("live.eventsTruncated")}</p>}
          <ol className={styles.list}>
            {shown.map((e) => (
              <EventRow key={e.key} event={e} />
            ))}
          </ol>
        </>
      )}
    </Panel>
  );
}
