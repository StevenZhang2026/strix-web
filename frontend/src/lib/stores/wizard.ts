"use client";

import { create } from "zustand";

import type {
  CreateScanRequest,
  ScanAcceptedResponse,
  TargetValidation,
} from "@/lib/api/client";

/**
 * 第 1 步那一次校验的**快照**。
 *
 * `targets` 是**真的发给后端的那份原文** —— 提交时发的就是它，不是把输入框重新
 * `parseTargets` 一遍。后端明写"不排序、不去重、不重排"（逐字确认串的期望值是
 * `targets[0]` 规范化后的 host），所以"发出去的 = 校验过的"必须是一条结构性事实，
 * 不能靠两处各自 parse 出同一个结果。
 */
export interface ValidationSnapshot {
  readonly targets: readonly string[];
  readonly allowLoopback: boolean;
  readonly allowPrivate: boolean;
  readonly results: readonly TargetValidation[];
}

/**
 * 三条声明。**三个具名 bool，不是 `string[]`** —— 数组里可以塞进两个一样的 id，
 * 而后端正是为这件事加了"`affirmed` 三个必须互不相同"的校验。
 */
export interface Affirmations {
  readonly owns_or_authorized: boolean;
  readonly not_third_party_production: boolean;
  readonly understands_real_attacks: boolean;
}

export type AffirmationId = keyof Affirmations;

/**
 * 三条声明的 id 与**渲染顺序**。后端只认这三个字面量。
 * 组装 `affirmed` 数组也用它 —— 顺序不重要，但固定下来便于比对。
 */
export const AFFIRMATION_IDS: readonly AffirmationId[] = [
  "owns_or_authorized",
  "not_third_party_production",
  "understands_real_attacks",
];

/**
 * 向导草稿。**纯内存**：不 persist、不碰 `sessionStorage`、不进 URL ——
 * 刷新页面草稿就没了，这是刻意的。里面有测试目标与预算，落盘一份等于多一个
 * 要解释来源的副本，而重填一份工单的成本远低于此。
 *
 * `vault_handle` 在 `stores/keys.ts` 里，不进这里。
 */
export interface WizardState {
  readonly step: number;
  /** textarea 原文，每行一个目标。拆行与去空行由 `parseTargets` 做。 */
  readonly rawTargets: string;
  readonly allowLoopback: boolean;
  readonly allowPrivate: boolean;
  readonly templateId: string | null;
  /**
   * 预算与轮数存**输入框原文**而不是 `number`：清空输入框时 `Number("")` 是 0，
   * 会把"还没填"显示成"填了 0"，而 0 与 1.5 在下限判定上是两种不同的话要说。
   */
  readonly budgetUsd: string;
  readonly maxTurns: string;

  /** 第 1 步的校验快照。`null` = 当前这批目标还没有过任何结论。 */
  readonly validation: ValidationSnapshot | null;

  readonly authorizationRef: string;
  readonly operatorName: string;
  readonly typedConfirmation: string;
  readonly affirmations: Affirmations;
  readonly multiTargetAffirmed: boolean;

  /**
   * 202 的回执。**在 store 里而不是提交面板的本地 state 里**：它是整份草稿的
   * 终态，而本地 state 会随"翻回第 3 步看一眼"一起消失 —— 那个 `scan_id` 是
   * 屏幕上唯一一份（没有扫描列表、`GET /api/scans/{id}` 还不存在，丢了连
   * `POST /api/scans/{id}/stop` 都调不出来）。
   *
   * 刻意**不**随目标变化作废：它记的是"这份草稿已经交出去了"，那件事不会因为
   * 之后又改了一行输入而没发生。
   */
  readonly accepted: ScanAcceptedResponse | null;

  readonly setStep: (step: number) => void;
  readonly setRawTargets: (raw: string) => void;
  readonly setAllowLoopback: (allow: boolean) => void;
  readonly setAllowPrivate: (allow: boolean) => void;
  /**
   * 选模板 = 选模板 **并**把预算与轮数重置成它的默认值（用户拍板）。
   * 刻意**不记**"用户改过没有"来避免重置：那是一个记不住的隐式状态 ——
   * 屏幕上看不出它此刻是哪一档，而它决定了下一次换模板的行为。
   */
  readonly chooseTemplate: (templateId: string, budgetUsd: number, maxTurns: number) => void;
  readonly setBudgetUsd: (budgetUsd: string) => void;
  readonly setMaxTurns: (maxTurns: string) => void;
  readonly setValidation: (snapshot: ValidationSnapshot | null) => void;
  readonly setAuthorizationRef: (ref: string) => void;
  readonly setOperatorName: (name: string) => void;
  readonly setTypedConfirmation: (typed: string) => void;
  readonly setAffirmation: (id: AffirmationId, checked: boolean) => void;
  readonly setMultiTargetAffirmed: (affirmed: boolean) => void;
  readonly setAccepted: (response: ScanAcceptedResponse) => void;
}

const NO_AFFIRMATIONS: Affirmations = {
  owns_or_authorized: false,
  not_third_party_production: false,
  understands_real_attacks: false,
};

