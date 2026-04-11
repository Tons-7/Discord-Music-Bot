from typing import List

import discord
from discord import ui


from config import COLOR

ACCENT_COLOUR = discord.Colour(COLOR)


class _PrevButton(ui.Button):
    def __init__(self, disabled: bool = True):
        super().__init__(label="\u25c0 Previous", style=discord.ButtonStyle.secondary, disabled=disabled)

    async def callback(self, interaction: discord.Interaction):
        view: PaginationView = self.view  # type: ignore
        if view.current_page > 0:
            view.current_page -= 1
            try:
                await interaction.response.edit_message(view=view._build_view())
            except (discord.NotFound, discord.HTTPException):
                pass


class _NextButton(ui.Button):
    def __init__(self, disabled: bool = True):
        super().__init__(label="Next \u25b6", style=discord.ButtonStyle.secondary, disabled=disabled)

    async def callback(self, interaction: discord.Interaction):
        view: PaginationView = self.view  # type: ignore
        if view.current_page < len(view.pages) - 1:
            view.current_page += 1
            try:
                await interaction.response.edit_message(view=view._build_view())
            except (discord.NotFound, discord.HTTPException):
                pass


class PaginationView(ui.LayoutView):
    def __init__(self, pages: List[str], user: discord.User, timeout: int = 240):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.user = user
        self.current_page = 0
        self.message: discord.Message | None = None

        self._rebuild()

    def _rebuild(self):
        self.clear_items()

        container = ui.Container(accent_colour=ACCENT_COLOUR)
        container.add_item(ui.TextDisplay(self.pages[self.current_page]))

        if len(self.pages) > 1:
            container.add_item(ui.Separator())
            container.add_item(ui.ActionRow(
                _PrevButton(disabled=self.current_page == 0),
                _NextButton(disabled=self.current_page == len(self.pages) - 1),
            ))

        self.add_item(container)

    def _build_view(self) -> 'PaginationView':
        """Rebuild and return self for message edits."""
        self._rebuild()
        return self

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.user:
            await interaction.response.send_message(
                "You can't use these buttons!", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        # Disable buttons on timeout
        self._rebuild()
        for item in self.children:
            if isinstance(item, ui.Container):
                for child in item.children:
                    if isinstance(child, ui.ActionRow):
                        for btn in child.children:
                            btn.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
