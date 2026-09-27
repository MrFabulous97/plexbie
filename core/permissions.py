# path: core/permissions.py
"""Shared authorization helpers for commands and interactive views.

The codebase historically used two different admin conventions:
  * ``interaction.user.guild_permissions.administrator`` (media_cleanup, user_mgmt, status)
  * a configured ``ADMIN_ROLE_ID`` (invite_tracker)

``is_bot_admin`` accepts either, plus the configured bot owner, so adopting it
does not revoke access from anyone who had it before.
"""
import discord

from core.logging import get_logger

logger = get_logger(__name__)

DENIED_MESSAGE = "⛔ You don't have permission to use this."


def _config_from_interaction(interaction: discord.Interaction):
    """Resolve the bot config off an interaction, or None if unavailable.

    Read from the client rather than a constructor argument so that persistent
    views registered with no arguments (``bot.add_view(SomeView())``) are still
    able to authorize.
    """
    services = getattr(interaction.client, "services", None)
    return getattr(services, "config", None)


def is_bot_admin(interaction: discord.Interaction) -> bool:
    """Return True if the interacting user may perform administrative actions.

    Fails closed: anything unexpected (DM context, missing config, member not
    resolvable) denies access rather than allowing it. The bot owner is always
    permitted so a misconfigured guild cannot lock everyone out.
    """
    user = interaction.user
    config = _config_from_interaction(interaction)

    if config is not None and config.bot_owner_id and user.id == config.bot_owner_id:
        return True

    # Guild administrator permission. Absent on discord.User (i.e. in DMs).
    perms = getattr(user, "guild_permissions", None)
    if perms is not None and perms.administrator:
        return True

    # Explicitly configured admin role.
    if config is not None and config.admin_role_id:
        roles = getattr(user, "roles", None)
        if roles and any(role.id == config.admin_role_id for role in roles):
            return True

    return False


async def deny(interaction: discord.Interaction) -> None:
    """Send the standard ephemeral refusal, whether or not we already responded."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(DENIED_MESSAGE, ephemeral=True)
        else:
            await interaction.response.send_message(DENIED_MESSAGE, ephemeral=True)
    except discord.HTTPException as e:
        logger.debug(f"Could not deliver permission refusal: {e}")


async def require_admin(interaction: discord.Interaction) -> bool:
    """Guard for command bodies. Returns True if allowed, else denies and returns False.

    Usage:
        if not await require_admin(interaction):
            return
    """
    if is_bot_admin(interaction):
        return True

    logger.warning(
        f"Denied admin action to {interaction.user} ({interaction.user.id}): "
        f"{getattr(interaction.command, 'name', None) or 'component interaction'}"
    )
    await deny(interaction)
    return False


class AdminOnlyView(discord.ui.View):
    """A View whose every component is restricted to administrators.

    discord.py calls ``interaction_check`` before dispatching to any item
    callback, so subclasses get the gate without repeating it per button. This
    is the authoritative check: an ephemeral delivery is not a permission
    boundary, and persistent views outlive the message they were sent with.
    """

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_bot_admin(interaction):
            return True

        logger.warning(
            f"Denied {self.__class__.__name__} interaction to "
            f"{interaction.user} ({interaction.user.id})"
        )
        await deny(interaction)
        return False
