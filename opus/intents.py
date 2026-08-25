from __future__ import annotations

import re
from dataclasses import dataclass

from opus.runtime import is_phone
from opus.tools.spotify import extract_spotify_query, should_resume_spotify, normalize_spotify_query


WAKE_RE = re.compile(r"\bopus\b", re.IGNORECASE)

PANEL_WORD = r"(panel|pannel|pane|window|settings|menu|overlay)"

OPEN_PANEL_RE = re.compile(
    rf"\b(open|show|bring up|pull up)\b.*\b{PANEL_WORD}\b",
    re.IGNORECASE,
)
CLOSE_PANEL_RE = re.compile(
    rf"\b(close|hide|dismiss|shut|minimize)\b.*\b{PANEL_WORD}\b",
    re.IGNORECASE,
)
CLOSE_SHORT_RE = re.compile(
    r"\b(close|hide|dismiss|shut)\s+(it|that|this)\b|"
    r"\b(put (it|that) away|go away|get (out of the way|lost))\b",
    re.IGNORECASE,
)
SPOTIFY_RESUME_RE = re.compile(
    r"\b(?:play|resume|start|unpause)\s+(?:on\s+)?spotify\b|\bspotify\s+(?:play|resume|start)\b",
    re.IGNORECASE,
)
SPOTIFY_PLAY_RE = re.compile(
    r"\b(?:play|start)\s+(.+?)(?:\s+(?:on|in|from)\s+spotify)?(?:\s+(?:please|now|for me))?[.!?,]*\s*$",
    re.IGNORECASE,
)
SLEEP_RE = re.compile(
    r"\b("
    r"stop listening|"
    r"go to sleep|"
    r"good ?night|"
    r"that'?s all|"
    r"never ?mind|"
    r"(go\s+)?(to\s+)?sleep(\s+now)?|"
    r"\bnap\b"
    r")\b",
    re.IGNORECASE,
)
SHUTDOWN_RE = re.compile(r"\b(shut\s*down|turn\s*off|power\s*off|shut\s*off)\b", re.IGNORECASE)
DISCORD_MOD_RE = re.compile(
    r"\b(server\s+)?(mute|unmute|deafen|undeafen|kick|timeout|time\s*out|untimeout)\s+"
    r"(all|everyone|everybody|every\s+one|.+)",
    re.IGNORECASE,
)
WAKE_ONLY_RE = re.compile(
    r"^\s*(hey|ok|okay|hi)?\s*opus\s*[,.!?:]*\s*$",
    re.IGNORECASE,
)
MUTE_RE = re.compile(r"\b(mute|disable)\b.*\b(voice|speech|talking)\b", re.IGNORECASE)
UNMUTE_RE = re.compile(r"\b(unmute|enable)\b.*\b(voice|speech|talking)\b", re.IGNORECASE)
DISCORD_SELF_RE = re.compile(r"\bself[\s-]*(mute|unmute|deafen|undeafen|disconnect|leave)\b", re.IGNORECASE)
DISCORD_LEAVE_RE = re.compile(r"\b(leave|hang\s*up|disconnect)\s+(the\s+)?(call|vc|voice|channel)\b", re.IGNORECASE)
DISCORD_JOIN_RE = re.compile(
    r"\b(join|enter|hop\s+in(?:to)?|come\s+(?:in|to|into))\s+(the\s+)?(call|vc|voice(\s+channel)?)\b",
    re.IGNORECASE,
)
DISCORD_BOT_LEAVE_RE = re.compile(
    r"\b(leave|drop|get out of)\s+(the\s+)?(voice|vc)\b",
    re.IGNORECASE,
)
DISCORD_SEARCH_RE = re.compile(
    r"\bsearch\s+(?:for\s+|in\s+)?(?:my\s+)?(?:dms?|discord|messages?)\s*(?:for\s+)?(.+)?",
    re.IGNORECASE,
)
COINFLIP_RE = re.compile(
    r"\b(flip(?:\s+a|\s+the)?\s+coins?|coin\s*flips?|heads\s+or\s+tails)\b",
    re.IGNORECASE,
)
DICE_RE = re.compile(
    r"\b(roll(?:\s+a|\s+the)?\s+dice|roll(?:\s+a)?\s+die|\bd(?:20|12|10|8|6|4)\b)\b",
    re.IGNORECASE,
)
PLAY_IN_CALL_RE = re.compile(
    r"\b(?:play|queue)\s+(.+?)\s+in\s+(the\s+)?(call|vc|voice(\s+channel)?)\b",
    re.IGNORECASE,
)
STOP_CALL_MUSIC_RE = re.compile(
    r"\b(stop|pause)\s+(the\s+)?(music|song|track)\s+in\s+(the\s+)?(call|vc|voice)\b|"
    r"\bstop\s+playing\s+(music|the\s+song)\s+in\s+(the\s+)?(call|vc)\b",
    re.IGNORECASE,
)
SKIP_SONG_RE = re.compile(
    r"\bskip(\s+(the\s+)?(song|track|music))?(\s+in\s+(the\s+)?(call|vc|voice))?\b",
    re.IGNORECASE,
)
SPOTIFY_PAUSE_RE = re.compile(
    r"\b(pause|stop)\s+(the\s+)?(music|song|track|spotify)\b|"
    r"\bpause\s+spotify\b|"
    r"\bspotify\s+pause\b",
    re.IGNORECASE,
)
SPOTIFY_SKIP_RE = re.compile(
    r"\b(skip|next)\s+(this\s+|the\s+)?(song|track|tune)\b|"
    r"\bnext\s+song\b|"
    r"\bskip\s+this\b|"
    r"\bspotify\s+(skip|next)\b",
    re.IGNORECASE,
)
SPOTIFY_PREV_RE = re.compile(
    r"\b(previous|last)\s+(song|track)\b|"
    r"\bgo\s+back\s+(a\s+)?(song|track)\b|"
    r"\bspotify\s+(previous|back)\b",
    re.IGNORECASE,
)
SPOTIFY_NOW_RE = re.compile(
    r"\bwhat('?s| is) (playing|this song|the song)\b|"
    r"\bwhat song is (this|playing)\b|"
    r"\bnow playing\b",
    re.IGNORECASE,
)
VOLUME_RE = re.compile(r"\bvolume\b(?:\s+(up|down|to)\s*(\d{1,3})?)?", re.IGNORECASE)
START_REC_RE = re.compile(
    r"\b(start|begin)\b.*\b(record|recording|captur)|"
    r"\brecord (the )?(screen|desktop|this|that)\b",
    re.IGNORECASE,
)
STOP_REC_RE = re.compile(
    r"\b(stop|end|finish|save)\b.*\b(record|recording)\b",
    re.IGNORECASE,
)
CLIP_RE = re.compile(
    r"\bclip(?:\s+(that|this|it|the last))?\b|"
    r"\b(save (a )?clip|instant replay|save that( clip)?)\b",
    re.IGNORECASE,
)
CREATOR_RE = re.compile(
    r"\b(who (created|made|built|coded|programmed|developed)|"
    r"who'?s your (creator|maker)|your creator|"
    r"who (are|is) your (creator|maker)|who owns you)\b",
    re.IGNORECASE,
)
SHOT_RE = re.compile(
    r"\b(screenshot|screen shot|take a (screen ?)?shot|capture (the )?screen)\b",
    re.IGNORECASE,
)
PLAY_GAME_RE = re.compile(
    r"\b("
    r"play\s+(this|the|my)?\s*(game|match|board|turn)|"
    r"take\s+over(\s+(the|this)\s+game)?|"
    r"(you|opus)\s+play(\s+(for\s+me|this|the\s+game))?|"
    r"start\s+playing|"
    r"play\s+(for\s+me|now)|"
    r"beat\s+(this|the)\s+(game|level|opponent)|"
    r"win\s+(this|the)\s+(game|match)"
    r")\b",
    re.IGNORECASE,
)
PLAY_OSU_RE = re.compile(
    r"\b("
    r"play\s+(osu!?|mania|webosu|web\s*osu(?:\s*mania)?|webosumania)|"
    r"(start|enable)\s+(osu!?|mania)|"
    r"osu!?\s*mania|"
    r"webosumania"
    r")\b",
    re.IGNORECASE,
)
PLAY_SHOWDOWN_RE = re.compile(
    r"\b("
    r"play\s+(?:the\s+)?(?:pokemon|pokémon|poke\s*mons?|pokeman|pocket\s*monsters?|pkmn|poke|showdown|psim|"
    r"pokemon\s+showdown|pokémon\s+showdown)|"
    r"(start|enable)\s+(?:the\s+)?(?:pokemon|pokémon|showdown)|"
    r"pokemon\s+showdown|"
    r"pokémon\s+showdown|"
    r"poke\s*showdown|"
    r"psim"
    r")\b",
    re.IGNORECASE,
)
GAME_PLAY_QUERY_RE = re.compile(
    r"\b(pokemon|pokémon|poke\s*mons?|pokeman|pocket\s*monsters?|pkmn|showdown|"
    r"osu!?|mania|webosu|webosumania|chess|lichess|psim|poke)\b",
    re.IGNORECASE,
)
PLAY_CHESS_RE = re.compile(
    r"\b("
    r"play\s+(chess|this\s+chess|the\s+chess)|"
    r"chess\s+(engine|mode|please)|"
    r"(start|enable)\s+(the\s+)?chess|"
    r"take\s+over\s+(this\s+)?chess|"
    r"win\s+(this\s+)?chess|"
    r"sack?\s+pawns?|"
    r"sacrifice.+\bchess\b|"
    r"\bchess\b.+\b(play|win|engine)\b"
    r")\b",
    re.IGNORECASE,
)
STOP_PLAY_RE = re.compile(
    r"\b("
    r"stop\s+playing(\s+chess)?|"
    r"quit\s+playing(\s+chess)?|"
    r"stop\s+(the\s+)?game\s+agent|"
    r"give\s+(me\s+)?(back\s+)?control|"
    r"stop\s+taking\s+over|"
    r"stop\s+chess|"
    r"quit\s+chess|"
    r"stop\s+(pokemon|pokémon|showdown)|"
    r"quit\s+(pokemon|pokémon|showdown)"
    r")\b",
    re.IGNORECASE,
)
RESTART_RE = re.compile(
    r"\b(restart|reboot|reload|relaunch)\s+(opus|yourself|the (program|app|assistant))\b|"
    r"\bopus\s+(restart|reboot|reload|relaunch)\b|"
    r"\brestart yourself\b",
    re.IGNORECASE,
)
TYPE_RE = re.compile(
    r"\b(?:please\s+|can you\s+|could you\s+)?(type|paste)\s+(?:this|that)?\s*[:,-]?\s*(.+)$",
    re.IGNORECASE,
)
SHUSH_RE = re.compile(
    r"\b(shut up|stop talking|be quiet|cancel that|forget (that|it)|never ?mind)\b",
    re.IGNORECASE,
)
PNG_RE = re.compile(
    r"\b(generate|make|draw|create)\b.*\b(png|image|picture|pic)\b|"
    r"\b(png|image) generator\b",
    re.IGNORECASE,
)
FILL_LOGIN_RE = re.compile(
    r"\b(log me in(?:to)?|log into|login to|sign me in(?:to)?|type (?:my )?password|fill (?:the )?login)\b",
    re.IGNORECASE,
)
DISMISS_RE = re.compile(r"\b(thank you|thanks|thank ya|appreciate it)\b", re.IGNORECASE)
WEATHER_RE = re.compile(
    r"\b(weather|temperature|forecast|how hot|how cold|will it rain|going to rain|rain today)\b",
    re.IGNORECASE,
)
LOCATION_RE = re.compile(
    r"\b(where am i|my location|what city am i in|where('?s| is) my pc)\b",
    re.IGNORECASE,
)
SCAN_RE = re.compile(
    r"\b("
    r"(?:extract\s+and\s+)?scan(?:\s+(?:this|that|the|my|it))?(?:\s+(?:file|folder|zip|archive|download|downloads|desktop|documents?))?|"
    r"cyber\s*box|"
    r"(?:run|open)\s+(?:this|that|the\s+folder|it)\s+in\s+(?:the\s+)?(?:cyber\s*box|sandbox|box)"
    r")\b",
    re.IGNORECASE,
)
INSTALL_EXT_RE = re.compile(
    r"\b(?:install|add|get)\s+(?:the\s+)?(.+?)(?:\s+(?:extension|addon|add-on|plugin))?\s*$",
    re.IGNORECASE,
)
REMOVE_EXT_RE = re.compile(
    r"\b(?:remove|uninstall|delete|unpin)\s+(?:the\s+)?(.+?)(?:\s+(?:extension|addon|add-on|plugin))?\s*$",
    re.IGNORECASE,
)
EXT_BROWSER_WORDS = {
    "opera",
    "opera gx",
    "operagx",
    "chrome",
    "google chrome",
    "edge",
    "firefox",
    "brave",
    "vivaldi",
    "browser",
}

