"use client";

import { useState } from "react";

import { ErrorNotice } from "@/components/errors/ErrorNotice";
import { Panel } from "@/components/ui/Panel";
import { ApiError, apiFetch } from "@/lib/api/client";
import { t } from "@/lib/messages";
import type { LiveEvent } from "@/lib/stores/scanLive";

import { asRecord } from "./EventFeed";
import styles from "./ScreenshotGallery.module.css";

/** 加载失败的探测结果：`probing` 期间保持空位，避免同一 URL 被探两次。 */
type Failure = { readonly kind: "probing" } | { readonly kind: "api"; readonly error: ApiError } | { readonly kind: "other" };

/**
 * 同源媒体 URL，去重，最新的在前。只认后端改写过的 `/api/scans/{id}/media/…`
 * —— `data:` 或任何别的地址一律不渲染，页面不许去加载任意外部地址。
 */
function screenshotUrls(events: readonly LiveEvent[], scanId: string): string[] {
  const prefix = `/api/scans/${scanId}/media/`;
  const seen = new Set<string>();
  for (const e of events) {
    if (e.kind !== "tool") continue;
    const result = asRecord(e.data.result);
    const url = result?.image_url;
    if (result?.type === "image" && typeof url === "string" && url.startsWith(prefix)) seen.add(url);
  }
  return [...seen].reverse();
}

export function ScreenshotGallery({
  events,
  scanId,
}: {
  readonly events: readonly LiveEvent[];
  readonly scanId: string;
}) {
  const [failures, setFailures] = useState<Readonly<Record<string, Failure>>>({});
  const urls = screenshotUrls(events, scanId);

  async function probe(url: string) {
    if (failures[url] !== undefined) return;
    setFailures((f) => ({ ...f, [url]: { kind: "probing" } }));
    let failure: Failure;
    try {
      await apiFetch<unknown>(url);
      failure = { kind: "other" };
    } catch (error: unknown) {
      failure = error instanceof ApiError ? { kind: "api", error } : { kind: "other" };
    }
    setFailures((f) => ({ ...f, [url]: failure }));
  }

  function renderFailure(failure: Failure) {
    if (failure.kind === "probing") return null;
    if (failure.kind === "api") {
      const e = failure.error;
      return <ErrorNotice code={e.code} traceId={e.traceId} params={e.params} />;
    }
    return <p className={styles.failed}>{t("live.screenshotLoadFailed")}</p>;
  }

  return (
    <Panel title={t("live.screenshotsHeading")}>
      {urls.length === 0 ? (
        <p className={styles.empty}>{t("live.screenshotsEmpty")}</p>
      ) : (
        <ul className={styles.list}>
          {urls.map((url) => {
            const failure = failures[url];
            return (
              <li key={url}>
                {failure !== undefined ? (
                  renderFailure(failure)
                ) : (
                  <a href={url} target="_blank" rel="noreferrer">
                    {/* eslint-disable-next-line @next/next/no-img-element -- 同源鉴权图片，走 next/image 会被优化器代理、丢 cookie */}
                    <img
                      className={styles.img}
                      src={url}
                      alt={t("live.screenshotsHeading")}
                      loading="lazy"
                      onError={() => void probe(url)}
                    />
                  </a>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}
