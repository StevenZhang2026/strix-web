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
 * `notifyUnauthenticated()`，不需要第二套判定。但浏览器的 `WebSocket` **看不到**
 * 握手状态码（只看到一次没 open 过的 close），所以 `lib/ws/scanStream.ts` 先问一次
 * `/api/auth/me` 再决定要不要调它。
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

  // `204 No Content` 没有正文，`response.json()` 会抛 `SyntaxError`。
  // 第一个 204 是 `DELETE /api/keys/{handle}`，调用方把 `T` 写成 `void`。
  if (response.status === 204) {
    return undefined as T;
  }

  // 这里是整个前端**唯一**信任后端形状的地方。见文件顶部第三节（不引 zod 的理由）。
  return (await response.json()) as T;
}

// =============================================================================
// 只包**后端真实存在、且前端此刻真的在用**的端点。不给不存在的接口写包装函数 ——
// 那会让下游以为它们能用；也不为已经存在但还没有调用点的接口先写一个占位包装。
// 现在是十五个：四个鉴权/健康 + `/api/system/status`（T3 收尾时首页要用）
// + 凭据那四个（`/api/providers` 与 `/api/keys` 的增删查，T7c 的凭据表单在用）
// + `/api/targets/validate` 与 `/api/scan-templates`（向导第 1／4 步在用）
// + `/api/allowlist`（T18b 第 2 步用它把命中的清单条目的授权编号摆出来）
// + `POST /api/scans`（T18b 的提交）
// + `GET /api/scans/{id}` 与 `POST /api/scans/{id}/stop`（`/scans/{id}` 实时面板在用）。
// `/ws/scans/{id}` 已上线，但它不是 HTTP 请求，不在本文件：由 `lib/ws/scanStream.ts` 负责。
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
  /**
   * 后端 `compute_blockers` 的输出，**已经排好序、已经抑制过派生项**。
   * 侧栏五行下方的修复指引照这个顺序逐条渲染，前端不再判定
   * （`components/system/ReadyRows.tsx` 第四节）。码表在
   * `services/system_status.py` 的 `ALL_BLOCKER_CODES`。
   */
  readonly blockers: readonly string[];
}

export function fetchSystemStatus(signal?: AbortSignal): Promise<SystemStatusSummary> {
  return apiFetch<SystemStatusSummary>(
    "/api/system/status",
    signal === undefined ? {} : { signal },
  );
}

/**
 * `GET /api/providers`。供应商 × 凭据形状的目录，进程内常量、无 IO、恒 200。
 *
 * `secret_keys` 与 `param_keys` **分开**给：前者渲染成 `type=password`（凭据），
 * 后者渲染成普通文本框（区域）。合成一个列表就没法区分了
 * （`backend/app/routes/providers.py` 的原话）。
 */
export interface ShapeView {
  readonly auth_shape: string;
  readonly secret_keys: readonly string[];
  readonly param_keys: readonly string[];
}

export interface ProviderView {
  readonly provider: string;
  readonly shapes: readonly ShapeView[];
  /** 实测过的模型名。可能是空的 —— 那时前端只提示"照它的文档填"。 */
  readonly models: readonly string[];
  readonly api_base_allowed: boolean;
}

export interface ProvidersResponse {
  readonly providers: readonly ProviderView[];
}

export function fetchProviders(signal?: AbortSignal): Promise<ProvidersResponse> {
  return apiFetch<ProvidersResponse>("/api/providers", signal === undefined ? {} : { signal });
}

/**
 * `POST /api/keys` 的入参。**这是全前端唯一一处明文密钥出现在数据结构里的地方**，
 * 而且只在一次 `await` 的生命周期内：调用方在 `finally` 里清掉自己的 state，
 * 这个对象随之不可达。刻意**不给它加 react-query 的 mutation 包装** ——
 * 那会把带密文的 variables 交给 query client 持有一段不确定的时间。
 */
export interface RegisterKeyRequest {
  readonly provider: string;
  readonly auth_shape: string;
  /** 模型名。后端按形状补前缀。 */
  readonly strix_llm: string;
  /** 留空就发 `null`，不发空串。 */
  readonly api_base: string | null;
  /** 键必须恰好是所选形状的 `secret_keys`。 */
  readonly secrets: Readonly<Record<string, string>>;
  /** 键必须恰好是所选形状的 `param_keys`。 */
  readonly params: Readonly<Record<string, string>>;
  /**
   * 类型写死 `true`：不验活的 handle 只会把"凭据填错了"推迟到扫描启动那一刻，
   * 那时用户已经签过授权工单了。不给这件事留一个开关。
   */
  readonly verify: true;
}

