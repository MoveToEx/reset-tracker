import copy
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from conftest import Clock

from reset_tracker.config import WebhookConfig
from reset_tracker.models import BANKED_STATES, ResetEvent
from reset_tracker.providers import CodexResetProvider, ProviderError
from reset_tracker.service import Tracker
from reset_tracker.storage import Store
from reset_tracker.templates import render_request

NOW = datetime(2026, 1, 2, 13, tzinfo=UTC)
TIMELINE = json.loads((Path(__file__).parent / "fixtures/banked-timeline.json").read_text())
FORECAST = json.loads((Path(__file__).parent / "fixtures/forecast.json").read_text())


def parse(item):
    return CodexResetProvider.parse_banked({"events": [item]}, now=NOW)


def record(store, events):
    return store.record_snapshot(events, ["first"], 1000, cache_namespace="banked")


def drain(store):
    result = []
    while heads := store.heads(["first"]):
        result.append(heads[0].event)
        store.delivered(heads[0].id, "first")
    return result


def test_timeline_includes_banked_announcements_and_observations_with_nullable_url():
    events = CodexResetProvider.parse_banked(TIMELINE, now=NOW)
    assert [event.banked_state for event in events] == ["available", "announced", "unknown"]
    observed = events[0]
    assert observed.kind == "banked"
    assert observed.id == "banked-observed"
    assert observed.url == "https://codex-reset.com/banked-reset"
    assert observed.banked_source == "observed"
    assert observed.probability is observed.effective_at is observed.occurrence_id is None
    assert ResetEvent.from_dict(observed.to_dict()) == observed


@pytest.mark.parametrize("state", BANKED_STATES)
def test_all_documented_banked_states_are_reported_without_claiming_availability(state):
    (event,) = parse({**TIMELINE["events"][0], "banked_state": state})
    assert event.banked_state == state


def test_future_announcement_is_skipped_but_announced_future_grant_is_reported():
    item = {**TIMELINE["events"][1], "announced_at": "2026-01-02T14:00:00Z"}
    assert parse(item) == []
    (announced,) = parse(
        {**TIMELINE["events"][1], "preview": True, "effective_at": "2026-01-03T12:00:00Z"}
    )
    assert announced.banked_state == "announced"
    assert announced.effective_at is None


@pytest.mark.parametrize(
    "marker", [{"banked_grant": True}, {"reset_kind": "banked"}, {"banked_state": "available"}]
)
def test_explicit_banked_markers_are_used_independently(marker):
    item = {
        "id": "one",
        "group": "credits",
        "summary": "Banked reset",
        "announced_at": "2026-01-02T12:00:00Z",
        **marker,
    }
    assert parse(item)[0].kind == "banked"


@pytest.mark.parametrize(
    "marker", [{"banked_grant": True}, {"reset_kind": "banked"}, {"banked_state": "available"}]
)
def test_banked_marker_never_qualifies_as_a_global_reset(marker):
    item = {**TIMELINE["events"][3], **marker}
    assert CodexResetProvider.parse_confirmed({"events": [item]}, now=NOW) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"id": None},
        {"id": 123},
        {"summary": ""},
        {"announced_at": "not a date"},
        {"announced_at": "2026-01-02T12:00:00"},
        {"url": 42},
        {"url": "javascript:alert(1)"},
        {"banked_state": "redeemed"},
        {"banked_state": True},
        {"banked_state": ""},
        {"source": 42},
        {"scope": None},
    ],
)
def test_malformed_banked_data_fails_provider(changes):
    payload = {"events": [{**TIMELINE["events"][0], **changes}]}
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
        ) as client,
        pytest.raises(ProviderError, match="invalid timeline response"),
    ):
        CodexResetProvider(clock=lambda: NOW).fetch_confirmations(client)


def test_same_event_with_multiple_snapshot_states_keeps_highest_state():
    payload = {
        "events": [
            {**TIMELINE["events"][0], "banked_state": state}
            for state in ("available", "announced", "unknown")
        ]
    }
    (event,) = CodexResetProvider.parse_banked(payload, now=NOW)
    assert event.banked_state == "available"


def test_confirmation_fetch_collects_banked_and_global_events_in_one_request():
    paths = []

    def handle(request):
        paths.append(request.url.path)
        assert request.headers["User-Agent"] == "test/1.0 (+mailto:test@example.com)"
        return httpx.Response(200, json=TIMELINE)

    with httpx.Client(
        transport=httpx.MockTransport(handle),
        headers={"User-Agent": "test/1.0 (+mailto:test@example.com)"},
    ) as client:
        events = CodexResetProvider(clock=lambda: NOW).fetch_confirmations(client)
    assert len(events) == 4
    assert {event.kind for event in events} == {"confirmed", "banked"}
    assert paths == ["/api/timeline"]


def test_first_start_latest_only_and_restart_without_replaying_global_history(tmp_path):
    path = tmp_path / "state.sqlite3"
    events = CodexResetProvider.parse_banked(TIMELINE, now=NOW)
    with Store(path) as store:
        assert record(store, events) == 1
        assert [event.id for event in drain(store)] == ["banked-observed"]
        assert store.latest_confirmed_at() is None
        assert store._metadata("confirmed:watermark") is None
        watermark = store._metadata("banked:watermark")
    with Store(path) as store:
        assert record(store, events) == 0
        assert drain(store) == []
        assert store._metadata("banked:watermark") == watermark


