"""Discord buttons for the in-call music player."""
from __future__ import annotations

import discord

from opus.logutil import get_logger

log = get_logger()


class AddSongModal(discord.ui.Modal, title="Add a song"):
    query = discord.ui.TextInput(
        label="Song or YouTube link",
        placeholder="Artist - title, or a YouTube URL",
        min_length=1,
        max_length=200,
    )

    def __init__(self, bot, guild_id: int):
        super().__init__()
        self._bot = bot
        self._guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            result = await self._bot._enqueue_from_control(self._guild_id, str(self.query.value), interaction.user)
        except Exception:
            log.exception("add song failed")
            result = "Couldn't add that song."
        await interaction.followup.send(result, ephemeral=True)


class PlaylistSelect(discord.ui.Select):
    def __init__(self, bot, guild_id: int, playlists: list[dict]):
        options = [
            discord.SelectOption(
                label=str(item.get("name") or "Playlist")[:100],
                value=str(item.get("id") or ""),
                description=(
                    f"{int(item.get('tracks') or 0)} songs"
                    if item.get("tracks")
                    else "Open to pick songs"
                )[:100],
            )
            for item in playlists
            if item.get("id")
        ]
        super().__init__(
            placeholder="Pick a playlist to see its songs",
            min_values=1,
            max_values=1,
            options=options[:25],
        )
        self._bot = bot
        self._guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:
        playlist_id = str(self.values[0] if self.values else "")
        label = next((opt.label for opt in self.options if opt.value == playlist_id), "that playlist")
        await interaction.response.defer(ephemeral=True)
        try:
            tracks = await self._bot._spotify_songs_for_player(playlist_id)
        except PermissionError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            log.exception("spotify playlist songs failed")
            await interaction.followup.send("Couldn't read that playlist.", ephemeral=True)
            return
        if not tracks:
            await interaction.followup.send(
                f"{label} didn't have songs I could read. Disconnect and connect Spotify in my panel if this keeps happening.",
                ephemeral=True,
            )
            return
        view = discord.ui.View(timeout=120)
        view.add_item(PlaylistTrackSelect(self._bot, self._guild_id, playlist_id, label, tracks))
        await interaction.followup.send(
            f"Songs in {label} — pick some, or queue the whole playlist.",
            view=view,
            ephemeral=True,
        )


class PlaylistTrackSelect(discord.ui.Select):
    def __init__(self, bot, guild_id: int, playlist_id: str, label: str, tracks: list[dict]):
        options = [
            discord.SelectOption(
                label="Queue entire playlist",
                value="__all__",
                description=f"{len(tracks)} songs"[:100],
            )
        ]
        for idx, track in enumerate(tracks[:24]):
            name = str(track.get("name") or "Song")[:100]
            artists = str(track.get("artists") or "")[:100]
            options.append(
                discord.SelectOption(
                    label=name,
                    value=str(idx),
                    description=artists or "Queue this song",
                )
            )
        super().__init__(
            placeholder=f"Songs in {label}"[:150],
            min_values=1,
            max_values=min(25, len(options)),
            options=options,
        )
        self._bot = bot
        self._guild_id = guild_id
        self._playlist_id = playlist_id
        self._label = label
        self._tracks = tracks

    async def callback(self, interaction: discord.Interaction) -> None:
        picked = list(self.values or [])
        await interaction.response.defer(ephemeral=True)
        if "__all__" in picked:
            queries = [str(track.get("query") or "") for track in self._tracks]
            label = self._label
        else:
            queries = []
            for value in picked:
                try:
                    track = self._tracks[int(value)]
                except (TypeError, ValueError, IndexError):
                    continue
                query = str(track.get("query") or "")
                if query:
                    queries.append(query)
            label = "your picks"
        try:
            result = await self._bot._queue_spotify_queries(
                self._guild_id,
                queries,
                interaction.user,
                label,
            )
        except Exception:
            log.exception("queue playlist songs failed")
            result = "Couldn't queue those songs."
        await interaction.followup.send(result, ephemeral=True)


