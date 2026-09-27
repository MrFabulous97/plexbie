# path: plugins/self_roles/cog.py
"""Self-assignable roles plugin - Smart or Dumb"""
import json
from database.kv_store import kv_get, kv_set
import re
from pathlib import Path

import discord
from discord.ext import commands

from plugins.forever_dumb.cog import dummy_nickname
from core.logging import get_logger
from core.services import BotServices

logger = get_logger(__name__)

# Storage file for role backups
ROLES_NAMESPACE = "role_backup"

# Import Dumb Family tracking functions from forever_dumb plugin
try:
    from plugins.forever_dumb.cog import add_to_dumb_family
except ImportError:
    # Fallback if import fails
    def add_to_dumb_family(user_id: int, username: str):
        """Fallback function if forever_dumb module is not available"""
        logger.warning("Could not import add_to_dumb_family from forever_dumb module")
        return False


async def load_role_backups():
    """Load role backups from database"""
    return await kv_get(ROLES_NAMESPACE, "backups", {})


async def save_role_backups(backups):
    """Save role backups to database"""
    await kv_set(ROLES_NAMESPACE, "backups", backups)


class SmartDumbRoleView(discord.ui.View):
    """Persistent view for Nerd/Dumb role self-assignment"""

    def __init__(self, nerd_role_id: int, dumb_role_id: int, forever_dumb_role_id: int, nerds_but_dumb_role_id: int, nerd_cat_emoji_id: int, dumb_cat_emoji_id: int, you_are_dumb_channel_id: int):
        super().__init__(timeout=None)  # Persistent view (no timeout)
        self.nerd_role_id = nerd_role_id
        self.dumb_role_id = dumb_role_id
        self.forever_dumb_role_id = forever_dumb_role_id
        self.nerds_but_dumb_role_id = nerds_but_dumb_role_id
        self.you_are_dumb_channel_id = you_are_dumb_channel_id

        # Create buttons with custom emojis
        smart_button = discord.ui.Button(
            label="Become Smart",
            style=discord.ButtonStyle.success,
            emoji=discord.PartialEmoji(name="NerdCat", id=nerd_cat_emoji_id),
            custom_id="become_smart_button"
        )
        smart_button.callback = self.smart_button_callback

        dumb_button = discord.ui.Button(
            label="Become Dumb",
            style=discord.ButtonStyle.danger,
            emoji=discord.PartialEmoji(name="dumbcat", id=dumb_cat_emoji_id),
            custom_id="become_dumb_button"
        )
        dumb_button.callback = self.dumb_button_callback

        self.add_item(smart_button)
        self.add_item(dumb_button)

    async def smart_button_callback(self, interaction: discord.Interaction):
        """Handle Become Smart button - Gives Nerd role or Nerds But Dumb role for Forever Dumb users"""
        try:
            member = interaction.user
            guild = interaction.guild

            if not guild:
                await interaction.response.send_message("Error: Could not find guild", ephemeral=True)
                return

            nerd_role = guild.get_role(self.nerd_role_id)
            dumb_role = guild.get_role(self.dumb_role_id)
            forever_dumb_role = guild.get_role(self.forever_dumb_role_id)
            nerds_but_dumb_role = guild.get_role(self.nerds_but_dumb_role_id)

            # Check if they have Forever Dumb role
            has_forever_dumb = forever_dumb_role and forever_dumb_role in member.roles

            if has_forever_dumb:
                # Forever Dumb users get "Nerds But Dumb" instead
                if not nerds_but_dumb_role:
                    await interaction.response.send_message(
                        "❌ Error: Nerds But Dumb role not found. Please contact an admin.",
                        ephemeral=True
                    )
                    return

                await member.add_roles(nerds_but_dumb_role, reason="Forever Dumb user became smart")
                await interaction.response.send_message(
                    f"🧠💀 You're now in the **{nerds_but_dumb_role.name}** club!\n\n"
                    f"You're smart... but still dumb. Forever.",
                    ephemeral=True
                )
                logger.info(f"{member} (Forever Dumb) became smart (added Nerds But Dumb role)")
            else:
                # Regular users get normal Nerd role
                if not nerd_role:
                    await interaction.response.send_message(
                        "❌ Error: Nerd role not found. Please contact an admin.",
                        ephemeral=True
                    )
                    return

                # Remove dumb role if they have it
                if dumb_role and dumb_role in member.roles:
                    await member.remove_roles(dumb_role, reason="Became smart")

                # Add nerd role
                await member.add_roles(nerd_role, reason="Self-assigned via Become Smart button")

                await interaction.response.send_message(
                    f"🧠 You're now smart! Welcome to the **{nerd_role.name}** club!",
                    ephemeral=True
                )
                logger.info(f"{member} became smart (added Nerd role)")

        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Error: I don't have permission to assign roles. Please contact an admin.",
                ephemeral=True
            )
            logger.error(f"No permission to assign Nerd role to {interaction.user}")
        except Exception as e:
            logger.error(f"Error handling Become Smart button: {e}")
            await interaction.response.send_message(
                f"❌ Error: {str(e)}",
                ephemeral=True
            )

    async def dumb_button_callback(self, interaction: discord.Interaction):
        """Handle Become Dumb button"""
        try:
            member = interaction.user
            guild = interaction.guild

            if not guild:
                await interaction.response.send_message("Error: Could not find guild", ephemeral=True)
                return

            nerd_role = guild.get_role(self.nerd_role_id)
            dumb_role = guild.get_role(self.dumb_role_id)
            forever_dumb_role = guild.get_role(self.forever_dumb_role_id)

            # Check if they have Forever Dumb role
            has_forever_dumb = forever_dumb_role and forever_dumb_role in member.roles

            if has_forever_dumb:
                # Check if they have the Nerds But Dumb role
                nerds_but_dumb_role = guild.get_role(self.nerds_but_dumb_role_id)
                has_nerds_but_dumb = nerds_but_dumb_role and nerds_but_dumb_role in member.roles

                if has_nerds_but_dumb:
                    # Case 1: Forever Dumb + Nerds But Dumb → Remove Nerds But Dumb role, NO counter increment
                    await member.remove_roles(nerds_but_dumb_role, reason="Forever Dumb user removing Nerds But Dumb")

                    await interaction.response.send_message(
                        f"🤪 You removed the **{nerds_but_dumb_role.name}** role!\n\n"
                        f"Back to being just Forever Dumb. Your counter remains unchanged.",
                        ephemeral=True
                    )
                    logger.info(f"{member} (Forever Dumb) removed Nerds But Dumb role")
                    return

                # Case 2: Forever Dumb + NO Nerds But Dumb → Increment counter
                # Forever Dumb users clicking Become Dumb again get mocked and nickname counter incremented
                current_nick = member.display_name

                # Parse current counter from nickname (e.g., ": Certified Dummy x3")
                match = re.search(r': Certified Dummy(?: x(\d+))?$', current_nick)

                if match:
                    current_count = int(match.group(1)) if match.group(1) else 1
                    new_count = current_count + 1

                    # Remove old suffix and add new one
                    base_name = current_nick[:match.start()]
                    new_nickname = dummy_nickname(base_name, new_count)

                    try:
                        if member.id != guild.owner_id:
                            await member.edit(nick=new_nickname, reason="Forever Dumb - clicked Become Dumb again")
                            logger.info(f"Incremented dumb counter for {member} to x{new_count}")
                        else:
                            logger.info(f"{member} is server owner, cannot update nickname counter")
                    except discord.Forbidden:
                        logger.warning(f"Could not update nickname for {member} (insufficient permissions)")

                # Send mocking message
                mocking_messages = [
                    "Oh look who's back! Can't get enough of being dumb, can you?",
                    "Wow. You ACTUALLY clicked it again. I'm impressed by your dedication to stupidity.",
                    "Are you... trying to speedrun being dumb? Because you're nailing it.",
                    "I would say I'm surprised, but... I'm really not.",
                    "At this point I'm just documenting your descent into maximum dumbness.",
                    "You know clicking this button doesn't make you UN-dumb, right? ...Right?",
                ]

                import random
                message = random.choice(mocking_messages)

                await interaction.response.send_message(
                    f"💀 **{message}**\n\n"
                    f"Your dumb counter has been incremented. Congratulations, I guess?",
                    ephemeral=True
                )
                return

            # If they have Nerd role, just remove it
            if nerd_role and nerd_role in member.roles:
                await member.remove_roles(nerd_role, reason="Became dumb")
                await interaction.response.send_message(
                    f"🤪 You removed the **{nerd_role.name}** role!",
                    ephemeral=True
                )
                logger.info(f"{member} became dumb (removed Nerd role)")
            else:
                # They don't have Nerd role, so give them Dumb role and strip all other roles
                if not dumb_role:
                    await interaction.response.send_message(
                        "❌ Error: Dumb role not found. Please contact an admin.",
                        ephemeral=True
                    )
                    return

                # Save current roles to JSON before removing them
                role_ids = [role.id for role in member.roles if role != guild.default_role and role.id != dumb_role.id]

                if role_ids:
                    backups = await load_role_backups()
                    backups[str(member.id)] = role_ids
                    await save_role_backups(backups)
                    logger.info(f"Saved {len(role_ids)} roles for {member}: {role_ids}")

                # Remove ALL roles except @everyone
                roles_to_remove = [role for role in member.roles if role != guild.default_role and role.id != dumb_role.id]

                if roles_to_remove:
                    await member.remove_roles(*roles_to_remove, reason="Became dumb - removed all roles for channel isolation")
                    logger.info(f"Removed {len(roles_to_remove)} roles from {member}: {[r.name for r in roles_to_remove]}")

                # Add the Dumb role
                await member.add_roles(dumb_role, reason="Self-assigned via Become Dumb button")

                # Track in Dumb Family and update display
                was_added = add_to_dumb_family(member.id, member.display_name)
                if was_added:
                    # Update the Dumb Family display message
                    forever_dumb_cog = interaction.client.get_cog("ForeverDumbCog")
                    if forever_dumb_cog:
                        await forever_dumb_cog.update_dumb_family_display()

                # Get the you-are-dumb channel to create a jump link
                channel_link = ""
                if self.you_are_dumb_channel_id:
                    you_are_dumb_channel = guild.get_channel(self.you_are_dumb_channel_id)
                    if you_are_dumb_channel:
                        channel_link = f"\n\n👉 [Click here to go to your new home]({you_are_dumb_channel.jump_url})"

                await interaction.response.send_message(
                    f"🤪 You're now in the **{dumb_role.name}** club!\n\n"
                    f"You've been isolated to the dumb zone. All other roles have been removed."
                    f"{channel_link}",
                    ephemeral=True
                )
                logger.info(f"{member} became dumb (added Dumb role, removed all other roles, saved backup)")

        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Error: I don't have permission to assign roles. Please contact an admin.",
                ephemeral=True
            )
            logger.error(f"No permission to assign/remove roles for {interaction.user}")
        except Exception as e:
            logger.error(f"Error handling Become Dumb button: {e}")
            await interaction.response.send_message(
                f"❌ Error: {str(e)}",
                ephemeral=True
            )


