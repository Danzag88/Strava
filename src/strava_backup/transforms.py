"""Pure helpers for activity summaries and segment efforts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone


def summary_hash(activity: dict) -> str:
    encoded = json.dumps(activity, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def gear_ids(athlete: dict) -> list[str]:
    found: list[str] = []
    for field in ("bikes", "shoes"):
        for item in athlete.get(field) or []:
            gear_id = item.get("id")
            if gear_id:
                found.append(str(gear_id))
    return found


def effort_rows(activity: dict) -> list[dict]:
    activity_id = str(activity["id"])
    rows: list[dict] = []
    for effort in activity.get("segment_efforts") or []:
        effort_id = effort.get("id")
        if not effort_id:
            continue
        segment = effort.get("segment") or {}
        segment_id = segment.get("id")
        rows.append(
            {
                "effort_id": str(effort_id),
                "activity_id": activity_id,
                "segment_id": str(segment_id) if segment_id else None,
                "payload": effort,
            }
        )
    return rows


def parse_start_date(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed
