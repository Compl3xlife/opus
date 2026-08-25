from __future__ import annotations

import re

from opus.discord_store import DiscordStore, resolve_author_hint

MENTION_RE = re.compile(r"<@!?(\d+)>")
ACHIEVEMENT_OF_RE = re.compile(
    r"\b(?:what are|list|tell me(?: about)?|show(?: me)?|read|give me)\s+"
    r"(?:the\s+)?achievements?\s+(?:of|for)\s+(.+?)(?:\?|$)",
    re.IGNORECASE,
)
ACHIEVEMENT_HAS_RE = re.compile(
    r"\bwhat has\s+(.+?)\s+(?:achieved|accomplished)\b",
    re.IGNORECASE,
)
ACHIEVEMENT_POSSESSIVE_RE = re.compile(
    r"\b(?:what(?:'s| is| are)\s+)?(.+?)(?:'s)\s+achievements?\b",
    re.IGNORECASE,
)
ACHIEVEMENT_KEYWORD_RE = re.compile(
    r"\b("
    r"deathless|hitless|no[- ]hit|no[- ]death|"
    r"world record|\bwr\b|personal best|\bpb\b|"
    r"speedrun|first place|top \d+\s*%|"
    r"infernum|eternity(?:\s+(?:mode|death))?|"
    r"freelance|self[- ]taught|"
    r"(?:created|built|made)\s+opus|"
    r"binding of isaac|terraria|"
    r"\d{2,4}\s*(?:lb|lbs|kg)\b|"
    r"(?:bench|squat|deadlift|row|calf raise)\b|"
    r"calisthenics|"
    r"art|artist|drawing|drew|drawn|paint(?:ing|ed)?|sketch(?:ed|ing)?|"
    r"illustration|commission|"
    r"gif|gifs|animation|animated|animating|pixel\s*art|digital\s*art|"
    r"learn(?:ed|ing|s)|studying|studied|taught\s+(?:myself|himself|herself|themself)"
    r")\b",
    re.IGNORECASE,
)
HOSTILE_CHAT_RE = re.compile(
    r"("
    r"\b(?:ur|you'?re|you are)\s+lying\b|"
    r"\bbeat\s+(?:her|him|them|you|me)\b|"
    r"\b(?:coded|programmed)\s+to\b|"
    r"\b(?:shut up|kys)\b"
    r")",
    re.IGNORECASE,
)
CHAT_NOISE_RE = re.compile(
    r"\b(lol|lmao|lmfao|bruh|wtf|omg|idk|tbh|ngl|cope|"
    r"good morning|good night|\bgn\b|\bgm\b|wyd|"
    r"https?://|discord\.gg)",
    re.IGNORECASE,
)

COMP_ALIASES = frozenset(
    {
        "comp",
        "complex",
        "invo",
        "involutional",
        "aegritudo",
        "matthew",
        "comp daddy",
        "compdaddy",
        "compy",
        "compy wompy",
        "compywompy",
        "._repeat_.",
        "1406180271363199069",
    }
)

KNOWN_BANKS = [
    {
        "key": "comp",
        "aliases": COMP_ALIASES,
        "display": "Comp",
        "summary": (
            "Comp taught himself to code, spent three years freelancing, and built Opus - "
            "the intelligence tool you're talking to."
        ),
        "items": [
            "Created Opus, the ultimate intelligence tool running here",
            "Self-taught coder who coaches himself across languages",
            "Three years of freelance coding",
            "The Binding of Isaac - deathless",
            "Terraria: Eternity Death, Infernum, no-hit, and full zoom",
            "Top ~6% of lifters for his bodyweight",
        ],
        "note": (
            "Lifting at 146 lb: 255 bench, 325 row for 6, 315 squat, 400 calf raise for 12, "
            "plus mid-tier calisthenics."
        ),
    }
]


def is_achievements_question(text: str) -> bool:
    message = (text or "").strip()
    if not message:
        return False
    if ACHIEVEMENT_OF_RE.search(message):
        return True
    if ACHIEVEMENT_HAS_RE.search(message):
        return True
    if ACHIEVEMENT_POSSESSIVE_RE.search(message):
        return True
    return bool(re.search(r"\bachievements?\b", message, re.IGNORECASE)) and bool(
        re.search(r"\b(of|for|what|list|tell|show)\b", message, re.IGNORECASE)
    )


def _clean_subject(raw: str) -> str:
    text = (raw or "").strip()
    text = MENTION_RE.sub(lambda match: match.group(1), text)
    text = re.sub(
        r"^(?:tell me about|tell me|about|list|show me|show)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\b(user|the user|member|person)\b", "", text, flags=re.IGNORECASE)
    text = text.strip(" \t\"'`.,!?:;:-")
    return re.sub(r"\s+", " ", text).strip()


def parse_achievements_subject(text: str, user_name: str = "", member_roster: str = "") -> str:
    message = (text or "").strip()
    subject = ""
    for pattern in (ACHIEVEMENT_OF_RE, ACHIEVEMENT_HAS_RE, ACHIEVEMENT_POSSESSIVE_RE):
        match = pattern.search(message)
        if match:
            subject = _clean_subject(match.group(1))
            break
    if not subject:
        mention = MENTION_RE.search(message)
        if mention:
            subject = mention.group(1)
    if subject.lower() in {"me", "myself", "my", "mine"}:
        subject = (user_name or "").strip() or subject
    if subject.isdigit():
        resolved = _display_for_id(subject, member_roster)
        if resolved:
            return resolved
    return subject


