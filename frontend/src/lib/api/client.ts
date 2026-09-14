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
 *
 * **T5b 拍板：本项目不加 middleware，一个都不加**（2026-09-10）。曾经的设想是
 * "没 cookie 就重定向"，它被否掉的理由有三条，最硬的是第一条：
 *   1. middleware 只能判断 cookie **在不在**，而这里最常见的失效原因是"api 重启了"
 *      —— 那时 cookie 一直都在（它刻意没有 `max_age`，见 `routes/auth.py`），
 *      middleware 会一路放行。花掉一条硬不变式，换来的几乎是零。
 *   2. 读 cookie 要求 Next 进程知道它叫什么名字，而下面第三节的原话是"我们连它叫
 *      什么名字都不需要知道"。那句话是"零个凭据"这条可一眼验证的性质的一半。
 *   3. eslint 的 `no-restricted-imports` 只封了 `next/headers`；middleware 读 cookie
 *      走的是 `next/server` 的 `NextRequest.cookies` —— **lint 拦不住**。
 *      也就是说那条边界会退化成一句要靠人记的话。
 * 未登录跳转因此放在客户端：`components/auth/RequireSession.tsx` 打 `/api/auth/me`
 * （永远 200），拿到 `authenticated:false` 才 `router.replace("/login")`。
 * 代价是未登录时会先闪一下首页骨架 —— 那个骨架是纯静态的，零用户数据。
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

/**
 * 这个响应是不是"你没有身份"。
 *
 * 两个条件是 **或**，不是且：
 *   · 只看 status：将来若有别的东西（反代、防火墙）也回 401，会误报会话失效；
 *     所以拿到机器码时以机器码为准。
 *   · 只看机器码：响应体解析失败（nginx 直接回的 502 HTML）时就漏了。
 *
 * ⚠️ 它回答的是"**这个响应是什么**"，**不是**"要不要弹会话失效遮罩"。两者不同：
 * `POST /api/auth/login` 打错口令也是 401（`InvalidCredentialsError`），
 * 但那不意味着会话失效 —— 那是一次登录尝试失败。要不要通报是**策略**，
 * 属于调用点，见下面 `isAuthAttempt`。所以本函数刻意**不认识**任何机器码黑名单：
 * 那种名单一定会长，而"我是一次登录尝试"这个知识只有调用方有。
 */
function isUnauthenticated(status: number, code: string): boolean {
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
  /**
   * 这个请求**本身就是在建立身份**。它拿到 401 只可能意味着"这一次尝试失败了"，
   * 不可能意味着"会话失效了"，所以不调 `notifyUnauthenticated()`。
   *
   * **全站只有 `login()` 传它。别扩散。** 类型写 `?: true` 而不是 `?: boolean`：
   * 不给"传一个变量进来"留口子 —— 那样这个开关就会有一天由运行期数据决定。
   */
  readonly isAuthAttempt?: true;
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
    if (isUnauthenticated(response.status, code) && options.isAuthAttempt !== true) {
      notifyUnauthenticated();
    }
    throw new ApiError(response.status, code, traceId, params);
  }

  // 这里是整个前端**唯一**信任后端形状的地方。见文件顶部第三节（不引 zod 的理由）。
  return (await response.json()) as T;
}

// =============================================================================
// 只包**后端真实存在、且前端此刻真的在用**的端点。不给不存在的接口写包装函数 ——
// 那会让下游以为它们能用；也不为已经存在但还没有调用点的接口先写一个占位包装。
// 现在是五个：四个鉴权/健康 + `/api/system/status`（T3 收尾时首页要用）。
// `/api/keys`、`/api/targets`、`/api/allowlist` 后端已经有了，包装留给它们的第一个
// 调用点（T18 的向导）；`/api/scans`、`/ws/*` 仍然是 404，要等 T9/T13。
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

/**
 * `POST /api/auth/login`。成功时 cookie 由响应的 `Set-Cookie` 落到浏览器 ——
 * **这个函数不接触、也无法接触那个 cookie**（它是 `HttpOnly` 的）。
 *
 * ⚠️ 本函数里**不许有任何日志、埋点、错误上报**。口令只以函数参数的形态存在于
 * 一次调用里，`JSON.stringify` 之后就交给 fetch 了。`console.log(arguments)` 这类
 * 顺手一行会把它写进浏览器控制台 —— 那是一个可被截图、可被扩展读取的地方。
 *
 * `isAuthAttempt: true` 是本项目**唯一**用到它的地方：这里的 401 是
 * `invalid_credentials`（口令错），不是会话失效，不该弹全屏遮罩 ——
 * 一个盖住登录表单的"请重新登录"遮罩是纯粹的自相矛盾。
 *
 * 失败抛 `ApiError`：`invalid_credentials`(401) / `auth_locked`(429) /
 * `invalid_request`(422)。调用方按码分支，见 `components/auth/LoginForm.tsx`。
 */
export function login(username: string, password: string): Promise<SessionState> {
  return apiFetch<SessionState>("/api/auth/login", {
    body: { username, password },
    isAuthAttempt: true,
  });
}

/**
 * `POST /api/auth/logout`。后端**幂等**、永远 200（在 `EXEMPT_PATHS` 里），
 * 所以这里只有网络层能失败。
 *
 * 顺序纪律（与 `stores/keys.ts` 同源）：**先请后端清，再清本地**。cookie 只能由
 * 后端的响应删除，本地先清再宣布"你已登出"是一句谎话 —— 服务端会话还活着。
 * 所以调用方必须在**成功之后**才 `queryClient.clear()` 并跳转。
 */
export function logout(): Promise<{ readonly ok: boolean }> {
  return apiFetch<{ readonly ok: boolean }>("/api/auth/logout", { method: "POST" });
}

/**
 * `GET /api/system/status`。**永远 200**（诊断结果在正文的 `blockers` 里）。
 *
 * 下面这个类型是后端 `SystemStatusResponse` 的**真子集** —— 那个模型有九节四十来个
 * 字段（数据目录绝对路径、孤儿容器名、证书 SAN、探测耗时…），首页侧栏五行只用得上
 * 这五个布尔。只声明用得到的字段是本文件第三节那条"不多解析一个字段"的直接后果：
 * 多声明的字段会被下游当成契约，而 T26 的诊断页才是那些字段真正的消费者。
 *
 * `bool | None` 的 `None` 在这里是 `null`，含义是"**我没能确认**"，不是"通过" ——
 * 后端 `services/system_status.py` 那三条总则的第一条。渲染成绿点就是编造。
 *
 * ⚠️ 它**在全局鉴权之后**（`routes/system.py` 第 1 条），所以未登录时会 401。
 * 这是全站第一个需要身份的接口 —— 调用方必须先确认已登录，理由见
 * `components/system/ReadyRows.tsx`。
 */
export interface SystemStatusSummary {
  readonly docker: { readonly reachable: boolean };
  readonly network: {
    readonly present: boolean | null;
    readonly api_attached: boolean | null;
  };
  readonly data_dir: { readonly identical_path_ok: boolean | null };
  readonly sandbox_image: { readonly present: boolean | null };
  readonly telemetry: { readonly strix_telemetry: boolean };
}

export function fetchSystemStatus(signal?: AbortSignal): Promise<SystemStatusSummary> {
  return apiFetch<SystemStatusSummary>(
    "/api/system/status",
    signal === undefined ? {} : { signal },
  );
}
