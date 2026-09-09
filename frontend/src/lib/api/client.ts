/**
 * 浏览器侧访问后端的**唯一出口**。
 *
 * =============================================================================
 * 一、为什么服务端**彻底不碰** cookie（2026-09-09 拍板，方案 B）
 *
 * 本项目**没有** `lib/api/server.ts`，也**没有任何文件** import `next/headers`
 * （eslint `no-restricted-imports` 封死了它，不靠人记）。所有需要鉴权的数据一律
 * 由客户端经本文件取；服务端组件只出静态骨架。
 *
 * 两条理由，以后一定有人想改成服务端渲染，所以写在代码里：
 *
 *   1. `web` 容器现在持有**零个凭据**。这是一个能一眼验证、也能一直守住的**结构性**
 *      性质，符合本项目「不变量写进代码，不是写进文档」。转发 cookie 会把会话 id
 *      引进 Next.js 进程，从此前端多一份"不许记日志"的责任 —— 而 fetch 失败时栈里
 *      带着请求头是很容易发生的事。
 *   2. **方向不对称。** 现在→服务端渲染，以后想加只要新建一个文件；反过来想退，
 *      得重写页面。先选可以廉价反悔的那一边。
 *
 * 代价已知并接受：`/scans/[id]` 的授权印记条会有一次可见加载态。
 * 那个加载态由 `components/ui/StampBar` 负责做到**零布局位移**。
 *
 * =============================================================================
 * 二、为什么 401 拦截在这里，而**不在** middleware
 *
 * middleware 跑在 Edge runtime 上，看不到响应体里的机器码 —— 而我们要区分的正是
 * 「HTTP 401」与「响应体 `{code:"unauthenticated"}`」这两件事同时成立的情形。
 * middleware 只适合"没 cookie 就重定向"，那是 T5b 的活。
 *
 * **WebSocket 握手失败复用同一段解析。** 已实测（`app/routes/auth.py` 的
 * `UnauthenticatedError` docstring）：全局依赖在 WS 握手上抛出时，握手返回的是
 * **真正的 401** + `content-type: application/json` + `{"code":"unauthenticated"}`
 * （ASGI WebSocket Denial Response 扩展），不是一个没有正文的 403。
 * 所以 T13/T17 建 `lib/ws/*` 时，握手失败可以直接调用本文件的
 * `notifyUnauthenticated()`，不需要第二套判定。本轮刻意不建 `lib/ws/*`。
 *
 * =============================================================================
 * 三、刻意不做的事
 *
 * · **不引 zod。** 后端是校验的唯一权威（Pydantic v2 边界模型），前端再校一遍就是
 *   两份真相，且必然漂移。`as T` 那一处就是我们信任后端形状的地方，只有一处，
 *   而且写在注释里，不装作有校验。
 * · **不配任何跨域代理。** 同源是这个项目挡住"任意网页打我们 API"的支柱之一
 *   （CLAUDE.md §安全不变式：永远不许装 CORSMiddleware）。为"本地开发方便"开一个
 *   代理等于把那条不变式作废。前端与后端在同一个 origin 下由 nginx 反代拼起来。
 * · **前端一行都不许碰会话 cookie。** 不读、不写、不存 sessionStorage、不放 URL。
 *   它是 `HttpOnly` 的，`credentials: "same-origin"` 让浏览器自己带上它 ——
 *   我们连它叫什么名字都不需要知道（与 `vault_handle` 刻意相反）。
 */

/** 后端 `ConsoleError.to_payload()` 的 `params` 值域，与 `errors.py` 的 `ParamValue` 一致。 */
export type ApiParamValue = string | number | boolean | null;

/**
 * 后端返回的错误。三个字段就是全部对外契约（`{code, trace_id, params}`），
 * 刻意不多解析一个字段 —— 多解析的那个迟早会被人当成契约用。
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly traceId: string;
  readonly params: Readonly<Record<string, ApiParamValue>>;

  constructor(
    status: number,
    code: string,
    traceId: string,
    params: Readonly<Record<string, ApiParamValue>>,
  ) {
    // `message` 只放机器码，**不放中文**：中文在文案表里，而这个 message 会进
    // 控制台与 react-query 的错误对象。两处都不该成为第二份文案来源。
    super(code);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.traceId = traceId;
    this.params = params;
  }
}

/** 网络层就没走通（后端没起来、nginx 502、被浏览器掐断）。与 `ApiError` 刻意分开。 */
export class NetworkError extends Error {
  constructor(cause: unknown) {
    super("network_unreachable");
    this.name = "NetworkError";
    this.cause = cause;
  }
}

// =============================================================================
// 会话失效的唯一通报点
//
// 模块级的一个可变变量。CLAUDE.md 禁止"模块级可变全局状态"，这里是**刻意的例外**，
// 理由：浏览器里"当前会话"本来就是一个进程级单例，而回调**只在 providers.tsx 注册
// 一次**。把它挂到某个对象上再层层传下去，只会让每个调用点都要多一个参数，
// 而那个参数永远是同一个值。
//
// 边界很窄：只有一个 setter，只有本文件调用它，且注册方是唯一的那个 Provider。
// =============================================================================
type UnauthenticatedHandler = () => void;

let unauthenticatedHandler: UnauthenticatedHandler | null = null;

