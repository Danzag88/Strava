"""Daily Strava backup entrypoint."""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Any

from strava_backup.auth import (
    AccessToken,
    TokenExchangeError,
    log_token_ready,
    refresh_access_token,
    resolve_scope,
    token_is_fresh,
)
from strava_backup.client import StravaClient, StravaError
from strava_backup.config import Settings, load_settings
from strava_backup.endpoints import (
    ACTIVITY_CHILD_KEYS,
    ACTIVITY_STREAM_KEYS,
    QUEUED_ENDPOINTS,
    ROUTE_CHILD_KEYS,
    SEGMENT_CHILD_KEYS,
    SEGMENT_STREAM_KEYS,
)
from strava_backup.rate_limit import DailyBudgetExhausted, RateLimit
from strava_backup.scopes import ScopeError, assert_required_scopes
from strava_backup.schedule import activity_list_params
from strava_backup.snowflake_io import SnowflakeStore
from strava_backup.transforms import effort_rows, gear_ids, parse_start_date, summary_hash

logger = logging.getLogger(__name__)

SKIP_STATUSES = {402, 403, 404}


def ensure_access_token(settings: Settings, store: SnowflakeStore) -> tuple[AccessToken, dict | None]:
    existing = store.get_token()
    if (
        not settings.reseed
        and existing
        and existing.access_token
        and token_is_fresh(existing)
    ):
        assert_required_scopes(existing.scope, settings.required_scopes)
        log_token_ready(existing, "stored")
        return existing, None

    if settings.reseed or existing is None:
        refresh_token = settings.refresh_token
        known_scope = settings.granted_scope
    else:
        refresh_token = existing.refresh_token
        known_scope = resolve_scope(existing.scope, settings.granted_scope)
    if not refresh_token:
        raise SystemExit(
            "No refresh token is available. Run python -m strava_backup.bootstrap, "
            "then set STRAVA_REFRESH_TOKEN."
        )

    token = refresh_access_token(settings, refresh_token, known_scope)
    athlete: dict | None = None
    if not token.athlete_id:
        if existing and existing.athlete_id and not settings.reseed:
            token.athlete_id = existing.athlete_id
        else:
            probe = StravaClient(token.access_token, settings.api_base)
            athlete = probe.get_json("/athlete")
            token.athlete_id = str(athlete["id"])
    store.upsert_token(token)
    log_token_ready(token, "refreshed")
    return token, athlete


def run_backup(settings: Settings, store: SnowflakeStore) -> None:
    token, athlete = ensure_access_token(settings, store)

    def refresh_callback() -> str:
        current = store.get_token()
        if current is None or not current.refresh_token:
            raise TokenExchangeError("Cannot refresh because no refresh token is stored")
        refreshed = refresh_access_token(
            settings,
            current.refresh_token,
            resolve_scope(current.scope, settings.granted_scope),
        )
        refreshed.athlete_id = refreshed.athlete_id or current.athlete_id
        if not refreshed.athlete_id:
            raise TokenExchangeError("Refresh succeeded without an athlete id")
        store.upsert_token(refreshed)
        log_token_ready(refreshed, "refreshed after 401")
        return refreshed.access_token

    def on_response(
        method: str,
        path: str,
        status: int,
        limits: RateLimit,
        error_message: str | None,
    ) -> None:
        try:
            store.log_call(method, path, status, limits.short_usage, limits.daily_usage, error_message)
        except Exception:
            logger.warning("Could not write API call log for %s", path, exc_info=True)
        if limits.daily_usage and limits.daily_usage % 25 == 0:
            logger.info("Read usage 15m=%s day=%s", limits.short_usage, limits.daily_usage)

    client = StravaClient(
        token.access_token,
        settings.api_base,
        on_response=on_response,
        refresh_access_token=refresh_callback,
    )
    if athlete is None:
        athlete = client.get_json("/athlete")
    athlete_id = str(athlete["id"])
    if token.athlete_id != athlete_id:
        token.athlete_id = athlete_id
        store.upsert_token(token)

    snapshot_date = datetime.now(timezone.utc).date()
    store.merge_snapshot("ATHLETE", athlete_id, snapshot_date, athlete)
    _catalog_get(client, store, "ATHLETE_STATS", athlete_id, snapshot_date, f"/athletes/{athlete_id}/stats")
    _catalog_get(client, store, "ATHLETE_ZONES", athlete_id, snapshot_date, "/athlete/zones")
    _enqueue_gear(store, athlete_id, athlete)
    _sync_collection(client, store, athlete_id, "/athlete/clubs", "CLUBS", ("club",))
    _sync_collection(
        client,
        store,
        athlete_id,
        f"/athletes/{athlete_id}/routes",
        "ROUTES",
        ROUTE_CHILD_KEYS,
    )
    _sync_collection(
        client,
        store,
        athlete_id,
        "/segments/starred",
        "STARRED_SEGMENTS",
        SEGMENT_CHILD_KEYS,
    )
    _sync_activities(client, store, athlete_id)
    _drain_queue(client, store, athlete_id)
    logger.info("Queue status: %s", store.queue_counts())


