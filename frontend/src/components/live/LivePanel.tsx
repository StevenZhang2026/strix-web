"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ErrorNotice } from "@/components/errors/ErrorNotice";
import { StatusDot, type DotTone } from "@/components/ui/StatusDot";
import { ApiError, fetchScan, stopScan } from "@/lib/api/client";
import { t, type MessageKey } from "@/lib/messages";
import { initialLiveState, useScanLiveStore } from "@/lib/stores/scanLive";
import { useScanStream } from "@/lib/ws/scanStream";

import { AgentTree } from "./AgentTree";
import { CostMeter } from "./CostMeter";
import { EventFeed } from "./EventFeed";
import styles from "./LivePanel.module.css";
import { NoticeBar } from "./NoticeBar";
import { ResumePanel } from "./ResumePanel";
import { ScanHeader, type StopState } from "./ScanHeader";
import { ScreenshotGallery } from "./ScreenshotGallery";
import { Terminal } from "./Terminal";

// 只决定按钮显不显示，真判定在后端 `services/scan_resume.py`，两边取值必须一致。
const RESUMABLE_STATUSES = new Set(["stopped", "interrupted"]);
const RESUMABLE_ERROR_CODES = new Set([
  "scan_incomplete",
  "stopped_by_operator",
  "interrupted_by_restart",
]);

type Connection = "connecting" | "live" | "reconnecting" | "ended" | "not_found";

const CONNECTION_LABEL: Record<Exclude<Connection, "not_found">, MessageKey> = {
  connecting: "live.streamConnecting",
  live: "live.streamLive",
  reconnecting: "live.streamReconnecting",
  ended: "live.streamEnded",
};

const CONNECTION_TONE: Record<Exclude<Connection, "not_found">, DotTone> = {
  connecting: "idle",
  live: "ok",
  reconnecting: "warn",
  ended: "idle",
};

/**
 * `/scans/[id]` 的唯一客户端根。
 *
 * 数据 = REST 快照（`["scan", scanId]`，数据层收到 `done` 时 invalidate 这个 key）
 * + WS 增量（agents、cost）。结论／状态／计数**只从 REST 快照取**：`done` 帧不带结论。
 *
 * 停止状态提升到这里：头部与费用尺共用同一个停止动作。手写 `async` + `useState`
 * （照 `SubmitPanel` 的 `createScan`），不包 `useMutation`、不重试。
 */
export function LivePanel({ scanId }: { readonly scanId: string }) {
  const [generation, setGeneration] = useState(0);
  useScanStream(scanId, generation);
  const queryClient = useQueryClient();
  // store 在 `useScanStream` 的 effect 里才 `reset(scanId)`，首帧渲染时装的可能还是上一个
  // 扫描的状态（客户端导航在两个扫描之间切换）→ 不是这个 scanId 的一律当初始态。
  const stored = useScanLiveStore((s) => s.live);
  const live = stored.scanId === scanId ? stored : initialLiveState(scanId);
  const query = useQuery({
    queryKey: ["scan", scanId],
    queryFn: ({ signal }) => fetchScan(scanId, signal),
  });
  const [stop, setStop] = useState<StopState>({ kind: "idle" });

  if (live.connection === "not_found") {
    return <ErrorNotice code="not_found" />;
  }
  // 已有快照时后台重取失败不顶掉整页：react-query 会保留上一份 data。
  if (query.data === undefined && query.error !== null) {
    const e = query.error;
    return e instanceof ApiError ? (
      <ErrorNotice code={e.code} traceId={e.traceId} params={e.params} />
    ) : (
      <ErrorNotice code="internal_error" />
    );
  }

  const data = query.data;
  const scan = data?.scan;
  const agents = live.agents ?? data?.agents ?? null;
  const cost = live.summary?.cost_usd ?? scan?.cost_usd ?? null;
  const canStop =
    scan !== undefined &&
    (scan.status === "starting" || scan.status === "running") &&
    !live.done &&
    stop.kind !== "requested";
  const canResume =
    scan !== undefined &&
    RESUMABLE_STATUSES.has(scan.status) &&
    scan.error_code !== null &&
    RESUMABLE_ERROR_CODES.has(scan.error_code) &&
    !canStop;

  async function onStop() {
    setStop({ kind: "pending" });
    try {
      await stopScan(scanId);
      setStop({ kind: "requested" });
    } catch (error: unknown) {
      setStop({ kind: "failed", error });
    }
  }

  return (
    <div className={styles.root}>
      <div className={styles.tabs} role="tablist">
        <span className={styles.tabActive} role="tab" aria-selected="true">
          {t("scan.tabLive")}
        </span>
      </div>

      <p className={styles.connection}>
        <StatusDot tone={CONNECTION_TONE[live.connection]} />
        {t(CONNECTION_LABEL[live.connection])}
      </p>

      <NoticeBar notices={live.notices} />

      <ScanHeader
        scanId={scanId}
        scan={scan}
        stop={stop}
        canStop={canStop}
        onStop={() => void onStop()}
      />

      {!canResume ? null : (
        // `key`：续跑后换代，本地 state（展开、总额、失败）一起重置。
        <ResumePanel
          key={generation}
          scan={scan}
          onResumed={() => {
            setStop({ kind: "idle" });
            setGeneration((g) => g + 1);
            void queryClient.invalidateQueries({ queryKey: ["scan", scanId] });
          }}
        />
      )}

      <div className={styles.grid}>
        <AgentTree agents={agents} />
        <CostMeter
          cost={cost}
          maxBudgetUsd={scan?.max_budget_usd ?? null}
          canStop={canStop}
          stopPending={stop.kind === "pending"}
          onStop={() => void onStop()}
        />
      </div>

      <div className={styles.grid}>
        <EventFeed events={live.events} />
        <div className={styles.column}>
          <Terminal events={live.events} />
          <ScreenshotGallery events={live.events} scanId={scanId} />
        </div>
      </div>
    </div>
  );
}