def _display_for_id(user_id: str, member_roster: str) -> str:
    needle = f"id={user_id}"
    for line in (member_roster or "").splitlines():
        if needle not in line.replace(" ", ""):
            continue
        for part in line.split(","):
            part = part.strip()
            if part.lower().startswith("display="):
                return part.split("=", 1)[1].strip()
    return user_id


def _norm_name(text: str) -> str:
    return re.sub(r"[\s_\-]+", " ", (text or "").strip().lower())


def known_bank_for(subject: str) -> dict | None:
    lowered = _norm_name(subject)
    compact = lowered.replace(" ", "")
    if not lowered:
        return None
    for bank in KNOWN_BANKS:
        aliases = {_norm_name(alias) for alias in bank["aliases"]}
        compact_aliases = {alias.replace(" ", "") for alias in aliases}
        if lowered in aliases or compact in compact_aliases:
            return bank
        if any(
            len(alias) > 5 and (alias in lowered or alias.replace(" ", "") in compact)
            for alias in aliases
        ):
            return bank
    return None


def _search_hints(subject: str, member_roster: str, bank: dict | None) -> list[str]:
    hints: list[str] = []
    resolved = resolve_author_hint(subject, member_roster)
    for item in (subject, resolved):
        if item and item not in hints:
            hints.append(item)
    if bank:
        hints.extend(sorted(bank["aliases"]))
    if member_roster and subject:
        lowered = subject.lower()
        for line in member_roster.splitlines():
            if lowered not in line.lower():
                continue
            for part in line.split(","):
                part = part.strip()
                if "=" in part:
                    value = part.split("=", 1)[1].strip()
                    if value and value not in hints:
                        hints.append(value)
    seen = set()
    unique: list[str] = []
    for hint in hints:
        key = hint.lower()
        if key in seen or len(hint) < 2:
            continue
        seen.add(key)
        unique.append(hint)
    return unique[:12]


def _looks_like_achievement(content: str) -> bool:
    text = (content or "").strip()
    if len(text) < 12 or len(text) > 280:
        return False
    if HOSTILE_CHAT_RE.search(text):
        return False
    return bool(ACHIEVEMENT_KEYWORD_RE.search(text))


def is_raw_discord_chat(text: str) -> bool:
    line = (text or "").strip()
    if not line:
        return True
    if HOSTILE_CHAT_RE.search(line):
        return True
    if line.startswith("@Opus") or line.lower().startswith("@opus"):
        return True
    if " in #" in line and ": " in line:
        return True
    return False


def sanitize_achievement_bullets(bullets: list[str], snippets: list[str] | None = None) -> list[str]:
    snippet_text = " ".join(snippets or []).lower()
    clean: list[str] = []
    seen = set()
    for raw in bullets or []:
        item = re.sub(r"^\s*[-*•\d.)]+\s*", "", (raw or "").strip())
        item = re.sub(r"\s+", " ", item)
        if len(item) < 12 or is_raw_discord_chat(item):
            continue
        lowered = item.lower()
        if lowered in seen:
            continue
        if snippet_text and item.lower() in snippet_text:
            continue
        seen.add(lowered)
        clean.append(item[:180])
        if len(clean) >= 8:
            break
    return clean


def collect_scan_snippets(
    store: DiscordStore,
    guild_id: str,
    subject: str,
    member_roster: str = "",
    bank: dict | None = None,
    limit: int = 24,
) -> list[str]:
    hints = _search_hints(subject, member_roster, bank)
    rows = store.messages_for_profile(guild_id, hints, limit=200, author_only=True)
    mention_rows = store.messages_for_profile(
        guild_id, hints, limit=120, author_only=False, content_min=5
    )
    seen_ids = set()
    merged = []
    for row in list(rows) + list(mention_rows):
        mid = row.get("message_id")
        if mid in seen_ids:
            continue
        seen_ids.add(mid)
        merged.append(row)
    scored: list[tuple[int, str]] = []
    for row in merged:
        content = (row.get("content") or "").replace("\n", " ").strip()
        if not _looks_like_achievement(content):
            continue
        author = row.get("author_name") or "someone"
        channel = row.get("channel_name") or "unknown"
        scored.append((len(content), f"{author} in #{channel}: {content[:220]}"))
    scored.sort(key=lambda item: item[0], reverse=True)
    unique: list[str] = []
    seen = set()
    for _, line in scored:
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(line)
        if len(unique) >= limit:
            break
    return unique


def format_known_card(bank: dict, extras: list[str] | None = None) -> str:
    lines = [
        f"**{bank['display']}**",
        "",
        bank["summary"],
        "",
        "**Achievements**",
    ]
    for item in bank["items"]:
        lines.append(f"- {item}")
    note = (bank.get("note") or "").strip()
    if note:
        lines.append("")
        lines.append(note)
    return "\n".join(lines).strip()


def format_scan_card(display: str, bullets: list[str]) -> str:
    name = (display or "This person").strip()
    lines = [
        f"**{name}**",
        "",
        f"From the scanned server history for {name}:",
        "",
        "**Achievements**",
    ]
    for item in bullets[:10]:
        lines.append(f"- {item}")
    return "\n".join(lines).strip()


def fallback_bullets_from_snippets(snippets: list[str]) -> list[str]:
    bullets: list[str] = []
    for snippet in snippets:
        body = snippet.split(": ", 1)[-1].strip()
        if not _looks_like_achievement(body):
            continue
        if body not in bullets:
            bullets.append(body[:180])
        if len(bullets) >= 8:
            break
    return bullets
