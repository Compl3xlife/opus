from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.requests import Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from opus.audio.clone import browse_audio_file, get_cloner
from opus.audio import list_devices
from opus.audio.tts import VOICES
from opus.hub import hub
from opus.net_policy import get_policy
from opus.settings import Settings, voices_dir
from opus.tools.defender import scan_path
from opus.tools.downloader import download_video, get_status as dl_status, open_downloads_folder
from opus.tools.record import open_clips_folder, recorder

UI_DIR = Path(__file__).resolve().parent / "ui"


class AskBody(BaseModel):
    text: str


class ScanBody(BaseModel):
    path: str


def create_app(settings: Settings, controller) -> FastAPI:
    app = FastAPI(title="Opus")
    app.mount("/static", StaticFiles(directory=UI_DIR), name="static")

    @app.middleware("http")
    async def inbound_network_gate(request: Request, call_next):
        client_host = request.client.host if request.client else ""
        if not get_policy().inbound_allowed(client_host, request.headers):
            return JSONResponse(
                {"error": "Inbound connection blocked by Opus network policy."},
                status_code=403,
            )
        return await call_next(request)

    async def _accept_socket(socket: WebSocket) -> bool:
        client_host = socket.client.host if socket.client else ""
        if not get_policy().inbound_allowed(client_host, socket.headers):
            from opus.logutil import get_logger

            get_logger().warning("websocket rejected by network policy host=%s", client_host)
            await socket.close(code=1008)
            return False
        await socket.accept()
        return True

    @app.get("/")
    def panel():
        return FileResponse(UI_DIR / "panel.html")

    @app.get("/api/port")
    def get_port():
        return {"port": int(settings.get("port") or 5840)}

    @app.get("/api/settings")
    def get_settings():
        data = settings.snapshot()
        if data.get("openai_api_key"):
            data["openai_api_key_set"] = True
            data["openai_api_key"] = ""
        else:
            data["openai_api_key_set"] = False
        if data.get("discord_bot_token"):
            data["discord_bot_token_set"] = True
            data["discord_bot_token"] = ""
        else:
            data["discord_bot_token_set"] = False
        data["discord_invite_url"] = ""
        if data.get("discord_bot_token_set"):
            from opus.discord_bot import discord_invite_url

            data["discord_invite_url"] = discord_invite_url(
                token=settings.get("discord_bot_token") or ""
            )
        data["spotify_client_secret"] = ""
        data["spotify_access_token"] = ""
        data["spotify_refresh_token"] = ""
        data["spotify_token_expires_at"] = 0
        from opus.apps.spotify import connection_status

        spotify = connection_status(settings)
        data["spotify_connected"] = bool(spotify.get("connected"))
        data["spotify_account"] = spotify.get("account") or ""
        data["spotify_redirect_uri"] = spotify.get("redirect_uri") or ""
        data["spotify_needs_reconnect"] = bool(spotify.get("needs_reconnect"))
        return data

    @app.put("/api/settings")
    def put_settings(patch: dict[str, Any] = Body(...)):
        cleaned = dict(patch)
        if cleaned.get("openai_api_key") == "":
            cleaned.pop("openai_api_key", None)
        if cleaned.get("discord_bot_token") == "":
            cleaned.pop("discord_bot_token", None)
        if cleaned.get("spotify_client_secret") == "":
            cleaned.pop("spotify_client_secret", None)
        for token_key in ("spotify_access_token", "spotify_refresh_token", "spotify_token_expires_at"):
            cleaned.pop(token_key, None)
        updated = settings.update(cleaned)
        controller.apply_settings()
        updated["openai_api_key"] = ""
        updated["openai_api_key_set"] = bool(settings.get("openai_api_key"))
        updated["discord_bot_token"] = ""
        updated["discord_bot_token_set"] = bool(settings.get("discord_bot_token"))
        updated["discord_invite_url"] = ""
        if updated["discord_bot_token_set"]:
            from opus.discord_bot import discord_invite_url

            updated["discord_invite_url"] = discord_invite_url(
                token=settings.get("discord_bot_token") or ""
            )
        updated["spotify_client_secret"] = ""
        updated["spotify_access_token"] = ""
        updated["spotify_refresh_token"] = ""
        from opus.apps.spotify import connection_status

        spotify = connection_status(settings)
        updated["spotify_connected"] = bool(spotify.get("connected"))
        updated["spotify_account"] = spotify.get("account") or ""
        updated["spotify_redirect_uri"] = spotify.get("redirect_uri") or ""
        updated["spotify_needs_reconnect"] = bool(spotify.get("needs_reconnect"))
        return updated

    @app.get("/api/apps")
    def list_apps():
        from opus.apps import list_connections

        return {"apps": list_connections(settings)}

    @app.post("/api/apps/spotify/connect")
    def spotify_connect():
        from opus.apps.spotify import start_login

        return start_login(settings, open_browser=True)

    @app.post("/api/apps/spotify/disconnect")
    def spotify_disconnect():
        from opus.apps.spotify import disconnect

        return disconnect(settings)

    @app.get("/api/apps/spotify/callback")
    def spotify_callback(code: str = "", state: str = "", error: str = ""):
        from opus.apps.spotify import finish_login

        if error:
            message = "Spotify login was cancelled."
            ok = False
        else:
            result = finish_login(settings, code=code, state=state)
            ok = bool(result.get("ok"))
            message = "Spotify is connected. You can close this tab." if ok else (result.get("error") or "Spotify login failed.")
        color = "#7dffb1" if ok else "#ffb4b4"
        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Opus + Spotify</title>
