from __future__ import annotations

import ctypes
import os
import re
import subprocess
import time

from pynput.keyboard import Controller, Key

from opus.tools.screen import foreground_window

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
CREATE_NO_WINDOW = 0x08000000

TARGET_RE = re.compile(
    r"\s+(in|into|on)\s+(notes|note|notepad|sticky notes|this|that|here)\b.*$",
    re.IGNORECASE,
)


def _set_clipboard(text: str) -> None:
    data = text.encode("utf-16-le") + b"\x00\x00"
    if not user32.OpenClipboard(None):
        raise OSError("Could not open the clipboard.")
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise OSError("Could not allocate clipboard memory.")
        locked = kernel32.GlobalLock(handle)
        if not locked:
            kernel32.GlobalFree(handle)
            raise OSError("Could not lock clipboard memory.")
        try:
            ctypes.memmove(locked, data, len(data))
        finally:
            kernel32.GlobalUnlock(handle)
        user32.SetClipboardData(CF_UNICODETEXT, handle)
    finally:
        user32.CloseClipboard()


def _open_notes() -> None:
    sticky = os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WindowsApps\Microsoft.MicrosoftStickyNotes_8wekyb3d8bbwe!App"
    )
    for cmd in (
        ["cmd", "/c", "start", "", "notepad"],
        ["explorer.exe", "shell:AppsFolder\\Microsoft.MicrosoftStickyNotes_8wekyb3d8bbwe!App"],
    ):
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
            time.sleep(0.8)
            return
        except OSError:
            continue
    _ = sticky


def _paste(payload: str) -> None:
    try:
        _set_clipboard(payload)
        keyboard = Controller()
        time.sleep(0.12)
        with keyboard.pressed(Key.ctrl):
            keyboard.tap("v")
    except Exception:
        keyboard = Controller()
        keyboard.type(payload)


def type_text(text: str) -> str:
    raw = (text or "").replace("\r\n", "\n")
    if not raw.strip():
        return "Nothing to type."
    target = TARGET_RE.search(raw)
    payload = TARGET_RE.sub("", raw).strip() if target else raw.strip()
    if target and (target.group(2) or "").lower() in {"notes", "note", "notepad", "sticky notes"}:
        _open_notes()
    else:
        time.sleep(0.25)
    window = foreground_window()
    title = window.get("title") or "the focused window"
    try:
        _paste(payload)
    except Exception:
        return "I couldn't type there. Click the box and say it again."
    return "Done."


def type_login(email: str, password: str) -> str:
    time.sleep(0.3)
    _paste(email)
    keyboard = Controller()
    time.sleep(0.12)
    keyboard.tap(Key.tab)
    time.sleep(0.12)
    _paste(password)
    return "Done."