class SelfRolesCog(commands.Cog):
    """Self-assignable roles - Smart or Dumb"""

    def __init__(self, bot: commands.Bot, services: BotServices):
        self.bot = bot
        self.services = services
        self.nerd_role_id = services.config.nerd_role_id
        self.dumb_role_id = services.config.dumb_role_id
        self.forever_dumb_role_id = services.config.forever_dumb_role_id
        self.nerds_but_dumb_role_id = services.config.nerds_but_dumb_role_id
        self.welcome_channel_id = services.config.welcome_channel_id
        self.nerd_message_id = services.config.nerd_message_id
        self.nerd_cat_emoji_id = services.config.nerd_cat_emoji_id
        self.dumb_cat_emoji_id = services.config.dumb_cat_emoji_id
        self.you_are_dumb_channel_id = services.config.you_are_dumb_channel_id
        self.button_attached = False

    async def cog_load(self):
        """Register persistent view"""
        if not self.nerd_role_id or not self.dumb_role_id:
            logger.warning("NERD_ROLE_ID or DUMB_ROLE_ID not configured in .env")
            return

        if not self.welcome_channel_id:
            logger.warning("WELCOME_CHANNEL_ID not configured")
            return

        if not self.nerd_cat_emoji_id or not self.dumb_cat_emoji_id:
            logger.warning("NERD_CAT_EMOJI_ID or DUMB_CAT_EMOJI_ID not configured in .env")
            return

        if not self.forever_dumb_role_id or not self.nerds_but_dumb_role_id:
            logger.warning("FOREVER_DUMB_ROLE_ID or NERDS_BUT_DUMB_ROLE_ID not configured in .env")
            return

        if not self.you_are_dumb_channel_id:
            logger.warning("YOU_ARE_DUMB_CHANNEL_ID not configured in .env")
            return

        # Register the persistent view
        view = SmartDumbRoleView(
            self.nerd_role_id,
            self.dumb_role_id,
            self.forever_dumb_role_id,
            self.nerds_but_dumb_role_id,
            self.nerd_cat_emoji_id,
            self.dumb_cat_emoji_id,
            self.you_are_dumb_channel_id
        )
        self.bot.add_view(view)
        logger.info("✅ Registered persistent Smart/Dumb role view")

    @commands.Cog.listener()
    async def on_ready(self):
        """Create smart/dumb role message with buttons"""
        # Only run once
        if self.button_attached:
            return

        if not self.nerd_role_id or not self.dumb_role_id or not self.welcome_channel_id:
            return

        if not self.nerd_cat_emoji_id or not self.dumb_cat_emoji_id:
            logger.warning("NERD_CAT_EMOJI_ID or DUMB_CAT_EMOJI_ID not configured")
            return

        if not self.forever_dumb_role_id or not self.nerds_but_dumb_role_id:
            logger.warning("FOREVER_DUMB_ROLE_ID or NERDS_BUT_DUMB_ROLE_ID not configured")
            return

        if not self.you_are_dumb_channel_id:
            logger.warning("YOU_ARE_DUMB_CHANNEL_ID not configured")
            return

        try:
            # Get the guild
            guild = self.bot.get_guild(self.services.config.guild_id)
            if not guild:
                logger.error("Could not find guild for Smart/Dumb role message")
                return

            # Get the welcome channel
            channel = guild.get_channel(self.welcome_channel_id)
            if not channel:
                logger.error(f"Could not find welcome channel {self.welcome_channel_id}")
                return

            # Create the view with both buttons
            view = SmartDumbRoleView(
                self.nerd_role_id,
                self.dumb_role_id,
                self.forever_dumb_role_id,
                self.nerds_but_dumb_role_id,
                self.nerd_cat_emoji_id,
                self.dumb_cat_emoji_id,
                self.you_are_dumb_channel_id
            )

            # Create embed with custom emoji
            embed = discord.Embed(
                title=f"Become a nerd <:NerdCat:{self.nerd_cat_emoji_id}>",
                description=(
                    "**Become Smart** - Join the Nerds and get access to the Stats Channel\n\n"
                    "**Become Dumb** - Press this if you want to remove the Nerds role. "
                    "Don't press it if you don't have the role already... "
                ),
                color=discord.Color.blue()
            )

            # Check if we should edit existing message or create new one
            try:
                if self.nerd_message_id:
                    # Try to fetch and edit existing message
                    try:
                        existing_message = await channel.fetch_message(self.nerd_message_id)
                        await existing_message.edit(content=None, embed=embed, view=view)
                        self.button_attached = True
                        logger.info(f"✅ Updated existing Smart/Dumb role message {self.nerd_message_id} in {channel.name}")
                    except discord.NotFound:
                        # Message was deleted, create a new one
                        logger.warning(f"Message {self.nerd_message_id} not found, creating new one")
                        new_message = await channel.send(content=None, embed=embed, view=view)
                        self.button_attached = True
                        logger.info(f"✅ Created new Smart/Dumb role message {new_message.id} in {channel.name}")
                        logger.info(f"💡 Update .env with: NERD_MESSAGE_ID={new_message.id}")
                else:
                    # No message ID configured, create new message
                    new_message = await channel.send(content=None, embed=embed, view=view)
                    self.button_attached = True
                    logger.info(f"✅ Created Smart/Dumb role message {new_message.id} in {channel.name}")
                    logger.info(f"💡 Update .env with: NERD_MESSAGE_ID={new_message.id}")

            except discord.Forbidden:
                logger.error(f"❌ No permission to send/edit message in {channel.name}")
            except Exception as e:
                logger.error(f"❌ Error creating/updating Smart/Dumb role message: {e}")

        except Exception as e:
            logger.error(f"❌ Error in on_ready: {e}")


async def setup(bot: commands.Bot):
    """Setup function for loading cog"""
    await bot.add_cog(SelfRolesCog(bot, bot.services))
