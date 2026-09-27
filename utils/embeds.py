# path: utils/embeds.py
"""Discord embed utilities"""
from typing import Optional, List, Any
import discord
from discord import Embed, Color


def create_info_embed(title: str, description: str = "", **kwargs) -> Embed:
    """Create a standard info embed"""
    embed = Embed(
        title=title,
        description=description,
        color=Color.blue(),
        **kwargs
    )
    return embed


def create_success_embed(title: str, description: str = "", **kwargs) -> Embed:
    """Create a success embed"""
    embed = Embed(
        title=title,
        description=description,
        color=Color.green(),
        **kwargs
    )
    return embed


def create_error_embed(title: str, description: str = "", **kwargs) -> Embed:
    """Create an error embed"""
    embed = Embed(
        title=title,
        description=description,
        color=Color.red(),
        **kwargs
    )
    return embed


def create_warning_embed(title: str, description: str = "", **kwargs) -> Embed:
    """Create a warning embed"""
    embed = Embed(
        title=title,
        description=description,
        color=Color.orange(),
        **kwargs
    )
    return embed


def create_media_embed(media_item: dict) -> Embed:
    """Create an embed for media items (movies, TV shows, etc.)"""
    embed = Embed(
        title=media_item.get("title", "Unknown"),
        description=media_item.get("summary", "No description available"),
        color=Color.blue()
    )

    if "year" in media_item:
        embed.add_field(name="Year", value=str(media_item["year"]), inline=True)

    if "rating" in media_item:
        embed.add_field(name="Rating", value=str(media_item["rating"]), inline=True)

    if "type" in media_item:
        embed.add_field(name="Type", value=media_item["type"].title(), inline=True)

    if "thumb" in media_item:
        embed.set_thumbnail(url=media_item["thumb"])

    if "art" in media_item:
        embed.set_image(url=media_item["art"])

    return embed


class PaginationView(discord.ui.View):
    """Pagination view for embeds"""

    def __init__(self, embeds: List[Embed], timeout: float = 180.0):
        super().__init__(timeout=timeout)
        self.embeds = embeds
        self.current_page = 0
        self.message: Optional[discord.Message] = None

        # Disable buttons if only one page
        if len(embeds) <= 1:
            self.previous_button.disabled = True
            self.next_button.disabled = True

    def update_buttons(self):
        """Update button states based on current page"""
        self.previous_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page == len(self.embeds) - 1

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.gray)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to previous page"""
        if self.current_page > 0:
            self.current_page -= 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.gray)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to next page"""
        if self.current_page < len(self.embeds) - 1:
            self.current_page += 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)

    async def on_timeout(self):
        """Disable buttons when view times out"""
        if self.message:
            for item in self.children:
                item.disabled = True
            try:
                await self.message.edit(view=self)
            except:
                pass
