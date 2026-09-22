"""Per-guild in-call music for Opus. Never shares a queue across servers."""
from __future__ import annotations

import asyncio
import queue as frame_queue
import threading
from dataclasses import dataclass, field
from typing import Callable

import discord
import numpy as np

from opus.logutil import get_logger

log = get_logger()

# 20ms of stereo 48kHz 16-bit PCM — Discord's native frame size.
FRAME_BYTES = 3840
MUSIC_VOL = 0.85
DUCK_VOL = 0.62
TTS_VOL = 1.15
TTS_WARMUP_FRAMES = 25
MUSIC_WARMUP_FRAMES = 450
# After audio has started, allow ~1s of empty reads before treating it as EOF.
# Live stalls are covered by PrefetchPcmSource, which keeps feeding silence until ffmpeg dies.
MUSIC_STALL_FRAMES = 50
PREFETCH_FRAMES = 75


@dataclass
class Track:
    title: str
    stream_url: str
    page_url: str = ""
    query: str = ""
    artist: str = ""
    duration: float = 0.0


@dataclass
class GuildMusic:
    queue: list[Track] = field(default_factory=list)
    current: Track | None = None
    repeat: bool = False
    mixer: CallMixer | None = None
    control_message_id: int | None = None
    control_channel_id: int | None = None
    lyrics_message_id: int | None = None
    lyrics_channel_id: int | None = None
    lyrics_task: object | None = None
    lyrics_lines: list = field(default_factory=list)
    lyrics_plain: str = ""
    lyrics_duration: float = 0.0
    lyrics_gen: int = 0
    tts_enabled: bool = True
    silent_retries: int = 0


def _extract_track(query: str) -> Track:
    import yt_dlp

    query = (query or "").strip()
    if not query:
        raise ValueError("Nothing to play.")
    search = query if query.startswith("http") else f"ytsearch1:{query}"
    opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch",
        "source_address": "0.0.0.0",
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(search, download=False)
        if not info:
            raise ValueError("Couldn't find that track.")
        if info.get("entries"):
            info = next((item for item in info["entries"] if item), None)
        if not info:
            raise ValueError("Couldn't find that track.")
        url = info.get("url") or ""
        if not url:
            raise ValueError("No audio stream for that track.")
        title = str(info.get("title") or query)[:120]
        artist = str(info.get("artist") or info.get("album_artist") or "")[:80]
        if not artist:
            creators = info.get("artists")
            if isinstance(creators, list) and creators:
                names = []
                for item in creators:
                    if isinstance(item, dict):
                        name = str(item.get("name") or "").strip()
                    else:
                        name = str(item).strip()
                    if name:
                        names.append(name)
                artist = ", ".join(names)[:80]
        if not artist:
            artist = str(info.get("uploader") or info.get("channel") or "")[:80]
        return Track(
            title=title,
            stream_url=url,
            page_url=str(info.get("webpage_url") or query),
            query=query,
            artist=artist,
            duration=float(info.get("duration") or 0),
        )


def ffmpeg_source(stream_url: str, *, reconnect: bool = True):
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    before = "-nostdin"
    if reconnect:
        before = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
    raw = discord.FFmpegPCMAudio(
        stream_url,
        executable=ffmpeg,
        before_options=before,
        options="-vn",
    )
    return PrefetchPcmSource(raw)


