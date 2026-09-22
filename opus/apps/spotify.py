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
READ_SCOPES = ("user-library-read", "playlist-read-private", "playlist-read-collaborative")
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


def _scope_set(settings: Settings) -> set[str]:
    return {part for part in str(settings.get("spotify_scope") or "").split() if part}


def _missing_scopes(settings: Settings, needed: tuple[str, ...] = READ_SCOPES) -> list[str]:
    have = _scope_set(settings)
    if not have:
        return []
    return [scope for scope in needed if scope not in have]


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
        "needs_reconnect": bool(connected and _missing_scopes(settings)),
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
        "show_dialog": "true",
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
            "spotify_scope": "",
            "spotify_product": "",
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
                "spotify_product": me.get("product") or "",
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
    if payload.get("scope") is not None:
        patch["spotify_scope"] = str(payload.get("scope") or "")
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


def _error_fields(payload: Any) -> tuple[str, str]:
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        return str(err.get("reason") or "").strip(), str(err.get("message") or "").strip()
    return "", str(err or payload or "").strip()


def _permission_from_403(settings: Settings, method: str, path: str, payload: Any) -> PermissionError:
    reason, message = _error_fields(payload)
    blob = f"{reason} {message}".lower()
    log.warning("spotify 403 %s %s reason=%s message=%s", method, path, reason, message)
    if method.upper() in {"GET", "HEAD"}:
        if "scope" in blob:
            return PermissionError(
                "Spotify is missing playlist permission. Disconnect and connect Spotify again in my panel."
            )
        return PermissionError(
            "Spotify wouldn't share that playlist. Disconnect and connect Spotify again in my panel, then try once more."
        )
    if "premium" in blob or reason.upper() == "PREMIUM_REQUIRED":
        if str(settings.get("spotify_product") or "").lower() == "premium":
            return PermissionError(
                "Spotify refused playback on that device. Open the Spotify app on this PC, then try again."
            )
        return PermissionError("Spotify Premium is required for playback control.")
    if "device" in blob or "restriction" in blob:
        return PermissionError("Spotify isn't ready as a playback device yet. Open Spotify, then try again.")
    return PermissionError("Spotify blocked playback. Open Spotify on this PC, then try once more.")


def _api(
    settings: Settings,
    method: str,
    path: str,
    *,
    json: dict | None = None,
    params: dict | None = None,
    forbidden_ok: bool = False,
) -> Any:
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
        if forbidden_ok:
            log.warning("spotify 403 %s %s %s", method, path, payload)
            return {"_forbidden": True, "_error": payload}
        raise _permission_from_403(settings, method, path, payload)
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


def _spotify_user_id(settings: Settings) -> str:
    uid = str(settings.get("spotify_user_id") or "").strip()
    if uid:
        return uid
    try:
        me = _api(settings, "GET", "/me") or {}
    except Exception:
        return ""
    uid = str(me.get("id") or "").strip()
    if uid:
        settings.update({"spotify_user_id": uid, "spotify_product": me.get("product") or settings.get("spotify_product") or ""})
    return uid


def _paging_items(payload: dict) -> list:
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if isinstance(items, dict):
        inner = items.get("items")
        return inner if isinstance(inner, list) else []
    if isinstance(items, list):
        return items
    tracks = payload.get("tracks")
    if isinstance(tracks, dict):
        inner = tracks.get("items")
        if isinstance(inner, dict):
            inner = inner.get("items")
        return inner if isinstance(inner, list) else []
    return []


def _paging_total(payload: dict) -> int:
    if not isinstance(payload, dict):
        return 0
    for key in ("items", "tracks"):
        block = payload.get(key)
        if isinstance(block, dict) and block.get("total") is not None:
            try:
                return int(block.get("total") or 0)
            except (TypeError, ValueError):
                pass
    try:
        return int(payload.get("total") or 0)
    except (TypeError, ValueError):
        return 0


def _playlist_entry(item: dict) -> dict | None:
    if not item or not item.get("id"):
        return None
    return {
        "id": str(item.get("id") or ""),
        "name": str(item.get("name") or "Playlist")[:100],
        "tracks": _paging_total(item),
    }


