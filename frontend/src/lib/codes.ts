/**
 * 机器码的联合类型，**从 `messages/zh-CN.json` 派生**。
 *
 * 为什么从文案表派生，而不是在这里手写一份联合类型：手写就是第三份真相
 * （`app/errors.py` 一份、文案表一份、这里一份），而三份里必然有一份先漂移。
 * 文案表与 `errors.py` 的一致性由 `backend/tests/test_message_coverage.py`
 * 双向比对守住，于是这里派生出来的类型也就跟着后端走。
 *
 * 前端**按码分支，绝不匹配文案**（CLAUDE.md §错误与文案）。
 */

import messages from "../../messages/zh-CN.json";

/** `ConsoleError` 子类的码 —— HTTP 错误，请求本身失败了。 */
export type ErrorCode = keyof typeof messages.errors;

/** `SCAN_FAILURE_CODES` —— 扫描归因，请求成功了、那次扫描失败了。 */
export type ScanFailureCode = keyof typeof messages.scanFailures;

export const ERROR_CODES: readonly string[] = Object.keys(messages.errors);
export const SCAN_FAILURE_CODES: readonly string[] = Object.keys(messages.scanFailures);

/**
 * 收窄一个来自 HTTP 响应的字符串。
 *
 * 后端返回的 `code` 是 `string`，不是我们的联合类型 —— 版本不一致时它**可能**是一个
 * 我们不认识的码。这个函数让"不认识"变成一条显式分支，而不是一次悄悄的类型断言。
 */
export function isErrorCode(code: string): code is ErrorCode {
  return Object.prototype.hasOwnProperty.call(messages.errors, code);
}

export function isScanFailureCode(code: string): code is ScanFailureCode {
  return Object.prototype.hasOwnProperty.call(messages.scanFailures, code);
}
