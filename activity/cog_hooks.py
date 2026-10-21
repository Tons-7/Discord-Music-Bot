import logging

from activity.helpers import schedule_broadcast

logger = logging.getLogger(__name__)


def install_broadcast_hooks(bot, ws_manager):
    """Broadcast state to Activity clients whenever the bot reports a change (saves, pauses, voice moves)."""
    bot.add_state_listener(lambda guild_id: schedule_broadcast(bot, ws_manager, guild_id))

    async def _on_app_command_completion(interaction, _command):
        if interaction.guild_id:
            schedule_broadcast(bot, ws_manager, interaction.guild_id)

    async def _on_voice_state_update(member, before, after):
        if bot.user and member.id == bot.user.id and before.channel != after.channel:
            schedule_broadcast(bot, ws_manager, member.guild.id)

    bot.add_listener(_on_app_command_completion, "on_app_command_completion")
    bot.add_listener(_on_voice_state_update, "on_voice_state_update")

    logger.info("Activity broadcast hooks installed")
