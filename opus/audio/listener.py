from __future__ import annotations

import io
import re
import threading
import time
from collections import deque

import numpy as np

from opus.api_pool import ApiPool, is_rate_limit
from opus.audio import InputStream, float_to_wav_bytes, resolve_device
from opus.hub import hub
from opus.intents import contains_wake, is_dismiss
from opus.logutil import friendly_error, get_logger
from opus.settings import Settings

log = get_logger()


SAMPLE_RATE = 16000
# Cut the utterance this soon after voice stops, then run STT / the command.
COMMAND_SILENCE_SECONDS = 0.5
# How much speech before we peek for the wake word and enter followup immediately.
WAKE_PROBE_SECONDS = 0.32
MIN_SECONDS = 0.28
MAX_SECONDS = 14.0
PREROLL_SECONDS = 0.35
ECHO_RE = re.compile(r"^\s*(yes|yeah|yep|ya|ok|okay|done|got it)[?.!]*\s*$", re.IGNORECASE)
STT_PROMPT = (
    "Opus, self mute, self deafen, self disconnect, "
    "open YouTube, open Opera GX, open Chrome, open Discord, open Spotify, "
    "play liked songs on Spotify, pause Spotify, skip the song, "
    "play chess, play mania, play osu, "
    "kick, kick all, kickall, mute all, deafen all, timeout, join the call, leave voice, "
    "flip a coin, play in the call, skip the song, "

    "clip that, take a screenshot, record, stop recording, "
    "open panel, close panel, weather, volume up, volume down, "
    "restart, thank you, never mind."
)