def list_playlists(settings: Settings, *, limit: int = 50) -> list[dict]:
    if not connected(settings):
        raise PermissionError(_need_connect())
    cap = max(1, min(50, int(limit)))
    paths = ["/me/playlists"]
    user_id = _spotify_user_id(settings)
    if user_id:
        paths.append(f"/users/{user_id}/playlists")
    playlists: list[dict] = []
    seen: set[str] = set()
    for path in paths:
        offset = 0
        while len(playlists) < cap and offset < 200:
            payload = _api(
                settings,
                "GET",
                path,
                params={
                    "limit": "50",
                    "offset": str(offset),
                },
                forbidden_ok=True,
            ) or {}
            if payload.get("_forbidden") or payload.get("_empty"):
                break
            items = payload.get("items") or []
            for item in items:
                entry = _playlist_entry(item) if isinstance(item, dict) else None
                if not entry or entry["id"] in seen:
                    continue
                seen.add(entry["id"])
                playlists.append(entry)
                if len(playlists) >= cap:
                    break
            if not items or not payload.get("next"):
                break
            offset += max(len(items), 1)
        if playlists:
            break
    return playlists


def liked_track_total(settings: Settings) -> int:
    if not connected(settings):
        return 0
    payload = _api(
        settings,
        "GET",
        "/me/tracks",
        params={"limit": "1", "market": "from_token"},
        forbidden_ok=True,
    ) or {}
    if payload.get("_forbidden"):
        return 0
    return int(payload.get("total") or 0)


def _media_from_row(row: dict | None) -> dict | None:
    if not isinstance(row, dict):
        return None
    media = row.get("item") or row.get("track") or row.get("episode")
    if not isinstance(media, dict):
        if row.get("type") in {"track", "episode"} and row.get("name"):
            media = row
        else:
            return None
    if media.get("is_local") or row.get("is_local"):
        return None
    name = str(media.get("name") or "").strip()
    if not name:
        return None
    episode = media.get("type") == "episode" or media.get("episode") is True
    if episode and not media.get("artists"):
        show = media.get("show") if isinstance(media.get("show"), dict) else {}
        artists = str(show.get("name") or "")
    else:
        artists = ", ".join(
            str(artist.get("name") or "")
            for artist in (media.get("artists") or [])
            if artist and artist.get("name")
        )
    return {
        "name": name[:100],
        "artists": artists[:100],
        "query": f"{artists} - {name}" if artists else name,
    }


def _playlist_rows(settings: Settings, playlist_id: str, cap: int) -> list[dict]:
    attempts = (
        (f"/playlists/{playlist_id}/items", {"limit": str(min(50, cap))}),
        (f"/playlists/{playlist_id}/items", {"limit": str(min(50, cap)), "additional_types": "track,episode"}),
        (f"/playlists/{playlist_id}", {}),
        (f"/playlists/{playlist_id}/tracks", {"limit": str(min(50, cap)), "additional_types": "track,episode"}),
        (f"/playlists/{playlist_id}/tracks", {"limit": str(min(50, cap)), "market": "from_token"}),
    )
    for path, params in attempts:
        payload = _api(settings, "GET", path, params=params, forbidden_ok=True) or {}
        if payload.get("_forbidden") or payload.get("_empty"):
            continue
        rows = [row for row in _paging_items(payload) if isinstance(row, dict)]
        if rows:
            return rows
        log.warning(
            "spotify playlist empty path=%s total=%s keys=%s",
            path,
            _paging_total(payload),
            list(payload.keys())[:12],
        )
    return []


def playlist_track_details(settings: Settings, playlist_id: str, *, limit: int = 25) -> list[dict]:
    if not connected(settings):
        raise PermissionError(_need_connect())
    cap = max(1, min(50, int(limit)))
    playlist_id = (playlist_id or "").strip()
    if playlist_id == "liked":
        payload = _api(
            settings,
            "GET",
            "/me/tracks",
            params={"limit": str(cap), "market": "from_token"},
        ) or {}
        rows = _paging_items(payload)
    else:
        rows = _playlist_rows(settings, playlist_id, cap)
    details: list[dict] = []
    for row in rows:
        parsed = _media_from_row(row if isinstance(row, dict) else None)
        if not parsed:
            continue
        details.append(parsed)
        if len(details) >= cap:
            break
    return details


def playlist_track_queries(settings: Settings, playlist_id: str, *, limit: int = 25) -> list[str]:
    return [
        item["query"]
        for item in playlist_track_details(settings, playlist_id, limit=limit)
        if item.get("query")
    ]


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
