"""Strava OAuth authorization-code bootstrap and refresh-token grant."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from strava_backup.config import Settings
from strava_backup.scopes import ScopeError, assert_required_scopes, format_scopes, parse_scopes

logger = logging.getLogger(__name__)

REFRESH_SKEW = timedelta(minutes=5)


@dataclass
class AccessToken:
    athlete_id: str | None
    access_token: str
    refresh_token: str
    expires_at: datetime | None
    scope: str


class TokenExchangeError(Exception):
    """Raised when Strava rejects a token request."""


def build_authorize_url(
    *,
    client_id: int,
    redirect_uri: str,
    scopes: tuple[str, ...] | list[str],
    authorize_url: str,
) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "approval_prompt": "force",
            "scope": format_scopes(tuple(scopes)),
        },
        safe=",:/",
    )
    return f"{authorize_url}?{query}"


def extract_code(pasted: str) -> str:
    text = pasted.strip()
    if not text:
        raise ValueError("Paste the redirect URL from the browser")
    if "error=" in text and "code=" not in text:
        parsed = urlparse(text if "://" in text else f"http://localhost/?{text.lstrip('?')}")
        error = parse_qs(parsed.query).get("error", ["access_denied"])[0]
        raise ValueError(f"Strava authorization failed: {error}")
    if "code=" in text:
        candidate = text if "://" in text else f"http://localhost/?{text.lstrip('?')}"
        code = parse_qs(urlparse(candidate).query).get("code", [""])[0]
    else:
        code = text
    if not code or any(char.isspace() for char in code):
        raise ValueError("No authorization code found in that value")
    return code


def scopes_from_redirect(pasted: str) -> set[str] | None:
    text = pasted.strip()
    if "scope=" not in text:
        return None
    candidate = text if "://" in text else f"http://localhost/?{text.lstrip('?')}"
    raw = parse_qs(urlparse(candidate).query).get("scope", [""])[0]
    return parse_scopes(raw) if raw else None


def _expires_at(payload: dict[str, Any]) -> datetime | None:
    if payload.get("expires_at"):
        return datetime.fromtimestamp(int(payload["expires_at"]), timezone.utc).replace(tzinfo=None)
    if payload.get("expires_in"):
        moment = datetime.now(timezone.utc) + timedelta(seconds=int(payload["expires_in"]))
        return moment.replace(tzinfo=None)
    return None


def _post_token(settings: Settings, form: dict[str, str]) -> dict[str, Any]:
    try:
        response = requests.post(settings.token_url, data=form, timeout=30)
    except requests.RequestException as exc:
        raise TokenExchangeError(f"Token request failed: {exc}") from exc
    if response.status_code >= 400:
        # Strava's error body names the OAuth error and does not echo the token.
        detail = response.text[:300]
        raise TokenExchangeError(f"Token request returned {response.status_code}: {detail}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise TokenExchangeError("Token response was not JSON") from exc
    if "access_token" not in payload or "refresh_token" not in payload:
        raise TokenExchangeError("Token response did not include access_token and refresh_token")
    return payload


def exchange_authorization_code(settings: Settings, code: str) -> AccessToken:
    payload = _post_token(
        settings,
        {
            "client_id": str(settings.client_id),
            "client_secret": settings.client_secret,
            "code": code,
            "grant_type": "authorization_code",
        },
    )
    scope = str(payload.get("scope") or "")
    assert_required_scopes(scope, settings.required_scopes)
    athlete = payload.get("athlete") or {}
    athlete_id = str(athlete["id"]) if athlete.get("id") else None
    return AccessToken(
        athlete_id=athlete_id,
        access_token=str(payload["access_token"]),
        refresh_token=str(payload["refresh_token"]),
        expires_at=_expires_at(payload),
        scope=scope,
    )


def refresh_access_token(settings: Settings, refresh_token: str, known_scope: str | None) -> AccessToken:
    payload = _post_token(
        settings,
        {
            "client_id": str(settings.client_id),
            "client_secret": settings.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )
    scope = str(payload.get("scope") or known_scope or "")
    assert_required_scopes(scope, settings.required_scopes)
    athlete = payload.get("athlete") or {}
    athlete_id = str(athlete["id"]) if athlete.get("id") else None
    return AccessToken(
        athlete_id=athlete_id,
        access_token=str(payload["access_token"]),
        refresh_token=str(payload["refresh_token"]),
        expires_at=_expires_at(payload),
        scope=scope,
    )


def token_is_fresh(token: AccessToken) -> bool:
    if token.expires_at is None:
        return False
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return token.expires_at - REFRESH_SKEW > now


def resolve_scope(*candidates: str | None) -> str:
    for candidate in candidates:
        if candidate and parse_scopes(candidate):
            return candidate
    return ""


def describe_token(token: AccessToken) -> str:
    expires = token.expires_at.isoformat() if token.expires_at else "unknown"
    return f"athlete {token.athlete_id or 'unknown'} expires {expires} scope {token.scope}"


def log_token_ready(token: AccessToken, source: str) -> None:
    logger.info("Using %s access token for %s", source, describe_token(token))


__all__ = [
    "AccessToken",
    "ScopeError",
    "TokenExchangeError",
    "build_authorize_url",
    "exchange_authorization_code",
    "extract_code",
    "refresh_access_token",
    "scopes_from_redirect",
    "token_is_fresh",
]
