# Reset tracker

A Python 3.13+ service that watches official forecasts and confirmed Codex resets and sends
webhook notifications. It runs in a foreground process, with Docker Compose as the primary
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
including its reported probability, and the **latest confirmed global reset**, when
available. Each stream initializes independently. Later fetches send new forecasts
and a separate confirmation after the tracker reports that a reset has taken effect.
An elapsed forecast deadline alone never produces a confirmation. Every newly
discovered reset is saved in SQLite and delivered individually in chronological order.
The container runs as UID/GID 10001. Configuration is mounted read-only, and the named
`reset-tracker-data` volume persists the SQLite database under `/data`.
Stopping or recreating the container preserves that state. Use one service instance
per database; horizontal replication is outside this version's scope.

## Configuration

```toml
interval_seconds = 600
confirmation_interval_seconds = 120
request_timeout_seconds = 20
confirmation_dedup_window_seconds = 1800
database_path = "/data/reset-tracker.sqlite3"
user_agent = "reset-tracker/0.1.0 (+mailto:${CONTACT_EMAIL})"
providers = ["codex_reset"]

[[webhooks]]
id = "discord"
url = "${DISCORD_WEBHOOK_URL}"
preset = "discord"
```

Relative database and template paths resolve from the configuration file's directory.
`interval_seconds` controls forecast polling (default 600 seconds).
`confirmation_interval_seconds` independently controls confirmation polling (default
120 seconds). Both must be integers of at least 60. Provider names and webhook IDs must be unique;
an ID uses 1–100 letters, digits, underscores, or hyphens. Unknown fields are rejected.
The required User-Agent identifies your project/version and a contact URL or email.
`confirmation_dedup_window_seconds` is a nonnegative integer, defaulting to 1800
(30 minutes). Set it to 0 to disable approximate time-window grouping while retaining
post identity and explicit reset-ID/effect-time matching.

Environment references use `${NAME}`. They are expanded in URLs, headers, the
User-Agent, database path, and template file paths. An undefined reference fails
startup. Template bodies themselves are not expanded as environment strings.
The local CLI reads the process environment; Docker Compose loads `.env` into the
container. The application does not load dotenv files itself.

### Built-in presets

| Preset | Body |
| --- | --- |
| `discord` | A rich embed labeled “🔄 Codex reset forecast” or “✅ Codex reset confirmed”. Forecasts include probabilities and the expected window; confirmations include the effect/observation time when available. Both include source links and tracker attribution. Mentions are disabled; `wait=true` confirms message storage. Existing query parameters such as `thread_id` are preserved. |
| `generic_json` | `{"event": {...}, "detected_at": "..."}` containing the normalized event. |

Each event exposes `id`, `announced_at`, `summary`, `url`, `provider`, `source_url`,
and `kind`. Events have `kind = "forecast"` or `kind = "confirmed"`. Existing cached events
without a kind are restored as `"confirmed"` so pending notifications retain their meaning.
Timestamps are UTC ISO 8601 strings. `url` links to the original announcement;
`source_url` identifies the tracker supplying the data.
`announced_at` is the official signal post's timestamp. `expected_at` is the
announced target time or window end, when supplied, as a UTC ISO 8601 string.
`window_label` preserves the tracker's human-readable window.
For confirmations, `effective_at` is the timeline's `effective_at`, falling back to
`observed_at` when supplied. It is otherwise `null`; `announced_at` always retains
the source's announcement timestamp. Confirmations carry no forecast probability or
expected window. Use `event.kind` in custom templates to distinguish the two alerts.
Confirmed webhook events also expose a persistent `occurrence_id`, shared by all
posts grouped as the same reset. It is `null` on forecasts and preview samples.
`reset_id` is an optional upstream reset identifier; `scope` defaults to `"global"`.
`effective_at_source` distinguishes an explicit `"effective_at"` from an
`"observed_at"` fallback, and is `null` if the source or a legacy payload did not
provide that provenance. An observation time alone is not an exact reset identity.

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
  "kind": {{ event.kind | tojson }},
  "announcement": {{ event.url | tojson }},
  "source": {{ event.source_url | tojson }},
  "probability": {{ event.probability | tojson }},
  "probability_24h": {{ event.probability_24h | tojson }},
  "probability_48h": {{ event.probability_48h | tojson }},
  "expected_at": {{ event.expected_at | tojson }},
  "effective_at": {{ event.effective_at | tojson }},
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
    "title": {{ (
      {"forecast": "🔄 ", "confirmed": "✅ "}.get(event.kind, "")
      ~ "Codex reset " ~ event.kind
    ) | tojson }},
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

Each stream invokes providers sequentially in configured order. The first successful
HTTP fetch and parse ends that stream's poll, even if there are no qualifying events
or no change. Errors advance to the next eligible provider. Both streams poll
immediately at startup, then follow independent fixed cadences: forecasts every
`interval_seconds` (600 by default) and confirmations every
`confirmation_interval_seconds` (120 by default). Runs remain sequential without
overlap; slow requests skip missed ticks independently. Delivery retries have their
own deadlines and do not trigger additional provider requests.