def test_independent_observations_without_urls_are_distinct_and_catch_up_in_order(tmp_path):
    (first,) = parse(TIMELINE["events"][0])
    second = replace(first, id="second", announced_at=datetime(2026, 1, 2, 12, 1, tzinfo=UTC))
    assert first.url == second.url
    assert first.identity != second.identity
    with Store(tmp_path / "state.sqlite3") as store:
        record(store, [])
        assert record(store, [second, first]) == 2
        assert [event.id for event in drain(store)] == [first.id, second.id]
        assert store.connection.execute("SELECT COUNT(*) FROM reset_occurrences").fetchone()[0] == 0


def test_status_progression_bypasses_watermark_and_never_replays_or_regresses(tmp_path):
    path = tmp_path / "state.sqlite3"
    (announcement,) = parse(TIMELINE["events"][1])
    (newer,) = parse(TIMELINE["events"][0])
    with Store(path) as store:
        assert record(store, [announcement]) == 1
        drain(store)
        assert record(store, [newer]) == 1
        drain(store)
        watermark = store._metadata("banked:watermark")
    with Store(path) as store:
        arriving = replace(announcement, banked_state="arriving")
        assert record(store, [arriving, newer]) == 1
        assert [event.banked_state for event in drain(store)] == ["arriving"]
        available = replace(announcement, banked_state="available")
        assert record(store, [available, newer]) == 1
        assert [event.banked_state for event in drain(store)] == ["available"]
        assert record(store, [announcement, arriving, available, newer]) == 0
        assert record(store, [replace(announcement, banked_state="unknown")]) == 0
        assert store._metadata("banked:watermark") == watermark
    with Store(path) as store:
        assert record(store, [available]) == 0
        assert store.pending_count(["first"]) == 0


def test_adding_an_x_url_to_an_observation_does_not_change_identity_or_resend(tmp_path):
    (observed,) = parse(TIMELINE["events"][0])
    linked = replace(observed, url="https://x.com/example/status/new-link")
    assert linked.identity == observed.identity
    with Store(tmp_path / "state.sqlite3") as store:
        assert record(store, [observed]) == 1
        drain(store)
        assert record(store, [linked]) == 0


def test_banked_records_and_queues_roll_back_with_other_timeline_changes(tmp_path):
    banked = CodexResetProvider.parse_banked(TIMELINE, now=NOW)
    confirmed = CodexResetProvider.parse_confirmed(TIMELINE, now=NOW)
    with Store(tmp_path / "state.sqlite3") as store:
        store.connection.execute(
            "CREATE TRIGGER abort_banked BEFORE INSERT ON deliveries "
            "WHEN NEW.event_identity LIKE 'banked:%' "
            "BEGIN SELECT RAISE(ABORT, 'queue failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.record_snapshots({"confirmed": confirmed, "banked": banked}, ["first"], 1000)
        assert store._metadata("confirmed:initialized") is None
        assert store._metadata("banked:initialized") is None
        assert store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert store.connection.execute("SELECT COUNT(*) FROM reset_occurrences").fetchone()[0] == 0


def test_banked_reset_does_not_suppress_forecast_or_advance_confirmed_watermark(make_settings):
    settings = make_settings()
    payload = copy.deepcopy(TIMELINE)
    payload["events"] = [{**TIMELINE["events"][0], "announced_at": "2026-01-02T12:40:00Z"}]
    sent = []

    def handle(request):
        if request.method == "POST":
            sent.append(json.loads(request.content)["event"])
            return httpx.Response(200)
        return httpx.Response(
            200, json=payload if request.url.path == "/api/timeline" else FORECAST
        )

    with (
        Store(settings.database_path) as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
    ):
        assert Tracker(
            settings,
            store,
            client,
            providers=[CodexResetProvider(clock=lambda: NOW)],
            clock=Clock(),
        ).once()
        assert store.latest_confirmed_at() is None
        assert store._metadata("confirmed:watermark") is None
        assert store._metadata("banked:initialized") == "1"
    assert [event["kind"] for event in sent] == ["forecast", "banked"]


@pytest.mark.parametrize("state", BANKED_STATES)
@pytest.mark.parametrize("preset", ["generic_json", "discord"])
def test_webhooks_report_banked_state_and_tracker_observation_source(state, preset):
    (event,) = parse({**TIMELINE["events"][0], "banked_state": state})
    payload = json.loads(
        render_request(
            WebhookConfig("test", "https://example.com", preset=preset), event, "now"
        ).body
    )
    if preset == "generic_json":
        assert payload["event"]["kind"] == "banked"
        assert payload["event"]["banked_state"] == state
        assert payload["event"]["banked_source"] == "observed"
    else:
        embed = payload["embeds"][0]
        assert embed["title"] == "🎟️ Codex banked reset"
        fields = {field["name"]: field["value"] for field in embed["fields"]}
        assert fields["Banked reset status"] == state
        assert fields["Reported by"] == "Tracker monitoring accounts"
        assert fields["Source"] == "https://codex-reset.com/banked-reset"
        assert "Signal probability" not in fields
        assert "Reset effective at (UTC)" not in fields
        assert payload["allowed_mentions"] == {"parse": []}
