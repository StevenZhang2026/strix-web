/**
 * 文案的唯一入口。**组件里不得出现中文字面量**（CLAUDE.md §TypeScript）。
 *
 * 只有一种语言，所以刻意**不引** next-intl / i18next —— 那些框架卖的是运行时切换
 * 语言、复数规则、消息编译，我们一样都不需要，只需要"把 JSON 里的一句话取出来"。
 *
 * 约定与理由见 `frontend/messages/README.md`。
 */

import messages from "../../messages/zh-CN.json";

/**
 * 所有**字符串叶子**的点分路径，由 JSON 的字面量类型直接推出来。
 *
 * 为什么值得这几行类型体操（CLAUDE.md §编码哲学 2「魔法需论证」）：
 * 它把"文案 key 拼错了"从一个**运行期空白**变成一个**编译错误**。没有它，
 * `t('home.docketTitel')` 会安静地渲染出一段 key 字符串，而这类错误只在有人正好
 * 打开那个页面时才被发现 —— 而某些页面（错误态、失效态）正是最少被打开的。
 * 代价只有一处：读这个文件的人要认识映射类型。收益覆盖全项目每一句文案。
 *
 * `T[K] extends readonly unknown[] ? never` 这一支是给 `errors.*.params` 用的：
 * 它是数组，不是可显示的字符串，不该出现在 `t()` 的可选路径里。
 */
type MessagePath<T> = {
  [K in keyof T & string]: T[K] extends string
    ? K
    : T[K] extends readonly unknown[]
      ? never
      : `${K}.${MessagePath<T[K]>}`;
}[keyof T & string];

export type MessageKey = MessagePath<typeof messages>;

/**
 * 取一句文案。
 *
 * 类型已经保证 key 存在，所以两处 `return key` 的兜底只在"JSON 被改坏了"时才会走到
 * （例如有人把一个字符串改成了对象）。那时露出 key 本身比抛异常好：一个页面上出现
 * `home.docketTitle` 这样的英文串是**刺眼且可搜索的**，而抛异常会把整棵子树炸掉。
 */
export function t(key: MessageKey): string {
  let node: unknown = messages;
  for (const segment of key.split(".")) {
    if (typeof node !== "object" || node === null) {
      return key;
    }
    node = (node as Record<string, unknown>)[segment];
  }
  return typeof node === "string" ? node : key;
}

/**
 * 一条错误/归因的四件套。`params` 是**参数名**的列表，不是参数值 ——
 * 值由后端放在 HTTP 响应的 `params` 里，标签由 `paramLabel()` 给。
 *
 * `title` / `detail` / `action` 里**没有占位符**，这是刻意的（README 约定 4）。
 */
export interface ErrorCopy {
  readonly title: string;
  readonly detail: string;
  readonly action: string;
  /** 要以键值行展示的参数名，按这里的顺序渲染。没有参数的错误是空数组。 */
  readonly params: readonly string[];
}

interface RawCopy {
  readonly title: string;
  readonly detail: string;
  readonly action: string;
  readonly params?: readonly string[];
}

// 两棵树刻意分开，镜像 backend/app/errors.py 的分法。理由见 messages/README.md。
const httpErrors: Readonly<Record<string, RawCopy>> = messages.errors;
const scanFailures: Readonly<Record<string, RawCopy>> = messages.scanFailures;
const paramLabels: Readonly<Record<string, string>> = messages.paramLabels;

function normalize(raw: RawCopy | undefined): ErrorCopy | null {
  if (raw === undefined) {
    return null;
  }
  return { title: raw.title, detail: raw.detail, action: raw.action, params: raw.params ?? [] };
}

/**
 * HTTP 错误码 → 文案。**未登记的码返回 `null`**，由调用方决定怎么兜底。
 *
 * 刻意不在这里兜底成 `internal_error` 的文案：那会把"后端加了新码但前端没跟"
 * 伪装成"后端崩了"，而这两件事的处理方式完全不同。守门的是
 * `backend/tests/test_message_coverage.py`（双向集合比对）。
 */
export function errorCopy(code: string): ErrorCopy | null {
  return normalize(httpErrors[code]);
}

/** 扫描归因码 → 文案。语义见 messages/README.md（不是 HTTP 错误）。 */
export function scanFailureCopy(code: string): ErrorCopy | null {
  return normalize(scanFailures[code]);
}

/**
 * 参数名 → 中文标签。
 *
 * 缺标签时返回**参数名原文**：它是 snake_case 的拉丁串，在一片中文里非常刺眼，
 * 等于自带一个"这里少了一条文案"的提示。返回空串会让那一行看起来只是没有值。
 */
export function paramLabel(name: string): string {
  return paramLabels[name] ?? name;
}
