"""Sonarr integration (stub)"""
import discord
from discord import app_commands
from discord.ext import commands

from core.services import BotServices


class SonarrCog(commands.Cog):
    """Sonarr TV show management"""
    
    def __init__(self, bot: commands.Bot, services: BotServices):
        self.bot = bot
        self.services = services
    
    sonarr_group = app_commands.Group(name="sonarr", description="TV show management", guild_only=True)
    
    @sonarr_group.command(name="search", description="Search for TV shows")
    async def sonarr_search(self, interaction: discord.Interaction, show: str):
        await interaction.response.send_message("Sonarr search not yet implemented", ephemeral=True)
    
    @sonarr_group.command(name="add", description="Add TV show to Sonarr")
    async def sonarr_add(self, interaction: discord.Interaction, tvdb_id: int):
        await interaction.response.send_message("Sonarr add not yet implemented", ephemeral=True)


async def setup(bot: commands.Bot):
    pass
