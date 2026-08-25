from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from opus.net_policy import guarded_httpx_get
from PIL import Image, ImageDraw, ImageFont

from opus.settings import clips_dir


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def generate_png(prompt: str) -> str:
    text = (prompt or "").strip()
    if not text:
        return "Tell me what to generate."
    dest = clips_dir() / f"opus-art-{_stamp()}.png"
    try:
        url = (
            "https://image.pollinations.ai/prompt/"
            + quote(text)
            + "?nologo=true&width=1024&height=1024&model=flux"
        )
        response = guarded_httpx_get(url, timeout=90.0, follow_redirects=True)
        response.raise_for_status()
        if len(response.content) > 8000 and response.headers.get("content-type", "").startswith("image"):
            dest.write_bytes(response.content)
            return f"Saved the image to {dest}"
    except Exception:
        pass
    _fallback_png(text, dest)
    return f"Saved the image to {dest}"


def _fallback_png(prompt: str, dest: Path) -> None:
    image = Image.new("RGB", (1024, 1024), (12, 13, 16))
    draw = ImageDraw.Draw(image)
    draw.ellipse((180, 180, 844, 844), outline=(212, 165, 116), width=8)
    try:
        font = ImageFont.truetype("segoeui.ttf", 42)
    except OSError:
        font = ImageFont.load_default()
    wrapped = prompt[:240]
    draw.multiline_text((80, 430), wrapped, fill=(236, 231, 223), font=font, align="center", spacing=8)
    image.save(dest)