<style>
body {{ margin:0; min-height:100vh; display:grid; place-items:center;
  background:#07090a; color:#e8f1ed; font:16px/1.45 Segoe UI, sans-serif; }}
.card {{ padding:28px 32px; border:1px solid rgba(119,255,170,.22); border-radius:16px; max-width:420px; }}
h1 {{ margin:0 0 8px; font-size:22px; color:{color}; }}
p {{ margin:0; color:#9fb6ab; }}
</style></head>
<body><div class="card"><h1>{"Connected" if ok else "Not connected"}</h1><p>{message}</p></div>
<script>setTimeout(function(){{ window.close(); }}, 1600);</script>
</body></html>"""
        return HTMLResponse(html)

    @app.post("/api/apps/spotify/play")
    def spotify_play(body: dict[str, Any] = Body(default={})):
        from opus.apps.spotify import play as spotify_play_api

        query = str((body or {}).get("query") or "")
        result = spotify_play_api(settings, query)
        return {"result": result}

    @app.post("/api/apps/spotify/control")
    def spotify_control(body: dict[str, Any] = Body(default={})):
        from opus.apps.spotify import control as spotify_control_api

        action = str((body or {}).get("action") or "pause")
        query = str((body or {}).get("query") or "")
        result = spotify_control_api(settings, action, query)
        return {"result": result}

    @app.get("/api/devices")
    def devices():
        return list_devices()

    @app.get("/api/voices")
    def voices():
        return VOICES

    @app.get("/api/voice/status")
    def voice_status():
        return get_cloner(settings).status()

    @app.post("/api/voice/browse")
    def voice_browse():
        chosen = browse_audio_file()
        if not chosen:
            return {"result": "No file selected.", "status": get_cloner(settings).status()}
        result = get_cloner(settings).import_file(chosen)
        hub.add_message("opus", result)
        return {"result": result, "status": get_cloner(settings).status()}

    @app.post("/api/voice/upload")
    async def voice_upload(file: UploadFile = File(...)):
        name = Path(file.filename or "sample.wav").name
        dest = voices_dir() / name
        dest.write_bytes(await file.read())
        result = get_cloner(settings).import_file(dest)
        hub.add_message("opus", result)
        return {"result": result, "status": get_cloner(settings).status()}

    @app.post("/api/voice/preview")
    def voice_preview():
        if not get_cloner(settings).status().get("ready"):
            return {"result": "Send a sound file first."}
        settings.update({"use_cloned_voice": True})
        threading.Thread(
            target=controller.speaker.say,
            args=("This is the cloned Opus voice.",),
            daemon=True,
        ).start()
        return {"result": "Playing preview.", "status": get_cloner(settings).status()}

    @app.get("/api/status")
    def status():
        return {
            "status": hub.status,
            "conversation": hub.recent_conversation(),
            "browser": hub.get_browser_context(),
        }

    @app.post("/api/panel/show")
    def show_panel():
        controller.show_panel()
        return {"ok": True}

    @app.post("/api/panel/hide")
    def hide_panel():
        controller.hide_panel()
        return {"ok": True}

    @app.post("/api/ask")
    def ask(body: AskBody):
        controller.handle_text(body.text, require_wake=False)
        return {"ok": True}

    @app.post("/api/listen")
    def force_listen():
        hub.set_status(speaking=False, mode="followup", message="I'm listening.")
        controller.listener.arm_followup(25)
        return {"ok": True}

    @app.post("/api/wake")
    def wake_up():
        controller.listener.set_enabled(True)
        controller.tray.set_active(True)
        hub.set_status(listening=True, mode="listening", message="I'm back.")
        return {"ok": True}

    @app.post("/api/record/start")
    def record_start():
        result = recorder.start()
        hub.add_message("opus", result)
        return {"result": result}

    @app.post("/api/record/stop")
    def record_stop():
        result = recorder.stop()
        hub.add_message("opus", result)
        return {"result": result}

    @app.post("/api/clip")
    def clip():
        result = recorder.clip()
        hub.add_message("opus", result)
        return {"result": result}

    @app.post("/api/screenshot")
    def shot():
        result = recorder.screenshot()
        hub.add_message("opus", result)
        return {"result": result}

    @app.post("/api/clips/open")
    def clips_open():
        return {"result": open_clips_folder()}

    @app.post("/api/scan")
    def scan(body: ScanBody):
        result = scan_path(body.path)
        hub.add_message("opus", result)
        return {"result": result}

    @app.post("/api/download")
    def start_download(body: dict[str, Any] = Body(...)):
        url = (body.get("url") or "").strip()
        if not url:
            return {"error": "No URL provided."}
        fmt = body.get("format", "video")
        result = download_video(url, format_type=fmt, settings=settings)
        return {"result": result}

    @app.get("/api/download/status")
    def download_status():
        return dl_status()

    @app.post("/api/download/open")
    def download_open():
        return {"result": open_downloads_folder()}

    @app.websocket("/ws/ui")
    async def ws_ui(socket: WebSocket):
        if not await _accept_socket(socket):
            return
        hub.ui_clients.add(socket)
        await socket.send_json({"type": "status", "data": hub.status})
        try:
            while True:
                await socket.receive_text()
        except WebSocketDisconnect:
            hub.ui_clients.discard(socket)

    @app.websocket("/ws/browser")
    async def ws_browser(socket: WebSocket):
        if not await _accept_socket(socket):
            return
        hub.register_browser_client(socket)
        try:
            while True:
                message = await socket.receive_json()
                if not isinstance(message, dict):
                    continue
                if message.get("request_id") and (
                    "ok" in message or "error" in message or "result" in message
                ):
                    hub.resolve_browser_response(str(message.get("request_id")), message)
                    continue
                if message.get("browser"):
                    hub.note_browser_client(socket, str(message.get("browser")))
                hub.set_browser_context(message)
                if message.get("download_path"):
                    controller.guardian.scan_now(message["download_path"])
        except WebSocketDisconnect:
            hub.forget_browser_client(socket)

    return app
