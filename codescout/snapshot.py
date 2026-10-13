"""Capture stable, bounded text content without changing the source project."""

import fnmatch
import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DIRS = {".git", ".hg", ".svn", "node_modules", "vendor", ".venv", "venv", "__pycache__", "dist", "build", ".next", ".codescout", ".superpowers"}
SECRET_NAMES = {".env", "id_rsa", "id_ed25519", "credentials", "credentials.json", ".npmrc", ".pypirc", "secrets.json"}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".db", ".sqlite", ".sqlite3"}
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_FILES = 2000


def _is_link(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _handle_path(file, fallback):
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
        api.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        api.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        length = api(msvcrt.get_osfhandle(file.fileno()), buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise ValueError("无法核对文件实际路径")
        name = buffer.value
        if name.startswith("\\\\?\\UNC\\"):
            name = "\\\\" + name[8:]
        elif name.startswith("\\\\?\\"):
            name = name[4:]
        return Path(name)
    proc = Path("/proc/self/fd") / str(file.fileno())
    return Path(os.readlink(proc)) if proc.exists() else fallback.resolve()


def read_file(root: Path, path: Path) -> bytes:
    """Check the opened handle before reading, and impose a hard byte bound."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        with os.fdopen(os.open(path, flags), "rb") as file:
            before = os.fstat(file.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
                raise ValueError("文件类型或大小不在检查范围内")
            _handle_path(file, path).relative_to(root)
            data = file.read(MAX_FILE_BYTES + 1)
            after = os.fstat(file.fileno())
            current = path.stat()
            if len(data) > MAX_FILE_BYTES or (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size) or (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino):
                raise ValueError("项目读取期间继续变化，稍后重试")
            return data
    except OSError as error:
        raise ValueError("无法安全读取项目文件") from error


@dataclass
class Snapshot:
    files: dict
    excluded: dict
    warnings: list
    digest: str
    git: dict | None


def _git(root, *args):
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=10, check=False)
        return result.stdout.decode("utf-8", errors="replace").strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _reason(name, excludes):
    path = Path(name)
    if any(part in DEFAULT_DIRS for part in path.parts):
        return "依赖、构建产物或内部目录"
    if any(part.lower() in {".aws", ".ssh"} for part in path.parts) or path.name.lower() in SECRET_NAMES or path.name.lower().startswith(".env.") or path.suffix.lower() in SECRET_SUFFIXES or path.name.lower().endswith((".db-wal", ".db-shm")):
        return "凭据或本机数据文件"
    for pattern in excludes:
        prefix = pattern.rstrip("/")
        if fnmatch.fnmatchcase(name, pattern) or name == prefix or name.startswith(prefix + "/"):
            return "用户排除"
    return None


def _read_once(root, excludes):
    files, excluded, warnings = {}, {}, []
    candidates = []
    def walk_error(error):
        raise ValueError("无法完整读取项目目录，请检查访问权限") from error
    for directory, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
        dirs.sort()
        for dirname in list(dirs):
            entry = Path(directory) / dirname
            name = entry.relative_to(root).as_posix()
            reason = _reason(name, excludes)
            if _is_link(entry):
                reason = "链接目录"
            if reason:
                dirs.remove(dirname)
                excluded[name + "/"] = reason
        for filename in sorted(names):
            candidates.append(Path(directory) / filename)
    # Use Git's own ignore semantics when available, while retaining untracked files.
    git_root = _git(root, "rev-parse", "--show-toplevel")
    allowed = None
    if git_root and Path(git_root).resolve() == root:
        output = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
        if output is not None:
            allowed = set(output.split("\0"))
    total = 0
    for path in candidates:
        name = path.relative_to(root).as_posix()
        reason = _reason(name, excludes)
        if reason:
            excluded[name] = reason
            continue
        if allowed is not None and name not in allowed:
            excluded[name] = "Git 忽略"
            continue
        if _is_link(path):
            excluded[name] = "链接文件"
            continue
        try:
            # Resolve before opening so links cannot intentionally cross the root.
            path.resolve().relative_to(root)
            before = path.stat()
            if before.st_size > MAX_FILE_BYTES:
                excluded[name] = "文件超过 256 KiB"
                warnings.append(name + "：文件超限，未检查")
                continue
            if len(files) >= MAX_FILES or total + before.st_size > MAX_TOTAL_BYTES:
                excluded[name] = "快照容量上限"
                warnings.append(name + "：快照容量超限，未检查")
                continue
            data = read_file(root, path)
            after = path.stat()
            if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                raise ValueError("项目读取期间继续变化，稍后重试")
            if b"\x00" in data:
                excluded[name] = "二进制文件"
                continue
            try:
                text = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
            except UnicodeDecodeError:
                excluded[name] = "非 UTF-8 文本"
                warnings.append(name + "：编码不支持，未检查")
                continue
            files[name] = text
            total += len(data)
        except OSError as error:
            raise ValueError("无法读取项目文件，请检查权限或稍后重试") from error
    git = None
    if git_root:
        git = {"branch": _git(root, "branch", "--show-current") or "detached", "head": _git(root, "rev-parse", "HEAD")}
    return files, excluded, warnings, git


def capture(root: Path, excludes=None) -> Snapshot:
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("项目文件夹不存在或不可访问")
    previous = None
    for _ in range(3):
        current = _read_once(root, excludes or [])
        payload = json.dumps(current, sort_keys=True, ensure_ascii=False).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        if digest == previous:
            return Snapshot(*current[:3], digest, current[3])
        previous = digest
    raise ValueError("项目读取期间继续变化，稍后重试")
