from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from .models import ResetEvent, timestamp_text

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    identity TEXT PRIMARY KEY,
    announced_at TEXT NOT NULL,
    payload TEXT NOT NULL
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


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), timeout=5)
        self.connection.row_factory = sqlite3.Row
        try:
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise sqlite3.DatabaseError("unsupported database version")
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA journal_mode = WAL")
            self.connection.execute("PRAGMA synchronous = FULL")
            self.connection.executescript(_SCHEMA)
            self.connection.execute("PRAGMA user_version = 1")
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
        first_latest = snapshot[-1].identity if snapshot else None
        for event in snapshot:
            event_time = timestamp_text(event.recorded_at)
            if event.kind == "confirmed":
                legacy = self.connection.execute(
                    "SELECT payload FROM events WHERE identity = ?",
                    (event.identity.removeprefix("confirmed:"),),
                ).fetchone()
                # Preserve deliveries made by the original timeline provider, but
                # a legacy commitment for the same post must not hide confirmation.
                if legacy and json.loads(legacy["payload"]).get("kind", "confirmed") == "confirmed":
                    continue
            inserted = self.connection.execute(
                # The historical column name holds the stream's ordering timestamp;
                # the payload retains the original announcement and effect times.
                "INSERT OR IGNORE INTO events(identity, announced_at, payload) VALUES (?, ?, ?)",
                (event.identity, event_time, json.dumps(event.to_dict(), ensure_ascii=False)),
            ).rowcount
            should_notify = (
                event.identity == first_latest
                if not initialized
                else watermark is None or event_time >= watermark
            )
            if inserted and should_notify:
                for webhook_id in webhook_ids:
                    queued += self.connection.execute(
                        "INSERT OR IGNORE INTO deliveries"
                        "(event_identity, webhook_id, detected_at, next_attempt_at) "
                        "VALUES (?, ?, ?, ?)",
                        (event.identity, webhook_id, detected_at, now),
                    ).rowcount
        if snapshot:
            latest = timestamp_text(snapshot[-1].recorded_at)
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
                "WHERE d.webhook_id = ? AND d.status = 'pending' "
                "ORDER BY e.announced_at, d.id LIMIT 1",
                (webhook_id,),
            ).fetchone()
            if row is not None:
                result.append(
                    PendingDelivery(
                        id=row["id"],
                        webhook_id=webhook_id,
                        event=ResetEvent.from_dict(json.loads(row["payload"])),
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
                        "WHERE webhook_id = ? AND status = 'pending'",
                        (now, webhook_id),
                    )
                    resumed += 1
        return resumed

    def pending_count(self, webhook_ids: list[str]) -> int:
        return sum(
            self.connection.execute(
                "SELECT COUNT(*) FROM deliveries WHERE webhook_id = ? AND status = 'pending'",
                (webhook_id,),
            ).fetchone()[0]
            for webhook_id in webhook_ids
        )
