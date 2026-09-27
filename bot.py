# path: bot.py
"""Plexbie - Modular Discord Bot for Plex Management"""
import asyncio
import sys
from pathlib import Path

import discord
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
            logger.error(
                f"App command error: /{command_name} by {interaction.user} ({interaction.user.id}) - {error}",
                exc_info=error
            )

            # Send user-friendly error message if not already responded
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "An error occurred while processing your command. Please try again later.",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "An error occurred while processing your command. Please try again later.",
                    ephemeral=True
                )

        # Load all plugins (they can now register webhook routes)
        await self.plugin_manager.load_all_plugins()

        # Register plugin webhook routes BEFORE starting server
        await self.plugin_manager.register_webhook_routes(self.webhook_server)

        # Register Sonarr/Radarr webhooks for media tracking
        from webhooks.sonarr_handler import register_sonarr_webhook
        from webhooks.radarr_handler import register_radarr_webhook

        register_sonarr_webhook(self.webhook_server.app, self)
        register_radarr_webhook(self.webhook_server.app, self)

        # Start webhook server (this freezes the router)
        await self.webhook_server.start()

        # DEBUG: Log what commands are in the tree before syncing
        logger.debug(f"Commands in tree before sync: {len(self.tree.get_commands())}")
        for cmd in self.tree.get_commands():
            logger.debug(f"  - {cmd.name}: {cmd.description}")

        # Sync slash commands (guild-specific only, no global commands)
        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            # Copy global commands to guild tree, then sync
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info(f"Commands synced to guild {self.config.guild_id}")
        else:
            await self.tree.sync()
            logger.info("Commands synced globally")
    
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