class PrefetchPcmSource(discord.AudioSource):
    """Read ffmpeg on a side thread so Discord's player never blocks on YouTube stalls."""

    def __init__(self, inner: discord.AudioSource) -> None:
        self._inner = inner
        self._frames: frame_queue.Queue[bytes | None] = frame_queue.Queue(maxsize=PREFETCH_FRAMES)
        self._stop = threading.Event()
        self._had_audio = False
        self._thread = threading.Thread(target=self._pump, name="opus-ffmpeg", daemon=True)
        self._thread.start()

    def is_opus(self) -> bool:
        return False

    def _pump(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    frame = self._inner.read() or b""
                except Exception:
                    log.exception("ffmpeg pump read failed")
                    break
                if not frame:
                    break
                if len(frame) < FRAME_BYTES:
                    frame = frame + b"\x00" * (FRAME_BYTES - len(frame))
                else:
                    frame = frame[:FRAME_BYTES]
                self._had_audio = True
                while not self._stop.is_set():
                    try:
                        self._frames.put(frame, timeout=0.05)
                        break
                    except frame_queue.Full:
                        continue
        finally:
            try:
                self._frames.put_nowait(None)
            except frame_queue.Full:
                pass
            log.info("ffmpeg pump ended had_audio=%s", self._had_audio)

    def read(self) -> bytes:
        try:
            frame = self._frames.get_nowait()
        except frame_queue.Empty:
            if self._thread.is_alive() and not self._stop.is_set():
                if self._had_audio:
                    return b"\x00" * FRAME_BYTES
                return b""
            return b""
        if frame is None:
            return b""
        return frame

    def cleanup(self) -> None:
        self._stop.set()
        try:
            while True:
                self._frames.get_nowait()
        except frame_queue.Empty:
            pass
        _cleanup_source(self._inner)


def wav_to_discord_pcm(path) -> bytes:
    """Decode a TTS wav into Discord's native PCM so mix-in has no FFmpeg startup gap."""
    import audioop
    import wave

    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    if not frames:
        return b""
    if width != 2:
        frames = audioop.lin2lin(frames, width, 2)
        width = 2
    if channels == 1:
        frames = audioop.tostereo(frames, 2, 1, 1)
        channels = 2
    elif channels > 2:
        arr = np.frombuffer(frames, dtype=np.int16).reshape(-1, channels)
        frames = np.ascontiguousarray(arr[:, :2]).tobytes()
        channels = 2
    if rate != 48000:
        frames, _state = audioop.ratecv(frames, 2, channels, rate, 48000, None)
    return frames


class PcmBufferSource(discord.AudioSource):
    """Preloaded s16le stereo 48kHz PCM. First read is always a full frame."""

    def __init__(self, pcm: bytes) -> None:
        self._pcm = pcm or b""
        self._offset = 0

    def is_opus(self) -> bool:
        return False

    def read(self) -> bytes:
        if self._offset >= len(self._pcm):
            return b""
        end = min(self._offset + FRAME_BYTES, len(self._pcm))
        chunk = self._pcm[self._offset:end]
        self._offset = end
        if len(chunk) < FRAME_BYTES:
            chunk += b"\x00" * (FRAME_BYTES - len(chunk))
        return chunk

    def cleanup(self) -> None:
        self._offset = len(self._pcm)


def _pad_frame(data: bytes) -> bytes:
    if not data:
        return b"\x00" * FRAME_BYTES
    if len(data) >= FRAME_BYTES:
        return data[:FRAME_BYTES]
    return data + (b"\x00" * (FRAME_BYTES - len(data)))


def mix_frames(music: bytes, tts: bytes, *, music_vol: float, tts_vol: float) -> bytes:
    """Mix two s16le stereo frames, clipping to 16-bit."""
    music_pcm = np.frombuffer(_pad_frame(music), dtype=np.int16).astype(np.int32)
    tts_pcm = np.frombuffer(_pad_frame(tts), dtype=np.int16).astype(np.int32)
    mixed = np.clip(music_pcm * music_vol + tts_pcm * tts_vol, -32768, 32767).astype(np.int16)
    return mixed.tobytes()


def _cleanup_source(source) -> None:
    if source is None:
        return
    try:
        source.cleanup()
    except Exception:
        pass


class CallMixer(discord.AudioSource):
    """One Discord play() source. TTS is a PCM buffer mixed onto the live music."""

    def __init__(self, on_music_end: Callable[[object], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._music: discord.AudioSource | None = None
        self._on_music_end = on_music_end
        self._tts_pcm = b""
        self._tts_off = 0
        self._tts_done: list[Callable[[], None]] = []
        self._closed = False
        self._idle_silence = 0
        self._music_token = None
        self._music_empty = 0
        self._music_heard = False
        self._music_frames = 0
        self._paused = False

    def is_opus(self) -> bool:
        return False

    @property
    def has_music(self) -> bool:
        with self._lock:
            return self._music is not None

    @property
    def paused(self) -> bool:
        with self._lock:
            return bool(self._paused and self._music is not None)

    def toggle_pause(self) -> bool:
        with self._lock:
            if self._music is None:
                self._paused = False
                return False
            self._paused = not self._paused
            return self._paused

    @property
    def heard_music(self) -> bool:
        with self._lock:
            return bool(self._music_heard)

    @property
    def has_tts(self) -> bool:
        with self._lock:
            return bool(self._tts_pcm) and self._tts_off < len(self._tts_pcm)

    @property
    def music_position(self) -> float:
        with self._lock:
            return self._music_frames * 0.02

    def set_music(self, source: discord.AudioSource | None, *, token=None) -> None:
        with self._lock:
            if self._music is source:
                return
            _cleanup_source(self._music)
            if source is not None and not isinstance(source, discord.PCMVolumeTransformer):
                source = discord.PCMVolumeTransformer(source, volume=MUSIC_VOL)
            self._music = source
            self._music_token = token if source is not None else None
            self._idle_silence = 0
            self._music_empty = 0
            self._music_heard = False
            self._music_frames = 0
            self._paused = False

    def _set_music_gain(self, volume: float) -> None:
        music = self._music
        if isinstance(music, discord.PCMVolumeTransformer):
            music.volume = volume

    def play_tts_pcm(self, pcm: bytes, on_done: Callable[[], None] | None = None) -> None:
        old_callbacks: list[Callable[[], None]] = []
        with self._lock:
            old_callbacks = list(self._tts_done)
            self._tts_done.clear()
            self._tts_pcm = pcm or b""
            self._tts_off = 0
            if on_done:
                self._tts_done.append(on_done)
        for cb in old_callbacks:
            try:
                cb()
            except Exception:
                log.exception("tts done callback failed")

    def stop_tts(self) -> None:
        callbacks: list[Callable[[], None]] = []
        with self._lock:
            callbacks = list(self._tts_done)
            self._tts_done.clear()
            self._tts_pcm = b""
            self._tts_off = 0
        for cb in callbacks:
            try:
                cb()
            except Exception:
                log.exception("tts done callback failed")

    def close(self) -> None:
        self._closed = True
        self.set_music(None)
        self.stop_tts()

    def cleanup(self) -> None:
        # AudioPlayer calls this when that play() ends. Do not kill music here —
        # a queued next track may already have attached a new source.
        self.stop_tts()

    def _pop_tts_frame(self) -> tuple[bytes, list[Callable[[], None]]]:
        callbacks: list[Callable[[], None]] = []
        with self._lock:
            if not self._tts_pcm or self._tts_off >= len(self._tts_pcm):
                return b"", callbacks
            end = min(self._tts_off + FRAME_BYTES, len(self._tts_pcm))
            chunk = self._tts_pcm[self._tts_off:end]
            self._tts_off = end
            if len(chunk) < FRAME_BYTES:
                chunk += b"\x00" * (FRAME_BYTES - len(chunk))
            if self._tts_off >= len(self._tts_pcm):
                self._tts_pcm = b""
                self._tts_off = 0
                callbacks = list(self._tts_done)
                self._tts_done.clear()
            return chunk, callbacks

    def read(self) -> bytes:
        if self._closed:
            return b""
        with self._lock:
            music_src = self._music
            paused = bool(self._paused and music_src is not None)
        music_frame = b""
        music_ended = False
        ended_token = None
        if music_src is not None and paused:
            music_frame = b"\x00" * FRAME_BYTES
        elif music_src is not None:
            try:
                music_frame = music_src.read() or b""
            except Exception:
                log.exception("music mix read failed")
                music_frame = b""
            with self._lock:
                if self._music is music_src:
                    if not music_frame:
                        self._music_empty += 1
                        warmup = MUSIC_WARMUP_FRAMES if not self._music_heard else MUSIC_STALL_FRAMES
                        if self._music_empty in (50, 150) or self._music_empty == warmup:
                            log.warning(
                                "music empty frames=%s heard=%s pos=%.1f ending=%s",
                                self._music_empty,
                                self._music_heard,
                                self._music_frames * 0.02,
                                self._music_empty >= warmup,
                            )
                        if self._music_empty >= warmup:
                            ended_token = self._music_token
                            _cleanup_source(self._music)
                            self._music = None
                            self._music_token = None
                            music_ended = True
                        else:
                            music_frame = b"\x00" * FRAME_BYTES
                    elif music_frame == b"\x00" * FRAME_BYTES or not any(music_frame):
                        # Prefetch keepalive / digital silence: keep sending, don't move lyrics.
                        pass
                    else:
                        self._music_heard = True
                        self._music_empty = 0
                        self._music_frames += 1
        tts_frame, callbacks = self._pop_tts_frame()
        ducking = bool(tts_frame) or self.has_tts
        if tts_frame and music_frame:
            mixed = mix_frames(music_frame, tts_frame, music_vol=DUCK_VOL, tts_vol=TTS_VOL)
        elif tts_frame:
            mixed = mix_frames(b"", tts_frame, music_vol=0.0, tts_vol=TTS_VOL)
        elif music_frame:
            vol = DUCK_VOL if ducking else 1.0
            mixed = mix_frames(music_frame, b"", music_vol=vol, tts_vol=0.0)
        else:
            with self._lock:
                idle = self._music is None and not self._tts_pcm
                if idle:
                    # Keep play() alive with silence so Discord recv doesn't die
                    # every time we start a new voice.play() for TTS.
                    self._idle_silence += 1
                    mixed = b"\x00" * FRAME_BYTES
                else:
                    mixed = b"\x00" * FRAME_BYTES
        for cb in callbacks:
            try:
                cb()
            except Exception:
                log.exception("tts done callback failed")
        if music_ended and self._on_music_end:
            try:
                self._on_music_end(ended_token)
            except Exception:
                log.exception("music end callback failed")
        return mixed


class MusicManager:
    def __init__(self) -> None:
        self._guilds: dict[int, GuildMusic] = {}

    def state(self, guild_id: int) -> GuildMusic:
        item = self._guilds.get(guild_id)
        if item is None:
            item = GuildMusic()
            self._guilds[guild_id] = item
        return item

    def clear(self, guild_id: int) -> None:
        item = self._guilds.pop(int(guild_id), None)
        if not item:
            return
        task = getattr(item, "lyrics_task", None)
        if task is not None:
            try:
                task.cancel()
            except Exception:
                pass
        if item.mixer:
            item.mixer.close()

    async def enqueue(self, query: str) -> Track:
        return await asyncio.to_thread(_extract_track, query)
