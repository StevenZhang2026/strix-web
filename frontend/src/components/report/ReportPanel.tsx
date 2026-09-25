"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { CredentialForm, CredentialSummary } from "@/components/credentials/CredentialForm";
import { ErrorNotice } from "@/components/errors/ErrorNotice";
import { Button } from "@/components/ui/Button";
import {
  ApiError,
  fetchReportZh,
  reportDocxPath,
  reportPrintPath,
  startReportZh,
} from "@/lib/api/client";
import { formatUsd } from "@/lib/format";
import { t } from "@/lib/messages";
import { useKeysStore } from "@/lib/stores/keys";

import styles from "./ReportPanel.module.css";

/**
 * 扫描页「中文报告」tab。报告本体用 `<iframe sandbox="">` 嵌后端打印版（零 JS、自包含），
 * 不在 React 里再画一份 —— 两份渲染必然漂移。
 *
 * 生成会真的调模型花钱：手写 `async` + `useState`（照 `ResumePanel`），不包 `useMutation`。
 */
export function ReportPanel({ scanId }: { readonly scanId: string }) {
  const queryClient = useQueryClient();
  const handle = useKeysStore((s) => s.handle);
  const forget = useKeysStore((s) => s.forget);
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<Error | null>(null);
  const query = useQuery({
    queryKey: ["reportZh", scanId],
    queryFn: ({ signal }) => fetchReportZh(scanId, signal),
    refetchInterval: (q) => (q.state.data?.status === "running" ? 2000 : false),
  });

  const data = query.data;
  const running = data?.status === "running";
  const hasAny = data !== undefined && (data.findings_zh.length > 0 || data.executive_zh !== null);
  const disabled = pending || running || handle === null;

  async function submit(force: boolean): Promise<void> {
    if (handle === null) {
      return;
    }
    setPending(true);
    setFailure(null);
    try {
      await startReportZh(scanId, { vault_handle: handle, force });
      await queryClient.invalidateQueries({ queryKey: ["reportZh", scanId] });
    } catch (cause) {
      // `key_required` = 后端内存 vault 已经空了。**不调 `dropKey`**（照 ResumePanel）。
      if (cause instanceof ApiError && cause.code === "key_required") {
        forget();
      }
      // 别人已经在跑：跟着轮询。
      if (cause instanceof ApiError && cause.code === "report_in_progress") {
        await queryClient.invalidateQueries({ queryKey: ["reportZh", scanId] });
      }
      setFailure(cause instanceof Error ? cause : new Error("report_failed"));
    } finally {
      setPending(false);
    }
  }

  function body() {
    if (query.error !== null) {
      const e = query.error;
      return e instanceof ApiError ? (
        <ErrorNotice code={e.code} traceId={e.traceId} params={e.params} />
      ) : (
        <ErrorNotice code="internal_error" />
      );
    }
    if (data === undefined) {
      return <p className={styles.hint}>{t("report.loading")}</p>;
    }
    if (running) {
      return <p className={styles.hint}>{t("report.generating")}</p>;
    }
    // `sandbox=""` 必须为空：页面里有目标站点可控内容与 LLM 输出，不许执行脚本。
    return (
      <iframe
        className={styles.frame}
        src={reportPrintPath(scanId)}
        sandbox=""
        title={t("report.frameTitle")}
      />
    );
  }

  const result = data?.status === "done" ? data.result : null;

  return (
    <div className={styles.root}>
      <div className={styles.field}>
        <div className={styles.actions}>
          <Button disabled={disabled} onClick={() => void submit(false)}>
            {running ? t("report.running") : hasAny ? t("report.fill") : t("report.generate")}
          </Button>
          {hasAny ? (
            <Button variant="ghost" disabled={disabled} onClick={() => void submit(true)}>
              {t("report.regenerate")}
            </Button>
          ) : null}
          <a
            className={styles.link}
            href={reportPrintPath(scanId)}
            target="_blank"
            rel="noopener noreferrer"
          >
            {t("report.openPrint")}
          </a>
          <a className={styles.link} href={reportDocxPath(scanId)} download>
            {t("report.downloadDocx")}
          </a>
        </div>
        {hasAny ? <p className={styles.hint}>{t("report.regenerateHint")}</p> : null}
        <p className={styles.hint}>{t("report.printHint")}</p>
      </div>

      {handle === null ? (
        <div className={styles.field}>
          <p className={styles.hint}>{t("report.credentialsNote")}</p>
          <CredentialForm />
        </div>
      ) : (
        <div className={styles.field}>
          <CredentialSummary handle={handle} />
          <p className={styles.hint}>{t("report.modelNote")}</p>
        </div>
      )}

      {failure === null ? null : (
        <div className={styles.field}>
          {failure instanceof ApiError ? (
            <ErrorNotice code={failure.code} traceId={failure.traceId} params={failure.params} />
          ) : (
            <ErrorNotice code="internal_error" />
          )}
        </div>
      )}

      {result === null ? null : (
        <div className={styles.field}>
          <p className={styles.stats}>
            <span>
              <span>{t("report.resultTranslated")}</span> <span>{result.translated_findings}</span>
            </span>
            <span>
              <span>{t("report.resultCached")}</span> <span>{result.cached_findings}</span>
            </span>
            <span>
              <span>{t("report.resultFailed")}</span> <span>{result.failed_findings}</span>
            </span>
            <span>
              <span>{t("report.resultCost")}</span>{" "}
              <span>
                {result.cost_usd === null ? t("report.costUnknown") : formatUsd(result.cost_usd)}
              </span>
            </span>
          </p>
          {result.failed_findings > 0 || result.executive === "failed" ? (
            <p className={styles.warn}>{t("report.partialFailed")}</p>
          ) : null}
        </div>
      )}
      {data?.status === "failed" ? <p className={styles.warn}>{t("report.jobFailed")}</p> : null}

      {body()}
    </div>
  );
}