def _catalog_get(
    client: StravaClient,
    store: SnowflakeStore,
    table: str,
    athlete_id: str,
    snapshot_date: Any,
    path: str,
) -> None:
    try:
        payload = client.get_json(path)
    except DailyBudgetExhausted:
        raise
    except StravaError as exc:
        if exc.status == 429:
            raise DailyBudgetExhausted(str(exc)) from exc
        logger.warning("Skipping %s: %s", path, exc)
        return
    if payload is None:
        return
    store.merge_snapshot(table, athlete_id, snapshot_date, payload)


def _enqueue_gear(store: SnowflakeStore, athlete_id: str, athlete: dict) -> None:
    for gear_id in gear_ids(athlete):
        endpoint = QUEUED_ENDPOINTS["gear"]
        store.enqueue(endpoint.key, gear_id, endpoint.priority)
        logger.info("Queued gear %s for athlete %s", gear_id, athlete_id)


def _sync_collection(
    client: StravaClient,
    store: SnowflakeStore,
    athlete_id: str,
    path: str,
    table: str,
    child_keys: tuple[str, ...],
) -> None:
    try:
        rows = list(client.get_pages(path))
    except DailyBudgetExhausted:
        raise
    except StravaError as exc:
        if exc.status == 429:
            raise DailyBudgetExhausted(str(exc)) from exc
        logger.warning("Skipping %s: %s", path, exc)
        return
    for row in rows:
        resource_id = row.get("id")
        if resource_id is None:
            continue
        resource_id = str(resource_id)
        store.merge_entity(table, resource_id, athlete_id, row)
        for child_key in child_keys:
            endpoint = QUEUED_ENDPOINTS[child_key]
            store.enqueue(endpoint.key, resource_id, endpoint.priority)
    logger.info("Stored %s rows from %s", len(rows), path)


def _sync_activities(client: StravaClient, store: SnowflakeStore, athlete_id: str) -> None:
    now = datetime.now(timezone.utc)
    params, full = activity_list_params(
        store.get_state("initial_activity_list_complete") == "true",
        store.get_state("last_full_sweep_at"),
        now,
    )
    logger.info("Listing activities full_sweep=%s params=%s", full, params)
    seen: list[str] = []
    count = 0
    try:
        pages = client.get_pages("/athlete/activities", params)
        for activity in pages:
            activity_id = activity.get("id")
            if activity_id is None:
                continue
            activity_id = str(activity_id)
            digest = summary_hash(activity)
            previous = store.activity_hash(activity_id)
            store.merge_activity(
                activity_id,
                athlete_id,
                parse_start_date(activity.get("start_date")),
                digest,
                activity,
            )
            if previous != digest:
                for child_key in ACTIVITY_CHILD_KEYS:
                    endpoint = QUEUED_ENDPOINTS[child_key]
                    store.enqueue(endpoint.key, activity_id, endpoint.priority)
            if full:
                seen.append(activity_id)
            count += 1
    except StravaError as exc:
        if exc.status == 429:
            raise DailyBudgetExhausted(str(exc)) from exc
        raise
    logger.info("Stored %s activity summaries", count)
    if full:
        deleted = store.mark_missing_activities_deleted(seen)
        store.set_state("initial_activity_list_complete", "true")
        store.set_state("last_full_sweep_at", now.isoformat())
        logger.info("Full activity sweep marked %s missing activities deleted", deleted)


