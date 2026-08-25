from __future__ import annotations

import threading
import time
from typing import Callable

from opus.hub import hub
from opus.logutil import get_logger
from opus.settings import Settings

log = get_logger()


class ManiaPlayer:
    """Perfect Web osu!mania play via the page clock (1K–18K)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._generation = 0
        self._running = False
        self._settings: Settings | None = None
        self._speak: Callable[[str], None] | None = None
        self._last_keys = 0
        self._last_state = ""

    @property
    def running(self) -> bool:
        return self._running

    def start(
        self,
        settings: Settings,
        *,
        speak: Callable[[str], None] | None = None,
    ) -> str:
        with self._lock:
            self._generation += 1
            gen = self._generation
            self._stop.set()
            old = self._thread

        if old is not None and old.is_alive() and old is not threading.current_thread():
            old.join(timeout=3.0)

        result = hub.browser_command("mania_play", {"startMap": True}, timeout=35.0)
        log.info("mania start payload=%s", result)
        if not result.get("ok"):
            self.stop("Mania inject failed.")
            return result.get("error") or (
                "Open https://webosumania.com/ in Edge with Opus Bridge loaded, then say play mania."
            )

        with self._lock:
            if self._generation != gen:
                return "Mania start was superseded."
            self._settings = settings
            self._speak = speak
            self._stop.clear()
            self._running = True
            self._thread = threading.Thread(target=self._loop, args=(gen,), daemon=True, name="opus-mania")
            self._thread.start()

        payload = result.get("result") or {}
        keys = int(payload.get("keys") or 0)
        state = str(payload.get("state") or "")
        self._last_keys = keys
        self._last_state = state
        hub.set_status(mode="playing", message="Playing Web osu!mania — perfect autoplay through 18K.")
        if state in {"PLAY", "WAIT"} or payload.get("playing") or payload.get("canvas"):
            if keys:
                return f"I've got the {keys}K chart. I'll SS it."
            return "Map's up. I'll SS it."
        if payload.get("started") == "started":
            return "Starting a map with autoplay. I'll SS it."
        return "Autoplay is on. Opening a beatmap on webosu!mania now."

    def stop(self, reason: str = "Stopped mania.") -> str:
        was_running = self._running
        self._stop.set()
        with self._lock:
            self._running = False
        if was_running:
            try:
                hub.browser_command("mania_stop", {}, timeout=8.0)
            except Exception:
                log.exception("mania_stop failed")
            if hub.status.get("mode") == "playing":
                hub.set_status(mode="idle", message=reason[:120])
        return reason

    def _loop(self, gen: int) -> None:
        try:
            while not self._stop.is_set():
                if gen != self._generation:
                    return
                need_start = self._last_state in {"", "menu", "idle"}
                result = hub.browser_command(
                    "mania_play",
                    {"startMap": need_start},
                    timeout=18.0 if need_start else 12.0,
                )
                if result.get("ok"):
                    payload = result.get("result") or {}
                    keys = int(payload.get("keys") or 0)
                    state = str(payload.get("state") or "")
                    started = str(payload.get("started") or "")
                    prev_state = self._last_state
                    if payload.get("canvas") or state in {"PLAY", "WAIT", "ingame", "loading"}:
                        self._last_state = state or "ingame"
                    elif started == "started":
                        self._last_state = "starting"
                    elif state:
                        self._last_state = state
                    if keys and keys != self._last_keys:
                        self._last_keys = keys
                        hub.set_status(
                            mode="playing",
                            message=f"Playing Web osu!mania {keys}K perfectly.",
                        )
                        log.info("mania tick keys=%s state=%s started=%s", keys, state, started)
                    elif self._last_state != prev_state:
                        if self._last_state == "PLAY":
                            hub.set_status(
                                mode="playing",
                                message=f"SS run — {self._last_keys or '?'}K.",
                            )
                        log.info("mania tick state=%s canvas=%s", self._last_state, payload.get("canvas"))
                else:
                    log.warning("mania tick failed: %s", result)
                time.sleep(1.5)
        except Exception:
            log.exception("mania loop crashed")
            self.stop("Mania player crashed.")


mania_player = ManiaPlayer()