OPEN_APP_RE = re.compile(
    r"\b(?:open|launch|start|run|bring up|pull up)[,\s]+(.+?)(?:\s+(?:for me|please))?\s*[.!]?$",
    re.IGNORECASE,
)
OPEN_IN_BROWSER_RE = re.compile(
    r"\b(?:open|launch|go to|pull up)[,\s]+(.+?)\s+(?:in|on|with|using)\s+(.+?)\s*[.!]?$",
    re.IGNORECASE,
)
OPEN_TAB_RE = re.compile(
    r"\b(?:open|go to|navigate to|pull up|bring up)\s+(?:a\s+)?(?:new\s+)?(?:tab|window)\b",
    re.IGNORECASE,
)
OPEN_URL_RE = re.compile(
    r"\b(?:open|go to|navigate to|pull up|bring up|search|look up)\s+(.+)",
    re.IGNORECASE,
)
URL_LIKE_RE = re.compile(r"^[\w.-]+\.(com|net|org|io|gg|tv|co|dev|app|me|xyz)\b")

COMMON_SITES: dict[str, str] = {
    "youtube": "https://youtube.com",
    "yt": "https://youtube.com",
    "google": "https://google.com",
    "gmail": "https://mail.google.com",
    "twitter": "https://twitter.com",
    "x": "https://x.com",
    "reddit": "https://reddit.com",
    "twitch": "https://twitch.tv",
    "github": "https://github.com",
    "instagram": "https://instagram.com",
    "insta": "https://instagram.com",
    "facebook": "https://facebook.com",
    "fb": "https://facebook.com",
    "tiktok": "https://tiktok.com",
    "amazon": "https://amazon.com",
    "netflix": "https://netflix.com",
    "hulu": "https://hulu.com",
    "wikipedia": "https://wikipedia.org",
    "chatgpt": "https://chat.openai.com",
    "linkedin": "https://linkedin.com",
    "pinterest": "https://pinterest.com",
    "soundcloud": "https://soundcloud.com",
    "crunchyroll": "https://crunchyroll.com",
}


