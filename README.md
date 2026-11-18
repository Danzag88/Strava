# Strava weekly activity backup

Strava consent happens once. Each run calls `https://www.strava.com/oauth/token` with the saved refresh token, then merges activities into `STRAVA_DB.ACTIVITIES.STRAVA_ACTIVITIES`. Activity and athlete ids are taken from `id_str` and stored in `NUMBER(38,0)`, which holds a 64-bit integer exactly. The Mac crontab runs that job on Sunday at 17:00 UTC. GitHub Actions uses the same time after this workflow is on `main` and the Actions secrets are set.

## One-time setup

1. In the Strava API settings for client id `289844`, set Authorization Callback Domain to `localhost`.
2. Copy `.env.example` to `.env` and set `STRAVA_CLIENT_SECRET`.
3. Create a Snowflake key pair and run [`sql/000_account.sql`](sql/000_account.sql) as `ACCOUNTADMIN` after replacing `REPLACE_WITH_PUBLIC_KEY`:

```bash
openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -outform PEM -nocrypt -out snowflake_key.p8
openssl rsa -in snowflake_key.p8 -pubout -out snowflake_key.pub
grep -v "PUBLIC KEY" snowflake_key.pub | tr -d '\n'
base64 < snowflake_key.p8 | tr -d '\n'
```

Put the public-key line in `RSA_PUBLIC_KEY`. Put either that base64 private key, or a Snowflake programmatic access token, in `.env` as `SNOWFLAKE_PRIVATE_KEY_B64`. Set `SNOWFLAKE_ACCOUNT` to the organization-account identifier (`ORGNAME-ACCOUNT`) without `.snowflakecomputing.com`. A bare locator returns a 404. A value that looks like a JWT is used as a PAT, and `SNOWFLAKE_USER` must be the user that token was issued to.

4. Install with Python 3.11 or newer and approve access. Leave every scope checked. The required grant is `read,read_all,profile:read_all,activity:read_all`. macOS `python3` is often 3.9, which is too old; use a 3.11+ interpreter.

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e .
python -m strava_backup.bootstrap
```

The command prints an authorize URL. After you approve, the browser cannot open `localhost`. Copy the full address bar and paste it into the prompt. The command writes the refresh token to `.env` and, when Snowflake is configured, to `OPS.OAUTH_TOKEN`. It refuses to save a token that is missing `read_all`, `profile:read_all`, or `activity:read_all`.

5. Push this repo to `main`. In GitHub, add these Actions secrets: `STRAVA_CLIENT_SECRET`, `STRAVA_REFRESH_TOKEN`, `STRAVA_GRANTED_SCOPE`, `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_ROLE`, `SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_DATABASE`, `SNOWFLAKE_PRIVATE_KEY_B64`. The weekly schedule runs only after the workflow file is on `main`.

The scheduled job lists activities and merges each response into `STRAVA_DB.ACTIVITIES.STRAVA_ACTIVITIES`. A manual run is available from the Actions tab. Use the `reseed_token` input only after a new bootstrap, when the GitHub refresh-token secret should replace the stored copy.

GitHub pauses scheduled workflows after 60 days without a commit. If GitHub emails that notice, re-enable the workflow. `workflow_dispatch` still works.
