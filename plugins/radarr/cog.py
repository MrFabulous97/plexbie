"""Radarr integration (stub)"""
import discord
from discord import app_commands
from discord.ext import commands

from core.services import BotServices


class RadarrCog(commands.Cog):
    """Radarr movie management"""
    
    def __init__(self, bot: commands.Bot, services: BotServices):
        self.bot = bot
        self.services = services
    
    radarr_group = app_commands.Group(name="radarr", description="Movie management")
    
    @radarr_group.command(name="search", description="Search for movies")
    async def radarr_search(self, interaction: discord.Interaction, movie: str):
        await interaction.response.send_message("Radarr search not yet implemented", ephemeral=True)
    
    @radarr_group.command(name="queue", description="View download queue")
    async def radarr_queue(self, interaction: discord.Interaction):
        await interaction.response.send_message("Radarr queue not yet implemented", ephemeral=True)


async def setup(bot: commands.Bot):
    pass
