import json
from pathlib import Path

import httpx
import pytest

from reset_tracker.providers import CodexResetProvider, ProviderError

FEED = json.loads((Path(__file__).parent / "fixtures/feed.json").read_text(encoding="utf-8"))


def test_active_commitment_signal_is_used_instead_of_completed_events():
    events = CodexResetProvider.parse(FEED)
    assert len(events) == 1
    assert events[0].id == FEED["signal"]["tweet_id"]
    assert events[0].kind == "committed"
    assert events[0].summary == FEED["signal"]["summary"]
    assert events[0].url == FEED["signal"]["url"]
    assert events[0].announced_at.isoformat() == "2026-01-02T12:30:00+00:00"
    assert events[0].source_url == "https://codex-reset.com/"


def test_fetch_identifies_reader():
    def handle(request):
        assert request.url.path == "/api/feed"
        assert request.headers["User-Agent"] == "reader/1.0 (+mailto:test@example.com)"
        return httpx.Response(200, json=FEED)

    with httpx.Client(
        transport=httpx.MockTransport(handle),
        headers={"User-Agent": "reader/1.0 (+mailto:test@example.com)"},
    ) as client:
        assert len(CodexResetProvider().fetch(client)) == 1


@pytest.mark.parametrize(
    "payload",
    [
        [],
        None,
        {"signal": []},
        {"signal": "invalid"},
        {"signal": 42},
        {"signal": {"active": True, "kind": "signal"}},
    ],
)
def test_malformed_payload_is_provider_failure(payload):
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
        ) as client,
        pytest.raises(ProviderError),
    ):
        CodexResetProvider().fetch(client)


@pytest.mark.parametrize(
    "signal",
    [
        None,
        {},
        {**FEED["signal"], "active": False},
        {**FEED["signal"], "active": None},
        {**FEED["signal"], "active": "true"},
        {**FEED["signal"], "kind": "forecast"},
        {**FEED["signal"], "kind": "reset"},
        {**FEED["signal"], "kind": "banked"},
    ],
)
def test_inactive_missing_or_unrelated_signals_are_valid_empty_results(signal):
    payload = {**FEED, "signal": signal}
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        events = CodexResetProvider().fetch(client)
    assert events == []


def test_missing_signal_is_valid_and_completed_events_are_ignored():
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={key: value for key, value in FEED.items() if key != "signal"}
            )
        )
    ) as client:
        assert CodexResetProvider().fetch(client) == []


def test_invalid_json_is_provider_failure():
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"not json"))
        ) as client,
        pytest.raises(ProviderError),
    ):
        CodexResetProvider().fetch(client)


@pytest.mark.parametrize("timestamp", ["2026-01-01T12:00:00", "yesterday"])
def test_invalid_timestamp_rejected(timestamp):
    signal = {**FEED["signal"], "at": timestamp}
    with pytest.raises(ValueError):
        CodexResetProvider.parse({"signal": signal})


def test_empty_results_are_valid():
    assert CodexResetProvider.parse({}) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tweet_id", None),
        ("tweet_id", 123),
        ("tweet_id", ""),
        ("summary", None),
        ("summary", ""),
        ("at", None),
        ("url", None),
        ("url", "javascript:alert(1)"),
    ],
)
def test_invalid_active_signal_is_provider_failure(field, value):
    payload = {**FEED, "signal": {**FEED["signal"], field: value}}
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
        ) as client,
        pytest.raises(ProviderError, match="invalid feed response"),
    ):
        CodexResetProvider().fetch(client)


def test_feed_refresh_does_not_change_event_identity_or_announcement_time():
    refreshed = {**FEED, "fetched_at": "2026-01-03T13:00:00Z"}
    assert CodexResetProvider.parse(FEED) == CodexResetProvider.parse(refreshed)
