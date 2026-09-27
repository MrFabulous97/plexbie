# path: utils/embeds.py
"""Discord embed utilities"""
from typing import Optional, List, Any
import discord
from discord import Embed, Color

from core.logging import get_logger

logger = get_logger(__name__)


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


#: Discord's hard limits. Exceeding either is rejected with HTTPException 400.
MAX_FIELD_VALUE = 1024
MAX_EMBED_TOTAL = 6000


def truncate_field(text: str, limit: int = MAX_FIELD_VALUE, suffix: str = "\n… truncated") -> str:
    """Clamp an embed field value to Discord's limit, keeping whole lines.

    Discord rejects a field value over 1024 characters with HTTPException 400,
    which callers surface as a generic error - so an over-long list makes a command
    look broken exactly when it has the most to report. /cleanup-plex-users hit
    this: ten entries describing over-long usernames came to roughly 2068
    characters.
    """
    if text is None:
        return ""
    if len(text) <= limit:
        return text

    room = limit - len(suffix)
    if room <= 0:
        return text[:limit]

    clipped = text[:room]
    # Prefer cutting at a line boundary so an entry is not left half-rendered.
    newline = clipped.rfind("\n")
    if newline > room // 2:
        clipped = clipped[:newline]
    return clipped + suffix


class PaginationView(discord.ui.View):
    """Pagination view for embeds.

    Pass author_id to restrict the buttons to whoever ran the command. The view
    edits one shared message and keeps current_page as shared state, so without
    that restriction any user could move the page another user was reading.
    """

    def __init__(
        self,
        embeds: List[Embed],
        timeout: float = 180.0,
        author_id: Optional[int] = None,
    ):
        super().__init__(timeout=timeout)
        self.embeds = embeds
        self.current_page = 0
        self.author_id = author_id
        self.message: Optional[discord.Message] = None

        # Set the initial states here rather than only handling the single-page
        # case. Previously Previous was left enabled on page 1, and its callback
        # returned without responding - which Discord surfaces to the user as
        # "This interaction failed".
        self.update_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.author_id is None or interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(
            "This isn't your result - run the command yourself to page through it.",
            ephemeral=True,
        )
        return False

    def update_buttons(self):
        """Update button states based on current page"""
        single_page = len(self.embeds) <= 1
        self.previous_button.disabled = single_page or self.current_page == 0
        self.next_button.disabled = single_page or self.current_page >= len(self.embeds) - 1

    async def _show_page(self, interaction: discord.Interaction, new_page: int):
        """Move to `new_page`, clamped, and always answer the interaction.

        Answering unconditionally matters: a callback that returns without
        responding leaves Discord to time the interaction out and show the user an
        error, which is what happened at the first and last pages.
        """
        self.current_page = max(0, min(new_page, len(self.embeds) - 1))
        self.update_buttons()
        # Remember the message so on_timeout can disable the buttons even if the
        # caller never assigned it.
        self.message = interaction.message
        await interaction.response.edit_message(
            embed=self.embeds[self.current_page], view=self
        )

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.gray)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to previous page"""
        await self._show_page(interaction, self.current_page - 1)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.gray)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to next page"""
        await self._show_page(interaction, self.current_page + 1)

    async def on_timeout(self):
        """Disable buttons when the view expires, so they stop looking clickable."""
        for item in self.children:
            item.disabled = True

        if not self.message:
            # Nothing to edit. Callers should assign view.message after sending,
            # and _show_page captures it on the first interaction.
            return

        try:
            await self.message.edit(view=self)
        except discord.HTTPException as e:
            logger.debug(f"Could not disable paginator buttons on timeout: {e}")
