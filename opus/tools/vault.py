from __future__ import annotations

import ctypes
import json
from ctypes import wintypes
from pathlib import Path

from opus.settings import appdata_dir


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


crypt32 = ctypes.windll.crypt32
kernel32 = ctypes.windll.kernel32


def _blob(data: bytes) -> DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))


def _protect(text: str) -> str:
    raw = (text or "").encode("utf-8")
    inp = _blob(raw)
    out = DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(inp), "Opus", None, None, None, 0, ctypes.byref(out)):
        return raw.hex()
    try:
        protected = ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)
    return protected.hex()


def _unprotect(stored: str) -> str:
    raw = bytes.fromhex(stored or "")
    inp = _blob(raw)
    out = DATA_BLOB()
    if crypt32.CryptUnprotectData(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        try:
            return ctypes.string_at(out.pbData, out.cbData).decode("utf-8", errors="replace")
        finally:
            kernel32.LocalFree(out.pbData)
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _vault_path() -> Path:
    folder = appdata_dir() / "vault"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "logins.json"


def _load() -> list[dict]:
    path = _vault_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _save(rows: list[dict]) -> None:
    _vault_path().write_text(json.dumps(rows, indent=2), encoding="utf-8")


def _match(row: dict, needle: str) -> bool:
    blob = " ".join(
        [
            str(row.get("site") or ""),
            str(row.get("url") or ""),
            str(row.get("email") or ""),
        ]
    ).lower()
    return needle.lower() in blob


def save_login(site: str, email: str, password: str, url: str = "") -> str:
    site = (site or "").strip()
    email = (email or "").strip()
    password = (password or "").strip()
    if not site or not email or not password:
        return "I need a site, email, and password to save."
    rows = [row for row in _load() if not _match(row, site)]
    rows.append(
        {
            "site": site,
            "url": (url or "").strip(),
            "email": email,
            "password": _protect(password),
        }
    )
    _save(rows)
    return f"Saved the login for {site}."


def list_logins() -> str:
    from opus.tools.credentials import hostname, system_sites

    names: list[str] = []
    for row in _load():
        names.append(hostname(str(row.get("url") or "")) or str(row.get("site") or "unknown"))
    names.extend(system_sites())
    unique = sorted({name for name in names if name})
    if not unique:
        return "No saved logins yet."
    return "Saved sites: " + ", ".join(unique[:40])


def _score(row: dict, query: str) -> int:
    from opus.tools.credentials import hostname, needles_for

    host = hostname(str(row.get("url") or "")) or str(row.get("site") or "").lower()
    site = str(row.get("site") or "").lower()
    url = str(row.get("url") or "").lower()
    email = str(row.get("email") or "").lower()
    best = 0
    for needle in needles_for(query):
        if host == needle or site == needle:
            best = max(best, 4)
        elif needle in host or host in needle:
            best = max(best, 3)
        elif needle in site or needle in url or needle in email:
            best = max(best, 2)
        elif _match(row, needle):
            best = max(best, 1)
    return best


def find_login(site: str) -> dict | None:
    needle = (site or "").strip().lower()
    if not needle:
        return None
    ranked: list[tuple[int, dict]] = []
    for row in _load():
        score = _score(row, needle)
        if not score:
            continue
        copy = dict(row)
        copy["password"] = _unprotect(str(row.get("password") or ""))
        ranked.append((score + (2 if copy.get("password") else 0), copy))
    from opus.tools.credentials import system_logins

    for row in system_logins(needle):
        score = _score(row, needle)
        if not score:
            continue
        ranked.append((score + (2 if row.get("password") else 0), row))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1]


def fill_login(site: str) -> str:
    from opus.tools.keyboard import type_login

    query = (site or "").strip()
    if not query:
        from opus.tools.screen import foreground_window

        query = foreground_window().get("title") or ""
    row = find_login(query)
    if not row:
        return f"I don't have a login saved for {query or 'this page'}."
    if not row.get("password"):
        return f"I found the account for {query} but the browser has that password locked."
    type_login(row.get("email") or "", row.get("password") or "")
    return "Typed the saved login."
