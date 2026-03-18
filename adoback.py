#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Adoback — macOS 本地 Adobe 项目文件备份守护工具
v0.5.2

仅面向 macOS，中文优先 CLI。
可通过 PyInstaller 打包为零依赖单文件二进制，任何 Mac 直接使用。
"""

import argparse
import copy
import datetime
import fcntl
import hashlib
import json
import os
import platform
import plistlib
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ─────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────

VERSION = "0.5.2"
APP_NAME = "adoback"
SERVICE_LABEL = "com.local.adoback"
GITHUB_REPO = "SOULRAi/adoback"
GITHUB_API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_RAW_SOURCE = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/adoback.py"

HOME = Path.home()
DEFAULT_INSTALL_DIR = HOME / ".local" / "adoback"
DEFAULT_CONFIG_PATH = DEFAULT_INSTALL_DIR / "config.toml"
DEFAULT_STATE_DIR = HOME / ".local" / "state" / "adoback"
DEFAULT_LOG_DIR = HOME / "Library" / "Logs" / "adoback"
DEFAULT_PLIST_PATH = HOME / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"

LOCK_FILENAME = "daemon.lock"
MANIFEST_FILENAME = "manifest.sqlite3"
LOG_FILENAME = "backup.log.jsonl"
REPORT_DIR_NAME = "reports"

ADOBE_EXTENSIONS = {
    ".psd", ".psb", ".ai", ".indd", ".idml",
    ".prproj", ".aep", ".aegraphic", ".xd", ".pdf",
}

DEFAULT_EXCLUDE_PATTERNS = {
    "*.tmp", "*.bak", "*.swp", "~*", "._*", ".DS_Store",
    "*.lck", "*.lock", "*.idlk",
}

DEFAULT_EXCLUDE_DIRS = {
    ".git", ".svn", "node_modules", "__pycache__",
    "Adobe Premiere Pro Auto-Save",
    "Adobe After Effects Auto-Save",
    "Adobe InDesign Recovered",
    "Media Cache Files",
    "Media Cache",
    "Peak Files",
    "Adobe Premiere Pro Preview Files",
}

# 默认忽略模式 (.adobackignore 格式)
DEFAULT_IGNORE_PATTERNS = [
    # Adobe 缓存与临时文件
    "*.tmp",
    "*.bak",
    "*.lck",
    "*.lock",
    "*.idlk",
    "*.swp",
    "~*",
    "._*",
    ".DS_Store",
    # Premiere / After Effects 缓存
    "Media Cache Files/",
    "Media Cache/",
    "Peak Files/",
    "Adobe Premiere Pro Auto-Save/",
    "Adobe After Effects Auto-Save/",
    "Adobe Premiere Pro Preview Files/",
    # InDesign 恢复文件
    "Adobe InDesign Recovered/",
    # 系统/版本管理
    ".git/",
    ".svn/",
    "node_modules/",
    "__pycache__/",
]

IGNOREFILE_NAME = ".adobackignore"

# 退出码
EXIT_OK = 0
EXIT_PARTIAL = 10
EXIT_LOCK = 20
EXIT_CONFIG = 30
EXIT_RUNTIME = 40
EXIT_INTERRUPT = 130

# ─────────────────────────────────────────────
# TOML 解析器 (最小实现，不依赖第三方库)
# ─────────────────────────────────────────────

class TOMLError(Exception):
    pass


def _toml_parse_value(raw: str):
    """解析单个 TOML 值。"""
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return None
    # 去掉行尾注释 (不在引号内的 #)
    in_str = False
    str_char = None
    for i, ch in enumerate(raw):
        if ch in ('"', "'") and not in_str:
            in_str = True
            str_char = ch
        elif ch == str_char and in_str:
            in_str = False
        elif ch == "#" and not in_str:
            raw = raw[:i].strip()
            break

    if raw.lower() == "true":
        return True
    if raw.lower() == "false":
        return False
    # 数组
    if raw.startswith("["):
        return _toml_parse_array(raw)
    # 字符串
    if raw.startswith('"""'):
        return raw[3:].rstrip('"').rstrip('"').rstrip('"')
    if raw.startswith('"'):
        return raw.strip('"')
    if raw.startswith("'"):
        return raw.strip("'")
    # 数字
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def _toml_parse_array(raw: str):
    """解析 TOML 数组。"""
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1].strip()
    if not raw:
        return []
    items = []
    current = ""
    depth = 0
    in_str = False
    for ch in raw:
        if ch == '"' and depth == 0:
            in_str = not in_str
        if ch == "[" and not in_str:
            depth += 1
        if ch == "]" and not in_str:
            depth -= 1
        if ch == "," and depth == 0 and not in_str:
            val = _toml_parse_value(current.strip())
            if val is not None:
                items.append(val)
            current = ""
        else:
            current += ch
    if current.strip():
        val = _toml_parse_value(current.strip())
        if val is not None:
            items.append(val)
    return items


def toml_load(path: Path) -> dict:
    """加载 TOML 文件为嵌套字典。"""
    result = {}
    current_section = result
    section_name = None

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise TOMLError(f"配置文件不存在: {path}")
    except PermissionError:
        raise TOMLError(f"无权读取配置文件: {path}")

    # 处理多行数组
    lines = text.splitlines()
    merged_lines = []
    buffer = ""
    bracket_depth = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") and bracket_depth == 0:
            merged_lines.append(line)
            continue
        buffer += (" " if buffer else "") + line
        bracket_depth += line.count("[") - line.count("]")
        if bracket_depth <= 0:
            bracket_depth = 0
            merged_lines.append(buffer)
            buffer = ""
    if buffer:
        merged_lines.append(buffer)

    for line in merged_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # section header
        if stripped.startswith("[") and "=" not in stripped.split("]")[0]:
            section_name = stripped.strip("[]").strip()
            parts = section_name.split(".")
            current_section = result
            for p in parts:
                if p not in current_section:
                    current_section[p] = {}
                current_section = current_section[p]
            continue
        # key = value
        if "=" in stripped:
            key, _, val_str = stripped.partition("=")
            key = key.strip()
            val = _toml_parse_value(val_str)
            if val is not None:
                current_section[key] = val

    return result


# ─────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────

DEFAULT_CONFIG = {
    "general": {
        "language": "zh",
    },
    "source": {
        "root": "",     # 向后兼容: 单目录
        "roots": [],    # 新版: 多目录列表
    },
    "destination": {
        "root": "",
        "layout": "snapshot",  # snapshot | mirror
    },
    "filters": {
        "include": sorted(list(ADOBE_EXTENSIONS)),
        "exclude": sorted(list(DEFAULT_EXCLUDE_PATTERNS)),
        "exclude_dirs": sorted(list(DEFAULT_EXCLUDE_DIRS)),
    },
    "performance": {
        "workers": 4,
        "copy_chunk_mb": 4,
        "verify": "mtime_size",  # none | mtime_size | sha256
        "fsync": True,
        "retry_count": 2,
        "retry_delay_ms": 500,
    },
    "retention": {
        "keep_last": 10,
        "keep_days": 30,
    },
    "state": {
        "dir": str(DEFAULT_STATE_DIR),
    },
    "logging": {
        "dir": str(DEFAULT_LOG_DIR),
        "level": "info",
    },
    "schedule": {
        "default_interval_seconds": 300,
    },
    "notification": {
        "enabled": True,
        "on_success": True,
        "on_failure": True,
        "on_disk_low": True,
        "disk_low_threshold_gb": 5,
    },
    "service": {
        "label": SERVICE_LABEL,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并两个字典，override 覆盖 base。"""
    result = copy.deepcopy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result


class Config:
    """运行时配置对象。"""

    def __init__(self, data: dict):
        self._data = data

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        if path is not None:
            path = Path(path).expanduser().resolve()
            if not path.exists():
                printerr(f"配置文件不存在: {path}")
                printerr(f"请先运行: {_prog()} setup  或  {_prog()} config init")
                sys.exit(EXIT_CONFIG)
        else:
            path = _find_config()
            if path is None:
                printerr(f"未找到配置文件")
                printerr(f"搜索路径: {', '.join(str(p) for p in _config_search_paths())}")
                printerr(f"请先运行: {_prog()} setup  或  {_prog()} config init")
                sys.exit(EXIT_CONFIG)
        try:
            user_conf = toml_load(path)
        except TOMLError as e:
            printerr(f"配置解析失败: {e}")
            sys.exit(EXIT_CONFIG)
        merged = _deep_merge(DEFAULT_CONFIG, user_conf)
        obj = cls(merged)
        obj._config_path = path
        return obj

    @classmethod
    def from_defaults(cls) -> "Config":
        return cls(copy.deepcopy(DEFAULT_CONFIG))

    def get(self, *keys, default=None):
        d = self._data
        for k in keys:
            if isinstance(d, dict) and k in d:
                d = d[k]
            else:
                return default
        return d

    @property
    def source_roots(self) -> list[Path]:
        """获取所有源目录（兼容旧版 root 和新版 roots）。"""
        roots_raw = self.get("source", "roots", default=[])
        root_raw = self.get("source", "root", default="")
        paths = []
        if roots_raw:
            for r in roots_raw:
                if r:
                    paths.append(Path(r).expanduser().resolve())
        if root_raw and not paths:
            paths.append(Path(root_raw).expanduser().resolve())
        return paths

    @property
    def source_root(self) -> Path:
        """向后兼容: 返回第一个源目录。"""
        roots = self.source_roots
        return roots[0] if roots else Path()

    @property
    def dest_root(self) -> Path:
        r = self.get("destination", "root", default="")
        return Path(r).expanduser().resolve() if r else Path()

    @property
    def layout(self) -> str:
        return self.get("destination", "layout", default="snapshot")

    @property
    def state_dir(self) -> Path:
        return Path(self.get("state", "dir", default=str(DEFAULT_STATE_DIR))).expanduser().resolve()

    @property
    def log_dir(self) -> Path:
        return Path(self.get("logging", "dir", default=str(DEFAULT_LOG_DIR))).expanduser().resolve()

    @property
    def report_dir(self) -> Path:
        return self.state_dir / REPORT_DIR_NAME

    @property
    def lock_path(self) -> Path:
        return self.state_dir / LOCK_FILENAME

    @property
    def manifest_path(self) -> Path:
        return self.state_dir / MANIFEST_FILENAME

    @property
    def log_path(self) -> Path:
        return self.log_dir / LOG_FILENAME

    @property
    def workers(self) -> int:
        return int(self.get("performance", "workers", default=4))

    @property
    def chunk_bytes(self) -> int:
        return int(self.get("performance", "copy_chunk_mb", default=4)) * 1024 * 1024

    @property
    def verify_mode(self) -> str:
        return self.get("performance", "verify", default="mtime_size")

    @property
    def use_fsync(self) -> bool:
        return bool(self.get("performance", "fsync", default=True))

    @property
    def retry_count(self) -> int:
        return int(self.get("performance", "retry_count", default=2))

    @property
    def retry_delay(self) -> float:
        return int(self.get("performance", "retry_delay_ms", default=500)) / 1000.0

    @property
    def keep_last(self) -> int:
        return int(self.get("retention", "keep_last", default=10))

    @property
    def keep_days(self) -> int:
        return int(self.get("retention", "keep_days", default=30))

    @property
    def interval(self) -> int:
        return int(self.get("schedule", "default_interval_seconds", default=300))

    @property
    def include_exts(self) -> set:
        raw = self.get("filters", "include", default=[])
        return {e if e.startswith(".") else f".{e}" for e in raw}

    @property
    def exclude_patterns(self) -> set:
        return set(self.get("filters", "exclude", default=[]))

    @property
    def exclude_dirs(self) -> set:
        return set(self.get("filters", "exclude_dirs", default=[]))

    @property
    def notify_enabled(self) -> bool:
        return bool(self.get("notification", "enabled", default=True))

    @property
    def notify_on_success(self) -> bool:
        return bool(self.get("notification", "on_success", default=True))

    @property
    def notify_on_failure(self) -> bool:
        return bool(self.get("notification", "on_failure", default=True))

    @property
    def notify_on_disk_low(self) -> bool:
        return bool(self.get("notification", "on_disk_low", default=True))

    @property
    def disk_low_threshold_gb(self) -> float:
        return float(self.get("notification", "disk_low_threshold_gb", default=5))

    @property
    def service_label(self) -> str:
        return self.get("service", "label", default=SERVICE_LABEL)

    @property
    def data(self) -> dict:
        return self._data

    def validate(self) -> list[str]:
        """验证配置，返回问题列表。"""
        issues = []
        roots = self.source_roots
        dst = self.get("destination", "root", default="")
        if not roots:
            issues.append("source.roots 未设置 (至少需要一个源目录)")
        else:
            for r in roots:
                if not r.exists():
                    issues.append(f"源目录不存在: {r}")
        if not dst:
            issues.append("destination.root 未设置")
        else:
            dp = Path(dst).expanduser().resolve()
            for r in roots:
                if dp == r:
                    issues.append(f"destination.root 不能与源目录相同: {r}")
                if str(dp).startswith(str(r) + "/"):
                    issues.append(f"destination.root 不能位于源目录内部: {r}")
        layout = self.layout
        if layout not in ("snapshot", "mirror"):
            issues.append(f"destination.layout 无效: {layout} (应为 snapshot 或 mirror)")
        verify = self.verify_mode
        if verify not in ("none", "mtime_size", "sha256"):
            issues.append(f"performance.verify 无效: {verify}")
        if self.workers < 1:
            issues.append("performance.workers 必须 >= 1")
        if self.retry_count < 0:
            issues.append("performance.retry_count 不能为负数")
        return issues


def _config_search_paths() -> list[Path]:
    """配置文件搜索路径，按优先级排列"""
    return [
        Path.cwd() / "config.toml",
        DEFAULT_CONFIG_PATH,
        HOME / ".config" / "adoback" / "config.toml",
    ]


def _find_config() -> Path | None:
    """按优先级搜索配置文件"""
    for p in _config_search_paths():
        if p.exists():
            return p
    return None


def _is_frozen() -> bool:
    """是否为 PyInstaller 打包后的冻结二进制"""
    return getattr(sys, 'frozen', False)


def _get_bin_path() -> str:
    """获取当前可执行文件的绝对路径"""
    if _is_frozen():
        return sys.executable
    return str(Path(__file__).resolve())


def _parse_version(v: str) -> tuple:
    """将版本字符串转为可比较的元组，如 'v0.4.0' → (0, 4, 0)"""
    return tuple(int(x) for x in v.strip().lstrip("v").split("."))


def _detect_asset_name() -> str:
    """根据当前架构返回 GitHub Release 资源名。"""
    machine = platform.machine().lower()
    if machine == "arm64":
        return "adoback-macos-arm64"
    elif machine == "x86_64":
        return "adoback-macos-x86_64"
    return "adoback-macos-universal"


def _github_get_json(url: str) -> dict:
    """从 GitHub API 获取 JSON 数据。"""
    req = urllib.request.Request(url, headers={"User-Agent": "adoback-updater"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _github_download(url: str, dest_path: Path):
    """下载文件到指定路径。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": "adoback-updater",
        "Accept": "application/octet-stream",
    })
    with urllib.request.urlopen(req, timeout=120) as resp:
        with open(dest_path, "wb") as f:
            shutil.copyfileobj(resp, f)


