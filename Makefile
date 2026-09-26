# Strix Web 控制台 —— T0 提供 lock 目标，T2 追加 lock-dev / test / lint。
# reap 由 T30 追加。
.PHONY: lock lock-resolve lock-hash lock-check lock-dev build-test test lint verify-e2e

# 基础镜像 digest 从 Dockerfile 抠出来，保持单一真源：
# 解析环境与安装环境必须是同一个 Python patch 版本和同一个 pip。
BASE_IMAGE := $(shell grep -oE 'python:3\.12-slim@sha256:[0-9a-f]{64}' backend/Dockerfile | head -1)
PIP_TOOLS_VERSION := 7.6.1

PINS := backend/pins.txt
LOCK := backend/requirements.lock
PINS_DEV := backend/pins-dev.txt
LOCK_DEV := backend/requirements-dev.lock

# test stage 的镜像 tag。刻意与生产镜像（strix-console/api:0.1.0）不同名 ——
# 生产镜像里不许有 pytest/ruff，两者混用一个 tag 迟早把测试依赖发到生产。
IMAGE_TEST := strix-console/api-test:0.1.0

# =============================================================================
# 生成 backend/requirements.lock —— 刻意分成"解析"和"算哈希"两步。
#
# 为什么不是一条 `pip-compile --generate-hashes`（它本该一步做完这件事）：
#   pip-tools 算哈希时是"JSON API 优先，拿不到就下载整个文件"，而取 JSON 的那一步
#   **失败时静默返回空字典**。本机这种"能连通但会中途断流"的网络上，一次请求失败就让
#   整棵依赖树 × 全部平台的 wheel 全都走下载路径 —— 实测 45 分钟没跑完。
#   拆开之后两步合计约 4 分钟。完整根因见 pitfalls/history-pitfalls.md 条 11。
#
# 为什么解析必须在容器里：宿主 Python 是 3.9.6，而 strix-agent 要求 >=3.12，解析器
# 根本起不来；即便起得来，macOS 上会挑 macosx_11_0_arm64 wheel，对 Linux 镜像无用。
#
# 为什么算哈希反而在宿主：scripts/gen_lock.py 只用标准库，3.9 就能跑。它是要跟 PyPI
# 反复重试的那一步，放宿主省掉一层容器网络，失败信息也直接可见。
#
# 刻意不写 --platform：这条命令要在 arm64 与 x86_64 宿主上都能跑，且产出与运行架构
# 无关（哈希覆盖全部平台，由 lock-check 把关），所以让 Docker 用宿主默认架构即可。
# =============================================================================
lock: lock-resolve lock-hash lock-check

# 第一步：只解析，产出 name==version。
#
# 不加 --generate-hashes 是本目标的全部要点。这样 pip-tools 停在解析阶段，靠 PEP 658
# 只取 .whl.metadata（每个 2–110 kB），不碰 wheel 本体。
#
# PIP_ONLY_BINARY=:all: 在解析期就排除 sdist —— 否则 pip-tools 会为拿元数据去构建
# sdist，strix-agent 的 hatch 钩子缺 Go 1.24 会直接失败（见 pitfalls 条 4）。
#
# 缓存用 named volume 是安全的：这条路径从不交给宿主 daemon 去挂载，不涉及路径别名
# 问题（见 pitfalls 条 5）。中断后重跑会累积进度，所以失败就再跑一次。
#
# 显式 --name：客户端超时**不会**停掉容器，没名字就成孤儿继续占带宽。
lock-resolve:
	@test -n "$(BASE_IMAGE)" || { echo "错误：无法从 backend/Dockerfile 解析基础镜像 digest。" >&2; exit 1; }
	-docker rm -f strix-lock-resolve >/dev/null 2>&1
	docker run --rm --name strix-lock-resolve \
	  -v "$(CURDIR)/backend:/work" -w /work \
	  -v strix_console_lockcache:/root/.cache \
	  -e PIP_TIMEOUT=120 -e PIP_RETRIES=8 -e PIP_ONLY_BINARY=:all: \
	  $(BASE_IMAGE) \
	  sh -eu -c 'pip install -q --disable-pip-version-check --root-user-action=ignore "pip-tools==$(PIP_TOOLS_VERSION)" && \
	    pip-compile --strip-extras --no-header --output-file /work/$(notdir $(PINS)) pyproject.toml'

