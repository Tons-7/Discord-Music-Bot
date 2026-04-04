import logging

import discord
from discord import ui

from config import COLOR

logger = logging.getLogger(__name__)

ACCENT_COLOUR = discord.Colour(COLOR)


class SongSelect(ui.Select):
    def __init__(self, songs_data):
        self.songs_data = songs_data

        options = []
        for i, entry in enumerate(songs_data[:5]):
            title = entry.get("title", "Unknown Title")
            uploader = entry.get("uploader", "Unknown")
            duration = entry.get("duration", 0)

            if duration:
                minutes, seconds = divmod(int(duration), 60)
                duration_str = f"{minutes}:{seconds:02d}"
            else:
                duration_str = "0:00"

            if len(title) > 60:
                title = title[:57] + "..."

            description = f"by {uploader} \u2022 {duration_str}"
            if len(description) > 100:
                description = description[:97] + "..."

            options.append(
                discord.SelectOption(
                    label=title, description=description, value=str(i), emoji="\U0001f3b5"
                )
            )

        super().__init__(
            placeholder="Choose a song to add to queue...",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        if self.view.selected:
            await interaction.response.send_message(
                "A song has already been selected!", ephemeral=True
            )
            return

        self.view.selected = True
        self.disabled = True
        self.view.stop()

        try:
            selected_index = int(self.values[0])
            selected_song = self.songs_data[selected_index]

            # Update with loading state
            loading_view = ui.LayoutView(timeout=None)
            container = ui.Container(accent_colour=ACCENT_COLOUR)
            container.add_item(ui.TextDisplay(f"### Adding Song...\nAdding **{selected_song['title']}** to queue..."))
            loading_view.add_item(container)
            await interaction.response.edit_message(view=loading_view)

            await self.view.music_commands_cog.process_selected_song(
                interaction, selected_song
            )

        except Exception as e:
            logger.error(f"Error in song selection: {e}")
            self.view.selected = False
            error_view = ui.LayoutView(timeout=None)
            container = ui.Container(accent_colour=ACCENT_COLOUR)
            container.add_item(ui.TextDisplay("### \u274c Error\nFailed to add song to queue."))
            error_view.add_item(container)
            try:
                if not interaction.response.is_done():
                    await interaction.response.edit_message(view=error_view)
                else:
                    await interaction.edit_original_response(view=error_view)
            except discord.HTTPException:
                pass


class SongSelectView(ui.LayoutView):
    def __init__(self, songs_data, user, music_commands_cog):
        super().__init__(timeout=30)
        self.songs_data = songs_data
        self.user = user
        self.music_commands_cog = music_commands_cog
        self.selected = False
        self.message = None

        # Build layout
        container = ui.Container(accent_colour=ACCENT_COLOUR)

        # Song list text
        lines = []
        for i, entry in enumerate(songs_data[:5], 1):
            title = entry.get("title", "Unknown Title")
            uploader = entry.get("uploader", "Unknown")
            duration = entry.get("duration", 0)
            if duration:
                minutes, seconds = divmod(int(duration), 60)
                dur_str = f"{minutes}:{seconds:02d}"
            else:
                dur_str = "LIVE"
            lines.append(f"`{i}.` **{title}** by {uploader} `[{dur_str}]`")

        container.add_item(ui.TextDisplay(f"### \U0001f50e Search Results\n" + "\n".join(lines)))
        container.add_item(ui.Separator())

        # Select menu inside container
        container.add_item(ui.ActionRow(SongSelect(songs_data)))
        self.add_item(container)

    async def on_timeout(self):
        if self.selected:
            return

        if not self.message:
            return

        try:
            timeout_view = ui.LayoutView(timeout=None)
            container = ui.Container(accent_colour=ACCENT_COLOUR)
            container.add_item(ui.TextDisplay("### \u23f0 Search Timed Out\nSearch selection timed out."))
            timeout_view.add_item(container)
            await self.message.edit(view=timeout_view)
        except discord.HTTPException:
            pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.user:
            await interaction.response.send_message(
                "This search menu is not for you!", ephemeral=True
            )
            return False
        return True
