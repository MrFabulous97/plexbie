# path: plugins/user_invites/cog.py
"""User invitation system matching original Python bot workflow"""
import json
from database.kv_store import kv_get, kv_set, kv_delete, kv_get_all
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from core.blocking import run_blocking
from core.logging import get_logger
from core.permissions import AdminOnlyView
from core.services import BotServices
from core.admin_mirror import send_user_dm

logger = get_logger(__name__)

# Storage file
# INVITES_FILE removed - now using database kv_store
INVITES_NAMESPACE = "plex_invites"
# Admin message id -> the request it represents, so a persistent view can recover
# its state after a restart. Deliberately a SEPARATE namespace: user_mgmt's
# auto_link_users iterates plex_invites and reads every key as a Discord user id,
# so message ids must never land there.
INVITE_MESSAGES_NAMESPACE = "plex_invite_messages"


class PlexInviteApprovalView(AdminOnlyView):
    """Admin approval buttons for Plex invites.

    Admin-gated: approving grants real Plex library access via inviteFriend and
    assigns the Plex member role, so it must never dispatch to a non-admin.
    """
    def __init__(
        self,
        user_id: int = None,
        email: str = None,
        services: BotServices = None,
    ):
        # Every argument is optional so setup() can register this view with no
        # arguments (bot.add_view(PlexInviteApprovalView())). State is recovered
        # per-interaction by _ensure_loaded().
        super().__init__(timeout=None)
        self.user_id = user_id
        self.email = email
        self.services = services

    async def _ensure_loaded(self, interaction: discord.Interaction) -> bool:
        """Populate state from storage when this view came from a restart.

        A persistent view registered at startup has no per-request state, so it
        is looked up by the admin message id. Previously this view was never
        registered at all and its buttons carried no custom_id, so after any
        restart - including every deploy - clicking Approve returned "This
        interaction failed" and the request became silently unactionable.
        """
        if self.user_id and self.email and self.services:
            return True

        record = await kv_get(INVITE_MESSAGES_NAMESPACE, str(interaction.message.id))
        if not record:
            logger.error(
                f"No stored invite request for message {interaction.message.id}"
            )
            await interaction.followup.send(
                "❌ Could not find this request's data. Ask the user to run "
                "`/join-plex` again.",
                ephemeral=True,
            )
            return False

        self.user_id = record.get("user_id")
        self.email = record.get("email")
        self.services = interaction.client.services
        return bool(self.user_id and self.email)

    @discord.ui.button(
        label="Approve & Send Invite",
        style=discord.ButtonStyle.success,
        custom_id="plex_invite_approve",
    )
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        if not await self._ensure_loaded(interaction):
            return

        # Send Plex invite
        success, plex_username = await self._send_plex_invite()

        if success:
            # Assign Discord role
            member = interaction.guild.get_member(self.user_id)
            if member and self.services.config.plex_member_role_id:
                role = interaction.guild.get_role(int(self.services.config.plex_member_role_id))
                if role:
                    await member.add_roles(role, reason="Plex access approved")
                    logger.info(f"Added Plex member role to {member.name}")

            # Add to user tracking system. Without a resolvable member we cannot
            # record the Discord side of the mapping, and a silent miss here means
            # the user keeps Plex access forever without inactivity tracking.
            if plex_username and member:
                await self._add_to_user_tracking(member, plex_username)
            elif plex_username:
                logger.error(
                    f"Invited {self.email} to Plex but Discord member {self.user_id} "
                    f"is not in the guild - NOT tracked for inactivity, link manually"
                )

            # Update message
            await interaction.message.edit(
                content=f"✅ **Approved by {interaction.user.name}** - Plex invitation sent to {self.email}!",
                view=None
            )

            # Notify user
            user = interaction.client.get_user(self.user_id)
            if user:
                embed = discord.Embed(
                    title="✅ Request Approved!",
                    description=f"Your request has been approved! A Plex invitation has been sent to **{self.email}**.\n\n"
                               f"Check your email and accept the invitation to access the Plex server.",
                    color=discord.Color.green()
                )
                embed.add_field(
                    name="Next Steps",
                    value="1. Check your email for the Plex invitation\n"
                          "2. Click the link and create/login to your Plex account\n"
                          "3. Start watching!",
                    inline=False
                )
                await send_user_dm(interaction.client, self.services, user, context=f"join-plex approved ({self.email})", embed=embed)

            await interaction.followup.send("Invite sent successfully!", ephemeral=True)
        else:
            await interaction.followup.send("Failed to send Plex invite. Please check logs.", ephemeral=True)
    
    @discord.ui.button(
        label="Deny",
        style=discord.ButtonStyle.danger,
        custom_id="plex_invite_deny",
    )
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        if not await self._ensure_loaded(interaction):
            return

        await interaction.message.edit(
            content=f"❌ **Denied by {interaction.user.name}**",
            view=None
        )

        # Notify user
        user = interaction.client.get_user(self.user_id)
        if user:
            embed = discord.Embed(
                title="❌ Request Denied",
                description="Sorry, your request for Plex access was not approved at this time.",
                color=discord.Color.red()
            )
            await send_user_dm(interaction.client, self.services, user, context=f"join-plex denied ({self.email})", embed=embed)
    
    async def _send_plex_invite(self) -> tuple[bool, str]:
        """Send Plex invitation - returns (success, plex_username)"""
        if not self.services.plex_server:
            logger.error("Plex server not configured")
            return False, ""

        try:
            from plexapi.myplex import MyPlexAccount

            if not self.services.config.plex_username or not self.services.config.plex_password:
                logger.error("Plex credentials not configured")
                return False, ""

            # MyPlexAccount() authenticates against plex.tv and inviteFriend()
            # is another round-trip to it; both are blocking, and this runs from a
            # button callback where a stalled loop delays every other interaction.
            account = await run_blocking(
                MyPlexAccount,
                self.services.config.plex_username,
                self.services.config.plex_password,
            )

            sections = await run_blocking(self.services.plex_server.library.sections)
            await run_blocking(
                account.inviteFriend,
                user=self.email,
                server=self.services.plex_server,
                sections=sections,
                allowSync=False,
            )

            # Extract username from email (username is the part before @)
            plex_username = self.email.split('@')[0]

            logger.info(f"Successfully sent Plex invite to {self.email}")
            return True, plex_username

        except Exception as e:
            message = str(e)
            if "already sharing this server with" in message:
                try:
                    # account.user() is another plex.tv lookup. account may be
                    # unbound if MyPlexAccount() itself raised, which the outer
                    # except also covers.
                    existing_user = await run_blocking(account.user, self.email)
                except Exception:
                    existing_user = None

                plex_username = (
                    getattr(existing_user, "username", None)
                    or getattr(existing_user, "title", None)
                    or self.email.split('@')[0]
                )
                logger.info(
                    f"Plex access already exists for {self.email}; restoring Discord access for {plex_username}"
                )
                return True, plex_username

            logger.error(f"Failed to send Plex invite: {e}")
            return False, ""

    async def _add_to_user_tracking(self, member: discord.Member, plex_username: str):
        """Add user to the inactivity tracking system"""
        try:
            from sqlalchemy import select
            from database.session import get_session
            from plugins.user_mgmt.models import PlexUser

            async with get_session() as session:
                # Check if already tracked
                result = await session.execute(
                    select(PlexUser).where(PlexUser.plex_username == plex_username)
                )
                existing_user = result.scalar_one_or_none()

                if existing_user:
                    # Update existing
                    existing_user.discord_id = member.id
                    existing_user.discord_username = str(member)
                    logger.info(f"Updated tracking for existing user {plex_username}")
                else:
                    # Create new tracking entry
                    new_user = PlexUser(
                        discord_id=member.id,
                        discord_username=str(member),
                        plex_username=plex_username,
                        plex_email=self.email
                    )
                    session.add(new_user)
                    logger.info(f"Started tracking new user {plex_username}")

                await session.commit()

        except Exception as e:
            logger.error(f"Error adding user to tracking: {e}", exc_info=True)


