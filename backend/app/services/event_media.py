"""截图抽取 —— 把事件 payload 里内联的 `data:image/png;base64,...` 换成一条媒体 URL。

# 这个模块零 IO，这是设计要求而不是巧合

不读文件、不写文件、不建目录、不碰数据库。落盘与 `scan_media` 的去重记账是另一条任务
（T14b）的事。理由有两层：

1. `CLAUDE.md` §Strix 集成「截图只保留最近 3 张（内联 data URL）→ 首次见到就落地」——
   "首次见到"这个判断跨事件、要查库，而"这一条事件里有哪几张图"是纯粹的字符串问题。
   混在一起写，就没办法给"同一张图的两种 base64 写法必须同摘要"写测试了。
2. 入参刻意是 `Mapping` 而不是 `ProjectedEvent`：写库那一半可以直接复用它，单测可以直接
   喂字典，而本模块不需要 import `run_projector` 里的任何东西。

# 为什么按"每个字符串叶子上跑一次正则"，而不是"整个字段是不是一个 data URL"

`ProjectedEvent.data` 是上游 payload 原样带过来的（`run_projector.py:17-18`：形状由工具
决定，投影层不解释它）。已知的两种形状都要处理：截图工具把 data URL 放在自己的一个字段
里，而报告类工具把它嵌在一大段 markdown 中间（`![](data:image/png;base64,...)`）。
一条正则 + `re.sub` 同时覆盖两者，而且"整个字段就是一个 data URL"只是"文本中间"的特例
—— 分成两条分支写就等于把同一件事实现两遍。

# 摘要取的是解码后的字节，不是 base64 文本

同一张 PNG 的 base64 文本形式不唯一：MIME 折行会插入换行、有些生成器省掉尾部 `=`、
mime 串大小写不定（`image/PNG`）。摘要落在文本上，同一张图就会得到几个不同的
`<sha256>.png`，前端反复重下同一张图，`scan_media` 也去不了重。所以先规范化再摘要。

# 只认 `image/png`

`PLAN.md` 只承诺 `media/<sha256>.png` 这一种落地形状。多认一种 mime 就要多一套
"mime → 扩展名"的映射和它的取值域校验，而目前没有任何一个真实 run 产出过 jpeg 截图
（`CLAUDE.md` §编码哲学 3「不要过早抽象」）。其余 mime **原样留在 payload 里**并记一条
`unsupported_mime`：留着比丢掉好 —— 至少前端还能把它显示出来。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass

# `MediaExtraction.skipped` 的取值域。稳定机器码，进日志与前端提示，**不是文案**。
SKIP_UNSUPPORTED_MIME = "unsupported_mime"
SKIP_UNDECODABLE_BASE64 = "undecodable_base64"

# 唯一支持的 mime（已小写）。
SUPPORTED_MIME = "image/png"

# base64 段：一串字母表字符，后面可以再跟若干"**换行**隔开的续行"。
#
# 为什么续行只认换行、不认一般空白：折行的 base64 用的是换行（MIME 的 76 列包装），
# 而"base64 后面紧跟一段散文"用的是空格。把一般空白也收进这个字符类**已实测出过 bug**
# —— `"a <urlA> b <urlB> c"` 里第一个匹配会一路吃掉 `= b data`（`b`/`data` 全在
# base64 字母表内、空格被空白类吃掉），于是两张图一张都抽不出来，还误报
# `undecodable_base64`。
#
# 残余的越界形状是"换行之后紧跟一个纯 ASCII 单词"（`"...base64,AAAA\nDone"`）。
# 不再收窄：真实 payload 里 data URL 的下一个字符是 `)`、`"` 或字符串结尾。
_DATA_URL_RE = re.compile(
    r"data:(?P<mime>[\w.+-]+/[\w.+-]+);base64,"
    r"(?P<payload>[A-Za-z0-9+/=]*(?:\s*\n\s*[A-Za-z0-9+/=]+)*)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ExtractedImage:
    """一张被抽出来的图。`sha256` 是对**解码后的字节**取的十六进制摘要。"""

    sha256: str
    mime: str
    payload: bytes


@dataclass(frozen=True, slots=True)
class MediaExtraction:
    """一次抽取的结果。`data` 是改写后的新 payload —— **不许原地改传进来的那个 dict**。"""

    data: dict[str, object]
    images: tuple[ExtractedImage, ...]
    skipped: tuple[str, ...]
    """每个被跳过的位置一条机器码。刻意不带下标或字段路径：调用方只需要知道
    "发生过几次、什么原因"，带上路径就得为它定义一套稳定的路径语法。"""


def _decode_base64(text: str) -> bytes | None:
    """解出字节，解不出返回 `None`（不抛 —— 一条坏事件不许把整轮投影搞崩）。

    两步规范化，两步都有具体出处：

    - 去掉所有空白：MIME 折行插入的换行不是数据。
    - 尾部 `=` 先剥掉再按长度补齐：见过省略 padding 的生成器，而 `b64decode` 对
      padding 不全直接报错。剥了再补也顺手修正了 padding 多写的情形。

    `validate=True` 不能省：默认行为是**静默丢掉**字母表外的字符，于是一段被截断或
    被别的文本污染的 base64 会安静地解出一张错图（同一条理由见 `auth.py:602`）。
    """
    core = "".join(text.split()).rstrip("=")
    if not core:
        # 空 payload（`data:image/png;base64,` 后面什么都没有，或只有空白）。
        # 不当成"一张 0 字节的图"—— 那会让 T14b 往磁盘上写一个空文件。
        return None
    try:
        return base64.b64decode(core + "=" * (-len(core) % 4), validate=True)
    except binascii.Error:
        # 长度 ≡ 1 (mod 4) 或含字母表外字符。binascii.Error 是 ValueError 的子类，
        # 这里写具体类型而不是 ValueError，免得把别的编程错误也一并吞掉。
        return None


def extract_media(data: Mapping[str, object], scan_id: str) -> MediaExtraction:
    """把 `data` 里所有内联 PNG 抽成字节，返回改写后的新 payload。

    纯函数：`data` 在返回后一个字节都没变（调用方还要拿它算指纹）。改写后的每一处都是
    `/api/scans/{scan_id}/media/{sha256}.png`，`sha256` 与 `images` 里那张图对得上。

    去重只在**单个事件内**按 `sha256` 做（同一张图在一条事件里出现两次 → 两处都改写，
    `images` 只一条）。跨事件去重是 `scan_media` 主键的事，本函数看不到别的事件。
    """
    images: dict[str, ExtractedImage] = {}
    skipped: list[str] = []

    def rewrite(match: re.Match[str]) -> str:
        mime = match.group("mime").lower()
        if mime != SUPPORTED_MIME:
            skipped.append(SKIP_UNSUPPORTED_MIME)
            return match.group(0)
        payload = _decode_base64(match.group("payload"))
        if payload is None:
            skipped.append(SKIP_UNDECODABLE_BASE64)
            return match.group(0)
        digest = hashlib.sha256(payload).hexdigest()
        images.setdefault(digest, ExtractedImage(sha256=digest, mime=mime, payload=payload))
        return f"/api/scans/{scan_id}/media/{digest}.png"

    def walk(value: object) -> object:
        """重建一份新结构。只下钻 `Mapping` 与 `list` —— payload 来自 JSON，没有别的容器。"""
        if isinstance(value, str):
            return _DATA_URL_RE.sub(rewrite, value)
        if isinstance(value, Mapping):
            return {str(key): walk(item) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        # int / float / bool / None 原样带过。
        return value

    rewritten = {str(key): walk(item) for key, item in data.items()}
    return MediaExtraction(data=rewritten, images=tuple(images.values()), skipped=tuple(skipped))
