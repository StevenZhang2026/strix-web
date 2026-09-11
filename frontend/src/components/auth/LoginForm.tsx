"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { ErrorNotice } from "@/components/errors/ErrorNotice";
import { Button } from "@/components/ui/Button";
import { ApiError, login, type ApiParamValue } from "@/lib/api/client";
import { formatDuration } from "@/lib/format";
import { t } from "@/lib/messages";
import { useSessionStore } from "@/lib/stores/session";

import styles from "./LoginForm.module.css";

/**
 * 登录表单。**客户端叶子。全站唯一持有口令的地方。**
 *
 * =============================================================================
 * 一、口令输入框用**标准** `name` + `autoComplete`，这与 API Key 输入框**相反**
 *
 * `CLAUDE.md` §TypeScript 那条「Key 输入框 `type=password` + 随机 `name`（破自动
 * 填充）」讲的是 `POST /api/keys` 的**模型凭据**输入框，**那一条不变** —— T7 做凭据
 * 表单时仍然用随机 `name`。两者的规则是反的，**不要互相"对齐"**。
 * 决定、日期与完整论证记在 `PLAN.md` 的 T5b 那一行（含一条残余风险：浏览器若开了
 * 云端同步，这个口令会离开本机，而 `autoComplete` 没有"可填充但不要同步"这种值）。
 *
 * =============================================================================
 * 二、为什么不用 `useMutation`
 *
 * 方案里原本写的是 `useMutation`，**落地时改掉了**：react-query 会把 `variables`
 * 留在 mutation 上（`mutation.variables`，随 mutation cache 存活，默认 gcTime 5 分钟，
 * 只有 `reset()` 才清）。那意味着明文口令在登录成功之后还在内存里的一个缓存对象里躺
 * 五分钟 —— 正好抵掉"提交后在 `finally` 里清空 state"这条规则。
 * 换成两个普通 `useState` 之后，口令只存在于：这个组件的 state（提交后立即清空）、
 * 一次 `login()` 调用的栈帧、以及 `JSON.stringify` 出来的那个请求体。
 * 代价是自己管 `pending` / `error` 两个状态，六行，值得。
 *
 * ⚠️ 如实记账：清空 React state **不等于**把口令从 JS 堆上擦掉（字符串不可变，
 * 旧的那份要等 GC）。这条规则挡的是"它还挂在一个活引用上"，不是内存取证。
 *
 * =============================================================================
 * 三、刻意**没有**任何口令校验
 *
 * 没有 `minLength`、没有强度提示、连 `required` 都没有。理由在后端
 * `routes/auth.py` 的 `LoginRequest`：口令下限只在**设置**口令时校验，登录接口上加
 * 下限会让"太短的口令"拿到 422 而不是 401，等于免费告诉攻击者"这个口令没进到比对
 * 环节"。前端加一条 `minLength` 就是在客户端**重现**后端刻意避免的那个信息泄漏。
 * 连 `required` 都不加，是为了让"这个表单里没有一行口令校验"成为一个 grep 就能确认
 * 的事实，而不是一句"我们只加了无害的那种"。空口令会真的发出去，拿一个诚实的 401。
 *
 * 用户名上有 `required` + `maxLength`（对齐后端 `MAX_USERNAME_LENGTH`）：用户名不是
 * 秘密，拦住空值和 10 MB 请求体只是省一次无意义的往返。**这是唯一的客户端校验。**
 */