@dataclass
class Intent:
    name: str
    rest: str = ""
    value: int | None = None


# Intents that are safe / useful to run concurrently with each other.
PARALLEL_INTENTS = frozenset(
    {
        "discord_mod",
        "discord_self_mute",
        "discord_self_deafen",
        "discord_leave_call",
        "discord_join_call",
        "discord_bot_leave",
        "discord_search",
        "coinflip",
        "dice",
        "discord_play_music",
        "discord_skip_music",
        "discord_stop_music",
        "mute_voice",
        "unmute_voice",
        "volume_up",
        "volume_down",
        "set_volume",
        "open_panel",
        "close_panel",
        "clip",
        "screenshot",
        "run_app",
        "open_url",
        "open_url_in",
        "open_tab",
        "spotify",
        "spotify_pause",
        "spotify_skip",
        "spotify_previous",
        "spotify_now",
        "type",
        "generate_png",
        "fill_login",
        "install_extension",
        "remove_extension",
        "creator",
        "location",
        "weather",
    }
)

# Must run alone / last; lower number runs earlier among ordered intents.
ORDERED_INTENT_PRIORITY = {
    "start_recording": 20,
    "stop_recording": 30,
    "start_chess": 40,
    "start_mania": 40,
    "start_showdown": 40,
    "start_playing": 40,
    "stop_playing": 50,
    "sleep": 80,
    "restart": 90,
    "shutdown": 100,
}

