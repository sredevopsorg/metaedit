# Configuration

Every environment variable the stack reads, what it changes, and its default. The
authoritative source is [`src/metaedit/config.py`](../src/metaedit/config.py)
(`Settings`, via pydantic-settings — case-insensitive, `.env` is read automatically);
this page is for reading, not for code generation. [`.env.example`](../.env.example) is
the template to copy and carries comments on the variables you are most likely to
touch.

Values are **environment only** — nothing is baked into the image and no secrets are
logged (both keys are `SecretStr`, and a test asserts they cannot leak through
`model_dump()` or `repr()`).

## Compose-level variables

These are interpolated by [`docker-compose.yml`](../docker-compose.yml) when bringing
the stack up; they shape the *deployment* rather than the app process.

| Variable | Default | Meaning |
|---|---|---|
| `APP_BIND` | `127.0.0.1` | Host address the app port is published on. Loopback on purpose — see the security note below. |
| `APP_PORT` | `8080` | Host port for the app. The container always listens on 8080. |
| `POSTGRES_USER` | `metaedit` | Postgres superuser name, also the database owner. |
| `POSTGRES_PASSWORD` | `metaedit` | Postgres password. **Set this** — the default is a placeholder. |
| `POSTGRES_DB` | `metaedit` | Database name. |
| `POSTGRES_PORT` | `5432` | Host port for Postgres, published for local tooling. |

The compose file also passes `JELLYFIN_URL`, `JELLYFIN_API_KEY`, `LASTFM_API_KEY`,
`LASTFM_USER_AGENT`, `ARCHIVE_SOFT_CAP_BYTES`, `LASTFM_MAX_RPS`, `LOG_LEVEL` and
`LOG_JSON` through to the app container; their meaning is in the app tables below. A
`migrate` one-shot service runs `alembic upgrade head` before the app starts, so
migrations never run implicitly at application startup.

## App: HTTP server and logging

| Variable | Default | Meaning |
|---|---|---|
| `BIND_HOST` | `127.0.0.1` | Address the API binds inside the container. |
| `BIND_PORT` | `8080` | Port the API binds inside the container. |
| `LOG_LEVEL` | `INFO` | Python log level. |
| `LOG_JSON` | `true` | Structured JSON logs (structlog). Turn off for human-readable local logs. |

Logs go to **stderr** — the CLI keeps stdout parseable for its JSON reports.

## App: Jellyfin

| Variable | Default | Meaning |
|---|---|---|
| `JELLYFIN_URL` | `http://localhost:8096` | Server root. A bare `host:port` is accepted and normalised to `http://`, because that is the natural way to write a LAN address. |
| `JELLYFIN_API_KEY` | *(empty)* | **Administrator** API key. Writes are gated on `RequiresElevation`; a non-admin key can browse but not save, and `GET /api/info` reports which one you have. |
| `JELLYFIN_USER_ID` | *(empty)* | An API key carries no user identity, and Jellyfin's single-item endpoint requires a user context. When empty, a user is discovered from `/Users` (first administrator in sorted-id order, cached per process); set it to pin the choice on a server where "first" is not the right one. |
| `JELLYFIN_TIMEOUT_S` | `10.0` | Per-request timeout, seconds. |
| `JELLYFIN_MAX_RETRIES` | `2` | Retries on transient failures. |

## App: Last.fm

| Variable | Default | Meaning |
|---|---|---|
| `LASTFM_API_KEY` | *(empty)* | API key. Read-only usage: no user auth, no session signing, nothing is ever written back to Last.fm. |
| `LASTFM_USER_AGENT` | `metaedit/0.2.0 (+https://github.com/sredevopsorg/metaedit)` | Identification Last.fm asks for. Leave the line out entirely for the default; a *set-but-empty* value sends no User-Agent, which Last.fm may treat as abusive. |
| `LASTFM_TIMEOUT_S` | `10.0` | Per-request timeout, seconds. |
| `LASTFM_MAX_RPS` | `4.0` | Shared client-side rate limit, requests per second. |
| `LASTFM_BURST` | `8` | Burst allowance for the rate limiter. |
| `LASTFM_MAX_RETRIES` | `3` | Retries with backoff. |

