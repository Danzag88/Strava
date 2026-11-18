import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from strava_backup.activities_job import in_weekly_window
from strava_backup.auth import build_authorize_url, extract_code, scopes_from_redirect
from strava_backup.config import as_bool, upsert_env_values
from strava_backup.endpoints import QUEUED_ENDPOINTS, RAW_TABLES
from strava_backup.rate_limit import parse_rate_limit
from strava_backup.schedule import activity_list_params, sweep_due
from strava_backup.scopes import ScopeError, assert_required_scopes, parse_scopes
from strava_backup.snowflake_io import is_programmatic_access_token
from strava_backup.sql_text import sql_statements
from strava_backup.transforms import (
    effort_rows,
    ensure_id_strings,
    gear_ids,
    parse_start_date,
    resource_id,
    summary_hash,
)


ROOT = Path(__file__).resolve().parents[1]


class ScheduleTests(unittest.TestCase):
    def test_sunday_at_17_utc_is_the_weekly_window(self):
        self.assertTrue(in_weekly_window(datetime(2026, 11, 1, 17, 0, tzinfo=timezone.utc)))
        self.assertFalse(in_weekly_window(datetime(2026, 11, 1, 16, 59, tzinfo=timezone.utc)))
        self.assertFalse(in_weekly_window(datetime(2026, 11, 2, 17, 0, tzinfo=timezone.utc)))

    def test_british_summer_time_still_uses_17_utc(self):
        london = datetime(2026, 7, 5, 18, 0, tzinfo=ZoneInfo("Europe/London"))
        self.assertTrue(in_weekly_window(london))


class AuthModeTests(unittest.TestCase):
    def test_jwt_value_is_a_programmatic_access_token(self):
        token = "eyJhbGciOiJFUzI1NiJ9.eyJpc3MiOiJTRjozMDAyIn0.c2ln"
        self.assertTrue(is_programmatic_access_token(token))

    def test_private_key_material_is_not_a_pat(self):
        self.assertFalse(is_programmatic_access_token("LS0tLS1CRUdJTiBQUklWQVRFIEtFWS0tLS0t"))


class ScopeTests(unittest.TestCase):
    def test_read_only_token_is_rejected(self):
        with self.assertRaises(ScopeError):
            assert_required_scopes("read")

    def test_read_all_alone_is_rejected(self):
        with self.assertRaises(ScopeError):
            assert_required_scopes("read_all")

    def test_activity_scope_does_not_satisfy_read_all(self):
        with self.assertRaises(ScopeError):
            assert_required_scopes("read,activity:read_all,profile:read_all")

    def test_full_read_grant_is_accepted(self):
        granted = assert_required_scopes("read,read_all,profile:read_all,activity:read_all")
        self.assertIn("read_all", granted)
        self.assertIn("activity:read_all", granted)
        self.assertIn("profile:read_all", granted)

    def test_spaces_and_lists(self):
        self.assertEqual(parse_scopes("read, read_all"), {"read", "read_all"})
        self.assertEqual(
            parse_scopes("activity:read_all profile:read_all read read_all"),
            {"activity:read_all", "profile:read_all", "read", "read_all"},
        )
        self.assertEqual(parse_scopes(["read_all", "profile:read_all"]), {"read_all", "profile:read_all"})


class AuthorizeUrlTests(unittest.TestCase):
    def test_url_requests_maximum_read_access(self):
        url = build_authorize_url(
            client_id=289844,
            redirect_uri="http://localhost",
            scopes=("read", "read_all", "profile:read_all", "activity:read_all"),
            authorize_url="https://www.strava.com/oauth/authorize",
        )
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        self.assertEqual(query["client_id"], ["289844"])
        self.assertEqual(query["redirect_uri"], ["http://localhost"])
        self.assertEqual(query["approval_prompt"], ["force"])
        self.assertEqual(
            query["scope"],
            ["read,read_all,profile:read_all,activity:read_all"],
        )

    def test_extract_code_from_redirect(self):
        pasted = "http://localhost/?state=&code=abc123&scope=read,read_all,profile:read_all,activity:read_all"
        self.assertEqual(extract_code(pasted), "abc123")
        self.assertIn("read_all", scopes_from_redirect(pasted))

    def test_denied_redirect_raises(self):
        with self.assertRaises(ValueError):
            extract_code("http://localhost/?error=access_denied")


