from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from uuid import uuid4

from .models import ResetEvent, parse_timestamp, timestamp_text

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    identity TEXT PRIMARY KEY,
    announced_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reset_occurrences (
    id TEXT PRIMARY KEY,
    anchor_at TEXT NOT NULL,
    scope TEXT NOT NULL,
    representative_identity TEXT NOT NULL REFERENCES events(identity),
    reset_id TEXT,
    reset_source TEXT,
    effective_at TEXT
);
CREATE INDEX IF NOT EXISTS occurrence_scope ON reset_occurrences(scope, anchor_at);
CREATE TABLE IF NOT EXISTS reset_sources (
    event_identity TEXT PRIMARY KEY REFERENCES events(identity),
    occurrence_id TEXT NOT NULL REFERENCES reset_occurrences(id)
);
CREATE TABLE IF NOT EXISTS provider_state (
    provider TEXT PRIMARY KEY,
    cooldown_until REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS endpoint_state (
    webhook_id TEXT PRIMARY KEY,
    paused INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    cooldown_until REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY,
    event_identity TEXT NOT NULL REFERENCES events(identity),
    webhook_id TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'delivered')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    occurrence_id TEXT REFERENCES reset_occurrences(id),
    superseded_by INTEGER REFERENCES deliveries(id),
    UNIQUE(event_identity, webhook_id)
);
CREATE INDEX IF NOT EXISTS pending_deliveries ON deliveries(webhook_id, status);
"""


@dataclass(frozen=True)
class PendingDelivery:
    id: int
    webhook_id: str
    event: ResetEvent
    detected_at: str
    attempts: int
    due_at: float


@dataclass(frozen=True)
class _Discovery:
    identity: str
    timestamp: str
    created: bool
    occurrence_id: str | None = None


class Store:
    def __init__(self, path: Path, *, confirmation_dedup_window_seconds: int = 1800):
        self.confirmation_dedup_window_seconds = confirmation_dedup_window_seconds
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), timeout=5)
        self.connection.row_factory = sqlite3.Row
        try:
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise sqlite3.DatabaseError("unsupported database version")
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA journal_mode = WAL")
            self.connection.execute("PRAGMA synchronous = FULL")
            self.connection.executescript(_SCHEMA)
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE")
                if version < 2:
                    self._migrate_occurrences()
                self.connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS occurrence_delivery "
                    "ON deliveries(occurrence_id, webhook_id) "
                    "WHERE occurrence_id IS NOT NULL AND superseded_by IS NULL"
                )
                self.connection.execute("PRAGMA user_version = 2")
        except BaseException:
            self.connection.close()
            raise

    def __enter__(self) -> Store:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _migrate_occurrences(self) -> None:
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(deliveries)")}
        if "occurrence_id" not in columns:
            self.connection.execute(
                "ALTER TABLE deliveries ADD COLUMN occurrence_id TEXT "
                "REFERENCES reset_occurrences(id)"
            )
        if "superseded_by" not in columns:
            self.connection.execute(
                "ALTER TABLE deliveries ADD COLUMN superseded_by INTEGER REFERENCES deliveries(id)"
            )
        rows = self.connection.execute(
            "SELECT identity, payload FROM events ORDER BY announced_at, identity"
        ).fetchall()
        for row in rows:
            event = ResetEvent.from_dict(json.loads(row["payload"]))
            if event.kind == "confirmed":
                self._associate_confirmation(row["identity"], event)
        self.connection.execute(
            "UPDATE deliveries SET occurrence_id = "
            "(SELECT occurrence_id FROM reset_sources "
            "WHERE event_identity = deliveries.event_identity)"
        )
        duplicates = self.connection.execute(
            "SELECT occurrence_id, webhook_id FROM deliveries WHERE occurrence_id IS NOT NULL "
            "GROUP BY occurrence_id, webhook_id HAVING COUNT(*) > 1"
        ).fetchall()
        for group in duplicates:
            winner = self.connection.execute(
                "SELECT id FROM deliveries WHERE occurrence_id = ? AND webhook_id = ? "
                "ORDER BY (status = 'delivered') DESC, id LIMIT 1",
                (group["occurrence_id"], group["webhook_id"]),
            ).fetchone()["id"]
            # Keep all historical attempts and accepted messages. Duplicate rows
            # remain available for audit but no longer participate in dispatch.
            self.connection.execute(
                "UPDATE deliveries SET superseded_by = ? "
                "WHERE occurrence_id = ? AND webhook_id = ? AND id != ?",
                (winner, group["occurrence_id"], group["webhook_id"], winner),
            )

    @staticmethod
    def _exact_effect_time(event: ResetEvent) -> str | None:
        if event.effective_at is not None and event.effective_at_source == "effective_at":
            return timestamp_text(event.effective_at)
        return None

    def _associate_confirmation(self, identity: str, event: ResetEvent) -> tuple[sqlite3.Row, bool]:
        occurrence = self.connection.execute(
            "SELECT o.* FROM reset_occurrences o JOIN reset_sources s ON o.id = s.occurrence_id "
            "WHERE s.event_identity IN (?, ?) LIMIT 1",
            (event.identity, event.identity.removeprefix("confirmed:")),
        ).fetchone()
        if occurrence is None:
            occurrence = self._match_occurrence(event)
        created = occurrence is None
        exact_effect = self._exact_effect_time(event)
        if created:
            occurrence_id = "reset-" + uuid4().hex
            self.connection.execute(
                "INSERT INTO reset_occurrences"
                "(id, anchor_at, scope, representative_identity, "
                "reset_id, reset_source, effective_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    occurrence_id,
                    timestamp_text(event.recorded_at),
                    event.scope,
                    identity,
                    event.reset_id,
                    event.source_url if event.reset_id else None,
                    exact_effect,
                ),
            )
        else:
            occurrence_id = occurrence["id"]
            # Strong evidence can enrich an existing group without moving its anchor.
            self.connection.execute(
                "UPDATE reset_occurrences SET "
                "reset_source = CASE WHEN reset_id IS NULL AND ? IS NOT NULL "
                "THEN ? ELSE reset_source END, reset_id = COALESCE(reset_id, ?), "
                "effective_at = COALESCE(effective_at, ?) WHERE id = ?",
                (event.reset_id, event.source_url, event.reset_id, exact_effect, occurrence_id),
            )
        self.connection.execute(
            "INSERT OR IGNORE INTO reset_sources(event_identity, occurrence_id) VALUES (?, ?)",
            (identity, occurrence_id),
        )
        return self.connection.execute(
            "SELECT * FROM reset_occurrences WHERE id = ?", (occurrence_id,)
        ).fetchone(), created

    def _match_occurrence(self, event: ResetEvent) -> sqlite3.Row | None:
        rows = self.connection.execute(
            "SELECT * FROM reset_occurrences WHERE scope = ? ORDER BY anchor_at, id",
            (event.scope,),
        ).fetchall()
        if event.reset_id is not None:
            for row in rows:
                if row["reset_id"] == event.reset_id and row["reset_source"] == event.source_url:
                    return row
        exact_effect = self._exact_effect_time(event)
        compatible = [
            row
            for row in rows
            if not (
                event.reset_id is not None
                and row["reset_id"] is not None
                and row["reset_source"] == event.source_url
                and row["reset_id"] != event.reset_id
            )
            and not (
                exact_effect is not None
                and row["effective_at"] is not None
                and row["effective_at"] != exact_effect
            )
        ]
        if exact_effect is not None:
            for row in compatible:
                if row["effective_at"] == exact_effect:
                    return row
        if self.confirmation_dedup_window_seconds == 0:
            return None
        nearby = [
            (abs((event.recorded_at - parse_timestamp(row["anchor_at"])).total_seconds()), row)
            for row in compatible
        ]
        nearby = [item for item in nearby if item[0] <= self.confirmation_dedup_window_seconds]
        return (
            min(nearby, key=lambda item: (item[0], item[1]["anchor_at"], item[1]["id"]))[1]
            if nearby
            else None
        )

    def _metadata(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_metadata(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def record_snapshot(
        self,
        events: list[ResetEvent],
        webhook_ids: list[str],
        now: float,
        *,
        cache_namespace: str | None = None,
    ) -> int:
        return self.record_snapshots({cache_namespace: events}, webhook_ids, now)

    def record_snapshots(
        self,
        snapshots: dict[str | None, list[ResetEvent]],
        webhook_ids: list[str],
        now: float,
    ) -> int:
        """Commit all streams and their deliveries together, with separate watermarks."""
        with self.connection:
            return sum(
                self._record_snapshot(events, webhook_ids, now, namespace)
                for namespace, events in snapshots.items()
            )

    def _record_snapshot(
        self,
        events: list[ResetEvent],
        webhook_ids: list[str],
        now: float,
        cache_namespace: str | None,
    ) -> int:
        # Freeze one snapshot before writing: duplicates cannot enqueue two deliveries.
        by_identity = {}
        for event in sorted(events, key=lambda item: (item.recorded_at, item.identity)):
            by_identity[event.identity] = event
        snapshot = sorted(by_identity.values(), key=lambda item: (item.recorded_at, item.identity))
        detected_at = timestamp_text(datetime.fromtimestamp(now, UTC))
        queued = 0
        prefix = f"{cache_namespace}:" if cache_namespace else ""
        initialized = self._metadata(prefix + "initialized") == "1"
        watermark = self._metadata(prefix + "watermark")
        discoveries = {}
        for event in snapshot:
            event_time = timestamp_text(event.recorded_at)
            identity = event.identity
            if event.kind == "confirmed":
                legacy = self.connection.execute(
                    "SELECT payload FROM events WHERE identity = ?",
                    (event.identity.removeprefix("confirmed:"),),
                ).fetchone()
                # Preserve deliveries made by the original timeline provider, but
                # a legacy commitment for the same post must not hide confirmation.
                if legacy and json.loads(legacy["payload"]).get("kind", "confirmed") == "confirmed":
                    identity = event.identity.removeprefix("confirmed:")
            inserted = self.connection.execute(
                # The historical column name holds the stream's ordering timestamp;
                # the payload retains the original announcement and effect times.
                "INSERT OR IGNORE INTO events(identity, announced_at, payload) VALUES (?, ?, ?)",
                (identity, event_time, json.dumps(event.to_dict(), ensure_ascii=False)),
            ).rowcount
            if event.kind == "confirmed":
                occurrence, created = self._associate_confirmation(identity, event)
                key = occurrence["id"]
                discovery = _Discovery(
                    occurrence["representative_identity"], occurrence["anchor_at"], created, key
                )
            else:
                key = identity
                discovery = _Discovery(identity, event_time, bool(inserted))
            if key not in discoveries or discovery.created:
                discoveries[key] = discovery
        ordered = sorted(discoveries.values(), key=lambda item: (item.timestamp, item.identity))
        for index, discovery in enumerate(ordered):
            should_notify = (
                index == len(ordered) - 1
                if not initialized
                else watermark is None or discovery.timestamp >= watermark
            )
            if discovery.created and should_notify:
                for webhook_id in webhook_ids:
                    queued += self.connection.execute(
                        "INSERT OR IGNORE INTO deliveries"
                        "(event_identity, webhook_id, detected_at, next_attempt_at, occurrence_id) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (discovery.identity, webhook_id, detected_at, now, discovery.occurrence_id),
                    ).rowcount
        if ordered:
            latest = ordered[-1].timestamp
            if watermark is None or latest > watermark:
                self._set_metadata(prefix + "watermark", latest)
        self._set_metadata(prefix + "initialized", "1")
        return queued

    def provider_ready(self, provider: str, now: float) -> bool:
        row = self.connection.execute(
            "SELECT cooldown_until FROM provider_state WHERE provider = ?", (provider,)
        ).fetchone()
        return row is None or row["cooldown_until"] <= now

    def cool_provider(self, provider: str, until: float) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO provider_state(provider, cooldown_until) VALUES (?, ?) "
                "ON CONFLICT(provider) DO UPDATE SET "
                "cooldown_until = MAX(provider_state.cooldown_until, excluded.cooldown_until)",
                (provider, until),
            )

    def heads(self, webhook_ids: list[str]) -> list[PendingDelivery]:
        """Only the oldest pending event per active endpoint can be attempted."""
        result = []
        for webhook_id in webhook_ids:
            endpoint = self.connection.execute(
                "SELECT paused, cooldown_until FROM endpoint_state WHERE webhook_id = ?",
                (webhook_id,),
            ).fetchone()
            if endpoint is not None and endpoint["paused"]:
                continue
            row = self.connection.execute(
                "SELECT d.*, e.payload FROM deliveries d "
                "JOIN events e ON e.identity = d.event_identity "
                "WHERE d.webhook_id = ? AND d.status = 'pending' AND d.superseded_by IS NULL "
                "ORDER BY e.announced_at, d.id LIMIT 1",
                (webhook_id,),
            ).fetchone()
            if row is not None:
                result.append(
                    PendingDelivery(
                        id=row["id"],
                        webhook_id=webhook_id,
                        event=replace(
                            ResetEvent.from_dict(json.loads(row["payload"])),
                            occurrence_id=row["occurrence_id"],
                        ),
                        detected_at=row["detected_at"],
                        attempts=row["attempts"],
                        due_at=max(
                            row["next_attempt_at"], endpoint["cooldown_until"] if endpoint else 0
                        ),
                    )
                )
        return result

    def delivered(self, delivery_id: int, webhook_id: str, cooldown_until: float = 0) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE deliveries SET status = 'delivered', attempts = attempts + 1, "
                "last_error = NULL WHERE id = ?",
                (delivery_id,),
            )
            self._endpoint(webhook_id, False, None, cooldown_until)

    def failed(
        self, delivery_id: int, webhook_id: str, reason: str, retry_at: float | None
    ) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE deliveries SET attempts = attempts + 1, last_error = ?, "
                "next_attempt_at = ? WHERE id = ?",
                (reason, retry_at or 0, delivery_id),
            )
            self._endpoint(webhook_id, retry_at is None, reason, retry_at or 0)

    def _endpoint(
        self, webhook_id: str, paused: bool, reason: str | None, cooldown_until: float
    ) -> None:
        self.connection.execute(
            "INSERT INTO endpoint_state(webhook_id, paused, reason, cooldown_until) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(webhook_id) DO UPDATE SET "
            "paused = excluded.paused, reason = excluded.reason, "
            "cooldown_until = excluded.cooldown_until",
            (webhook_id, int(paused), reason, cooldown_until),
        )

    def retry_failed(self, webhook_ids: list[str], now: float) -> int:
        resumed = 0
        with self.connection:
            for webhook_id in webhook_ids:
                changed = self.connection.execute(
                    "UPDATE endpoint_state SET paused = 0, reason = NULL, cooldown_until = 0 "
                    "WHERE webhook_id = ? AND paused = 1",
                    (webhook_id,),
                ).rowcount
                if changed:
                    self.connection.execute(
                        "UPDATE deliveries SET next_attempt_at = ? "
                        "WHERE webhook_id = ? AND status = 'pending' AND superseded_by IS NULL",
                        (now, webhook_id),
                    )
                    resumed += 1
        return resumed

    def pending_count(self, webhook_ids: list[str]) -> int:
        return sum(
            self.connection.execute(
                "SELECT COUNT(*) FROM deliveries WHERE webhook_id = ? "
                "AND status = 'pending' AND superseded_by IS NULL",
                (webhook_id,),
            ).fetchone()[0]
            for webhook_id in webhook_ids
        )
