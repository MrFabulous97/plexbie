"""Search plugin (stub)"""
import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional

from core.services import BotServices


class SearchCog(commands.Cog):
    """Search commands"""
    
    def __init__(self, bot: commands.Bot, services: BotServices):
        self.bot = bot
        self.services = services
    
    @app_commands.command(name="search", description="Search Plex libraries")
    @app_commands.guild_only()
    @app_commands.describe(
        query="Search query",
        media_type="Type of media to search"
    )
    async def plex_search(
        self,
        interaction: discord.Interaction,
        query: str,
        media_type: Optional[str] = None
    ):
        await interaction.response.send_message("Search not yet implemented", ephemeral=True)


async def setup(bot: commands.Bot):
    pass
