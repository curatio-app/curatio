"""A token-bucket pacer shared by every outbound API client.

Concurrency bounds how many requests are *in flight*; it does not bound the
*rate*. An ``asyncio.Semaphore(8)`` with no pacer fires as fast as responses
return, which is what tripped MDBList's rate limit after ~8,000 requests against
a 500,000/day quota -- 1.6% of it -- and ended a full-library enrichment at 2.4%
complete. TMDB enrichment already had this class; MDBList never got one.

Lives in ``app/`` rather than ``workers/`` so both layers can import it without
``app`` depending on ``workers``.
"""

from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """Admit at most ``rps`` calls per second across all waiters.

    ``rps <= 0`` disables pacing, which keeps it free to construct on paths that
    do not want it.
    """

    def __init__(self, rps: float):
        self._interval = 1.0 / rps if rps > 0 else 0.0
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if self._interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self._interval
