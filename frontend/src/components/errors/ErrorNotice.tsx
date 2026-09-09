import type { ApiParamValue } from "@/lib/api/client";
import { errorCopy, paramLabel, scanFailureCopy, t, type ErrorCopy } from "@/lib/messages";

import styles from "./ErrorNotice.module.css";

/**
 * 两棵码树，刻意分开（镜像 `backend/app/errors.py`）：
 *   · `http` —— 这次**请求**失败了（19 个 `ConsoleError` 子类）
 *   · `scan` —— 请求成功了，但**那次扫描**失败了（13 个 `SCAN_FAILURE_CODES`）
 * 合成一棵会让前端有一天去为 `llm_tls_intercepted` 找 HTTP 状态码。
 */
export type CodeTree = "http" | "scan";

interface ErrorNoticeProps {
  readonly code: string;
  readonly tree?: CodeTree;
  /** 后端 `{code, trace_id, params}` 里的 `trace_id`。没有就不显示那一行。 */
  readonly traceId?: string;
  /** 后端给的具体值。只渲染 `params` 里登记过的那几个键，多余的忽略。 */
  readonly params?: Readonly<Record<string, ApiParamValue>>;
}

/**
 * 把参数值渲染成字符串。
 *
 * `null` 与 `undefined` 一律不渲染那一行：一行「解析到 —」不如没有这一行。
 * 数组（`declared_ips` 这类）由后端序列化成什么就显示什么 —— 后端的
 * `ParamValue` 只有 string / number / boolean / null 四种，数组是已经拼好的字符串。
 */
function renderValue(value: ApiParamValue): string | null {
  if (value === null) {
    return null;
  }
  if (typeof value === "boolean") {
    // 布尔参数目前一个都没有。真出现了，`true` / `false` 直接露出来比翻译成
    // 「是 / 否」好：那两个字需要上下文才有意义，而这里没有上下文。
    return String(value);
  }
  return String(value);
}

/**
 * 错误告知块。**服务端组件**（纯展示，没有交互）。
 *
 * 前端**按码分支，不匹配文案**（CLAUDE.md §错误与文案）。这个组件就是那条规则的
 * 落点：调用方只交出一个机器码，中文从 `messages/zh-CN.json` 取。
 */
export function ErrorNotice({ code, tree = "http", traceId, params }: ErrorNoticeProps) {
  const looked = tree === "http" ? errorCopy(code) : scanFailureCopy(code);

  /**
   * 码没登记时的处理：用 `internal_error` 的文案，**并且把原始码作为一行相关信息
   * 露出来**。只做前者会把"后端加了新码、前端文案没跟上"伪装成"后端崩了"，
   * 那两件事的处理方式完全不同。露出原始码之后，看到页面的人就能直接反馈它。
   *
   * 这一支正常情况下永远走不到 —— `backend/tests/test_message_coverage.py`
   * 会在 T6–T26 任何人加机器码而没加文案时让测试失败。它是那道门失守后的兜底。
   */
  const copy: ErrorCopy = looked ?? {
    // 经 `t()` 逐条取，而不是 `errorCopy("internal_error")!` —— 后者要一个非空断言，
    // 而 `t()` 的 key 是编译期校验过的，不需要任何断言就能保证这三句存在。
    title: t("errors.internal_error.title"),
    detail: t("errors.internal_error.detail"),
    action: t("errors.internal_error.action"),
    params: [],
  };
  const unknownCode = looked === null;

  const rows: { key: string; label: string; value: string }[] = [];
  if (unknownCode) {
    rows.push({ key: "__code", label: paramLabel("code"), value: code });
  }
  for (const name of copy.params) {
    const raw = params?.[name];
    if (raw === undefined) {
      continue;
    }
    const value = renderValue(raw);
    if (value === null) {
      continue;
    }
    rows.push({ key: name, label: paramLabel(name), value });
  }
  if (traceId !== undefined) {
    rows.push({ key: "__trace", label: t("common.traceIdLabel"), value: traceId });
  }

  return (
    <div className={styles.notice} role="alert">
      <p className={styles.title}>{copy.title}</p>
      <p className={styles.detail}>{copy.detail}</p>
      <p className={styles.action}>{copy.action}</p>

      {rows.length === 0 ? null : (
        <dl className={styles.rows}>
          {rows.map((row) => (
            <Row key={row.key} label={row.label} value={row.value} />
          ))}
        </dl>
      )}

      {traceId === undefined ? null : <p className={styles.traceHint}>{t("common.traceIdHint")}</p>}
    </div>
  );
}

/** 一对 dt/dd。抽出来只为了让 key 挂在这一对上，理由同 `ui/Field.tsx`。 */
function Row({ label, value }: { readonly label: string; readonly value: string }) {
  return (
    <>
      <dt className={styles.rowKey}>{label}</dt>
      <dd className={styles.rowValue}>{value}</dd>
    </>
  );
}