/** `201`。**没有任何字段能推回明文** —— `labels` 是后端算好的掩码。 */
export interface KeyRegisteredResponse {
  readonly vault_handle: string;
  readonly labels: Readonly<Record<string, string>>;
  readonly verified: boolean;
  readonly verify_latency_ms: number | null;
}

export function registerKey(request: RegisterKeyRequest): Promise<KeyRegisteredResponse> {
  return apiFetch<KeyRegisteredResponse>("/api/keys", { body: request });
}

/** `GET /api/keys/{handle}`。刷新页面后用它恢复"当前用的是哪个凭据"。 */
export interface KeyStateResponse {
  readonly provider: string;
  readonly auth_shape: string;
  readonly strix_llm: string;
  readonly api_base: string | null;
  readonly labels: Readonly<Record<string, string>>;
  readonly params: Readonly<Record<string, string>>;
}

export function fetchKeyState(handle: string, signal?: AbortSignal): Promise<KeyStateResponse> {
  return apiFetch<KeyStateResponse>(
    `/api/keys/${encodeURIComponent(handle)}`,
    signal === undefined ? {} : { signal },
  );
}

/**
 * `DELETE /api/keys/{handle}` → **204 无正文**，所以返回 `Promise<void>`。
 *
 * 调用顺序是写死的：**先请后端清，成功后才 `useKeysStore().forget()`**。
 * 反了的话后端失败时本地已经没有 handle 可重试。`404` 也算成功（handle 早就不在了）。
 */
export function dropKey(handle: string): Promise<void> {
  return apiFetch<void>(`/api/keys/${encodeURIComponent(handle)}`, { method: "DELETE" });
}

/**
 * `POST /api/targets/validate` 的入参。
 *
 * `overrides` 里只有这两个开关，**刻意没有第三个**：`blocked_metadata` 与
 * `split_horizon` 在后端是永久硬拦、不可覆盖的（CLAUDE.md §安全不变式），
 * 给它们留一个字段就等于在契约上暗示"有办法绕过"。
 */
export interface ValidateTargetsRequest {
  /** 用户输入的原文，每行一条，已 `trim()` 且去掉空行。至少 1 条。 */
  readonly raw: readonly string[];
  readonly overrides: {
    readonly allow_loopback: boolean;
    readonly allow_private: boolean;
  };
}

/** 规范化结果。字段名全是后端真名（`snake_case`），**不许改成 camelCase**。 */
export interface TargetNormalized {
  readonly url: string;
  readonly scheme: string;
  /** punycode 之后的 host（ASCII）—— 真正被请求的那个名字。 */
  readonly host: string;
  /** 给人看的 host。与 `host` 不同时说明发生了 punycode 转换。 */
  readonly host_unicode: string;
  readonly port: number | null;
  readonly path: string;
  readonly is_ip: boolean;
  readonly punycode_applied: boolean;
}

/** 一个解析到的地址。`rule` 是后端命中的分类规则，原文展示。 */
export interface ResolvedIp {
  readonly address: string;
  /** 4 或 6。 */
  readonly version: number;
  readonly ip_class: string;
  readonly rule: string;
  readonly embedded_ipv4: string | null;
}

/** 一条目标的校验结论。字段与后端 `TargetValidation` 逐字一致。 */
export interface TargetValidation {
  readonly raw: string;
  readonly ok: boolean;
  readonly normalized: TargetNormalized | null;
  readonly kind: string | null;
  readonly resolved_ips: readonly ResolvedIp[];
  readonly ip_class: string | null;
  readonly allowlist_entry: string | null;
  readonly requirement: string | null;
  readonly overridable: boolean;
  /** 非空 = 还缺勾选。元素取 `targetGuard.optIn.<值>`。 */
  readonly required_opt_in: readonly string[];
  /** 被护栏拒时的机器码，交给 `<ErrorNotice code={code} />`。 */
  readonly code: string | null;
  readonly reason: string | null;
  readonly resolution_error: string | null;
  readonly note_code: string | null;
}

