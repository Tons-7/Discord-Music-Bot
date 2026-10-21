import asyncio

import discord


class ConfirmView(discord.ui.View):
    """Confirm or Cancel, for one user; answers False when they don't choose within the timeout."""

    def __init__(self, user: discord.abc.User, *, confirm_label: str = "Delete", timeout: float = 60):
        super().__init__(timeout=timeout)
        self.user = user
        self._answer: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.confirm.label = confirm_label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This isn't your choice to make.", ephemeral=True)
            return False
        return True

    def _settle(self, value: bool) -> None:
        if not self._answer.done():
            self._answer.set_result(value)
        self.stop()

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.defer()
        self._settle(True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.defer()
        self._settle(False)

    async def on_timeout(self) -> None:
        self._settle(False)

    async def wait_for_answer(self) -> bool:
        return await self._answer