class UserInvitesCog(commands.Cog):
    """Plex user invitation system with email collection via DM"""
    
    def __init__(self, bot: commands.Bot, services: BotServices):
        self.bot = bot
        self.services = services
        # Discord ids with a /join-plex flow currently awaiting a DM reply.
        self._in_progress: set = set()

    async def cog_load(self):
        """Register the persistent approval view.

        Must be here, not in setup(): core.plugin_manager instantiates the cog
        class and calls bot.add_cog directly, so a module-level setup() is never
        invoked by this bot. add_cog does trigger cog_load, and it also fires if
        the plugin is ever loaded as a normal discord.py extension.
        """
        self.bot.add_view(PlexInviteApprovalView())
        logger.info("✅ Registered persistent Plex invite approval view")
        
    
    @app_commands.command(name="join-plex", description="Request access to the Plex server")
    async def join_plex(self, interaction: discord.Interaction):
        """Request Plex access with email collection via DM"""
        logger.info(f"User {interaction.user.name} ({interaction.user.id}) invoked /join-plex")

        # Check if user already has role
        if self.services.config.plex_member_role_id:
            member = interaction.guild.get_member(interaction.user.id)
            role = interaction.guild.get_role(int(self.services.config.plex_member_role_id))
            if member and role and role in member.roles:
                await interaction.response.send_message(
                    "You already have access to the Plex server!",
                    ephemeral=True
                )
                return
        
        # One flow per user at a time. The wait_for check below matches any DM from
        # this user, so two concurrent flows would both be resolved by a single
        # reply - producing two admin approval messages, two saved requests, and
        # potentially two Plex invites for one request.
        if interaction.user.id in self._in_progress:
            await interaction.response.send_message(
                "You already have a request in progress - check your DMs and reply "
                "there with your email address.",
                ephemeral=True,
            )
            return

        # Defer rather than replying now: the DM is attempted first, so the reply
        # can tell the truth about whether it actually arrived. Previously the user
        # was told "Check your DMs!" before any DM was sent, and a blocked DM was
        # only logged - leaving them waiting for a message that never came.
        await interaction.response.defer(ephemeral=True)
        self._in_progress.add(interaction.user.id)

        try:
            # Send DM requesting email
            embed = discord.Embed(
                title="Plex Server Access Request",
                description="To request access to the Plex server, please reply with your email address.\n\n"
                           "**This email will be used to send you a Plex invitation.**",
                color=discord.Color.blue()
            )
            embed.add_field(
                name="Important",
                value="Use the same email associated with your Plex account "
                      "(or the one you want to use for Plex).",
                inline=False
            )
            
            await send_user_dm(self.bot, self.services, interaction.user, context="join-plex email collection prompt", embed=embed)

            # The DM is confirmed sent, so this is now truthful.
            await interaction.followup.send("Check your DMs!", ephemeral=True)
            
            # Wait for email response
            def check(m):
                return m.author.id == interaction.user.id and isinstance(m.channel, discord.DMChannel)
            
            try:
                msg = await self.bot.wait_for('message', timeout=300, check=check)
            except asyncio.TimeoutError:
                await send_user_dm(self.bot, self.services, interaction.user, context="join-plex request timed out", content="Request timed out. Please use `/join-plex` again to start over.")
                return
            
            email = msg.content.strip()

            # Enhanced email validation
            from utils.validators import validate_email
            if not validate_email(email):
                await send_user_dm(self.bot, self.services, interaction.user, context="join-plex invalid email", content="That doesn't look like a valid email address. Please use `/join-plex` again and provide a valid email.")
                return
            
            await send_user_dm(self.bot, self.services, interaction.user, context="join-plex request submitted", content="Email received! Your request has been sent to the admin for approval.")
            
            # Save the request BEFORE notifying admins. _send_to_admin records the
            # message id against this record, and previously ran first - so its
            # kv_get found nothing, the message id was silently dropped, and
            # _save_request then overwrote the record without it.
            await self._save_request(interaction.user.id, email)

            # Send to admin channel
            await self._send_to_admin(interaction, email)
            
        except discord.Forbidden:
            logger.warning(f"Could not DM user {interaction.user.id} for /join-plex")
            await interaction.followup.send(
                "❌ I could not send you a DM. Enable **Allow direct messages from "
                "server members** in your Privacy Settings for this server, then "
                "run `/join-plex` again.",
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"Join-plex error for user {interaction.user.id}: {e}", exc_info=True)
            try:
                await interaction.followup.send(
                    "❌ Something went wrong starting your request. Please try again.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
        finally:
            # Always release the guard, or the user could never retry.
            self._in_progress.discard(interaction.user.id)
    
    async def _send_to_admin(self, interaction: discord.Interaction, email: str):
        """Send invite request to admin channel"""
        admin_channel_id = int(self.services.config.admin_channel_id or 0)
        if not admin_channel_id:
            logger.error("Admin channel not configured")
            return
        
        admin_channel = self.bot.get_channel(admin_channel_id)
        if not admin_channel:
            logger.error(f"Admin channel {admin_channel_id} not found")
            return
        
        embed = discord.Embed(
            title="New Plex Access Request",
            description=f"**{interaction.user.name}** has requested access to the Plex server.",
            color=discord.Color.blue()
        )
        embed.add_field(name="Discord User", value=interaction.user.mention, inline=True)
        embed.add_field(name="Email Address", value=email, inline=True)
        embed.timestamp = datetime.now(timezone.utc)
        
        view = PlexInviteApprovalView(interaction.user.id, email, self.services)
        
        message = await admin_channel.send(embed=embed, view=view)

        # Record the message -> request mapping so the persistent view can recover
        # its state after a restart. Written unconditionally rather than as an
        # update to an existing record, which is what previously failed silently.
        await kv_set(INVITE_MESSAGES_NAMESPACE, str(message.id), {
            "user_id": interaction.user.id,
            "email": email,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })

        # Update saved request with message ID
        invite_data = await kv_get(INVITES_NAMESPACE, str(interaction.user.id))
        if invite_data:
            invite_data["message_id"] = message.id
            await kv_set(INVITES_NAMESPACE, str(interaction.user.id), invite_data)
    
    async def _save_request(self, user_id: int, email: str):
        """Save invite request"""
        await kv_set(INVITES_NAMESPACE, str(user_id), {
            "email": email,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "pending"
        })


async def setup(bot: commands.Bot):
    """Setup function for loading cog"""
    # The view is registered in UserInvitesCog.cog_load, which add_cog triggers.
    await bot.add_cog(UserInvitesCog(bot, bot.services))
