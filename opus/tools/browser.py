from __future__ import annotations

import io
import json
import re
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from opus.logutil import get_logger
from opus.net_policy import guarded_urlopen

log = get_logger()

EXT_ID_RE = re.compile(r"[a-p]{32}")
CHROME_DETAIL_RE = re.compile(r"/detail/[^/\"'?]+/([a-p]{32})")
CHROME_EPOCH_OFFSET = 11644473600000000
FROM_WEBSTORE = 8

_opera_lock = threading.Lock()
_install_lock = threading.Lock()
_last_opera_cycle = 0.0


def _profiles(browser_id: str = "") -> list[tuple[str, Path]]:
    home = Path.home()
    all_profiles = [
        ("Opera GX", home / "AppData/Roaming/Opera Software/Opera GX Stable"),
        ("Opera", home / "AppData/Roaming/Opera Software/Opera Stable"),
        ("Chrome", home / "AppData/Local/Google/Chrome/User Data/Default"),
        ("Edge", home / "AppData/Local/Microsoft/Edge/User Data/Default"),
        ("Brave", home / "AppData/Local/BraveSoftware/Brave-Browser/User Data/Default"),
    ]
    found = [(name, path) for name, path in all_profiles if path.exists()]
    bid = (browser_id or "").lower()
    if "opera" in bid:
        opera = [item for item in found if item[0].startswith("Opera")]
        return opera or found
    return found


def _chrome_time_now() -> str:
    return str(int(time.time() * 1_000_000) + CHROME_EPOCH_OFFSET)


def _chrome_time(value: int) -> str:
    try:
        epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
        stamp = epoch + timedelta(microseconds=int(value))
        return stamp.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _fetch_bytes(url: str, timeout: float = 30.0) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        },
    )
    with guarded_urlopen(request, timeout=timeout) as response:
        return response.read()


def _fetch_text(url: str, timeout: float = 15.0) -> str:
    return _fetch_bytes(url, timeout=timeout).decode("utf-8", errors="replace")


def _crx_install_url(extension_id: str) -> str:
    payload = urllib.parse.quote(f"id={extension_id}&installsource=ondemand&uc")
    return (
        "https://clients2.google.com/service/update2/crx?"
        f"response=redirect&prodversion=122.0&acceptformat=crx2,crx3&x={payload}"
    )


def _crx_available(extension_id: str) -> bool:
    try:
        data = _fetch_bytes(_crx_install_url(extension_id), timeout=20)
        return len(data) > 512
    except Exception:
        return False


def _normalize_query(query: str) -> str:
    text = (query or "").strip()
    if text.lower().startswith("http"):
        match = CHROME_DETAIL_RE.search(text) or EXT_ID_RE.search(text)
        if match:
            return match.group(1)
    return text


def _search_chrome_store(query: str) -> tuple[str, str] | None:
    needle = _normalize_query(query)
    if EXT_ID_RE.fullmatch(needle):
        return needle, needle
    url = "https://chromewebstore.google.com/search/" + urllib.parse.quote(needle)
    try:
        html = _fetch_text(url)
    except Exception:
        log.exception("chrome store search failed")
        return None
    match = CHROME_DETAIL_RE.search(html)
    if not match:
        ids = EXT_ID_RE.findall(html)
        if ids:
            return ids[0], needle
        return None
    ext_id = match.group(1)
    title_match = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html, re.I)
    name = title_match.group(1).strip() if title_match else needle
    return ext_id, name


def _search_opera_addons(query: str) -> tuple[str, str] | None:
    needle = _normalize_query(query)
    if EXT_ID_RE.fullmatch(needle):
        return needle, needle
    url = "https://addons.opera.com/en/search/?query=" + urllib.parse.quote(needle)
    try:
        html = _fetch_text(url)
    except Exception:
        log.exception("opera addon search failed")
        return None
    links = re.findall(r'href="(/en/extensions/details/[^"]+)"', html, flags=re.I)
    detail_html = html
    if links:
        try:
            detail_html = _fetch_text("https://addons.opera.com" + links[0])
        except Exception:
            log.exception("opera addon detail fetch failed")
    ids = EXT_ID_RE.findall(detail_html)
    if not ids:
        return None
    title_match = re.search(r"<h1[^>]*>([^<]+)</h1>", detail_html, re.I)
    name = title_match.group(1).strip() if title_match else needle
    return ids[0], name