export interface ValidateTargetsResponse {
  readonly targets: readonly TargetValidation[];
}

/**
 * `POST /api/targets/validate`。**会做真实 `getaddrinfo`**，所以调用点是一个
 * 明确的按钮，不是输入防抖 —— 见 `components/wizard/StepTargets.tsx`。
 */
export function validateTargets(
  request: ValidateTargetsRequest,
  signal?: AbortSignal,
): Promise<ValidateTargetsResponse> {
  return apiFetch<ValidateTargetsResponse>("/api/targets/validate", {
    body: request,
    ...(signal === undefined ? {} : { signal }),
  });
}

/**
 * `GET /api/scan-templates`。六个模板的目录，后端常量。
 *
 * 默认预算与轮数**只能**从这里来：前端硬编码那张表就会在改后端常量时静默漂移。
 */
export interface ScanTemplateView {
  readonly template_id: string;
  /** `"quick" | "standard" | "deep"`，取 `wizard.modes.<值>`。 */
  readonly scan_mode: string;
  readonly default_budget_usd: number;
  readonly default_max_turns: number;
  readonly recommended: boolean;
}

export interface ScanTemplatesResponse {
  readonly templates: readonly ScanTemplateView[];
}

export function fetchScanTemplates(signal?: AbortSignal): Promise<ScanTemplatesResponse> {
  return apiFetch<ScanTemplatesResponse>(
    "/api/scan-templates",
    signal === undefined ? {} : { signal },
  );
}

/**
 * `GET /api/allowlist`。授权清单的当前状态。
 *
 * 后端的条目还有 `allow_private` / `allow_loopback` / `max_budget_usd` /
 * `forbidden_paths`，**这里刻意不声明** —— 本文件第三节那条"不多解析一个字段"：
 * 多声明的那个迟早会被下游当成契约。第 2 步用得到的只有这六个。
 */
export interface AllowlistEntryView {
  readonly label: string;
  readonly owner: string;
  readonly authorization_ref: string;
  readonly expires: string | null;
  readonly hosts: readonly string[];
  readonly cidrs: readonly string[];
}

/** `config === null` = 清单文件不在或读不出来，那时只能手填授权编号。 */
export interface AllowlistStateResponse {
  readonly config: {
    readonly mode: string;
    readonly entries: readonly AllowlistEntryView[];
  } | null;
  readonly effective_mode: string;
  readonly file_present: boolean;
  readonly stale: boolean;
  readonly file_error: string | null;
  readonly file_error_line: number | null;
}

export function fetchAllowlist(signal?: AbortSignal): Promise<AllowlistStateResponse> {
  return apiFetch<AllowlistStateResponse>("/api/allowlist", signal === undefined ? {} : { signal });
}

/** 覆盖开关。与 `ValidateTargetsRequest.overrides` 同形，同样刻意只有两个。 */
export interface ScanOverridesInput {
  readonly allow_loopback: boolean;
  readonly allow_private: boolean;
}

/**
 * 授权声明。
 *
 * `affirmed` 后端要求**恰好 3 个、且互不相同**，取值只能是
 * `owns_or_authorized` / `not_third_party_production` / `understands_real_attacks`
 * （码表在 `lib/stores/wizard.ts` 的 `AFFIRMATION_IDS`）。
 *
 * `resolved_ips_seen` 是"声明授权时我看到的地址"：key 是 `/api/targets/validate`
 * 回来的 `normalized.host`，value 是那条目标的 `resolved_ips[].address`。后端起扫描
 * 前会重新解析一遍按集合比对，不一致即 `dns_changed` —— 所以**没有第 1 步的校验结果
 * 就构造不出这个字段**，"提交前必须有本会话的校验快照"是结构性要求。
 */
export interface ScanAuthorizationInput {
  readonly operator_name: string;
  readonly authorization_ref: string;
  readonly typed_confirmation: string;
  readonly affirmed: readonly string[];
  readonly multi_target_affirmed: boolean;
  readonly resolved_ips_seen: Readonly<Record<string, readonly string[]>>;
}

/**
 * `POST /api/scans` 的入参。字段名全是后端真名（`snake_case`），**不许改成 camelCase**。
 *
 * `targets` 是**用户原文**，后端自己规范化，并明写"不排序、不去重、不重排"——
 * 逐字确认串的期望值是 `targets[0]` 规范化后的 host，重排会改变语义。
 *
 * `scan_mode` / `reasoning_effort` / `extra_instruction` / `credentials` 四个字段
 * **刻意不在这个类型里**（本轮不发，缺省会走模板默认值）。后端 `extra="forbid"`
 * 只禁多余字段、不禁缺省。
 */
