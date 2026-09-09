// @ts-check
/**
 * ESLint 扁平配置。
 *
 * 这个文件在本项目里**不只是代码风格检查**，它是三条服务端 / 客户端边界规则的
 * 唯一机械执行者 —— 靠人记的规则等于没有规则（PLAN.md T5 拍板）。
 *
 * -----------------------------------------------------------------------------
 * 关于 `@eslint/eslintrc`：`eslint-config-next` 15.5 只发 eslintrc 老格式
 * （包里就 `index.js` / `core-web-vitals.js` / `typescript.js`，没有任何 flat 入口），
 * 要在扁平配置里用它必须经 `FlatCompat`。`@eslint/eslintrc` 是 `eslint` 自己的直接
 * 依赖，所以它一定在 `node_modules` 里；但它**没有**列进 `package.json` 的
 * devDependencies —— 这是本轮批准的依赖清单里漏掉的一行，已如实报回主会话，
 * 未经批准不自行安装（清单外的包一律先报）。
 */

import { FlatCompat } from "@eslint/eslintrc";

const compat = new FlatCompat();

/** 中日韩统一表意文字。用来机械地挡住"组件里出现中文字面量"。 */
const CJK = /[一-鿿]/;

/**
 * 规则一：任何文件都不许 import `next/headers`。
 *
 * 这是"服务端彻底不碰 cookie"（方案 B）的执行点。`next/headers` 是服务端读
 * cookie 的唯一入口，封死它，`web` 容器"持有零个凭据"就从一句约定变成结构性事实。
 * 理由全文见 `src/lib/api/client.ts` 顶部第一节。
 */
const forbidNextHeaders = {
  paths: [
    {
      name: "next/headers",
      message:
        "服务端不碰 cookie（方案 B）。需要鉴权的数据一律经 src/lib/api/client.ts 在客户端取；服务端组件只出静态骨架。要改这条先读 client.ts 顶部第一节。",
    },
  ],
};

// 具名之后再导出：`eslint-config-next` 带的 `import/no-anonymous-default-export`
// 会对匿名数组默认导出报警，而它说得对 —— 具名的东西在栈里可读。
const config = [
  {
    ignores: [".next/**", "node_modules/**", "next-env.d.ts", "out/**"],
  },

  ...compat.extends("next/core-web-vitals", "next/typescript"),

  {
    // 规则一是全局的，没有例外。
    rules: {
      "no-restricted-imports": ["error", forbidNextHeaders],
    },
  },

  {
    /**
     * 规则二 + 规则三，作用于 `app/` 下默认是服务端组件的那些文件。
     *
     * 规则二（服务端默认）：这些文件里不许出现 `"use client"`。唯一例外是
     * `providers.tsx` —— react-query 的 Provider 必须是客户端组件。
     *
     * 规则三（引用 store 就必须是客户端组件）：eslint 看不到"这个文件是不是客户端
     * 组件"，但它能看到"一个默认是服务端组件的文件 import 了 store"。import store
     * 的唯一理由是要调它的 hook，而 hook 在服务端组件里必然运行时炸。
     * 所以这里挡的是那件事真正会发生的地方，不是它的近似。
     */
    files: [
      "src/app/**/page.tsx",
      "src/app/**/layout.tsx",
      "src/app/**/template.tsx",
      "src/app/**/not-found.tsx",
    ],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          paths: forbidNextHeaders.paths,
          patterns: [
            {
              group: ["@/lib/stores/*", "**/lib/stores/*"],
              message:
                "app/ 下的 page/layout 默认是服务端组件，不能调 zustand 的 hook。把用到 store 的那一小块拆成 components/ 里的客户端叶子组件。",
            },
          ],
        },
      ],
      "no-restricted-syntax": [
        "error",
        {
          selector: 'Program > ExpressionStatement[directive="use client"]',
          message:
            "服务端组件优先。整棵 app/ 只有 providers.tsx 允许是客户端组件；需要交互就下沉到 components/ 里的叶子组件。",
        },
      ],
    },
  },

  {
    /**
     * `lib/` 里除了 store 之外都不该带 `"use client"`。
     *
     * `lib/api/client.ts`、`lib/format.ts` 这些是**普通模块**，不是组件。给它们加
     * 指令会把所有 import 它们的服务端文件一起拖过边界，而它们本来在两边都能用。
     */
    files: ["src/lib/**/*.ts"],
    ignores: ["src/lib/stores/*.ts"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector: 'Program > ExpressionStatement[directive="use client"]',
          message:
            'lib/ 下只有 stores/ 允许带 "use client"。其余是普通模块，两侧都能 import。',
        },
      ],
    },
  },

  {
    /**
     * 组件与页面里**不许出现中文字面量** —— 全部走 `messages/zh-CN.json`
     * （CLAUDE.md §TypeScript）。这条以前只写在文档里，现在由 esquery 的正则
     * 属性匹配机械地挡住：字符串字面量和 JSX 文本各一条。
     *
     * 注意作用域只到 `app/` 与 `components/`：
     *   · 注释不是 AST 节点，所以中文注释不受影响（本项目大量中文注释是刻意的）；
     *   · 模板字符串里的中文也抓不到，那里只用于开发者可见的不变量报错
     *     （如 `apiFetch` 拒绝非站内路径），不是用户文案。
     */
    files: ["src/app/**/*.{ts,tsx}", "src/components/**/*.{ts,tsx}"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector: 'Program > ExpressionStatement[directive="use client"]',
          message:
            "服务端组件优先；客户端指令只允许出现在 components/ 的叶子组件和 app/providers.tsx。",
        },
        {
          selector: `Literal[value=${CJK.toString()}]`,
          message:
            "组件里不许写中文字面量。加一条 key 到 messages/zh-CN.json，用 t() 取。",
        },
        {
          selector: `JSXText[value=${CJK.toString()}]`,
          message:
            "JSX 里不许写中文文本。加一条 key 到 messages/zh-CN.json，用 {t(...)} 取。",
        },
      ],
    },
  },

  {
    /**
     * 允许 `"use client"` 的白名单，把上一块的那条撤掉，只留中文字面量两条。
     * 层叠顺序：后面的块覆盖前面的同名规则。
     *
     * `app/` 下只有三个文件在里面，每一个都有硬理由：
     *   · `providers.tsx`   —— react-query 的 Provider 必须在客户端。
     *   · `error.tsx`       —— Next 规定错误边界**必须**是客户端组件（它要接
     *     `reset()` 回调）。这不是我们的选择，是框架的要求。
     *   · `global-error.tsx` —— 同上。本轮没建，先把位置留在规则里，
     *     免得 T17 建它的时候以为是自己写错了。
     */
    files: [
      "src/components/**/*.tsx",
      "src/app/providers.tsx",
      "src/app/error.tsx",
      "src/app/global-error.tsx",
    ],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector: `Literal[value=${CJK.toString()}]`,
          message:
            "组件里不许写中文字面量。加一条 key 到 messages/zh-CN.json，用 t() 取。",
        },
        {
          selector: `JSXText[value=${CJK.toString()}]`,
          message:
            "JSX 里不许写中文文本。加一条 key 到 messages/zh-CN.json，用 {t(...)} 取。",
        },
      ],
    },
  },
];

export default config;
