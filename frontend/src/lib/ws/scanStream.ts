/**
 * `/ws/scans/{id}` 的连接与重连。只负责接线：帧交给 `stores/scanLive.ts` 的 `applyFrame`，
 * 组件读 `useScanLiveStore`，不从这里拿任何东西。
 *
 * 协议（服务端已上线）：连上后客户端先发一帧 hello（带游标，首连 `null`），服务端等它才开始
 * 回放；之后客户端不再发任何消息。
 *
 * 为什么 socket / 定时器 / 退避计数全挂在 `useEffect` 的闭包里而不是模块级：
 * 模块级可变状态是本项目明令禁止的（Strix 的教训）；闭包还顺带让"卸载后别再重连"
 * 只需要一个 `disposed` 标志。
 *
 * `generation`：续跑 202 后调用方把它加一 → effect 重跑：`reset` + 空游标 hello，全量回放镜像；
 * 扫描还在 starting 时现有的『1000 → 查 REST → 仍 starting 就重连』兜住。
 */

import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { fetchScan, fetchSession, notifyUnauthenticated } from "@/lib/api/client";
import { isEnvelope, useScanLiveStore } from "@/lib/stores/scanLive";

const BACKOFF_BASE_MS = 1_000;
const BACKOFF_MAX_MS = 15_000;

/** 这两个状态说明 channel 可能只是还没开（刚提交就打开了页面），值得再连。 */
const STILL_RUNNING = new Set(["starting", "running"]);

export function useScanStream(scanId: string, generation: number): void {
  const queryClient = useQueryClient();

  useEffect(() => {
    const store = useScanLiveStore.getState;
    // 规则 12：store 同一时刻只服务一个扫描。
    store().reset(scanId);

    let disposed = false;
    let socket: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;

    // 规则 9：1s、2s、4s … 上限 15s；成功 open 后归零（见 onopen）。
    function scheduleReconnect(): void {
      if (disposed) {
        return;
      }
      store().setConnection("reconnecting");
      const delay = Math.min(BACKOFF_BASE_MS * 2 ** attempt, BACKOFF_MAX_MS);
      attempt += 1;
      timer = setTimeout(connect, delay);
    }

    async function handleClose(code: number, opened: boolean): Promise<void> {
      // 规则 7：收到过 done 就是正常结束。
      if (store().live.done) {
        store().setConnection("ended");
        return;
      }

      // 规则 9 例外：握手期未登录时服务端回的是真 401，但浏览器的 WebSocket 看不到
      // 握手状态码，只看到一次没 open 过的 close（1006）。只能另问一次 /api/auth/me。
      if (!opened) {
        try {
          const session = await fetchSession();
          if (disposed) {
            return;
          }
          if (!session.authenticated) {
            notifyUnauthenticated();
            store().setConnection("ended");
            return;
          }
        } catch {
          // 连 /api/auth/me 都打不通：多半是网络或 api 在重启，照常退避，别判死。
        }
        scheduleReconnect();
        return;
      }

      // 规则 8：1000 但没有 done —— 可能扫描早就结束了（只回放镜像不发 done），
      // 也可能 channel 还没开。问一次 REST 快照来区分。
      if (code === 1000) {
        try {
          const detail = await queryClient.fetchQuery({
            queryKey: ["scan", scanId],
            queryFn: () => fetchScan(scanId),
          });
          if (disposed) {
            return;
          }
          if (STILL_RUNNING.has(detail.scan.status)) {
            scheduleReconnect();
          } else {
            store().setConnection("ended");
          }
        } catch {
          // 拉取失败不等于扫描结束：网络抖动时别把面板永久判死。
          scheduleReconnect();
        }
        return;
      }

      // 规则 9：1011（stream_lagged 被摘）与其它任何异常关闭 —— 带游标重连即可续上。
      scheduleReconnect();
    }

    function connect(): void {
      timer = null;
      if (disposed) {
        return;
      }
      const ws = new WebSocket(
        `wss://${window.location.host}/ws/scans/${encodeURIComponent(scanId)}`,
      );
      socket = ws;
      let opened = false;
      let notFound = false;

      // 规则 5：先发 hello（带当前游标）再算 live。
      ws.onopen = () => {
        opened = true;
        attempt = 0;
        ws.send(JSON.stringify({ type: "hello", resume_from: store().live.cursor }));
        store().setConnection("live");
      };

      ws.onmessage = (ev: MessageEvent) => {
        // 卸载后 / 换了 scanId 后迟到的帧不能写进新扫描的 store。
        if (disposed || typeof ev.data !== "string") {
          return;
        }
        let parsed: unknown;
        try {
          parsed = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (!isEnvelope(parsed)) {
          return;
        }
        // `error` 帧不进 reducer：服务端给它的 `epoch=0, seq=0` 是占位（它不属于任何一代数据，
        // `routes/stream.py` 的 `_error_frame`），合进状态会让"还没有 epoch"被当成第 0 代。
        if (parsed.type === "error") {
          if (parsed.payload.code === "not_found") {
            // 规则 6：扫描不存在，永不重连（随后的 close(1000) 在 onclose 里被跳过）。
            notFound = true;
            store().setConnection("not_found");
          }
          // `stream_lagged` 什么也不用做：随后的 close(1011) 走退避重连。
          return;
        }
        store().apply(parsed);

        if (parsed.type === "done") {
          // 规则 10：done 不带结论，让页面去 REST 拿；不必等 close。
          void queryClient.invalidateQueries({ queryKey: ["scan", scanId] });
        }
      };

      ws.onclose = (ev: CloseEvent) => {
        // 规则 11：卸载触发的那次 close 不许再引起重连。
        if (disposed || notFound) {
          return;
        }
        void handleClose(ev.code, opened);
      };
    }

    connect();

    return () => {
      disposed = true;
      if (timer !== null) {
        clearTimeout(timer);
      }
      socket?.close(1000);
    };
  }, [scanId, generation, queryClient]);
}
