"""Spotify Web API connector. Playback is API-only — never SendKeys."""
from __future__ import annotations

import hashlib
import os
import secrets
import subprocess
import time
import webbrowser
from base64 import urlsafe_b64encode
from typing import Any
from urllib.parse import urlencode

import httpx

from opus.logutil import get_logger
from opus.net_policy import get_policy
from opus.settings import Settings

log = get_logger()

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_URL = "https://api.spotify.com/v1"
SCOPES = " ".join(
    (
        "user-modify-playback-state",
        "user-read-playback-state",
        "user-read-currently-playing",
        "user-library-read",
        "playlist-read-private",
        "playlist-read-collaborative",
        "user-read-email",
    )
)
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
SW_SHOWMINNOACTIVE = 7
SPOTIFY_EXE = os.path.expandvars(r"%APPDATA%\Spotify\Spotify.exe")
SPOTIFY_STORE_EXE = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\Spotify.exe")

_pending: dict[str, dict[str, str]] = {}


def redirect_uri(settings: Settings, *, public_host: str | None = None) -> str:
    host = (public_host or settings.get("host") or "127.0.0.1").strip() or "127.0.0.1"
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    port = int(settings.get("port") or 5840)
    return f"http://{host}:{port}/api/apps/spotify/callback"


def connection_status(settings: Settings, *, public_host: str | None = None) -> dict:
    connected = bool(settings.get("spotify_refresh_token"))
    return {
        "id": "spotify",
        "name": "Spotify",
        "connected": connected,
        "account": (settings.get("spotify_user_name") or "") if connected else "",
        "client_id_set": bool((settings.get("spotify_client_id") or "").strip()),
        "redirect_uri": redirect_uri(settings, public_host=public_host),
        "needs_premium": True,
    }


def _pkce_pair() -> tuple[str, str]:
    verifier = urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def start_login(settings: Settings, *, open_browser: bool = True, public_host: str | None = None) -> dict:
    client_id = (settings.get("spotify_client_id") or "").strip()
    if not client_id:
        from opus.runtime import is_phone

        where = "settings" if is_phone() else "the panel"
        return {
            "ok": False,
            "error": f"Add your Spotify Client ID in {where} first.",
        }
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    uri = redirect_uri(settings, public_host=public_host)
    _pending[state] = {"verifier": verifier, "client_id": client_id, "redirect_uri": uri}
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": uri,
        "scope": SCOPES,
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    url = f"{AUTH_URL}?{urlencode(params)}"
    if open_browser:
        webbrowser.open(url)
    return {"ok": True, "url": url, "redirect_uri": uri}


def disconnect(settings: Settings) -> dict:
    settings.update(
        {
            "spotify_access_token": "",
            "spotify_refresh_token": "",
            "spotify_token_expires_at": 0,
            "spotify_user_name": "",
            "spotify_user_id": "",
        }
    )
    return connection_status(settings)


def _request(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    data: dict | None = None,
    json: dict | None = None,
    timeout: float = 15.0,
) -> httpx.Response:
    get_policy().assert_outbound(url)
    return httpx.request(
        method,
        url,
        headers=headers,
        data=data,
        json=json,
        timeout=timeout,
    )


def finish_login(settings: Settings, *, code: str, state: str) -> dict:
    pending = _pending.pop(state, None)
    if not pending:
        return {"ok": False, "error": "Login expired. Click Connect Spotify again."}
    client_id = pending["client_id"]
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending.get("redirect_uri") or redirect_uri(settings),
        "client_id": client_id,
        "code_verifier": pending["verifier"],
    }
    secret = (settings.get("spotify_client_secret") or "").strip()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if secret:
        body["client_secret"] = secret
    try:
        response = _request("POST", TOKEN_URL, headers=headers, data=body)
        payload = response.json()
    except Exception:
        log.exception("spotify token exchange failed")
        return {"ok": False, "error": "Couldn't finish Spotify login."}
    if response.status_code >= 400 or "access_token" not in payload:
        log.warning("spotify token error %s %s", response.status_code, payload)
        return {"ok": False, "error": "Spotify refused the login. Check the Client ID and redirect URI."}
    _store_tokens(settings, payload, client_id=client_id)
    try:
        me = _api(settings, "GET", "/me")
        settings.update(
            {
                "spotify_user_name": me.get("display_name") or me.get("id") or "",
                "spotify_user_id": me.get("id") or "",
            }
        )
    except Exception:
        log.exception("spotify profile fetch failed")
    return {"ok": True, **connection_status(settings)}


