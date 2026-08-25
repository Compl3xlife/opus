"""Discord voice receive: per-speaker PCM buffers for in-call STT."""
from __future__ import annotations

import threading
import time
from collections import defaultdict

import numpy as np

DISCORD_RATE = 48000
STT_RATE = 16000
SILENCE_SECONDS = 0.85
MIN_SECONDS = 0.38
MAX_SECONDS = 12.0
BYTES_PER_SECOND = DISCORD_RATE * 2 * 2  # stereo s16

try:
    from discord.ext import voice_recv

    HAS_VOICE_RECV = True
except Exception:
    voice_recv = None  # type: ignore
    HAS_VOICE_RECV = False


def pcm_to_mono16(pcm: bytes) -> np.ndarray:
    if not pcm:
        return np.zeros(0, dtype=np.float32)
    samples = np.frombuffer(pcm, dtype=np.int16)
    if samples.size == 0:
        return np.zeros(0, dtype=np.float32)
    if samples.size % 2 == 0:
        mono = samples.reshape(-1, 2).astype(np.float32).mean(axis=1)
    else:
        mono = samples.astype(np.float32)
    step = max(1, DISCORD_RATE // STT_RATE)
    down = mono[::step]
    return (down / 32768.0).astype(np.float32)


class CallSink:
    """Collects decoded PCM per Discord user until silence."""

    def __init__(self) -> None:
        self._chunks: dict[int, list[bytes]] = defaultdict(list)
        self._last: dict[int, float] = {}
        self._users: dict[int, object] = {}
        self._lock = threading.Lock()
        self.ignore_until = 0.0
        self.packets = 0
        self.bytes_seen = 0

    def write(self, user, data) -> None:
        if time.time() < self.ignore_until:
            return
        if user is not None and getattr(user, "bot", False):
            return
        pcm = getattr(data, "pcm", None) or b""
        if not pcm:
            return
        packet = getattr(data, "packet", None)
        ssrc = int(getattr(packet, "ssrc", 0) or 0)
        uid = int(user.id) if user is not None else (-ssrc if ssrc else 0)
        if not uid:
            return
        with self._lock:
            if user is not None:
                self._users[uid] = user
                anon = -ssrc if ssrc else None
                if anon and anon in self._chunks and anon != uid:
                    self._chunks[uid].extend(self._chunks.pop(anon, []))
                    self._last.pop(anon, None)
            self._chunks[uid].append(pcm)
            self._last[uid] = time.time()
            self.packets += 1
            self.bytes_seen += len(pcm)
            bucket = self._chunks[uid]
            total = sum(len(chunk) for chunk in bucket)
            if total > BYTES_PER_SECOND * MAX_SECONDS:
                bucket[:] = bucket[-max(1, len(bucket) // 2) :]

    def cleanup(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._last.clear()
            self._users.clear()

    def ready_users(self, silence: float = SILENCE_SECONDS) -> list[int]:
        now = time.time()
        ready: list[int] = []
        with self._lock:
            for uid, last in list(self._last.items()):
                if now - last >= silence and self._chunks.get(uid):
                    ready.append(uid)
        return ready

    def take(self, uid: int) -> tuple[object | None, bytes]:
        with self._lock:
            chunks = self._chunks.pop(uid, [])
            self._last.pop(uid, None)
            user = self._users.get(uid)
        return user, b"".join(chunks)
