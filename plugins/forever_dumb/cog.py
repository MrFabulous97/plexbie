# path: plugins/forever_dumb/cog.py
"""Forever Dumb role with nickname enforcement"""
import json
from database.kv_store import kv_get, kv_set
from pathlib import Path

import discord
from discord.ext import commands

from utils.embeds import truncate_field
from core.logging import get_logger
from core.services import BotServices

logger = get_logger(__name__)

# Color code for "Certified Dummy" users
CERTIFIED_DUMMY_COLOR = 0x3c302c  # Brown color

# Discord rejects nicknames longer than this with a 400.
MAX_NICKNAME_LENGTH = 32
DUMMY_SUFFIX = ": Certified Dummy"


def dummy_nickname(base_name: str, times: int = 1) -> str:
    """Build the Certified Dummy nickname, trimmed to Discord's 32-char limit.

    The suffix is 17 characters, so any display name over 15 produced a nickname
    Discord rejects with HTTPException (400). That was not caught - only
    discord.Forbidden was, and Forbidden is a SUBCLASS of HTTPException, so the
    400 escaped every handler. The role had already been assigned by then, so the
    user ended up permanently Forever Dumb while seeing only "This interaction
    failed". Names of 16+ characters are the common case.
    """
    suffix = DUMMY_SUFFIX if times <= 1 else f"{DUMMY_SUFFIX} x{times}"
    room = MAX_NICKNAME_LENGTH - len(suffix)
    if room <= 0:
        # Pathological (a huge multiplier); keep the suffix, drop the name.
        return suffix[:MAX_NICKNAME_LENGTH]
    return f"{base_name[:room].rstrip()}{suffix}"


# Storage file for role backups (shared with self_roles)
ROLES_NAMESPACE = "role_backup"
DUMB_FAMILY_NAMESPACE = "dumb_family"


async def load_role_backups():
    """Load role backups from JSON file"""
    if not False:
        return {}
    try:
        return await kv_get(ROLES_NAMESPACE, "backups", {})
    except (json.JSONDecodeError, OSError) as e:  # Corrupt or unreadable file
        logger.warning(f"Could not load role backups: {e}"); return {}


async def save_role_backups(backups):
    """Save role backups to JSON file"""
    
    await kv_set(ROLES_NAMESPACE, "backups", backups)


async def load_dumb_family():
    """Load Dumb Family history from JSON file"""
    if not False:
        return []
    try:
        return await kv_get(DUMB_FAMILY_NAMESPACE, "members", [])
    except (json.JSONDecodeError, OSError) as e:  # Corrupt or unreadable file
        logger.warning(f"Could not load dumb family: {e}"); return []


async def save_dumb_family(family_list):
    """Save Dumb Family history to JSON file"""
    
    await kv_set(DUMB_FAMILY_NAMESPACE, "members", family_list)


async def add_to_dumb_family(user_id: int, username: str):
    """Add a user to the Dumb Family history if not already present"""
    family = await load_dumb_family()

    # Check if user already exists
    for entry in family:
        if entry.get("user_id") == user_id:
            return False  # User already in family

    # Add new entry
    from datetime import datetime, timezone
    family.append({
        "user_id": user_id,
        "username": username,
        "joined_at": datetime.now(timezone.utc).isoformat()
    })
    await save_dumb_family(family)
    return True  # User was added


