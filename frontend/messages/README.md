# 文案表约定（`zh-CN.json`）

**所有用户可见文案都在 `zh-CN.json` 里，组件里不得出现中文字面量。** 只有一种语言，
所以刻意**不引** `next-intl` / `i18next` —— `lib/messages.ts` 的 `t()` 二十行就够了，
一个 i18n 框架换不到任何东西，只多一份要维护的配置。

## 键的约定

1. key 一律 **lowerCamelCase**，例外是**机器码本身**（snake_case）—— 它出现在
   `errors.*` / `scanFailures.*` 的第二段，以及 `targetGuard.*` 的**第三段**
   （`targetGuard.reasons.invalid_port`、`targetGuard.resolution.dns_timeout` …）。
   任何改写都会引入失配，而失配的表现是"用户看到一个空白的提示"，不是编译错误。
2. 嵌套**最多三段** `<域>.<组件>.<元素>`。
3. **一个 key = 一句完整的话。组件里禁止拼接字符串。** 中文语序和拉丁不同，拼接必然
   在某个分支里读起来是病句。需要"标签 + 值"时渲染成**两个节点**（键值行），
   那不是拼接 —— 它正好长成工单的字段行，和视觉语言同形。
4. **`title` / `detail` / `action` 里禁止占位符。** 具体值走 `params` 数组 +
   全局 `paramLabels`，由 `ErrorNotice` 渲染成键值行。三条理由：中文插值的语序问题；
   长 URL 会把句子撑坏；缺参数时会露出花括号。
5. 中文与拉丁/数字之间写**一个半角空格**。这一步**由人执行**，不靠 CSS ——
   `text-spacing` 类特性在各浏览器上表现不一致，而写进字符串是确定的。
6. **金额、时间、时长、字节不写进文案**，走 `lib/format.ts`。`units.*` 那三个词是
   `format.ts` 唯一需要的中文，放在这里而不是硬编码进 `lib/`，是为了保住"文案只有
   一个来源"这条。SI 符号（`MB`/`GB`/`$`）不是文案，留在 `format.ts` 里。
7. **预算文案只许写「达到 $X 后停止」，永远不许写「绝不超过」。** 上限是**软的** ——
   结算发生在每一轮之后，最后一轮必然把总额顶出上限一点。费用尺上那段斜纹就是这件事
   的视觉陈述；写"绝不超过"会让界面和图自相矛盾，并且是一句假话。
8. **`paramLabels.*` 的每一条都得是名词短语**（字段、目标、耗时、来源、访问地址…），
   不许写成动词短语。理由是它们的**渲染位置**：`ErrorNotice` 把它们放进一张
   `auto 1fr` 的键值表，标签和值之间有 16px 列间隙 —— 一个动词短语在那里会读成
   一句被切断的话，而不是一个被命名的值。
   实测踩过：T5b 的登录页第一次让 `auth_locked` 真的显示出来，`retry_after` 的标签
   原本写的是「解锁还需」，渲染成「解锁还需␣␣␣␣4 分钟」。已改为「剩余锁定时间」。
   （`resolved_ip` 那组「解析到 / 声明时解析到 / 现在解析到」是**边界情形**：值是
   一个 IP，标签读成关系词尚可，暂不动；再出第三例就该统一成名词。）

## 为什么机器码分成三棵树

镜像后端的分法，**刻意不合并**：

- `errors.*` ← `ConsoleError` 的子类。**HTTP 错误**，请求本身失败了，有 HTTP status。
- `scanFailures.*` ← `SCAN_FAILURE_CODES`。**扫描归因**，请求成功了、那次扫描失败了，
  只出现在 `scans.error_code` 里。
- `targetGuard.*` ← `app/services/target_guard.py` 与 `dns_resolver.py` 的枚举。
  **目标预览的逐条结论**，出现在 `POST /api/targets/validate` 的 200 正文里，
  一个也不是 HTTP 错误。

合成一棵的后果很具体：前端某天会想给 `llm_tls_intercepted` 找一个 HTTP status，
而没有任何接口会用它做响应码 —— 假字段最终一定会被人当真用。`targetGuard.*` 同理，
`invalid_port` 若躺在 `errors.*` 里，就会有人拿它去 `raise`。

`targetGuard.*` 的条目是**纯字符串**，不是 `{title, detail, action}` 三段 ——
它们渲染在输入框下面的一行里（或一个类别徽章上），不是一张错误卡片。
所以它不参与 `test_copy_entries_are_complete` 那组按 `tree` 参数化的测试，
有自己一组形状断言。

## 新增机器码时

后端加一个码，这里就必须跟一条，两边都不许漏。
守门的是 `backend/tests/test_message_coverage.py`：它读 `app/errors.py` 的**真实来源**
（`ALL_ERRORS` + `ConsoleError.__subclasses__()` + `SCAN_FAILURE_CODES`）与本文件做
**双向**集合比对 —— 缺文案要红，本文件里有代码中不存在的孤儿码也要红。