PHASE_SPLIT_RE = re.compile(
    r"\s*(?:,\s*)?(?:\band\s+)?(?:then|after\s+that|afterwards?)\s+|\s*;\s+",
    re.IGNORECASE,
)
SOFT_SPLIT_RE = re.compile(r"\s*,\s*(?:and\s+)?|\s+and\s+", re.IGNORECASE)
CLAUSE_FILLER_RE = re.compile(
    r"^\s*(?:please\s+|go\s+ahead\s+and\s+|"
    r"go\s+to\s+(?=(?:server\s+)?(?:mute|unmute|deafen|undeafen|kick|timeout|time\s*out|untimeout|sleep|restart|self)\b))",
    re.IGNORECASE,
)
BARE_MOD_RE = re.compile(
    r"^(?:server\s+)?(mute|unmute|deafen|undeafen|kick|timeout|time\s*out|untimeout)$",
    re.IGNORECASE,
)
NAME_LIKE_RE = re.compile(r"^[a-z0-9_./'@\- ]{1,48}$", re.IGNORECASE)
NOT_A_NAME = frozenset(
    {
        "then",
        "please",
        "sleep",
        "restart",
        "reboot",
        "shutdown",
        "mute",
        "unmute",
        "deafen",
        "undeafen",
        "kick",
        "timeout",
        "untimeout",
        "self",
        "voice",
        "speech",
        "talking",
        "yourself",
        "tell",
        "what",
        "who",
        "how",
        "why",
        "when",
        "where",
        "open",
        "close",
        "play",
        "stop",
        "start",
    }
)


def strip_wake(text: str) -> str:
    return re.sub(r"^\s*opus[,:]?\s*", "", text.strip(), flags=re.IGNORECASE).strip()


def contains_wake(text: str) -> bool:
    return bool(WAKE_RE.search(text or ""))


def is_wake_only(text: str) -> bool:
    return bool(WAKE_ONLY_RE.match(text or ""))


def is_dismiss(text: str) -> bool:
    return bool(DISMISS_RE.search(text or ""))


def _normalize_mod_target(target: str) -> str:
    cleaned = (target or "").strip(" .,!")
    lowered = cleaned.lower()
    if lowered in ("the jew", "jew"):
        return "flyingcat124"
    return cleaned


def _strip_clause_filler(clause: str) -> str:
    text = CLAUSE_FILLER_RE.sub("", (clause or "").strip())
    return text.strip(" .,!")


def _looks_like_name(part: str) -> bool:
    text = (part or "").strip(" .,!")
    if not text or not NAME_LIKE_RE.match(text):
        return False
    words = text.lower().split()
    if not words or len(words) > 4:
        return False
    if any(word in NOT_A_NAME for word in words):
        return False
    if text.lower() in NOT_A_NAME:
        return False
    return True