export function LoginForm() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const clearExpired = useSessionStore((state) => state.clearExpired);

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<Error | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (pending) {
      return;
    }
    setPending(true);
    setFailure(null);
    try {
      const state = await login(username, password);
      // 顺序有意义：先把**权威的**新登录态写进缓存（`login` 的响应体就是它），
      // 再清"会话已失效"，最后才跳转。反过来（先跳转）会让 `(app)` 挂载时读到
      // 缓存里那个 `authenticated:false`，被 `RequireSession` 立刻踢回登录页。
      queryClient.setQueryData(["session"], state);
      clearExpired();
      // 全量失效而不是只失效 `["session"]`：它给出的性质是"登录后你看到的每一条数据
      // 都是在新会话下取的"，而这条性质在 T6/T13 加进来一堆 query 之后自动继续成立。
      // 不 `clear()`：那会连"刚写进去的登录态"一起删掉。
      void queryClient.invalidateQueries();
      router.replace("/");
    } catch (cause) {
      // 这里**不许**有任何日志、埋点、错误上报 —— `password` 还在作用域里，
      // 任何"顺手把上下文一起记下来"的写法都会把它带走。只留一个错误对象给渲染。
      setFailure(cause instanceof Error ? cause : new Error("login_failed"));
    } finally {
      // 无论成败都清。代价：网络抖一下也要重新输一遍口令 —— 接受，
      // 这正是"口令不在内存里多待一秒"的价钱。
      setPassword("");
      setPending(false);
    }
  }

  const notice = failure === null ? null : noticeProps(failure);

  return (
    <form onSubmit={handleSubmit}>
      <div className={styles.grid}>
        <label className={styles.label} htmlFor="login-username">
          {t("login.username")}
        </label>
        <div className={styles.value}>
          <input
            className={styles.input}
            id="login-username"
            name="username"
            type="text"
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            required
            maxLength={64}
            autoFocus
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            disabled={pending}
          />
        </div>

        <label className={styles.label} htmlFor="login-password">
          {t("login.password")}
        </label>
        <div className={styles.value}>
          <input
            className={styles.input}
            id="login-password"
            name="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            disabled={pending}
          />
        </div>
      </div>

      <div className={styles.actions}>
        <Button type="submit" disabled={pending}>
          {pending ? t("login.submitting") : t("login.submit")}
        </Button>
      </div>

      {/* 错误块的位置固定在按钮下方**表单之内**：出错时按钮不移动，要重试的东西
          也不会被盖住（一个盖住表单的弹窗正是本轮要修掉的那个毛病）。 */}
      {notice === null ? null : (
        <div className={styles.failure}>
          <ErrorNotice {...notice} />
        </div>
      )}
    </form>
  );
}

/** `ErrorNotice` 的入参。与那个组件的 props 保持同形，不另立一套。 */
interface NoticeProps {
  readonly code: string;
  readonly traceId?: string;
  readonly params?: Readonly<Record<string, ApiParamValue>>;
}

/**
 * 一个失败对象 → 展示用的三件事。**纯函数，好测。**
 *
 * 三条判据：
 *   · `invalid_credentials`(401) / `auth_locked`(429) —— **刻意不显示问题编号**。
 *     `common.traceIdHint` 说的是"反馈问题时带上这个编号"，而打错口令不是问题，
 *     给它一个编号是在暗示用户去反馈一个 bug。
 *   · `auth_locked` 的 `retry_after` 后端给的是**秒数**，而 `ErrorNotice` 只做
 *     `String(value)` —— 直接交过去会显示成「解锁还需 300」。所以在这里过一遍
 *     `formatDuration`（金额/时间/时长的唯一出口），得到「解锁还需 5 分钟」。
 *     这样 `ErrorNotice`、文案表、`paramLabels` 一个都不用改。
 *   · 其它（`NetworkError`、渲染期 bug）—— 归 `internal_error`，沿用
 *     `app/error.tsx` 已有的判据，不另立一套映射。
 */
function noticeProps(failure: Error): NoticeProps {
  if (!(failure instanceof ApiError)) {
    return { code: "internal_error" };
  }
  if (failure.code === "invalid_credentials") {
    return { code: failure.code };
  }
  if (failure.code === "auth_locked") {
    const seconds = failure.params.retry_after;
    return {
      code: failure.code,
      params:
        typeof seconds === "number"
          ? { retry_after: formatDuration(seconds * 1000) }
          : failure.params,
    };
  }
  return { code: failure.code, traceId: failure.traceId, params: failure.params };
}
