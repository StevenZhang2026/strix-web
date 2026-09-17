"""**本包是整个 web 进程里唯一允许 `import strix.*` 的地方。**

允许的只有两个模块：`strix.interface.tui.backend.live_view`（**子类**，父类没有游标 API）
与 `strix.interface.viewer.transcript`。**禁止** `strix.core.*` / `strix.runtime.*` /
`strix.llm.*` —— 那几个会把 agents SDK、litellm 以及 `configure_sdk_model_defaults` 对
`os.environ` 的改写拖进 web 进程（模块级全局可变状态 → 跨用户污染 API Key）。

T29 用 import-linter 把这条边界钉成 CI 闸门。在那之前它是靠 review 守的，所以本包不放
任何业务逻辑：只有"读上游产物、翻成我们自己的 dataclass"这一件事。
"""
