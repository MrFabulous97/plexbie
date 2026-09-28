# path: plugins/watch_tracking/cog.py
"""Watch tracking system with channel-based stats displays"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.blocking import run_blocking
from utils.formatting import episode_label
from utils.embeds import truncate_field
from core.logging import get_logger
from core.services import BotServices
from database.session import get_session
from plugins.user_mgmt.models import PlexUser
from sqlalchemy import select
from plugins.watch_party.models import WatchPartyCredit

logger = get_logger(__name__)


# Storage files
STREAKS_FILE = Path("config/watch_streaks.json")
MESSAGE_IDS_FILE = Path("config/stats_messages.json")
USER_ALIASES_FILE = Path("config/user_aliases.json")

# Latency compensation for Discord timestamp display (in seconds)
# This offset is added to timestamps to compensate for network latency,
# Discord API processing time, and client rendering delay.
# For the 10-second "Now Watching" loop, we want the countdown to show ~10 seconds
# when the message is displayed, so this should be tuned based on observed drift.
TIMESTAMP_LATENCY_OFFSET = 96


def _collect_watched_today(plex):
    """Blocking: map each Plex account name -> did they watch anything today.

    Runs in a worker thread. One request lists the accounts and one more fetches
    history per account, so the cost scales with user count - exactly the shape
    that must not sit on the event loop.

    History rows with no viewedAt are treated as "not today" rather than raising:
    plexapi can return them, and an AttributeError here used to abort the whole
    streak pass for every user.
    """
    watched = {}
    today = datetime.now().date()
    for account in plex.systemAccounts():
        name = account.name
        if not name:
            continue
        history = plex.history(maxresults=100, accountID=account.id)
        watched[name] = any(
            getattr(entry, "viewedAt", None) is not None
            and entry.viewedAt.date() == today
            for entry in history
        )
    return watched


class WatchTrackingCog(commands.Cog):
    """Watch tracking with auto-updating channel-based stats"""

    def __init__(self, bot: commands.Bot, services: BotServices):
        self.bot = bot
        self.services = services

        # Single stats channel with three separate messages
        self.stats_channel: Optional[discord.TextChannel] = None

        self.now_watching_message_id: Optional[int] = None
        self.leaderboard_message_id: Optional[int] = None
        self.streaks_message_id: Optional[int] = None

        # Cache for Plex username -> Discord display name mappings
        self._username_cache: Dict[str, str] = {}
        self._cache_last_updated: Optional[datetime] = None

        # Alias file contents, invalidated by (mtime, size) - see _load_aliases.
        self._aliases_cache: Dict[str, str] = {}
        self._aliases_stamp = None

        # Ensure files exist
        STREAKS_FILE.parent.mkdir(exist_ok=True)
        if not STREAKS_FILE.exists():
            STREAKS_FILE.write_text("{}")

        # Start background tasks
        self.update_now_watching.start()
        self.update_leaderboard.start()
        self.update_watch_streaks.start()
        self.update_streaks_display.start()

    def cog_unload(self):
        """Cleanup tasks on unload"""
        self.update_now_watching.cancel()
        self.update_leaderboard.cancel()
        self.update_watch_streaks.cancel()
        self.update_streaks_display.cancel()

    async def _ensure_channel(self):
        """Fetch stats channel from config"""
        if not self.stats_channel:
            channel_id = self.services.config.stats_channel_id
            if not channel_id:
                logger.error("STATS_CHANNEL_ID not configured")
                return False

            try:
                self.stats_channel = self.bot.get_channel(channel_id)
                if not self.stats_channel:
                    self.stats_channel = await self.bot.fetch_channel(channel_id)

                # Load message IDs from config first, then fall back to file
                self.now_watching_message_id = self.services.config.now_watching_message_id
                self.leaderboard_message_id = self.services.config.leaderboard_message_id
                self.streaks_message_id = self.services.config.watch_streak_message_id

                # If not in config, try loading from file (legacy support)
                if not any([self.now_watching_message_id, self.leaderboard_message_id, self.streaks_message_id]):
                    message_ids = self._load_message_ids()
                    self.now_watching_message_id = self.now_watching_message_id or message_ids.get('now_watching')
                    self.leaderboard_message_id = self.leaderboard_message_id or message_ids.get('leaderboard')
                    self.streaks_message_id = self.streaks_message_id or message_ids.get('streaks')

                logger.info(f"Loaded stats channel: {self.stats_channel.id}")
            except Exception as e:
                logger.error(f"Failed to fetch stats channel: {e}")
                return False

        return True

    def _load_message_ids(self):
        """Load message IDs from persistent storage"""
        try:
            if MESSAGE_IDS_FILE.exists():
                return json.loads(MESSAGE_IDS_FILE.read_text())
        except Exception as e:
            logger.error(f"Error loading message IDs: {e}")
        return {}

    def _save_message_id(self, channel_name: str, message_id: int):
        """Save message ID to persistent storage"""
        try:
            message_data = self._load_message_ids()
            message_data[channel_name] = message_id
            MESSAGE_IDS_FILE.parent.mkdir(exist_ok=True)
            MESSAGE_IDS_FILE.write_text(json.dumps(message_data, indent=2))
        except Exception as e:
            logger.error(f"Error saving message ID: {e}")

    async def _refresh_username_cache(self):
        """Refresh the Plex username -> Discord display name cache"""
        try:
            # Refresh cache every 5 minutes
            if (self._cache_last_updated and
                (datetime.utcnow() - self._cache_last_updated).total_seconds() < 300):
                return

            async with get_session() as session:
                result = await session.execute(
                    select(PlexUser).where(PlexUser.discord_id.isnot(None))
                )
                linked_users = result.scalars().all()

            # Build cache: plex_username -> discord display name
            self._username_cache = {}
            for user in linked_users:
                # Try to get Discord member for nickname
                try:
                    guild = self.bot.get_guild(self.services.config.guild_id)
                    if guild:
                        member = guild.get_member(user.discord_id)
                        if member:
                            # Use nickname if available, otherwise username
                            display_name = member.nick if member.nick else member.name
                            self._username_cache[user.plex_username] = display_name
                            continue
                except:
                    pass

                # Fallback to stored Discord username
                if user.discord_username:
                    self._username_cache[user.plex_username] = user.discord_username

            self._cache_last_updated = datetime.utcnow()
            logger.debug(f"Refreshed username cache with {len(self._username_cache)} mappings")

        except Exception as e:
            logger.error(f"Error refreshing username cache: {e}")

    def _get_display_name(self, plex_username: str) -> str:
        """Get Discord display name for a Plex username, or return Plex username if not linked"""
        # First resolve any alias to the primary username
        primary_username = self._resolve_alias(plex_username)
        return self._username_cache.get(primary_username, primary_username)

    def _load_aliases(self) -> Dict[str, str]:
        """User aliases, cached and re-read only when the file actually changes.

        This is called once per streaming user on every tick of the 10-second
        update_now_watching loop (via _get_display_name -> _resolve_alias), and it
        is a synchronous read on the event loop. Unconditionally opening, reading
        and JSON-parsing the file there came to tens of thousands of blocking reads
        a day to produce the same handful of mappings.

        Keyed on (mtime, size) rather than a timer so that editing the file still
        takes effect without a restart. The steady-state cost is one stat().
        """
        try:
            stat = USER_ALIASES_FILE.stat()
        except OSError:
            # Missing or unreadable: no aliases, and nothing to invalidate against.
            self._aliases_stamp = None
            self._aliases_cache = {}
            return self._aliases_cache

        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._aliases_stamp:
            return self._aliases_cache

        try:
            data = json.loads(USER_ALIASES_FILE.read_text())
            self._aliases_cache = data.get('aliases', {}) or {}
            self._aliases_stamp = stamp
        except Exception as e:
            logger.error(f"Error loading user aliases: {e}")
            # Keep whatever was last parsed successfully, but do not record the
            # stamp - a corrupt file should be retried, not cached as empty.
            self._aliases_stamp = None
        return self._aliases_cache

    def _resolve_alias(self, username: str) -> str:
        """Resolve a username to its primary name using aliases"""
        aliases = self._load_aliases()
        return aliases.get(username, username)

    def _apply_aliases_to_users(self, users: List[dict]) -> List[dict]:
        """Combine watch times for aliased users"""
        aliases = self._load_aliases()
        if not aliases:
            return users

        # Group users by their primary name
        combined: Dict[str, dict] = {}
        for user in users:
            friendly_name = user.get('friendly_name', 'Unknown')
            # Map to primary name if aliased
            primary_name = aliases.get(friendly_name, friendly_name)

            if primary_name in combined:
                # Add to existing entry
                combined[primary_name]['duration'] = combined[primary_name].get('duration', 0) + user.get('duration', 0)
                combined[primary_name]['plays'] = combined[primary_name].get('plays', 0) + user.get('plays', 0)
            else:
                # Create new entry with primary name
                combined[primary_name] = {
                    'friendly_name': primary_name,
                    'duration': user.get('duration', 0),
                    'plays': user.get('plays', 0),
                    'user_id': user.get('user_id')
                }

        return list(combined.values())

    async def _get_watch_party_credits(self) -> Dict[str, int]:
        """Get aggregated watch party credits by plex_username"""
        try:
            async with get_session() as session:
                result = await session.execute(select(WatchPartyCredit))
                credits = result.scalars().all()
            return {credit.plex_username: credit.total_duration for credit in credits}
        except Exception as e:
            logger.error(f"Error fetching watch party credits: {e}")
            return {}


    @tasks.loop(seconds=10)
    async def update_now_watching(self):
        """Update now watching display every 10 seconds"""
        if not await self._ensure_channel():
            return

        if not self.services.plex_server:
            await self.services.reconnect_plex()
            if not self.services.plex_server:
                return

        try:
            # Refresh username cache
            await self._refresh_username_cache()

            # Get active sessions
            # Shared snapshot: watch_party polls the same fact on the same 10s
            # cadence, and service_health every 30s. See services.plex_sessions.
            sessions = await self.services.plex_sessions()

            embed = discord.Embed(
                title="📺 Now Watching on Plex",
                description="",
                color=discord.Color.blue()
            )

            if sessions:
                for session in sessions:
                    plex_user = session.usernames[0] if session.usernames else "Unknown"
                    # Get Discord display name if linked
                    display_name = self._get_display_name(plex_user)
                    title = session.title

                    # Get progress
                    if hasattr(session, 'viewOffset') and hasattr(session, 'duration'):
                        progress = int((session.viewOffset / session.duration) * 100)
                        progress_bar = f"{'█' * (progress // 10)}{'░' * (10 - progress // 10)} {progress}%"
                    else:
                        progress_bar = "Streaming..."

                    # Get player state
                    player_state = "▶️" if session.players[0].state == 'playing' else "⏸️"

                    field_value = f"{player_state} {title}\n`{progress_bar}`"

                    if session.type == 'episode':
                        label = episode_label(
                            session.grandparentTitle,
                            session.parentIndex,
                            session.index,
                        )
                        field_value = f"{player_state} {label}\n`{progress_bar}`"

                    embed.add_field(name=f"👤 {display_name}", value=field_value, inline=False)
            else:
                embed.description = "No one is currently watching."

            # Update existing message only (never create new ones)
            if self.now_watching_message_id:
                try:
                    # A partial message edits without fetching first. The fetched body
                    # was never read, so each update cost two REST calls instead of
                    # one: the GET and PATCH counts on this route were identical in a
                    # live sample (98/98), and this loop runs every 10 seconds.
                    message = self.stats_channel.get_partial_message(self.now_watching_message_id)

                    # Calculate timestamp with latency compensation
                    # Add extra seconds to account for network/API/rendering delays
                    next_update = int((datetime.now(timezone.utc) + timedelta(seconds=10 + TIMESTAMP_LATENCY_OFFSET)).timestamp())

                    # Update description with timestamp
                    if sessions:
                        embed.description = f"*Next update <t:{next_update}:R>*"
                    else:
                        embed.description = f"No one is currently watching.\n\n*Next update <t:{next_update}:R>*"

                    await message.edit(embed=embed)
                except Exception as e:
                    logger.error(f"Could not edit Now Watching message {self.now_watching_message_id}: {e}")
            else:
                logger.warning("No Now Watching message ID configured. Set NOW_WATCHING_MESSAGE_ID in .env")

        except Exception as e:
            logger.error(f"Error updating now watching: {e}")

    @update_now_watching.before_loop
    async def before_update_now_watching(self):
        await self.bot.wait_until_ready()
        # Wait 15 seconds - posts second (after Leaderboard)
        import asyncio
        await asyncio.sleep(15)

    @tasks.loop(seconds=300)
    async def update_leaderboard(self):
        """Update leaderboard display every 5 minutes"""
        if not await self._ensure_channel():
            return

        try:
            # Refresh username cache
            await self._refresh_username_cache()

            if not self.services.config.tautulli_url:
                # Skip if Tautulli not configured
                return

            # Get stats from Tautulli
            url = f"{self.services.config.tautulli_url}/api/v2"
            params = {
                "apikey": self.services.config.tautulli_token,
                "cmd": "get_users_table"
            }

            async with self.services.http_session.get(url, params=params) as response:
                if response.status != 200:
                    return

                data = await response.json()
                users = data.get('response', {}).get('data', {}).get('data', [])

            if not users:
                return

            # Apply aliases to combine duplicate users
            users = self._apply_aliases_to_users(users)

            # Add watch party credits to user totals
            watch_party_credits = await self._get_watch_party_credits()
            for user in users:
                plex_username = user.get("friendly_name", "")
                extra_duration = watch_party_credits.get(plex_username, 0)
                if extra_duration > 0:
                    user["duration"] = user.get("duration", 0) + extra_duration

            # Filter out users with zero watch time
            users = [user for user in users if user.get('duration', 0) > 0]

            # Sort by total duration
            users.sort(key=lambda x: x.get('duration', 0), reverse=True)

            medals = ['🥇', '🥈', '🥉', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣', '🔟']

            embed = discord.Embed(
                title="🏆 PLEX WATCH LEADERBOARD 🏆",
                description="",  # Will be set with timestamp below
                color=discord.Color.gold()
            )

            # All-time top 10
            all_time_text = []
            for i, user in enumerate(users[:10]):
                medal = medals[i] if i < len(medals) else '🔸'
                total_seconds = user.get('duration', 0)
                hours = total_seconds // 3600
                minutes = (total_seconds % 3600) // 60

                # Format time based on duration
                if hours > 0:
                    time_str = f"{hours:,}h {minutes}m"
                else:
                    time_str = f"{minutes}m"

                # Get Plex username and map to Discord display name
                plex_username = user.get('friendly_name', 'Unknown')
                display_name = self._get_display_name(plex_username)

                all_time_text.append(f"{medal} **{display_name}** - {time_str}")

            embed.add_field(
                name="👑 All-Time Champions",
                value=truncate_field("\n".join(all_time_text) or "No data yet"),
                inline=False
            )

            # Update existing message only (never create new ones)
            if self.leaderboard_message_id:
                try:
                    # Calculate timestamp with latency compensation
                    next_update = int((datetime.now(timezone.utc) + timedelta(seconds=300 + TIMESTAMP_LATENCY_OFFSET)).timestamp())

                    # Update description with timestamp
                    embed.description = f"Compete for the crown of ultimate couch potato!\n\n*Next update <t:{next_update}:R>*"

                    # Partial message: edits without the wasted GET (see update_now_watching).
                    message = self.stats_channel.get_partial_message(self.leaderboard_message_id)
                    await message.edit(embed=embed)
                except Exception as e:
                    logger.error(f"Could not edit Leaderboard message {self.leaderboard_message_id}: {e}")
            else:
                logger.warning("No Leaderboard message ID configured. Set LEADERBOARD_MESSAGE_ID in .env")

        except Exception as e:
            logger.error(f"Error updating leaderboard: {e}")

    @update_leaderboard.before_loop
    async def before_update_leaderboard(self):
        await self.bot.wait_until_ready()
        # Wait 10 seconds - posts first
        import asyncio
        await asyncio.sleep(10)

    @tasks.loop(hours=1)
    async def update_watch_streaks(self):
        """Calculate watch streaks hourly (background calculation)"""
        if not self.services.plex_server:
            await self.services.reconnect_plex()
            if not self.services.plex_server:
                return

        try:
            # Load and update streaks
            try:
                streaks = json.loads(STREAKS_FILE.read_text())
            except:
                streaks = {}

            today = datetime.now().date().isoformat()
            yesterday = (datetime.now() - timedelta(days=1)).date().isoformat()

            # Gather every account's watch-today flag in a single thread hop.
            # This is one request to list accounts plus one history request per
            # account; done inline it stalled the loop for the sum of all of them.
            watched_by_user = await run_blocking(
                _collect_watched_today, self.services.plex_server
            )

            for username, watched_today in watched_by_user.items():
                if username not in streaks:
                    streaks[username] = {'current': 0, 'longest': 0, 'last_watched': None}

                if watched_today:
                    if streaks[username]['last_watched'] == yesterday:
                        streaks[username]['current'] += 1
                    else:
                        streaks[username]['current'] = 1

                    streaks[username]['last_watched'] = today

                    if streaks[username]['current'] > streaks[username]['longest']:
                        streaks[username]['longest'] = streaks[username]['current']
                else:
                    if streaks[username]['last_watched'] != yesterday:
                        streaks[username]['current'] = 0

            STREAKS_FILE.write_text(json.dumps(streaks, indent=2))

        except Exception as e:
            logger.error(f"Error calculating watch streaks: {e}")

    @tasks.loop(seconds=300)
    async def update_streaks_display(self):
        """Update streaks display every 5 minutes"""
        if not await self._ensure_channel():
            return

        try:
            # Load existing streaks from file
            try:
                streaks = json.loads(STREAKS_FILE.read_text())
            except:
                streaks = {}

            if streaks:
                await self._post_streaks_update(streaks)

        except Exception as e:
            logger.error(f"Error updating streaks display: {e}")

    async def _post_streaks_update(self, streaks: dict):
        """Post updated streaks to channel"""
        if not streaks:
            return

        # Refresh username cache
        await self._refresh_username_cache()

        sorted_current = sorted(streaks.items(), key=lambda x: x[1]['current'], reverse=True)
        sorted_longest = sorted(streaks.items(), key=lambda x: x[1]['longest'], reverse=True)

        embed = discord.Embed(
            title="🔥 Watch Streaks",
            description="",  # Will be set with timestamp below
            color=discord.Color.orange()
        )

        # Current streaks
        current_text = []
        rank = 1
        for plex_username, data in sorted_current:
            if data['current'] > 0 and plex_username and plex_username.strip():
                # Get Discord display name if linked
                display_name = self._get_display_name(plex_username)
                emoji = "🥇" if rank == 1 else "🥈" if rank == 2 else "🥉" if rank == 3 else f"{rank}."
                current_text.append(f"{emoji} **{display_name}**: {data['current']} days")
                rank += 1
                if rank > 5:
                    break

        if current_text:
            embed.add_field(
                name="Current Streaks",
                value=truncate_field("\n".join(current_text)),
                inline=False
            )

        # Longest streaks
        longest_text = []
        rank = 1
        for plex_username, data in sorted_longest:
            if data['longest'] > 0 and plex_username and plex_username.strip():
                # Get Discord display name if linked
                display_name = self._get_display_name(plex_username)
                emoji = "🏆" if rank == 1 else f"{rank}."
                longest_text.append(f"{emoji} **{display_name}**: {data['longest']} days")
                rank += 1
                if rank > 5:
                    break

        if longest_text:
            embed.add_field(
                name="Longest Streaks (All-Time)",
                value=truncate_field("\n".join(longest_text)),
                inline=False
            )

        # Update existing message only (never create new ones)
        if self.streaks_message_id:
            try:
                # Calculate timestamp with latency compensation
                next_update = int((datetime.now(timezone.utc) + timedelta(seconds=300 + TIMESTAMP_LATENCY_OFFSET)).timestamp())

                # Update description with timestamp
                embed.description = f"Keep your streak alive by watching something every day!\n\n*Next update <t:{next_update}:R>*"

                # Partial message: edits without the wasted GET (see update_now_watching).
                message = self.stats_channel.get_partial_message(self.streaks_message_id)
                await message.edit(embed=embed)
            except Exception as e:
                logger.error(f"Could not edit Streaks message {self.streaks_message_id}: {e}")
        else:
            logger.warning("No Streaks message ID configured. Set WATCH_STREAK_MESSAGE_ID in .env")

    @update_watch_streaks.before_loop
    async def before_update_watch_streaks(self):
        await self.bot.wait_until_ready()

    @update_streaks_display.before_loop
    async def before_update_streaks_display(self):
        await self.bot.wait_until_ready()
        # Wait 20 seconds - posts third (after Now Watching)
        import asyncio
        await asyncio.sleep(20)



async def setup(bot: commands.Bot):
    """Setup function for loading cog"""
    await bot.add_cog(WatchTrackingCog(bot, bot.services))
