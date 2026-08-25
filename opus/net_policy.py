from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING, Any, Mapping

import httpx

from opus.logutil import get_logger

if TYPE_CHECKING:
    from opus.settings import Settings

log = get_logger()

DEFAULT_OUTBOUND_SUFFIXES = (
    "openai.com",
    "groq.com",
    "googleapis.com",
    "google.com",
    "gstatic.com",
    "googleusercontent.com",
    "duckduckgo.com",
    "bing.com",
    "wikipedia.org",
    "minecraft.wiki",
    "open-meteo.com",
    "ip-api.com",
    "pollinations.ai",
    "discord.com",
    "discordapp.com",
    "discord.gg",
    "discord.media",
    "discordapp.net",
    "spotify.com",
    "spotifycdn.com",
    "clients2.google.com",
    "chromewebstore.google.com",
    "addons.opera.com",
    "opera.com",
    "microsoft.com",
    "windows.net",
    "azure.com",
    "speech.microsoft.com",
)

LOCALHOST_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

_policy: "NetworkPolicy | None" = None


class NetworkPolicyError(PermissionError):
    pass


class NetworkPolicy:
    """Single outbound gate for Opus internet access; inbound is localhost-only by default."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _setting(self, key: str, default):
        value = self.settings.get(key)
        return default if value is None else value

    def enabled(self) -> bool:
        return bool(self._setting("network_policy_enabled", True))

    def _parse_hosts(self, raw: str) -> list[str]:
        return [item.strip().lower() for item in str(raw or "").split(",") if item.strip()]

    def _extra_outbound(self) -> list[str]:
        return self._parse_hosts(self.settings.get("network_outbound_allowlist") or "")

    def _extra_inbound(self) -> list[str]:
        return self._parse_hosts(self.settings.get("network_inbound_allow_hosts") or "")

    def _api_hosts(self) -> list[str]:
        hosts: list[str] = []
        for key in ("openai_base_url", "fallback_openai_base_url"):
            base = (self.settings.get(key) or "").strip()
            if not base:
                continue
            host = (urllib.parse.urlparse(base).hostname or "").lower()
            if host:
                hosts.append(host)
        return hosts

    def _host_allowed(self, host: str) -> bool:
        host = (host or "").lower().strip(".")
        if not host:
            return False
        for allowed in (*self._api_hosts(), *DEFAULT_OUTBOUND_SUFFIXES, *self._extra_outbound()):
            allowed = allowed.lower()
            if host == allowed or host.endswith("." + allowed):
                return True
        return False

    def outbound_allowed(self, url: str, *, web_read: bool = False) -> bool:
        if not self.enabled():
            return True
        parsed = urllib.parse.urlparse(url or "")
        scheme = (parsed.scheme or "").lower()
        host = (parsed.hostname or "").lower()
        if scheme not in {"http", "https"} or not host:
            return False
        if web_read and bool(self._setting("network_outbound_web_read", True)):
            return True
        return self._host_allowed(host)

    def assert_outbound(self, url: str, *, web_read: bool = False) -> None:
        if not self.outbound_allowed(url, web_read=web_read):
            raise NetworkPolicyError(f"Outbound blocked by Opus network policy: {url}")

    def inbound_allowed(self, client_host: str, headers: Mapping[str, Any] | None = None) -> bool:
        if not self.enabled():
            return True
        host = (client_host or "").lower()
        headers = headers or {}
        if host in self._extra_inbound():
            return True
        if not bool(self._setting("network_inbound_localhost_only", True)):
            return True
        if host not in LOCALHOST_HOSTS:
            return False
        token = (self.settings.get("network_inbound_token") or "").strip()
        if not token:
            return True
        provided = str(headers.get("x-opus-token") or headers.get("X-Opus-Token") or "").strip()
        return provided == token

    def bind_host(self, preferred: str) -> str:
        preferred = (preferred or "127.0.0.1").strip()
        if not self.enabled():
            return preferred
        if bool(self._setting("network_inbound_localhost_only", True)):
            if preferred not in LOCALHOST_HOSTS:
                log.warning("network policy forcing bind host to 127.0.0.1 (was %s)", preferred)
                return "127.0.0.1"
        return preferred


def configure_policy(settings: Settings) -> NetworkPolicy:
    global _policy
    _policy = NetworkPolicy(settings)
    return _policy


def get_policy() -> NetworkPolicy:
    if _policy is None:
        from opus.settings import Settings

        return configure_policy(Settings())
    return _policy


def guarded_urlopen(request, timeout: float = 12.0, *, web_read: bool = False):
    url = request.full_url if hasattr(request, "full_url") else str(request)
    get_policy().assert_outbound(url, web_read=web_read)
    return urllib.request.urlopen(request, timeout=timeout)


def guarded_httpx_get(url: str, **kwargs):
    get_policy().assert_outbound(url, web_read=bool(kwargs.pop("web_read", False)))
    return httpx.get(url, **kwargs)
