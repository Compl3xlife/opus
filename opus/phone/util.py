from __future__ import annotations

import socket
from pathlib import Path

from PIL import Image

from opus.ui.icons import draw_icon

UI_DIR = Path(__file__).resolve().parent / "ui"


def ensure_phone_icons() -> Path:
    static = UI_DIR / "static"
    static.mkdir(parents=True, exist_ok=True)
    master = draw_icon(512)
    icon = static / "icon.png"
    master.save(icon)
    master.resize((192, 192), Image.Resampling.LANCZOS).save(static / "icon-192.png")
    master.resize((512, 512), Image.Resampling.LANCZOS).save(static / "icon-512.png")
    return icon


def lan_addresses() -> list[str]:
    hosts: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            hosts.append(sock.getsockname()[0])
    except OSError:
        pass
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            addr = info[4][0]
            if addr not in hosts and not addr.startswith("127."):
                hosts.append(addr)
    except OSError:
        pass
    return hosts


def public_host_from_request(request) -> str:
    forwarded = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    raw = forwarded or (request.headers.get("host") or "")
    host = raw.split(":")[0].strip()
    if not host or host in {"0.0.0.0", "::", "[::]"}:
        return "127.0.0.1"
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host
