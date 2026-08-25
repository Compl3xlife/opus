from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


def draw_icon(size: int, active: bool = True) -> Image.Image:
    """Royal crown icon. Green active, red asleep, black outline."""
    work = max(256, size)
    img = Image.new("RGBA", (work, work), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if active:
        fill = (80, 220, 80)
        outline = (0, 0, 0)
    else:
        fill = (180, 20, 20)
        outline = (0, 0, 0)

    def p(xf, yf):
        m = work * 0.06
        return (int(m + xf * (work - 2 * m)), int(m + yf * (work - 2 * m)))

    lw = max(2, work // 100)

    # Crown - matching reference image proportions
    # Wider aspect, shorter height, elegant curves
    crown = [
        # Bottom left of crown body
        p(0.08, 0.78),
        # Left side
        p(0.05, 0.65),
        p(0.03, 0.50),
        # Left tip (bulb)
        p(0.05, 0.40),
        p(0.08, 0.35),
        p(0.12, 0.33),
        p(0.15, 0.35),
        p(0.14, 0.39),
        # Valley
        p(0.20, 0.55),
        p(0.25, 0.60),
        # Second tip
        p(0.30, 0.38),
        p(0.33, 0.28),
        p(0.35, 0.24),
        p(0.37, 0.22),
        p(0.39, 0.24),
        p(0.38, 0.28),
        # Valley
        p(0.42, 0.48),
        p(0.46, 0.52),
        # Center tip (tallest)
        p(0.48, 0.30),
        p(0.49, 0.18),
        p(0.50, 0.12),
        p(0.51, 0.18),
        p(0.52, 0.30),
        # Valley
        p(0.54, 0.52),
        p(0.58, 0.48),
        # Fourth tip
        p(0.62, 0.28),
        p(0.61, 0.24),
        p(0.63, 0.22),
        p(0.65, 0.24),
        p(0.67, 0.28),
        p(0.70, 0.38),
        # Valley
        p(0.75, 0.60),
        p(0.80, 0.55),
        # Right tip (bulb)
        p(0.86, 0.39),
        p(0.85, 0.35),
        p(0.88, 0.33),
        p(0.92, 0.35),
        p(0.95, 0.40),
        # Right side
        p(0.97, 0.50),
        p(0.95, 0.65),
        p(0.92, 0.78),
    ]
    draw.polygon(crown, fill=fill, outline=outline)
    for i in range(len(crown) - 1):
        draw.line([crown[i], crown[i + 1]], fill=outline, width=lw)
    draw.line([crown[-1], crown[0]], fill=outline, width=lw)

    # Band at bottom - thin elegant oval
    band = [
        p(0.08, 0.78),
        p(0.15, 0.76),
        p(0.30, 0.74),
        p(0.50, 0.73),
        p(0.70, 0.74),
        p(0.85, 0.76),
        p(0.92, 0.78),
        p(0.92, 0.88),
        p(0.85, 0.91),
        p(0.70, 0.93),
        p(0.50, 0.94),
        p(0.30, 0.93),
        p(0.15, 0.91),
        p(0.08, 0.88),
    ]
    draw.polygon(band, fill=fill, outline=outline)
    for i in range(len(band) - 1):
        draw.line([band[i], band[i + 1]], fill=outline, width=lw)
    draw.line([band[-1], band[0]], fill=outline, width=lw)

    # Thin line across band middle
    draw.line([p(0.12, 0.84), p(0.88, 0.84)], fill=outline, width=max(1, lw - 1))

    if size != work:
        img = img.resize((size, size), Image.Resampling.LANCZOS)
    return img


def icon_path(active: bool = True) -> Path:
    ui = Path(__file__).resolve().parent
    return ui / ("icon.ico" if active else "icon_sleep.ico")


def load_icon_image(active: bool = True, size: int = 256, opaque: bool = False) -> Image.Image:
    path = icon_path(active)
    if path.exists():
        img = Image.open(path)
        try:
            img.load()
        except Exception:
            pass
        if img.size[0] != size:
            img = img.resize((size, size), Image.Resampling.LANCZOS)
        img = img.convert("RGBA")
    else:
        img = draw_icon(size, active=active)
    if opaque:
        bg = Image.new("RGBA", img.size, (24, 24, 24, 255))
        bg.alpha_composite(img)
        return bg.convert("RGB")
    return img


def ensure_icons() -> Path:
    ui = Path(__file__).resolve().parent
    master = draw_icon(512)
    icon = ui / "icon.png"
    master.save(icon)

    sleep_icon = ui / "icon_sleep.png"
    draw_icon(512, active=False).save(sleep_icon)

    ico = ui / "icon.ico"
    sizes = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]
    master.resize((256, 256), Image.Resampling.LANCZOS).save(ico, format="ICO", sizes=sizes)

    sleep_ico = ui / "icon_sleep.ico"
    draw_icon(256, active=False).save(sleep_ico, format="ICO", sizes=sizes)

    root_ico = ui.parent.parent / "opus.ico"
    master.resize((256, 256), Image.Resampling.LANCZOS).save(root_ico, format="ICO", sizes=sizes)

    ext_root = ui.parent.parent / "extensions"
    for folder in (ext_root / "chromium" / "icons", ext_root / "firefox" / "icons"):
        folder.mkdir(parents=True, exist_ok=True)
        for size in (16, 32, 48, 128):
            master.resize((size, size), Image.Resampling.LANCZOS).save(folder / f"icon{size}.png")
    return icon
