from __future__ import annotations

import os
import subprocess
from pathlib import Path

from opus.tools.bridge import browser_executable

CREATE_NO_WINDOW = 0x08000000

APP_ALIASES: dict[str, str] = {
    "opera": "opera-gx",
    "opera gx": "opera-gx",
    "operagx": "opera-gx",
    "opera-gx": "opera-gx",
    "chrome": "chrome",
    "google chrome": "chrome",
    "google": "chrome",
    "edge": "edge",
    "microsoft edge": "edge",
    "msedge": "edge",
    "firefox": "firefox",
    "brave": "brave",
    "vivaldi": "vivaldi",
}

# Fuzzy aliases: common mishearings / similar-sounding words
FUZZY_APP_MAP: dict[str, str] = {
    "opera": "opera-gx",
    "oprah": "opera-gx",
    "opra": "opera-gx",
    "opera gx": "opera-gx",
    "opera jx": "opera-gx",
    "opera gs": "opera-gx",
    "chrome": "chrome",
    "crome": "chrome",
    "krome": "chrome",
    "google": "chrome",
    "edge": "edge",
    "microsoft edge": "edge",
    "firefox": "firefox",
    "fire fox": "firefox",
    "brave": "brave",
    "spotify": "spotify",
    "discord": "discord",
    "steam": "steam",
    "notepad": "notepad",
    "note pad": "notepad",
    "calculator": "calc",
    "calc": "calc",
    "file explorer": "explorer",
    "explorer": "explorer",
    "files": "explorer",
    "task manager": "taskmgr",
    "settings": "ms-settings:",
    "control panel": "control",
    "terminal": "wt",
    "powershell": "powershell",
    "cmd": "cmd",
    "command prompt": "cmd",
    "minecraft": "minecraft",
    "epic games": "com.epicgames.launcher:",
    "epic": "com.epicgames.launcher:",
}

BROWSER_LABELS: list[dict[str, str]] = [
    {"id": "opera-gx", "name": "Opera GX"},
    {"id": "opera", "name": "Opera"},
    {"id": "chrome", "name": "Google Chrome"},
    {"id": "firefox", "name": "Firefox"},
    {"id": "edge", "name": "Microsoft Edge"},
    {"id": "brave", "name": "Brave"},
    {"id": "vivaldi", "name": "Vivaldi"},
]


from difflib import SequenceMatcher


def _normalize_app(name: str) -> str:
    return " ".join((name or "").strip().lower().split())


def _fuzzy_app(name: str) -> str:
    """Fuzzy match an app name to a known app."""
    norm = _normalize_app(name)
    # Exact match in aliases
    if norm in APP_ALIASES:
        return APP_ALIASES[norm]
    # Exact match in fuzzy map
    if norm in FUZZY_APP_MAP:
        return FUZZY_APP_MAP[norm]
    # Fuzzy match against all known names
    best_name = ""
    best_score = 0.0
    for key, val in FUZZY_APP_MAP.items():
        score = SequenceMatcher(None, norm, key).ratio()
        if norm in key or key in norm:
            score += 0.25
        if score > best_score:
            best_score = score
            best_name = val
    if best_score >= 0.6:
        return best_name
    return ""


def _resolve_browser_id(name: str) -> str:
    return APP_ALIASES.get(_normalize_app(name), "")


def _start_silent(target: str) -> bool:
    """Launch an app by name without flashing a console window."""
    try:
        os.startfile(target)  # noqa: S606
        return True
    except OSError:
        return False


def _launch_exe(exe: Path, argument: str = "") -> bool:
    flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    command = [str(exe)]
    if argument:
        command.append(argument)
    try:
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
        )
        return True
    except OSError:
        return False


def run_app(name: str) -> str:
    target = (name or "").strip()
    if not target:
        return "No app name given."
    path = Path(os.path.expandvars(os.path.expanduser(target)))
    if path.exists():
        os.startfile(path)  # noqa: S606
        return "Done."
    # Try fuzzy match first
    fuzzy = _fuzzy_app(target)
    if fuzzy and fuzzy not in APP_ALIASES.values():
        # It's a non-browser app, launch it directly
        if _start_silent(fuzzy):
            return "Done."
    browser_id = _resolve_browser_id(target) or (fuzzy if fuzzy in APP_ALIASES.values() else "")
    if browser_id:
        if browser_id in {"opera-gx", "opera"}:
            from opus.tools.browser import _opera_running, opera_busy, opera_recently_cycled

            if opera_busy():
                return "Opera is busy installing or removing an extension. Try again in a moment."
            if _opera_running() or opera_recently_cycled():
                return "Done."
        exe = browser_executable(browser_id)
        if exe and _launch_exe(exe):
            return "Done."
        return f"I couldn't find {target} on this PC."
    if _start_silent(target):
        return "Done."
    return f"Could not launch {target}."


def open_url(url: str, browser_id: str = "") -> str:
    url = (url or "").strip()
    if not url:
        return "No URL given."
    bid = _resolve_browser_id(browser_id) or (browser_id or "").strip().lower()
    if bid in {"opera-gx", "opera"}:
        from opus.tools.browser import opera_busy

        if opera_busy():
            return "Opera is busy installing or removing an extension. Try again in a moment."
    exe = browser_executable(bid)
    if exe and _launch_exe(exe, url):
        label = bid
        for item in BROWSER_LABELS:
            if item["id"] == bid:
                label = item["name"]
                break
        return f"Opened that in {label}."
    os.startfile(url)  # noqa: S606
    return "Done."
