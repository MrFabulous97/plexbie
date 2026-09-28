# path: bot.py
"""Plexbie - Modular Discord Bot for Plex Management"""
import asyncio
import sys
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from core.config import Config
from core.logging import setup_logging, get_logger
from core.plugin_manager import PluginManager
from core.services import BotServices
from core.webhooks import WebhookServer
from database.session import init_database

logger = get_logger(__name__)


class Plexbie(commands.Bot):
    """Main bot class with modular plugin support"""
    
    def __init__(self, config: Config, services: BotServices):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        
        super().__init__(
            command_prefix="!",  # Not used but required
            intents=intents,
            help_command=None
        )
        
        self.config = config
        self.services = services
        self.plugin_manager = PluginManager(self, services)
        self.webhook_server = WebhookServer(services)
    
    async def setup_hook(self):
        """Initialize bot components on startup"""
        logger.info("Starting Plexbie bot setup")

        # Set up global interaction logging
        @self.tree.error
        async def on_app_command_error(interaction: discord.Interaction, error: Exception):
            """Global error handler for app commands"""
            command_name = interaction.command.name if interaction.command else "unknown"

            # A refused command is not a crash. These used to be logged at ERROR
            # with a full traceback and answered with "An error occurred... try
            # again later", which told the user nothing and made ordinary denials
            # look like failures in the log.
            if isinstance(error, app_commands.MissingPermissions):
                logger.info(
                    f"/{command_name} denied for {interaction.user} "
                    f"({interaction.user.id}): missing permissions"
                )
                message = "You don't have permission to use this command."
            elif isinstance(error, app_commands.NoPrivateMessage):
                logger.info(f"/{command_name} attempted in a DM by {interaction.user}")
                message = "This command only works inside a server."
            elif isinstance(error, app_commands.CommandOnCooldown):
                logger.info(f"/{command_name} on cooldown for {interaction.user}")
                message = f"That command is on cooldown - try again in {error.retry_after:.0f}s."
            else:
                logger.error(
                    f"App command error: /{command_name} by {interaction.user} "
                    f"({interaction.user.id}) - {error}",
                    exc_info=error,
                )
                message = "An error occurred while processing your command. Please try again later."

            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(message, ephemeral=True)
                else:
                    await interaction.followup.send(message, ephemeral=True)
            except discord.HTTPException as e:
                # The interaction may have expired; nothing more to do than note it.
                logger.debug(f"Could not deliver error message for /{command_name}: {e}")

        # Load all plugins (they can now register webhook routes)
        await self.plugin_manager.load_all_plugins()

        # Register plugin webhook routes BEFORE starting server
        await self.plugin_manager.register_webhook_routes(self.webhook_server)

        # Register Sonarr/Radarr webhooks for media tracking
        from webhooks.sonarr_handler import register_sonarr_webhook
        from webhooks.radarr_handler import register_radarr_webhook

        register_sonarr_webhook(self.webhook_server, self)
        register_radarr_webhook(self.webhook_server, self)

        # Start webhook server (this freezes the router)
        await self.webhook_server.start()

        # DEBUG: Log what commands are in the tree before syncing
        logger.debug(f"Commands in tree before sync: {len(self.tree.get_commands())}")
        for cmd in self.tree.get_commands():
            logger.debug(f"  - {cmd.name}: {cmd.description}")

        # Sync slash commands. Guild-scoped only: a guild sync is immediate,
        # whereas global commands take up to an hour to propagate.
        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info(
                f"Commands: {len(synced)} synced to guild {self.config.guild_id}"
            )

            # Then make sure the global scope is empty. Discord merges global and
            # guild commands in the picker, so anything left there from an earlier
            # global sync appears a second time - this deployment had accumulated
            # 15 such duplicates, which nothing in the code ever removed. Cheap and
            # idempotent: after the first cleanup fetch_commands() returns nothing.
            try:
                stale = await self.tree.fetch_commands()
                if stale:
                    logger.warning(
                        f"Commands: removing {len(stale)} stale global command(s) "
                        f"that duplicate the guild-scoped ones"
                    )
                    self.tree.clear_commands(guild=None)
                    await self.tree.sync()
            except discord.HTTPException as e:
                # Not worth failing startup over; the duplicates are cosmetic.
                logger.warning(f"Could not check for stale global commands: {e}")
        else:
            synced = await self.tree.sync()
            logger.info(f"Commands: {len(synced)} synced globally")
    
    async def on_ready(self):
        """Bot is ready and connected"""
        logger.info("=" * 60)
        logger.info("🎉 PLEXBIE DISCORD BOT - READY")
        logger.info("=" * 60)
        logger.info(f"✅ Bot User: {self.user} (ID: {self.user.id})")
        logger.info(f"✅ Connected Guilds: {len(self.guilds)}")
        logger.info(f"✅ Loaded Plugins: {len(self.plugin_manager.loaded_cogs)}")

        # List loaded plugins
        if self.plugin_manager.loaded_cogs:
            logger.info("   📦 Active Plugins:")
            for plugin_name in self.plugin_manager.loaded_cogs:
                plugin_meta = self.plugin_manager.plugins.get(plugin_name, {})
                version = plugin_meta.get('version', '1.0.0')
                logger.info(f"      • {plugin_name} v{version}")

        logger.info(f"✅ Webhook Server: Port {self.config.webhook_port}")
        logger.info(f"✅ Commands: Synced and ready to use")
        logger.info("=" * 60)
        logger.info("🚀 All systems operational!")
        logger.info("=" * 60)

        # Set presence
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Plex Media Server"
            )
        )
    
    async def on_command_error(self, ctx, error):
        """Global error handler"""
        logger.error(f"Command error: {error}", exc_info=error)


async def main():
    """Main entry point"""
    # Load environment variables from .env file
    load_dotenv("config/.env")

    # Setup logging
    setup_logging()

    try:
        logger.info("=" * 60)
        logger.info("🤖 PLEXBIE - Starting Up...")
        logger.info("=" * 60)

        # Load configuration
        config = Config()
        logger.info("✅ Configuration loaded")

        # Initialize database
        await init_database(config.db_url)
        logger.info("✅ Database initialized (SQLite)")

        # Create services container
        services = BotServices(config)
        await services.initialize()
        logger.info("✅ Services initialized")

        logger.info("=" * 60)
        
        # Create and run bot
        bot = Plexbie(config, services)
        
        async with bot:
            await bot.start(config.discord_bot_token)
            
    except KeyboardInterrupt:
        logger.info("Received shutdown signal")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=e)
        sys.exit(1)
    finally:
        logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
