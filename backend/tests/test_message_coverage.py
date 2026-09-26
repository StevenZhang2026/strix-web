"""前端文案表必须与后端机器码**双向**对齐。

# 这个文件为什么在 backend/tests 里

它测的是一条**跨越前后端的契约**：`app/errors.py` 里的每一个码都必须在
`frontend/messages/zh-CN.json` 里有一条中文文案，反过来文案表里也不许有后端已经
不存在的孤儿码。放在后端是因为**码的权威源在后端** —— 加码的人在改 `errors.py`，
让他在同一次 `make test` 里看到失败，比让他记得去跑一次前端的测试现实得多。

T6–T26 每一轮都会加机器码（`PLAN.md` 的派发清单里至少还有十几个抛出方要落地）。
**这个测试是那件事唯一的自动提醒。** 没有它，漏掉的文案要等到某个用户正好撞上
那条错误分支时才被发现 —— 而错误分支恰恰是最少被打开的页面。

# 双向的理由

- **缺文案 → 失败**：前端 `errorCopy()` 对未登记的码返回 `None`，页面会退化成
  `internal_error` 的文案 + 露出原始码。那是兜底，不是可接受的终态。
- **孤儿码 → 也失败**：文案表里留着一条后端已经删掉的码，看起来无害，实际是在骗
  下一个读文案表的人 —— 他会以为那条错误还会发生，于是在 UI 里为它留位置。

# 为什么不 grep「绝不超过」四个字

那四个字是**被禁止出现在 UI 上**的措辞（预算是软上限，`--max-budget-usd` 达到后
才停止结算，UI 只许说「达到上限后停止」）。但"禁止"这条规则本身要被记在某处，
而记它的地方（`frontend/messages/README.md`、本文件的这段注释）必然包含这四个字。
全文扫描会把"规则的记录"报成"规则的违反"。所以下面只断**具体的 key**。

# 找不到文案表时会 skip，而不是假装通过

`make test` 的构建上下文只有 `backend/`（见 `Makefile` 的 `build-test`），所以镜像
里**没有** `frontend/messages/zh-CN.json`。本文件会按顺序找：
`CONSOLE_MESSAGES_JSON` 环境变量 → 从 `__file__` 往上找 `frontend/messages/`。
两者都不中就 `pytest.skip`，并在原因里写清一行修法。

skip 不是通过：pytest 会在汇总里列出来。但它也确实意味着**这一轮 CI 没有真的验到
这条契约** —— 要让它在容器里也生效，得把 `frontend/messages` 一起进构建上下文
（或在 `make test` 的 `docker run` 上加一个只读挂载）。这一点已如实报回主会话。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from app.errors import ALL_ERRORS, SCAN_FAILURE_CODES, ConsoleError
from app.routes.stream import STREAM_LAGGED
from app.services.dns_resolver import RESOLUTION_ERROR_CODES
from app.services.run_projector import (
    NOTICE_CONTEXT_COMPACTED,
    NOTICE_SCREENSHOT_ELIDED,
    NOTICE_STREAM_RESYNCED,
)
from app.services.scan_resume import RESUME_REFUSAL_REASONS
from app.services.scan_templates import TEMPLATES
from app.services.system_status import ALL_BLOCKER_CODES
from app.services.target_guard import (
    GuardRequirement,
    OperatorOptIn,
    OptInFlag,
    RejectionReason,
    TargetCategory,
    evaluate_target,
    no_allowlist,
    normalize_target,
)

_MESSAGES_ENV = "CONSOLE_MESSAGES_JSON"
_REL_PATH = Path("frontend") / "messages" / "zh-CN.json"

_SKIP_REASON = (
    f"找不到 {_REL_PATH}。`make test` 的构建上下文只有 backend/，镜像里没有前端文案表。"
    f"要在容器里也验这条契约：给 make test 的 docker run 加 "
    f"`-v $(PWD)/frontend/messages:/app/frontend/messages:ro`，或设 {_MESSAGES_ENV}=<路径>。"
)


def _locate_messages() -> Path | None:
    """环境变量优先，其次从本文件往上逐级找仓库根。

    **环境变量设了但文件不在 → `pytest.fail`，不是 skip。** 这两种情形必须分开：
      · 没设环境变量（宿主上裸跑 `pytest`，没有前端目录）→ skip 是对的降级。
      · 设了却找不到 → 说明 `make test` 的只读挂载坏了，而那正是这条契约在 CI 里
        被验到的唯一途径。此时 skip 会让 `make test` 继续退 0，于是"守卫没在跑"
        这件事只在有人去数 skip 数量时才会被发现 —— 那等于没有守卫。
    """
    override = os.environ.get(_MESSAGES_ENV)
    if override:
        candidate = Path(override)
        if not candidate.is_file():
            pytest.fail(
                f"{_MESSAGES_ENV}={override} 指向的文件不存在。"
                "多半是 Makefile 里那条 `-v .../frontend/messages:...:ro` 挂载坏了。"
                "这里刻意不 skip —— 见 _locate_messages 的注释。"
            )
        return candidate
    for parent in Path(__file__).resolve().parents:
        candidate = parent / _REL_PATH
        if candidate.is_file():
            return candidate
    return None


@pytest.fixture(scope="module")
def messages() -> dict[str, Any]:
    """整张文案表。

    返回类型里的 `Any` 是必要的：这是一份任意深度的外部 JSON，值可能是 str、dict
    或 list（`errors.*.params`）。给它编一个精确的 TypedDict 只会在下一次加一层
    嵌套时失配，而下面每一处取值都自己做了类型检查。
    """
    path = _locate_messages()
    if path is None:
        pytest.skip(_SKIP_REASON)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), "zh-CN.json 的根必须是对象"
    return loaded


# =============================================================================
# 一、后端侧的真实码集合
#
# 取 `ALL_ERRORS` 与 `ConsoleError.__subclasses__()` 的**并集**，不是二选一：
#   · 只看 `ALL_ERRORS` —— 有人加了子类但忘了登记，这个测试就看不见那个新码；
#   · 只看 `__subclasses__()` —— 只有被 import 过的类才在里面，将来码分文件之后
#     可能漏。
# 并集让两种遗忘都暴露。`ALL_ERRORS` 自身的完整性由 test_error_shape.py 守。
# =============================================================================
def _backend_http_codes() -> set[str]:
    codes = {error.code for error in ALL_ERRORS}
    codes |= {subclass.code for subclass in ConsoleError.__subclasses__()}
    return codes


def test_every_http_error_code_has_copy(messages: dict[str, Any]) -> None:
    copy_codes = set(messages["errors"].keys())
    missing = _backend_http_codes() - copy_codes
    assert not missing, (
        f"这些 HTTP 错误码没有中文文案，去 zh-CN.json 的 errors 里加：{sorted(missing)}"
    )


def test_no_orphan_http_error_copy(messages: dict[str, Any]) -> None:
    copy_codes = set(messages["errors"].keys())
    orphans = copy_codes - _backend_http_codes()
    assert not orphans, (
        f"文案表里这些码后端已经不存在了，删掉或去 errors.py 补回来：{sorted(orphans)}"
    )


def test_every_scan_failure_code_has_copy(messages: dict[str, Any]) -> None:
    copy_codes = set(messages["scanFailures"].keys())
    missing = SCAN_FAILURE_CODES - copy_codes
    assert not missing, f"这些扫描归因码没有中文文案：{sorted(missing)}"


def test_no_orphan_scan_failure_copy(messages: dict[str, Any]) -> None:
    copy_codes = set(messages["scanFailures"].keys())
    orphans = copy_codes - SCAN_FAILURE_CODES
    assert not orphans, f"文案表里这些归因码后端已经不存在了：{sorted(orphans)}"


def test_two_code_trees_stay_disjoint(messages: dict[str, Any]) -> None:
    """同一个码不许同时出现在两棵树里。

    两棵树是刻意分开的（HTTP 错误 vs 扫描归因，见 errors.py 的模块 docstring）。
    一个码同时在两边，前端就无从知道该用哪一棵 —— 而 `ErrorNotice` 的 `tree`
    参数默认是 `"http"`，于是会静默地取错文案。
    """
    both = set(messages["errors"].keys()) & set(messages["scanFailures"].keys())
    assert not both, f"这些码同时出现在 errors 与 scanFailures 里：{sorted(both)}"


# =============================================================================
# 二、每条文案自身的形状
# =============================================================================
@pytest.mark.parametrize("tree", ["errors", "scanFailures"])
def test_copy_entries_are_complete(messages: dict[str, Any], tree: str) -> None:
    """三段齐全、都是非空字符串。

    缺一段不会让前端崩，只会渲染出一个空段落 —— 而 `action`（该怎么办）恰好是
    用户唯一真正需要的那一段，它空掉的后果最重。
    """
    for code, entry in messages[tree].items():
        assert isinstance(entry, dict), f"{tree}.{code} 必须是对象"
        for field in ("title", "detail", "action"):
            value = entry.get(field)
            assert isinstance(value, str) and value.strip(), f"{tree}.{code}.{field} 缺失或为空"


@pytest.mark.parametrize("tree", ["errors", "scanFailures"])
def test_params_are_names_not_values(messages: dict[str, Any], tree: str) -> None:
    """`params` 是**参数名**的列表（snake_case 拉丁串），不是参数值。

    这条挡住的是"顺手把中文标签写进 params"那种写法：标签在 `paramLabels` 里，
    `params` 只负责说"这条错误要展示哪几个字段、按什么顺序"。
    """
    for code, entry in messages[tree].items():
        params = entry.get("params")
        if params is None:
            continue
        assert isinstance(params, list), f"{tree}.{code}.params 必须是数组"
        for name in params:
            assert isinstance(name, str) and name, f"{tree}.{code}.params 里有空项"
            assert name.isascii(), f"{tree}.{code}.params 里的 {name!r} 不是参数名"


def _referenced_param_names(messages: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for tree in ("errors", "scanFailures"):
        for entry in messages[tree].values():
            names.update(entry.get("params", []))
    return names


def test_every_referenced_param_has_a_label(messages: dict[str, Any]) -> None:
    labels = set(messages["paramLabels"].keys())
    missing = _referenced_param_names(messages) - labels
    assert not missing, (
        f"这些参数名没有中文标签，前端会直接把 snake_case 露在页面上：{sorted(missing)}"
    )


def test_no_orphan_param_labels(messages: dict[str, Any]) -> None:
    """没被任何一条错误引用的标签也算失败。

    孤儿标签的害处和孤儿码一样：它让人以为某个字段还会被展示。
    """
    labels = set(messages["paramLabels"].keys())
    orphans = labels - _referenced_param_names(messages)
    assert not orphans, f"这些 paramLabels 没有任何错误在用：{sorted(orphans)}"


# =============================================================================
# 三、`targetGuard.*` —— 第三棵机器码树（T8）
#
# 它不参与上面那组按 `tree` 参数化的测试：那些条目是 `{title, detail, action}` 三段的
# 错误卡片，而这里的每一条是**一个字符串** —— 渲染在输入框下面的一行提示里，或者一个
# 类别徽章上。硬套三段形状会逼出两个空字段，而空字段迟早会被人填上一句凑数的话。
#
# 码的权威源是 `services/target_guard.py` 与 `services/dns_resolver.py` 的枚举，
# 不是 `errors.py`：这些值出现在 `POST /api/targets/validate` 的 **200 正文**里，
# 一个也不是 HTTP 错误（见 `frontend/messages/README.md` 那一节）。
# =============================================================================
_GUARD_TREE = "targetGuard"

# `(子树名, 后端真实取值域)`。加一个枚举成员就必须在这里露出来 —— 这张表本身就是
# "别忘了写文案"的提醒，而不是一份可以偷偷落后的副本。
_GUARD_SUBTREES: tuple[tuple[str, frozenset[str]], ...] = (
    ("reasons", frozenset(item.value for item in RejectionReason)),
    ("categories", frozenset(item.value for item in TargetCategory)),
    ("requirements", frozenset(item.value for item in GuardRequirement)),
    ("optIn", frozenset(item.value for item in OptInFlag)),
    ("resolution", RESOLUTION_ERROR_CODES),
)


def _guard_subtree(messages: dict[str, Any], name: str) -> dict[str, Any]:
    subtree = messages[_GUARD_TREE][name]
    assert isinstance(subtree, dict), f"{_GUARD_TREE}.{name} 必须是对象"
    return subtree


@pytest.mark.parametrize(("name", "codes"), _GUARD_SUBTREES, ids=[n for n, _ in _GUARD_SUBTREES])
def test_every_guard_code_has_copy(
    messages: dict[str, Any], name: str, codes: frozenset[str]
) -> None:
    missing = codes - set(_guard_subtree(messages, name).keys())
    assert not missing, (
        f"这些码没有中文文案，去 zh-CN.json 的 {_GUARD_TREE}.{name} 里加：{sorted(missing)}"
    )


@pytest.mark.parametrize(("name", "codes"), _GUARD_SUBTREES, ids=[n for n, _ in _GUARD_SUBTREES])
def test_no_orphan_guard_copy(messages: dict[str, Any], name: str, codes: frozenset[str]) -> None:
    orphans = set(_guard_subtree(messages, name).keys()) - codes
    assert not orphans, f"{_GUARD_TREE}.{name} 里这些码后端已经不存在了：{sorted(orphans)}"


@pytest.mark.parametrize("name", [name for name, _ in _GUARD_SUBTREES] + ["notes"])
def test_guard_copy_entries_are_plain_sentences(messages: dict[str, Any], name: str) -> None:
    """每条都是非空字符串。**不许是对象** —— 那意味着有人在往这棵树上套错误卡片的形状。"""
    for code, value in _guard_subtree(messages, name).items():
        assert isinstance(value, str) and value.strip(), f"{_GUARD_TREE}.{name}.{code} 缺失或为空"


def test_loopback_note_names_the_rewrite_target(messages: dict[str, Any]) -> None:
    """环回目标的那句提示**必须点名** `host.docker.internal`。

    这是验收 3 的一条：Strix 会把 `127.0.0.1` 改写成 `host.docker.internal`
    （`scan_setup.py:51`），也就是沙箱访问的是**宿主机**上的服务。不说清这件事，
    用户会以为自己在测沙箱容器内部，然后对"什么都没测到"完全无法归因。

    码不从 `target_guard` 的私有常量 import，而是**跑一遍真实判定**拿出来的：
    直接 import `_LOOPBACK_NOTE_CODE` 只能证明"文案表和那个常量一致"，证明不了
    "这条判定真的会产出这个码"。中间那个 `assert note_code is not None` 就是
    pitfalls 条 36 要求的反面对照 —— 少了它，护栏哪天不再产出 note_code，
    下面的断言会因为"没有要检查的东西"而静默通过。
    """
    target = normalize_target("http://127.0.0.1:8080")
    verdict = evaluate_target(
        target,
        ("127.0.0.1",),
        allowlist=no_allowlist(),
        opt_in=OperatorOptIn(loopback=True),
    )
    note_code = verdict.note_code
    assert note_code is not None, (
        "环回目标没有产出 note_code —— 要检查的事根本没发生，这条断言等于空转。"
        "先去看 target_guard.evaluate_target 是不是改了。"
    )
    note = _guard_subtree(messages, "notes")[note_code]
    assert "host.docker.internal" in note, (
        f"{_GUARD_TREE}.notes.{note_code} 没有点名 host.docker.internal，"
        "用户会以为测的是沙箱容器内部的服务。"
    )


# 续跑的拒绝原因（`resumeRefusal.*`，T31b5）：`resume_unavailable` 卡片下面那一行。
# 码的权威源是 `services/scan_resume.RESUME_REFUSAL_REASONS`，不是 `errors.py`（它们是
# `params.reason` 的取值，不是 HTTP 码）；形状同 `targetGuard.*`：一条一句纯字符串。
def test_every_resume_refusal_reason_has_copy(messages: dict[str, Any]) -> None:
    copy = messages["resumeRefusal"]
    missing = set(RESUME_REFUSAL_REASONS) - set(copy)
    assert not missing, (
        f"这些 reason 没有中文文案，去 zh-CN.json 的 resumeRefusal 里加：{sorted(missing)}"
    )
    for reason, value in copy.items():
        assert isinstance(value, str) and value.strip(), f"resumeRefusal.{reason} 缺失或为空"


def test_no_orphan_resume_refusal_copy(messages: dict[str, Any]) -> None:
    orphans = set(messages["resumeRefusal"]) - set(RESUME_REFUSAL_REASONS)
    assert not orphans, f"resumeRefusal 里这些 reason 后端已经不存在了：{sorted(orphans)}"


# 模板的中文名与说明（`templates.*`／`templateNotes.*`）：后端只回 `template_id`，
# 加了模板忘了文案，向导上就是一行裸机器码。
@pytest.mark.parametrize("tree", ["templates", "templateNotes"])
def test_every_template_has_copy_and_no_orphans(messages: dict[str, Any], tree: str) -> None:
    ids = {template.template_id for template in TEMPLATES}
    copy = messages[tree]
    assert set(copy) == ids, (
        f"{tree} 与后端模板不一致：缺 {sorted(ids - set(copy))}，多 {sorted(set(copy) - ids)}"
    )
    for template_id, value in copy.items():
        assert isinstance(value, str) and value.strip(), f"{tree}.{template_id} 缺失或为空"


# =============================================================================
# 四、预算措辞
#
# 只断这两处具体的 key，不做全文扫描（理由见模块 docstring）。
# =============================================================================
_FORBIDDEN_BUDGET_WORDING = "绝不超过"


def test_budget_copy_never_promises_a_hard_ceiling(messages: dict[str, Any]) -> None:
    """`--max-budget-usd` 是**软上限**，UI 不许承诺"绝不超过"。

    事实：结算发生在每一轮之后，所以最后一轮会把总额顶出上限一点（这正是费用尺
    上那段斜纹画的东西）。承诺一个做不到的上限，第一次超出就把整个工具的可信度
    一起花掉了。
    """
    entry = messages["errors"]["budget_exceeds_ceiling"]
    for field in ("title", "detail", "action"):
        assert _FORBIDDEN_BUDGET_WORDING not in entry[field], (
            f"errors.budget_exceeds_ceiling.{field} 承诺了做不到的硬上限"
        )
    for key, value in messages["budget"].items():
        assert _FORBIDDEN_BUDGET_WORDING not in value, f"budget.{key} 承诺了做不到的硬上限"


def test_budget_copy_says_it_stops(messages: dict[str, Any]) -> None:
    """反面：光是"没写禁语"不够，还得**真的**把"达到上限后停止"说出来。

    这条是为了防住一种很容易发生的退化 —— 有人为了让上面那条通过，直接把整句话
    删掉。那样禁语确实不见了，但用户也不知道钱花到上限会发生什么。
    """
    assert "停止" in messages["budget"]["stopsAt"]


# =============================================================================
# 五、`systemStatus.blockers.*` —— 第四棵机器码树（T3）
#
# 码的权威源是 `services/system_status.py` 的 `ALL_BLOCKER_CODES`，**不是** `errors.py`：
# 这些值出现在 `GET /api/system/status` 的 **200 正文**的 `blockers` 里，一个也不是 HTTP
# 错误（那个接口永远 200，见 routes/system.py 第 3 条）。混进 `errors.*` 就会有人拿
# `sandbox_network_missing` 去 `raise`，而没有任何接口会用它做响应码。
#
# 条目是**纯字符串**，与 `targetGuard.*` 同一形状（渲染在首页侧栏那一行的右侧，
# 不是错误卡片），所以不参与按 `tree` 参数化的那组三段测试。
# =============================================================================
_STATUS_TREE = "systemStatus"


def _blocker_copy(messages: dict[str, Any]) -> dict[str, Any]:
    subtree = messages[_STATUS_TREE]["blockers"]
    assert isinstance(subtree, dict), f"{_STATUS_TREE}.blockers 必须是对象"
    return subtree


def test_every_blocker_code_has_copy(messages: dict[str, Any]) -> None:
    missing = set(ALL_BLOCKER_CODES) - set(_blocker_copy(messages).keys())
    assert not missing, (
        f"这些就绪阻断码没有中文文案，去 zh-CN.json 的 {_STATUS_TREE}.blockers 里加："
        f"{sorted(missing)}"
    )


def test_no_orphan_blocker_copy(messages: dict[str, Any]) -> None:
    orphans = set(_blocker_copy(messages).keys()) - set(ALL_BLOCKER_CODES)
    assert not orphans, f"{_STATUS_TREE}.blockers 里这些码后端已经不存在了：{sorted(orphans)}"


def test_blocker_copy_entries_are_plain_sentences(messages: dict[str, Any]) -> None:
    """每条都是非空字符串，**不许是对象**。

    它渲染在侧栏那一行的右侧（`ui/Rows` 的 `value`），只有一个字符串的位置。
    套成 `{title, detail, action}` 的人是在把它当错误卡片用 —— 那种展开的修复指引
    属于 T26 的诊断页，到时加一棵 `systemStatus.fixes.*` 兄弟子树，不要改这一棵的形状。
    """
    for code, value in _blocker_copy(messages).items():
        assert isinstance(value, str) and value.strip(), (
            f"{_STATUS_TREE}.blockers.{code} 缺失或为空"
        )


# "阻断码不许和 HTTP 错误码重名"**刻意不在这里测** ——
# `test_system_status.py::test_blocker_codes_are_disjoint_from_http_error_codes` 已经测了，
# 而上面那两条双向比对已经把"文案树 == ALL_BLOCKER_CODES"钉死，
# 在这里再写一条就是同一条不变式的第三个副本（`agent-rules.md` §十.4）。


# =============================================================================
# 修复指引：`systemStatus.fixes.*`（T26）
#
# 与上面那棵 `blockers` 是**兄弟**，一码一条，形状同样是纯字符串。分工：
# `blockers.<code>` 是侧栏那一行右侧的短标签（"哪一项没过"），
# `fixes.<code>` 是它下方可展开区块里的一句话（"那我该怎么办"）。
#
# 这两条双向比对是 T26 唯一的自动闸门 —— 那个区块是纯前端的（前端不写测试），
# 少一条 `fixes` 文案时 `t()` 会把 key 原样渲染到用户眼前，而阻断项面板恰恰是
# 机器没配好时才打开的地方，靠人去撞不现实。
# =============================================================================


def _fix_copy(messages: dict[str, Any]) -> dict[str, Any]:
    subtree = messages[_STATUS_TREE]["fixes"]
    assert isinstance(subtree, dict), f"{_STATUS_TREE}.fixes 必须是对象"
    return subtree


def test_every_blocker_code_has_fix_copy(messages: dict[str, Any]) -> None:
    missing = set(ALL_BLOCKER_CODES) - set(_fix_copy(messages).keys())
    assert not missing, (
        f"这些就绪阻断码没有修复指引，去 zh-CN.json 的 {_STATUS_TREE}.fixes 里加：{sorted(missing)}"
    )


def test_no_orphan_fix_copy(messages: dict[str, Any]) -> None:
    orphans = set(_fix_copy(messages).keys()) - set(ALL_BLOCKER_CODES)
    assert not orphans, f"{_STATUS_TREE}.fixes 里这些码后端已经不存在了：{sorted(orphans)}"


# =============================================================================
# 实时流帧里的码（`wsNotices.*`）
#
# 第五棵码树：`notice` 帧的三个码 + `error{stream_lagged}`。它们既不是 HTTP 码也不是
# 扫描归因码，所以不进 `errors.*`／`scanFailures.*`（理由在 `routes/stream.py` 的
# `STREAM_LAGGED` docstring）。`error{not_found}` 刻意不在这里：它与 HTTP 的 `not_found`
# 是同一件事，前端复用 `errors.not_found` 那张卡片。
# 条目是纯字符串（渲染成面板顶上的一行提示，不是错误卡片）。
# =============================================================================
_WS_NOTICE_TREE = "wsNotices"
_WS_NOTICE_CODES = frozenset(
    {NOTICE_SCREENSHOT_ELIDED, NOTICE_CONTEXT_COMPACTED, NOTICE_STREAM_RESYNCED, STREAM_LAGGED}
)


def test_every_ws_notice_code_has_copy(messages: dict[str, Any]) -> None:
    missing = _WS_NOTICE_CODES - set(messages[_WS_NOTICE_TREE].keys())
    assert not missing, (
        f"这些实时流帧码没有中文文案，去 zh-CN.json 的 {_WS_NOTICE_TREE} 里加：{sorted(missing)}"
    )


def test_no_orphan_ws_notice_copy(messages: dict[str, Any]) -> None:
    orphans = set(messages[_WS_NOTICE_TREE].keys()) - _WS_NOTICE_CODES
    assert not orphans, f"{_WS_NOTICE_TREE} 里这些码后端已经不发了：{sorted(orphans)}"


def test_ws_notice_copy_entries_are_plain_sentences(messages: dict[str, Any]) -> None:
    for code, value in messages[_WS_NOTICE_TREE].items():
        assert isinstance(value, str) and value.strip(), f"{_WS_NOTICE_TREE}.{code} 缺失或为空"
