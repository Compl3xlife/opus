from __future__ import annotations

import os
from pathlib import Path

from opus.logutil import get_logger

log = get_logger()


def extension_root() -> Path:
    return (Path(__file__).resolve().parent.parent.parent / "extensions" / "chromium").resolve()


def browser_executable(browser_id: str = "opera-gx") -> Path | None:
    bid = (browser_id or "").strip().lower()
    if bid == "opera":
        bid = "opera-gx"
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    program_files_x86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    candidates: dict[str, list[Path]] = {
        "opera-gx": [
            local / "Programs/Opera GX/opera.exe",
            program_files / "Opera GX/opera.exe",
        ],
        "opera": [
            local / "Programs/Opera/opera.exe",
            program_files / "Opera/opera.exe",
        ],
        "chrome": [
            program_files / "Google/Chrome/Application/chrome.exe",
            program_files_x86 / "Google/Chrome/Application/chrome.exe",
            local / "Google/Chrome/Application/chrome.exe",
        ],
        "edge": [
            program_files / "Microsoft/Edge/Application/msedge.exe",
            program_files_x86 / "Microsoft/Edge/Application/msedge.exe",
        ],
        "brave": [
            program_files / "BraveSoftware/Brave-Browser/Application/brave.exe",
            local / "BraveSoftware/Brave-Browser/Application/brave.exe",
        ],
        "vivaldi": [
            local / "Vivaldi/Application/vivaldi.exe",
            program_files / "Vivaldi/Application/vivaldi.exe",
        ],
    }
    for candidate in candidates.get(bid, []):
        if candidate.exists():
            return candidate
    if bid == "opera-gx":
        return browser_executable("opera")
    return None


def opera_gx_exe() -> Path | None:
    return browser_executable("opera-gx")


def ensure_bridge_icons() -> None:
    icons_dir = extension_root() / "icons"
    if all((icons_dir / f"icon{size}.png").exists() for size in (16, 32, 48, 128)):
        return
    try:
        from opus.ui.icons import ensure_icons

        ensure_icons()
    except Exception:
        log.exception("failed to create Opus Bridge icons")
