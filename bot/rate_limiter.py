"""Per-user rate limiting for Discord message processing."""

import time
import logging

logger = logging.getLogger("sophee.bot.rate_limiter")

DEFAULT_COOLDOWN_SECONDS = 3.0


class RateLimiter:
    """Simple per-user cooldown rate limiter."""

    def __init__(self, cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS):
        self.cooldown_seconds = cooldown_seconds
        self._last_used: dict[str, float] = {}

    def check(self, user_id: str) -> bool:
        """Returns True if the user is allowed to proceed, False if rate-limited."""
        now = time.monotonic()
        last = self._last_used.get(user_id, 0.0)
        if now - last < self.cooldown_seconds:
            remaining = self.cooldown_seconds - (now - last)
            logger.debug("Rate limited user %s (%.1fs remaining)", user_id, remaining)
            return False
        self._last_used[user_id] = now
        return True

    def remaining(self, user_id: str) -> float:
        """Returns seconds remaining in the cooldown, or 0 if not rate-limited."""
        now = time.monotonic()
        last = self._last_used.get(user_id, 0.0)
        remaining = self.cooldown_seconds - (now - last)
        return max(0.0, remaining)
