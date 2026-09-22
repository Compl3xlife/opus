from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import uvicorn
import webview

from opus.assistant import Assistant
from opus.audio.listener import WakeListener
from opus.audio.tts import Speaker
from opus.discord_bot import DiscordBot
from opus.hub import hub
from opus.intents import (
    Intent,
    contains_wake,
    is_wake_only,
    match_intent_phases,
    plan_execution_batches,
)
from opus.logutil import friendly_error, get_logger
from opus.panel import Panel
from opus.net_policy import configure_policy, get_policy
from opus.server import create_app
from opus.settings import Settings
from opus.tools.downloads import Guardian
from opus.tools.images import generate_png
from opus.tools.keyboard import type_text
from opus.tools.record import open_clips_folder, recorder
from opus.tools.vault import fill_login
from opus.tray import Tray
from opus.game.agent import game_agent
from opus.game.chess_player import chess_player
from opus.game.mania_player import mania_player
from opus.game.showdown_player import showdown_player
from opus.speech_text import spoken_text
from opus.ui.icons import ensure_icons, icon_path
from opus.win_icon import set_process_icon

log = get_logger()


@dataclass
class IntentResult:
    reply: str | None = None
    keep_listening: bool = True
    arm_followup: bool = True
    # Deferred terminal actions after the spoken summary.
    restart: bool = False
    shutdown: bool = False


