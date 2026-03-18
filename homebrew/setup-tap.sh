#!/bin/bash
# Adoback — Homebrew tap 仓库初始化脚本
#
# 用途: 在 GitHub 创建 SOULRAi/homebrew-tap 仓库并推送 Formula
#
# 使用方法:
#   chmod +x setup-tap.sh
#   ./setup-tap.sh
#
# 前提: 需要 gh CLI 已登录

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TAP_REPO="SOULRAi/homebrew-tap"
TMP_DIR="${TMPDIR:-/tmp}/homebrew-tap-setup"

echo ""
echo "Adoback · Homebrew tap 初始化"
echo ""

# 检查 gh
if ! command -v gh &>/dev/null; then
    echo "✗ 需要 GitHub CLI (gh)"
    echo "  安装: brew install gh"
    exit 1
fi

# 清理
rm -rf "$TMP_DIR"
mkdir -p "$TMP_DIR"

# 检查仓库是否已存在
if gh repo view "$TAP_REPO" &>/dev/null 2>&1; then
    echo "✓ 仓库已存在: $TAP_REPO"
    cd "$TMP_DIR"
    gh repo clone "$TAP_REPO" homebrew-tap
    cd homebrew-tap
else
    echo "  正在创建仓库: $TAP_REPO"
    cd "$TMP_DIR"
    mkdir homebrew-tap && cd homebrew-tap
    git init
    gh repo create "$TAP_REPO" --public --description "Homebrew formulae for SOULRAi tools" --source .
fi

# 复制 Formula
mkdir -p Formula
cp "$SCRIPT_DIR/adoback.rb" Formula/adoback.rb
echo "✓ 已复制 Formula/adoback.rb"

# 创建 README
cat > README.md << 'EOF'
# SOULRAi Homebrew Tap

## 安装

```bash
brew tap SOULRAi/tap
brew install adoback
```

## 可用 Formula

| 名称 | 说明 |
|------|------|
| adoback | macOS 本地 Adobe 项目文件备份守护工具 |
EOF

# 提交并推送
git add -A
git commit -m "feat: add adoback formula"
git branch -M main
git push -u origin main

echo ""
echo "✓ Homebrew tap 已就绪！"
echo ""
echo "  用户安装方式:"
echo "    brew tap SOULRAi/tap"
echo "    brew install adoback"
echo ""

# 清理
rm -rf "$TMP_DIR"