def _store_tokens(settings: Settings, payload: dict, *, client_id: str = "") -> None:
    expires = time.time() + max(60, int(payload.get("expires_in") or 3600) - 30)
    patch = {
        "spotify_access_token": payload.get("access_token") or "",
        "spotify_token_expires_at": expires,
    }
    if payload.get("refresh_token"):
        patch["spotify_refresh_token"] = payload["refresh_token"]
    if client_id:
        patch["spotify_client_id"] = client_id
    settings.update(patch)


def _refresh(settings: Settings) -> bool:
    refresh = (settings.get("spotify_refresh_token") or "").strip()
    client_id = (settings.get("spotify_client_id") or "").strip()
    if not refresh or not client_id:
        return False
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": client_id,
    }
    secret = (settings.get("spotify_client_secret") or "").strip()
    if secret:
        body["client_secret"] = secret
    try:
        response = _request(
            "POST",
            TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=body,
        )
        payload = response.json()
    except Exception:
        log.exception("spotify refresh failed")
        return False
    if response.status_code >= 400 or "access_token" not in payload:
        log.warning("spotify refresh error %s %s", response.status_code, payload)
        return False
    _store_tokens(settings, payload)
    return True


def _token(settings: Settings) -> str:
    token = (settings.get("spotify_access_token") or "").strip()
    expires = float(settings.get("spotify_token_expires_at") or 0)
    if token and time.time() < expires:
        return token
    if _refresh(settings):
        return (settings.get("spotify_access_token") or "").strip()
    return token


def connected(settings: Settings) -> bool:
    return bool((settings.get("spotify_refresh_token") or "").strip())


def _api(settings: Settings, method: str, path: str, *, json: dict | None = None, params: dict | None = None) -> Any:
    token = _token(settings)
    if not token:
        raise PermissionError("Spotify isn't connected.")
    url = path if path.startswith("http") else f"{API_URL}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    headers = {"Authorization": f"Bearer {token}"}
    response = _request(method, url, headers=headers, json=json)
    if response.status_code == 401 and _refresh(settings):
        headers["Authorization"] = f"Bearer {_token(settings)}"
        response = _request(method, url, headers=headers, json=json)
    if response.status_code == 204 or not response.content:
        return {}
    try:
        payload = response.json()
    except Exception:
        payload = {"error": response.text}
    if response.status_code == 403:
        reason = ""
        if isinstance(payload, dict):
            reason = str((payload.get("error") or {}).get("reason") or payload.get("error") or "")
        if "premium" in reason.lower():
            raise PermissionError("Spotify Premium is required for playback control.")
        raise PermissionError("Spotify blocked that. Premium is required, and Spotify needs a moment after it opens.")
    if response.status_code == 404:
        if method.upper() in {"PUT", "POST"}:
            raise RuntimeError("I opened Spotify, but it isn't ready as a playback device yet. Try once more.")
        return {"_empty": True, "_status": 404}
    if response.status_code >= 400:
        log.warning("spotify api %s %s %s", method, path, payload)
        raise RuntimeError("Spotify API request failed.")
    return payload


def _spotify_running() -> bool:
    if os.name != "nt":
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Spotify.exe", "/NH"],
            capture_output=True,
            text=True,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
        return "Spotify.exe" in (result.stdout or "")
    except Exception:
        return False


