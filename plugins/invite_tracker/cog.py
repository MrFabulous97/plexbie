# path: plugins/invite_tracker/cog.py
"""Invite tracking plugin - Auto-assign roles based on who invited"""
import discord
from discord import app_commands
from discord.ext import commands
from typing import Dict
from datetime import datetime, timezone
from sqlalchemy import select

from core.logging import get_logger
from core.services import BotServices
from database.session import get_session
from utils.embeds import create_error_embed
from .models import InviteTracker, InviteUse

logger = get_logger(__name__)


class InviteTrackerCog(commands.Cog):
    """Track invites and auto-assign roles based on inviter"""

    def __init__(self, bot: commands.Bot, services: BotServices):
        self.bot = bot
        self.services = services
        self.invite_cache: Dict[str, Dict[str, discord.Invite]] = {}  # {guild_id: {code: invite}}

    async def cog_load(self):
        """Called when cog is loaded - cache all current invites"""
        logger.info("Invite tracker cog loaded, will cache invites when bot is ready")

    @commands.Cog.listener()
    async def on_ready(self):
        """Cache invites when bot is ready and connected"""
        logger.info("Loading invite cache...")
        try:
            for guild in self.bot.guilds:
                await self._update_invite_cache(guild)
            logger.info(f"✅ Invite cache loaded for {len(self.bot.guilds)} guild(s)")
        except Exception as e:
            logger.error(f"Error loading invite cache: {e}")

    async def _update_invite_cache(self, guild: discord.Guild):
        """Update invite cache for a guild"""
        try:
            invites = await guild.invites()
            self.invite_cache[str(guild.id)] = {invite.code: invite for invite in invites}

            # Also save to database
            async with get_session() as session:
                for invite in invites:
                    # Check if invite already exists
                    result = await session.execute(
                        select(InviteTracker).where(
                            InviteTracker.guild_id == str(guild.id),
                            InviteTracker.invite_code == invite.code
                        )
                    )
                    existing = result.scalar_one_or_none()

                    if not existing:
                        # Create new tracker entry
                        tracker = InviteTracker(
                            guild_id=str(guild.id),
                            invite_code=invite.code,
                            inviter_id=str(invite.inviter.id) if invite.inviter else "0",
                            inviter_name=str(invite.inviter) if invite.inviter else "Unknown",
                            uses=invite.uses,
                            max_uses=invite.max_uses,
                            created_at=invite.created_at or datetime.now(timezone.utc),
                            expires_at=invite.expires_at,
                            is_temporary=invite.temporary
                        )
                        session.add(tracker)
                    else:
                        # Update uses count
                        existing.uses = invite.uses

                await session.commit()

        except discord.Forbidden:
            logger.warning(f"No permission to fetch invites for guild {guild.id}")
        except Exception as e:
            logger.error(f"Error updating invite cache for {guild.name}: {e}")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Detect which invite was used and assign role if invited by bot owner"""
        if member.bot:
            return

        guild = member.guild
        logger.info(f"Member {member} joined {guild.name}")

        try:
            # Get bot owner ID from config
            bot_owner_id = self.services.config.bot_owner_id
            homies_role_id = self.services.config.homies_role_id

            if not bot_owner_id or not homies_role_id:
                logger.warning("BOT_OWNER_ID or HOMIES_ROLE_ID not configured in .env")
                return

            # Get current invites
            current_invites = await guild.invites()
            cached_invites = self.invite_cache.get(str(guild.id), {})

            logger.debug(f"Current invites: {len(current_invites)}, Cached invites: {len(cached_invites)}")

            # Find which invite was used by comparing use counts
            used_invite = None
            for current_invite in current_invites:
                cached_invite = cached_invites.get(current_invite.code)

                if cached_invite and current_invite.uses > cached_invite.uses:
                    used_invite = current_invite
                    logger.debug(f"Found used invite: {current_invite.code} (uses: {cached_invite.uses} -> {current_invite.uses})")
                    break

            if not used_invite:
                logger.warning(f"Could not determine which invite {member} used (cached: {len(cached_invites)}, current: {len(current_invites)})")
                # Update cache anyway
                await self._update_invite_cache(guild)
                return

            logger.info(f"{member} joined via invite {used_invite.code} created by {used_invite.inviter}")

            # Assign the auto-role first, then record the outcome. The previous
            # order added the row and flushed it - taking SQLite's write lock -
            # and then made a Discord REST call with that lock still held, which
            # blocks every other plugin's writes for the duration of the call.
            auto_role_assigned = False
            assigned_role_id = None

            if used_invite.inviter and used_invite.inviter.id == bot_owner_id:
                role = guild.get_role(homies_role_id)

                if role:
                    try:
                        await member.add_roles(role, reason=f"Auto-role: Invited by bot owner")
                        logger.info(f"✅ Assigned {role.name} role to {member} (invited by bot owner)")
                        auto_role_assigned = True
                        assigned_role_id = str(role.id)

                    except discord.Forbidden:
                        logger.error(f"❌ No permission to assign role {role.name} to {member}")
                    except Exception as e:
                        logger.error(f"❌ Error assigning role: {e}")
                else:
                    logger.warning(f"⚠️ Homies role (ID: {homies_role_id}) not found in guild")
            else:
                logger.info(f"Member was not invited by bot owner, no auto-role assigned")

            # Record the invite use and the role outcome in one short write.
            async with get_session() as session:
                session.add(InviteUse(
                    guild_id=str(guild.id),
                    invite_code=used_invite.code,
                    inviter_id=str(used_invite.inviter.id) if used_invite.inviter else "0",
                    inviter_name=str(used_invite.inviter) if used_invite.inviter else "Unknown",
                    joiner_id=str(member.id),
                    joiner_name=str(member),
                    joined_at=datetime.now(timezone.utc),
                    auto_role_assigned=auto_role_assigned,
                    role_id=assigned_role_id,
                ))
                await session.commit()

            # Update cache
            await self._update_invite_cache(guild)

        except discord.Forbidden:
            logger.error(f"No permission to fetch invites for {guild.name}")
        except Exception as e:
            logger.error(f"Error processing member join: {e}", exc_info=e)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        """Update cache when new invite is created"""
        logger.info(f"Invite {invite.code} created in {invite.guild.name}")
        await self._update_invite_cache(invite.guild)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        """Update cache when invite is deleted"""
        logger.info(f"Invite {invite.code} deleted from {invite.guild.name}")
        if str(invite.guild.id) in self.invite_cache:
            self.invite_cache[str(invite.guild.id)].pop(invite.code, None)

    # Admin Command - Only accessible by users with ADMIN_ROLE_ID

    def _has_admin_role(self, interaction: discord.Interaction) -> bool:
        """Check if user has the admin role"""
        admin_role_id = self.services.config.admin_role_id
        if not admin_role_id:
            return False

        member = interaction.guild.get_member(interaction.user.id)
        if not member:
            return False

        return any(role.id == admin_role_id for role in member.roles)

    @app_commands.command(name="who-invited", description="Check who invited a specific user")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    @app_commands.describe(member="The member to check")
    async def who_invited(self, interaction: discord.Interaction, member: discord.Member):
        """Check who invited a specific member - Admin only"""
        await interaction.response.defer(ephemeral=True)

        # Check if user has admin role
        if not self._has_admin_role(interaction):
            embed = create_error_embed(
                "Permission Denied",
                "You need the Admin role to use this command."
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        try:
            async with get_session() as session:
                result = await session.execute(
                    select(InviteUse).where(
                        InviteUse.guild_id == str(interaction.guild.id),
                        InviteUse.joiner_id == str(member.id)
                    ).order_by(InviteUse.joined_at.desc()).limit(1)
                )
                invite_use = result.scalar_one_or_none()

            if not invite_use:
                embed = discord.Embed(
                    title="❌ No Invite Data",
                    description=f"No invite data found for {member.mention}. They may have joined before invite tracking was enabled.",
                    color=discord.Color.red()
                )
            else:
                embed = discord.Embed(
                    title="🔍 Invite Information",
                    description=f"Information about {member.mention}",
                    color=discord.Color.blue()
                )
                embed.add_field(name="Invited By", value=f"<@{invite_use.inviter_id}>", inline=True)
                embed.add_field(name="Invite Code", value=invite_use.invite_code, inline=True)
                embed.add_field(name="Joined At", value=f"<t:{int(invite_use.joined_at.timestamp())}:F>", inline=False)

                if invite_use.auto_role_assigned and invite_use.role_id:
                    role = interaction.guild.get_role(int(invite_use.role_id))
                    role_name = role.name if role else f"Role ID: {invite_use.role_id}"
                    embed.add_field(name="Auto-Role Assigned", value=f"✅ {role_name}", inline=False)

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error checking who invited: {e}")
            embed = create_error_embed("Failed to Check Invite", str(e))
            await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    """Setup function for loading cog"""
    await bot.add_cog(InviteTrackerCog(bot, bot.services))
