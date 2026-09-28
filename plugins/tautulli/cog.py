"""Tautulli analytics (stub)"""
import discord
from discord import app_commands
from discord.ext import commands

from core.services import BotServices


class TautulliCog(commands.Cog):
    """Tautulli statistics and analytics"""
    
    def __init__(self, bot: commands.Bot, services: BotServices):
        self.bot = bot
        self.services = services
    
    stats_group = app_commands.Group(name="stats", description="Server statistics", guild_only=True)
    
    @stats_group.command(name="history", description="View watch history")
    async def stats_history(self, interaction: discord.Interaction):
        await interaction.response.send_message("Watch history not yet implemented", ephemeral=True)
    
    @stats_group.command(name="top", description="View top users")
    async def stats_top(self, interaction: discord.Interaction):
        await interaction.response.send_message("Top users not yet implemented", ephemeral=True)
    
    @stats_group.command(name="now", description="View currently watching")
    async def stats_now(self, interaction: discord.Interaction):
        await interaction.response.send_message("Now watching not yet implemented", ephemeral=True)


async def setup(bot: commands.Bot):
    pass
