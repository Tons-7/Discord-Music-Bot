import json
import logging
from datetime import datetime
from typing import List

import discord
from discord.ext import commands

from config import COLOR, MAX_PLAYLIST_SIZE, SONGS_PER_PAGE
from models.song import Song
from utils.helpers import get_existing_urls, interaction_check, create_embed
from views.pagination import PaginationView

logger = logging.getLogger(__name__)


class PlaylistCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.queue_service = bot._playback_service.queue_service
        self.music_service = bot._music_service
        super().__init__()

    # ── DB helpers ──────────────────────────────────────────────────────

    async def _playlist_exists(self, user_id: int, name: str, guild_id: int = None) -> bool:
        if guild_id is not None:
            result = await self.bot.fetch_db_query(
                "SELECT id FROM playlists WHERE user_id = ? AND guild_id = ? AND name = ?",
                (user_id, guild_id, name),
            )
        else:
            result = await self.bot.fetch_db_query(
                "SELECT id FROM global_playlists WHERE user_id = ? AND name = ?",
                (user_id, name),
            )
        return bool(result)

    async def _create_playlist_row(self, user_id: int, name: str, guild_id: int = None):
        if guild_id is not None:
            await self.bot.execute_db_query(
                "INSERT INTO playlists (user_id, guild_id, name, songs) VALUES (?, ?, ?, ?)",
                (user_id, guild_id, name, json.dumps([])),
            )
        else:
            await self.bot.execute_db_query(
                "INSERT INTO global_playlists (user_id, name, songs) VALUES (?, ?, ?)",
                (user_id, name, json.dumps([])),
            )

    async def _save_songs(self, user_id: int, name: str, songs: list, guild_id: int = None):
        songs_json = json.dumps(songs)
        if guild_id is not None:
            await self.bot.execute_db_query(
                "UPDATE playlists SET songs = ? WHERE user_id = ? AND guild_id = ? AND name = ?",
                (songs_json, user_id, guild_id, name),
            )
        else:
            await self.bot.execute_db_query(
                "UPDATE global_playlists SET songs = ? WHERE user_id = ? AND name = ?",
                (songs_json, user_id, name),
            )

    async def _delete_playlist_row(self, user_id: int, name: str, guild_id: int = None):
        if guild_id is not None:
            await self.bot.execute_db_query(
                "DELETE FROM playlists WHERE user_id = ? AND guild_id = ? AND name = ?",
                (user_id, guild_id, name),
            )
        else:
            await self.bot.execute_db_query(
                "DELETE FROM global_playlists WHERE user_id = ? AND name = ?",
                (user_id, name),
            )

    async def _list_playlists_from_db(self, user_id: int, guild_id: int = None):
        if guild_id is not None:
            return await self.bot.fetch_db_query(
                "SELECT name, songs, created_at FROM playlists WHERE user_id = ? AND guild_id = ? ORDER BY created_at DESC",
                (user_id, guild_id),
            )
        return await self.bot.fetch_db_query(
            "SELECT name, songs, created_at FROM global_playlists WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        )

    # Collaborative helpers

    async def _get_playlist_id_and_owner(
            self, name: str, user_id: int, guild_id: int = None, *, collab_only: bool = False
    ) -> tuple[int | None, int | None]:
        """Return (playlist_id, owner_id) for a playlist the user owns OR collaborates on.
        If collab_only=True, skip the ownership check — only search collab access."""
        is_global = guild_id is None

        # Check if user owns it (unless caller wants collab-only)
        if not collab_only:
            pid = await self.bot.get_playlist_id(user_id, name, guild_id)
            if pid is not None:
                return pid, user_id

        # Search for a playlist with this name where user is a collaborator
        if is_global:
            rows = await self.bot.fetch_db_query(
                "SELECT gp.id, gp.user_id FROM global_playlists gp "
                "JOIN playlist_collaborators pc ON pc.playlist_id = gp.id AND pc.is_global = 1 "
                "WHERE gp.name = ? AND pc.user_id = ?",
                (name, user_id),
            )
        else:
            rows = await self.bot.fetch_db_query(
                "SELECT p.id, p.user_id FROM playlists p "
                "JOIN playlist_collaborators pc ON pc.playlist_id = p.id AND pc.is_global = 0 "
                "WHERE p.name = ? AND p.guild_id = ? AND pc.user_id = ?",
                (name, guild_id, user_id),
            )
        if rows:
            return rows[0][0], rows[0][1]
        return None, None

    async def _get_collab_playlist_songs(
            self, interaction: discord.Interaction, name: str,
            *, use_followup: bool, global_mode: bool = False, collab_only: bool = False,
    ) -> tuple[list[dict] | None, int | None, int | None]:
        """Like _get_playlist_songs but also checks collaborator access.
        If collab_only=True, skip ownership check — only find playlists where user is a collaborator.
        Returns (songs, owner_id, playlist_id) or (None, None, None) on failure."""
        send = interaction.followup.send if use_followup else interaction.response.send_message
        label = "Global playlist" if global_mode else "Playlist"
        guild_id = None if global_mode else interaction.guild.id

        pid, owner_id = await self._get_playlist_id_and_owner(
            name, interaction.user.id, guild_id, collab_only=collab_only
        )
        if pid is None:
            embed = create_embed("Error", f"{label} **{name}** not found", COLOR, self.bot.user)
            await send(embed=embed, ephemeral=True)
            return None, None, None

        if guild_id is not None:
            result = await self.bot.fetch_db_query(
                "SELECT songs FROM playlists WHERE id = ?", (pid,)
            )
        else:
            result = await self.bot.fetch_db_query(
                "SELECT songs FROM global_playlists WHERE id = ?", (pid,)
            )

        if not result:
            embed = create_embed("Error", f"{label} **{name}** not found", COLOR, self.bot.user)
            await send(embed=embed, ephemeral=True)
            return None, None, None

        try:
            songs_json = result[0][0]
            return (json.loads(songs_json) if songs_json else [], owner_id, pid)
        except (json.JSONDecodeError, IndexError, KeyError) as e:
            logger.error(f"Error parsing playlist data: {e}")
            embed = create_embed("Error", f"{label} data is corrupted", COLOR, self.bot.user)
            await send(embed=embed)
            return None, None, None

    async def _save_songs_by_id(self, playlist_id: int, songs: list, global_mode: bool = False):
        """Save songs to a playlist by its row ID."""
        songs_json = json.dumps(songs)
        if global_mode:
            await self.bot.execute_db_query(
                "UPDATE global_playlists SET songs = ? WHERE id = ?",
                (songs_json, playlist_id),
            )
        else:
            await self.bot.execute_db_query(
                "UPDATE playlists SET songs = ? WHERE id = ?",
                (songs_json, playlist_id),
            )

    # Shared helpers

    async def _get_music_cog(self, interaction: discord.Interaction):
        music_cog = self.bot.get_cog("MusicCommands")
        if not music_cog:
            embed = create_embed("Error", "Music commands not loaded", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return None
        return music_cog

    async def _get_playlist_songs(
            self,
            interaction: discord.Interaction,
            name: str,
            *,
            use_followup: bool,
            ephemeral_not_found: bool = True,
            global_mode: bool = False,
    ) -> List[dict] | None:

        send = interaction.followup.send if use_followup else interaction.response.send_message
        label = "Global playlist" if global_mode else "Playlist"

        if global_mode:
            result = await self.bot.fetch_db_query(
                "SELECT songs FROM global_playlists WHERE user_id = ? AND name = ?",
                (interaction.user.id, name),
            )
        else:
            result = await self.bot.fetch_db_query(
                "SELECT songs FROM playlists WHERE user_id = ? AND guild_id = ? AND name = ?",
                (interaction.user.id, interaction.guild.id, name),
            )

        if not result or len(result) == 0 or len(result[0]) == 0:
            embed = create_embed(
                "Error", f"{label} **{name}** not found", COLOR, self.bot.user
            )
            await send(embed=embed, ephemeral=ephemeral_not_found)
            return None

        try:
            songs_json = result[0][0]
            return json.loads(songs_json) if songs_json else []
        except (json.JSONDecodeError, IndexError, KeyError) as e:
            logger.error(f"Error parsing playlist data: {e}")
            embed = create_embed("Error", f"{label} data is corrupted", COLOR, self.bot.user)
            await send(embed=embed)
            return None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await interaction_check(self, interaction)

    async def queue_autocomplete(
            self, interaction: discord.Interaction, current: str
    ) -> List[discord.app_commands.Choice]:
        guild_data = self.bot.get_guild_data(interaction.guild.id)
        choices = []

        if guild_data.get("current"):
            current_song = guild_data["current"]
            choice_name = f"Now Playing: {current_song.title[:60]}"
            if len(choice_name) > 80:
                choice_name = choice_name[:77] + "..."
            choices.append(
                discord.app_commands.Choice(name=choice_name, value="current")
            )

        for i, song in enumerate(guild_data.get("queue", [])[:20]):
            choice_name = f"Queue #{i + 1}: {song.title[:60]}"
            if len(choice_name) > 80:
                choice_name = choice_name[:77] + "..."
            choices.append(
                discord.app_commands.Choice(name=choice_name, value=f"queue_{i}")
            )

        if current:
            choices = [
                choice for choice in choices if current.lower() in choice.name.lower()
            ]

        return choices[:25]

    # ── Handler methods ─────────────────────────────────────────────────

    async def _handle_create(self, interaction: discord.Interaction, name: str, global_mode: bool = False):
        label = "Global playlist" if global_mode else "Playlist"
        guild_id = None if global_mode else interaction.guild.id

        if len(name) > 50:
            embed = create_embed(
                "Error", "Playlist name must be 50 characters or less.", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        try:
            exists = await self._playlist_exists(interaction.user.id, name, guild_id)
            if exists:
                embed = create_embed(
                    "Error", f"{label} **{name}** already exists", COLOR, self.bot.user
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            await self._create_playlist_row(interaction.user.id, name, guild_id)

            embed = create_embed(
                f"{label} Created", f"Created empty {label.lower()} **{name}**", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed, silent=True)

        except Exception as e:
            logger.error(f"Playlist create error: {e}")
            embed = create_embed("Error", f"Failed to create {label.lower()}.", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed)

    async def _handle_add(self, interaction: discord.Interaction, name: str, song: str, global_mode: bool = False,
                          collaborative: bool = False):
        label = "Global playlist" if global_mode else "Playlist"
        guild_id = None if global_mode else interaction.guild.id
        await interaction.response.defer()

        try:
            # Check owner or collaborator access
            existing_songs, owner_id, pid = await self._get_collab_playlist_songs(
                interaction, name, use_followup=True, global_mode=global_mode, collab_only=collaborative
            )
            if existing_songs is None:
                return

            is_youtube_playlist = "playlist" in song.lower() and "youtube.com" in song.lower()
            is_spotify_playlist = "playlist" in song.lower() and "spotify.com" in song.lower()
            is_spotify_album = "album" in song.lower() and "spotify.com" in song.lower()

            if is_youtube_playlist:
                youtube_songs = await self.music_service.handle_youtube_playlist(song)

                if not youtube_songs:
                    embed = create_embed(
                        "Error", "Could not process playlist!", COLOR, self.bot.user
                    )
                    await interaction.followup.send(embed=embed)
                    return

                existing_urls = {s.get("webpage_url") for s in existing_songs}
                songs_to_add = []
                added_count = 0
                skipped_count = 0

                for song_info in youtube_songs:
                    song_url = song_info.get("webpage_url")
                    if not song_url:
                        continue

                    if song_url not in existing_urls:
                        if len(existing_songs) + len(songs_to_add) >= MAX_PLAYLIST_SIZE:
                            break
                        songs_to_add.append(song_info)
                        existing_urls.add(song_url)
                        added_count += 1
                    else:
                        skipped_count += 1

                if not songs_to_add:
                    embed = create_embed(
                        "No Songs Added",
                        f"All songs from the playlist are already in **{name}**" + (
                            f"\n({skipped_count} songs skipped)" if skipped_count > 0 else ""
                        ),
                        COLOR,
                        self.bot.user
                    )
                    await interaction.followup.send(embed=embed)
                    return

                for song_info in songs_to_add:
                    new_song = Song(song_info)
                    new_song.requested_by = interaction.user.mention
                    existing_songs.append(new_song.to_dict())

                await self._save_songs_by_id(pid, existing_songs, global_mode)

                embed = create_embed(
                    "Playlist Added",
                    f"Added **{added_count}** song{'s' if added_count != 1 else ''} from the playlist to **{name}**" + (
                        f"\n({skipped_count} duplicate{'s' if skipped_count != 1 else ''} skipped)" if skipped_count > 0 else ""
                    ),
                    COLOR,
                    self.bot.user
                )
                await interaction.followup.send(embed=embed)
                return

            elif is_spotify_playlist or is_spotify_album:
                spotify_songs = await self.bot.get_song_info_cached(song)

                if not spotify_songs or not isinstance(spotify_songs, list):
                    embed = create_embed(
                        "Error", "Could not process Spotify playlist/album!", COLOR, self.bot.user
                    )
                    await interaction.followup.send(embed=embed)
                    return

                existing_urls = {s.get("webpage_url") for s in existing_songs}
                songs_to_add = []
                added_count = 0
                skipped_count = 0

                for song_info in spotify_songs:
                    song_url = song_info.get("webpage_url")
                    if not song_url or not song_info.get("title"):
                        continue

                    if song_url not in existing_urls:
                        if len(existing_songs) + len(songs_to_add) >= MAX_PLAYLIST_SIZE:
                            break
                        songs_to_add.append(song_info)
                        existing_urls.add(song_url)
                        added_count += 1
                    else:
                        skipped_count += 1

                if not songs_to_add:
                    embed = create_embed(
                        "No Songs Added",
                        f"All songs from the Spotify {'playlist' if is_spotify_playlist else 'album'} are already in **{name}**" + (
                            f"\n({skipped_count} songs skipped)" if skipped_count > 0 else ""
                        ),
                        COLOR,
                        self.bot.user
                    )
                    await interaction.followup.send(embed=embed)
                    return

                for song_info in songs_to_add:
                    new_song = Song(song_info)
                    new_song.requested_by = interaction.user.mention
                    existing_songs.append(new_song.to_dict())

                await self._save_songs_by_id(pid, existing_songs, global_mode)

                embed = create_embed(
                    "Spotify Playlist/Album Added",
                    f"Added **{added_count}** song{'s' if added_count != 1 else ''} from the Spotify {'playlist' if is_spotify_playlist else 'album'} to **{name}**" + (
                        f"\n({skipped_count} duplicate{'s' if skipped_count != 1 else ''} skipped)" if skipped_count > 0 else ""
                    ),
                    COLOR,
                    self.bot.user
                )
                await interaction.followup.send(embed=embed)
                return

            song_info = await self.bot.get_song_info_cached(song)
            if not song_info or not song_info.get("webpage_url"):
                embed = create_embed(
                    "Error", "Could not find that song", COLOR, self.bot.user
                )
                await interaction.followup.send(embed=embed)
                return

            song_url = song_info["webpage_url"]
            if any(s.get("webpage_url") == song_url for s in existing_songs):
                embed = create_embed(
                    "Error", "Song is already in the playlist", COLOR, self.bot.user
                )
                await interaction.followup.send(embed=embed)
                return

            if len(existing_songs) >= MAX_PLAYLIST_SIZE:
                embed = create_embed(
                    "Error", f"Playlist is full! Maximum {MAX_PLAYLIST_SIZE} songs allowed.", COLOR, self.bot.user
                )
                await interaction.followup.send(embed=embed)
                return

            new_song = Song(song_info)
            new_song.requested_by = interaction.user.mention
            existing_songs.append(new_song.to_dict())

            await self._save_songs_by_id(pid, existing_songs, global_mode)

            embed = create_embed(
                "Song Added",
                f"Added **{new_song.title}** to {label.lower()} **{name}**",
                COLOR,
                self.bot.user
            )
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"Playlist add error: {e}")
            embed = create_embed("Error", f"Failed to add to {label.lower()}.", COLOR, self.bot.user)
            await interaction.followup.send(embed=embed)

    async def _handle_add_from_queue(
            self, interaction: discord.Interaction, name: str, from_queue: str, global_mode: bool = False,
            collaborative: bool = False
    ):
        label = "Global playlist" if global_mode else "Playlist"
        guild_id = None if global_mode else interaction.guild.id
        await interaction.response.defer()

        try:
            existing_songs, owner_id, pid = await self._get_collab_playlist_songs(
                interaction, name, use_followup=True, global_mode=global_mode, collab_only=collaborative
            )
            if existing_songs is None:
                return

            guild_data = self.bot.get_guild_data(interaction.guild.id)
            target_song = None

            if from_queue == "current" and guild_data.get("current"):
                target_song = guild_data["current"]
            elif from_queue.startswith("queue_"):
                try:
                    queue_index = int(from_queue.split("_")[1])
                    if 0 <= queue_index < len(guild_data.get("queue", [])):
                        target_song = guild_data["queue"][queue_index]
                except (ValueError, IndexError):
                    pass

            if not target_song:
                embed = create_embed(
                    "Error", "Selected song not found in current session", COLOR, self.bot.user
                )
                await interaction.followup.send(embed=embed)
                return

            if any(
                    s.get("webpage_url") == target_song.webpage_url
                    for s in existing_songs
            ):
                embed = create_embed(
                    "Error", "Song is already in the playlist", COLOR, self.bot.user
                )
                await interaction.followup.send(embed=embed)
                return

            if len(existing_songs) >= MAX_PLAYLIST_SIZE:
                embed = create_embed(
                    "Error", f"Playlist is full! Maximum {MAX_PLAYLIST_SIZE} songs allowed.", COLOR, self.bot.user
                )
                await interaction.followup.send(embed=embed)
                return

            song_copy = Song.from_dict(target_song.to_dict())
            song_copy.requested_by = interaction.user.mention
            existing_songs.append(song_copy.to_dict())

            await self._save_songs_by_id(pid, existing_songs, global_mode)

            embed = create_embed(
                "Song Added",
                f"Added **{target_song.title}** to {label.lower()} **{name}**",
                COLOR,
                self.bot.user
            )
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"Playlist add from queue error: {e}")
            embed = create_embed("Error", f"Failed to add to {label.lower()}.", COLOR, self.bot.user)
            await interaction.followup.send(embed=embed)

    async def _handle_add_session(self, interaction: discord.Interaction, name: str, global_mode: bool = False,
                                  collaborative: bool = False):
        label = "Global playlist" if global_mode else "Playlist"
        guild_id = None if global_mode else interaction.guild.id
        await interaction.response.defer()

        try:
            existing_songs, owner_id, pid = await self._get_collab_playlist_songs(
                interaction, name, use_followup=True, global_mode=global_mode, collab_only=collaborative
            )
            if existing_songs is None:
                return

            guild_data = self.bot.get_guild_data(interaction.guild.id)
            queue_items = []
            current_dict = None
            seen_urls = set()

            if guild_data.get("current"):
                current_song = guild_data["current"]
                current_dict = current_song.to_dict()
                seen_urls.add(current_song.webpage_url)

            for queue_song in guild_data.get("queue", []):
                if queue_song.webpage_url not in seen_urls:
                    queue_items.append(queue_song.to_dict())
                    seen_urls.add(queue_song.webpage_url)

            if not current_dict and not queue_items:
                embed = create_embed(
                    "Error", "No songs in current session", COLOR, self.bot.user
                )
                await interaction.followup.send(embed=embed)
                return

            existing_urls = {s.get("webpage_url") for s in existing_songs}
            songs_to_add = []

            if current_dict:
                current_url = current_dict.get("webpage_url")
                if current_url not in existing_urls:
                    songs_to_add.append(current_dict)
                    existing_urls.add(current_url)

            for song_info in queue_items:
                song_url = song_info.get("webpage_url")
                if song_url and song_url not in existing_urls:
                    songs_to_add.append(song_info)
                    existing_urls.add(song_url)

            if not songs_to_add:
                embed = create_embed(
                    "Error",
                    f"All songs from current session are already in the {label.lower()}",
                    COLOR,
                    self.bot.user
                )
                await interaction.followup.send(embed=embed)
                return

            if len(existing_songs) + len(songs_to_add) > MAX_PLAYLIST_SIZE:
                max_can_add = MAX_PLAYLIST_SIZE - len(existing_songs)
                songs_to_add = songs_to_add[:max_can_add]
                embed = create_embed(
                    "Partial Add",
                    f"Added {len(songs_to_add)} songs to {label.lower()} **{name}**\n({label} size limit reached)",
                    COLOR,
                    self.bot.user
                )
            else:
                embed = create_embed(
                    "Session Added",
                    f"Added {len(songs_to_add)} songs from current session to {label.lower()} **{name}**",
                    COLOR,
                    self.bot.user
                )

            for song_info in songs_to_add:
                song_info["requested_by"] = interaction.user.mention

            existing_songs.extend(songs_to_add)

            await self._save_songs_by_id(pid, existing_songs, global_mode)

            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"Playlist add session error: {e}")
            embed = create_embed("Error", f"Failed to add session to {label.lower()}.", COLOR, self.bot.user)
            await interaction.followup.send(embed=embed)

    async def _handle_remove(
            self, interaction: discord.Interaction, name: str, position: int, global_mode: bool = False,
            collaborative: bool = False
    ):
        label = "Global playlist" if global_mode else "Playlist"
        guild_id = None if global_mode else interaction.guild.id

        try:
            playlist_items, owner_id, pid = await self._get_collab_playlist_songs(
                interaction, name, use_followup=False, global_mode=global_mode, collab_only=collaborative
            )
            if playlist_items is None:
                return

            if not playlist_items:
                embed = create_embed("Error", f"{label} is empty", COLOR, self.bot.user)
                await interaction.response.send_message(embed=embed)
                return

            if position < 1 or position > len(playlist_items):
                embed = create_embed(
                    "Error",
                    f"Invalid position! {label} has {len(playlist_items)} songs.",
                    COLOR,
                    self.bot.user
                )
                await interaction.response.send_message(embed=embed)
                return

            removed_song = playlist_items.pop(position - 1)

            await self._save_songs_by_id(pid, playlist_items, global_mode)

            embed = create_embed(
                "Song Removed",
                f"Removed **{removed_song.get('title', 'Unknown')}** from {label.lower()} **{name}**",
                COLOR,
                self.bot.user
            )
            await interaction.response.send_message(embed=embed, silent=True)

        except Exception as e:
            logger.error(f"Playlist remove error: {e}")
            embed = create_embed(
                "Error", f"Failed to remove song from {label.lower()}.", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed)

    async def _handle_move(
            self,
            interaction: discord.Interaction,
            name: str,
            from_pos: int,
            to_pos: int,
            global_mode: bool = False,
            collaborative: bool = False,
    ):
        label = "Global playlist" if global_mode else "Playlist"
        guild_id = None if global_mode else interaction.guild.id

        try:
            playlist_items, owner_id, pid = await self._get_collab_playlist_songs(
                interaction, name, use_followup=False, global_mode=global_mode, collab_only=collaborative
            )
            if playlist_items is None:
                return

            if not playlist_items:
                embed = create_embed("Error", f"{label} is empty", COLOR, self.bot.user)
                await interaction.response.send_message(embed=embed)
                return

            length = len(playlist_items)
            if (
                    from_pos < 1
                    or from_pos > length
                    or to_pos < 1
                    or to_pos > length
            ):
                embed = create_embed(
                    "Error",
                    f"Invalid position! {label} has {length} songs.",
                    COLOR,
                    self.bot.user
                )
                await interaction.response.send_message(embed=embed)
                return

            from_index = from_pos - 1
            to_index = to_pos - 1

            song = playlist_items.pop(from_index)
            playlist_items.insert(to_index, song)

            await self._save_songs_by_id(pid, playlist_items, global_mode)

            title = song.get("title") or "Unknown"
            embed = create_embed(
                "Song Moved",
                f"Moved **{title}**\nFrom position {from_pos} to position {to_pos} in {label.lower()} **{name}**",
                COLOR,
                self.bot.user
            )
            await interaction.response.send_message(embed=embed, silent=True)

        except Exception as e:
            logger.error(f"Playlist move error: {e}")
            embed = create_embed(
                "Error", f"Failed to move song in {label.lower()}.", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed)

    async def _handle_load(self, interaction: discord.Interaction, name: str, global_mode: bool = False,
                           collaborative: bool = False):
        label = "Global playlist" if global_mode else "Playlist"

        music_cog = await self._get_music_cog(interaction)
        if not music_cog:
            return

        if not await music_cog.check_voice_channel(interaction, allow_auto_join=True):
            return

        try:
            playlist_items, _, _ = await self._get_collab_playlist_songs(
                interaction, name, use_followup=False, global_mode=global_mode, collab_only=collaborative
            )
            if playlist_items is None:
                return

            if not playlist_items:
                embed = create_embed(
                    "Error", f"{label} **{name}** is empty", COLOR, self.bot.user
                )
                await interaction.response.send_message(embed=embed)
                return

            guild_data = self.bot.get_guild_data(interaction.guild.id)

            if not guild_data.get("music_channel_id"):
                guild_data["music_channel_id"] = interaction.channel.id
                await self.bot.save_guild_music_channel(
                    interaction.guild.id, interaction.channel.id
                )

            if not await music_cog.ensure_voice_connection(interaction):
                embed = create_embed(
                    "Error", "Failed to connect to voice channel", COLOR, self.bot.user
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            loaded_count = 0
            skipped_count = 0
            seen_urls = get_existing_urls(guild_data)

            for song_info in playlist_items:
                try:
                    if not song_info.get("webpage_url") or not song_info.get("title"):
                        continue

                    if song_info["webpage_url"] in seen_urls:
                        skipped_count += 1
                        continue

                    song = Song.from_dict(song_info)
                    song.requested_by = interaction.user.mention

                    self.queue_service.add_song_to_queue(interaction.guild.id, song)
                    seen_urls.add(song_info["webpage_url"])
                    loaded_count += 1

                except Exception as e:
                    logger.warning(f"Skipped invalid song data: {e}")
                    continue

            if loaded_count == 0:
                embed = create_embed(
                    "Error", f"No valid songs found in {label.lower()}", COLOR, self.bot.user
                )
                await interaction.response.send_message(embed=embed)
                return

            skip_note = f"\n({skipped_count} duplicate{'s' if skipped_count != 1 else ''} skipped)" if skipped_count > 0 else ""
            embed = create_embed(
                f"{label} Loaded",
                f"Loaded **{name}** with {loaded_count} songs{skip_note}",
                COLOR,
                self.bot.user
            )
            await interaction.response.send_message(embed=embed, silent=True)

            playback_service = self.bot._playback_service

            if (
                    guild_data["voice_client"]
                    and not guild_data["voice_client"].is_playing()
                    and not guild_data["current"]
            ):
                await playback_service.play_next(interaction.guild.id)

            guild_data["last_activity"] = datetime.now()
            await self.bot.save_guild_queue(interaction.guild.id)

        except Exception as e:
            logger.error(f"Playlist load error: {e}")
            embed = create_embed("Error", f"Failed to load {label.lower()}.", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed)

    async def _handle_show(
            self, interaction: discord.Interaction, name: str, page: int = 1, global_mode: bool = False,
            collaborative: bool = False
    ):
        label = "Global Playlist" if global_mode else "Playlist"

        try:
            playlist_items, _, _ = await self._get_collab_playlist_songs(
                interaction, name, use_followup=False, global_mode=global_mode, collab_only=collaborative
            )
            if playlist_items is None:
                return

            if not playlist_items:
                embed = create_embed(
                    f"{label}: {name}", f"{label} is empty", COLOR, self.bot.user
                )
                await interaction.response.send_message(embed=embed, silent=True)
                return

            total_pages = max(
                1, (len(playlist_items) + SONGS_PER_PAGE - 1) // SONGS_PER_PAGE
            )

            page = max(1, min(page, total_pages))

            pages = []
            for page_num in range(total_pages):
                start_idx = page_num * SONGS_PER_PAGE
                end_idx = start_idx + SONGS_PER_PAGE

                description = ""
                for i, song_info in enumerate(playlist_items[start_idx:end_idx], start_idx + 1):
                    title = song_info.get("title", "Unknown Title")
                    uploader = song_info.get("uploader", "Unknown")
                    description += f"`{i}.` **{title}** by {uploader}\n"

                embed = create_embed(
                    f"{label}: {name} - Page {page_num + 1}/{total_pages}",
                    description[:4000],
                    COLOR,
                    self.bot.user
                )
                embed.add_field(name="Total Songs", value=str(len(playlist_items)), inline=True)

                pages.append(embed)

            view = PaginationView(pages, interaction.user)
            view.current_page = page - 1

            view.previous_button.disabled = view.current_page == 0
            view.next_button.disabled = view.current_page == len(pages) - 1

            await interaction.response.send_message(embed=pages[page - 1], view=view, silent=True)
            view.message = await interaction.original_response()

        except Exception as e:
            logger.error(f"Playlist show error: {e}")
            embed = create_embed("Error", f"Failed to show {label.lower()}.", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, silent=True)

    async def _handle_list(self, interaction: discord.Interaction, global_mode: bool = False):
        label = "Your Global Playlists" if global_mode else "Your Playlists"
        guild_id = None if global_mode else interaction.guild.id

        try:
            results = await self._list_playlists_from_db(interaction.user.id, guild_id)

            if not results:
                empty_msg = "You don't have any global playlists." if global_mode else "You don't have any saved playlists."
                embed = create_embed(label, empty_msg, COLOR, self.bot.user)
            else:
                description = ""
                for playlist_name, songs_json, created_at in results:
                    try:
                        playlist_items = json.loads(songs_json) if songs_json else []
                        song_count = len(playlist_items)
                        description += f"\u2022 **{playlist_name}** ({song_count} songs) - {created_at[:10]}\n"
                    except json.JSONDecodeError:
                        description += f"\u2022 **{playlist_name}** (corrupted data) - {created_at[:10]}\n"

                embed = create_embed(label, description, COLOR, self.bot.user)

            await interaction.response.send_message(embed=embed, silent=True)

        except Exception as e:
            logger.error(f"Playlist list error: {e}")
            embed = create_embed("Error", "Failed to retrieve playlists.", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, silent=True)

    async def _handle_delete(self, interaction: discord.Interaction, name: str, global_mode: bool = False):
        label = "Global playlist" if global_mode else "Playlist"
        guild_id = None if global_mode else interaction.guild.id

        try:
            exists = await self._playlist_exists(interaction.user.id, name, guild_id)
            if not exists:
                embed = create_embed(
                    "Error", f"{label} **{name}** not found", COLOR, self.bot.user
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            await self._delete_playlist_row(interaction.user.id, name, guild_id)

            embed = create_embed(
                f"{label} Deleted", f"Deleted {label.lower()} **{name}**.", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed, silent=True)

        except Exception as e:
            logger.error(f"Playlist delete error: {e}")
            embed = create_embed("Error", f"Failed to delete {label.lower()}.", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed)

    # Collab handler methods

    async def _handle_collab_add(
            self, interaction: discord.Interaction, name: str, user: discord.Member, global_mode: bool = False
    ):
        label = "Global playlist" if global_mode else "Playlist"
        guild_id = None if global_mode else interaction.guild.id

        pid = await self.bot.get_playlist_id(interaction.user.id, name, guild_id)
        if pid is None:
            embed = create_embed("Error", f"{label} **{name}** not found (you must own it)", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if user.id == interaction.user.id:
            embed = create_embed("Error", "You can't add yourself as a collaborator", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if user.bot:
            embed = create_embed("Error", "You can't add a bot as a collaborator", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        already = await self.bot.is_collaborator(pid, user.id, global_mode)
        if already:
            embed = create_embed("Error", f"{user.display_name} is already a collaborator", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await self.bot.add_collaborator(pid, user.id, global_mode)
        embed = create_embed(
            "Collaborator Added",
            f"Added **{user.display_name}** as a collaborator on {label.lower()} **{name}**",
            COLOR, self.bot.user
        )
        await interaction.response.send_message(embed=embed, silent=True)

    async def _handle_collab_remove(
            self, interaction: discord.Interaction, name: str, user: discord.Member, global_mode: bool = False
    ):
        label = "Global playlist" if global_mode else "Playlist"
        guild_id = None if global_mode else interaction.guild.id

        pid = await self.bot.get_playlist_id(interaction.user.id, name, guild_id)
        if pid is None:
            embed = create_embed("Error", f"{label} **{name}** not found (you must own it)", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        is_collab = await self.bot.is_collaborator(pid, user.id, global_mode)
        if not is_collab:
            embed = create_embed("Error", f"{user.display_name} is not a collaborator", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await self.bot.remove_collaborator(pid, user.id, global_mode)
        embed = create_embed(
            "Collaborator Removed",
            f"Removed **{user.display_name}** from {label.lower()} **{name}**",
            COLOR, self.bot.user
        )
        await interaction.response.send_message(embed=embed, silent=True)

    async def _handle_collab_list(
            self, interaction: discord.Interaction, name: str, global_mode: bool = False
    ):
        label = "Global playlist" if global_mode else "Playlist"
        guild_id = None if global_mode else interaction.guild.id

        pid = await self.bot.get_playlist_id(interaction.user.id, name, guild_id)
        if pid is None:
            embed = create_embed("Error", f"{label} **{name}** not found (you must own it)", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        collab_ids = await self.bot.get_collaborators(pid, global_mode)
        if not collab_ids:
            embed = create_embed(f"{label}: {name}", "No collaborators", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, silent=True)
            return

        lines = []
        for uid in collab_ids:
            user = interaction.guild.get_member(uid) or self.bot.get_user(uid)
            display = user.display_name if user else f"User {uid}"
            lines.append(f"- {display}")

        embed = create_embed(
            f"Collaborators: {name}",
            "\n".join(lines),
            COLOR, self.bot.user
        )
        await interaction.response.send_message(embed=embed, silent=True)

    async def _handle_my_collabs(self, interaction: discord.Interaction, global_mode: bool = False):
        """Show all playlists the user is a collaborator on."""
        user_id = interaction.user.id

        if global_mode:
            rows = await self.bot.fetch_db_query(
                "SELECT gp.name, gp.user_id, gp.songs FROM global_playlists gp "
                "JOIN playlist_collaborators pc ON pc.playlist_id = gp.id AND pc.is_global = 1 "
                "WHERE pc.user_id = ? ORDER BY gp.name",
                (user_id,),
            )
        else:
            rows = await self.bot.fetch_db_query(
                "SELECT p.name, p.user_id, p.songs FROM playlists p "
                "JOIN playlist_collaborators pc ON pc.playlist_id = p.id AND pc.is_global = 0 "
                "WHERE pc.user_id = ? AND p.guild_id = ? ORDER BY p.name",
                (user_id, interaction.guild.id),
            )

        label = "Your Global Collaborations" if global_mode else "Your Collaborations"

        if not rows:
            embed = create_embed(label, "You're not a collaborator on any playlists.", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        description = ""
        for name, owner_id, songs_json in rows:
            owner = interaction.guild.get_member(owner_id) or self.bot.get_user(owner_id)
            owner_name = owner.display_name if owner else f"User {owner_id}"
            try:
                song_count = len(json.loads(songs_json)) if songs_json else 0
            except json.JSONDecodeError:
                song_count = 0
            description += f"\u2022 **{name}** by {owner_name} ({song_count} songs)\n"

        embed = create_embed(label, description[:4000], COLOR, self.bot.user)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # Server Playlist commands

    playlist_group = discord.app_commands.Group(
        name="playlist", description="Manage your server playlists"
    )

    @playlist_group.command(name="create", description="Create a new empty playlist")
    @discord.app_commands.describe(name="Name for the playlist")
    async def playlist_create(self, interaction: discord.Interaction, name: str):
        await self._handle_create(interaction, name)

    @playlist_group.command(name="add", description="Add a song or playlist to a playlist")
    @discord.app_commands.describe(
        name="Playlist name",
        song="Song URL, playlist URL, or search term",
        collaborative="Target a collaborative playlist instead of your own",
    )
    async def playlist_add(self, interaction: discord.Interaction, name: str, song: str, collaborative: bool = False):
        await self._handle_add(interaction, name, song, collaborative=collaborative)

    @playlist_group.command(name="add-from-queue", description="Add a song from the current queue to a playlist")
    @discord.app_commands.describe(
        name="Playlist name",
        from_queue="Song from current session to add",
        collaborative="Target a collaborative playlist instead of your own",
    )
    @discord.app_commands.autocomplete(from_queue=queue_autocomplete)
    async def playlist_add_from_queue(self, interaction: discord.Interaction, name: str, from_queue: str,
                                      collaborative: bool = False):
        await self._handle_add_from_queue(interaction, name, from_queue, collaborative=collaborative)

    @playlist_group.command(name="add-all-queue", description="Add entire current queue to a playlist")
    @discord.app_commands.describe(name="Playlist name",
                                   collaborative="Target a collaborative playlist instead of your own")
    async def playlist_add_session(self, interaction: discord.Interaction, name: str, collaborative: bool = False):
        await self._handle_add_session(interaction, name, collaborative=collaborative)

    @playlist_group.command(name="remove", description="Remove a song from a playlist")
    @discord.app_commands.describe(
        name="Playlist name", position="Position of song to remove (1-based)",
        collaborative="Target a collaborative playlist instead of your own",
    )
    async def playlist_remove(self, interaction: discord.Interaction, name: str, position: int,
                              collaborative: bool = False):
        await self._handle_remove(interaction, name, position, collaborative=collaborative)

    @playlist_group.command(name="move", description="Move a song to a different position in a playlist")
    @discord.app_commands.describe(
        name="Playlist name",
        from_pos="Current position of the song (1-based)",
        to_pos="New position for the song (1-based)",
        collaborative="Target a collaborative playlist instead of your own",
    )
    async def playlist_move(self, interaction: discord.Interaction, name: str, from_pos: int, to_pos: int,
                            collaborative: bool = False):
        await self._handle_move(interaction, name, from_pos, to_pos, collaborative=collaborative)

    @playlist_group.command(name="load", description="Load a playlist into the queue")
    @discord.app_commands.describe(name="Playlist name",
                                   collaborative="Target a collaborative playlist instead of your own")
    async def playlist_load(self, interaction: discord.Interaction, name: str, collaborative: bool = False):
        await self._handle_load(interaction, name, collaborative=collaborative)

    @playlist_group.command(name="show", description="Show songs in a playlist")
    @discord.app_commands.describe(name="Playlist name", page="Page number to view (optional)",
                                   collaborative="Target a collaborative playlist instead of your own")
    async def playlist_show(self, interaction: discord.Interaction, name: str, page: int = 1,
                            collaborative: bool = False):
        await self._handle_show(interaction, name, page, collaborative=collaborative)

    @playlist_group.command(name="list", description="List all your playlists")
    async def playlist_list(self, interaction: discord.Interaction):
        await self._handle_list(interaction)

    @playlist_group.command(name="delete", description="Delete a playlist")
    @discord.app_commands.describe(name="Playlist name to delete")
    async def playlist_delete(self, interaction: discord.Interaction, name: str):
        await self._handle_delete(interaction, name)

    @playlist_group.command(name="collab-add", description="Add a collaborator to your playlist")
    @discord.app_commands.describe(name="Playlist name", user="User to add as collaborator")
    async def playlist_collab_add(self, interaction: discord.Interaction, name: str, user: discord.Member):
        await self._handle_collab_add(interaction, name, user)

    @playlist_group.command(name="collab-remove", description="Remove a collaborator from your playlist")
    @discord.app_commands.describe(name="Playlist name", user="User to remove")
    async def playlist_collab_remove(self, interaction: discord.Interaction, name: str, user: discord.Member):
        await self._handle_collab_remove(interaction, name, user)

    @playlist_group.command(name="collab-list", description="List collaborators on your playlist")
    @discord.app_commands.describe(name="Playlist name")
    async def playlist_collab_list(self, interaction: discord.Interaction, name: str):
        await self._handle_collab_list(interaction, name)

    @playlist_group.command(name="my-collabs", description="Show playlists you're a collaborator on")
    async def playlist_my_collabs(self, interaction: discord.Interaction):
        await self._handle_my_collabs(interaction)

    # Global Playlist commands

    globalplaylist_group = discord.app_commands.Group(
        name="globalplaylist", description="Manage your global playlists (accessible from any server)"
    )

    @globalplaylist_group.command(name="create", description="Create a new global playlist")
    @discord.app_commands.describe(name="Name for the global playlist")
    async def globalplaylist_create(self, interaction: discord.Interaction, name: str):
        await self._handle_create(interaction, name, global_mode=True)

    @globalplaylist_group.command(name="add", description="Add a song or playlist to a global playlist")
    @discord.app_commands.describe(
        name="Global playlist name",
        song="Song URL, playlist URL, or search term",
        collaborative="Target a collaborative playlist instead of your own",
    )
    async def globalplaylist_add(self, interaction: discord.Interaction, name: str, song: str,
                                 collaborative: bool = False):
        await self._handle_add(interaction, name, song, global_mode=True, collaborative=collaborative)

    @globalplaylist_group.command(name="add-from-queue",
                                  description="Add a song from the current queue to a global playlist")
    @discord.app_commands.describe(
        name="Global playlist name",
        from_queue="Song from current session to add",
        collaborative="Target a collaborative playlist instead of your own",
    )
    @discord.app_commands.autocomplete(from_queue=queue_autocomplete)
    async def globalplaylist_add_from_queue(self, interaction: discord.Interaction, name: str, from_queue: str,
                                            collaborative: bool = False):
        await self._handle_add_from_queue(interaction, name, from_queue, global_mode=True, collaborative=collaborative)

    @globalplaylist_group.command(name="add-all-queue", description="Add entire current queue to a global playlist")
    @discord.app_commands.describe(name="Global playlist name",
                                   collaborative="Target a collaborative playlist instead of your own")
    async def globalplaylist_add_session(self, interaction: discord.Interaction, name: str,
                                         collaborative: bool = False):
        await self._handle_add_session(interaction, name, global_mode=True, collaborative=collaborative)

    @globalplaylist_group.command(name="remove", description="Remove a song from a global playlist")
    @discord.app_commands.describe(
        name="Global playlist name", position="Position of song to remove (1-based)",
        collaborative="Target a collaborative playlist instead of your own",
    )
    async def globalplaylist_remove(self, interaction: discord.Interaction, name: str, position: int,
                                    collaborative: bool = False):
        await self._handle_remove(interaction, name, position, global_mode=True, collaborative=collaborative)

    @globalplaylist_group.command(name="move", description="Move a song to a different position in a global playlist")
    @discord.app_commands.describe(
        name="Global playlist name",
        from_pos="Current position of the song (1-based)",
        to_pos="New position for the song (1-based)",
        collaborative="Target a collaborative playlist instead of your own",
    )
    async def globalplaylist_move(self, interaction: discord.Interaction, name: str, from_pos: int, to_pos: int,
                                  collaborative: bool = False):
        await self._handle_move(interaction, name, from_pos, to_pos, global_mode=True, collaborative=collaborative)

    @globalplaylist_group.command(name="load", description="Load a global playlist into the queue")
    @discord.app_commands.describe(name="Global playlist name",
                                   collaborative="Target a collaborative playlist instead of your own")
    async def globalplaylist_load(self, interaction: discord.Interaction, name: str, collaborative: bool = False):
        await self._handle_load(interaction, name, global_mode=True, collaborative=collaborative)

    @globalplaylist_group.command(name="show", description="Show songs in a global playlist")
    @discord.app_commands.describe(name="Global playlist name", page="Page number to view (optional)",
                                   collaborative="Target a collaborative playlist instead of your own")
    async def globalplaylist_show(self, interaction: discord.Interaction, name: str, page: int = 1,
                                  collaborative: bool = False):
        await self._handle_show(interaction, name, page, global_mode=True, collaborative=collaborative)

    @globalplaylist_group.command(name="list", description="List all your global playlists")
    async def globalplaylist_list(self, interaction: discord.Interaction):
        await self._handle_list(interaction, global_mode=True)

    @globalplaylist_group.command(name="delete", description="Delete a global playlist")
    @discord.app_commands.describe(name="Global playlist name to delete")
    async def globalplaylist_delete(self, interaction: discord.Interaction, name: str):
        await self._handle_delete(interaction, name, global_mode=True)

    @globalplaylist_group.command(name="collab-add", description="Add a collaborator to your global playlist")
    @discord.app_commands.describe(name="Global playlist name", user="User to add as collaborator")
    async def globalplaylist_collab_add(self, interaction: discord.Interaction, name: str, user: discord.Member):
        await self._handle_collab_add(interaction, name, user, global_mode=True)

    @globalplaylist_group.command(name="collab-remove", description="Remove a collaborator from your global playlist")
    @discord.app_commands.describe(name="Global playlist name", user="User to remove")
    async def globalplaylist_collab_remove(self, interaction: discord.Interaction, name: str, user: discord.Member):
        await self._handle_collab_remove(interaction, name, user, global_mode=True)

    @globalplaylist_group.command(name="collab-list", description="List collaborators on your global playlist")
    @discord.app_commands.describe(name="Global playlist name")
    async def globalplaylist_collab_list(self, interaction: discord.Interaction, name: str):
        await self._handle_collab_list(interaction, name, global_mode=True)

    @globalplaylist_group.command(name="my-collabs", description="Show global playlists you're a collaborator on")
    async def globalplaylist_my_collabs(self, interaction: discord.Interaction):
        await self._handle_my_collabs(interaction, global_mode=True)

    # ── History commands ────────────────────────────────────────────────

    history_group = discord.app_commands.Group(
        name="history", description="Manage song history"
    )

    @history_group.command(name="show", description="Show recently played songs")
    @discord.app_commands.describe(page="Page number to view (optional)")
    async def history_show(self, interaction: discord.Interaction, page: int = 1):
        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if not guild_data["history"]:
            embed = create_embed("History", "No songs in history yet", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, silent=True)
            return

        total_pages = max(
            1, (len(guild_data["history"]) + SONGS_PER_PAGE - 1) // SONGS_PER_PAGE
        )

        page = max(1, min(page, total_pages))

        pages = []
        for page_num in range(total_pages):
            start_idx = page_num * SONGS_PER_PAGE
            end_idx = start_idx + SONGS_PER_PAGE

            description = ""
            for i, song in enumerate(guild_data["history"][start_idx:end_idx], start_idx + 1):
                description += f"`{i}.` **{song.title}** by {song.uploader}\n"

            embed = create_embed(
                f"Recent History - Page {page_num + 1}/{total_pages}",
                description[:4000],
                COLOR,
                self.bot.user
            )
            embed.add_field(name="Total", value=str(len(guild_data["history"])), inline=True)
            embed.set_footer(
                text="Use /history play <number> to replay a song or /history add_all to add all songs"
            )

            pages.append(embed)

        view = PaginationView(pages, interaction.user)
        view.current_page = page - 1

        view.previous_button.disabled = view.current_page == 0
        view.next_button.disabled = view.current_page == len(pages) - 1

        await interaction.response.send_message(
            embed=pages[page - 1],
            view=view,
            silent=True
        )
        view.message = await interaction.original_response()

    @history_group.command(name="play", description="Play a song from history by number")
    @discord.app_commands.describe(song_number="Song number from history to play")
    async def history_play(self, interaction: discord.Interaction, song_number: int):
        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if not guild_data["history"]:
            embed = create_embed("History", "No songs in history yet", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, silent=True)
            return

        music_cog = self.bot.get_cog("MusicCommands")
        if not music_cog:
            embed = create_embed("Error", "Music commands not loaded", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if not await music_cog.check_voice_channel(interaction, allow_auto_join=True):
            return

        if song_number < 1 or song_number > len(guild_data["history"]):
            embed = create_embed(
                "Error",
                f"Invalid history position! History has {len(guild_data['history'])} songs.",
                COLOR,
                self.bot.user
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        selected_song = guild_data["history"][song_number - 1]
        queue_urls = {song.webpage_url for song in guild_data.get("queue", [])}

        if (
                guild_data.get("current")
                and selected_song.webpage_url == guild_data["current"].webpage_url
        ):
            embed = create_embed(
                "Error", "This song is currently playing", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        elif selected_song.webpage_url in queue_urls:
            for i, song in enumerate(guild_data.get("queue", []), 1):
                if song.webpage_url == selected_song.webpage_url:
                    embed = create_embed(
                        "Error",
                        f"This song is already in queue at position {i}",
                        COLOR,
                        self.bot.user
                    )
                    await interaction.response.send_message(
                        embed=embed, ephemeral=True
                    )
                    return

        if not await music_cog.ensure_voice_connection(interaction):
            embed = create_embed(
                "Error", "Failed to connect to voice channel", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        song_copy = Song.from_dict(selected_song.to_dict())
        song_copy.requested_by = interaction.user.mention
        self.queue_service.add_song_to_queue(interaction.guild.id, song_copy)

        playback_service = self.bot._playback_service
        voice_client = guild_data.get("voice_client")

        if (
                voice_client
                and not voice_client.is_playing()
                and not guild_data.get("current")
        ):
            await playback_service.play_next(interaction.guild.id)
            embed = create_embed(
                "Now Playing from History", str(song_copy), COLOR, self.bot.user
            )
        else:
            position = len(guild_data.get("queue", []))
            embed = create_embed(
                "Added from History",
                f"{song_copy}\n\nPosition in queue: {position}",
                COLOR,
                self.bot.user
            )

        if song_copy.thumbnail:
            embed.set_thumbnail(url=song_copy.thumbnail)

        await interaction.response.send_message(embed=embed, silent=True)
        guild_data["last_activity"] = datetime.now()
        await self.bot.save_guild_queue(interaction.guild.id)

    @history_group.command(
        name="add_all", description="Add all songs from history to the queue"
    )
    async def history_add_all(self, interaction: discord.Interaction):
        music_cog = await self._get_music_cog(interaction)
        if not music_cog:
            return

        if not await music_cog.check_voice_channel(interaction, allow_auto_join=True):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if not guild_data["history"]:
            embed = create_embed("History", "No songs in history yet", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, silent=True)
            return

        if not await music_cog.ensure_voice_connection(interaction):
            embed = create_embed(
                "Error", "Failed to connect to voice channel", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await interaction.response.defer()

        existing_urls = get_existing_urls(guild_data)
        added_count = 0
        skipped_count = 0

        for history_song in guild_data["history"]:
            if history_song.webpage_url not in existing_urls:
                song_copy = Song.from_dict(history_song.to_dict())
                song_copy.requested_by = interaction.user.mention
                self.queue_service.add_song_to_queue(interaction.guild.id, song_copy)
                existing_urls.add(history_song.webpage_url)
                added_count += 1
            else:
                skipped_count += 1

        if added_count == 0:
            embed = create_embed(
                "No Songs Added",
                "All history songs are already in queue or currently playing",
                COLOR,
                self.bot.user
            )
        else:
            embed = create_embed(
                "History Added to Queue",
                f"Added {added_count} songs from history to queue"
                + (
                    f"\nSkipped {skipped_count} duplicates."
                    if skipped_count > 0
                    else ""
                ),
                COLOR,
                self.bot.user
            )

        await interaction.followup.send(embed=embed, silent=True)

        playback_service = self.bot._playback_service

        if guild_data["voice_client"] and not guild_data["voice_client"].is_playing() and not guild_data["current"]:
            await playback_service.play_next(interaction.guild.id)

        guild_data["last_activity"] = datetime.now()
        await self.bot.save_guild_queue(interaction.guild.id)

    @history_group.command(
        name="clear",
        description="Clear history songs"
    )
    async def history_clear(self, interaction: discord.Interaction):
        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if not guild_data["history"]:
            embed = create_embed(
                "Error",
                "History already empty!",
                COLOR,
                self.bot.user
            )
            await interaction.response.send_message(embed=embed)
            return

        guild_data["history"].clear()
        guild_data["history_position"] = 0
        embed = create_embed(
            "History cleared",
            "Removed all songs from history",
            COLOR,
            self.bot.user
        )
        await interaction.response.send_message(embed=embed, silent=True)
        await self.bot.save_guild_queue(interaction.guild.id)
