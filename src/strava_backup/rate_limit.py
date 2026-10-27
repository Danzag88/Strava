"""Parse Strava rate-limit headers."""

from __future__ import annotations

from dataclasses import dataclass


class DailyBudgetExhausted(Exception):
    """Raised when the daily read cap leaves no safe headroom."""


@dataclass(frozen=True)
class RateLimit:
    short_usage: int | None = None
    daily_usage: int | None = None
    short_limit: int | None = None
    daily_limit: int | None = None


def _pair(header: str | None) -> tuple[int, int] | None:
    if not header:
        return None
    parts = [part.strip() for part in header.split(",")]
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def parse_rate_limit(headers: dict[str, str]) -> RateLimit:
    """Prefer the read-limit headers. Strava sends short-window usage, then daily usage."""
    lowered = {key.lower(): value for key, value in headers.items()}
    usage = _pair(lowered.get("x-readratelimit-usage")) or _pair(lowered.get("x-ratelimit-usage"))
    limit = _pair(lowered.get("x-readratelimit-limit")) or _pair(lowered.get("x-ratelimit-limit"))
    return RateLimit(
        short_usage=usage[0] if usage else None,
        daily_usage=usage[1] if usage else None,
        short_limit=limit[0] if limit else None,
        daily_limit=limit[1] if limit else None,
    )
