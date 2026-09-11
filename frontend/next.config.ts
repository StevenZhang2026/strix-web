import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /**
   * `standalone` 会把运行期真正用到的那些 `node_modules` 文件挑出来放进
   * `.next/standalone`，运行时镜像里就不需要 `node_modules` 整棵树。
   * 这直接决定了 `Dockerfile` 第三阶段只 COPY 三样东西。
   *
   * 刻意**不用** `output: "export"`：静态导出要求每个动态段在**构建期**就能被
   * `generateStaticParams` 枚举出来，而 T25 的 `/scans/[id]` 里的 id 是运行期才产生的
   * 扫描记录 —— 构建时那份清单必然是空的。
   *
   * ⚠️ 这里原先写的理由是"以后 T5b 要加 middleware（没 cookie 就重定向）时会无处可放"。
   * **那条理由已作废**：T5b 拍板本项目不加 middleware（一个都不加），未登录跳转在
   * `src/components/auth/RequireSession.tsx`，论证全文见 `src/lib/api/client.ts` 第二节。
   * 结论（用 `standalone`）没变，但它现在靠的是上面那条，不是一个不会发生的需求。
   */
  output: "standalone",

  // 遥测由环境变量 `NEXT_TELEMETRY_DISABLED=1` 关掉（`next.config` 里没有对应开关）。
  // Dockerfile 在构建阶段与运行阶段各设一次；本机开发靠 `npm run dev` 前置的同名变量。
  // 「不引入向任何第三方外发数据的代码路径」是这个项目存在的理由之一（CLAUDE.md §禁区）。

  eslint: {
    /**
     * 镜像构建**不跑 eslint**。
     *
     * 不是嫌它慢：lint 是一道独立的门（`npm run lint` / CI），构建产物的正确性
     * 不该依赖 lint 工具链能不能装起来。而 `eslint-config-next` 那一串插件里带
     * 原生二进制（`unrs-resolver`），在 npm 11 的 allow-scripts 门下默认不执行
     * postinstall —— 让它决定镜像能不能构建出来是把两件事绑在了一起。
     *
     * **类型检查照样跑**（下面没有 `typescript.ignoreBuildErrors`）：那才是真正
     * 能拦住 bug 的那道门。
     */
    ignoreDuringBuilds: true,
  },

  /**
   * 生产构建不带 source map。它会把完整源码（含全部中文注释）发到浏览器，
   * 而这些注释里写满了安全设计的推理过程。本机工具不需要线上调试能力。
   */
  productionBrowserSourceMaps: false,

  /** `X-Powered-By: Next.js` 没有任何用处，只是白送一条版本指纹。 */
  poweredByHeader: false,
};

export default nextConfig;
