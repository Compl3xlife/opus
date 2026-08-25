from __future__ import annotations

import asyncio
import hashlib
import subprocess
import tempfile
import threading
import time
import winsound
from pathlib import Path

import edge_tts
import imageio_ffmpeg

from opus.audio import play_wav_bytes, resolve_device, stop_playback
from opus.audio.clone import get_cloner, has_reference
from opus.hub import hub
from opus.logutil import get_logger
from opus.settings import Settings, appdata_dir

log = get_logger()

CREATE_NO_WINDOW = 0x08000000

VOICES = [
    {
        "id": "opus-ultron",
        "name": "Ultron — movie (raspy)",
        "edge": "en-US-DavisNeural",
        "rate": "-8%",
        "pitch": "-8Hz",
        "depth": 0.96,
        "style": "ultron",
    },
    {"id": "en-US-GuyNeural", "name": "Guy — deep, US", "edge": "en-US-GuyNeural", "rate": "-8%", "pitch": "-18Hz"},
    {"id": "en-US-AndrewNeural", "name": "Andrew — natural, US", "edge": "en-US-AndrewNeural", "rate": "+8%", "pitch": "-2Hz"},
    {"id": "en-US-JennyNeural", "name": "Jenny — warm, US", "edge": "en-US-JennyNeural", "rate": "+8%", "pitch": "-2Hz"},
    {"id": "en-US-AriaNeural", "name": "Aria — clear, US", "edge": "en-US-AriaNeural", "rate": "+8%", "pitch": "-2Hz"},
    {"id": "en-US-AvaNeural", "name": "Ava — conversational, US", "edge": "en-US-AvaNeural", "rate": "+8%", "pitch": "-2Hz"},
    {"id": "en-US-BrianNeural", "name": "Brian — calm, US", "edge": "en-US-BrianNeural", "rate": "+8%", "pitch": "-2Hz"},
    {"id": "en-US-DavisNeural", "name": "Davis — baritone, US", "edge": "en-US-DavisNeural", "rate": "-2%", "pitch": "-10Hz"},
    {"id": "en-US-EmmaNeural", "name": "Emma — friendly, US", "edge": "en-US-EmmaNeural", "rate": "+8%", "pitch": "-2Hz"},
    {"id": "en-GB-RyanNeural", "name": "Ryan — British", "edge": "en-GB-RyanNeural", "rate": "+4%", "pitch": "-4Hz"},
    {"id": "en-GB-SoniaNeural", "name": "Sonia — British", "edge": "en-GB-SoniaNeural", "rate": "+8%", "pitch": "-2Hz"},
]

DEFAULT_VOICE = "opus-ultron"
DEFAULT_RATE = "-8%"
DEFAULT_PITCH = "-8Hz"

_VOICE_MAP = {item["id"]: item for item in VOICES}

# James Spader Ultron: baritone with rasp, presence, light metallic tail.
ULTRON_EQ = (
    "highpass=f=88,"
    "equalizer=f=200:width_type=h:width=120:g=2,"
    "equalizer=f=900:width_type=h:width=450:g=6,"
    "equalizer=f=1350:width_type=h:width=650:g=4,"
    "equalizer=f=3200:width_type=h:width=900:g=-3,"
    "equalizer=f=6800:width_type=h:width=2200:g=-7,"
    "acompressor=threshold=0.10:ratio=3.5:attack=2:release=35:makeup=2.5,"
    "asoftclip=type=tanh:param=0.82,"
    "aecho=0.6:0.5:16:0.08"
)


def voice_synthesis_params(voice_id: str, settings: Settings | None = None) -> tuple[str, str, str, float, str]:
    entry = _VOICE_MAP.get(voice_id or DEFAULT_VOICE, _VOICE_MAP[DEFAULT_VOICE])
    edge = entry.get("edge", entry.get("id", DEFAULT_VOICE))
    rate = entry.get("rate", DEFAULT_RATE)
    pitch = entry.get("pitch", DEFAULT_PITCH)
    depth = float(entry.get("depth") or 1.0)
    style = str(entry.get("style") or "")
    if settings:
        rate = settings.get("voice_rate") or rate
        pitch = settings.get("voice_pitch") or pitch
        if settings.get("voice_depth"):
            try:
                depth = float(settings.get("voice_depth"))
            except (TypeError, ValueError):
                pass
        style = settings.get("voice_style") or style
    return edge, rate, pitch, depth, style