# ─────────────────────────────────────────────
# 输出工具（带终端颜色）
# ─────────────────────────────────────────────

_BOLD = "\033[1m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_CYAN = "\033[36m"
_MAGENTA = "\033[35m"
_BLUE = "\033[34m"
_DIM = "\033[2m"
_RESET = "\033[0m"

# 图标
_ICON_OK = "✔"
_ICON_ERR = "✘"
_ICON_WARN = "⚠"
_ICON_ARROW = "›"
_ICON_DOT = "·"
_ICON_SPARK = "✦"


def _use_color() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"{code}{text}{_RESET}" if _use_color() else text


def _visible_len(s: str) -> int:
    """计算字符串可见宽度（去掉 ANSI 转义、中文算 2 宽度）。"""
    import re
    clean = re.sub(r'\033\[[0-9;]*m', '', s)
    w = 0
    for ch in clean:
        if '\u4e00' <= ch <= '\u9fff' or '\u3000' <= ch <= '\u303f' or '\uff00' <= ch <= '\uffef':
            w += 2
        else:
            w += 1
    return w


def printout(msg: str = ""):
    print(msg)


def printerr(msg: str):
    print(f"  {_c(_RED, _ICON_ERR)} {msg}", file=sys.stderr)


def printwarn(msg: str):
    print(f"  {_c(_YELLOW, _ICON_WARN)} {msg}", file=sys.stderr)


def printinfo(msg: str):
    print(f"  {_c(_GREEN, _ICON_OK)} {msg}")


def printdim(msg: str):
    print(f"  {_c(_DIM, msg)}")


def printstep(step: int | str, msg: str):
    label = f"[{step}]" if isinstance(step, int) else step
    print(f"\n  {_c(_CYAN, label)} {_c(_BOLD, msg)}")


def printtitle(msg: str):
    print(f"\n{_c(_BOLD, msg)}")


def printbox(title: str, width: int = 48, style: str = "double"):
    """打印对齐的 Unicode 边框标题。"""
    if style == "double":
        tl, tr, bl, br, h, v = "╔", "╗", "╚", "╝", "═", "║"
    else:
        tl, tr, bl, br, h, v = "┌", "┐", "└", "┘", "─", "│"
    inner = width - 2
    top = f"  {_c(_CYAN, tl + h * inner + tr)}"
    # 居中标题
    t = f" {_ICON_SPARK} {title} {_ICON_SPARK} " if _use_color() else f"  {title}  "
    vis = _visible_len(t)
    pad_total = inner - vis
    pad_l = pad_total // 2
    pad_r = pad_total - pad_l
    mid = f"  {_c(_CYAN, v)}{' ' * pad_l}{_c(_BOLD, t)}{' ' * pad_r}{_c(_CYAN, v)}"
    bot = f"  {_c(_CYAN, bl + h * inner + br)}"
    printout(top)
    printout(mid)
    printout(bot)


def printbar(char: str = "─", width: int = 44):
    printout(f"  {_c(_DIM, char * width)}")


# ─────────────────────────────────────────────
# macOS 桌面通知
# ─────────────────────────────────────────────

def _notify(title: str, message: str, sound: str = "default"):
    """发送 macOS 原生桌面通知 (通过 osascript)。"""
    if platform.system() != "Darwin":
        return
    # 转义双引号
    safe_title = title.replace('"', '\\"')
    safe_msg = message.replace('"', '\\"')
    script = f'display notification "{safe_msg}" with title "{safe_title}" sound name "{sound}"'
    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass  # 通知是尽力而为，不应影响主流程


def notify_backup_success(cfg, result):
    """备份成功通知。"""
    if not cfg.notify_enabled or not cfg.notify_on_success:
        return
    mb = result.bytes_copied / (1024 * 1024)
    _notify(
        "Adoback 备份完成 ✔",
        f"已备份 {result.copied} 个文件 ({mb:.1f} MB)",
    )


def notify_backup_failure(cfg, result):
    """备份失败通知。"""
    if not cfg.notify_enabled or not cfg.notify_on_failure:
        return
    _notify(
        "Adoback 备份异常 ⚠",
        f"{result.failed} 个文件失败，{result.copied} 个成功",
        sound="Basso",
    )


def notify_disk_low(cfg, dest_path: Path):
    """磁盘空间不足通知。"""
    if not cfg.notify_enabled or not cfg.notify_on_disk_low:
        return
    try:
        st = os.statvfs(dest_path)
        free_gb = (st.f_bavail * st.f_frsize) / (1024 ** 3)
        if free_gb < cfg.disk_low_threshold_gb:
            _notify(
                "Adoback 磁盘空间不足 ⚠",
                f"备份盘剩余 {free_gb:.1f} GB，低于阈值 {cfg.disk_low_threshold_gb} GB",
                sound="Basso",
            )
            return True
    except OSError:
        pass
    return False


# ─────────────────────────────────────────────
# 忽略规则 (.adobackignore)
# ─────────────────────────────────────────────

def _load_ignore_patterns(roots: list[Path]) -> list[str]:
    """加载忽略模式：默认模式 + 各源目录下的 .adobackignore。"""
    patterns = list(DEFAULT_IGNORE_PATTERNS)

    # 在每个源目录下查找 .adobackignore
    for root in roots:
        ignore_file = root / IGNOREFILE_NAME
        if ignore_file.is_file():
            try:
                for line in ignore_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        if line not in patterns:
                            patterns.append(line)
            except OSError:
                pass

    # 也检查全局配置目录
    global_ignore = DEFAULT_INSTALL_DIR / IGNOREFILE_NAME
    if global_ignore.is_file():
        try:
            for line in global_ignore.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    if line not in patterns:
                        patterns.append(line)
        except OSError:
            pass

    return patterns


def _match_ignore(name: str, rel_path: str, ignore_patterns: list[str]) -> bool:
    """判断文件是否应被忽略。

    支持的模式:
    - *.ext        按扩展名匹配文件名
    - ~*           前缀匹配文件名
    - dirname/     目录名匹配 (匹配路径中包含该目录)
    - filename     精确匹配文件名
    """
    for pat in ignore_patterns:
        if not pat:
            continue
        # 目录模式：pattern 以 / 结尾
        if pat.endswith("/"):
            dir_name = pat.rstrip("/")
            if f"/{dir_name}/" in f"/{rel_path}" or rel_path.startswith(dir_name + "/"):
                return True
            continue
        # 通配符模式
        if pat.startswith("*"):
            if name.endswith(pat[1:]):
                return True
        elif pat.endswith("*"):
            if name.startswith(pat[:-1]):
                return True
        elif pat.startswith("."):
            # 隐藏文件/扩展名匹配
            if name == pat or name.endswith(pat):
                return True
        else:
            if name == pat:
                return True
    return False


# ─────────────────────────────────────────────
# JSONL 日志
# ─────────────────────────────────────────────

class JSONLLogger:
    def __init__(self, path: Path):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def log(self, level: str, event: str, **extra):
        entry = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "level": level,
            "event": event,
            **extra,
        }
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def info(self, event: str, **kw):
        self.log("info", event, **kw)

    def warn(self, event: str, **kw):
        self.log("warn", event, **kw)

    def error(self, event: str, **kw):
        self.log("error", event, **kw)


class NullLogger:
    def log(self, *a, **kw): pass
    def info(self, *a, **kw): pass
    def warn(self, *a, **kw): pass
    def error(self, *a, **kw): pass


# ─────────────────────────────────────────────
# 单实例锁 (fcntl)
# ─────────────────────────────────────────────

class LockError(Exception):
    pass


class InstanceLock:
    """基于 fcntl.flock 的单实例锁。"""

    def __init__(self, path: Path):
        self._path = path
        self._fd = None

    def acquire(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = open(self._path, "w")
            fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fd.write(str(os.getpid()))
            self._fd.flush()
        except (IOError, OSError):
            if self._fd:
                self._fd.close()
                self._fd = None
            # 检查是否为 stale lock
            if self._check_stale():
                # 重试
                try:
                    self._fd = open(self._path, "w")
                    fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._fd.write(str(os.getpid()))
                    self._fd.flush()
                    printinfo("已自动回收过期锁文件")
                    return
                except (IOError, OSError):
                    if self._fd:
                        self._fd.close()
                        self._fd = None
            raise LockError("另一个实例正在运行，无法获取锁")

    def release(self):
        if self._fd:
            try:
                fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
                self._fd.close()
            except Exception:
                pass
            self._fd = None
        try:
            self._path.unlink(missing_ok=True)
        except Exception:
            pass

    def _check_stale(self) -> bool:
        """检查锁文件是否为残留锁。"""
        try:
            pid_str = self._path.read_text().strip()
            if not pid_str:
                return True
            pid = int(pid_str)
            os.kill(pid, 0)  # 检测进程是否存在
            return False
        except (ValueError, ProcessLookupError, FileNotFoundError):
            return True
        except PermissionError:
            return False  # 进程存在但无权发信号

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


# ─────────────────────────────────────────────
# SQLite 存储
# ─────────────────────────────────────────────

class Database:
    def __init__(self, path: Path):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._lock = threading.Lock()
        self._init_tables()

    def _init_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS manifest (
                rel_path    TEXT PRIMARY KEY,
                size        INTEGER,
                mtime_ns    INTEGER,
                sha256      TEXT,
                last_backup TEXT
            );
            CREATE TABLE IF NOT EXISTS runs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at  TEXT,
                finished_at TEXT,
                mode        TEXT,
                status      TEXT,
                total_files INTEGER DEFAULT 0,
                copied      INTEGER DEFAULT 0,
                skipped     INTEGER DEFAULT 0,
                failed      INTEGER DEFAULT 0,
                bytes_copied INTEGER DEFAULT 0,
                duration_ms INTEGER DEFAULT 0,
                error_msg   TEXT
            );
        """)
        self._conn.commit()

    def get_manifest(self, rel_path: str) -> dict | None:
        cur = self._conn.execute(
            "SELECT size, mtime_ns, sha256 FROM manifest WHERE rel_path = ?",
            (rel_path,),
        )
        row = cur.fetchone()
        if row:
            return {"size": row[0], "mtime_ns": row[1], "sha256": row[2]}
        return None

    def update_manifest(self, rel_path: str, size: int, mtime_ns: int, sha256: str = ""):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO manifest (rel_path, size, mtime_ns, sha256, last_backup)
                   VALUES (?, ?, ?, ?, ?)""",
                (rel_path, size, mtime_ns, sha256, now),
            )
            self._conn.commit()

    def start_run(self, mode: str) -> int:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        cur = self._conn.execute(
            "INSERT INTO runs (started_at, mode, status) VALUES (?, ?, 'running')",
            (now, mode),
        )
        self._conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, status: str, total: int, copied: int,
                   skipped: int, failed: int, bytes_copied: int, duration_ms: int,
                   error_msg: str = ""):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self._conn.execute(
            """UPDATE runs SET finished_at=?, status=?, total_files=?, copied=?,
               skipped=?, failed=?, bytes_copied=?, duration_ms=?, error_msg=?
               WHERE id=?""",
            (now, status, total, copied, skipped, failed, bytes_copied, duration_ms, error_msg, run_id),
        )
        self._conn.commit()

    def last_run(self) -> dict | None:
        cur = self._conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def recent_runs(self, n: int = 10) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (n,)
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def close(self):
        self._conn.close()


