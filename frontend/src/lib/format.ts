/**
 * 金额 / 时间 / 时长 / 字节的**唯一出口**。
 *
 * 为什么必须收口在一个文件：这四类值一旦允许在组件里各自 `toFixed(2)`，就会出现
 * "同一个金额在两个面板里位数不同"这种事，而它极难在 review 里被发现。
 * 收口之后，"钱显示几位"这类问题永远只有一个答案。
 *
 * 文案表里**不许**出现金额、时间、时长、字节（messages/README.md 约定 6）。
 * 反过来，这里也**不许**出现中文字面量 —— `units.*` 三个词从文案表读。
 * SI 符号（`MB` / `GB` / `$`）不是文案，留在本文件里。
 *
 * ⚠️ 本文件**只在浏览器侧使用**。理由是时区：`formatTimestamp` 渲染的是**本机时区**
 * 的时间，而服务端渲染会用容器的时区（UTC）—— 两边不一致就是一次 hydration 不匹配。
 * 按 T5 的裁决 ②，需要鉴权的数据全部客户端取，所以这个约束天然成立。
 *
 * 刻意**不引** date-fns / dayjs：`Intl` 与 `Date` 已经够，一个日期库是纯负债
 * （CLAUDE.md §编码哲学 4）。
 */

import { t } from "./messages";

/**
 * 金额。**永远两位小数**，永远带 `$`。
 *
 * locale 写死 `en-US` 而不是 `zh-CN`：`zh-CN` 会把 USD 渲染成 `US$8.42`，
 * 那个 `US` 是给"可能有多种货币"的界面做消歧的，而 Strix 的计费只有美元一种，
 * 多出来的两个字母只是噪音。设计稿里也是 `$8.42`。
 */
const usd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

export function formatUsd(value: number): string {
  if (!Number.isFinite(value)) {
    return t("common.unknown");
  }
  return usd.format(value);
}

function pad2(value: number): string {
  return value < 10 ? `0${value}` : String(value);
}

/**
 * 后端给的 UTC ISO 串 → **本机时区**的 `2026-09-09 14:02`。
 *
 * 刻意手写而不用 `Intl.DateTimeFormat`：`zh-CN` 的 dateStyle 会给出 `2026/09/09`，
 * 而设计里用的是短横线（与工单编号、审计文件名同一种形状）。用 `formatToParts`
 * 再拼回来的代码比这几行更长也更绕。
 *
 * ⚠️ **绝不给输出缀 `Z`。** 这是本机时区的墙上时间，缀 `Z` 就是说谎
 * （`pitfalls` 条 37 是同一个错误在后端 Formatter 上的版本）。要显示"这是哪个时区"
 * 由界面自己说，不由这个函数偷偷加后缀。
 */
export function formatTimestamp(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  const day = `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
  return `${day} ${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

/** 事件流左侧那一列的 `14:12:07`（同样是本机时区，同样不缀 `Z`）。 */
export function formatClock(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return `${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`;
}

/**
 * 时长。`42 秒` / `4 分钟` / `1 小时 20 分钟`。
 *
 * 数字与中文单位之间有一个**半角空格**（messages/README.md 约定 5）。
 * 这里拼接字符串是允许的 —— "禁止拼接"那条约束的是**组件里的句子**，
 * 而单位拼装正是本文件存在的理由：它是那件事唯一被允许发生的地方。
 *
 * 不显示秒的小数、不显示"天"：一次扫描的量级是分钟到小时，
 * 出现"天"说明有别的东西坏了，那时该看的是状态而不是时长。
 */
export function formatDuration(milliseconds: number): string {
  if (!Number.isFinite(milliseconds) || milliseconds < 0) {
    return t("common.unknown");
  }
  const totalSeconds = Math.floor(milliseconds / 1000);
  if (totalSeconds < 60) {
    return `${totalSeconds} ${t("units.second")}`;
  }
  const totalMinutes = Math.floor(totalSeconds / 60);
  if (totalMinutes < 60) {
    return `${totalMinutes} ${t("units.minute")}`;
  }
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (minutes === 0) {
    return `${hours} ${t("units.hour")}`;
  }
  return `${hours} ${t("units.hour")} ${minutes} ${t("units.minute")}`;
}

// 十进制（1000）而不是二进制（1024）：这些数字要跟 `docker images` / 磁盘可用量对读，
// 而那些工具报的都是十进制。混用两套会让"10 GB 可用"和界面显示的数对不上。
const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB"] as const;

/** 字节。`842 B` / `1.5 MB` / `12.3 GB`。 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) {
    return t("common.unknown");
  }
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1000 && unitIndex < BYTE_UNITS.length - 1) {
    value /= 1000;
    unitIndex += 1;
  }
  const unit = BYTE_UNITS[unitIndex] ?? "B";
  // 字节整数不带小数点，其余保留一位：`842 B` 比 `842.0 B` 干净。
  const shown = unitIndex === 0 ? String(Math.round(value)) : value.toFixed(1);
  return `${shown} ${unit}`;
}
