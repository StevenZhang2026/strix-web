"""截图抽取的全部判据。对应 `PLAN.md` T14 与本模块的两条不变式（I-1 / I-2）。

# 为什么用 JSON 模板做表驱动

被测的是"任意嵌套深度都要抽干净"。如果每个嵌套形状都手写一遍"输入 dict"和"期望 dict"，
那两个字面量之间的差异就得靠人眼比对，而真正在测的那一件事（只有 data URL 那一处变了）
反而看不出来。所以每一行用例是**一个 JSON 模板 + 一个占位符 `{IMG}`**：
输入 = 模板填上 data URL，期望 = 同一个模板填上我们的媒体 URL。**结构由同一份模板产生，
所以任何结构上的差异只可能是被测代码造成的。**

# 这个文件零 IO

`extract_media` 是纯函数，所以这里没有 fixture、没有 monkeypatch、没有临时目录。
图片"内容"是几个假字节串 —— 摘要是对字节取的，字节是不是合法 PNG 与本模块无关
（真去构造合法 PNG 只会让用例里多出一堆与判据无关的字面量）。
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from app.services.event_media import (
    SKIP_UNDECODABLE_BASE64,
    SKIP_UNSUPPORTED_MIME,
    ExtractedImage,
    extract_media,
)

SCAN_ID = "scan-7f3a"

# 两张"图"。长度刻意取 20 / 19 字节，好让 base64 分别带 1 个和 2 个 `=` ——
# I-2 要断言"去掉 padding 之后摘要不变"，而 21 字节这类整除 3 的长度根本没有 padding。
PNG_A = b"\x89PNG\r\n\x1a\nalpha-pixels"
PNG_B = b"\x89PNG\r\n\x1a\nbeta-pixels"
SHA_A = hashlib.sha256(PNG_A).hexdigest()
SHA_B = hashlib.sha256(PNG_B).hexdigest()
B64_A = base64.b64encode(PNG_A).decode("ascii")
B64_B = base64.b64encode(PNG_B).decode("ascii")
# MIME 换行包装（每 8 字符一行）。真实的 base64 附件就是这么折行的。
WRAPPED_A = "\n".join(B64_A[index : index + 8] for index in range(0, len(B64_A), 8))

IMAGE_A = ExtractedImage(sha256=SHA_A, mime="image/png", payload=PNG_A)
IMAGE_B = ExtractedImage(sha256=SHA_B, mime="image/png", payload=PNG_B)


def data_url(mime: str, payload_b64: str) -> str:
    return f"data:{mime};base64,{payload_b64}"


def media_url(sha256: str) -> str:
    """URL 形状在这里**写死**，不从生产代码 import —— 否则改坏它测试也跟着改。"""
    return f"/api/scans/{SCAN_ID}/media/{sha256}.png"


def fill(template: str, *urls: str) -> dict[str, object]:
    """把模板里每一个 `{IMG}` 依次换成 `urls` 里的一项，再 `json.loads`。"""
    text = template
    for url in urls:
        text = text.replace("{IMG}", url, 1)
    return json.loads(text)


# =============================================================================
# I-1 抽干净且改写正确（任意嵌套深度 + 文本中间）
# =============================================================================

# 每一行：一个 JSON 模板，含**恰好一个** `{IMG}`（最后两行故意含两个，见下方 ids）。
ONE_IMAGE_SHAPES: tuple[tuple[str, str, int], ...] = (
    ("top_level_value", '{"image": "{IMG}"}', 1),
    ("nested_dict", '{"a": {"b": {"screenshot": "{IMG}"}}}', 1),
    ("inside_list", '{"items": ["x", "{IMG}", 3]}', 1),
    ("dict_in_list_in_dict", '{"a": [{"b": [{"c": "{IMG}"}]}]}', 1),
    ("scalar_leaves_untouched", '{"n": 1, "f": 1.5, "b": true, "z": null, "i": "{IMG}"}', 1),
    ("markdown_text_middle", '{"result": "\\u89c1\\n\\n![shot]({IMG})\\n\\n\\u5b8c"}', 1),
    ("html_attr_in_text", '{"result": "<img src=\\"{IMG}\\" alt=\\"x\\">"}', 1),
    # 同一张图在同一段文本里出现两次：两处都要改写，但 `images` 只出现一次（单事件去重）。
    ("twice_in_one_text", '{"result": "a {IMG} b {IMG} c"}', 2),
    ("twice_across_branches", '{"a": "{IMG}", "b": ["{IMG}"]}', 2),
)


@pytest.mark.parametrize(
    ("template", "occurrences"),
    [(template, count) for _, template, count in ONE_IMAGE_SHAPES],
    ids=[name for name, _, _ in ONE_IMAGE_SHAPES],
)
def test_png_data_urls_are_extracted_and_rewritten(template: str, occurrences: int) -> None:
    url = data_url("image/png", B64_A)
    original = fill(template, *[url] * occurrences)
    frozen = json.dumps(original, sort_keys=True, ensure_ascii=False)

    result = extract_media(original, SCAN_ID)

    assert result.data == fill(template, *[media_url(SHA_A)] * occurrences)
    text = json.dumps(result.data, ensure_ascii=False)
    assert "data:image" not in text
    assert ";base64," not in text
    assert result.images == (IMAGE_A,)
    assert result.skipped == ()
    # 原对象一个字节都没变 —— 调用方还要拿它算指纹。
    assert json.dumps(original, sort_keys=True, ensure_ascii=False) == frozen


def test_two_distinct_images_are_both_extracted_in_first_seen_order() -> None:
    original = fill(
        '{"a": {"x": "{IMG}"}, "b": ["t {IMG} t"]}',
        data_url("image/png", B64_A),
        data_url("image/png", B64_B),
    )

    result = extract_media(original, SCAN_ID)

    assert result.images == (IMAGE_A, IMAGE_B)
    assert result.data == {"a": {"x": media_url(SHA_A)}, "b": [f"t {media_url(SHA_B)} t"]}


# =============================================================================
# I-2 摘要取的是解码后的字节，不是 base64 文本
# =============================================================================

SAME_IMAGE_ENCODINGS: tuple[tuple[str, str, str], ...] = (
    ("plain", "image/png", B64_A),
    ("newline_wrapped", "image/png", WRAPPED_A),
    ("no_padding", "image/png", B64_A.rstrip("=")),
    ("no_padding_and_wrapped", "image/png", WRAPPED_A.rstrip("=")),
    ("uppercase_mime", "image/PNG", B64_A),
    ("mixed_case_mime", "Image/Png", B64_A),
)


@pytest.mark.parametrize(
    ("mime", "payload_b64"),
    [(mime, payload) for _, mime, payload in SAME_IMAGE_ENCODINGS],
    ids=[name for name, _, _ in SAME_IMAGE_ENCODINGS],
)
def test_sha256_is_taken_over_decoded_bytes(mime: str, payload_b64: str) -> None:
    result = extract_media({"i": data_url(mime, payload_b64)}, SCAN_ID)

    # mime 一并规范化成小写：`images` 是要写库的，同一张图不该因为大小写分成两条。
    assert result.images == (IMAGE_A,)
    assert result.data == {"i": media_url(SHA_A)}
    assert result.skipped == ()


def test_same_image_in_different_encodings_is_deduped_within_one_event() -> None:
    original = {
        "a": data_url("image/png", B64_A),
        "b": data_url("image/PNG", B64_A.rstrip("=")),
        "c": data_url("image/png", WRAPPED_A),
    }

    result = extract_media(original, SCAN_ID)

    assert result.images == (IMAGE_A,)
    assert result.data == {key: media_url(SHA_A) for key in ("a", "b", "c")}


UNDECODABLE_PAYLOADS: tuple[tuple[str, str], ...] = (
    # 去掉 padding 之后长度 ≡ 1 (mod 4) —— base64 里不存在这种长度，只能是被截断了。
    ("one_char", "A"),
    ("five_chars", "AAAAA"),
    ("nine_chars", "AAAAAAAAA"),
    ("padding_only", "=="),
    ("empty", ""),
    ("whitespace_only", "\n \n"),
)


@pytest.mark.parametrize(
    "payload_b64",
    [payload for _, payload in UNDECODABLE_PAYLOADS],
    ids=[name for name, _ in UNDECODABLE_PAYLOADS],
)
def test_undecodable_base64_is_kept_verbatim_and_reported(payload_b64: str) -> None:
    original = {"i": data_url("image/png", payload_b64), "keep": 7}

    # 不抛异常：一条坏事件不许把整轮投影搞崩。
    result = extract_media(original, SCAN_ID)

    assert result.images == ()
    assert result.skipped == (SKIP_UNDECODABLE_BASE64,)
    assert result.data == original


# =============================================================================
# 只认 image/png：其余 mime 原样保留 + 记一条 unsupported_mime
# =============================================================================

UNSUPPORTED_MIMES: tuple[str, ...] = ("image/jpeg", "IMAGE/JPEG", "image/webp", "image/gif")


@pytest.mark.parametrize("mime", UNSUPPORTED_MIMES)
def test_non_png_is_left_in_place_and_reported(mime: str) -> None:
    original = {"deep": [{"i": data_url(mime, B64_A)}]}

    result = extract_media(original, SCAN_ID)

    assert result.images == ()
    assert result.skipped == (SKIP_UNSUPPORTED_MIME,)
    assert result.data == original


def test_skipped_records_one_code_per_occurrence_and_png_still_extracted() -> None:
    original = {
        "a": data_url("image/jpeg", B64_A),
        "b": data_url("image/gif", B64_A),
        "c": data_url("image/png", "AAAAA"),
        "d": data_url("image/png", B64_B),
    }

    result = extract_media(original, SCAN_ID)

    assert result.skipped == (
        SKIP_UNSUPPORTED_MIME,
        SKIP_UNSUPPORTED_MIME,
        SKIP_UNDECODABLE_BASE64,
    )
    assert result.images == (IMAGE_B,)
    assert result.data == {**original, "d": media_url(SHA_B)}
