"""Weekly backup of Strava activities into STRAVA_DB.ACTIVITIES.STRAVA_ACTIVITIES.

OAuth consent is once. Each run uses the refresh token at
https://www.strava.com/oauth/token and writes a rotated refresh token back to .env.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

from strava_backup.auth import refresh_access_token
from strava_backup.client import StravaClient
from strava_backup.config import load_settings, upsert_env_values
from strava_backup.snowflake_io import open_connection
from strava_backup.transforms import ensure_id_strings, resource_id

logger = logging.getLogger(__name__)

TARGET = "STRAVA_DB.ACTIVITIES.STRAVA_ACTIVITIES"
BATCH_BYTES = 8_000_000


def in_weekly_window(moment: datetime) -> bool:
    """Sunday 17:00–17:59 UTC."""
    current = moment.astimezone(timezone.utc)
    return current.weekday() == 6 and current.hour == 17


def _use_warehouse(cursor, preferred: str) -> str:
    names = [preferred] if preferred == "COMPUTE_WH" else [preferred, "COMPUTE_WH"]
    last_error: Exception | None = None
    for name in names:
        try:
            cursor.execute(f"USE WAREHOUSE {name}")
            return name
        except Exception as exc:
            last_error = exc
            logger.info("Warehouse %s is not available", name)
    if last_error:
        raise last_error
    raise RuntimeError("No Snowflake warehouse is configured")


def _merge_batch(cursor, batch: list[dict]) -> None:
    body = json.dumps(batch, separators=(",", ":"), default=str)
    cursor.execute(
        f"""
        MERGE INTO {TARGET} AS target
        USING (
          SELECT
            TRY_TO_NUMBER(value:activity_id_str::VARCHAR, 38, 0) AS ACTIVITY_ID,
            value:activity_id_str::VARCHAR AS ACTIVITY_ID_STR,
            TRY_TO_TIMESTAMP_TZ(value:start_date::VARCHAR) AS START_DATE,
            value:raw AS RAW
          FROM TABLE(FLATTEN(INPUT => PARSE_JSON(%s)))
        ) AS source
        ON target.ACTIVITY_ID = source.ACTIVITY_ID
        WHEN MATCHED THEN UPDATE SET
          ACTIVITY_ID_STR = source.ACTIVITY_ID_STR,
          START_DATE = source.START_DATE,
          RAW = source.RAW,
          LOADED_AT = CURRENT_TIMESTAMP()
        WHEN NOT MATCHED THEN INSERT (ACTIVITY_ID, ACTIVITY_ID_STR, START_DATE, RAW)
          VALUES (source.ACTIVITY_ID, source.ACTIVITY_ID_STR, source.START_DATE, source.RAW)
        """,
        (body,),
    )


def backup_activities() -> int:
    if os.environ.get("STRAVA_BACKUP_SCHEDULED") == "1" and not in_weekly_window(datetime.now(timezone.utc)):
        logger.info("Skipping. The scheduled backup runs on Sunday at 17:00 UTC.")
        return 0

    settings = load_settings(require_snowflake=True)
    if not settings.refresh_token:
        raise SystemExit("STRAVA_REFRESH_TOKEN is empty. Approve Strava once with python -m strava_backup.bootstrap.")

    token = refresh_access_token(settings, settings.refresh_token, settings.granted_scope)
    updates: dict[str, str] = {}
    if token.refresh_token != settings.refresh_token:
        updates["STRAVA_REFRESH_TOKEN"] = token.refresh_token
    if token.scope and token.scope != (settings.granted_scope or ""):
        updates["STRAVA_GRANTED_SCOPE"] = token.scope
    if updates:
        upsert_env_values(updates)
        logger.info("Saved rotated Strava token fields: %s", ", ".join(sorted(updates)))
    logger.info("Athlete %s scope %s", token.athlete_id, token.scope)

    client = StravaClient(token.access_token, settings.api_base)
    activities: list[dict] = []
    seen: set[str] = set()
    for activity in client.get_pages("/athlete/activities"):
        activity_id = resource_id(activity)
        if activity_id is None or activity_id in seen:
            continue
        seen.add(activity_id)
        stored = ensure_id_strings(activity)
        activities.append(
            {
                "activity_id_str": activity_id,
                "start_date": stored.get("start_date"),
                "raw": stored,
            }
        )
    logger.info(
        "Collected %s activities. Read usage %s/%s this window, %s/%s today",
        len(activities),
        client.limits.short_usage,
        client.limits.short_limit,
        client.limits.daily_usage,
        client.limits.daily_limit,
    )

    connection = open_connection(settings, include_context=False)
    try:
        cursor = connection.cursor()
        warehouse = _use_warehouse(cursor, settings.snowflake_warehouse)
        cursor.execute(f"ALTER TABLE {TARGET} ADD COLUMN IF NOT EXISTS ACTIVITY_ID_STR VARCHAR")
        logger.info("Loading into %s with warehouse %s", TARGET, warehouse)
        batch: list[dict] = []
        batch_bytes = 2
        loaded = 0
        for activity in activities:
            encoded = json.dumps(activity, separators=(",", ":"), default=str)
            if batch and batch_bytes + len(encoded) > BATCH_BYTES:
                _merge_batch(cursor, batch)
                loaded += len(batch)
                batch = []
                batch_bytes = 2
            batch.append(activity)
            batch_bytes += len(encoded) + 1
        if batch:
            _merge_batch(cursor, batch)
            loaded += len(batch)
        cursor.execute(f"SELECT COUNT(*) FROM {TARGET}")
        stored = cursor.fetchone()[0]
        logger.info("Merged %s activities. Table now has %s rows.", loaded, stored)
    finally:
        connection.close()
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("snowflake.connector").setLevel(logging.WARNING)
    try:
        return backup_activities()
    except Exception as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
