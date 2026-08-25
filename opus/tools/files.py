from __future__ import annotations

import json
import os
import re
from pathlib import Path

from opus.settings import scripts_dir

SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "Windows",
    "WinSxS",
    "$Recycle.Bin",
    "System Volume Information",
}


def _safe_roots(scope: str) -> list[Path]:
    home = Path.home()
    if scope == "all":
        roots = []
        for letter in "CDEFGH":
            drive = Path(f"{letter}:/")
            if drive.exists():
                roots.append(drive)
        return roots or [home]
    return [
        home / "Desktop",
        home / "Documents",
        home / "Downloads",
        home / "Pictures",
        home / "Videos",
        home / "Music",
        home / "Projects",
        home,
    ]


def search_files(query: str, scope: str = "user", limit: int = 40) -> str:
    query = (query or "").strip().lower()
    if not query:
        return "No search query provided."
    hits: list[str] = []
    for root in _safe_roots(scope):
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                if query in name.lower():
                    hits.append(str(Path(dirpath) / name))
                    if len(hits) >= limit:
                        return json.dumps(hits, indent=2)
            if len(hits) >= limit:
                break
    return json.dumps(hits or ["No matches"], indent=2)


def read_file(path: str, max_chars: int = 8000) -> str:
    target = Path(os.path.expandvars(os.path.expanduser(path))).resolve()
    if not target.exists() or not target.is_file():
        return f"File not found: {target}"
    if target.stat().st_size > 2_000_000:
        return "File is too large to read directly."
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"Could not read file: {exc}"
    if not text.strip():
        return "File is empty or binary."
    return text[:max_chars]


def list_directory(path: str) -> str:
    target = Path(os.path.expandvars(os.path.expanduser(path))).resolve()
    if not target.exists():
        return f"Path not found: {target}"
    if target.is_file():
        return str(target)
    names = []
    for child in sorted(target.iterdir())[:80]:
        kind = "dir" if child.is_dir() else "file"
        names.append(f"{kind}: {child.name}")
    return "\n".join(names) or "Empty folder."


def open_path(path: str) -> str:
    target = Path(os.path.expandvars(os.path.expanduser(path))).resolve()
    if not target.exists():
        return f"Path not found: {target}"
    os.startfile(target)  # noqa: S606 — user-requested local open
    return f"Opened {target}"


def _safe_script_path(filename: str | None, language: str | None) -> Path:
    extensions = {
        "python": ".py",
        "py": ".py",
        "powershell": ".ps1",
        "ps1": ".ps1",
        "javascript": ".js",
        "js": ".js",
        "html": ".html",
        "css": ".css",
        "json": ".json",
        "text": ".txt",
        "txt": ".txt",
        "bat": ".bat",
        "cmd": ".cmd",
    }
    raw = (filename or "").strip()
    if raw:
        name = Path(raw.replace("\\", "/")).name
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-") or "opus-script"
    else:
        stamp = __import__("datetime").datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        name = f"opus-script-{stamp}"
    if "." not in name:
        ext = extensions.get((language or "python").lower().lstrip("."), ".py")
        name += ext
    return scripts_dir() / name


def write_file(content: str, filename: str = "", language: str = "python") -> str:
    text = content or ""
    if not text.strip():
        return "No file content was provided."
    target = _safe_script_path(filename, language)
    target.write_text(text.replace("\r\n", "\n"), encoding="utf-8")
    return f"Saved {target.name} to {target}"
