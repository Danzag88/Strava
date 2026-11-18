"""Pure helpers for activity summaries and segment efforts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone


def resource_id(record: object) -> str | None:
    """Decimal id string. Prefer id_str so values past 2^53 are not read as floats.

    Activity and athlete ids are 64-bit after 1 June 2027. A Python int is exact;
    a float is rejected because it has already lost integer precision.
    """
    if not isinstance(record, dict):
        return None
    text = record.get("id_str")
    if isinstance(text, str) and text.strip():
        return text.strip()
    value = record.get("id")
    if isinstance(value, bool) or isinstance(value, float):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def athlete_id(record: object) -> str | None:
    if not isinstance(record, dict):
        return None
    athlete = record.get("athlete")
    if isinstance(athlete, dict):
        found = resource_id(athlete)
        if found:
            return found
    return resource_id(record)


def ensure_id_strings(record: dict) -> dict:
    """Copy the record and fill a missing id_str from the exact integer id."""
    copy = dict(record)
    found = resource_id(copy)
    if found and not (isinstance(copy.get("id_str"), str) and copy["id_str"].strip()):
        copy["id_str"] = found
    athlete = copy.get("athlete")
    if isinstance(athlete, dict):
        athlete_copy = dict(athlete)
        found_athlete = resource_id(athlete_copy)
        if found_athlete and not (isinstance(athlete_copy.get("id_str"), str) and athlete_copy["id_str"].strip()):
            athlete_copy["id_str"] = found_athlete
        copy["athlete"] = athlete_copy
    return copy


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
    activity_id = resource_id(activity)
    if activity_id is None:
        return []
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