def _resolve_extension(query: str, browser_id: str = "opera-gx") -> tuple[str, str]:
    direct = _normalize_query(query)
    if EXT_ID_RE.fullmatch(direct):
        if not _crx_available(direct):
            raise RuntimeError(f'Extension ID "{direct}" is not available from the Chrome Web Store.')
        return direct, direct

    bid = (browser_id or "").lower()
    # Opera GX uses Chrome Web Store extension IDs; prefer Chrome search and verify CRX exists.
    searches = (
        (_search_chrome_store, _search_opera_addons)
        if "opera" in bid
        else (_search_chrome_store, _search_opera_addons)
    )
    for search in searches:
        hit = search(query)
        if hit and _crx_available(hit[0]):
            return hit
    raise RuntimeError(f'I could not find an extension called "{query}".')


def _extract_crx(data: bytes, dest: Path) -> None:
    if data[:4] == b"Cr24":
        version = struct.unpack("<I", data[4:8])[0]
        if version == 3:
            header_size = struct.unpack("<I", data[8:12])[0]
            zip_start = 12 + header_size
        else:
            pub_len = struct.unpack("<I", data[8:12])[0]
            sig_len = struct.unpack("<I", data[12:16])[0]
            zip_start = 16 + pub_len + sig_len
        payload = data[zip_start:]
    else:
        payload = data
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        archive.extractall(dest)


def _opera_running() -> bool:
    try:
        import psutil

        return any((proc.info.get("name") or "").lower() == "opera.exe" for proc in psutil.process_iter(["name"]))
    except Exception:
        return False


