"""Per-guild in-call music for Opus. Never shares a queue across servers."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import discord

from opus.logutil import get_logger

log = get_logger()


@dataclass
class Track:
    title: str
    stream_url: str
    page_url: str = ""


@dataclass
class GuildMusic:
    queue: list[Track] = field(default_factory=list)
    current: Track | None = None
    paused_for_tts: bool = False


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
        return Track(
            title=str(info.get("title") or query)[:120],
            stream_url=url,
            page_url=str(info.get("webpage_url") or query),
        )


def ffmpeg_source(stream_url: str):
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    return discord.FFmpegPCMAudio(
        stream_url,
        executable=ffmpeg,
        before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin",
        options="-vn",
    )


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
        self._guilds.pop(int(guild_id), None)

    async def enqueue(self, query: str) -> Track:
        return await asyncio.to_thread(_extract_track, query)
