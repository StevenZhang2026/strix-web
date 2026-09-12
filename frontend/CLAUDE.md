# frontend/CLAUDE.md — 前端局部规则（层三）

根 `CLAUDE.md` 只留一句结论，细则在这里。冲突时以 `PLAN.md` 为准。

## 硬约束

- `strict: true`，**禁止 `any`**；Next.js App Router；服务端组件优先
- **样式只用 CSS Modules + `globals.css` 里的 token 层，不引 Tailwind**（2026-09-09 拍板；理由是 `PLAN.md` §仓库结构 里 `globals.css` 那几行注释）。也**不引 `clsx`/`cva`** —— 变体走级联（`.btn.stop`）
- **构建链只用 Next.js 自带的（Turbopack/webpack），不许引入 Vite / Rollup / esbuild 作为独立构建层** —— 那是第二套互斥的构建体系
- **不许 `next export` / `output: 'export'`** —— 静态导出会废掉服务端组件与 `/scans/[id]` 的 SSR，而 nginx 的 `location /` 是 `proxy_pass` 到 `web:3000` 的**运行中 Node 进程**，不是发静态文件
- compose 里 `web` 只写 `expose: ["3000"]`，**绝不写 `ports`**
- **所有用户可见文案集中在 `messages/zh-CN.json`**，组件里**不得**出现中文字面量（eslint 强制，但**抓不到模板字符串** —— 模板字符串要自己盯）
- **任何文件不许 import `next/headers`**（服务端彻底不碰会话 cookie）；**只有 `src/lib/stores/keys.ts` 可以碰 `window.sessionStorage`**。两条都由 `make lint-web` 的 eslint 封死
- 状态：zustand（客户端）+ react-query（服务端数据）；WS 流单独一个 store，专家 tab 复用同一 store
- Key 输入框 `type=password` + 随机 `name`（破自动填充）；POST 后 `finally` 清空 React state

（`vault_handle` 只进 `sessionStorage`、错误按机器码分支、`make lint-web` 怎么装依赖 —— 这几条在根
`CLAUDE.md` 里常驻，这里不重复，免得两处漂移。）