def _stop_opera(force: bool = True) -> bool:
    from opus.tools.bridge import browser_executable

    if not _opera_running():
        return False
    opera = browser_executable("opera-gx")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if opera:
        try:
            subprocess.run([str(opera), "--shutdown"], timeout=15, check=False, creationflags=flags)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for _ in range(20):
            if not _opera_running():
                return True
            time.sleep(0.4)
    if force and _opera_running():
        try:
            subprocess.run(
                ["taskkill", "/IM", "opera.exe", "/F"],
                timeout=20,
                check=False,
                creationflags=flags,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        for _ in range(30):
            if not _opera_running():
                return True
            time.sleep(0.3)
    return not _opera_running()


def _start_opera() -> None:
    from opus.tools.bridge import browser_executable

    if _opera_running():
        return
    opera = browser_executable("opera-gx")
    if not opera:
        return
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            [str(opera)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
    except OSError:
        log.exception("failed to start Opera GX")


def opera_busy() -> bool:
    return _install_lock.locked() or _opera_lock.locked()


def opera_recently_cycled(within_seconds: float = 8.0) -> bool:
    return time.time() - _last_opera_cycle < within_seconds


def _with_opera_restarted(action) -> None:
    with _opera_lock:
        was_running = _opera_running()
        if was_running and not _stop_opera(force=True):
            raise RuntimeError("Could not close Opera completely.")
        action(restart=True)
        if was_running:
            if _opera_running():
                _stop_opera(force=True)
            time.sleep(0.5)
            _start_opera()
        global _last_opera_cycle
        _last_opera_cycle = time.time()


def _load_prefs(profile: Path) -> dict:
    prefs = profile / "Preferences"
    if not prefs.exists():
        return {}
    try:
        return json.loads(prefs.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_prefs(profile: Path, data: dict) -> None:
    (profile / "Preferences").write_text(json.dumps(data), encoding="utf-8")


def _manifest_name(manifest: dict, manifest_path: Path) -> str:
    name = str(manifest.get("name") or "")
    if name.startswith("__MSG_") and name.endswith("__"):
        key = name[6:-2]
        locales = manifest_path.parent / "_locales"
        if locales.is_dir():
            candidates: list[Path] = []
            for code in ("en", "en_US", "en_GB"):
                path = locales / code / "messages.json"
                if path.exists():
                    candidates.append(path)
            if not candidates:
                candidates = sorted(locales.glob("*/messages.json"))
            for messages in candidates:
                try:
                    data = json.loads(messages.read_text(encoding="utf-8", errors="replace"))
                    message = data.get(key, {}).get("message")
                    if message:
                        return str(message)
                except Exception:
                    continue
    return name or ""


def _extension_label(profile: Path, ext_id: str) -> str:
    folder = profile / "Extensions" / ext_id
    if not folder.exists():
        return ext_id
    for manifest in folder.glob("*/manifest.json"):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
            label = _manifest_name(data, manifest)
            return label or ext_id
        except Exception:
            continue
    return ext_id


def _is_installed(profile: Path, ext_id: str) -> bool:
    folder = profile / "Extensions" / ext_id
    if not folder.exists():
        return False
    return any(folder.glob("*/manifest.json"))


def _register_extension(profile: Path, ext_id: str, version: str, manifest: dict) -> None:
    data = _load_prefs(profile)
    extensions = data.setdefault("extensions", {})
    settings = extensions.setdefault("settings", {})
    path = str((profile / "Extensions" / ext_id / version).resolve())
    settings[ext_id] = {
        "creation_flags": 1,
        "from_webstore": True,
        "grant_permissions": True,
        "location": FROM_WEBSTORE,
        "path": path,
        "state": 1,
        "was_installed_by_default": False,
        "was_installed_by_oem": False,
        "install_time": _chrome_time_now(),
        "manifest": manifest,
    }
    _save_prefs(profile, data)


def _strip_extension_from_prefs(profile: Path, ext_id: str) -> None:
    data = _load_prefs(profile)
    extensions = data.setdefault("extensions", {})
    settings = extensions.setdefault("settings", {})
    settings.pop(ext_id, None)
    for key in ("pinned_extensions", "toolbar"):
        value = extensions.get(key)
        if isinstance(value, list):
            extensions[key] = [item for item in value if item != ext_id]
    for section in ("sidebar", "ui_pinned_extensions", "opera_gx"):
        block = data.get(section)
        if isinstance(block, dict):
            for key in list(block.keys()):
                if ext_id in str(key) or ext_id in str(block.get(key)):
                    block.pop(key, None)
        elif isinstance(block, list):
            data[section] = [item for item in block if item != ext_id and ext_id not in str(item)]
    _save_prefs(profile, data)


def _install_to_profile(profile: Path, ext_id: str, label: str) -> str:
    if _is_installed(profile, ext_id):
        data = _load_prefs(profile)
        state = data.get("extensions", {}).get("settings", {}).get(ext_id, {}).get("state")
        if state != 1:
            data.setdefault("extensions", {}).setdefault("settings", {}).setdefault(ext_id, {})["state"] = 1
            _save_prefs(profile, data)
        return _extension_label(profile, ext_id)

    crx = _fetch_bytes(_crx_install_url(ext_id))
    tmp = Path(tempfile.mkdtemp(prefix="opus-crx-"))
    try:
        _extract_crx(crx, tmp)
        manifest = json.loads((tmp / "manifest.json").read_text(encoding="utf-8"))
        version = str(manifest.get("version") or "1.0.0")
        target = profile / "Extensions" / ext_id / version
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(tmp, target)
        _register_extension(profile, ext_id, version, manifest)
        return _manifest_name(manifest, target / "manifest.json") or label
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _remove_from_profile(profile: Path, ext_id: str) -> str:
    label = _extension_label(profile, ext_id)
    _strip_extension_from_prefs(profile, ext_id)
    folder = profile / "Extensions" / ext_id
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    return label


def _find_extension_id(profile: Path, needle: str) -> str | None:
    needle = needle.strip().lower()
    if EXT_ID_RE.fullmatch(needle):
        return needle
    folder = profile / "Extensions"
    if not folder.exists():
        return None
    for ext_id in folder.iterdir():
        if not ext_id.is_dir():
            continue
        label = _extension_label(profile, ext_id.name).lower()
        if needle in label or needle == ext_id.name.lower():
            return ext_id.name
    data = _load_prefs(profile)
    for ext_id, meta in (data.get("extensions", {}).get("settings", {}) or {}).items():
        name = str((meta or {}).get("manifest", {}).get("name") or "").lower()
        if needle in name:
            return ext_id
    return None


def browser_history(query: str = "", limit: int = 25) -> str:
    needle = (query or "").strip().lower()
    limit = max(5, min(40, int(limit or 25)))
    lines: list[str] = []
    for name, profile in _profiles():
        history = profile / "History"
        if not history.exists():
            continue
        tmp = Path(tempfile.gettempdir()) / f"opus-{name.replace(' ', '')}-history"
        try:
            shutil.copy2(history, tmp)
            with sqlite3.connect(str(tmp)) as conn:
                rows = conn.execute(
                    "SELECT title, url, last_visit_time FROM urls ORDER BY last_visit_time DESC LIMIT 80"
                ).fetchall()
        except Exception:
            continue
        hits = 0
        for title, url, visited in rows:
            blob = f"{title} {url}".lower()
            if needle and needle not in blob:
                continue
            lines.append(f"{name} | {_chrome_time(visited)} | {title or '(no title)'} | {url}")
            hits += 1
            if hits >= limit:
                break
    return "\n".join(lines) or "No browser history found."


def list_extensions() -> str:
    found: list[str] = []
    for name, profile in _profiles("opera-gx"):
        folder = profile / "Extensions"
        if not folder.exists():
            continue
        data = _load_prefs(profile)
        settings = data.get("extensions", {}).get("settings", {})
        for ext_id in sorted(p.name for p in folder.iterdir() if p.is_dir() and p.name != "Temp"):
            label = _extension_label(profile, ext_id)
            state = (settings.get(ext_id) or {}).get("state", 1)
            found.append(f"{'enabled' if state == 1 else 'disabled'}: {label}")
    return "\n".join(found[:80]) or "No extensions found."


def add_extension(query: str, browser_id: str = "opera-gx") -> str:
    q = (query or "").strip()
    if not q:
        return "Tell me which extension to add."
    try:
        ext_id, label = _resolve_extension(q, browser_id)
    except RuntimeError as exc:
        return str(exc)

    profiles = _profiles(browser_id)
    if not profiles:
        return "I couldn't find an Opera GX profile on this PC."

    _name, profile = profiles[0]
    if _is_installed(profile, ext_id):
        installed_label = _extension_label(profile, ext_id)
        data = _load_prefs(profile)
        state = data.get("extensions", {}).get("settings", {}).get(ext_id, {}).get("state")
        if state != 1:
            data.setdefault("extensions", {}).setdefault("settings", {}).setdefault(ext_id, {})["state"] = 1
            _save_prefs(profile, data)
        return f"{installed_label} is already installed and turned on."

    with _install_lock:
        result = {"label": label}

        def work(restart: bool) -> None:
            result["label"] = _install_to_profile(profile, ext_id, label)

        try:
            _with_opera_restarted(work)
        except Exception as exc:
            log.exception("extension install failed")
            return f"Could not install {label}: {exc}"

    return f"Installed and turned on {result['label']}."


def remove_extension(query: str, browser_id: str = "opera-gx") -> str:
    needle = (query or "").strip()
    if not needle:
        return "Tell me which extension to remove."

    profiles = _profiles(browser_id)
    if not profiles:
        return "I couldn't find an Opera GX profile on this PC."

    _name, profile = profiles[0]
    ext_id = _find_extension_id(profile, needle)
    if not ext_id:
        return f'I could not find an extension called "{needle}".'

    with _install_lock:
        result = {"label": needle}

        def work(restart: bool) -> None:
            result["label"] = _remove_from_profile(profile, ext_id)

        try:
            _with_opera_restarted(work)
        except Exception as exc:
            log.exception("extension remove failed")
            return f"Could not remove {needle}: {exc}"

    return f"Removed and unpinned {result['label']}."
