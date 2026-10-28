from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit


def parse_timestamp(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return timestamp.astimezone(UTC)


def timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def http_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("expected an absolute HTTP(S) URL")
    if any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise ValueError("URL contains whitespace or control characters")
    # Accessing port also checks malformed ports.
    _ = parts.port
    return value


@dataclass(frozen=True)
class ResetEvent:
    id: str
    announced_at: datetime
    summary: str
    url: str
    provider: str
    source_url: str
    kind: Literal["confirmed", "committed", "forecast"] = "confirmed"
    probability: int | float | None = None
    probability_24h: int | float | None = None
    probability_48h: int | float | None = None
    window_label: str | None = None
    expected_at: datetime | None = None
    effective_at: datetime | None = None
    effective_at_source: Literal["effective_at", "observed_at"] | None = None
    reset_id: str | None = None
    scope: str = "global"
    occurrence_id: str | None = None

    @property
    def identity(self) -> str:
        """Original announcement URLs identify the same event across providers."""
        parts = urlsplit(self.url)
        hostname = parts.netloc.lower()
        if hostname in {"twitter.com", "www.twitter.com", "www.x.com"}:
            hostname = "x.com"
        identity = urlunsplit(("https", hostname, parts.path.rstrip("/"), "", ""))
        # A source post may produce both a forecast and a later confirmation.
        # Keep both distinct from legacy commitment-only notifications as well.
        return f"{self.kind}:{identity}" if self.kind in {"forecast", "confirmed"} else identity

    @property
    def recorded_at(self) -> datetime:
        """Order confirmed resets by their effect/observation time when available."""
        if self.kind == "confirmed":
            return self.effective_at or self.announced_at
        return self.announced_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "announced_at": timestamp_text(self.announced_at),
            "summary": self.summary,
            "url": self.url,
            "provider": self.provider,
            "source_url": self.source_url,
            "kind": self.kind,
            "probability": self.probability,
            "probability_24h": self.probability_24h,
            "probability_48h": self.probability_48h,
            "window_label": self.window_label,
            "expected_at": timestamp_text(self.expected_at) if self.expected_at else None,
            "effective_at": timestamp_text(self.effective_at) if self.effective_at else None,
            "effective_at_source": self.effective_at_source,
            "reset_id": self.reset_id,
            "scope": self.scope,
            "occurrence_id": self.occurrence_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ResetEvent:
        fields = {**value, "announced_at": parse_timestamp(value["announced_at"])}
        for field in ("expected_at", "effective_at"):
            if fields.get(field) is not None:
                fields[field] = parse_timestamp(fields[field])
        return cls(**fields)


def sample_event() -> ResetEvent:
    return ResetEvent(
        id="sample-reset",
        announced_at=datetime(2026, 1, 1, 12, tzinfo=UTC),
        summary="An official signal forecasts an upcoming global Codex reset.",
        url="https://example.com/announcements/sample-reset",
        provider="codex_reset",
        source_url="https://codex-reset.com/",
        kind="forecast",
        probability=93,
        probability_24h=32,
        probability_48h=53,
        window_label="by 12 PM UTC on Jan 2",
        expected_at=datetime(2026, 1, 2, 12, tzinfo=UTC),
    )
