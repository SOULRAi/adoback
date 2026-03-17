#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Adoback — macOS 本地 Adobe 项目文件备份守护工具
v0.3.0

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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ─────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────

VERSION = "0.3.0"
APP_NAME = "adoback"
SERVICE_LABEL = "com.local.adoback"

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
}

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
        "root": "",
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
    def source_root(self) -> Path:
        r = self.get("source", "root", default="")
        return Path(r).expanduser().resolve() if r else Path()

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
    def service_label(self) -> str:
        return self.get("service", "label", default=SERVICE_LABEL)

    @property
    def data(self) -> dict:
        return self._data

    def validate(self) -> list[str]:
        """验证配置，返回问题列表。"""
        issues = []
        src = self.get("source", "root", default="")
        dst = self.get("destination", "root", default="")
        if not src:
            issues.append("source.root 未设置")
        elif not Path(src).expanduser().exists():
            issues.append(f"source.root 目录不存在: {src}")
        if not dst:
            issues.append("destination.root 未设置")
        else:
            dp = Path(dst).expanduser().resolve()
            sp = Path(src).expanduser().resolve() if src else Path()
            if src and dp == sp:
                issues.append("destination.root 不能与 source.root 相同")
            if src and str(dp).startswith(str(sp) + "/"):
                issues.append("destination.root 不能位于 source.root 内部")
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


# ─────────────────────────────────────────────
# 输出工具（带终端颜色）
# ─────────────────────────────────────────────