class RateLimitTests(unittest.TestCase):
    def test_read_headers_win(self):
        limits = parse_rate_limit(
            {
                "X-RateLimit-Usage": "1,2",
                "X-RateLimit-Limit": "200,2000",
                "X-ReadRateLimit-Usage": "90,400",
                "X-ReadRateLimit-Limit": "100,1000",
            }
        )
        self.assertEqual(limits.short_usage, 90)
        self.assertEqual(limits.daily_usage, 400)
        self.assertEqual(limits.short_limit, 100)
        self.assertEqual(limits.daily_limit, 1000)


class ScheduleTests(unittest.TestCase):
    def test_first_run_lists_everything(self):
        now = datetime(2026, 10, 27, tzinfo=timezone.utc)
        params, full = activity_list_params(False, None, now)
        self.assertEqual(params, {})
        self.assertTrue(full)

    def test_recent_window_between_sweeps(self):
        now = datetime(2026, 10, 27, tzinfo=timezone.utc)
        last = (now - timedelta(days=1)).isoformat()
        params, full = activity_list_params(True, last, now)
        self.assertFalse(full)
        self.assertEqual(params["after"], int((now - timedelta(days=14)).timestamp()))

    def test_weekly_sweep_is_due(self):
        now = datetime(2026, 10, 27, tzinfo=timezone.utc)
        last = (now - timedelta(days=8)).isoformat()
        self.assertTrue(sweep_due(last, now))
        params, full = activity_list_params(True, last, now)
        self.assertTrue(full)
        self.assertEqual(params, {})


class TransformTests(unittest.TestCase):
    def test_summary_hash_is_stable(self):
        payload = {"id": 2, "name": "Ride"}
        self.assertEqual(summary_hash(payload), summary_hash({"name": "Ride", "id": 2}))

    def test_gear_and_efforts(self):
        athlete = {"bikes": [{"id": "b1"}], "shoes": [{"id": "g2"}, {}]}
        self.assertEqual(gear_ids(athlete), ["b1", "g2"])
        activity = {
            "id": 9,
            "segment_efforts": [
                {"id": 5, "segment": {"id": 7}, "name": "Hill"},
                {"name": "ignored"},
            ],
        }
        rows = effort_rows(activity)
        self.assertEqual(rows[0]["effort_id"], "5")
        self.assertEqual(rows[0]["segment_id"], "7")
        self.assertEqual(rows[0]["activity_id"], "9")
        self.assertEqual(len(rows), 1)

    def test_id_str_is_preferred_for_64_bit_ids(self):
        large = 2**53 + 3
        self.assertEqual(resource_id({"id": large, "id_str": str(large)}), str(large))
        self.assertEqual(resource_id({"id": large}), str(large))
        self.assertIsNone(resource_id({"id": float(large)}))
        copied = ensure_id_strings({"id": large, "athlete": {"id": 1176895}})
        self.assertEqual(copied["id_str"], str(large))
        self.assertEqual(copied["athlete"]["id_str"], "1176895")
        self.assertEqual(copied["id"], large)

    def test_start_date_is_utc_naive(self):
        parsed = parse_start_date("2018-05-02T12:15:09Z")
        self.assertEqual(parsed, datetime(2018, 5, 2, 12, 15, 9))
        self.assertIsNone(parsed.tzinfo)


