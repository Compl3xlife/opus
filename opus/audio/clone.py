from __future__ import annotations

import io
import subprocess
import sys
import threading
from pathlib import Path

import imageio_ffmpeg
import numpy as np
import soundfile as sf
from openai import OpenAI

from opus.hub import hub
from opus.logutil import get_logger
from opus.settings import Settings, appdata_dir, voices_dir

log = get_logger()

CREATE_NO_WINDOW = 0x08000000
REF_NAME = "reference.wav"


def reference_path() -> Path:
    folder = appdata_dir() / "voices"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / REF_NAME


def has_reference() -> bool:
    path = reference_path()
    return path.exists() and path.stat().st_size > 8_000


class VoiceCloner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._model = None
        self._installing = False
        self._torchaudio_patched = False

    def status(self) -> dict:
        name = self.settings.get("clone_ref_name") or ""
        return {
            "ready": has_reference(),
            "enabled": bool(self.settings.get("use_cloned_voice")),
            "engine_loaded": self._model is not None,
            "sample_name": name,
            "sample_text": (self.settings.get("clone_ref_text") or "")[:240],
        }

    def import_file(self, source: str | Path) -> str:
        try:
            src = Path(source)
            if not src.exists() or not src.is_file():
                return "That sound file wasn't found."
            wav = self._normalize(src)
            if wav.stat().st_size < 8_000:
                return "That clip is too short or silent. Use 5–15 seconds of clean speech."
            text = self._transcribe(wav)
            self.settings.update(
                {
                    "clone_ref_text": text,
                    "clone_ref_name": src.name,
                    "use_cloned_voice": False,
                    "tts_voice": "opus-ultron",
                    "voice_style": "ultron",
                    "voice_rate": "-8%",
                    "voice_pitch": "-8Hz",
                    "voice_depth": 0.96,
                    "voice_preset_version": 2,
                    "ultron_voice_applied": True,
                }
            )
            with self._lock:
                self._model = None
            extra = f' I heard: “{text[:80]}”.' if text else ""
            return (
                f"Baked Ultron voice settings from {src.name}.{extra} "
                "Opus will use the fast raspy movie voice — no warmup."
            )
        except Exception as exc:
            log.exception("voice import failed")
            return f"Could not clone that voice: {exc}"

    def synthesize(self, text: str, dest: Path) -> None:
        if not has_reference():
            raise RuntimeError("No cloned voice sample is loaded.")
        ref_text = (self.settings.get("clone_ref_text") or "").strip()
        model = self._ensure_model()
        hub.set_status(message="Speaking with cloned voice…")
        wav, sr, _spec = model.infer(
            ref_file=str(reference_path()),
            ref_text=ref_text,
            gen_text=text,
            file_wave=str(dest),
            nfe_step=32,
            cfg_strength=2.0,
            speed=1.0,
            remove_silence=True,
            show_info=lambda *_args, **_kwargs: None,
        )
        if dest.exists() and dest.stat().st_size > 64:
            return
        if wav is None:
            raise RuntimeError("Voice clone produced no audio.")
        sf.write(str(dest), np.asarray(wav), int(sr or 24000))

    def _ensure_model(self):
        with self._lock:
            if self._model is not None:
                return self._model
            self._install_if_needed()
            self._patch_torchaudio_loader()
            hub.set_status(message="Loading cloned-voice model…")
            from f5_tts.api import F5TTS

            self._model = F5TTS(model="F5TTS_v1_Base")
            return self._model

    def _patch_torchaudio_loader(self) -> None:
        """
        F5-TTS uses torchaudio.load() internally, but on some Windows setups
        torchaudio's torchcodec backend fails to load native DLLs.
        Patch load() to use soundfile directly so clone synthesis remains usable.
        """
        if self._torchaudio_patched:
            return
        try:
            import torch
            import torchaudio

            def _sf_load(path: str, *args, **kwargs):  # noqa: ANN001
                data, sr = sf.read(path, dtype="float32", always_2d=True)
                # soundfile returns [samples, channels]; torchaudio expects [channels, samples]
                tensor = torch.from_numpy(np.asarray(data).T)
                return tensor, int(sr)

            torchaudio.load = _sf_load  # type: ignore[assignment]
            self._torchaudio_patched = True
        except Exception:
            log.exception("torchaudio patch failed")

    def _install_if_needed(self) -> None:
        try:
            import f5_tts  # noqa: F401
            return
        except Exception:
            pass
        if self._installing:
            raise RuntimeError("Voice engine is still installing.")
        self._installing = True
        hub.set_status(message="Installing voice clone engine (first time only)…")
        try:
            cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "f5-tts"]
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=1800,
                creationflags=CREATE_NO_WINDOW,
            )
            if completed.returncode != 0:
                raise RuntimeError((completed.stderr or completed.stdout or "pip failed")[-400])
        finally:
            self._installing = False

    def _normalize(self, source: Path) -> Path:
        dest = reference_path()
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        voices_dir()
        args = [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-t",
            "15",
            "-ac",
            "1",
            "-ar",
            "24000",
            "-acodec",
            "pcm_s16le",
            "-af",
            "loudnorm=I=-16:TP=-1.5:LRA=11,silenceremove=start_periods=1:start_silence=0.15:start_threshold=-40dB",
            str(dest),
        ]
        completed = subprocess.run(args, capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW)
        if completed.returncode != 0 or not dest.exists():
            err = (completed.stderr or b"").decode(errors="replace")[:240]
            raise RuntimeError(f"Could not read that sound file. {err}")
        return dest

    def _transcribe(self, wav: Path) -> str:
        key = (self.settings.get("openai_api_key") or "").strip()
        if not key:
            return ""
        client = OpenAI(
            api_key=key,
            base_url=self.settings.get("openai_base_url") or None,
            timeout=20.0,
            max_retries=0,
        )
        model = self.settings.get("stt_model") or "whisper-large-v3-turbo"
        data = wav.read_bytes()
        buffer = io.BytesIO(data)
        buffer.name = "reference.wav"
        try:
            result = client.audio.transcriptions.create(model=model, file=buffer, language="en")
        except Exception:
            log.exception("clone sample transcription failed")
            return ""
        return (result.text or "").strip()


def browse_audio_file() -> str:
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$d = New-Object System.Windows.Forms.OpenFileDialog; "
        "$d.Filter = 'Audio|*.wav;*.mp3;*.m4a;*.flac;*.ogg;*.wma;*.aac|All files|*.*'; "
        "$d.Title = 'Choose a voice sample for Opus'; "
        "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { $d.FileName }"
    )
    completed = subprocess.run(
        ["powershell", "-STA", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=300,
        creationflags=CREATE_NO_WINDOW,
    )
    return (completed.stdout or "").strip()


_cloner: VoiceCloner | None = None


def get_cloner(settings: Settings) -> VoiceCloner:
    global _cloner
    if _cloner is None:
        _cloner = VoiceCloner(settings)
    else:
        _cloner.settings = settings
    return _cloner
