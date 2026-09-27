"""Bazarr webhook handler (stub)"""
from discord.ext import commands

from core.services import BotServices


class BazarrCog(commands.Cog):
    """Bazarr subtitle notifications"""
    
    def __init__(self, bot: commands.Bot, services: BotServices):
        self.bot = bot
        self.services = services
        # This plugin mainly receives webhooks, no commands


async def setup(bot: commands.Bot):
    pass
