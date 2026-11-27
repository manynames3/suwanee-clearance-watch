"""Async token bucket rate limiter with jitter, adaptive slow-down and drip pacing."""

from __future__ import annotations

import asyncio
import random
import time


class TokenBucketRateLimiter:
    """Token bucket rate limiter with random jitter between requests.

    Three behaviours sit on top of the plain bucket:

    * **Adaptive rate.** `slow_down()` halves the effective rate (down to a
      floor) when the API pushes back; `note_success()` recovers it in small
      steps after a clean streak. The configured `rps` is a ceiling — recovery
      never exceeds it, so a limiter that has been told "slower" cannot talk
      itself back into the speed that drew the complaint.
    * **Drip pacing.** With `pace_min_s`/`pace_max_s` set, consecutive requests
      are at least a random 4–10 s apart. That is the default for a home
      connection: one request at a time, at a human-ish tempo.
    * **Idle pauses.** Every `idle_every` requests the limiter rests for a
      longer random pause, so a long run does not read as one unbroken stream.
    """

    def __init__(
        self,
        rps: float = 1.0,
        burst: int = 3,
        jitter_min_ms: int = 200,
        jitter_max_ms: int = 800,
        *,
        pace_min_s: float = 0.0,
        pace_max_s: float = 0.0,
        idle_every: int = 0,
        idle_min_s: float = 0.0,
        idle_max_s: float = 0.0,
        floor_fraction: float = 0.125,
    ) -> None:
        self._ceiling_rate = rps
        self._rate = rps
        self._floor_rate = rps * floor_fraction
        self._burst = burst
        self._jitter_min_ms = jitter_min_ms
        self._jitter_max_ms = jitter_max_ms
        self._pace_min_s = max(0.0, pace_min_s)
        self._pace_max_s = max(self._pace_min_s, pace_max_s)
        self._idle_every = max(0, idle_every)
        self._idle_min_s = max(0.0, idle_min_s)
        self._idle_max_s = max(self._idle_min_s, idle_max_s)
        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._last_request_at: float | None = None
        self._acquired = 0
        self._clean_streak = 0
        self._lock = asyncio.Lock()

    @property
    def effective_rps(self) -> float:
        """The rate currently in force, after any slow-down."""
        return self._rate

    @property
    def is_slowed(self) -> bool:
        return self._rate < self._ceiling_rate

    def slow_down(self, factor: float = 2.0) -> float:
        """Cut the rate by `factor`, not below the floor. Returns the new rate."""
        self._rate = max(self._floor_rate, self._rate / max(factor, 1.0))
        self._tokens = min(self._tokens, 0.0)  # no banked tokens spent at the old speed
        self._clean_streak = 0
        return self._rate

    def note_success(self, streak_needed: int = 10, step: float = 1.25) -> None:
        """Recover toward the configured ceiling after a clean streak."""
        if self._rate >= self._ceiling_rate:
            return
        self._clean_streak += 1
        if self._clean_streak >= streak_needed:
            self._rate = min(self._ceiling_rate, self._rate * step)
            self._clean_streak = 0

    async def acquire(self) -> None:
        """Wait until a token is available, then apply pacing and jitter."""
        async with self._lock:
            await self._wait_for_token()
            await self._pace()

        # Apply jitter after acquiring the token
        jitter_s = random.randint(self._jitter_min_ms, self._jitter_max_ms) / 1000.0
        await asyncio.sleep(jitter_s)

    async def _pace(self) -> None:
        self._acquired += 1
        if self._idle_every and self._acquired % self._idle_every == 0:
            await asyncio.sleep(random.uniform(self._idle_min_s, self._idle_max_s))
            self._last_request_at = time.monotonic()
            return
        if self._pace_max_s > 0 and self._last_request_at is not None:
            gap = random.uniform(self._pace_min_s, self._pace_max_s)
            wait = gap - (time.monotonic() - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
        self._last_request_at = time.monotonic()

    async def _wait_for_token(self) -> None:
        while True:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            # Calculate wait time until next token
            wait = (1.0 - self._tokens) / self._rate
            await asyncio.sleep(wait)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._last_refill = now
