"""Call-to-table map for the Strava API v3 read endpoints this backup stores.

GET /segments/explore and the write endpoints are intentionally absent.
SEGMENT_EFFORT and SEGMENT_EFFORT_STREAMS tables exist, but the daily job does
not call those endpoints. Segment efforts are copied out of the activity detail
payload so the backfill stays inside the daily read cap.
"""

from __future__ import annotations

from dataclasses import dataclass

ACTIVITY_STREAM_KEYS = (
    "time,distance,latlng,altitude,velocity_smooth,heartrate,"
    "cadence,watts,temp,moving,grade_smooth"
)
SEGMENT_STREAM_KEYS = "distance,latlng,altitude"

# Lower numbers are drained first.
PRIORITY_CATALOG = 10
PRIORITY_ACTIVITY_DETAIL = 20
PRIORITY_ACTIVITY_CHILD = 30
PRIORITY_ACTIVITY_STREAMS = 40
PRIORITY_SEGMENT = 50
PRIORITY_SEGMENT_STREAMS = 60


@dataclass(frozen=True)
class QueuedEndpoint:
    key: str
    table: str
    key_column: str
    priority: int


QUEUED_ENDPOINTS: dict[str, QueuedEndpoint] = {
    "club": QueuedEndpoint("club", "CLUB", "CLUB_ID", PRIORITY_CATALOG),
    "gear": QueuedEndpoint("gear", "GEAR", "GEAR_ID", PRIORITY_CATALOG),
    "route": QueuedEndpoint("route", "ROUTE", "ROUTE_ID", PRIORITY_CATALOG),
    "route_streams": QueuedEndpoint("route_streams", "ROUTE_STREAMS", "ROUTE_ID", PRIORITY_CATALOG),
    "route_gpx": QueuedEndpoint("route_gpx", "ROUTE_GPX", "ROUTE_ID", PRIORITY_CATALOG),
    "route_tcx": QueuedEndpoint("route_tcx", "ROUTE_TCX", "ROUTE_ID", PRIORITY_CATALOG),
    "activity_detail": QueuedEndpoint("activity_detail", "ACTIVITY", "ACTIVITY_ID", PRIORITY_ACTIVITY_DETAIL),
    "activity_laps": QueuedEndpoint("activity_laps", "ACTIVITY_LAPS", "ACTIVITY_ID", PRIORITY_ACTIVITY_CHILD),
    "activity_zones": QueuedEndpoint("activity_zones", "ACTIVITY_ZONES", "ACTIVITY_ID", PRIORITY_ACTIVITY_CHILD),
    "activity_comments": QueuedEndpoint(
        "activity_comments", "ACTIVITY_COMMENTS", "ACTIVITY_ID", PRIORITY_ACTIVITY_CHILD
    ),
    "activity_kudos": QueuedEndpoint("activity_kudos", "ACTIVITY_KUDOS", "ACTIVITY_ID", PRIORITY_ACTIVITY_CHILD),
    "activity_streams": QueuedEndpoint(
        "activity_streams", "ACTIVITY_STREAMS", "ACTIVITY_ID", PRIORITY_ACTIVITY_STREAMS
    ),
    "segment": QueuedEndpoint("segment", "SEGMENT", "SEGMENT_ID", PRIORITY_SEGMENT),
    "segment_streams": QueuedEndpoint(
        "segment_streams", "SEGMENT_STREAMS", "SEGMENT_ID", PRIORITY_SEGMENT_STREAMS
    ),
}

ACTIVITY_CHILD_KEYS = (
    "activity_detail",
    "activity_laps",
    "activity_zones",
    "activity_comments",
    "activity_kudos",
    "activity_streams",
)

ROUTE_CHILD_KEYS = ("route", "route_streams", "route_gpx", "route_tcx")
SEGMENT_CHILD_KEYS = ("segment", "segment_streams")

RAW_TABLES = (
    "ATHLETE",
    "ATHLETE_STATS",
    "ATHLETE_ZONES",
    "ACTIVITIES",
    "ACTIVITY",
    "ACTIVITY_LAPS",
    "ACTIVITY_ZONES",
    "ACTIVITY_COMMENTS",
    "ACTIVITY_KUDOS",
    "ACTIVITY_STREAMS",
    "CLUBS",
    "CLUB",
    "GEAR",
    "ROUTES",
    "ROUTE",
    "ROUTE_STREAMS",
    "ROUTE_GPX",
    "ROUTE_TCX",
    "STARRED_SEGMENTS",
    "SEGMENT",
    "SEGMENT_STREAMS",
    "SEGMENT_EFFORTS",
    "SEGMENT_EFFORT",
    "SEGMENT_EFFORT_STREAMS",
)