def _spotify_launch_path() -> str | None:
    for candidate in (SPOTIFY_EXE, SPOTIFY_STORE_EXE):
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def _wake_local_player() -> bool:
    """Open the local Spotify app in the background so the API has a device."""
    if os.name != "nt":
        return False
    if _spotify_running():
        return True
    launched = False
    try:
        exe = _spotify_launch_path()
        if exe:
            info = subprocess.STARTUPINFO()
            info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            info.wShowWindow = SW_SHOWMINNOACTIVE
            subprocess.Popen(
                [exe],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                startupinfo=info,
                creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            )
            launched = True
        else:
            subprocess.Popen(
                ["cmd", "/c", "start", "", "/min", "spotify:"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )
            launched = True
    except Exception:
        log.exception("spotify launch failed")
        return False
    return launched


def _wait_for_devices(settings: Settings, *, seconds: float = 12.0) -> list[dict]:
    deadline = time.time() + max(2.0, seconds)
    devices: list[dict] = []
    while time.time() < deadline:
        try:
            devices = (_api(settings, "GET", "/me/player/devices") or {}).get("devices") or []
        except Exception:
            devices = []
        if devices:
            return devices
        time.sleep(0.7)
    return devices


def _ensure_player(settings: Settings) -> None:
    """If Spotify isn't open, start it in the background, then wait for a Connect device."""
    if os.name == "nt" and not _spotify_running():
        _wake_local_player()
        _wait_for_devices(settings, seconds=12.0)


def _active_device_id(settings: Settings) -> str | None:
    _ensure_player(settings)
    playback = _api(settings, "GET", "/me/player")
    if playback and not playback.get("_empty"):
        device = playback.get("device") or {}
        if device.get("id"):
            return str(device["id"])
    devices = (_api(settings, "GET", "/me/player/devices") or {}).get("devices") or []
    if not devices:
        devices = _wait_for_devices(settings, seconds=8.0)
    if not devices:
        return None
    active = next((item for item in devices if item.get("is_active")), devices[0])
    device_id = str(active.get("id") or "")
    if device_id and not active.get("is_active"):
        try:
            _api(settings, "PUT", "/me/player", json={"device_ids": [device_id], "play": False})
            time.sleep(0.4)
        except Exception:
            log.exception("spotify transfer failed")
    return device_id or None


def _play(settings: Settings, body: dict) -> None:
    device_id = _active_device_id(settings)
    if not device_id:
        raise RuntimeError("I opened Spotify, but it isn't ready as a playback device yet. Try once more.")
    path = f"/me/player/play?{urlencode({'device_id': device_id})}"
    _api(settings, "PUT", path, json=body)


def _search(settings: Settings, query: str) -> dict:
    return _api(
        settings,
        "GET",
        "/search",
        params={"q": query, "type": "track,artist,album,playlist", "limit": "8"},
    ) or {}


def _items(payload: dict, kind: str) -> list[dict]:
    block = (payload.get(kind) or {}) if isinstance(payload, dict) else {}
    return [item for item in (block.get("items") or []) if item]


def _name_eq(item: dict, query: str) -> bool:
    return (item.get("name") or "").strip().lower() == query.strip().lower()


def _pick_target(query: str, results: dict) -> tuple[str, dict] | None:
    q = (query or "").strip().lower()
    tracks = _items(results, "tracks")
    artists = _items(results, "artists")
    albums = _items(results, "albums")
    playlists = _items(results, "playlists")
    for artist in artists:
        if _name_eq(artist, q):
            return "artist", artist
    for playlist in playlists:
        if _name_eq(playlist, q):
            return "playlist", playlist
    for album in albums:
        if _name_eq(album, q):
            return "album", album
    if "playlist" in q and playlists:
        return "playlist", playlists[0]
    if "album" in q and albums:
        return "album", albums[0]
    if artists and _name_eq(artists[0], q):
        return "artist", artists[0]
    if tracks:
        return "track", tracks[0]
    if artists:
        return "artist", artists[0]
    if albums:
        return "album", albums[0]
    if playlists:
        return "playlist", playlists[0]
    return None


def _play_saved_tracks(settings: Settings) -> str:
    saved = _api(settings, "GET", "/me/tracks", params={"limit": "50"}) or {}
    uris = [item["track"]["uri"] for item in saved.get("items") or [] if item.get("track", {}).get("uri")]
    if not uris:
        return "I couldn't find liked songs on this Spotify account."
    _play(settings, {"uris": uris})
    return "Playing your liked songs."


def _need_connect() -> str:
    from opus.runtime import is_phone

    where = "settings" if is_phone() else "my panel"
    return f"Connect Spotify in {where} first — I'll control it through Spotify's API."


def _need_connect_short() -> str:
    from opus.runtime import is_phone

    where = "settings" if is_phone() else "my panel"
    return f"Connect Spotify in {where} first."


def play(settings: Settings, query: str = "") -> str:
    if not connected(settings):
        return _need_connect()
    query = (query or "").strip()
    if not query:
        return resume(settings)
    lowered = query.lower()
    if lowered in {"liked songs", "liked", "likes", "my liked songs", "my likes", "like songs"}:
        try:
            return _play_saved_tracks(settings)
        except PermissionError as exc:
            return str(exc)
        except RuntimeError as exc:
            return str(exc)
        except Exception:
            log.exception("spotify liked songs failed")
            return "Couldn't play liked songs."
    try:
        results = _search(settings, query)
        picked = _pick_target(query, results)
        if not picked:
            return f"I couldn't find {query} on Spotify."
        kind, item = picked
        uri = item.get("uri") or ""
        name = item.get("name") or query
        if kind == "track":
            _play(settings, {"uris": [uri]})
        else:
            _play(settings, {"context_uri": uri})
        if kind == "artist":
            return f"Playing {name}."
        if kind == "playlist":
            return f"Playing the playlist {name}."
        if kind == "album":
            return f"Playing the album {name}."
        artist = ""
        artists = item.get("artists") or []
        if artists:
            artist = artists[0].get("name") or ""
        return f"Playing {name} by {artist}." if artist else f"Playing {name}."
    except PermissionError as exc:
        return str(exc)
    except RuntimeError as exc:
        return str(exc)
    except Exception:
        log.exception("spotify play failed")
        return "Spotify didn't take that command."


def resume(settings: Settings) -> str:
    if not connected(settings):
        return _need_connect()
    try:
        device_id = _active_device_id(settings)
        if not device_id:
            return "I opened Spotify, but it isn't ready as a playback device yet. Try once more."
        _api(settings, "PUT", f"/me/player/play?{urlencode({'device_id': device_id})}", json={})
        return "Playing Spotify."
    except PermissionError as exc:
        return str(exc)
    except Exception:
        log.exception("spotify resume failed")
        return "Couldn't resume Spotify."


def pause(settings: Settings) -> str:
    if not connected(settings):
        return _need_connect_short()
    try:
        _api(settings, "PUT", "/me/player/pause")
        return "Paused Spotify."
    except PermissionError as exc:
        return str(exc)
    except Exception:
        log.exception("spotify pause failed")
        return "Couldn't pause Spotify."


def skip(settings: Settings) -> str:
    if not connected(settings):
        return _need_connect_short()
    try:
        _api(settings, "POST", "/me/player/next")
        return "Skipped."
    except PermissionError as exc:
        return str(exc)
    except Exception:
        log.exception("spotify skip failed")
        return "Couldn't skip."


def previous(settings: Settings) -> str:
    if not connected(settings):
        return _need_connect_short()
    try:
        _api(settings, "POST", "/me/player/previous")
        return "Previous track."
    except PermissionError as exc:
        return str(exc)
    except Exception:
        log.exception("spotify previous failed")
        return "Couldn't go back."


def now_playing(settings: Settings) -> str:
    if not connected(settings):
        return _need_connect_short()
    try:
        playback = _api(settings, "GET", "/me/player/currently-playing")
        if not playback or playback.get("_empty") or not playback.get("item"):
            return "Nothing is playing on Spotify."
        item = playback["item"]
        name = item.get("name") or "a track"
        artists = ", ".join(a.get("name") or "" for a in item.get("artists") or [] if a.get("name"))
        return f"{name} by {artists}." if artists else name
    except Exception:
        log.exception("spotify now playing failed")
        return "I couldn't see what's playing."


def control(settings: Settings, action: str, query: str = "") -> str:
    action = (action or "play").strip().lower()
    if action in {"play", "resume", "start"}:
        return play(settings, query) if query else resume(settings)
    if action in {"pause", "stop"}:
        return pause(settings)
    if action in {"skip", "next"}:
        return skip(settings)
    if action in {"previous", "prev", "back"}:
        return previous(settings)
    if action in {"status", "now", "what"}:
        return now_playing(settings)
    return play(settings, query or action)
