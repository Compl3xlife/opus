"""Tiny Discord fun replies — coin flip, dice."""
from __future__ import annotations

import random
import re

COINFLIP_RE = re.compile(
    r"\b("
    r"flip(?:\s+a|\s+the)?\s+coins?|"
    r"coin\s*flips?|"
    r"heads\s+or\s+tails"
    r")\b",
    re.IGNORECASE,
)
DICE_RE = re.compile(
    r"\b(roll(?:\s+a|\s+the)?\s+dice|roll(?:\s+a)?\s+die|d(?:20|12|10|8|6|4)\b)\b",
    re.IGNORECASE,
)
DICE_SIDES_RE = re.compile(r"\bd(\d{1,3})\b", re.IGNORECASE)


def coinflip() -> str:
    face = random.choice(("heads", "tails"))
    return f"It's {face}."


def roll_dice(text: str = "") -> str:
    sides = 6
    match = DICE_SIDES_RE.search(text or "")
    if match:
        try:
            sides = max(2, min(1000, int(match.group(1))))
        except ValueError:
            sides = 6
    value = random.randint(1, sides)
    return f"Rolled {value} on a d{sides}."


def fun_reply(text: str) -> str | None:
    raw = re.sub(r"^\s*opus[,:]?\s*", "", (text or "").strip(), flags=re.IGNORECASE).strip()
    if not raw:
        return None
    if COINFLIP_RE.search(raw) and not re.search(r"\d", raw):
        return coinflip()
    if DICE_RE.search(raw):
        return roll_dice(raw)
    return None
