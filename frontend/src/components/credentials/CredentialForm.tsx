"use client";

import { useQuery } from "@tanstack/react-query";
import { Fragment, useEffect, useId, useState, type FormEvent } from "react";

import { ErrorNotice } from "@/components/errors/ErrorNotice";
import { Button } from "@/components/ui/Button";
import { StatusDot } from "@/components/ui/StatusDot";
import {
  ApiError,
  dropKey,
  fetchKeyState,
  fetchProviders,
  registerKey,
  type ApiParamValue,
} from "@/lib/api/client";
import { t, type MessageKey } from "@/lib/messages";
import { useKeysStore } from "@/lib/stores/keys";

import styles from "./CredentialForm.module.css";

/**
 * 凭据表单。**全前端唯一一处明文密钥进入内存的地方。**
 *
 * =============================================================================
 * 密文纪律（四条，每一条都是结构性的，不靠人记）
 *
 * · **提交不走 react-query 的 `useMutation`** —— 那会把带密文的 variables 交给
 *   query client 持有一段不确定的时间。所以是手写 `async` + `useState`，照
 *   `components/auth/LoginForm.tsx` 那个先例。`GET` 那两个（目录、恢复显示）
 *   才用 `useQuery`，它们的响应里没有任何能推回明文的东西。
 * · **`finally` 里清空所有密文 state**，成功失败都清。
 * · **这条链路上一行日志都不许有**（`console.*`、埋点、错误上报都算）。
 * · **密文框刻意破浏览器自动填充**：随机 `name` + `autoComplete="off"`。
 *   别人家的 provider 密钥被浏览器存下来是净损失。这与登录口令框**刻意相反**
 *   （那里是标准 `name` + `autoComplete="current-password"`），不要去"统一"它们。
 *
 * `vault_handle` 只经 `useKeysStore().setHandle()` 落地 —— 这个文件不碰
 * `sessionStorage`（全项目只有 `lib/stores/keys.ts` 许可碰它，eslint 封死）。
 *
 * `preset`（续跑用）：供应商／形状／模型锁死为上次那一套，只让用户重新填密文与参数。
 * 后端要求三者逐字相等，所以这里不许回落到目录里的别家。
 */
export interface CredentialPreset {
  readonly provider: string;
  readonly auth_shape: string;
  readonly strix_llm: string;
}

