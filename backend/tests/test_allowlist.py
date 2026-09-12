"""授权清单（T8：`services/allowlist.py` + `routes/allowlist.py`）。

# 这个文件盯的是一句话：**`enforce` 不许被静默降成 `advisory`**

那件事发生时界面上什么都不会变 —— 用户看不到区别，只是从此公网随便扫。所以本文件里
最重要的不是"好文件能被读出来"（那条也在），而是三种坏情况各自的落点：

  · 文件**缺席** → `advisory`、什么都不放行。这是合法状态（第一次启动就是这样）。
  · 文件**存在但坏** → 保留内存里最后一次成功加载的配置，快照上挂 `error`。
  · **冷启动就坏**（内存里没有"上一次好的"）→ 失败关闭（`enforce` + 未命中）。

前两种的区别是"我还没用这个功能"与"我在用，但它此刻不可读"，判定结论完全相反。

# 热重载那两条测试为什么要自己 `os.utime`

指纹是 `(st_mtime_ns, st_size)`，比 `PLAN.md` 说的 mtime 严一档。要证明"纳秒那一半
真的在起作用"，就必须构造出"同一秒内改了两次"——靠 `time.sleep` 是碰运气，靠
`os.utime(..., ns=)` 是确定的。同理，要证明"大小那一半在起作用"，就得把 mtime 按回去。
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import create_app
from app.routes.auth import EXEMPT_PATHS, SESSION_COOKIE_NAME
from app.services.allowlist import (
    AllowlistConfig,
    AllowlistEntry,
    AllowlistFileErrorCode,
    AllowlistSnapshot,
    AllowlistStore,
    decide,
    effective_mode,
    parse_allowlist_text,
)
from app.services.target_guard import AllowlistDecision, AllowlistMode
from app.settings import Settings
from tests.conftest import PASSWORD, USERNAME

ALLOWLIST_PATH = "/api/allowlist"
ENTRIES_PATH = "/api/allowlist/entries"

TODAY = date(2026, 9, 12)


def make_entry(label: str = "预生产", **overrides: object) -> AllowlistEntry:
    fields: dict[str, object] = {
        "label": label,
        "owner": "安全组",
        "authorization_ref": "TICKET-1",
        "hosts": ("example.com",),
    }
    fields.update(overrides)
    return AllowlistEntry.model_validate(fields)


def entry_payload(label: str = "预生产", **overrides: object) -> dict[str, object]:
    """`POST /api/allowlist/entries` 的请求体。`BoundaryModel` 是 `extra="forbid"`，
    所以这里只许出现真实字段 —— 拼错一个字段名会 422，那正是我们要的。
    """
    payload: dict[str, object] = {
        "label": label,
        "owner": "安全组",
        "authorization_ref": "TICKET-1",
        "hosts": ["example.com"],
    }
    payload.update(overrides)
    return payload


# =============================================================================
# 一、条目与配置的字段校验
# =============================================================================
@pytest.mark.parametrize(
    "label",
    [
        "a/b",  # `/` 会让 DELETE 的路径段无法表达这条 label
        " 前导空白",
        "尾随空白 ",
        "带\x00控制字符",
        "带\n换行",
    ],
)
def test_label_shapes_that_break_the_delete_route_are_rejected(label: str) -> None:
    """label 是 `DELETE /api/allowlist/entries/{label}` 的路径段。

    这些形状被拦掉的代价是"一份手写的清单可能因此被判 invalid_shape"，
    换来的是"界面上显示着的条目一定删得掉"。反过来那种失败只能靠改文件解决 ——
    而那时用户看着界面上那一条，点删除没有任何反应。
    """
    with pytest.raises(ValidationError):
        make_entry(label)


def test_an_entry_that_can_never_match_is_rejected() -> None:
    """既没有 hosts 也没有 cidrs 的条目长得像一条生效的授权，实际永不命中。"""
    with pytest.raises(ValidationError):
        make_entry(hosts=())


def test_host_patterns_are_normalized() -> None:
    entry = make_entry(hosts=("EXAMPLE.COM.", "*.Corp.Example.com"))
    assert entry.hosts == ("example.com", "*.corp.example.com")


@pytest.mark.parametrize(
    "pattern",
    [
        "例子.中国",  # 非 ASCII：不许在这里做第二份 IDNA 实现
        "*.*.example.com",  # 通配只支持 `*.` 前缀一种
        "a*.example.com",
        "*",
        "",
        "has space.com",
        "a" * 254,
    ],
)
def test_bad_host_patterns_are_rejected(pattern: str) -> None:
    with pytest.raises(ValidationError):
        make_entry(hosts=(pattern,))


def test_non_ascii_host_error_points_at_punycode() -> None:
    """报错要告诉操作者填什么。

    `xn--` 那一串正是界面在目标旁边并排显示的东西（`NormalizedTarget.host`），
    所以这句提示是可执行的，不是"格式不对"那种空话。
    """
    with pytest.raises(ValidationError, match="punycode"):
        make_entry(hosts=("例子.中国",))


def test_cidr_must_be_a_network_not_a_host() -> None:
    """`strict=True`：`10.0.0.1/8` 被拒，不是静默变成 `10.0.0.0/8`。

    静默纠正会让操作者以为自己授权的是那一台主机。
    """
    with pytest.raises(ValidationError):
        make_entry(cidrs=("10.0.0.1/8",), hosts=())
    assert make_entry(cidrs=("10.0.0.0/8",), hosts=()).cidrs == ("10.0.0.0/8",)


def test_forbidden_paths_must_be_path_prefixes() -> None:
    with pytest.raises(ValidationError):
        make_entry(forbidden_paths=("admin",))
    assert make_entry(forbidden_paths=("/admin",)).forbidden_paths == ("/admin",)


def test_duplicate_labels_are_rejected() -> None:
    """label 是 DELETE 的 id，也是审计里"改了哪一条"的唯一线索。"""
    with pytest.raises(ValidationError):
        AllowlistConfig(entries=(make_entry("同名"), make_entry("同名", hosts=("b.com",))))


def test_mode_defaults_to_enforce() -> None:
    """文件里忘了写 mode → 倒向更严的一侧。

    与"文件不存在时是 advisory"不矛盾：文件存在意味着操作者在用这份清单。
    """
    assert AllowlistConfig().mode is AllowlistMode.ENFORCE


# =============================================================================
# 二、匹配 —— 纯函数，`today` 由调用方传入
# =============================================================================
def loaded(config: AllowlistConfig, tmp_path: Path) -> AllowlistSnapshot:
    """把配置**经过文件往返一趟**再拿快照。

    直接构造 `AllowlistSnapshot(config=..., ...)` 会更短，但那样测的就只是 `_match`。
    走一遍 `write` + `current` 顺带钉住"写出去的能被读回来"，而序列化那一层正是
    `expires`（date）与 `hosts`（tuple）最容易走形的地方。
    """
    store = AllowlistStore(tmp_path / "config" / "allowlist.yaml")
    store.write(config)
    return store.current()


def decide_for(
    config: AllowlistConfig,
    *,
    host: str,
    addresses: tuple[str, ...] = ("93.184.216.34",),
    today: date = TODAY,
    tmp_path: Path,
) -> AllowlistDecision:
    return decide(loaded(config, tmp_path), host=host, addresses=addresses, today=today)


def test_explicit_host_beats_wildcard(tmp_path: Path) -> None:
    """`PLAN.md` 原话：最长后缀优先，显式主机胜过通配。

    这条为什么重要：命中的 `entry_label` 会进审计。两条都能命中时选错一条，
    审计记录里写的就是另一份授权的编号。
    """
    config = AllowlistConfig(
        entries=(
            make_entry("通配", hosts=("*.example.com",)),
            make_entry("显式", hosts=("api.example.com",)),
        )
    )
    result = decide_for(config, host="api.example.com", tmp_path=tmp_path)
    assert result.entry_label == "显式"


def test_longer_wildcard_suffix_wins(tmp_path: Path) -> None:
    config = AllowlistConfig(
        entries=(
            make_entry("短", hosts=("*.example.com",)),
            make_entry("长", hosts=("*.corp.example.com",)),
        )
    )
    result = decide_for(config, host="api.corp.example.com", tmp_path=tmp_path)
    assert result.entry_label == "长"


def test_explicit_host_beats_a_cidr(tmp_path: Path) -> None:
    """主机名是操作者写下的**意图**，CIDR 是对解析结果的兜底描述。"""
    config = AllowlistConfig(
        entries=(
            make_entry("网段", hosts=(), cidrs=("93.184.216.0/24",)),
            make_entry("主机", hosts=("example.com",)),
        )
    )
    result = decide_for(config, host="example.com", tmp_path=tmp_path)
    assert result.entry_label == "主机"


@pytest.mark.parametrize("host", ["a.b.example.com", "example.com"])
def test_wildcard_is_single_level(host: str, tmp_path: Path) -> None:
    """`*.example.com` 只命中一层。

    多层生效会把一整棵子域树都授权掉 —— 那通常不是写它的人的意思，而"通常"在
    授权这件事上不够。
    """
    config = AllowlistConfig(entries=(make_entry("通配", hosts=("*.example.com",)),))
    result = decide_for(config, host=host, tmp_path=tmp_path)
    assert result.matched is False


def test_cidr_matches_the_resolved_addresses(tmp_path: Path) -> None:
    config = AllowlistConfig(entries=(make_entry("网段", hosts=(), cidrs=("10.0.0.0/8",)),))
    result = decide_for(config, host="intranet.local", addresses=("10.1.2.3",), tmp_path=tmp_path)
    assert result.matched is True


def test_ties_land_on_the_earlier_entry(tmp_path: Path) -> None:
    """平局稳定地落在文件里靠前的那一条上。

    稳定不是洁癖 —— `entry_label` 会进审计，一个随机漂移的 label 会让审计记录无法复现。
    """
    config = AllowlistConfig(
        entries=(
            make_entry("第一条", hosts=("example.com",)),
            make_entry("第二条", hosts=("example.com",)),
        )
    )
    result = decide_for(config, host="example.com", tmp_path=tmp_path)
    assert result.entry_label == "第一条"


def test_expired_entry_does_not_match(tmp_path: Path) -> None:
    """过期 = **没命中**，不是"命中了但被拒"。

    后者会让 `entry_label` 指向一条已经失效的授权，而那个 label 会进审计 ——
    读审计的人会以为当时有一份有效授权。
    """
    config = AllowlistConfig(entries=(make_entry("过期", expires=date(2026, 9, 11)),))
    result = decide_for(config, host="example.com", tmp_path=tmp_path)
    assert result.matched is False
    assert result.entry_label is None


def test_expiry_includes_the_last_day(tmp_path: Path) -> None:
    """`expires` 是最后一个**有效**日期，含当天。差一天在授权上就是越权。"""
    config = AllowlistConfig(entries=(make_entry("今天到期", expires=TODAY),))
    result = decide_for(config, host="example.com", tmp_path=tmp_path)
    assert result.matched is True


def test_expired_entry_still_lets_a_later_one_match(tmp_path: Path) -> None:
    """过期那条被跳过之后，别的条目照样能命中 —— 它不是"整份清单作废"。"""
    config = AllowlistConfig(
        entries=(
            make_entry("过期的显式", hosts=("example.com",), expires=date(2020, 1, 1)),
            make_entry("有效的通配", hosts=("*.com",)),
        )
    )
    result = decide_for(config, host="example.com", tmp_path=tmp_path)
    assert result.matched is True, "过期那条被跳过了，但 *.com 仍然命中 example.com"
    assert result.entry_label == "有效的通配", "命中的必须是还有效的那条，不能是过期的那条"


def test_matched_entry_carries_its_opt_in_flags(tmp_path: Path) -> None:
    """`allow_reserved` **只能**从文件来（界面上永不提供），所以它必须能穿过来。"""
    config = AllowlistConfig(
        entries=(
            make_entry(
                "运营商网段",
                hosts=(),
                cidrs=("100.64.0.0/10",),
                allow_reserved=True,
                allow_private=True,
            ),
        )
    )
    result = decide_for(config, host="cgnat.local", addresses=("100.64.1.1",), tmp_path=tmp_path)
    assert result.allow_reserved is True
    assert result.allow_private is True
    assert result.allow_loopback is False


def test_advisory_mode_is_carried_through(tmp_path: Path) -> None:
    config = AllowlistConfig(mode=AllowlistMode.ADVISORY, entries=(make_entry(),))
    result = decide_for(config, host="nothing.example.org", tmp_path=tmp_path)
    assert result.mode is AllowlistMode.ADVISORY
    assert result.matched is False


# =============================================================================
# 三、三种"没有配置" —— 本文件最重要的一组
# =============================================================================
def test_missing_file_is_advisory_and_matches_nothing(tmp_path: Path) -> None:
    """文件缺席是**合法状态**：控制台第一次启动就是这样。

    `enforce` + 空清单等于什么都不准扫，界面开箱即死。
    """
    store = AllowlistStore(tmp_path / "config" / "allowlist.yaml")
    snapshot = store.current()
    assert snapshot.config is None
    assert snapshot.file_present is False
    assert snapshot.error is None
    assert snapshot.stale is False
    assert effective_mode(snapshot) is AllowlistMode.ADVISORY

    result = decide(snapshot, host="example.com", addresses=("93.184.216.34",), today=TODAY)
    assert result.mode is AllowlistMode.ADVISORY
    assert result.matched is False


def test_broken_file_on_cold_start_fails_closed(tmp_path: Path) -> None:
    """**这一条是本文件的核心。**

    冷启动就读不懂文件时内存里没有"上一次好的"，此时必须是 `enforce` + 未命中 ——
    也就是所有公网目标一律 `not_in_allowlist`。写成 advisory 的话，"手滑把文件写坏"
    和"故意删掉限制"的效果完全一样。
    """
    path = tmp_path / "config" / "allowlist.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("mode: [这不是合法的 mode\n", encoding="utf-8")

    snapshot = AllowlistStore(path).current()
    assert snapshot.config is None
    assert snapshot.file_present is True
    assert snapshot.error is not None
    assert snapshot.stale is False, "内存里没有旧配置，所以不是 stale，是彻底没有"
    assert effective_mode(snapshot) is AllowlistMode.ENFORCE

    result = decide(snapshot, host="example.com", addresses=("93.184.216.34",), today=TODAY)
    assert result.mode is AllowlistMode.ENFORCE
    assert result.matched is False


def test_broken_file_keeps_the_last_good_config(tmp_path: Path) -> None:
    """文件坏掉之后**继续按上一次成功加载的配置判定**，并如实报告 `error`。

    绝不因为解析失败就退回 advisory。
    """
    path = tmp_path / "config" / "allowlist.yaml"
    store = AllowlistStore(path)
    store.write(AllowlistConfig(entries=(make_entry("原来的"),)))

    path.write_text("这不是: [一份合法的 YAML\n", encoding="utf-8")
    snapshot = store.current()
    assert snapshot.error is not None
    assert snapshot.stale is True
    assert snapshot.config is not None
    assert effective_mode(snapshot) is AllowlistMode.ENFORCE

    result = decide(snapshot, host="example.com", addresses=("93.184.216.34",), today=TODAY)
    assert result.entry_label == "原来的", "坏文件不该让还记着的授权失效"


def test_deleting_the_file_drops_the_config(tmp_path: Path) -> None:
    """删文件是操作者说"我不用这份清单了"的合法方式。

    继续按一份磁盘上已经不存在的配置放行/拒绝，会让界面显示的内容和实际生效的规则
    对不上，没法排查。这不是绕过限制的口子：能删这个文件的人同样能重写它的内容。
    """
    path = tmp_path / "config" / "allowlist.yaml"
    store = AllowlistStore(path)
    store.write(AllowlistConfig(entries=(make_entry(),)))
    path.unlink()

    snapshot = store.current()
    assert snapshot.config is None
    assert snapshot.file_present is False
    assert effective_mode(snapshot) is AllowlistMode.ADVISORY


# =============================================================================
# 四、解析器 —— 失败是返回值，不是异常
# =============================================================================
def test_syntax_error_reports_a_line_number() -> None:
    result = parse_allowlist_text("mode: enforce\nentries: [\n")
    assert not isinstance(result, AllowlistConfig)
    assert result.code is AllowlistFileErrorCode.SYNTAX_ERROR
    assert result.line is not None and result.line >= 1


def test_empty_file_is_enforce_with_no_entries() -> None:
    """空文件（或只有注释）→ 文件存在 = 在用这份清单 → `enforce` + 零条目 = 公网一律拒。"""
    result = parse_allowlist_text("# 只有一行注释\n")
    assert isinstance(result, AllowlistConfig)
    assert result.mode is AllowlistMode.ENFORCE
    assert result.entries == ()


@pytest.mark.parametrize("text", ["- 一个列表\n", "就是一个字符串\n", "42\n"])
def test_non_mapping_top_level_is_invalid_shape(text: str) -> None:
    result = parse_allowlist_text(text)
    assert not isinstance(result, AllowlistConfig)
    assert result.code is AllowlistFileErrorCode.INVALID_SHAPE


def test_unknown_field_is_invalid_shape() -> None:
    """`BoundaryModel` 是 `extra="forbid"`：拼错的字段名不许被静默忽略。

    静默忽略的具体后果：把 `expires` 拼成 `expiry` 的条目会变成一条**永不过期**的授权。
    """
    result = parse_allowlist_text("mode: enforce\nentries: []\nexpiry: 2026-01-01\n")
    assert not isinstance(result, AllowlistConfig)
    assert result.code is AllowlistFileErrorCode.INVALID_SHAPE


def test_yaml_tags_are_not_executed() -> None:
    """`safe_load` 而不是 `load`。

    这个文件可经 `PUT /api/allowlist` 写入 —— 鉴权之后的 RCE 依然是 RCE。
    `yaml.load` 的默认 Loader 会执行 `!!python/object/apply:os.system`。
    """
    result = parse_allowlist_text("mode: !!python/object/apply:os.system ['echo hi']\n")
    assert not isinstance(result, AllowlistConfig), "safe_load 必须拒绝 python 标签"


def test_parse_errors_do_not_echo_the_input() -> None:
    """错误里不带文件内容。

    `ValidationError` 的正文会把出错字段的输入值原样嵌进消息，而这条消息的去向包括
    日志与 HTTP 正文。这里的返回值只有码与行号两个字段 —— 结构上没有回显的位置。
    """
    secret_ish = "AKIAIOSFODNN7EXAMPLE"
    result = parse_allowlist_text(f"mode: {secret_ish}\n")
    assert not isinstance(result, AllowlistConfig)
    assert secret_ish not in repr(result)


# =============================================================================
# 五、热重载与原子写
# =============================================================================
def test_unchanged_file_is_not_reparsed(tmp_path: Path) -> None:
    """指纹没变就走快路径，**连读都不读**。

    断言用对象同一性：快路径原样返回上一次那个快照对象。这条检查跑在每一次目标校验
    和每一次扫描启动之前，一个 `stat()` 与一次 `open+read` 的差距在那个位置上
    是可以感知的。
    """
    store = AllowlistStore(tmp_path / "config" / "allowlist.yaml")
    store.write(AllowlistConfig(entries=(make_entry(),)))
    assert store.current() is store.current()


def config_text(label: str) -> str:
    """两个 label 等长，所以两份文本的**字节数完全相同** —— 这是下面那条测试的前提。"""
    return (
        "mode: enforce\n"
        "entries:\n"
        f"  - label: {label}\n"
        "    owner: 安全组\n"
        "    authorization_ref: TICKET-1\n"
        "    hosts: [example.com]\n"
    )


def test_two_edits_inside_one_second_are_both_seen(tmp_path: Path) -> None:
    """同一秒内改两次，第二次也必须生效。

    `st_mtime` 是**秒**粒度，而在编辑器里改两次的间隔通常小于一秒 —— 那时第二次改动
    会被当成"文件没变"，症状是"我把条目删了但还是能扫"。这里用 `os.utime(..., ns=)`
    把两次改动钉在同一秒的两个不同纳秒上，让这件事确定地发生而不是靠 sleep 碰运气。
    """
    path = tmp_path / "config" / "allowlist.yaml"
    path.parent.mkdir(parents=True)
    second = 1_757_000_000

    path.write_text(config_text("甲"), encoding="utf-8")
    os.utime(path, ns=(second * 10**9, second * 10**9))
    store = AllowlistStore(path)
    first = store.current()
    assert first.config is not None
    assert first.config.entries[0].label == "甲"

    path.write_text(config_text("乙"), encoding="utf-8")
    os.utime(path, ns=(second * 10**9 + 500_000, second * 10**9 + 500_000))
    # 自证前提：秒粒度看不出区别，大小也没变 —— 只有纳秒能救。
    assert int(path.stat().st_mtime) == second
    assert path.stat().st_size == len(config_text("甲").encode("utf-8"))

    second_read = store.current()
    assert second_read.config is not None
    assert second_read.config.entries[0].label == "乙", "同一秒内的第二次改动被漏掉了"


def test_a_size_change_is_seen_even_if_mtime_is_pushed_back(tmp_path: Path) -> None:
    """时间戳被按回去（`touch -t`、rsync、某些编辑器）时，靠大小兜住。"""
    path = tmp_path / "config" / "allowlist.yaml"
    path.parent.mkdir(parents=True)
    stamp = 1_757_000_000 * 10**9

    path.write_text(config_text("甲"), encoding="utf-8")
    os.utime(path, ns=(stamp, stamp))
    store = AllowlistStore(path)
    assert store.current().config is not None

    path.write_text(config_text("甲乙丙丁"), encoding="utf-8")
    os.utime(path, ns=(stamp, stamp))  # 时间戳一模一样
    assert path.stat().st_mtime_ns == stamp

    reloaded = store.current()
    assert reloaded.config is not None
    assert reloaded.config.entries[0].label == "甲乙丙丁"


def test_write_round_trips_through_the_file(tmp_path: Path) -> None:
    """写出去的东西必须能被自己读回来。

    这两条路径（`safe_dump` 与 `safe_load` + Pydantic）分开写就有分歧的余地，
    而分歧的表现是"界面上保存成功，重启后那条授权不见了"。
    """
    path = tmp_path / "config" / "allowlist.yaml"
    store = AllowlistStore(path)
    config = AllowlistConfig(
        mode=AllowlistMode.ADVISORY,
        entries=(
            make_entry("客户预生产", hosts=("*.corp.example.com",), expires=date(2026, 12, 31)),
            make_entry("内网靶场", hosts=(), cidrs=("10.0.0.0/8",), allow_private=True),
        ),
    )
    store.write(config)
    assert parse_allowlist_text(path.read_text(encoding="utf-8")) == config


def test_written_file_is_owner_only_and_carries_the_header(tmp_path: Path) -> None:
    """0600 + 一句说明。

    那句说明要交代两件只有文件才知道的事：编辑会整份重写（手写注释会丢）、
    `allow_reserved` 只能在这里改。
    """
    path = tmp_path / "config" / "allowlist.yaml"
    AllowlistStore(path).write(AllowlistConfig())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#")
    assert "allow_reserved" in text


def test_write_keeps_chinese_readable_and_leaves_no_temp_file(tmp_path: Path) -> None:
    path = tmp_path / "config" / "allowlist.yaml"
    AllowlistStore(path).write(AllowlistConfig(entries=(make_entry("客户预生产"),)))
    assert "客户预生产" in path.read_text(encoding="utf-8"), "中文被转义了，这个文件是给人读的"
    assert list(path.parent.glob("*.tmp*")) == [], "留下了临时文件，下一次写入的 O_EXCL 会失败"


def test_write_is_immediately_visible_without_a_fingerprint_check(tmp_path: Path) -> None:
    """经界面写入之后**立刻**生效，不依赖指纹比较。

    这就是"时间戳粒度"那条残余风险在我们自己的写路径上不成立的原因。
    """
    store = AllowlistStore(tmp_path / "config" / "allowlist.yaml")
    snapshot = store.write(AllowlistConfig(entries=(make_entry("刚写的"),)))
    assert snapshot.config is not None
    assert snapshot.config.entries[0].label == "刚写的"
    assert snapshot.error is None
    assert snapshot.file_present is True


# =============================================================================
# 六、路由
# =============================================================================
@pytest.fixture
def app(settings: Settings, auth_file: Path, restore_logging: None) -> FastAPI:
    """真实应用。`auth_file` 与那两个凭据常量在 conftest 里（第三次出现时搬过去的）。

    这里**什么都不替换**：`AllowlistStore` 读写的是 `tmp_path` 下的真文件，审计写的是
    `tmp_path` 下的真库与真 ndjson。本任务要证明的事全都发生在磁盘上，替身会把它们
    一起替掉。这也是本文件不需要 DNS 替身的原因 —— 授权清单这一路完全不碰网络。
    """
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    # base_url 必须是 https：会话 cookie 带 `Secure`，http 下 TestClient 不会回传它。
    with TestClient(app, base_url="https://testserver") as test_client:
        test_client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
        assert test_client.cookies.get(SESSION_COOKIE_NAME), "登录没成功，后面的断言会全是 401"
        yield test_client


def audit_entries(settings: Settings) -> list[dict[str, Any]]:
    """ndjson 镜像里的全部记录，解析成对象。

    路由层的审计断言读**镜像**而不是表：表里那一列是 JSON 字符串，多一层
    `json.loads` 只会让断言更绕，而"表和镜像内容一致"已经由 `test_audit.py` 钉住了。
    这里只额外数一下表的行数（`audit_row_count`），确认双写的另一半也真的发生了。

    `dict[str, Any]`：ndjson 的 `detail` 是任意嵌套的对象，给它编精确类型会在下一次
    往 detail 里加字段时失配，而每处取值都自己检查了类型。
    """
    files = sorted(settings.audit_dir.glob("*.ndjson"))
    lines = [line for path in files for line in path.read_text(encoding="utf-8").splitlines()]
    parsed: list[dict[str, Any]] = []
    for line in lines:
        item = json.loads(line)
        assert isinstance(item, dict)
        parsed.append(item)
    return parsed


def last_detail(settings: Settings) -> dict[str, Any]:
    entries = audit_entries(settings)
    assert entries, "一条审计都没有 —— 下面的断言本来要检查的事根本没发生"
    detail = entries[-1]["detail"]
    assert isinstance(detail, dict)
    return detail


def audit_row_count(settings: Settings) -> int:
    """从**另一个连接**读表。应用那一侧每个事务都提交了，所以这里看得见。"""
    conn = sqlite3.connect(settings.db_path)
    try:
        return int(conn.execute("SELECT count(*) FROM audit_log").fetchone()[0])
    finally:
        conn.close()


def test_allowlist_routes_need_a_session(app: FastAPI) -> None:
    """没登录一律 401。

    `GET` 的正文是一份内网主机名与授权编号清单（现成的侦察结果），`PUT` 能直接
    把 `enforce` 改成 `advisory`。两者都绝不能进免鉴权名单。
    """
    with TestClient(app, base_url="https://testserver") as anonymous:
        for method, path in (
            ("GET", ALLOWLIST_PATH),
            ("PUT", ALLOWLIST_PATH),
            ("POST", ENTRIES_PATH),
            ("DELETE", f"{ENTRIES_PATH}/x"),
        ):
            # 走 `request()` 而不是 `anonymous.get(...)`：`TestClient.get` 没有 `json=`
            # 参数（httpx 不给 GET/DELETE 带 body 的快捷方式）。四个动词必须**同一种**
            # 调用方式，否则"GET 那条到底带没带 body"会成为一个看不出来的差异。
            response = anonymous.request(method, path, json={})
            assert response.status_code == 401, f"{method} {path} 没要求登录"
            assert response.json()["code"] == "unauthenticated"


def test_allowlist_paths_are_not_exempt() -> None:
    """上面那条测的是"现在是对的"，这条测的是"以后也别加进去"。"""
    for path in (ALLOWLIST_PATH, ENTRIES_PATH):
        assert path not in EXEMPT_PATHS


def test_get_on_a_fresh_machine(client: TestClient) -> None:
    body = client.get(ALLOWLIST_PATH).json()
    assert body["config"] is None
    assert body["file_present"] is False
    assert body["stale"] is False
    assert body["file_error"] is None
    assert body["effective_mode"] == "advisory"


def test_get_writes_no_audit(client: TestClient, settings: Settings) -> None:
    """只读接口不写审计。审计是"谁改了授权"，不是访问日志。"""
    client.get(ALLOWLIST_PATH)
    assert audit_entries(settings) == []
    assert audit_row_count(settings) == 0


def test_put_replaces_and_audits(client: TestClient, settings: Settings) -> None:
    payload = {"mode": "enforce", "entries": [entry_payload("客户预生产")]}
    response = client.put(ALLOWLIST_PATH, json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["effective_mode"] == "enforce"
    assert body["file_present"] is True
    assert [item["label"] for item in body["config"]["entries"]] == ["客户预生产"]
    # 真的落盘了，不只是内存 —— 而且落下去的东西能被自己的解析器读回来。
    assert isinstance(
        parse_allowlist_text(settings.allowlist_path.read_text(encoding="utf-8")),
        AllowlistConfig,
    )

    entries = audit_entries(settings)
    assert len(entries) == 1
    assert audit_row_count(settings) == 1, "只写了 ndjson，没写表 —— 双写断了一半"
    assert entries[0]["event"] == "allowlist.changed"
    assert entries[0]["actor"] == USERNAME, "审计里没有操作者"
    detail = last_detail(settings)
    assert detail["operation"] == "replace"
    assert detail["added"] == ["客户预生产"]


def test_put_records_the_diff_not_the_whole_config(client: TestClient, settings: Settings) -> None:
    """审计正文记的是**差异**。整份配置在文件里就有，"这次改了什么"只有这一刻知道。"""
    client.put(ALLOWLIST_PATH, json={"mode": "enforce", "entries": [entry_payload("甲")]})
    client.put(ALLOWLIST_PATH, json={"mode": "enforce", "entries": [entry_payload("乙")]})
    detail = last_detail(settings)
    assert detail["added"] == ["乙"]
    assert detail["removed"] == ["甲"], "被删掉的那条也要记下来"
    assert detail["labels_truncated"] is False


def test_put_with_duplicate_labels_is_422_and_writes_nothing(
    client: TestClient, settings: Settings
) -> None:
    """**坏配置写不进文件。**

    `PUT` 的请求体类型就是 `AllowlistConfig`，所以校验发生在路由函数被调用之前 ——
    "先写文件再校验"这个顺序在结构上不可能出现。
    """
    payload = {
        "mode": "enforce",
        "entries": [entry_payload("同名"), entry_payload("同名", hosts=["b.example.com"])],
    }
    response = client.put(ALLOWLIST_PATH, json=payload)
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"
    assert not settings.allowlist_path.exists()
    assert audit_entries(settings) == []


def test_put_rejects_unknown_fields(client: TestClient) -> None:
    response = client.put(
        ALLOWLIST_PATH, json={"mode": "enforce", "entries": [], "expiry": "2026-01-01"}
    )
    assert response.status_code == 422


def test_post_adds_one_entry_and_audits(client: TestClient, settings: Settings) -> None:
    client.put(ALLOWLIST_PATH, json={"mode": "enforce", "entries": [entry_payload("原有")]})
    response = client.post(ENTRIES_PATH, json=entry_payload("新增"))
    assert response.status_code == 200
    labels = [item["label"] for item in response.json()["config"]["entries"]]
    assert labels == ["原有", "新增"], "加一条不许动其它条目，也不许重排"

    detail = last_detail(settings)
    assert detail["operation"] == "add"
    assert detail["label"] == "新增"
    assert detail["entry_count"] == 2


def test_post_on_a_fresh_machine_starts_the_file(client: TestClient, settings: Settings) -> None:
    """文件不存在时"加一条"是合法起点，不是错误。"""
    response = client.post(ENTRIES_PATH, json=entry_payload("第一条"))
    assert response.status_code == 200
    assert settings.allowlist_path.exists()
    # 新文件按 AllowlistConfig 的默认值来，也就是 enforce。
    assert response.json()["effective_mode"] == "enforce"


def test_post_with_a_duplicate_label_is_422_and_changes_nothing(
    client: TestClient, settings: Settings
) -> None:
    """重复判定不在路由里手写，而是交给 `AllowlistConfig` 的唯一性校验。

    同一条不变量只有一个执行点，"接口漏判但文件里能出现重名"这种分歧因此不可能发生。
    """
    client.put(ALLOWLIST_PATH, json={"mode": "enforce", "entries": [entry_payload("同名")]})
    before = settings.allowlist_path.read_text(encoding="utf-8")
    audit_before = len(audit_entries(settings))

    response = client.post(ENTRIES_PATH, json=entry_payload("同名", hosts=["b.example.com"]))
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"
    assert settings.allowlist_path.read_text(encoding="utf-8") == before
    assert len(audit_entries(settings)) == audit_before


def test_delete_removes_one_entry_and_audits(client: TestClient, settings: Settings) -> None:
    client.put(
        ALLOWLIST_PATH,
        json={"mode": "enforce", "entries": [entry_payload("留下"), entry_payload("删掉")]},
    )
    response = client.delete(f"{ENTRIES_PATH}/删掉")
    assert response.status_code == 200
    assert [item["label"] for item in response.json()["config"]["entries"]] == ["留下"]

    detail = last_detail(settings)
    assert detail["operation"] == "remove"
    assert detail["label"] == "删掉"
    assert detail["entry_count"] == 1


def test_delete_an_unknown_label_is_404(client: TestClient, settings: Settings) -> None:
    """**不是幂等的 200。**

    "删一条不存在的授权"意味着调用方看到的清单和真实的不一样（很可能有人刚刚手工改了
    文件），而那正是操作者需要知道的事。
    """
    client.put(ALLOWLIST_PATH, json={"mode": "enforce", "entries": [entry_payload("在")]})
    audit_before = len(audit_entries(settings))
    assert audit_before == 1, "上面那个 PUT 没写审计 —— 下面的比较会变成 0 == 0 的空转"

    response = client.delete(f"{ENTRIES_PATH}/不在")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert len(audit_entries(settings)) == audit_before, "失败的删除不该留下审计"


# =============================================================================
# 七、坏文件在路由上的表现
# =============================================================================
def break_the_file(settings: Settings) -> None:
    settings.allowlist_path.parent.mkdir(parents=True, exist_ok=True)
    settings.allowlist_path.write_text("entries: [没闭合\n", encoding="utf-8")


def test_get_tells_the_truth_about_a_broken_file(client: TestClient, settings: Settings) -> None:
    """先有一份好的，再把文件写坏 —— `GET` 必须说出"内存里还有一份旧的"。"""
    client.put(ALLOWLIST_PATH, json={"mode": "advisory", "entries": [entry_payload("旧的")]})
    break_the_file(settings)

    body = client.get(ALLOWLIST_PATH).json()
    assert body["stale"] is True
    assert body["file_error"] == "allowlist_syntax_error"
    assert body["file_error_line"] is not None
    assert [item["label"] for item in body["config"]["entries"]] == ["旧的"]
    assert body["effective_mode"] == "advisory", "生效的仍是内存里那份配置的模式"


def test_get_reports_fail_closed_on_a_cold_broken_file(
    client: TestClient, settings: Settings
) -> None:
    """内存里没有旧配置时，界面必须能说出"现在按 enforce 拒"。

    只给一个错误码是不够的 —— 操作者会不知道此刻到底还能不能扫。
    """
    break_the_file(settings)
    body = client.get(ALLOWLIST_PATH).json()
    assert body["config"] is None
    assert body["stale"] is False
    assert body["effective_mode"] == "enforce"


@pytest.mark.parametrize("method", ["post", "delete"])
def test_incremental_edits_refuse_a_broken_file(
    client: TestClient, settings: Settings, method: str
) -> None:
    """增量编辑要求当前文件可读。

    拿一份空配置当基准的话，"加一条"实际上等于"删掉文件里其它所有条目" ——
    一次静默的数据丢失。
    """
    break_the_file(settings)
    before = settings.allowlist_path.read_text(encoding="utf-8")

    if method == "post":
        response = client.post(ENTRIES_PATH, json=entry_payload("新增"))
    else:
        response = client.delete(f"{ENTRIES_PATH}/随便")

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "allowlist_file_broken"
    assert body["params"]["file_error"] == "allowlist_syntax_error"
    assert settings.allowlist_path.read_text(encoding="utf-8") == before
    assert audit_entries(settings) == []


def test_put_is_the_recovery_path(client: TestClient, settings: Settings) -> None:
    """整份替换本来就要覆盖全部内容，所以它是坏文件的恢复手段。"""
    break_the_file(settings)
    response = client.put(
        ALLOWLIST_PATH, json={"mode": "enforce", "entries": [entry_payload("修好了")]}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["file_error"] is None
    assert body["stale"] is False
    assert [item["label"] for item in body["config"]["entries"]] == ["修好了"]


def test_a_hand_edited_file_shows_up_without_a_restart(
    client: TestClient, settings: Settings
) -> None:
    """`${DATA}/config/allowlist.yaml` 是可以手工编辑的，改完不需要重启。"""
    client.put(ALLOWLIST_PATH, json={"mode": "enforce", "entries": [entry_payload("界面写的")]})
    settings.allowlist_path.write_text(config_text("手写的"), encoding="utf-8")

    body = client.get(ALLOWLIST_PATH).json()
    assert [item["label"] for item in body["config"]["entries"]] == ["手写的"]
