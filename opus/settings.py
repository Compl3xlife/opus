from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from threading import Lock

APP_NAME = "Opus"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5840

DEFAULTS: dict = {
    "wake_word": "opus",
    "voice_responses": True,
    "volume": 80,
    "input_device": None,
    "output_device": None,
    "desktop_audio_device": "",
    "game_audio_device": "",
    "call_audio_device": "",
    "openai_api_key": "",
    "openai_api_keys": [],
    "openai_base_url": "https://api.openai.com/v1",
    "fallback_openai_api_key": "",
    "fallback_openai_api_keys": [],
    "fallback_openai_base_url": "",
    "fallback_model": "",
    "fallback_vision_model": "",
    "api_key_cooldown_seconds": 60,
    "model": "gpt-4o-mini",
    "vision_model": "gpt-4o",
    "stt_model": "whisper-large-v3-turbo",
    "tts_voice": "opus-ultron",
    "ultron_voice_applied": False,
    "use_cloned_voice": False,
    "voice_rate": "-8%",
    "voice_pitch": "-8Hz",
    "voice_depth": 0.96,
    "voice_style": "ultron",
    "voice_preset_version": 2,
    "clone_ref_text": "",
    "clone_ref_name": "",
    "screen_awareness": True,
    "file_access": True,
    "download_scanning": True,
    "background_protection": True,
    "default_browser": "opera-gx",
    "clip_buffer": True,
    "game_play_enabled": True,
    "game_max_steps": 250,
    "game_step_delay_ms": 350,
    "chess_movetime_ms": 4000,
    "chess_sac_window_cp": 0,
    "chess_memory_min_depth": 14,
    "chess_think_min_s": 1.2,
    "chess_think_max_s": 14.0,
    "location_city": "",
    "location_lat": None,
    "location_lon": None,
    "location_label": "",
    "host": DEFAULT_HOST,
    "port": DEFAULT_PORT,
    "discord_enabled": False,
    "discord_bot_token": "",
    "discord_prefix": "opus",
    "discord_allowed_guilds": "",
    "discord_allowed_channels": "",
    "spotify_client_id": "",
    "spotify_client_secret": "",
    "spotify_access_token": "",
    "spotify_refresh_token": "",
    "spotify_token_expires_at": 0,
    "spotify_user_name": "",
    "spotify_user_id": "",
    "network_policy_enabled": True,
    "network_inbound_localhost_only": True,
    "network_inbound_token": "",
    "network_inbound_allow_hosts": "",
    "network_outbound_allowlist": "",
    "network_outbound_web_read": True,
}


def appdata_dir() -> Path:
    from opus.runtime import is_phone

    override = (os.environ.get("OPUS_APPDATA") or "").strip()
    if override:
        root = Path(override)
        root.mkdir(parents=True, exist_ok=True)
        return root
    if is_phone():
        if os.name == "nt":
            root = Path.home() / "AppData" / "Roaming" / "OpusPhone"
        else:
            root = Path.home() / ".opus-phone"
        root.mkdir(parents=True, exist_ok=True)
        return root
    if os.name == "nt":
        root = Path.home() / "AppData" / "Roaming" / APP_NAME
    else:
        xdg = (os.environ.get("XDG_CONFIG_HOME") or "").strip()
        root = Path(xdg) / APP_NAME.lower() if xdg else Path.home() / ".config" / APP_NAME.lower()
    root.mkdir(parents=True, exist_ok=True)
    return root


def settings_path() -> Path:
    return appdata_dir() / "settings.json"


def opus_media_root() -> Path:
    path = Path("D:/Clips/Opus")
    path.mkdir(parents=True, exist_ok=True)
    return path


def recordings_dir() -> Path:
    path = opus_media_root() / "Recordings"
    path.mkdir(parents=True, exist_ok=True)
    return path


def clips_dir() -> Path:
    path = opus_media_root() / "Clips"
    path.mkdir(parents=True, exist_ok=True)
    return path


def voices_dir() -> Path:
    path = opus_media_root() / "Voices"
    path.mkdir(parents=True, exist_ok=True)
    return path


def scripts_dir() -> Path:
    path = Path("D:/Opus/Scripts")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _apply_runtime_defaults(merged: dict, loaded: dict | None) -> dict:
    from opus.runtime import is_phone

    if not is_phone():
        return merged
    merged["game_play_enabled"] = False
    merged["background_protection"] = False
    merged["screen_awareness"] = False
    merged["clip_buffer"] = False
    if not loaded or "host" not in loaded:
        merged["host"] = "0.0.0.0"
    if not loaded or "port" not in loaded:
        merged["port"] = 5841
    merged["network_inbound_localhost_only"] = False
    return merged


class Settings:
    def __init__(self) -> None:
        self._lock = Lock()
        self._data = deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        path = settings_path()
        if not path.exists():
            with self._lock:
                self._data = _apply_runtime_defaults(deepcopy(DEFAULTS), None)
            self.save()
            return
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        should_persist = False
        with self._lock:
            merged = deepcopy(DEFAULTS)
            merged.update({k: v for k, v in loaded.items() if k in DEFAULTS})
            if "background_protection" not in loaded and "download_scanning" in loaded:
                merged["background_protection"] = loaded["download_scanning"]
            if not loaded.get("voice_style"):
                merged["tts_voice"] = "opus-ultron"
                merged["voice_style"] = "ultron"
                merged["voice_rate"] = "-8%"
                merged["voice_pitch"] = "-8Hz"
                merged["voice_depth"] = 0.96
                merged["voice_preset_version"] = 2
                merged["use_cloned_voice"] = False
                merged["ultron_voice_applied"] = True
                self._data = merged
                should_persist = True
            elif merged.get("tts_voice") in {"en-US-AndrewNeural", "en-US-GuyNeural"} and not loaded.get("ultron_voice_applied"):
                merged["tts_voice"] = "opus-ultron"
                merged["voice_style"] = "ultron"
                merged["voice_rate"] = "-8%"
                merged["voice_pitch"] = "-8Hz"
                merged["voice_depth"] = 0.96
                merged["voice_preset_version"] = 2
                merged["use_cloned_voice"] = False
                merged["ultron_voice_applied"] = True
                self._data = merged
                should_persist = True
            elif int(loaded.get("voice_preset_version") or 0) < 2 and merged.get("tts_voice") == "opus-ultron":
                merged["voice_rate"] = "-8%"
                merged["voice_pitch"] = "-8Hz"
                merged["voice_depth"] = 0.96
                merged["voice_preset_version"] = 2
                self._data = merged
                should_persist = True
            else:
                self._data = merged
            self._data = _apply_runtime_defaults(self._data, loaded)
        if should_persist:
            self.save()

    def save(self) -> None:
        with self._lock:
            payload = json.dumps(self._data, indent=2)
        settings_path().write_text(payload, encoding="utf-8")

    def snapshot(self) -> dict:
        with self._lock:
            return deepcopy(self._data)

    def get(self, key: str):
        with self._lock:
            return deepcopy(self._data.get(key, DEFAULTS.get(key)))

    def update(self, patch: dict) -> dict:
        with self._lock:
            for key, value in patch.items():
                if key in DEFAULTS:
                    self._data[key] = value
            data = deepcopy(self._data)
        self.save()
        return data
