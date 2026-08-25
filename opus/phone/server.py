from __future__ import annotations

from typing import Any

from fastapi import Body, FastAPI, File, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.requests import Request

from opus.audio.stt import transcribe_audio_bytes
from opus.hub import hub
from opus.logutil import friendly_error, get_logger
from opus.net_policy import get_policy
from opus.phone.util import UI_DIR, public_host_from_request
from opus.settings import Settings

log = get_logger()


class AskBody(BaseModel):
    text: str


def _mask_settings(data: dict, settings: Settings, *, public_host: str | None = None) -> dict:
    out = dict(data)
    out["openai_api_key_set"] = bool(settings.get("openai_api_key"))
    out["openai_api_key"] = ""
    out["discord_bot_token_set"] = bool(settings.get("discord_bot_token"))
    out["discord_bot_token"] = ""
    out["spotify_client_secret"] = ""
    out["spotify_access_token"] = ""
    out["spotify_refresh_token"] = ""
    out["spotify_token_expires_at"] = 0
    from opus.apps.spotify import connection_status

    spotify = connection_status(settings, public_host=public_host)
    out["spotify_connected"] = bool(spotify.get("connected"))
    out["spotify_account"] = spotify.get("account") or ""
    out["spotify_redirect_uri"] = spotify.get("redirect_uri") or ""
    out["runtime"] = "phone"
    return out


def create_app(settings: Settings, controller) -> FastAPI:
    app = FastAPI(title="Opus Phone")
    static_dir = UI_DIR / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.middleware("http")
    async def inbound_network_gate(request: Request, call_next):
        client_host = request.client.host if request.client else ""
        if not get_policy().inbound_allowed(client_host, request.headers):
            return JSONResponse(
                {"error": "Inbound connection blocked by Opus network policy."},
                status_code=403,
            )
        return await call_next(request)

    @app.get("/")
    def home():
        return FileResponse(UI_DIR / "index.html")

    @app.get("/callback.html")
    def spotify_pwa_callback():
        return FileResponse(UI_DIR / "callback.html")

    @app.get("/.nojekyll")
    def nojekyll():
        return FileResponse(UI_DIR / ".nojekyll")

    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(UI_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js")
    def service_worker():
        return FileResponse(UI_DIR / "sw.js", media_type="text/javascript")

    @app.get("/api/port")
    def get_port():
        return {"port": int(settings.get("port") or 5841), "runtime": "phone"}

    @app.get("/api/settings")
    def get_settings(request: Request):
        return _mask_settings(settings.snapshot(), settings, public_host=public_host_from_request(request))

    @app.put("/api/settings")
    def put_settings(request: Request, patch: dict[str, Any] = Body(...)):
        cleaned = dict(patch)
        if cleaned.get("openai_api_key") == "":
            cleaned.pop("openai_api_key", None)
        if cleaned.get("spotify_client_secret") == "":
            cleaned.pop("spotify_client_secret", None)
        for token_key in ("spotify_access_token", "spotify_refresh_token", "spotify_token_expires_at"):
            cleaned.pop(token_key, None)
        cleaned.pop("discord_bot_token", None)
        updated = settings.update(cleaned)
        controller.apply_settings()
        return _mask_settings(updated, settings, public_host=public_host_from_request(request))

    @app.get("/api/apps")
    def list_apps(request: Request):
        from opus.apps.spotify import connection_status

        return {"apps": [connection_status(settings, public_host=public_host_from_request(request))]}

    @app.post("/api/apps/spotify/connect")
    def spotify_connect(request: Request):
        from opus.apps.spotify import start_login

        return start_login(
            settings,
            open_browser=False,
            public_host=public_host_from_request(request),
        )

    @app.post("/api/apps/spotify/disconnect")
    def spotify_disconnect(request: Request):
        from opus.apps.spotify import disconnect, redirect_uri

        status = disconnect(settings)
        status["redirect_uri"] = redirect_uri(settings, public_host=public_host_from_request(request))
        return status

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
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Opus + Spotify</title>
<style>
body {{ margin:0; min-height:100vh; display:grid; place-items:center;
  background:#07090a; color:#e8f1ed; font:16px/1.45 Segoe UI, sans-serif; }}
.card {{ padding:28px 32px; border:1px solid rgba(119,255,170,.22); border-radius:16px; max-width:420px; }}
h1 {{ margin:0 0 8px; font-size:22px; color:{color}; }}
p {{ margin:0; color:#9fb6ab; }}
</style></head>
<body><div class="card"><h1>{"Connected" if ok else "Not connected"}</h1><p>{message}</p></div>
</body></html>"""
        return HTMLResponse(html)

    @app.post("/api/apps/spotify/play")
    def spotify_play(body: dict[str, Any] = Body(default={})):
        from opus.apps.spotify import play as spotify_play_api

        query = str((body or {}).get("query") or "")
        return {"result": spotify_play_api(settings, query)}

    @app.post("/api/apps/spotify/control")
    def spotify_control(body: dict[str, Any] = Body(default={})):
        from opus.apps.spotify import control as spotify_control_api

        action = str((body or {}).get("action") or "pause")
        query = str((body or {}).get("query") or "")
        return {"result": spotify_control_api(settings, action, query)}

    @app.get("/api/status")
    def status():
        return {
            "status": hub.status,
            "conversation": hub.recent_conversation(),
            "runtime": "phone",
        }

    @app.post("/api/ask")
    def ask(body: AskBody):
        reply = controller.handle_text_sync(body.text)
        return {"ok": True, "reply": reply}

    @app.post("/api/stt")
    async def stt(file: UploadFile = File(...)):
        data = await file.read()
        try:
            text = transcribe_audio_bytes(settings, data, filename=file.filename or "utterance.webm")
        except Exception as exc:
            log.exception("phone stt failed")
            return JSONResponse({"text": "", "error": friendly_error(exc)}, status_code=502)
        return {"text": text}

    return app
