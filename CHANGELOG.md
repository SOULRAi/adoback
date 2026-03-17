# Changelog

## v0.3.0 (2026-03-17)

### 新增

- **项目更名** — `adobe-backup` → `Adoback`，CLI 命令、npm 包名、目录路径全部统一为 `adoback`
- **一行远程安装** — `curl -fsSL .../install.sh | bash` 或 `npx adoback setup`，自动下载 + 安装 + 交互引导
- **npm 包分发** — `npm i -g adoback`，postinstall 自动下载二进制，cli.js 薄壳路由
- **`setup` 一键初始化命令** — 安装 + 生成配置 + 交互设置路径 + 配置 PATH + 自检，首次使用一条命令搞定
- **`backup` 智能备份命令** — 默认增量备份，`--full` 全量，`--dry-run` 预演，合并原 `full` / `incremental`
- **`config` 子命令组** — `config show` / `config validate` / `config paths` / `config init`，替代原 `config-*` 系列
- **`service` 子命令组** — `service on`（安装并启动）/ `service off`（停止并卸载）/ `service status` / `service restart`
- **首次运行检测** — 无配置文件时自动提示 `setup`，降低新手门槛

### 变更

- `install.sh` 重写为远程安装脚本，支持 `curl | bash` 一行安装
- 所有旧命令（`full`、`incremental`、`config-show`、`service-install` 等）保留为隐藏别名，完全向后兼容
- 本地目录从 `~/.local/adobe-backup/` 改为 `~/.local/adoback/`
- launchd 服务标签从 `com.local.adobe-backup` 改为 `com.local.adoback`

### 命令对照

| 旧命令 | 新命令 |
|--------|--------|
| `adobe-backup full` | `adoback backup --full` |
| `adobe-backup incremental` | `adoback backup` |
| `adobe-backup incremental --dry-run` | `adoback backup --dry-run` |
| `adobe-backup config-init` | `adoback config init` |
| `adobe-backup config-show` | `adoback config show` |
| `adobe-backup config-validate` | `adoback config validate` |
| `adobe-backup config-paths` | `adoback config paths` |
| `adobe-backup service-install` + `service-start` | `adoback service on` |
| `adobe-backup service-stop` + `service-uninstall` | `adoback service off` |
| `adobe-backup service-status` | `adoback service status` |
| `adobe-backup service-restart` | `adoback service restart` |

---

## v0.2.0 (2026-03-17)

### 新增

- 终端彩色输出（自动检测 TTY）
- 多路径配置文件搜索（`./config.toml` → `~/.local/adobe-backup/config.toml` → `~/.config/adobe-backup/config.toml`）
- PyInstaller 零依赖二进制打包支持（`build.sh`）
- `install.sh` 一键安装脚本（自动检测二进制/源码模式）
- `install` / `uninstall` 命令
- launchd plist 自动适配二进制/源码模式
- `doctor` 自检增强：显示版本、运行模式、文件扫描统计
- `config-paths` 显示路径存在状态

---

## v0.1.0 (2026-03-17)

### 初始版本

- 全量 / 增量备份
- 多线程并发复制（ThreadPoolExecutor）
- 临时文件 + `os.replace` 原子替换
- `fsync` 持久化写入
- `mtime_size` / `sha256` 校验模式
- SQLite manifest 文件变化追踪
- SQLite runs 运行记录
- JSONL 结构化日志
- `fcntl.flock` 单实例锁 + stale lock 回收
- 单文件失败自动重试
- 快照保留策略（按数量 + 按天数）
- launchd 服务管理（install / start / stop / restart / status / uninstall）
- 守护模式（daemon）
- 系统自检（doctor）
- 失败报告导出（文本 + JSON）
- 交互式新手引导（guide --interactive）
- 零依赖 TOML 解析器
- 仅 macOS，中文优先 CLI
