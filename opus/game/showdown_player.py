from __future__ import annotations

import threading
import time
from typing import Callable

from opus.hub import hub
from opus.logutil import get_logger
from opus.settings import Settings

log = get_logger()


class ShowdownPlayer:
    """Play Pokémon Showdown from the live battle request."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._generation = 0
        self._running = False
        self._speak: Callable[[str], None] | None = None
        self._last_choice = ""

    @property
    def running(self) -> bool:
        return self._running

    def start(
        self,
        settings: Settings,
        *,
        speak: Callable[[str], None] | None = None,
        create_tab: bool = True,
    ) -> str:
        with self._lock:
            self._generation += 1
            gen = self._generation
            self._stop.set()
            old = self._thread

        if old is not None and old.is_alive() and old is not threading.current_thread():
            old.join(timeout=3.0)

        result = hub.browser_command(
            "showdown_play",
            {"createIfMissing": create_tab},
            timeout=20.0,
        )
        log.info("showdown start payload=%s", result)
        if not result.get("ok"):
            self.stop("Showdown inject failed.")
            return result.get("error") or (
                "Open https://play.pokemonshowdown.com/ in Edge with Opus Bridge loaded, then say play showdown."
            )

        with self._lock:
            if self._generation != gen:
                return "Showdown start was superseded."
            self._speak = speak
            self._stop.clear()
            self._running = True
            self._last_choice = ""
            self._thread = threading.Thread(target=self._loop, args=(gen,), daemon=True, name="opus-showdown")
            self._thread.start()

        payload = result.get("result") or {}
        hub.set_status(mode="playing", message="Playing Pokémon Showdown.")
        if payload.get("waiting") and not payload.get("choice"):
            return payload.get("message") or "Showdown's up. I'll take every turn."
        return "Playing Showdown. I'll pick the attacks and switches."

    def stop(self, reason: str = "Stopped Showdown.") -> str:
        was_running = self._running
        self._stop.set()
        with self._lock:
            self._running = False
        if was_running:
            try:
                hub.browser_command("showdown_stop", {}, timeout=8.0)
            except Exception:
                log.exception("showdown_stop failed")
            if hub.status.get("mode") == "playing":
                hub.set_status(mode="idle", message=reason[:120])
        return reason

    def _loop(self, gen: int) -> None:
        try:
            while not self._stop.is_set():
                if gen != self._generation:
                    return
                result = hub.browser_command("showdown_play", {"createIfMissing": False}, timeout=12.0)
                if result.get("ok"):
                    payload = result.get("result") or {}
                    choice = str(payload.get("choice") or "")
                    reason = str(payload.get("reason") or payload.get("message") or "")
                    if choice and choice != self._last_choice:
                        self._last_choice = choice
                        log.info("showdown chose %s (%s)", choice, reason)
                        hub.set_status(mode="playing", message=(reason or choice)[:120])
                else:
                    log.warning("showdown tick failed: %s", result)
                time.sleep(0.9)
        except Exception:
            log.exception("showdown loop crashed")
            self.stop("Showdown player crashed.")


showdown_player = ShowdownPlayer()
