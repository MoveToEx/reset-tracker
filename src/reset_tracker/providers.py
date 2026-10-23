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
    endpoint = "https://codex-reset.com/api/timeline"

    def fetch(self, client: httpx.Client) -> list[ResetEvent]:
        response = client.get(self.endpoint)
        response.raise_for_status()
        try:
            return self.parse(response.json())
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise ProviderError("invalid timeline response") from exc

    @staticmethod
    def parse(payload: object) -> list[ResetEvent]:
        if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
            raise ValueError("expected an events array")
        events = []
        for item in payload["events"]:
            if not isinstance(item, dict) or not isinstance(item.get("group"), str):
                raise ValueError("invalid timeline event")
            if item["group"] != "reset":
                continue
            if not isinstance(item.get("announcement_state"), str):
                raise ValueError("missing announcement state")
            if item["announcement_state"] != "announced":
                continue
            for field in ("id", "announced_at", "summary", "url"):
                if not isinstance(item.get(field), str) or not item[field]:
                    raise ValueError("invalid reset field")
            events.append(
                ResetEvent(
                    id=item["id"],
                    announced_at=parse_timestamp(item["announced_at"]),
                    summary=item["summary"],
                    url=http_url(item["url"]),
                    provider="codex_reset",
                    source_url="https://codex-reset.com/",
                )
            )
        return events


# Add a factory here to make another provider available in TOML.
PROVIDERS: dict[str, Callable[[], Provider]] = {"codex_reset": CodexResetProvider}
