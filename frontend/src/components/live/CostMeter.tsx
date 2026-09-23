import { Button } from "@/components/ui/Button";
import { Panel } from "@/components/ui/Panel";
import { formatUsd } from "@/lib/format";
import { t } from "@/lib/messages";

import styles from "./CostMeter.module.css";

/** 横轴是 0 → 1.25 × 上限：上限之后那 25% 是"可能溢出区"。 */
const AXIS = 1.25;
const RESERVE = 0.9;
const SOFT_ALERT = 0.8;

interface CostMeterProps {
  /** `null` = 还在加载。 */
  readonly cost: number | null;
  readonly maxBudgetUsd: number | null;
  readonly canStop: boolean;
  readonly stopPending: boolean;
  readonly onStop: () => void;
}

function axisPct(fraction: number): number {
  return (fraction / AXIS) * 100;
}

export function CostMeter({ cost, maxBudgetUsd, canStop, stopPending, onStop }: CostMeterProps) {
  if (cost === null || maxBudgetUsd === null) {
    return <Panel title={t("budget.heading")}>{t("common.loading")}</Panel>;
  }
  // 上限 <= 0 或非有限数：不画尺、不告警（防除零）。
  const drawable = Number.isFinite(maxBudgetUsd) && maxBudgetUsd > 0 && Number.isFinite(cost);
  const spentPct = drawable ? Math.min(axisPct(Math.max(cost, 0) / maxBudgetUsd), 100) : 0;
  const softAlert = drawable && canStop && cost >= SOFT_ALERT * maxBudgetUsd;

  return (
    <Panel title={t("budget.heading")}>
      <p className={styles.figures}>
        <span className={`${styles.amount} num`}>{formatUsd(cost)}</span>
        <span className={styles.ceiling}>
          {t("budget.ceilingLabel")} <span className="num">{formatUsd(maxBudgetUsd)}</span>
        </span>
      </p>

      {drawable ? (
        <div className={styles.track} aria-hidden="true">
          <div className={styles.overflow} style={{ left: `${axisPct(1)}%` }} />
          <div className={styles.spent} style={{ width: `${spentPct}%` }} />
          <div className={styles.reserve} style={{ left: `${axisPct(RESERVE)}%` }} />
          <div className={styles.limit} style={{ left: `${axisPct(1)}%` }} />
        </div>
      ) : null}

      <ul className={styles.notes}>
        <li>{t("budget.stopsAt")}</li>
        <li>{t("budget.reserveNote")}</li>
        <li>{t("budget.overflowNote")}</li>
      </ul>

      {softAlert ? (
        <div className={styles.alert}>
          <p className={styles.alertText}>{t("live.costSoftAlert")}</p>
          <Button variant="stop" disabled={stopPending} onClick={onStop}>
            {t("scan.stop")}
          </Button>
        </div>
      ) : null}
    </Panel>
  );
}
