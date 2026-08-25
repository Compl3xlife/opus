from __future__ import annotations

import asyncio
import re
import threading
from difflib import SequenceMatcher

import discord

from opus.discord_guild import CREATOR_DISCORD_ID, GuildScopedOps
from opus.discord_music import MusicManager
from opus.discord_store import DiscordStore, _collect_image_urls
from opus.discord_voice import CallSink
from opus.logutil import get_logger

log = get_logger()

JOIN_CALL_RE = re.compile(
    r"^(join|come)(\s+(the\s+)?(call|vc|voice|channel))?$",
    re.IGNORECASE,
)
LEAVE_CALL_RE = re.compile(
    r"^(leave|disconnect)(\s+(the\s+)?(call|vc|voice|channel))?$",
    re.IGNORECASE,
)
CALL_CHAT_NOISE_RE = re.compile(
    r"^(lol+|lmao+|ok|okay|k|yeah|yea|yep|nah|true|fr|nice|omg|lmk)+\s*[.!?]*$",
    re.IGNORECASE,
)
CALL_QUESTION_RE = re.compile(
    r"^\s*(how|how's|hows|what|what's|whats|who|who's|whos|why|when|where|which|"
    r"can|could|would|will|are|is|do|did|does|"
    r"tell|explain|look up|search|google)\b",
    re.IGNORECASE,
)
CALL_GREETING_RE = re.compile(
    r"\b("
    r"how are you|how're you|how r u|how's it going|hows it going|"
    r"what's up|whats up|you good|you there|you here|"
    r"hello|hey opus|hi opus"
    r")\b",
    re.IGNORECASE,
)

# Moderation command patterns
MOD_RE = re.compile(
    r"\b(mute|unmute|deafen|undeafen|kick|timeout|time\s*out|untimeout)\s*alls?\b|"
    r"\b(mute|unmute|deafen|undeafen|kick|timeout|time\s*out|untimeout)\s+(.+)",
    re.IGNORECASE,
)
ORIGIN_QUESTION_RE = re.compile(
    r"\b("
    r"how were you (?:made|created|built|coded)|"
    r"who (?:made|created|built|coded|programmed|developed)\s+(?:you|u|opus|this bot)|"
    r"who(?:'s| is| are) your (?:creator|maker|developer)|"
    r"who owns you"
    r")\b",
    re.IGNORECASE,
)
NOT_ORIGIN_RE = re.compile(
    r"\b(gif|gifs|art|drawing|drew|sketch|paint(?:ing)?|animation|happy|fun|should)\b",
    re.IGNORECASE,
)
SCAN_COMMAND_RE = re.compile(
    r"^scan(?:\s+(server|messages|channel|status|stop))?\s*$",
    re.IGNORECASE,
)


