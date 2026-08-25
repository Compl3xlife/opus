from __future__ import annotations

import time
from typing import Iterable

from pynput.keyboard import Controller as KeyController
from pynput.keyboard import Key
from pynput.mouse import Button
from pynput.mouse import Controller as MouseController

from opus.tools.screen import foreground_window

_mouse = MouseController()
_keyboard = KeyController()

_BUTTONS = {
    "left": Button.left,
    "right": Button.right,
    "middle": Button.middle,
}

_SPECIAL = {
    "enter": Key.enter,
    "return": Key.enter,
    "tab": Key.tab,
    "esc": Key.esc,
    "escape": Key.esc,
    "space": Key.space,
    "backspace": Key.backspace,
    "delete": Key.delete,
    "up": Key.up,
    "down": Key.down,
    "left": Key.left,
    "right": Key.right,
    "home": Key.home,
    "end": Key.end,
    "pageup": Key.page_up,
    "pagedown": Key.page_down,
    "ctrl": Key.ctrl,
    "control": Key.ctrl,
    "alt": Key.alt,
    "shift": Key.shift,
    "win": Key.cmd,
    "cmd": Key.cmd,
    "super": Key.cmd,
    "f1": Key.f1,
    "f2": Key.f2,
    "f3": Key.f3,
    "f4": Key.f4,
    "f5": Key.f5,
    "f6": Key.f6,
    "f7": Key.f7,
    "f8": Key.f8,
    "f9": Key.f9,
    "f10": Key.f10,
    "f11": Key.f11,
    "f12": Key.f12,
}


def screen_size() -> tuple[int, int]:
    import mss

    with mss.mss() as sct:
        monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
        return int(monitor["width"]), int(monitor["height"])


def mouse_position() -> tuple[int, int]:
    pos = _mouse.position
    return int(pos[0]), int(pos[1])


def move_mouse(x: int, y: int) -> str:
    width, height = screen_size()
    x = max(0, min(width - 1, int(x)))
    y = max(0, min(height - 1, int(y)))
    _mouse.position = (x, y)
    return f"Moved mouse to {x},{y}."


def click(x: int | None = None, y: int | None = None, button: str = "left", clicks: int = 1) -> str:
    if x is not None and y is not None:
        move_mouse(x, y)
        time.sleep(0.04)
    btn = _BUTTONS.get((button or "left").lower(), Button.left)
    count = max(1, min(3, int(clicks or 1)))
    for i in range(count):
        _mouse.click(btn, 1)
        if i + 1 < count:
            time.sleep(0.06)
    pos = mouse_position()
    return f"Clicked {button} x{count} at {pos[0]},{pos[1]}."


def drag(x1: int, y1: int, x2: int, y2: int, button: str = "left", duration: float = 0.25) -> str:
    btn = _BUTTONS.get((button or "left").lower(), Button.left)
    move_mouse(x1, y1)
    time.sleep(0.05)
    _mouse.press(btn)
    steps = max(6, int(max(0.05, duration) / 0.02))
    for i in range(1, steps + 1):
        nx = int(x1 + (x2 - x1) * i / steps)
        ny = int(y1 + (y2 - y1) * i / steps)
        _mouse.position = (nx, ny)
        time.sleep(0.02)
    _mouse.release(btn)
    return f"Dragged from {x1},{y1} to {x2},{y2}."


def scroll(dx: int = 0, dy: int = 0) -> str:
    _mouse.scroll(int(dx or 0), int(dy or 0))
    return f"Scrolled dx={int(dx or 0)} dy={int(dy or 0)}."


def _resolve_key(token: str):
    name = (token or "").strip().lower()
    if not name:
        return None
    if name in _SPECIAL:
        return _SPECIAL[name]
    if len(name) == 1:
        return name
    return name


def press_keys(keys: str | Iterable[str], hold_ms: int = 40) -> str:
    if isinstance(keys, str):
        parts = [p for p in keys.replace("+", " ").replace(",", " ").split() if p]
    else:
        parts = [str(p) for p in keys if str(p).strip()]
    if not parts:
        return "No keys to press."
    resolved = []
    for part in parts:
        key = _resolve_key(part)
        if key is None:
            continue
        resolved.append(key)
    if not resolved:
        return "No valid keys."
    for key in resolved[:-1]:
        _keyboard.press(key)
    try:
        _keyboard.press(resolved[-1])
        time.sleep(max(0.01, hold_ms / 1000.0))
        _keyboard.release(resolved[-1])
    finally:
        for key in reversed(resolved[:-1]):
            try:
                _keyboard.release(key)
            except Exception:
                pass
    return f"Pressed {'+'.join(parts)}."


def type_keys(text: str) -> str:
    payload = text or ""
    if not payload:
        return "Nothing to type."
    _keyboard.type(payload)
    return f"Typed {len(payload)} characters."


def wait_ms(ms: int) -> str:
    delay = max(0, min(10_000, int(ms or 0)))
    time.sleep(delay / 1000.0)
    return f"Waited {delay}ms."


def focus_info() -> str:
    info = foreground_window()
    pos = mouse_position()
    width, height = screen_size()
    return (
        f"Foreground: {info.get('title') or 'Unknown'}. "
        f"Mouse: {pos[0]},{pos[1]}. Screen: {width}x{height}."
    )
