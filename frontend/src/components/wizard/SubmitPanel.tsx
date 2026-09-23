"use client";

import Link from "next/link";
import { useState } from "react";

import { ErrorNotice } from "@/components/errors/ErrorNotice";
import { Button } from "@/components/ui/Button";
import { ButtonLink } from "@/components/ui/ButtonLink";
import { ApiError, createScan, type ScanAcceptedResponse } from "@/lib/api/client";
import { formatUsd } from "@/lib/format";
import { t, type MessageKey } from "@/lib/messages";
import { useKeysStore } from "@/lib/stores/keys";
import { buildCreateScanRequest, submitBlockers, useWizardStore } from "@/lib/stores/wizard";

import steps from "./Steps.module.css";
import styles from "./SubmitPanel.module.css";

/**
 * 提交整份工单。第 5 步底部。
 *
 * 走**手写 `async` + `useState`**（照 `credentials/CredentialForm.tsx`），刻意
 * **不用** `useMutation`：这是一次会真的起进程、真的花钱的 POST，不该被 query
 * client 按自己的节奏重放。
 *
 * 三态：未提交（闸门清单 + 按钮）／失败（`ErrorNotice`，按钮仍可再按）／
 * 成功（成功块，**并且不再渲染提交按钮**）。
 */
export function SubmitPanel() {
  // 闸门几乎依赖 store 里的每一个字段，所以整份订阅 —— 挑字段订阅在这里只会是
  // 一张必须跟着 `submitBlockers()` 一起改的清单。
  const state = useWizardStore();
  const setAccepted = useWizardStore((s) => s.setAccepted);
  const handle = useKeysStore((s) => s.handle);
  const forget = useKeysStore((s) => s.forget);

  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<Error | null>(null);

  // 成功之后整块换成成功块：同一份工单按第二次就是第二次扫描，而并发上限默认 1，
  // 第二次也只会拿到 `concurrency_limit` —— 让它按不下去更诚实。
  // 回执在 **store** 里（`stores/wizard.ts` 的 `accepted`），不在这里的本地 state：
  // 本地 state 会随"翻回第 3 步看一眼"一起消失，而那个 `scan_id` 是屏幕上唯一一份。
  if (state.accepted !== null) {
    return <Accepted response={state.accepted} />;
  }

  const blockers = submitBlockers(state, handle);

  async function handleSubmit(): Promise<void> {
    const snapshot = state.validation;
    if (pending || blockers.length > 0 || handle === null || snapshot === null) {
      return;
    }
    setPending(true);
    setFailure(null);
    try {
      setAccepted(await createScan(buildCreateScanRequest(state, handle, snapshot)));
    } catch (cause) {
      // `key_required` = 后端内存 vault 已经空了（`api` 重启过）。本地还留着 handle
      // 就是屏幕上的一句假话。**不调 `DELETE /api/keys/{h}`** —— 那是"用户主动忘记"
      // 的路径，这里不是。
      if (cause instanceof ApiError && cause.code === "key_required") {
        forget();
      }
      setFailure(cause instanceof Error ? cause : new Error("create_scan_failed"));
    } finally {
      setPending(false);
    }
  }

  return (
    <div className={styles.panel}>
      {blockers.length === 0 ? null : (
        <div className={styles.blockers}>
          <p className={steps.label}>{t("wizard.blockersHeading")}</p>
          <ul className={styles.blockerList}>
            {blockers.map((code) => (
              <li key={code}>
                {t(`wizard.blockers.${code}` as MessageKey)}
                {/* 缺凭据是唯一一条"在别的页面才能补"的 —— 给它一个去处。 */}
                {code !== "vault_handle" ? null : (
                  <Link className={styles.link} href="/credentials">
                    {t("credentials.provide")}
                  </Link>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className={steps.actions}>
        <Button type="button" disabled={blockers.length > 0 || pending} onClick={handleSubmit}>
          {pending ? t("wizard.submitting") : t("wizard.submit")}
        </Button>
      </div>
      <p className={steps.hint}>{t("wizard.submitHint")}</p>

      {failure === null ? null : (
        <div className={steps.failure}>
          {/* 前端按码分支、不匹配文案；`NetworkError` 与渲染期 bug 归 `internal_error`，
              判据与 `CredentialForm` 一致。`blocked_metadata` 这里**没有**任何
              "强行继续"的入口 —— 它是永久硬拦，给入口就是撒谎。 */}
          {failure instanceof ApiError ? (
            <ErrorNotice code={failure.code} traceId={failure.traceId} params={failure.params} />
          ) : (
            <ErrorNotice code="internal_error" />
          )}
          <Rejection failure={failure} targets={state.validation?.targets ?? []} />
        </div>
      )}
    </div>
  );
}

/**
 * `params.reason`（目标规范化被拒）时额外说两句：那句人话，以及**哪一个**目标。
 *
 * 后端从不回显目标原文（`raw` 只出现在 validate 的 200 正文里），所以原文只能从
 * 本地这份快照按 `params.index`（0 基）取。
 */
function Rejection({
  failure,
  targets,
}: {
  readonly failure: Error;
  readonly targets: readonly string[];
}) {
  if (!(failure instanceof ApiError)) {
    return null;
  }
  const reason = failure.params.reason;
  if (typeof reason !== "string") {
    return null;
  }
  const index = failure.params.index;
  const raw = typeof index === "number" ? targets[index] : undefined;

  return (
    <div className={styles.rejection}>
      <p className={steps.hint}>{t(`targetGuard.reasons.${reason}` as MessageKey)}</p>
      {raw === undefined ? null : (
        <dl className={steps.pairs}>
          <dt className={steps.pairKey}>{t("wizard.rejectedTargetLabel")}</dt>
          <dd className={`${steps.pairValue} ${steps.mono}`}>{raw}</dd>
        </dl>
      )}
    </div>
  );
}

/**
 * 202 之后。
 *
 * 链接指向 `/scans/{scan_id}`；仍不渲染 `ws`（WS 由实时面板自己连）。
 */
function Accepted({ response }: { readonly response: ScanAcceptedResponse }) {
  return (
    <div className={styles.accepted}>
      <p className={styles.acceptedTitle}>{t("wizard.acceptedTitle")}</p>
      <dl className={steps.pairs}>
        <dt className={steps.pairKey}>{t("wizard.acceptedScanId")}</dt>
        <dd className={`${steps.pairValue} ${steps.mono}`}>{response.scan_id}</dd>
        <dt className={steps.pairKey}>{t("wizard.acceptedStatus")}</dt>
        <dd className={steps.pairValue}>{t(`status.${response.status}` as MessageKey)}</dd>
        <dt className={steps.pairKey}>{t("wizard.acceptedBudget")}</dt>
        <dd className={steps.pairValue}>{formatUsd(response.budget_usd)}</dd>
      </dl>

      {/* 逐项一行，**刻意不拼成一条 shell 命令**：拼出来的东西会被人复制去执行，
          而它不是一条能直接跑的命令。 */}
      <details className={styles.argv}>
        <summary className={styles.argvSummary}>{t("wizard.argvHeading")}</summary>
        <p className={steps.hint}>{t("wizard.argvHint")}</p>
        <ol className={styles.argvList}>
          {response.argv_preview.map((item, index) => (
            <li className={steps.mono} key={`${index}-${item}`}>
              {item}
            </li>
          ))}
        </ol>
      </details>

      <ButtonLink href={`/scans/${encodeURIComponent(response.scan_id)}`}>{t("live.openLive")}</ButtonLink>
    </div>
  );
}
