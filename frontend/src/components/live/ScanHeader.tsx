import { ErrorNotice } from "@/components/errors/ErrorNotice";
import { Button } from "@/components/ui/Button";
import { Panel } from "@/components/ui/Panel";
import { ApiError, type ScanDetail } from "@/lib/api/client";
import { t, type MessageKey } from "@/lib/messages";

import styles from "./ScanHeader.module.css";

export type StopState =
  | { readonly kind: "idle" }
  | { readonly kind: "pending" }
  | { readonly kind: "requested" }
  | { readonly kind: "failed"; readonly error: unknown };

const STATUS_LABEL: Readonly<Record<string, MessageKey>> = {
  starting: "status.starting",
  running: "status.running",
  completed: "status.completed",
  stopped: "status.stopped",
  failed: "status.failed",
  interrupted: "status.interrupted",
  orphaned_running: "status.orphanedRunning",
};

export function statusLabel(status: string): string {
  const key = STATUS_LABEL[status];
  return key === undefined ? t("common.unknown") : t(key);
}

type Headline = "live.conclusionPending" | "live.conclusionFound" | "live.conclusionNoneFound";

export interface Conclusion {
  readonly headline: Headline | null;
  /** 只在 `conclusionFound` 时非空；为 0 的级别也在里面。 */
  readonly counts: readonly { readonly label: MessageKey; readonly value: number }[] | null;
  readonly errorCode: string | null;
}

// 「未发现漏洞」必须同时要求 status === "completed"：预算耗尽等情况下扫描被停止、
// 退出码仍是 0，`exit_meaning` 照样是 no_vulnerabilities_found —— 那时说"没发现"
// 是把"没跑完"说成"没问题"（发布阻断项）。别把这里简化成只看 exit_meaning。
export function conclusionOf(scan: ScanDetail): Conclusion {
  if (scan.status === "starting" || scan.status === "running") {
    return { headline: "live.conclusionPending", counts: null, errorCode: null };
  }
  if (scan.exit_meaning === "vulnerabilities_found") {
    return {
      headline: "live.conclusionFound",
      counts: [
        { label: "severity.critical", value: scan.count_critical },
        { label: "severity.high", value: scan.count_high },
        { label: "severity.medium", value: scan.count_medium },
        { label: "severity.low", value: scan.count_low },
      ],
      errorCode: scan.error_code,
    };
  }
  if (scan.status === "completed" && scan.exit_meaning === "no_vulnerabilities_found") {
    return { headline: "live.conclusionNoneFound", counts: null, errorCode: scan.error_code };
  }
  return { headline: null, counts: null, errorCode: scan.error_code };
}

interface ScanHeaderProps {
  readonly scanId: string;
  /** `undefined` = REST 快照还在加载。 */
  readonly scan: ScanDetail | undefined;
  readonly stop: StopState;
  readonly canStop: boolean;
  readonly onStop: () => void;
}

const STOP_HINT_ID = "scan-stop-hint";

export function ScanHeader({ scanId, scan, stop, canStop, onStop }: ScanHeaderProps) {
  const loading = t("common.loading");
  return (
    <div className={styles.header}>
      <dl className={styles.pairs}>
        <dt className={styles.key}>{t("live.scanIdLabel")}</dt>
        <dd className={`${styles.value} mono`}>{scanId}</dd>
        <dt className={styles.key}>{t("live.statusLabel")}</dt>
        <dd className={styles.value}>{scan === undefined ? loading : statusLabel(scan.status)}</dd>
        <dt className={styles.key}>{t("scan.stampTarget")}</dt>
        <dd className={`${styles.value} mono`}>
          {scan === undefined ? loading : scan.targets.join(", ")}
        </dd>
      </dl>

      {canStop ? (
        <div className={styles.stop}>
          <Button
            variant="stop"
            disabled={stop.kind === "pending"}
            onClick={onStop}
            ariaDescribedBy={STOP_HINT_ID}
          >
            {t("scan.stop")}
          </Button>
          <span id={STOP_HINT_ID} className={styles.hint}>
            {t("scan.stopHint")}
          </span>
        </div>
      ) : null}
      {stop.kind === "requested" ? <p className={styles.hint}>{t("live.stopRequested")}</p> : null}
      {stop.kind === "failed" ? <StopError error={stop.error} /> : null}

      <Panel title={t("live.conclusionHeading")}>
        {scan === undefined ? loading : <ConclusionView conclusion={conclusionOf(scan)} />}
      </Panel>
    </div>
  );
}

function StopError({ error }: { readonly error: unknown }) {
  return error instanceof ApiError ? (
    <ErrorNotice code={error.code} traceId={error.traceId} params={error.params} />
  ) : (
    <ErrorNotice code="internal_error" />
  );
}

function ConclusionView({ conclusion }: { readonly conclusion: Conclusion }) {
  return (
    <div className={styles.conclusion}>
      {conclusion.headline === null ? null : (
        <p className={styles.headline}>{t(conclusion.headline)}</p>
      )}
      {conclusion.counts === null ? null : (
        <dl className={styles.counts}>
          {conclusion.counts.map((c) => (
            <div key={c.label} className={styles.count}>
              <dt className={styles.key}>{t(c.label)}</dt>
              <dd className={`${styles.countValue} num`}>{c.value}</dd>
            </div>
          ))}
        </dl>
      )}
      {conclusion.errorCode === null ? null : (
        <ErrorNotice code={conclusion.errorCode} tree="scan" />
      )}
    </div>
  );
}
