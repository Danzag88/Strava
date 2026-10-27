"""Decide whether a run lists every activity or only the recent window."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

LOOKBACK_DAYS = 14
FULL_SWEEP_DAYS = 7


def sweep_due(last_sweep: str | None, now: datetime) -> bool:
    if not last_sweep:
        return True
    try:
        moment = datetime.fromisoformat(last_sweep)
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now - moment >= timedelta(days=FULL_SWEEP_DAYS)


def activity_list_params(
    initial_complete: bool,
    last_sweep: str | None,
    now: datetime,
) -> tuple[dict[str, int], bool]:
    full = (not initial_complete) or sweep_due(last_sweep, now)
    if full:
        return {}, True
    after = int((now - timedelta(days=LOOKBACK_DAYS)).timestamp())
    return {"after": after}, False
