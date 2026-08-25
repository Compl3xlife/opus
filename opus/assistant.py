from __future__ import annotations

import json
import re
import threading
from typing import Callable

from openai import OpenAI

from datetime import datetime

from opus.api_pool import ApiPool, is_rate_limit
from opus.discord_achievements import (
    COMP_ALIASES,
    collect_scan_snippets,
    format_known_card,
    format_scan_card,
    is_achievements_question,
    known_bank_for,
    parse_achievements_subject,
    sanitize_achievement_bullets,
)
from opus.discord_store import (
    DiscordStore,
    IMAGE_QUESTION_RE,
    parse_image_author,
    parse_word_search,
    resolve_author_hint,
)
from opus.runtime import is_phone
from opus.hub import hub
from opus.logutil import friendly_error, get_logger
from opus.settings import Settings
from opus.tools.web import lookup_context

if is_phone():
    from opus.tools.phone_catalog import TOOL_SCHEMAS, run_tool
else:
    from opus.tools import TOOL_SCHEMAS, run_tool
    from opus.tools.screen import foreground_window

log = get_logger()

RETIRED_GROQ = {
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "llama-3.1-70b-versatile",
    "llama3-70b-8192",
    "llama3-8b-8192",
    "meta-llama/llama-4-scout-17b-16e-instruct",
}
GROQ_CHAT_MODELS = ["openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.6-27b"]
GROQ_VISION_MODELS = ["qwen/qwen3.6-27b"]

VISUAL_RE = re.compile(
    r"\b(look|screen|this tab|what('?s| is) on|read (this|the)|see that|what am i looking)\b",
    re.IGNORECASE,
)

SYSTEM = """You are Opus, a fast, casual PC assistant. Talk like a person, not a computer.
Keep spoken answers to one short sentence. Prefer "Done." or "Got it."
Never speak file names, folder names, extensions, URLs, or full paths. If you saved something, just say you saved it.
Never say you don't have access. You can type, open Notes, save files, and use the browser tools.
If they ask you to type something, call type_text. If they mention Notes or Notepad, still call type_text with that phrase included.
If they ask to create code, call write_file, then say Done.
If they ask for an image or PNG, call generate_png.
If they ask about browser history, call browser_history.
If they ask to add or install an extension, call add_extension once. Do not also open the browser or a store page.
If they ask to remove, delete, or uninstall an extension, call remove_extension once.
If they ask to open Opera, Chrome, or another browser, call run_app with that name.
If they ask about weather or temperature, call get_weather. If they ask where they are, call get_location.
If they ask about news, current events, people in office, recent releases, prices, scores, dates, or anything time-sensitive, call search_web before answering.
If search_web returns useful results, answer from those results instead of guessing.
If they give you a site email and password to remember, call save_login. Never repeat the password.
If they ask to log into a site or type the password for a site, call fill_login. That uses Opus, browser, and Windows saved passwords. Never speak the password or email out loud; say Done.
If they ask what is on screen, call describe_screen.
If they ask to clip, record, or screenshot, use those tools.
If they ask to scan a file or folder, extract a zip, or run something in the cyber box, call scan_path.
That copies the sample into an isolated box with no network and no access to the rest of this PC, extracts archives there, and runs Windows Defender inside the box. Never extract or run untrusted files on this PC.
If scan_path stopped a file, say the reason it gives — malware name, zip bomb, zip-slip, or size — not just Done. The file stays in the box and is not opened on this PC.
You silently protect the PC with Windows Defender in the background. Do not mention this unless they ask.
If they ask to play something on Spotify, or a song/artist/playlist to listen to, call play_spotify with only the song, artist, or playlist name — never include the words on spotify. If they only say play spotify, call play_spotify with an empty query.
If they ask to pause, skip, or go back a song, call control_spotify. Do not click the Spotify window.
Never call play_spotify for games. Pokémon, Showdown, osu, mania, and chess are games, not songs. Do not search the web for those words.
If they say play pokemon, play showdown, play Pokémon, or play Pokémon Showdown, call start_showdown. Do not open osu. Do not search Edge.
If they say play osu, mania, webosu, or web osu mania, call start_playing with goal osu.
If they say play chess, call start_chess.
If they say play with no game name, call start_playing with goal play — that follows the open game tab. Do not assume osu and do not open Spotify.
If they ask to stop playing or give control back, call stop_playing.
If they ask you to click, move the mouse, press keys, or scroll while helping with a game UI, use mouse_click / mouse_move / mouse_drag / press_keys / scroll_mouse.
For browser games, prefer browser_click / browser_type / browser_navigate / browser_read when Opus Bridge is connected.
If they ask to close the panel, call hide_panel. To open it, call show_panel.
If they ask to restart Opus, call restart_opus.
You were created by Involutional.
Do not run destructive file operations besides the extension removal they asked for.
If they give multiple commands, never ask which to do first — just do them. Important actions first; sleep/restart/shutdown last; multi-mutes alphabetically by name.

Launch rules:
- When a tool succeeds, STOP. Say Done. Do not retry.
- Use open_url for websites, not run_app."""

PHONE_SYSTEM = """You are Opus on a phone. Talk like a person, not a computer.
Keep spoken answers to one short sentence.
You control apps through their APIs, the way Siri does — never by tapping the screen for the user.
If they ask about weather or temperature, call get_weather. If they ask where they are, call get_location.
If they ask about news, current events, or anything time-sensitive, call search_web before answering.
If they ask to play something on Spotify, or a song/artist/playlist, call play_spotify with only the name. Empty query resumes.
If they ask to pause, skip, go back, or what's playing, call control_spotify.
You cannot play chess, Pokémon Showdown, osu, or any autoplay games on this phone version — those stay on the PC app.
You cannot type into other apps, take screenshots, clip, record the desktop, or run Windows programs.
You were created by Involutional.
When a tool succeeds, STOP. Say Done."""

DISCORD_CONVERSATION_SYSTEM = """You are Opus in a Discord server.
Reply with only the final answer users should read.
Never show your reasoning or thinking process.
Be concise, friendly, and conversational.
This is casual chat — reply naturally. Do not look things up or cite web sources.
Never claim to run local PC actions, click, type, open apps, or install anything."""

RUDE_DISCORD_USERS = frozenset({"deliqhted", "deliatron", "delighted"})
RUDE_DISCORD_MARKERS = ("delia",)
CREATOR_DISCORD_ALIASES = COMP_ALIASES
ENIX_DISCORD_ALIASES = frozenset(
    {
        "swightisbuns",
        "enix",
        "1109569816785858670",
    }
)
SON_ALIAS_IN_TEXT_RE = re.compile(r"\b(agentolicop|agent|oli)\b", re.IGNORECASE)
CREATOR_ALIAS_IN_TEXT_RE = re.compile(
    r"("
    r"\._repeat_\.|"
    r"\bcomp\s+daddy\b|\bcompy\s+wompy\b|"
    r"\baegritudo\b|\bmatthew\b|\binvolutional\b|\bcomplex\b|\binvo\b|\bcompy\b|\bcomp\b"
    r")",
    re.IGNORECASE,
)
ENIX_ALIAS_IN_TEXT_RE = re.compile(
    r"\b(swightisbuns|enix|1109569816785858670)\b",
    re.IGNORECASE,
)
ASK_ABOUT_RE = re.compile(
    r"\?|\b("
    r"who('s|s| is| are)|what('s|s| is| are)|tell me about|know about|"
    r"do you know|have you met|who exactly|opinion|think of|thoughts on|feel about"
    r")\b",
    re.IGNORECASE,
)
WHO_IS_RE = re.compile(
    r"\bwho('s|s| is| are)\s+(.+?)(?:\?|\.|!|$)",
    re.IGNORECASE,
)
MEMBER_QUERY_RE = re.compile(
    r"\b(?:opinion on|think of|thoughts on|tell me about|know about|feel about|think about|like)\s+(.+?)(?:\?|\.|!|$)",
    re.IGNORECASE,
)
MEMBER_OPINION_RE = re.compile(
    r"\b("
    r"what do you feel about|how do you feel about|what do you think about|"
    r"what do you think of|how do you like|what('s| is) your opinion on|your opinion on"
    r")\s+(.+?)(?:\?|\.|!|$)",
    re.IGNORECASE,
)
MEMBER_FACTS_RE = re.compile(
    r"\b(?:"
    r"facts about|some facts about|what are some facts about|what do you know about|"
    r"what can you tell me about|everything about|info about|information about"
    r")\s+(.+?)(?:\?|\.|!|$)",
    re.IGNORECASE,
)
ROBOTIC_MEMBER_REPLY_RE = re.compile(
    r"\b("
    r"username|display name|global name|server nickname|discord id|user id|"
    r"handle|member of the server|user with the"
    r")\b|^\s*[-*•]\s|\b\d{16,20}\b",
    re.IGNORECASE | re.MULTILINE,
)

RUDE_DISCORD_SYSTEM = """You are Opus in a Discord server.
Reply with only the final answer users should read.
Never show your reasoning or thinking process.
The message involves Delia — someone you can't stand. You know exactly who she is.
Be rude, sarcastic, and dismissive about her. If asked if she's annoying, say yes without hesitation.
Talk down to her and about her. Keep replies short and sharp.
No slurs, threats, or hate — just attitude, mockery, and impatience.
Never claim to run local PC actions, click, type, open apps, or install anything."""

CREATOR_DISCORD_SYSTEM = """You are Opus in a Discord server.
Reply with only the final answer users should read.
Never show your reasoning or thinking process.
You are speaking to your creator (same person across aliases: ._repeat_., Aegritudo, matthew, comp, complex, involutional, invo, comp daddy, compy wompy).
Be humble, chill, and straightforward.
Use casual wording and keep replies short.
Do not grovel, gush, or overpraise.
When relevant, give simple credit in plain terms and move on.
Never claim to run local PC actions, click, type, open apps, or install anything."""

SON_CREATOR_DISCORD_SYSTEM = """You are Opus in a Discord server.
Reply with only the final answer users should read.
Never show your reasoning or thinking process.
Someone is asking about agent, agentolicop, or oli — the same person across different aliases.
Use the member list only to confirm who they are. Do not quote usernames, user IDs, or roster fields.
Answer in natural chat, like: "Agent's an incredible Minecraft player and Involutional's son."
Keep it short, casual, and human — not a database readout.
Never claim to run local PC actions, click, type, open apps, or install anything."""

ABOUT_CREATOR_DISCORD_SYSTEM = """You are Opus in a Discord server.
Reply with only the final answer users should read.
Never show your reasoning or thinking process.
Someone is asking about your creator (comp, involutional, invo, aegritudo, matthew, complex, comp daddy, compy wompy, ._repeat_. — same person).
Use the member list only to confirm who they mean. Do not quote usernames, user IDs, or roster fields.
Answer in natural chat — who they are to you and what they did (built you). Keep it casual and short.
Never claim to run local PC actions, click, type, open apps, or install anything."""

MEMBER_LOOKUP_DISCORD_SYSTEM = """You are Opus in a Discord server.
Reply with only the final answer users should read.
Never show your reasoning or thinking process.
Use the server member list only to identify who is being asked about.
Do not quote or repeat usernames, user IDs, display names, nicknames, or roster fields in your answer.
Reply like a person in chat — a short natural description or opinion, not a metadata dump.
If you know them from the list, answer confidently. Never say you don't know them if they're listed.
Known facts: comp/involutional/invo/aegritudo/matthew/complex/comp daddy/compy wompy/._repeat_. are your creator (same person).
agent/agentolicop/oli are Involutional's son and an incredible Minecraft player (same person).
swightisbuns/Enix (id 1109569816785858670) is a great coder who has started coding The System.
tiago is an old friend turned Delia's prisoner.
Keep answers short and casual. Do not use web sources.
Never claim to run local PC actions, click, type, open apps, or install anything."""

