# Reset tracker

A Python 3.13+ service that watches official forecasts of future Codex resets and sends webhook
notifications. It runs in a foreground process, with Docker Compose as the primary
deployment method.

## Start with Docker

1. Copy the example files:

   ```sh
   cp config.example.toml config.toml
   cp .env.example .env
   ```

2. Set your contact email and Discord webhook URL in `.env`. Edit `config.toml`
   to add endpoints or change their payloads.
3. Validate and preview, then start:

   ```sh
   docker compose build
   docker compose run --rm reset-tracker --config /config/config.toml validate
   docker compose run --rm reset-tracker --config /config/config.toml preview --webhook discord
   docker compose up -d
   docker compose logs -f
   ```

The first successful fetch sends the **current official future-reset forecast**,
including its reported probability. Later fetches notify about new official
announcements. These are predictions, not confirmations that a reset has taken place.
The container runs as UID/GID 10001. Configuration is mounted read-only, and the named
`reset-tracker-data` volume persists the SQLite database under `/data`.
Stopping or recreating the container preserves that state. Use one service instance
per database; horizontal replication is outside this version's scope.

## Configuration

```toml
interval_seconds = 600
request_timeout_seconds = 20
database_path = "/data/reset-tracker.sqlite3"
user_agent = "reset-tracker/0.1.0 (+mailto:${CONTACT_EMAIL})"
providers = ["codex_reset"]

[[webhooks]]
id = "discord"
url = "${DISCORD_WEBHOOK_URL}"
preset = "discord"
```

Relative database and template paths resolve from the configuration file's directory.
`interval_seconds` must be at least 60. Provider names and webhook IDs must be unique;
an ID uses 1–100 letters, digits, underscores, or hyphens. Unknown fields are rejected.
The required User-Agent identifies your project/version and a contact URL or email.

Environment references use `${NAME}`. They are expanded in URLs, headers, the
User-Agent, database path, and template file paths. An undefined reference fails
startup. Template bodies themselves are not expanded as environment strings.
The local CLI reads the process environment; Docker Compose loads `.env` into the
container. The application does not load dotenv files itself.

### Built-in presets

| Preset | Body |
| --- | --- |
| `discord` | A rich embed labeled “Codex reset forecast” with the signal probability, separate 24h/48h cadence estimates, expected window, source links, and tracker attribution. Mentions are disabled; `wait=true` confirms message storage. Existing query parameters such as `thread_id` are preserved. |
| `generic_json` | `{"event": {...}, "detected_at": "..."}` containing the normalized event. |

Each event exposes `id`, `announced_at`, `summary`, `url`, `provider`, `source_url`,
and `kind`. New forecast events have `kind = "forecast"`. Existing cached events
without a kind are restored as `"confirmed"` so pending notifications retain their meaning.
Timestamps are UTC ISO 8601 strings. `url` links to the original announcement;
`source_url` identifies the tracker supplying the data.
`announced_at` is the official signal post's timestamp. `expected_at` is the
announced target time or window end, when supplied, as a UTC ISO 8601 string.
`window_label` preserves the tracker's human-readable window.

Forecasts also expose numeric percentage fields, each on a **0–100** scale:

| Template field | Source and meaning |
| --- | --- |
| `event.probability` | Official signal probability from `probabilities.signal_percent`, falling back to `official_signal.score.value` or `probabilities.commitment × 100`. |
| `event.probability_24h` | The separate 24-hour cadence estimate, `probabilities.rounded_24h`. |
| `event.probability_48h` | The separate 48-hour cadence estimate, `probabilities.rounded_48h`. |

Unknown values are `null`, including probabilities on legacy queued events.
Signal probability and cadence estimates are distinct; for example, a 93% official
signal can coexist with 32% and 53% cadence estimates. Values are captured when the
forecast is first detected and preserved through delivery retries.

### Custom Jinja2 payloads

```toml
[[webhooks]]
id = "custom"
url = "${CUSTOM_WEBHOOK_URL}"
preset = "custom"
method = "POST"
body_format = "json"
body_template = '''
{
  "text": {{ event.summary | tojson }},
  "announcement": {{ event.url | tojson }},
  "source": {{ event.source_url | tojson }},
  "probability": {{ event.probability | tojson }},
  "probability_24h": {{ event.probability_24h | tojson }},
  "probability_48h": {{ event.probability_48h | tojson }},
  "expected_at": {{ event.expected_at | tojson }},
  "detected_at": {{ detected_at | tojson }}
}
'''

[webhooks.headers]
Authorization = "Bearer ${CUSTOM_WEBHOOK_TOKEN}"
```

