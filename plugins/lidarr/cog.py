"""Lidarr integration (stub)"""
import discord
from discord import app_commands
from discord.ext import commands

from core.services import BotServices


class LidarrCog(commands.Cog):
    """Lidarr music management"""
    
    def __init__(self, bot: commands.Bot, services: BotServices):
        self.bot = bot
        self.services = services
    
    lidarr_group = app_commands.Group(name="lidarr", description="Music management", guild_only=True)
    
    @lidarr_group.command(name="search", description="Search for music")
    async def lidarr_search(self, interaction: discord.Interaction, artist: str):
        await interaction.response.send_message("Lidarr search not yet implemented", ephemeral=True)
    
    @lidarr_group.command(name="add", description="Add artist to Lidarr")
    async def lidarr_add(self, interaction: discord.Interaction, musicbrainz_id: str):
        await interaction.response.send_message("Lidarr add not yet implemented", ephemeral=True)


async def setup(bot: commands.Bot):
    pass