# ─────────────────────────────────────────────
# 文件扫描
# ─────────────────────────────────────────────

class FileItem:
    __slots__ = ("abs_path", "rel_path", "size", "mtime_ns")

    def __init__(self, abs_path: Path, rel_path: str, size: int, mtime_ns: int):
        self.abs_path = abs_path
        self.rel_path = rel_path
        self.size = size
        self.mtime_ns = mtime_ns


def _match_glob(name: str, pattern: str) -> bool:
    """简单通配符匹配。"""
    if pattern.startswith("*"):
        return name.endswith(pattern[1:])
    if pattern.endswith("*"):
        return name.startswith(pattern[:-1])
    return name == pattern


def scan_files(cfg: Config) -> list[FileItem]:
    """扫描所有源目录，返回候选文件列表。"""
    roots = cfg.source_roots
    if not roots:
        printerr("未配置源目录")
        return []

    include = cfg.include_exts
    exclude = cfg.exclude_patterns
    exclude_dirs = cfg.exclude_dirs
    ignore_patterns = _load_ignore_patterns(roots)
    items = []

    for root in roots:
        if not root.is_dir():
            printerr(f"源目录不存在，已跳过: {root}")
            continue

        for dirpath, dirnames, filenames in os.walk(root):
            # 过滤目录：排除配置中的 exclude_dirs + ignore 中的目录模式
            dirnames[:] = [
                d for d in dirnames
                if d not in exclude_dirs
                and not _match_ignore(d, d + "/", ignore_patterns)
            ]

            for fname in filenames:
                _, ext = os.path.splitext(fname)
                if ext.lower() not in include:
                    continue
                if any(_match_glob(fname, pat) for pat in exclude):
                    continue

                full = Path(dirpath) / fname
                # 多根目录时用 "根目录名/相对路径" 作为 rel
                rel = str(full.relative_to(root))
                if len(roots) > 1:
                    rel = f"{root.name}/{rel}"

                # 检查 ignore 规则
                if _match_ignore(fname, rel, ignore_patterns):
                    continue

                try:
                    st = full.stat()
                except (OSError, PermissionError):
                    continue
                items.append(FileItem(full, rel, st.st_size, st.st_mtime_ns))

    return items


# ─────────────────────────────────────────────
# 文件复制与校验
# ─────────────────────────────────────────────

