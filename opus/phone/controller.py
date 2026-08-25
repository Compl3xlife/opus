"""Lightweight phone assistant — weather, Spotify API, chat. No games or PC control."""
from __future__ import annotations

import threading

from opus.assistant import Assistant
from opus.hub import hub
from opus.intents import Intent, match_intent_phases, plan_execution_batches
from opus.logutil import friendly_error, get_logger
from opus.speech_text import spoken_text

log = get_logger()


class PhoneController:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.assistant = Assistant(settings, on_panel=lambda _shown: None)
        self._lock = threading.Lock()

    def apply_settings(self) -> None:
        from opus.net_policy import configure_policy

        configure_policy(self.settings)

    def handle_text_sync(self, text: str) -> str:
        raw = (text or "").strip()
        if not raw:
            return ""
        with self._lock:
            try:
                reply = self._handle_text_inner(raw)
            except Exception as exc:
                log.exception("phone handle_text crashed for: %r", text)
                reply = friendly_error(exc)
        return spoken_text(reply)

    def _handle_text_inner(self, text: str) -> str:
        phases = match_intent_phases(text)
        flat = [intent for phase in phases for intent in phase]
        if not flat:
            return self._ask(text)
        if any(intent.name == "shush" for intent in flat):
            return ""
        if any(intent.name == "dismiss" for intent in flat):
            return "You're welcome."

        hub.add_message("you", text)
        replies: list[str] = []
        for phase in phases:
            for batch in plan_execution_batches(phase):
                for intent in batch:
                    reply = self._execute_intent(intent, text)
                    if reply:
                        replies.append(reply.rstrip("."))
        if not replies:
            return ""
        if len(replies) == 1:
            summary = replies[0]
        elif len(replies) <= 4:
            summary = ". ".join(replies)
        else:
            summary = f"Done — {len(replies)} commands"
        spoken = spoken_text(summary)
        hub.add_message("opus", spoken)
        return spoken

    def _ask(self, text: str) -> str:
        hub.add_message("you", text)
        try:
            reply = spoken_text(self.assistant.ask(text))
        except Exception as exc:
            log.exception("phone ask failed")
            reply = friendly_error(exc)
        if reply:
            hub.add_message("opus", reply)
        return reply

    def _execute_intent(self, intent: Intent, text: str) -> str:
        if intent.name == "location":
            from opus.tools.weather import location_summary

            return location_summary(self.settings)
        if intent.name == "weather":
            from opus.tools.weather import get_weather

            return get_weather(self.settings, city=intent.rest)
        if intent.name == "await_command":
            return "Yes?"
        if intent.name == "coinflip":
            from opus.discord_fun import coinflip

            return coinflip()
        if intent.name == "dice":
            from opus.discord_fun import roll_dice

            return roll_dice(intent.rest or text)
        if intent.name == "sleep":
            return "I'm still here whenever you need me."
        if intent.name == "spotify":
            from opus.tools.spotify import play_spotify

            return play_spotify(intent.rest, raw=text)
        if intent.name == "spotify_pause":
            from opus.tools.spotify import control_spotify

            return control_spotify("pause")
        if intent.name == "spotify_skip":
            from opus.tools.spotify import control_spotify

            return control_spotify("skip")
        if intent.name == "spotify_previous":
            from opus.tools.spotify import control_spotify

            return control_spotify("previous")
        if intent.name == "spotify_now":
            from opus.tools.spotify import control_spotify

            return control_spotify("now")
        if intent.name == "mute_voice":
            self.settings.update({"voice_responses": False})
            return "Voice responses off"
        if intent.name == "unmute_voice":
            self.settings.update({"voice_responses": True})
            return "Voice responses on"
        if intent.name == "set_volume" and intent.value is not None:
            self.settings.update({"volume": intent.value})
            return f"Volume set to {intent.value}"
        if intent.name == "volume_up":
            value = min(100, int(self.settings.get("volume") or 80) + 10)
            self.settings.update({"volume": value})
            return f"Volume {value}"
        if intent.name == "volume_down":
            value = max(0, int(self.settings.get("volume") or 80) - 10)
            self.settings.update({"volume": value})
            return f"Volume {value}"
        if intent.name == "creator":
            return "Involutional"
        if intent.name == "pc_only":
            return intent.rest or "That stays on the PC version of Opus."
        if intent.name == "ask":
            return self.assistant.ask(intent.rest or text)
        return self.assistant.ask(intent.rest or text)
