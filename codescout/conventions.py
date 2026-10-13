"""Explicit project conventions; reads never create or migrate files."""

import os
import stat
import tempfile
import threading
from pathlib import Path

from .snapshot import _handle_path


RULES_FILENAME = "CODESCOUT_RULES.md"
MAX_RULES_CHARS = 20000
_save_lock = threading.RLock()


def _is_link(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _rules_path(root: Path) -> Path:
    root = Path(root)
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("约定文件必须保存在可访问的绝对项目目录内")
    if any(_is_link(component) for component in (root, *root.parents)):
        raise ValueError("项目目录或约定文件不能使用符号链接或目录链接")
    resolved = root.resolve(strict=True)
    if os.path.normcase(str(root.absolute())) != os.path.normcase(str(resolved)):
        raise ValueError("约定文件路径不能越出项目目录")
    if Path(RULES_FILENAME).name != RULES_FILENAME or Path(RULES_FILENAME).is_absolute():
        raise ValueError("约定文件路径不能越出项目目录")
    path = resolved / RULES_FILENAME
    if _is_link(path):
        raise ValueError("约定文件不能使用符号链接或目录链接")
    if path.resolve(strict=False).parent != resolved or (path.exists() and not path.is_file()):
        raise ValueError("约定文件必须是项目目录内的普通文件")
    return path


def _read_source(path: Path) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        actual = _handle_path(source, path)
        if not stat.S_ISREG(before.st_mode) or actual != path or _is_link(path):
            raise ValueError("约定文件必须是项目目录内的普通文件，不能使用链接或越界路径")
        data = source.read(MAX_RULES_CHARS * 4 + 1)
        after = os.fstat(source.fileno())
        current = path.stat()
        if (_is_link(path) or (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size)
                or (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino)):
            raise ValueError("约定文件在读取时已被修改，请重新读取后再保存")
    _decode(data)
    return data


def _decode(data: bytes) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{RULES_FILENAME} 必须使用 UTF-8 编码，原文件已保留") from exc
    if len(text) > MAX_RULES_CHARS:
        raise ValueError(f"约定文件不能超过 {MAX_RULES_CHARS} 个字符")
    return text


def load_rules(root: Path) -> str:
    """Read UTF-8 Markdown verbatim, including source CRLF for save checks."""
    with _save_lock:
        try:
            data = _read_source(_rules_path(root))
            return "" if data is None else _decode(data)
        except OSError as exc:
            raise ValueError(f"无法读取项目约定文件：{exc}") from exc


def save_rules(root: Path, text: str, expected: str | None = None) -> None:
    """Atomically save an explicit edit, rejecting stale or unknown source."""
    if not isinstance(text, str) or len(text) > MAX_RULES_CHARS:
        raise ValueError(f"附加约定必须是最多 {MAX_RULES_CHARS} 个字符的文本")
    if expected is not None and not isinstance(expected, str):
        raise ValueError("约定原文必须是文本")
    with _save_lock:
        temporary = None
        try:
            path = _rules_path(root)
            original = _read_source(path)
            if expected is None:
                if original is not None:
                    if original == text.encode("utf-8"):
                        return
                    raise ValueError("约定文件已存在，请先读取原文再保存；原文件已保留")
            elif (original or b"") != expected.encode("utf-8"):
                raise ValueError("约定文件已被修改，请重新读取后再保存；当前文件已保留")

            # Browser textareas normalize CRLF. Preserve the source's Windows style.
            if original and b"\r\n" in original and b"\n" not in original.replace(b"\r\n", b""):
                text = text.replace("\r\n", "\n").replace("\n", "\r\n")
            if len(text) > MAX_RULES_CHARS:
                raise ValueError(f"保留原文件换行格式后，附加约定不能超过 {MAX_RULES_CHARS} 个字符")
            replacement = text.encode("utf-8")
            if original == replacement:
                return
            descriptor, name = tempfile.mkstemp(prefix=".codescout-rules-", suffix=".tmp", dir=path.parent)
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(replacement)
                destination.flush()
                os.fsync(destination.fileno())

            # Check again after preparing the replacement, including filesystem links.
            if _rules_path(root) != path or _read_source(path) != original:
                raise ValueError("约定文件在保存时已被修改，请重新读取后再保存；当前文件已保留")
            if original is None:
                # The first save must fail if an unrelated file appeared, even
                # in the interval after the final content check. Windows rename
                # is exclusive; POSIX link provides the same atomic creation.
                if os.name == "nt":
                    os.rename(temporary, path)
                else:
                    os.link(temporary, path)
                    temporary.unlink()
            else:
                os.replace(temporary, path)
            temporary = None
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"无法保存项目约定文件，原文件已保留：{exc}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