class WakeListener:
    """Always-on mic: VAD, audio cleanup, cloud transcription, then wake-word filter."""

    def __init__(self, settings: Settings, on_utterance, on_enabled=None) -> None:
        self.settings = settings
        self.on_utterance = on_utterance
        self.on_enabled = on_enabled
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stream: InputStream | None = None
        self._followup_until = 0.0
        self._mute_until = 0.0
        self._enabled = True
        self._frames: deque[np.ndarray] = deque(maxlen=400)
        self._speaking = False
        self._buffer: list[np.ndarray] = []
        self._preroll: deque[np.ndarray] = deque()
        self._preroll_samples = 0
        self._last_voice = 0.0
        self._speech_started = 0.0
        self._noise = 0.008
        self._processing = False
        self._wake_probed = False
        self._pending: deque[np.ndarray] = deque(maxlen=4)
        self._pool = ApiPool(settings)

    def start(self) -> None:
        self._stop.clear()
        self._open_stream()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="opus-listener")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close_stream()

    def restart(self) -> None:
        self._close_stream()
        self._open_stream()

    def set_enabled(self, enabled: bool) -> None:
        was = self._enabled
        self._enabled = enabled
        if enabled:
            hub.set_status(
                listening=True,
                mode="listening",
                message='Say "Opus" to wake me.',
            )
        else:
            hub.set_status(
                listening=False,
                mode="paused",
                message='Sleeping — say "Opus" to wake me.',
            )
        if enabled != was and self.on_enabled is not None:
            try:
                self.on_enabled(enabled)
            except Exception:
                log.exception("on_enabled callback failed")

    def is_followup(self) -> bool:
        return time.time() < self._followup_until

    def arm_followup(self, seconds: float = 25.0) -> None:
        self._followup_until = time.time() + seconds
        hub.set_status(mode="followup", message="I'm listening.")

    def disarm_followup(self) -> None:
        self._followup_until = 0.0
        hub.set_status(mode="listening", message='Say "Opus" to wake me.')

    def mute_for(self, seconds: float = 0.12) -> None:
        self._mute_until = time.time() + seconds
        self._speaking = False
        self._wake_probed = False
        self._buffer = []
        self._frames.clear()
        self._preroll.clear()
        self._preroll_samples = 0

    def _open_stream(self) -> None:
        device = resolve_device(self.settings.get("input_device"), "input")

        def callback(indata, frames, time_info, status):  # noqa: ARG001
            # Always capture audio — sleep only filters commands, so "Opus" can wake.
            if self._stop.is_set():
                return
            self._frames.append(indata.copy().reshape(-1))

        try:
            self._stream = InputStream(
                callback=callback, device=device, samplerate=SAMPLE_RATE,
                blocksize=512, latency="low",
            )
            self._stream.start()
        except Exception:
            log.exception("mic device %s failed, using default", device)
            self._stream = InputStream(
                callback=callback, device=None, samplerate=SAMPLE_RATE,
                blocksize=512, latency="low",
            )
            self._stream.start()

    def _close_stream(self) -> None:
        if self._stream:
            try:
                self._stream.stop()
            except Exception:
                pass
            self._stream = None

    def _muted(self) -> bool:
        now = time.time()
        if now < self._mute_until:
            return True
        if hub.status.get("speaking"):
            since = float(hub.status.get("speaking_since") or 0)
            if since and now - since > 45:
                hub.set_status(
                    speaking=False,
                    mode="followup" if self.is_followup() else "listening",
                    message="Unstuck from speaking — I'm listening.",
                )
        return False

    def _keep_preroll(self, block: np.ndarray) -> None:
        self._preroll.append(block)
        self._preroll_samples += int(block.shape[0])
        limit = int(PREROLL_SECONDS * SAMPLE_RATE)
        while self._preroll_samples > limit and self._preroll:
            dropped = self._preroll.popleft()
            self._preroll_samples -= int(dropped.shape[0])

    def _silence_needed(self) -> float:
        return COMMAND_SILENCE_SECONDS

    def _maybe_probe_wake(self) -> None:
        """As soon as 'Opus' is in the current clip, enter followup — don't wait for silence."""
        if self._wake_probed or self.is_followup() or not self._buffer:
            return
        if (time.time() - self._speech_started) < WAKE_PROBE_SECONDS:
            return
        self._wake_probed = True
        try:
            snapshot = np.concatenate(self._buffer)
        except ValueError:
            return
        threading.Thread(target=self._probe_wake, args=(snapshot,), daemon=True, name="opus-wake-probe").start()

    def _probe_wake(self, audio: np.ndarray) -> None:
        try:
            if self.is_followup():
                return
            text = self._transcribe(self._preprocess(self._trim(audio)))
            log.info("wake probe: %r", text)
            if contains_wake(text):
                self.arm_followup(25)
        except Exception:
            log.exception("wake probe failed")

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._muted():
                self._frames.clear()
                self._speaking = False
                self._wake_probed = False
                self._buffer = []
                self._preroll.clear()
                self._preroll_samples = 0
                time.sleep(0.008)
                continue
            if not self._frames:
                time.sleep(0.005)
                continue
            while self._frames and not self._muted():
                block = self._frames.popleft()
                self._keep_preroll(block)
                rms = float(np.sqrt(np.mean(np.square(block))) + 1e-8)
                self._noise = min(0.035, self._noise * 0.965 + rms * 0.035) if not self._speaking else self._noise
                start_threshold = max(0.013, self._noise * 3.2)
                hold_threshold = start_threshold * 0.58
                now = time.time()
                voiced = rms > (start_threshold if not self._speaking else hold_threshold)
                if voiced:
                    if not self._speaking:
                        self._speaking = True
                        self._speech_started = now
                        self._wake_probed = False
                        self._buffer = list(self._preroll)
                    else:
                        self._buffer.append(block)
                    self._last_voice = now
                    self._maybe_probe_wake()
                elif self._speaking:
                    self._buffer.append(block)
                    duration = now - self._speech_started
                    if now - self._last_voice >= self._silence_needed() and duration >= MIN_SECONDS:
                        audio = np.concatenate(self._buffer)
                        self._speaking = False
                        self._wake_probed = False
                        self._buffer = []
                        self._queue_audio(audio)
                    elif duration > MAX_SECONDS:
                        audio = np.concatenate(self._buffer)
                        self._speaking = False
                        self._wake_probed = False
                        self._buffer = []
                        self._queue_audio(audio)

    def _highpass(self, audio: np.ndarray, cutoff: float = 90.0) -> np.ndarray:
        if audio.size < 2:
            return audio
        rc = 1.0 / (2.0 * np.pi * cutoff)
        alpha = rc / (rc + (1.0 / SAMPLE_RATE))
        out = np.empty_like(audio)
        out[0] = audio[0]
        prev_y = float(audio[0])
        prev_x = float(audio[0])
        for index in range(1, audio.size):
            sample = float(audio[index])
            prev_y = alpha * (prev_y + sample - prev_x)
            out[index] = prev_y
            prev_x = sample
        return out

    def _preprocess(self, audio: np.ndarray) -> np.ndarray:
        if audio.size == 0:
            return audio
        cleaned = audio.astype(np.float32, copy=True)
        cleaned -= float(np.mean(cleaned))
        cleaned = self._highpass(cleaned)
        peak = float(np.max(np.abs(cleaned)))
        if peak > 1e-5:
            cleaned *= min(0.95 / peak, 6.0)
        rms = float(np.sqrt(np.mean(np.square(cleaned))) + 1e-8)
        target = 0.11
        if rms < target:
            cleaned *= min(target / rms, 3.5)
        return np.clip(cleaned, -1.0, 1.0)

    def _trim(self, audio: np.ndarray) -> np.ndarray:
        if audio.size < SAMPLE_RATE // 10:
            return audio
        window = max(1, int(0.02 * SAMPLE_RATE))
        energy = np.convolve(np.square(audio), np.ones(window) / window, mode="same")
        voiced = energy > max(0.00006, self._noise * self._noise * 1.8)
        idx = np.flatnonzero(voiced)
        if idx.size == 0:
            return audio
        start = max(0, int(idx[0]) - int(0.06 * SAMPLE_RATE))
        end = min(audio.size, int(idx[-1]) + int(0.1 * SAMPLE_RATE))
        return audio[start:end]

    def _queue_audio(self, audio: np.ndarray) -> None:
        hub.set_status(mode="hearing", message="Got it…")
        processed = self._preprocess(self._trim(audio))
        self._pending.append(processed)
        if self._processing:
            return
        threading.Thread(target=self._drain, daemon=True, name="opus-stt").start()

    def _drain(self) -> None:
        if self._processing:
            return
        self._processing = True
        try:
            while self._pending:
                audio = self._pending.popleft()
                self._handle_audio(audio)
        finally:
            self._processing = False

    def _handle_audio(self, audio: np.ndarray) -> None:
        try:
            text = self._transcribe(audio)
            log.info("STT heard: %r", text)
            if not text:
                if self.is_followup():
                    hub.set_status(mode="followup", message="I'm listening.")
                else:
                    hub.set_status(mode="listening", message='Say "Opus" to wake me.')
                return
            hub.set_status(last_heard=text)
            followup = self.is_followup()
            speaking = bool(hub.status.get("speaking")) or hub.status.get("mode") in {"thinking", "speaking"}

            # Sleep / tray-pause: keep STT alive, but only accept wake word to resume.
            if not self._enabled:
                if contains_wake(text):
                    log.info("waking from sleep on: %r", text)
                    self.set_enabled(True)
                    self.arm_followup(25)
                    # Defer handling so we don't nest tray/TTS work inside STT drain.
                    uttered = text
                    threading.Thread(
                        target=lambda: self.on_utterance(uttered, require_wake=True),
                        daemon=True,
                        name="opus-wake",
                    ).start()
                else:
                    hub.set_status(
                        mode="paused",
                        message='Sleeping — say "Opus" to wake me.',
                    )
                return

            if self._is_echo(text) and not contains_wake(text) and not is_dismiss(text):
                if followup:
                    hub.set_status(mode="followup", message="I'm listening.")
                else:
                    hub.set_status(mode="listening", message='Say "Opus" to wake me.')
                return
            if speaking and (contains_wake(text) or is_dismiss(text)):
                self.on_utterance(text, require_wake=not is_dismiss(text))
                return
            if followup or contains_wake(text):
                self.on_utterance(text, require_wake=not followup)
        except Exception as exc:
            log.exception("transcription failed")
            hub.set_status(message=friendly_error(exc))

    def _is_echo(self, text: str) -> bool:
        spoken = (hub.status.get("last_spoken") or "").strip().lower()
        heard = text.strip().lower()
        if not heard:
            return True
        if ECHO_RE.match(heard) and (not spoken or heard in spoken or "yes" in spoken):
            return True
        if spoken and (heard == spoken or heard in spoken or spoken in heard):
            return True
        spoken_words = {word for word in re.findall(r"[a-z0-9']+", spoken) if len(word) > 2}
        heard_words = {word for word in re.findall(r"[a-z0-9']+", heard) if len(word) > 2}
        if spoken_words and heard_words and heard_words.issubset(spoken_words):
            return True
        return False

    def transcribe_array(self, audio: np.ndarray) -> str:
        return self._transcribe(audio)

    def _transcribe(self, audio: np.ndarray) -> str:
        if not self._pool.has_keys():
            hub.set_status(message="Add an API key in the panel to enable listening.")
            return ""
        wav = float_to_wav_bytes(audio, SAMPLE_RATE)
        model = self.settings.get("stt_model") or "whisper-large-v3-turbo"
        last_error: Exception | None = None
        for _ in range(self._pool.max_attempts()):
            client, key, _, _ = self._pool.stt_client()
            buffer = io.BytesIO(wav)
            buffer.name = "utterance.wav"
            try:
                try:
                    result = client.audio.transcriptions.create(
                        model=model,
                        file=buffer,
                        language="en",
                        prompt=STT_PROMPT,
                        temperature=0,
                    )
                except TypeError:
                    buffer.seek(0)
                    result = client.audio.transcriptions.create(model=model, file=buffer, language="en")
                return (result.text or "").strip()
            except Exception as exc:
                last_error = exc
                if is_rate_limit(exc):
                    self._pool.mark_cooldown(key)
                    continue
                log.exception("whisper failed")
                raise
        if last_error:
            raise last_error
        return ""
