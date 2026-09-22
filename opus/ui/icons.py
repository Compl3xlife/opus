from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import Image

_UI = Path(__file__).resolve().parent
_SOURCE = _UI / "crown.png"


@lru_cache(maxsize=1)
def _master() -> Image.Image:
    path = _SOURCE if _SOURCE.exists() else _UI / "icon.png"
    img = Image.open(path).convert("RGBA")
    return img


def _asleep(img: Image.Image) -> Image.Image:
    img = img.copy()
    px = img.load()
    width, height = img.size
    for y in range(height):
        for x in range(width):
            red, green, blue, alpha = px[x, y]
            if alpha < 8:
                continue
            if green >= red + 12 and green >= blue:
                px[x, y] = (
                    min(255, int(green * 1.05)),
                    min(255, int(red * 0.22 + 18)),
                    min(255, int(blue * 0.28 + 12)),
                    alpha,
                )
    return img


def draw_icon(size: int, active: bool = True) -> Image.Image:
    img = _master().copy() if active else _asleep(_master())
    size = max(16, int(size))
    if img.size != (size, size):
        img = img.resize((size, size), Image.Resampling.LANCZOS)
    return img


def icon_path(active: bool = True) -> Path:
    return _UI / ("icon.ico" if active else "icon_sleep.ico")


def load_icon_image(active: bool = True, size: int = 256, opaque: bool = False) -> Image.Image:
    path = icon_path(active)
    if path.exists():
        img = Image.open(path)
        try:
            img.load()
        except Exception:
            pass
        img = img.convert("RGBA")
        if img.size[0] != size:
            img = img.resize((size, size), Image.Resampling.LANCZOS)
    else:
        img = draw_icon(size, active=active)
    if opaque:
        bg = Image.new("RGBA", img.size, (24, 24, 24, 255))
        bg.alpha_composite(img)
        return bg.convert("RGB")
    return img


def ensure_icons() -> Path:
    master = draw_icon(512)
    icon = _UI / "icon.png"
    master.save(icon)

    sleep_icon = _UI / "icon_sleep.png"
    draw_icon(512, active=False).save(sleep_icon)

    sizes = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]
    ico = _UI / "icon.ico"
    master.resize((256, 256), Image.Resampling.LANCZOS).save(ico, format="ICO", sizes=sizes)

    sleep_ico = _UI / "icon_sleep.ico"
    draw_icon(256, active=False).save(sleep_ico, format="ICO", sizes=sizes)

    root_ico = _UI.parent.parent / "opus.ico"
    master.resize((256, 256), Image.Resampling.LANCZOS).save(root_ico, format="ICO", sizes=sizes)

    ext_root = _UI.parent.parent / "extensions"
    for folder in (ext_root / "chromium" / "icons", ext_root / "firefox" / "icons"):
        folder.mkdir(parents=True, exist_ok=True)
        for size in (16, 32, 48, 128):
            master.resize((size, size), Image.Resampling.LANCZOS).save(folder / f"icon{size}.png")
    return icon
