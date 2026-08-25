from __future__ import annotations

from opus.settings import Settings
from opus.tools.guardian import Guardian

__all__ = ["DownloadWatcher", "Guardian"]


class DownloadWatcher(Guardian):
    """Backward-compatible alias for the background protection service."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