class ForeverDumbView(discord.ui.View):
    """Persistent view for Forever Dumb button"""

    def __init__(self, dumb_role_id: int, forever_dumb_role_id: int, nerd_role_id: int):
        super().__init__(timeout=None)  # Persistent view
        self.dumb_role_id = dumb_role_id
        self.forever_dumb_role_id = forever_dumb_role_id
        self.nerd_role_id = nerd_role_id

        # Create "Become Forever Dumb" button
        forever_dumb_button = discord.ui.Button(
            label="Become Forever Dumb",
            style=discord.ButtonStyle.danger,
            emoji="💀",
            custom_id="become_forever_dumb_button"
        )
        forever_dumb_button.callback = self.forever_dumb_callback
        self.add_item(forever_dumb_button)

    async def forever_dumb_callback(self, interaction: discord.Interaction):
        """Handle Become Forever Dumb button"""
        try:
            member = interaction.user
            guild = interaction.guild

            if not guild:
                await interaction.response.send_message("Error: Could not find guild", ephemeral=True)
                return

            dumb_role = guild.get_role(self.dumb_role_id)
            forever_dumb_role = guild.get_role(self.forever_dumb_role_id)
            nerd_role = guild.get_role(self.nerd_role_id)

            if not forever_dumb_role:
                await interaction.response.send_message(
                    "❌ Error: Forever Dumb role not found. Please contact an admin.",
                    ephemeral=True
                )
                return

            # Load role backups from JSON
            backups = await load_role_backups()
            user_backup = backups.get(str(member.id), [])

            # Remove the regular dumb role if they have it
            if dumb_role and dumb_role in member.roles:
                await member.remove_roles(dumb_role, reason="Became Forever Dumb")

            # Restore saved roles from JSON (excluding Nerd role)
            if user_backup:
                roles_to_restore = []
                for role_id in user_backup:
                    # Skip the Nerd role if it was in the backup
                    if role_id == self.nerd_role_id:
                        continue

                    role = guild.get_role(role_id)
                    if role:
                        roles_to_restore.append(role)

                if roles_to_restore:
                    await member.add_roles(*roles_to_restore, reason="Forever Dumb - Restoring saved roles")
                    logger.info(f"Restored {len(roles_to_restore)} roles for {member}: {[r.name for r in roles_to_restore]}")

                # Remove from backup file after restoration
                del backups[str(member.id)]
                await save_role_backups(backups)
                logger.info(f"Removed {member} from role backup file")

            # Add the Forever Dumb role
            await member.add_roles(forever_dumb_role, reason="Self-assigned via Forever Dumb button")

            # Track in Dumb Family and update display
            was_added = await add_to_dumb_family(member.id, member.display_name)
            if was_added:
                # Update the Dumb Family display message
                forever_dumb_cog = interaction.client.get_cog("ForeverDumbCog")
                if forever_dumb_cog:
                    await forever_dumb_cog.update_dumb_family_display()

            # Get user's current display name (without any previous Certified Dummy suffix)
            base_name = member.display_name
            if ": Certified Dummy" in base_name:
                base_name = base_name.replace(": Certified Dummy", "").strip()

            # Set nickname with Certified Dummy suffix
            new_nickname = dummy_nickname(base_name)
            nickname_set = False
            nickname_message = ""

            # Check if user is the server owner
            if member.id == guild.owner_id:
                nickname_message = "\n\n⚠️ **Note:** As the server owner, I cannot change your nickname. Please manually add `: Certified Dummy` to your name."
                logger.info(f"{member} became Forever Dumb but is server owner - cannot set nickname")
            else:
                try:
                    await member.edit(nick=new_nickname, reason="Forever Dumb - Certified Dummy")
                    nickname_set = True
                    logger.info(f"{member} became Forever Dumb - nickname set to '{new_nickname}'")
                except discord.Forbidden:
                    nickname_message = "\n\n⚠️ **Note:** I don't have permission to change your nickname. Please manually add `: Certified Dummy` to your name."
                    logger.warning(f"Could not set nickname for {member} (insufficient permissions)")
                except discord.HTTPException as e:
                    # Forbidden is a subclass of HTTPException, so this must come
                    # second. Catching it at all matters because the role is
                    # already assigned by this point - letting it escape left the
                    # user Forever Dumb with only "This interaction failed".
                    nickname_message = "\n\n⚠️ **Note:** I could not change your nickname. Please manually add `: Certified Dummy` to your name."
                    logger.warning(f"Could not set nickname for {member}: {e}")

            # Build response message
            response_parts = [
                f"💀 **You are now Forever Dumb!**\n",
                f"You have been given the **{forever_dumb_role.name}** role.",
            ]

            if user_backup:
                response_parts.append(f"\n✅ Your previous roles have been restored (except Nerd role).")

            response_parts.append(f"\n{'Your name has been updated to include `: Certified Dummy`' if nickname_set else 'Your color is now brown.'}")
            response_parts.append(f"\n\nThis is **permanent and cannot be undone**. Welcome to eternal dumbness! 🧠❌")
            response_parts.append(nickname_message)

            await interaction.response.send_message(
                "".join(response_parts),
                ephemeral=True
            )

        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Error: I don't have permission to assign roles or change nicknames. Please contact an admin.",
                ephemeral=True
            )
            logger.error(f"No permission to assign Forever Dumb role to {interaction.user}")
        except Exception as e:
            logger.error(f"Error handling Forever Dumb button: {e}")
            await interaction.response.send_message(
                f"❌ Error: {str(e)}",
                ephemeral=True
            )


