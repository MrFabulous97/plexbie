# path: plugins/rate_limit_monitor/cog.py
"""Monitor Discord API rate limits and request counts"""
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Dict, Deque

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.logging import get_logger
from core.services import BotServices

logger = get_logger(__name__)


class RateLimitMonitor(commands.Cog):
    """Monitor Discord API rate limits and track request patterns"""

    def __init__(self, bot: commands.Bot, services: BotServices):
        self.bot = bot
        self.services = services

        # Track requests per second (last 60 seconds)
        self.request_history: Deque[float] = deque(maxlen=60)

        # Track requests by route
        self.route_counts: Dict[str, int] = defaultdict(int)

        # Track rate limit hits
        self.rate_limit_hits: Deque[dict] = deque(maxlen=100)

        # Start monitoring
        self.log_stats.start()

        # Hook into discord.py's HTTP client
        self._setup_http_hooks()

    def _setup_http_hooks(self):
        """Hook into Discord HTTP client to track requests"""
        original_request = self.bot.http.request

        async def tracked_request(route, *args, **kwargs):
            # Record request time
            self.request_history.append(time.time())

            # Track by route
            route_key = f"{route.method} {route.path}"
            self.route_counts[route_key] += 1

            # Make the actual request
            try:
                response = await original_request(route, *args, **kwargs)
                return response
            except discord.HTTPException as e:
                if e.status == 429:  # Rate limited
                    self.rate_limit_hits.append({
                        'route': route_key,
                        'timestamp': datetime.now(timezone.utc).isoformat(),
                        'retry_after': getattr(e, 'retry_after', None)
                    })
                    logger.warning(f"Rate limited on {route_key}, retry after: {getattr(e, 'retry_after', 'unknown')}")
                raise

        self.bot.http.request = tracked_request

    def cog_unload(self):
        """Cleanup when unloading"""
        self.log_stats.cancel()

    def get_requests_per_second(self) -> float:
        """Calculate current requests per second (average over last 10 seconds)"""
        if not self.request_history:
            return 0.0

        now = time.time()
        recent_requests = [t for t in self.request_history if now - t <= 10]

        if not recent_requests:
            return 0.0

        return len(recent_requests) / 10.0

    @tasks.loop(minutes=5)
    async def log_stats(self):
        """Log API usage statistics every 5 minutes"""
        rps = self.get_requests_per_second()
        total_requests = sum(self.route_counts.values())

        logger.info(
            f"API Stats - RPS: {rps:.2f}, Total requests: {total_requests}, "
            f"Rate limit hits: {len(self.rate_limit_hits)}"
        )

        # Log top 10 most called routes
        if self.route_counts:
            top_routes = sorted(self.route_counts.items(), key=lambda x: x[1], reverse=True)[:10]
            logger.info(f"Top routes: {dict(top_routes)}")

    @log_stats.before_loop
    async def before_log_stats(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    """Setup function for loading cog"""
    await bot.add_cog(RateLimitMonitor(bot, bot.services))
