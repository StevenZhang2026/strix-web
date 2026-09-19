"use client";

import { useId, useState, type FormEvent } from "react";

import { ErrorNotice } from "@/components/errors/ErrorNotice";
import { Button } from "@/components/ui/Button";
import { StatusDot } from "@/components/ui/StatusDot";
import { ApiError, validateTargets, type TargetValidation } from "@/lib/api/client";
import { t, type MessageKey } from "@/lib/messages";
import { parseTargets, useWizardStore } from "@/lib/stores/wizard";

import steps from "./Steps.module.css";
import styles from "./StepTargets.module.css";

/**
 * 第 1 步：测试目标 + 护栏校验结果。
 *
 * **校验是一个按钮，不是输入防抖。** 服务端每次都会做真实 `getaddrinfo`
 * （分裂域名检测靠的就是它），边打字边查等于每敲一个字符发一次 DNS 查询 ——
 * 对内网 DNS 是一串噪声，对用户是一串半成品结论。
 *
 * 提交走**手写 `async` + `useState`**（照 `credentials/CredentialForm.tsx`），
 * `useQuery` 只给第 4 步的目录用：这是一次有副作用（DNS 查询）的 POST，
 * 不该被 query client 按自己的节奏重放。
 */
export function StepTargets() {
  const rawTargets = useWizardStore((s) => s.rawTargets);
  const setRawTargets = useWizardStore((s) => s.setRawTargets);
  const allowLoopback = useWizardStore((s) => s.allowLoopback);
  const setAllowLoopback = useWizardStore((s) => s.setAllowLoopback);
  const allowPrivate = useWizardStore((s) => s.allowPrivate);
  const setAllowPrivate = useWizardStore((s) => s.setAllowPrivate);

  const validation = useWizardStore((s) => s.validation);
  const setValidation = useWizardStore((s) => s.setValidation);

  const uid = useId();

  // `pending` / `failure` 留在本地 state：它们是这一步的**瞬时态**，不是工单内容。
  // 结论本身进 store —— 第 2 步与提交面板都要消费那份快照（`stores/wizard.ts`
  // 的 `ValidationSnapshot`）。
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<Error | null>(null);

  const targets = parseTargets(rawTargets);

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (pending || targets.length === 0) {
      return;
    }
    setPending(true);
    setFailure(null);
    try {
      const response = await validateTargets({
        raw: targets,
        overrides: { allow_loopback: allowLoopback, allow_private: allowPrivate },
      });
      // 快照里的 `targets` 就是刚才**真的发出去的**那一份 —— 提交时发的也是它。
      setValidation({
        targets,
        allowLoopback,
        allowPrivate,
        results: response.targets,
      });
    } catch (cause) {
      // 失败时把旧结论也清掉：留着它就是屏幕上的一句假话。
      setValidation(null);
      setFailure(cause instanceof Error ? cause : new Error("validate_failed"));
    } finally {
      setPending(false);
    }
  }

  return (
    <form onSubmit={handleSubmit}>
      <div className={steps.row}>
        <label className={steps.label} htmlFor={`${uid}-targets`}>
          {t("home.fieldTarget")}
        </label>
        {/* 一个 textarea、每行一个目标（用户拍板）。不做动态增删的多个 input：
            粘贴一串 URL 是最常见的输入方式，多个 input 会让它变成十次点击。 */}
        <textarea
          className={steps.textarea}
          id={`${uid}-targets`}
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          value={rawTargets}
          disabled={pending}
          onChange={(event) => {
            setRawTargets(event.target.value);
            // 上一次的**结论**由 store 的 setter 自己作废（`stores/wizard.ts`），
            // 这里只清这一步自己的失败提示 —— 它讲的是另一批目标的事。
            setFailure(null);
          }}
        />
        <p className={steps.hint}>{t("wizard.targetsHint")}</p>
      </div>

      <div className={steps.checks}>
        <p className={steps.label}>{t("wizard.optInHeading")}</p>
        {/* 只有这两个开关。`blocked_metadata` 与 `split_horizon` 是永久硬拦、
            不可覆盖的，所以刻意没有第三个勾选框。 */}
        <label className={steps.check}>
          <input
            type="checkbox"
            checked={allowLoopback}
            disabled={pending}
            onChange={(event) => {
              setAllowLoopback(event.target.checked);
              setFailure(null);
            }}
          />
          <span>{t("targetGuard.optIn.loopback")}</span>
        </label>
        <label className={steps.check}>
          <input
            type="checkbox"
            checked={allowPrivate}
            disabled={pending}
            onChange={(event) => {
              setAllowPrivate(event.target.checked);
              setFailure(null);
            }}
          />
          <span>{t("targetGuard.optIn.private")}</span>
        </label>
      </div>

      <div className={steps.actions}>
        <Button type="submit" disabled={pending || targets.length === 0}>
          {pending ? t("wizard.validating") : t("wizard.validate")}
        </Button>
      </div>
      <p className={steps.hint}>{t("wizard.validateHint")}</p>

      {failure !== null ? (
        <div className={steps.failure}>
          {/* 前端按码分支、不匹配文案；`NetworkError` 与渲染期 bug 归
              `internal_error`，判据与 `CredentialForm` 一致。 */}
          {failure instanceof ApiError ? (
            <ErrorNotice code={failure.code} traceId={failure.traceId} params={failure.params} />
          ) : (
            <ErrorNotice code="internal_error" />
          )}
        </div>
      ) : validation === null ? (
        <p className={steps.hint}>{t("wizard.notValidated")}</p>
      ) : (
        <ul className={styles.results}>
          {validation.results.map((item, index) => (
            <li className={styles.result} key={`${index}-${item.raw}`}>
              <TargetResult item={item} />
            </li>
          ))}
        </ul>
      )}
    </form>
  );
}

