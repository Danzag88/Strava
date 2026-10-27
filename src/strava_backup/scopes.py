"""Scope checks for the Strava OAuth grant."""

from __future__ import annotations

REQUIRED_SCOPE_NAMES = ("read_all", "profile:read_all", "activity:read_all")


class ScopeError(Exception):
    """Raised when a token does not include the required read scopes."""


def parse_scopes(scope_value: object) -> set[str]:
    if scope_value is None:
        return set()
    if isinstance(scope_value, (list, tuple, set)):
        return {str(part).strip() for part in scope_value if str(part).strip()}
    text = str(scope_value).replace(" ", "")
    return {part for part in text.split(",") if part}


def missing_scopes(scope_value: object, required: tuple[str, ...] = REQUIRED_SCOPE_NAMES) -> list[str]:
    granted = parse_scopes(scope_value)
    return [name for name in required if name not in granted]


def assert_required_scopes(scope_value: object, required: tuple[str, ...] = REQUIRED_SCOPE_NAMES) -> set[str]:
    granted = parse_scopes(scope_value)
    missing = [name for name in required if name not in granted]
    if missing:
        granted_text = ",".join(sorted(granted)) or "(none)"
        raise ScopeError(
            "Strava token is missing required scopes: "
            + ", ".join(missing)
            + f". Granted: {granted_text}. "
            "Run python -m strava_backup.bootstrap and approve every scope."
        )
    return granted


def format_scopes(scopes: tuple[str, ...] | list[str]) -> str:
    return ",".join(scopes)
