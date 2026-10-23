from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
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
    kind: Literal["confirmed", "committed"] = "confirmed"

    @property
    def identity(self) -> str:
        """Original announcement URLs identify the same event across providers."""
        parts = urlsplit(self.url)
        hostname = parts.netloc.lower()
        if hostname in {"twitter.com", "www.twitter.com", "www.x.com"}:
            hostname = "x.com"
        return urlunsplit(("https", hostname, parts.path.rstrip("/"), "", ""))

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "announced_at": timestamp_text(self.announced_at),
            "summary": self.summary,
            "url": self.url,
            "provider": self.provider,
            "source_url": self.source_url,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, value: dict[str, str]) -> ResetEvent:
        return cls(**{**value, "announced_at": parse_timestamp(value["announced_at"])})


def sample_event() -> ResetEvent:
    return ResetEvent(
        id="sample-reset",
        announced_at=datetime(2026, 1, 1, 12, tzinfo=UTC),
        summary="An upcoming global Codex reset has been committed.",
        url="https://example.com/announcements/sample-reset",
        provider="codex_reset",
        source_url="https://codex-reset.com/",
        kind="committed",
    )
