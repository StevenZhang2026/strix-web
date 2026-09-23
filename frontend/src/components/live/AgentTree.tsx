import { Panel } from "@/components/ui/Panel";
import { StatusDot, type DotTone } from "@/components/ui/StatusDot";
import type { AgentRow } from "@/lib/api/client";
import { t, type MessageKey } from "@/lib/messages";

import styles from "./AgentTree.module.css";

const KNOWN_STATUS = new Set([
  "running",
  "waiting",
  "completed",
  "stopped",
  "crashed",
  "failed",
  "budget_paused",
]);

const STATUS_TONE: Readonly<Record<string, DotTone>> = {
  running: "ok",
  waiting: "warn",
  budget_paused: "warn",
  crashed: "warn",
  failed: "warn",
};

interface TreeNode {
  readonly agent: AgentRow;
  readonly children: readonly TreeNode[];
}

function bySiblingOrder(a: AgentRow, b: AgentRow): number {
  if (a.created_at !== b.created_at) return a.created_at < b.created_at ? -1 : 1;
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
}

/**
 * 按 `parent_id` 建树。根 = `parent_id === null` **或父节点不在列表里**。
 * 同级按 `created_at`、再按 `id` 排：库里没存上游顺序，这样快照与直播画出来一样。
 * 防环：见过的 id 不再下钻；环上一个根都没有的节点最后补成根，不丢。
 */
function buildTree(agents: readonly AgentRow[]): readonly TreeNode[] {
  const sorted = [...agents].sort(bySiblingOrder);
  const ids = new Set(sorted.map((a) => a.id));
  const childrenOf = new Map<string, AgentRow[]>();
  for (const a of sorted) {
    if (a.parent_id !== null && ids.has(a.parent_id)) {
      const list = childrenOf.get(a.parent_id) ?? [];
      list.push(a);
      childrenOf.set(a.parent_id, list);
    }
  }
  const visited = new Set<string>();
  function build(agent: AgentRow): TreeNode | null {
    if (visited.has(agent.id)) return null;
    visited.add(agent.id);
    const children = (childrenOf.get(agent.id) ?? [])
      .map(build)
      .filter((n): n is TreeNode => n !== null);
    return { agent, children };
  }
  const roots: TreeNode[] = [];
  for (const a of sorted) {
    if (a.parent_id === null || !ids.has(a.parent_id)) {
      const node = build(a);
      if (node !== null) roots.push(node);
    }
  }
  for (const a of sorted) {
    const node = build(a);
    if (node !== null) roots.push(node);
  }
  return roots;
}

function statusText(status: string | null): string {
  return status !== null && KNOWN_STATUS.has(status)
    ? t(`live.agentStatus.${status}` as MessageKey)
    : t("common.unknown");
}

function NodeList({ nodes }: { readonly nodes: readonly TreeNode[] }) {
  return (
    <ul className={styles.list}>
      {nodes.map(({ agent, children }) => (
        <li key={agent.id}>
          <div className={styles.row}>
            <StatusDot tone={STATUS_TONE[agent.status ?? ""] ?? "idle"} />
            <span className={styles.name}>{agent.name ?? agent.id}</span>
            <span className={styles.status}>{statusText(agent.status)}</span>
          </div>
          {children.length > 0 ? <NodeList nodes={children} /> : null}
        </li>
      ))}
    </ul>
  );
}

/** `agents === null` = 还在加载。**不渲染 `error_message`**（上游原文，不经我们的词表）。 */
export function AgentTree({ agents }: { readonly agents: readonly AgentRow[] | null }) {
  return (
    <Panel title={t("live.agentsHeading")}>
      {agents === null ? (
        t("common.loading")
      ) : agents.length === 0 ? (
        <p className={styles.empty}>{t("live.agentsEmpty")}</p>
      ) : (
        <div className={styles.tree}>
          <NodeList nodes={buildTree(agents)} />
        </div>
      )}
    </Panel>
  );
}