## App: database

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://metaedit:metaedit@localhost:5432/metaedit` | SQLAlchemy URL. Compose points it at the `postgres` service; local development uses the host-published port. |
| `DB_POOL_SIZE` | `5` | Connection pool size. |
| `DB_MAX_OVERFLOW` | `5` | Pool overflow allowance. |
| `DB_ECHO` | `false` | Echo SQL — debugging only. |

## App: archive

| Variable | Default | Meaning |
|---|---|---|
| `ARCHIVE_ENABLED` | `true` | Master switch. `false` stops recording and skips partition maintenance at startup; the editor then finds no candidates. Escape hatch, not a supported configuration. |
| `ARCHIVE_LOG_REQUESTS` | `true` | Log every HTTP attempt in `lastfm_request`. |
| `ARCHIVE_SOFT_CAP_BYTES` | `104857600` | The Last.fm ToS "Reasonable Usage Cap" (100 MiB). Soft: re-observing a stored body costs nothing; only genuinely new payloads count against it. |
| `ARCHIVE_WARN_RATIO` | `0.8` | Warn when stored bytes cross this fraction of the cap. |
| `ARCHIVE_REFUSE_RATIO` | `1.0` | Refuse new payloads (`ArchiveCapReached`) at this fraction. Must not be below the warn ratio — the app rejects that at startup, because a warning that cannot fire before the refusal is dead code. |
| `ARCHIVE_FRESHNESS_TTL_S` | `86400` | How long a stored response may be served without a refresh (24 h). |
| `ARCHIVE_SIMILAR_FRESHNESS_TTL_S` | `604800` | Same, for `artist.getSimilar` — it changes slowly (7 days). |
| `ARCHIVE_PARTITION_MONTHS_AHEAD` | `3` | How many monthly `lastfm_request` partitions `metaedit partitions` pre-creates by default. |

## App: mapping policy

| Variable | Default | Meaning |
|---|---|---|
| `LASTFM_MIN_TAG_COUNT` | `0` | Tags below this `getTopTags` count are not proposed. |
| `LASTFM_GENRE_LIMIT` | `5` | How many genres mapping proposes at most. |
| `LASTFM_STYLE_LIMIT` | `10` | How many styles (mapped onto `Tags`) at most. |
| `MAX_TAGS_PER_ITEM` | `30` | Hard ceiling on tags written per item. |
| `MAX_TAG_LENGTH` | `100` | Maximum tag length accepted. |
| `TAG_BLACKLIST_EXTRA` | *(empty)* | Comma-separated genres never *proposed* — deployment policy, merged with the built-in list and the database list edited at Settings → *Blacklisted genres*. Matching is exact and case-insensitive; it never rewrites existing items. |

## App: frontend

| Variable | Default | Meaning |
|---|---|---|
| `WEB_DIST_DIR` | *(empty)* | Where the built SPA is served from. Empty disables SPA serving (API only); the compose image sets `/app/web`. |

## Read directly, outside `Settings`

| Variable | Default | Meaning |
|---|---|---|
| `METAEDIT_BROWSE_SCAN_CAP` | `2000` | Bounds how many items a filter that needs a read will scan (`missing`, `exclude`). Raise it on a large library and filtering gets slower, not wrong. It began as a test hook for the truncation path; it is read per call, not cached. |

## Tests only

| Variable | Default | Meaning |
|---|---|---|
| `METAEDIT_TEST_DATABASE_URL` | `postgresql+psycopg://metaedit:metaedit@localhost:5432/metaedit` | Admin URL for the integration suite, which builds disposable per-test databases from a template migrated once per session. Tests **skip cleanly** when no Postgres 18 answers. |

## Security note

`APP_BIND` defaults to loopback deliberately: the app holds an administrator Jellyfin
key and has no multi-user authentication of its own. If you expose it, put an
authenticating proxy in front of it first. The app adds its own hardening — CSP,
`nosniff`, `frame-ancestors 'none'`, a 2 MiB body cap — but that is defence in depth,
not an authentication layer. See [API](api.md#hardening).
