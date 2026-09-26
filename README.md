# Strix Web 控制台

给 [Strix](https://github.com/usestrix/strix) 加一层本地 Web 控制台：向导式发起渗透测试、
实时进度可视化、中文报告、授权护栏。

核心设计约束是三条：**LLM 可切换**、**谁用谁的 Key**、**Key 绝不落盘**。

---

## ⚠️ 合规声明

Strix 会**真实攻击**你指向的目标。仅限对**自己拥有或已获书面授权**的系统使用，并严格遵守约定范围。
未授权测试在多数司法辖区违法。授权与合规责任完全由使用者承担。
本工具只能**记录**你的授权声明，**无法验证**授权真实性。

---

## 环境要求

- macOS 或 Linux，且都要求 **Docker Desktop**。**原生 Windows 不支持**（路径含冒号，
  同路径挂载不成立 —— 请在 WSL2 内运行）。
- Docker VM 内存 ≥ 4 GB（推荐 8 GB）、CPU ≥ 2 核（推荐 4 核）、数据目录可用空间 ≥ 10 GB（推荐 20 GB）。
  "Docker VM 内存"是 `docker info` 的 `MemTotal`，不是宿主物理内存。
- 这些由 `./setup.sh` 强制校验，低于阻断线会直接退出。细节见 [运维手册](docs/OPERATIONS-zh.md)。

## 快速开始

```sh
# 1. 校验环境、生成 .env、签发自签证书、创建登录账号（交互式，按提示输入）
./setup.sh

# 2. 启动
docker compose -p strix-console up -d api web nginx
```

3. **信任自签证书**（一次性，手动）。证书在 `${STRIX_HOST_DATA_DIR}/tls/cert.pem`，
   `setup.sh` 结尾也会打印这条命令。**脚本不会替你改系统信任库**，这一步必须由你自己执行。
   macOS：

   ```sh
   sudo security add-trusted-cert -d -r trustRoot \
     -k /Library/Keychains/System.keychain "${STRIX_HOST_DATA_DIR}/tls/cert.pem"
   ```

   Linux 的做法（系统信任库 + Chrome 的 NSS 库）以 `setup.sh` 打印的命令为准。

4. 打开 `https://127.0.0.1/`，用第 1 步建的账号登录。LLM 的 API Key 在发起扫描时于网页里输入。

---

## 安全姿态

这个工具**只为本机使用设计**：对外只经 `nginx` 终止 TLS，绑 `127.0.0.1`，不监听 80，
`api` / `web` 不发布端口；单账号登录；没有多用户与数据隔离。LLM API Key 只存在于 `api` 进程内存
与扫描子进程的环境变量里 —— 不进数据库、不进日志、不进命令行参数、不进 `.env`、不进镜像，
代价是 `api` 重启后需要重新输入。**Strix 需要 docker.sock 来创建沙箱容器，而挂 docker.sock
约等于宿主 root** —— 只在自己的机器上运行，不要暴露到局域网。
完整的威胁模型与已知且接受的残余风险见 [`docs/SECURITY-zh.md`](docs/SECURITY-zh.md)。

---

## 与 Strix 的关系

- 本项目**依赖** `strix-agent`（Apache-2.0），**精确 pin 在 `1.6.2`**（hash lock），不修改也不分发它的代码。
  升级走固定流程，见运维手册「Strix 版本升级」。
- 通过 Strix 的命令行接口调用它（每次扫描一个独立子进程），不内嵌它的 Python 运行时。
- **不包含任何向第三方外发数据的代码路径**：`STRIX_TELEMETRY=false`、`STRIX_NO_UPDATE_CHECK=1` 是强制的。
- **不代理** Strix 自带的 Web 界面，扫描详情与报告由本控制台自己渲染。

本仓库自己的代码是 MIT（见 [`LICENSE`](LICENSE)）。

---

## 文档索引

| 文档 | 内容 |
|---|---|
| [`docs/SECURITY-zh.md`](docs/SECURITY-zh.md) | 安全模型、凭据处理、已知残余风险 |
| [`docs/OPERATIONS-zh.md`](docs/OPERATIONS-zh.md) | 安装、主机要求、启停升级、日志、数据目录、留存清理、故障排查 |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 架构 |
| [`docs/STRIX-INTEGRATION.md`](docs/STRIX-INTEGRATION.md) | 与 Strix 的集成契约 |
