# path: core/services.py
"""Dependency injection container for bot services"""
import time
from typing import Optional, Dict, Any, Tuple

import aiohttp
import redis.asyncio as aioredis
from plexapi.server import PlexServer
from sqlalchemy.ext.asyncio import AsyncSession

from core.blocking import run_blocking
from core.config import Config
from core.logging import get_logger
from core.security import redact
from database.session import get_session

logger = get_logger(__name__)


class _ServiceAPIProxy:
    """Compatibility wrapper for legacy plugin code using services.api.<svc>.get/post/delete"""

    def __init__(self, services: "BotServices"):
        self._services = services

    async def get(self, *args, **kwargs):
        if not self._services.http_session:
            raise RuntimeError("HTTP session not initialized")
        return self._services.http_session.get(*args, **kwargs)

    async def post(self, *args, **kwargs):
        if not self._services.http_session:
            raise RuntimeError("HTTP session not initialized")
        return self._services.http_session.post(*args, **kwargs)

    async def delete(self, *args, **kwargs):
        if not self._services.http_session:
            raise RuntimeError("HTTP session not initialized")
        return self._services.http_session.delete(*args, **kwargs)


class _LegacyAPIClients:
    """Expose legacy service names over the shared aiohttp session."""

    def __init__(self, services: "BotServices"):
        proxy = _ServiceAPIProxy(services)
        self.tautulli = proxy
        self.tmdb = proxy
        self.sonarr = proxy
        self.radarr = proxy
        self.overseerr = proxy


class BotServices:
    """Service container for dependency injection"""

    def __init__(self, config: Config):
        self.config = config
        self.http_session: Optional[aiohttp.ClientSession] = None
        self.redis_client: Optional[aioredis.Redis] = None
        self.plex_server: Optional[PlexServer] = None
        self.api = _LegacyAPIClients(self)

    async def initialize(self) -> None:
        """Initialize all services"""
        # HTTP session
        self.http_session = aiohttp.ClientSession()

        # Redis (optional)
        if self.config.redis_url:
            try:
                self.redis_client = aioredis.from_url(
                    self.config.redis_url,
                    decode_responses=True
                )
                await self.redis_client.ping()
                logger.info("   ✅ Redis: Connected (caching enabled)")
            except (aioredis.ConnectionError, aioredis.TimeoutError) as e:
                logger.warning(f"   ⚠️  Redis: Not connected - {e}")
                self.redis_client = None
            except Exception as e:
                logger.error(f"   ❌ Redis: Unexpected error - {e}", exc_info=True)
                self.redis_client = None

        # Plex server. PlexServer() performs an HTTP handshake, so keep it off
        # the event loop even during startup.
        if self.config.plex_url and self.config.plex_token:
            try:
                self.plex_server = await run_blocking(
                    PlexServer,
                    self.config.plex_url,
                    self.config.plex_token,
                )
                logger.info(f"   ✅ Plex: Connected to {redact(self.config.plex_url)}")
            except Exception as e:
                logger.warning(f"   ⚠️  Plex: Connection failed - {str(e)[:50]}")
                self.plex_server = None

        # Check for configured integrations
        integrations = []
        if self.config.overseerr_url and self.config.overseerr_token:
            integrations.append("Overseerr")
        if self.config.tautulli_url and self.config.tautulli_token:
            integrations.append("Tautulli")
        if self.config.tmdb_api_key:
            integrations.append("TMDB")

        if integrations:
            logger.info(f"   ✅ Integrations: {', '.join(integrations)}")

    async def reconnect_plex(self) -> bool:
        """Attempt to reconnect to Plex server. Returns True if successful.

        Async because PlexServer() is a blocking HTTP handshake and this is
        called from task loops that run as often as every 10 seconds - a failing
        reconnect would otherwise stall the loop for plexapi's 30s timeout on
        every tick.
        """
        if not self.config.plex_url or not self.config.plex_token:
            return False
        try:
            self.plex_server = await run_blocking(
                PlexServer,
                self.config.plex_url,
                self.config.plex_token,
            )
            logger.info(f"Plex: Reconnected to {redact(self.config.plex_url)}")
            return True
        except Exception as e:
            logger.debug(f"Plex reconnect failed: {str(e)[:80]}")
            self.plex_server = None
            return False

    async def cleanup(self) -> None:
        """Cleanup services on shutdown"""
        if self.http_session:
            await self.http_session.close()

        if self.redis_client:
            await self.redis_client.close()

    async def get_db_session(self) -> AsyncSession:
        """Get database session"""
        async with get_session() as session:
            return session

    def get_cache(self) -> "RedisCache | MemoryCache":
        """Get cache interface (Redis or memory)"""
        if self.redis_client:
            return RedisCache(self.redis_client)
        return MemoryCache()


class RedisCache:
    """Redis cache implementation"""

    def __init__(self, client: aioredis.Redis):
        self.client = client

    async def get(self, key: str) -> Optional[str]:
        return await self.client.get(key)

    async def set(self, key: str, value: str, ttl: int = 300) -> None:
        await self.client.set(key, value, ex=ttl)

    async def delete(self, key: str) -> None:
        await self.client.delete(key)


class MemoryCache:
    """Simple in-memory cache fallback with TTL support and max size eviction"""

    DEFAULT_MAX_SIZE = 1000

    def __init__(self, max_size: int = DEFAULT_MAX_SIZE):
        self._cache: Dict[str, Tuple[str, float]] = {}  # key -> (value, expiry_timestamp)
        self._max_size = max_size

    def _evict_expired(self) -> None:
        """Remove all expired entries"""
        now = time.time()
        expired_keys = [k for k, (_, expiry) in self._cache.items() if now > expiry]
        for key in expired_keys:
            del self._cache[key]

    def _evict_oldest(self) -> None:
        """Remove oldest entries if cache exceeds max size"""
        if len(self._cache) >= self._max_size:
            # Sort by expiry time and remove oldest 10%
            sorted_keys = sorted(self._cache.keys(), key=lambda k: self._cache[k][1])
            to_remove = max(1, len(sorted_keys) // 10)
            for key in sorted_keys[:to_remove]:
                del self._cache[key]

    async def get(self, key: str) -> Optional[str]:
        """Get value from cache, returning None if expired or missing"""
        if key not in self._cache:
            return None

        value, expiry = self._cache[key]
        if time.time() > expiry:
            del self._cache[key]
            return None

        return value

    async def set(self, key: str, value: str, ttl: int = 300) -> None:
        """Set value in cache with TTL (seconds)"""
        self._evict_expired()
        self._evict_oldest()

        expiry = time.time() + ttl
        self._cache[key] = (value, expiry)

    async def delete(self, key: str) -> None:
        """Delete key from cache"""
        self._cache.pop(key, None)

    def clear(self) -> None:
        """Clear all cache entries"""
        self._cache.clear()

    def __len__(self) -> int:
        """Return number of entries (including potentially expired)"""
        return len(self._cache)