export function CredentialForm({ preset }: { readonly preset?: CredentialPreset } = {}) {
  const handle = useKeysStore((s) => s.handle);
  const setHandle = useKeysStore((s) => s.setHandle);

  const uid = useId();

  // 随机 `name` 在**挂载后**才生成：`useId()` 是服务端与客户端一致的，
  // 而 `crypto.randomUUID()` 不是 —— 放进 `useState` 的惰性初始值会在
  // 客户端组件的 SSR 与 hydration 之间对不上（属性 mismatch）。
  const [nameSalt, setNameSalt] = useState("");
  useEffect(() => {
    setNameSalt(crypto.randomUUID());
  }, []);

  const { data: catalog, isError: catalogFailed } = useQuery({
    queryKey: ["providers"],
    queryFn: ({ signal }) => fetchProviders(signal),
    enabled: handle === null,
  });

  const locked = preset !== undefined;
  const [providerName, setProviderName] = useState(preset?.provider ?? "");
  const [shapeName, setShapeName] = useState(preset?.auth_shape ?? "");
  const [model, setModel] = useState(preset?.strix_llm ?? "");
  const [apiBase, setApiBase] = useState("");
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [params, setParams] = useState<Record<string, string>>({});
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<Error | null>(null);

  // 已登记：整块换成"当前用的是哪个凭据"，不留一个空表单在下面招人再填一次。
  if (handle !== null) {
    return <CredentialSummary handle={handle} />;
  }

  if (catalogFailed) {
    return <ErrorNotice code="internal_error" />;
  }
  if (catalog === undefined) {
    return <p className={styles.hint}>{t("common.loading")}</p>;
  }

  // 选中项由"名字 + 目录"推出来，没选过就落在第一个上 —— 这样不需要一个
  // 把默认值写进 state 的 effect，也不需要一个空占位 option。
  const namedProvider = catalog.providers.find((item) => item.provider === providerName);
  // 有 preset 时不回落：悄悄换成别家，后端只会回一个 `key_required`。
  const providerOrNone = locked ? namedProvider : (namedProvider ?? catalog.providers[0]);
  if (providerOrNone === undefined) {
    return <p className={styles.hint}>{t("common.empty")}</p>;
  }
  const provider = providerOrNone;
  const namedShape = provider.shapes.find((item) => item.auth_shape === shapeName);
  const shapeOrNone = locked ? namedShape : (namedShape ?? provider.shapes[0]);
  if (shapeOrNone === undefined) {
    return <p className={styles.hint}>{t("common.empty")}</p>;
  }
  const shape = shapeOrNone;

  // 换供应商 / 换形状都要把已填的东西清掉：那几个框的键名整套都变了，
  // 留着旧值只会连同旧的密文一起发出去。
  function resetInputs(): void {
    setModel("");
    setApiBase("");
    setSecrets({});
    setParams({});
    // 连上一次的失败一起清：那条 `ErrorNotice` 讲的是另一个供应商/形状的事，
    // 留在新表单下面就是在指认一个不存在的问题。
    setFailure(null);
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (pending) {
      return;
    }
    setPending(true);
    setFailure(null);
    try {
      const base = apiBase.trim();
      const registered = await registerKey({
        provider: provider.provider,
        auth_shape: shape.auth_shape,
        strix_llm: model.trim(),
        // 留空就发 `null`，不发空串 —— 空串是"我要一个叫空的地址"。
        api_base: provider.api_base_allowed && base !== "" ? base : null,
        // 每个值都 `trim()`：**粘贴带进来的首尾空白是这个表单最常见的失败**，而它
        // 产生的失败一个原因都不带（验活按契约不外带异常正文）。实测两种后果：
        // 区域多一个空格 → litellm 本地就拒（`Invalid AWS region format`，9 毫秒，
        // 请求没发出去）；token 末尾一个换行 → httpx 当成 header 注入直接拒。
        // 凭据首尾的空白不可能是有意义的，剥掉它比让用户对着一个没有原因的失败排查好。
        secrets: Object.fromEntries(
          shape.secret_keys.map((key) => [key, (secrets[key] ?? "").trim()]),
        ),
        params: Object.fromEntries(shape.param_keys.map((key) => [key, (params[key] ?? "").trim()])),
        // 恒 `true`，不给用户开关：不验活只会把"凭据填错了"推迟到扫描启动那一刻。
        verify: true,
      });
      setHandle(registered.vault_handle);
    } catch (cause) {
      // 这里**不许**有任何日志、埋点、错误上报 —— 密文还在作用域里。
      setFailure(cause instanceof Error ? cause : new Error("register_failed"));
    } finally {
      setSecrets({});
      setPending(false);
    }
  }

  return (
    <form onSubmit={handleSubmit}>
      {/* 「凭据只在内存里、后端一重启就要重新提供」放在**这个分支**里，因为
          `credentials.none` 头四个字是"还没有提供凭据" —— 对已登记的人是假话。
          页面壳里那一份已经因此撤掉了（`(app)/credentials/page.tsx`）。 */}
      <p className={styles.lede}>{t("credentials.none")}</p>

      <div className={styles.row}>
        <label className={styles.label} htmlFor={`${uid}-provider`}>
          {t("credentials.providerLabel")}
        </label>
        <select
          className={styles.select}
          id={`${uid}-provider`}
          value={provider.provider}
          disabled={pending || locked}
          onChange={(event) => {
            setProviderName(event.target.value);
            setShapeName("");
            resetInputs();
          }}
        >
          {catalog.providers.map((item) => (
            <option key={item.provider} value={item.provider}>
              {t(`providers.vendors.${item.provider}` as MessageKey)}
            </option>
          ))}
        </select>
      </div>

      <fieldset className={styles.shapes} disabled={pending || locked}>
        <legend className={styles.label}>{t("credentials.shapeLabel")}</legend>
        {provider.shapes.map((item) => (
          <label className={styles.shape} key={item.auth_shape}>
            <input
              type="radio"
              name={`${uid}-shape`}
              value={item.auth_shape}
              checked={item.auth_shape === shape.auth_shape}
              onChange={() => {
                setShapeName(item.auth_shape);
                resetInputs();
              }}
            />
            <span>
              <span className={styles.shapeTitle}>
                {t(`providers.shapes.${item.auth_shape}` as MessageKey)}
              </span>
              {/* 形状说明不是 tooltip、不折叠：bearer 那条是一段费用取舍，
                  要在选之前读到，读不到就会多花 4～6 倍的钱。 */}
              <span className={styles.shapeNote}>
                {t(`providers.shapeNotes.${item.auth_shape}` as MessageKey)}
              </span>
            </span>
          </label>
        ))}
      </fieldset>

      <div className={styles.row}>
        <label className={styles.label} htmlFor={`${uid}-model`}>
          {t("providers.modelLabel")}
        </label>
        {/* 自由文本，**刻意不是下拉框**：我们只实测过几个模型名，
            用下拉框就等于宣布"别的都不支持"。`models` 只是可以抄的值。 */}
        <input
          className={styles.input}
          id={`${uid}-model`}
          type="text"
          autoComplete="off"
          spellCheck={false}
          required
          value={model}
          readOnly={locked}
          disabled={pending}
          onChange={(event) => setModel(event.target.value)}
        />
        <p className={styles.hint}>{t("providers.modelHint")}</p>
        {/* OpenRouter 的 `:free` 模型有 50 次/天硬顶，撑不住一次完整扫描（实测：6 个
            子 agent 还没发出探测就集体 429）。只提示、不拦 —— 用户可能只是想试跑。 */}
        {model.trim().endsWith(":free") ? (
          <p className={styles.warning}>{t("providers.modelFreeTierWarning")}</p>
        ) : null}
        {locked ? null : provider.models.length === 0 ? (
          <p className={styles.hint}>{t("providers.modelsEmpty")}</p>
        ) : (
          <div className={styles.samples}>
            {provider.models.map((name) => (
              <Button key={name} variant="ghost" disabled={pending} onClick={() => setModel(name)}>
                {name}
              </Button>
            ))}
          </div>
        )}
      </div>

      {shape.secret_keys.map((key, index) => (
        <div className={styles.row} key={key}>
          <label className={styles.label} htmlFor={`${uid}-secret-${index}`}>
            {t(`providers.secretLabels.${key}` as MessageKey)}
          </label>
          <input
            className={styles.input}
            id={`${uid}-secret-${index}`}
            // 随机 `name`：破浏览器自动填充。见文件顶部第四条。
            name={`s-${nameSalt}-${index}`}
            type="password"
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            required
            value={secrets[key] ?? ""}
            disabled={pending}
            onChange={(event) =>
              setSecrets((prev) => ({ ...prev, [key]: event.target.value }))
            }
          />
        </div>
      ))}

      {shape.param_keys.map((key) => (
        <div className={styles.row} key={key}>
          <label className={styles.label} htmlFor={`${uid}-param-${key}`}>
            {t(`providers.paramFields.${key}` as MessageKey)}
          </label>
          <input
            className={styles.input}
            id={`${uid}-param-${key}`}
            type="text"
            autoComplete="off"
            spellCheck={false}
            required
            value={params[key] ?? ""}
            disabled={pending}
            onChange={(event) => setParams((prev) => ({ ...prev, [key]: event.target.value }))}
          />
          <p className={styles.hint}>{t(`providers.paramHints.${key}` as MessageKey)}</p>
        </div>
      ))}

      {!provider.api_base_allowed ? null : (
        <div className={styles.row}>
          <label className={styles.label} htmlFor={`${uid}-api-base`}>
            {t("providers.apiBaseLabel")}
          </label>
          <input
            className={styles.input}
            id={`${uid}-api-base`}
            type="text"
            autoComplete="off"
            spellCheck={false}
            value={apiBase}
            disabled={pending}
            onChange={(event) => setApiBase(event.target.value)}
          />
          <p className={styles.hint}>{t("providers.apiBaseHint")}</p>
        </div>
      )}

      <div className={styles.actions}>
        {/* 本页唯一的 `--gate` 动作：它会真的拿用户的 Key 去发一次请求。 */}
        {/* 按钮上写「登记并验活」而不是「现在提供凭据」：按下去会**真的拿这把 Key
            去发一次请求**，动作名要说出这件事（首页侧栏那个链接才是"去提供凭据"）。 */}
        <Button type="submit" disabled={pending}>
          {pending ? t("credentials.verifying") : t("credentials.register")}
        </Button>
        {!pending ? null : <span className={styles.hint}>{t("providers.verifyingLabel")}</span>}
      </div>

      {/* 错误块固定在按钮下方**表单之内**：出错时按钮不移动。 */}
      {failure === null ? null : (
        <div className={styles.failure}>
          <ErrorNotice {...noticeProps(failure)} />
        </div>
      )}
    </form>
  );
}

