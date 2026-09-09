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
# 三、预算措辞
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
