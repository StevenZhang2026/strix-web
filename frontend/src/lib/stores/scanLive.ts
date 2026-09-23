"use client";

/**
 * `/scans/[id]` 实时面板的状态：一个纯 reducer `applyFrame` + 一个 zustand 壳。
 *
 * 为什么 reducer 与 store 分开写：帧的合并规则（epoch 换代、游标取最大、事件 upsert）
 * 是本面板唯一有判定逻辑的地方，写成无 IO 的纯函数才一眼看得清、改得动；
 * store 只负责把它的结果放到组件能订阅的地方。
 *
 * 为什么是 store 而不是 hook 里的 `useState`：写它的是 `lib/ws/scanStream.ts` 的连接闭包，
 * 读它的是页面上好几块互不相干的组件（agent 树、事件流、专家 tab 复用同一份）。
 *
 * 同一时刻只服务一个 `scanId`：hook 挂载时 `reset(scanId)`，旧扫描的帧不会串进来。
 */

import { create } from "zustand";

import type { AgentRow } from "@/lib/api/client";

export interface StreamCursor {
  readonly epoch: number;
  readonly seq: number;
}

export interface LiveEvent {
  readonly key: string;
  readonly kind: string;
  readonly agent_id: string | null;
  readonly ts: string | null;
  readonly upstream_version: number;
  /** 形状由上游工具决定，本层不解释（页面那一层解释）。 */
  readonly data: Readonly<Record<string, unknown>>;
}

export interface LiveNotice {
  readonly code: string;
  readonly keys: readonly string[];
  /** 信封的 `ts`（payload 里没有时间）。 */
  readonly ts: string;
}

export interface LiveSummary {
  readonly run_status: string | null;
  readonly finished: boolean;
  readonly cost_usd: number | null;
  readonly severity: Readonly<Record<string, unknown>> | null;
}

export type StreamConnection = "connecting" | "live" | "reconnecting" | "ended" | "not_found";

export interface LiveState {
  readonly scanId: string | null;
  readonly epoch: number | null;
  /** 见过的最大 `(epoch, seq)`，重连时原样发回去。见 `applyFrame` 规则 2。 */
  readonly cursor: StreamCursor | null;
  readonly events: readonly LiveEvent[];
  /** `null` = 还没收到 `agents` 帧，页面先用 REST 快照里的 `agents[]`。 */
  readonly agents: readonly AgentRow[] | null;
  readonly summary: LiveSummary | null;
  readonly notices: readonly LiveNotice[];
  /** 收到过 `done`。**纯信号**：结论只从 `GET /api/scans/{id}` 取。 */
  readonly done: boolean;
  readonly connection: StreamConnection;
}

/** 服务端每一帧的信封。`payload` 的字段按 `type` 各自在 `applyFrame` 里做最小检查。 */
export interface Envelope {
  readonly v: number;
  readonly epoch: number;
  readonly seq: number;
  readonly type: string;
  readonly ts: string;
  readonly payload: Readonly<Record<string, unknown>>;
}

function isRecord(x: unknown): x is Record<string, unknown> {
  return typeof x === "object" && x !== null && !Array.isArray(x);
}

function isStringOrNull(x: unknown): x is string | null {
  return x === null || typeof x === "string";
}

/**
 * `JSON.parse` 的结果是 `unknown`，这里是它变成 `Envelope` 的唯一关口。
 * 只查信封 6 个键的类型，不合格的帧由调用方直接丢。
 */
export function isEnvelope(x: unknown): x is Envelope {
  return (
    isRecord(x) &&
    typeof x.v === "number" &&
    typeof x.epoch === "number" &&
    typeof x.seq === "number" &&
    typeof x.type === "string" &&
    typeof x.ts === "string" &&
    isRecord(x.payload)
  );
}

function isAgentRow(x: unknown): x is AgentRow {
  return (
    isRecord(x) &&
    typeof x.id === "string" &&
    isStringOrNull(x.name) &&
    isStringOrNull(x.parent_id) &&
    isStringOrNull(x.status) &&
    typeof x.created_at === "string" &&
    typeof x.updated_at === "string" &&
    isStringOrNull(x.error_message)
  );
}

function toLiveEvent(p: Readonly<Record<string, unknown>>): LiveEvent | null {
  if (
    typeof p.key !== "string" ||
    typeof p.kind !== "string" ||
    !isStringOrNull(p.agent_id) ||
    !isStringOrNull(p.ts) ||
    typeof p.upstream_version !== "number" ||
    !isRecord(p.data)
  ) {
    return null;
  }
  return {
    key: p.key,
    kind: p.kind,
    agent_id: p.agent_id,
    ts: p.ts,
    upstream_version: p.upstream_version,
    data: p.data,
  };
}