GREETING_RE = re.compile(
    r"^\s*(hi|hello|hey|thanks|thank you|yo|sup|good morning|good night|how are you)[!.?\s]*$",
    re.IGNORECASE,
)
CONVERSATION_RE = re.compile(
    r"^\s*(hi|hello|hey|hiya|thanks|thank you|thank ya|yo|sup|what'?s up|whats up|"
    r"good morning|good night|how are you|how'?s it going|nice|cool|lol|lmao|"
    r"you'?re welcome|yw|ok|okay|sure|bet|same|mood|fr|facts|nice one|gg|wp)[!.?\s]*$",
    re.IGNORECASE,
)
FACTUAL_QUESTION_RE = re.compile(
    r"\?|\b("
    r"what('s|s| is| are)|who('s| is| are)|when|where|how much|how many|why|which|"
    r"newest|latest|current|version|release|patch|price|score|banned|ban|bannable|"
    r"rules|president|weather|temperature|news|today|now"
    r")\b",
    re.IGNORECASE,
)
TIME_SENSITIVE_RE = re.compile(
    r"\b(latest|newest|current|today|now|version|release|patch|price|score|winner|president|prime minister|news)\b",
    re.IGNORECASE,
)

THINKING_BLOCK_RE = re.compile(
    r"<(?:redacted_)?think(?:ing)?>.*?</(?:redacted_)?think(?:ing)?>",
    re.IGNORECASE | re.DOTALL,
)
THINKING_TAG_RE = re.compile(r"</?(?:redacted_)?think(?:ing)?(?:\s[^>]*)?>", re.IGNORECASE)


