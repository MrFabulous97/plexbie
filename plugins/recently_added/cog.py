# path: plugins/recently_added/cog.py
"""Recently added media plugin"""
import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional, List

import plexapi.exceptions
from core.logging import get_logger
from core.services import BotServices
from utils.embeds import create_media_embed, create_error_embed, PaginationView

logger = get_logger(__name__)


class RecentlyAddedCog(commands.Cog):
    """View recently added media"""

    def __init__(self, bot: commands.Bot, services: BotServices):
        self.bot = bot
        self.services = services

    @app_commands.command(name="recent", description="View recently added media")
    async def plex_recently(self, interaction: discord.Interaction):
        """Display recently added media with pagination"""
        await interaction.response.defer()

        # Fixed parameters - show 10 items from all libraries
        library = None
        limit = 10

        try:
            if not self.services.plex_server:
                embed = create_error_embed(
                    "Plex Not Configured",
                    "Plex server connection is not configured."
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            plex = self.services.plex_server

            # Get recently added items
            recent_items = []

            if library:
                # Check specific library
                try:
                    section = plex.library.section(library)
                    recent_items = section.recentlyAdded(maxresults=limit)
                except (plexapi.exceptions.NotFound, KeyError) as e:  # Library not found
                    embed = create_error_embed(
                        "Library Not Found",
                        f"Could not find library: {library}"
                    )
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    return
            else:
                # Get from all libraries
                for section in plex.library.sections():
                    items = section.recentlyAdded(maxresults=limit)
                    recent_items.extend(items)

                # Sort by date added and limit
                recent_items.sort(key=lambda x: x.addedAt, reverse=True)
                recent_items = recent_items[:limit]

            if not recent_items:
                embed = create_error_embed(
                    "No Recent Items",
                    "No recently added media found."
                )
                await interaction.followup.send(embed=embed)
                return

            # Create embeds for pagination
            embeds: List[discord.Embed] = []
            items_per_page = 5

            for i in range(0, len(recent_items), items_per_page):
                page_items = recent_items[i:i + items_per_page]

                embed = discord.Embed(
                    title="🆕 Recently Added Media",
                    color=discord.Color.blue()
                )

                for item in page_items:
                    # Build item description
                    title = item.title
                    media_type = item.type

                    # Get year
                    year = getattr(item, "year", None)

                    # Get rating if available
                    rating = getattr(item, "rating", None)

                    # Build field value
                    value_parts = [f"Type: {media_type.title()}"]
                    if year:
                        value_parts.append(f"Year: {year}")
                    if rating:
                        value_parts.append(f"Rating: ⭐ {rating:.1f}")

                    # For TV shows, add episode info
                    if media_type == "episode":
                        show_title = item.grandparentTitle
                        season = item.parentIndex
                        episode = item.index
                        title = f"{show_title} - S{season:02d}E{episode:02d}: {item.title}"

                    embed.add_field(
                        name=title[:256],  # Discord field name limit
                        value="\n".join(value_parts),
                        inline=False
                    )

                # Add page info
                embed.set_footer(
                    text=f"Page {i // items_per_page + 1} of {(len(recent_items) - 1) // items_per_page + 1}"
                )

                embeds.append(embed)

            # Send with pagination if multiple pages
            if len(embeds) > 1:
                view = PaginationView(embeds)
                await interaction.followup.send(embed=embeds[0], view=view)
            else:
                await interaction.followup.send(embed=embeds[0])

        except Exception as e:
            logger.error(f"Error getting recently added: {e}")
            embed = create_error_embed(
                "Failed to Get Recently Added",
                str(e)
            )
            await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    """Setup function for loading cog"""
    await bot.add_cog(RecentlyAddedCog(bot, bot.services))