function toLiveSummary(p: Readonly<Record<string, unknown>>): LiveSummary | null {
  if (
    !isStringOrNull(p.run_status) ||
    typeof p.finished !== "boolean" ||
    !(p.cost_usd === null || typeof p.cost_usd === "number") ||
    !(p.severity === null || isRecord(p.severity))
  ) {
    return null;
  }
  return {
    run_status: p.run_status,
    finished: p.finished,
    cost_usd: p.cost_usd,
    severity: p.severity,
  };
}

/** 按 `key` upsert：已有的原位替换（保持位置），没有的追加到末尾。 */
function upsertEvent(events: readonly LiveEvent[], ev: LiveEvent): readonly LiveEvent[] {
  const i = events.findIndex((e) => e.key === ev.key);
  if (i === -1) {
    return [...events, ev];
  }
  const next = events.slice();
  next[i] = ev;
  return next;
}

/** 按 epoch 再 seq 的字典序，取两者中较大的那个。 */
function maxCursor(a: StreamCursor | null, b: StreamCursor): StreamCursor {
  if (a === null || b.epoch > a.epoch || (b.epoch === a.epoch && b.seq > a.seq)) {
    return b;
  }
  return a;
}

export function initialLiveState(scanId: string | null): LiveState {
  return {
    scanId,
    epoch: null,
    cursor: null,
    events: [],
    agents: null,
    summary: null,
    notices: [],
    done: false,
    connection: "connecting",
  };
}

/**
 * 把一帧合进状态。纯函数：不碰全局、不 throw；形状不对的帧原样返回 state。
 *
 * 规则（都拍过板，别"简化"）：
 *   1. epoch：还没有 → 采纳；更小 → 陈旧代的残帧，丢；更大 → 先清空 `events` 再应用。
 *      只清 `events`：新 epoch 的第一帧往往就是解释"为什么重画了"的那条 notice，
 *      `notices` / `agents` / `summary` / `done` 都留着。
 *   2. `cursor` 取见过的**最大** `(epoch, seq)`，对所有 type 都算 —— 不是"最后一帧的 seq"：
 *      回放与直播的接缝上，非事件帧会以更小的 seq 到达，存最后一帧会让重连要一个更早的位置。
 *   3. `seq` 有空洞是正常的（非事件帧不进镜像，回放必然跳号），这里不检查也不报警。
 *   4. `v !== 1`：不认识的协议版本不猜。
 */
export function applyFrame(state: LiveState, frame: Envelope): LiveState {
  if (frame.v !== 1) {
    return state;
  }
  if (state.epoch !== null && frame.epoch < state.epoch) {
    return state;
  }

  const newEpoch = state.epoch === null || frame.epoch > state.epoch;
  const base: LiveState = {
    ...state,
    epoch: frame.epoch,
    cursor: maxCursor(state.cursor, { epoch: frame.epoch, seq: frame.seq }),
    events: newEpoch && state.epoch !== null ? [] : state.events,
  };
  const p = frame.payload;

  switch (frame.type) {
    case "agents": {
      // 整棵树，不是差分：整个替换。
      const rows = p.agents;
      if (!Array.isArray(rows) || !rows.every(isAgentRow)) {
        return state;
      }
      return { ...base, agents: rows };
    }
    case "event.add":
    case "event.update": {
      // 两者处理完全一样：服务端的 add/update 区分对"按 key upsert"没有信息量。
      const ev = toLiveEvent(p);
      if (ev === null) {
        return state;
      }
      return { ...base, events: upsertEvent(base.events, ev) };
    }
    case "summary": {
      const summary = toLiveSummary(p);
      if (summary === null) {
        return state;
      }
      return { ...base, summary };
    }
    case "notice": {
      const { code, keys } = p;
      if (
        typeof code !== "string" ||
        !Array.isArray(keys) ||
        !keys.every((k): k is string => typeof k === "string")
      ) {
        return state;
      }
      const notice: LiveNotice = { code, keys, ts: frame.ts };
      return { ...base, notices: [...base.notices, notice] };
    }
    case "done":
      return { ...base, done: true };
    case "log":
    case "report":
    case "vuln.add":
      // v1 本面板不渲染这三种（用户 2026-09-23 拍板），内容丢弃。
      // 但返回 base 而不是 state：它们同样占 seq，游标要推进（规则 2 对所有 type 都算）。
      return base;
    default:
      // 未知 type 不猜，但游标照样推进：规则 2 对所有帧都算。
      // （`error` 帧到不了这里：连接层在 apply 之前就把它拦下了，见 `ws/scanStream.ts`。）
      return base;
  }
}

interface ScanLiveStore {
  readonly live: LiveState;
  readonly reset: (scanId: string) => void;
  readonly apply: (frame: Envelope) => void;
  readonly setConnection: (connection: StreamConnection) => void;
}

export const useScanLiveStore = create<ScanLiveStore>((set) => ({
  live: initialLiveState(null),
  reset: (scanId) => {
    set({ live: initialLiveState(scanId) });
  },
  apply: (frame) => {
    set((s) => ({ live: applyFrame(s.live, frame) }));
  },
  setConnection: (connection) => {
    set((s) => ({ live: { ...s.live, connection } }));
  },
}));