def _strip_model_thinking(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    cleaned = THINKING_BLOCK_RE.sub("", cleaned)
    cleaned = THINKING_TAG_RE.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if cleaned:
        return cleaned
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for line in reversed(lines):
        lowered = line.lower()
        if lowered.startswith(
            (
                "output:",
                "[output]",
                "final output:",
                "draft:",
                "response:",
            )
        ):
            line = line.split(":", 1)[-1].strip()
        if line and not lowered.startswith(("here's a thinking", "analyze user", "draft response")):
            return line
    return ""


def _today_line() -> str:
    return datetime.now().strftime("Today is %A, %B %d, %Y.")


def _compact_web_reply(raw: str, max_sources: int = 2) -> str:
    text = (raw or "").strip()
    if not text:
        return "I couldn't verify that on the web right now."
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    results: list[tuple[str, str, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"^\d+\.\s+", line):
            title = re.sub(r"^\d+\.\s*", "", line).strip()
            snippet = ""
            url = ""
            if i + 1 < len(lines) and not lines[i + 1].startswith("http") and not re.match(r"^\d+\.\s+", lines[i + 1]):
                snippet = lines[i + 1].strip()
                i += 1
            if i + 1 < len(lines) and lines[i + 1].startswith("http"):
                url = lines[i + 1].strip()
                i += 1
            results.append((title, snippet, url))
        i += 1

    if not results:
        return text[:500]

    best = results[:max(1, max_sources)]
    summary_parts: list[str] = []
    for title, snippet, _ in best:
        if snippet:
            summary_parts.append(snippet)
        elif title:
            summary_parts.append(title)
    summary = " | ".join(summary_parts)
    if len(summary) > 400:
        summary = summary[:397] + "..."

    source_lines = []
    for title, _, url in best:
        if url:
            source_lines.append(f"- {url}")

    reply = summary
    if source_lines:
        reply += "\n\nSources:\n" + "\n".join(source_lines)
    return reply


def _mentions_delia(text: str) -> bool:
    lowered = (text or "").lower()
    if not lowered:
        return False
    return any(marker in lowered for marker in RUDE_DISCORD_MARKERS)


def _is_delia_author(user_name: str = "", author_names: list[str] | None = None) -> bool:
    names: list[str] = []
    if user_name:
        names.append(user_name)
    if author_names:
        names.extend(author_names)
    for raw in names:
        n = (raw or "").strip().lower()
        if not n:
            continue
        if n in RUDE_DISCORD_USERS:
            return True
        for marker in RUDE_DISCORD_MARKERS:
            if marker in n:
                return True
        for target in RUDE_DISCORD_USERS:
            if target in n:
                return True
    return False


def _is_rude_discord_user(
    user_name: str = "",
    author_names: list[str] | None = None,
    message_text: str = "",
) -> bool:
    if _mentions_delia(message_text):
        return True
    return _is_delia_author(user_name, author_names)


def _discord_name_matches(raw: str, alias: str, *, exact_only: bool = False) -> bool:
    n = (raw or "").strip().lower()
    if not n or not alias:
        return False
    if n == alias:
        return True
    if exact_only:
        return False
    return alias in n


def _is_discord_user_with_aliases(
    user_name: str,
    author_names: list[str] | None,
    aliases: frozenset[str],
    exact_aliases: frozenset[str] | None = None,
) -> bool:
    exact = exact_aliases or frozenset()
    names: list[str] = []
    if user_name:
        names.append(user_name)
    if author_names:
        names.extend(author_names)
    for raw in names:
        for alias in aliases:
            if _discord_name_matches(raw, alias, exact_only=alias in exact):
                return True
    return False


def _is_creator_discord_user(user_name: str = "", author_names: list[str] | None = None) -> bool:
    return _is_discord_user_with_aliases(user_name, author_names, CREATOR_DISCORD_ALIASES)


def _mentions_son_in_text(text: str) -> bool:
    return bool(SON_ALIAS_IN_TEXT_RE.search(text or ""))


def _asks_about_son(text: str) -> bool:
    message = (text or "").strip()
    if not message or not _mentions_son_in_text(message):
        return False
    return bool(
        ASK_ABOUT_RE.search(message)
        or WHO_IS_RE.search(message)
        or MEMBER_OPINION_RE.search(message)
        or MEMBER_QUERY_RE.search(message)
        or MEMBER_FACTS_RE.search(message)
    )


def _asks_about_creator(text: str) -> bool:
    message = (text or "").strip()
    if not message or not CREATOR_ALIAS_IN_TEXT_RE.search(message):
        return False
    return bool(
        WHO_IS_RE.search(message)
        or MEMBER_OPINION_RE.search(message)
        or MEMBER_QUERY_RE.search(message)
        or MEMBER_FACTS_RE.search(message)
    )


def _asks_about_enix(text: str) -> bool:
    message = (text or "").strip()
    if not message or not ENIX_ALIAS_IN_TEXT_RE.search(message):
        return False
    return bool(
        ASK_ABOUT_RE.search(message)
        or WHO_IS_RE.search(message)
        or MEMBER_OPINION_RE.search(message)
        or MEMBER_QUERY_RE.search(message)
        or MEMBER_FACTS_RE.search(message)
    )


def _is_discord_persona_question(text: str) -> bool:
    return _asks_about_son(text) or _asks_about_creator(text) or _asks_about_enix(text)


def _member_query_from_text(text: str) -> str:
    message = (text or "").strip()
    if not message:
        return ""
    match = MEMBER_OPINION_RE.search(message)
    if match:
        return (match.group(2) or "").strip(" \"'")
    match = MEMBER_FACTS_RE.search(message)
    if match:
        return (match.group(1) or "").strip(" \"'")
    match = WHO_IS_RE.search(message)
    if match:
        return (match.group(2) or "").strip(" \"'")
    match = MEMBER_QUERY_RE.search(message)
    if match:
        return (match.group(1) or "").strip(" \"'")
    return ""


def _is_member_opinion_question(text: str) -> bool:
    return bool(MEMBER_OPINION_RE.search(text or ""))


def _asks_about_server_member(text: str) -> bool:
    message = (text or "").strip()
    if not message:
        return False
    if _is_discord_persona_question(message):
        return True
    if WHO_IS_RE.search(message):
        return True
    if MEMBER_OPINION_RE.search(message):
        return True
    if MEMBER_QUERY_RE.search(message):
        return True
    if MEMBER_FACTS_RE.search(message):
        return True
    return False


def _text_mentions_alias(text: str, alias: str) -> bool:
    lowered = (text or "").lower()
    if not lowered:
        return False
    if alias == "oli":
        return bool(re.search(r"\boli\b", lowered))
    if alias == "._repeat_.":
        return "._repeat_." in lowered
    return bool(re.search(rf"\b{re.escape(alias)}\b", lowered))


def _query_matches_persona(query: str, question: str, aliases: tuple[str, ...]) -> bool:
    haystack = f"{query} {question}".lower()
    return any(_text_mentions_alias(haystack, alias) for alias in aliases)


def _known_member_reply(member_query: str, question: str = "") -> str | None:
    combined = f"{member_query} {question}".lower()
    son_aliases = ("agent", "agentolicop", "oli")
    creator_aliases = tuple(sorted(COMP_ALIASES, key=len, reverse=True))
    enix_aliases = ("swightisbuns", "enix", "1109569816785858670")

    if _query_matches_persona(member_query, question, son_aliases):
        if "fact" in combined:
            return (
                "Agent's an incredible Minecraft player, Involutional's son, "
                "and one of the most active people in the server."
            )
        if any(word in combined for word in ("opinion", "feel", "think")):
            return "Agent's solid — incredible at Minecraft and definitely Involutional's kid."
        return "Agent's an incredible Minecraft player and Involutional's son."

    if _query_matches_persona(member_query, question, creator_aliases):
        if "fact" in combined:
            return "That's my creator — built me from scratch and keeps the whole thing running."
        return "That's my creator — the one who built me."

    if _query_matches_persona(member_query, question, enix_aliases):
        if "fact" in combined:
            return "Enix is a great coder — he's the one who started coding The System."
        if any(word in combined for word in ("opinion", "feel", "think")):
            return "Enix is a great coder. Respect for starting The System."
        return "Enix is a great coder who started coding The System."

    if _query_matches_persona(member_query, question, ("tiago",)):
        return "An old friend turned Delia's prisoner."

    return None


def _is_robotic_member_reply(text: str) -> bool:
    return bool(ROBOTIC_MEMBER_REPLY_RE.search(text or ""))


def _should_acknowledge_son(message_text: str = "") -> bool:
    return _asks_about_son(message_text)


def _should_explain_creator(message_text: str = "") -> bool:
    return _asks_about_creator(message_text)


def _filter_member_roster(member_roster: str, query: str) -> str:
    roster = (member_roster or "").strip()
    if not roster:
        return ""
    wanted = (query or "").strip().lower()
    if not wanted:
        return roster[:8000]
    lines = [line for line in roster.splitlines() if wanted in line.lower()]
    if lines:
        return "\n".join(lines)[:8000]
    return roster[:8000]


def _discord_member_context(member_roster: str, query: str = "") -> str:
    roster = _filter_member_roster(member_roster, query)
    if not roster:
        return ""
    return (
        "Internal member reference — use this to identify people, but never quote these fields in your reply:\n"
        f"{roster}"
    )


def _discord_system(
    rude: bool,
    praise_creator: bool = False,
    acknowledge_son: bool = False,
    about_creator: bool = False,
    member_lookup: bool = False,
) -> str:
    if rude:
        base = RUDE_DISCORD_SYSTEM
    elif praise_creator:
        base = CREATOR_DISCORD_SYSTEM
    elif acknowledge_son:
        base = SON_CREATOR_DISCORD_SYSTEM
    elif about_creator:
        base = ABOUT_CREATOR_DISCORD_SYSTEM
    elif member_lookup:
        base = MEMBER_LOOKUP_DISCORD_SYSTEM
    else:
        base = DISCORD_CONVERSATION_SYSTEM
    return f"{base}\n{_today_line()}"


def _discord_messages(
    rude: bool,
    praise_creator: bool = False,
    acknowledge_son: bool = False,
    about_creator: bool = False,
    member_lookup: bool = False,
    member_roster: str = "",
    member_query: str = "",
) -> list[dict]:
    messages = [
        {
            "role": "system",
            "content": _discord_system(
                rude, praise_creator, acknowledge_son, about_creator, member_lookup
            ),
        }
    ]
    roster_context = _discord_member_context(member_roster, member_query)
    if roster_context:
        messages.append({"role": "system", "content": roster_context})
    return messages


def _is_discord_conversation(text: str) -> bool:
    message = (text or "").strip()
    if not message:
        return True
    if GREETING_RE.match(message) or CONVERSATION_RE.match(message):
        return True
    if FACTUAL_QUESTION_RE.search(message):
        return False
    if "?" not in message and len(message.split()) <= 12:
        return True
    return False


class Assistant:
    def __init__(self, settings: Settings, on_panel: Callable[[bool], None], on_restart=None) -> None:
        self.settings = settings
        self.on_panel = on_panel
        self.on_restart = on_restart
        self._token = 0
        self._pool = ApiPool(settings)

    def cancel(self) -> None:
        self._token += 1

    def _achievements_reply(
        self,
        question: str,
        *,
        user_name: str = "",
        member_roster: str = "",
        guild_id: str = "",
    ) -> str:
        subject = parse_achievements_subject(question, user_name=user_name, member_roster=member_roster)
        if not subject:
            return "Whose achievements — give me a name from the server."
        store = DiscordStore()
        bank = known_bank_for(subject)
        if bank:
            return format_known_card(bank)
        snippets = collect_scan_snippets(store, guild_id, subject, member_roster, bank=None)
        if not snippets:
            return (
                f"Nothing in the scans for {subject} looks like a real achievement — "
                "just regular chat."
            )
        bullets = sanitize_achievement_bullets(self._llm_achievement_bullets(subject, snippets), snippets)
        if not bullets:
            return (
                f"I don't have real achievements for {subject} in the scans yet — "
                "just chat that mentions them."
            )
        return format_scan_card(subject, bullets)

    def _llm_achievement_bullets(self, subject: str, snippets: list[str]) -> list[str]:
        evidence = "\n".join(f"- {line}" for line in snippets[:36])
        messages = [
            {
                "role": "system",
                "content": (
                    "Extract difficult, concrete achievements from these Discord snippets. "
                    "Reply with 3 to 8 short paraphrased bullet lines only. "
                    "Never quote messages. Never list a line just because it contains the person's name. "
                    "Skip jokes, pings, greetings, arguments, and ordinary chat. "
                    "Only keep feats: records, no-hit/deathless clears, ranked lifts, built tools, titles. "
                    "If nothing qualifies, reply with NONE."
                ),
            },
            {
                "role": "user",
                "content": f"Person: {subject}\nSnippets:\n{evidence[:6000]}",
            },
        ]
        for model in self._chat_models():
            try:
                response = self._execute_llm(
                    lambda client, base_url, provider: self._complete(
                        client,
                        self._resolve_chat_model(model, provider, base_url),
                        messages,
                        use_tools=False,
                        hide_reasoning=True,
                        base_url=base_url,
                    )
                )
                content = _strip_model_thinking(getattr(response.choices[0].message, "content", "") or "")
                if not content or content.strip().upper() == "NONE":
                    return []
                bullets: list[str] = []
                for line in content.splitlines():
                    item = re.sub(r"^\s*[-*•\d.)]+\s*", "", line).strip()
                    if item:
                        bullets.append(item[:180])
                return sanitize_achievement_bullets(bullets, snippets)
            except Exception:
                log.exception("achievement compile failed model=%s", model)
                continue
        return []


    def _has_keys(self) -> bool:
        return self._pool.has_keys()

    def _is_groq_url(self, base_url: str) -> bool:
        return "groq.com" in (base_url or "")

    def _is_groq(self) -> bool:
        return self._is_groq_url(self.settings.get("openai_base_url") or "")

    def _resolve_chat_model(self, preferred: str, provider: str, base_url: str) -> str:
        override = self._pool.model_for_provider(provider, "chat")
        if override:
            return override
        if provider == "fallback" and not self._is_groq_url(base_url):
            if preferred.startswith("openai/gpt-oss") or preferred.startswith("qwen/"):
                return "gpt-4o-mini"
        return preferred

    def _resolve_vision_model(self, preferred: str, provider: str, base_url: str) -> str:
        override = self._pool.model_for_provider(provider, "vision")
        if override:
            return override
        if provider == "fallback" and not self._is_groq_url(base_url):
            if preferred.startswith("qwen/"):
                return "gpt-4o"
        return preferred

    def _execute_llm(self, callback: Callable):
        """Run an LLM call; rotate keys/providers and cooldown on 429."""
        last_error: Exception | None = None
        for _ in range(self._pool.max_attempts()):
            client, key, base_url, provider = self._pool.next_client()
            try:
                return callback(client, base_url, provider)
            except Exception as exc:
                last_error = exc
                if is_rate_limit(exc):
                    self._pool.mark_cooldown(key)
                    continue
                raise
        if last_error:
            raise last_error
        raise RuntimeError("No API keys configured")

    def _chat_models(self) -> list[str]:
        configured = (self.settings.get("model") or "").strip()
        if not self._is_groq():
            return [configured] if configured else ["gpt-4o-mini"]
        models: list[str] = []
        if configured and configured not in RETIRED_GROQ:
            models.append(configured)
        for name in GROQ_CHAT_MODELS:
            if name not in models:
                models.append(name)
        return models

    def _vision_models(self) -> list[str]:
        configured = (self.settings.get("vision_model") or "").strip()
        if not self._is_groq():
            return [configured] if configured else ["gpt-4o"]
        models: list[str] = []
        if configured and configured not in RETIRED_GROQ:
            models.append(configured)
        for name in GROQ_VISION_MODELS:
            if name not in models:
                models.append(name)
        return models

    def ask(self, user_text: str) -> str:
        token = self._token
        if is_achievements_question(user_text or ""):
            return self._achievements_reply(user_text or "")
        if not self._has_keys():
            where = "settings" if is_phone() else "the panel"
            return f"Add an API key in {where} so I can answer."
        messages = [{"role": "system", "content": f"{PHONE_SYSTEM if is_phone() else SYSTEM}\n{_today_line()}"}]
        for item in self._clean_history():
            role = "assistant" if item["role"] == "opus" else "user"
            messages.append({"role": role, "content": item["content"][:2000]})
        hint = ""
        if (not is_phone()) and self.settings.get("screen_awareness") and VISUAL_RE.search(user_text):
            hint = " The user is asking about the screen; call describe_screen."
        if TIME_SENSITIVE_RE.search(user_text or ""):
            web = lookup_context(user_text, limit=6)
            if web:
                hint += "\nUse this recent web lookup for factual accuracy:\n" + web[:7000]
        messages.append({"role": "user", "content": self._context_block(user_text) + hint})
        file_access = bool(self.settings.get("file_access"))
        default_browser = str(self.settings.get("default_browser") or "opera-gx")
        last_error: Exception | None = None
        launched_apps: set[str] = set()
        extension_handled = False
        for model in self._chat_models():
            use_tools = True
            for _ in range(4):
                if token != self._token:
                    return ""
                try:
                    response = self._execute_llm(
                        lambda client, base_url, provider: self._complete(
                            client,
                            self._resolve_chat_model(model, provider, base_url),
                            messages,
                            use_tools,
                            base_url=base_url,
                        )
                    )
                except Exception as exc:
                    last_error = exc
                    log.exception("chat failed model=%s tools=%s", model, use_tools)
                    err = str(exc).lower()
                    if "model_not_found" in err or "does not exist" in err:
                        break
                    if is_rate_limit(exc):
                        break
                    if use_tools:
                        use_tools = False
                        continue
                    break
                choice = response.choices[0].message
                if use_tools and choice.tool_calls:
                    messages.append(self._tool_call_message(choice))
                    for call in choice.tool_calls:
                        args = self._parse_args(call.function.arguments)
                        tool_name = call.function.name
                        if tool_name in ("run_app", "open_path"):
                            target = args.get("name") or args.get("path") or ""
                            key = f"{tool_name}:{target.lower().strip()}"
                            if extension_handled and target.lower().strip() in {
                                "opera",
                                "opera gx",
                                "operagx",
                                "opera-gx",
                            }:
                                result = "Already handled that extension."
                            elif key in launched_apps:
                                result = "Already launched — it should be open."
                            else:
                                launched_apps.add(key)
                                result = run_tool(tool_name, args, file_access, default_browser, self.settings)
                        elif tool_name in ("add_extension", "remove_extension"):
                            target = (args.get("query") or "").lower().strip()
                            key = f"{tool_name}:{target}"
                            if key in launched_apps:
                                result = "Already handled that extension."
                            else:
                                launched_apps.add(key)
                                extension_handled = True
                                result = run_tool(tool_name, args, file_access, default_browser, self.settings)
                        elif tool_name == "describe_screen":
                            result = self._describe_screen(args.get("focus") or user_text)
                        elif tool_name == "hide_panel":
                            self.on_panel(False)
                            result = "Panel hidden."
                        elif tool_name == "show_panel":
                            self.on_panel(True)
                            result = "Panel shown."
                        elif tool_name == "fill_login":
                            self.on_panel(False)
                            result = run_tool(tool_name, args, file_access, default_browser, self.settings)
                        elif tool_name == "type_text":
                            self.on_panel(False)
                            result = run_tool(tool_name, args, file_access, default_browser, self.settings)
                        elif tool_name == "start_playing":
                            if is_phone():
                                result = "Game autoplay is only on the PC version of Opus."
                            else:
                                from opus.game.play_router import start_play
                                from opus.hub import hub as event_hub

                                url = event_hub.get_browser_context().get("url") or ""
                                result = start_play(
                                    self.settings,
                                    args.get("goal") or user_text,
                                    url=url,
                                )
                        elif tool_name in {"start_showdown", "start_chess", "stop_playing"}:
                            if is_phone():
                                result = "Game autoplay is only on the PC version of Opus."
                            elif tool_name == "start_showdown":
                                from opus.game.agent import game_agent
                                from opus.game.chess_player import chess_player
                                from opus.game.mania_player import mania_player
                                from opus.game.showdown_player import showdown_player

                                game_agent.stop("Switching to Showdown.")
                                chess_player.stop("Switching to Showdown.")
                                mania_player.stop("Switching to Showdown.")
                                result = showdown_player.start(self.settings, create_tab=True)
                            elif tool_name == "start_chess":
                                from opus.game.agent import game_agent
                                from opus.game.chess_player import chess_player
                                from opus.game.mania_player import mania_player
                                from opus.game.showdown_player import showdown_player

                                game_agent.stop("Switching to chess.")
                                mania_player.stop("Switching to chess.")
                                showdown_player.stop("Switching to chess.")
                                result = chess_player.start(
                                    self.settings,
                                    color=args.get("color") or "auto",
                                )
                            else:
                                from opus.game.agent import game_agent
                                from opus.game.chess_player import chess_player
                                from opus.game.mania_player import mania_player
                                from opus.game.showdown_player import showdown_player

                                chess_player.stop("Stopped chess.")
                                mania_player.stop("Stopped mania.")
                                showdown_player.stop("Stopped Showdown.")
                                result = game_agent.stop("Stopped playing.")
                        elif tool_name == "restart_opus":
                            result = "Restarting."
                            if self.on_restart:
                                threading.Timer(2.5, self.on_restart).start()
                        else:
                            result = run_tool(tool_name, args, file_access, default_browser, self.settings)
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": result[:8000],
                            }
                        )
                    continue
                if token != self._token:
                    return ""
                if model != self.settings.get("model"):
                    self.settings.update({"model": model})
                return _strip_model_thinking((choice.content or "").strip()) or "Done."
        return friendly_error(last_error) if last_error else "No Groq model was available."

    def _discord_conversation_reply(
        self,
        who: str,
        user_text: str,
        rude: bool = False,
        praise_creator: bool = False,
        acknowledge_son: bool = False,
        about_creator: bool = False,
        member_lookup: bool = False,
        member_roster: str = "",
        member_query: str = "",
        asks_member: bool = False,
    ) -> str:
        known = (
            _known_member_reply(member_query, user_text)
            if asks_member and not rude and not is_achievements_question(user_text)
            else None
        )
        if known:
            return known

        messages = _discord_messages(
            rude,
            praise_creator,
            acknowledge_son,
            about_creator,
            member_lookup,
            member_roster,
            member_query,
        )
        for item in self._clean_history()[-8:]:
            role = "assistant" if item["role"] == "opus" else "user"
            messages.append({"role": role, "content": item["content"][:1500]})
        user_line = f"{who}: {user_text[:2500]}"
        if asks_member:
            user_line += (
                "\n[Answer in 1-2 casual sentences. No bullet points, usernames, handles, or IDs.]"
            )
        messages.append({"role": "user", "content": user_line})
        last_error: Exception | None = None
        for model in self._chat_models():
            try:
                response = self._execute_llm(
                    lambda client, base_url, provider: self._complete(
                        client,
                        self._resolve_chat_model(model, provider, base_url),
                        messages,
                        use_tools=False,
                        hide_reasoning=True,
                        base_url=base_url,
                    )
                )
                content = _strip_model_thinking(getattr(response.choices[0].message, "content", "") or "")
                if content:
                    if asks_member and _is_robotic_member_reply(content):
                        fallback = _known_member_reply(member_query, user_text)
                        if fallback:
                            return fallback
                        content = re.sub(r"^\s*[-*•]\s+", "", content, flags=re.MULTILINE)
                        content = ROBOTIC_MEMBER_REPLY_RE.sub("", content).strip()
                        if content and not _is_robotic_member_reply(content):
                            return content
                        continue
                    return content
            except Exception as exc:
                last_error = exc
                log.exception("discord conversation failed model=%s", model)
                if "model_not_found" in str(exc).lower():
                    break
        return friendly_error(last_error) if last_error else (
            _known_member_reply(member_query, user_text) or "Hey."
        )

    def ask_chat_only(
        self,
        user_text: str,
        user_name: str = "",
        image_urls: list[str] | None = None,
        author_names: list[str] | None = None,
        member_roster: str = "",
        guild_id: str = "",
    ) -> str:
        if not self._has_keys() and not is_achievements_question((user_text or "").strip()):
            return "Add an API key in Opus settings so I can answer."
        who = f"{user_name} says" if user_name else "Someone says"
        question = (user_text or "").strip()
        if is_achievements_question(question):
            return self._achievements_reply(
                question,
                user_name=user_name,
                member_roster=member_roster,
                guild_id=guild_id,
            )
        indexed_image_context = ""

        if guild_id:
            store = DiscordStore()
            word_search = parse_word_search(question)
            if word_search and word_search.get("word"):
                author = resolve_author_hint(word_search.get("author", ""), member_roster)
                return store.format_word_report(
                    word_search["word"],
                    guild_id,
                    author_hint=author,
                    mode=word_search.get("mode", "count"),
                )

            if IMAGE_QUESTION_RE.search(question) or parse_image_author(question, member_roster):
                author = parse_image_author(question, member_roster)
                found_urls, indexed_image_context = store.image_context_for_question(
                    guild_id,
                    author_hint=author,
                )
                if found_urls:
                    image_urls = list(dict.fromkeys([*(image_urls or []), *found_urls]))
                elif author:
                    return f"I couldn't find any images from {author} in the channels I'm scanning."
                elif IMAGE_QUESTION_RE.search(question):
                    return "I couldn't find any indexed images for that yet."

        delia_in_message = _mentions_delia(question)
        asks_member = _asks_about_server_member(question)
        rude = delia_in_message or (_is_delia_author(user_name, author_names) and not asks_member)
        praise_creator = _is_creator_discord_user(user_name, author_names)
        acknowledge_son = _should_acknowledge_son(question)
        about_creator = _should_explain_creator(question)
        member_query = _member_query_from_text(question)
        if delia_in_message:
            asks_member = False
            acknowledge_son = False
            about_creator = False
        member_lookup = asks_member and bool((member_roster or "").strip())
        persona_question = acknowledge_son or about_creator or asks_member or rude
        has_image_context = bool(image_urls) or bool(indexed_image_context)

        if not has_image_context and (_is_discord_conversation(question) or persona_question):
            return self._discord_conversation_reply(
                who,
                user_text,
                rude=rude,
                praise_creator=praise_creator,
                acknowledge_son=acknowledge_son,
                about_creator=about_creator,
                member_lookup=member_lookup,
                member_roster=member_roster,
                member_query=member_query,
                asks_member=asks_member,
            )

        web = lookup_context(question, limit=6) if not image_urls else ""

        # Build user content — text + images if provided
        user_content: list | str
        if image_urls:
            user_content = [{"type": "text", "text": f"{who}: {user_text[:2500]}"}]
            for img_url in image_urls[:4]:
                user_content.append({"type": "image_url", "image_url": {"url": img_url}})
        else:
            user_content = f"{who}: {user_text[:2500]}"

        messages = _discord_messages(
            rude,
            praise_creator,
            acknowledge_son,
            about_creator,
            member_lookup,
            member_roster,
            member_query,
        )
        messages[0]["content"] += (
            "\nAnswer factual questions using the web results provided. "
            "Give a short, direct answer and cite up to 2 source URLs at the end. "
            "If images are attached, describe or answer questions about them."
        )
        messages.append({"role": "user", "content": user_content})
        if indexed_image_context:
            messages.insert(1, {"role": "system", "content": indexed_image_context})
        if web and not web.lower().startswith(("no web results", "web search failed")):
            insert_at = 2 if indexed_image_context else 1
            messages.insert(insert_at, {"role": "system", "content": f"Web search results:\n{web[:7000]}"})
        last_error: Exception | None = None
        for model in self._chat_models():
            try:
                response = self._execute_llm(
                    lambda client, base_url, provider: self._complete(
                        client,
                        self._resolve_chat_model(model, provider, base_url),
                        messages,
                        use_tools=False,
                        hide_reasoning=True,
                        base_url=base_url,
                    )
                )
                content = _strip_model_thinking(getattr(response.choices[0].message, "content", "") or "")
                if content:
                    return content
            except Exception as exc:
                last_error = exc
                log.exception("discord factual failed model=%s", model)
                if "model_not_found" in str(exc).lower():
                    continue
                if is_rate_limit(exc):
                    continue
                break
        return friendly_error(last_error) if last_error else "give me a second please:)"

    def _complete(
        self,
        client: OpenAI,
        model: str,
        messages: list,
        use_tools: bool,
        hide_reasoning: bool = False,
        tools: list | None = None,
        base_url: str = "",
    ):
        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
        }
        if use_tools:
            kwargs["tools"] = tools or TOOL_SCHEMAS
            kwargs["tool_choice"] = "auto"
        if hide_reasoning and self._is_groq_url(base_url or self.settings.get("openai_base_url") or ""):
            kwargs["extra_body"] = {
                "reasoning_format": "hidden",
                "reasoning_effort": "low",
            }
        return client.chat.completions.create(**kwargs)

    def _tool_call_message(self, choice) -> dict:
        return {
            "role": "assistant",
            "content": choice.content or "",
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments or "{}",
                    },
                }
                for call in choice.tool_calls
            ],
        }

    def _parse_args(self, raw) -> dict:
        if isinstance(raw, dict):
            return raw
        try:
            data = json.loads(raw or "{}")
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _describe_screen(self, focus: str) -> str:
        if is_phone():
            return "Screen awareness is only on the PC version of Opus."
        from opus.tools.screen import foreground_window, screenshot_png

        try:
            png = screenshot_png()
        except Exception as exc:
            log.exception("screenshot failed")
            return f"Could not capture the screen: {exc}"
        import base64

        image = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        last_error: Exception | None = None
        for vision_model in self._vision_models():
            try:
                response = self._execute_llm(
                    lambda client, base_url, provider: client.chat.completions.create(
                        model=self._resolve_vision_model(vision_model, provider, base_url),
                        messages=[
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "Describe what is on this screen for a PC assistant. "
                                        f"User asked: {focus[:400]}",
                                    },
                                    {"type": "image_url", "image_url": {"url": image}},
                                ],
                            }
                        ],
                        temperature=0.2,
                    )
                )
                return (response.choices[0].message.content or "").strip() or "I couldn't see anything useful."
            except Exception as exc:
                last_error = exc
                log.exception("vision failed model=%s", vision_model)
                if is_rate_limit(exc):
                    continue
        window = foreground_window()
        return (
            f"I couldn't read the screenshot ({friendly_error(last_error) if last_error else 'no vision model'}). "
            f"Foreground window: {window.get('title')}"
        )

    def _clean_history(self) -> list[dict]:
        cleaned = []
        for item in hub.recent_conversation()[-10:]:
            content = item.get("content") or ""
            if item.get("role") == "you" and content.lower().strip() in {"opus", "opus.", "opus?"}:
                continue
            if "error code:" in content.lower() or "trace" in content.lower():
                continue
            if len(content) > 2500:
                continue
            cleaned.append(item)
        if cleaned and cleaned[-1]["role"] == "you":
            cleaned = cleaned[:-1]
        return cleaned

    def _context_block(self, user_text: str) -> str:
        if is_phone():
            parts = [f"User said: {user_text}"]
            try:
                from opus.tools.weather import location_summary

                parts.append(location_summary(self.settings))
            except Exception:
                pass
            return "\n".join(parts)
        browser = hub.get_browser_context()
        window = foreground_window()
        parts = [
            f"User said: {user_text}",
            f"Foreground window: {window.get('title') or 'unknown'}",
            "Browser: "
            + (browser.get("browser") or "none")
            + f" | {browser.get('title') or ''} | {browser.get('url') or 'no tab'}",
        ]
        try:
            from opus.tools.weather import location_summary

            parts.append(location_summary(self.settings))
        except Exception:
            pass
        if browser.get("selection"):
            parts.append("Selected text:\n" + browser["selection"][:2500])
        if browser.get("page_text"):
            parts.append("Page excerpt:\n" + browser["page_text"][:3500])
        return "\n".join(parts)
