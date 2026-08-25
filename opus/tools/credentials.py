from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import shutil
import sqlite3
import tempfile
from ctypes import wintypes
from pathlib import Path
from urllib.parse import urlparse

from opus.tools.vault import DATA_BLOB, crypt32, kernel32


BROWSER_SUFFIX_RE = re.compile(
    r"\s+[-–—|]\s+(Google Chrome|Chromium|Opera GX|Opera|Microsoft Edge|Brave|Mozilla Firefox|Firefox).*$",
    re.IGNORECASE,
)
STOP_TOKENS = {
    "https",
    "http",
    "www",
    "com",
    "login",
    "sign",
    "the",
    "and",
    "for",
    "into",
    "onto",
    "chrome",
    "opera",
    "edge",
    "brave",
    "firefox",
}
ALIASES = {
    "gmail": "google",
    "youtube": "google",
    "outlook": "live",
    "hotmail": "live",
    "xbox": "microsoft",
}


def needles_for(query: str) -> list[str]:
    text = BROWSER_SUFFIX_RE.sub("", query or "").strip().lower()
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    candidates = [text, *re.findall(r"[a-z0-9]{3,}", text)]
    for item in candidates:
        if item in seen or item in STOP_TOKENS:
            continue
        seen.add(item)
        found.append(item)
        alias = ALIASES.get(item)
        if alias and alias not in seen:
            seen.add(alias)
            found.append(alias)
    return found


def _dpapi_decrypt(data: bytes) -> bytes:
    if not data:
        return b""
    buf = ctypes.create_string_buffer(data, len(data))
    inp = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    out = DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        return b""
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


