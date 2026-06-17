from __future__ import annotations

import asyncio


class CredentialStore:
    def __init__(self, access_key: str = "", access_secret: str = "") -> None:
        self._access_key = access_key
        self._access_secret = access_secret
        self._lock = asyncio.Lock()

    async def get(self) -> tuple[str, str]:
        async with self._lock:
            return self._access_key, self._access_secret

    async def set(self, access_key: str, access_secret: str) -> None:
        async with self._lock:
            self._access_key = access_key
            self._access_secret = access_secret
