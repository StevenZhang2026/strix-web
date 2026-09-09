# syntax=docker/dockerfile:1
#
# 前端镜像。三段：deps / build / runtime。
#
# 基镜像按 **manifest-list digest** pin（`node:24-slim` 本来就是多架构，digest 指的是
# 那张 manifest list，所以 arm64 与 x86_64 都能用同一行 —— 与 CLAUDE.md
# 「宿主架构不得假定」一致，不写 `--platform`）。
# 标签 `24-slim` 对应宿主的 Node v24.19.0，构建期与开发机同一个大版本。
ARG NODE_IMAGE=node:24-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e

# =============================================================================
# 一、deps —— 只装依赖
#
# 单独一段是为了让这一层的缓存键只由 `package.json` + `package-lock.json` 决定。
# 改一行组件代码不该重新走一次 `npm ci`（本机出网约 250 KB/s，那是几分钟）。
# =============================================================================
FROM ${NODE_IMAGE} AS deps
WORKDIR /app

ENV NEXT_TELEMETRY_DISABLED=1

COPY package.json package-lock.json ./

# `npm ci` 而不是 `npm install`：前者严格按 lock 装，装不出来就失败，不会悄悄升版本。
# 不设任何 registry —— 镜像里没有宿主的 `~/.npmrc`，默认就是官方源。
# （宿主那份 npmrc 指向一个被企业防火墙解密的镜像站，见本轮报告；不许把它带进镜像。）
RUN npm ci --no-audit --no-fund

# =============================================================================
# 二、build —— 出 standalone 产物
# =============================================================================
FROM ${NODE_IMAGE} AS build
WORKDIR /app

ENV NEXT_TELEMETRY_DISABLED=1
# 让 `next build` 走生产分支（压缩、不注入 dev 运行时）。
ENV NODE_ENV=production

COPY --from=deps /app/node_modules ./node_modules
COPY package.json package-lock.json next.config.ts tsconfig.json next-env.d.ts ./
COPY messages ./messages
COPY src ./src

# 这一步同时做**类型检查**（`next.config.ts` 里没有 `typescript.ignoreBuildErrors`），
# 所以类型错会让镜像构建失败。eslint 刻意不在这里跑，理由见 `next.config.ts`。
RUN npm run build

# =============================================================================
# 三、runtime —— 只带运行期真正要的东西
# =============================================================================
FROM ${NODE_IMAGE} AS runtime
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
# standalone 的 `server.js` 会拿 `HOSTNAME` 当监听地址。Docker 默认往容器里注入
# `HOSTNAME=<容器 id>`，那个名字解析不到可监听的地址。这里显式覆盖成 0.0.0.0。
# compose 里也写了同一行 —— 两处都要，因为 `docker run` 直接跑这个镜像时没有 compose。
ENV HOSTNAME=0.0.0.0

# `node:24-slim` 自带 uid/gid 1000 的 `node` 用户。不新建用户，少一层可出错的地方。
USER node

# standalone 产物两样，缺一不可：
#   · `.next/standalone` —— server.js + 精简过的 node_modules
#   · `.next/static`     —— 客户端 JS / CSS（standalone **不含**它，是 Next 的已知行为）
#
# 刻意**没有 `public/`**：本项目暂时没有任何静态文件。图标走 App Router 的
# `src/app/icon.svg`（构建期生成路由），截图由后端出。空目录 git 不跟踪，
# 放一个 `.gitkeep` 只为了让 COPY 不失败是典型的死代码。以后真有静态文件再加回来。
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static

EXPOSE 3000

# 刻意**没有 HEALTHCHECK**：Next.js 没有内建健康端点，自己造一个就等于在前端到后端
# 之间多开一条路径（或者多一个只为探活存在的路由）。nginx 反代不通时会报 502，
# 那已经是我们需要的信号。
CMD ["node", "server.js"]
