<p align="center">
  <h1 align="center">Adoback</h1>
  <p align="center">
    <strong>macOS 本地 Adobe 项目文件备份守护工具</strong>
  </p>
  <p align="center">
    <a href="https://www.npmjs.com/package/adoback"><img src="https://img.shields.io/npm/v/adoback?color=cb3837&label=npm&logo=npm" alt="npm"></a>
    <a href="https://github.com/SOULRAi/adoback/releases"><img src="https://img.shields.io/github/v/release/SOULRAi/adoback?color=blue&logo=github" alt="release"></a>
    <img src="https://img.shields.io/badge/platform-macOS-lightgrey?logo=apple" alt="macOS">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT">
  </p>
</p>

---

> PS 闪退、PR 崩溃、文件被覆盖？装个兜底的。

在 Adobe 之外建立一层**独立、可控、可长期运行的备份系统**，一行安装，开机自动跑，装完就不用管了。

## ✨ 功能亮点

| | 功能 | 说明 |
|---|---|---|
| 🔄 | **增量 / 全量备份** | 只复制变化文件，全量也支持 |
| 🕐 | **快照历史版本** | 带时间戳的快照布局，随时回滚 |
| ♻️ | **一键恢复** | `restore` 交互式选择文件和版本 |
| 🧹 | **空间清理** | `clean` 按策略清理过期快照 |
| 👻 | **后台守护** | launchd 开机自启，登录即守护 |
| 📁 | **多源目录** | PS、PR、AE 项目分开管理 |
| 🔒 | **原子写入** | 临时文件 + fsync + rename，不怕断电 |
| 🚀 | **零依赖** | 单文件二进制，任何 Mac 直接用 |

## 🎯 支持的文件类型

| 应用 | 扩展名 |
|------|--------|
| Photoshop | `.psd` `.psb` |
| Illustrator | `.ai` |
| InDesign | `.indd` `.idml` |
| Premiere Pro | `.prproj` |
| After Effects | `.aep` `.aegraphic` |
| XD | `.xd` |
| 通用 | `.pdf` |

## 📦 安装

选一种你喜欢的，都是一行：

```bash
# npm（有 Node.js 环境）
npx adoback setup

# 或全局安装
npm i -g adoback && adoback setup
```

```bash
# curl（无需任何前置）
curl -fsSL https://raw.githubusercontent.com/SOULRAi/adoback/main/install.sh | bash
```

> **它做了什么？** 优先下载预编译二进制（零依赖），回退到源码 + Python 3.10+。装完自动运行 `setup` 交互引导。

<details>
<summary>📎 其他安装方式</summary>

```bash
# Clone 后安装
git clone https://github.com/SOULRAi/adoback.git
cd adoback && ./install.sh

# 构建零依赖二进制后安装
git clone https://github.com/SOULRAi/adoback.git
cd adoback && ./build.sh && ./install.sh

# 直接运行源码（需 Python 3.10+）
python3 adoback.py setup
```

</details>

## 🚀 快速开始

```bash
# 一条命令搞定（安装 + 配置 + 引导 + 自检）
adoback setup

# 或者手动逐步来
adoback config init                    # 生成配置模板
nano ~/.local/adoback/config.toml      # 编辑配置
adoback doctor                         # 自检
adoback backup --dry-run               # 试运行
adoback backup --full                  # 全量备份
adoback service on                     # 开机自启
```

## ⚙️ 配置

配置文件位于 `~/.local/adoback/config.toml`，最少只需改两行：

```toml
[source]
roots = [
    "/Users/你的用户名/Documents/Photoshop",
    "/Users/你的用户名/Documents/Premiere"
]

[destination]
root = "/Volumes/BackupDisk/AdobeBackup"
```

<details>
<summary>📋 完整配置项</summary>

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `source.roots` | （必填） | 源目录列表（支持多个） |
| `destination.root` | （必填） | 备份输出目录 |
| `destination.layout` | `snapshot` | `snapshot` 按时间戳 / `mirror` 镜像覆盖 |
| `performance.workers` | `4` | 并发复制线程数 |
| `performance.copy_chunk_mb` | `4` | 单次读写分块 (MB) |
| `performance.verify` | `mtime_size` | 校验模式：`none` / `mtime_size` / `sha256` |
| `performance.fsync` | `true` | 复制后 fsync 确保落盘 |
| `performance.retry_count` | `2` | 单文件失败重试次数 |
| `performance.retry_delay_ms` | `500` | 重试间隔 (ms) |
| `retention.keep_last` | `10` | 保留最近 N 个快照 |
| `retention.keep_days` | `30` | 保留最近 N 天的快照 |
| `schedule.default_interval_seconds` | `300` | 守护模式轮询间隔 (秒) |