# 第二步：按 name==version 去 PyPI JSON API 取全部平台的 sha256，写出 lock。
lock-hash:
	@test -s $(PINS) || { echo "错误：$(PINS) 不存在或为空，先跑 make lock-resolve。" >&2; exit 1; }
	python3 scripts/gen_lock.py $(PINS) $(LOCK)

# 第三步：把 lock 的不变量当成断言，而不是当成文档。
# 坏 lock 的失败模式全是"安静的" —— 少了 x86_64 哈希，只有换架构的人才会撞上。
lock-check:
	python3 scripts/check_lock.py $(PINS) $(LOCK)

# =============================================================================
# 生成 backend/requirements-dev.lock —— 运行时依赖 + [dev] extra（pytest / ruff）。
#
# 为什么 dev lock 是**全集**而不是"只有 pytest/ruff 两行"：
#   Dockerfile 的 test stage 继承 runtime，运行时那 85 个包已经装好了。用全集 lock
#   安装时 pip 对它们只打印 "Requirement already satisfied"，不下载；但 --require-hashes
#   会**逐个核对版本号**。也就是说，全集 lock 顺带把"dev 解析没有偷偷动运行时版本"
#   变成了安装期的硬断言。只写两行就没有这层保护。
#
# 为什么首次要 cp pins.txt → pins-dev.txt：
#   pip-compile 把已存在的 output-file 当成约束。不预置的话，加 --extra dev 是一次全新
#   解析，完全可能选出与 pins.txt 不同的传递依赖版本（例如 packaging）。那会让 test 镜像
#   在安装期降级运行时包 —— 测的就不是要发布的那套依赖了。
#   预置之后，运行时的 85 个 pin 被钉死，只有 pytest/ruff 及其依赖是新解析的。
# =============================================================================
lock-dev:
	@test -n "$(BASE_IMAGE)" || { echo "错误：无法从 backend/Dockerfile 解析基础镜像 digest。" >&2; exit 1; }
	@test -s $(PINS) || { echo "错误：$(PINS) 不存在或为空，先跑 make lock。" >&2; exit 1; }
	@test -f $(PINS_DEV) || cp $(PINS) $(PINS_DEV)
	-docker rm -f strix-lock-resolve-dev >/dev/null 2>&1
	docker run --rm --name strix-lock-resolve-dev \
	  -v "$(CURDIR)/backend:/work" -w /work \
	  -v strix_console_lockcache:/root/.cache \
	  -e PIP_TIMEOUT=120 -e PIP_RETRIES=8 -e PIP_ONLY_BINARY=:all: \
	  $(BASE_IMAGE) \
	  sh -eu -c 'pip install -q --disable-pip-version-check --root-user-action=ignore "pip-tools==$(PIP_TOOLS_VERSION)" && \
	    pip-compile --strip-extras --no-header --extra dev --output-file /work/$(notdir $(PINS_DEV)) pyproject.toml'
	python3 scripts/gen_lock.py $(PINS_DEV) $(LOCK_DEV)
	python3 scripts/check_lock.py $(PINS_DEV) $(LOCK_DEV)
	@python3 -c "import sys; sys.path.insert(0, 'scripts'); \
	from check_lock import read_pins; \
	rt = read_pins('$(PINS)'); dv = read_pins('$(PINS_DEV)'); \
	bad = sorted((n, v, dv.get(n, '缺失')) for n, v in rt.items() if dv.get(n) != v); \
	[print('  %s: 运行时 %s -> dev %s' % b) for b in bad]; \
	sys.exit('dev 解析改动了运行时版本，见上。' if bad else 0)"
	@echo "✓ dev lock 未改动任何运行时 pin"

