"""
A simple in-memory, per-user rate limiter.
"""

import time

from osg.jupyterhub_code_service import config as config_module

__all__ = [
    "Limiter",
]


class Limiter:  # pylint: disable=too-few-public-methods
    """
    Rate-limits code submissions per user with a token bucket.

    Each user has a bucket that holds up to `_capacity` tokens (the
    burst) and refills at `_refill_rate` tokens per second.  Every
    attempt consumes one token via `try_acquire`; when the bucket is
    empty the attempt is refused.  This lets a user make a short burst of
    quick attempts (e.g. a few typos in a row) while capping the
    sustained rate to one attempt per refill interval.

    Tokens refill lazily: each call recomputes the balance from the time
    elapsed since the user's last attempt, so no background process is
    needed.  A bucket that has refilled to capacity is indistinguishable
    from a fresh user, so such entries are evicted lazily to bound
    memory.

    The two knobs are read from `CODE_SERVICE_BURST` (the capacity) and
    `CODE_SERVICE_REFILL_SECONDS` (the seconds to regain one token).

    `try_acquire` is a single synchronous check-and-decrement with no
    intervening `await`, so on the service's single event loop
    concurrent submissions from one user each consume their own token
    rather than all slipping through a stale read.

    State is held in memory and is therefore per-process (per-replica).
    A single replica is the expected deployment; a multi-replica
    deployment should pin to one replica or use a shared store.
    """

    # Sweep `_state` for fully-refilled (evictable) entries only once
    # every this many attempts, so the O(n) scan is amortized rather
    # than paid on every submission.  Delaying a sweep only keeps a few
    # stale entries around longer; it never affects a token decision.
    _SWEEP_INTERVAL = 256

    def __init__(self) -> None:
        current = config_module.settings()
        self._capacity = float(current.burst)
        self._refill_rate = 1.0 / current.refill_seconds
        # Maps a username to (tokens, last), where `last` is the
        # monotonic time of the user's most recent attempt.
        self._state: dict[str, tuple[float, float]] = {}
        # Counts attempts to drive the periodic eviction sweep.
        self._acquire_count = 0

    def try_acquire(self, username: str) -> bool:
        """
        Consumes one token for the user, returning whether one was free.

        Returns `True` and charges a token when the user is within their
        rate; returns `False` without charging when the bucket is empty.
        """

        now = time.monotonic()
        self._acquire_count += 1
        if self._acquire_count % self._SWEEP_INTERVAL == 0:
            self._evict_full(now)

        tokens, last = self._state.get(username, (self._capacity, now))
        tokens = min(self._capacity, tokens + (now - last) * self._refill_rate)

        if tokens < 1.0:
            self._state[username] = (tokens, now)
            return False

        self._state[username] = (tokens - 1.0, now)
        return True

    def _evict_full(self, now: float) -> None:
        # A bucket refills fully after `_capacity / _refill_rate`
        # seconds; past that a user is indistinguishable from a fresh
        # one, so drop the entry.
        full_after = self._capacity / self._refill_rate
        stale = [u for u, (_, last) in self._state.items() if now - last >= full_after]
        for username in stale:
            del self._state[username]