def _drain_queue(client: StravaClient, store: SnowflakeStore, athlete_id: str) -> None:
    while True:
        item = store.next_queue_item()
        if item is None:
            return
        endpoint_key = item["ENDPOINT_KEY"]
        resource_id = item["RESOURCE_ID"]
        try:
            _process_queue_item(client, store, athlete_id, endpoint_key, resource_id)
        except DailyBudgetExhausted:
            raise
        except StravaError as exc:
            if exc.status == 429:
                raise DailyBudgetExhausted(str(exc)) from exc
            if exc.status in SKIP_STATUSES:
                store.mark_queue(endpoint_key, resource_id, "skipped", str(exc))
                logger.info("Skipped %s %s: %s", endpoint_key, resource_id, exc)
                continue
            attempts = int(item["ATTEMPTS"]) + 1
            status = "error" if attempts >= 3 else "pending"
            store.mark_queue(endpoint_key, resource_id, status, str(exc))
            logger.warning("Failed %s %s (attempt %s): %s", endpoint_key, resource_id, attempts, exc)
            continue
        store.mark_queue(endpoint_key, resource_id, "done")
        logger.info("Loaded %s %s", endpoint_key, resource_id)


def _process_queue_item(
    client: StravaClient,
    store: SnowflakeStore,
    athlete_id: str,
    endpoint_key: str,
    resource_id: str,
) -> None:
    endpoint = QUEUED_ENDPOINTS[endpoint_key]
    if endpoint_key == "activity_detail":
        payload = client.get_json(f"/activities/{resource_id}", {"include_all_efforts": "true"})
        store.merge_entity(endpoint.table, resource_id, athlete_id, payload)
        _store_efforts(store, athlete_id, payload)
        return
    payload = _fetch_payload(client, endpoint_key, resource_id)
    store.merge_entity(endpoint.table, resource_id, athlete_id, payload)


def _fetch_payload(client: StravaClient, endpoint_key: str, resource_id: str) -> Any:
    if endpoint_key == "club":
        return client.get_json(f"/clubs/{resource_id}")
    if endpoint_key == "gear":
        return client.get_json(f"/gear/{resource_id}")
    if endpoint_key == "route":
        return client.get_json(f"/routes/{resource_id}")
    if endpoint_key == "route_streams":
        return client.get_json(f"/routes/{resource_id}/streams")
    if endpoint_key == "route_gpx":
        return {"content_type": "application/gpx+xml", "body": client.get_text(f"/routes/{resource_id}/export_gpx")}
    if endpoint_key == "route_tcx":
        return {
            "content_type": "application/vnd.garmin.tcx+xml",
            "body": client.get_text(f"/routes/{resource_id}/export_tcx"),
        }
    if endpoint_key == "activity_laps":
        return client.get_json(f"/activities/{resource_id}/laps")
    if endpoint_key == "activity_zones":
        return client.get_json(f"/activities/{resource_id}/zones")
    if endpoint_key == "activity_comments":
        return client.get_comments(resource_id)
    if endpoint_key == "activity_kudos":
        return client.get_kudos(resource_id)
    if endpoint_key == "activity_streams":
        return client.get_json(
            f"/activities/{resource_id}/streams",
            {"keys": ACTIVITY_STREAM_KEYS, "key_by_type": "true"},
        )
    if endpoint_key == "segment":
        return client.get_json(f"/segments/{resource_id}")
    if endpoint_key == "segment_streams":
        return client.get_json(
            f"/segments/{resource_id}/streams",
            {"keys": SEGMENT_STREAM_KEYS, "key_by_type": "true"},
        )
    raise RuntimeError(f"No fetcher for {endpoint_key}")


def _store_efforts(store: SnowflakeStore, athlete_id: str, activity: dict) -> None:
    for row in effort_rows(activity):
        store.merge_effort(
            row["effort_id"],
            row["activity_id"],
            row["segment_id"],
            athlete_id,
            row["payload"],
        )
        if not row["segment_id"]:
            continue
        for child_key in SEGMENT_CHILD_KEYS:
            endpoint = QUEUED_ENDPOINTS[child_key]
            store.enqueue(endpoint.key, row["segment_id"], endpoint.priority)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("snowflake.connector").setLevel(logging.WARNING)
    store: SnowflakeStore | None = None
    try:
        settings = load_settings(require_snowflake=True)
        logger.info(
            "Strava backup starting reseed=%s account=%s database=%s",
            settings.reseed,
            settings.snowflake_account,
            settings.snowflake_database,
        )
        store = SnowflakeStore(settings)
        store.apply_project_ddl()
        run_backup(settings, store)
    except DailyBudgetExhausted as exc:
        logger.info("Stopping until the next run: %s", exc)
        if store is not None:
            logger.info("Queue status: %s", store.queue_counts())
        return 0
    except (ScopeError, TokenExchangeError, StravaError, SystemExit) as exc:
        message = exc.code if isinstance(exc, SystemExit) and exc.code else exc
        logger.error("%s", message)
        return 1
    except Exception:
        logger.exception("Backup failed")
        return 1
    finally:
        if store is not None:
            store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