def _sha256_file(path: Path, chunk: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            data = f.read(chunk)
            if not data:
                break
            h.update(data)
    return h.hexdigest()


def copy_file(src: Path, dst: Path, chunk_bytes: int, use_fsync: bool) -> int:
    """复制单个文件，返回复制字节数。使用临时文件 + 原子替换。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    fd = None
    tmp_path = None
    try:
        # 使用同目录临时文件以确保 rename 原子性
        fd_int, tmp_path = tempfile.mkstemp(
            dir=str(dst.parent),
            prefix=f".{dst.name}.",
            suffix=".tmp",
        )
        fd = os.fdopen(fd_int, "wb")
        with open(src, "rb") as sf:
            while True:
                data = sf.read(chunk_bytes)
                if not data:
                    break
                fd.write(data)
                total += len(data)
        if use_fsync:
            fd.flush()
            os.fsync(fd.fileno())
        fd.close()
        fd = None
        # 保留源文件的修改时间
        src_stat = src.stat()
        os.utime(tmp_path, ns=(src_stat.st_atime_ns, src_stat.st_mtime_ns))
        # 原子替换
        os.replace(tmp_path, str(dst))
        tmp_path = None
        return total
    finally:
        if fd:
            try:
                fd.close()
            except Exception:
                pass
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def verify_copy(src: Path, dst: Path, mode: str) -> bool:
    """校验复制结果。"""
    if mode == "none":
        return True
    try:
        ss = src.stat()
        ds = dst.stat()
    except OSError:
        return False
    if mode == "mtime_size":
        return ss.st_size == ds.st_size
    if mode == "sha256":
        return _sha256_file(src) == _sha256_file(dst)
    return True


# ─────────────────────────────────────────────
# 备份引擎
# ─────────────────────────────────────────────

class BackupResult:
    def __init__(self):
        self.total = 0
        self.copied = 0
        self.skipped = 0
        self.failed = 0
        self.bytes_copied = 0
        self.failures: list[tuple[str, str]] = []  # (rel_path, error)
        self.start_time = time.monotonic()
        self.end_time = 0.0

    @property
    def duration_ms(self) -> int:
        return int((self.end_time - self.start_time) * 1000)

    @property
    def status(self) -> str:
        if self.failed == 0:
            return "success"
        if self.copied > 0:
            return "partial"
        return "failed"


def _needs_backup(item: FileItem, db: Database) -> bool:
    """增量模式：判断文件是否需要备份。"""
    record = db.get_manifest(item.rel_path)
    if record is None:
        return True
    return record["size"] != item.size or record["mtime_ns"] != item.mtime_ns


def run_backup(cfg: Config, mode: str, dry_run: bool = False,
               logger=None) -> BackupResult:
    """执行一次备份任务。"""
    if logger is None:
        logger = NullLogger()

    result = BackupResult()
    db = Database(cfg.manifest_path)

    try:
        # 扫描
        all_items = scan_files(cfg)
        if not all_items:
            printinfo("未找到符合条件的文件")
            result.end_time = time.monotonic()
            return result

        # 增量过滤
        if mode == "incremental":
            items = [it for it in all_items if _needs_backup(it, db)]
        else:
            items = all_items

        result.total = len(all_items)
        result.skipped = len(all_items) - len(items)

        if not items:
            printinfo("所有文件均为最新，无需备份")
            result.end_time = time.monotonic()
            return result

        if dry_run:
            printinfo(f"[预演模式] 将备份 {len(items)} 个文件:")
            for it in items:
                size_mb = it.size / (1024 * 1024)
                printout(f"  {it.rel_path}  ({size_mb:.1f} MB)")
            result.end_time = time.monotonic()
            return result

        # 确定目标基础路径
        if cfg.layout == "snapshot":
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            base_dst = cfg.dest_root / ts
        else:
            base_dst = cfg.dest_root
        base_dst.mkdir(parents=True, exist_ok=True)

        run_id = db.start_run(mode)
        logger.info("backup_start", mode=mode, files=len(items))
        printinfo(f"开始{('全量' if mode == 'full' else '增量')}备份: {len(items)} 个文件")

        # 并发复制
        lock = threading.Lock()

        def _do_copy(item: FileItem) -> tuple[str, int, str]:
            """返回 (rel_path, bytes_copied, error)。"""
            dst = base_dst / item.rel_path
            last_err = ""
            for attempt in range(1 + cfg.retry_count):
                try:
                    nbytes = copy_file(item.abs_path, dst, cfg.chunk_bytes, cfg.use_fsync)
                    if not verify_copy(item.abs_path, dst, cfg.verify_mode):
                        raise IOError("校验失败")
                    # 更新 manifest
                    sha = ""
                    if cfg.verify_mode == "sha256":
                        sha = _sha256_file(dst)
                    db.update_manifest(item.rel_path, item.size, item.mtime_ns, sha)
                    return (item.rel_path, nbytes, "")
                except Exception as e:
                    last_err = str(e)
                    if attempt < cfg.retry_count:
                        time.sleep(cfg.retry_delay)
            return (item.rel_path, 0, last_err)

        with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
            futures = {pool.submit(_do_copy, it): it for it in items}
            for fut in as_completed(futures):
                rel, nbytes, err = fut.result()
                with lock:
                    if err:
                        result.failed += 1
                        result.failures.append((rel, err))
                        logger.error("copy_failed", file=rel, error=err)
                    else:
                        result.copied += 1
                        result.bytes_copied += nbytes

        result.end_time = time.monotonic()

        # 记录运行
        db.finish_run(
            run_id, result.status, result.total, result.copied,
            result.skipped, result.failed, result.bytes_copied,
            result.duration_ms,
        )
        logger.info("backup_done", status=result.status,
                     copied=result.copied, failed=result.failed,
                     bytes=result.bytes_copied, duration_ms=result.duration_ms)

        # 保留策略 (仅 snapshot 模式)
        if cfg.layout == "snapshot":
            _apply_retention(cfg)

    finally:
        db.close()

    return result


def _apply_retention(cfg: Config):
    """应用快照保留策略。"""
    dest = cfg.dest_root
    if not dest.is_dir():
        return
    snapshots = sorted(
        [d for d in dest.iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True,
    )
    cutoff = datetime.datetime.now() - datetime.timedelta(days=cfg.keep_days)
    to_remove = []
    for i, snap in enumerate(snapshots):
        if i < cfg.keep_last:
            continue
        # 尝试从目录名解析时间
        try:
            snap_time = datetime.datetime.strptime(snap.name, "%Y%m%d_%H%M%S")
            if snap_time < cutoff:
                to_remove.append(snap)
        except ValueError:
            continue

    for snap in to_remove:
        try:
            shutil.rmtree(snap)
            printinfo(f"已清理过期快照: {snap.name}")
        except Exception as e:
            printwarn(f"清理快照失败: {snap.name} - {e}")


# ─────────────────────────────────────────────
# 摘要与报告
# ─────────────────────────────────────────────

def print_summary(result: BackupResult):
    """输出运行摘要。"""
    dur = result.duration_ms / 1000
    mb = result.bytes_copied / (1024 * 1024)
    throughput = mb / dur if dur > 0 else 0

    printout("")
    printbar("─")
    status_str = _status_zh(result.status)
    if result.status == "success":
        icon = _c(_GREEN, _ICON_OK)
        status_str = _c(_GREEN, status_str)
    elif result.status == "partial":
        icon = _c(_YELLOW, _ICON_WARN)
        status_str = _c(_YELLOW, status_str)
    elif result.status == "failed":
        icon = _c(_RED, _ICON_ERR)
        status_str = _c(_RED, status_str)
    else:
        icon = _c(_DIM, _ICON_DOT)

    printout(f"  {icon} {_c(_BOLD, '运行摘要')}  {status_str}")
    printbar("─")
    printout("")
    printout(f"    总文件   {_c(_BOLD, str(result.total)):>8s}")
    printout(f"    已复制   {_c(_GREEN, str(result.copied)):>8s}")
    printout(f"    已跳过   {_c(_DIM, str(result.skipped)):>8s}")
    if result.failed > 0:
        printout(f"    失败     {_c(_RED, str(result.failed)):>8s}")
    else:
        printout(f"    失败     {_c(_DIM, '0'):>8s}")
    printout(f"    数据量   {mb:>7.1f} MB")
    printout(f"    耗时     {dur:>7.1f} 秒")
    if dur > 0:
        printout(f"    吞吐     {throughput:>7.1f} MB/s")
    printout("")

    if result.failures:
        printbar("─")
        printout(f"  {_c(_RED, _ICON_ERR)} {_c(_BOLD, '失败文件')}")
        printbar("─")
        for rel, err in result.failures:
            printerr(f"{rel}: {err}")
        printout("")


def _status_zh(status: str) -> str:
    return {"success": "成功", "partial": "部分失败", "failed": "全部失败",
            "running": "运行中"}.get(status, status)


def export_report(cfg: Config, result: BackupResult, fmt: str = "text") -> Path | None:
    """导出报告。"""
    if result.failed == 0 and fmt == "text":
        return None
    cfg.report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    if fmt == "json":
        report_path = cfg.report_dir / f"report_{ts}.json"
        data = {
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "status": result.status,
            "total_files": result.total,
            "copied": result.copied,
            "skipped": result.skipped,
            "failed": result.failed,
            "bytes_copied": result.bytes_copied,
            "duration_ms": result.duration_ms,
            "failures": [{"file": r, "error": e} for r, e in result.failures],
        }
        report_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        report_path = cfg.report_dir / f"report_{ts}.txt"
        lines = [
            f"Adoback 运行报告",
            f"生成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"状态: {_status_zh(result.status)}",
            f"总文件: {result.total}",
            f"已复制: {result.copied}",
            f"已跳过: {result.skipped}",
            f"失败: {result.failed}",
            f"已复制: {result.bytes_copied / (1024*1024):.1f} MB",
            f"耗时: {result.duration_ms / 1000:.1f} 秒",
            "",
        ]
        if result.failures:
            lines.append("失败文件:")
            for rel, err in result.failures:
                lines.append(f"  {rel}: {err}")
        report_path.write_text("\n".join(lines), encoding="utf-8")

    printinfo(f"报告已导出: {report_path}")
    return report_path


# ─────────────────────────────────────────────
# launchd 服务管理
# ─────────────────────────────────────────────

def _get_plist_path(cfg: Config) -> Path:
    return HOME / "Library" / "LaunchAgents" / f"{cfg.service_label}.plist"


def _generate_plist(cfg: Config) -> dict:
    """生成 launchd plist 字典。"""
    bin_path = _get_bin_path()
    config_path = str(getattr(cfg, '_config_path', DEFAULT_CONFIG_PATH))

    if _is_frozen():
        # PyInstaller 打包后的二进制，直接运行
        program_args = [bin_path, "--config", config_path, "daemon"]
    else:
        # Python 源文件，需要 python3 解释器
        program_args = [sys.executable, bin_path, "--config", config_path, "daemon"]

    return {
        "Label": cfg.service_label,
        "ProgramArguments": program_args,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": str(cfg.log_dir / "service.stdout.log"),
        "StandardErrorPath": str(cfg.log_dir / "service.stderr.log"),
        "WorkingDirectory": str(HOME),
        "ProcessType": "Background",
        "ThrottleInterval": 30,
    }


def service_install(cfg: Config):
    plist_path = _get_plist_path(cfg)
    if plist_path.exists():
        printwarn(f"服务配置已存在: {plist_path}")
        printinfo("如需重新安装，请先执行 service-uninstall")
        return

    plist_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    plist_data = _generate_plist(cfg)
    with open(plist_path, "wb") as f:
        plistlib.dump(plist_data, f)
    printinfo(f"服务已安装: {plist_path}")
    printinfo("运行 'service on' 或 'service-start' 启动服务")


def service_uninstall(cfg: Config):
    plist_path = _get_plist_path(cfg)
    # 先尝试停止
    _launchctl("bootout", cfg.service_label, silent=True)
    if plist_path.exists():
        plist_path.unlink()
        printinfo("服务已卸载")
    else:
        printinfo("服务配置不存在，无需卸载")


def service_start(cfg: Config):
    plist_path = _get_plist_path(cfg)
    if not plist_path.exists():
        printerr("服务未安装，请先运行 service on")
        sys.exit(EXIT_CONFIG)
    _launchctl("bootstrap", cfg.service_label, plist_path=plist_path)
    printinfo("服务已启动")


def service_stop(cfg: Config):
    _launchctl("bootout", cfg.service_label)
    printinfo("服务已停止")


def service_restart(cfg: Config):
    service_stop(cfg)
    time.sleep(1)
    service_start(cfg)


def service_status(cfg: Config):
    plist_path = _get_plist_path(cfg)
    if not plist_path.exists():
        printout("服务状态: 未安装")
        return

    result = subprocess.run(
        ["launchctl", "list"],
        capture_output=True, text=True,
    )
    for line in result.stdout.splitlines():
        if cfg.service_label in line:
            parts = line.split()
            pid = parts[0] if parts[0] != "-" else "未运行"
            status_code = parts[1]
            printout(f"服务状态: 已安装")
            printout(f"  PID:      {pid}")
            printout(f"  退出码:   {status_code}")
            printout(f"  配置文件: {plist_path}")
            return

    printout("服务状态: 已安装但未加载")
    printout(f"  配置文件: {plist_path}")


def _launchctl(action: str, label: str, plist_path: Path = None, silent: bool = False):
    uid = os.getuid()
    domain = f"gui/{uid}"
    if action == "bootstrap":
        cmd = ["launchctl", "bootstrap", domain, str(plist_path)]
    elif action == "bootout":
        cmd = ["launchctl", "bootout", f"{domain}/{label}"]
    else:
        cmd = ["launchctl", action, label]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 and not silent:
        # bootstrap 可能因为已加载而失败，尝试旧版 load
        if action == "bootstrap":
            result2 = subprocess.run(
                ["launchctl", "load", str(plist_path)],
                capture_output=True, text=True,
            )
            if result2.returncode != 0:
                printwarn(f"launchctl 操作失败: {result2.stderr.strip()}")
        elif action == "bootout":
            result2 = subprocess.run(
                ["launchctl", "unload", str(plist_path or _get_plist_path(Config.from_defaults()))],
                capture_output=True, text=True,
            )
            if result2.returncode != 0 and not silent:
                printwarn(f"launchctl 操作失败: {result2.stderr.strip()}")


# ─────────────────────────────────────────────
# Daemon 守护模式
# ─────────────────────────────────────────────

_daemon_running = True

def _daemon_signal_handler(sig, frame):
    global _daemon_running
    _daemon_running = False
    printinfo("收到中断信号，正在退出...")


def run_daemon(cfg: Config):
    """守护模式：循环执行增量备份。"""
    global _daemon_running
    signal.signal(signal.SIGTERM, _daemon_signal_handler)
    signal.signal(signal.SIGINT, _daemon_signal_handler)

    logger = JSONLLogger(cfg.log_path)
    interval = cfg.interval

    printinfo(f"守护模式已启动 (间隔 {interval} 秒)")
    logger.info("daemon_start", interval=interval)

    while _daemon_running:
        try:
            # 每轮检查磁盘空间
            if cfg.dest_root.is_dir():
                notify_disk_low(cfg, cfg.dest_root)

            result = run_backup(cfg, "incremental", logger=logger)
            if result.copied > 0 or result.failed > 0:
                print_summary(result)
                # 守护模式通知
                if result.failed > 0:
                    notify_backup_failure(cfg, result)
                elif result.copied > 0:
                    notify_backup_success(cfg, result)
                if result.failures:
                    export_report(cfg, result)
        except Exception as e:
            logger.error("daemon_error", error=str(e))
            printerr(f"备份执行异常: {e}")
            # 守护模式异常通知
            if cfg.notify_enabled:
                _notify("Adoback 运行异常 ⚠", str(e), sound="Basso")

        # 等待下一轮，支持提前中断
        for _ in range(interval):
            if not _daemon_running:
                break
            time.sleep(1)

    logger.info("daemon_stop")
    printinfo("守护进程已退出")


# ─────────────────────────────────────────────
# Doctor 自检
# ─────────────────────────────────────────────

def run_doctor(cfg: Config):
    """自检：检查系统环境和配置。"""
    printout("")
    printbox("Adoback 自检")
    ok = True

    # 1. 系统
    printstep("◆", "系统环境")
    printout(f"    macOS:    {platform.mac_ver()[0] or '未知'}")
    if _is_frozen():
        printout(f"    运行模式: 独立二进制")
    else:
        printout(f"    Python:   {platform.python_version()}")
    printout(f"    用户:     {os.environ.get('USER', '未知')}")
    printout(f"    版本:     {VERSION}")

    if platform.system() != "Darwin":
        printwarn("当前系统非 macOS，部分功能可能不可用")
        ok = False
    else:
        printinfo("macOS 环境正常")

    # 2. 配置
    printstep("◆", "配置检查")
    config_path = getattr(cfg, '_config_path', None)
    if config_path:
        printinfo(f"配置文件: {config_path}")
    else:
        printdim("使用默认配置")
    issues = cfg.validate()
    if issues:
        for issue in issues:
            printerr(issue)
            ok = False
    else:
        printinfo("配置验证通过")

    # 3. 源目录
    printstep("◆", "路径检查")
    roots = cfg.source_roots
    if roots:
        for src in roots:
            if src.is_dir():
                printinfo(f"源目录可访问: {src}")
                if not os.access(src, os.R_OK):
                    printerr(f"源目录不可读: {src}")
                    ok = False
            else:
                printerr(f"源目录不存在: {src}")
                ok = False
        # 扫描文件数
        if any(r.is_dir() for r in roots):
            items = scan_files(cfg)
            if items:
                total_size = sum(i.size for i in items)
                total_mb = total_size / (1024 * 1024)
                printinfo(f"发现 {len(items)} 个候选文件 ({total_mb:.1f} MB)")
            else:
                printwarn("未发现符合条件的文件")

    dst = cfg.dest_root
    if dst and dst != Path():
        if dst.is_dir():
            printinfo(f"目标目录存在: {dst}")
            if os.access(dst, os.W_OK):
                printinfo("目标目录可写")
            else:
                printerr("目标目录不可写")
                ok = False
        else:
            printwarn(f"目标目录不存在 (将自动创建): {dst}")

    # 4. 状态目录
    printstep("◆", "运行状态")
    state = cfg.state_dir
    if state.is_dir():
        printinfo(f"状态目录: {state}")
    else:
        printwarn(f"状态目录不存在 (将自动创建): {state}")

    log_dir = cfg.log_dir
    if log_dir.is_dir():
        printinfo(f"日志目录: {log_dir}")
    else:
        printwarn(f"日志目录不存在 (将自动创建): {log_dir}")

    # 5. 锁文件
    lock_path = cfg.lock_path
    if lock_path.exists():
        try:
            pid_str = lock_path.read_text().strip()
            pid = int(pid_str)
            try:
                os.kill(pid, 0)
                printwarn(f"锁文件存在，进程 {pid} 正在运行")
            except ProcessLookupError:
                printwarn(f"发现残留锁文件 (PID {pid} 已不存在)，可自动回收")
        except (ValueError, FileNotFoundError):
            printwarn("锁文件内容异常，可自动回收")
    else:
        printinfo("无锁冲突")

    # 6. 通知与忽略规则
    printstep("◆", "通知与忽略规则")
    if cfg.notify_enabled:
        printinfo("桌面通知: 已开启")
        details = []
        if cfg.notify_on_success:
            details.append("成功")
        if cfg.notify_on_failure:
            details.append("失败")
        if cfg.notify_on_disk_low:
            details.append(f"磁盘<{cfg.disk_low_threshold_gb}GB")
        printdim(f"    触发条件: {' / '.join(details)}")
    else:
        printdim("桌面通知: 已关闭")

    ignore_pats = _load_ignore_patterns(roots)
    ignore_count = len(ignore_pats)
    printinfo(f"忽略规则: {ignore_count} 条模式")
    # 检查是否存在 .adobackignore 文件
    for root in roots:
        igf = root / IGNOREFILE_NAME
        if igf.is_file():
            printdim(f"    自定义忽略: {igf}")

    # 7. 服务
    printstep("◆", "服务状态")
    plist = _get_plist_path(cfg)
    if plist.exists():
        printinfo(f"服务已安装: {plist}")
    else:
        printdim("服务尚未安装")

    # 结论
    printout("")
    printbar()
    if ok:
        printout(f"  {_c(_GREEN, _ICON_SPARK + ' 自检通过 — 一切就绪，可以开始使用了！')}")
    else:
        printout(f"  {_c(_RED, _ICON_ERR + ' 自检发现问题，请根据上方提示修复')}")

    return ok


# ─────────────────────────────────────────────
# 配置命令
# ─────────────────────────────────────────────

CONFIG_TEMPLATE = """\
# Adoback 配置文件
# 文档: https://github.com/SOULRAi/adoback

[general]
language = "zh"

[source]
# 必填: 你的 Adobe 项目文件目录 (支持多个)
# 每行一个目录，Adoback 会扫描所有目录中的 Adobe 文件
# 示例:
#   roots = [
#       "/Users/你的用户名/Documents/Photoshop",
#       "/Users/你的用户名/Documents/Premiere",
#       "/Users/你的用户名/Documents/AfterEffects"
#   ]
roots = []

[destination]
# 必填: 备份输出目录 (不能与源目录相同)
# 示例: root = "/Volumes/BackupDisk/AdobeBackup"
root = ""
# 布局模式: snapshot (带时间戳快照) 或 mirror (直接镜像)
layout = "snapshot"

[filters]
# 要备份的文件扩展名
include = [
    ".aep", ".aegraphic", ".ai", ".idml", ".indd",
    ".pdf", ".prproj", ".psd", ".psb", ".xd"
]
# 排除的文件模式
exclude = [
    "*.bak", "*.idlk", "*.lck", "*.lock",
    "*.swp", "*.tmp", "._*", ".DS_Store", "~*"
]
# 排除的目录
exclude_dirs = [
    ".git", ".svn", "__pycache__", "node_modules",
    "Adobe After Effects Auto-Save",
    "Adobe InDesign Recovered",
    "Adobe Premiere Pro Auto-Save"
]

[performance]
# 并发复制线程数
workers = 4
# 单次读写块大小 (MB)
copy_chunk_mb = 4
# 校验模式: none / mtime_size / sha256
verify = "mtime_size"
# 复制后 fsync 确保落盘
fsync = true
# 单文件失败重试次数
retry_count = 2
# 重试间隔 (毫秒)
retry_delay_ms = 500

[retention]
# 保留最近 N 个快照
keep_last = 10
# 保留最近 N 天的快照
keep_days = 30

[state]
dir = "{state_dir}"

[logging]
dir = "{log_dir}"
level = "info"

[schedule]
# 守护模式轮询间隔 (秒)
default_interval_seconds = 300

[notification]
# macOS 桌面通知
enabled = true
# 备份成功时通知
on_success = true
# 备份失败时通知
on_failure = true
# 磁盘空间不足时通知
on_disk_low = true
# 磁盘空间不足阈值 (GB)
disk_low_threshold_gb = 5

[service]
label = "com.local.adoback"
"""


def cmd_config_init(args, cfg):
    """生成配置模板。"""
    path = Path(args.output) if args.output else DEFAULT_CONFIG_PATH
    if path.exists() and not args.force:
        printerr(f"配置文件已存在: {path}")
        printinfo("使用 --force 覆盖")
        sys.exit(EXIT_CONFIG)

    path.parent.mkdir(parents=True, exist_ok=True)
    content = CONFIG_TEMPLATE.format(
        state_dir=str(DEFAULT_STATE_DIR),
        log_dir=str(DEFAULT_LOG_DIR),
    )
    path.write_text(content, encoding="utf-8")
    printinfo(f"配置模板已生成: {path}")
    printout("")
    printout("下一步:")
    printout(f"  1. 编辑配置文件，设置 source.root 和 destination.root")
    printout(f"     nano {path}")
    printout(f"  2. 运行自检:")
    printout(f"     {_prog()} doctor")


def cmd_config_show(args, cfg):
    """显示当前生效配置。"""
    printout("当前配置:")
    printout("=" * 40)
    _print_config_section(cfg.data, indent=0)


def _print_config_section(d: dict, indent: int):
    for k, v in d.items():
        prefix = "  " * indent
        if isinstance(v, dict):
            printout(f"{prefix}[{k}]")
            _print_config_section(v, indent + 1)
        elif isinstance(v, list):
            printout(f"{prefix}{k} = {v}")
        else:
            printout(f"{prefix}{k} = {v!r}")


def cmd_config_validate(args, cfg):
    """验证配置。"""
    issues = cfg.validate()
    if not issues:
        printinfo("✓ 配置验证通过")
    else:
        printerr("配置存在以下问题:")
        for issue in issues:
            printout(f"  ✗ {issue}")
        sys.exit(EXIT_CONFIG)


def cmd_config_paths(args, cfg):
    """显示所有关键路径及其状态。"""
    printout("")
    printbox("关键路径", style="single")
    printout("")

    config_p = Path(args.config) if args.config else getattr(cfg, '_config_path', DEFAULT_CONFIG_PATH)
    exists_mark = _c(_GREEN, _ICON_OK) if config_p.exists() else _c(_DIM, _ICON_ERR)
    printout(f"  {exists_mark} {'配置文件':10s}  {config_p}")

    # 多源目录
    roots = cfg.source_roots
    if roots:
        for i, r in enumerate(roots):
            label = "源目录" if i == 0 else ""
            exists_mark = _c(_GREEN, _ICON_OK) if r.exists() else _c(_DIM, _ICON_ERR)
            printout(f"  {exists_mark} {label:10s}  {r}")
    else:
        printout(f"  {_c(_DIM, _ICON_ERR)} {'源目录':10s}  (未设置)")

    other_paths = [
        ("目标目录", cfg.dest_root),
        ("状态目录", cfg.state_dir),
        ("日志目录", cfg.log_dir),
        ("报告目录", cfg.report_dir),
        ("锁文件", cfg.lock_path),
        ("数据库", cfg.manifest_path),
        ("JSONL日志", cfg.log_path),
        ("服务plist", _get_plist_path(cfg)),
    ]
    for name, p in other_paths:
        p = Path(p)
        exists_mark = _c(_GREEN, _ICON_OK) if p.exists() else _c(_DIM, _ICON_ERR)
        printout(f"  {exists_mark} {name:10s}  {p}")
    printout("")


# ─────────────────────────────────────────────
# Guide 引导
# ─────────────────────────────────────────────

GUIDE_TEXT = """\

  ✦ Adoback 新手引导 ✦

  嘿！欢迎使用 Adoback 👋

  简单来说，这个工具帮你自动备份 Mac 上的 Adobe 项目文件。
  不管是 PS 闪退、PR 崩溃、还是不小心覆盖了文件——
  有了 Adoback，你总能找回之前的版本。

  它能备份这些文件:
    Photoshop (.psd .psb)    Illustrator (.ai)
    Premiere (.prproj)       After Effects (.aep)
    InDesign (.indd .idml)   XD (.xd)    PDF (.pdf)

  ─────────────────────────────────────────

  🚀 第一次用？一条命令搞定:

    {prog} setup

  它会引导你设置好所有东西，包括:
  告诉它你的 Adobe 文件放在哪、备份存到哪。
  你可以添加多个目录，比如 PS 的一个、PR 的一个。

  ─────────────────────────────────────────

  📋 也可以手动来，一步一步:

    1. 生成配置文件     {prog} config init
    2. 编辑配置         nano ~/.local/adoback/config.toml
    3. 跑一下自检       {prog} doctor
    4. 先试试看(不复制)  {prog} backup --dry-run
    5. 真正备份一次     {prog} backup --full
    6. 设成开机自动跑   {prog} service on

  ─────────────────────────────────────────

  📌 日常用得最多的命令:

    {prog} backup               跑一次增量备份
    {prog} backup --full        全量备份（第一次建议用这个）
    {prog} service on           设成开机自动备份，装完就不用管了
    {prog} doctor               检查一下状态，看看有没有问题
    {prog} last-run             看看上次备份的情况

  📌 管理类命令:

    {prog} config show          看当前配置
    {prog} service status       看看服务跑没跑
    {prog} service off          不想用了？关掉服务

  ─────────────────────────────────────────

  🔔 桌面通知:

  备份完成、失败、磁盘空间不足时都会弹通知。
  不想被打扰？在 config.toml 里关掉:
    [notification]
    enabled = false

  📝 忽略规则:

  默认就帮你排除了 Adobe 缓存、Media Cache 等垃圾文件。
  想自定义？在源目录下放一个 .adobackignore 文件:
    # 排除这些文件
    *.tmp
    Media Cache Files/
  语法跟 .gitignore 差不多，很好理解。

  ─────────────────────────────────────────

  ⚠️  有件事要提前说清楚:

  Adoback 只能备份已经保存到硬盘上的文件。
  如果你在 PS 里画了一个小时但一直没按 Cmd+S，
  那这部分内容是没法被任何外部工具备份到的。

  建议: 打开 Adobe 自带的自动保存，再加上 Adoback，双重保险。
"""


def cmd_guide(args, cfg):
    """新手引导。"""
    if getattr(args, "interactive", False):
        _interactive_guide()
    else:
        printout(GUIDE_TEXT.format(prog=_prog()))


def _interactive_guide():
    """交互式新手引导。"""
    printout("")
    printbox("Adoback 交互式引导")
    printout("")
    printdim("    一步一步来，帮你把备份配好 ~")

    # 1. 检查配置文件
    printstep(1, "检查配置文件")
    config_path = DEFAULT_CONFIG_PATH
    if config_path.exists():
        printinfo(f"配置文件已存在: {config_path}")
        printout(f"  {_c(_CYAN, '?')} 要重新生成吗？(y/N) ", )
        try:
            ans = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            printout("")
            return
        if ans == "y":
            config_path.parent.mkdir(parents=True, exist_ok=True)
            content = CONFIG_TEMPLATE.format(
                state_dir=str(DEFAULT_STATE_DIR),
                log_dir=str(DEFAULT_LOG_DIR),
            )
            config_path.write_text(content, encoding="utf-8")
            printinfo("已重新生成")
    else:
        printdim("    配置文件不存在，帮你生成一个...")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        content = CONFIG_TEMPLATE.format(
            state_dir=str(DEFAULT_STATE_DIR),
            log_dir=str(DEFAULT_LOG_DIR),
        )
        config_path.write_text(content, encoding="utf-8")
        printinfo(f"已生成: {config_path}")

    # 2. 设置源目录（多个）
    printstep(2, "设置源目录")
    roots = _ask_source_dirs()

    # 3. 设置目标目录
    printstep(3, "设置目标目录")
    dst = _ask_dest_dir()

    if roots and dst:
        try:
            _write_roots_to_config(config_path, roots, dst)
            printout("")
            printinfo("配置已更新")
            for r in roots:
                printdim(f"    源: {r}")
            printdim(f"    目标: {dst}")
        except Exception as e:
            printerr(f"写入配置失败: {e}")
            return

    # 4. 自检
    printout("")
    printstep(4, "运行自检")
    try:
        cfg = Config.load(config_path)
        run_doctor(cfg)
    except SystemExit:
        printerr("自检中发现配置问题")

    # 5. 建议下一步
    printout("")
    printout("━━━ 引导完成 ━━━")
    printout(f"  接下来你可以:")
    printout(f"  1. 试运行:    {_prog()} backup --dry-run")
    printout(f"  2. 全量备份:  {_prog()} backup --full")
    printout(f"  3. 开机自启:  {_prog()} service on")


# ─────────────────────────────────────────────
# last-run / report 命令
# ─────────────────────────────────────────────

def cmd_last_run(args, cfg):
    """查看最近一次运行。"""
    db = Database(cfg.manifest_path)
    try:
        if getattr(args, "count", None) and args.count > 1:
            runs = db.recent_runs(args.count)
            if not runs:
                printinfo("暂无运行记录")
                return
            printout(f"最近 {len(runs)} 次运行:")
            printout("")
            for r in runs:
                _print_run(r)
                printout("")
        else:
            run = db.last_run()
            if not run:
                printinfo("暂无运行记录")
                return
            printout("最近一次运行:")
            _print_run(run)
    finally:
        db.close()


def _print_run(run: dict):
    printout(f"  ID:       {run.get('id')}")
    printout(f"  开始:     {run.get('started_at', '-')}")
    printout(f"  结束:     {run.get('finished_at', '-')}")
    printout(f"  模式:     {run.get('mode', '-')}")
    printout(f"  状态:     {_status_zh(run.get('status', '-'))}")
    printout(f"  总文件:   {run.get('total_files', 0)}")
    printout(f"  已复制:   {run.get('copied', 0)}")
    printout(f"  已跳过:   {run.get('skipped', 0)}")
    printout(f"  失败:     {run.get('failed', 0)}")
    mb = run.get('bytes_copied', 0) / (1024 * 1024)
    dur = run.get('duration_ms', 0) / 1000
    printout(f"  数据量:   {mb:.1f} MB")
    printout(f"  耗时:     {dur:.1f} 秒")


def cmd_report(args, cfg):
    """查看或列出报告。"""
    report_dir = cfg.report_dir
    if not report_dir.is_dir():
        printinfo("暂无报告")
        return

    reports = sorted(report_dir.iterdir(), reverse=True)
    if not reports:
        printinfo("暂无报告")
        return

    fmt = getattr(args, "format", "text") or "text"

    if getattr(args, "list_all", False):
        printout(f"报告目录: {report_dir}")
        printout(f"共 {len(reports)} 份报告:")
        for r in reports[:20]:
            printout(f"  {r.name}")
        return

    # 显示最新报告
    latest = reports[0]
    printout(f"最新报告: {latest.name}")
    printout("-" * 40)
    printout(latest.read_text(encoding="utf-8"))


# ─────────────────────────────────────────────
# 安装/卸载
# ─────────────────────────────────────────────

def cmd_install(args, cfg):
    """一键安装。"""
    printout("")
    printbox("Adoback 安装", style="single")

    # 1. 创建目录
    printstep(1, "创建目录结构")
    for d in [DEFAULT_INSTALL_DIR / "bin", DEFAULT_STATE_DIR, DEFAULT_LOG_DIR]:
        d.mkdir(parents=True, exist_ok=True)
        printinfo(f"{d}")

    # 2. 复制主程序
    printstep(2, "安装主程序")
    src_path = Path(_get_bin_path())
    if _is_frozen():
        dst_bin = DEFAULT_INSTALL_DIR / "bin" / "adoback"
    else:
        dst_bin = DEFAULT_INSTALL_DIR / "bin" / "adoback.py"

    if src_path.resolve() != dst_bin.resolve():
        shutil.copy2(str(src_path), str(dst_bin))
        dst_bin.chmod(0o755)
    printinfo(f"{dst_bin}")

    # 3. 创建符号链接（方便 PATH 调用）
    if _is_frozen():
        link_path = Path.home() / ".local" / "bin" / "adoback"
        link_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if link_path.is_symlink() or link_path.exists():
                link_path.unlink()
            link_path.symlink_to(dst_bin)
            printinfo(f"符号链接: {link_path}")
        except OSError as e:
            printwarn(f"无法创建符号链接: {e}")

    # 4. 配置文件
    printstep(3, "检查配置文件")
    if DEFAULT_CONFIG_PATH.exists():
        printinfo(f"配置已存在: {DEFAULT_CONFIG_PATH}")
    else:
        content = CONFIG_TEMPLATE.format(
            state_dir=str(DEFAULT_STATE_DIR),
            log_dir=str(DEFAULT_LOG_DIR),
        )
        DEFAULT_CONFIG_PATH.write_text(content, encoding="utf-8")
        printinfo(f"已生成默认配置: {DEFAULT_CONFIG_PATH}")

    # 5. 提示 PATH
    printstep(4, "环境变量")
    shell_profile = "~/.zshrc" if "zsh" in os.environ.get("SHELL", "") else "~/.bash_profile"
    if _is_frozen():
        path_hint = 'export PATH="$HOME/.local/bin:$PATH"'
    else:
        path_hint = f'alias adoback="python3 {dst_bin}"'
    printdim(f"建议添加到 {shell_profile}:")
    printdim(f"  {path_hint}")

    printout("")
    printinfo(_c(_GREEN, "安装完成!"))
    printout("")
    printout("  下一步:")
    printout(f"  1. 编辑配置: nano {DEFAULT_CONFIG_PATH}")
    printout(f"  2. 运行自检: {_prog()} doctor")
    printout(f"  3. 开机自启: {_prog()} service on")


def cmd_uninstall(args, cfg):
    """卸载。"""
    printout("")
    printbox("Adoback 卸载", style="single")
    printout("")

    # 停止并卸载服务
    printstep(1, "停止服务")
    service_uninstall(cfg)

    # 询问是否删除数据
    if not getattr(args, "yes", False):
        printout("")
        printout("是否同时删除所有备份数据和状态? (y/N) ", )
        try:
            ans = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
    else:
        ans = "y"

    printstep(2, "清理文件")
    # 删除安装目录
    install_dir = DEFAULT_INSTALL_DIR
    if install_dir.is_dir():
        shutil.rmtree(install_dir)
        printout(f"    ✓ 已删除: {install_dir}")

    if ans == "y":
        state_dir = cfg.state_dir
        if state_dir.is_dir():
            shutil.rmtree(state_dir)
            printout(f"    ✓ 已删除: {state_dir}")
        log_dir = cfg.log_dir
        if log_dir.is_dir():
            shutil.rmtree(log_dir)
            printout(f"    ✓ 已删除: {log_dir}")

    printinfo("卸载完成")


# ─────────────────────────────────────────────
# update 自动更新
# ─────────────────────────────────────────────

def cmd_restore(args, cfg):
    """交互式恢复备份文件。"""
    dest = cfg.dest_root
    layout = cfg.layout

    printout("")
    printbox("Adoback · 备份恢复")
    printout("")

    if not dest.is_dir():
        printerr(f"备份目录不存在: {dest}")
        sys.exit(EXIT_CONFIG)

    # ── 收集所有备份文件 ──
    # 结构: {rel_path: [(snapshot_name, full_path, size, mtime), ...]}
    file_map: dict[str, list[tuple[str, Path, int, float]]] = {}

    if layout == "snapshot":
        snapshots = sorted(
            [d for d in dest.iterdir() if d.is_dir()],
            key=lambda d: d.name,
            reverse=True,
        )
        if not snapshots:
            printinfo("备份目录为空，没有可恢复的快照")
            return

        for snap in snapshots:
            for fp in snap.rglob("*"):
                if fp.is_file():
                    rel = str(fp.relative_to(snap))
                    try:
                        st = fp.stat()
                    except OSError:
                        continue
                    file_map.setdefault(rel, []).append(
                        (snap.name, fp, st.st_size, st.st_mtime)
                    )
    else:
        # mirror 模式：只有一份
        for fp in dest.rglob("*"):
            if fp.is_file():
                rel = str(fp.relative_to(dest))
                try:
                    st = fp.stat()
                except OSError:
                    continue
                file_map.setdefault(rel, []).append(
                    ("mirror", fp, st.st_size, st.st_mtime)
                )

    if not file_map:
        printinfo("备份目录为空，没有可恢复的文件")
        return

    # ── 搜索/筛选 ──
    search = getattr(args, "search", None)
    list_only = getattr(args, "list", False)

    if search:
        search_lower = search.lower()
        matched = {k: v for k, v in file_map.items() if search_lower in k.lower()}
        if not matched:
            printerr(f"没有找到匹配 '{search}' 的备份文件")
            printdim(f"    共 {len(file_map)} 个文件可搜索")
            return
        file_map = matched

    # 按文件名排序
    sorted_files = sorted(file_map.keys())

    # ── 仅列表模式 ──
    if list_only:
        printinfo(f"备份文件列表 ({len(sorted_files)} 个)")
        printbar("─")
        for rel in sorted_files:
            versions = file_map[rel]
            size_mb = versions[0][2] / (1024 * 1024)
            snap_count = len(versions)
            snap_info = f"{snap_count} 个快照" if layout == "snapshot" else "mirror"
            printout(f"  {_c(_CYAN, rel)}")
            printdim(f"    {size_mb:.1f} MB · {snap_info}")
        printout("")
        return

    # ── 交互式选择文件 ──
    printinfo(f"找到 {len(sorted_files)} 个可恢复的文件")
    printout("")

    # 分页显示
    page_size = 20
    page = 0
    total_pages = (len(sorted_files) + page_size - 1) // page_size

    while True:
        start = page * page_size
        end = min(start + page_size, len(sorted_files))
        page_files = sorted_files[start:end]

        for i, rel in enumerate(page_files, start=start + 1):
            versions = file_map[rel]
            snap_count = len(versions)
            tag = f"[{snap_count}个版本]" if layout == "snapshot" else ""
            printout(f"  {_c(_BOLD, str(i)):>4s}  {rel}  {_c(_DIM, tag)}")

        printout("")
        if total_pages > 1:
            printdim(f"    第 {page + 1}/{total_pages} 页")
        printout(f"  {_c(_CYAN, '?')} 输入编号选择文件 (n=下一页, q=退出): ", )

        try:
            choice = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            printout("")
            return

        if choice == "q" or choice == "quit":
            return
        if choice == "n" and page + 1 < total_pages:
            page += 1
            continue
        if choice == "p" and page > 0:
            page -= 1
            continue

        try:
            idx = int(choice)
            if 1 <= idx <= len(sorted_files):
                selected_rel = sorted_files[idx - 1]
                break
            printout(f"    {_c(_YELLOW, '编号超出范围，请重新输入')}")
        except ValueError:
            # 尝试作为搜索词
            search_lower = choice.lower()
            matched_idx = [i for i, f in enumerate(sorted_files) if search_lower in f.lower()]
            if len(matched_idx) == 1:
                selected_rel = sorted_files[matched_idx[0]]
                break
            elif len(matched_idx) > 1:
                printout(f"    {_c(_YELLOW, f'匹配到 {len(matched_idx)} 个文件，请用编号精确选择')}")
            else:
                printout(f"    {_c(_YELLOW, '无效输入，请输入编号')}")

    # ── 选择版本 ──
    versions = file_map[selected_rel]
    printout("")
    printbar("─")
    printout(f"  {_c(_BOLD, selected_rel)} 的备份版本:")
    printbar("─")

    for i, (snap_name, fp, size, mtime) in enumerate(versions, 1):
        size_mb = size / (1024 * 1024)
        t = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        if layout == "snapshot":
            printout(f"  {_c(_BOLD, str(i)):>4s}  {_c(_CYAN, snap_name)}  {size_mb:.1f} MB  {_c(_DIM, t)}")
        else:
            printout(f"  {_c(_BOLD, str(i)):>4s}  {size_mb:.1f} MB  {_c(_DIM, t)}")

    if len(versions) == 1:
        version_idx = 0
    else:
        printout("")
        printout(f"  {_c(_CYAN, '?')} 选择版本编号 (默认 1 = 最新): ", )
        try:
            v_choice = input().strip()
        except (EOFError, KeyboardInterrupt):
            printout("")
            return
        version_idx = 0
        if v_choice:
            try:
                vi = int(v_choice)
                if 1 <= vi <= len(versions):
                    version_idx = vi - 1
            except ValueError:
                pass

    snap_name, src_fp, size, mtime = versions[version_idx]

    # ── 确定恢复目标路径 ──
    # 尝试还原到原始源目录
    roots = cfg.source_roots
    restore_target = None

    # 如果 rel_path 格式是 "rootname/subpath"，尝试匹配源目录
    parts = selected_rel.split("/", 1)
    if len(parts) > 1 and roots:
        for r in roots:
            if r.name == parts[0]:
                restore_target = r / parts[1]
                break
    if not restore_target and roots:
        restore_target = roots[0] / selected_rel

    printout("")
    printout(f"  {_c(_MAGENTA, _ICON_ARROW)} 恢复到: {_c(_BOLD, str(restore_target))}")
    size_mb = size / (1024 * 1024)
    printdim(f"    大小: {size_mb:.1f} MB  来自: {snap_name}")

    # 如果目标文件已存在，先备份
    if restore_target and restore_target.exists():
        printout(f"  {_c(_YELLOW, _ICON_WARN)} 目标文件已存在，将先备份当前版本")

    printout("")
    printout(f"  {_c(_CYAN, '?')} 确认恢复? (y/N): ", )
    try:
        confirm = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        printout("")
        return

    if confirm not in ("y", "yes"):
        printinfo("已取消恢复")
        return

    # ── 执行恢复 ──
    try:
        # 如果目标已存在，备份到 .bak
        if restore_target.exists():
            bak_ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            bak_path = restore_target.with_suffix(restore_target.suffix + f".bak.{bak_ts}")
            shutil.copy2(str(restore_target), str(bak_path))
            printinfo(f"当前版本已备份到: {bak_path.name}")

        # 确保目标目录存在
        restore_target.parent.mkdir(parents=True, exist_ok=True)

        # 复制恢复
        shutil.copy2(str(src_fp), str(restore_target))

        printout("")
        printbar("═")
        printout(f"  {_c(_GREEN, _ICON_SPARK)} {_c(_BOLD, _c(_GREEN, '恢复成功!'))}")
        printbar("═")
        printout(f"    {_c(_DIM, str(restore_target))}")
        printout("")

    except Exception as e:
        printerr(f"恢复失败: {e}")
        sys.exit(EXIT_RUNTIME)


def cmd_clean(args, cfg):
    """备份空间清理管理。"""
    dest = cfg.dest_root
    layout = cfg.layout
    dry_run = getattr(args, "dry_run", False)
    force = getattr(args, "force", False)

    # 清理策略参数（命令行覆盖配置文件）
    keep_last = getattr(args, "keep_last", None)
    if keep_last is None:
        keep_last = cfg.keep_last
    keep_days = getattr(args, "keep_days", None)
    if keep_days is None:
        keep_days = cfg.keep_days
    max_size_gb = getattr(args, "max_size", None)

    printout("")
    printbox("Adoback · 备份清理")
    printout("")

    if not dest.is_dir():
        printerr(f"备份目录不存在: {dest}")
        sys.exit(EXIT_CONFIG)

    # ── 统计当前空间占用 ──
    printstep(1, "扫描备份目录")
    total_size = 0
    total_files = 0

    if layout == "snapshot":
        snapshots = sorted(
            [d for d in dest.iterdir() if d.is_dir()],
            key=lambda d: d.name,
            reverse=True,
        )
        for snap in snapshots:
            for fp in snap.rglob("*"):
                if fp.is_file():
                    try:
                        total_size += fp.stat().st_size
                        total_files += 1
                    except OSError:
                        pass

        total_mb = total_size / (1024 * 1024)
        total_gb = total_size / (1024 * 1024 * 1024)
        printinfo(f"备份目录: {dest}")
        printinfo(f"快照数量: {len(snapshots)}")
        printinfo(f"文件总数: {total_files}")
        printinfo(f"占用空间: {total_gb:.2f} GB ({total_mb:.0f} MB)")
        printout("")

        if not snapshots:
            printinfo("没有快照需要清理")
            return

        # ── 确定要清理的快照 ──
        printstep(2, "分析清理策略")
        printdim(f"    保留最新 {keep_last} 个快照")
        printdim(f"    清理 {keep_days} 天前的快照")
        if max_size_gb is not None:
            printdim(f"    总容量上限 {max_size_gb} GB")

        cutoff = datetime.datetime.now() - datetime.timedelta(days=keep_days)
        to_remove = []
        remove_size = 0

        for i, snap in enumerate(snapshots):
            if i < keep_last:
                continue
            # 尝试从目录名解析时间
            try:
                snap_time = datetime.datetime.strptime(snap.name, "%Y%m%d_%H%M%S")
            except ValueError:
                continue
            if snap_time < cutoff:
                snap_size = sum(
                    f.stat().st_size for f in snap.rglob("*")
                    if f.is_file()
                )
                to_remove.append((snap, snap_size, snap_time))
                remove_size += snap_size

        # 容量上限策略：如果设置了 max_size_gb，从最旧的开始删
        if max_size_gb is not None:
            max_bytes = max_size_gb * 1024 * 1024 * 1024
            if total_size > max_bytes:
                # 从最旧的（列表末尾）开始标记
                already_removing = {s[0].name for s in to_remove}
                remaining_size = total_size - remove_size
                for snap in reversed(snapshots):
                    if remaining_size <= max_bytes:
                        break
                    if snap.name in already_removing:
                        continue
                    # 不删最新的 keep_last 个
                    snap_idx = snapshots.index(snap)
                    if snap_idx < keep_last:
                        continue
                    snap_size = sum(
                        f.stat().st_size for f in snap.rglob("*")
                        if f.is_file()
                    )
                    try:
                        snap_time = datetime.datetime.strptime(snap.name, "%Y%m%d_%H%M%S")
                    except ValueError:
                        snap_time = datetime.datetime.now()
                    to_remove.append((snap, snap_size, snap_time))
                    remove_size += snap_size
                    remaining_size -= snap_size

        if not to_remove:
            printout("")
            printout(f"  {_c(_GREEN, _ICON_OK)} 没有需要清理的快照")
            printdim(f"    所有快照都在保留策略范围内")
            printout("")
            return

        # ── 预览 ──
        printstep(3, "清理预览" if dry_run else "执行清理")
        remove_mb = remove_size / (1024 * 1024)
        remove_gb = remove_size / (1024 * 1024 * 1024)
        printout("")
        printout(f"  {_c(_YELLOW, _ICON_WARN)} 将清理 {_c(_BOLD, str(len(to_remove)))} 个快照，释放 {_c(_BOLD, f'{remove_gb:.2f} GB')} 空间")
        printout("")

        for snap, snap_size, snap_time in sorted(to_remove, key=lambda x: x[2]):
            snap_mb = snap_size / (1024 * 1024)
            t = snap_time.strftime("%Y-%m-%d %H:%M")
            printout(f"    {_c(_RED, '✗')} {snap.name}  {_c(_DIM, f'{snap_mb:.1f} MB')}  {_c(_DIM, t)}")

        # ── dry-run 到此为止 ──
        if dry_run:
            printout("")
            printout(f"  {_c(_DIM, '[预演模式] 以上文件不会被删除')}")
            printout(f"  去掉 --dry-run 执行实际清理")
            printout("")
            return

        # ── 确认 ──
        if not force:
            printout("")
            printout(f"  {_c(_CYAN, '?')} 确认清理? 删除后不可恢复 (y/N): ", )
            try:
                confirm = input().strip().lower()
            except (EOFError, KeyboardInterrupt):
                printout("")
                return
            if confirm not in ("y", "yes"):
                printinfo("已取消清理")
                return

        # ── 执行删除 ──
        cleaned = 0
        cleaned_size = 0
        for snap, snap_size, _ in to_remove:
            try:
                shutil.rmtree(snap)
                cleaned += 1
                cleaned_size += snap_size
                printinfo(f"已删除: {snap.name}")
            except Exception as e:
                printwarn(f"删除失败: {snap.name} - {e}")

        cleaned_gb = cleaned_size / (1024 * 1024 * 1024)
        remaining_gb = (total_size - cleaned_size) / (1024 * 1024 * 1024)

        printout("")
        printbar("═")
        printout(f"  {_c(_GREEN, _ICON_SPARK)} {_c(_BOLD, _c(_GREEN, '清理完成!'))}")
        printbar("═")
        printout(f"    删除快照   {cleaned} 个")
        printout(f"    释放空间   {cleaned_gb:.2f} GB")
        printout(f"    剩余占用   {remaining_gb:.2f} GB")
        printout("")

    else:
        # mirror 模式：没有快照概念，直接统计
        for fp in dest.rglob("*"):
            if fp.is_file():
                try:
                    total_size += fp.stat().st_size
                    total_files += 1
                except OSError:
                    pass
        total_gb = total_size / (1024 * 1024 * 1024)
        printinfo(f"备份目录 (mirror 模式): {dest}")
        printinfo(f"文件总数: {total_files}")
        printinfo(f"占用空间: {total_gb:.2f} GB")
        printout("")
        printdim("    mirror 模式下每个文件只保留一份，无需按快照清理")
        printdim("    如需释放空间，请手动删除不需要的备份文件")
        printout("")


def cmd_update(args, cfg):
    """检查并自动更新到最新版本。"""
    check_only = getattr(args, "check", False)

    printout("")
    printbox("Adoback 更新", style="single")

    # 1. 当前版本
    printstep(1, "当前版本")
    printinfo(f"v{VERSION}")

    # 2. 检查最新版本
    printstep(2, "检查最新版本")
    try:
        release = _github_get_json(GITHUB_API_LATEST)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            printerr("该仓库尚未发布任何 Release")
        else:
            printerr(f"GitHub API 返回错误: HTTP {e.code}")
        printdim(f"    https://github.com/{GITHUB_REPO}/releases")
        sys.exit(EXIT_RUNTIME)
    except urllib.error.URLError as e:
        printerr(f"无法连接 GitHub: {e.reason}")
        printdim("    请检查网络连接，或手动前往:")
        printdim(f"    https://github.com/{GITHUB_REPO}/releases")
        sys.exit(EXIT_RUNTIME)
    except Exception as e:
        printerr(f"获取版本信息失败: {e}")
        sys.exit(EXIT_RUNTIME)

    latest_tag = release.get("tag_name", "").lstrip("v")
    if not latest_tag:
        printerr("无法解析最新版本号")
        sys.exit(EXIT_RUNTIME)

    printinfo(f"最新版本: v{latest_tag}")

    # 3. 比较版本
    try:
        local_ver = _parse_version(VERSION)
        remote_ver = _parse_version(latest_tag)
    except (ValueError, TypeError):
        printerr("版本号格式异常")
        sys.exit(EXIT_RUNTIME)

    if remote_ver <= local_ver:
        printout("")
        printout(f"  {_c(_GREEN, _ICON_SPARK)} {_c(_BOLD, '已是最新版本，无需更新')}")
        printout("")
        return

    printout("")
    printout(f"  {_c(_MAGENTA, _ICON_ARROW)} 发现新版本: {_c(_DIM, 'v' + VERSION)} → {_c(_GREEN, _c(_BOLD, 'v' + latest_tag))}")

    # 仅检查模式
    if check_only:
        printout("")
        printout(f"  运行 {_c(_CYAN, _prog() + ' update')} 进行更新")
        printout("")
        return

    # 4. 确定下载地址和目标路径
    target_path = Path(_get_bin_path()).resolve()

    if _is_frozen():
        # 二进制模式: 从 Release 下载架构匹配的二进制
        asset_name = _detect_asset_name()
        assets = release.get("assets", [])
        asset = None
        for a in assets:
            if asset_name.lower() in a["name"].lower():
                asset = a
                break
        if not asset:
            printerr(f"未找到匹配的二进制: {asset_name}")
            printdim(f"    可用资源: {', '.join(a['name'] for a in assets)}")
            printdim(f"    请手动前往 https://github.com/{GITHUB_REPO}/releases 下载")
            sys.exit(EXIT_RUNTIME)
        download_url = asset["browser_download_url"]
        download_name = asset["name"]
    else:
        # 源码模式: 从 main 分支下载 adoback.py
        download_url = GITHUB_RAW_SOURCE
        download_name = "adoback.py"

    # 5. 下载到临时文件
    printstep(3, "下载更新")
    printdim(f"    {download_name}")
    tmp_fd, tmp_path_str = tempfile.mkstemp(
        dir=str(target_path.parent),
        prefix=".adoback-update-",
    )
    tmp_path = Path(tmp_path_str)
    try:
        os.close(tmp_fd)
        _github_download(download_url, tmp_path)
        tmp_path.chmod(0o755)
        printinfo("下载完成")

        # 6. 验证
        printstep(4, "验证")
        if _is_frozen():
            result = subprocess.run(
                ["file", str(tmp_path)],
                capture_output=True, text=True,
            )
            if "Mach-O" not in result.stdout:
                raise RuntimeError("下载的文件不是有效的 macOS 二进制")
            printinfo("二进制验证通过 (Mach-O)")
        else:
            content = tmp_path.read_text(encoding="utf-8")
            if "VERSION" not in content or "def main" not in content:
                raise RuntimeError("下载的源码文件不完整")
            printinfo("源码验证通过")

        # 7. 原子替换
        printstep(5, "替换文件")
        os.replace(str(tmp_path), str(target_path))
        printinfo(f"已替换: {target_path}")

        # 同步符号链接（如果有）
        link_path = Path.home() / ".local" / "bin" / "adoback"
        if link_path.is_symlink():
            # 确保符号链接指向正确
            if link_path.resolve() != target_path:
                try:
                    link_path.unlink()
                    link_path.symlink_to(target_path)
                except OSError:
                    pass

        printout("")
        printbar("═")
        printout(f"  {_c(_GREEN, _ICON_SPARK)} {_c(_BOLD, _c(_GREEN, '更新完成!'))}  v{VERSION} → v{latest_tag}")
        printbar("═")
        printout("")

    except Exception as e:
        # 清理临时文件
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        printerr(f"更新失败: {e}")
        sys.exit(EXIT_RUNTIME)


# ─────────────────────────────────────────────
# setup 一键初始化
# ─────────────────────────────────────────────

def _write_roots_to_config(config_path: Path, roots: list[str], dst: str):
    """把多个源目录和目标目录写入 config.toml。"""
    text = config_path.read_text(encoding="utf-8")
    lines = text.split("\n")
    new_lines = []
    roots_set = dst_set = False
    in_source = in_dest = False
    skip_until_next_section = False
    for line in lines:
        stripped = line.strip()
        if stripped == "[source]":
            in_source, in_dest = True, False
            skip_until_next_section = False
        elif stripped == "[destination]":
            in_source, in_dest = False, True
            skip_until_next_section = False
        elif stripped.startswith("[") and stripped.endswith("]"):
            in_source = in_dest = False
            skip_until_next_section = False

        if skip_until_next_section:
            # 跳过旧的 roots 多行值
            if stripped.startswith("]") or stripped.startswith('"') or stripped == "":
                if stripped.startswith("]"):
                    skip_until_next_section = False
                continue

        if in_source and stripped.startswith("roots") and "=" in stripped and not roots_set:
            # 写入新的 roots 列表
            if len(roots) == 1:
                new_lines.append(f'roots = ["{roots[0]}"]')
            else:
                new_lines.append("roots = [")
                for r in roots:
                    new_lines.append(f'    "{r}",')
                new_lines.append("]")
            roots_set = True
            # 如果旧值是多行，跳过直到 ]
            if "[" in stripped and "]" not in stripped:
                skip_until_next_section = True
            continue
        elif in_dest and stripped.startswith("root") and "=" in stripped and not dst_set:
            new_lines.append(f'root = "{dst}"')
            dst_set = True
            continue

        new_lines.append(line)
    config_path.write_text("\n".join(new_lines), encoding="utf-8")


def _ask_source_dirs() -> list[str]:
    """交互式询问用户的源目录（支持多个）。"""
    user = os.environ.get("USER", "you")
    printout("")
    printout(f"  {_c(_CYAN, '?')} 你的 Adobe 项目文件放在哪？")
    printdim(f"    就是你平时存 PSD、AI、PR 工程的目录")
    printdim(f"    例如: /Users/{user}/Documents/Adobe")
    printout("")

    roots = []
    while True:
        if not roots:
            printout(f"  {_c(_MAGENTA, _ICON_ARROW)} 输入源目录路径: ", )
        else:
            printout(f"  {_c(_MAGENTA, _ICON_ARROW)} 再添加一个目录 (直接回车跳过): ", )
        try:
            path = input().strip()
        except (EOFError, KeyboardInterrupt):
            printout("")
            break
        if not path:
            if roots:
                break
            printout(f"    {_c(_YELLOW, '至少需要输入一个目录哦')}")
            continue
        # 展开 ~ 并验证
        expanded = str(Path(path).expanduser())
        roots.append(expanded)
        printinfo(f"已添加: {expanded}")
        if len(roots) < 10:
            printout(f"    {_c(_DIM, '还要添加其他目录吗？比如 PS 一个、PR 一个')}")

    return roots


def _ask_dest_dir() -> str:
    """交互式询问用户的备份目标目录。"""
    printout("")
    printout(f"  {_c(_CYAN, '?')} 备份存到哪？")
    printdim(f"    建议用外置硬盘或单独的分区")
    printdim(f"    例如: /Volumes/BackupDisk/AdobeBackup")
    printout("")
    printout(f"  {_c(_MAGENTA, _ICON_ARROW)} 输入目标目录路径: ", )
    try:
        dst = input().strip()
    except (EOFError, KeyboardInterrupt):
        printout("")
        return ""
    if dst:
        dst = str(Path(dst).expanduser())
    return dst


def cmd_setup(args, cfg):
    """一键初始化：安装 + 生成配置 + 交互引导 + 自检。"""
    printout("")
    printbox("Adoback · 一键初始化")
    printout("")
    printdim("    跟着提示走，几步就搞定 ~")

    # 1. 安装程序
    printstep(1, "安装程序")
    for d in [DEFAULT_INSTALL_DIR / "bin", DEFAULT_STATE_DIR, DEFAULT_LOG_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    src_path = Path(_get_bin_path())
    if _is_frozen():
        dst_bin = DEFAULT_INSTALL_DIR / "bin" / "adoback"
    else:
        dst_bin = DEFAULT_INSTALL_DIR / "bin" / "adoback.py"

    if src_path.resolve() != dst_bin.resolve():
        shutil.copy2(str(src_path), str(dst_bin))
        dst_bin.chmod(0o755)
    printinfo(f"已安装: {dst_bin}")

    if _is_frozen():
        link_path = Path.home() / ".local" / "bin" / "adoback"
        link_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if link_path.is_symlink() or link_path.exists():
                link_path.unlink()
            link_path.symlink_to(dst_bin)
            printinfo(f"符号链接: {link_path}")
        except OSError as e:
            printwarn(f"无法创建符号链接: {e}")

    # 2. 生成 / 检查配置
    printstep(2, "配置文件")
    config_path = DEFAULT_CONFIG_PATH
    if config_path.exists():
        printinfo(f"配置已存在: {config_path}")
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        content = CONFIG_TEMPLATE.format(
            state_dir=str(DEFAULT_STATE_DIR),
            log_dir=str(DEFAULT_LOG_DIR),
        )
        config_path.write_text(content, encoding="utf-8")
        printinfo(f"已生成: {config_path}")

    # 3. 交互设置源目录（多个）+ 目标目录
    printstep(3, "设置备份路径")
    roots = _ask_source_dirs()
    dst = _ask_dest_dir()

    if roots and dst:
        try:
            _write_roots_to_config(config_path, roots, dst)
            printout("")
            printinfo("备份路径已写入配置")
            for r in roots:
                printdim(f"    源: {r}")
            printdim(f"    目标: {dst}")
        except Exception as e:
            printerr(f"写入配置失败: {e}")
    elif roots or dst:
        printwarn("源目录和目标目录都需要填写才能保存")

    # 4. PATH 环境变量
    printstep(4, "环境变量")
    link_dir = str(Path.home() / ".local" / "bin")
    if link_dir in os.environ.get("PATH", ""):
        printinfo(f"$PATH 已包含 {link_dir}")
    else:
        shell_rc = "~/.zshrc" if "zsh" in os.environ.get("SHELL", "") else "~/.bash_profile"
        shell_rc_path = Path(shell_rc).expanduser()
        path_line = 'export PATH="$HOME/.local/bin:$PATH"'
        if shell_rc_path.exists() and ".local/bin" in shell_rc_path.read_text(encoding="utf-8"):
            printinfo(f"PATH 配置已存在于 {shell_rc}")
        else:
            with open(shell_rc_path, "a", encoding="utf-8") as f:
                f.write(f"\n# Adoback\n{path_line}\n")
            printinfo(f"已添加 PATH 到 {shell_rc}")
            printwarn(f"请运行 source {shell_rc} 或重新打开终端")

    # 5. 自检
    printstep(5, "自检")
    try:
        cfg = Config.load(config_path)
        run_doctor(cfg)
    except Exception:
        printwarn("自检未完成（可能配置尚未填写）")

    # 完成
    printout("")
    printbar("═")
    printout(f"  {_c(_GREEN, _ICON_SPARK)} {_c(_BOLD, _c(_GREEN, '初始化完成!'))}")
    printbar("═")
    printout("")
    printout(f"  {_c(_DIM, '下一步:')}")
    printout(f"    {_c(_CYAN, _prog() + ' backup --dry-run')}   先试试看（不复制文件）")
    printout(f"    {_c(_CYAN, _prog() + ' backup --full')}      跑一次全量备份")
    printout(f"    {_c(_CYAN, _prog() + ' service on')}         设成开机自动备份，装完就不用管了")
    printout("")


# ─────────────────────────────────────────────
# CLI 入口
# ─────────────────────────────────────────────

def _prog() -> str:
    if _is_frozen():
        return "adoback"
    return os.path.basename(sys.argv[0])


def build_parser() -> argparse.ArgumentParser:
    prog = _prog()
    parser = argparse.ArgumentParser(
        prog="adoback",
        description="Adoback — macOS 本地 Adobe 项目文件备份守护工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(f"""\
            快速开始:
              {prog} setup                一键初始化 (首次使用)
              {prog} backup               增量备份
              {prog} backup --full        全量备份
              {prog} restore              恢复备份文件
              {prog} clean --dry-run      预览可清理空间
              {prog} service on           安装并启动开机自启
              {prog} doctor               自检

            运行 '{prog} guide' 查看完整新手引导
        """),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--config", "-c", default=None, help="配置文件路径")

    sub = parser.add_subparsers(dest="command", metavar="命令")

    # ── 核心命令 ──

    # setup — 一键初始化
    sub.add_parser("setup", help="一键初始化 (安装 + 配置 + 引导)")

    # backup — 智能备份 (默认增量)
    p = sub.add_parser("backup", help="备份 (默认增量)")
    p.add_argument("--full", action="store_true", help="全量备份")
    p.add_argument("--dry-run", action="store_true", help="预演模式，不实际复制")
    p.add_argument("--json-report", action="store_true", help="导出 JSON 格式报告")

    # daemon
    sub.add_parser("daemon", help="守护模式 (前台循环增量备份)")

    # doctor
    sub.add_parser("doctor", help="系统自检")

    # guide
    p = sub.add_parser("guide", help="新手引导")
    p.add_argument("--interactive", "-i", action="store_true", help="交互式引导")

    # ── config 子命令组 ──
    config_parser = sub.add_parser("config", help="配置管理")
    config_sub = config_parser.add_subparsers(dest="config_action", metavar="操作")
    p = config_sub.add_parser("init", help="生成配置模板")
    p.add_argument("-o", "--output", help="输出路径")
    p.add_argument("--force", action="store_true", help="覆盖已有配置")
    config_sub.add_parser("show", help="显示当前生效配置")
    config_sub.add_parser("validate", help="验证配置")
    config_sub.add_parser("paths", help="显示关键路径")

    # ── service 子命令组 ──
    svc_parser = sub.add_parser("service", help="服务管理")
    svc_sub = svc_parser.add_subparsers(dest="service_action", metavar="操作")
    svc_sub.add_parser("on", help="安装并启动服务 (开机自启)")
    svc_sub.add_parser("off", help="停止并卸载服务")
    svc_sub.add_parser("status", help="查看服务状态")
    svc_sub.add_parser("restart", help="重启服务")

    # ── 查看命令 ──
    p = sub.add_parser("last-run", help="查看最近运行记录")
    p.add_argument("-n", "--count", type=int, default=1, help="显示最近 N 次记录")

    p = sub.add_parser("report", help="查看报告")
    p.add_argument("--list", dest="list_all", action="store_true", help="列出所有报告")
    p.add_argument("--format", choices=["text", "json"], default="text", help="报告格式")

    # ── 安装/卸载/更新 ──
    sub.add_parser("install", help="安装到本地 (setup 已包含此功能)")
    p = sub.add_parser("uninstall", help="卸载")
    p.add_argument("-y", "--yes", action="store_true", help="跳过确认")

    p = sub.add_parser("update", help="检查并更新到最新版本")
    p.add_argument("--check", action="store_true", help="仅检查是否有新版本，不更新")

    # restore — 恢复备份文件
    p = sub.add_parser("restore", help="从备份中恢复文件")
    p.add_argument("--search", "-s", help="搜索文件名关键词")
    p.add_argument("--list", "-l", dest="list", action="store_true", help="仅列出备份文件，不恢复")

    # clean — 备份空间清理
    p = sub.add_parser("clean", help="清理过期备份，释放磁盘空间")
    p.add_argument("--dry-run", action="store_true", help="预览将清理的文件，不实际删除")
    p.add_argument("--force", "-f", action="store_true", help="跳过确认直接清理")
    p.add_argument("--keep-last", type=int, help="保留最新 N 个快照 (覆盖配置)")
    p.add_argument("--keep-days", type=int, help="保留最近 N 天的快照 (覆盖配置)")
    p.add_argument("--max-size", type=float, help="备份总容量上限 (GB)")

    # ── 向后兼容隐藏别名 ──
    # 旧命令仍可用，但不在 help 中显示
    # 注意: argparse.SUPPRESS 在部分 Python 版本下仍会泄露
    # 因此我们用覆盖 format_help 的方式彻底隐藏
    _hidden = ("config-init", "config-show", "config-validate", "config-paths",
               "full", "incremental",
               "service-install", "service-uninstall",
               "service-start", "service-stop", "service-restart", "service-status",
               "status")
    for old_cmd in _hidden:
        sub.add_parser(old_cmd)

    # 覆盖 format_help 以隐藏旧命令
    _orig_format_help = parser.format_help
    def _clean_format_help():
        text = _orig_format_help()
        lines = text.split("\n")
        cleaned = [l for l in lines if not any(f"    {h} " in l or l.strip() == h for h in _hidden)]
        return "\n".join(cleaned)
    parser.format_help = _clean_format_help

    return parser


def _first_run_hint():
    """首次运行提示：无配置文件时引导用户 setup。"""
    printout("")
    printbox(f"Adoback v{VERSION}")
    printout("")
    printout(f"  {_c(_CYAN, '?')} 看起来你是第一次运行")
    printout("")
    printout(f"    {_c(_GREEN, _ICON_ARROW)} {_c(_BOLD, _prog() + ' setup')}     一键初始化（推荐）")
    printout(f"    {_c(_DIM, _ICON_ARROW)} {_prog()} guide     查看新手引导")
    printout(f"    {_c(_DIM, _ICON_ARROW)} {_prog()} --help    查看所有命令")
    printout("")


def main():
    parser = build_parser()
    args = parser.parse_args()

    # ── 首次运行检测 ──
    if not args.command:
        config_found = _find_config()
        if not config_found:
            _first_run_hint()
        else:
            parser.print_help()
        sys.exit(0)

    # ── 映射旧命令到新命令 ──
    cmd = args.command
    _old_cmd_map = {
        "config-init": ("config", "init"),
        "config-show": ("config", "show"),
        "config-validate": ("config", "validate"),
        "config-paths": ("config", "paths"),
        "full": ("backup", "full"),
        "incremental": ("backup", "incremental"),
        "service-install": ("service", "on"),
        "service-start": ("service", "on"),
        "service-uninstall": ("service", "off"),
        "service-stop": ("service", "off"),
        "service-restart": ("service", "restart"),
        "service-status": ("service", "status"),
        "status": ("service", "status"),
    }

    if cmd in _old_cmd_map:
        mapped = _old_cmd_map[cmd]
        cmd = mapped[0]
        if cmd == "config":
            args.config_action = mapped[1]
        elif cmd == "service":
            args.service_action = mapped[1]
        elif cmd == "backup":
            if mapped[1] == "full":
                args.full = True
            else:
                args.full = getattr(args, "full", False)
            args.dry_run = getattr(args, "dry_run", False)
            args.json_report = getattr(args, "json_report", False)

    # ── 不需要配置文件的命令 ──
    no_config_commands = {"setup", "config", "guide", "install", "update", "uninstall"}
    needs_config = cmd not in no_config_commands

    cfg = None
    if needs_config:
        config_path = Path(args.config) if args.config else None
        try:
            cfg = Config.load(config_path)
        except SystemExit:
            raise
        except Exception as e:
            printerr(f"加载配置失败: {e}")
            sys.exit(EXIT_CONFIG)
    else:
        try:
            found = _find_config()
            if args.config:
                cfg = Config.load(Path(args.config))
            elif found:
                cfg = Config.load(found)
            else:
                cfg = Config.from_defaults()
        except Exception:
            cfg = Config.from_defaults()

    exit_code = EXIT_OK

    try:
        # ── setup ──
        if cmd == "setup":
            cmd_setup(args, cfg)

        # ── backup (默认增量，--full 全量) ──
        elif cmd == "backup":
            mode = "full" if getattr(args, "full", False) else "incremental"
            issues = cfg.validate()
            if issues:
                printerr("配置验证失败:")
                for issue in issues:
                    printout(f"  ✗ {issue}")
                sys.exit(EXIT_CONFIG)

            logger = JSONLLogger(cfg.log_path) if not args.dry_run else NullLogger()

            try:
                with InstanceLock(cfg.lock_path):
                    # 备份前检查磁盘空间
                    if cfg.dest_root.is_dir():
                        notify_disk_low(cfg, cfg.dest_root)

                    result = run_backup(cfg, mode, dry_run=args.dry_run, logger=logger)
                    if not args.dry_run:
                        print_summary(result)
                        # 发送通知
                        if result.failed > 0:
                            notify_backup_failure(cfg, result)
                        elif result.copied > 0:
                            notify_backup_success(cfg, result)
                        if result.failures:
                            fmt = "json" if getattr(args, "json_report", False) else "text"
                            export_report(cfg, result, fmt=fmt)
                        if getattr(args, "json_report", False) and not result.failures:
                            export_report(cfg, result, fmt="json")
                        if result.failed > 0:
                            exit_code = EXIT_PARTIAL
            except LockError:
                printerr("另一个备份实例正在运行")
                exit_code = EXIT_LOCK

        # ── daemon ──
        elif cmd == "daemon":
            issues = cfg.validate()
            if issues:
                printerr("配置验证失败:")
                for issue in issues:
                    printout(f"  ✗ {issue}")
                sys.exit(EXIT_CONFIG)
            try:
                with InstanceLock(cfg.lock_path):
                    run_daemon(cfg)
            except LockError:
                printerr("另一个实例正在运行")
                exit_code = EXIT_LOCK

        # ── doctor ──
        elif cmd == "doctor":
            ok = run_doctor(cfg)
            exit_code = EXIT_OK if ok else EXIT_CONFIG

        # ── guide ──
        elif cmd == "guide":
            cmd_guide(args, cfg)

        # ── config ──
        elif cmd == "config":
            action = getattr(args, "config_action", None)
            if action == "init":
                cmd_config_init(args, cfg)
            elif action == "show":
                cmd_config_show(args, cfg)
            elif action == "validate":
                cmd_config_validate(args, cfg)
            elif action == "paths":
                cmd_config_paths(args, cfg)
            else:
                # 无子命令时显示 config show
                cmd_config_show(args, cfg)

        # ── service ──
        elif cmd == "service":
            action = getattr(args, "service_action", None)
            if action == "on":
                plist_path = _get_plist_path(cfg)
                if not plist_path.exists():
                    service_install(cfg)
                service_start(cfg)
            elif action == "off":
                service_stop(cfg)
                service_uninstall(cfg)
            elif action == "restart":
                service_restart(cfg)
            elif action == "status":
                service_status(cfg)
            else:
                service_status(cfg)

        # ── last-run ──
        elif cmd == "last-run":
            cmd_last_run(args, cfg)

        # ── report ──
        elif cmd == "report":
            cmd_report(args, cfg)

        # ── install / uninstall ──
        elif cmd == "install":
            cmd_install(args, cfg)

        elif cmd == "uninstall":
            cmd_uninstall(args, cfg)

        # ── restore ──
        elif cmd == "restore":
            cmd_restore(args, cfg)

        # ── clean ──
        elif cmd == "clean":
            cmd_clean(args, cfg)

        # ── update ──
        elif cmd == "update":
            cmd_update(args, cfg)

        else:
            parser.print_help()

    except KeyboardInterrupt:
        printout("")
        printinfo("用户中断")
        exit_code = EXIT_INTERRUPT
    except Exception as e:
        printerr(f"运行时错误: {e}")
        exit_code = EXIT_RUNTIME

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
