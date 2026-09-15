"""T12a：`evaluate_admission` 的判定顺序、码映射、`params` 里放了什么与**不该**放什么。

**刻意不重测** `target_guard` 与 `allowlist` 自己的行为（那两个模块各有自己的测试文件）：
这里只钉住准入这一层新增的东西 —— 四条判定的先后、每条落到哪个已有机器码、以及
`params` 的内容边界（CLAUDE.md §安全不变式：`params` 里绝不放凭据）。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

import pytest

from app.services.allowlist import AllowlistConfig, AllowlistSnapshot
from app.services.audit import EVENT_TARGET_DNS_CHANGED, EVENT_TARGET_REJECTED
from app.services.dns_resolver import Resolution, ResolutionError
from app.services.scan_admission import (
    AdmissionPassed,
    AdmissionRejected,
    AdmissionRequest,
    audit_for,
    evaluate_admission,
)
from app.services.target_guard import NO_OPT_IN, AllowlistMode, OperatorOptIn
from tests.conftest import make_entry

_TODAY = date(2026, 9, 16)
_PUBLIC = "93.184.216.34"


def _neutral() -> AllowlistSnapshot:
    """文件缺席 —— `decide` 给出 advisory 中性值，公网目标不设限。"""
    return AllowlistSnapshot(config=None, file_present=False, error=None, fingerprint=None)


def _enforce(*, matched: bool) -> AllowlistSnapshot:
    entries = (make_entry(),) if matched else ()
    return AllowlistSnapshot(
        config=AllowlistConfig(mode=AllowlistMode.ENFORCE, entries=entries),
        file_present=True,
        error=None,
        fingerprint=(1, 1),
    )


def _request(
    *raw_targets: str,
    typed: str = "example.com",
    opt_in: OperatorOptIn = NO_OPT_IN,
    declared: Mapping[str, tuple[str, ...]] | None = None,
    resolutions: Mapping[str, Resolution] | None = None,
    allowlist: AllowlistSnapshot | None = None,
) -> AdmissionRequest:
    """默认形状：`example.com` 声明与本次解析都是同一个公网地址、清单文件缺席。"""
    resolved = {"example.com": Resolution(addresses=(_PUBLIC,), error=None)}
    return AdmissionRequest(
        raw_targets=raw_targets,
        typed_confirmation=typed,
        opt_in=opt_in,
        declared_ips={"example.com": (_PUBLIC,)} if declared is None else declared,
        resolutions=resolved if resolutions is None else resolutions,
        allowlist=_neutral() if allowlist is None else allowlist,
        today=_TODAY,
    )


def _rejection(outcome: object) -> AdmissionRejected:
    assert isinstance(outcome, AdmissionRejected), outcome
    return outcome


def test_all_targets_pass_in_request_order() -> None:
    outcome = evaluate_admission(
        _request(
            "https://example.com/app",
            "http://127.0.0.1:3000",
            opt_in=OperatorOptIn(loopback=True),
            allowlist=_enforce(matched=True),
        )
    )
    assert isinstance(outcome, AdmissionPassed)
    assert [item.target.host for item in outcome.targets] == ["example.com", "127.0.0.1"]
    assert all(item.verdict.allowed for item in outcome.targets)
    # entry_label 要原样透出：T12c 靠它落 allowlist_entry_id 与审计。
    assert outcome.targets[0].allowlist.entry_label == "预生产"
    assert outcome.targets[1].allowlist.entry_label is None


def test_normalization_failure_reports_index_and_reason() -> None:
    outcome = _rejection(evaluate_admission(_request("http://user:pass@example.com")))
    assert outcome.code == "invalid_request"
    assert outcome.params == {
        "field": "targets",
        "index": 0,
        "reason": "credentials_in_url",
    }
    # 规范化失败时还没有"已规范化的 host"可言。
    assert outcome.target_host is None


def test_params_never_contain_the_raw_input() -> None:
    raw = "http://user:pass@example.com"
    outcome = _rejection(evaluate_admission(_request(raw)))
    for value in outcome.params.values():
        text = str(value)
        assert raw not in text
        assert "pass" not in text


def test_normalization_failure_precedes_guard_rejection() -> None:
    # 第 0 个目标在 enforce 模式下没命中清单（会是 not_in_allowlist），第 1 个连规范化
    # 都过不去。规范化是第一步，所以赢的必须是后者。
    outcome = _rejection(
        evaluate_admission(
            _request(
                "https://example.com",
                "http://example.com/x?y=1",
                allowlist=_enforce(matched=False),
            )
        )
    )
    assert outcome.code == "invalid_request"
    assert outcome.params["index"] == 1
    assert outcome.params["reason"] == "query_or_fragment_not_allowed"


def test_resolution_failure_maps_to_dns_changed() -> None:
    outcome = _rejection(
        evaluate_admission(
            _request(
                "https://example.com",
                resolutions={
                    "example.com": Resolution(addresses=(), error=ResolutionError.TIMEOUT)
                },
            )
        )
    )
    assert outcome.code == "dns_changed"
    assert outcome.params == {
        "field": "targets",
        "index": 0,
        "resolution_error": "dns_timeout",
    }
    assert outcome.target_host == "example.com"


def test_declared_addresses_mismatch_is_dns_changed() -> None:
    outcome = _rejection(
        evaluate_admission(_request("https://example.com", declared={"example.com": ("1.1.1.1",)}))
    )
    assert outcome.code == "dns_changed"
    # 比对失败与解析失败共用一个码，但 params 不同：这里没有 resolution_error。
    assert outcome.params == {"field": "targets", "index": 0}
    assert outcome.target_host == "example.com"


def test_declared_addresses_compare_as_sets() -> None:
    # 轮询域名每次 getaddrinfo 的顺序都可能不同；按元组比会让它每次都报假警报。
    outcome = evaluate_admission(
        _request(
            "https://example.com",
            declared={"example.com": ("2.2.2.2", _PUBLIC)},
            resolutions={"example.com": Resolution(addresses=(_PUBLIC, "2.2.2.2"), error=None)},
        )
    )
    assert isinstance(outcome, AdmissionPassed)


def test_literal_ip_target_skips_dns_comparison() -> None:
    # 字面 IP 没有 DNS 这一步：既不在 resolutions 里，也不需要 declared_ips 有它。
    outcome = evaluate_admission(
        _request(
            "http://127.0.0.1:3000",
            typed="127.0.0.1",
            opt_in=OperatorOptIn(loopback=True),
            declared={},
            resolutions={},
        )
    )
    assert isinstance(outcome, AdmissionPassed)
    assert outcome.targets[0].verdict.addresses[0].address == "127.0.0.1"


def test_missing_resolution_for_hostname_raises() -> None:
    # 调用方少走了解析这一步 —— 静默当成"没有地址所以放行"会是最糟的失败模式。
    # 断言的是**本模块**那句话，不是 evaluate_target 那句：护栏自己也会为"主机名+空地址"
    # 抛 ValueError，只匹配 host 名的话这条测试对本模块的守卫被删掉是瞎的。
    with pytest.raises(ValueError, match="没有本次解析结果"):
        evaluate_admission(_request("https://example.com", resolutions={}))


def test_empty_target_list_raises() -> None:
    with pytest.raises(ValueError):
        evaluate_admission(_request())


def test_error_code_wins_over_missing_opt_in() -> None:
    # 100.64.0.1 是 carrier_reserved（只能改 allowlist.yaml 放行），10.0.0.5 是内网
    # （缺勾选）。两者同时缺 → 汇总类别取更严的那个，所以要报它的码而不是"缺勾选"。
    addresses = ("100.64.0.1", "10.0.0.5")
    outcome = _rejection(
        evaluate_admission(
            _request(
                "https://example.com",
                declared={"example.com": addresses},
                resolutions={"example.com": Resolution(addresses=addresses, error=None)},
            )
        )
    )
    assert outcome.code == "not_in_allowlist"
    assert outcome.params == {"field": "targets", "index": 0}
    assert outcome.target_host == "example.com"


def test_missing_opt_in_maps_to_invalid_request_on_overrides() -> None:
    outcome = _rejection(
        evaluate_admission(
            _request(
                "https://example.com",
                declared={"example.com": ("10.0.0.5",)},
                resolutions={"example.com": Resolution(addresses=("10.0.0.5",), error=None)},
            )
        )
    )
    assert outcome.code == "invalid_request"
    # 缺勾选不是"目标错了"，是"overrides 少给了" —— field 指向前端该改的地方。
    assert outcome.params == {"field": "overrides", "index": 0, "missing_opt_in": "private"}
    assert outcome.target_host == "example.com"


def test_metadata_target_is_blocked_and_host_stays_out_of_params() -> None:
    outcome = _rejection(
        evaluate_admission(
            _request(
                "http://169.254.169.254/latest/meta-data/",
                typed="169.254.169.254",
                declared={},
                resolutions={},
            )
        )
    )
    assert outcome.code == "blocked_metadata"
    assert outcome.params == {"field": "targets", "index": 0}
    # host 只进审计 detail，不进 HTTP 响应体。
    assert "host" not in outcome.params
    assert outcome.target_host == "169.254.169.254"


def test_first_failing_target_wins_over_a_more_severe_later_one() -> None:
    # 顺序即优先级：不排严重度阶梯。第 0 个只是缺勾选，第 1 个是永久硬拦，赢的是第 0 个。
    addresses = ("10.0.0.5",)
    outcome = _rejection(
        evaluate_admission(
            _request(
                "https://example.com",
                "http://169.254.169.254/",
                declared={"example.com": addresses},
                resolutions={"example.com": Resolution(addresses=addresses, error=None)},
            )
        )
    )
    assert outcome.code == "invalid_request"
    assert outcome.params["index"] == 0


def test_typed_confirmation_mismatch_is_rejected_with_empty_params() -> None:
    outcome = _rejection(evaluate_admission(_request("https://example.com", typed="wrong.com")))
    assert outcome.code == "missing_typed_confirmation"
    # 期望串前端已经灰显在输入框旁，后端不再复述一遍。
    assert outcome.params == {}
    assert outcome.target_host == "example.com"


def test_typed_confirmation_is_stripped_and_casefolded() -> None:
    outcome = evaluate_admission(_request("https://example.com", typed="  EXAMPLE.COM \n"))
    assert isinstance(outcome, AdmissionPassed)


def test_guard_rejection_precedes_typed_confirmation() -> None:
    # 确认串也错，但目标本身还没过护栏 —— 先让操作者去改目标，别让他白打一遍确认串。
    outcome = _rejection(
        evaluate_admission(
            _request("https://example.com", typed="wrong.com", allowlist=_enforce(matched=False))
        )
    )
    assert outcome.code == "not_in_allowlist"


def test_audit_for_maps_event_and_merges_params() -> None:
    dns = AdmissionRejected(
        code="dns_changed",
        params={"field": "targets", "index": 0},
        target_host="example.com",
    )
    assert audit_for(dns) == (
        EVENT_TARGET_DNS_CHANGED,
        {"code": "dns_changed", "host": "example.com", "field": "targets", "index": 0},
    )

    other = AdmissionRejected(
        code="invalid_request",
        params={"field": "targets", "index": 1, "reason": "invalid_host"},
        target_host=None,
    )
    assert audit_for(other) == (
        EVENT_TARGET_REJECTED,
        {
            "code": "invalid_request",
            "host": None,
            "field": "targets",
            "index": 1,
            "reason": "invalid_host",
        },
    )
