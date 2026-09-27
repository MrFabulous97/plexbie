# path: core/rate_limit.py
"""Rate limiting utilities"""
import asyncio
import time
from typing import Dict


class TokenBucket:
    """Token bucket rate limiter"""
    
    def __init__(self, capacity: int, refill_rate: float):
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens = capacity
        self.last_refill = time.time()
        self._lock = asyncio.Lock()
    
    async def acquire(self, tokens: int = 1) -> bool:
        """Try to acquire tokens, returns True if successful"""
        async with self._lock:
            # Refill bucket
            now = time.time()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_refill = now
            
            # Try to acquire
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False
    
    async def wait_and_acquire(self, tokens: int = 1):
        """Wait until tokens are available"""
        while not await self.acquire(tokens):
            await asyncio.sleep(0.1)


class RateLimitManager:
    """Manages rate limits for different services"""
    
    def __init__(self):
        self.limiters: Dict[str, TokenBucket] = {}
    
    def get_limiter(self, service: str) -> TokenBucket:
        """Get or create limiter for service"""
        if service not in self.limiters:
            # Default limits per service
            limits = {
                "plex": (30, 1),      # 30 requests per second
                "overseerr": (10, 0.5),  # 10 requests per 2 seconds
                "tautulli": (20, 1),     # 20 requests per second
                "sonarr": (10, 0.3),     # 10 requests per ~3 seconds
                "radarr": (10, 0.3),
                "lidarr": (10, 0.3),
                "discord": (5, 1),       # 5 requests per second
            }
            
            capacity, rate = limits.get(service, (10, 0.5))
            self.limiters[service] = TokenBucket(capacity, rate)
        
        return self.limiters[service]


# Global rate limit manager
rate_limiter = RateLimitManager()