class OpusApp:
    def __init__(self) -> None:
        ensure_icons()
        self.settings = Settings()
        configure_policy(self.settings)
        recorder.set_settings(self.settings)
        self.panel = Panel(self.settings)
        self.speaker = Speaker(self.settings)
        self.assistant = Assistant(self.settings, on_panel=self._set_panel, on_restart=self.restart)
        self.listener = WakeListener(
            self.settings,
            on_utterance=self.handle_text,
            on_enabled=lambda enabled: self.tray.set_active(enabled),
        )
        self.tray = Tray(self)
        self.guardian = Guardian(self.settings)
        self.discord: DiscordBot | None = None
        self._from_call = False
        self._call_reply_text = ""
        self._call_guild_id = ""
        self._server: uvicorn.Server | None = None
        self._hotkeys = None
        self._job = 0

    def interrupt(self) -> None:
        self._job += 1
        try:
            self.assistant.cancel()
        except Exception:
            pass
        try:
            self.speaker.stop()
        except Exception:
            pass
        hub.set_status(speaking=False, mode="followup", message="I'm listening.")
        self.listener.arm_followup(25)

    def listening_enabled(self) -> bool:
        return bool(self.listener._enabled)

    def show_panel(self) -> None:
        self.panel.show()

    def hide_panel(self) -> None:
        self.panel.hide()

    def toggle_listening(self) -> None:
        enabled = not self.listener._enabled
        self.listener.set_enabled(enabled)
        self.tray.set_active(enabled)

    def apply_settings(self) -> None:
        configure_policy(self.settings)
        try:
            self.listener.restart()
        except Exception:
            pass
        try:
            self.guardian.stop()
            if self.settings.get("background_protection"):
                self.guardian.start()
        except Exception:
            log.exception("guardian restart failed")
        try:
            enabled = bool(self.settings.get("discord_enabled") and self.settings.get("discord_bot_token"))
            if enabled:
                if not self.discord:
                    self.discord = self._make_discord()
                self.discord.start()
            elif self.discord:
                self.discord.stop()
                self.discord = None
        except Exception:
            log.exception("discord restart failed")

    def handle_text(self, text: str, require_wake: bool = True) -> None:
        threading.Thread(target=self._handle_text, args=(text, require_wake), daemon=True).start()

    def _handle_text(self, text: str, require_wake: bool) -> None:
        try:
            self._handle_text_inner(text, require_wake)
        except Exception:
            log.exception("_handle_text crashed for: %r", text)

    def _handle_text_inner(self, text: str, require_wake: bool) -> None:
        busy = bool(hub.status.get("speaking")) or hub.status.get("mode") in {"thinking", "speaking"}
        if contains_wake(text) and busy:
            self.interrupt()
            if is_wake_only(text):
                return

        phases = match_intent_phases(text)
        flat = [intent for phase in phases for intent in phase]
        if not flat:
            return

        if any(intent.name == "shush" for intent in flat):
            self.interrupt()
            return
        if any(intent.name == "dismiss" for intent in flat):
            if busy:
                try:
                    self.assistant.cancel()
                except Exception:
                    pass
                try:
                    self.speaker.stop()
                except Exception:
                    pass
            self._reply("You're welcome.", arm_followup=True)
            return

        if (
            require_wake
            and len(flat) == 1
            and flat[0].name == "ask"
            and not text.lower().strip().startswith("opus")
            and not contains_wake(text)
        ):
            return

        if len(flat) == 1 and flat[0].name == "await_command":
            self._execute_intent(flat[0], text, self._job)
            return

        self._job += 1
        job = self._job
        hub.add_message("you", text)

        results: list[IntentResult] = []
        for phase in phases:
            for batch in plan_execution_batches(phase):
                if job != self._job:
                    return
                results.extend(self._run_intent_batch(batch, text, job))

        if job != self._job:
            return

        do_restart = any(result.restart for result in results)
        do_shutdown = any(result.shutdown for result in results)
        keep_listening = all(result.keep_listening for result in results) and not do_restart and not do_shutdown
        arm_followup = any(result.arm_followup for result in results) and keep_listening
        summary = self._combine_intent_replies(results)

        if summary:
            self._reply(summary, keep_listening=keep_listening, arm_followup=arm_followup)
        elif keep_listening and arm_followup:
            self.listener.arm_followup(25)

        if do_shutdown:
            self.shutdown()
        elif do_restart:
            self.restart()

    def _run_intent_batch(self, batch: list[Intent], text: str, job: int) -> list[IntentResult]:
        if not batch:
            return []
        if len(batch) == 1:
            return [self._execute_intent(batch[0], text, job)]

        results: list[IntentResult | None] = [None] * len(batch)

        def _worker(index: int, intent: Intent) -> None:
            results[index] = self._execute_intent(intent, text, job)

        with ThreadPoolExecutor(max_workers=min(8, len(batch))) as pool:
            futures = [pool.submit(_worker, index, intent) for index, intent in enumerate(batch)]
            for future in as_completed(futures):
                future.result()

        return [result for result in results if result is not None]

    @staticmethod
    def _combine_intent_replies(results: list[IntentResult]) -> str:
        parts: list[str] = []
        for result in results:
            reply = (result.reply or "").strip()
            if reply:
                parts.append(reply.rstrip("."))
        if not parts:
            return ""
        if len(parts) == 1:
            return parts[0] + ("." if not parts[0].endswith((".", "!", "?")) else "")
        # Keep the spoken summary short for multi-command runs.
        if len(parts) <= 4:
            return ". ".join(parts) + "."
        return f"Done — {len(parts)} commands."

    def _active_call_guild_id(self) -> str:
        if getattr(self, "_from_call", False):
            return str(getattr(self, "_call_guild_id", "") or "")
        return ""

    def _execute_intent(self, intent: Intent, text: str, job: int) -> IntentResult:
        if intent.name == "location":
            from opus.tools.weather import location_summary

            return IntentResult(reply=location_summary(self.settings))
        if intent.name == "weather":
            from opus.tools.weather import get_weather

            return IntentResult(reply=get_weather(self.settings, city=intent.rest))
        if intent.name == "cyberbox":
            from opus.tools.cyberbox import inspect_in_box

            return IntentResult(reply=inspect_in_box(intent.rest or text))
        if intent.name == "open_panel":
            self.show_panel()
            return IntentResult(reply="Panel is open")
        if intent.name == "close_panel":
            try:
                self.hide_panel()
            except Exception:
                log.exception("close panel failed")
            return IntentResult(reply="Panel closed")
        if intent.name == "await_command":
            if not self.listener._enabled:
                self.listener.set_enabled(True)
                self.tray.set_active(True)
            self.listener.arm_followup(25)
            hub.set_status(listening=True, mode="followup", message="I'm listening.")
            return IntentResult(reply=None, arm_followup=True)
        if intent.name == "discord_mod":
            if hasattr(self, "discord") and self.discord:
                action, _, target = intent.rest.partition("|")
                return IntentResult(reply=self.discord.run_mod_command(action, target))
            return IntentResult(reply="Discord bot isn't running")
        if intent.name == "coinflip":
            from opus.discord_fun import coinflip

            return IntentResult(reply=coinflip())
        if intent.name == "dice":
            from opus.discord_fun import roll_dice

            return IntentResult(reply=roll_dice(intent.rest or text))
        if intent.name == "discord_play_music":
            if hasattr(self, "discord") and self.discord:
                return IntentResult(
                    reply=self.discord.run_play_music(intent.rest, guild_id=self._active_call_guild_id())
                )
            return IntentResult(reply="Discord bot isn't running")
        if intent.name == "discord_skip_music":
            if hasattr(self, "discord") and self.discord:
                return IntentResult(reply=self.discord.run_skip_music(guild_id=self._active_call_guild_id()))
            return IntentResult(reply="Discord bot isn't running")
        if intent.name == "discord_stop_music":
            if hasattr(self, "discord") and self.discord:
                return IntentResult(reply=self.discord.run_stop_music(guild_id=self._active_call_guild_id()))
            return IntentResult(reply="Discord bot isn't running")
        if intent.name == "discord_join_call":
            if hasattr(self, "discord") and self.discord:
                return IntentResult(reply=self.discord.run_join_call())
            return IntentResult(reply="Discord bot isn't running")
        if intent.name == "discord_invite":
            if hasattr(self, "discord") and self.discord:
                return IntentResult(reply=self.discord.invite_reply())
            return IntentResult(reply="Discord bot isn't running")
        if intent.name == "discord_bot_leave":
            if hasattr(self, "discord") and self.discord:
                return IntentResult(reply=self.discord.run_leave_call())
            return IntentResult(reply="Discord bot isn't running")
        if intent.name == "discord_self_mute":
            from opus.tools.discord_keys import toggle_mute

            toggle_mute()
            return IntentResult(reply="Toggled mute")
        if intent.name == "discord_self_deafen":
            from opus.tools.discord_keys import toggle_deafen

            toggle_deafen()
            return IntentResult(reply="Toggled deafen")
        if intent.name == "discord_leave_call":
            from opus.tools.discord_keys import leave_call

            leave_call()
            return IntentResult(reply="Left the call")
        if intent.name == "discord_search":
            from opus.tools.discord_keys import search_discord

            search_discord(intent.rest)
            return IntentResult(reply="Opened search" if intent.rest else "Opened Discord search")
        if intent.name == "shutdown":
            return IntentResult(reply="Goodbye", keep_listening=False, arm_followup=False, shutdown=True)
        if intent.name == "sleep":
            self.listener.set_enabled(False)
            self.tray.set_active(False)
            return IntentResult(
                reply="Goodnight. Say Opus to wake me",
                keep_listening=False,
                arm_followup=False,
            )
        if intent.name == "spotify":
            from opus.tools.spotify import play_spotify

            return IntentResult(reply=play_spotify(intent.rest, raw=text))
        if intent.name == "spotify_pause":
            from opus.tools.spotify import control_spotify

            return IntentResult(reply=control_spotify("pause"))
        if intent.name == "spotify_skip":
            from opus.tools.spotify import control_spotify

            return IntentResult(reply=control_spotify("skip"))
        if intent.name == "spotify_previous":
            from opus.tools.spotify import control_spotify

            return IntentResult(reply=control_spotify("previous"))
        if intent.name == "spotify_now":
            from opus.tools.spotify import control_spotify

            return IntentResult(reply=control_spotify("now"))
        if intent.name == "mute_voice":
            self.settings.update({"voice_responses": False})
            hub.add_message("opus", "Voice responses off.")
            return IntentResult(reply="Voice responses off")
        if intent.name == "unmute_voice":
            self.settings.update({"voice_responses": True})
            return IntentResult(reply="Voice responses on")
        if intent.name == "set_volume" and intent.value is not None:
            self.settings.update({"volume": intent.value})
            return IntentResult(reply=f"Volume set to {intent.value}")
        if intent.name == "volume_up":
            value = min(100, int(self.settings.get("volume") or 80) + 10)
            self.settings.update({"volume": value})
            return IntentResult(reply=f"Volume {value}")
        if intent.name == "volume_down":
            value = max(0, int(self.settings.get("volume") or 80) - 10)
            self.settings.update({"volume": value})
            return IntentResult(reply=f"Volume {value}")
        if intent.name == "start_recording":
            return IntentResult(reply=recorder.start())
        if intent.name == "stop_recording":
            return IntentResult(reply=recorder.stop())
        if intent.name == "clip":
            return IntentResult(reply=recorder.clip())
        if intent.name == "creator":
            return IntentResult(reply="Involutional")
        if intent.name == "restart":
            return IntentResult(reply="Restarting", keep_listening=False, arm_followup=False, restart=True)
        if intent.name == "type":
            try:
                self.hide_panel()
            except Exception:
                pass
            type_text(intent.rest)
            if job != self._job:
                return IntentResult(reply=None, arm_followup=False)
            return IntentResult(reply="Done")
        if intent.name == "generate_png":
            generate_png(intent.rest)
            if job != self._job:
                return IntentResult(reply=None, arm_followup=False)
            return IntentResult(reply="Done")
        if intent.name == "fill_login":
            try:
                self.hide_panel()
            except Exception:
                pass
            result = fill_login(intent.rest)
            if job != self._job:
                return IntentResult(reply=None, arm_followup=False)
            lowered = result.lower()
            if lowered.startswith("typed"):
                return IntentResult(reply="Done")
            if "locked" in lowered:
                return IntentResult(reply="That password is locked in the browser")
            return IntentResult(reply="I don't have that login yet")
        if intent.name == "install_extension":
            from opus.tools.browser import add_extension

            result = add_extension(intent.rest, self.settings.get("default_browser") or "opera-gx")
            if job != self._job:
                return IntentResult(reply=None, arm_followup=False)
            return IntentResult(
                reply="Done" if result.lower().startswith(("installed", "already")) else result
            )
        if intent.name == "remove_extension":
            from opus.tools.browser import remove_extension

            result = remove_extension(intent.rest, self.settings.get("default_browser") or "opera-gx")
            if job != self._job:
                return IntentResult(reply=None, arm_followup=False)
            return IntentResult(reply="Done" if result.lower().startswith("removed") else result)
        if intent.name == "screenshot":
            return IntentResult(reply=recorder.screenshot())
        if intent.name == "stop_playing":
            chess_player.stop("Stopped chess.")
            mania_player.stop("Stopped mania.")
            showdown_player.stop("Stopped Showdown.")
            return IntentResult(reply=game_agent.stop("Stopped playing."))
        if intent.name == "start_showdown":
            chess_player.stop("Switching to Showdown.")
            mania_player.stop("Switching to Showdown.")
            game_agent.stop("Switching to Showdown.")
            try:
                result = showdown_player.start(
                    self.settings,
                    speak=lambda msg: self._reply(msg, arm_followup=False),
                    create_tab=True,
                )
            except Exception:
                log.exception("start_showdown failed")
                return IntentResult(reply="Showdown failed to start")
            return IntentResult(reply=result)
        if intent.name == "start_mania":
            chess_player.stop("Switching to mania.")
            showdown_player.stop("Switching to mania.")
            game_agent.stop("Switching to mania.")
            try:
                result = mania_player.start(
                    self.settings,
                    speak=lambda msg: self._reply(msg, arm_followup=False),
                )
            except Exception:
                log.exception("start_mania failed")
                return IntentResult(reply="Mania failed to start")
            return IntentResult(reply=result)
        if intent.name == "start_chess":
            game_agent.stop("Switching to chess engine.")
            mania_player.stop("Switching to chess.")
            showdown_player.stop("Switching to chess.")
            color = "auto"
            rest = (intent.rest or "").lower()
            if re.search(r"\bas\s+black\b|\bplay\s+black\b", rest):
                color = "black"
            elif re.search(r"\bas\s+white\b|\bplay\s+white\b", rest):
                color = "white"
            try:
                result = chess_player.start(
                    self.settings,
                    color=color,
                    speak=None,
                )
                log.info("start_chess result=%s running=%s", result, chess_player.running)
            except Exception:
                log.exception("start_chess failed")
                return IntentResult(reply="Chess failed to start")
            return IntentResult(reply=result)
        if intent.name == "start_playing":
            if not self.settings.get("game_play_enabled"):
                return IntentResult(reply="Game play is disabled in settings")
            from opus.game.play_router import start_play

            url = hub.get_browser_context().get("url") or ""
            try:
                result = start_play(
                    self.settings,
                    intent.rest or "",
                    url=url,
                    speak=lambda msg: self._reply(msg, arm_followup=False),
                )
            except Exception:
                log.exception("start_playing failed")
                return IntentResult(reply="Couldn't start playing")
            return IntentResult(reply=result)
        if intent.name == "run_app":
            from opus.tools.apps import run_app

            result = run_app(intent.rest)
            if job != self._job:
                return IntentResult(reply=None, arm_followup=False)
            return IntentResult(reply="Done" if "done" in result.lower() else result)
        if intent.name == "open_url":
            from opus.tools.apps import open_url

            url = intent.rest
            if not url.startswith("http"):
                url = "https://" + url
            browser = self.settings.get("default_browser") or "opera-gx"
            log.info("open_url: %s browser=%s", url, browser)
            result = open_url(url, browser)
            log.info("open_url result: %s", result)
            if job != self._job:
                return IntentResult(reply=None, arm_followup=False)
            return IntentResult(reply="Done" if "opened" in result.lower() else result)
        if intent.name == "open_url_in":
            from opus.tools.apps import open_url

            url, _, browser = intent.rest.partition("|")
            if not url.startswith("http"):
                url = "https://" + url
            result = open_url(url, browser)
            if job != self._job:
                return IntentResult(reply=None, arm_followup=False)
            return IntentResult(reply="Done" if "opened" in result.lower() else result)
        if intent.name == "open_tab":
            from opus.tools.apps import open_url

            open_url("about:blank", self.settings.get("default_browser") or "opera-gx")
            if job != self._job:
                return IntentResult(reply=None, arm_followup=False)
            return IntentResult(reply="Done")

        hub.set_status(mode="thinking", message="Thinking…")
        from opus.tools.spotify import try_spotify_from_text

        spotify_result = try_spotify_from_text(text)
        if spotify_result is not None:
            return IntentResult(reply=spotify_result)
        try:
            answer = self.assistant.ask(intent.rest or text)
        except Exception as exc:
            log.exception("ask failed")
            answer = friendly_error(exc)
        if job != self._job:
            return IntentResult(reply=None, arm_followup=False)
        return IntentResult(reply=answer)

    def _reply(self, text: str, *, keep_listening: bool = True, arm_followup: bool = True) -> None:
        spoken = spoken_text(text)
        hub.add_message("opus", spoken)
        hub.set_status(mode="speaking", message=spoken[:120], last_spoken=spoken)
        try:
            if getattr(self, "_from_call", False):
                self._call_reply_text = text.strip() or spoken
                return
            self.speaker.say(spoken)
        finally:
            hub.set_status(speaking=False)
            if keep_listening:
                if arm_followup:
                    self.listener.arm_followup(25)
                else:
                    self.listener.disarm_followup()
                self.listener.mute_for(0.12)
            else:
                hub.set_status(mode="listening", message=text[:120])

    def ask_discord(
        self,
        text: str,
        user_name: str = "",
        image_urls: list[str] | None = None,
        author_names: list[str] | None = None,
        member_roster: str = "",
        guild_id: str = "",
    ) -> str:
        answer = self.assistant.ask_chat_only(
            text,
            user_name=user_name,
            image_urls=image_urls,
            author_names=author_names,
            member_roster=member_roster,
            guild_id=guild_id,
        )
        hub.add_message("you", f"[discord:{user_name}] {text}")
        hub.add_message("opus", answer)
        return answer

    def _set_panel(self, show: bool) -> None:
        if show:
            self.show_panel()
        else:
            self.hide_panel()

    def restart(self) -> None:
        root = Path(__file__).resolve().parent.parent
        pythonw = root / ".venv" / "Scripts" / "pythonw.exe"
        if not pythonw.exists():
            pythonw = Path(sys.executable)
        # Use powershell to wait 2 seconds then relaunch — more reliable than cmd timeout
        ps_cmd = (
            f'Start-Sleep -Seconds 2; '
            f'Set-Location "{root}"; '
            f'Start-Process "{pythonw}" -ArgumentList "-m","opus" -WorkingDirectory "{root}"'
        )
        try:
            subprocess.Popen(
                ["powershell", "-WindowStyle", "Hidden", "-Command", ps_cmd],
                cwd=str(root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000,
            )
        except Exception:
            log.exception("failed to relaunch Opus")
            self._reply("I couldn't restart.")
            return
        self.shutdown()

    def shutdown(self) -> None:
        chess_player.stop("Stopped chess.")
        mania_player.stop("Stopped mania.")
        showdown_player.stop("Stopped Showdown.")
        game_agent.stop("Stopped playing.")
        if self.discord:
            self.discord.stop()
        self.listener.stop()
        self.guardian.stop()
        try:
            if recorder.status().get("recording"):
                recorder.stop(restart_buffer=False)
            recorder.stop_buffer()
        except Exception:
            log.exception("recorder shutdown failed")
        # Hotkey thread is daemon — dies with process
        if self._server:
            self._server.should_exit = True
        self.tray.stop()
        try:
            if self.panel.window:
                self.panel.window.destroy()
        except Exception:
            pass
        os._exit(0)

    def _start_server(self) -> None:
        host = get_policy().bind_host(str(self.settings.get("host") or "127.0.0.1"))
        port = int(self.settings.get("port"))
        app = create_app(self.settings, self)
        config = uvicorn.Config(app, host=host, port=port, log_level="warning", loop="asyncio")
        self._server = uvicorn.Server(config)

        def run() -> None:
            loop = __import__("asyncio").new_event_loop()
            __import__("asyncio").set_event_loop(loop)
            hub.set_loop(loop)
            loop.run_until_complete(self._server.serve())

        threading.Thread(target=run, daemon=True, name="opus-http").start()
        for _ in range(50):
            if self._server.started:
                break
            time.sleep(0.1)

    def _start_hotkeys(self) -> None:
        """Hotkeys disabled to prevent cursor flicker."""
        pass

    def _make_discord(self) -> DiscordBot:
        return DiscordBot(
            self.settings,
            self.ask_discord,
            speaker=self.speaker,
            on_call_speech=self.handle_call_speech,
            transcribe=self.listener.transcribe_array,
        )

    def handle_call_speech(
        self,
        text: str,
        followup: bool = False,
        user_name: str = "",
        author_names: list[str] | None = None,
        member_roster: str = "",
        guild_id: str = "",
    ) -> str:
        from opus.intents import contains_wake, match_intent_phases

        raw = text if contains_wake(text) or followup else f"opus {text}"
        phases = match_intent_phases(raw)
        flat = [intent for phase in phases for intent in phase]
        extra = {intent.name for intent in flat} - {"ask", "await_command"}
        # Call follow-up is everyone in the VC. Don't let overheard "sleep"/"thanks"
        # pause the PC mic unless they actually said Opus.
        if not contains_wake(text):
            extra -= {"sleep", "shutdown", "restart", "dismiss"}
            phases = [
                [intent for intent in phase if intent.name not in {"sleep", "shutdown", "restart", "dismiss"}]
                for phase in phases
            ]
            flat = [intent for phase in phases for intent in phase]
        self._from_call = True
        self._call_guild_id = guild_id or ""
        self._call_reply_text = ""
        try:
            if not extra and any(intent.name == "ask" for intent in flat):
                return (
                    self.ask_discord(
                        text,
                        user_name=user_name,
                        author_names=author_names,
                        member_roster=member_roster,
                        guild_id=guild_id,
                    )
                    or ""
                )
            self._handle_text_inner(text, require_wake=not followup)
            return self._call_reply_text
        finally:
            self._from_call = False
            self._call_guild_id = ""

    def run(self) -> None:
        set_process_icon()
        log.info("Opus starting")
        self._start_server()
        self.tray.start()
        try:
            self.guardian.start()
        except Exception:
            log.exception("background protection failed")
        try:
            self.listener.start()
        except Exception:
            log.exception("listener failed")
        if self.settings.get("discord_enabled") and self.settings.get("discord_bot_token"):
            try:
                self.discord = self._make_discord()
                self.discord.start()
            except Exception:
                log.exception("discord bot failed")
        self._start_hotkeys()
        # Clip buffer disabled on startup — ffmpeg causes cursor flicker
        # Use "Opus start buffer" to enable manually if needed
        hub.set_status(listening=True, mode="listening")
        window = self.panel.create()

        def shown():
            window.show()

        ico = icon_path(active=True)
        webview.start(shown, debug=False, icon=str(ico) if ico.exists() else None)


def main() -> None:
    OpusApp().run()
