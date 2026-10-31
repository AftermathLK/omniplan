"""Product lookups for unknown barcodes on the public Open Food Facts database.

Only the barcode digits leave Home Assistant (no account, no token, nothing
about the household). Requests time out after four seconds, refuse redirects
and read at most 512 KB, and the lookup can be switched off in the card's
settings. Answers are cached so a code scanned twice is only asked about once.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

import aiohttp

from .barcodes import lookup_gtin, product_summary
from .const import OPENFOODFACTS_TIMEOUT_SECONDS, OPENFOODFACTS_URL, VERSION

FIELDS = "product_name,product_name_en,generic_name,abbreviated_product_name,brands,quantity,categories_tags"
MAX_RESPONSE_BYTES = 512 * 1024
CACHE_ENTRIES = 500
FOUND_SECONDS = 24 * 3600
MISSING_SECONDS = 3600
USER_AGENT = f"TaskDragon/{VERSION} (Home Assistant integration; https://github.com/AftermathLK/taskdragon)"


class LookupFailed(Exception):
    """Open Food Facts could not be reached or answered with an error."""


class OpenFoodFactsClient:
    def __init__(self, session: aiohttp.ClientSession, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._session = session
        self._monotonic = monotonic
        self._cache: OrderedDict[str, tuple[dict[str, Any] | None, float]] = OrderedDict()

    async def lookup(self, code: str) -> dict[str, Any] | None:
        """Product summary for a retail barcode, None when the database has no such product."""
        gtin = lookup_gtin(code)
        if gtin is None:
            return None
        cached = self._cache.get(gtin)
        if cached is not None:
            summary, stored = cached
            if self._monotonic() - stored < (FOUND_SECONDS if summary else MISSING_SECONDS):
                self._cache.move_to_end(gtin)
                return summary
        summary = await self._fetch(gtin)
        self._cache[gtin] = (summary, self._monotonic())
        while len(self._cache) > CACHE_ENTRIES:
            self._cache.popitem(last=False)
        return summary

    async def _fetch(self, gtin: str) -> dict[str, Any] | None:
        url = OPENFOODFACTS_URL.format(code=gtin)
        timeout = aiohttp.ClientTimeout(total=OPENFOODFACTS_TIMEOUT_SECONDS)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        try:
            async with self._session.get(url, params={"fields": FIELDS}, headers=headers, timeout=timeout, allow_redirects=False) as response:
                if response.status == 404:
                    return None
                if response.status != 200:
                    raise LookupFailed(f"Open Food Facts answered {response.status}")
                body = await response.content.read(MAX_RESPONSE_BYTES + 1)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise LookupFailed("Open Food Facts could not be reached") from err
        if len(body) > MAX_RESPONSE_BYTES:
            raise LookupFailed("Open Food Facts sent too much data")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as err:
            raise LookupFailed("Open Food Facts sent something that is not JSON") from err
        return product_summary(payload)
