import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from reset_tracker.providers import CodexResetProvider, ProviderError

FORECAST = json.loads(
    (Path(__file__).parent / "fixtures/forecast.json").read_text(encoding="utf-8")
)
NOW = datetime(2026, 1, 2, 13, tzinfo=UTC)


def test_future_official_signal_carries_distinct_probabilities():
    (event,) = CodexResetProvider.parse(FORECAST, now=NOW)
    signal = FORECAST["official_signal"]
    assert event.id == signal["tweet_id"]
    assert event.kind == "forecast"
    assert event.summary == signal["summary"]
    assert event.url == signal["url"]
    assert event.announced_at.isoformat() == "2026-01-02T12:30:00+00:00"
    assert event.probability == 93
    assert event.probability_24h == 32
    assert event.probability_48h == 53
    assert event.window_label == signal["window"]["label"]
    assert event.expected_at == datetime(2026, 1, 2, 17, tzinfo=UTC)
    assert event.to_dict()["expected_at"] == "2026-01-02T17:00:00.000000Z"
    assert event.source_url == "https://codex-reset.com/"


def test_fetch_uses_forecast_endpoint_and_identifies_reader():
    paths = []

    def handle(request):
        paths.append(request.url.path)
        assert request.headers["User-Agent"] == "reader/1.0 (+mailto:test@example.com)"
        return httpx.Response(
            200, json=FORECAST if request.url.path == "/api/forecast" else {"events": []}
        )

    with httpx.Client(
        transport=httpx.MockTransport(handle),
        headers={"User-Agent": "reader/1.0 (+mailto:test@example.com)"},
    ) as client:
        (event,) = CodexResetProvider(clock=lambda: NOW).fetch(client)
    assert event.probability == 93
    assert paths == ["/api/forecast"]


@pytest.mark.parametrize(
    "payload",
    [
        [],
        None,
        {"official_signal": []},
        {"official_signal": "invalid"},
        {"official_signal": 42},
        {"official_signal": {"score": {"value": 93}}},
    ],
)
def test_malformed_payload_is_provider_failure(payload):
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
        ) as client,
        pytest.raises(ProviderError, match="invalid forecast response"),
    ):
        CodexResetProvider(clock=lambda: NOW).fetch(client)


@pytest.mark.parametrize(
    "signal",
    [
        None,
        {},
        {**FORECAST["official_signal"], "active": False},
    ],
)
def test_missing_or_inactive_official_signals_are_valid_empty_results(signal):
    payload = {**FORECAST, "official_signal": signal}
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json=payload if request.url.path == "/api/forecast" else {"events": []}
            )
        )
    ) as client:
        assert CodexResetProvider(clock=lambda: NOW).fetch(client) == []


def test_model_probabilities_and_old_feed_signal_do_not_trigger_without_official_signal():
    payload = {key: value for key, value in FORECAST.items() if key != "official_signal"}
    payload["signal"] = {"active": True, "kind": "signal"}
    assert CodexResetProvider.parse(payload, now=NOW) == []
    assert CodexResetProvider.parse({}, now=NOW) == []


@pytest.mark.parametrize(
    "deadline",
    [
        "2026-01-02T12:59:59Z",
        "2026-01-02T13:00:00Z",
    ],
)
def test_expired_official_signal_is_ignored(deadline):
    payload = copy.deepcopy(FORECAST)
    payload["official_signal"]["window"]["end_at"] = deadline
    assert CodexResetProvider.parse(payload, now=NOW) == []


def test_target_time_expires_signal_without_window_end():
    payload = copy.deepcopy(FORECAST)
    payload["official_signal"]["window"] = {"target_at": "2026-01-02T12:00:00Z"}
    assert CodexResetProvider.parse(payload, now=NOW) == []


@pytest.mark.parametrize(
    "last_reset",
    [
        "2026-01-02T12:30:00Z",
        "2026-01-02T12:45:00Z",
    ],
)
def test_already_fulfilled_signal_is_ignored_even_with_a_future_deadline(last_reset):
    assert CodexResetProvider.parse({**FORECAST, "last_reset_at": last_reset}, now=NOW) == []