export interface CreateScanRequest {
  readonly vault_handle: string;
  readonly template_id: string;
  readonly targets: readonly string[];
  readonly overrides: ScanOverridesInput;
  readonly max_budget_usd: number;
  readonly max_turns: number;
  readonly authorization: ScanAuthorizationInput;
}

/**
 * `202`。`status` 恒为 `"starting"`。
 *
 * `argv_preview` 里**没有任何凭据**（那就是它叫 preview 的原因）——
 * Key 只经环境变量进子进程。
 */
export interface ScanAcceptedResponse {
  readonly scan_id: string;
  readonly status: string;
  readonly ws: string;
  readonly argv_preview: readonly string[];
  readonly budget_usd: number;
}

/**
 * `POST /api/scans`。**会真的起一个进程、会真的花钱。**
 *
 * 调用点是手写 `async` + `useState`（`components/wizard/SubmitPanel.tsx`），
 * 刻意**不包** `useMutation` —— 不该被 query client 按自己的节奏重放。
 */
export function createScan(request: CreateScanRequest): Promise<ScanAcceptedResponse> {
  return apiFetch<ScanAcceptedResponse>("/api/scans", { body: request });
}

/**
 * Agent 树的一行。REST 快照（`GET /api/scans/{id}` 的 `agents[]`）与 WS 的 `agents` 帧
 * **逐字段同形状**（后端刻意共用一套解析），所以只定义一次、两边共用。
 */
export interface AgentRow {
  readonly id: string;
  readonly name: string | null;
  readonly parent_id: string | null;
  readonly status: string | null;
  readonly created_at: string;
  readonly updated_at: string;
  readonly error_message: string | null;
}

/**
 * 扫描列表的一行（后端 19 键）。`status` 取值：
 * `starting running completed stopped failed interrupted`（另有历史值 `orphaned_running`）。
 */
export interface ScanSummary {
  readonly id: string;
  readonly created_at: string;
  readonly started_at: string | null;
  readonly finished_at: string | null;
  readonly status: string;
  readonly template_id: string;
  readonly targets: readonly string[];
  readonly scan_mode: string;
  readonly max_budget_usd: number;
  readonly cost_usd: number;
  readonly count_critical: number;
  readonly count_high: number;
  readonly count_medium: number;
  readonly count_low: number;
  readonly agent_count: number;
  readonly event_count: number;
  readonly exit_code: number | null;
  readonly exit_meaning: string | null;
  readonly error_code: string | null;
}

export interface ScanDetail extends ScanSummary {
  readonly error_message: string | null;
  readonly max_turns: number | null;
  readonly reasoning_effort: string | null;
  readonly provider: string;
  readonly strix_llm: string;
  readonly current_epoch: number;
  readonly authorization_id: string;
}

export interface ScanDetailResponse {
  readonly scan: ScanDetail;
  readonly agents: readonly AgentRow[];
  readonly findings: readonly Readonly<Record<string, unknown>>[];
}

/**
 * `GET /api/scans/{id}`。未知 id → `ApiError(404, "not_found")`。
 *
 * **扫描结论只从这里取** —— WS 的 `done` 帧是纯信号，不带结论。
 */
export function fetchScan(scanId: string, signal?: AbortSignal): Promise<ScanDetailResponse> {
  const path = `/api/scans/${encodeURIComponent(scanId)}`;
  return apiFetch<ScanDetailResponse>(path, signal === undefined ? {} : { signal });
}

/** `202`。 */
export interface StopScanAcceptedResponse {
  readonly scan_id: string;
  readonly mode: string;
}

/**
 * `POST /api/scans/{id}/stop`。已结束或未知 → `ApiError(404, "not_found")`。
 *
 * 只发 `graceful`：`force` 没有 UI 入口，所以不暴露成参数。
 */
export function stopScan(scanId: string): Promise<StopScanAcceptedResponse> {
  const path = `/api/scans/${encodeURIComponent(scanId)}/stop`;
  return apiFetch<StopScanAcceptedResponse>(path, { body: { mode: "graceful" } });
}
