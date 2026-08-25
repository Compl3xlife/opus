from __future__ import annotations

import ctypes
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import imageio_ffmpeg
import sounddevice as sd

from opus.hub import hub
from opus.logutil import get_logger
from opus.settings import Settings
from opus.settings import appdata_dir, clips_dir, opus_media_root, recordings_dir
from opus.tools.screen import screenshot_png

log = get_logger()

CREATE_NO_WINDOW = 0x08000000

_EVEN_PAD = "pad=ceil(iw/2)*2:ceil(ih/2)*2"
user32 = ctypes.windll.user32
VK_LWIN, VK_MENU, VK_R, VK_G, VK_F10 = 0x5B, 0x12, 0x52, 0x47, 0x79
KEYEVENTF_KEYUP = 0x0002


def _ffmpeg() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def _popen(args: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=CREATE_NO_WINDOW,
    )


def _stop_proc(proc: subprocess.Popen | None) -> None:
    if not proc:
        return
    try:
        if proc.stdin:
            proc.stdin.write(b"q")
            proc.stdin.flush()
    except Exception:
        proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()


class Recorder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buffer: subprocess.Popen | None = None
        self._session: subprocess.Popen | None = None
        self._session_path: Path | None = None
        self._buffer_dir = appdata_dir() / "buffer"
        self._settings: Settings | None = None
        self._last_audio_tracks: list[str] = []
        self._wasapi_supported: bool | None = None

    def set_settings(self, settings: Settings) -> None:
        self._settings = settings

    def status(self) -> dict:
        return {
            "buffering": bool(self._buffer and self._buffer.poll() is None),
            "recording": bool(self._session and self._session.poll() is None),
            "clips_dir": str(clips_dir()),
            "recordings_dir": str(recordings_dir()),
            "current_file": str(self._session_path) if self._session_path else "",
            "audio_tracks": list(self._last_audio_tracks),
        }

    def _settings_value(self, key: str, fallback=""):
        if not self._settings:
            return fallback
        try:
            return self._settings.get(key)
        except Exception:
            return fallback

    def _capture_inputs(self) -> list[dict]:
        devices = sd.query_devices()
        return [
            {"index": idx, "name": str(device["name"])}
            for idx, device in enumerate(devices)
            if int(device.get("max_input_channels", 0)) > 0
        ]

    def _resolve_input_name(self, value) -> str:
        if value is None:
            return ""
        needle = str(value).strip()
        if not needle:
            return ""
        inputs = self._capture_inputs()
        try:
            idx = int(needle)
            for item in inputs:
                if item["index"] == idx:
                    return item["name"]
        except ValueError:
            pass
        lower = needle.lower()
        for item in inputs:
            if lower == item["name"].lower() or lower in item["name"].lower():
                return item["name"]
        return ""

    def _auto_desktop_input(self) -> str:
        inputs = self._capture_inputs()
        ranked_tokens = [
            "stereo mix",
            "what u hear",
            "loopback",
            "monitor",
            "cable output",
            "virtual audio cable",
            "line 1",
            "mix",
        ]
        for token in ranked_tokens:
            for item in inputs:
                if token in item["name"].lower():
                    return item["name"]
        return ""

    def _supports_wasapi(self) -> bool:
        if self._wasapi_supported is not None:
            return self._wasapi_supported
        try:
            completed = subprocess.run(
                [_ffmpeg(), "-hide_banner", "-formats"],
                capture_output=True,
                text=True,
                creationflags=CREATE_NO_WINDOW,
                timeout=5,
                check=False,
            )
            text = (completed.stdout or "") + "\n" + (completed.stderr or "")
            self._wasapi_supported = " wasapi" in text.lower()
        except Exception:
            self._wasapi_supported = False
        return self._wasapi_supported

    def _track_spec(self, raw: str, *, desktop_default: bool = False) -> tuple[str, str] | None:
        value = str(raw or "").strip()
        if not value and desktop_default:
            return None
        if not value:
            return None
        lower = value.lower()
        if lower.startswith("dshow:"):
            return ("dshow", value.split(":", 1)[1].strip())
        if lower.startswith("wasapi:"):
            if not self._supports_wasapi():
                return None
            return ("wasapi", value.split(":", 1)[1].strip() or "default")
        resolved = self._resolve_input_name(value)
        if resolved:
            return ("dshow", resolved)
        if self._supports_wasapi():
            return ("wasapi", value)
        return None

    def _build_audio_tracks(self) -> list[tuple[str, str, str]]:
        desktop_raw = self._settings_value("desktop_audio_device", "")
        game_raw = self._settings_value("game_audio_device", "")
        call_raw = self._settings_value("call_audio_device", "")
        desktop_spec = self._track_spec(desktop_raw, desktop_default=True)
        if not desktop_spec:
            auto = self._auto_desktop_input()
            desktop_spec = ("dshow", auto) if auto else None
        game_spec = self._track_spec(game_raw)
        call_spec = self._track_spec(call_raw)
        tracks: list[tuple[str, str, str]] = []
        if desktop_spec:
            tracks.append(("Desktop", desktop_spec[0], desktop_spec[1]))
        if game_spec and game_spec != desktop_spec:
            tracks.append(("Game", game_spec[0], game_spec[1]))
        if call_spec and call_spec not in {desktop_spec, game_spec}:
            tracks.append(("Calls", call_spec[0], call_spec[1]))
        return tracks

    def _ffmpeg_capture_command(
        self,
        fps: int,
        output: str,
        segment: bool,
        tracks: list[tuple[str, str, str]],
    ) -> list[str]:
        cmd = [
            _ffmpeg(),
            "-y",
            "-loglevel",
            "error",
            "-f",
            "gdigrab",
            "-framerate",
            str(fps),
            "-draw_mouse",
            "0",
            "-i",
            "desktop",
        ]
        for _, backend, device_name in tracks:
            if backend == "dshow":
                cmd.extend(["-f", "dshow", "-audio_buffer_size", "50", "-i", f"audio={device_name}"])
            else:
                cmd.extend(["-f", "wasapi", "-i", device_name or "default"])
        cmd.extend(["-map", "0:v:0", "-vf", _EVEN_PAD, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"])
        if tracks:
            cmd.extend(["-c:a", "aac", "-b:a", "192k", "-ac", "2"])
        for index, (label, _, _) in enumerate(tracks):
            cmd.extend(["-map", f"{index + 1}:a:0", f"-metadata:s:a:{index}", f"title={label}"])
        if segment:
            cmd.extend(
                [
                    "-f",
                    "segment",
                    "-segment_time",
                    "5",
                    "-segment_wrap",
                    "8",
                    "-reset_timestamps",
                    "1",
                    output,
                ]
            )
        else:
            cmd.append(output)
        return cmd

    def _spawn_capture(self, fps: int, output: str, segment: bool) -> tuple[subprocess.Popen | None, list[str], str]:
        all_tracks = self._build_audio_tracks()
        attempts: list[list[tuple[str, str, str]]] = []
        if all_tracks:
            attempts.append(all_tracks)
            if len(all_tracks) > 2:
                attempts.append(all_tracks[:-1])
            if len(all_tracks) > 1:
                attempts.append(all_tracks[:1])
        attempts.append([])
        last_error = ""
        for tracks in attempts:
            proc = _popen(self._ffmpeg_capture_command(fps=fps, output=output, segment=segment, tracks=tracks))
            time.sleep(0.7)
            if proc.poll() is None:
                return proc, [label for label, _, _ in tracks], ""
            try:
                last_error = (proc.stderr.read() or b"").decode(errors="replace")[:500]
            except Exception:
                last_error = ""
            if tracks:
                log.warning("capture failed for tracks %s: %s", [label for label, _, _ in tracks], last_error)
        return None, [], last_error

    def start_buffer(self) -> str:
        return "Clip buffer replaced by ShadowPlay. Make sure Instant Replay is on in GeForce Experience."

    def stop_buffer(self) -> None:
        pass

    def start(self) -> str:
        with self._lock:
            if self._session and self._session.poll() is None:
                return f"Already recording to {self._session_path}"
            _stop_proc(self._buffer)
            self._buffer = None
            hub.set_status(buffering=False)
            self._session_path = recordings_dir() / f"opus-record-{_stamp()}.mp4"
            proc, tracks, error = self._spawn_capture(
                fps=24,
                output=str(self._session_path),
                segment=False,
            )
            if not proc:
                self._session_path = None
                return f"Could not start recording: {(error[:120] or 'unknown error')}"
            self._session = proc
            self._last_audio_tracks = tracks
            hub.set_status(recording=True, audio_tracks=list(self._last_audio_tracks))
            return f"Recording the desktop to {self._session_path.name}"

    def stop(self, restart_buffer: bool = True) -> str:
        path = None
        with self._lock:
            if not self._session:
                return "Nothing is recording."
            proc = self._session
            path = self._session_path
            self._session = None
            self._session_path = None
        _stop_proc(proc)
        hub.set_status(recording=False, audio_tracks=[])
        self._last_audio_tracks = []
        if restart_buffer:
            threading.Thread(target=self.start_buffer, daemon=True).start()
        return f"Saved recording to {path}" if path else "Stopped recording."

    def _ensure_buffer(self) -> None:
        pass

    def clip(self) -> str:
        """Trigger NVIDIA ShadowPlay to save the last 30 seconds."""
        _tap([VK_MENU, VK_F10])
        return "Clipped."

    def _concat_buffer_UNUSED(self) -> Path | None:
        files = [
            p
            for p in sorted(self._buffer_dir.glob("seg*.mp4"), key=lambda p: p.stat().st_mtime)
            if p.stat().st_size > 12_000
        ]
        if len(files) > 1:
            files = files[:-1]
        if not files:
            return None
        out = clips_dir() / f"opus-clip-{_stamp()}.mp4"
        listing = self._buffer_dir / "concat.txt"
        listing.write_text("\n".join(f"file '{p.as_posix()}'" for p in files), encoding="utf-8")
        completed = subprocess.run(
            [
                _ffmpeg(),
                "-y",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(listing),
                "-c",
                "copy",
                str(out),
            ],
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0 or not out.exists() or out.stat().st_size < 1000:
            log.warning("concat failed: %s", completed.stderr)
            return None
        return out

    def screenshot(self) -> str:
        path = clips_dir() / f"opus-shot-{_stamp()}.png"
        path.write_bytes(screenshot_png(max_width=3840))
        return f"Saved screenshot to {path.name} in D:\\Clips\\Opus\\Clips"


def open_clips_folder() -> str:
    folder = opus_media_root()
    os.startfile(folder)  # noqa: S606
    return str(folder)


def _tap(codes: list[int]) -> None:
    for code in codes:
        user32.keybd_event(code, 0, 0, 0)
        time.sleep(0.02)
    for code in reversed(codes):
        user32.keybd_event(code, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.02)


def game_bar_record() -> str:
    _tap([VK_LWIN, VK_MENU, VK_R])
    return "Toggled Xbox Game Bar recording (Win+Alt+R)."


def game_bar_clip() -> str:
    _tap([VK_LWIN, VK_MENU, VK_G])
    return "Asked Xbox Game Bar to save the last clip (Win+Alt+G)."


recorder = Recorder()
