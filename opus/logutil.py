from __future__ import annotations

import logging
from pathlib import Path

from opus.settings import appdata_dir


def get_logger() -> logging.Logger:
    log = logging.getLogger("opus")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    path = appdata_dir() / "opus.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    return log


def friendly_error(exc: BaseException) -> str:
    text = str(exc)
    lowered = text.lower()
    if "404" in text or "model_not_found" in lowered or "does not exist" in lowered:
        return "That model is no longer on Groq. Opus will switch to a current one — try again."
    if "429" in text or "rate limit" in lowered or "slow down" in lowered:
        return "give me a second please:)"
    if "401" in text or "invalid_api_key" in lowered or "unauthorized" in lowered:
        return "The API key was rejected. Open the panel and check it."
    if "timeout" in lowered or "timed out" in lowered:
        return "The model timed out."
    if "413" in text or "too large" in lowered:
        return "That request was too large. Try asking without looking at the whole screen."
    if "400" in text or "invalid" in lowered:
        return "The model couldn't handle that request. Try a shorter question."
    return "Something went wrong talking to the model."
