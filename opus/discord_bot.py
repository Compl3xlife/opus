from __future__ import annotations

import asyncio
import base64
import json
import re
import threading
import urllib.error
import urllib.request
from difflib import SequenceMatcher

import discord
from discord import app_commands

from opus.discord_guild import CREATOR_DISCORD_ID, GuildScopedOps
from opus.discord_music import MusicManager
from opus.discord_store import DiscordStore, _collect_image_urls
from opus.discord_voice import CallSink
from opus.logutil import get_logger

log = get_logger()

JOIN_CALL_RE = re.compile(
    r"^(please\s+)?(join|come(\s+(here|in|on))?|hop\s+in)"
    r"(\s+(the\s+|this\s+|our\s+)?(call|vc|voice|channel))?\s*[.!?]*$",
    re.IGNORECASE,
)
INVITE_RE = re.compile(
    r"^(please\s+)?("
    r"invite(\s+(link|url|me))?"
    r"|invite\s+(to\s+)?((a|the|another|other)\s+)?servers?"
    r"|add(\s+me|\s+you|\s+the\s+bot)?(\s+to)?(\s+(a|the|another|other)\s+)?servers?"
    r"|join(\s+(another|other|a|the)\s+)?servers?"
    r")\s*[.!?]*$",
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


class InteractionMessage:
    """Duck-typed message so slash commands reuse the prefix command path."""

    def __init__(self, interaction: discord.Interaction, content: str) -> None:
        self.author = interaction.user
        self.guild = interaction.guild
        self.channel = interaction.channel
        self.content = content
        self.reference = None
        self.attachments: list = []
        self.embeds: list = []
        self.role_mentions: list = []
        self.mentions: list = []
        self.id = int(getattr(interaction, "id", 0) or 0)
        self._interaction = interaction

    async def reply(self, content=None, **kwargs):
        text = content if content is not None else kwargs.pop("content", "")
        kwargs.pop("mention_author", None)
        await self._interaction.followup.send(str(text)[:1800], **kwargs)


def bot_user_id_from_token(token: str) -> str:
    raw = (token or "").strip()
    if not raw:
        return ""
    try:
        part = raw.split(".")[0]
        pad = "=" * ((4 - len(part) % 4) % 4)
        decoded = base64.b64decode(part + pad).decode("ascii")
        if decoded.isdigit():
            return decoded
    except Exception:
        pass
    return ""


def _invite_permissions() -> discord.Permissions:
    return discord.Permissions(
        view_channel=True,
        send_messages=True,
        embed_links=True,
        attach_files=True,
        read_message_history=True,
        add_reactions=True,
        connect=True,
        speak=True,
        mute_members=True,
        deafen_members=True,
        move_members=True,
        use_voice_activation=True,
        moderate_members=True,
        manage_roles=True,
        send_polls=True,
    )


def _invite_permission_value() -> str:
    return str(_invite_permissions().value)


def discord_invite_url(*, token: str = "", client_id: str | int | None = None) -> str:
    cid = str(client_id or "").strip() or bot_user_id_from_token(token)
    if not cid:
        return ""
    try:
        url = discord.utils.oauth_url(
            int(cid),
            permissions=_invite_permissions(),
            scopes=("bot", "applications.commands"),
        )
        if "integration_type=" not in url:
            url += "&integration_type=0"
        return url
    except Exception:
        log.exception("discord invite url failed")
        return ""


def _discord_app_request(method: str, token: str, payload: dict | None = None) -> dict | None:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://discord.com/api/v10/applications/@me",
        data=body,
        method=method,
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": "Opus",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:400]
        except Exception:
            pass
        log.warning("discord application %s failed status=%s body=%s", method, exc.code, detail)
        return None
    except Exception:
        log.exception("discord application %s failed", method)
        return None


def ensure_discord_guild_install(token: str) -> bool:
    """Make Discord's Add to Server actually add the bot user, not only slash commands."""
    raw = (token or "").strip()
    if not raw:
        return False
    app = _discord_app_request("GET", raw)
    if not app:
        return False
    perms = _invite_permission_value()
    guild_params = ((app.get("integration_types_config") or {}).get("0") or {}).get(
        "oauth2_install_params"
    ) or {}
    default_params = app.get("install_params") or {}
    guild_scopes = [str(s) for s in (guild_params.get("scopes") or [])]
    default_scopes = [str(s) for s in (default_params.get("scopes") or [])]
    user_cfg = (app.get("integration_types_config") or {}).get("1") or {}
    user_scopes = [
        str(s) for s in (user_cfg.get("oauth2_install_params") or {}).get("scopes") or []
    ]
    if (
        "bot" in guild_scopes
        and "bot" in default_scopes
        and "applications.commands" in user_scopes
    ):
        return True
    if not user_scopes:
        user_cfg = {
            "oauth2_install_params": {"scopes": ["applications.commands"], "permissions": "0"}
        }
    updated = _discord_app_request(
        "PATCH",
        raw,
        {
            "install_params": {
                "scopes": ["bot", "applications.commands"],
                "permissions": perms,
            },
            "integration_types_config": {
                "0": {
                    "oauth2_install_params": {
                        "scopes": ["bot", "applications.commands"],
                        "permissions": perms,
                    }
                },
                "1": user_cfg,
            },
        },
    )
    if not updated:
        return False
    log.info("discord Add to Server now includes the bot user")
    return True


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
        self._chip_watch_started = False

    def invite_url(self) -> str:
        user = getattr(self._client, "user", None) if self._client else None
        return discord_invite_url(
            token=self.settings.get("discord_bot_token") or "",
            client_id=getattr(user, "id", None),
        )

    def invite_reply(self) -> str:
        url = self.invite_url()
        if not url:
            return "Enable Discord in the Opus panel first, then ask me for the invite link."
        return (
            "Open this link and use Add to Server so I can join that server's voice. "
            "For group DMs, also add me as a user app (Add App) so /opus, /join, and /play work there. "
            "Discord still won't let bots sit in a group-DM call — join a server voice channel and I'll hop in from the group chat.\n"
            f"{url}"
        )

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
        if message.guild and guild_allow and str(message.guild.id) not in guild_allow:
            return False, ""
        channel_allow = self._as_set(self.settings.get("discord_allowed_channels"))
        if message.guild and channel_allow and str(message.channel.id) not in channel_allow:
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

    def _is_voice_question(self, text: str) -> bool:
        cleaned = (text or "").strip()
        if not cleaned:
            return False
        from opus.intents import strip_wake

        stripped = strip_wake(cleaned) or cleaned
        if stripped.endswith("?") or cleaned.endswith("?"):
            return True
        if CALL_QUESTION_RE.search(stripped) or CALL_QUESTION_RE.search(cleaned):
            return True
        if ORIGIN_QUESTION_RE.search(cleaned) and not NOT_ORIGIN_RE.search(cleaned):
            return True
        return False

    async def _speak_if_question(self, prompt: str, answer: str, message: discord.Message) -> None:
        if not answer:
            return
        if not self._is_voice_question(prompt):
            return
        guild = message.guild or self._guild_for(member=message.author)
        if not guild:
            return
        voice = self._voice_for_guild(guild.id)
        in_chat = self._is_connected_channel(message.channel)
        in_their_call = False
        if voice and voice.channel:
            in_their_call = any(
                getattr(item, "id", 0) == getattr(message.author, "id", 0)
                for item in voice.channel.members
            )
        if not in_chat and not in_their_call:
            return
        await self._speak_in_call(answer, guild.id)

    def _is_talking_to_opus(self, text: str) -> bool:
        cleaned = (text or "").strip()
        if not cleaned or CALL_CHAT_NOISE_RE.match(cleaned):
            return False
        from opus.intents import contains_wake, match_intents, strip_wake
        from opus.discord_fun import fun_reply
        from opus.discord_guild import (
            MUSIC_PAUSE_RE,
            MUSIC_REPEAT_RE,
            MUSIC_REPLAY_RE,
            MUSIC_SKIP_RE,
            MUSIC_STOP_RE,
            PLAY_MUSIC_RE,
        )

        if contains_wake(cleaned) or CALL_GREETING_RE.search(cleaned):
            return True
        if cleaned.endswith("?") or CALL_QUESTION_RE.search(cleaned):
            return True
        stripped = strip_wake(cleaned)
        if (
            fun_reply(stripped)
            or PLAY_MUSIC_RE.match(stripped)
            or MUSIC_SKIP_RE.match(stripped)
            or MUSIC_REPLAY_RE.match(stripped)
            or MUSIC_PAUSE_RE.match(stripped)
            or MUSIC_STOP_RE.match(stripped)
            or MUSIC_REPEAT_RE.match(stripped)
        ):
            return True
        from opus.discord_lyrics import LYRICS_RE
        from opus.discord_polls import POLL_COMMAND_RE
        from opus.discord_roles import REACTIONROLE_COMMAND_RE
        from opus.discord_guild import TTS_TOGGLE_RE
        from opus.discord_chips import CHIPS_COMMAND_RE

        if (
            LYRICS_RE.match(stripped)
            or POLL_COMMAND_RE.match(stripped)
            or REACTIONROLE_COMMAND_RE.match(stripped)
            or TTS_TOGGLE_RE.match(stripped)
            or CHIPS_COMMAND_RE.match(stripped)
        ):
            return True
        intents = match_intents(cleaned if contains_wake(cleaned) else f"opus {cleaned}")
        return any(
            item.name in {
                "discord_mod",
                "discord_join_call",
                "discord_bot_leave",
                "discord_leave_call",
                "discord_invite",
                "coinflip",
                "dice",
                "discord_play_music",
                "discord_skip_music",
                "discord_stop_music",
            }
            for item in intents
        )

    async def _index_message(self, message: discord.Message, *, force: bool = False) -> None:
        if getattr(message, "_interaction", None) is not None:
            return
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

    def _can_manage_roles(self, member) -> bool:
        if self._member_is_admin(member):
            return True
        perms = getattr(member, "guild_permissions", None)
        return bool(perms and perms.manage_roles)

    async def _handle_poll_command(self, message: discord.Message, prompt: str) -> None:
        from opus.discord_polls import parse_poll

        parsed = parse_poll(prompt)
        if isinstance(parsed, str):
            await message.reply(parsed)
            return
        if not getattr(message, "channel", None):
            await message.reply("I couldn't see this chat to post a poll.")
            return
        question, options, duration, multiple = parsed
        poll = discord.Poll(question=question, duration=duration, multiple=multiple)
        for option in options:
            poll.add_answer(text=option)
        try:
            sent = await message.channel.send(poll=poll)
        except discord.Forbidden:
            await message.reply("I need Send Polls permission in this channel.")
            return
        except Exception as exc:
            log.exception("poll send failed")
            await message.reply(f"Couldn't start that poll: {exc}")
            return
        if message.guild and not multiple:
            from opus.discord_chips import start_poll_bets

            try:
                await start_poll_bets(self, message, sent, question, options, duration)
            except Exception:
                log.exception("poll bet setup failed")

    async def _handle_reactionrole_command(self, message: discord.Message, prompt: str) -> None:
        from opus.discord_roles import USAGE, action_name, emoji_key_from_token, parse_role_pairs

        if not message.guild:
            await message.reply("Reaction roles only work in servers.")
            return
        if not self._can_manage_roles(message.author):
            await message.reply("You need Manage Roles to set those up.")
            return
        action = action_name(prompt)
        if action == "help":
            await message.reply(USAGE)
            return
        if action == "list":
            rows = self.store.list_reaction_roles(str(message.guild.id))
            if not rows:
                await message.reply("No reaction roles in this server yet.")
                return
            lines: list[str] = []
            for row in rows[:20]:
                lines.append(f"<#{row['channel_id']}> {row['emoji']} <@&{row['role_id']}>")
            extra = f"\n…and {len(rows) - 20} more." if len(rows) > 20 else ""
            await message.reply("\n".join(lines) + extra)
            return
        if action == "remove":
            target_id = 0
            if message.reference and message.reference.message_id:
                target_id = int(message.reference.message_id)
            else:
                match = re.search(r"\b(\d{15,25})\b", prompt)
                if match:
                    target_id = int(match.group(1))
            if not target_id:
                await message.reply("Reply to the roles message, or include its message ID.")
                return
            removed = self.store.delete_reaction_message(str(message.guild.id), str(target_id))
            await message.reply("Removed those reaction roles." if removed else "I didn't have roles on that message.")
            return
        pairs = parse_role_pairs(message)
        if not pairs:
            await message.reply(USAGE)
            return
        target = None
        if message.reference and message.reference.message_id:
            try:
                target = await message.channel.fetch_message(int(message.reference.message_id))
            except Exception:
                target = None
        if target is None:
            embed = discord.Embed(title="Reaction roles", color=0xC4A574)
            embed.description = "\n".join(f"{emoji} — {role.mention}" for emoji, role in pairs)
            try:
                target = await message.channel.send(embed=embed)
            except Exception as exc:
                await message.reply(f"Couldn't post the roles message: {exc}")
                return
        added = 0
        for emoji, role in pairs:
            try:
                await target.add_reaction(emoji)
            except Exception:
                log.exception("reactionrole emoji failed emoji=%s", emoji)
                continue
            self.store.save_reaction_role(
                guild_id=str(message.guild.id),
                channel_id=str(target.channel.id),
                message_id=str(target.id),
                emoji_key=emoji_key_from_token(str(emoji)),
                emoji=str(emoji),
                role_id=str(role.id),
            )
            added += 1
        if not added:
            await message.reply("I couldn't add those emoji reactions.")
            return
        await message.reply(f"Reaction roles are live on that message ({added}).")

    async def _apply_reaction_role(self, payload: discord.RawReactionActionEvent, *, add: bool) -> None:
        from opus.discord_roles import emoji_key

        if not payload.guild_id or not self._client or not self._client.user:
            return
        if payload.user_id == self._client.user.id:
            return
        role_id = self.store.lookup_reaction_role(
            str(payload.guild_id),
            str(payload.message_id),
            emoji_key(payload.emoji),
        )
        if not role_id:
            return
        guild = self._client.get_guild(int(payload.guild_id))
        if guild is None:
            return
        role = guild.get_role(int(role_id))
        member = guild.get_member(int(payload.user_id))
        if member is None:
            try:
                member = await guild.fetch_member(int(payload.user_id))
            except Exception:
                return
        if role is None or member is None or member.bot:
            return
        try:
            if add:
                await member.add_roles(role, reason="Opus reaction role")
            else:
                await member.remove_roles(role, reason="Opus reaction role")
        except discord.Forbidden:
            log.warning("reaction role forbidden guild=%s role=%s", payload.guild_id, role_id)
        except Exception:
            log.exception("reaction role update failed")

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
        tree = app_commands.CommandTree(client)

        def _slash(name, description):
            return tree.command(name=name, description=description)

        async def _run_slash(interaction: discord.Interaction, content: str) -> None:
            await interaction.response.defer()
            try:
                await on_message(InteractionMessage(interaction, content))
            except Exception:
                log.exception("slash command failed")
                try:
                    await interaction.followup.send("I hit an error running that.")
                except Exception:
                    pass

        @_slash("opus", "Ask Opus or run a command (play, join, poll, …)")
        @app_commands.describe(command="What to say, like play never gonna give you up")
        @app_commands.allowed_installs(guilds=True, users=True)
        @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
        async def opus_slash(interaction: discord.Interaction, command: str):
            await _run_slash(interaction, f"opus {command}")

        @_slash("join", "Join your current server voice call")
        @app_commands.allowed_installs(guilds=True, users=True)
        @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
        async def join_slash(interaction: discord.Interaction):
            await _run_slash(interaction, "opus join")

        @_slash("play", "Play a song in the call you're in")
        @app_commands.describe(query="Song name or YouTube link")
        @app_commands.allowed_installs(guilds=True, users=True)
        @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
        async def play_slash(interaction: discord.Interaction, query: str):
            await _run_slash(interaction, f"opus play {query}")

        @client.event
        async def on_ready():
            for guild in client.guilds:
                try:
                    await guild.chunk()
                except Exception:
                    log.exception("guild chunk failed guild=%s", guild.id)
                self._refresh_guild_roster(guild)
                asyncio.create_task(self._backfill_guild(guild))
                try:
                    self.store.grant_opus_allowance(str(guild.id))
                except Exception:
                    log.exception("opus allowance failed guild=%s", guild.id)
            try:
                synced = await tree.sync()
                log.info("discord slash commands synced count=%s", len(synced))
            except Exception:
                log.exception("discord command sync failed")
            log.info("discord connected as %s", client.user)
            if not getattr(self, "_chip_watch_started", False):
                self._chip_watch_started = True
                from opus.discord_chips import watch_chip_polls

                asyncio.create_task(watch_chip_polls(self))
            if not getattr(self, "_chip_restored", False):
                self._chip_restored = True
                try:
                    holds = self.store.restore_open_holds()
                    challenges = self.store.abandon_live_challenges()
                    if holds or challenges:
                        log.info("restored chip state holds=%s challenges=%s", holds, challenges)
                except Exception:
                    log.exception("chip restore failed")

        @client.event
        async def on_guild_join(guild: discord.Guild):
            log.info("joined discord server %s (%s)", guild.name, guild.id)
            try:
                await guild.chunk()
            except Exception:
                log.exception("guild chunk failed guild=%s", guild.id)
            self._refresh_guild_roster(guild)
            asyncio.create_task(self._backfill_guild(guild))
            try:
                self.store.grant_opus_allowance(str(guild.id))
            except Exception:
                log.exception("opus allowance failed guild=%s", guild.id)

        @client.event
        async def on_guild_remove(guild: discord.Guild):
            log.info("left discord server %s (%s)", guild.name, guild.id)
            self._member_rosters.pop(int(guild.id), None)
            self._drop_guild_voice_state(int(guild.id))

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
        async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
            await self._apply_reaction_role(payload, add=True)

        @client.event
        async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
            await self._apply_reaction_role(payload, add=False)

        @client.event
        async def on_message(message: discord.Message):
            try:
                await _handle_on_message(message)
            except Exception:
                log.exception("discord on_message failed")
                try:
                    await message.reply("I hit an error on that one.")
                except Exception:
                    pass

        async def _handle_on_message(message: discord.Message):
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
            from opus.discord_polls import POLL_COMMAND_RE
            from opus.discord_roles import REACTIONROLE_COMMAND_RE

            if POLL_COMMAND_RE.match(prompt.strip()):
                await self._handle_poll_command(message, prompt)
                return
            from opus.discord_chips import handle_chip_command

            try:
                if await handle_chip_command(self, message, prompt):
                    return
            except Exception:
                log.exception("chip command failed")
                try:
                    await message.reply("I hit an error on that chip command.")
                except Exception:
                    pass
                return
            if REACTIONROLE_COMMAND_RE.match(prompt.strip()):
                await self._handle_reactionrole_command(message, prompt)
                return
            if INVITE_RE.match(prompt.strip()):
                await message.reply(self.invite_reply())
                return
            if JOIN_CALL_RE.match(prompt.strip()):
                result = await self._join_for_member(
                    message.author,
                    message.guild,
                )
                await message.reply(result)
                return
            if LEAVE_CALL_RE.match(prompt.strip()):
                result = await self._leave_voice(message.guild or self._guild_for(member=message.author))
                await message.reply(result)
                return
            from opus.intents import contains_wake, match_intents

            call_intents = match_intents(
                prompt if contains_wake(prompt) else f"opus {prompt}"
            )
            if any(item.name == "discord_invite" for item in call_intents):
                await message.reply(self.invite_reply())
                return
            join_or_leave = [
                item
                for item in call_intents
                if item.name in {"discord_join_call", "discord_bot_leave", "discord_leave_call"}
            ]
            if join_or_leave:
                last = join_or_leave[-1]
                if last.name in {"discord_bot_leave", "discord_leave_call"}:
                    await message.reply(await self._leave_voice(message.guild or self._guild_for(member=message.author)))
                    return
                result = await self._join_for_member(
                    message.author,
                    message.guild,
                )
                await message.reply(result)
                return
            extra = await self._handle_fun_or_music(
                prompt,
                message.guild,
                message.author,
            )
            if extra:
                await message.reply(extra)
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
                return
            asks_creation = bool(ORIGIN_QUESTION_RE.search(prompt)) and not NOT_ORIGIN_RE.search(prompt)
            if asks_creation:
                aegritudo = self._mention_by_name(message, "Aegritudo", "@Aegritudo")
                reply = f"{aegritudo} built me from scratch and made me what I am."
                await message.reply(reply)
                await self._speak_if_question(prompt, "Aegritudo built me from scratch and made me what I am.", message)
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

            async def _answer_prompt():
                try:
                    return await asyncio.to_thread(
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
                    return "I hit an error while answering."

            if getattr(message.channel, "typing", None):
                async with message.channel.typing():
                    answer = await _answer_prompt()
            else:
                answer = await _answer_prompt()
            await message.reply(answer[:1800] if answer else "Done.")
            await self._speak_if_question(prompt, answer or "Done.", message)

        token = (self.settings.get("discord_bot_token") or "").strip()
        await asyncio.to_thread(ensure_discord_guild_install, token)
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
        try:
            self.store.restore_open_holds()
            self.store.abandon_live_challenges()
            self.store.checkpoint()
        except Exception:
            log.exception("chip restore on stop failed")
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