/** 一条目标的结论。字段全部来自后端，前端不自己判"能不能用"。 */
function TargetResult({ item }: { readonly item: TargetValidation }) {
  /*
   * 什么时候说出"放行条件"这句话。`requirement` 与 `ok` 正交
   * （`routes/targets.py:177-178`），而 `targetGuard.requirements.*` 里有两种语气：
   *   · `ok=false` → 必说。它是"怎么才能放行"的唯一出处 —— 缺勾选那一种连 `code` 都没有，
   *     `allowlist_file_only` 的 `code` 又只是通用的 `not_in_allowlist`。
   *   · `ok=true` 且命中了清单条目 → 不说。上面"命中授权清单"那一行已经说了它为什么能过，
   *     再来一句"需要它出现在授权清单里"是把已完成的事写成待办。
   *   · `ok=true` 且 `requirement === "none"` → **必说**。否则屏幕上只剩"没有命中授权清单里的
   *     任何条目"加一个绿点，看起来自相矛盾；真话是"公网目标，当前是提示模式，可以直接扫"。
   * 拿字面量 `"none"` 比一次是刻意的：这是后端 `GuardRequirement` 的枚举值，
   * 而"这一句是陈述句、其余四句是祈使句"这件事只存在于文案里，没有别的地方能读出来。
   */
  const tellRequirement = item.requirement !== null && (!item.ok || item.requirement === "none");

  return (
    <>
      <p className={styles.head}>
        <StatusDot tone={item.ok ? "ok" : "warn"} />
        <span className={styles.raw}>{item.raw}</span>
        {/* 「能不能扫」必须成句地写在点旁边，不能只由颜色表达 —— 这是 `StatusDot`
            自己的 docstring 定的约定（它 `aria-hidden`，并写明"只有点、没有文字的用法
            是那处用法错了"）。只靠颜色的区分在打印与高对比模式下会消失，而这一位
            正是整张卡里最重要的一位。 */}
        <span className={styles.verdict}>
          {t(item.ok ? "wizard.verdictOk" : "wizard.verdictBlocked")}
        </span>
      </p>

      {item.normalized === null ? null : (
        <dl className={steps.pairs}>
          <dt className={steps.pairKey}>{t("wizard.normalizedLabel")}</dt>
          <dd className={steps.pairValue}>{item.normalized.url}</dd>
          <dt className={steps.pairKey}>{t("wizard.allowlistLabel")}</dt>
          <dd className={steps.pairValue}>
            {item.allowlist_entry ?? t("wizard.allowlistMissing")}
          </dd>
        </dl>
      )}

      {/* punycode：`host_unicode` 与真正被请求的 `host` 不是同一个字符串，
          这正是同形字攻击的形状 —— 所以它不是一行小字，是一块提示。 */}
      {item.normalized !== null && item.normalized.punycode_applied ? (
        <p className={styles.notable}>
          <span className={styles.unicodeHost}>{item.normalized.host_unicode}</span>
          <span>{t("wizard.punycodeNote")}</span>
        </p>
      ) : null}

      {item.resolved_ips.length === 0 ? null : (
        <div className={styles.block}>
          <p className={steps.label}>{t("wizard.resolvedLabel")}</p>
          <ul className={styles.addresses}>
            {item.resolved_ips.map((ip) => (
              <li className={styles.address} key={ip.address}>
                <span className={styles.raw}>{ip.address}</span>
                <span>{t(`targetGuard.categories.${ip.ip_class}` as MessageKey)}</span>
                {/* `rule` 原文展示：它是后端命中的那条规则名，翻译它等于再造一份码表。 */}
                <span className={steps.pairKey}>{ip.rule}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* 放行条件。后端加这个字段就是给这里用的（`routes/targets.py:176-182`：
          "缺勾选"那一种根本没有 `code`，只能靠它说出"怎么才能放行"）。
          `allowlist_file_only` 更是只有它说得出口 —— 那种目标的 `code` 是通用的
          `not_in_allowlist`，而真话是"界面上没有这个开关，只能改清单文件"。
          它同时替掉了原先那句 `wizard.optInMissing`：两句话意思一样，
          屏幕上连着说两遍同一件事比不说更让人怀疑自己看错了。
          说不说由 `tellRequirement` 决定，理由在上面那段注释里。 */}
      {!tellRequirement ? null : item.required_opt_in.length === 0 ? (
        <p className={steps.hint}>
          {t(`targetGuard.requirements.${item.requirement}` as MessageKey)}
        </p>
      ) : (
        // 用中性的重量块而不是 `steps.warn`：`--gate` 那一组语义色说的是"钱"，
        // 而这里缺的是一次授权确认。语义色不串用。
        <div className={styles.block}>
          <p className={styles.notable}>
            {t(`targetGuard.requirements.${item.requirement}` as MessageKey)}
          </p>
          <ul className={styles.plainList}>
            {item.required_opt_in.map((name) => (
              <li key={name}>{t(`targetGuard.optIn.${name}` as MessageKey)}</li>
            ))}
          </ul>
        </div>
      )}

      {item.reason === null ? null : (
        <p className={styles.notable}>{t(`targetGuard.reasons.${item.reason}` as MessageKey)}</p>
      )}
      {item.resolution_error === null ? null : (
        <p className={styles.notable}>
          {t(`targetGuard.resolution.${item.resolution_error}` as MessageKey)}
        </p>
      )}
      {item.note_code === null ? null : (
        <p className={steps.hint}>{t(`targetGuard.notes.${item.note_code}` as MessageKey)}</p>
      )}

      {/* 被护栏拒：码交给 `ErrorNotice`，它自己去 `errors.*` 取 title/detail/action。
          `blocked_metadata` 与 `split_horizon` 这里**没有**任何"强行继续"的入口。 */}
      {item.code === null ? null : (
        <div className={steps.failure}>
          <ErrorNotice code={item.code} />
        </div>
      )}
    </>
  );
}
