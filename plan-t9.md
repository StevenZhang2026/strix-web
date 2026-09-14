# T9 方案（2026-09-14 用户审阅通过，未派发）

> `agent-rules.md` §一.3 允许的单次 Plan 快照。**这份文件的唯一用途**：让一个 `/clear` 之后的
> 主会话不用重新读一遍 `PLAN.md` 与 5 个源文件，就能写出 T9 实现 agent 的派发 prompt。
> 派发完就可以删。判据与长期事实已分别落在 `PLAN.md`（T9 行、§向导→CLI 映射、§DooD 那张表），
> **这里不是权威，只是待派发的施工图**。

## 审阅时定下的两件事

1. **6 个模板存 Python 常量**，不存 YAML（理由已写进 `PLAN.md` §向导→CLI 映射）。
2. **`--resume` 分支不进 T9**（`strix_run_name` 要等 T10 的 RunDiscovery 抢到才存得下，现在写分支只能拿假值测）。

## 派发形态

模板 3 的后半段：**另起一个 agent 只做实现**（方案已由主会话写完、人已审）。prompt 里必须带
`agent-rules.md` §十 那五条 + TDD 两句 + 下面「出处摘录」整段（§九.1：只给文件名它必然整读）。

预算：≤ 55 次工具调用、0 次压缩、**第 6 次调用之前必须开始写代码**。
快闸门（子 agent 每条测试都跑它，秒级）：
```
docker run --rm -v "$PWD/backend/app:/app/app:ro" -v "$PWD/backend/tests:/work/tests:ro" \
  -v "$PWD/frontend/messages:/messages:ro" -e CONSOLE_MESSAGES_JSON=/messages/zh-CN.json \
  strix-console/api-test:0.1.0 pytest tests/test_scan_launcher.py tests/test_scan_templates.py
```
（官方闸门 `make test` + `make lint-api` 由主会话在收货时跑一次，见 §九.4。挂载点要按
`backend/Dockerfile` 的 test stage 实际布局核一次：app 在 `/app`、tests 在 `/work`。）

---

## 1. 边界

**做**：把「一次扫描的意图」翻成「可以直接 `create_subprocess_exec` 的四元组」——
argv、env、cwd，以及落在 tmpfs 上的两个文件（`instruction.txt` + 预置 `cli-config.json`）。
**纯构造 + 建目录，不起进程。**

**不做**（每条都有归属）：起进程／退出码归因／`finally` 清理时机 → T10；并发闸、DNS 重解析、
逐字确认、写 `scans` 行、审计 → T12；spec 上传 → T18/T19；`--resume` → T10；
**`zh-CN.json` 的模板中文名 → T19**（`/api/scan-templates` 只回机器码）。

## 2. 文件

```
backend/app/services/scan_templates.py   # 6 个模板的目录（纯数据 + template_for()）
backend/app/services/scan_launcher.py    # argv/env/工作区，纯函数为主
backend/app/routes/templates.py          # GET /api/scan-templates，只做 dataclass→JSON
backend/tests/test_scan_templates.py
backend/tests/test_scan_launcher.py
backend/app/main.py                      # 只加一行 include_router
```

## 3. 模块形状

```python
@dataclass(frozen=True, slots=True)
class LaunchSpec:          # 调用方（T12）填的意图，值都已校验过
    scan_id: str
    template_id: str
    targets: tuple[str, ...]          # 已过 target_guard 的 normalized 值
    scan_mode: str                    # quick|standard|deep
    max_budget_usd: float             # 必填，无默认
    max_turns: int
    reasoning_effort: str | None
    extra_instruction: str | None
    test_credentials: tuple[TestCredential, ...]   # 只进 instruction 文件

@dataclass(frozen=True, slots=True)
class LaunchPlan:          # 交给 T10 去 exec
    argv: tuple[str, ...]
    env: Mapping[str, str]            # 含明文凭据 —— 绝不整体进日志/DB
    env_var_names: tuple[str, ...]    # 给 scans.env_var_names_json
    cwd: Path
    home: Path
    config_path: Path
    instruction_path: Path
    instruction_sha256: str
    argv_preview: tuple[str, ...]     # == argv（Key 从不进 argv）

def build_argv(spec, *, config_path, instruction_path) -> tuple[str, ...]   # 纯
def build_env(credentials, spec, *, home, cwd, environ) -> dict[str, str]   # 纯
def prepare_workspace(settings, spec, instruction_text) -> ...              # 唯一碰 IO
def cleanup_workspace(settings, scan_id) -> None                            # T10 在 finally 调
def compose_instruction(template, spec) -> str                              # 纯
```