_BOLD = "\033[1m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_CYAN = "\033[36m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def _use_color() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"{code}{text}{_RESET}" if _use_color() else text


def printout(msg: str = ""):
    print(msg)


def printerr(msg: str):
    print(f"  {_c(_RED, '✗')} {msg}", file=sys.stderr)


def printwarn(msg: str):
    print(f"  {_c(_YELLOW, '⚠')} {msg}", file=sys.stderr)


def printinfo(msg: str):
    print(f"  {_c(_GREEN, '✓')} {msg}")


def printdim(msg: str):
    print(f"  {_c(_DIM, msg)}")


def printstep(step: int, msg: str):
    print(f"\n  {_c(_CYAN, f'[{step}]')} {_c(_BOLD, msg)}")


def printtitle(msg: str):
    print(f"\n{_c(_BOLD, msg)}")


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
    """扫描源目录，返回候选文件列表。"""
    root = cfg.source_root
    if not root.is_dir():
        printerr(f"源目录不存在: {root}")
        return []

    include = cfg.include_exts
    exclude = cfg.exclude_patterns
    exclude_dirs = cfg.exclude_dirs
    items = []

    for dirpath, dirnames, filenames in os.walk(root):
        # 剪枝排除目录
        dirnames[:] = [
            d for d in dirnames
            if d not in exclude_dirs and not d.startswith(".")
            or d in (".git",)  # .git 已在 exclude_dirs 默认值中
        ]
        # 重新过滤：只排除 exclude_dirs 中的
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs]

        for fname in filenames:
            # 检查扩展名
            _, ext = os.path.splitext(fname)
            if ext.lower() not in include:
                continue
            # 检查排除模式
            if any(_match_glob(fname, pat) for pat in exclude):
                continue

            full = Path(dirpath) / fname
            try:
                st = full.stat()
            except (OSError, PermissionError):
                continue
            rel = str(full.relative_to(root))
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

    printtitle("运行摘要")
    status_str = _status_zh(result.status)
    if result.status == "success":
        status_str = _c(_GREEN, status_str)
    elif result.status == "partial":
        status_str = _c(_YELLOW, status_str)
    elif result.status == "failed":
        status_str = _c(_RED, status_str)

    printout(f"  状态:     {status_str}")
    printout(f"  总文件:   {result.total}")
    printout(f"  已复制:   {_c(_GREEN, str(result.copied))}")
    printout(f"  已跳过:   {result.skipped}")
    if result.failed > 0:
        printout(f"  失败:     {_c(_RED, str(result.failed))}")
    else:
        printout(f"  失败:     0")
    printout(f"  数据量:   {mb:.1f} MB")
    printout(f"  耗时:     {dur:.1f} 秒")
    if dur > 0:
        printout(f"  吞吐:     {throughput:.1f} MB/s")

    if result.failures:
        printout("")
        printtitle("失败文件")
        for rel, err in result.failures:
            printerr(f"{rel}: {err}")


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
            result = run_backup(cfg, "incremental", logger=logger)
            if result.copied > 0 or result.failed > 0:
                print_summary(result)
                if result.failures:
                    export_report(cfg, result)
        except Exception as e:
            logger.error("daemon_error", error=str(e))
            printerr(f"备份执行异常: {e}")

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
    printtitle("Adoback 自检")
    ok = True

    # 1. 系统
    printtitle("[系统环境]")
    printout(f"  macOS:    {platform.mac_ver()[0] or '未知'}")
    if _is_frozen():
        printout(f"  运行模式: 独立二进制")
    else:
        printout(f"  Python:   {platform.python_version()}")
    printout(f"  用户:     {os.environ.get('USER', '未知')}")
    printout(f"  版本:     {VERSION}")

    if platform.system() != "Darwin":
        printwarn("当前系统非 macOS，部分功能可能不可用")
        ok = False
    else:
        printinfo("macOS 环境正常")

    # 2. 配置
    printtitle("[配置检查]")
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
    printtitle("[路径检查]")
    src = cfg.source_root
    if src and src != Path():
        if src.is_dir():
            printinfo(f"源目录可访问: {src}")
            if os.access(src, os.R_OK):
                printinfo("源目录可读")
                # 扫描文件数
                items = scan_files(cfg)
                if items:
                    total_size = sum(i.size for i in items)
                    total_mb = total_size / (1024 * 1024)
                    printinfo(f"发现 {len(items)} 个候选文件 ({total_mb:.1f} MB)")
                else:
                    printwarn("未发现符合条件的文件")
            else:
                printerr("源目录不可读")
                ok = False
        else:
            printerr(f"源目录不存在: {src}")
            ok = False

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
    printtitle("[运行状态]")
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

    # 6. 服务
    printtitle("[服务状态]")
    plist = _get_plist_path(cfg)
    if plist.exists():
        printinfo(f"服务已安装: {plist}")
    else:
        printdim("服务尚未安装")

    # 结论
    printout("")
    if ok:
        printinfo(_c(_GREEN, "自检通过 — 可以开始使用"))
    else:
        printerr("自检发现问题，请根据上方提示修复")

    return ok


# ─────────────────────────────────────────────
# 配置命令
# ─────────────────────────────────────────────

CONFIG_TEMPLATE = """\
# Adoback 配置文件
# 文档: https://github.com/user/adoback

[general]
language = "zh"

[source]
# 必填: 你的 Adobe 项目文件目录
# 示例: root = "/Users/你的用户名/Documents/Adobe"
root = ""

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
    printtitle("关键路径")
    paths = [
        ("配置文件", Path(args.config) if args.config else getattr(cfg, '_config_path', DEFAULT_CONFIG_PATH)),
        ("源目录", cfg.source_root),
        ("目标目录", cfg.dest_root),
        ("状态目录", cfg.state_dir),
        ("日志目录", cfg.log_dir),
        ("报告目录", cfg.report_dir),
        ("锁文件", cfg.lock_path),
        ("数据库", cfg.manifest_path),
        ("JSONL日志", cfg.log_path),
        ("服务plist", _get_plist_path(cfg)),
    ]
    for name, p in paths:
        p = Path(p)
        exists_mark = _c(_GREEN, "✓") if p.exists() else _c(_DIM, "✗")
        printout(f"  {exists_mark} {name:10s}  {p}")


# ─────────────────────────────────────────────
# Guide 引导
# ─────────────────────────────────────────────

GUIDE_TEXT = """\
╔════════════════════════════════════════════╗
║    Adoback 新手引导             ║
╚════════════════════════════════════════════╝