/**
 * 目标那一批变了 → 上一次的**校验结论与整份声明**一起作废。
 *
 * 为什么声明也要清：三条勾选与"以上 N 个均已授权"说的是**那一批目标**的事。
 * 只清校验结果的话，勾还留着 —— 唯一挡住它的是逐字确认串，而那只钉住
 * `targets[0]`：改掉第二行、加一个新 host，三条声明与多目标那一条**全都仍然勾着**，
 * 用户就为一份他没签过的清单交了一次签字。而这一屏存在的理由正是这件事。
 *
 * `authorizationRef` 与 `operatorName` **不清**：授权编号与负责人是这次委托的属性，
 * 不是某一批目标的属性，改一个错字就把它们清掉是在惩罚改错字。
 */
const CLEARED_DECLARATION = {
  validation: null,
  typedConfirmation: "",
  affirmations: NO_AFFIRMATIONS,
  multiTargetAffirmed: false,
} as const;

export const useWizardStore = create<WizardState>((set) => ({
  step: 1,
  rawTargets: "",
  allowLoopback: false,
  allowPrivate: false,
  templateId: null,
  // 空串而不是某个数字：默认值只能来自 `/api/scan-templates`，前端编一个
  // 就会在后端常量变化时静默漂移。
  budgetUsd: "",
  maxTurns: "",
  validation: null,
  authorizationRef: "",
  operatorName: "",
  typedConfirmation: "",
  affirmations: NO_AFFIRMATIONS,
  multiTargetAffirmed: false,
  accepted: null,

  setStep: (step: number) => {
    set({ step });
  },
  // 下面三个 setter 都顺手作废 `CLEARED_DECLARATION` 那一组。**这是结构性的，
  // 不是礼貌**：第 2 步与提交面板都消费那份快照与那几个勾，靠三个组件各自记得
  // 调一次"作废"是一条迟早会破的口头约定；写在 setter 里之后，"store 里存在一份
  // 与当前目标不符的校验结果或声明"在结构上不可能发生。
  setRawTargets: (rawTargets: string) => {
    set({ rawTargets, ...CLEARED_DECLARATION });
  },
  setAllowLoopback: (allowLoopback: boolean) => {
    set({ allowLoopback, ...CLEARED_DECLARATION });
  },
  setAllowPrivate: (allowPrivate: boolean) => {
    set({ allowPrivate, ...CLEARED_DECLARATION });
  },
  chooseTemplate: (templateId: string, budgetUsd: number, maxTurns: number) => {
    set({ templateId, budgetUsd: String(budgetUsd), maxTurns: String(maxTurns) });
  },
  setBudgetUsd: (budgetUsd: string) => {
    set({ budgetUsd });
  },
  setMaxTurns: (maxTurns: string) => {
    set({ maxTurns });
  },
  setValidation: (validation: ValidationSnapshot | null) => {
    set({ validation });
  },
  setAuthorizationRef: (authorizationRef: string) => {
    set({ authorizationRef });
  },
  setOperatorName: (operatorName: string) => {
    set({ operatorName });
  },
  setTypedConfirmation: (typedConfirmation: string) => {
    set({ typedConfirmation });
  },
  setAffirmation: (id: AffirmationId, checked: boolean) => {
    set((state) => ({ affirmations: { ...state.affirmations, [id]: checked } }));
  },
  setMultiTargetAffirmed: (multiTargetAffirmed: boolean) => {
    set({ multiTargetAffirmed });
  },
  // 只有 202 会调它，所以没有"置回 null"这一路 —— 一次已经发起的扫描收不回来。
  setAccepted: (accepted: ScanAcceptedResponse) => {
    set({ accepted });
  },
}));

/**
 * 预算下限（用户拍板）。实测一次模型调用能花 $0.2，上限比这低几倍等于
 * "钱花了、结论没有" —— 扫描会在拿到任何结论之前就停。
 *
 * **改这个数要同时改 `wizard.budgetTooLow` 那句话**：`t()` 刻意没有占位符
 * （`messages/README.md` 约定 4），所以下限只能在文案里写成字面数字 ——
 * 而一句"太低了"却不说"那要填多少"，等于把用户留在原地。`MIN_TURNS` 与
 * `wizard.turnsTooLow` 同理。
 */
export const MIN_BUDGET_USD = 2;

/** 轮数下限：0 轮的扫描不是扫描。 */
export const MIN_TURNS = 1;

/** 输入框原文 → 目标列表。空行忽略，每行 `trim()`。纯函数。 */
export function parseTargets(rawTargets: string): readonly string[] {
  return rawTargets
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "");
}

/** 预算低于下限（含没填、填了非数字）。纯函数，第 5 步的提示与前进闸共用它。 */
export function isBudgetTooLow(budgetUsd: string): boolean {
  const value = Number(budgetUsd);
  return budgetUsd.trim() === "" || !Number.isFinite(value) || value < MIN_BUDGET_USD;
}

/** 轮数低于下限（含没填、填了非数字）。纯函数。 */
export function isTurnsTooLow(maxTurns: string): boolean {
  const value = Number(maxTurns);
  return maxTurns.trim() === "" || !Number.isFinite(value) || value < MIN_TURNS;
}