class SqlTests(unittest.TestCase):
    def test_every_read_table_is_created(self):
        script = (ROOT / "sql" / "002_raw_tables.sql").read_text()
        for table in RAW_TABLES:
            self.assertIn(f"STRAVA.RAW.{table} (", script)

    def test_comments_are_not_executed(self):
        statements = sql_statements("-- comment\nCREATE TABLE T (ID STRING);\n")
        self.assertEqual(statements, ["CREATE TABLE T (ID STRING)"])

    def test_queue_tables_exist(self):
        for endpoint in QUEUED_ENDPOINTS.values():
            self.assertIn(endpoint.table, RAW_TABLES)

    def test_activities_endpoint_table(self):
        script = (ROOT / "sql" / "003_activities.sql").read_text()
        statements = sql_statements(script)
        create = next(statement for statement in statements if statement.upper().startswith("CREATE TABLE"))
        for column in (
            "ACTIVITY_ID",
            "NAME",
            "DISTANCE",
            "SPORT_TYPE",
            "START_LATITUDE",
            "MAP_SUMMARY_POLYLINE",
            "SUFFER_SCORE",
        ):
            self.assertIn(column, create)
        self.assertIn("TRY_TO_NUMBER(PAYLOAD:id_str::VARCHAR, 38, 0)", script)
        self.assertNotIn("PAYLOAD:id::NUMBER", script)
        self.assertNotIn("PAYLOAD:athlete:id::NUMBER", script)

    def test_ops_tables(self):
        script = (ROOT / "sql" / "001_ops.sql").read_text()
        for name in ("OAUTH_TOKEN", "SYNC_STATE", "SYNC_QUEUE", "API_CALL_LOG"):
            self.assertIn(name, script)


class EnvTests(unittest.TestCase):
    def test_bool(self):
        self.assertTrue(as_bool("true"))
        self.assertFalse(as_bool("false"))
        self.assertFalse(as_bool(None))

    def test_upsert_replaces_one_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("STRAVA_CLIENT_SECRET=abc\nSTRAVA_REFRESH_TOKEN=old\n")
            upsert_env_values({"STRAVA_REFRESH_TOKEN": "new", "STRAVA_GRANTED_SCOPE": "read_all"}, path)
            text = path.read_text()
            self.assertIn("STRAVA_CLIENT_SECRET=abc", text)
            self.assertIn("STRAVA_REFRESH_TOKEN=new", text)
            self.assertIn("STRAVA_GRANTED_SCOPE=read_all", text)
            self.assertNotIn("old", text)


class ClientTests(unittest.TestCase):
    def test_pagination_stops_on_short_page(self):
        from strava_backup import client as client_module
        from strava_backup.client import StravaClient

        original = client_module.PAGE_SIZE
        client_module.PAGE_SIZE = 2
        try:
            pages = [
                _response([{"id": 1}, {"id": 2}], usage="1,1"),
                _response([{"id": 3}], usage="2,2"),
            ]
            session = _Session(pages)
            client = StravaClient(
                "token",
                "https://www.strava.com/api/v3",
                session=session,
                sleep=lambda _seconds: None,
            )
            rows = list(client.get_pages("/athlete/activities", {}))
        finally:
            client_module.PAGE_SIZE = original
        self.assertEqual([row["id"] for row in rows], [1, 2, 3])
        self.assertEqual(session.calls[0]["per_page"], 2)
        self.assertEqual(session.calls[1]["page"], 2)

    def test_daily_budget_stops_before_another_call(self):
        from strava_backup.client import StravaClient
        from strava_backup.rate_limit import DailyBudgetExhausted

        session = _Session([_response({"id": 1}, usage="10,950")])
        sleeps = []
        client = StravaClient(
            "token",
            "https://www.strava.com/api/v3",
            session=session,
            sleep=sleeps.append,
            daily_budget=950,
        )
        self.assertEqual(client.get_json("/athlete")["id"], 1)
        with self.assertRaises(DailyBudgetExhausted):
            client.get_json("/athlete")
        self.assertEqual(len(session.calls), 1)


class _Response:
    def __init__(self, payload, headers, status_code=200):
        self._payload = payload
        self.headers = headers
        self.status_code = status_code
        self.text = json.dumps(payload)
        self.content = self.text.encode()

    def json(self):
        return self._payload


def _response(payload, usage):
    return _Response(
        payload,
        {
            "X-ReadRateLimit-Usage": usage,
            "X-ReadRateLimit-Limit": "100,1000",
        },
    )


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, params=None, timeout=None):
        self.calls.append(params)
        return self.responses.pop(0)


if __name__ == "__main__":
    unittest.main()
