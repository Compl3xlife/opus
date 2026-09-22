"""Reaction-role setup helpers for Discord chat commands."""
from __future__ import annotations

import re

REACTIONROLE_COMMAND_RE = re.compile(
    r"^(reaction\s*roles?|roles?)\b",
    re.IGNORECASE,
)
USAGE = (
    "Set reaction roles like: `opus roles 🎮 @Gamer 🎨 @Artist`\n"
    "`opus roles list` shows them. Reply with `opus roles remove` to unbind a message."
)

CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:(\d+)>")
ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")


def command_rest(text: str) -> str:
    cleaned = re.sub(r"^opus[,:]?\s*", "", text or "", flags=re.IGNORECASE).strip()
    return REACTIONROLE_COMMAND_RE.sub("", cleaned, count=1).strip()


def action_name(text: str) -> str:
    rest = command_rest(text).lower()
    if rest in {"", "help"}:
        return "help"
    if rest in {"list", "show", "status"}:
        return "list"
    first = rest.split()[:1]
    if first and first[0] in {"remove", "clear", "delete", "unbind"}:
        return "remove"
    return "set"


def emoji_key(emoji) -> str:
    ident = getattr(emoji, "id", None)
    if ident:
        return str(ident)
    return str(getattr(emoji, "name", None) or emoji)


def emoji_key_from_token(token: str) -> str:
    raw = (token or "").strip()
    match = CUSTOM_EMOJI_RE.search(raw)
    if match:
        return match.group(1)
    return raw


def parse_role_pairs(message) -> list[tuple[object, object]]:
    guild = getattr(message, "guild", None)
    if guild is None:
        return []
    content = command_rest(message.content or "")
    roles = list(getattr(message, "role_mentions", []) or [])
    if not roles:
        return []
    pairs: list[tuple[object, object]] = []
    cursor = 0
    for role in roles:
        needle = f"<@&{role.id}>"
        idx = content.find(needle, cursor)
        if idx < 0:
            continue
        before = content[cursor:idx].strip()
        cursor = idx + len(needle)
        token = before.split()[-1] if before.split() else ""
        if not token:
            continue
        pairs.append((token, role))
    return pairs
