from __future__ import annotations

import io
import os
import tempfile
import threading
from typing import Callable

import numpy as np
import sounddevice as sd
import soundfile as sf

# sounddevice/PortAudio is not safe to play while an InputStream callback is live
# on Windows — that combo was access-violating (0xC0000005) and killing Opus.
_device_lock = threading.RLock()


def list_devices() -> dict:
    with _device_lock:
        devices = sd.query_devices()
    inputs = []
    outputs = []
    for index, device in enumerate(devices):
        item = {
            "index": index,
            "name": device["name"],
            "max_input_channels": int(device["max_input_channels"]),
            "max_output_channels": int(device["max_output_channels"]),
            "default_samplerate": float(device["default_samplerate"]),
        }
        if device["max_input_channels"] > 0:
            inputs.append(item)
        if device["max_output_channels"] > 0:
            outputs.append(item)
    with _device_lock:
        defaults = sd.default.device
    return {
        "inputs": inputs,
        "outputs": outputs,
        "default_input": defaults[0],
        "default_output": defaults[1],
    }


def resolve_device(name_or_index, kind: str):
    if name_or_index is None or name_or_index == "":
        return None
    if isinstance(name_or_index, int):
        return name_or_index
    try:
        return int(name_or_index)
    except (TypeError, ValueError):
        pass
    with _device_lock:
        devices = sd.query_devices()
    needle = str(name_or_index).lower()
    for index, device in enumerate(devices):
        channels = device["max_input_channels"] if kind == "input" else device["max_output_channels"]
        if channels > 0 and needle in device["name"].lower():
            return index
    return None


def float_to_wav_bytes(audio: np.ndarray, samplerate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    sf.write(buffer, audio, samplerate, format="WAV")
    return buffer.getvalue()


def play_wav_bytes(data: bytes, output_device=None, volume: float = 1.0) -> None:
    buffer = io.BytesIO(data)
    audio, samplerate = sf.read(buffer, dtype="float32")
    audio = np.clip(audio * max(0.0, min(1.0, volume)), -1.0, 1.0)

    # Windows: never call sd.play alongside the always-on mic stream.
    if os.name == "nt":
        _play_wav_winsound(audio, int(samplerate))
        return

    with _device_lock:
        sd.play(audio, samplerate, device=output_device)
        sd.wait()


def _play_wav_winsound(audio: np.ndarray, samplerate: int) -> None:
    import winsound

    path = None
    try:
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        sf.write(path, audio, samplerate, format="WAV")
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_NODEFAULT)
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def stop_playback() -> None:
    if os.name == "nt":
        try:
            import winsound

            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass
        return
    with _device_lock:
        try:
            sd.stop()
        except Exception:
            pass


class InputStream:
    def __init__(
        self,
        callback: Callable,
        device=None,
        samplerate: int = 16000,
        blocksize: int = 512,
        latency=None,
    ) -> None:
        kwargs: dict = dict(
            samplerate=samplerate,
            channels=1,
            dtype="float32",
            blocksize=blocksize,
            device=device,
            callback=callback,
        )
        if latency:
            kwargs["latency"] = latency
        with _device_lock:
            self.stream = sd.InputStream(**kwargs)

    def start(self) -> None:
        with _device_lock:
            self.stream.start()

    def stop(self) -> None:
        with _device_lock:
            try:
                self.stream.stop()
            except Exception:
                pass
            try:
                self.stream.close()
            except Exception:
                pass