class ForeverDumbCog(commands.Cog):
    """Forever Dumb role with nickname enforcement"""

    def __init__(self, bot: commands.Bot, services: BotServices):
        self.bot = bot
        self.services = services
        self.dumb_role_id = services.config.dumb_role_id
        self.forever_dumb_role_id = services.config.forever_dumb_role_id
        self.nerd_role_id = services.config.nerd_role_id
        self.you_are_dumb_channel_id = services.config.you_are_dumb_channel_id
        self.forever_dumb_message_id = services.config.forever_dumb_message_id
        self.dumb_family_message_id = services.config.dumb_family_message_id
        self.button_attached = False
        self.family_message_attached = False

    async def cog_load(self):
        """Register persistent view"""
        if not self.dumb_role_id or not self.forever_dumb_role_id:
            logger.warning("DUMB_ROLE_ID or FOREVER_DUMB_ROLE_ID not configured in .env")
            return

        if not self.nerd_role_id:
            logger.warning("NERD_ROLE_ID not configured in .env")
            return

        if not self.you_are_dumb_channel_id:
            logger.warning("YOU_ARE_DUMB_CHANNEL_ID not configured")
            return

        # Register the persistent view
        view = ForeverDumbView(self.dumb_role_id, self.forever_dumb_role_id, self.nerd_role_id)
        self.bot.add_view(view)
        logger.info("✅ Registered persistent Forever Dumb view")

    @commands.Cog.listener()
    async def on_ready(self):
        """Create Forever Dumb message with button"""
        # Only run once
        if self.button_attached:
            return

        if not self.dumb_role_id or not self.forever_dumb_role_id or not self.you_are_dumb_channel_id:
            return

        if not self.nerd_role_id:
            return

        try:
            # Get the guild
            guild = self.bot.get_guild(self.services.config.guild_id)
            if not guild:
                logger.error("Could not find guild for Forever Dumb message")
                return

            # Get the you-are-dumb channel
            channel = guild.get_channel(self.you_are_dumb_channel_id)
            if not channel:
                logger.error(f"Could not find you-are-dumb channel {self.you_are_dumb_channel_id}")
                return

            # Create the view with the button
            view = ForeverDumbView(self.dumb_role_id, self.forever_dumb_role_id, self.nerd_role_id)

            # Create embed
            embed = discord.Embed(
                title="Well well well if it isn't Mr/Mrs/Mx DUMB DUMB stupid ASS.",
                description=(
                    "I see you've pushed the button made specifically to weed people like yourself out from the rest.\n\n"
                    "And honestly, thank you. Thank you for being such a humble dummy. That button only works because people like yourself (morons) have the courtesy to be so honest.\n\n"
                    "But now you're HERE. Welcome to little idiot purgatory! It's a nice little place we've set aside for the likes of you. We do group reading comprehension sessions on Tuesdays at 5:00 PM, but our goal isn't to help you improve its just to point at you and laugh.\n\n"
                    "Anyways, you now find yourself at a crossroads. Actually, hold on a second, metaphors might be too complicated for you.\n\n\n\n"
                    "You have TWO options moving forward.\n\n"
                    "**Option A) Stay In Dumb Dumb Stupid Ass Purgatory Forever** - Make yourself this purgatory's latest new resident.\n\n"
                    "**Option B) Bare The Mark Of Shame** - You will forever bare the mark of stupidity.\n\n"
                    "*sigh* <:dumbcat:1437246287320842300>"
                ),
                color=CERTIFIED_DUMMY_COLOR
            )

            # Check if we should edit existing message or create new one
            try:
                if self.forever_dumb_message_id:
                    # Try to fetch and edit existing message
                    try:
                        existing_message = await channel.fetch_message(self.forever_dumb_message_id)
                        await existing_message.edit(content=None, embed=embed, view=view)
                        self.button_attached = True
                        logger.info(f"✅ Updated existing Forever Dumb message {self.forever_dumb_message_id} in {channel.name}")
                    except discord.NotFound:
                        # Message was deleted, create a new one
                        logger.warning(f"Message {self.forever_dumb_message_id} not found, creating new one")
                        new_message = await channel.send(content=None, embed=embed, view=view)
                        self.button_attached = True
                        logger.info(f"✅ Created new Forever Dumb message {new_message.id} in {channel.name}")
                        logger.info(f"💡 Update .env with: FOREVER_DUMB_MESSAGE_ID={new_message.id}")
                else:
                    # No message ID configured, create new message
                    new_message = await channel.send(content=None, embed=embed, view=view)
                    self.button_attached = True
                    logger.info(f"✅ Created Forever Dumb message {new_message.id} in {channel.name}")
                    logger.info(f"💡 Update .env with: FOREVER_DUMB_MESSAGE_ID={new_message.id}")

            except discord.Forbidden:
                logger.error(f"❌ No permission to send/edit message in {channel.name}")
            except Exception as e:
                logger.error(f"❌ Error creating/updating Forever Dumb message: {e}")

            # Create/update Dumb Family message
            if not self.family_message_attached:
                await self.update_dumb_family_display()
                self.family_message_attached = True

        except Exception as e:
            logger.error(f"❌ Error in on_ready: {e}")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Enforce nickname for Forever Dumb members"""
        import re

        if not self.forever_dumb_role_id:
            return

        forever_dumb_role = after.guild.get_role(self.forever_dumb_role_id)
        if not forever_dumb_role or forever_dumb_role not in after.roles:
            return

        # Check if nickname changed
        if before.display_name != after.display_name:
            # Check if they have a valid Certified Dummy format
            current_nick = after.display_name

            # Match pattern: "Username: Certified Dummy x2" or "Username: Certified Dummy"
            match = re.search(r': Certified Dummy(?: x(\d+))?$', current_nick)

            if not match:
                # Nickname doesn't have the required suffix - need to enforce it

                # First, check if the OLD nickname had a counter we need to preserve
                old_match = re.search(r': Certified Dummy(?: x(\d+))?$', before.display_name)
                preserved_count = 1
                if old_match and old_match.group(1):
                    # Preserve (do not increment) the counter from the old nickname
                    try:
                        preserved_count = int(old_match.group(1))
                    except ValueError:
                        preserved_count = 1

                # Extract base name (remove any existing Certified Dummy suffix from current nickname)
                base_name = current_nick
                if ": Certified Dummy" in base_name:
                    base_name = re.sub(r': Certified Dummy(?: x\d+)?$', '', base_name).strip()

                # Build new nickname with the counter if it existed
                new_nickname = dummy_nickname(base_name, preserved_count)

                try:
                    await after.edit(nick=new_nickname, reason="Forever Dumb - Enforcing Certified Dummy suffix")
                    logger.info(f"Enforced nickname for Forever Dumb member {after}: '{new_nickname}'")
                except discord.Forbidden:
                    logger.warning(f"Could not enforce nickname for {after} (insufficient permissions)")
                except Exception as e:
                    logger.error(f"Error enforcing nickname for {after}: {e}")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Restore nickname for Forever Dumb members who rejoin"""
        if not self.forever_dumb_role_id:
            return

        forever_dumb_role = member.guild.get_role(self.forever_dumb_role_id)
        if not forever_dumb_role or forever_dumb_role not in member.roles:
            return

        # Set nickname when they rejoin
        base_name = member.name
        new_nickname = dummy_nickname(base_name)
        try:
            await member.edit(nick=new_nickname, reason="Forever Dumb - Restored Certified Dummy suffix")
            logger.info(f"Restored nickname for Forever Dumb member {member}: '{new_nickname}'")
        except discord.Forbidden:
            logger.warning(f"Could not restore nickname for {member} (insufficient permissions)")
        except Exception as e:
            logger.error(f"Error restoring nickname for {member}: {e}")

    async def update_dumb_family_display(self):
        """Update the Dumb Family message with current family members"""
        if not self.you_are_dumb_channel_id:
            return

        try:
            # Get the guild
            guild = self.bot.get_guild(self.services.config.guild_id)
            if not guild:
                logger.error("Could not find guild for Dumb Family message")
                return

            # Get the you-are-dumb channel
            channel = guild.get_channel(self.you_are_dumb_channel_id)
            if not channel:
                logger.error(f"Could not find you-are-dumb channel {self.you_are_dumb_channel_id}")
                return

            # Load the Dumb Family history
            family = await load_dumb_family()

            # Create embed
            embed = discord.Embed(
                title="👨‍👩‍👧‍👦 The Dumb Family",
                description="Welcome to the family! These brave souls have joined the ranks of dumbness.",
                color=CERTIFIED_DUMMY_COLOR
            )

            if family:
                # Build the family list
                family_members = []
                for entry in family:
                    username = entry.get("username", "Unknown")
                    family_members.append(f"• {username}")

                # Add field with all members
                embed.add_field(
                    name=f"Family Members ({len(family)})",
                    value=truncate_field("\n".join(family_members) if family_members else "No members yet"),
                    inline=False
                )

                embed.set_footer(text="This is your dumb family ❤️")
            else:
                embed.description = "No one has joined the family yet. Be the first!"

            # Update or create message
            if self.dumb_family_message_id:
                try:
                    message = await channel.fetch_message(self.dumb_family_message_id)
                    await message.edit(embed=embed)
                    logger.info(f"Updated Dumb Family message {self.dumb_family_message_id}")
                except discord.NotFound:
                    logger.warning(f"Dumb Family message {self.dumb_family_message_id} not found, creating new one")
                    message = await channel.send(embed=embed)
                    logger.info(f"Created new Dumb Family message {message.id}")
                    logger.info(f"💡 Update .env with: DUMB_FAMILY_MESSAGE_ID={message.id}")
            else:
                message = await channel.send(embed=embed)
                logger.info(f"Created Dumb Family message {message.id}")
                logger.info(f"💡 Update .env with: DUMB_FAMILY_MESSAGE_ID={message.id}")

        except Exception as e:
            logger.error(f"Error updating Dumb Family display: {e}")


async def setup(bot: commands.Bot):
    """Setup function for loading cog"""
    await bot.add_cog(ForeverDumbCog(bot, bot.services))