</details>

## 📖 命令一览

### 🔧 核心

```bash
adoback setup                # ✦ 一键初始化（首次使用）
adoback backup               #   增量备份
adoback backup --full        #   全量备份
adoback backup --dry-run     #   预演模式（不复制）
adoback daemon               #   守护模式（前台循环备份）
adoback doctor               #   系统自检
```

### 🔁 恢复与清理

```bash
adoback restore              #   交互式恢复文件
adoback restore -s "logo"    #   搜索备份文件
adoback restore --list       #   列出所有备份
adoback clean --dry-run      #   预览可释放空间
adoback clean                #   清理过期快照
adoback clean --max-size 10  #   总容量上限 10GB
```

### ⚡ 服务管理

```bash
adoback service on           #   安装并启动（开机自启）
adoback service off          #   停止并卸载
adoback service status       #   查看状态
adoback service restart      #   重启
```

### 📊 查看与报告

```bash
adoback config show          #   显示当前配置
adoback config paths         #   显示关键路径
adoback last-run             #   最近一次运行
adoback last-run -n 5        #   最近 5 次运行
adoback report --list        #   列出所有报告
```

### 🛠 其他

```bash
adoback update               #   一键更新到最新版本
adoback update --check       #   仅检查新版本
adoback guide                #   新手引导
adoback uninstall            #   卸载
```

## 🔍 工作原理

```
源目录 ──扫描──▶ 过滤 Adobe 文件 ──比对 manifest──▶ 变化文件 ──并发复制──▶ 快照目录
                                                                          │
                        SQLite 记录 ◀── 校验 ◀── 原子写入 (tmp+rename) ◀──┘
                            │
                     保留策略自动清理
```

1. 扫描源目录，按扩展名过滤出 Adobe 项目文件
2. 增量模式下，通过 SQLite manifest 比对 `size` + `mtime_ns` 识别变化文件
3. 多线程并发复制，临时文件写入 + `os.replace` 原子替换
4. 可选 `fsync` 落盘 + `sha256` 校验
5. 单文件失败自动重试，不影响其他文件
6. 按保留策略自动清理过期快照

## 📂 目录结构

```
~/.local/adoback/
├── bin/adoback              # 主程序
└── config.toml              # 配置文件

~/.local/state/adoback/
├── manifest.sqlite3         # 文件变化追踪
├── daemon.lock              # 单实例锁
└── reports/                 # 运行报告

~/Library/Logs/adoback/
├── backup.log.jsonl         # JSONL 结构化日志
├── service.stdout.log       # launchd 标准输出
└── service.stderr.log       # launchd 错误输出
```

## 🖥 系统要求

- macOS Big Sur 及以上
- **二进制模式**：无额外依赖
- **源码模式**：Python 3.10+

## 📊 退出码

| 代码 | 含义 |
|:----:|------|
| `0` | 成功 |
| `10` | 部分文件失败 |
| `20` | 锁冲突（另一实例运行中） |
| `30` | 配置错误 |
| `40` | 运行时错误 |
| `130` | 用户中断 (Ctrl+C) |

## 📍 路线图

- [x] 全量 / 增量备份、多线程并发、原子写入
- [x] SQLite manifest + JSONL 日志 + 单实例锁
- [x] launchd 服务管理 + 守护模式
- [x] 一键 setup + 交互引导 + 系统自检
- [x] PyInstaller 零依赖打包 + npm 分发
- [x] 多源目录 + 美化 CLI
- [x] 备份恢复 (restore) + 空间清理 (clean)
- [x] 一键更新 (update)
- [ ] macOS 桌面通知
- [ ] 忽略规则 (backup.ignore)
- [ ] TUI 实时仪表盘
- [ ] FSEvents 文件监听（替代轮询）
- [ ] Homebrew Formula

## 🤝 它不能做什么

- **无法备份 Adobe 内存中未保存的内容** — 这是所有外部备份工具的天然边界
- 不替代 Adobe 自身的自动保存，而是在其之外多一层兜底
- 不保证 100% 零丢失

## License

MIT