Use `tojson` for JSON string values, without surrounding quotation marks.
Every JSON template renders the complete request body as a **JSON object**.
Nested objects and arrays retain their types, allowing rich payloads such as
Discord embeds, fields, and components. The renderer parses and validates the
result before sending; a top-level array or scalar is rejected.
Templates receive only `event` and `detected_at`, with standard Jinja filters
and strict undefined-variable handling. The `limit_utf16(n)` filter bounds text
to `n` UTF-16 units, including emoji, for Discord character limits.
Startup validation renders a sample payload; failures specific to real data pause
the endpoint at delivery time.

Use `template_file = "templates/webhook.j2"` instead of `body_template` to load a
UTF-8 template file. When using Docker, mount that directory read-only under
`/config/templates`. Choose exactly one template source. Both `body_template`
and `template_file` can override a built-in preset; `preset = "custom"` requires one.

For example, override the complete Discord payload to customize its rich content:

```toml
[[webhooks]]
id = "discord"
url = "${DISCORD_WEBHOOK_URL}"
preset = "discord"
body_template = '''
{
  "embeds": [{
    "title": {{ ("Codex reset " ~ event.kind) | tojson }},
    "description": {{ event.summary | limit_utf16(4096) | tojson }},
    "url": {{ event.url | tojson }},
    "timestamp": {{ event.announced_at | tojson }},
    "color": 5793266,
    "fields": [
      {
        "name": "Signal probability",
        "value": {{ (("%g%%" | format(event.probability)) if event.probability is not none else "Unknown") | tojson }}
      },
      {"name": "Data", "value": {{ event.source_url | tojson }}}
    ]
  }],
  "allowed_mentions": {"parse": []}
}
'''
```

The Discord preset preserves `wait=true` with a body override and adds disabled
mentions when `allowed_mentions` is omitted. Explicit payload fields are preserved.

For a plain text request, set `preset = "custom"` and `body_format = "text"`.
The default method is POST; custom requests can specify another method.
Content-Type defaults to application/json or text/plain with UTF-8, and can be
overridden through `headers`. Redirect responses are not followed.

## Providers and polling

Providers run sequentially in configured order. The first successful HTTP fetch
and parse ends the poll, even if there is no future official signal or no change.
Errors advance to the next provider. Polling begins immediately, then follows
the configured cadence without overlap; slow polls skip missed ticks.

