"""Per-user sliding-window rate limits (no Home Assistant imports).

Used where one household member's requests cost everyone something: outbound
fetches of store pages, the shared product-lookup allowance and LLM calls.
Requests without a user (automations, timers) are never limited.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from collections.abc import Callable

MAX_TRACKED_USERS = 256


class RateLimiter:
    """At most ``limit`` units per user within ``window`` seconds."""

    def __init__(self, limit: int, window: float, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.limit = int(limit)
        self.window = float(window)
        self._monotonic = monotonic
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def _recent(self, user_id: str) -> deque[float]:
        now = self._monotonic()
        hits = self._hits.setdefault(user_id, deque())
        self._hits.move_to_end(user_id)
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        while len(self._hits) > MAX_TRACKED_USERS:
            self._hits.popitem(last=False)
        return hits

    def allow(self, user_id: str | None, cost: int = 1) -> bool:
        """Record ``cost`` units for ``user_id`` and say whether they fit in the window."""
        if not user_id:
            return True
        hits = self._recent(user_id)
        cost = max(1, int(cost))
        if len(hits) + cost > self.limit:
            return False
        now = self._monotonic()
        hits.extend([now] * cost)
        return True

    def retry_after(self, user_id: str | None) -> int:
        """Seconds until one more unit fits (0 when it fits now)."""
        if not user_id:
            return 0
        hits = self._recent(user_id)
        if len(hits) < self.limit:
            return 0
        return max(1, int(self.window - (self._monotonic() - hits[0])) + 1)