`argv_preview == argv` **不是冗余**：接口契约写了"无 Key 的预览"，保留这个名字让
"预览等于真 argv"成为可断言的事实，而不是靠人记得没往 argv 里塞 Key。
`environ` 从参数注入（不在模块里读 `os.environ`）—— 这是"模块级不得有可变全局状态"的落地，
也让 env 测试不用 monkeypatch。

## 4. 黄金 argv（`full_review`，`scan_id=s-abc123`）

```
strix -n -t https://app.example.com -m standard
      --max-budget-usd 25 --max-turns 200
      --instruction-file /run/strix/scan-s-abc123/instruction.txt
      --config /run/strix/scan-s-abc123/home/.strix/cli-config.json
```

固定顺序；`-t` 按目标顺序重复；**永远带 `--instruction-file`**（不用 `--instruction`：
测试账号口令会进指令正文，走 argv 等于进 `ps`）；`--scope-mode`／`--diff-base` 一律不发
（那是代码目标的参数，v1 没有代码目标）。

## 5. 工作区（§安全不变式 里"唯一防线"的那三样）

```
/run/strix/scan-<id>/                    0700  tmpfs(noexec,nosuid,size=16m,mode=0700)
├── instruction.txt                      0600  ← 测试账号口令只存在于这里
└── home/                                0700  ← HOME
    └── .strix/cli-config.json           0600  ← 预置 {"env":{}}
${DATA}/scans/<id>/                      0700  ← cwd（strix_runs/<自动名>/ 落在这里）
└── tmp/                                 0700  ← TMPDIR
```

`--config` 指到 `$HOME/.strix/cli-config.json` 是**刻意的双保险**：`--config` 生效时是那个文件；
万一它不生效、`persist_current()` 回落到 `$HOME/.strix/`，落点仍是同一个 tmpfs 文件。
M0 实测跑通的就是这个形状。

## 6. env：显式白名单，**绝不 `os.environ.copy()`**

| 来源 | 变量 |
|---|---|
| 每任务算出 | `HOME`、`TMPDIR`、`STRIX_RUN_ID=<scan_id>`、`STRIX_LLM=model_for(spec, cred.strix_llm)`、`STRIX_PROMPT_CACHE`（解析后的模型名含 `invoke/` → `false`，否则 `true`；判据是**路由**不是 auth_shape）、`STRIX_REASONING_EFFORT`（给了才设） |
| 凭据（`CredentialSet`）| `secrets` 每个键原样注入 + `params` 每个键（`AWS_REGION_NAME`）+ `LLM_API_BASE`（`api_base` 非空才设） |
| 容器级透传（按名字白名单从注入的 `environ` 取，缺就跳过）| `PATH` `STRIX_IMAGE` `STRIX_DOCKER_SANDBOX_NETWORK` `STRIX_RUN_TYPE` `STRIX_TELEMETRY` `STRIX_NO_UPDATE_CHECK` `LITELLM_LOG` `SSL_CERT_FILE` `REQUESTS_CA_BUNDLE` `DOCKER_HOST` `LANG` |
| **永不出现** | `STRIX_DEBUG`（一条独立测试断言它不在结果里，哪天有人往 compose 里加了也拦得住） |

## 7. 拒绝路径（构造阶段就拒，**一个新码都不加**）

- `max_budget_usd` 缺失／≤0 → `InvalidRequestError(field="max_budget_usd")`；
  \> `settings.console_max_budget_ceiling_usd` → 已有的 `BudgetExceedsCeilingError`（409）
- `max_turns` ≤0 → 422；`template_id` 不认识 → 422；`targets` 为空 → 422
- `reasoning_effort` 不在 `none|minimal|low|medium|high|xhigh|max` 里 → 422
  （不拦就是子进程在 pydantic 启动校验上死掉，归因成"扫描失败"）
- `vault_handle` 失效 → `KeyRequiredError` **由 T12 抛**（launcher 收到的是已取出的 `CredentialSet`）

## 8. 6 个模板