def _self_intent_from_part(part: str) -> Intent | None:
    self_match = DISCORD_SELF_RE.search(part or "")
    if not self_match:
        return None
    action = self_match.group(1).lower()
    if action in ("mute", "unmute"):
        return Intent("discord_self_mute")
    if action in ("deafen", "undeafen"):
        return Intent("discord_self_deafen")
    if action in ("disconnect", "leave"):
        return Intent("discord_leave_call")
    return None


def _match_single_intent(text: str, *, allow_ask: bool = True) -> Intent | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if WAKE_ONLY_RE.match(raw):
        return Intent("await_command")
    command = strip_wake(raw)
    command = _strip_clause_filler(command)
    command = re.sub(
        r"\b(mute|unmute|deafen|undeafen|kick|timeout|untimeout)\s*alls?\b",
        r"\1 all",
        command,
        flags=re.IGNORECASE,
    )
    if not command:
        return Intent("await_command") if allow_ask else None
    if OPEN_PANEL_RE.search(raw) or OPEN_PANEL_RE.search(command):
        if is_phone():
            return Intent("pc_only", rest="The settings gear on this page opens phone settings.")
        return Intent("open_panel")
    if (
        CLOSE_PANEL_RE.search(raw)
        or CLOSE_PANEL_RE.search(command)
        or CLOSE_SHORT_RE.search(raw)
        or CLOSE_SHORT_RE.search(command)
    ):
        if is_phone():
            return Intent("pc_only", rest="The panel is only on the PC app.")
        return Intent("close_panel")
    discord_mod = DISCORD_MOD_RE.search(command)
    if discord_mod:
        if is_phone():
            return Intent("pc_only", rest="Discord voice commands are only on the PC app.")
        action = re.sub(r"\s+", "", discord_mod.group(2).lower())
        target = discord_mod.group(3).strip()
        if target.lower() not in ("voice", "speech", "talking", "yourself"):
            return Intent("discord_mod", rest=f"{action}|{_normalize_mod_target(target)}")
    if COINFLIP_RE.search(command) or COINFLIP_RE.search(raw):
        return Intent("coinflip")
    if DICE_RE.search(command) or DICE_RE.search(raw):
        return Intent("dice", rest=command)
    play_in_call = PLAY_IN_CALL_RE.search(command) or PLAY_IN_CALL_RE.search(raw)
    if play_in_call:
        query = (play_in_call.group(1) or "").strip(" .,!")
        if query:
            if is_phone():
                return Intent("spotify", rest=query)
            return Intent("discord_play_music", rest=query)
    if STOP_CALL_MUSIC_RE.search(command) or STOP_CALL_MUSIC_RE.search(raw):
        return Intent("spotify_pause") if is_phone() else Intent("discord_stop_music")
    skip_in_call = SKIP_SONG_RE.search(command) or SKIP_SONG_RE.search(raw)
    if skip_in_call and re.search(r"\bin\s+(the\s+)?(call|vc|voice)\b", skip_in_call.group(0), re.IGNORECASE):
        return Intent("spotify_skip") if is_phone() else Intent("discord_skip_music")
    if SPOTIFY_PAUSE_RE.search(command) or SPOTIFY_PAUSE_RE.search(raw):
        if not re.search(r"\bin\s+(the\s+)?(call|vc|voice)\b", command, re.IGNORECASE):
            return Intent("spotify_pause")
    if SPOTIFY_SKIP_RE.search(command) or SPOTIFY_SKIP_RE.search(raw):
        if not re.search(r"\bin\s+(the\s+)?(call|vc|voice)\b", command, re.IGNORECASE):
            return Intent("spotify_skip")
    if SPOTIFY_PREV_RE.search(command) or SPOTIFY_PREV_RE.search(raw):
        return Intent("spotify_previous")
    if SPOTIFY_NOW_RE.search(command) or SPOTIFY_NOW_RE.search(raw):
        return Intent("spotify_now")
    self_intent = _self_intent_from_part(command)
    if self_intent and not is_phone():
        return self_intent
    if not is_phone():
        if DISCORD_JOIN_RE.search(command):
            return Intent("discord_join_call")
        if DISCORD_BOT_LEAVE_RE.search(command):
            return Intent("discord_bot_leave")
        if DISCORD_LEAVE_RE.search(command):
            return Intent("discord_leave_call")
        discord_search = DISCORD_SEARCH_RE.search(command)
        if discord_search:
            query = (discord_search.group(1) or "").strip()
            return Intent("discord_search", rest=query)
    elif (
        DISCORD_JOIN_RE.search(command)
        or DISCORD_BOT_LEAVE_RE.search(command)
        or DISCORD_LEAVE_RE.search(command)
        or DISCORD_SEARCH_RE.search(command)
        or _self_intent_from_part(command)
    ):
        return Intent("pc_only", rest="Discord voice commands are only on the PC app.")
    if SHUTDOWN_RE.search(command):
        if is_phone():
            return Intent("pc_only", rest="Shutting down the PC is only on the desktop app.")
        return Intent("shutdown")
    if SLEEP_RE.search(command):
        return Intent("sleep")
    if MUTE_RE.search(command):
        return Intent("mute_voice")
    if UNMUTE_RE.search(command):
        return Intent("unmute_voice")
    if SHUSH_RE.search(command) or SHUSH_RE.search(raw):
        return Intent("shush")
    if DISMISS_RE.search(command) or DISMISS_RE.search(raw):
        return Intent("dismiss")
    if LOCATION_RE.search(command) or LOCATION_RE.search(raw):
        return Intent("location")
    scan = SCAN_RE.search(command) or SCAN_RE.search(raw)
    if scan:
        if is_phone():
            return Intent("pc_only", rest="Scanning is only on the PC app.")
        rest = SCAN_RE.sub("", command).strip()
        rest = re.sub(r"^(in|for|at|of|on|to)\s+", "", rest, flags=re.IGNORECASE).strip(" .,!")
        return Intent("cyberbox", rest=rest)
    if WEATHER_RE.search(command) or WEATHER_RE.search(raw):
        rest = WEATHER_RE.sub("", command).strip()
        rest = re.sub(r"^(in|for|at|like|outside|today|tomorrow)\s+", "", rest, flags=re.IGNORECASE).strip()
        return Intent("weather", rest=rest)
    install = INSTALL_EXT_RE.search(command) or INSTALL_EXT_RE.search(raw)
    if install:
        if is_phone():
            return Intent("pc_only", rest="Browser extensions are only on the PC app.")
        name = re.sub(r"\s+(extension|addon|add-on|plugin)\s*$", "", install.group(1), flags=re.IGNORECASE).strip()
        if name and name.lower() not in EXT_BROWSER_WORDS:
            return Intent("install_extension", rest=name)
    remove = REMOVE_EXT_RE.search(command) or REMOVE_EXT_RE.search(raw)
    if remove:
        if is_phone():
            return Intent("pc_only", rest="Browser extensions are only on the PC app.")
        name = re.sub(r"\s+(extension|addon|add-on|plugin)\s*$", "", remove.group(1), flags=re.IGNORECASE).strip()
        if name and name.lower() not in EXT_BROWSER_WORDS:
            return Intent("remove_extension", rest=name)
    if CREATOR_RE.search(raw) or CREATOR_RE.search(command):
        return Intent("creator")
    if command.lower() in {"restart", "reboot", "relaunch", "reload"} or RESTART_RE.search(raw) or RESTART_RE.search(command):
        if is_phone():
            return Intent("pc_only", rest="Restarting Opus is only on the PC app.")
        return Intent("restart")
    if CLIP_RE.search(raw) or CLIP_RE.search(command):
        if is_phone():
            return Intent("pc_only", rest="Clips and screenshots are only on the PC app.")
        return Intent("clip")
    if SHOT_RE.search(raw) or SHOT_RE.search(command):
        if is_phone():
            return Intent("pc_only", rest="Clips and screenshots are only on the PC app.")
        return Intent("screenshot")
    if STOP_PLAY_RE.search(raw) or STOP_PLAY_RE.search(command):
        if is_phone():
            return Intent("spotify_pause")
        return Intent("stop_playing")
    if is_phone() and (
        PLAY_SHOWDOWN_RE.search(raw)
        or PLAY_SHOWDOWN_RE.search(command)
        or PLAY_OSU_RE.search(raw)
        or PLAY_OSU_RE.search(command)
        or PLAY_CHESS_RE.search(raw)
        or PLAY_CHESS_RE.search(command)
        or PLAY_GAME_RE.search(raw)
        or PLAY_GAME_RE.search(command)
        or re.fullmatch(r"play[.!]*", command.strip(), re.IGNORECASE)
    ):
        return Intent("pc_only", rest="Game autoplay is only on the PC version of Opus.")
    if not is_phone():
        if PLAY_SHOWDOWN_RE.search(raw) or PLAY_SHOWDOWN_RE.search(command):
            return Intent("start_showdown", rest=command)
        if PLAY_OSU_RE.search(raw) or PLAY_OSU_RE.search(command):
            return Intent("start_mania", rest=command)
        if PLAY_CHESS_RE.search(raw) or PLAY_CHESS_RE.search(command):
            return Intent("start_chess", rest=command)
        if PLAY_GAME_RE.search(raw) or PLAY_GAME_RE.search(command) or re.fullmatch(r"play[.!]*", command.strip(), re.IGNORECASE):
            if re.search(r"\bchess\b", command, re.IGNORECASE):
                return Intent("start_chess", rest=command)
            return Intent("start_playing", rest=command)
        if START_REC_RE.search(raw) or START_REC_RE.search(command):
            return Intent("start_recording")
        if STOP_REC_RE.search(raw) or STOP_REC_RE.search(command):
            return Intent("stop_recording")
        if FILL_LOGIN_RE.search(command) or FILL_LOGIN_RE.search(raw):
            site = FILL_LOGIN_RE.sub("", command).strip()
            site = re.sub(r"^(to|for|into|on)\s+", "", site, flags=re.IGNORECASE).strip()
            return Intent("fill_login", rest=site or command)
        if PNG_RE.search(command) or PNG_RE.search(raw):
            return Intent("generate_png", rest=command)
        typed = TYPE_RE.search(command) or TYPE_RE.search(raw)
        if typed:
            return Intent("type", rest=(typed.group(2) or "").strip())
    volume = VOLUME_RE.search(command)
    lowered = command.lower()
    if volume and ("volume" in lowered):
        direction = (volume.group(1) or "").lower()
        number = volume.group(2)
        if number is not None:
            return Intent("set_volume", value=max(0, min(100, int(number))))
        if direction == "up":
            return Intent("volume_up")
        if direction == "down":
            return Intent("volume_down")
    if SPOTIFY_RESUME_RE.search(command):
        return Intent("spotify", rest="")
    spotify_match = SPOTIFY_PLAY_RE.search(command)
    if spotify_match:
        query = normalize_spotify_query(spotify_match.group(1))
        if GAME_PLAY_QUERY_RE.search(command) or GAME_PLAY_QUERY_RE.search(query):
            if is_phone():
                return Intent("pc_only", rest="Game autoplay is only on the PC version of Opus.")
            if PLAY_SHOWDOWN_RE.search(command) or re.search(
                r"\b(pokemon|pokémon|poke\s*mons?|pokeman|pkmn|showdown|psim|poke)\b",
                f"{command} {query}",
                re.IGNORECASE,
            ):
                return Intent("start_showdown", rest=command)
            if PLAY_OSU_RE.search(command) or re.search(r"\b(osu!?|mania|webosu)\b", f"{command} {query}", re.IGNORECASE):
                return Intent("start_mania", rest=command)
            if PLAY_CHESS_RE.search(command) or re.search(r"\bchess\b", f"{command} {query}", re.IGNORECASE):
                return Intent("start_chess", rest=command)
        elif should_resume_spotify(query=query, raw=command):
            return Intent("spotify", rest="")
        elif query:
            return Intent("spotify", rest=query)
    spotify_query = extract_spotify_query(command)
    if spotify_query is not None:
        return Intent("spotify", rest=spotify_query)
    if is_phone():
        open_match = OPEN_APP_RE.search(command)
        if open_match:
            target = open_match.group(1).strip().rstrip(".").replace(",", "").strip().lower()
            if "spotify" in target:
                return Intent("spotify", rest="")
        return Intent("ask", rest=command) if allow_ask else None
    if OPEN_TAB_RE.search(command):
        return Intent("open_tab")
    open_in_match = OPEN_IN_BROWSER_RE.search(command)
    if open_in_match:
        target = open_in_match.group(1).strip().rstrip(".").replace(",", "").strip()
        browser = open_in_match.group(2).strip().rstrip(".").replace(",", "").strip()
        target_lower = target.lower()
        site = COMMON_SITES.get(target_lower)
        if site:
            return Intent("open_url_in", rest=f"{site}|{browser}")
        if URL_LIKE_RE.match(target) or target.startswith("http"):
            url = target if target.startswith("http") else "https://" + target
            return Intent("open_url_in", rest=f"{url}|{browser}")
        return Intent("open_url_in", rest=f"https://{target}.com|{browser}")

    open_match = OPEN_APP_RE.search(command)
    if open_match:
        target = open_match.group(1).strip().rstrip(".").replace(",", "").strip()
        if URL_LIKE_RE.match(target) or target.startswith("http"):
            return Intent("open_url", rest=target)
        target_lower = target.lower()
        site = COMMON_SITES.get(target_lower)
        if site:
            return Intent("open_url", rest=site)
        if target_lower in EXT_BROWSER_WORDS or target_lower in (
            "notepad", "calculator", "file explorer", "explorer",
            "task manager", "settings", "control panel", "terminal",
            "powershell", "cmd", "command prompt", "spotify",
            "discord", "steam", "epic games", "minecraft",
        ):
            return Intent("run_app", rest=target)
        if "." in target and " " not in target:
            return Intent("open_url", rest=target)
        return Intent("run_app", rest=target)
    if allow_ask:
        return Intent("ask", rest=command)
    return None


