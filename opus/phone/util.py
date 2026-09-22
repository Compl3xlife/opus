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
    return [ip for _kind, ip in nic_ipv4s()]


def nic_ipv4s() -> list[tuple[str, str]]:
    """Return (kind, ipv4) for this PC. kind is usb, wifi, or lan."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        import psutil

        for name, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if getattr(addr, "family", None) != socket.AF_INET:
                    continue
                ip = addr.address or ""
                if not ip or ip.startswith("127.") or ip in seen:
                    continue
                seen.add(ip)
                found.append((_nic_kind(name, ip), ip))
    except Exception:
        pass
    if found:
        return found
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                found.append((_nic_kind("", ip), ip))
    except OSError:
        pass
    return found


def _nic_kind(name: str, ip: str) -> str:
    lowered = f"{name} {ip}".lower()
    if ip.startswith("172.20.10.") or "apple" in lowered or "iphone" in lowered or "mobile" in lowered:
        return "usb"
    if "wi-fi" in lowered or "wifi" in lowered or "wlan" in lowered:
        return "wifi"
    return "lan"


def public_host_from_request(request) -> str:
    forwarded = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    raw = forwarded or (request.headers.get("host") or "")
    host = raw.split(":")[0].strip()
    if not host or host in {"0.0.0.0", "::", "[::]"}:
        return "127.0.0.1"
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host
