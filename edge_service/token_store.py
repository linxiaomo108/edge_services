from __future__ import annotations

import asyncio


class TokenStore:
    def __init__(self, token: str = "") -> None:
        self._token = token
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        async with self._lock:
            return self._token

    async def set(self, token: str) -> None:
        async with self._lock:
            self._token = token