def _parse_phase_clause(clause: str) -> list[Intent]:
    cleaned = _strip_clause_filler(clause)
    if not cleaned:
        return []

    parts = [p.strip(" .,!") for p in SOFT_SPLIT_RE.split(cleaned) if p.strip(" .,!")]
    parts = [re.sub(r"^(?:and\s+)", "", p, flags=re.IGNORECASE).strip(" .,!") for p in parts]
    parts = [p for p in parts if p]
    if len(parts) <= 1:
        intent = _match_single_intent(cleaned, allow_ask=True)
        return [intent] if intent else []

    intents: list[Intent] = []
    last_mod_action: str | None = None
    for part in parts:
        part = _strip_clause_filler(part)
        part = re.sub(r"^(?:and\s+)", "", part, flags=re.IGNORECASE).strip(" .,!")
        if not part:
            continue

        bare_mod = BARE_MOD_RE.match(part)
        if bare_mod:
            last_mod_action = re.sub(r"\s+", "", bare_mod.group(1).lower())
            continue

        self_intent = _self_intent_from_part(part)
        if self_intent and not is_phone():
            intents.append(self_intent)
            continue

        matched = _match_single_intent(part, allow_ask=False)
        if matched and matched.name == "discord_mod":
            action, _, target = matched.rest.partition("|")
            if SOFT_SPLIT_RE.search(target) or BARE_MOD_RE.match(target):
                # e.g. leftover list text — treat as action + later names
                last_mod_action = action
                leftover = target.strip()
                if leftover and _looks_like_name(leftover):
                    intents.append(
                        Intent("discord_mod", rest=f"{action}|{_normalize_mod_target(leftover)}")
                    )
                continue
            intents.append(Intent("discord_mod", rest=f"{action}|{_normalize_mod_target(target)}"))
            last_mod_action = action
            continue
        if matched:
            intents.append(matched)
            continue

        if last_mod_action and _looks_like_name(part):
            intents.append(Intent("discord_mod", rest=f"{last_mod_action}|{_normalize_mod_target(part)}"))
            continue

        # Compound parse failed — fall back to whole clause as one intent.
        whole = _match_single_intent(cleaned, allow_ask=True)
        return [whole] if whole else []

    # Drop lone ask fragments mixed into commands.
    actionable = [i for i in intents if i.name != "ask"]
    if len(actionable) >= 2:
        return actionable
    if len(actionable) == 1 and len(intents) == 1:
        return actionable
    # Bare "mute, agent, enix" only produced mods via last_mod_action — already in actionable.
    if actionable:
        return actionable
    whole = _match_single_intent(cleaned, allow_ask=True)
    return [whole] if whole else []


