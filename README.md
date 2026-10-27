# Strava daily backup

The backup runs by itself at 06:00 UTC from GitHub Actions. Strava consent happens once. Each run exchanges the refresh token for a new access token and writes that refresh token back to Snowflake.

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

Put the public-key line in `RSA_PUBLIC_KEY`. Put the base64 private key in `.env` as `SNOWFLAKE_PRIVATE_KEY_B64`, and set `SNOWFLAKE_ACCOUNT` to the account identifier without `.snowflakecomputing.com`.

4. Install with Python 3.11 or newer and approve access. Leave every scope checked. The required grant is `read,read_all,profile:read_all,activity:read_all`. macOS `python3` is often 3.9, which is too old; use a 3.11+ interpreter.

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e .
python -m strava_backup.bootstrap
```

The command prints an authorize URL. After you approve, the browser cannot open `localhost`. Copy the full address bar and paste it into the prompt. The command writes the refresh token to `.env` and, when Snowflake is configured, to `OPS.OAUTH_TOKEN`. It refuses to save a token that is missing `read_all`, `profile:read_all`, or `activity:read_all`.

5. Push this repo to `main`. In GitHub, add these Actions secrets: `STRAVA_CLIENT_SECRET`, `STRAVA_REFRESH_TOKEN`, `STRAVA_GRANTED_SCOPE`, `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_ROLE`, `SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_DATABASE`, `SNOWFLAKE_PRIVATE_KEY_B64`. The schedule runs only after the workflow file is on `main`.

The first runs fill history until the daily read cap, then continue the next morning. Segment efforts are copied from each activity detail response into `SEGMENT_EFFORTS`. `SEGMENT_EFFORT` and `SEGMENT_EFFORT_STREAMS` are created for those API calls and stay empty, because fetching every effort would use up the daily read cap. A manual run is available from the Actions tab. Use the `reseed_token` input only after a new bootstrap, when the GitHub refresh-token secret should replace the Snowflake copy.

GitHub pauses scheduled workflows after 60 days without a commit. If GitHub emails that notice, re-enable the workflow. `workflow_dispatch` still works.
