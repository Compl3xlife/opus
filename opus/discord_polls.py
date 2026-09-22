"""Native Discord polls from chat commands."""
from __future__ import annotations

import re
from datetime import timedelta

POLL_COMMAND_RE = re.compile(
    r"^(poll|start\s+(a\s+)?poll|make\s+(a\s+)?poll)\b",
    re.IGNORECASE,
)
DURATION_RE = re.compile(
    r"\b(?:for|lasting)\s+(\d+)\s*(minutes?|mins?|m|hours?|hrs?|h|days?|d)\b",
    re.IGNORECASE,
)
MULTI_RE = re.compile(r"\b(multi(ple)?(\s+choice)?|allow\s+multiple)\b", re.IGNORECASE)
QUOTE_RE = re.compile(r'"([^"]+)"|“([^”]+)”')

USAGE = (
    "Start a poll like: `opus poll What should we play? | Valorant | Minecraft | League`\n"
    "Optional: `for 6 hours` or `multiple`.\n"
    "Single-choice polls can be bet with chips: `opus bet 50 1` or the buttons under the poll."
)


def _duration_from_match(match: re.Match) -> timedelta:
    amount = max(1, int(match.group(1)))
    unit = match.group(2).lower()
    if unit.startswith("d"):
        hours = min(amount * 24, 768)
    elif unit.startswith("min") or unit == "m":
        hours = max(1, min((amount + 59) // 60, 768))
    else:
        hours = min(amount, 768)
    return timedelta(hours=hours)


def parse_poll(text: str) -> tuple[str, list[str], timedelta, bool] | str:
    raw = POLL_COMMAND_RE.sub("", text or "", count=1).strip(" :,-")
    if not raw:
        return USAGE
    multiple = bool(MULTI_RE.search(raw))
    raw = MULTI_RE.sub("", raw).strip()
    duration = timedelta(hours=24)
    dur = DURATION_RE.search(raw)
    if dur:
        duration = _duration_from_match(dur)
        raw = (raw[: dur.start()] + raw[dur.end() :]).strip(" ,-")
    quoted = [(m.group(1) or m.group(2) or "").strip() for m in QUOTE_RE.finditer(raw)]
    quoted = [item for item in quoted if item]
    if len(quoted) >= 3:
        question, options = quoted[0], quoted[1:]
    else:
        parts = [part.strip(" ,") for part in re.split(r"\s*\|\s*", raw) if part.strip(" ,")]
        if len(parts) >= 3 or (len(parts) == 2 and "?" in parts[0]):
            question, options = parts[0], parts[1:]
        else:
            return USAGE
    question = question.strip()
    if question and "?" not in question:
        question = question.rstrip(".!") + "?"
    options = [opt[:55] for opt in options if opt]
    if not question or len(options) < 2:
        return USAGE
    return question[:300], options[:10], duration, multiple
