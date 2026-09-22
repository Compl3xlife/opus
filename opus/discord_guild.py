"""Per-guild voice, moderation, and in-call music. Commands never cross servers."""
from __future__ import annotations

import asyncio
import re
import time
from datetime import timedelta

import discord

from opus.discord_fun import fun_reply
from opus.discord_lyrics import LYRICS_RE, align_lyric_times, fetch_lyrics_result, lyrics_embed, lyrics_index
from opus.discord_music import CallMixer, ffmpeg_source, wav_to_discord_pcm
from opus.discord_voice import HAS_VOICE_RECV, MIN_SECONDS, pcm_to_mono16
from opus.logutil import get_logger

log = get_logger()

CREATOR_DISCORD_ID = 1406180271363199069

TIMEOUT_FOR_RE = re.compile(
    r"^(?P<name>.+?)\s+for\s+(?P<num>\d+)\s*"
    r"(?P<unit>seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)\s*$",
    re.IGNORECASE,
)
PLAY_MUSIC_RE = re.compile(r"^(play|queue)\s+(.+)$", re.IGNORECASE)
MUSIC_SKIP_RE = re.compile(r"^(skip|next)(\s+(the\s+)?(song|track|music))?$", re.IGNORECASE)
MUSIC_REPLAY_RE = re.compile(
    r"^(replay|restart)(\s+(the\s+)?(song|track|music|this))?$|"
    r"^play\s+(it\s+)?again$|"
    r"^start\s+over$",
    re.IGNORECASE,
)
MUSIC_PAUSE_RE = re.compile(
    r"^(pause|unpause|resume)(\s+(the\s+)?(music|song|track|playing))?$|"
    r"^(pause|resume)\s+playback$",
    re.IGNORECASE,
)
MUSIC_STOP_RE = re.compile(
    r"^stop\s+(the\s+)?(music|song|track|playing)$|"
    r"^stop\s+playing$",
    re.IGNORECASE,
)
MUSIC_REPEAT_RE = re.compile(
    r"^(repeat|loop)(\s+(the\s+)?(song|track|music|this))?$|"
    r"^repeat\s+(on|off)$|"
    r"^(stop\s+)?(repeat|looping)$",
    re.IGNORECASE,
)
MUSIC_NOT_A_TRACK_RE = re.compile(
    r"^(this|the\s+game|chess|showdown|pokemon|pokémon|osu!?|mania|spotify)\b",
    re.IGNORECASE,
)
TTS_TOGGLE_RE = re.compile(
    r"^(tts|voice replies|speak(ing)?)\s*(on|off|toggle)?\s*$",
    re.IGNORECASE,
)