def test_signal_without_deadline_remains_usable():
    payload = copy.deepcopy(FORECAST)
    del payload["official_signal"]["window"]
    (event,) = CodexResetProvider.parse(payload, now=NOW)
    assert event.expected_at is None
    assert event.window_label is None
    assert event.probability == 93


def test_window_end_is_used_when_target_time_is_missing():
    payload = copy.deepcopy(FORECAST)
    del payload["official_signal"]["window"]["target_at"]
    (event,) = CodexResetProvider.parse(payload, now=NOW)
    assert event.expected_at == datetime(2026, 1, 2, 17, tzinfo=UTC)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tweet_id", None),
        ("tweet_id", 123),
        ("tweet_id", ""),
        ("summary", None),
        ("summary", ""),
        ("at", None),
        ("at", "yesterday"),
        ("at", "2026-01-02T12:30:00"),
        ("url", None),
        ("url", "javascript:alert(1)"),
        ("window", []),
        ("window", {"label": 42}),
        ("window", {"target_at": "not a time"}),
    ],
)
def test_invalid_official_signal_is_provider_failure(field, value):
    payload = {**FORECAST, "official_signal": {**FORECAST["official_signal"], field: value}}
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
        ) as client,
        pytest.raises(ProviderError, match="invalid forecast response"),
    ):
        CodexResetProvider(clock=lambda: NOW).fetch(client)


@pytest.mark.parametrize("field", ["signal_percent", "rounded_24h", "rounded_48h"])
@pytest.mark.parametrize("value", [-1, 101, True, "93", float("nan"), float("inf")])
def test_probabilities_must_be_finite_numeric_percentages(field, value):
    payload = {**FORECAST, "probabilities": {**FORECAST["probabilities"], field: value}}
    with pytest.raises(ValueError, match="invalid probability"):
        CodexResetProvider.parse(payload, now=NOW)


def test_signal_score_is_used_without_signal_percent():
    payload = copy.deepcopy(FORECAST)
    del payload["probabilities"]["signal_percent"]
    (event,) = CodexResetProvider.parse(payload, now=NOW)
    assert event.probability == 93


def test_fractional_commitment_is_converted_to_percentage():
    payload = copy.deepcopy(FORECAST)
    del payload["probabilities"]["signal_percent"]
    del payload["official_signal"]["score"]
    (event,) = CodexResetProvider.parse(payload, now=NOW)
    assert event.probability == 93


def test_missing_probability_is_unknown_instead_of_a_model_probability():
    payload = copy.deepcopy(FORECAST)
    del payload["probabilities"]["signal_percent"]
    del payload["probabilities"]["commitment"]
    del payload["official_signal"]["score"]
    (event,) = CodexResetProvider.parse(payload, now=NOW)
    assert event.probability is None
    assert event.probability_24h == 32
    assert event.probability_48h == 53


@pytest.mark.parametrize("probability", [0, 93.5, 100])
def test_zero_and_fractional_percentages_are_preserved(probability):
    payload = {
        **FORECAST,
        "probabilities": {**FORECAST["probabilities"], "signal_percent": probability},
    }
    (event,) = CodexResetProvider.parse(payload, now=NOW)
    assert event.probability == probability


def test_refresh_and_probability_changes_do_not_change_announcement_identity():
    (old,) = CodexResetProvider.parse(FORECAST, now=NOW)
    payload = {
        **FORECAST,
        "updated_at": "2026-01-02T13:10:00Z",
        "probabilities": {**FORECAST["probabilities"], "signal_percent": 94},
    }
    (new,) = CodexResetProvider.parse(payload, now=NOW)
    assert new.identity == old.identity
    assert new.announced_at == old.announced_at
    assert new.probability == 94


def test_invalid_json_is_provider_failure():
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"not json"))
        ) as client,
        pytest.raises(ProviderError),
    ):
        CodexResetProvider(clock=lambda: NOW).fetch(client)
