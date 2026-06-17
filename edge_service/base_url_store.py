from __future__ import annotations

import asyncio


class BaseUrlStore:
    def __init__(self, base_url: str = "") -> None:
        self._base_url = base_url
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        async with self._lock:
            return self._base_url

    async def set(self, base_url: str) -> None:
        async with self._lock:
            self._base_url = base_url

