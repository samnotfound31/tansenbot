"""
bot.utils.cache
Simple async-safe in-memory TTL cache.
"""

import time
import asyncio
from typing import Any, Optional

DEFAULT_TTL = 3600  # 1 hour


class TTLCache:
    def __init__(self, default_ttl: int = DEFAULT_TTL):
        self._store: dict[str, tuple[Any, float]] = {}
        self._lock = asyncio.Lock()
        self.default_ttl = default_ttl

    def get(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if not entry:
            return None
        val, expiry = entry
        if time.time() > expiry:
            self._store.pop(key, None)
            return None
        return val

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        self._store[key] = (value, time.time() + (ttl or self.default_ttl))

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()

    async def aget(self, key: str) -> Optional[Any]:
        async with self._lock:
            return self.get(key)

    async def aset(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        async with self._lock:
            self.set(key, value, ttl)

    def cleanup_expired(self) -> int:
        now = time.time()
        expired = [k for k, (_, exp) in self._store.items() if now > exp]
        for k in expired:
            del self._store[k]
        return len(expired)


search_cache = TTLCache(default_ttl=1800)   # 30 min
lyrics_cache = TTLCache(default_ttl=3600)   # 1 hour