| `template_id` | `-m` | 预算/轮数 | 指令要点（照 `PLAN.md` §向导→CLI 映射 那张表展开成 8–15 行英文） |
|---|---|---|---|
| `quick_triage` | quick | 5 / 60 | 限时分诊；只测 6 类高影响可直接利用的问题；跳过子域枚举与目录爆破 |
| `full_review`（推荐）| standard | 25 / 200 | 先枚举功能与角色，再系统性走 OWASP Top 10 全入口；边走边记 coverage |
| `deep_audit` | deep | 80 / 500 | 按功能域派生子 agent；把发现串成完整攻击链而非孤立原语 |
| `auth_and_access` | standard | 15 / 120 | 登录/注册/找回/改邮箱/MFA/会话/JWT/水平与垂直越权；**不**花轮数在 XSS 与注入上；只用自建账号，绝不锁死真实账号 |
| `api_surface` | standard | 25 / 200 | 枚举 spec 每个 operation；报告"spec 里有但不可达"与"未文档化但存在"。**v1 不做 spec 上传**（`--workspace-file` 在 1.5.3 不存在） |
| `pre_release_recheck` | standard | 12 / 100 | 针对改动说明做回归；已报问题要**实测**是否真修好 |

**统一尾巴**（6 份逐字相同，一条测试断言它出现在每一份里）：finding 的
`description`/`impact`/`remediation_steps` **中英双写、中文在前**；禁止 DoS／资源耗尽／
数据破坏／账号锁定类测试。
尾巴里**不写**"不得扩大授权范围"之类的话 —— 权威 scope 由 Strix 的 `build_scope_context` 注入，
我们再写一遍只会让人以为护栏在 prompt 里。

`instruction_sha256` = 最终合成文本的 sha256（DB 只留它，正文随 tmpfs 消失）。

## 9. 测试

- **6 条黄金 argv**，逐元素比对完整 `tuple`（不是"包含某个片段"）
- env 三条：白名单外的变量不出现；`STRIX_DEBUG` 不出现；`invoke/` 模型 → `STRIX_PROMPT_CACHE=false`
  （bearer 与"用户手打 `bedrock/invoke/x` 的 SigV4"都要覆盖）
- 工作区：路径／权限位（0700/0600）／`cli-config.json` 内容是 `{"env":{}}`／`cleanup_workspace` 后整棵树消失
- 卫生：argv 不含 `CredentialSet` 里任何一个值；测试账号口令**只**出现在 instruction 文件里；
  `instruction_sha256` 与文件内容对得上
- 路由：返回 6 条、字段是机器码、无中文（一条 `一-鿿` 断言）
- **不起真进程、不碰真 docker**；`prepare_workspace` 用 `tmp_path` 当两个根

---

## 出处摘录（**整段抄进派发 prompt**，§九.1：只给文件名它必然整读）

### `strix 1.5.3` 的 CLI 全表面（2026-09-14 `strix --help` 实测）
```
strix [-h] [-v] [--update] [-t TARGET] [--target-list PATH]
      [--instruction I] [--instruction-file F] [-n]
      [-m {quick,standard,deep}] [--scope-mode {auto,diff,full}] [--diff-base B]
      [--config CONFIG] [--max-budget USD | --max-budget-usd USD]
      [--max-turns N] [--resume RUN_NAME]
```
`--max-budget` 与 `--max-budget-usd` 是同一个参数的两个名字（用长名）。`--max-turns` 默认 500。
**没有** `--model`、`--output-dir`、`--run-name`、`--workspace-file`。

### `strix.config.settings.LlmSettings` 的 env 别名（2026-09-14 读源码）
```python
model:            alias="STRIX_LLM"
api_key:          AliasChoices("LLM_API_KEY", "OPENAI_API_KEY")
api_base:         AliasChoices("LLM_API_BASE", "OPENAI_API_BASE", "OPENAI_BASE_URL", ...)
reasoning_effort: alias="STRIX_REASONING_EFFORT"   # Literal[none,minimal,low,medium,high,xhigh,max]，默认 high
prompt_cache:     alias="STRIX_PROMPT_CACHE"       # 默认 True
```

### `app/services/key_vault.py` —— launcher 收到的东西
```python
@dataclass(frozen=True, slots=True)
class CredentialSet:
    provider: str
    auth_shape: str
    strix_llm: str
    api_base: str | None
    secrets: Mapping[str, SecretStr]   # 键就是要注入子进程的 env 变量名
    params: Mapping[str, str]          # 非机密参数（AWS_REGION_NAME），不进 secret_values()
```
`KeyVault.acquire(handle) -> CredentialSet | None` / `release(handle)` 由 **T12** 配对调用，
不是 launcher 的事。`KeyVault.secret_values() -> frozenset[str]` 供卫生测试用。

