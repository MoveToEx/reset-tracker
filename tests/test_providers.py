import json
from pathlib import Path

import httpx
import pytest

from reset_tracker.providers import CodexResetProvider, ProviderError

TIMELINE = json.loads((Path(__file__).parent / "fixtures/timeline.json").read_text())


def test_confirmed_resets_only():
    events = CodexResetProvider.parse(TIMELINE)
    assert len(events) == 1
    assert events[0].id == "confirmed"
    assert events[0].source_url == "https://codex-reset.com/"


def test_fetch_identifies_reader():
    def handle(request):
        assert request.url.path == "/api/timeline"
        assert request.headers["User-Agent"] == "reader/1.0 (+mailto:test@example.com)"
        return httpx.Response(200, json=TIMELINE)

    with httpx.Client(
        transport=httpx.MockTransport(handle),
        headers={"User-Agent": "reader/1.0 (+mailto:test@example.com)"},
    ) as client:
        assert len(CodexResetProvider().fetch(client)) == 1


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"events": None},
        {"events": [None]},
        {"events": [{"group": "reset", "announcement_state": "announced"}]},
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
    "state_fields",
    [
        {},
        {"announcement_state": None},
        {"announcement_state": ""},
        {"announcement_state": "pending"},
        {"announcement_state": 42},
    ],
)
def test_unclassified_entries_do_not_abort_fetch(state_fields):
    confirmed = TIMELINE["events"][1]
    unclassified = (
        {key: value for key, value in confirmed.items() if key != "announcement_state"}
        | {"id": "unclassified"}
        | state_fields
    )
    payload = {"events": [unclassified, confirmed]}
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        events = CodexResetProvider().fetch(client)
    assert [event.id for event in events] == ["confirmed"]


def test_missing_announcement_state_is_valid_without_qualifying_resets():
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"events": [{"group": "reset"}]})
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
    event = {**TIMELINE["events"][1], "announced_at": timestamp}
    with pytest.raises(ValueError):
        CodexResetProvider.parse({"events": [event]})


def test_empty_results_are_valid():
    assert CodexResetProvider.parse({"events": []}) == []
