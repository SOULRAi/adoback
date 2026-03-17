#!/bin/bash
# Adoback — 一行安装脚本
# 仅支持 macOS
#
# 远程安装 (推荐):
#   curl -fsSL https://raw.githubusercontent.com/SOULRAi/adoback/main/install.sh | bash
#
# 本地安装:
#   ./install.sh
#
# 工作流程:
#   1. 优先从 GitHub Releases 下载预编译二进制 (零依赖)
#   2. 回退: 下载源码 + 检测 Python 3.10+
#   3. 运行 setup 交互引导

set -euo pipefail

# ─── 配置 ───
REPO="SOULRAi/adoback"                      # ← 改成你的 GitHub 仓库
INSTALL_DIR="$HOME/.local/adoback"
BIN_DIR="$INSTALL_DIR/bin"
LINK_DIR="$HOME/.local/bin"
TMP_DIR="${TMPDIR:-/tmp}/adoback-install"

# ─── 颜色 ───
RED='\033[31m'; GREEN='\033[32m'; CYAN='\033[36m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
err()  { echo -e "  ${RED}✗${NC} $1" >&2; }
info() { echo -e "  ${DIM}$1${NC}"; }

# ─── macOS 检查 ───
if [[ "$(uname)" != "Darwin" ]]; then
    err "此工具仅支持 macOS"
    exit 1
fi

echo ""
echo -e "${BOLD}Adoback · 安装${NC}"
echo ""

# ─── 检测架构 ───
ARCH="$(uname -m)"
case "$ARCH" in
    arm64)  ASSET_PATTERN="adoback-macos-arm64" ;;
    x86_64) ASSET_PATTERN="adoback-macos-x86_64" ;;
    *)      ASSET_PATTERN="adoback-macos-universal" ;;
esac

# ─── 清理临时目录 ───
cleanup() { rm -rf "$TMP_DIR" 2>/dev/null || true; }
trap cleanup EXIT
mkdir -p "$TMP_DIR"

# ─── 检测本地文件 (clone 后 ./install.sh 场景) ───
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")"
LOCAL_BIN=""
LOCAL_PY=""

if [ -n "$SCRIPT_DIR" ]; then
    if [ -f "$SCRIPT_DIR/dist/adoback" ]; then
        LOCAL_BIN="$SCRIPT_DIR/dist/adoback"
    elif [ -f "$SCRIPT_DIR/adoback" ] && file "$SCRIPT_DIR/adoback" 2>/dev/null | grep -q "Mach-O"; then
        LOCAL_BIN="$SCRIPT_DIR/adoback"
    fi
    [ -f "$SCRIPT_DIR/adoback.py" ] && LOCAL_PY="$SCRIPT_DIR/adoback.py"
fi

# ─── 安装函数 ───
install_binary() {
    local src="$1"
    mkdir -p "$BIN_DIR" "$LINK_DIR"
    cp "$src" "$BIN_DIR/adoback"
    chmod +x "$BIN_DIR/adoback"
    ln -sf "$BIN_DIR/adoback" "$LINK_DIR/adoback"
    ok "已安装: $LINK_DIR/adoback"
}

install_source() {
    local src="$1"
    local py="$2"
    mkdir -p "$BIN_DIR" "$LINK_DIR"
    cp "$src" "$BIN_DIR/adoback.py"
    chmod +x "$BIN_DIR/adoback.py"
    cat > "$LINK_DIR/adoback" << EOF
#!/bin/bash
exec "$py" "$BIN_DIR/adoback.py" "\$@"
EOF
    chmod +x "$LINK_DIR/adoback"
    ok "已安装: $LINK_DIR/adoback"
}

find_python() {
    for cmd in python3 python; do
        if command -v "$cmd" &>/dev/null; then
            local ver
            ver=$("$cmd" -c "import sys; v=sys.version_info; print(f'{v.major}.{v.minor}')" 2>/dev/null || echo "0.0")
            local major=${ver%%.*}
            local minor=${ver##*.}
            if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
                echo "$cmd"
                return 0
            fi
        fi
    done
    return 1
}

ensure_path() {
    if echo "$PATH" | tr ':' '\n' | grep -qx "$LINK_DIR"; then
        return
    fi
    local shell_rc="$HOME/.zshrc"
    [[ "${SHELL:-/bin/zsh}" == */bash ]] && shell_rc="$HOME/.bash_profile"
    local path_line='export PATH="$HOME/.local/bin:$PATH"'
    if [ -f "$shell_rc" ] && grep -q '.local/bin' "$shell_rc" 2>/dev/null; then
        return
    fi
    echo "" >> "$shell_rc"
    echo "# Adoback" >> "$shell_rc"
    echo "$path_line" >> "$shell_rc"
    ok "已添加 PATH 到 $shell_rc"
    export PATH="$LINK_DIR:$PATH"
}

# ─── 安装策略 ───
INSTALLED=false

# 策略 1: 本地已有二进制
if [ -n "$LOCAL_BIN" ]; then
    ok "检测到本地二进制: $LOCAL_BIN"
    install_binary "$LOCAL_BIN"
    INSTALLED=true
fi

# 策略 2: 从 GitHub Releases 下载二进制
if ! $INSTALLED && command -v curl &>/dev/null; then
    echo "  正在从 GitHub Releases 下载..."
    DOWNLOAD_URL=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
        | grep "browser_download_url" \
        | grep -i "$ASSET_PATTERN" \
        | head -1 \
        | cut -d '"' -f 4 || true)

    if [ -n "$DOWNLOAD_URL" ]; then
        if curl -fsSL "$DOWNLOAD_URL" -o "$TMP_DIR/adoback" 2>/dev/null; then
            chmod +x "$TMP_DIR/adoback"
            if file "$TMP_DIR/adoback" | grep -q "Mach-O"; then
                install_binary "$TMP_DIR/adoback"
                INSTALLED=true
                ok "已下载预编译二进制 ($ARCH)"
            fi
        fi
    fi

    if ! $INSTALLED; then
        info "未找到预编译二进制，尝试源码安装..."
    fi
fi

# 策略 3: 本地已有源码
if ! $INSTALLED && [ -n "$LOCAL_PY" ]; then
    if PYTHON=$(find_python); then
        ok "使用本地源码 + $($PYTHON --version)"
        install_source "$LOCAL_PY" "$PYTHON"
        INSTALLED=true
    fi
fi

# 策略 4: 下载源码
if ! $INSTALLED; then
    PYTHON=$(find_python) || {
        err "安装失败"
        echo ""
        echo "  未找到预编译二进制，也未找到 Python 3.10+"
        echo "  请安装 Python: brew install python"
        echo "  或者从 GitHub Releases 手动下载二进制"
        echo ""
        exit 1
    }

    echo "  正在下载源码..."
    SOURCE_URL="https://raw.githubusercontent.com/$REPO/main/adoback.py"
    if curl -fsSL "$SOURCE_URL" -o "$TMP_DIR/adoback.py" 2>/dev/null; then
        ok "已下载源码"
        install_source "$TMP_DIR/adoback.py" "$PYTHON"
        INSTALLED=true
    else
        err "下载失败，请检查网络或仓库地址"
        exit 1
    fi
fi

# ─── 配置 PATH ───
ensure_path

# ─── 运行 setup ───
echo ""
"$LINK_DIR/adoback" setup