The built-in `codex_reset` provider reads
[codex-reset.com's forecast endpoint](https://codex-reset.com/api/forecast) and
[timeline endpoint](https://codex-reset.com/api/timeline) on their respective
schedules. Confirmation ticks request only the timeline; forecast ticks request
only the forecast endpoint. Each successful stream commits its own cache changes
and deliveries atomically. A failure or cooldown in one stream does not prevent
the other stream from collecting or delivering events.
It reads the top-level `official_signal` object, which does not require the
feed endpoint's `active` field.
The provider maps `tweet_id` to the event ID, `at` to `announced_at`, and carries
the signal's `summary`, original `url`, probabilities, and announced window
into the notification. Missing, null, empty, or explicitly inactive official signals
are successful empty results. A signal is skipped if a reset has occurred at or after
its announcement, or if its announced window has expired. Persisted confirmation
occurrences also suppress fulfilled forecasts when the forecast snapshot has not
refreshed yet, without an extra timeline request. Confirmations run first when both
streams are due, so this check includes any newly observed reset.
Malformed official signals or invalid probability values fail that provider,
allowing the next provider to run.
Cadence estimates alone, without an official signal, do not trigger notifications.
The timeline supplies confirmed resets using the
[documented real-reset condition](https://codex-reset.com/developers):
`group = "reset"` and `announcement_state = "announced"`. Missing confirmation
metadata is skipped. Banked grants, previews, and events explicitly scoped to a
subset of users are excluded. Future announcement or effect/observation timestamps
are skipped until due. When provided, the effect/observation time orders confirmed
events and advances their watermark; otherwise the announcement timestamp is used.
The provider does not infer a reset from a forecast's probability, deadline, or
an undocumented `confirmed` flag.
Provider HTTP 429 responses create a persisted cooldown for that stream of at least
60 seconds, respecting longer Retry-After values. Existing whole-provider cooldowns
from earlier versions are honored until they expire.

Events use normalized original announcement URLs and their kind to deduplicate across
providers. The same post can generate one forecast and one later confirmation,
including when its legacy commitment alert was already sent. Confirmations previously
cached by the original timeline provider are preserved without duplicate notifications.
Confirmed resets are additionally grouped into persistent occurrences:

- A matching upstream reset ID from the same tracker and scope takes precedence.
  An identical explicit effect time and scope also matches, unless explicit IDs
  from that tracker conflict. The current API does not supply a shared reset ID;
  optional `reset_id` metadata is supported when provided by a source.
- Otherwise, confirmations with compatible evidence and the same scope join the
  nearest occurrence within `confirmation_dedup_window_seconds` of its anchor.
  Distinct explicit IDs or distinct explicit effect times keep resets separate.
  With time-window grouping disabled, matching observation timestamps alone do not
  merge posts.
- The anchor is the first known confirmation's effect/observation/announcement
  time. It stays fixed, including when a late source predates it. The comparison
  uses the absolute distance from that anchor. Supporting posts never extend the
  window or advance its watermark, preventing a chain of posts from indefinitely
  merging later resets.
- All posts and their original payloads are retained in `events`, with associations
  in `reset_sources` and stable groups in `reset_occurrences`. Each endpoint gets
  one queued delivery per occurrence. Supporting sources discovered in a later poll
  or after restart are recorded without resending or changing the queued message.
  The notification links to the group's first known post.

Grouping by time is a heuristic: two genuine resets inside the configured window
can merge when no conflicting explicit evidence is available, and a late follow-up
outside it can be treated as a separate reset. Adjust or disable the window to
choose that tradeoff; fully reliable grouping requires an upstream occurrence ID.
Forecasts retain their independent post identities and probability snapshots.

Unseen occurrences (or forecast posts) at or beyond their stream's timestamp watermark qualify;
older history and previously recorded announcements do not replay.
Equal timestamps can contain distinct new events, and the watermark never moves
backward. Summary edits, probability changes, and refreshed calculation timestamps
alone do not send another alert.
SQLite schema version 2 automatically groups existing confirmation records using
the configured window when upgrading. Previously delivered notifications and all
attempt history remain recorded. Pending duplicates of the same occurrence and
endpoint are marked with `superseded_by` and excluded from dispatch; if any duplicate
was already delivered, none of the others is sent. Otherwise the oldest queued
delivery retains its retry state. Existing pauses, cooldowns, and watermarks remain.
Changing the window afterward affects new associations and does not regroup saved
occurrences or replay past notifications. No database deletion is needed.
Forecasts and confirmations use separate initialization states and cache watermarks,
independent of the former timeline and feed providers. A newer forecast cannot
suppress an older reset confirmation. On upgrade, the confirmed stream sends only
its latest reset, then catches up with each newly discovered reset on later polls.
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
Forecast providers should set `cache_namespace = "forecast"` to share its
initialization state and watermark. For independent confirmation polling, also
implement the optional `ConfirmationProvider` method
`fetch_confirmations(httpx.Client) -> list[ResetEvent]`, returning confirmed events.
The confirmation schedule skips providers without that method and uses the shared
`"confirmed"` namespace. Existing providers implementing only `fetch` continue on
the original `interval_seconds` schedule. Each stream initializes valid empty
snapshots and queues deliveries in the same transaction as its watermark update.

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
the variables referenced in the configuration. `once` polls both streams once
(confirmations first), then drains
currently due deliveries without waiting for future retry deadlines.
It exits 0 when both streams succeed and no configured deliveries remain pending,
1 when either stream's providers are unavailable, deliveries remain pending, or
storage fails, and 2 for
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
the transition from forecast to confirmation for the same post, and no replay,
including multiple supporting posts arriving together and after process restart,
without contacting an actual reset site or messaging service.
It also verifies separate endpoint polling schedules in a fresh process using a
virtual clock, and advances the saved retry deadline to avoid a 30-second wait.
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