### `app/services/llm_client.py` —— 模型名怎么拼（**T9 必须调它，别自己拼前缀**）
```python
def model_for(spec: ShapeSpec, strix_llm: str) -> str:
    """把用户填的模型名拼成 litellm 要的形状。纯函数。
    **路由由 auth_shape 决定，不由用户的打字决定** —— 先剥掉用户自己带的 bedrock/ 或
    bedrock/invoke/ 再补。"""
```
`spec_for(provider, auth_shape) -> ShapeSpec | None`；`ShapeSpec` 有
`auth_shape / secret_keys / param_keys / model_prefix`；`_BEDROCK_INVOKE_PREFIX = "bedrock/invoke/"`。
`SHAPE_BEDROCK_BEARER` 的 `model_prefix` 就是那个 invoke 前缀 → 所以
**`STRIX_PROMPT_CACHE` 的判据是 `"invoke/" in model_for(...)`，不是 `auth_shape == bearer`**。

### `app/errors.py` —— 现成的码，别新增
```python
ParamValue = str | int | float | bool | None      # 只许 JSON 标量，刻意不许 dict/list
class ConsoleError(Exception):
    code: ClassVar[str]; status: ClassVar[int]
    def __init__(self, **params: ParamValue) -> None: ...   # raise XxxError(field="max_budget_usd")
class InvalidRequestError(ConsoleError):      code="invalid_request";       status=422
class BudgetExceedsCeilingError(ConsoleError): code="budget_exceeds_ceiling"; status=409
class ConcurrencyLimitError(ConsoleError):    code="concurrency_limit";     status=409   # T12 用
class KeyRequiredError(ConsoleError):         code="key_required";          status=409   # T12 用
```

### `app/settings.py` —— 用到的字段名
```
console_data_dir: Path                  console_ephemeral_home_root: Path = Path("/run/strix")
console_max_concurrent_scans: int = 1    console_max_budget_ceiling_usd: float = 100.0
```

### 风格范本：`app/routes/providers.py`（**只点这一个文件，别搭售别的**）
72 行，与 `routes/templates.py` 是同一个形状（进程内常量 → 对外 JSON）：
```python
router = APIRouter(prefix="/api/providers", tags=["providers"])

class ProvidersResponse(BoundaryModel):
    """信封而不是裸数组 —— 顶层是数组就没法再加字段。"""
    providers: tuple[ProviderView, ...]

@router.get("", response_model=ProvidersResponse)
async def list_providers() -> ProvidersResponse:
    """无参、恒 200。目录是进程内常量，没有 IO。"""
```
它的模块 docstring 里两条**照抄的判据**：① 目录数据放在 service 里、路由只做转换；
② 响应**全是机器码**，中文在 `zh-CN.json`——"后端一旦回中文，同一个名字就有两份文案，
而后端那份还绕过脱敏"。

### `tests/conftest.py` 已有的夹具（**不许再造**，§十.3）
`settings`（`tmp_path` 版）· `auth_file` · `app` · `anonymous` · `client` · `db` · `conn` ·
`make_entry` · `insert_authorization` · `insert_scan` · `restore_logging`。
`/api/scan-templates` 在全局鉴权之后 → 路由测试用 `client`（已登录），不是 `anonymous`。

### M0 实测跑通的工作区形状（`scripts/m0_probe_inner.sh:41-43,155-161`）
```sh
HOME_DIR="/run/strix/${RUN_ID}";  CFG_DIR="${HOME_DIR}/.strix";  CFG="${CFG_DIR}/cli-config.json"
WORK="${CONSOLE_DATA_DIR}/scans/${RUN_ID}"
mkdir -p "${WORK}/tmp" "${CFG_DIR}";  chmod 700 "${HOME_DIR}" "${CFG_DIR}"
printf '%s\n' '{"env":{}}' > "${CFG}";  chmod 600 "${CFG}"
HOME="${HOME_DIR}" STRIX_RUN_ID="${RUN_ID}" strix -n -t "$M0_TARGET" -m quick \
  --max-budget-usd 2 --max-turns 20 --config "${CFG}"
```
T9 在它上面**只多一级** `home/`（任务目录 `/run/strix/scan-<id>/` 还要放 `instruction.txt`，
它不该在 HOME 里面），与 `CLAUDE.md` §安全不变式 写的 `HOME=<tmpfs>/scan-<id>/home` 一致。