class MusicControlView(discord.ui.View):
    def __init__(
        self,
        bot,
        guild_id: int,
        *,
        repeating: bool = False,
        tts_enabled: bool = True,
        paused: bool = False,
    ):
        super().__init__(timeout=None)
        self._bot = bot
        self._guild_id = guild_id
        for item in self.children:
            if not isinstance(item, discord.ui.Button):
                continue
            if item.label in {"Pause", "Resume"}:
                item.label = "Resume" if paused else "Pause"
                item.emoji = "▶️" if paused else "⏸️"
                item.style = discord.ButtonStyle.success if paused else discord.ButtonStyle.secondary
            if item.label == "Repeat":
                item.style = (
                    discord.ButtonStyle.success if repeating else discord.ButtonStyle.secondary
                )
            if item.label in {"TTS", "TTS on", "TTS off"}:
                item.style = (
                    discord.ButtonStyle.success if tts_enabled else discord.ButtonStyle.secondary
                )
                item.label = "TTS on" if tts_enabled else "TTS off"

    def _scope_guild(self, interaction: discord.Interaction):
        if interaction.guild:
            return interaction.guild
        client = getattr(self._bot, "_client", None)
        if client:
            return client.get_guild(self._guild_id)
        return None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await self._bot._can_use_music_controls(interaction):
            return True
        await interaction.response.send_message(
            "Join this call first to use the player.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary, emoji="⏸️", row=0)
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        result = await self._bot._toggle_pause(self._scope_guild(interaction))
        try:
            await interaction.followup.send(result, ephemeral=True)
        except Exception:
            pass

    @discord.ui.button(label="Replay", style=discord.ButtonStyle.secondary, emoji="⏮️", row=0)
    async def replay_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        result = await self._bot._replay_music(self._scope_guild(interaction))
        try:
            await interaction.followup.send(result, ephemeral=True)
        except Exception:
            pass

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.primary, emoji="⏭️", row=0)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        result = await self._bot._skip_music(self._scope_guild(interaction))
        try:
            await interaction.followup.send(result, ephemeral=True)
        except Exception:
            pass

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="⏹️", row=0)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        result = await self._bot._stop_music(self._scope_guild(interaction))
        try:
            await interaction.followup.send(result, ephemeral=True)
        except Exception:
            pass

    @discord.ui.button(label="Repeat", style=discord.ButtonStyle.secondary, emoji="🔁", row=0)
    async def repeat_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        result = await self._bot._toggle_repeat(self._scope_guild(interaction))
        try:
            await interaction.followup.send(result, ephemeral=True)
        except Exception:
            pass

    @discord.ui.button(label="Add song", style=discord.ButtonStyle.secondary, emoji="➕", row=1)
    async def add_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(AddSongModal(self._bot, self._guild_id))

    @discord.ui.button(label="Playlists", style=discord.ButtonStyle.success, emoji="🎵", row=1)
    async def playlists_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            playlists = await self._bot._spotify_playlists_for_player()
        except PermissionError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            log.exception("spotify playlist list failed")
            await interaction.followup.send(
                "Couldn't load Spotify playlists. Connect Spotify in my panel first.",
                ephemeral=True,
            )
            return
        if not playlists:
            await interaction.followup.send("No Spotify playlists on that account.", ephemeral=True)
            return
        view = discord.ui.View(timeout=120)
        view.add_item(PlaylistSelect(self._bot, self._guild_id, playlists))
        await interaction.followup.send("Pick a playlist, then I'll show the songs in it.", view=view, ephemeral=True)

    @discord.ui.button(label="Lyrics", style=discord.ButtonStyle.primary, emoji="🎤", row=1)
    async def lyrics_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            result = await self._bot._show_lyrics(self._scope_guild(interaction))
        except Exception:
            log.exception("lyrics button failed")
            result = "Couldn't load lyrics."
        await interaction.followup.send(result, ephemeral=True)

    @discord.ui.button(label="TTS on", style=discord.ButtonStyle.success, emoji="🗣️", row=1)
    async def tts_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        try:
            result = await self._bot._toggle_call_tts(self._scope_guild(interaction))
        except Exception:
            log.exception("tts toggle failed")
            result = "Couldn't toggle TTS."
        try:
            await interaction.followup.send(result, ephemeral=True)
        except Exception:
            pass
