"""Overseerr integration (stub)"""
import discord
from discord import app_commands
from discord.ext import commands

from core.services import BotServices


class OverseerrCog(commands.Cog):
    """Overseerr request commands"""
    
    def __init__(self, bot: commands.Bot, services: BotServices):
        self.bot = bot
        self.services = services
    
    request_group = app_commands.Group(name="request", description="Media requests")
    
    @request_group.command(name="movie", description="Request a movie")
    async def request_movie(self, interaction: discord.Interaction, title: str):
        await interaction.response.send_message("Movie requests not yet implemented", ephemeral=True)
    
    @request_group.command(name="show", description="Request a TV show")
    async def request_show(self, interaction: discord.Interaction, title: str):
        await interaction.response.send_message("TV show requests not yet implemented", ephemeral=True)
    
    @request_group.command(name="status", description="Check request status")
    async def request_status(self, interaction: discord.Interaction):
        await interaction.response.send_message("Request status not yet implemented", ephemeral=True)


async def setup(bot: commands.Bot):
    pass
