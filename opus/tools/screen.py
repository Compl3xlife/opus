from __future__ import annotations

import base64
import ctypes
import io

import mss
from PIL import Image


user32 = ctypes.windll.user32


def foreground_window() -> dict:
    hwnd = user32.GetForegroundWindow()
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return {"title": buffer.value, "hwnd": int(hwnd)}


def screenshot_png(max_width: int | None = 1280) -> bytes:
    png, _meta = capture_screen(max_width=max_width)
    return png


def capture_screen(max_width: int | None = 1280) -> tuple[bytes, dict]:
    """Return PNG bytes plus scale metadata for mapping image coords → screen."""
    with mss.mss() as sct:
        monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
        raw = sct.grab(monitor)
        image = Image.frombytes("RGB", raw.size, raw.rgb)
    native_w, native_h = image.width, image.height
    scale = 1.0
    if max_width and image.width > max_width:
        scale = max_width / image.width
        image = image.resize((max_width, int(image.height * scale)))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    meta = {
        "native_width": native_w,
        "native_height": native_h,
        "image_width": image.width,
        "image_height": image.height,
        "scale": scale,
        "monitor_left": int(monitor.get("left", 0)),
        "monitor_top": int(monitor.get("top", 0)),
    }
    return buffer.getvalue(), meta


def map_image_to_screen(x: float, y: float, meta: dict) -> tuple[int, int]:
    scale = float(meta.get("scale") or 1.0) or 1.0
    left = int(meta.get("monitor_left") or 0)
    top = int(meta.get("monitor_top") or 0)
    sx = int(round(x / scale)) + left
    sy = int(round(y / scale)) + top
    return sx, sy


def screenshot_data_url() -> str:
    return "data:image/png;base64," + base64.b64encode(screenshot_png()).decode("ascii")
