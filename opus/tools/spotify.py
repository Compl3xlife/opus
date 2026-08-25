"""Spotify voice commands — API playback, not window clicking."""
from __future__ import annotations

import re

from opus.settings import Settings

KNOWN_COLLECTIONS: dict[str, str] = {
    "liked songs": "liked songs",
    "liked": "liked songs",
    "likes": "liked songs",
    "my liked songs": "liked songs",
    "my likes": "liked songs",
    "like songs": "liked songs",
}

RESUME_QUERIES = frozenset(
    {
        "",
        "spotify",
        "music",
        "my music",
        "something",
        "a song",
        "some music",
        "play",
        "resume",
        "start",
    }
)

PLAYLIST_WORDS = re.compile(r"\b(songs?|playlist|playlists?|mix|collection)\b", re.IGNORECASE)
SPOTIFY_TAIL_RE = re.compile(r"\s+(?:on|in|from)\s+spotify\b.*$", re.IGNORECASE)
SPOTIFY_FILLER_RE = re.compile(r"\s+(?:please|now|for me)\s*[.!?,]*$", re.IGNORECASE)
SPOTIFY_RESUME_PHRASE_RE = re.compile(
    r"^\s*(?:play|resume|start|unpause)(?:\s+(?:on|my))?\s*(?:spotify|music)?\s*[.!?,]*\s*$|"
    r"^\s*(?:spotify|music)\s+(?:play|resume|start)\s*[.!?,]*\s*$|"
    r"^\s*(?:spotify|music|play|resume|start)\s*[.!?,]*\s*$",
    re.IGNORECASE,
)
SPOTIFY_PLAY_QUERY_RE = re.compile(
    r"\b(?:play|start)\s+(.+?)(?:\s+(?:on|in|from)\s+spotify\b)",
    re.IGNORECASE,
)


def _clean_utterance(text: str) -> str:
    return re.sub(r"^\s*opus[,:]?\s*", "", (text or "").strip(), flags=re.IGNORECASE).strip()


def normalize_spotify_query(query: str) -> str:
    """Strip wake-word leftovers, 'on spotify', and filler from a play query."""
    q = (query or "").strip()
    q = q.strip(".!?, ")
    q = SPOTIFY_TAIL_RE.sub("", q).strip()
    q = re.sub(r"\bspotify\b", "", q, flags=re.IGNORECASE).strip(" ,.-")
    q = SPOTIFY_FILLER_RE.sub("", q).strip()
    q = re.sub(r"^\s*(?:play|start|resume|unpause)\s+", "", q, flags=re.IGNORECASE).strip()
    return q.strip(".!?, ")


def should_resume_spotify(query: str = "", raw: str = "") -> bool:
    for candidate in (_clean_utterance(raw), (query or "").strip()):
        if not candidate:
            continue
        if SPOTIFY_RESUME_PHRASE_RE.match(candidate):
            return True
        normalized = normalize_spotify_query(candidate).lower()
        if normalized in RESUME_QUERIES:
            return True
    normalized = normalize_spotify_query(query or _clean_utterance(raw)).lower()
    return normalized in RESUME_QUERIES


def extract_spotify_query(text: str) -> str | None:
    """Return a play query, empty string for resume, or None if not a Spotify play command."""
    cleaned = _clean_utterance(text)
    if not cleaned:
        return None
    if not re.search(r"\bspotify\b", cleaned, re.IGNORECASE):
        return None
    if not re.search(r"\b(?:play|resume|start|unpause)\b", cleaned, re.IGNORECASE):
        return None
    if should_resume_spotify(raw=cleaned):
        return ""
    match = SPOTIFY_PLAY_QUERY_RE.search(cleaned)
    if match:
        query = normalize_spotify_query(match.group(1))
        return "" if should_resume_spotify(query=query) else query
    match = re.search(r"\bplay\s+(.+)$", cleaned, re.IGNORECASE)
    if match:
        query = normalize_spotify_query(match.group(1))
        return "" if should_resume_spotify(query=query) else query
    return ""


def try_spotify_from_text(text: str) -> str | None:
    query = extract_spotify_query(text)
    if query is None:
        return None
    return play_spotify(query, raw=text)


def _collection_query(query: str) -> str | None:
    q = query.lower().strip().rstrip(".")
    if q in KNOWN_COLLECTIONS:
        return KNOWN_COLLECTIONS[q]
    if PLAYLIST_WORDS.search(q):
        root = PLAYLIST_WORDS.sub("", q).strip()
        if root in ("liked", "like", "likes", "my liked", "my like", "my likes", "my"):
            return KNOWN_COLLECTIONS["liked songs"]
    return None


def _settings() -> Settings:
    return Settings()


def play_spotify(query: str = "", *, raw: str = "") -> str:
    """Play or resume Spotify through the Web API."""
    from opus.apps import spotify as spotify_api
    from opus.runtime import is_phone

    combined = f"{query} {raw}".strip()
    if not is_phone():
        from opus.game.play_router import is_game_play_query, start_play

        if is_game_play_query(combined):
            return start_play(_settings(), combined)

    query = normalize_spotify_query(query)
    settings = _settings()
    if should_resume_spotify(query=query, raw=raw):
        return spotify_api.resume(settings)

    collection = _collection_query(query)
    if collection:
        return spotify_api.play(settings, collection)
    if not query:
        return spotify_api.resume(settings)
    return spotify_api.play(settings, query)


def control_spotify(action: str, query: str = "") -> str:
    from opus.apps import spotify as spotify_api

    return spotify_api.control(_settings(), action, query)
