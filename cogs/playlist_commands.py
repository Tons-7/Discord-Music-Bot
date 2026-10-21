import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from config import COLOR, COMMAND_COOLDOWN, MAX_PLAYLIST_SIZE, MAX_QUEUE_SIZE, SONGS_PER_PAGE
from models.song import Song
from services import playlist_service as playlists
from services.music_service import SPOTIFY_TRACK_LIMIT, track_collection
from utils.helpers import (
    clip,
    create_embed,
    create_v2_embed,
    get_existing_urls,
    interaction_check,
    md,
    reply_error,
    report_command_error,
)
from views.pagination import PaginationView

logger = logging.getLogger(__name__)

PERMISSION_CHOICES = [
    app_commands.Choice(name="View only", value="view"),
    app_commands.Choice(name="Add songs", value="append"),
    app_commands.Choice(name="Full edit", value="edit"),
]


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'s' if count != 1 else ''}"


def _pages(title: str, lines: list[str], footer: str = "") -> list[str]:
    """Lines split into SONGS_PER_PAGE-sized pages, each well under Discord's text limit."""
    chunks = [lines[i:i + SONGS_PER_PAGE] for i in range(0, len(lines), SONGS_PER_PAGE)] or [[]]
    total = len(chunks)
    pages = []
    for number, chunk in enumerate(chunks, 1):
        heading = f"### {title}" + (f" - Page {number}/{total}" if total > 1 else "")
        pages.append(clip(f"{heading}\n" + "\n".join(chunk), 3800) + (f"\n{footer}" if footer else ""))
    return pages


class PlaylistCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.queue_service = bot.playback.queue_service
        self.music_service = bot.music
        super().__init__()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id and interaction.channel_id:
            self.bot.get_guild_data(interaction.guild_id)["command_channel_id"] = interaction.channel_id
        return await interaction_check(self, interaction)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        await report_command_error(self.bot, interaction, error)

    # ── Shared helpers ──────────────────────────────────────────────────

    async def _fail(self, interaction: discord.Interaction, text: str, title: str = "Error"):
        await reply_error(self.bot, interaction, text, title)

    async def _send(self, interaction: discord.Interaction, **kwargs):
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)

    def _music(self):
        return self.bot.get_cog("MusicCommands")

    @staticmethod
    def _scope(interaction: discord.Interaction, global_mode: bool) -> int | None:
        return None if global_mode else interaction.guild.id

    async def _resolve(
            self, interaction: discord.Interaction, name: str, global_mode: bool, need: str,
            *, owner: discord.abc.User | None = None, collaborative: bool = False,
    ) -> playlists.Playlist | None:
        """The named playlist with at least ``need`` access, or None after telling the user why."""
        label = playlists.label(global_mode)
        playlist = await playlists.find_by_name(
            self.bot, interaction.user.id, name, self._scope(interaction, global_mode), global_mode,
            owner.id if owner else None, shared_only=collaborative and owner is None,
        )
        if playlist is None:
            await self._fail(interaction, f"{label} **{md(name)}** not found")
            return None
        if not playlist.allows(need):
            await self._fail(interaction, f"You only have **{playlist.level}** access to **{md(playlist.name)}**")
            return None
        return playlist

    async def _owned(self, interaction: discord.Interaction, name: str, global_mode: bool) -> playlists.Playlist | None:
        playlist = await playlists.find_by_name(
            self.bot, interaction.user.id, name, self._scope(interaction, global_mode), global_mode,
            owner_id=interaction.user.id,
        )
        if playlist is None:
            await self._fail(interaction, f"{playlists.label(global_mode)} **{md(name)}** not found (you must own it)")
        return playlist

    async def _save(self, interaction: discord.Interaction, playlist: playlists.Playlist, songs: list[dict]) -> dict | None:
        """Append what fits under the playlist's lock; None after telling the user the playlist is gone or full."""
        try:
            async with playlists.editing(self.bot, playlist):
                if not playlist.room:
                    await self._fail(interaction, f"**{md(playlist.name)}** is full ({MAX_PLAYLIST_SIZE} songs max).")
                    return None
                return playlists.append_new(playlist, songs)
        except playlists.PlaylistGone:
            await self._fail(interaction, f"**{md(playlist.name)}** was deleted.")
            return None

    @staticmethod
    def _result_text(result: dict, source: str, name: str) -> tuple[str, str]:
        """Title and text for a bulk add's reply."""
        added, skipped, dropped = result["added"], result["skipped"], result["dropped"]
        if not added:
            return "No Songs Added", f"All songs from {source} are already in **{md(name)}**"
        text = f"Added **{_plural(added, 'song')}** from {source} to **{md(name)}**"
        if skipped:
            text += f"\n({_plural(skipped, 'duplicate')} skipped)"
        if dropped:
            text += f"\n({dropped} didn't fit: the playlist is full)"
        return "Songs Added", text

    def _session_songs(self, interaction: discord.Interaction) -> list[dict]:
        """The whole queue (played songs, the current one, then the upcoming ones) as playlist entries."""
        guild_data = self.bot.get_guild_data(interaction.guild.id)
        session = self.queue_service.whole_queue(guild_data)
        return [{**song.to_dict(), "requested_by": interaction.user.mention} for song in session]

    # ── Autocomplete ─────────────────────────────────────────────────────

    async def _name_choices(self, interaction: discord.Interaction, current: str, global_mode: bool):
        scope = self._scope(interaction, global_mode)
        needle = current.lower()
        found = await playlists.owned(self.bot, interaction.user.id, scope, global_mode)
        found += await playlists.shared_with(self.bot, interaction.user.id, scope, global_mode)
        choices, seen = [], set()
        for playlist in found:
            if needle in playlist.name.lower() and playlist.name not in seen:
                suffix = "" if playlist.level == "owner" else f" (shared, {playlist.level})"
                choices.append(app_commands.Choice(name=clip(playlist.name + suffix, 100), value=playlist.name))
                seen.add(playlist.name)
            if len(choices) >= 25:
                break
        return choices

    async def playlist_name_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._name_choices(interaction, current, False)

    async def global_name_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._name_choices(interaction, current, True)

    async def queue_autocomplete(self, interaction: discord.Interaction, current: str):
        """Songs of the current session; the value is the song's URL, so it can't go stale as the queue moves."""
        guild_data = self.bot.get_guild_data(interaction.guild.id)
        needle = current.lower()
        entries = []
        if guild_data.get("current"):
            entries.append((f"Now Playing: {guild_data['current'].title}", guild_data["current"].webpage_url))
        entries += [(f"Queue #{i}: {song.title}", song.webpage_url)
                    for i, song in enumerate(guild_data.get("queue", []), 1)]
        entries += [(f"Played: {song.title}", song.webpage_url)
                    for song in reversed(self.queue_service.played_before(guild_data))]
        choices = [
            app_commands.Choice(name=clip(label, 100), value=url)
            for label, url in entries
            if url and len(url) <= 100 and needle in label.lower()
        ]
        return choices[:25]

    # ── Handlers ─────────────────────────────────────────────────────────

    async def _handle_create(self, interaction: discord.Interaction, name: str, global_mode: bool = False):
        label = playlists.label(global_mode)
        try:
            name = await playlists.create(
                self.bot, interaction.user.id, name, self._scope(interaction, global_mode), global_mode,
            )
        except ValueError as e:
            await self._fail(interaction, str(e))
            return
        embed = create_embed(f"{label} Created", f"Created empty {label.lower()} **{md(name)}**", COLOR, self.bot.user)
        await interaction.response.send_message(embed=embed, silent=True)

    async def _link_songs(self, link: str, room: int) -> tuple[list[dict], str, bool] | None:
        """Songs of a playlist/album link (at most ``room`` looked up), what to call it, and whether it was cut.

        None when the link is not a collection.
        """
        collection = track_collection(link)
        if collection:
            service = {"spotify": "Spotify", "apple": "Apple Music"}.get(collection[0], "the")
            tracks = await self.music_service.collection_tracks(link)
            cut = collection[0] == "spotify" and len(tracks) >= SPOTIFY_TRACK_LIMIT
            # Only as many as can still fit are looked up on YouTube.
            songs = [song async for song in self.music_service.iter_youtube_matches(tracks[:room])]
            return songs, f"the {service} {collection[1]}", cut or len(tracks) > room

        from activity.helpers import collection_entries
        info = await self.bot.get_song_info_cached(link)
        entries = collection_entries(info)
        if entries is None:
            return None
        return entries, "the playlist", bool(isinstance(info, dict) and info.get("capped"))

    async def _handle_add(self, interaction: discord.Interaction, name: str, song: str, global_mode: bool = False,
                          collaborative: bool = False, owner: discord.User | None = None):
        await interaction.response.defer()
        playlist = await self._resolve(interaction, name, global_mode, "append", owner=owner,
                                       collaborative=collaborative)
        if playlist is None:
            return
        if not playlist.room:
            await self._fail(interaction, f"**{md(playlist.name)}** is full ({MAX_PLAYLIST_SIZE} songs max).")
            return

        try:
            listed = await self._link_songs(song, playlist.room)
            if listed is not None:
                entries, source, cut = listed
                songs = [
                    Song({**e, "requested_by": interaction.user.mention}).to_dict()
                    for e in entries if e.get("title") and e.get("webpage_url")
                ]
                if not songs:
                    await self._fail(interaction, f"Could not find any songs in {source}!")
                    return
                result = await self._save(interaction, playlist, songs)
                if result is None:
                    return
                title, text = self._result_text(result, source, playlist.name)
                if cut:
                    text += "\n-# Only the first part of that list was read."
                await interaction.followup.send(embed=create_embed(title, text, COLOR, self.bot.user))
                return

            info = await self.bot.get_song_info_cached(song)
            if not isinstance(info, dict) or not info.get("webpage_url") or not info.get("title"):
                await self._fail(interaction, "Could not find that song")
                return
            new_song = Song({**info, "requested_by": interaction.user.mention})
            if any(s.get("webpage_url") == new_song.webpage_url for s in playlist.songs):
                await self._fail(interaction, "Song is already in the playlist")
                return
            result = await self._save(interaction, playlist, [new_song.to_dict()])
            if result is None:
                return
            if not result["added"]:
                await self._fail(interaction, "Song is already in the playlist")
                return
            label = playlists.label(global_mode)
            embed = create_embed(
                "Song Added", f"Added {new_song.linked_title} to {label.lower()} **{md(playlist.name)}**", COLOR,
                self.bot.user,
            )
            await interaction.followup.send(embed=embed)
        except Exception:
            logger.exception("Playlist add error")
            await self._fail(interaction, f"Failed to add to {playlists.label(global_mode).lower()}.")

    async def _handle_add_from_queue(
            self, interaction: discord.Interaction, name: str, from_queue: str, global_mode: bool = False,
            collaborative: bool = False, owner: discord.User | None = None,
    ):
        await interaction.response.defer()
        playlist = await self._resolve(interaction, name, global_mode, "append", owner=owner,
                                       collaborative=collaborative)
        if playlist is None:
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)
        session = self.queue_service.whole_queue(guild_data)
        # Older clients sent "current" or "queue_<index>"
        if from_queue == "current" and guild_data.get("current"):
            from_queue = guild_data["current"].webpage_url
        elif from_queue.startswith("queue_") and from_queue[6:].isdigit():
            index = int(from_queue[6:])
            if index < len(guild_data.get("queue", [])):
                from_queue = guild_data["queue"][index].webpage_url
        target = next((song for song in session if song.webpage_url == from_queue), None)
        if target is None:
            await self._fail(interaction, "That song isn't in the queue any more")
            return

        result = await self._save(interaction, playlist, [{**target.to_dict(), "requested_by": interaction.user.mention}])
        if result is None:
            return
        if not result["added"]:
            await self._fail(interaction, "Song is already in the playlist")
            return
        label = playlists.label(global_mode)
        embed = create_embed(
            "Song Added", f"Added {target.linked_title} to {label.lower()} **{md(playlist.name)}**", COLOR, self.bot.user,
        )
        await interaction.followup.send(embed=embed)

    async def _handle_add_session(self, interaction: discord.Interaction, name: str, global_mode: bool = False,
                                  collaborative: bool = False, owner: discord.User | None = None):
        await interaction.response.defer()
        playlist = await self._resolve(interaction, name, global_mode, "append", owner=owner,
                                       collaborative=collaborative)
        if playlist is None:
            return
        songs = self._session_songs(interaction)
        if not songs:
            await self._fail(interaction, "No songs in current session")
            return
        result = await self._save(interaction, playlist, songs)
        if result is None:
            return
        title, text = self._result_text(result, "the current session", playlist.name)
        await interaction.followup.send(embed=create_embed(title, text, COLOR, self.bot.user))

    async def _handle_remove(
            self, interaction: discord.Interaction, name: str, position: int, global_mode: bool = False,
            collaborative: bool = False, owner: discord.User | None = None,
    ):
        playlist = await self._resolve(interaction, name, global_mode, "edit", owner=owner, collaborative=collaborative)
        if playlist is None:
            return
        label = playlists.label(global_mode)
        try:
            async with playlists.editing(self.bot, playlist):
                if not 1 <= position <= len(playlist.songs):
                    raise LookupError(f"Invalid position! {label} has {_plural(len(playlist.songs), 'song')}.")
                removed = playlist.songs.pop(position - 1)
        except LookupError as e:
            await self._fail(interaction, str(e))
            return
        except playlists.PlaylistGone:
            await self._fail(interaction, f"**{md(playlist.name)}** was deleted.")
            return
        embed = create_embed(
            "Song Removed",
            f"Removed **{md(removed.get('title') or 'Unknown')}** from {label.lower()} **{md(playlist.name)}**",
            COLOR, self.bot.user,
        )
        await interaction.response.send_message(embed=embed, silent=True)

    async def _handle_move(
            self, interaction: discord.Interaction, name: str, from_pos: int, to_pos: int,
            global_mode: bool = False, collaborative: bool = False, owner: discord.User | None = None,
    ):
        playlist = await self._resolve(interaction, name, global_mode, "edit", owner=owner, collaborative=collaborative)
        if playlist is None:
            return
        label = playlists.label(global_mode)
        try:
            async with playlists.editing(self.bot, playlist):
                length = len(playlist.songs)
                if not (1 <= from_pos <= length and 1 <= to_pos <= length):
                    raise LookupError(f"Invalid position! {label} has {_plural(length, 'song')}.")
                song = playlist.songs.pop(from_pos - 1)
                playlist.songs.insert(to_pos - 1, song)
        except LookupError as e:
            await self._fail(interaction, str(e))
            return
        except playlists.PlaylistGone:
            await self._fail(interaction, f"**{md(playlist.name)}** was deleted.")
            return
        embed = create_embed(
            "Song Moved",
            f"Moved **{md(song.get('title') or 'Unknown')}**\nFrom position {from_pos} to position {to_pos} "
            f"in {label.lower()} **{md(playlist.name)}**",
            COLOR, self.bot.user,
        )
        await interaction.response.send_message(embed=embed, silent=True)

    async def _handle_load(self, interaction: discord.Interaction, name: str, global_mode: bool = False,
                           collaborative: bool = False, owner: discord.User | None = None):
        music_cog = self._music()
        if not music_cog:
            await self._fail(interaction, "Music commands not loaded")
            return
        if not await music_cog.check_voice_channel(interaction, allow_auto_join=True):
            return
        # Joining voice can take longer than the 3 s an answer is allowed.
        await interaction.response.defer()

        playlist = await self._resolve(interaction, name, global_mode, "view", owner=owner, collaborative=collaborative)
        if playlist is None:
            return
        label = playlists.label(global_mode)
        if not playlist.songs:
            await self._fail(interaction, f"{label} **{md(playlist.name)}** is empty")
            return

        guild_id = interaction.guild.id
        guild_data = self.bot.get_guild_data(guild_id)
        if not guild_data.get("music_channel_id"):
            guild_data["music_channel_id"] = interaction.channel.id
            await self.bot.save_guild_music_channel(guild_id, interaction.channel.id)
        if error := await music_cog.connect_voice(interaction):
            await self._fail(interaction, error)
            return

        seen_urls = get_existing_urls(guild_data)
        room = MAX_QUEUE_SIZE - len(guild_data["queue"])
        loaded = skipped = 0
        for song_info in playlist.songs:
            if not song_info.get("webpage_url") or not song_info.get("title"):
                continue
            if song_info["webpage_url"] in seen_urls:
                skipped += 1
                continue
            if loaded >= room:
                break
            self.queue_service.add_song_to_queue(guild_id, Song({**song_info, "requested_by": interaction.user.mention}))
            seen_urls.add(song_info["webpage_url"])
            loaded += 1

        if not loaded:
            text = "The queue is full!" if room <= 0 else f"No new songs in {label.lower()} **{md(playlist.name)}**"
            await self._fail(interaction, text)
            return

        notes = f"\n({_plural(skipped, 'duplicate')} skipped)" if skipped else ""
        if loaded >= room and loaded + skipped < len(playlist.songs):
            notes += f"\n(the queue is full at {MAX_QUEUE_SIZE} songs)"
        embed = create_embed(
            f"{label} Loaded", f"Loaded **{md(playlist.name)}** with {_plural(loaded, 'song')}{notes}", COLOR,
            self.bot.user,
        )
        await interaction.followup.send(embed=embed, silent=True)

        voice_client = guild_data["voice_client"]
        if voice_client and not voice_client.is_playing() and not guild_data["current"]:
            self.bot.spawn(self.bot.playback.play_next(guild_id))
        guild_data["last_activity"] = time.monotonic()
        await self.bot.save_guild_queue(guild_id)

    async def _handle_show(
            self, interaction: discord.Interaction, name: str, page: int = 1, global_mode: bool = False,
            collaborative: bool = False, owner: discord.User | None = None,
    ):
        playlist = await self._resolve(interaction, name, global_mode, "view", owner=owner, collaborative=collaborative)
        if playlist is None:
            return
        label = "Global Playlist" if global_mode else "Playlist"
        if not playlist.songs:
            embed = create_embed(f"{label}: {md(playlist.name)}", f"{label} is empty", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, silent=True)
            return

        lines = []
        for i, song_info in enumerate(playlist.songs, 1):
            title = md(song_info.get("title") or "Unknown Title")
            url = song_info.get("webpage_url", "")
            title_str = f"**[{title}](<{url}>)**" if url else f"**{title}**"
            lines.append(f"`{i}.` {title_str} by {md(song_info.get('uploader') or 'Unknown')}")
        pages = _pages(f"{label}: {md(playlist.name)}", lines, f"-# {_plural(len(playlist.songs), 'song')}")

        view = PaginationView(pages, interaction.user)
        view.current_page = max(0, min(page, len(pages)) - 1)
        view._rebuild()
        await interaction.response.send_message(view=view, silent=True)
        view.message = await interaction.original_response()

    async def _handle_list(self, interaction: discord.Interaction, global_mode: bool = False):
        label = "Your Global Playlists" if global_mode else "Your Playlists"
        found = await playlists.owned(self.bot, interaction.user.id, self._scope(interaction, global_mode), global_mode)
        if not found:
            empty = "You don't have any global playlists." if global_mode else "You don't have any saved playlists."
            await interaction.response.send_message(view=create_v2_embed(label, empty, COLOR), silent=True)
            return
        lines = [f"• **{md(p.name)}** ({_plural(len(p.songs), 'song')})" for p in found]
        view = PaginationView(_pages(label, lines), interaction.user)
        await interaction.response.send_message(view=view, silent=True)
        view.message = await interaction.original_response()

    async def _handle_delete(self, interaction: discord.Interaction, name: str, global_mode: bool = False):
        playlist = await self._owned(interaction, name, global_mode)
        if playlist is None:
            return
        await playlists.delete(self.bot, playlist)
        label = playlists.label(global_mode)
        embed = create_embed(
            f"{label} Deleted", f"Deleted {label.lower()} **{md(playlist.name)}**.", COLOR, self.bot.user,
        )
        await interaction.response.send_message(embed=embed, silent=True)

    async def _handle_copy(
            self, interaction: discord.Interaction, name: str, target: str, global_mode: bool = False,
            owner: discord.User | None = None,
    ):
        source = await self._resolve(interaction, name, global_mode, "view", owner=owner)
        if source is None:
            return
        if not source.songs:
            await self._fail(interaction, f"**{md(source.name)}** is empty")
            return
        destination = await self._resolve(interaction, target, global_mode, "append")
        if destination is None:
            return
        if destination.id == source.id:
            await self._fail(interaction, "Pick a different playlist to copy into")
            return
        copies = [{**song, "requested_by": interaction.user.mention} for song in source.songs if song.get("webpage_url")]
        result = await self._save(interaction, destination, copies)
        if result is None:
            return
        title, text = self._result_text(result, f"**{md(source.name)}**", destination.name)
        await interaction.response.send_message(embed=create_embed(title, text, COLOR, self.bot.user), silent=True)

    # Collab handler methods

    async def _handle_collab_add(
            self, interaction: discord.Interaction, name: str, user: discord.Member,
            global_mode: bool = False, permission: str = "edit",
    ):
        playlist = await self._owned(interaction, name, global_mode)
        if playlist is None:
            return
        if user.id == interaction.user.id:
            await self._fail(interaction, "You can't add yourself as a collaborator")
            return
        if user.bot:
            await self._fail(interaction, "You can't add a bot as a collaborator")
            return
        if await self.bot.is_collaborator(playlist.id, user.id, global_mode):
            await self._fail(interaction, f"{md(user.display_name)} is already a collaborator")
            return

        await self.bot.add_collaborator(playlist.id, user.id, global_mode, permission)
        label = playlists.label(global_mode)
        embed = create_embed(
            "Collaborator Added",
            f"Added **{md(user.display_name)}** to {label.lower()} **{md(playlist.name)}** with `{permission}` access",
            COLOR, self.bot.user,
        )
        await interaction.response.send_message(embed=embed, silent=True)

    async def _handle_collab_permission(
            self, interaction: discord.Interaction, name: str, user: discord.Member, permission: str,
            global_mode: bool = False,
    ):
        playlist = await self._owned(interaction, name, global_mode)
        if playlist is None:
            return
        if not await self.bot.is_collaborator(playlist.id, user.id, global_mode):
            await self._fail(interaction, f"{md(user.display_name)} is not a collaborator")
            return
        await self.bot.set_collaborator_permission(playlist.id, user.id, permission, global_mode)
        embed = create_embed(
            "Access Changed", f"**{md(user.display_name)}** now has `{permission}` access to **{md(playlist.name)}**",
            COLOR, self.bot.user,
        )
        await interaction.response.send_message(embed=embed, silent=True)

    async def _handle_collab_remove(
            self, interaction: discord.Interaction, name: str, user: discord.Member, global_mode: bool = False,
    ):
        playlist = await self._owned(interaction, name, global_mode)
        if playlist is None:
            return
        if not await self.bot.is_collaborator(playlist.id, user.id, global_mode):
            await self._fail(interaction, f"{md(user.display_name)} is not a collaborator")
            return
        await self.bot.remove_collaborator(playlist.id, user.id, global_mode)
        label = playlists.label(global_mode)
        embed = create_embed(
            "Collaborator Removed",
            f"Removed **{md(user.display_name)}** from {label.lower()} **{md(playlist.name)}**",
            COLOR, self.bot.user,
        )
        await interaction.response.send_message(embed=embed, silent=True)

    def _person_name(self, interaction: discord.Interaction, user_id: int) -> str:
        person = interaction.guild.get_member(user_id) or self.bot.get_user(user_id)
        return md(person.display_name) if person else f"User {user_id}"

    async def _handle_collab_list(self, interaction: discord.Interaction, name: str, global_mode: bool = False):
        playlist = await self._owned(interaction, name, global_mode)
        if playlist is None:
            return
        label = playlists.label(global_mode)
        collaborators = await self.bot.get_collaborators(playlist.id, global_mode)
        if not collaborators:
            view = create_v2_embed(f"{label}: {md(playlist.name)}", "No collaborators", COLOR)
            await interaction.response.send_message(view=view, silent=True)
            return
        lines = [f"- {self._person_name(interaction, uid)} (`{permission}`)" for uid, permission in collaborators]
        view = create_v2_embed(f"Collaborators: {md(playlist.name)}", "\n".join(lines), COLOR)
        await interaction.response.send_message(view=view, silent=True)

    async def _handle_my_collabs(self, interaction: discord.Interaction, global_mode: bool = False):
        """Show all playlists the user is a collaborator on."""
        label = "Your Global Collaborations" if global_mode else "Your Collaborations"
        shared = await playlists.shared_with(
            self.bot, interaction.user.id, self._scope(interaction, global_mode), global_mode,
        )
        if not shared:
            view = create_v2_embed(label, "You're not a collaborator on any playlists.", COLOR)
            await interaction.response.send_message(view=view, ephemeral=True)
            return
        lines = [
            f"• **{md(p.name)}** by {self._person_name(interaction, p.owner_id)} "
            f"({_plural(len(p.songs), 'song')}, `{p.level}`)"
            for p in sorted(shared, key=lambda p: p.name.lower())
        ]
        view = PaginationView(_pages(label, lines), interaction.user)
        await interaction.response.send_message(view=view, ephemeral=True)
        view.message = await interaction.original_response()

    # Server Playlist commands

    playlist_group = app_commands.Group(name="playlist", description="Manage your server playlists")

    @playlist_group.command(name="create", description="Create a new empty playlist")
    @app_commands.describe(name="Name for the playlist")
    async def playlist_create(self, interaction: discord.Interaction, name: str):
        await self._handle_create(interaction, name)

    @playlist_group.command(name="add", description="Add a song or playlist to a playlist")
    @app_commands.describe(
        name="Playlist name", song="Song name, URL, or playlist/album link",
        collaborative="Add to a playlist shared with you", owner="Owner of the shared playlist",
    )
    @app_commands.autocomplete(name=playlist_name_autocomplete)
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def playlist_add(self, interaction: discord.Interaction, name: str, song: str, collaborative: bool = False,
                           owner: discord.User | None = None):
        await self._handle_add(interaction, name, song, collaborative=collaborative, owner=owner)

    @playlist_group.command(name="add-from-queue", description="Add a song from the current queue to a playlist")
    @app_commands.describe(
        name="Playlist name", from_queue="Song from the current session",
        collaborative="Add to a playlist shared with you", owner="Owner of the shared playlist",
    )
    @app_commands.autocomplete(name=playlist_name_autocomplete, from_queue=queue_autocomplete)
    async def playlist_add_from_queue(self, interaction: discord.Interaction, name: str, from_queue: str,
                                      collaborative: bool = False, owner: discord.User | None = None):
        await self._handle_add_from_queue(interaction, name, from_queue, collaborative=collaborative, owner=owner)

    @playlist_group.command(name="add-all-queue", description="Add entire current queue to a playlist")
    @app_commands.describe(name="Playlist name", collaborative="Add to a playlist shared with you",
                           owner="Owner of the shared playlist")
    @app_commands.autocomplete(name=playlist_name_autocomplete)
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def playlist_add_session(self, interaction: discord.Interaction, name: str, collaborative: bool = False,
                                   owner: discord.User | None = None):
        await self._handle_add_session(interaction, name, collaborative=collaborative, owner=owner)

    @playlist_group.command(name="remove", description="Remove a song from a playlist")
    @app_commands.describe(name="Playlist name", position="Position of the song to remove",
                           collaborative="Edit a playlist shared with you", owner="Owner of the shared playlist")
    @app_commands.autocomplete(name=playlist_name_autocomplete)
    async def playlist_remove(self, interaction: discord.Interaction, name: str,
                              position: app_commands.Range[int, 1, MAX_PLAYLIST_SIZE],
                              collaborative: bool = False, owner: discord.User | None = None):
        await self._handle_remove(interaction, name, position, collaborative=collaborative, owner=owner)

    @playlist_group.command(name="move", description="Move a song to a different position in a playlist")
    @app_commands.describe(name="Playlist name", from_pos="Current position", to_pos="New position",
                           collaborative="Edit a playlist shared with you", owner="Owner of the shared playlist")
    @app_commands.autocomplete(name=playlist_name_autocomplete)
    async def playlist_move(self, interaction: discord.Interaction, name: str,
                            from_pos: app_commands.Range[int, 1, MAX_PLAYLIST_SIZE],
                            to_pos: app_commands.Range[int, 1, MAX_PLAYLIST_SIZE],
                            collaborative: bool = False, owner: discord.User | None = None):
        await self._handle_move(interaction, name, from_pos, to_pos, collaborative=collaborative, owner=owner)

    @playlist_group.command(name="load", description="Load a playlist into the queue")
    @app_commands.describe(name="Playlist name", collaborative="Load a playlist shared with you",
                           owner="Owner of the shared playlist")
    @app_commands.autocomplete(name=playlist_name_autocomplete)
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def playlist_load(self, interaction: discord.Interaction, name: str, collaborative: bool = False,
                            owner: discord.User | None = None):
        await self._handle_load(interaction, name, collaborative=collaborative, owner=owner)

    @playlist_group.command(name="show", description="Show songs in a playlist")
    @app_commands.describe(name="Playlist name", page="Page number to view (optional)",
                           collaborative="Show a playlist shared with you", owner="Owner of the shared playlist")
    @app_commands.autocomplete(name=playlist_name_autocomplete)
    async def playlist_show(self, interaction: discord.Interaction, name: str,
                            page: app_commands.Range[int, 1, None] = 1, collaborative: bool = False,
                            owner: discord.User | None = None):
        await self._handle_show(interaction, name, page, collaborative=collaborative, owner=owner)

    @playlist_group.command(name="list", description="List all your playlists")
    async def playlist_list(self, interaction: discord.Interaction):
        await self._handle_list(interaction)

    @playlist_group.command(name="delete", description="Delete a playlist")
    @app_commands.describe(name="Playlist name to delete")
    @app_commands.autocomplete(name=playlist_name_autocomplete)
    async def playlist_delete(self, interaction: discord.Interaction, name: str):
        await self._handle_delete(interaction, name)

    @playlist_group.command(name="copy", description="Copy every song of one playlist into another")
    @app_commands.describe(name="Playlist to copy from", target="Playlist to copy into",
                           owner="Owner of the source playlist, when it is shared with you")
    @app_commands.autocomplete(name=playlist_name_autocomplete, target=playlist_name_autocomplete)
    async def playlist_copy(self, interaction: discord.Interaction, name: str, target: str,
                            owner: discord.User | None = None):
        await self._handle_copy(interaction, name, target, owner=owner)

    @playlist_group.command(name="collab-add", description="Add a collaborator to your playlist")
    @app_commands.describe(name="Playlist name", user="User to add as collaborator", permission="What they may do")
    @app_commands.choices(permission=PERMISSION_CHOICES)
    @app_commands.autocomplete(name=playlist_name_autocomplete)
    async def playlist_collab_add(self, interaction: discord.Interaction, name: str, user: discord.Member,
                                  permission: str = "edit"):
        await self._handle_collab_add(interaction, name, user, permission=permission)

    @playlist_group.command(name="collab-permission", description="Change what a collaborator may do")
    @app_commands.describe(name="Playlist name", user="The collaborator", permission="What they may do")
    @app_commands.choices(permission=PERMISSION_CHOICES)
    @app_commands.autocomplete(name=playlist_name_autocomplete)
    async def playlist_collab_permission(self, interaction: discord.Interaction, name: str, user: discord.Member,
                                         permission: str):
        await self._handle_collab_permission(interaction, name, user, permission)

    @playlist_group.command(name="collab-remove", description="Remove a collaborator from your playlist")
    @app_commands.describe(name="Playlist name", user="User to remove")
    @app_commands.autocomplete(name=playlist_name_autocomplete)
    async def playlist_collab_remove(self, interaction: discord.Interaction, name: str, user: discord.Member):
        await self._handle_collab_remove(interaction, name, user)

    @playlist_group.command(name="collab-list", description="List collaborators on your playlist")
    @app_commands.describe(name="Playlist name")
    @app_commands.autocomplete(name=playlist_name_autocomplete)
    async def playlist_collab_list(self, interaction: discord.Interaction, name: str):
        await self._handle_collab_list(interaction, name)

    @playlist_group.command(name="my-collabs", description="Show playlists you're a collaborator on")
    async def playlist_my_collabs(self, interaction: discord.Interaction):
        await self._handle_my_collabs(interaction)

    # Global Playlist commands

    globalplaylist_group = app_commands.Group(name="globalplaylist", description="Manage your global playlists")

    @globalplaylist_group.command(name="create", description="Create a new global playlist")
    @app_commands.describe(name="Name for the global playlist")
    async def globalplaylist_create(self, interaction: discord.Interaction, name: str):
        await self._handle_create(interaction, name, global_mode=True)

    @globalplaylist_group.command(name="add", description="Add a song or playlist to a global playlist")
    @app_commands.describe(
        name="Global playlist name", song="Song name, URL, or playlist/album link",
        collaborative="Add to a playlist shared with you", owner="Owner of the shared playlist",
    )
    @app_commands.autocomplete(name=global_name_autocomplete)
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def globalplaylist_add(self, interaction: discord.Interaction, name: str, song: str,
                                 collaborative: bool = False, owner: discord.User | None = None):
        await self._handle_add(interaction, name, song, global_mode=True, collaborative=collaborative, owner=owner)

    @globalplaylist_group.command(name="add-from-queue",
                                  description="Add a song from the current queue to a global playlist")
    @app_commands.describe(
        name="Global playlist name", from_queue="Song from the current session",
        collaborative="Add to a playlist shared with you", owner="Owner of the shared playlist",
    )
    @app_commands.autocomplete(name=global_name_autocomplete, from_queue=queue_autocomplete)
    async def globalplaylist_add_from_queue(self, interaction: discord.Interaction, name: str, from_queue: str,
                                            collaborative: bool = False, owner: discord.User | None = None):
        await self._handle_add_from_queue(interaction, name, from_queue, global_mode=True,
                                          collaborative=collaborative, owner=owner)

    @globalplaylist_group.command(name="add-all-queue", description="Add entire current queue to a global playlist")
    @app_commands.describe(name="Global playlist name", collaborative="Add to a playlist shared with you",
                           owner="Owner of the shared playlist")
    @app_commands.autocomplete(name=global_name_autocomplete)
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def globalplaylist_add_session(self, interaction: discord.Interaction, name: str,
                                         collaborative: bool = False, owner: discord.User | None = None):
        await self._handle_add_session(interaction, name, global_mode=True, collaborative=collaborative, owner=owner)

    @globalplaylist_group.command(name="remove", description="Remove a song from a global playlist")
    @app_commands.describe(name="Global playlist name", position="Position of the song to remove",
                           collaborative="Edit a playlist shared with you", owner="Owner of the shared playlist")
    @app_commands.autocomplete(name=global_name_autocomplete)
    async def globalplaylist_remove(self, interaction: discord.Interaction, name: str,
                                    position: app_commands.Range[int, 1, MAX_PLAYLIST_SIZE],
                                    collaborative: bool = False, owner: discord.User | None = None):
        await self._handle_remove(interaction, name, position, global_mode=True, collaborative=collaborative,
                                  owner=owner)

    @globalplaylist_group.command(name="move", description="Move a song to a different position in a global playlist")
    @app_commands.describe(name="Global playlist name", from_pos="Current position", to_pos="New position",
                           collaborative="Edit a playlist shared with you", owner="Owner of the shared playlist")
    @app_commands.autocomplete(name=global_name_autocomplete)
    async def globalplaylist_move(self, interaction: discord.Interaction, name: str,
                                  from_pos: app_commands.Range[int, 1, MAX_PLAYLIST_SIZE],
                                  to_pos: app_commands.Range[int, 1, MAX_PLAYLIST_SIZE],
                                  collaborative: bool = False, owner: discord.User | None = None):
        await self._handle_move(interaction, name, from_pos, to_pos, global_mode=True, collaborative=collaborative,
                                owner=owner)

    @globalplaylist_group.command(name="load", description="Load a global playlist into the queue")
    @app_commands.describe(name="Global playlist name", collaborative="Load a playlist shared with you",
                           owner="Owner of the shared playlist")
    @app_commands.autocomplete(name=global_name_autocomplete)
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def globalplaylist_load(self, interaction: discord.Interaction, name: str, collaborative: bool = False,
                                  owner: discord.User | None = None):
        await self._handle_load(interaction, name, global_mode=True, collaborative=collaborative, owner=owner)

    @globalplaylist_group.command(name="show", description="Show songs in a global playlist")
    @app_commands.describe(name="Global playlist name", page="Page number to view (optional)",
                           collaborative="Show a playlist shared with you", owner="Owner of the shared playlist")
    @app_commands.autocomplete(name=global_name_autocomplete)
    async def globalplaylist_show(self, interaction: discord.Interaction, name: str,
                                  page: app_commands.Range[int, 1, None] = 1, collaborative: bool = False,
                                  owner: discord.User | None = None):
        await self._handle_show(interaction, name, page, global_mode=True, collaborative=collaborative, owner=owner)

    @globalplaylist_group.command(name="list", description="List all your global playlists")
    async def globalplaylist_list(self, interaction: discord.Interaction):
        await self._handle_list(interaction, global_mode=True)

    @globalplaylist_group.command(name="delete", description="Delete a global playlist")
    @app_commands.describe(name="Global playlist name to delete")
    @app_commands.autocomplete(name=global_name_autocomplete)
    async def globalplaylist_delete(self, interaction: discord.Interaction, name: str):
        await self._handle_delete(interaction, name, global_mode=True)

    @globalplaylist_group.command(name="copy", description="Copy every song of one global playlist into another")
    @app_commands.describe(name="Playlist to copy from", target="Playlist to copy into",
                           owner="Owner of the source playlist, when it is shared with you")
    @app_commands.autocomplete(name=global_name_autocomplete, target=global_name_autocomplete)
    async def globalplaylist_copy(self, interaction: discord.Interaction, name: str, target: str,
                                  owner: discord.User | None = None):
        await self._handle_copy(interaction, name, target, global_mode=True, owner=owner)

    @globalplaylist_group.command(name="collab-add", description="Add a collaborator to your global playlist")
    @app_commands.describe(name="Global playlist name", user="User to add as collaborator",
                           permission="What they may do")
    @app_commands.choices(permission=PERMISSION_CHOICES)
    @app_commands.autocomplete(name=global_name_autocomplete)
    async def globalplaylist_collab_add(self, interaction: discord.Interaction, name: str, user: discord.Member,
                                        permission: str = "edit"):
        await self._handle_collab_add(interaction, name, user, global_mode=True, permission=permission)

    @globalplaylist_group.command(name="collab-permission", description="Change what a collaborator may do")
    @app_commands.describe(name="Global playlist name", user="The collaborator", permission="What they may do")
    @app_commands.choices(permission=PERMISSION_CHOICES)
    @app_commands.autocomplete(name=global_name_autocomplete)
    async def globalplaylist_collab_permission(self, interaction: discord.Interaction, name: str,
                                               user: discord.Member, permission: str):
        await self._handle_collab_permission(interaction, name, user, permission, global_mode=True)

    @globalplaylist_group.command(name="collab-remove", description="Remove a collaborator from your global playlist")
    @app_commands.describe(name="Global playlist name", user="User to remove")
    @app_commands.autocomplete(name=global_name_autocomplete)
    async def globalplaylist_collab_remove(self, interaction: discord.Interaction, name: str, user: discord.Member):
        await self._handle_collab_remove(interaction, name, user, global_mode=True)

    @globalplaylist_group.command(name="collab-list", description="List collaborators on your global playlist")
    @app_commands.describe(name="Global playlist name")
    @app_commands.autocomplete(name=global_name_autocomplete)
    async def globalplaylist_collab_list(self, interaction: discord.Interaction, name: str):
        await self._handle_collab_list(interaction, name, global_mode=True)

    @globalplaylist_group.command(name="my-collabs", description="Show global playlists you're a collaborator on")
    async def globalplaylist_my_collabs(self, interaction: discord.Interaction):
        await self._handle_my_collabs(interaction, global_mode=True)

    # ── History commands ────────────────────────────────────────────────

    history_group = app_commands.Group(name="history", description="Manage song history")

    @history_group.command(name="show", description="Show recently played songs")
    @app_commands.describe(page="Page number to view (optional)")
    async def history_show(self, interaction: discord.Interaction, page: app_commands.Range[int, 1, None] = 1):
        history = self.bot.get_guild_data(interaction.guild.id)["history"]
        if not history:
            embed = create_embed("History", "No songs in history yet", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, silent=True)
            return

        # Newest first, numbered the way /history play takes them
        lines = [f"`{i}.` {song}" for i, song in enumerate(reversed(history), 1)]
        footer = (
            f"-# {_plural(len(history), 'song')} total\n"
            "-# Use /history play <number> to replay a song or /history add_all to add all songs"
        )
        pages = _pages("Recent History", lines, footer)
        view = PaginationView(pages, interaction.user)
        view.current_page = max(0, min(page, len(pages)) - 1)
        view._rebuild()
        await interaction.response.send_message(view=view, silent=True)
        view.message = await interaction.original_response()

    @history_group.command(name="play", description="Play a song from history by number")
    @app_commands.describe(song_number="Song number from /history show (1 is the most recent)")
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def history_play(self, interaction: discord.Interaction, song_number: app_commands.Range[int, 1, None]):
        guild_id = interaction.guild.id
        guild_data = self.bot.get_guild_data(guild_id)
        history = guild_data["history"]
        if not history:
            embed = create_embed("History", "No songs in history yet", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, silent=True)
            return

        music_cog = self._music()
        if not music_cog:
            await self._fail(interaction, "Music commands not loaded")
            return
        if not await music_cog.check_voice_channel(interaction, allow_auto_join=True):
            return
        if song_number > len(history):
            await self._fail(interaction, f"Invalid history position! History has {_plural(len(history), 'song')}.")
            return

        selected_song = history[-song_number]
        if guild_data.get("current") and selected_song.webpage_url == guild_data["current"].webpage_url:
            await self._fail(interaction, "This song is currently playing")
            return
        for i, song in enumerate(guild_data.get("queue", []), 1):
            if song.webpage_url == selected_song.webpage_url:
                await self._fail(interaction, f"This song is already in queue at position {i}")
                return
        if len(guild_data["queue"]) >= MAX_QUEUE_SIZE:
            await self._fail(interaction, f"The queue is full ({MAX_QUEUE_SIZE} songs).")
            return

        # Joining voice can take longer than the 3 s an answer is allowed.
        await interaction.response.defer()
        if error := await music_cog.connect_voice(interaction):
            await self._fail(interaction, error)
            return

        song_copy = Song({**selected_song.to_dict(), "requested_by": interaction.user.mention})
        self.queue_service.add_song_to_queue(guild_id, song_copy)

        voice_client = guild_data.get("voice_client")
        if voice_client and not voice_client.is_playing() and not guild_data.get("current"):
            self.bot.spawn(self.bot.playback.play_next(guild_id))
            embed = create_embed("Now Playing from History", str(song_copy), COLOR, self.bot.user)
        else:
            embed = create_embed(
                "Added from History", f"{song_copy}\n\nPosition in queue: {len(guild_data['queue'])}", COLOR,
                self.bot.user,
            )
        if song_copy.thumbnail:
            embed.set_thumbnail(url=song_copy.thumbnail)

        await interaction.followup.send(embed=embed, silent=True)
        guild_data["last_activity"] = time.monotonic()
        await self.bot.save_guild_queue(guild_id)

    @history_group.command(name="add_all", description="Add all songs from history to the queue")
    @app_commands.checks.cooldown(1, COMMAND_COOLDOWN)
    async def history_add_all(self, interaction: discord.Interaction):
        music_cog = self._music()
        if not music_cog:
            await self._fail(interaction, "Music commands not loaded")
            return
        if not await music_cog.check_voice_channel(interaction, allow_auto_join=True):
            return

        guild_id = interaction.guild.id
        guild_data = self.bot.get_guild_data(guild_id)
        if not guild_data["history"]:
            embed = create_embed("History", "No songs in history yet", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, silent=True)
            return

        await interaction.response.defer()
        if error := await music_cog.connect_voice(interaction):
            await self._fail(interaction, error)
            return

        existing_urls = get_existing_urls(guild_data)
        room = MAX_QUEUE_SIZE - len(guild_data["queue"])
        added = skipped = 0
        for history_song in guild_data["history"]:
            if history_song.webpage_url in existing_urls:
                skipped += 1
                continue
            if added >= room:
                break
            self.queue_service.add_song_to_queue(
                guild_id, Song({**history_song.to_dict(), "requested_by": interaction.user.mention}),
            )
            existing_urls.add(history_song.webpage_url)
            added += 1

        if not added:
            text = "The queue is full!" if room <= 0 else "All history songs are already in queue or currently playing"
            embed = create_embed("No Songs Added", text, COLOR, self.bot.user)
        else:
            text = f"Added {_plural(added, 'song')} from history to queue"
            if skipped:
                text += f"\nSkipped {_plural(skipped, 'duplicate')}."
            embed = create_embed("History Added to Queue", text, COLOR, self.bot.user)
        await interaction.followup.send(embed=embed, silent=True)

        voice_client = guild_data["voice_client"]
        if voice_client and not voice_client.is_playing() and not guild_data["current"]:
            self.bot.spawn(self.bot.playback.play_next(guild_id))
        guild_data["last_activity"] = time.monotonic()
        await self.bot.save_guild_queue(guild_id)

    @history_group.command(name="clear", description="Clear history songs")
    async def history_clear(self, interaction: discord.Interaction):
        music_cog = self._music()
        # Clearing history is a DJ action, as in the Activity.
        if music_cog and not await music_cog._check_dj(interaction):
            return
        guild_data = self.bot.get_guild_data(interaction.guild.id)
        if not guild_data["history"]:
            await self._fail(interaction, "History already empty!")
            return

        guild_data["history"].clear()
        embed = create_embed("History cleared", "Removed all songs from history", COLOR, self.bot.user)
        await interaction.response.send_message(embed=embed, silent=True)
        await self.bot.save_guild_queue(interaction.guild.id)

