"""**每一条**错误响应的形状都必须是 `{code, trace_id, params}`。

包括框架自己产出的那些。这一条不是洁癖：

前端被要求"按码分支，**不得**匹配文案"（CLAUDE.md §错误与文案）。Starlette 默认的
404 返回 `{"detail": "Not Found"}` —— 没有 `code`，于是前端要么多写一个解析器，
要么在这条分支上显示一片空白。而它恰恰只在"出了意料之外的事"时才出现（重构之后前端
请求了一个拼错的路径），也就是最需要看到一个明确错误码的时刻。

本文件用 `TestClient` 但**不进 `with`**：进 `with` 会跑 lifespan，那需要真实的数据
目录、SQLite、strix-agent 版本探测 —— 而 404 / 405 是路由层的事，一行 app.state 都
不碰。不跑 lifespan 让这几条测试保持在"单测"的范畴（CLAUDE.md §测试：禁止真实网络
与真实 Docker）。
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.errors import ConsoleError
from app.main import TRACE_ID_HEADER, console_error_for_http_status, create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


# =============================================================================
# 一、映射表本身（纯函数，不起服务）
# =============================================================================
@pytest.mark.parametrize(
    ("status", "code"),
    [
        (404, "not_found"),
        (405, "method_not_allowed"),
        (418, "invalid_request"),  # 兜底：4xx
        (503, "internal_error"),  # 兜底：5xx
    ],
)
def test_status_maps_to_code(status: int, code: str) -> None:
    assert console_error_for_http_status(status).code == code


def test_mapping_always_returns_a_registered_error() -> None:
    """负面覆盖：映射结果必须是真的 `ConsoleError`，不是临时拼的字典。

    这条挡住的是"直接 return {'code': 'not_found'}"那种写法 —— 它能让上面四条
    通过，却绕过了 `to_payload()`，于是响应体形状不再由一个地方决定。
    """
    for status in (400, 404, 405, 500, 599):
        assert isinstance(console_error_for_http_status(status), ConsoleError)


# =============================================================================
# 二、真实响应
# =============================================================================
def test_unmatched_route_returns_our_shape(client: TestClient) -> None:
    response = client.get("/api/definitely-not-a-route")
    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"code", "trace_id", "params"}
    assert body["code"] == "not_found"
    # 这一条是本文件的要点：Starlette 的默认形状不能残留。
    assert "detail" not in body


def test_wrong_method_returns_our_shape(client: TestClient) -> None:
    """`/api/health` 只有 GET。用 POST 打它必须拿到 `method_not_allowed`。

    单独一个码而不是并进 `not_found`：405 几乎总是前端写错了动词，
    而 404 通常是路径拼错或前端版本过旧。合成一个码会让前者伪装成后者。
    """
    response = client.post("/api/health")
    assert response.status_code == 405
    body = response.json()
    assert body["code"] == "method_not_allowed"
    assert "detail" not in body
    # `Allow` 由 Starlette 路由层填（RFC 9110 §15.5.6）。我们转发 `exc.headers`，
    # 所以它必须还在 —— 丢了它就说明处理器把框架的头吃掉了。
    assert "GET" in response.headers["allow"]


def test_trace_id_is_present_and_matches_the_header(client: TestClient) -> None:
    """响应体里的 trace_id 必须是真的，而不是占位符 `-`。

    trace_id 由中间件生成，而中间件在异常处理器**外面** —— 若两者的顺序装错，
    这里会拿到 `trace_id_var` 的默认值 `-`，而用户报上来的那个 id 就对不上任何日志。
    """
    response = client.get("/api/definitely-not-a-route")
    trace_id = response.json()["trace_id"]
    assert trace_id != "-"
    assert trace_id == response.headers[TRACE_ID_HEADER]
