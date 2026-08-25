from __future__ import annotations

import re
from typing import Callable

from opus.hub import hub
from opus.logutil import get_logger
from opus.settings import Settings

log = get_logger()

SHOWDOWN_RE = re.compile(
    r"\b(pokemon|pokémon|poke\s*mons?|pokeman|pocket\s*monsters?|pkmn|"
    r"showdown|psim|poke)\b",
    re.IGNORECASE,
)
MANIA_RE = re.compile(
    r"\b(osu!?|mania|webosu|webosumania|web-osu-mania)\b",
    re.IGNORECASE,
)
CHESS_RE = re.compile(
    r"\b(chess|lichess|stockfish)\b",
    re.IGNORECASE,
)
BARE_PLAY_RE = re.compile(
    r"^\s*(play(?:\s+(?:this|the|my)?\s*(?:game|match|board|turn))?|"
    r"play\s+(?:for\s+me|now)|start\s+playing)\s*[.!]*\s*$",
    re.IGNORECASE,
)


def _url(url: str) -> str:
    return (url or "").lower()


def classify_play(rest: str = "", url: str = "") -> str | None:
    """Return showdown, mania, chess, or None if it is a bare/unknown play command."""
    text = (rest or "").strip()
    href = _url(url)
    if SHOWDOWN_RE.search(text) or "pokemonshowdown" in href or "psim.us" in href:
        return "showdown"
    if MANIA_RE.search(text) or "webosumania" in href or "web-osu-mania" in href:
        return "mania"
    if CHESS_RE.search(text) or "chess.com" in href or "lichess.org" in href:
        return "chess"
    return None


def is_game_play_query(text: str) -> bool:
    return bool(SHOWDOWN_RE.search(text or "") or MANIA_RE.search(text or "") or CHESS_RE.search(text or ""))


def detect_open_game() -> str | None:
    href = _url(hub.get_browser_context().get("url") or "")
    kind = classify_play("", href)
    if kind:
        return kind
    try:
        result = hub.browser_command("play_detect", {}, timeout=6.0)
    except Exception:
        log.exception("play_detect failed")
        return None
    payload = result.get("result") if result.get("ok") else None
    if not isinstance(payload, dict):
        return None
    active = _url(payload.get("url") or "")
    kind = classify_play("", active)
    if kind:
        return kind
    if payload.get("showdown"):
        return "showdown"
    if payload.get("mania"):
        return "mania"
    if payload.get("chess"):
        return "chess"
    return None


def start_play(
    settings: Settings,
    rest: str = "",
    *,
    url: str = "",
    speak: Callable[[str], None] | None = None,
) -> str:
    from opus.game.agent import game_agent
    from opus.game.chess_player import chess_player
    from opus.game.mania_player import mania_player
    from opus.game.showdown_player import showdown_player

    kind = classify_play(rest, url)
    if kind is None and BARE_PLAY_RE.match((rest or "").strip()):
        kind = detect_open_game()
    elif kind is None:
        kind = classify_play("", url) or detect_open_game()

    if kind == "showdown":
        chess_player.stop("Switching to Showdown.")
        mania_player.stop("Switching to Showdown.")
        game_agent.stop("Switching to Showdown.")
        return showdown_player.start(settings, speak=speak, create_tab=True)
    if kind == "mania":
        chess_player.stop("Switching to mania.")
        showdown_player.stop("Switching to mania.")
        game_agent.stop("Switching to mania.")
        return mania_player.start(settings, speak=speak)
    if kind == "chess":
        mania_player.stop("Switching to chess.")
        showdown_player.stop("Switching to chess.")
        game_agent.stop("Switching to chess.")
        return chess_player.start(settings, speak=speak)

    return (
        "Which game — say play showdown, play osu, or play chess. "
        "I won't open Spotify or osu unless you name them."
    )
