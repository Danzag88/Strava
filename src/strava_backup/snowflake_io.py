"""Snowflake connection, token storage, queue, and raw-table merges."""

from __future__ import annotations

import base64
import json
from datetime import date, datetime
from typing import Any

import snowflake.connector
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    load_pem_private_key,
)

from strava_backup.auth import AccessToken
from strava_backup.config import Settings, repo_root
from strava_backup.endpoints import RAW_TABLES
from strava_backup.sql_text import sql_statements

ENTITY_KEY_COLUMNS = {
    "ACTIVITIES": ("ACTIVITY_ID", "ATHLETE_ID"),
    "ACTIVITY": ("ACTIVITY_ID", "ATHLETE_ID"),
    "ACTIVITY_LAPS": ("ACTIVITY_ID", "ATHLETE_ID"),
    "ACTIVITY_ZONES": ("ACTIVITY_ID", "ATHLETE_ID"),
    "ACTIVITY_COMMENTS": ("ACTIVITY_ID", "ATHLETE_ID"),
    "ACTIVITY_KUDOS": ("ACTIVITY_ID", "ATHLETE_ID"),
    "ACTIVITY_STREAMS": ("ACTIVITY_ID", "ATHLETE_ID"),
    "CLUBS": ("CLUB_ID", "ATHLETE_ID"),
    "CLUB": ("CLUB_ID", "ATHLETE_ID"),
    "GEAR": ("GEAR_ID", "ATHLETE_ID"),
    "ROUTES": ("ROUTE_ID", "ATHLETE_ID"),
    "ROUTE": ("ROUTE_ID", "ATHLETE_ID"),
    "ROUTE_STREAMS": ("ROUTE_ID", "ATHLETE_ID"),
    "ROUTE_GPX": ("ROUTE_ID", "ATHLETE_ID"),
    "ROUTE_TCX": ("ROUTE_ID", "ATHLETE_ID"),
    "STARRED_SEGMENTS": ("SEGMENT_ID", "ATHLETE_ID"),
    "SEGMENT": ("SEGMENT_ID", "ATHLETE_ID"),
    "SEGMENT_STREAMS": ("SEGMENT_ID", "ATHLETE_ID"),
    "SEGMENT_EFFORT": ("EFFORT_ID", "ATHLETE_ID"),
    "SEGMENT_EFFORT_STREAMS": ("EFFORT_ID", "ATHLETE_ID"),
}


def private_key_der(encoded: str) -> bytes:
    try:
        pem = base64.b64decode(encoded)
    except Exception as exc:
        raise SystemExit("SNOWFLAKE_PRIVATE_KEY_B64 is not valid base64") from exc
    if b"BEGIN" not in pem:
        raise SystemExit("SNOWFLAKE_PRIVATE_KEY_B64 did not decode to a PEM private key")
    try:
        key = load_pem_private_key(pem, password=None)
    except Exception as exc:
        raise SystemExit("Could not read the Snowflake private key. Use an unencrypted PKCS8 PEM.") from exc
    return key.private_bytes(encoding=Encoding.DER, format=PrivateFormat.PKCS8, encryption_algorithm=NoEncryption())


class SnowflakeStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.database = settings.snowflake_database
        self.conn = snowflake.connector.connect(
            account=settings.snowflake_account,
            user=settings.snowflake_user,
            private_key=private_key_der(settings.snowflake_private_key_b64),
            role=settings.snowflake_role,
            warehouse=settings.snowflake_warehouse,
            database=self.database,
            schema="OPS",
            autocommit=True,
            client_session_keep_alive=True,
        )

    def close(self) -> None:
        self.conn.close()

    def apply_project_ddl(self) -> None:
        root = repo_root()
        for name in ("001_ops.sql", "002_raw_tables.sql"):
            script = (root / "sql" / name).read_text()
            script = script.replace("STRAVA.", f"{self.database}.")
            for statement in sql_statements(script):
                self._execute(statement)

    def get_token(self) -> AccessToken | None:
        rows = self._fetchall(
            f"""
            SELECT ATHLETE_ID, REFRESH_TOKEN, ACCESS_TOKEN, EXPIRES_AT, SCOPE
            FROM {self.database}.OPS.OAUTH_TOKEN
            ORDER BY UPDATED_AT DESC
            LIMIT 1
            """
        )
        if not rows:
            return None
        row = rows[0]
        expires_at = row["EXPIRES_AT"]
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        if isinstance(expires_at, datetime):
            expires_at = expires_at.replace(tzinfo=None)
        return AccessToken(
            athlete_id=row["ATHLETE_ID"],
            access_token=row["ACCESS_TOKEN"] or "",
            refresh_token=row["REFRESH_TOKEN"],
            expires_at=expires_at,
            scope=row["SCOPE"] or "",
        )

    def upsert_token(self, token: AccessToken) -> None:
        if not token.athlete_id:
            raise RuntimeError("Cannot store an OAuth token without an athlete id")
        self._execute(
            f"""
            MERGE INTO {self.database}.OPS.OAUTH_TOKEN AS target
            USING (
              SELECT %s AS ATHLETE_ID, %s AS REFRESH_TOKEN, %s AS ACCESS_TOKEN,
                     TO_TIMESTAMP_NTZ(%s) AS EXPIRES_AT, %s AS SCOPE
            ) AS source
            ON target.ATHLETE_ID = source.ATHLETE_ID
            WHEN MATCHED THEN UPDATE SET
              REFRESH_TOKEN = source.REFRESH_TOKEN,
              ACCESS_TOKEN = source.ACCESS_TOKEN,
              EXPIRES_AT = source.EXPIRES_AT,
              SCOPE = source.SCOPE,
              UPDATED_AT = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
              (ATHLETE_ID, REFRESH_TOKEN, ACCESS_TOKEN, EXPIRES_AT, SCOPE, UPDATED_AT)
            VALUES
              (source.ATHLETE_ID, source.REFRESH_TOKEN, source.ACCESS_TOKEN,
               source.EXPIRES_AT, source.SCOPE, CURRENT_TIMESTAMP())
            """,
            (
                token.athlete_id,
                token.refresh_token,
                token.access_token,
                token.expires_at.strftime("%Y-%m-%d %H:%M:%S") if token.expires_at else None,
                token.scope,
            ),
        )

    def get_state(self, key: str) -> str | None:
        rows = self._fetchall(
            f"SELECT STATE_VALUE FROM {self.database}.OPS.SYNC_STATE WHERE STATE_KEY = %s",
            (key,),
        )
        if not rows:
            return None
        return rows[0]["STATE_VALUE"]

    def set_state(self, key: str, value: str) -> None:
        self._execute(
            f"""
            MERGE INTO {self.database}.OPS.SYNC_STATE AS target
            USING (SELECT %s AS STATE_KEY, %s AS STATE_VALUE) AS source
            ON target.STATE_KEY = source.STATE_KEY
            WHEN MATCHED THEN UPDATE SET
              STATE_VALUE = source.STATE_VALUE,
              UPDATED_AT = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (STATE_KEY, STATE_VALUE, UPDATED_AT)
            VALUES (source.STATE_KEY, source.STATE_VALUE, CURRENT_TIMESTAMP())
            """,
            (key, value),
        )

    def log_call(
        self,
        method: str,
        path: str,
        status_code: int,
        short_usage: int | None,
        daily_usage: int | None,
        error_message: str | None,
    ) -> None:
        self._execute(
            f"""
            INSERT INTO {self.database}.OPS.API_CALL_LOG
              (CALLED_AT, METHOD, PATH, STATUS_CODE, READ_USAGE_15M, READ_USAGE_DAY, ERROR_MESSAGE)
            VALUES (CURRENT_TIMESTAMP(), %s, %s, %s, %s, %s, %s)
            """,
            (method, path, status_code, short_usage, daily_usage, (error_message or "")[:1000] or None),
        )

    def enqueue(self, endpoint_key: str, resource_id: str, priority: int) -> None:
        self._execute(
            f"""
            MERGE INTO {self.database}.OPS.SYNC_QUEUE AS target
            USING (
              SELECT %s AS ENDPOINT_KEY, %s AS RESOURCE_ID, %s AS PRIORITY
            ) AS source
            ON target.ENDPOINT_KEY = source.ENDPOINT_KEY
               AND target.RESOURCE_ID = source.RESOURCE_ID
            WHEN MATCHED AND target.STATUS IN ('done', 'error', 'skipped') THEN UPDATE SET
              STATUS = 'pending',
              PRIORITY = source.PRIORITY,
              ATTEMPTS = 0,
              LAST_ERROR = NULL,
              UPDATED_AT = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
              (ENDPOINT_KEY, RESOURCE_ID, PRIORITY, STATUS, ATTEMPTS, ENQUEUED_AT, UPDATED_AT)
            VALUES
              (source.ENDPOINT_KEY, source.RESOURCE_ID, source.PRIORITY, 'pending', 0,
               CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP())
            """,
            (endpoint_key, resource_id, priority),
        )

    def next_queue_item(self) -> dict[str, Any] | None:
        rows = self._fetchall(
            f"""
            SELECT ENDPOINT_KEY, RESOURCE_ID, ATTEMPTS, PRIORITY
            FROM {self.database}.OPS.SYNC_QUEUE
            WHERE STATUS = 'pending' AND ATTEMPTS < 3
            ORDER BY PRIORITY, ENQUEUED_AT
            LIMIT 1
            """
        )
        return rows[0] if rows else None

    def mark_queue(self, endpoint_key: str, resource_id: str, status: str, error: str | None = None) -> None:
        if status == "pending":
            self._execute(
                f"""
                UPDATE {self.database}.OPS.SYNC_QUEUE
                SET STATUS = 'pending',
                    ATTEMPTS = ATTEMPTS + 1,
                    LAST_ERROR = %s,
                    UPDATED_AT = CURRENT_TIMESTAMP()
                WHERE ENDPOINT_KEY = %s AND RESOURCE_ID = %s
                """,
                ((error or "")[:1000] or None, endpoint_key, resource_id),
            )
            return
        self._execute(
            f"""
            UPDATE {self.database}.OPS.SYNC_QUEUE
            SET STATUS = %s,
                LAST_ERROR = %s,
                UPDATED_AT = CURRENT_TIMESTAMP()
            WHERE ENDPOINT_KEY = %s AND RESOURCE_ID = %s
            """,
            (status, (error or "")[:1000] or None, endpoint_key, resource_id),
        )

    def queue_counts(self) -> dict[str, int]:
        rows = self._fetchall(
            f"SELECT STATUS, COUNT(*) AS N FROM {self.database}.OPS.SYNC_QUEUE GROUP BY STATUS"
        )
        return {row["STATUS"]: int(row["N"]) for row in rows}

    def activity_hash(self, activity_id: str) -> str | None:
        rows = self._fetchall(
            f"SELECT SUMMARY_HASH FROM {self.database}.RAW.ACTIVITIES WHERE ACTIVITY_ID = %s",
            (activity_id,),
        )
        if not rows:
            return None
        return rows[0]["SUMMARY_HASH"]

    def merge_activity(
        self,
        activity_id: str,
        athlete_id: str,
        start_date: datetime | None,
        summary_hash_value: str,
        payload: dict,
    ) -> None:
        self._execute(
            f"""
            MERGE INTO {self.database}.RAW.ACTIVITIES AS target
            USING (
              SELECT %s AS ACTIVITY_ID, %s AS ATHLETE_ID, TO_TIMESTAMP_NTZ(%s) AS START_DATE,
                     %s AS SUMMARY_HASH, PARSE_JSON(%s) AS PAYLOAD
            ) AS source
            ON target.ACTIVITY_ID = source.ACTIVITY_ID
            WHEN MATCHED THEN UPDATE SET
              ATHLETE_ID = source.ATHLETE_ID,
              START_DATE = source.START_DATE,
              SUMMARY_HASH = source.SUMMARY_HASH,
              PAYLOAD = source.PAYLOAD,
              INGESTED_AT = CURRENT_TIMESTAMP(),
              DELETED_AT = NULL
            WHEN NOT MATCHED THEN INSERT
              (ACTIVITY_ID, ATHLETE_ID, START_DATE, SUMMARY_HASH, PAYLOAD, INGESTED_AT, DELETED_AT)
            VALUES
              (source.ACTIVITY_ID, source.ATHLETE_ID, source.START_DATE, source.SUMMARY_HASH,
               source.PAYLOAD, CURRENT_TIMESTAMP(), NULL)
            """,
            (
                activity_id,
                athlete_id,
                start_date.strftime("%Y-%m-%d %H:%M:%S") if start_date else None,
                summary_hash_value,
                _json(payload),
            ),
        )

    def merge_entity(self, table: str, resource_id: str, athlete_id: str | None, payload: Any) -> None:
        self._check_table(table)
        key_column, athlete_column = ENTITY_KEY_COLUMNS[table]
        self._execute(
            f"""
            MERGE INTO {self.database}.RAW.{table} AS target
            USING (
              SELECT %s AS {key_column}, %s AS {athlete_column}, PARSE_JSON(%s) AS PAYLOAD
            ) AS source
            ON target.{key_column} = source.{key_column}
            WHEN MATCHED THEN UPDATE SET
              {athlete_column} = source.{athlete_column},
              PAYLOAD = source.PAYLOAD,
              INGESTED_AT = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT ({key_column}, {athlete_column}, PAYLOAD, INGESTED_AT)
            VALUES (source.{key_column}, source.{athlete_column}, source.PAYLOAD, CURRENT_TIMESTAMP())
            """,
            (resource_id, athlete_id, _json(payload)),
        )

    def merge_effort(
        self,
        effort_id: str,
        activity_id: str,
        segment_id: str | None,
        athlete_id: str,
        payload: dict,
    ) -> None:
        self._execute(
            f"""
            MERGE INTO {self.database}.RAW.SEGMENT_EFFORTS AS target
            USING (
              SELECT %s AS EFFORT_ID, %s AS ACTIVITY_ID, %s AS SEGMENT_ID,
                     %s AS ATHLETE_ID, PARSE_JSON(%s) AS PAYLOAD
            ) AS source
            ON target.EFFORT_ID = source.EFFORT_ID
            WHEN MATCHED THEN UPDATE SET
              ACTIVITY_ID = source.ACTIVITY_ID,
              SEGMENT_ID = source.SEGMENT_ID,
              ATHLETE_ID = source.ATHLETE_ID,
              PAYLOAD = source.PAYLOAD,
              INGESTED_AT = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
              (EFFORT_ID, ACTIVITY_ID, SEGMENT_ID, ATHLETE_ID, PAYLOAD, INGESTED_AT)
            VALUES
              (source.EFFORT_ID, source.ACTIVITY_ID, source.SEGMENT_ID, source.ATHLETE_ID,
               source.PAYLOAD, CURRENT_TIMESTAMP())
            """,
            (effort_id, activity_id, segment_id, athlete_id, _json(payload)),
        )

    def merge_snapshot(self, table: str, athlete_id: str, snapshot_date: date, payload: Any) -> None:
        if table not in {"ATHLETE", "ATHLETE_STATS", "ATHLETE_ZONES"}:
            raise ValueError(f"Not a snapshot table: {table}")
        self._execute(
            f"""
            MERGE INTO {self.database}.RAW.{table} AS target
            USING (
              SELECT %s AS ATHLETE_ID, TO_DATE(%s) AS SNAPSHOT_DATE, PARSE_JSON(%s) AS PAYLOAD
            ) AS source
            ON target.ATHLETE_ID = source.ATHLETE_ID
               AND target.SNAPSHOT_DATE = source.SNAPSHOT_DATE
            WHEN MATCHED THEN UPDATE SET
              PAYLOAD = source.PAYLOAD,
              INGESTED_AT = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT (ATHLETE_ID, SNAPSHOT_DATE, PAYLOAD, INGESTED_AT)
            VALUES (source.ATHLETE_ID, source.SNAPSHOT_DATE, source.PAYLOAD, CURRENT_TIMESTAMP())
            """,
            (athlete_id, snapshot_date.isoformat(), _json(payload)),
        )

    def mark_missing_activities_deleted(self, seen_ids: list[str]) -> int:
        self._execute(f"CREATE OR REPLACE TEMPORARY TABLE {self.database}.OPS.SEEN_ACTIVITY_IDS (ACTIVITY_ID STRING)")
        for offset in range(0, len(seen_ids), 500):
            chunk = seen_ids[offset : offset + 500]
            placeholders = ", ".join(["(%s)"] * len(chunk))
            self._execute(
                f"INSERT INTO {self.database}.OPS.SEEN_ACTIVITY_IDS (ACTIVITY_ID) VALUES {placeholders}",
                tuple(chunk),
            )
        self._execute(
            f"""
            UPDATE {self.database}.RAW.ACTIVITIES
            SET DELETED_AT = CURRENT_TIMESTAMP()
            WHERE DELETED_AT IS NULL
              AND ACTIVITY_ID NOT IN (SELECT ACTIVITY_ID FROM {self.database}.OPS.SEEN_ACTIVITY_IDS)
            """
        )
        deleted = self._fetchall(
            f"""
            SELECT COUNT(*) AS N
            FROM {self.database}.RAW.ACTIVITIES
            WHERE DELETED_AT IS NOT NULL
              AND ACTIVITY_ID NOT IN (SELECT ACTIVITY_ID FROM {self.database}.OPS.SEEN_ACTIVITY_IDS)
            """
        )
        return int(deleted[0]["N"]) if deleted else 0

    def _check_table(self, table: str) -> None:
        if table not in RAW_TABLES or table not in ENTITY_KEY_COLUMNS:
            raise ValueError(f"Unknown raw table: {table}")

    def _execute(self, statement: str, params: tuple | None = None) -> None:
        with self.conn.cursor() as cursor:
            if params is None:
                cursor.execute(statement)
            else:
                cursor.execute(statement, params)

    def _fetchall(self, statement: str, params: tuple | None = None) -> list[dict[str, Any]]:
        with self.conn.cursor(snowflake.connector.DictCursor) as cursor:
            if params is None:
                cursor.execute(statement)
            else:
                cursor.execute(statement, params)
            return [{str(key).upper(): value for key, value in row.items()} for row in cursor.fetchall()]


def _json(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"), default=str)
