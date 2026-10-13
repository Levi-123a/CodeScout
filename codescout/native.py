"""The local folder picker owns its Tk lifetime and serializes OS dialogs."""

import threading
from pathlib import Path


_dialog_lock = threading.Lock()


def _show_folder_dialog() -> str:
    # Tk's Windows askdirectory implementation opens the native OS folder chooser.
    # Import lazily: a missing GUI runtime must become a clear API error.
    import tkinter
    from tkinter import filedialog

    window = tkinter.Tk()
    try:
        window.withdraw()
        window.attributes("-topmost", True)
        return filedialog.askdirectory(parent=window, title="选择要接入巡码的项目文件夹", mustexist=True)
    finally:
        window.destroy()


def choose_folder() -> str | None:
    """Return the selected absolute directory, or None when cancelled."""
    with _dialog_lock:
        try:
            selected = _show_folder_dialog()
        except Exception as exc:
            raise ValueError(f"无法打开本机文件夹选择器，请检查 Windows 桌面及 Python Tk 支持：{exc}") from exc
        if not selected:
            return None
        try:
            root = Path(selected)
            if not root.is_absolute() or not root.is_dir():
                raise ValueError("所选路径必须是可访问的绝对文件夹路径")
            return str(root.resolve(strict=True))
        except OSError as exc:
            raise ValueError(f"无法读取所选文件夹：{exc}") from exc
