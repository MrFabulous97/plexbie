"""User management plugin with automatic inactivity tracking"""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from database.kv_store import kv_get, kv_set, kv_get_all
from typing import Optional, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.blocking import run_blocking
from core.permissions import AdminOnlyView
from core.logging import get_logger
from core.services import BotServices
from core.admin_mirror import send_user_dm
from utils.embeds import truncate_field
from database.session import get_session
from .models import PlexUser

logger = get_logger(__name__)

# Invites file location (shared with user_invites plugin)
# INVITES_FILE removed - now using database kv_store
INVITES_NAMESPACE = "plex_invites"

# Discord allows 25 fields per embed.
MAX_LISTED_USERS = 25


class UserMgmtCog(commands.Cog):
    """User management with automatic inactivity removal"""

    def __init__(self, bot: commands.Bot, services: BotServices):
        self.bot = bot
        self.services = services

        # Start background tasks
        self.check_inactive_users.start()
        self.auto_link_users.start()

    def cog_unload(self):
        """Cleanup tasks on unload"""
        self.check_inactive_users.cancel()
        self.auto_link_users.cancel()

    @tasks.loop(hours=24)
    async def check_inactive_users(self):
        """Check all users for inactivity every 24 hours"""
        if not self.services.config.tautulli_url:
            logger.warning("Tautulli not configured, skipping inactivity check")
            return

        try:
            logger.info("Starting daily inactivity check...")

            # Get user data from Tautulli (uses same usernames as our database)
            url = f"{self.services.config.tautulli_url}/api/v2"
            params = {
                "apikey": self.services.config.tautulli_token,
                "cmd": "get_users_table"
            }

            async with await self.services.api.tautulli.get(url, params=params) as response:
                if response.status != 200:
                    logger.error("Failed to fetch users from Tautulli")
                    return
                data = await response.json()
                tautulli_users = data.get('response', {}).get('data', {}).get('data', [])

            # Build lookup by friendly_name (case-insensitive)
            tautulli_lookup = {u.get('friendly_name', '').lower(): u for u in tautulli_users}

            async with get_session() as session:
                # Get all tracked Plex users
                result = await session.execute(select(PlexUser))
                tracked_users = result.scalars().all()

                for tracked_user in tracked_users:
                    # Find corresponding Tautulli user
                    tautulli_user = tautulli_lookup.get(tracked_user.plex_username.lower())

                    if not tautulli_user:
                        logger.warning(f"Tracked user {tracked_user.plex_username} not found in Tautulli - skipping (may be new)")
                        # Don't auto-delete - user might just not have watched anything yet
                        continue

                    # Get last watched from Tautulli
                    last_played = tautulli_user.get('last_seen')

                    if last_played:
                        # Convert Unix timestamp to datetime
                        last_watched = datetime.fromtimestamp(int(last_played), tz=timezone.utc)
                        created_at = tracked_user.created_at.replace(tzinfo=timezone.utc) if tracked_user.created_at.tzinfo is None else tracked_user.created_at

                        # Ignore historical watch activity that predates the current tracking entry.
                        # This prevents freshly re-approved users from being immediately re-removed
                        # because Tautulli still reports an older last_seen from before reinvite.
                        if last_watched < created_at:
                            tracked_user.days_inactive = 0
                            tracked_user.warning_sent = False
                            logger.info(
                                f"User {tracked_user.plex_username}: ignoring stale Tautulli last_seen {last_watched.isoformat()} before tracking start {created_at.isoformat()}"
                            )
                            continue

                        days_since = (datetime.now(timezone.utc) - last_watched).days

                        # Update tracked user
                        tracked_user.last_watched = last_watched
                        tracked_user.days_inactive = days_since

                        # Reset warning flag if user became active again. Use the
                        # configured threshold, not a hardcoded 25, so the reset and
                        # the warning below cannot desynchronize.
                        if days_since < self.services.config.inactivity_warning_days:
                            tracked_user.warning_sent = False

                        logger.info(f"User {tracked_user.plex_username}: {days_since} days inactive")

                        # Warning uses >= not ==: this loop runs every 24h and restarts
                        # with the bot, so an exact-day match is skipped whenever a
                        # pass is missed, and the user would then hit the removal
                        # threshold having never been warned.
                        if days_since >= self.services.config.inactivity_warning_days and not tracked_user.warning_sent:
                            await self._send_warning_dm(tracked_user)
                            tracked_user.warning_sent = True
                            # Deliberately do not remove on the same pass that warns,
                            # even if already past the removal threshold: a warning
                            # nobody had a chance to act on is not a warning.
                            continue

                        # Removal, only ever after a warning was delivered.
                        if days_since >= self.services.config.inactivity_removal_days:
                            user_id = tautulli_user.get('user_id', 0)
                            await self._remove_inactive_user(tracked_user, user_id, session)
                            continue  # User removed, skip update

                    else:
                        # No history found - might be a new user
                        if tracked_user.last_watched is None:
                            # New user with no activity yet
                            tracked_user.days_inactive = 0
                        else:
                            # Existing user with no new activity
                            last_watched_aware = tracked_user.last_watched.replace(tzinfo=timezone.utc) if tracked_user.last_watched.tzinfo is None else tracked_user.last_watched
                            days_since = (datetime.now(timezone.utc) - last_watched_aware).days
                            tracked_user.days_inactive = days_since

                            # Same warn-then-remove sequencing as the branch above.
                            if days_since >= self.services.config.inactivity_warning_days and not tracked_user.warning_sent:
                                await self._send_warning_dm(tracked_user)
                                tracked_user.warning_sent = True
                                continue

                            if days_since >= self.services.config.inactivity_removal_days:
                                user_id = tautulli_user.get('user_id', 0)
                                await self._remove_inactive_user(tracked_user, user_id, session)
                                continue

                await session.commit()
                logger.info("Daily inactivity check completed")

        except Exception as e:
            logger.error(f"Error during inactivity check: {e}", exc_info=True)

    @tasks.loop(minutes=5)
    async def auto_link_users(self):
        """Check for pending invites and auto-link when users appear on Plex/Tautulli"""
        if not self.services.config.tautulli_url:
            return

        try:
            # Load pending invites from database
            invites = await kv_get_all(INVITES_NAMESPACE)
            if not invites:
                return

            # Only contact Tautulli if something is actually waiting to be linked.
            # Invite records are never removed once they link, so the steady state is
            # a namespace where every entry is already 'linked' - and this loop was
            # fetching the entire Tautulli user table every 5 minutes (288 times a
            # day) only to skip every row it got back.
            pending = {
                discord_id_str: invite_data
                for discord_id_str, invite_data in invites.items()
                if invite_data.get('status') != 'linked' and invite_data.get('email')
            }
            if not pending:
                return

            # Get current Tautulli users with emails
            url = f"{self.services.config.tautulli_url}/api/v2"
            params = {
                "apikey": self.services.config.tautulli_token,
                "cmd": "get_users"
            }

            async with await self.services.api.tautulli.get(url, params=params) as response:
                if response.status != 200:
                    return
                data = await response.json()
                tautulli_users = data.get('response', {}).get('data', [])

            # Build email -> Tautulli user mapping (case-insensitive)
            email_to_tautulli = {}
            for user in tautulli_users:
                if user.get('email'):
                    email_to_tautulli[user['email'].lower()] = user

            # Check each pending invite. The 'linked' and missing-email cases are
            # already excluded by the `pending` filter above.
            linked_count = 0
            for discord_id_str, invite_data in list(pending.items()):
                invite_email = invite_data['email'].lower()

                # Check if this email exists in Tautulli
                if invite_email in email_to_tautulli:
                    tautulli_user = email_to_tautulli[invite_email]
                    plex_username = tautulli_user.get('friendly_name')
                    discord_id = int(discord_id_str)

                    # Auto-link this user
                    async with get_session() as session:
                        # Check if plex_username already exists
                        result = await session.execute(
                            select(PlexUser).where(PlexUser.plex_username == plex_username)
                        )
                        existing_by_plex = result.scalar_one_or_none()

                        # Check if discord_id already exists
                        result2 = await session.execute(
                            select(PlexUser).where(PlexUser.discord_id == discord_id)
                        )
                        existing_by_discord = result2.scalar_one_or_none()

                        if existing_by_plex and existing_by_plex.discord_id == discord_id:
                            # Already linked correctly
                            logger.debug(f"User {plex_username} already linked to Discord ID {discord_id}")
                        elif existing_by_plex:
                            # Plex user exists but linked to different/no Discord - update it
                            existing_by_plex.discord_id = discord_id
                            try:
                                guild = self.bot.get_guild(self.services.config.guild_id)
                                if guild:
                                    member = guild.get_member(discord_id)
                                    if member:
                                        existing_by_plex.discord_username = member.nick if member.nick else member.name
                            except (AttributeError, discord.NotFound) as e:  # Guild or member lookup failed
                                logger.debug(f"Could not get Discord member info: {e}")
                            existing_by_plex.plex_email = invite_email
                            logger.info(f"Auto-linked existing Plex user {plex_username} to Discord ID {discord_id}")
                        elif existing_by_discord:
                            # Discord user exists but with different Plex username - update Plex info
                            existing_by_discord.plex_username = plex_username
                            existing_by_discord.plex_email = invite_email
                            logger.info(f"Updated Plex username for Discord ID {discord_id} to {plex_username}")
                        else:
                            # Create new entry
                            discord_username = None
                            try:
                                guild = self.bot.get_guild(self.services.config.guild_id)
                                if guild:
                                    member = guild.get_member(discord_id)
                                    if member:
                                        discord_username = member.nick if member.nick else member.name
                            except (AttributeError, discord.NotFound) as e:  # Guild or member lookup failed
                                logger.debug(f"Could not get Discord member info: {e}")

                            new_user = PlexUser(
                                discord_id=discord_id,
                                discord_username=discord_username,
                                plex_username=plex_username,
                                plex_email=invite_email
                            )
                            session.add(new_user)
                            logger.info(f"Auto-linked new Plex user {plex_username} to Discord ID {discord_id}")

                        await session.commit()

                    # Mark invite as linked
                    invites[discord_id_str]['status'] = 'linked'
                    invites[discord_id_str]['plex_username'] = plex_username
                    linked_count += 1

            # Save updated invites to database
            if linked_count > 0:
                for discord_id_str, invite_data in invites.items():
                    await kv_set(INVITES_NAMESPACE, discord_id_str, invite_data)
                logger.info(f"Auto-linked {linked_count} user(s)")

        except Exception as e:
            logger.error(f"Error in auto_link_users: {e}", exc_info=True)

    @auto_link_users.before_loop
    async def before_auto_link_users(self):
        """Wait for bot to be ready"""
        await self.bot.wait_until_ready()

    async def _send_warning_dm(self, user: PlexUser):
        """Send 25-day inactivity warning to user"""
        if not user.discord_id:
            logger.warning(f"Cannot send warning to {user.plex_username}: No Discord account linked")
            return

        try:
            discord_user = await self.bot.fetch_user(user.discord_id)

            embed = discord.Embed(
                title="⚠️ Plex Inactivity Warning",
                description=f"Hey there! We noticed you haven't watched anything on the Plex server in **{self.services.config.inactivity_warning_days} days**.",
                color=discord.Color.orange()
            )

            embed.add_field(
                name="What happens next?",
                value=(
                    f"If you remain inactive for **{self.services.config.inactivity_removal_days - self.services.config.inactivity_warning_days} more days** ({self.services.config.inactivity_removal_days} days total), "
                    "you'll be automatically removed from the Plex server to make room for active members."
                ),
                inline=False
            )

            embed.add_field(
                name="Want to stay?",
                value="Simply watch something on Plex to reset your inactivity timer!",
                inline=False
            )

            embed.set_footer(text="This is an automated message from Plexbie")

            await send_user_dm(self.bot, self.services, discord_user, context=f"25-day inactivity warning for {user.plex_username}", embed=embed)
            logger.info(f"Sent 25-day warning to {user.plex_username} (Discord: {user.discord_username})")

        except discord.Forbidden:
            logger.warning(f"Cannot DM user {user.discord_username} - DMs are disabled")
        except Exception as e:
            logger.error(f"Error sending warning DM to {user.plex_username}: {e}")

    async def _remove_inactive_user(self, user: PlexUser, plex_user_id: int, session: AsyncSession):
        """Remove user from Plex and send farewell message"""
        try:
            # Collect stats up front (needs the account to still exist in Tautulli),
            # but do not announce anything until the removal actually succeeds -
            # otherwise a failed removal DMs a farewell on every daily pass.
            stats = await self._get_user_stats(user.plex_username)

            # Remove from Plex
            if not self.services.config.plex_username or not self.services.config.plex_password:
                logger.error(
                    f"Cannot remove {user.plex_username} from Plex: PLEX_USERNAME/PLEX_PASSWORD "
                    f"not configured. Keeping tracking row so this retries."
                )
                return

            try:
                from plexapi.myplex import MyPlexAccount

                # Both plex.tv calls are blocking; this runs inside the daily
                # loop, once per user being removed.
                account = await run_blocking(
                    MyPlexAccount,
                    self.services.config.plex_username,
                    self.services.config.plex_password,
                )
                friend_key = user.plex_email or user.plex_username
                await run_blocking(account.removeFriend, friend_key)
                logger.info(f"Removed {user.plex_username} from Plex server")
            except Exception as e:
                # Do NOT fall through to the database delete. Dropping the row
                # after a failed removal leaves the user with Plex access forever
                # and no record to retry against.
                logger.error(
                    f"Error removing {user.plex_username} from Plex: {e} - "
                    f"keeping tracking row so the next pass retries",
                    exc_info=True
                )
                return

            # Removal succeeded - now notify and clean up Discord state.
            if user.discord_id:
                await self._send_farewell_dm(user, stats)
                await self._remove_plex_role(user.discord_id)

            # Remove from database
            await session.delete(user)
            logger.info(f"Removed {user.plex_username} from tracking database")

        except Exception as e:
            logger.error(f"Error removing inactive user {user.plex_username}: {e}", exc_info=True)

    async def _send_farewell_dm(self, user: PlexUser, stats: Dict[str, Any]):
        """Send farewell message with user stats"""
        if not user.discord_id:
            return

        try:
            discord_user = await self.bot.fetch_user(user.discord_id)

            embed = discord.Embed(
                title="👋 Farewell from Plex",
                description=(
                    f"Thank you for being part of our Plex community, **{user.discord_username}**! "
                    f"Due to {self.services.config.inactivity_removal_days} days of inactivity, it's time to say goodbye."
                ),
                color=discord.Color.red()
            )

            # Add user stats
            if stats:
                stats_text = []

                if stats.get('total_plays'):
                    stats_text.append(f"**Total Plays:** {stats['total_plays']:,}")

                if stats.get('total_time'):
                    hours = stats['total_time'] // 3600
                    stats_text.append(f"**Total Watch Time:** {hours:,} hours")

                if stats.get('last_watched'):
                    stats_text.append(f"**Last Watched:** {stats['last_watched']}")

                if stats.get('favorite_media'):
                    stats_text.append(f"**Most Watched:** {stats['favorite_media']}")

                if stats_text:
                    embed.add_field(
                        name="📊 Your Stats",
                        value=truncate_field("\n".join(stats_text)),
                        inline=False
                    )

            embed.add_field(
                name="Want to come back?",
                value=(
                    "You're still welcome in the Discord! If you'd like to rejoin Plex in the future, "
                    "just use the `/join-plex` command to request access again."
                ),
                inline=False
            )

            embed.set_footer(text="Thanks for the memories! 🎬")

            await send_user_dm(self.bot, self.services, discord_user, context=f"inactivity removal farewell for {user.plex_username}", embed=embed)
            logger.info(f"Sent farewell message to {user.plex_username} (Discord: {user.discord_username})")

        except discord.Forbidden:
            logger.warning(f"Cannot DM user {user.discord_username} - DMs are disabled")
        except Exception as e:
            logger.error(f"Error sending farewell DM to {user.plex_username}: {e}")

    async def _get_user_stats(self, plex_username: str) -> Dict[str, Any]:
        """Get user statistics from Tautulli"""
        if not self.services.config.tautulli_url:
            return {}

        try:
            url = f"{self.services.config.tautulli_url}/api/v2"
            params = {
                "apikey": self.services.config.tautulli_token,
                "cmd": "get_user",
                "user": plex_username
            }

            async with await self.services.api.tautulli.get(url, params=params) as response:
                if response.status != 200:
                    return {}

                data = await response.json()
                user_data = data.get('response', {}).get('data', {})

                # Get user's watch history for "most watched"
                history_params = {
                    "apikey": self.services.config.tautulli_token,
                    "cmd": "get_history",
                    "user": plex_username,
                    "length": 1
                }

                async with await self.services.api.tautulli.get(url, params=history_params) as hist_response:
                    history_data = await hist_response.json()
                    history_item = history_data.get('response', {}).get('data', {}).get('data', [])
                    last_watched_item = history_item[0] if history_item else None

                return {
                    'total_plays': user_data.get('plays', 0),
                    'total_time': user_data.get('duration', 0),
                    'last_watched': last_watched_item.get('full_title') if last_watched_item else None,
                    'favorite_media': user_data.get('most_watched', {}).get('title')
                }

        except Exception as e:
            logger.error(f"Error getting stats for {plex_username}: {e}")
            return {}

    async def _remove_plex_role(self, discord_id: int):
        """Remove Plex member role from Discord user"""
        if not self.services.config.plex_member_role_id:
            logger.warning("Plex member role ID not configured")
            return

        try:
            guild = self.bot.get_guild(self.services.config.guild_id)
            if not guild:
                logger.error("Guild not found")
                return

            member = guild.get_member(discord_id)
            if not member:
                logger.warning(f"Member {discord_id} not found in guild")
                return

            role = guild.get_role(self.services.config.plex_member_role_id)
            if not role:
                logger.warning(f"Plex member role {self.services.config.plex_member_role_id} not found")
                return

            await member.remove_roles(role, reason="Removed due to inactivity on Plex")
            logger.info(f"Removed Plex role from {member.name}")

        except Exception as e:
            logger.error(f"Error removing Plex role from user {discord_id}: {e}")

    @check_inactive_users.before_loop
    async def before_check_inactive_users(self):
        """Wait for bot to be ready before starting checks"""
        await self.bot.wait_until_ready()

    # Admin command for manual removal
    @app_commands.command(name="removeuser", description="Manually remove a user from Plex (Admin only)")
    @app_commands.describe(plex_username="The Plex username to remove")
    @app_commands.checks.has_permissions(administrator=True)
    async def remove_user(self, interaction: discord.Interaction, plex_username: str):
        """Manually remove a user from Plex with notification"""
        await interaction.response.defer(ephemeral=True)

        try:
            if not self.services.plex_server:
                await interaction.followup.send("❌ Plex server not configured", ephemeral=True)
                return

            async with get_session() as session:
                # Find user in tracking
                result = await session.execute(
                    select(PlexUser).where(PlexUser.plex_username == plex_username)
                )
                tracked_user = result.scalar_one_or_none()

                if not tracked_user:
                    await interaction.followup.send(
                        f"❌ User `{plex_username}` not found in tracking database.\n"
                        f"They may not be tracked or may not exist on the Plex server.",
                        ephemeral=True
                    )
                    return

                # Find Plex user
                plex_users = await run_blocking(self.services.plex_server.systemAccounts)
                plex_user = next((u for u in plex_users if u.name == plex_username), None)

                if not plex_user:
                    await interaction.followup.send(
                        f"❌ Plex user `{plex_username}` not found on server",
                        ephemeral=True
                    )
                    return

                # Collect stats up front (the account must still exist in Tautulli),
                # but do not tell the user anything until the removal has actually
                # succeeded. The DM is titled "Removed from Plex Server", so sending
                # it first meant a failed removal left the user believing they had
                # lost access while they still had it. Same ordering already fixed
                # in the automatic path, _remove_inactive_user.
                stats = await self._get_user_stats(plex_username)

                # Remove from Plex
                try:
                    from plexapi.myplex import MyPlexAccount

                    account = await run_blocking(
                        MyPlexAccount,
                        self.services.config.plex_username,
                        self.services.config.plex_password,
                    )
                    friend_key = tracked_user.plex_email or plex_username
                    await run_blocking(account.removeFriend, friend_key)
                    logger.info(f"Manually removed {plex_username} from Plex by {interaction.user.name}")
                except Exception as e:
                    logger.error(f"Error removing {plex_username} from Plex: {e}")
                    await interaction.followup.send(
                        f"❌ Error removing user from Plex: {str(e)}",
                        ephemeral=True
                    )
                    return

                # Removal succeeded - now notify and clean up Discord state.
                if tracked_user.discord_id:
                    await self._send_manual_removal_dm(tracked_user, stats, interaction.user.name)
                    await self._remove_plex_role(tracked_user.discord_id)

                # Remove from database
                await session.delete(tracked_user)
                await session.commit()

                await interaction.followup.send(
                    f"✅ Successfully removed `{plex_username}` from Plex server and tracking database.\n"
                    f"User has been notified via DM.",
                    ephemeral=True
                )

        except Exception as e:
            logger.error(f"Error in manual user removal: {e}", exc_info=True)
            await interaction.followup.send(
                f"❌ Error removing user: {str(e)}",
                ephemeral=True
            )

    async def _send_manual_removal_dm(self, user: PlexUser, stats: Dict[str, Any], removed_by: str):
        """Send DM for manual removal"""
        if not user.discord_id:
            return

        try:
            discord_user = await self.bot.fetch_user(user.discord_id)

            embed = discord.Embed(
                title="⚠️ Removed from Plex Server",
                description=(
                    f"You have been removed from the Plex server.\n\n"
                    f"If you weren't aware of this or would like more information about why, "
                    f"please reach out to **{removed_by}**."
                ),
                color=discord.Color.orange()
            )

            # Add user stats
            if stats:
                stats_text = []

                if stats.get('total_plays'):
                    stats_text.append(f"**Total Plays:** {stats['total_plays']:,}")

                if stats.get('total_time'):
                    hours = stats['total_time'] // 3600
                    stats_text.append(f"**Total Watch Time:** {hours:,} hours")

                if stats.get('last_watched'):
                    stats_text.append(f"**Last Watched:** {stats['last_watched']}")

                if stats.get('favorite_media'):
                    stats_text.append(f"**Most Watched:** {stats['favorite_media']}")

                if stats_text:
                    embed.add_field(
                        name="📊 Your Stats",
                        value=truncate_field("\n".join(stats_text)),
                        inline=False
                    )

            embed.add_field(
                name="Want to come back?",
                value=(
                    "You're still welcome in the Discord! If you'd like to rejoin Plex in the future, "
                    "just use the `/join-plex` command to request access again."
                ),
                inline=False
            )

            embed.set_footer(text="This action was performed by a server administrator")

            await send_user_dm(self.bot, self.services, discord_user, context=f"manual Plex removal for {user.plex_username} by {removed_by}", embed=embed)
            logger.info(f"Sent manual removal notification to {user.plex_username} (Discord: {user.discord_username})")

        except discord.Forbidden:
            logger.warning(f"Cannot DM user {user.discord_username} - DMs are disabled")
        except Exception as e:
            logger.error(f"Error sending manual removal DM to {user.plex_username}: {e}")

    # ===== User Linking System =====

    @app_commands.command(name="list-tracked-users", description="List all tracked Plex users in database (Admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    async def list_tracked_users(self, interaction: discord.Interaction):
        """List all users being tracked in the database"""
        await interaction.response.defer(ephemeral=True)

        try:
            async with get_session() as session:
                result = await session.execute(select(PlexUser))
                tracked_users = result.scalars().all()

            if not tracked_users:
                await interaction.followup.send("No tracked users found.", ephemeral=True)
                return

            # Get Plex users to check for orphans
            plex_users = await run_blocking(self.services.plex_server.systemAccounts) if self.services.plex_server else []
            plex_usernames = {u.name for u in plex_users}

            embed = discord.Embed(
                title="📊 Tracked Plex Users",
                description=f"Total: {len(tracked_users)} users in database",
                color=discord.Color.blue()
            )

            # Count orphans across ALL tracked users, not just the visible page -
            # computing it inside the display loop understated the real number.
            orphaned_count = sum(
                1 for user in tracked_users if user.plex_username not in plex_usernames
            )

            shown = tracked_users[:MAX_LISTED_USERS]
            for user in shown:
                discord_info = f"<@{user.discord_id}>" if user.discord_id else "❌ Not linked"
                status_emoji = "🟢" if user.days_inactive < 25 else "🟡" if user.days_inactive < 30 else "🔴"

                # Check if orphaned
                is_orphaned = user.plex_username not in plex_usernames
                if is_orphaned:
                    status_emoji = "⚠️"

                user_info = f"**Plex:** `{user.plex_username}`"
                if is_orphaned:
                    user_info += " ⚠️ **NOT ON PLEX**"
                user_info += f"\n**Discord:** {discord_info}\n"
                user_info += f"**Inactive:** {status_emoji} {user.days_inactive} days"

                if user.last_watched:
                    user_info += f"\n**Last Watch:** <t:{int(user.last_watched.timestamp())}:R>"

                embed.add_field(
                    name=f"ID: {user.id}",
                    value=user_info,
                    inline=False
                )

            # Footer must state what actually happens. The inactivity check does NOT
            # delete orphans - it logs "skipping (may be new)" and continues - so
            # claiming auto-removal left them to accumulate while the admin was
            # told they were handled.
            notes = []
            if len(tracked_users) > len(shown):
                notes.append(f"Showing first {len(shown)} of {len(tracked_users)}")
            if orphaned_count > 0:
                notes.append(
                    f"⚠️ {orphaned_count} not on Plex - remove with /removeuser "
                    f"(not cleaned up automatically)"
                )
            if notes:
                embed.set_footer(text=" · ".join(notes))

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error listing tracked users: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

    @app_commands.command(name="list-plex-users", description="List all Plex users (Admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    async def list_plex_users(self, interaction: discord.Interaction):
        """List all Plex users with details to find problematic accounts"""
        await interaction.response.defer(ephemeral=True)

        try:
            if not self.services.plex_server:
                await interaction.followup.send("❌ Plex server not configured", ephemeral=True)
                return

            # Get all Plex users
            plex_users = await run_blocking(self.services.plex_server.systemAccounts)

            embed = discord.Embed(
                title="📋 All Plex Users",
                description=f"Total: {len(plex_users)} users",
                color=discord.Color.blue()
            )

            valid_users = []
            invalid_users = []

            for user in plex_users:
                user_info = f"**ID:** {user.id}\n**Name:** `{repr(user.name)}`"

                # Check if valid
                is_valid = user.name and user.name.strip() and len(user.name) <= 100

                if hasattr(user, 'email') and user.email:
                    user_info += f"\n**Email:** {user.email}"

                if is_valid:
                    valid_users.append(user_info)
                else:
                    issues = []
                    if not user.name or not user.name.strip():
                        issues.append("Empty/blank name")
                    if user.name and len(user.name) > 100:
                        issues.append(f"Too long ({len(user.name)} chars)")
                    user_info += f"\n⚠️ **Issues:** {', '.join(issues)}"
                    invalid_users.append(user_info)

            # Show invalid users first (if any)
            if invalid_users:
                embed.add_field(
                    name="❌ Invalid Users (Should be removed)",
                    # The "max 10 fits" assumption was wrong: entries describing
                    # over-long usernames can exceed the 1024-char field limit.
                    value=truncate_field("\n\n".join(invalid_users[:10])),
                    inline=False
                )

            # Show valid users
            if valid_users:
                # Split into chunks if too many
                for i in range(0, min(len(valid_users), 15), 5):  # Max 15 valid users shown
                    chunk = valid_users[i:i+5]
                    embed.add_field(
                        name=f"✅ Valid Users ({i+1}-{i+len(chunk)})",
                        value=truncate_field("\n\n".join(chunk)),
                        inline=False
                    )

            if invalid_users:
                embed.set_footer(text="Use /cleanup-plex-users to see removal instructions")

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error listing Plex users: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

    @app_commands.command(name="cleanup-plex-users", description="List invalid Plex users for manual cleanup (Admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    async def cleanup_plex_users(self, interaction: discord.Interaction):
        """List invalid Plex system accounts that should be manually removed from Plex"""
        await interaction.response.defer(ephemeral=True)

        try:
            if not self.services.plex_server:
                await interaction.followup.send("❌ Plex server not configured", ephemeral=True)
                return

            # Get all Plex users
            plex_users = await run_blocking(self.services.plex_server.systemAccounts)

            invalid_users = []
            protected_ids = [1]  # Protect admin account (ID 1)

            for user in plex_users:
                # Skip protected accounts
                if user.id in protected_ids:
                    continue

                # Check if invalid
                is_invalid = False
                reasons = []

                if not user.name or not user.name.strip():
                    is_invalid = True
                    reasons.append("Empty/blank username")

                if user.name and len(user.name) > 100:
                    is_invalid = True
                    reasons.append(f"Username too long ({len(user.name)} chars)")

                if is_invalid:
                    invalid_users.append({
                        'id': user.id,
                        'name': repr(user.name),
                        'reasons': reasons
                    })

            if not invalid_users:
                await interaction.followup.send("✅ No invalid Plex users found!", ephemeral=True)
                return

            # Build result message
            embed = discord.Embed(
                title="⚠️ Invalid Plex Users Found",
                description=(
                    f"Found {len(invalid_users)} invalid user(s) that should be manually removed.\n\n"
                    "**Note:** System accounts cannot be deleted via the Plex API. "
                    "You must remove these users manually through the Plex web interface:\n"
                    "Settings → Users → [User] → Remove Access"
                ),
                color=discord.Color.orange()
            )

            # List invalid users
            user_list = []
            for user in invalid_users[:10]:  # Limit to 10 to fit in embed
                reasons_str = ", ".join(user['reasons'])
                user_list.append(f"**ID {user['id']}:** {user['name']}\n└─ Issues: {reasons_str}")

            embed.add_field(
                name=f"Users to Remove ({len(invalid_users)} total)",
                # Clamped: an over-long value is rejected with HTTPException 400,
                # which made this command fail whenever it had findings - and the
                # long-username case is exactly what it looks for.
                value=truncate_field("\n\n".join(user_list)),
                inline=False
            )

            if len(invalid_users) > 10:
                embed.set_footer(text=f"Showing first 10 of {len(invalid_users)} invalid users")

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error listing invalid Plex users: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

    @app_commands.command(name="manage-links", description="Manage Discord-Plex user links (Admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    async def manage_links(self, interaction: discord.Interaction):
        """Open the user link management panel"""
        await interaction.response.defer(ephemeral=True)

        try:
            # Create the control panel
            view = UserLinkControlPanel(self.bot, self.services)

            embed = discord.Embed(
                title="🔗 User Link Management",
                description="Manage Discord to Plex account links",
                color=discord.Color.blue()
            )

            embed.add_field(
                name="📋 View Links",
                value="See all current Discord-Plex links",
                inline=False
            )

            embed.add_field(
                name="➕ Link User",
                value="Link a Discord user to their Plex account",
                inline=False
            )

            embed.add_field(
                name="➖ Unlink User",
                value="Remove an existing Discord-Plex link",
                inline=False
            )

            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        except Exception as e:
            logger.error(f"Error opening manage-links panel: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error opening link management: {str(e)}", ephemeral=True)

    async def _get_unlinked_discord_users(self, guild: discord.Guild) -> list:
        """Get Discord users that are not yet linked to a Plex account"""
        async with get_session() as session:
            # Get all linked Discord IDs
            result = await session.execute(
                select(PlexUser.discord_id).where(PlexUser.discord_id.isnot(None))
            )
            linked_ids = {row[0] for row in result.all()}

        # Filter guild members
        unlinked = []
        for member in guild.members:
            if member.bot:
                continue
            if member.id not in linked_ids:
                # Format: "username (nickname)" or just "username" if no nickname
                display = f"{member.name}"
                if member.nick:
                    display += f" ({member.nick})"
                unlinked.append((member.id, display))

        return unlinked[:25]  # Discord limit

    async def _get_unlinked_plex_users(self) -> list:
        """Get Plex/Tautulli users that are not yet linked to a Discord account"""
        # Get users from Tautulli (more reliable usernames than Plex system accounts)
        if not self.services.config.tautulli_url:
            return []

        async with get_session() as session:
            # Get Plex usernames that ARE linked to a Discord account
            result = await session.execute(
                select(PlexUser.plex_username).where(PlexUser.discord_id.isnot(None))
            )
            linked_usernames = {row[0].lower() for row in result.all()}  # Case-insensitive

        try:
            # Fetch users from Tautulli
            url = f"{self.services.config.tautulli_url}/api/v2"
            params = {
                "apikey": self.services.config.tautulli_token,
                "cmd": "get_users"
            }

            async with await self.services.api.tautulli.get(url, params=params) as response:
                if response.status != 200:
                    return []
                data = await response.json()
                tautulli_users = data.get('response', {}).get('data', [])

            unlinked = []
            for user in tautulli_users:
                friendly_name = user.get('friendly_name', '')
                user_id = user.get('user_id', 0)

                # Filter out invalid usernames (empty, too long, or already linked)
                if (friendly_name and
                    friendly_name.strip() and
                    len(friendly_name) <= 100 and
                    friendly_name.lower() not in linked_usernames):
                    # Use tuple: (user_id, display_name, username)
                    unlinked.append((user_id, friendly_name, friendly_name))

            return unlinked[:25]  # Discord limit

        except Exception as e:
            logger.error(f"Error fetching Tautulli users: {e}")
            return []


class UserLinkControlPanel(AdminOnlyView):
    """Main control panel for user link management"""

    def __init__(self, bot: commands.Bot, services: BotServices):
        super().__init__(timeout=300)
        self.bot = bot
        self.services = services

    @discord.ui.button(label="View Links", style=discord.ButtonStyle.primary, emoji="📋")
    async def view_links(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show all current links"""
        await interaction.response.defer(ephemeral=True)

        try:
            async with get_session() as session:
                result = await session.execute(
                    select(PlexUser).where(PlexUser.discord_id.isnot(None))
                )
                linked_users = result.scalars().all()

            if not linked_users:
                await interaction.followup.send("No linked users found.", ephemeral=True)
                return

            embed = discord.Embed(
                title="📋 Current Discord-Plex Links",
                description=f"Found {len(linked_users)} linked accounts",
                color=discord.Color.blue()
            )

            # Group into fields (max 25)
            for user in linked_users[:25]:
                discord_mention = f"<@{user.discord_id}>"
                embed.add_field(
                    name=f"{user.discord_username or 'Unknown'}",
                    value=f"{discord_mention} ↔️ `{user.plex_username}`",
                    inline=False
                )

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error viewing links: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

    @discord.ui.button(label="Link User", style=discord.ButtonStyle.success, emoji="➕")
    async def link_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Start the linking process"""
        await interaction.response.defer(ephemeral=True)

        try:
            # Get bot's cog for helper methods
            cog = self.bot.get_cog("UserMgmtCog")
            if not cog:
                await interaction.followup.send("❌ User management system not loaded", ephemeral=True)
                return

            # Get unlinked users
            guild = interaction.guild
            unlinked_discord = await cog._get_unlinked_discord_users(guild)
            unlinked_plex = await cog._get_unlinked_plex_users()

            if not unlinked_discord:
                await interaction.followup.send("✅ All Discord users are already linked!", ephemeral=True)
                return

            if not unlinked_plex:
                await interaction.followup.send("✅ All Plex users are already linked!", ephemeral=True)
                return

            # Show link selection view
            view = LinkUserView(self.bot, self.services, unlinked_discord, unlinked_plex)

            embed = discord.Embed(
                title="➕ Link Discord User to Plex Account",
                description="Select a Discord user and their corresponding Plex account",
                color=discord.Color.green()
            )

            embed.add_field(
                name="Step 1",
                value="Select the Discord user from the dropdown below",
                inline=False
            )

            embed.add_field(
                name="Step 2",
                value="Select their Plex account from the second dropdown",
                inline=False
            )

            embed.add_field(
                name="Step 3",
                value="Click 'Confirm Link' to save",
                inline=False
            )

            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        except Exception as e:
            logger.error(f"Error starting link process: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

    @discord.ui.button(label="Unlink User", style=discord.ButtonStyle.danger, emoji="➖")
    async def unlink_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Remove an existing link"""
        await interaction.response.defer(ephemeral=True)

        try:
            async with get_session() as session:
                result = await session.execute(
                    select(PlexUser).where(PlexUser.discord_id.isnot(None))
                )
                linked_users = result.scalars().all()

            if not linked_users:
                await interaction.followup.send("No linked users to unlink.", ephemeral=True)
                return

            # Create unlink view
            view = UnlinkUserView(self.bot, self.services, linked_users)

            embed = discord.Embed(
                title="➖ Unlink Discord-Plex Account",
                description="Select a user to unlink",
                color=discord.Color.red()
            )

            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        except Exception as e:
            logger.error(f"Error starting unlink process: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)


class LinkUserView(AdminOnlyView):
    """View for linking a Discord user to Plex account"""

    def __init__(self, bot: commands.Bot, services: BotServices, discord_users: list, plex_users: list):
        super().__init__(timeout=300)
        self.bot = bot
        self.services = services
        self.selected_discord_id = None
        self.selected_plex_user_id = None
        self.selected_plex_username = None

        # Add Discord user select
        discord_select = DiscordUserSelect(discord_users)
        discord_select.callback = self.discord_user_selected
        self.add_item(discord_select)

        # Add Plex user select
        plex_select = PlexUserSelect(plex_users)
        plex_select.callback = self.plex_user_selected
        self.add_item(plex_select)

    async def discord_user_selected(self, interaction: discord.Interaction):
        """Called when Discord user is selected"""
        select = [item for item in self.children if isinstance(item, DiscordUserSelect)][0]
        self.selected_discord_id = int(select.values[0])

        await interaction.response.send_message(
            f"✅ Discord user selected: <@{self.selected_discord_id}>",
            ephemeral=True
        )

    async def plex_user_selected(self, interaction: discord.Interaction):
        """Called when Plex user is selected"""
        select = [item for item in self.children if isinstance(item, PlexUserSelect)][0]
        self.selected_plex_user_id = int(select.values[0])

        # Look up the Plex username from the Plex server using the ID
        try:
            plex_users = await run_blocking(self.services.plex_server.systemAccounts)
            plex_user = next((u for u in plex_users if u.id == self.selected_plex_user_id), None)
            if plex_user:
                self.selected_plex_username = plex_user.name
            else:
                await interaction.response.send_message(
                    "❌ Error: Could not find Plex user",
                    ephemeral=True
                )
                return
        except Exception as e:
            logger.error(f"Error looking up Plex user: {e}", exc_info=True)
            await interaction.response.send_message(
                f"❌ Error looking up Plex user: {str(e)}",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"✅ Plex user selected: `{self.selected_plex_username}`",
            ephemeral=True
        )

    @discord.ui.button(label="Confirm Link", style=discord.ButtonStyle.success, row=2)
    async def confirm_link(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Confirm and create the link"""
        if not self.selected_discord_id or not self.selected_plex_username:
            await interaction.response.send_message(
                "❌ Please select both a Discord user and a Plex account first",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            async with get_session() as session:
                # Check if Plex user already exists
                result = await session.execute(
                    select(PlexUser).where(PlexUser.plex_username == self.selected_plex_username)
                )
                existing_user = result.scalar_one_or_none()

                # Get Discord user info
                discord_user = await self.bot.fetch_user(self.selected_discord_id)

                if existing_user:
                    # Update existing Plex user with Discord link
                    existing_user.discord_id = self.selected_discord_id
                    existing_user.discord_username = discord_user.name
                else:
                    # Create new PlexUser entry
                    new_user = PlexUser(
                        discord_id=self.selected_discord_id,
                        discord_username=discord_user.name,
                        plex_username=self.selected_plex_username
                    )
                    session.add(new_user)

                await session.commit()

            # Success message
            embed = discord.Embed(
                title="✅ Link Created Successfully",
                description="Discord user has been linked to Plex account",
                color=discord.Color.green()
            )

            embed.add_field(
                name="Discord User",
                value=f"<@{self.selected_discord_id}> ({discord_user.name})",
                inline=True
            )

            embed.add_field(
                name="Plex Account",
                value=f"`{self.selected_plex_username}`",
                inline=True
            )

            await interaction.followup.send(embed=embed, ephemeral=True)

            # Disable all buttons
            for item in self.children:
                item.disabled = True

            # Try to edit the message, but don't fail if it's gone
            try:
                await interaction.message.edit(view=self)
            except discord.NotFound:
                logger.debug("Original message not found, skipping view update")
            except Exception as edit_error:
                logger.warning(f"Could not update view: {edit_error}")

            logger.info(f"Linked Discord user {discord_user.name} ({self.selected_discord_id}) to Plex user {self.selected_plex_username}")

        except Exception as e:
            logger.error(f"Error creating link: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error creating link: {str(e)}", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Cancel the linking process"""
        await interaction.response.send_message("❌ Linking cancelled", ephemeral=True)

        # Disable all buttons
        for item in self.children:
            item.disabled = True

        # Try to edit the message, but don't fail if it's gone
        try:
            await interaction.message.edit(view=self)
        except (discord.NotFound, Exception) as e:
            logger.debug(f"Could not update view on cancel: {e}")


class UnlinkUserView(AdminOnlyView):
    """View for unlinking a user"""

    def __init__(self, bot: commands.Bot, services: BotServices, linked_users: list):
        super().__init__(timeout=300)
        self.bot = bot
        self.services = services
        self.selected_user_id = None

        # Create select options
        options = []
        for user in linked_users[:25]:
            label = f"{user.discord_username} ↔ {user.plex_username}"
            options.append(discord.SelectOption(
                label=label[:100],  # Discord max length
                value=str(user.id),
                description=f"Discord ID: {user.discord_id}"[:100]
            ))

        select = discord.ui.Select(
            placeholder="Select a linked user to unlink",
            options=options
        )
        select.callback = self.user_selected
        self.add_item(select)

    async def user_selected(self, interaction: discord.Interaction):
        """Called when user is selected"""
        # Find the select menu among children
        select = next((item for item in self.children if isinstance(item, discord.ui.Select)), None)
        if select and select.values:
            self.selected_user_id = int(select.values[0])

        await interaction.response.send_message(
            "✅ User selected. Click 'Confirm Unlink' to remove the link.",
            ephemeral=True
        )

    @discord.ui.button(label="Confirm Unlink", style=discord.ButtonStyle.danger, row=1)
    async def confirm_unlink(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Confirm and remove the link"""
        if not self.selected_user_id:
            await interaction.response.send_message(
                "❌ Please select a user first",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            async with get_session() as session:
                result = await session.execute(
                    select(PlexUser).where(PlexUser.id == self.selected_user_id)
                )
                user = result.scalar_one_or_none()

                if not user:
                    await interaction.followup.send("❌ User not found", ephemeral=True)
                    return

                # Store info for confirmation message
                discord_username = user.discord_username
                plex_username = user.plex_username
                discord_id = user.discord_id

                # Remove Discord link (keep Plex user entry for tracking)
                user.discord_id = None
                user.discord_username = None

                await session.commit()

            # Success message
            embed = discord.Embed(
                title="✅ Link Removed Successfully",
                description="Discord-Plex link has been removed",
                color=discord.Color.orange()
            )

            embed.add_field(
                name="Discord User",
                value=f"<@{discord_id}> ({discord_username})",
                inline=True
            )

            embed.add_field(
                name="Plex Account",
                value=f"`{plex_username}`",
                inline=True
            )

            await interaction.followup.send(embed=embed, ephemeral=True)

            # Disable all buttons
            for item in self.children:
                item.disabled = True

            # Try to edit the message, but don't fail if it's gone
            try:
                await interaction.message.edit(view=self)
            except discord.NotFound:
                logger.debug("Original message not found, skipping view update")
            except Exception as edit_error:
                logger.warning(f"Could not update view: {edit_error}")

            logger.info(f"Unlinked Discord user {discord_username} ({discord_id}) from Plex user {plex_username}")

        except Exception as e:
            logger.error(f"Error removing link: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error removing link: {str(e)}", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Cancel the unlinking process"""
        await interaction.response.send_message("❌ Unlinking cancelled", ephemeral=True)

        # Disable all buttons
        for item in self.children:
            item.disabled = True

        # Try to edit the message, but don't fail if it's gone
        try:
            await interaction.message.edit(view=self)
        except (discord.NotFound, Exception) as e:
            logger.debug(f"Could not update view on cancel: {e}")


class DiscordUserSelect(discord.ui.Select):
    """Select menu for Discord users"""

    def __init__(self, users: list):
        options = []
        for user_id, display_name in users:
            options.append(discord.SelectOption(
                label=display_name[:100],
                value=str(user_id),
                description=f"ID: {user_id}"
            ))

        super().__init__(
            placeholder="Select Discord user",
            options=options,
            row=0
        )


class PlexUserSelect(discord.ui.Select):
    """Select menu for Plex users"""

    def __init__(self, users: list):
        options = []
        for plex_user_id, display_name, plex_username in users:
            options.append(discord.SelectOption(
                label=display_name[:100],
                value=str(plex_user_id)  # Use ID for uniqueness
            ))

        super().__init__(
            placeholder="Select Plex account",
            options=options,
            row=1
        )


async def setup(bot: commands.Bot):
    """Setup function for loading cog"""
    await bot.add_cog(UserMgmtCog(bot, bot.services))