class DiscordBot(GuildScopedOps):
    def __init__(self, settings, on_prompt, speaker=None, on_call_speech=None, transcribe=None) -> None:
        self.settings = settings
        self.on_prompt = on_prompt
        self.speaker = speaker
        self.on_call_speech = on_call_speech
        self.transcribe = transcribe
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: discord.Client | None = None
        self._member_rosters: dict[int, str] = {}
        self.store = DiscordStore()
        self._scan_tasks: dict[int, asyncio.Task] = {}
        self._scan_cancel: set[int] = set()
        self._voices: dict[int, discord.VoiceClient] = {}
        self._sinks: dict[int, CallSink] = {}
        self._watchdogs: dict[int, asyncio.Task] = {}
        self._speaking: dict[int, bool] = {}
        self._call_session_until: dict[int, float] = {}
        self._music = MusicManager()
        self._call_followup: dict[int, float] = {}

    @staticmethod
    def _member_names(member: discord.Member) -> dict[str, str]:
        return {
            "id": str(member.id),
            "username": member.name or "",
            "global_name": member.global_name or "",
            "nickname": member.nick or "",
            "display_name": member.display_name or "",
        }

    @classmethod
    def _format_member_roster(cls, members: list[discord.Member]) -> str:
        lines: list[str] = []
        for member in sorted(members, key=lambda item: (item.display_name or item.name).lower()):
            if member.bot:
                continue
            names = cls._member_names(member)
            parts = [
                f"display={names['display_name']}",
                f"username={names['username']}",
            ]
            if names["global_name"]:
                parts.append(f"global={names['global_name']}")
            if names["nickname"]:
                parts.append(f"nickname={names['nickname']}")
            parts.append(f"id={names['id']}")
            lines.append("- " + ", ".join(parts))
        return "\n".join(lines)

    def _refresh_guild_roster(self, guild: discord.Guild) -> str:
        roster = self._format_member_roster(list(guild.members))
        self._member_rosters[guild.id] = roster
        return roster

    def _get_member_roster(self, guild: discord.Guild | None) -> str:
        if not guild:
            return ""
        cached = self._member_rosters.get(guild.id)
        if cached:
            return cached
        return self._refresh_guild_roster(guild)

    @staticmethod
    def _as_set(raw: str) -> set[str]:
        return {item.strip() for item in str(raw or "").split(",") if item.strip()}

    @staticmethod
    def _creator_mention(message: discord.Message) -> str:
        if message.guild:
            for member in message.guild.members:
                names = {
                    member.name.lower(),
                    (member.global_name or "").lower(),
                    (member.display_name or "").lower(),
                }
                if "._repeat_." in names or "involutional" in names:
                    return member.mention
        return "@._repeat_."

    @staticmethod
    def _mention_by_name(message: discord.Message, target_name: str, fallback: str) -> str:
        if message.guild:
            wanted = (target_name or "").strip().lower()
            for member in message.guild.members:
                names = {
                    member.name.lower(),
                    (member.global_name or "").lower(),
                    (member.display_name or "").lower(),
                }
                if wanted in names:
                    return member.mention
        return fallback

    def _should_respond(self, message: discord.Message) -> tuple[bool, str]:
        if message.author.bot:
            return False, ""
        guild_allow = self._as_set(self.settings.get("discord_allowed_guilds"))
        if guild_allow and (not message.guild or str(message.guild.id) not in guild_allow):
            return False, ""
        channel_allow = self._as_set(self.settings.get("discord_allowed_channels"))
        if channel_allow and str(message.channel.id) not in channel_allow:
            return False, ""
        text = (message.content or "").strip()
        if not text:
            return False, ""
        prefix = (self.settings.get("discord_prefix") or "opus").strip().lower()
        bot_id = self._client.user.id if self._client and self._client.user else None
        in_my_vc = self._is_connected_channel(message.channel)
        if bot_id:
            mention_at_start = re.match(rf"^<@!?{bot_id}>\s*", text)
            if mention_at_start:
                return True, text[mention_at_start.end() :].strip(" ,:-")
            if re.search(rf"<@!?{bot_id}>", text):
                stripped = re.sub(rf"<@!?{bot_id}>", " ", text).strip(" ,:-")
                return True, stripped
        lowered = text.lower()
        if lowered.startswith(prefix + " "):
            return True, text[len(prefix) :].strip(" ,:-")
        if lowered == prefix:
            return True, ""
        if in_my_vc and self._is_talking_to_opus(text):
            return True, text
        return False, ""

    def _should_index(self, message: discord.Message) -> bool:
        if message.author.bot or not message.guild:
            return False
        guild_allow = self._as_set(self.settings.get("discord_allowed_guilds"))
        if guild_allow and str(message.guild.id) not in guild_allow:
            return False
        channel_allow = self._as_set(self.settings.get("discord_allowed_channels"))
        if channel_allow and str(message.channel.id) not in channel_allow:
            return False
        return True

    @staticmethod
    def _member_is_admin(member) -> bool:
        if member is None:
            return False
        guild = getattr(member, "guild", None)
        if getattr(member, "guild_permissions", None) and member.guild_permissions.administrator:
            return True
        if guild and getattr(guild, "owner_id", None) and member.id == guild.owner_id:
            return True
        if int(getattr(member, "id", 0) or 0) == CREATOR_DISCORD_ID:
            return True
        return False

    @staticmethod
    def _is_admin(message: discord.Message) -> bool:
        if not message.guild:
            return False
        return DiscordBot._member_is_admin(message.author)

    def _is_connected_channel(self, channel) -> bool:
        if not channel:
            return False
        channel_id = int(getattr(channel, "id", 0) or 0)
        guild = getattr(channel, "guild", None)
        if guild:
            voice = self._voice_for_guild(guild.id)
            return bool(
                voice
                and voice.is_connected()
                and voice.channel
                and int(voice.channel.id) == channel_id
            )
        for voice in self._voices.values():
            if voice and voice.is_connected() and voice.channel and int(voice.channel.id) == channel_id:
                return True
        return False

    def _is_talking_to_opus(self, text: str) -> bool:
        cleaned = (text or "").strip()
        if not cleaned or CALL_CHAT_NOISE_RE.match(cleaned):
            return False
        from opus.intents import contains_wake, match_intents, strip_wake
        from opus.discord_fun import fun_reply
        from opus.discord_guild import MUSIC_SKIP_RE, MUSIC_STOP_RE, PLAY_MUSIC_RE

        if contains_wake(cleaned) or CALL_GREETING_RE.search(cleaned):
            return True
        if cleaned.endswith("?") or CALL_QUESTION_RE.search(cleaned):
            return True
        stripped = strip_wake(cleaned)
        if fun_reply(stripped) or PLAY_MUSIC_RE.match(stripped) or MUSIC_SKIP_RE.match(stripped) or MUSIC_STOP_RE.match(stripped):
            return True
        intents = match_intents(cleaned if contains_wake(cleaned) else f"opus {cleaned}")
        return any(
            item.name in {
                "discord_mod",
                "discord_join_call",
                "discord_bot_leave",
                "discord_leave_call",
                "coinflip",
                "dice",
                "discord_play_music",
                "discord_skip_music",
                "discord_stop_music",
            }
            for item in intents
        )

    async def _index_message(self, message: discord.Message, *, force: bool = False) -> None:
        if not force and not self._should_index(message):
            return
        if message.author.bot or not message.guild:
            return
        try:
            await asyncio.to_thread(self.store.index_discord_message, message)
        except Exception:
            log.exception("discord index failed message=%s", message.id)

    async def _scan_channel_history(
        self,
        channel: discord.abc.Messageable,
        guild: discord.Guild,
        stats: dict,
    ) -> None:
        if guild.id in self._scan_cancel:
            return
        channel_name = getattr(channel, "name", "") or str(getattr(channel, "id", ""))
        indexed = 0
        try:
            if not hasattr(channel, "history"):
                stats["skipped"].append(channel_name)
                return
            async for msg in channel.history(limit=None):
                if guild.id in self._scan_cancel:
                    return
                if msg.author.bot:
                    continue
                await asyncio.to_thread(self.store.index_discord_message, msg)
                indexed += 1
                stats["messages"] += 1
                if _collect_image_urls(msg):
                    stats["images"] += 1
                if indexed % 250 == 0:
                    await asyncio.sleep(0.35)
        except discord.Forbidden:
            stats["skipped"].append(channel_name)
            log.warning("scan forbidden channel=%s", channel_name)
        except Exception:
            stats["skipped"].append(channel_name)
            log.exception("scan failed channel=%s", channel_name)
        else:
            stats["channels"] += 1
            stats["scanned"].append(channel_name)

    async def _scan_guild_all_channels(
        self,
        guild: discord.Guild,
        *,
        only_channel: discord.TextChannel | None = None,
        status_message: discord.Message | None = None,
    ) -> dict:
        stats = {"messages": 0, "channels": 0, "images": 0, "skipped": [], "scanned": []}
        self._scan_cancel.discard(guild.id)
        targets: list = [only_channel] if only_channel else list(guild.text_channels)
        total = len(targets)
        for idx, channel in enumerate(targets, start=1):
            if guild.id in self._scan_cancel:
                stats["cancelled"] = True
                break
            if status_message and idx % 3 == 0:
                try:
                    await status_message.edit(
                        content=(
                            f"Scanning `{guild.name}` — channel {idx}/{total} "
                            f"(`#{getattr(channel, 'name', channel)}`)...\n"
                            f"Indexed so far: {stats['messages']} messages."
                        )
                    )
                except Exception:
                    pass
            await self._scan_channel_history(channel, guild, stats)
        self._scan_cancel.discard(guild.id)
        return stats

    def _format_scan_stats(self, guild: discord.Guild, stats: dict) -> str:
        db_stats = self.store.guild_stats(str(guild.id))
        lines = []
        if stats.get("cancelled"):
            lines.append("Scan cancelled.")
        else:
            lines.append(f"Scan finished for `{guild.name}`.")
        lines.append(
            f"This run: {stats['messages']} messages across {stats['channels']} channels "
            f"({stats['images']} with images)."
        )
        lines.append(
            f"Database total: {db_stats['messages']} messages, {db_stats['channels']} channels, "
            f"{db_stats['images']} images."
        )
        if db_stats["oldest"] or db_stats["newest"]:
            lines.append(f"Stored range: {db_stats['oldest'] or '?'} -> {db_stats['newest'] or '?'}")
        if stats["skipped"]:
            lines.append("Skipped (no access): " + ", ".join(f"#{name}" for name in stats["skipped"][:8]))
        return "\n".join(lines)

    async def _handle_scan_command(self, message: discord.Message, prompt: str) -> None:
        if not message.guild:
            await message.reply("Scan commands only work in servers.")
            return
        if not self._is_admin(message):
            await message.reply("Only administrators can run scan commands.")
            return

        match = SCAN_COMMAND_RE.match((prompt or "").strip())
        if not match:
            return
        action = (match.group(1) or "server").lower()
        guild = message.guild
        guild_id = guild.id

        if action == "status":
            stats = self.store.guild_stats(str(guild_id))
            active = guild_id in self._scan_tasks and not self._scan_tasks[guild_id].done()
            await message.reply(
                f"Scan status for `{guild.name}`:\n"
                f"- Active scan: {'yes' if active else 'no'}\n"
                f"- Indexed messages: {stats['messages']}\n"
                f"- Indexed channels: {stats['channels']}\n"
                f"- Indexed images: {stats['images']}\n"
                f"- Oldest stored: {stats['oldest'] or 'n/a'}\n"
                f"- Newest stored: {stats['newest'] or 'n/a'}"
            )
            return

        if action == "stop":
            self._scan_cancel.add(guild_id)
            task = self._scan_tasks.get(guild_id)
            if task and not task.done():
                task.cancel()
                await message.reply("Stopping the active scan.")
            else:
                await message.reply("No active scan to stop.")
            return

        existing = self._scan_tasks.get(guild_id)
        if existing and not existing.done():
            await message.reply("A scan is already running. Use `@Opus scan stop` first.")
            return

        if action == "channel":
            if not isinstance(message.channel, discord.TextChannel):
                await message.reply("Run `scan channel` in a text channel.")
                return
            status = await message.reply(
                f"Scanning all past messages in #{message.channel.name}..."
            )
            stats = await self._scan_guild_all_channels(
                guild,
                only_channel=message.channel,
                status_message=status,
            )
            await status.edit(content=self._format_scan_stats(guild, stats))
            return

        status = await message.reply(
            f"Scanning all past messages in every channel I can read in `{guild.name}`..."
        )

        async def runner() -> None:
            try:
                stats = await self._scan_guild_all_channels(guild, status_message=status)
                await status.edit(content=self._format_scan_stats(guild, stats))
            except asyncio.CancelledError:
                await status.edit(content=f"Scan cancelled for `{guild.name}`.")
            except Exception:
                log.exception("guild scan failed guild=%s", guild.id)
                await status.edit(content="Scan failed — check opus.log for details.")
            finally:
                self._scan_tasks.pop(guild_id, None)
                self._scan_cancel.discard(guild_id)

        self._scan_tasks[guild_id] = asyncio.create_task(runner())

    async def _backfill_guild(self, guild: discord.Guild, limit: int = 400) -> None:
        channel_allow = self._as_set(self.settings.get("discord_allowed_channels"))
        for channel in guild.text_channels:
            if channel_allow and str(channel.id) not in channel_allow:
                continue
            try:
                async for msg in channel.history(limit=limit):
                    await self._index_message(msg)
            except Exception:
                log.exception("discord backfill failed channel=%s", channel.id)

    @staticmethod
    def _best_member_match(name: str, members: list[discord.Member]) -> discord.Member | None:
        """Find the member whose display name best matches the given name."""
        name_lower = name.strip().lower()
        best: discord.Member | None = None
        best_score = 0.0
        for m in members:
            for candidate in (m.display_name.lower(), m.name.lower(), (m.global_name or "").lower()):
                if not candidate:
                    continue
                if name_lower == candidate:
                    return m
                score = SequenceMatcher(None, name_lower, candidate).ratio()
                if candidate.startswith(name_lower) or name_lower in candidate:
                    score += 0.3
                if score > best_score:
                    best_score = score
                    best = m
        return best if best_score >= 0.45 else None

    @staticmethod
    def _is_mass_target(name: str) -> bool:
        cleaned = re.sub(
            r"\b(from\s+)?(the\s+)?(vc|voice(\s+channel)?|call|channel)\b",
            "",
            (name or "").strip().lower(),
            flags=re.IGNORECASE,
        ).strip(" .,!")
        return cleaned in {"all", "everyone", "everybody", "every one"}

    async def _run(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.voice_states = True
        client = discord.Client(intents=intents)
        self._client = client

        @client.event
        async def on_ready():
            for guild in client.guilds:
                try:
                    await guild.chunk()
                except Exception:
                    log.exception("guild chunk failed guild=%s", guild.id)
                self._refresh_guild_roster(guild)
                asyncio.create_task(self._backfill_guild(guild))
            log.info("discord connected as %s", client.user)

        @client.event
        async def on_voice_state_update(member, before, after):
            if not client.user or member.id != client.user.id:
                return
            if after.channel is not None:
                return
            guild = member.guild
            if guild is None and before.channel is not None:
                guild = before.channel.guild
            if guild is not None:
                self._drop_guild_voice_state(int(guild.id))

        @client.event
        async def on_member_join(member: discord.Member):
            if member.guild:
                self._refresh_guild_roster(member.guild)

        @client.event
        async def on_member_remove(member: discord.Member):
            if member.guild:
                self._refresh_guild_roster(member.guild)

        @client.event
        async def on_member_update(before: discord.Member, after: discord.Member):
            if after.guild:
                self._refresh_guild_roster(after.guild)

        @client.event
        async def on_message(message: discord.Message):
            await self._index_message(message)
            ok, prompt = self._should_respond(message)
            if not ok:
                return
            if not prompt:
                await message.reply("Ask me something after my name.")
                return
            if SCAN_COMMAND_RE.match(prompt.strip()):
                await self._handle_scan_command(message, prompt)
                return
            if JOIN_CALL_RE.match(prompt.strip()):
                result = await self._join_for_member(
                    message.author if message.guild else None,
                    message.guild,
                )
                await message.reply(result)
                return
            if LEAVE_CALL_RE.match(prompt.strip()):
                result = await self._leave_voice(message.guild)
                await message.reply(result)
                return
            extra = await self._handle_fun_or_music(
                prompt,
                message.guild,
                message.author if message.guild else None,
            )
            if extra:
                await message.reply(extra)
                if message.guild and self._is_connected_channel(message.channel):
                    await self._speak_in_call(extra, message.guild.id)
                return
            prompt = re.sub(
                r"\b(mute|unmute|deafen|undeafen|kick|timeout|untimeout)\s*alls?\b",
                r"\1 all",
                prompt,
                flags=re.IGNORECASE,
            )
            mod = MOD_RE.search(prompt)
            if mod and message.guild:
                if not self._is_admin(message):
                    await message.reply("Only administrators can use moderation commands.")
                    return
                from opus.intents import match_intents

                compound = match_intents(f"opus {prompt}")
                mod_intents = [
                    item
                    for item in compound
                    if item.name in {
                        "discord_mod",
                        "discord_self_mute",
                        "discord_self_deafen",
                        "discord_leave_call",
                    }
                ]
                if len(mod_intents) >= 2 or (
                    len(mod_intents) == 1 and mod_intents[0].name == "discord_mod"
                ):
                    replies: list[str] = []
                    # Alphabetical mod targets, then self actions.
                    mods = sorted(
                        [item for item in mod_intents if item.name == "discord_mod"],
                        key=lambda item: item.rest.partition("|")[2].lower(),
                    )
                    for item in mods:
                        action, _, target = item.rest.partition("|")
                        replies.append(
                            await self._handle_mod_command(
                                message.guild,
                                action,
                                target,
                                actor=message.author,
                            )
                        )
                    for item in mod_intents:
                        if item.name == "discord_self_mute":
                            replies.append("Self mute is a local Discord keybind — use voice Opus for that.")
                        elif item.name == "discord_self_deafen":
                            replies.append("Self deafen is a local Discord keybind — use voice Opus for that.")
                        elif item.name == "discord_leave_call":
                            replies.append("Leave call is a local Discord keybind — use voice Opus for that.")
                    await message.reply(" ".join(replies)[:1800])
                    if self._is_connected_channel(message.channel) and message.guild:
                        await self._speak_in_call(" ".join(replies), message.guild.id)
                    return
                action = self._normalize_mod_action(mod.group(1) or mod.group(2) or "")
                target = (mod.group(3) or "all").strip()
                result = await self._handle_mod_command(
                    message.guild,
                    action,
                    target,
                    actor=message.author,
                )
                await message.reply(result)
                if self._is_connected_channel(message.channel) and message.guild:
                    await self._speak_in_call(result, message.guild.id)
                return
            asks_creation = bool(ORIGIN_QUESTION_RE.search(prompt)) and not NOT_ORIGIN_RE.search(prompt)
            if asks_creation:
                aegritudo = self._mention_by_name(message, "Aegritudo", "@Aegritudo")
                reply = f"{aegritudo} built me from scratch and made me what I am."
                await message.reply(reply)
                if self._is_connected_channel(message.channel) and message.guild:
                    await self._speak_in_call(
                        "Aegritudo built me from scratch and made me what I am.",
                        message.guild.id,
                    )
                return


            # Collect image URLs from attachments
            image_urls: list[str] = _collect_image_urls(message)

            # If replying to another message, include that message's content and images
            reply_context = ""
            if message.reference and message.reference.message_id:
                try:
                    ref_msg = await message.channel.fetch_message(message.reference.message_id)
                    if ref_msg.content:
                        reply_context = f"[Replying to {ref_msg.author.display_name}: \"{ref_msg.content[:1000]}\"]"
                    for att in ref_msg.attachments:
                        if att.content_type and att.content_type.startswith("image"):
                            image_urls.append(att.url)
                        elif str(getattr(att, "filename", "")).lower().endswith(
                            (".png", ".jpg", ".jpeg", ".gif", ".webp")
                        ):
                            image_urls.append(att.url)
                    # Also grab embed images from the referenced message
                    for embed in ref_msg.embeds:
                        if embed.image and embed.image.url:
                            image_urls.append(embed.image.url)
                except Exception:
                    log.exception("failed to fetch referenced message")

            full_prompt = f"{reply_context}\n{prompt}".strip() if reply_context else prompt
            author = message.author
            author_names = [
                str(getattr(author, "name", "") or ""),
                str(getattr(author, "global_name", "") or ""),
                str(getattr(author, "display_name", "") or ""),
                str(getattr(author, "id", "") or ""),
            ]

            member_roster = (
                self._refresh_guild_roster(message.guild) if message.guild else ""
            )

            async with message.channel.typing():
                try:
                    answer = await asyncio.to_thread(
                        self.on_prompt,
                        full_prompt,
                        str(message.author.display_name),
                        image_urls,
                        author_names,
                        member_roster,
                        str(message.guild.id) if message.guild else "",
                    )
                except Exception:
                    log.exception("discord prompt failed")
                    answer = "I hit an error while answering."
            await message.reply(answer[:1800] if answer else "Done.")
            if self._is_connected_channel(message.channel) and message.guild:
                await self._speak_in_call(answer or "Done.", message.guild.id)

        token = (self.settings.get("discord_bot_token") or "").strip()
        await client.start(token)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def target() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._run())
            except Exception:
                log.exception("discord bot exited")
            finally:
                self._loop = None
                self._client = None

        self._thread = threading.Thread(target=target, daemon=True, name="opus-discord")
        self._thread.start()

    def stop(self) -> None:
        if not self._loop or not self._client:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._leave_all_voice(), self._loop)
        except Exception:
            pass
        try:
            asyncio.run_coroutine_threadsafe(self._client.close(), self._loop)
        except Exception:
            log.exception("discord close failed")
