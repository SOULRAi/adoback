# Adoback 更新计划

> 按优先级排列，✅ 已完成 / 📋 计划中 / 🧊 冰箱

---

## ✅ 已完成

### v0.3.0 — CLI 简化 + 一键安装 + npm 分发
- [x] 命令简化：setup / backup / config / service 分组
- [x] `curl | bash` 一行远程安装
- [x] npm 包分发（npx adoback setup）
- [x] 首次运行检测 + 交互引导

### v0.4.0 — 多源目录 + CLI 美化 + 自动更新
- [x] 多源目录支持（source.roots = [...]）
- [x] CLI 界面美化：Unicode 边框对齐、彩色图标
- [x] 交互式多目录设置
- [x] 口语化引导文案
- [x] `update` 一键更新命令

### v0.5.0 — 备份恢复 + 空间清理
- [x] `restore` 交互式恢复（搜索、分页、多版本选择、恢复前自动 .bak）
- [x] `clean` 按策略清理过期快照（keep_last / keep_days / max_size / --dry-run / --force）

### v0.5.2 — 通知 + 忽略规则
- [x] macOS 桌面通知（osascript 原生通知中心）
  - 备份完成/失败/异常通知
  - 磁盘空间不足警告（可配阈值）
  - 守护模式运行时异常通知
  - `[notification]` 配置段，可精细开关
- [x] 忽略规则 .adobackignore
  - 类似 .gitignore 语法（通配符、目录模式）
  - 内置 25+ 条默认忽略（Media Cache、Peak Files 等）
  - 支持源目录 + 全局 .adobackignore
  - scan_files 自动过滤，减少备份体积

### v0.5.4 — TUI 状态面板 + FSEvents 实时监听
- [x] `adoback status` TUI 状态面板
  - 服务运行状态（PID、是否活跃）
  - 监控目录概览（各源目录 Adobe 文件数）
  - 备份空间占用（快照数、文件数、磁盘使用率进度条）
  - 最近 5 次备份记录 + 最近 5 条日志
  - `--watch` 实时刷新模式，`--interval` 自定义间隔
- [x] `adoback watch` FSEvents 实时监听
  - 纯 ctypes 调用 macOS FSEvents API，零依赖
  - 文件保存后 ~2 秒自动触发增量备份
  - 内置防抖机制，避免频繁触发
  - 非 macOS 自动回退为定时轮询

---

## 📋 计划中

### ~~v0.8.0~~ v0.5.8 — 分发渠道扩展
- [x] Homebrew Formula（`brew tap SOULRAi/tap && brew install adoback`）
- [x] GitHub Actions 自动构建 + Release（push tag 自动触发）

### v1.0.0 — 稳定版
- [ ] 全面测试覆盖
- [ ] 边界场景处理完善
- [ ] 正式稳定版发布

---

## 🧊 冰箱（有想法但不急）

- [ ] 云端同步（iCloud Drive / 外置硬盘 / NAS）
- [ ] 备份时间线可视化（文件大小变化趋势）
- [ ] Web UI（本地 HTTP 服务，浏览器管理）
- [ ] Windows / Linux 移植
- [ ] 插件系统（自定义备份策略）

---

## 版本路线

| 版本 | 内容 | 状态 |
|------|------|------|
| v0.3.0 | CLI 简化 + 一键安装 + npm 分发 | ✅ 已发布 |
| v0.4.0 | 多源目录 + CLI 美化 + 自动更新 | ✅ 已发布 |
| v0.5.0 | restore 恢复 + clean 清理 | ✅ 已发布 |
| v0.5.2 | 桌面通知 + ignore 规则 | ✅ 已发布 |
| v0.5.4 | status 面板 + watch 监听 | ✅ 已发布 |
| v0.5.6 | CLI 面板化视觉升级 | ✅ 已发布 |
| v0.5.8 | Homebrew + CI/CD | ✅ 已发布 |
| v1.0.0 | 稳定版发布 | 📋 计划中 |
