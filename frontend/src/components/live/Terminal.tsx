import { Panel } from "@/components/ui/Panel";
import { t } from "@/lib/messages";
import type { LiveEvent } from "@/lib/stores/scanLive";

import { asRecord, TOOL_STATUS_LABEL } from "./EventFeed";
import styles from "./Terminal.module.css";

interface Command {
  readonly key: string;
  readonly cmd: string;
  readonly status: string;
  readonly output: string | null;
}

/** 上游 `exec_command` 的参数名是 `cmd`（shell_tool.py:106），不是 `command`。 */
function toCommand(event: LiveEvent): Command | null {
  const { data } = event;
  if (event.kind !== "tool" || data.tool_name !== "exec_command") return null;
  const cmd = asRecord(data.args)?.cmd;
  if (typeof cmd !== "string") return null;
  return {
    key: event.key,
    cmd,
    status: typeof data.status === "string" ? data.status : "",
    output: typeof data.result === "string" ? data.result : null,
  };
}

export function Terminal({ events }: { readonly events: readonly LiveEvent[] }) {
  const commands = events.map(toCommand).filter((c): c is Command => c !== null);
  return (
    <Panel title={t("live.terminalHeading")}>
      {commands.length === 0 ? (
        <p className={styles.empty}>{t("live.terminalEmpty")}</p>
      ) : (
        <div className={styles.term}>
          {commands.map((c) => (
            <div key={c.key} className={styles.entry}>
              <div className={c.status === "failed" ? styles.cmdFailed : styles.cmd}>$ {c.cmd}</div>
              {c.output !== null ? (
                <pre className={styles.output}>{c.output}</pre>
              ) : (
                <div className={styles.status}>{t(TOOL_STATUS_LABEL[c.status] ?? "common.unknown")}</div>
              )}
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}
