"""Load committed Strava config and gitignored environment values."""

from __future__ import annotations

import os
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from strava_backup.scopes import REQUIRED_SCOPE_NAMES

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def repo_root() -> Path:
    here = Path(__file__).resolve()
    candidates = [here.parents[2], Path.cwd()]
    for candidate in candidates:
        if (candidate / "config" / "strava.toml").exists():
            return candidate
    raise FileNotFoundError("config/strava.toml not found")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def as_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "y"}


def require_identifier(name: str, value: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise SystemExit(f"{name} must be a Snowflake identifier")
    return value


@dataclass(frozen=True)
class Settings:
    client_id: int
    client_secret: str
    redirect_uri: str
    scopes: tuple[str, ...]
    required_scopes: tuple[str, ...]
    refresh_token: str | None
    granted_scope: str | None
    reseed: bool
    api_base: str
    token_url: str
    authorize_url: str
    snowflake_account: str
    snowflake_user: str
    snowflake_role: str
    snowflake_warehouse: str
    snowflake_database: str
    snowflake_private_key_b64: str

    @property
    def snowflake_configured(self) -> bool:
        return bool(
            self.snowflake_account
            and self.snowflake_user
            and self.snowflake_private_key_b64
        )


def load_settings(*, require_snowflake: bool, require_secret: bool = True) -> Settings:
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11 or newer is required")
    root = repo_root()
    load_dotenv(root / ".env")
    with (root / "config" / "strava.toml").open("rb") as handle:
        config = tomllib.load(handle)

    missing: list[str] = []
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET", "").strip()
    if require_secret and not client_secret:
        missing.append("STRAVA_CLIENT_SECRET")

    account = os.environ.get("SNOWFLAKE_ACCOUNT", "").strip()
    if account.endswith(".snowflakecomputing.com"):
        account = account[: -len(".snowflakecomputing.com")]
    user = os.environ.get("SNOWFLAKE_USER", "STRAVA_LOADER").strip()
    role = os.environ.get("SNOWFLAKE_ROLE", "STRAVA_BACKUP").strip()
    warehouse = os.environ.get("SNOWFLAKE_WAREHOUSE", "STRAVA_WH").strip()
    database = os.environ.get("SNOWFLAKE_DATABASE", "STRAVA").strip()
    private_key = os.environ.get("SNOWFLAKE_PRIVATE_KEY_B64", "").strip()

    if require_snowflake:
        if not account:
            missing.append("SNOWFLAKE_ACCOUNT")
        if not private_key:
            missing.append("SNOWFLAKE_PRIVATE_KEY_B64")
        if not user:
            missing.append("SNOWFLAKE_USER")
    if missing:
        raise SystemExit("Missing environment values: " + ", ".join(missing))

    if require_snowflake:
        if not ACCOUNT.fullmatch(account):
            raise SystemExit("SNOWFLAKE_ACCOUNT contains unsupported characters")
        require_identifier("SNOWFLAKE_USER", user)
        require_identifier("SNOWFLAKE_ROLE", role)
        require_identifier("SNOWFLAKE_WAREHOUSE", warehouse)
        require_identifier("SNOWFLAKE_DATABASE", database)

    scopes = tuple(config.get("scopes") or [])
    required = tuple(config.get("required_scopes") or REQUIRED_SCOPE_NAMES)
    return Settings(
        client_id=int(config["client_id"]),
        client_secret=client_secret,
        redirect_uri=str(config["redirect_uri"]),
        scopes=scopes,
        required_scopes=required,
        refresh_token=os.environ.get("STRAVA_REFRESH_TOKEN", "").strip() or None,
        granted_scope=os.environ.get("STRAVA_GRANTED_SCOPE", "").strip() or None,
        reseed=as_bool(os.environ.get("RESEED_TOKEN")),
        api_base=str(config.get("api_base", "https://www.strava.com/api/v3")).rstrip("/"),
        token_url=str(config.get("token_url", "https://www.strava.com/oauth/token")),
        authorize_url=str(config.get("authorize_url", "https://www.strava.com/oauth/authorize")),
        snowflake_account=account,
        snowflake_user=user,
        snowflake_role=role,
        snowflake_warehouse=warehouse,
        snowflake_database=database,
        snowflake_private_key_b64=private_key,
    )


def upsert_env_values(updates: dict[str, str], path: Path | None = None) -> None:
    root = repo_root()
    env_path = path or (root / ".env")
    if not env_path.exists():
        example = root / ".env.example"
        if example.exists() and path is None:
            env_path.write_text(example.read_text())
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    remaining = dict(updates)
    rewritten: list[str] = []
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            rewritten.append(line)
            continue
        key, _value = line.split("=", 1)
        key = key.strip()
        if key in remaining:
            rewritten.append(f"{key}={remaining.pop(key)}")
        else:
            rewritten.append(line)
    for key, value in remaining.items():
        rewritten.append(f"{key}={value}")
    env_path.write_text("\n".join(rewritten) + "\n")
    env_path.chmod(0o600)
