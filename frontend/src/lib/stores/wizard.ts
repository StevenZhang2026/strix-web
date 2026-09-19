"use client";

import { create } from "zustand";

/**
 * 向导草稿。**纯内存**：不 persist、不碰 `sessionStorage`、不进 URL ——
 * 刷新页面草稿就没了，这是刻意的。里面有测试目标与预算，落盘一份等于多一个
 * 要解释来源的副本，而重填一份工单的成本远低于此。
 *
 * 字段只有第 1／4／5 步真正用到的那些。授权依据、操作人、逐字确认串是 T18b 的，
 * 现在一个都不声明（CLAUDE.md §编码哲学 3：为"以后可能"预留的东西一律不写）。
 * `vault_handle` 在 `stores/keys.ts` 里，不进这里。
 */
interface WizardState {
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
}

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

  setStep: (step: number) => {
    set({ step });
  },
  setRawTargets: (rawTargets: string) => {
    set({ rawTargets });
  },
  setAllowLoopback: (allowLoopback: boolean) => {
    set({ allowLoopback });
  },
  setAllowPrivate: (allowPrivate: boolean) => {
    set({ allowPrivate });
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