def match_intent_phases(text: str) -> list[list[Intent]]:
    """Parse an utterance into intents (phases flattened; priority planner orders them)."""
    raw = (text or "").strip()
    if not raw:
        return []
    if WAKE_ONLY_RE.match(raw):
        return [[Intent("await_command")]]
    command = _strip_clause_filler(strip_wake(raw))
    if not command:
        return [[Intent("await_command")]]

    phase_texts = [p for p in PHASE_SPLIT_RE.split(command) if p and p.strip(" .,!")]
    if not phase_texts:
        phase_texts = [command]

    intents: list[Intent] = []
    for phase_text in phase_texts:
        intents.extend(_parse_phase_clause(phase_text))
    return [intents] if intents else []


def match_intents(text: str) -> list[Intent]:
    return [intent for phase in match_intent_phases(text) for intent in phase]


def match_intent(text: str) -> Intent | None:
    intents = match_intents(text)
    return intents[0] if intents else None


def _mod_sort_key(intent: Intent) -> tuple[str, str]:
    action, _, target = intent.rest.partition("|")
    return (target.lower(), action.lower())


def plan_execution_batches(phase: list[Intent]) -> list[list[Intent]]:
    """Importance order: work first (multi-mutes A→Z), sleep/restart/shutdown last.

    Never ask the user which to do first — always pick this order.
    Discord mod targets run sequentially alphabetically.
    Other parallel-safe intents may share a batch.
    """
    if not phase:
        return []
    if len(phase) == 1:
        return [phase]

    mods = [intent for intent in phase if intent.name == "discord_mod"]
    mods.sort(key=_mod_sort_key)

    self_actions = [
        intent
        for intent in phase
        if intent.name in {"discord_self_mute", "discord_self_deafen", "discord_leave_call"}
    ]
    other_parallel = [
        intent
        for intent in phase
        if intent.name in PARALLEL_INTENTS
        and intent.name != "discord_mod"
        and intent.name
        not in {"discord_self_mute", "discord_self_deafen", "discord_leave_call"}
    ]
    ordered = [intent for intent in phase if intent.name not in PARALLEL_INTENTS]
    ordered.sort(key=lambda item: ORDERED_INTENT_PRIORITY.get(item.name, 50))

    batches: list[list[Intent]] = []
    # Multi-mute/kick/etc: alphabetical, one after another.
    for mod in mods:
        batches.append([mod])
    # Self-mute and other independent actions can run together.
    side = self_actions + other_parallel
    if side:
        batches.append(side)
    for intent in ordered:
        batches.append([intent])
    return batches