Adoback 是一个 macOS 本地备份守护工具，
为你的 Adobe 项目文件提供独立于 Adobe 自身的备份保护。

支持的文件类型:
  PSD, PSB, AI, INDD, IDML, PRPROJ, AEP, XD, PDF 等

━━━ 快速开始 ━━━

  首次使用，一条命令搞定:
    {prog} setup

  也可以手动逐步来:

  1. 生成配置:    {prog} config init
  2. 编辑配置:    nano ~/.local/adoback/config.toml
  3. 自检:        {prog} doctor
  4. 试运行:      {prog} backup --dry-run
  5. 全量备份:    {prog} backup --full
  6. 开机自启:    {prog} service on

━━━ 常用命令 ━━━

  backup              增量备份 (默认)
  backup --full       全量备份
  backup --dry-run    预演模式
  daemon              守护模式 (前台运行)
  last-run            查看最近运行
  report              查看报告
  doctor              系统自检

  config show         查看配置
  config validate     验证配置
  config paths        查看路径
  config init         生成配置模板

  service on          安装并启动服务
  service off         停止并卸载服务
  service status      查看服务状态
  service restart     重启服务

━━━ 重要提醒 ━━━

• 本工具只能备份已落盘的文件
• Adobe 应用内存中尚未保存的内容无法被外部工具捕获
• 建议配合 Adobe 自身的自动保存一起使用
• 本工具是兜底系统，不是替代方案
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
    printout("╔════════════════════════════════════════════╗")
    printout("║    Adoback 交互式引导           ║")
    printout("╚════════════════════════════════════════════╝")
    printout("")

    # 1. 检查配置文件
    printstep(1, "检查配置文件")
    config_path = DEFAULT_CONFIG_PATH
    if config_path.exists():
        printout(f"    ✓ 配置文件已存在: {config_path}")
        printout(f"    是否重新生成? (y/N) ", )
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
            printout(f"    ✓ 已重新生成")
    else:
        printout(f"    配置文件不存在，正在生成...")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        content = CONFIG_TEMPLATE.format(
            state_dir=str(DEFAULT_STATE_DIR),
            log_dir=str(DEFAULT_LOG_DIR),
        )
        config_path.write_text(content, encoding="utf-8")
        printout(f"    ✓ 已生成: {config_path}")

    # 2. 设置源目录
    printout("")
    printstep(2, "设置源目录")
    printout(f"    请输入你的 Adobe 项目文件目录")
    printout(f"    示例: /Users/{os.environ.get('USER', 'you')}/Documents/Adobe")
    printout(f"    源目录: ", )
    try:
        src = input().strip()
    except (EOFError, KeyboardInterrupt):
        printout("")
        return

    # 3. 设置目标目录
    printout("")
    printstep(3, "设置目标目录")
    printout(f"    请输入备份输出目录 (不能与源目录相同)")
    printout(f"    示例: /Volumes/BackupDisk/AdobeBackup")
    printout(f"    目标目录: ", )
    try:
        dst = input().strip()
    except (EOFError, KeyboardInterrupt):
        printout("")
        return

    if src and dst:
        # 写入配置
        try:
            text = config_path.read_text(encoding="utf-8")
            # 简单替换 root = "" 行
            lines = text.split("\n")
            new_lines = []
            src_set = False
            dst_set = False
            in_source = False
            in_dest = False
            for line in lines:
                stripped = line.strip()
                if stripped == "[source]":
                    in_source = True
                    in_dest = False
                elif stripped == "[destination]":
                    in_source = False
                    in_dest = True
                elif stripped.startswith("[") and stripped.endswith("]"):
                    in_source = False
                    in_dest = False

                if in_source and stripped.startswith("root") and "=" in stripped and not src_set:
                    new_lines.append(f'root = "{src}"')
                    src_set = True
                elif in_dest and stripped.startswith("root") and "=" in stripped and not dst_set:
                    new_lines.append(f'root = "{dst}"')
                    dst_set = True
                else:
                    new_lines.append(line)
            config_path.write_text("\n".join(new_lines), encoding="utf-8")
            printout("")
            printinfo("✓ 配置已更新")
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
    printtitle("Adoback 安装")

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
    printout("Adoback 卸载")
    printout("=" * 40)

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
# setup 一键初始化
# ─────────────────────────────────────────────