/**
 * 已登记态：provider / 形状 / 模型 / 掩码标签 + 「立即忘记凭据」。
 *
 * `/credentials` 页与首页侧栏用的是**同一个**它 —— 这里有一条不能写两遍的决定：
 * **先 `DELETE`，成功后才清本地 handle**。反了的话后端失败时本地已经没有
 * handle 可重试（`lib/stores/keys.ts` 的 docstring 早写了）。
 */
export function CredentialSummary({ handle }: { readonly handle: string }) {
  const forget = useKeysStore((s) => s.forget);
  const [dropping, setDropping] = useState(false);
  const [failure, setFailure] = useState<Error | null>(null);

  const { data, isError } = useQuery({
    queryKey: ["keyState", handle],
    queryFn: async ({ signal }) => {
      try {
        return await fetchKeyState(handle, signal);
      } catch (cause) {
        // 404 = 后端重启过，内存 vault 空了。清掉本地 handle，否则这一片会一直
        // 显示一个已经不存在的凭据，而用户要等到向导提交才撞 409。
        if (cause instanceof ApiError && cause.status === 404) {
          forget();
        }
        throw cause;
      }
    },
  });

  async function handleForget(): Promise<void> {
    if (dropping) {
      return;
    }
    setDropping(true);
    setFailure(null);
    try {
      await dropKey(handle);
      forget();
    } catch (cause) {
      // 404 照样算成功：后端内存里本来就没有它了，本地留着只会更糟。
      if (cause instanceof ApiError && cause.status === 404) {
        forget();
      } else {
        setFailure(cause instanceof Error ? cause : new Error("drop_failed"));
      }
    } finally {
      setDropping(false);
    }
  }

  return (
    <div>
      <p className={styles.verified}>
        <StatusDot tone="ok" />
        {t("credentials.verified")}
      </p>

      {isError ? (
        // 拉不回来（404 已经在 `queryFn` 里清掉本地 handle 了，所以这里只剩
        // 真故障）。不装作"还在加载"——那会一直转下去。
        <ErrorNotice code="internal_error" />
      ) : data === undefined ? (
        <p className={styles.hint}>{t("common.loading")}</p>
      ) : (
        <dl className={styles.summary}>
          <dt className={styles.summaryKey}>{t("credentials.maskedLabel")}</dt>
          <dd className={styles.summaryValue}>
            {t(`providers.vendors.${data.provider}` as MessageKey)}
            {" · "}
            {t(`providers.shapes.${data.auth_shape}` as MessageKey)}
          </dd>

          <dt className={styles.summaryKey}>{t("providers.modelLabel")}</dt>
          <dd className={styles.summaryValue}>{data.strix_llm}</dd>

          {Object.entries(data.labels).map(([key, masked]) => (
            <Fragment key={key}>
              <dt className={styles.summaryKey}>
                {t(`providers.secretLabels.${key}` as MessageKey)}
              </dt>
              <dd className={styles.summaryValue}>{masked}</dd>
            </Fragment>
          ))}

          {Object.entries(data.params).map(([key, value]) => (
            <Fragment key={key}>
              <dt className={styles.summaryKey}>
                {t(`providers.paramFields.${key}` as MessageKey)}
              </dt>
              <dd className={styles.summaryValue}>{value}</dd>
            </Fragment>
          ))}

          {data.api_base === null ? null : (
            <>
              <dt className={styles.summaryKey}>{t("providers.apiBaseLabel")}</dt>
              <dd className={styles.summaryValue}>{data.api_base}</dd>
            </>
          )}
        </dl>
      )}

      <div className={styles.actions}>
        <Button variant="stop" disabled={dropping} onClick={handleForget}>
          {t("credentials.forget")}
        </Button>
      </div>

      {failure === null ? null : (
        <div className={styles.failure}>
          <ErrorNotice {...noticeProps(failure)} />
        </div>
      )}
    </div>
  );
}

/** `ErrorNotice` 的入参。与那个组件的 props 保持同形，不另立一套。 */
interface NoticeProps {
  readonly code: string;
  readonly traceId?: string;
  readonly params?: Readonly<Record<string, ApiParamValue>>;
}

/**
 * 一个失败对象 → 展示用的三件事。**纯函数。**
 *
 * `NetworkError` 与渲染期 bug 归 `internal_error`，沿用 `LoginForm` 已有的判据，
 * 不另立一套映射。其余一律把后端给的 `{code, trace_id, params}` 原样交给
 * `ErrorNotice`（`key_verify_failed` 的 `provider` / `latency_ms` 就在里面）——
 * 前端按码分支，**不匹配文案**。
 */
function noticeProps(failure: Error): NoticeProps {
  if (!(failure instanceof ApiError)) {
    return { code: "internal_error" };
  }
  return { code: failure.code, traceId: failure.traceId, params: failure.params };
}
