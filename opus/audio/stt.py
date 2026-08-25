"""Cloud speech-to-text from an uploaded clip. No local microphone required."""
from __future__ import annotations

import io

from opus.api_pool import ApiPool, is_rate_limit
from opus.logutil import get_logger
from opus.settings import Settings

log = get_logger()

PHONE_STT_PROMPT = (
    "Opus, play liked songs on Spotify, pause Spotify, skip the song, "
    "what's playing, weather, volume up, never mind."
)


def transcribe_audio_bytes(settings: Settings, data: bytes, *, filename: str = "utterance.webm") -> str:
    if not data:
        return ""
    pool = ApiPool(settings)
    if not pool.has_keys():
        return ""
    model = settings.get("stt_model") or "whisper-large-v3-turbo"
    name = filename or "utterance.webm"
    last_error: Exception | None = None
    for _ in range(pool.max_attempts()):
        client, key, _, _ = pool.stt_client()
        buffer = io.BytesIO(data)
        buffer.name = name
        try:
            try:
                result = client.audio.transcriptions.create(
                    model=model,
                    file=buffer,
                    language="en",
                    prompt=PHONE_STT_PROMPT,
                    temperature=0,
                )
            except TypeError:
                buffer.seek(0)
                result = client.audio.transcriptions.create(model=model, file=buffer, language="en")
            return (result.text or "").strip()
        except Exception as exc:
            last_error = exc
            if is_rate_limit(exc):
                pool.mark_cooldown(key)
                continue
            log.exception("phone stt failed")
            raise
    if last_error:
        raise last_error
    return ""
