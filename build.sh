#!/bin/bash
# Adoback — 构建脚本
# 用途: 将 Python 源码打包为 macOS 零依赖单文件可执行二进制
# 产出: dist/adoback (约 10-15 MB)
#
# 使用方法:
#   chmod +x build.sh
#   ./build.sh
#
# 前提: 需要 Python 3.10+ 和 pip（仅开发者构建时需要，用户不需要）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "╔══════════════════════════════════════════╗"
echo "║  Adoback · 构建                          ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# 检查 Python
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "✗ 需要 Python 3.10+，未找到"
    echo "  安装方式: brew install python"
    exit 1
fi
echo "✓ Python: $($PYTHON --version)"

# 安装 PyInstaller（如果没有）
if ! $PYTHON -m PyInstaller --version &>/dev/null; then
    echo "  正在安装 PyInstaller..."
    $PYTHON -m pip install pyinstaller --quiet
fi
echo "✓ PyInstaller: $($PYTHON -m PyInstaller --version 2>&1)"

# 清理旧构建
rm -rf build/ dist/ *.spec

# 打包
echo ""
echo "正在构建..."
$PYTHON -m PyInstaller \
    --onefile \
    --name adoback \
    --strip \
    --noupx \
    --target-architecture universal2 \
    --console \
    --clean \
    --log-level WARN \
    adoback.py

# 检查产出
BINARY="dist/adoback"
if [ -f "$BINARY" ]; then
    SIZE=$(du -h "$BINARY" | cut -f1)
    echo ""
    echo "╔══════════════════════════════════════════╗"
    echo "║  构建完成                                 ║"
    echo "╚══════════════════════════════════════════╝"
    echo ""
    echo "  二进制:  $BINARY"
    echo "  大小:    $SIZE"
    echo "  架构:    $(file "$BINARY" | grep -o 'arm64\|x86_64' | tr '\n' '+' | sed 's/+$//')"
    echo ""
    echo "  测试运行:"
    echo "    $BINARY --version"
    echo ""
    echo "  安装:"
    echo "    ./install.sh"
else
    echo "✗ 构建失败"
    exit 1
fi

# 清理中间产物
rm -rf build/ *.spec