class _BCRYPT_AUTH_INFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.ULONG),
        ("dwInfoVersion", wintypes.ULONG),
        ("pbNonce", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbNonce", wintypes.ULONG),
        ("pbAuthData", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbAuthData", wintypes.ULONG),
        ("pbTag", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbTag", wintypes.ULONG),
        ("pbMacContext", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbMacContext", wintypes.ULONG),
        ("cbAAD", wintypes.ULONG),
        ("cbData", wintypes.ULONG),
        ("dwFlags", wintypes.ULONG),
    ]


def _bcrypt_aes_gcm_decrypt(key: bytes, nonce: bytes, cipher: bytes, tag: bytes) -> bytes:
    bcrypt = ctypes.windll.bcrypt
    bcrypt.BCryptOpenAlgorithmProvider.restype = wintypes.LONG
    bcrypt.BCryptSetProperty.restype = wintypes.LONG
    bcrypt.BCryptGenerateSymmetricKey.restype = wintypes.LONG
    bcrypt.BCryptDecrypt.restype = wintypes.LONG
    alg = ctypes.c_void_p()
    key_handle = ctypes.c_void_p()
    status = bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(alg), "AES", None, 0)
    if status:
        raise OSError(status)
    try:
        mode = ctypes.create_unicode_buffer("ChainingModeGCM")
        status = bcrypt.BCryptSetProperty(alg, "ChainingMode", mode, len("ChainingModeGCM") * 2, 0)
        if status:
            raise OSError(status)
        key_buf = ctypes.create_string_buffer(key, len(key))
        status = bcrypt.BCryptGenerateSymmetricKey(
            alg, ctypes.byref(key_handle), None, 0, key_buf, len(key), 0
        )
        if status:
            raise OSError(status)
        nonce_buf = (ctypes.c_ubyte * len(nonce)).from_buffer_copy(nonce)
        tag_buf = (ctypes.c_ubyte * len(tag)).from_buffer_copy(tag)
        info = _BCRYPT_AUTH_INFO()
        info.cbSize = ctypes.sizeof(_BCRYPT_AUTH_INFO)
        info.dwInfoVersion = 1
        info.pbNonce = ctypes.cast(nonce_buf, ctypes.POINTER(ctypes.c_ubyte))
        info.cbNonce = len(nonce)
        info.pbTag = ctypes.cast(tag_buf, ctypes.POINTER(ctypes.c_ubyte))
        info.cbTag = len(tag)
        cipher_buf = ctypes.create_string_buffer(cipher, len(cipher))
        out = ctypes.create_string_buffer(len(cipher))
        out_len = wintypes.ULONG()
        status = bcrypt.BCryptDecrypt(
            key_handle,
            cipher_buf,
            len(cipher),
            ctypes.byref(info),
            None,
            0,
            out,
            len(cipher),
            ctypes.byref(out_len),
            0,
        )
        if status:
            raise OSError(status)
        return out.raw[: out_len.value]
    finally:
        if key_handle:
            bcrypt.BCryptDestroyKey(key_handle)
        if alg:
            bcrypt.BCryptCloseAlgorithmProvider(alg, 0)


def _aes_gcm_decrypt(key: bytes, blob: bytes) -> str:
    nonce = blob[3:15]
    tag = blob[-16:]
    cipher = blob[15:-16]
    try:
        return _bcrypt_aes_gcm_decrypt(key, nonce, cipher, tag).decode("utf-8", errors="replace")
    except Exception:
        pass
    try:
        from Crypto.Cipher import AES

        aes = AES.new(key, AES.MODE_GCM, nonce=nonce)
        return aes.decrypt_and_verify(cipher, tag).decode("utf-8", errors="replace")
    except Exception:
        return ""


def decrypt_secret(blob: bytes, key: bytes | None) -> str:
    if not blob:
        return ""
    if blob.startswith(b"v10") or blob.startswith(b"v11"):
        if not key:
            return ""
        return _aes_gcm_decrypt(key, blob)
    if blob.startswith(b"v20"):
        return ""
    try:
        return _dpapi_decrypt(blob).decode("utf-8", errors="replace")
    except Exception:
        return ""


def hostname(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if "://" in text:
        host = (urlparse(text).hostname or "").lower()
    else:
        host = text.lower().replace("https://", "").replace("http://", "").split("/")[0]
    host = host.removeprefix("www.")
    for prefix in ("legacygeneric:target=", "domain:target=", "microsoft_wininet_"):
        if host.startswith(prefix):
            host = host[len(prefix) :]
    return host.strip(" .")


def _matches(blob: str, query: str) -> bool:
    needles = needles_for(query)
    if not needles:
        return True
    hay = blob.lower()
    return any(needle in hay for needle in needles)


def _browser_roots() -> list[tuple[str, Path]]:
    home = Path.home()
    return [
        ("Opera GX", home / "AppData/Roaming/Opera Software/Opera GX Stable"),
        ("Opera", home / "AppData/Roaming/Opera Software/Opera Stable"),
        ("Chrome", home / "AppData/Local/Google/Chrome/User Data"),
        ("Edge", home / "AppData/Local/Microsoft/Edge/User Data"),
        ("Brave", home / "AppData/Local/BraveSoftware/Brave-Browser/User Data"),
    ]


def _profiles() -> list[tuple[str, Path]]:
    skip = {"system profile", "guest profile", "crashpad", "snapshots", "dictionaries"}
    found: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for name, root in _browser_roots():
        if not root.exists():
            continue
        if (root / "Login Data").exists():
            found.append((name, root))
            seen.add(root)
        for child in root.iterdir():
            if not child.is_dir() or child.name.lower() in skip:
                continue
            if (child / "Login Data").exists() and child not in seen:
                label = name if child.name.lower() == "default" else f"{name} {child.name}"
                found.append((label, child))
                seen.add(child)
    return found


def _master_key(profile: Path) -> bytes | None:
    for candidate in (profile / "Local State", profile.parent / "Local State"):
        if not candidate.exists():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            secret = base64.b64decode(data["os_crypt"]["encrypted_key"])
        except Exception:
            continue
        if secret.startswith(b"DPAPI"):
            secret = secret[5:]
        key = _dpapi_decrypt(secret)
        if key:
            return key
    return None


def _copy_login_db(src: Path) -> Path | None:
    fd, raw = tempfile.mkstemp(prefix="opus-logins-", suffix=".db")
    os.close(fd)
    dest = Path(raw)
    try:
        shutil.copy2(src, dest)
    except OSError:
        dest.unlink(missing_ok=True)
        return None
    for suffix in ("-wal", "-shm", "-journal"):
        extra = Path(str(src) + suffix)
        if extra.exists():
            try:
                shutil.copy2(extra, Path(str(dest) + suffix))
            except OSError:
                pass
    return dest


def _cleanup_db(path: Path) -> None:
    for extra in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"), Path(str(path) + "-journal")):
        extra.unlink(missing_ok=True)


def _query_logins(db: Path) -> list[tuple[str, str, bytes]]:
    try:
        with sqlite3.connect(str(db)) as conn:
            hits = conn.execute(
                "SELECT origin_url, username_value, password_value FROM logins"
            ).fetchall()
    except sqlite3.Error:
        return []
    return [(url or "", user or "", bytes(secret or b"")) for url, user, secret in hits]


def browser_sites() -> list[str]:
    names: list[str] = []
    for _name, profile in _profiles():
        tmp = _copy_login_db(profile / "Login Data")
        if not tmp:
            continue
        try:
            for url, _user, _secret in _query_logins(tmp):
                host = hostname(url)
                if host:
                    names.append(host)
        finally:
            _cleanup_db(tmp)
    return names


def browser_logins(query: str = "") -> list[dict]:
    rows: list[dict] = []
    for name, profile in _profiles():
        key = _master_key(profile)
        tmp = _copy_login_db(profile / "Login Data")
        if not tmp:
            continue
        try:
            hits = _query_logins(tmp)
        finally:
            _cleanup_db(tmp)
        for url, user, secret in hits:
            blob = f"{url} {user} {name} {hostname(url)}"
            if query and not _matches(blob, query):
                continue
            password = decrypt_secret(secret, key)
            if not user and not password:
                continue
            rows.append(
                {
                    "site": hostname(url) or name,
                    "url": url,
                    "email": user,
                    "password": password,
                    "source": name,
                }
            )
    return rows


class CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _printable(text: str) -> bool:
    if not text:
        return False
    return all(ord(ch) >= 32 or ch in "\t\n\r" for ch in text)


def _windows_rows(query: str = "", secrets: bool = True) -> list[dict]:
    advapi32 = ctypes.windll.advapi32
    advapi32.CredEnumerateW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    count = wintypes.DWORD()
    creds = ctypes.POINTER(ctypes.POINTER(CREDENTIAL))()
    if not advapi32.CredEnumerateW(None, 0, ctypes.byref(count), ctypes.byref(creds)):
        return []
    rows: list[dict] = []
    try:
        for i in range(count.value):
            cred = creds[i].contents
            target = cred.TargetName or ""
            user = cred.UserName or ""
            password = ""
            if secrets and cred.CredentialBlob and cred.CredentialBlobSize:
                raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
                decoded = raw.decode("utf-16-le", errors="ignore").rstrip("\x00")
                if not _printable(decoded):
                    decoded = raw.decode("utf-8", errors="ignore").rstrip("\x00")
                if _printable(decoded):
                    password = decoded
            blob = f"{target} {user} {hostname(target)}"
            if query and not _matches(blob, query):
                continue
            if secrets and not user and not password:
                continue
            if not secrets and not (user or target):
                continue
            rows.append(
                {
                    "site": hostname(target) or target,
                    "url": target,
                    "email": user,
                    "password": password,
                    "source": "Windows",
                }
            )
    finally:
        advapi32.CredFree(creds)
    return rows


def windows_logins(query: str = "") -> list[dict]:
    return _windows_rows(query, secrets=True)


def windows_sites() -> list[str]:
    names: list[str] = []
    for row in _windows_rows(secrets=False):
        host = hostname(row.get("url") or "") or str(row.get("site") or "")
        if host:
            names.append(host)
    return names


def system_logins(query: str = "") -> list[dict]:
    return browser_logins(query) + windows_logins(query)


def system_sites() -> list[str]:
    return browser_sites() + windows_sites()
