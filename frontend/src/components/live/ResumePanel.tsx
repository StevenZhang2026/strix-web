"use client";

import { useQuery } from "@tanstack/react-query";
import { useId, useState } from "react";

import { CredentialForm, CredentialSummary } from "@/components/credentials/CredentialForm";
import { ErrorNotice } from "@/components/errors/ErrorNotice";
import { Button } from "@/components/ui/Button";
import { ApiError, fetchKeyState, resumeScan, type ScanDetail } from "@/lib/api/client";
import { formatUsd } from "@/lib/format";
import { t, type MessageKey } from "@/lib/messages";
import { useKeysStore } from "@/lib/stores/keys";
import { SUGGESTED_MIN_BUDGET_USD } from "@/lib/stores/wizard";

import styles from "./ResumePanel.module.css";

/**
 * 「提高上限并继续」：页内展开区，不是弹窗。
 *
 * 提交手写 `async` + `useState`（照 `SubmitPanel`）：`resumeScan` 会真的起进程花钱，
 * 不包 `useMutation`、不重试。三元组比对只为提前讲清楚，真判定在后端（不一致 → `key_required`）。
 */
export function ResumePanel({
  scan,
  onResumed,
}: {
  readonly scan: ScanDetail;
  readonly onResumed: () => void;
}) {
  const uid = useId();
  const handle = useKeysStore((s) => s.handle);
  const forget = useKeysStore((s) => s.forget);
  const [open, setOpen] = useState(false);
  const [total, setTotal] = useState(String(scan.max_budget_usd * 2));
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<Error | null>(null);

  // 与 `CredentialSummary` 同一个 key：共享缓存，不多发请求。
  const keyState = useQuery({
    queryKey: ["keyState", handle],
    queryFn: ({ signal }) => fetchKeyState(handle ?? "", signal),
    enabled: open && handle !== null,
  });

  const totalNumber = Number(total);
  const totalOk = Number.isFinite(totalNumber) && totalNumber > scan.cost_usd;
  const state = keyState.data;
  const matches =
    state !== undefined &&
    state.provider === scan.provider &&
    state.auth_shape === scan.auth_shape &&
    state.strix_llm === scan.strix_llm;
  const canSubmit = !pending && handle !== null && matches && total.trim() !== "" && totalOk;

  async function handleSubmit(): Promise<void> {
    if (!canSubmit || handle === null) {
      return;
    }
    setPending(true);
    setFailure(null);
    try {
      await resumeScan(scan.id, { vault_handle: handle, max_budget_usd: totalNumber });
      setOpen(false);
      onResumed();
    } catch (cause) {
      // `key_required` = 后端内存 vault 已经空了或三元组对不上。**不调 `dropKey`**（照 SubmitPanel）。
      if (cause instanceof ApiError && cause.code === "key_required") {
        forget();
      }
      setFailure(cause instanceof Error ? cause : new Error("resume_failed"));
    } finally {
      setPending(false);
    }
  }

  const reason =
    failure instanceof ApiError && failure.code === "resume_unavailable"
      ? failure.params.reason
      : undefined;

  return (
    <section className={styles.root}>
      <div>
        <Button variant="ghost" onClick={() => setOpen((value) => !value)}>
          {open ? t("resume.collapse") : t("resume.toggle")}
        </Button>
      </div>

      {!open ? null : (
        <>
          <dl className={styles.facts}>
            <dt className={styles.label}>{t("resume.spentLabel")}</dt>
            <dd className={styles.value}>{formatUsd(scan.cost_usd)}</dd>
            <dt className={styles.label}>{t("resume.previousCeilingLabel")}</dt>
            <dd className={styles.value}>{formatUsd(scan.max_budget_usd)}</dd>
          </dl>

          <div className={styles.field}>
            <label className={styles.label} htmlFor={`${uid}-total`}>
              {t("resume.totalLabel")}
            </label>
            {/* 刻意不写 `min`：浏览器会把值夹住，下面那句人话永远不会被读到（照 StepBudget）。 */}
            <input
              className={styles.input}
              id={`${uid}-total`}
              type="number"
              inputMode="decimal"
              step={1}
              autoComplete="off"
              value={total}
              disabled={pending}
              onChange={(event) => setTotal(event.target.value)}
            />
            <p className={styles.hint}>{t("resume.totalHint")}</p>
            {total.trim() !== "" && !totalOk ? (
              <p className={styles.warn}>{t("resume.totalTooLow")}</p>
            ) : null}
            {/* 只提示不拦；比的是这次还能花的，不是总额。 */}
            {totalOk && totalNumber - scan.cost_usd < SUGGESTED_MIN_BUDGET_USD ? (
              <p className={styles.hint}>{t("resume.headroomBelowSuggested")}</p>
            ) : null}
          </div>

          {handle === null ? (
            <div className={styles.field}>
              <p className={styles.hint}>{t("resume.credentialsNote")}</p>
              <CredentialForm
                preset={{
                  provider: scan.provider,
                  auth_shape: scan.auth_shape,
                  strix_llm: scan.strix_llm,
                }}
              />
            </div>
          ) : (
            <div className={styles.field}>
              <CredentialSummary handle={handle} />
              {state === undefined || matches ? null : (
                <>
                  <p className={styles.warn}>{t("resume.credentialsNote")}</p>
                  <p className={styles.warn}>{t("resume.credentialsMismatch")}</p>
                </>
              )}
            </div>
          )}

          <div>
            <Button disabled={!canSubmit} onClick={() => void handleSubmit()}>
              {pending ? t("resume.submitting") : t("resume.submit")}
            </Button>
          </div>

          {failure === null ? null : (
            <div className={styles.field}>
              {failure instanceof ApiError ? (
                <ErrorNotice code={failure.code} traceId={failure.traceId} params={failure.params} />
              ) : (
                <ErrorNotice code="internal_error" />
              )}
              {typeof reason === "string" ? (
                <p className={styles.hint}>{t(`resumeRefusal.${reason}` as MessageKey)}</p>
              ) : null}
            </div>
          )}
        </>
      )}
    </section>
  );
}