/** 由 `app/providers.tsx` 注册。**只有那一个挂载点**。 */
export function setUnauthenticatedHandler(handler: UnauthenticatedHandler | null): void {
  unauthenticatedHandler = handler;
}

/** 供 WS 握手失败时复用（T13/T17）。见文件顶部第二节。 */
export function notifyUnauthenticated(): void {
  unauthenticatedHandler?.();
}

function isUnauthenticated(status: number, code: string): boolean {
  // 两个条件是 **或**，不是且：
  //   · 只看 status：将来若有别的东西（反代、防火墙）也回 401，会误报会话失效；
  //     所以拿到机器码时以机器码为准。
  //   · 只看机器码：响应体解析失败（nginx 直接回的 502 HTML）时就漏了。
  return status === 401 || code === "unauthenticated";
}

/** 未能从响应体里解析出契约形状时用的兜底码。 */
const UNPARSEABLE = "internal_error";

async function readErrorBody(
  response: Response,
): Promise<{ code: string; traceId: string; params: Record<string, ApiParamValue> }> {
  let parsed: unknown = null;
  try {
    parsed = await response.json();
  } catch {
    // 不是 JSON。最常见的来源是 nginx 自己生成的 502/504 页面 —— 那时后端根本没被
    // 碰到。归到 `internal_error`，前端文案说的正是"后端出错了"。
    return { code: UNPARSEABLE, traceId: "-", params: {} };
  }
  if (typeof parsed !== "object" || parsed === null) {
    return { code: UNPARSEABLE, traceId: "-", params: {} };
  }
  const body = parsed as Record<string, unknown>;
  const code = typeof body.code === "string" ? body.code : UNPARSEABLE;
  const traceId = typeof body.trace_id === "string" ? body.trace_id : "-";
  const params =
    typeof body.params === "object" && body.params !== null
      ? (body.params as Record<string, ApiParamValue>)
      : {};
  return { code, traceId, params };
}

interface RequestOptions {
  /** 有 body 就是 POST（并自动带 `Content-Type: application/json`），没有就是 GET。 */
  readonly method?: "GET" | "POST" | "DELETE";
  /** 已经是普通对象，不做任何校验 —— 校验是后端的事。 */
  readonly body?: unknown;
  readonly signal?: AbortSignal;
}

/**
 * 发一次请求。成功返回解析后的 JSON，失败抛 `ApiError` / `NetworkError`。
 *
 * `path` 必须是站内绝对路径（`/api/...`）。不接受完整 URL —— 那会让"同源"这条
 * 不变式从结构性事实退化成一句约定。
 */
export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  if (!path.startsWith("/")) {
    throw new Error(`apiFetch 只接受站内绝对路径，收到 ${path}`);
  }

  const method = options.method ?? (options.body === undefined ? "GET" : "POST");
  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.body !== undefined) {
    // ⚠️ 这个 header 是安全机制的一部分，不是礼貌：FastAPI 只把
    // `application/json` 的 body 喂给 Pydantic，另三种 CORS 安全名单类型全 422
    // （已实测）。这正是"没有 CORS"能挡住跨域打我们 API 的一半原因。
    headers["Content-Type"] = "application/json";
  }

  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers,
      // `same-origin` 而不是 `include`：我们只发同源请求，`include` 会让"哪天有人
      // 写了个跨域 URL"变成一次带凭据的跨域请求。
      credentials: "same-origin",
      // 会话与凭据相关的响应一律不许进 HTTP 缓存。
      cache: "no-store",
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      ...(options.signal === undefined ? {} : { signal: options.signal }),
    });
  } catch (cause) {
    throw new NetworkError(cause);
  }

  if (!response.ok) {
    const { code, traceId, params } = await readErrorBody(response);
    if (isUnauthenticated(response.status, code)) {
      notifyUnauthenticated();
    }
    throw new ApiError(response.status, code, traceId, params);
  }

  // 这里是整个前端**唯一**信任后端形状的地方。见文件顶部第三节（不引 zod 的理由）。
  return (await response.json()) as T;
}

// =============================================================================
// 后端在 T5 阶段**真实存在**的端点，就这四个。
// `/api/scans`、`/api/keys`、`/ws/*` 全部是 404，要等 T6/T7/T9/T13。
// 不给不存在的接口写包装函数 —— 那会让下游以为它们能用。
// =============================================================================

/** `GET /api/health`。免鉴权（nginx 与 compose 的 healthcheck 要打它）。 */
export interface HealthResponse {
  readonly status: string;
  readonly app_version: string;
  readonly strix_version: string;
}

export function fetchHealth(signal?: AbortSignal): Promise<HealthResponse> {
  return apiFetch<HealthResponse>("/api/health", signal === undefined ? {} : { signal });
}

/**
 * `GET /api/auth/me`。**永远 200**，从不 401 —— 未登录时在响应体里说话。
 * 后端刻意这么设计：401 会让前端无法区分"我没登录"和"服务被别的东西拦了"。
 */
export interface SessionState {
  readonly authenticated: boolean;
  readonly username: string | null;
}

export function fetchSession(signal?: AbortSignal): Promise<SessionState> {
  return apiFetch<SessionState>("/api/auth/me", signal === undefined ? {} : { signal });
}
