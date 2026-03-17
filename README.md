# Adoback

macOS 本地 Adobe 项目文件备份守护工具。

在 Adobe 之外建立一层**独立、可控、可配置、可长期运行的兜底备份系统**，降低闪退丢数据、自动保存失效、版本被覆盖等风险。

## 它能做什么

- 全量 / 增量备份 Adobe 项目文件（PSD、AI、INDD、PRPROJ、AEP 等）
- 后台守护模式，定时自动备份
- launchd 开机自启，登录即守护
- 单文件失败自动重试，不拖垮整轮任务
- 带时间戳的快照布局，保留历史版本
- 自动清理过期快照
- 运行摘要、失败报告、JSON 导出
- 系统自检（doctor）
- 交互式新手引导
- **零依赖部署** — 打包为单个二进制，任何 Mac 直接使用

## 它不能做什么

- **无法备份 Adobe 内存中未保存的内容** — 这是所有外部文件级备份工具的天然边界
- 不替代 Adobe 自身的自动保存，而是在其之外多一层兜底
- 不保证 100% 零丢失

## 支持的文件类型

| 应用 | 扩展名 |
|------|--------|
| Photoshop | `.psd` `.psb` |
| Illustrator | `.ai` |
| InDesign | `.indd` `.idml` |
| Premiere Pro | `.prproj` |
| After Effects | `.aep` `.aegraphic` |
| XD | `.xd` |
| 通用 | `.pdf` |

## 安装

选一种你喜欢的，都是一行：

```bash
# npm (有 Node.js 环境)
npx adoback setup

# 或全局安装
npm i -g adoback && adoback setup
```

```bash
# curl (无需任何前置)
curl -fsSL https://raw.githubusercontent.com/SOULRAi/adoback/main/install.sh | bash
```

自动下载 → 安装 → 交互引导 → 完成。

> **它做了什么？** 优先下载预编译二进制（零依赖），如果没有则下载源码并检测 Python 3.10+。安装后运行 `setup` 交互引导你设置备份路径。

<details>
<summary>其他安装方式</summary>

**Clone 后安装：**
```bash
git clone https://github.com/SOULRAi/adoback.git
cd adoback
./install.sh
```

**构建零依赖二进制后安装：**
```bash
git clone https://github.com/SOULRAi/adoback.git
cd adoback
./build.sh && ./install.sh
```

**直接运行源码（需 Python 3.10+）：**
```bash
python3 adoback.py setup
```

</details>

## 快速开始

```bash
# 一条命令搞定（安装 + 配置 + 引导 + 自检）
adoback setup

# 或者手动逐步来:
adoback config init                    # 生成配置模板
nano ~/.local/adoback/config.toml      # 编辑配置
adoback doctor                         # 自检
adoback backup --dry-run               # 试运行
adoback backup --full                  # 全量备份
adoback service on                     # 开机自启
```

## 配置

配置文件位于 `~/.local/adoback/config.toml`，最少只需改两行：

```toml
[source]
# 支持多个目录
roots = [
    "/Users/你的用户名/Documents/Photoshop",
    "/Users/你的用户名/Documents/Premiere"
]

[destination]
root = "/Volumes/BackupDisk/AdobeBackup"
```

### 完整配置项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `source.roots` | （必填） | 待备份的源目录列表（支持多个） |
| `destination.root` | （必填） | 备份输出目录 |
| `destination.layout` | `snapshot` | `snapshot`（按时间戳快照）或 `mirror`（镜像覆盖） |
| `performance.workers` | `4` | 并发复制线程数 |
| `performance.copy_chunk_mb` | `4` | 单次读写分块大小 (MB) |
| `performance.verify` | `mtime_size` | 校验模式：`none` / `mtime_size` / `sha256` |
| `performance.fsync` | `true` | 复制后 fsync 确保落盘 |
| `performance.retry_count` | `2` | 单文件失败重试次数 |
| `performance.retry_delay_ms` | `500` | 重试间隔 (毫秒) |
| `retention.keep_last` | `10` | 保留最近 N 个快照 |
| `retention.keep_days` | `30` | 保留最近 N 天的快照 |
| `schedule.default_interval_seconds` | `300` | 守护模式轮询间隔 (秒) |

## 命令一览

### 核心命令

```bash
adoback setup                # 一键初始化 (首次使用)
adoback backup               # 增量备份
adoback backup --full        # 全量备份
adoback backup --dry-run     # 预演模式
adoback daemon               # 守护模式（前台循环增量备份）
adoback doctor               # 系统自检
```