# =============================================================================
# 测试与 lint —— 都在一次性容器里跑。
#
# 宿主 Python 是 3.9.6 且没有 pytest/ruff，后端代码要求 3.12，本机跑不了（CLAUDE.md
# §环境）。所以"跑测试"就等于"构建 test stage 再 docker run --rm"。
#
# 刻意不用 `docker compose exec api` ——
#   1. 生产镜像里没有 pytest/ruff，也不该有；
#   2. 那会依赖一个长期在跑的容器，测试就不再是可重复的了。
# =============================================================================
build-test:
	docker build --target test -t $(IMAGE_TEST) backend

# 只读挂前端文案表：`test_message_coverage.py` 要拿 `frontend/messages/zh-CN.json`
# 跟 `app/errors.py` 的机器码做双向比对，而 build-test 的构建上下文只有 `backend/`，
# 镜像里没有它。不挂的话那 13 个测试全 skip，而 skip 会让 `make test` 照样退 0 ——
# 一个"看起来在跑、其实没在跑"的守卫（这正是它被加进来要防的那类事）。
#
# 用环境变量指路，不靠测试自己从 `__file__` 往上找仓库根：镜像里 `WORKDIR=/app`、
# app 在 /app/app、tests 在 /app/tests，`__file__` 往上找几层的结论取决于这个布局 ——
# 一个会在下次改 Dockerfile 时静默失效的假设。挂到一个固定的中立路径再显式告诉它，
# 没有可推导的余地。（2026-09-14 修：这里原写「tests 在 /work」，与 Dockerfile 的
# `WORKDIR /app` + `COPY tests ./tests` 不符，害得 T9 方案里的快闸门挂错了挂载点。）
# （路径不是机密，走 -e 没问题；凭据绝不这样传。）
test: build-test
	docker run --rm \
	  -v "$(PWD)/frontend/messages:/messages:ro" \
	  -e CONSOLE_MESSAGES_JSON=/messages/zh-CN.json \
	  $(IMAGE_TEST) pytest

lint: lint-api lint-web

lint-api: build-test
	docker run --rm $(IMAGE_TEST) sh -eu -c 'ruff check . && ruff format --check .'

# 前端闸门。**这条 target 是「服务端彻底不碰 cookie」那条裁决的唯一执行点** ——
# 它靠 eslint 的 `no-restricted-imports` 禁掉 `next/headers`，而 `next.config.ts` 刻意
# 设了 `eslint.ignoreDuringBuilds: true`（lint 不该由原生二进制的 postinstall 门决定
# 镜像能不能构建）。两件事合起来的后果是：没有这条 target，那些边界规则在任何自动
# 流程里都不会跑，「用工具封死」就退化成「靠人记」——而那正是它要替代的东西。
#
# 用宿主 node（v24.19.0，CLAUDE.md §环境 里声明的版本），不另起镜像：这里跑的是
# Node 工具链，不是 Python 业务代码，「后端必须容器化」那条约束不适用。
#
# 缺 node_modules 时**硬失败并给出命令**，不 skip。理由同 test 的只读挂载：
# 一个会静默跳过的检查等于没有检查。
#
# ⚠️ 全新克隆上 `npm run typecheck` 必须在**至少一次 `npm run build` 之后**跑 ——
# `next-env.d.ts` 里有一行 `/// <reference path="./.next/types/routes.d.ts" />`，
# 是 `next build` 自己写进去的。T30b 写验收脚本时注意这个顺序。
lint-web:
	@test -d frontend/node_modules || { \
	  echo "frontend/node_modules 不存在。先跑：(cd frontend && npm ci --registry=https://registry.npmjs.org/)"; \
	  exit 1; }
	cd frontend && npm run lint && npm run typecheck

# 端到端验收（PLAN.md §端到端验收）。由人在终端里跑：要登录的检查会用 read -rs 问口令。
# ONLY="22 23" 只跑列出的几条；TEARDOWN=1 额外跑 21（compose down，拆栈）。
verify-e2e:
	./scripts/verify_e2e.sh