/**
 * 要用户逐字打一遍的那个串 = 第一条目标规范化后的 host。拿不到回 `null`。
 *
 * **用 `host` 而不是 `host_unicode`**：后端比的是 punycode 之后那个串。
 */
export function expectedConfirmation(snapshot: ValidationSnapshot | null): string | null {
  return snapshot?.results[0]?.normalized?.host ?? null;
}

/**
 * 声明授权时**我看到的地址**（`host → [address, ...]`）。后端起扫描前会重新解析
 * 一遍并按集合比对，不一致即 `dns_changed`。
 *
 * 同一个 host 出现两次就覆盖 —— 同一次校验里它们的地址必然相同。
 */
export function declaredIps(snapshot: ValidationSnapshot): Record<string, readonly string[]> {
  const declared: Record<string, readonly string[]> = {};
  for (const item of snapshot.results) {
    if (item.normalized === null) {
      continue;
    }
    declared[item.normalized.host] = item.resolved_ips.map((ip) => ip.address);
  }
  return declared;
}

/**
 * 逐字确认对不对。后端用 `strip().casefold()`；host 已经是 ASCII 小写，
 * `toLowerCase()` 足够，不引 `Intl`。
 */
export function confirmationMatches(typed: string, expected: string | null): boolean {
  return expected !== null && typed.trim().toLowerCase() === expected.trim().toLowerCase();
}

/**
 * 还差哪几项才能提交。与 `wizard.blockers.*` 的键**一一对应**。
 */
export type BlockerCode =
  | "targets_not_validated"
  | "target_blocked"
  | "authorization_ref"
  | "affirmations"
  | "typed_confirmation"
  | "multi_target"
  | "operator_name"
  | "template"
  | "budget"
  | "turns"
  | "vault_handle";

/**
 * 提交闸。**无 IO 的纯函数**，返回稳定机器码数组（空数组 = 可以提交）。
 *
 * 顺序就是用户填写的顺序 —— 读起来是"从前往后还差什么"。
 * `vault_handle` 在另一个 store 里，所以它是独立参数。
 */
export function submitBlockers(
  state: WizardState,
  vaultHandle: string | null,
): readonly BlockerCode[] {
  const snapshot = state.validation;
  const blockers: BlockerCode[] = [];

  if (snapshot === null) {
    blockers.push("targets_not_validated");
  } else if (snapshot.results.some((item) => !item.ok)) {
    blockers.push("target_blocked");
  }
  if (state.authorizationRef.trim() === "") {
    blockers.push("authorization_ref");
  }
  if (!AFFIRMATION_IDS.every((id) => state.affirmations[id])) {
    blockers.push("affirmations");
  }
  if (!confirmationMatches(state.typedConfirmation, expectedConfirmation(snapshot))) {
    blockers.push("typed_confirmation");
  }
  if (snapshot !== null && snapshot.targets.length > 1 && !state.multiTargetAffirmed) {
    blockers.push("multi_target");
  }
  if (state.operatorName.trim() === "") {
    blockers.push("operator_name");
  }
  if (state.templateId === null) {
    blockers.push("template");
  }
  if (isBudgetTooLow(state.budgetUsd)) {
    blockers.push("budget");
  }
  if (isTurnsTooLow(state.maxTurns)) {
    blockers.push("turns");
  }
  if (vaultHandle === null) {
    blockers.push("vault_handle");
  }
  return blockers;
}

/**
 * 组装 `POST /api/scans` 的请求体。
 *
 * **`Number()` 转换只发生在这里**（store 里存的是输入框原文）。
 * `targets` 与 `overrides` 一律取**快照**里的值，不取 store 当前值 —— 两者不一致时
 * store 里那份快照根本不存在，但取快照让"发出去的 = 校验过的"成为读代码就能看出来
 * 的事实。
 *
 * `templateId` 为 `null` 时抛：提交按钮由 `submitBlockers()` 拦着，走到这里说明
 * 闸坏了 —— 编一个空串发出去会让后端替我们说一句更难懂的话。
 */
export function buildCreateScanRequest(
  state: WizardState,
  vaultHandle: string,
  snapshot: ValidationSnapshot,
): CreateScanRequest {
  if (state.templateId === null) {
    throw new Error("template_required");
  }
  return {
    vault_handle: vaultHandle,
    template_id: state.templateId,
    targets: snapshot.targets,
    overrides: {
      allow_loopback: snapshot.allowLoopback,
      allow_private: snapshot.allowPrivate,
    },
    max_budget_usd: Number(state.budgetUsd),
    max_turns: Number(state.maxTurns),
    authorization: {
      operator_name: state.operatorName.trim(),
      authorization_ref: state.authorizationRef.trim(),
      typed_confirmation: state.typedConfirmation.trim(),
      affirmed: AFFIRMATION_IDS.filter((id) => state.affirmations[id]),
      multi_target_affirmed: state.multiTargetAffirmed,
      resolved_ips_seen: declaredIps(snapshot),
    },
  };
}