### 配置管理

```bash
adoback config show          # 显示当前配置
adoback config validate      # 校验配置
adoback config paths         # 显示关键路径及状态
adoback config init          # 生成配置模板
```

### 服务管理

```bash
adoback service on           # 安装并启动服务（开机自启）
adoback service off          # 停止并卸载服务
adoback service status       # 查看服务状态
adoback service restart      # 重启服务
```

### 恢复与清理

```bash
adoback restore              # 交互式选择文件恢复
adoback restore -s "logo"    # 搜索包含 logo 的备份文件
adoback restore --list       # 列出所有备份文件
adoback clean --dry-run      # 预览可清理空间
adoback clean                # 按策略清理过期快照
adoback clean --max-size 10  # 备份总容量上限 10GB
```

### 查看与报告

```bash
adoback last-run             # 最近一次运行
adoback last-run -n 5        # 最近 5 次运行
adoback report               # 查看最新报告
adoback report --list        # 列出所有报告
```

### 其他

```bash
adoback update               # 一键更新到最新版本
adoback update --check       # 仅检查是否有新版本
adoback guide                # 新手引导
adoback guide --interactive  # 交互式引导
adoback uninstall            # 卸载
```

## 退出码

| 代码 | 含义 |
|------|------|
| `0` | 成功 |
| `10` | 部分文件失败 |
| `20` | 锁冲突（另一个实例运行中） |
| `30` | 配置错误 |
| `40` | 运行时错误 |
| `130` | 用户中断 (Ctrl+C) |

## 目录结构

```
~/.local/adoback/
├── bin/
│   └── adoback              # 主程序
└── config.toml              # 配置文件

~/.local/state/adoback/
├── manifest.sqlite3          # 文件变化追踪数据库
├── daemon.lock               # 单实例锁
└── reports/                  # 失败报告
    ├── report_20260317_120000.txt
    └── report_20260317_120000.json

~/Library/Logs/adoback/
├── backup.log.jsonl          # JSONL 结构化日志
├── service.stdout.log        # launchd 标准输出
└── service.stderr.log        # launchd 错误输出
```

## 工作原理

1. 扫描源目录，按扩展名过滤出 Adobe 项目文件
2. 增量模式下，通过 SQLite manifest 比对 `size` + `mtime_ns` 识别变化文件
3. 多线程并发复制，使用临时文件写入 + `os.replace` 原子替换
4. 可选 `fsync` 确保数据落盘，可选 `sha256` 校验
5. 单文件失败自动重试，不影响其他文件
6. 按保留策略自动清理过期快照
7. 记录运行结果到 SQLite，导出失败报告

## 系统要求

- macOS（Big Sur 及以上推荐）
- 二进制模式：无额外依赖
- 源码模式：Python 3.10+

## 项目状态

当前版本 **v0.5.0**，处于**可用原型**阶段。核心备份、恢复、清理、守护、服务化功能已验证可用。

### 已实现

- [x] 全量 / 增量备份
- [x] 多线程并发复制
- [x] 临时文件 + 原子替换
- [x] fsync 持久化
- [x] mtime_size / sha256 校验
- [x] SQLite manifest + runs 记录
- [x] JSONL 结构化日志
- [x] fcntl 单实例锁 + stale lock 回收
- [x] 单文件失败重试
- [x] 快照保留策略
- [x] launchd 服务管理
- [x] 守护模式
- [x] 系统自检 (doctor)
- [x] 失败报告 (文本 + JSON)
- [x] 交互式引导
- [x] 一键 setup 初始化
- [x] 简洁 CLI（backup / service on / config show）
- [x] 终端颜色输出
- [x] PyInstaller 零依赖打包
- [x] 一键安装脚本
- [x] npm 包分发
- [x] 多源目录支持
- [x] 美化 CLI 界面（Unicode 边框对齐、彩色图标）
- [x] 备份恢复 (restore)
- [x] 备份空间清理 (clean)
- [x] 一键更新 (update)

### 计划中

- [ ] macOS 桌面通知
- [ ] 忽略规则 (backup.ignore)
- [ ] TUI 实时仪表盘 (status)
- [ ] FSEvents 实时文件监听（替代轮询）
- [ ] Homebrew Formula
- [ ] APFS clone 优化

## License

MIT