def cmd_setup(args, cfg):
    """一键初始化：安装 + 生成配置 + 交互引导 + 自检。"""
    printout("")
    printtitle("╔══════════════════════════════════════════════╗")
    printtitle("║    Adoback · 一键初始化            ║")
    printtitle("╚══════════════════════════════════════════════╝")

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

    # 3. 交互设置源/目标目录
    printstep(3, "设置备份路径")
    src = ""
    dst = ""
    try:
        printout(f"    Adobe 项目文件目录 (源目录):")
        printdim(f"    示例: /Users/{os.environ.get('USER', 'you')}/Documents/Adobe")
        printout(f"    > ", )
        src = input().strip()
        printout(f"    备份输出目录 (不能与源目录相同):")
        printdim(f"    示例: /Volumes/BackupDisk/AdobeBackup")
        printout(f"    > ", )
        dst = input().strip()
    except (EOFError, KeyboardInterrupt):
        printout("")

    if src and dst:
        try:
            text = config_path.read_text(encoding="utf-8")
            lines = text.split("\n")
            new_lines = []
            src_set = dst_set = False
            in_source = in_dest = False
            for line in lines:
                stripped = line.strip()
                if stripped == "[source]":
                    in_source, in_dest = True, False
                elif stripped == "[destination]":
                    in_source, in_dest = False, True
                elif stripped.startswith("[") and stripped.endswith("]"):
                    in_source = in_dest = False

                if in_source and stripped.startswith("root") and "=" in stripped and not src_set:
                    new_lines.append(f'root = "{src}"')
                    src_set = True
                elif in_dest and stripped.startswith("root") and "=" in stripped and not dst_set:
                    new_lines.append(f'root = "{dst}"')
                    dst_set = True
                else:
                    new_lines.append(line)
            config_path.write_text("\n".join(new_lines), encoding="utf-8")
            printinfo("备份路径已写入配置")
        except Exception as e:
            printerr(f"写入配置失败: {e}")

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
    printtitle("╔══════════════════════════════════════════════╗")
    printtitle(f"║  {_c(_GREEN, '✓ 初始化完成!')}                                   ║")
    printtitle("╚══════════════════════════════════════════════╝")
    printout("")
    printout("  下一步:")
    printout(f"    {_prog()} backup --dry-run   试运行")
    printout(f"    {_prog()} backup --full      全量备份")
    printout(f"    {_prog()} service on         安装并启动开机自启服务")
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
              {prog} service on           安装并启动开机自启
              {prog} doctor               自检
              {prog} config show          查看配置

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

    # ── 安装/卸载 ──
    sub.add_parser("install", help="安装到本地 (setup 已包含此功能)")
    p = sub.add_parser("uninstall", help="卸载")
    p.add_argument("-y", "--yes", action="store_true", help="跳过确认")

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
    printtitle("Adoback")
    printout(f"  版本 {VERSION}")
    printout("")
    printout("  看起来你是第一次运行，建议执行:")
    printout(f"    {_c(_CYAN, _prog() + ' setup')}    一键初始化")
    printout("")
    printout("  或者查看帮助:")
    printout(f"    {_prog()} --help")
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
    no_config_commands = {"setup", "config", "guide", "install"}
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
                    result = run_backup(cfg, mode, dry_run=args.dry_run, logger=logger)
                    if not args.dry_run:
                        print_summary(result)
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