async def _synthesize(text: str, voice: str, rate: str, pitch: str, dest: Path) -> None:
    communicate = edge_tts.Communicate(text, voice=voice, rate=rate, pitch=pitch)
    await communicate.save(str(dest))


def _play_wav(path: Path) -> None:
    winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_NODEFAULT)


class Speaker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cache = appdata_dir() / "tts-cache"
        self._cache.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()
        try:
            stop_playback()
        except Exception:
            pass
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass
        hub.set_status(speaking=False)

    def _play_file(self, path: Path) -> None:
        try:
            output = resolve_device(self.settings.get("output_device"), "output")
            play_wav_bytes(path.read_bytes(), output_device=output, volume=1.0)
            return
        except Exception:
            log.exception("playback failed, using winsound fallback")
        _play_wav(path)

    def say(self, text: str) -> None:
        if not text.strip() or not self.settings.get("voice_responses"):
            return
        self._stop.clear()
        spoken = text.strip()
        if len(spoken) > 220:
            spoken = spoken[:220].rsplit(" ", 1)[0] + "."
        done = threading.Event()
        hub.set_status(
            speaking=True,
            mode="speaking",
            last_spoken=spoken,
            speaking_since=time.time(),
        )

        want_clone = bool(self.settings.get("use_cloned_voice") and has_reference())
        clone_ready = False
        if want_clone:
            try:
                clone_ready = bool(get_cloner(self.settings).status().get("engine_loaded"))
            except Exception:
                clone_ready = False
            if not clone_ready:
                name = self.settings.get("clone_ref_name") or "clone"
                volume = max(0.05, min(1.0, int(self.settings.get("volume") or 80) / 100.0))
                cached = self._cache_path(spoken, f"clone:{name}", DEFAULT_RATE, DEFAULT_PITCH, volume)
                clone_ready = cached.exists() and cached.stat().st_size > 64

        use_clone = want_clone and clone_ready

        def run() -> None:
            try:
                if self._stop.is_set():
                    return
                if use_clone:
                    self._speak_clone(spoken)
                else:
                    self._speak_neural(spoken)
            except Exception:
                log.exception("cloned or neural voice failed, using fallback")
                try:
                    if self._stop.is_set():
                        return
                    self._speak_neural(spoken)
                except Exception:
                    try:
                        if not self._stop.is_set():
                            self._speak_sapi(spoken)
                    except Exception as exc:
                        hub.set_status(message=f"Voice error: {exc}")
            finally:
                done.set()

        threading.Thread(target=run, daemon=True, name="opus-tts").start()
        wait_for = 180 if use_clone else 25
        if not done.wait(timeout=wait_for):
            log.warning("voice timed out after %ss; falling back to neural", wait_for)
            self._stop.set()
            try:
                self._stop.clear()
                self._speak_neural(spoken)
            except Exception:
                log.exception("neural fallback after timeout failed")
            hub.set_status(message="Voice timed out — used backup voice.")
        hub.set_status(speaking=False)

    def _warm_clone(self) -> None:
        try:
            cloner = get_cloner(self.settings)
            if cloner.status().get("engine_loaded"):
                return
            hub.set_status(message="Warming Ultron clone voice in background…")
            dest = appdata_dir() / "voices" / "warm.wav"
            cloner.synthesize("Ready.", dest)
            hub.set_status(message="Cloned voice is ready.")
        except Exception:
            log.exception("clone warm-up failed")

    def _cache_path(self, text: str, voice: str, rate: str, pitch: str, volume: float, extra: str = "") -> Path:
        key = hashlib.sha1(f"{voice}|{rate}|{pitch}|{volume:.2f}|{extra}|{text}".encode("utf-8")).hexdigest()
        return self._cache / f"{key}.wav"

    def _ultron_filter(self, depth: float, volume: float) -> str:
        depth = max(0.88, min(1.0, depth))
        tempo = 1.0 / depth
        parts = [f"asetrate=22050*{depth:.3f}", f"atempo={tempo:.3f}", ULTRON_EQ]
        if volume < 0.99:
            parts.append(f"volume={volume:.2f}")
        return ",".join(parts)

    def ensure_wav(self, text: str) -> Path | None:
        """Synthesize speech to a cached wav without playing it."""
        spoken = (text or "").strip()
        if not spoken:
            return None
        if len(spoken) > 280:
            spoken = spoken[:280].rsplit(" ", 1)[0] + "."
        try:
            return self._neural_wav(spoken)
        except Exception:
            log.exception("ensure_wav neural failed, trying SAPI")
        try:
            return self._sapi_wav(spoken)
        except Exception:
            log.exception("ensure_wav failed")
            return None

    def _sapi_wav(self, text: str) -> Path:
        import pythoncom
        import pyttsx3

        dest = self._cache_path(text, "sapi", "0", "0", 1.0)
        if dest.exists() and dest.stat().st_size > 64:
            return dest
        pythoncom.CoInitialize()
        try:
            engine = pyttsx3.init()
            engine.setProperty("rate", 175)
            volume = max(0.05, min(1.0, int(self.settings.get("volume") or 80) / 100.0))
            engine.setProperty("volume", volume)
            engine.save_to_file(text, str(dest))
            engine.runAndWait()
        finally:
            pythoncom.CoUninitialize()
        if not dest.exists() or dest.stat().st_size < 64:
            raise RuntimeError("SAPI produced no audio")
        return dest

    def _neural_wav(self, text: str) -> Path:
        voice_id = self.settings.get("tts_voice") or DEFAULT_VOICE
        voice, rate, pitch, depth, style = voice_synthesis_params(voice_id, self.settings)
        volume = max(0.05, min(1.0, int(self.settings.get("volume") or 80) / 100.0))
        extra = f"{depth:.3f}|{style}"
        cached = self._cache_path(text, voice, rate, pitch, volume, extra)
        if cached.exists() and cached.stat().st_size > 64:
            return cached
        with tempfile.TemporaryDirectory(prefix="opus-tts-") as folder:
            mp3 = Path(folder) / "speech.mp3"
            wav = Path(folder) / "speech.wav"
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_synthesize(text, voice, rate, pitch, mp3))
            finally:
                loop.close()
            if not mp3.exists() or mp3.stat().st_size < 64:
                raise RuntimeError("edge-tts produced no audio")
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            args = [
                ffmpeg,
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(mp3),
                "-acodec",
                "pcm_s16le",
                "-ar",
                "22050",
            ]
            if style == "ultron" or depth < 0.99:
                args.extend(["-filter:a", self._ultron_filter(depth, volume)])
            elif volume < 0.99:
                args.extend(["-filter:a", f"volume={volume:.2f}"])
            args.append(str(wav))
            subprocess.run(args, check=True, timeout=12, creationflags=CREATE_NO_WINDOW)
            cached.write_bytes(wav.read_bytes())
        return cached

    def _speak_neural(self, text: str) -> None:
        cached = self._neural_wav(text)
        if not self._stop.is_set() and cached and cached.exists():
            self._play_file(cached)

    def _speak_clone(self, text: str) -> None:
        volume = max(0.05, min(1.0, int(self.settings.get("volume") or 80) / 100.0))
        name = self.settings.get("clone_ref_name") or "clone"
        cached = self._cache_path(text, f"clone:{name}", DEFAULT_RATE, DEFAULT_PITCH, volume)
        if cached.exists() and cached.stat().st_size > 64:
            if not self._stop.is_set():
                self._play_file(cached)
            return
        cloner = get_cloner(self.settings)
        with tempfile.TemporaryDirectory(prefix="opus-clone-") as folder:
            raw = Path(folder) / "clone.wav"
            cloner.synthesize(text, raw)
            if volume < 0.99:
                ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
                out = Path(folder) / "clone-vol.wav"
                subprocess.run(
                    [
                        ffmpeg,
                        "-y",
                        "-loglevel",
                        "error",
                        "-i",
                        str(raw),
                        "-filter:a",
                        f"volume={volume:.2f}",
                        str(out),
                    ],
                    check=True,
                    timeout=20,
                    creationflags=CREATE_NO_WINDOW,
                )
                raw = out
            try:
                cached.write_bytes(raw.read_bytes())
                self._play_file(cached)
            except OSError:
                self._play_file(raw)

    def _speak_sapi(self, text: str) -> None:
        import pythoncom
        import pyttsx3

        pythoncom.CoInitialize()
        try:
            engine = pyttsx3.init()
            engine.setProperty("rate", 190)
            volume = max(0, min(100, int(self.settings.get("volume") or 80))) / 100.0
            engine.setProperty("volume", volume)
            engine.say(text)
            engine.runAndWait()
        finally:
            pythoncom.CoUninitialize()