The built-in `codex_reset` provider reads
[codex-reset.com's forecast endpoint](https://codex-reset.com/api/forecast).
It reads the top-level `official_signal` object, which does not require the
feed endpoint's `active` field.
The provider maps `tweet_id` to the event ID, `at` to `announced_at`, and carries
the signal's `summary`, original `url`, probabilities, and announced window
into the notification. Missing, null, empty, or explicitly inactive official signals
are successful empty results. A signal is skipped if a reset has occurred at or after
its announcement, or if its announced window has expired.
Malformed official signals or invalid probability values fail that provider,
allowing the next provider to run.
Cadence estimates alone, without an official signal, do not trigger notifications.
Provider HTTP 429 responses create a persisted cooldown of at least 60 seconds,
respecting longer Retry-After values.

Forecasts use normalized original announcement URLs to deduplicate across providers,
with a forecast-specific identity distinct from older confirmed and committed alerts.
Unseen events at or beyond the stored announcement timestamp watermark qualify;
older history and previously recorded announcements do not replay.
Equal timestamps can contain distinct new events, and the watermark never moves
backward. Summary edits, probability changes, and refreshed calculation timestamps
alone do not send another alert.
Forecasts use a separate cache watermark from the former timeline and feed providers.
Upgrading preserves existing events and pending deliveries while allowing one new
forecast notification for the current official announcement, including when its
commitment-only notification was previously sent. No database deletion is needed.
The endpoint exposes one current official signal, so announcements that appear and disappear
between polls cannot be recovered as historical catch-up.

To add a provider, implement the `Provider` protocol in `src/reset_tracker/providers.py`:
provide a stable `name` and `fetch(httpx.Client) -> list[ResetEvent]`, then register
a zero-argument factory in `PROVIDERS`. Raise `ProviderError` for invalid source
data, and return an empty list for a valid response without qualifying resets.
Use the original announcement URL consistently to deduplicate fallback results.
Providers for the same forecast stream should set `cache_namespace = "forecast"`
to share its initialization state and watermark.

## Delivery and recovery

SQLite commits detected events, the cache watermark, and queued webhook deliveries
in one transaction. Queued events survive process restarts.

- Each endpoint processes its oldest pending event first. A failing endpoint does
  not block other endpoints.
- Network errors, HTTP 408/429, and HTTP 5xx retry indefinitely, starting at 30
  seconds and doubling to a one-hour cap. Longer Retry-After hints, including
  Discord's `retry_after` field, take precedence.
- A cooldown applies to the entire endpoint. Discord's successful responses with
  exhausted rate-limit headers also defer later messages. Retries do not trigger
  additional provider fetches.
- Other unsuccessful HTTP statuses and rendering errors pause the endpoint.
  Outstanding messages stay queued. After correcting configuration, restart the
  service to load it, then resume paused endpoints:

  ```sh
  docker compose restart reset-tracker
  docker compose exec reset-tracker reset-tracker --config /config/config.toml retry-failed --webhook discord
  ```

Pending deliveries use the current configuration, including a rotated URL or
corrected template. Keep endpoint IDs stable when changing credentials.
Removed endpoints retain their pending queue without sending. Adding a new
endpoint does not replay cached history; it receives future discoveries.

Delivery is **at least once**. If the receiver accepts a message and the process
exits before recording success, the message may be repeated on recovery.
Successful deliveries are otherwise retained and never resent.
Data availability is limited to events returned by upstream providers.

Logs go to stdout in UTC and identify providers/endpoints without printing
webhook URLs, credentials, response bodies, or authorization headers.
Exceptions include their messages, chained causes, and full tracebacks by default,
including provider, network, rendering, configuration, storage, and unexpected
application failures. HTTP(S) URLs and configured credentials are redacted before
the log record is emitted; traceback locals are not captured. Polling fallback,
delivery retry, and endpoint pause behavior remain the same.
Payload preview prints the rendered body, so template authors should keep secrets
in headers rather than message bodies.

## Local use and development

```sh
uv sync --frozen
uv run reset-tracker presets
uv run reset-tracker --config config.toml validate
uv run reset-tracker --config config.toml preview --webhook discord
uv run reset-tracker --config config.toml once
uv run reset-tracker --config config.toml run
uv run reset-tracker --config config.toml retry-failed
```

For local use, change `database_path` to `data/reset-tracker.sqlite3` and export
the variables referenced in the configuration. `once` fetches once and drains
currently due deliveries without waiting for future retry deadlines.
It exits 0 when a provider succeeds and no configured deliveries remain pending,
1 for unavailable providers, pending deliveries, or storage failures, and 2 for
configuration errors. `retry-failed` only resumes paused endpoints; the next run
performs delivery. In a live service, resumption is noticed within 60 seconds.
`validate`, `preview`, and `presets` make no network requests and do not open the database.

HTTPX and uv respect HTTP_PROXY, HTTPS_PROXY, and NO_PROXY. For a local proxy in
PowerShell:

```powershell
$env:HTTP_PROXY = "http://localhost:7890"
$env:HTTPS_PROXY = "http://localhost:7890"
$env:NO_PROXY = "localhost,127.0.0.1"
```

Inside Docker Desktop, use `http://host.docker.internal:7890` in `.env` instead
of localhost. For dependency installation during an image build, pass
`--build-arg HTTP_PROXY=http://host.docker.internal:7890` and the corresponding
HTTPS_PROXY build argument. Image pulls use the Docker daemon's proxy settings.

Run verification:

```sh
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run python tests/smoke.py
```

The smoke script uses real local HTTP servers and separate CLI processes. It
verifies collection, a persisted failed send, restart recovery, ordered catch-up,
and no replay, without contacting an actual reset site or messaging service.
It advances the saved retry deadline to avoid a 30-second wait.
To run it inside the production image with a persistent Docker volume:

```sh
docker build -t reset-tracker:local .
docker volume create reset-tracker-smoke
docker run --rm --entrypoint python --mount type=bind,source="$PWD/tests/smoke.py",target=/smoke.py,readonly --mount type=volume,source=reset-tracker-smoke,target=/data reset-tracker:local /smoke.py --data-dir /data/smoke
docker volume rm reset-tracker-smoke
```

## Source attribution

Data: [codex-reset.com](https://codex-reset.com/), an independent community tracker.
Its [developer terms](https://codex-reset.com/developers) require an identifying
User-Agent and visible source credit for public redistribution. Both built-in
presets include the source URL; retain visible credit in custom public messages.
