"""Rate limit simple en fenêtre glissante (en mémoire, réinitialisé au redémarrage)."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable


class SlidingWindowRateLimiter:
    def __init__(self, max_events: int, window_seconds: float = 3600.0, clock: Callable[[], float] = time.monotonic):
        self.max_events = max_events
        self.window = window_seconds
        self._clock = clock
        self._events: deque[float] = deque()

    def _purge(self, now: float) -> None:
        while self._events and now - self._events[0] >= self.window:
            self._events.popleft()

    def try_acquire(self) -> tuple[bool, float]:
        """Consomme un jeton. Renvoie (autorisé, secondes avant le prochain jeton libre)."""
        now = self._clock()
        self._purge(now)
        if len(self._events) >= self.max_events:
            return False, self.window - (now - self._events[0])
        self._events.append(now)
        return True, 0.0

    def refund(self) -> None:
        """Rend le dernier jeton (ex. : la requête a échoué avant de coûter quoi que ce soit)."""
        if self._events:
            self._events.pop()

    @property
    def remaining(self) -> int:
        self._purge(self._clock())
        return max(0, self.max_events - len(self._events))
