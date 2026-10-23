from collections.abc import Callable
from typing import Protocol

import httpx

from .models import ResetEvent, http_url, parse_timestamp


class ProviderError(Exception):
    """A provider could not return a usable snapshot."""


class Provider(Protocol):
    name: str

    def fetch(self, client: httpx.Client) -> list[ResetEvent]: ...


class CodexResetProvider:
    name = "codex_reset"
    endpoint = "https://codex-reset.com/api/feed"
    cache_namespace = "committed"

    def fetch(self, client: httpx.Client) -> list[ResetEvent]:
        response = client.get(self.endpoint)
        response.raise_for_status()
        try:
            return self.parse(response.json())
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise ProviderError("invalid feed response") from exc

    @staticmethod
    def parse(payload: object) -> list[ResetEvent]:
        if not isinstance(payload, dict):
            raise ValueError("expected a feed object")
        signal = payload.get("signal")
        if signal is None:
            return []
        if not isinstance(signal, dict):
            raise ValueError("expected a signal object or null")
        if signal.get("active") is not True or signal.get("kind") != "signal":
            return []
        for field in ("tweet_id", "at", "summary", "url"):
            if not isinstance(signal.get(field), str) or not signal[field]:
                raise ValueError(f"invalid signal field: {field}")
        return [
            ResetEvent(
                id=signal["tweet_id"],
                announced_at=parse_timestamp(signal["at"]),
                summary=signal["summary"],
                url=http_url(signal["url"]),
                provider="codex_reset",
                source_url="https://codex-reset.com/",
                kind="committed",
            )
        ]


# Add a factory here to make another provider available in TOML.
PROVIDERS: dict[str, Callable[[], Provider]] = {"codex_reset": CodexResetProvider}
