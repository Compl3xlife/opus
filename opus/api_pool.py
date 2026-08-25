"""Shared API key rotation, 429 cooldown, and provider failover."""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from openai import OpenAI

from opus.net_policy import get_policy

if TYPE_CHECKING:
    from opus.settings import Settings

_COOLDOWNS: dict[str, float] = {}
_KEY_INDEX = 0
_LOCK = threading.Lock()


def is_rate_limit(exc: BaseException) -> bool:
    text = str(exc).lower()
    if "429" in str(exc) or "rate limit" in text or "slow down" in text:
        return True
    status = getattr(exc, "status_code", None)
    return status == 429


class ApiPool:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def _dedupe_keys(keys: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for key in keys:
            key = (key or "").strip()
            if key and key not in seen:
                seen.add(key)
                out.append(key)
        return out

    def _provider_keys(self, prefix: str = "") -> tuple[str, list[str]]:
        if prefix:
            base = (self.settings.get(f"{prefix}openai_base_url") or "").strip()
            primary = (self.settings.get(f"{prefix}openai_api_key") or "").strip()
            extras = self.settings.get(f"{prefix}openai_api_keys") or []
        else:
            base = (self.settings.get("openai_base_url") or "").strip()
            primary = (self.settings.get("openai_api_key") or "").strip()
            extras = self.settings.get("openai_api_keys") or []
        keys = self._dedupe_keys([primary, *list(extras)])
        return base, keys

    def providers(self) -> list[tuple[str, str, list[str]]]:
        """Return (label, base_url, keys) for primary then optional fallback."""
        out: list[tuple[str, str, list[str]]] = []
        primary_base, primary_keys = self._provider_keys("")
        if primary_keys:
            out.append(("primary", primary_base or "https://api.openai.com/v1", primary_keys))

        fallback_base, fallback_keys = self._provider_keys("fallback_")
        if fallback_keys and fallback_base:
            out.append(("fallback", fallback_base, fallback_keys))
        return out

    def has_keys(self) -> bool:
        return bool(self.providers())

    def max_attempts(self) -> int:
        total = sum(len(keys) for _, _, keys in self.providers())
        return max(total, 1)

    def cooldown_seconds(self) -> float:
        try:
            return float(self.settings.get("api_key_cooldown_seconds") or 60)
        except (TypeError, ValueError):
            return 60.0

    def mark_cooldown(self, api_key: str, seconds: float | None = None) -> None:
        if not api_key:
            return
        delay = self.cooldown_seconds() if seconds is None else seconds
        with _LOCK:
            _COOLDOWNS[api_key] = time.time() + max(1.0, delay)

    def _available_keys(self, keys: list[str]) -> list[str]:
        now = time.time()
        with _LOCK:
            ready = [k for k in keys if now >= _COOLDOWNS.get(k, 0)]
        return ready or keys

    def next_client(self) -> tuple[OpenAI, str, str, str]:
        """Rotate through providers/keys, skipping keys in 429 cooldown."""
        global _KEY_INDEX
        providers = self.providers()
        if not providers:
            return OpenAI(api_key="", base_url=None), "", "", ""

        slots: list[tuple[str, str, str]] = []
        for label, base_url, keys in providers:
            for key in self._available_keys(keys):
                slots.append((label, base_url, key))

        if not slots:
            for label, base_url, keys in providers:
                for key in keys:
                    slots.append((label, base_url, key))

        with _LOCK:
            label, base_url, pick = slots[_KEY_INDEX % len(slots)]
            _KEY_INDEX += 1

        if base_url:
            get_policy().assert_outbound(base_url)

        client = OpenAI(
            api_key=pick,
            base_url=base_url or None,
            timeout=18.0,
            max_retries=0,
        )
        return client, pick, base_url, label

    def stt_client(self) -> tuple[OpenAI, str, str, str]:
        """Same rotation policy, shorter timeout for Whisper."""
        client, key, base_url, label = self.next_client()
        return (
            OpenAI(api_key=key, base_url=base_url or None, timeout=10.0, max_retries=0),
            key,
            base_url,
            label,
        )

    def model_for_provider(self, provider_label: str, kind: str = "chat") -> str | None:
        if provider_label != "fallback":
            return None
        if kind == "vision":
            return (self.settings.get("fallback_vision_model") or "").strip() or None
        return (self.settings.get("fallback_model") or "").strip() or None
