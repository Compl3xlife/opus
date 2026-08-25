"""Per-guild voice, moderation, and in-call music. Commands never cross servers."""
from __future__ import annotations

import asyncio
import re
import time
from datetime import timedelta

import discord

from opus.discord_fun import fun_reply
from opus.discord_music import ffmpeg_source
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
MUSIC_STOP_RE = re.compile(
    r"^(stop|pause)\s+(the\s+)?(music|song|track|playing)$|"
    r"^stop\s+playing$",
    re.IGNORECASE,
)
MUSIC_NOT_A_TRACK_RE = re.compile(
    r"^(this|the\s+game|chess|showdown|pokemon|pokémon|osu!?|mania|spotify)\b",
    re.IGNORECASE,
)


class GuildScopedOps:
    """Mixin: one voice client, one music queue, one mod scope per Discord server."""

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
            channel = getattr(state, "channel", None) if state else None
            if channel is not None:
                if guild is None or channel.guild.id == guild.id:
                    return channel
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
        if member is not None and getattr(member, "voice", None) and member.voice and member.voice.channel:
            channel = member.voice.channel
            if guild is None or channel.guild.id == guild.id:
                return channel
        if guild is not None:
            creator = guild.get_member(CREATOR_DISCORD_ID)
            if creator and creator.voice and creator.voice.channel and creator.voice.channel.guild.id == guild.id:
                return creator.voice.channel
            return None
        _g, creator = self._creator_in_voice()
        if creator and creator.voice and creator.voice.channel:
            return creator.voice.channel
        return None

    async def _join_for_member(self, member=None, guild=None) -> str:
        channel = self._find_join_channel(member, guild or (member.guild if member else None))
        if channel is None:
            return "Join a voice channel in this server first."
        return await self._connect_voice(channel)

    async def _connect_voice(self, channel) -> str:
        from opus.discord_voice import CallSink

        guild_id = int(channel.guild.id)
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
            await self._speak_in_call(greeting, guild_id)
            log.info(
                "joined voice channel=%s guild=%s listening=%s recv=%s",
                channel.name,
                guild_id,
                bool(self._sinks.get(guild_id)),
                HAS_VOICE_RECV,
            )
            return f"Joined {channel.name}.{extra}"
        except Exception as exc:
            log.exception("voice connect failed")
            return f"Couldn't join {channel.name}: {exc}"

    def _make_packet_handler(self, guild_id: int):
        def handler(user, data) -> None:
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

        return handler

    def _start_listening(self, guild_id: int) -> None:
        voice = self._voice_for_guild(guild_id)
        sink = self._sinks.get(guild_id)
        if not voice or not hasattr(voice, "listen") or not sink:
            return
        try:
            from discord.ext import voice_recv

            if hasattr(voice, "is_listening") and voice.is_listening():
                if hasattr(voice, "stop_listening"):
                    voice.stop_listening()
            elif hasattr(voice, "stop_listening"):
                try:
                    voice.stop_listening()
                except Exception:
                    pass
            voice.listen(voice_recv.BasicSink(self._make_packet_handler(guild_id), decode=True))
            log.info(
                "voice receive listening guild=%s listening=%s",
                guild_id,
                getattr(voice, "is_listening", lambda: False)(),
            )
        except Exception:
            log.exception("voice listen failed guild=%s", guild_id)
            self._sinks.pop(guild_id, None)

    def _drop_guild_voice_state(self, guild_id: int) -> None:
        watchdog = self._watchdogs.pop(int(guild_id), None)
        if watchdog:
            watchdog.cancel()
        self._voices.pop(int(guild_id), None)
        self._sinks.pop(int(guild_id), None)
        self._speaking.pop(int(guild_id), None)
        self._call_session_until.pop(int(guild_id), None)
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

    async def _say_in_call(self, text: str, guild_id: int | None, *, chat: bool = True, reply_to=None) -> None:
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
        await self._speak_in_call(spoken, guild_id)

    async def _speak_in_call(self, text: str, guild_id: int | None = None) -> None:
        spoken = (text or "").strip()
        if not spoken or not guild_id:
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
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
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
            music = self._music.state(int(guild_id))
            if voice.is_playing() and music.current:
                music.paused_for_tts = True
            self._speaking[int(guild_id)] = True
            if voice.is_playing():
                voice.stop()
            source = discord.FFmpegPCMAudio(
                str(wav),
                executable=ffmpeg,
                before_options="-nostdin",
            )
            voice.play(source)
            log.info("call speaking guild=%s %.1fs file=%s", guild_id, duration, wav.name)
            waited = 0.0
            while voice.is_playing() and waited < duration + 2:
                await asyncio.sleep(0.12)
                waited += 0.12
        except Exception:
            log.exception("call playback failed")
        finally:
            self._speaking[int(guild_id)] = False
            await self._resume_music_if_needed(int(guild_id))

    async def _resume_music_if_needed(self, guild_id: int) -> None:
        music = self._music.state(guild_id)
        if not music.paused_for_tts or not music.current:
            return
        music.paused_for_tts = False
        voice = self._voice_for_guild(guild_id)
        if not voice or voice.is_playing():
            return
        try:
            await self._start_track(guild_id, music.current, announce=False)
        except Exception:
            log.exception("resume music failed guild=%s", guild_id)

    async def _enqueue_music(self, guild, query: str, member=None) -> str:
        if not guild:
            return "I can only play music in a server."
        channel = self._command_voice_channel(guild=guild, member=member)
        if channel is None:
            return "Join a voice channel in this server first."
        voice = self._voice_for_guild(guild.id)
        if not voice or not voice.channel or voice.channel.id != channel.id:
            joined = await self._connect_voice(channel)
            if joined.startswith("Couldn't"):
                return joined
        try:
            track = await self._music.enqueue(query)
        except Exception as exc:
            log.exception("music lookup failed")
            return f"Couldn't find that track: {exc}"
        state = self._music.state(guild.id)
        voice = self._voice_for_guild(guild.id)
        if voice and (voice.is_playing() or state.current) and not self._speaking.get(int(guild.id)):
            state.queue.append(track)
            return f"Queued {track.title}."
        if state.current:
            state.queue.append(track)
            return f"Queued {track.title}."
        return await self._start_track(int(guild.id), track)

    async def _start_track(self, guild_id: int, track, *, announce: bool = True) -> str:
        voice = self._voice_for_guild(guild_id)
        if not voice:
            return "I'm not in a call here."
        state = self._music.state(guild_id)
        state.current = track
        state.paused_for_tts = False
        source = ffmpeg_source(track.stream_url)

        def after(error):
            if error:
                log.warning("music playback error guild=%s: %s", guild_id, error)
            if not self._loop:
                return

            async def nxt():
                if self._speaking.get(guild_id):
                    return
                current = self._music.state(guild_id)
                if current.paused_for_tts:
                    return
                if current.current is not track:
                    return
                current.current = None
                await self._play_next(guild_id)

            try:
                asyncio.run_coroutine_threadsafe(nxt(), self._loop)
            except Exception:
                log.exception("music after-play failed guild=%s", guild_id)

        if voice.is_playing():
            voice.stop()
        voice.play(source, after=after)
        return f"Playing {track.title}." if announce else ""

    async def _play_next(self, guild_id: int) -> None:
        state = self._music.state(guild_id)
        if not state.queue:
            state.current = None
            return
        track = state.queue.pop(0)
        await self._start_track(guild_id, track)

    async def _skip_music(self, guild) -> str:
        if not guild:
            return "I can only skip music in a server."
        state = self._music.state(guild.id)
        voice = self._voice_for_guild(guild.id)
        if not state.current and not state.queue:
            return "Nothing is playing in this call."
        skipped = state.current.title if state.current else "that track"
        if voice and voice.is_playing() and not self._speaking.get(int(guild.id)):
            voice.stop()
        else:
            state.current = None
            if state.queue:
                await self._play_next(int(guild.id))
        return f"Skipped {skipped}."

    async def _stop_music(self, guild) -> str:
        if not guild:
            return "I can only stop music in a server."
        self._music.clear(int(guild.id))
        voice = self._voice_for_guild(guild.id)
        if voice and voice.is_playing() and not self._speaking.get(int(guild.id)):
            voice.stop()
        return "Stopped the music in this call."

    async def _handle_fun_or_music(self, prompt: str, guild, member) -> str | None:
        from opus.intents import strip_wake

        cleaned = strip_wake(prompt or "")
        fun = fun_reply(cleaned) or fun_reply(prompt or "")
        if fun:
            return fun
        text = cleaned.strip()
        if MUSIC_SKIP_RE.match(text):
            return await self._skip_music(guild)
        if MUSIC_STOP_RE.match(text):
            return await self._stop_music(guild)
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
            if now - last_log > 8:
                last_log = now
                log.info(
                    "call listen guild=%s packets=%s bytes=%s speaking=%s",
                    guild_id,
                    sink.packets,
                    sink.bytes_seen,
                    self._speaking.get(guild_id),
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
            await self._say_in_call(" ".join(replies), guild_id)
            return
        join_or_leave = [
            item
            for item in compound
            if item.name in {"discord_join_call", "discord_bot_leave", "discord_leave_call"}
        ]
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
            await self._say_in_call(str(answer), guild_id)
