# path: plugins/status/cog.py
"""Server status plugin for Plexbie"""
import discord
from discord import app_commands
from discord.ext import commands

from core.blocking import run_blocking
from core.logging import get_logger
from core.services import BotServices
from utils.embeds import create_info_embed, create_error_embed

logger = get_logger(__name__)


def _collect_library_counts(plex):
    """Blocking: (counts by library type, total items). Runs in a worker thread.

    Every attribute here can perform HTTP: sections() lists the libraries and
    LibrarySection.totalSize issues a request per library. Keeping the whole walk
    in one thread hop is the point - offloading only sections() still left one
    blocking request per library on the event loop.
    """
    counts = {}
    total = 0
    for library in plex.library.sections():
        try:
            size = library.totalSize
        except Exception as e:
            logger.warning(f"Could not read size of library {library.title}: {e}")
            continue
        counts[library.type] = counts.get(library.type, 0) + size
        total += size
    return counts, total


class StatusCog(commands.Cog):
    """Server status monitoring commands"""
    
    def __init__(self, bot: commands.Bot, services: BotServices):
        self.bot = bot
        self.services = services
    
    @app_commands.command(name="status", description="Check Plex server status")
    async def plex_status(self, interaction: discord.Interaction):
        """Display Plex server status and statistics"""
        await interaction.response.defer()
        
        try:
            if not self.services.plex_server:
                embed = create_error_embed(
                    "Plex Not Configured",
                    "Plex server connection is not configured. Please contact the bot administrator."
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return
            
            # Get server info
            plex = self.services.plex_server
            
            embed = create_info_embed(
                f"📊 {plex.friendlyName} Status",
                f"Version: {plex.version}"
            )
            
            # Server details
            embed.add_field(name="Platform", value=plex.platform, inline=True)
            embed.add_field(name="Platform Version", value=plex.platformVersion, inline=True)
            
            # Library counts, gathered in a single thread hop. LibrarySection
            # .totalSize is a cached_data_property that performs an HTTP request,
            # so reading it per section in this loop blocked the event loop once
            # per library even though sections() itself was offloaded.
            library_counts, total_items = await run_blocking(
                _collect_library_counts, plex
            )
            
            embed.add_field(
                name="Libraries",
                value=f"📚 Total Items: {total_items:,}",
                inline=False
            )
            
            for lib_type, count in library_counts.items():
                emoji = "🎬" if lib_type == "movie" else "📺" if lib_type == "show" else "🎵"
                embed.add_field(
                    name=f"{emoji} {lib_type.title()}s",
                    value=f"{count:,}",
                    inline=True
                )
            
            # Active sessions
            sessions = await run_blocking(plex.sessions)
            embed.add_field(
                name="Active Streams",
                value=f"👥 {len(sessions)} user(s) streaming",
                inline=False
            )
            
            # Add current sessions if any
            if sessions:
                session_list = []
                for session in sessions[:5]:  # Show max 5
                    user = session.usernames[0] if session.usernames else "Unknown"
                    title = session.title
                    session_list.append(f"• **{user}**: {title}")
                
                embed.add_field(
                    name="Currently Watching",
                    value="\n".join(session_list),
                    inline=False
                )
            
            embed.set_footer(text=f"Server: {self.services.config.plex_url}")
            embed.color = discord.Color.green()
            
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Error getting Plex status: {e}")
            embed = create_error_embed(
                "Status Check Failed",
                f"Could not retrieve server status: {str(e)}"
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="say", description="Make Plexbie send a message (Admin only)")
    @app_commands.describe(
        channel="The channel to send the message to",
        message="The message content"
    )
    async def say_command(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        message: str
    ):
        """Make the bot send a message to a channel"""
        # Check if user is admin
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "You don't have permission to use this command.",
                ephemeral=True
            )
            return

        try:
            # Send the message to the specified channel
            await channel.send(message)

            # Confirm to the user (privately)
            await interaction.response.send_message(
                f"Message sent to {channel.mention}!",
                ephemeral=True
            )

            logger.info(f"{interaction.user} used /say in {channel.name}: {message}")

        except discord.Forbidden:
            await interaction.response.send_message(
                f"I don't have permission to send messages in {channel.mention}.",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error in say command: {e}")
            await interaction.response.send_message(
                f"Failed to send message: {str(e)}",
                ephemeral=True
            )


async def setup(bot: commands.Bot):
    """Setup function for loading cog"""
    # This is called if using bot.load_extension
    pass
