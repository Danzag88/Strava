"""One-time Strava OAuth approval.

Run: python -m strava_backup.bootstrap
"""

from __future__ import annotations

import logging
import sys

from strava_backup.auth import (
    TokenExchangeError,
    build_authorize_url,
    exchange_authorization_code,
    extract_code,
    scopes_from_redirect,
)
from strava_backup.config import load_settings, upsert_env_values
from strava_backup.scopes import ScopeError, assert_required_scopes
from strava_backup.snowflake_io import SnowflakeStore


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("snowflake.connector").setLevel(logging.WARNING)
    try:
        settings = load_settings(require_snowflake=False)
        url = build_authorize_url(
            client_id=settings.client_id,
            redirect_uri=settings.redirect_uri,
            scopes=settings.scopes,
            authorize_url=settings.authorize_url,
        )
        print("Open this URL and approve every scope Strava shows:")
        print(url)
        print("The browser will fail to connect to localhost. Copy the full address bar anyway.")
        pasted = input("Redirect URL: ").strip()
        redirect_scopes = scopes_from_redirect(pasted)
        if redirect_scopes is not None:
            assert_required_scopes(redirect_scopes, settings.required_scopes)
        token = exchange_authorization_code(settings, extract_code(pasted))
        if not token.athlete_id:
            raise SystemExit("Strava did not return an athlete id")
        upsert_env_values(
            {
                "STRAVA_REFRESH_TOKEN": token.refresh_token,
                "STRAVA_GRANTED_SCOPE": token.scope,
            }
        )
        if settings.snowflake_configured:
            store = SnowflakeStore(settings)
            try:
                store.apply_project_ddl()
                store.upsert_token(token)
            finally:
                store.close()
            print("Saved the refresh token in .env and Snowflake.")
        else:
            print("Saved the refresh token in .env. Snowflake was not configured, so it was not updated.")
        print(f"Athlete {token.athlete_id}")
        print(f"Granted scope: {token.scope}")
        print(
            "Add STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN, and STRAVA_GRANTED_SCOPE "
            "to the GitHub Actions secrets before the daily job runs."
        )
    except (ScopeError, TokenExchangeError, ValueError, SystemExit) as exc:
        message = exc.code if isinstance(exc, SystemExit) and not isinstance(exc.code, int) else exc
        if isinstance(exc, SystemExit) and exc.code in (0, None):
            return 0
        if isinstance(exc, SystemExit) and isinstance(exc.code, int) and exc.code != 0:
            print(exc.code, file=sys.stderr)
            return int(exc.code)
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