class GuildScopedOps:
    """Mixin: one voice client, one music queue, one mod scope per Discord server."""

    def invite_url(self) -> str:
        return ""

    @staticmethod
    def _normalize_mod_action(action: str) -> str:
        return re.sub(r"\s+", "", (action or "").lower())

    @staticmethod
    def _parse_timeout_spec(target_name: str) -> tuple[str, timedelta]:
        raw = (target_name or "").strip()
        match = TIMEOUT_FOR_RE.match(raw)
        if not match:
            return raw, timedelta(minutes=5)
        num = max(1, int(match.group("num")))
        unit = match.group("unit").lower()
        if unit in {"s", "sec", "secs", "second", "seconds"}:
            delta = timedelta(seconds=num)
        elif unit in {"h", "hr", "hrs", "hour", "hours"}:
            delta = timedelta(hours=num)
        elif unit in {"d", "day", "days"}:
            delta = timedelta(days=num)
        else:
            delta = timedelta(minutes=num)
        cap = timedelta(days=28)
        if delta > cap:
            delta = cap
        return match.group("name").strip(), delta

    def _voice_for_guild(self, guild_id) -> discord.VoiceClient | None:
        if not guild_id:
            return None
        voice = self._voices.get(int(guild_id))
        if voice and voice.is_connected():
            return voice
        return None

    @staticmethod
    def _server_voice_channel(channel):
        if channel is None or getattr(channel, "guild", None) is None:
            return None
        return channel

    def _user_voice_channel(self, user):
        if user is None:
            return None
        state = getattr(user, "voice", None)
        channel = self._server_voice_channel(getattr(state, "channel", None) if state else None)
        if channel is not None:
            return channel
        if not self._client:
            return None
        uid = int(getattr(user, "id", 0) or 0)
        if not uid:
            return None
        for guild in self._client.guilds:
            member = guild.get_member(uid)
            if member and member.voice:
                found = self._server_voice_channel(member.voice.channel)
                if found is not None:
                    return found
            for channel in (
                *list(getattr(guild, "voice_channels", []) or []),
                *list(getattr(guild, "stage_channels", []) or []),
            ):
                for person in getattr(channel, "members", []) or []:
                    if int(getattr(person, "id", 0) or 0) == uid:
                        return channel
        return None

    def _guild_for(self, guild=None, member=None):
        if guild is not None:
            return guild
        channel = self._user_voice_channel(member)
        return getattr(channel, "guild", None) if channel is not None else None

    def _creator_in_voice(self):
        if not self._client:
            return None, None
        for guild in self._client.guilds:
            member = guild.get_member(CREATOR_DISCORD_ID)
            if member and member.voice and member.voice.channel:
                return guild, member
        return None, None

    def _command_voice_channel(self, *, guild=None, member=None):
        if member is not None:
            state = getattr(member, "voice", None)
            channel = self._server_voice_channel(getattr(state, "channel", None) if state else None)
            if channel is not None:
                if guild is None or channel.guild.id == guild.id:
                    return channel
            found = self._user_voice_channel(member)
            if found is not None:
                if guild is None or found.guild.id == guild.id:
                    return found
        if guild is not None:
            voice = self._voice_for_guild(guild.id)
            if voice and voice.channel:
                return voice.channel
        return None

    async def _apply_mod_action(
        self,
        member: discord.Member,
        action: str,
        *,
        duration: timedelta | None = None,
    ) -> None:
        if action == "mute":
            await member.edit(mute=True, reason="Opus moderation")
        elif action == "unmute":
            await member.edit(mute=False, reason="Opus moderation")
        elif action == "deafen":
            await member.edit(deafen=True, reason="Opus moderation")
        elif action == "undeafen":
            await member.edit(deafen=False, reason="Opus moderation")
        elif action == "kick":
            await member.move_to(None, reason="Opus moderation")
        elif action == "timeout":
            await member.timeout(duration or timedelta(minutes=5), reason="Opus moderation")
        elif action == "untimeout":
            await member.timeout(None, reason="Opus moderation")
        else:
            raise ValueError(f"Unknown action: {action}")

    async def _handle_mass_mod(
        self,
        action: str,
        vc_members: list[discord.Member],
        *,
        duration: timedelta | None = None,
    ) -> str:
        targets = [m for m in vc_members if not m.bot]
        if not targets:
            return "No one is in that voice channel right now."

        ok: list[str] = []
        failed: list[str] = []
        for member in targets:
            try:
                await self._apply_mod_action(member, action, duration=duration)
                ok.append(member.display_name)
            except discord.Forbidden:
                failed.append(member.display_name)
            except Exception:
                log.exception("mass mod failed action=%s member=%s", action, member.id)
                failed.append(member.display_name)

        labels = {
            "mute": "Muted",
            "unmute": "Unmuted",
            "deafen": "Deafened",
            "undeafen": "Undeafened",
            "kick": "Disconnected",
            "timeout": "Timed out",
            "untimeout": "Removed timeout from",
        }
        verb = labels.get(action, action)
        if not ok and failed:
            return f"Couldn't {action} anyone — missing permissions."
        if failed:
            return f"{verb} {len(ok)} people. Failed: {', '.join(failed[:6])}."
        if action == "kick":
            return f"Disconnected everyone in this call ({len(ok)})."
        if action == "timeout":
            return f"Timed out everyone in this call ({len(ok)})."
        return f"{verb} everyone in this call ({len(ok)})."

    async def _handle_mod_command(
        self,
        guild: discord.Guild,
        action: str,
        target_name: str,
        *,
        actor=None,
    ) -> str:
        action = self._normalize_mod_action(action)
        duration = None
        if action in {"timeout", "untimeout"}:
            target_name, duration = self._parse_timeout_spec(target_name)
            if action == "untimeout":
                duration = None
        if action not in {"mute", "unmute", "deafen", "undeafen", "kick", "timeout", "untimeout"}:
            return f"Unknown action: {action}"

        scoped = self._command_voice_channel(guild=guild, member=actor)
        vc_members: list[discord.Member] = list(scoped.members) if scoped else []

        if self._is_mass_target(target_name):
            if not scoped:
                return "Join a voice channel in this server first — I won't touch other calls."
            return await self._handle_mass_mod(action, vc_members, duration=duration)

        pool = vc_members
        if action in {"timeout", "untimeout"}:
            pool = list(guild.members)
        member = self._best_member_match(target_name, pool)
        if not member and action not in {"timeout", "untimeout"}:
            member = self._best_member_match(target_name, list(guild.members))
        if not member:
            if scoped:
                names = ", ".join(m.display_name for m in vc_members[:10] if not m.bot)
                return (
                    f"Couldn't find anyone matching '{target_name}'. "
                    f"People in this call: {names or 'none'}"
                )
            return f"Couldn't find anyone matching '{target_name}' in this server."

        if action in {"mute", "unmute", "deafen", "undeafen", "kick"}:
            member_channel = member.voice.channel if member.voice else None
            if member_channel is None or member_channel.guild.id != guild.id:
                return f"{member.display_name} isn't in a voice channel in this server."
            if scoped and member_channel.id != scoped.id:
                return f"{member.display_name} isn't in this call."

        try:
            await self._apply_mod_action(member, action, duration=duration)
            if action == "mute":
                return f"Server muted {member.display_name}."
            if action == "unmute":
                return f"Unmuted {member.display_name}."
            if action == "deafen":
                return f"Server deafened {member.display_name}."
            if action == "undeafen":
                return f"Undeafened {member.display_name}."
            if action == "timeout":
                delta = duration or timedelta(minutes=5)
                if delta.total_seconds() < 60:
                    secs = int(delta.total_seconds())
                    return f"Timed out {member.display_name} for {secs} seconds."
                minutes = max(1, int(delta.total_seconds() // 60))
                return f"Timed out {member.display_name} for {minutes} minute{'s' if minutes != 1 else ''}."
            if action == "untimeout":
                return f"Removed {member.display_name}'s timeout."
            return f"Disconnected {member.display_name} from this call."
        except discord.Forbidden:
            return f"I don't have permission to {action} {member.display_name}."
        except Exception as exc:
            return f"Failed to {action} {member.display_name}: {exc}"

    def run_mod_command(self, action: str, target_name: str) -> str:
        if not self._client or not self._loop:
            return "Discord bot isn't connected."
        guild, member = self._creator_in_voice()
        if not guild:
            return "You're not in a Discord call, so I won't moderate anyone."
        future = asyncio.run_coroutine_threadsafe(
            self._handle_mod_command(guild, action, target_name, actor=member),
            self._loop,
        )
        try:
            return future.result(timeout=10)
        except Exception as exc:
            return f"Mod command failed: {exc}"

    def run_join_call(self) -> str:
        if not self._client or not self._loop:
            return "Discord bot isn't connected."
        guild, member = self._creator_in_voice()
        if not guild:
            extra = f" Add me with this link first: {self.invite_url()}" if self.invite_url() else " Add me to that server first — ask me for an invite link."
            return "I don't see you in a voice channel in a server I'm already in." + extra
        future = asyncio.run_coroutine_threadsafe(self._join_for_member(member, guild), self._loop)
        try:
            return future.result(timeout=20)
        except Exception as exc:
            return f"Couldn't join the call: {exc}"

    def run_leave_call(self) -> str:
        if not self._client or not self._loop:
            return "Discord bot isn't connected."
        guild, _member = self._creator_in_voice()
        future = asyncio.run_coroutine_threadsafe(self._leave_voice(guild), self._loop)
        try:
            return future.result(timeout=10)
        except Exception as exc:
            return f"Couldn't leave the call: {exc}"

    def run_play_music(self, query: str, guild_id: str = "") -> str:
        if not self._client or not self._loop:
            return "Discord bot isn't connected."
        future = asyncio.run_coroutine_threadsafe(self._play_music_for_desk(query, guild_id), self._loop)
        try:
            return future.result(timeout=40)
        except Exception as exc:
            return f"Couldn't play that: {exc}"

    def run_skip_music(self, guild_id: str = "") -> str:
        if not self._client or not self._loop:
            return "Discord bot isn't connected."
        future = asyncio.run_coroutine_threadsafe(self._skip_music_for_desk(guild_id), self._loop)
        try:
            return future.result(timeout=10)
        except Exception as exc:
            return f"Couldn't skip: {exc}"

    def run_stop_music(self, guild_id: str = "") -> str:
        if not self._client or not self._loop:
            return "Discord bot isn't connected."
        future = asyncio.run_coroutine_threadsafe(self._stop_music_for_desk(guild_id), self._loop)
        try:
            return future.result(timeout=10)
        except Exception as exc:
            return f"Couldn't stop the music: {exc}"

    def _desk_guild_and_member(self, guild_id: str = ""):
        if guild_id:
            try:
                gid = int(guild_id)
            except (TypeError, ValueError):
                gid = 0
            guild = self._client.get_guild(gid) if self._client and gid else None
            if guild:
                return guild, guild.get_member(CREATOR_DISCORD_ID)
        return self._creator_in_voice()

    async def _play_music_for_desk(self, query: str, guild_id: str = "") -> str:
        guild, member = self._desk_guild_and_member(guild_id)
        if not guild:
            return "Join a Discord call first — I won't play music in a random server."
        return await self._enqueue_music(guild, query, member)

    async def _skip_music_for_desk(self, guild_id: str = "") -> str:
        guild, _member = self._desk_guild_and_member(guild_id)
        if not guild:
            return "You're not in a Discord call."
        return await self._skip_music(guild)

    async def _stop_music_for_desk(self, guild_id: str = "") -> str:
        guild, _member = self._desk_guild_and_member(guild_id)
        if not guild:
            return "You're not in a Discord call."
        return await self._stop_music(guild)

    def speak_in_call(self, text: str, guild_id: int | None = None) -> None:
        if not text or not self._loop:
            return
        if guild_id is None:
            guild, _member = self._creator_in_voice()
            guild_id = guild.id if guild else None
        if not guild_id:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._speak_in_call(text, guild_id), self._loop)
        except Exception:
            log.exception("queue voice tts failed")

    def _find_join_channel(self, member=None, guild=None):
        if member is not None and getattr(member, "voice", None) and member.voice:
            channel = self._server_voice_channel(member.voice.channel)
            if channel is not None and (guild is None or channel.guild.id == guild.id):
                return channel
        found = self._user_voice_channel(member)
        if found is not None and (guild is None or found.guild.id == guild.id):
            return found
        if guild is not None:
            creator = guild.get_member(CREATOR_DISCORD_ID)
            if creator and creator.voice and creator.voice.channel and creator.voice.channel.guild.id == guild.id:
                return creator.voice.channel
            populated = self._populated_voice_channel(guild)
            if populated:
                return populated
            return None
        _g, creator = self._creator_in_voice()
        if creator and creator.voice and creator.voice.channel:
            return creator.voice.channel
        return None

    def _populated_voice_channel(self, guild):
        if guild is None:
            return None
        channels = [
            *list(getattr(guild, "voice_channels", []) or []),
            *list(getattr(guild, "stage_channels", []) or []),
        ]
        best = None
        best_count = 0
        for channel in channels:
            humans = [m for m in getattr(channel, "members", []) if not getattr(m, "bot", False)]
            if len(humans) > best_count:
                best = channel
                best_count = len(humans)
        return best if best_count else None

    async def _join_for_member(self, member=None, guild=None) -> str:
        channel = self._find_join_channel(member, guild or getattr(member, "guild", None))
        if channel is None:
            return (
                "Join a server voice channel first. Discord doesn't let bots join "
                "private or group-DM calls, but I can hop into your server call from this chat."
            )
        return await self._connect_voice(channel)

    async def _connect_voice(self, channel) -> str:
        from opus.discord_voice import CallSink

        guild = getattr(channel, "guild", None)
        if guild is None:
            return (
                "Discord doesn't let me join private or group-DM voice. "
                "Jump into a server call and say join — I'll meet you there. Commands still work in this chat."
            )
        guild_id = int(guild.id)
        try:
            existing = self._voice_for_guild(guild_id)
            if existing:
                if existing.channel and existing.channel.id == channel.id:
                    return f"I'm already in {channel.name}."
                await existing.move_to(channel)
                self._start_listening(guild_id)
                return f"Moved to {channel.name}."
            recv_cls = None
            if HAS_VOICE_RECV:
                from discord.ext import voice_recv

                recv_cls = voice_recv.VoiceRecvClient
            kwargs = {"self_deaf": False, "self_mute": False}
            if recv_cls:
                kwargs["cls"] = recv_cls
            voice = await channel.connect(**kwargs)
            self._voices[guild_id] = voice
            await asyncio.sleep(0.35)
            if recv_cls and hasattr(voice, "listen"):
                self._sinks[guild_id] = CallSink()
                mixer = self._live_mixer(guild_id, voice)
                self._ensure_mixer_playing(guild_id, voice, mixer)
                if not (hasattr(voice, "is_listening") and voice.is_listening()):
                    self._start_listening(guild_id)
                old = self._watchdogs.get(guild_id)
                if old:
                    old.cancel()
                self._watchdogs[guild_id] = asyncio.create_task(self._voice_watchdog(guild_id))
            extra = "" if self._sinks.get(guild_id) else " I can talk, but I can't hear the call yet."
            self._call_session_until[guild_id] = time.time() + 45
            greeting = "I'm in the call. Talk to me here — I can hear you and I'll answer out loud."
            try:
                await channel.send(greeting)
            except Exception:
                log.exception("vc join chat failed")
            log.info(
                "joined voice channel=%s guild=%s listening=%s recv=%s",
                channel.name,
                guild_id,
                bool(self._sinks.get(guild_id)),
                HAS_VOICE_RECV,
            )
            return f"Joined {channel.name}.{extra}"
        except discord.Forbidden:
            url = self.invite_url() if hasattr(self, "invite_url") else ""
            extra = (
                f" Re-invite me with this link so I get Connect and Speak: {url}"
                if url
                else " Re-invite me with Connect and Speak permissions."
            )
            return f"I don't have permission to join {channel.name}.{extra}"
        except Exception as exc:
            log.exception("voice connect failed")
            return f"Couldn't join {channel.name}: {exc}"

    def _make_packet_handler(self, guild_id: int):
        def handler(user, data) -> None:
            try:
                sink = self._sinks.get(guild_id)
                if not sink:
                    return
                before = sink.packets
                sink.write(user, data)
                if before == 0 and sink.packets:
                    log.info(
                        "call audio started guild=%s user=%s",
                        guild_id,
                        getattr(user, "display_name", user),
                    )
            except Exception:
                log.exception("call packet handler failed guild=%s", guild_id)

        return handler

    def _start_listening(self, guild_id: int) -> None:
        voice = self._voice_for_guild(guild_id)
        sink = self._sinks.get(guild_id)
        if not voice or not hasattr(voice, "listen") or not sink:
            return
        try:
            from discord.ext import voice_recv

            if hasattr(voice, "stop_listening"):
                try:
                    if not hasattr(voice, "is_listening") or voice.is_listening():
                        voice.stop_listening()
                except Exception:
                    pass
            voice.listen(voice_recv.BasicSink(self._make_packet_handler(guild_id), decode=True))
            log.info(
                "voice receive listening guild=%s listening=%s packets=%s raw=%s",
                guild_id,
                getattr(voice, "is_listening", lambda: False)(),
                sink.packets,
                getattr(sink, "raw_packets", 0),
            )
        except Exception:
            log.exception("voice listen failed guild=%s", guild_id)

    def _drop_guild_voice_state(self, guild_id: int) -> None:
        watchdog = self._watchdogs.pop(int(guild_id), None)
        if watchdog:
            watchdog.cancel()
        self._voices.pop(int(guild_id), None)
        self._sinks.pop(int(guild_id), None)
        self._speaking.pop(int(guild_id), None)
        self._call_session_until.pop(int(guild_id), None)
        state = self._music.state(int(guild_id))
        lyrics_ch = state.lyrics_channel_id
        lyrics_msg = state.lyrics_message_id
        if self._loop and lyrics_ch and lyrics_msg:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._delete_lyrics_message(int(lyrics_ch), int(lyrics_msg)),
                    self._loop,
                )
            except Exception:
                pass
        self._music.clear(int(guild_id))

    async def _leave_voice(self, guild=None) -> str:
        if guild is not None:
            return await self._leave_guild_voice(int(guild.id))
        if len(self._voices) == 1:
            only_id = next(iter(self._voices))
            return await self._leave_guild_voice(only_id)
        return "Say that in the server whose call I should leave."

    async def _leave_all_voice(self) -> None:
        for guild_id in list(self._voices):
            await self._leave_guild_voice(guild_id)

    async def _leave_guild_voice(self, guild_id: int) -> str:
        voice = self._voices.get(int(guild_id))
        self._drop_guild_voice_state(int(guild_id))
        if not voice:
            return "I'm not in a call here."
        try:
            if hasattr(voice, "stop_listening"):
                try:
                    voice.stop_listening()
                except Exception:
                    pass
            if voice.is_playing():
                try:
                    voice.stop()
                except Exception:
                    pass
            await voice.disconnect()
        except Exception:
            log.exception("voice disconnect failed guild=%s", guild_id)
        return "Left the call."

    async def _say_in_call(
        self,
        text: str,
        guild_id: int | None,
        *,
        chat: bool = True,
        reply_to=None,
        speak: bool = False,
    ) -> None:
        spoken = (text or "").strip()
        if not spoken or not guild_id:
            return
        voice = self._voice_for_guild(guild_id)
        channel = voice.channel if voice else None
        if chat:
            try:
                if reply_to is not None:
                    await reply_to.reply(spoken[:1800])
                elif channel is not None:
                    await channel.send(spoken[:1800])
            except Exception:
                log.exception("vc chat send failed")
        if speak:
            await self._speak_in_call(spoken, guild_id)

    async def _speak_in_call(self, text: str, guild_id: int | None = None) -> None:
        spoken = (text or "").strip()
        if not spoken or not guild_id:
            return
        if not self._music.state(int(guild_id)).tts_enabled:
            return
        voice = self._voice_for_guild(guild_id)
        if not voice:
            return
        if not self.speaker:
            log.warning("call tts skipped: no speaker")
            return
        if len(spoken) > 280:
            spoken = spoken[:280].rsplit(" ", 1)[0] + "."
        try:
            wav = await asyncio.to_thread(self.speaker.ensure_wav, spoken)
        except Exception:
            log.exception("call tts synth failed")
            return
        if not wav:
            log.warning("call tts produced no wav")
            return
        try:
            duration = 2.0
            try:
                import wave

                with wave.open(str(wav), "rb") as handle:
                    duration = handle.getnframes() / float(handle.getframerate() or 22050)
            except Exception:
                duration = max(1.5, wav.stat().st_size / 44100)
            sink = self._sinks.get(int(guild_id))
            if sink:
                sink.ignore_until = time.time() + duration + 0.6
            mixer = self._live_mixer(int(guild_id), voice)
            loop = asyncio.get_running_loop()
            done = asyncio.Event()

            def on_done() -> None:
                loop.call_soon_threadsafe(done.set)

            pcm = await asyncio.to_thread(wav_to_discord_pcm, wav)
            if not pcm:
                log.warning("call tts produced no pcm file=%s", wav)
                return
            self._speaking[int(guild_id)] = True
            mixer.play_tts_pcm(pcm, on_done=on_done)
            if not voice.is_playing():
                self._ensure_mixer_playing(int(guild_id), voice, mixer)
            log.info(
                "call speaking guild=%s %.1fs bytes=%s live=%s file=%s",
                guild_id,
                duration,
                len(pcm),
                type(getattr(voice, "source", None)).__name__,
                wav.name,
            )
            try:
                await asyncio.wait_for(done.wait(), timeout=duration + 3)
            except asyncio.TimeoutError:
                mixer.stop_tts()
        except Exception:
            log.exception("call playback failed")
        finally:
            self._speaking[int(guild_id)] = False

    def _mixer_for(self, guild_id: int) -> CallMixer:
        voice = self._voice_for_guild(guild_id)
        live = getattr(voice, "source", None) if voice else None
        if isinstance(live, CallMixer) and not getattr(live, "_closed", False):
            self._music.state(int(guild_id)).mixer = live
            return live
        state = self._music.state(int(guild_id))
        if state.mixer is None or getattr(state.mixer, "_closed", False):
            state.mixer = CallMixer(on_music_end=lambda token: self._schedule_music_end(int(guild_id), token))
        return state.mixer

    def _live_mixer(self, guild_id: int, voice) -> CallMixer:
        live = getattr(voice, "source", None) if voice else None
        if isinstance(live, CallMixer) and not getattr(live, "_closed", False):
            self._music.state(int(guild_id)).mixer = live
            return live
        return self._mixer_for(int(guild_id))

    def _ensure_mixer_playing(self, guild_id: int, voice, mixer: CallMixer) -> None:
        if not voice:
            return
        current = getattr(voice, "source", None)
        if voice.is_playing() and current is mixer and not getattr(mixer, "_closed", False):
            return
        if voice.is_playing():
            log.warning(
                "restart mixer play guild=%s playing=%s wanted=%s closed=%s",
                guild_id,
                type(current).__name__,
                type(mixer).__name__,
                getattr(mixer, "_closed", False),
            )
            try:
                voice.stop()
            except Exception:
                pass

        def after(error) -> None:
            if error:
                log.warning("mixer playback error guild=%s: %s", guild_id, error)

        try:
            voice.play(mixer, after=after)
            listening = False
            try:
                listening = bool(voice.is_listening()) if hasattr(voice, "is_listening") else False
            except Exception:
                listening = False
            if not listening:
                self._start_listening(int(guild_id))
        except Exception:
            log.exception("mixer play failed guild=%s", guild_id)

    def _schedule_music_end(self, guild_id: int, token=None) -> None:
        if not self._loop:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._on_music_natural_end(int(guild_id), token), self._loop)
        except Exception:
            log.exception("queue music end failed guild=%s", guild_id)

    async def _on_music_natural_end(self, guild_id: int, token=None) -> None:
        state = self._music.state(int(guild_id))
        ended = state.current
        if ended is None or (token is not None and id(ended) != token):
            return
        mixer = state.mixer
        heard = bool(mixer and mixer.heard_music)
        duration = float(getattr(ended, "duration", 0) or 0)
        pos = mixer.music_position if mixer else 0.0
        finished = bool(heard) and (duration <= 0 or pos >= max(duration - 15.0, duration * 0.9))
        log.info(
            "music ended guild=%s title=%s heard=%s pos=%.1f duration=%.1f finished=%s retries=%s",
            guild_id,
            ended.title,
            heard,
            pos,
            duration,
            finished,
            state.silent_retries,
        )
        if not finished and state.silent_retries < 2:
            state.silent_retries += 1
            log.warning(
                "music stopped early, retrying %s attempt=%s pos=%.1f",
                ended.title,
                state.silent_retries,
                pos,
            )
            await self._replay_track(int(guild_id), ended)
            return
        state.silent_retries = 0
        if state.repeat:
            await self._replay_track(int(guild_id), ended)
            return
        state.current = None
        if state.queue:
            await self._play_next(int(guild_id))
            return
        await self._stop_lyrics(int(guild_id), delete=True)
        await self._refresh_music_ui(int(guild_id))

    async def _replay_track(self, guild_id: int, track) -> None:
        query = track.page_url or track.query or track.title
        try:
            fresh = await self._music.enqueue(query)
        except Exception:
            log.exception("repeat lookup failed guild=%s", guild_id)
            fresh = track
        await self._start_track(int(guild_id), fresh, announce=False)

    async def _replay_music(self, guild) -> str:
        guild = self._guild_for(guild)
        if not guild:
            return "I can only replay music in a call."
        state = self._music.state(int(guild.id))
        track = state.current
        if not track:
            return "Nothing is playing in this call."
        title = track.title
        await self._replay_track(int(guild.id), track)
        return f"Restarted {title}."

    def _music_embed(self, state) -> discord.Embed:
        paused = bool(getattr(state.mixer, "paused", False))
        if state.current:
            color = 0xFFC14D if paused else 0xA855F7
            embed = discord.Embed(color=color)
            embed.title = "Paused" if paused else "Now playing"
            artist = getattr(state.current, "artist", "") or ""
            embed.description = f"**{state.current.title}**"
            if artist:
                embed.set_author(name=artist[:80])
            if state.current.page_url:
                embed.url = state.current.page_url
            mixer = state.mixer
            pos = mixer.music_position if mixer else 0.0
            dur = float(getattr(state.current, "duration", 0) or 0)
            if dur > 0:
                frac = min(1.0, max(0.0, pos / dur))
                filled = int(round(frac * 12))
                bar = "█" * filled + "░" * (12 - filled)
                now = f"{int(pos) // 60}:{int(pos) % 60:02d}"
                total = f"{int(dur) // 60}:{int(dur) % 60:02d}"
                embed.add_field(name="Time", value=f"`{bar}`  {now} / {total}", inline=False)
        else:
            embed = discord.Embed(color=0x64748B)
            embed.title = "Music player"
            embed.description = "Nothing playing. Add a song or pick a Spotify playlist."
        queue_lines = [f"**{idx}.** {track.title}" for idx, track in enumerate(state.queue[:8], start=1)]
        extra = len(state.queue) - 8
        if extra > 0:
            queue_lines.append(f"*…and {extra} more*")
        embed.add_field(name="Queue", value="\n".join(queue_lines) if queue_lines else "*Empty*", inline=False)
        embed.add_field(name="Repeat", value="🔁 On" if state.repeat else "Off", inline=True)
        embed.add_field(name="TTS", value="🔊 On" if state.tts_enabled else "Off", inline=True)
        embed.add_field(name="Paused", value="Yes" if paused and state.current else "No", inline=True)
        return embed

    async def _refresh_music_ui(self, guild_id: int) -> None:
        from opus.discord_music_ui import MusicControlView

        voice = self._voice_for_guild(int(guild_id))
        state = self._music.state(int(guild_id))
        channel = None
        if state.control_channel_id and self._client:
            channel = self._client.get_channel(int(state.control_channel_id))
        if channel is None and voice is not None:
            channel = voice.channel
        if channel is None:
            return
        embed = self._music_embed(state)
        view = MusicControlView(
            self,
            int(guild_id),
            repeating=state.repeat,
            tts_enabled=state.tts_enabled,
            paused=bool(getattr(state.mixer, "paused", False)),
        )
        message = None
        if state.control_message_id:
            try:
                message = await channel.fetch_message(int(state.control_message_id))
            except Exception:
                message = None
                state.control_message_id = None
        try:
            if message is not None:
                await message.edit(embed=embed, view=view)
            else:
                sent = await channel.send(embed=embed, view=view)
                state.control_message_id = sent.id
                state.control_channel_id = int(channel.id)
        except Exception:
            log.exception("music player ui failed guild=%s", guild_id)

    async def _can_use_music_controls(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if self._member_is_admin(member):
            return True
        guild = interaction.guild or self._guild_for(member=member)
        if not guild:
            voice = None
            if interaction.guild_id:
                voice = self._voice_for_guild(interaction.guild_id)
            return bool(voice and voice.channel)
        voice = self._voice_for_guild(guild.id)
        if not voice or not voice.channel:
            return False
        return any(item.id == member.id for item in voice.channel.members)

    async def _enqueue_from_control(self, guild_id: int, query: str, member) -> str:
        if not self._client:
            return "Discord bot isn't connected."
        guild = self._client.get_guild(int(guild_id)) or self._guild_for(member=member)
        if not guild:
            return "I'm not in that server."
        return await self._enqueue_music(guild, query, member)

    async def _toggle_repeat(self, guild) -> str:
        guild = self._guild_for(guild)
        if not guild:
            return "I can only control music in a call."
        state = self._music.state(int(guild.id))
        state.repeat = not state.repeat
        await self._refresh_music_ui(int(guild.id))
        return "Repeat is on." if state.repeat else "Repeat is off."

    async def _toggle_pause(self, guild) -> str:
        guild = self._guild_for(guild)
        if not guild:
            return "I can only pause music in a call."
        voice = self._voice_for_guild(int(guild.id))
        state = self._music.state(int(guild.id))
        if not state.current:
            return "Nothing is playing in this call."
        mixer = self._live_mixer(int(guild.id), voice) if voice else state.mixer
        if mixer is None or not mixer.has_music:
            return "Nothing is playing in this call."
        paused = mixer.toggle_pause()
        if voice:
            self._ensure_mixer_playing(int(guild.id), voice, mixer)
        await self._refresh_music_ui(int(guild.id))
        title = state.current.title if state.current else "the song"
        return f"Paused {title}." if paused else f"Resumed {title}."

    async def _set_paused(self, guild, paused: bool) -> str:
        guild = self._guild_for(guild)
        if not guild:
            return "I can only pause music in a call."
        state = self._music.state(int(guild.id))
        mixer = state.mixer
        if not state.current or mixer is None or not mixer.has_music:
            return "Nothing is playing in this call."
        if mixer.paused == paused:
            title = state.current.title
            return f"{title} is already paused." if paused else f"{title} is already playing."
        mixer.toggle_pause()
        voice = self._voice_for_guild(int(guild.id))
        if voice:
            self._ensure_mixer_playing(int(guild.id), voice, mixer)
        await self._refresh_music_ui(int(guild.id))
        title = state.current.title
        return f"Paused {title}." if paused else f"Resumed {title}."

    async def _toggle_call_tts(self, guild) -> str:
        guild = self._guild_for(guild)
        if not guild:
            return "I can only toggle TTS in a call."
        state = self._music.state(int(guild.id))
        state.tts_enabled = not state.tts_enabled
        await self._refresh_music_ui(int(guild.id))
        if state.tts_enabled:
            return "Voice replies are on."
        return "Voice replies are off. I'll still type in chat."

    async def _spotify_playlists_for_player(self) -> list[dict]:
        from opus.apps import spotify

        if not spotify.connected(self.settings):
            raise PermissionError(
                "Connect Spotify in my panel first, then I can queue your playlists in this call."
            )
        playlists = await asyncio.to_thread(spotify.list_playlists, self.settings)
        liked_total = 0
        try:
            liked_total = await asyncio.to_thread(spotify.liked_track_total, self.settings)
        except Exception:
            liked_total = 0
        return ([{"id": "liked", "name": "Liked Songs", "tracks": liked_total}] + playlists)[:25]

    async def _spotify_songs_for_player(self, playlist_id: str) -> list[dict]:
        from opus.apps import spotify

        return await asyncio.to_thread(
            spotify.playlist_track_details,
            self.settings,
            playlist_id,
            limit=50,
        )

    async def _queue_spotify_queries(self, guild_id: int, queries: list[str], member, label: str) -> str:
        if not self._client:
            return "Discord bot isn't connected."
        guild = self._client.get_guild(int(guild_id))
        if not guild:
            return "I'm not in that server."
        cleaned = [str(query).strip() for query in queries if str(query).strip()]
        if not cleaned:
            return f"{label} didn't have songs I could queue."
        added = 0
        failed = 0
        for query in cleaned:
            try:
                result = await self._enqueue_music(guild, query, member)
                if str(result).startswith("Couldn't"):
                    failed += 1
                else:
                    added += 1
            except Exception:
                failed += 1
        await self._refresh_music_ui(int(guild_id))
        if not added:
            return f"Couldn't queue songs from {label}."
        extra = f" Skipped {failed}." if failed else ""
        if added == 1:
            return f"Queued {cleaned[0]}.{extra}"
        return f"Queued {added} songs from {label}.{extra}"

    async def _queue_spotify_playlist(self, guild_id: int, playlist_id: str, member, label: str) -> str:
        from opus.apps import spotify

        try:
            queries = await asyncio.to_thread(
                spotify.playlist_track_queries,
                self.settings,
                playlist_id,
                limit=20,
            )
        except PermissionError as exc:
            return str(exc)
        except Exception:
            log.exception("spotify playlist tracks failed")
            return "Couldn't read that playlist."
        return await self._queue_spotify_queries(guild_id, queries, member, label)

    async def _delete_lyrics_message(self, channel_id: int, message_id: int) -> None:
        if not self._client:
            return
        channel = self._client.get_channel(int(channel_id))
        if channel is None:
            return
        try:
            message = await channel.fetch_message(int(message_id))
            await message.delete()
        except Exception:
            pass

    async def _stop_lyrics(self, guild_id: int, *, delete: bool = True) -> None:
        state = self._music.state(int(guild_id))
        state.lyrics_gen = int(getattr(state, "lyrics_gen", 0) or 0) + 1
        task = state.lyrics_task
        state.lyrics_task = None
        state.lyrics_lines = []
        state.lyrics_plain = ""
        state.lyrics_duration = 0.0
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        channel_id = state.lyrics_channel_id
        message_id = state.lyrics_message_id
        state.lyrics_channel_id = None
        state.lyrics_message_id = None
        if delete and channel_id and message_id:
            await self._delete_lyrics_message(int(channel_id), int(message_id))

    async def _start_lyrics(self, guild_id: int, track) -> None:
        packed = await asyncio.to_thread(
            fetch_lyrics_result,
            track.title,
            getattr(track, "artist", "") or "",
            float(getattr(track, "duration", 0) or 0),
        )
        await self._stop_lyrics(int(guild_id), delete=True)
        state = self._music.state(int(guild_id))
        if state.current is None or id(state.current) != id(track):
            return
        track_dur = float(getattr(track, "duration", 0) or 0)
        state.lyrics_lines = align_lyric_times(
            packed.lines,
            float(packed.duration or 0),
            track_dur,
        )
        state.lyrics_plain = packed.plain
        state.lyrics_duration = track_dur or float(packed.duration or 0)
        state.lyrics_gen = int(getattr(state, "lyrics_gen", 0) or 0) + 1
        gen = state.lyrics_gen
        channel = None
        if state.control_channel_id and self._client:
            channel = self._client.get_channel(int(state.control_channel_id))
        voice = self._voice_for_guild(int(guild_id))
        if channel is None and voice is not None:
            channel = voice.channel
        if channel is None:
            return
        mixer = state.mixer
        position = mixer.music_position if mixer else 0.0
        try:
            sent = await channel.send(embed=self._lyrics_embed_for(state, position))
        except Exception:
            log.exception("lyrics send failed guild=%s", guild_id)
            return
        if state.lyrics_gen != gen or state.current is None or id(state.current) != id(track):
            try:
                await sent.delete()
            except Exception:
                pass
            return
        state.lyrics_message_id = sent.id
        state.lyrics_channel_id = int(channel.id)
        if packed.lines or packed.plain:
            state.lyrics_task = asyncio.create_task(self._lyrics_loop(int(guild_id), id(track), gen))

    def _lyrics_embed_for(self, state, position: float):
        track = state.current
        return lyrics_embed(
            track.title if track else "Lyrics",
            state.lyrics_lines,
            position,
            plain=getattr(state, "lyrics_plain", "") or "",
            duration=float(
                getattr(track, "duration", 0)
                or getattr(state, "lyrics_duration", 0)
                or 0
            ),
            artist=getattr(track, "artist", "") or "",
            paused=bool(getattr(state.mixer, "paused", False)),
        )

    async def _lyrics_loop(self, guild_id: int, token, gen: int) -> None:
        last_idx = -8
        last_stamp = 0.0
        try:
            while True:
                state = self._music.state(int(guild_id))
                if getattr(state, "lyrics_gen", 0) != gen:
                    return
                if state.current is None or id(state.current) != token:
                    return
                if not state.lyrics_lines and not getattr(state, "lyrics_plain", ""):
                    return
                mixer = state.mixer
                position = mixer.music_position if mixer else 0.0
                idx = lyrics_index(
                    state.lyrics_lines,
                    position,
                    plain=getattr(state, "lyrics_plain", "") or "",
                    duration=float(
                        getattr(state.current, "duration", 0)
                        or getattr(state, "lyrics_duration", 0)
                        or 0
                    ),
                )
                now = time.time()
                if idx != last_idx or now - last_stamp >= 5.0:
                    await self._edit_lyrics(state, position, gen=gen)
                    last_idx = idx
                    last_stamp = now
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("lyrics loop failed guild=%s", guild_id)

    async def _edit_lyrics(self, state, position: float, *, gen: int) -> None:
        if getattr(state, "lyrics_gen", 0) != gen:
            return
        if not self._client or not state.lyrics_channel_id or not state.lyrics_message_id:
            return
        channel = self._client.get_channel(int(state.lyrics_channel_id))
        if channel is None:
            return
        try:
            message = channel.get_partial_message(int(state.lyrics_message_id))
            await message.edit(embed=self._lyrics_embed_for(state, position))
        except discord.HTTPException as exc:
            retry = float(getattr(exc, "retry_after", 0) or 0)
            if exc.status == 429 or retry:
                await asyncio.sleep(min(8.0, retry or 2.0))
            else:
                log.exception("lyrics edit failed")
        except Exception:
            log.exception("lyrics edit failed")

    async def _show_lyrics(self, guild) -> str:
        guild = self._guild_for(guild)
        if not guild:
            return "I can only show lyrics in a call."
        state = self._music.state(int(guild.id))
        if not state.current:
            return "Nothing is playing in this call."
        await self._start_lyrics(int(guild.id), state.current)
        state = self._music.state(int(guild.id))
        if state.lyrics_lines:
            return f"Lyrics are up for {state.current.title}."
        if getattr(state, "lyrics_plain", ""):
            return f"I found unsynced lyrics for {state.current.title}."
        return f"Couldn't find lyrics for {state.current.title}."

    async def _enqueue_music(self, guild, query: str, member=None) -> str:
        channel = self._command_voice_channel(guild=guild, member=member)
        if channel is None:
            return (
                "Join a server voice channel first. Discord doesn't let me play into "
                "private or group-DM calls."
            )
        guild = getattr(channel, "guild", None) or guild
        if not guild:
            return (
                "Discord doesn't let me join private or group-DM voice. "
                "Hop into a server call and I'll play there."
            )
        voice = self._voice_for_guild(guild.id)
        if not voice or not voice.channel or voice.channel.id != channel.id:
            joined = await self._connect_voice(channel)
            voice = self._voice_for_guild(guild.id)
            if not voice or not voice.channel or voice.channel.id != channel.id:
                return joined
        try:
            track = await self._music.enqueue(query)
        except Exception as exc:
            log.exception("music lookup failed")
            return f"Couldn't find that track: {exc}"
        state = self._music.state(guild.id)
        if state.current:
            state.queue.append(track)
            await self._refresh_music_ui(int(guild.id))
            return f"Queued {track.title}."
        return await self._start_track(int(guild.id), track)

    async def _start_track(self, guild_id: int, track, *, announce: bool = True) -> str:
        voice = self._voice_for_guild(guild_id)
        if not voice:
            return "I'm not in a call here."
        state = self._music.state(guild_id)
        state.current = track
        mixer = self._live_mixer(int(guild_id), voice)
        mixer.set_music(ffmpeg_source(track.stream_url), token=id(track))
        self._ensure_mixer_playing(int(guild_id), voice, mixer)
        log.info("music start guild=%s title=%s playing=%s", guild_id, track.title, voice.is_playing())
        await self._refresh_music_ui(int(guild_id))
        asyncio.create_task(self._start_lyrics(int(guild_id), track))
        return f"Playing {track.title}." if announce else ""

    async def _play_next(self, guild_id: int) -> None:
        state = self._music.state(guild_id)
        if not state.queue:
            state.current = None
            await self._stop_lyrics(int(guild_id), delete=True)
            await self._refresh_music_ui(int(guild_id))
            return
        track = state.queue.pop(0)
        await self._replay_track(int(guild_id), track)

    async def _skip_music(self, guild) -> str:
        guild = self._guild_for(guild)
        if not guild:
            return "I can only skip music in a call."
        state = self._music.state(guild.id)
        mixer = state.mixer
        if not state.current and not state.queue:
            return "Nothing is playing in this call."
        skipped = state.current.title if state.current else "that track"
        state.silent_retries = 0
        await self._stop_lyrics(int(guild.id), delete=True)
        state.current = None
        if mixer:
            mixer.set_music(None)
        if state.queue:
            await self._play_next(int(guild.id))
        else:
            await self._refresh_music_ui(int(guild.id))
        return f"Skipped {skipped}."

    async def _stop_music(self, guild) -> str:
        guild = self._guild_for(guild)
        if not guild:
            return "I can only stop music in a call."
        state = self._music.state(int(guild.id))
        mixer = state.mixer
        state.silent_retries = 0
        await self._stop_lyrics(int(guild.id), delete=True)
        state.queue.clear()
        state.current = None
        if mixer:
            mixer.set_music(None)
        await self._refresh_music_ui(int(guild.id))
        return "Stopped the music in this call."

    async def _handle_fun_or_music(self, prompt: str, guild, member) -> str | None:
        from opus.intents import strip_wake

        guild = self._guild_for(guild, member)
        cleaned = strip_wake(prompt or "")
        fun = fun_reply(cleaned) or fun_reply(prompt or "")
        if fun:
            return fun
        text = cleaned.strip()
        if MUSIC_SKIP_RE.match(text):
            return await self._skip_music(guild)
        if MUSIC_REPLAY_RE.match(text):
            return await self._replay_music(guild)
        if MUSIC_PAUSE_RE.match(text):
            wanted = text.lower()
            if wanted.startswith("resume") or wanted.startswith("unpause"):
                return await self._set_paused(guild, False)
            if wanted.startswith("pause"):
                return await self._set_paused(guild, True)
            return await self._toggle_pause(guild)
        if MUSIC_STOP_RE.match(text):
            return await self._stop_music(guild)
        if MUSIC_REPEAT_RE.match(text):
            return await self._toggle_repeat(guild)
        if LYRICS_RE.match(text):
            return await self._show_lyrics(guild)
        tts = TTS_TOGGLE_RE.match(text)
        if tts:
            wanted = (tts.group(3) or "toggle").lower()
            state = self._music.state(int(guild.id)) if guild else None
            if wanted == "on":
                if state and not state.tts_enabled:
                    return await self._toggle_call_tts(guild)
                return "Voice replies are already on." if guild else "Join a call first."
            if wanted == "off":
                if state and state.tts_enabled:
                    return await self._toggle_call_tts(guild)
                return "Voice replies are already off." if guild else "Join a call first."
            return await self._toggle_call_tts(guild)
        play = PLAY_MUSIC_RE.match(text)
        if play:
            query = (play.group(2) or "").strip(" .,!")
            if query and not MUSIC_NOT_A_TRACK_RE.search(query):
                return await self._enqueue_music(guild, query, member)
        return None

    async def _voice_watchdog(self, guild_id: int) -> None:
        last_log = 0.0
        while True:
            voice = self._voices.get(guild_id)
            sink = self._sinks.get(guild_id)
            if not voice or not voice.is_connected() or not sink:
                break
            await asyncio.sleep(0.2)
            now = time.time()
            listening = True
            try:
                listening = bool(voice.is_listening()) if hasattr(voice, "is_listening") else True
            except Exception:
                listening = False
            if now - last_log > 8:
                last_log = now
                log.info(
                    "call listen guild=%s packets=%s raw=%s bytes=%s speaking=%s listening=%s playing=%s",
                    guild_id,
                    sink.packets,
                    getattr(sink, "raw_packets", sink.packets),
                    sink.bytes_seen,
                    self._speaking.get(guild_id),
                    listening,
                    voice.is_playing(),
                )
            if self._speaking.get(guild_id):
                continue
            for uid in sink.ready_users():
                await self._flush_call_user(uid, guild_id)

    async def _flush_call_user(self, uid: int, guild_id: int) -> None:
        sink = self._sinks.get(guild_id)
        if not sink or not self.transcribe:
            return
        user, pcm = sink.take(uid)
        audio = pcm_to_mono16(pcm)
        if audio.size < int(16000 * MIN_SECONDS):
            return
        if user is None:
            user = self._guess_speaker(guild_id)
        try:
            text = await asyncio.to_thread(self.transcribe, audio)
        except Exception:
            log.exception("call stt failed")
            return
        text = (text or "").strip()
        if not text:
            log.info("call stt empty from %s", getattr(user, "display_name", uid))
            return
        log.info("call heard guild=%s %s: %s", guild_id, getattr(user, "display_name", uid), text)
        await self._handle_call_transcript(user, text, guild_id)

    def _guess_speaker(self, guild_id: int | None = None):
        voice = self._voice_for_guild(guild_id) if guild_id else None
        channel = voice.channel if voice else None
        if not channel:
            return None
        humans = [m for m in channel.members if not m.bot]
        if len(humans) == 1:
            return humans[0]
        return None

    async def _handle_call_transcript(self, user, text: str, guild_id: int) -> None:
        from opus.intents import contains_wake, is_wake_only, match_intents

        now = time.time()
        uid = int(getattr(user, "id", 0) or 0)
        followup = now < self._call_followup.get(uid, 0) or now < self._call_session_until.get(guild_id, 0)
        directed = self._is_talking_to_opus(text)
        if is_wake_only(text):
            self._call_followup[uid] = now + 25
            self._call_session_until[guild_id] = now + 25
            await self._say_in_call("Yes?", guild_id)
            return
        if not contains_wake(text) and not followup and not directed:
            return
        log.info("call handling %s followup=%s directed=%s", getattr(user, "display_name", uid), followup, directed)
        guild = getattr(user, "guild", None)
        if guild is None and self._client:
            guild = self._client.get_guild(int(guild_id))
        extra = await self._handle_fun_or_music(text, guild, user)
        if extra:
            self._call_followup[uid] = now + 25
            self._call_session_until[guild_id] = now + 25
            await self._say_in_call(extra, guild_id)
            return
        compound = match_intents(text if contains_wake(text) else f"opus {text}")
        mod_intents = [item for item in compound if item.name == "discord_mod"]
        if mod_intents:
            if not self._member_is_admin(user):
                await self._say_in_call("Only administrators can use those commands.", guild_id)
                return
            if not guild:
                await self._say_in_call("I'm not in a server.", guild_id)
                return
            replies: list[str] = []
            for item in mod_intents:
                action, _, target = item.rest.partition("|")
                replies.append(await self._handle_mod_command(guild, action, target, actor=user))
            self._call_followup[uid] = now + 25
            self._call_session_until[guild_id] = now + 25
            await self._say_in_call(" ".join(replies), guild_id)
            return
        join_or_leave = [
            item
            for item in compound
            if item.name in {"discord_join_call", "discord_bot_leave", "discord_leave_call"}
        ]
        if any(item.name == "discord_invite" for item in compound):
            reply = self.invite_reply() if hasattr(self, "invite_reply") else "Ask in chat for my invite link."
            await self._say_in_call(reply, guild_id)
            return
        if join_or_leave:
            last = join_or_leave[-1]
            if last.name in {"discord_bot_leave", "discord_leave_call"}:
                await self._leave_voice(guild)
                return
            result = await self._join_for_member(user, guild)
            await self._say_in_call(result, guild_id)
            return
        if not self.on_call_speech:
            return
        self._call_followup[uid] = now + 25
        self._call_session_until[guild_id] = now + 25
        author_names = [
            str(getattr(user, "name", "") or ""),
            str(getattr(user, "global_name", "") or ""),
            str(getattr(user, "display_name", "") or ""),
            str(getattr(user, "id", "") or ""),
        ]
        roster = self._get_member_roster(guild) if guild else ""
        try:
            answer = await asyncio.to_thread(
                self.on_call_speech,
                text,
                True,
                str(getattr(user, "display_name", "") or ""),
                author_names,
                roster,
                str(guild.id) if guild else str(guild_id),
            )
        except Exception:
            log.exception("call speech handler failed")
            answer = "I hit an error answering that."
        if answer:
            await self._say_in_call(
                str(answer),
                guild_id,
                speak=True,
            )
