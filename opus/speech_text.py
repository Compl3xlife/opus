"""Shorten tool/LLM replies so they sound like speech, not file paths."""
from __future__ import annotations

import re

FILEISH_RE = re.compile(
    r"(?i)(?:[a-z]:[\\/][^\s,]+)|(?:/[^\s,]+)|(?:\b[\w.-]+\.(?:py|ps1|js|png|mp4|txt|json|html|wav|exe|bat|cmd|md|ico|jpg|jpeg|gif)\b)"
)


def spoken_text(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return "Done."
    if raw.lower().startswith("i stopped"):
        cleaned = re.sub(r"\s+", " ", raw).strip()
        if len(cleaned) > 280:
            cleaned = cleaned[:277].rsplit(" ", 1)[0] + "."
        return cleaned
    cleaned = FILEISH_RE.sub("", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" :-.")
    lowered = cleaned.lower()
    if not cleaned or lowered in {"to", "in", "at", "the", "on", "saved", "saved to"}:
        return "Done."
    if len(cleaned) > 280:
        cleaned = cleaned[:277].rsplit(" ", 1)[0] + "."
    return cleaned
