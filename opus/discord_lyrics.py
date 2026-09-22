"""Synced lyrics for the Discord in-call player."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

import httpx

from opus.logutil import get_logger

log = get_logger()

LYRICS_RE = re.compile(
    r"^(lyrics|lyric|show\s+lyrics|play[\s-]*along(\s+lyrics)?)\s*[.!?]*$",
    re.IGNORECASE,
)
LRC_LINE_RE = re.compile(r"\[(\d{1,2}):(\d{2}(?:\.\d+)?)\](.*)")
LRC_OFFSET_RE = re.compile(r"\[offset:\s*([+-]?\d+)\s*\]", re.IGNORECASE)
JUNK_RE = re.compile(
    r"\s*[\(\[]([^)\]]*(official|audio|video|lyrics|visuali[sz]er|hd|4k|"
    r"remaster|lyric\s*video|slowed|reverb|tiktok|theme)[^)\]]*)[\)\]]",
    re.IGNORECASE,
)
FEAT_RE = re.compile(r"\s*[\(\[]?\s*(feat\.?|ft\.?|featuring)\s+.+?[\)\]]?\s*$", re.IGNORECASE)
LRCLIB = "https://lrclib.net/api"
# Mixer position is frames of real audio sent. Discord still plays a beat later.
HEAR_LAG = 1.15
WINDOW_BEFORE = 1
WINDOW_AFTER = 2
HUD_PLAYING = 0xC84BFF
HUD_WAIT = 0x64748B
HUD_PAUSED = 0xFFC14D
ANSI = "\u001b"


@dataclass
class LyricLine:
    start: float
    text: str


@dataclass
class Lyrics:
    lines: list[LyricLine] = field(default_factory=list)
    plain: str = ""
    duration: float = 0.0
    label: str = ""


def clean_track_title(title: str) -> str:
    cleaned = JUNK_RE.sub("", title or "")
    cleaned = FEAT_RE.sub("", cleaned)
    cleaned = re.sub(r"\s*[\-|–—]\s*(official|audio|lyrics|video).*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—")
    return cleaned or (title or "").strip()


def split_artist_title(title: str, artist: str = "") -> tuple[str, str]:
    artist = clean_track_title(artist)
    title = clean_track_title(title)
    if artist:
        for sep in (" - ", " – ", " — ", " | ", "-"):
            lead = f"{artist}{sep}"
            if title.lower().startswith(lead.lower()):
                title = title[len(lead):].strip()
                break
        return artist, title or artist
    for sep in (" - ", " – ", " — ", " | "):
        if sep in title:
            left, right = title.split(sep, 1)
            if left and right:
                return left.strip(), right.strip()
    return artist, title


def parse_lrc(raw: str) -> list[LyricLine]:
    offset = 0.0
    match = LRC_OFFSET_RE.search(raw or "")
    if match:
        # Positive offset = lyrics appear sooner, so subtract from timestamps.
        offset = int(match.group(1)) / 1000.0
    lines: list[LyricLine] = []
    for row in (raw or "").splitlines():
        hit = LRC_LINE_RE.search(row)
        if not hit:
            continue
        minutes = int(hit.group(1))
        seconds = float(hit.group(2))
        text = (hit.group(3) or "").strip()
        if not text or text in {"♪", "♫", "...", "…"}:
            continue
        start = max(0.0, minutes * 60 + seconds - offset)
        lines.append(LyricLine(start=start, text=text[:140]))
    lines.sort(key=lambda item: item.start)
    return lines


def align_lyric_times(lines: list[LyricLine], lyrics_duration: float, track_duration: float) -> list[LyricLine]:
    """Stretch or nudge LRC timestamps to the YouTube/audio length actually playing."""
    if not lines:
        return lines
    last = lines[-1].start
    src = float(lyrics_duration or 0) or (last + 4.0)
    dst = float(track_duration or 0) or src
    if src <= 1 or dst <= 1:
        return lines
    ratio = dst / src
    if abs(1.0 - ratio) < 0.012:
        return lines
    if 0.84 <= ratio <= 1.18:
        return [LyricLine(start=line.start * ratio, text=line.text) for line in lines]
    gap = dst - src
    if 2.0 <= gap <= 40.0 and lines[0].start < 10.0:
        nudge = min(gap * 0.35, 12.0)
        return [LyricLine(start=line.start + nudge, text=line.text) for line in lines]
    return lines


def heard_position(position: float, *, lag: float = HEAR_LAG, duration: float = 0.0) -> float:
    heard = max(0.0, float(position or 0) - lag)
    if duration > 0:
        return min(heard, float(duration))
    return heard


def current_index(lines: list[LyricLine], position: float, *, lag: float = HEAR_LAG, duration: float = 0.0) -> int:
    if not lines:
        return -1
    heard = heard_position(position, lag=lag, duration=duration)
    idx = -1
    for i, line in enumerate(lines):
        if heard >= line.start:
            idx = i
        else:
            break
    return idx


def lyrics_index(lines: list[LyricLine], position: float, *, plain: str = "", duration: float = 0.0) -> int:
    if lines:
        return current_index(lines, position, duration=duration)
    parts = _plain_lines(plain)
    return _plain_index(parts, position, duration)


def _plain_lines(plain: str) -> list[str]:
    return [row.strip() for row in (plain or "").splitlines() if row.strip()]


def _plain_index(parts: list[str], position: float, duration: float) -> int:
    if not parts:
        return -1
    if duration <= 0:
        return 0
    frac = min(0.99, max(0.0, heard_position(position, duration=duration) / duration))
    return min(len(parts) - 1, int(frac * len(parts)))


def _fmt_time(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _ansi(code: str, text: str) -> str:
    return f"{ANSI}[{code}m{text}{ANSI}[0m"


def _safe_lyric(text: str) -> str:
    cleaned = (text or "").replace("`", "'").replace(ANSI, "").replace("\r", " ").strip()
    return cleaned[:90] or "…"


def _progress_bar(position: float, duration: float, width: int = 16) -> str:
    if duration <= 0:
        return _ansi("2;37", _fmt_time(position))
    frac = min(1.0, max(0.0, position / duration))
    filled = int(round(frac * width))
    filled = min(width, max(0, filled))
    bar = _ansi("1;35", "█" * filled) + _ansi("2;35", "░" * (width - filled))
    return f"{bar}  {_ansi('1;37', _fmt_time(position))} {_ansi('2;37', '/ ' + _fmt_time(duration))}"


def lyrics_embed(
    title: str,
    lines: list[LyricLine],
    position: float,
    *,
    plain: str = "",
    duration: float = 0.0,
    artist: str = "",
    paused: bool = False,
):
    import discord

    artist, song = split_artist_title(title, artist)
    heard = heard_position(position, duration=duration)
    if paused:
        color = HUD_PAUSED
        status = "paused"
    elif lines and current_index(lines, position, duration=duration) < 0:
        color = HUD_WAIT
        status = "intro"
    else:
        color = HUD_PLAYING
        status = "live"
    embed = discord.Embed(color=color)
    embed.title = song[:200] or "Lyrics"
    if artist:
        embed.set_author(name=artist[:80])

    if lines:
        idx = current_index(lines, position, duration=duration)
        embed.description = _hud_block(lines, idx, heard, duration)
        embed.set_footer(text=f"♪  {status}")
        return embed

    parts = _plain_lines(plain)
    if parts:
        idx = _plain_index(parts, position, duration)
        fake = [LyricLine(start=0.0, text=row) for row in parts]
        embed.description = _hud_block(fake, idx, heard, duration, synced=False)
        embed.set_footer(text=f"♪  unsynced  ·  {status}")
        return embed

    embed.description = _ansi_wrap(_ansi("2;37", "no lyrics for this track"))
    embed.set_footer(text=status)
    return embed


def _ansi_wrap(body: str) -> str:
    return f"```ansi\n{body}\n```"


def _hud_block(
    lines: list[LyricLine],
    idx: int,
    position: float,
    duration: float,
    *,
    synced: bool = True,
) -> str:
    rows: list[str] = []
    if not lines:
        rows.append(_ansi("2;37", "  getting ready…"))
    elif idx < 0:
        rows.append(_ansi("2;37", "  waiting for vocals…"))
        rows.append("")
        rows.append(_ansi("1;36", f"  ▸ {_safe_lyric(lines[0].text)}"))
        for line in lines[1:WINDOW_AFTER]:
            rows.append(_ansi("2;36", f"    {_safe_lyric(line.text)}"))
    else:
        start = max(0, idx - WINDOW_BEFORE)
        end = min(len(lines), idx + WINDOW_AFTER + 1)
        for i in range(start, end):
            text = _safe_lyric(lines[i].text)
            if i == idx:
                if rows:
                    rows.append("")
                rows.append(_ansi("1;37;45", f" ▶  {text}  "))
                rows.append("")
            elif i < idx:
                rows.append(_ansi("2;37", f"    {text}"))
            elif i == idx + 1:
                rows.append(_ansi("1;36", f"  ▸ {text}"))
            else:
                rows.append(_ansi("2;36", f"    {text}"))
    rows.append("")
    rows.append(_progress_bar(position, duration))
    if not synced:
        rows.append(_ansi("2;33", "  timed by length, not word-by-word"))
    return _ansi_wrap("\n".join(rows))


def _similar(a: str, b: str) -> float:
    left = (a or "").strip().lower()
    right = (b or "").strip().lower()
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        return 0.86
    return SequenceMatcher(None, left, right).ratio()


def _score_hit(hit: dict, artist: str, title: str, duration: float) -> float:
    if not isinstance(hit, dict):
        return -1.0
    synced = (hit.get("syncedLyrics") or "").strip()
    plain = (hit.get("plainLyrics") or "").strip()
    if not synced and not plain:
        return -1.0
    title_score = _similar(title, str(hit.get("trackName") or ""))
    artist_score = _similar(artist, str(hit.get("artistName") or "")) if artist else 0.45
    hit_dur = float(hit.get("duration") or 0)
    if duration > 0 and hit_dur > 0:
        gap = abs(duration - hit_dur)
        dur_score = max(0.0, 1.0 - gap / max(duration, 1.0))
        if gap > 12:
            dur_score *= 0.15
        elif gap > 6:
            dur_score *= 0.45
    else:
        dur_score = 0.35
    bonus = 0.2 if synced else 0.0
    return title_score * 3.2 + artist_score * 1.8 + dur_score * 2.4 + bonus


def _hit_to_lyrics(hit: dict) -> Lyrics:
    synced = parse_lrc(str(hit.get("syncedLyrics") or ""))
    plain = str(hit.get("plainLyrics") or "").strip()
    label = " — ".join(
        part for part in (str(hit.get("artistName") or "").strip(), str(hit.get("trackName") or "").strip()) if part
    )
    return Lyrics(
        lines=synced,
        plain=plain,
        duration=float(hit.get("duration") or 0),
        label=label,
    )


def fetch_lyrics(title: str, artist: str = "", duration: float = 0.0) -> list[LyricLine]:
    return fetch_lyrics_result(title, artist, duration).lines


def fetch_lyrics_result(title: str, artist: str = "", duration: float = 0.0) -> Lyrics:
    artist, title = split_artist_title(title, artist)
    if not title:
        return Lyrics()
    headers = {"User-Agent": "Opus/1.0 (Discord lyrics)"}
    try:
        with httpx.Client(timeout=8.0, headers=headers) as client:
            hit = _get_exact(client, artist, title, duration)
            if hit is None:
                hit = _search_best(client, artist, title, duration)
    except Exception:
        log.exception("lyrics search failed title=%s artist=%s", title, artist)
        return Lyrics()
    if not hit:
        return Lyrics()
    result = _hit_to_lyrics(hit)
    log.info(
        "lyrics matched %s lines=%s duration=%.1f vs %.1f",
        result.label or title,
        len(result.lines),
        result.duration,
        duration,
    )
    return result


def _get_exact(client: httpx.Client, artist: str, title: str, duration: float) -> dict | None:
    if not artist or duration <= 0:
        return None
    try:
        response = client.get(
            f"{LRCLIB}/get",
            params={
                "artist_name": artist,
                "track_name": title,
                "duration": int(round(duration)),
            },
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json()
    except Exception:
        return None
    if isinstance(data, dict) and (data.get("syncedLyrics") or data.get("plainLyrics")):
        return data
    return None


def _search_best(client: httpx.Client, artist: str, title: str, duration: float) -> dict | None:
    queries = []
    if artist:
        queries.append({"track_name": title, "artist_name": artist})
        queries.append({"q": f"{artist} {title}"})
    queries.append({"q": title})
    best: tuple[float, dict] | None = None
    seen: set[str] = set()
    for params in queries:
        try:
            response = client.get(f"{LRCLIB}/search", params=params)
            response.raise_for_status()
            hits = response.json()
        except Exception:
            continue
        if not isinstance(hits, list):
            continue
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            key = f"{hit.get('id')}|{hit.get('trackName')}|{hit.get('artistName')}|{hit.get('duration')}"
            if key in seen:
                continue
            seen.add(key)
            score = _score_hit(hit, artist, title, duration)
            if score < 2.4:
                continue
            if best is None or score > best[0]:
                best = (score, hit)
        if best and best[0] >= 6.0:
            break
    return best[1] if best else None
