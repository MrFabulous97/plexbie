"""Admin-channel mirroring for user-directed Plexbie DMs."""
from __future__ import annotations

from typing import Optional

import discord

from core.logging import get_logger

logger = get_logger(__name__)


async def _get_admin_channel(bot, services):
    channel_id = getattr(services.config, "admin_channel_id", None)
    if not channel_id:
        logger.warning("ADMIN_CHANNEL_ID not configured - cannot mirror user DM to admin channel")
        return None

    channel = bot.get_channel(channel_id)
    if channel:
        return channel

    try:
        return await bot.fetch_channel(channel_id)
    except Exception as e:
        logger.warning(f"Could not fetch admin channel {channel_id} for DM mirror: {e}")
        return None


def _copy_embed(embed: Optional[discord.Embed]) -> Optional[discord.Embed]:
    if embed is None:
        return None
    return discord.Embed.from_dict(embed.to_dict())


async def _send_admin_receipt(bot, services, *, header: str, content: Optional[str] = None, embed: Optional[discord.Embed] = None):
    """Mirror a DM to the admin channel. Never raises.

    Mirroring is an observability feature and must not be able to fail the
    operation it is reporting on. Previously an exception here propagated out of
    send_user_dm, so a failure to post the receipt was indistinguishable from the
    DM itself failing - and callers acted on that: user_invites.approve aborted
    after the Plex invite had already been sent and the role assigned.
    """
    try:
        channel = await _get_admin_channel(bot, services)
        if not channel:
            return

        message = header
        if content:
            message = f"{message}\n{content}"

        kwargs = {"content": message}
        copied = _copy_embed(embed)
        if copied is not None:
            kwargs["embed"] = copied

        await channel.send(**kwargs)
    except Exception as e:
        logger.warning(f"Could not mirror to the admin channel: {e}")


async def send_user_dm(bot, services, user, *, context: str, content: Optional[str] = None, embed: Optional[discord.Embed] = None):
    try:
        await user.send(content=content, embed=embed)
    except Exception as e:
        await _send_admin_receipt(
            bot,
            services,
            header=f"⚠️ Plexbie DM failed → <@{user.id}> ({user}) — {context}",
            content=f"Error: `{e}`",
            embed=embed,
        )
        raise

    await _send_admin_receipt(
        bot,
        services,
        header=f"📬 Plexbie DM sent → <@{user.id}> ({user}) — {context}",
        content=content,
        embed=embed,
    )
