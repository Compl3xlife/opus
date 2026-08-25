"""Connected apps — Siri-style API control, not UI clicking.

Each app exposes the same shape so a future phone client can call Opus over HTTP
instead of depending on Windows window focus or SendKeys.
"""
from __future__ import annotations

from opus.settings import Settings


def list_connections(settings: Settings) -> list[dict]:
    from opus.apps.spotify import connection_status

    return [connection_status(settings)]
