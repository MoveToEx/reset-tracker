import math
from collections.abc import Callable
from datetime import UTC, datetime
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
    endpoint = "https://codex-reset.com/api/forecast"
    timeline_endpoint = "https://codex-reset.com/api/timeline"
    cache_namespaces = ("forecast", "confirmed")

    def __init__(self, *, clock: Callable[[], datetime] | None = None):
        self.clock = clock or (lambda: datetime.now(UTC))

    def fetch(self, client: httpx.Client) -> list[ResetEvent]:
        now = self.clock()
        response = client.get(self.endpoint)
        response.raise_for_status()
        try:
            events = self.parse(response.json(), now=now)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise ProviderError("invalid forecast response") from exc
        response = client.get(self.timeline_endpoint)
        response.raise_for_status()
        try:
            confirmed = self.parse_confirmed(response.json(), now=now)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise ProviderError("invalid timeline response") from exc
        # Published snapshots may refresh separately. A newer timeline confirmation
        # also invalidates an already fulfilled forecast from an older snapshot.
        latest_reset = max((event.recorded_at for event in confirmed), default=None)
        return [
            event for event in events if latest_reset is None or event.announced_at > latest_reset
        ] + confirmed

    @staticmethod
    def parse_confirmed(payload: object, *, now: datetime | None = None) -> list[ResetEvent]:
        if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
            raise ValueError("expected an events array")
        now = now if now is not None else datetime.now(UTC)
        events = []
        for item in payload["events"]:
            if not isinstance(item, dict) or not isinstance(item.get("group"), str):
                raise ValueError("invalid timeline event")
            # This is the tracker's documented real-reset condition. Missing state
            # metadata is common on older entries and does not imply confirmation.
            if item["group"] != "reset" or item.get("announcement_state") != "announced":
                continue
            if (
                item.get("banked_grant") is True
                or item.get("preview") is True
                or item.get("scope", "global") != "global"
            ):
                continue
            for field in ("id", "announced_at", "summary", "url"):
                if not isinstance(item.get(field), str) or not item[field]:
                    raise ValueError(f"invalid confirmed reset field: {field}")
            announced_at = parse_timestamp(item["announced_at"])
            effective_at = _optional_timestamp(item.get("effective_at"), "effective_at")
            effective_at_source = "effective_at" if effective_at is not None else None
            if effective_at is None:
                effective_at = _optional_timestamp(item.get("observed_at"), "observed_at")
                if effective_at is not None:
                    effective_at_source = "observed_at"
            reset_id = item.get("reset_id")
            if reset_id is not None and (not isinstance(reset_id, str) or not reset_id.strip()):
                raise ValueError("invalid confirmed reset field: reset_id")
            if announced_at > now or (effective_at is not None and effective_at > now):
                continue
            events.append(
                ResetEvent(
                    id=item["id"],
                    announced_at=announced_at,
                    summary=item["summary"],
                    url=http_url(item["url"]),
                    provider="codex_reset",
                    source_url="https://codex-reset.com/",
                    kind="confirmed",
                    effective_at=effective_at,
                    effective_at_source=effective_at_source,
                    reset_id=reset_id,
                )
            )
        return events

    @staticmethod
    def parse(payload: object, *, now: datetime | None = None) -> list[ResetEvent]:
        if not isinstance(payload, dict):
            raise ValueError("expected a forecast object")
        signal = payload.get("official_signal")
        if signal is None:
            return []
        if not isinstance(signal, dict):
            raise ValueError("expected an official_signal object or null")
        if not signal or signal.get("active") is False:
            return []
        for field in ("tweet_id", "at", "summary", "url"):
            if not isinstance(signal.get(field), str) or not signal[field]:
                raise ValueError(f"invalid official_signal field: {field}")
        announced_at = parse_timestamp(signal["at"])
        last_reset = _optional_timestamp(payload.get("last_reset_at"), "last_reset_at")
        if last_reset is not None and announced_at <= last_reset:
            return []
        window = signal.get("window")
        if window is None:
            window = {}
        if not isinstance(window, dict):
            raise ValueError("invalid official_signal window")
        target = _optional_timestamp(window.get("target_at"), "window.target_at")
        end = _optional_timestamp(window.get("end_at"), "window.end_at")
        deadline = end or target
        if deadline is not None and deadline <= (now if now is not None else datetime.now(UTC)):
            return []
        label = window.get("label")
        if label is not None and not isinstance(label, str):
            raise ValueError("invalid official_signal window label")
        probabilities = payload.get("probabilities")
        if probabilities is None:
            probabilities = {}
        if not isinstance(probabilities, dict):
            raise ValueError("invalid forecast probabilities")
        probability = probabilities.get("signal_percent")
        if probability is None:
            score = signal.get("score")
            probability = score.get("value") if isinstance(score, dict) else score
        if probability is None and probabilities.get("commitment") is not None:
            commitment = _probability(probabilities["commitment"], "commitment", maximum=1)
            probability = round(commitment * 100, 6)
        return [
            ResetEvent(
                id=signal["tweet_id"],
                announced_at=announced_at,
                summary=signal["summary"],
                url=http_url(signal["url"]),
                provider="codex_reset",
                source_url="https://codex-reset.com/",
                kind="forecast",
                probability=_probability(probability, "signal probability"),
                probability_24h=_probability(probabilities.get("rounded_24h"), "rounded_24h"),
                probability_48h=_probability(probabilities.get("rounded_48h"), "rounded_48h"),
                window_label=label,
                expected_at=target or end,
            )
        ]


def _optional_timestamp(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"invalid timestamp field: {field}")
    return parse_timestamp(value)


def _probability(value: object, field: str, *, maximum: int = 100) -> int | float | None:
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= maximum:
        raise ValueError(f"invalid probability field: {field}")
    return value


# Add a factory here to make another provider available in TOML.
PROVIDERS: dict[str, Callable[[], Provider]] = {"codex_reset": CodexResetProvider}
