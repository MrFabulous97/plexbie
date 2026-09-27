# path: core/plugin_manager.py
"""Plugin discovery and management system"""
import json
from pathlib import Path
from typing import Dict, List

import discord
from discord.ext import commands

from core.logging import get_logger
from core.services import BotServices

logger = get_logger(__name__)


def cog_class_name(plugin_name: str) -> str:
    """Map a plugin directory name to its cog class name.

    watch_tracking -> WatchTrackingCog

    Single source of truth on purpose. load_plugin derived this with a PascalCase
    join while unload_plugin used str.title(), which yields "Watch_TrackingCog"
    for any multi-word plugin - so remove_cog() silently matched nothing and the
    plugin stayed loaded while being dropped from loaded_cogs.
    """
    return "".join(word.capitalize() for word in plugin_name.split("_")) + "Cog"


class PluginManager:
    """Manages plugin discovery, loading, and lifecycle"""
    
    def __init__(self, bot: commands.Bot, services: BotServices):
        self.bot = bot
        self.services = services
        self.plugins: Dict[str, dict] = {}
        self.loaded_cogs: List[str] = []
    
    async def load_all_plugins(self):
        """Discover and load all plugins from plugins directory"""
        plugins_dir = Path("plugins")

        if not plugins_dir.exists():
            logger.warning(f"⚠️  Plugins directory not found at: {plugins_dir.absolute()}")
            return

        plugin_count = 0
        for plugin_dir in plugins_dir.iterdir():
            if not plugin_dir.is_dir():
                continue

            plugin_count += 1
            await self.load_plugin(plugin_dir)

        logger.info(f"✅ Plugins: Loaded {len(self.loaded_cogs)}/{plugin_count} available plugins")
    
    async def load_plugin(self, plugin_dir: Path):
        """Load a single plugin"""
        plugin_name = plugin_dir.name
        
        # Check for required files
        plugin_json = plugin_dir / "plugin.json"
        cog_py = plugin_dir / "cog.py"
        
        if not plugin_json.exists():
            logger.warning(f"Plugin {plugin_name} missing plugin.json")
            return
        
        if not cog_py.exists():
            logger.warning(f"Plugin {plugin_name} missing cog.py")
            return
        
        try:
            # Load plugin metadata
            with open(plugin_json) as f:
                metadata = json.load(f)
            
            self.plugins[plugin_name] = metadata
            
            # Check if enabled
            if not metadata.get("enabled", True):
                # Disabled plugins are silently skipped
                return

            # Import and load cog
            import_path = f"plugins.{plugin_name}.cog"

            # Dynamic import
            module = __import__(import_path, fromlist=["setup"])

            # Get cog class (e.g., watch_tracking -> WatchTrackingCog)
            cog_class = getattr(module, cog_class_name(plugin_name), None)

            if cog_class:
                # Initialize cog with services
                cog_instance = cog_class(self.bot, self.services)

                # Add to bot
                await self.bot.add_cog(cog_instance)
                self.loaded_cogs.append(plugin_name)
                # Suppress individual plugin load messages
            else:
                logger.warning(f"⚠️  Plugin {plugin_name}: No cog class found")
                
        except Exception as e:
            logger.error(f"Failed to load plugin {plugin_name}: {e}", exc_info=e)
    
    async def unload_plugin(self, plugin_name: str) -> bool:
        """Unload a plugin. Returns True if a cog was actually removed.

        remove_cog() returns None when the name does not match a loaded cog, so
        report that instead of dropping the plugin from loaded_cogs regardless -
        which left the cog running while the manager believed it was gone.
        """
        if plugin_name not in self.loaded_cogs:
            logger.warning(f"Plugin {plugin_name} is not loaded")
            return False

        removed = await self.bot.remove_cog(cog_class_name(plugin_name))
        if removed is None:
            logger.error(
                f"Could not unload plugin {plugin_name}: no cog named "
                f"{cog_class_name(plugin_name)} is registered. Leaving it in "
                f"loaded_cogs so state stays accurate."
            )
            return False

        self.loaded_cogs.remove(plugin_name)
        logger.info(f"Unloaded plugin: {plugin_name}")
        return True

    async def reload_plugin(self, plugin_name: str) -> bool:
        """Reload a plugin. Returns True if it was unloaded and loaded again.

        Note: this re-instantiates the cog but does NOT re-read the file from
        disk. __import__ returns the already-cached module, so code changes need
        a process restart. Bail out if the unload failed, otherwise add_cog would
        raise "cog already loaded" against the still-registered instance.
        """
        if not await self.unload_plugin(plugin_name):
            return False

        plugin_dir = Path(f"plugins/{plugin_name}")
        await self.load_plugin(plugin_dir)
        return plugin_name in self.loaded_cogs

    async def register_webhook_routes(self, webhook_server):
        """Allow plugins to register webhook routes before server starts"""
        for cog in self.bot.cogs.values():
            if hasattr(cog, "register_webhook_routes"):
                try:
                    await cog.register_webhook_routes(webhook_server)
                except Exception as e:
                    logger.error(f"Error registering webhook routes for {cog.__class__.__name__}: {e}")
