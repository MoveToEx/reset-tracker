import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import Clock, FakeProvider

from reset_tracker.config import WebhookConfig
from reset_tracker.models import timestamp_text
from reset_tracker.providers import CodexResetProvider, ProviderError
from reset_tracker.service import Tracker
from reset_tracker.storage import Store
from reset_tracker.templates import render_request

NOW = datetime(2026, 1, 2, 13, tzinfo=UTC)
RESET = {
    "id": "reset-1",
    "group": "reset",
    "announcement_state": "announced",
    "announced_at": "2026-01-02T10:00:00Z",
    "summary": 'Global reset "now"!\nEnjoy.',
    "url": "https://x.com/example/status/1",
    "effective_at": None,
    "observed_at": "2026-01-02T12:00:00Z",
}


def parse(item):
    return CodexResetProvider.parse_confirmed({"events": [item]}, now=NOW)


def test_confirmed_reset_records_actual_observation_without_forecast_probabilities():
    (event,) = parse(RESET)
    assert event.kind == "confirmed"
    assert event.id == RESET["id"]
    assert event.summary == RESET["summary"]
    assert event.url == RESET["url"]
    assert event.announced_at == datetime(2026, 1, 2, 10, tzinfo=UTC)
    assert event.effective_at == datetime(2026, 1, 2, 12, tzinfo=UTC)
    assert event.effective_at_source == "observed_at"
    assert event.recorded_at == event.effective_at
    assert event.probability is event.probability_24h is event.probability_48h is None
    assert event.expected_at is event.window_label is None


@pytest.mark.parametrize(
    "changes",
    [
        {"group": "credits"},
        {"announcement_state": None},
        {"announcement_state": "none"},
        {"announcement_state": "committed"},
        {"banked_grant": True},
        {"preview": True},
        {"scope": "team"},
        {"announced_at": "2026-01-02T14:00:00Z"},
        {"effective_at": "2026-01-02T14:00:00Z"},
        {"observed_at": "2026-01-02T14:00:00Z"},
    ],
)
def test_unconfirmed_banked_partial_or_future_resets_are_skipped(changes):
    assert parse({**RESET, **changes}) == []


def test_missing_confirmation_metadata_is_skipped():
    item = {k: v for k, v in RESET.items() if k != "announcement_state"}
    item["confirmed"] = True
    assert parse(item) == []


def test_documented_confirmation_does_not_require_undocumented_flags_or_effect_time():
    item = {k: v for k, v in RESET.items() if k not in {"effective_at", "observed_at"}}
    (event,) = parse(item)
    assert event.effective_at is None
    assert event.recorded_at == event.announced_at


def test_explicit_effect_time_takes_precedence_over_observation():
    (event,) = parse({**RESET, "effective_at": "2026-01-02T11:00:00+00:00"})
    assert event.effective_at == datetime(2026, 1, 2, 11, tzinfo=UTC)
    assert event.effective_at_source == "effective_at"


@pytest.mark.parametrize(
    "payload",
    [None, [], {}, {"events": None}, {"events": {}}, {"events": [None]}, {"events": [{}]}],
)
def test_malformed_timeline_fails(payload):
    with pytest.raises(ValueError):
        CodexResetProvider.parse_confirmed(payload, now=NOW)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": None},
        {"id": 12},
        {"summary": ""},
        {"url": "javascript:alert(1)"},
        {"announced_at": "bad"},
        {"announced_at": "2026-01-02T10:00:00"},
        {"effective_at": 12},
        {"observed_at": "bad"},
        {"reset_id": ""},
        {"reset_id": 123},
    ],
)
def test_invalid_confirmed_event_fails(changes):
    with pytest.raises(ValueError):
        parse({**RESET, **changes})


@pytest.mark.parametrize("failure", ["invalid_json", "malformed", "http", "network"])
def test_timeline_failure_advances_fallback_without_committing_partial_streams(
    failure, make_settings
):
    settings = make_settings()
    calls = []

    def handle(request):
        calls.append(request.url.path)
        if request.url.path == "/api/forecast":
            return httpx.Response(200, json={"official_signal": None})
        if failure == "invalid_json":
            return httpx.Response(200, content=b"not json")
        if failure == "malformed":
            return httpx.Response(200, json={"events": [None]})
        if failure == "network":
            raise httpx.ReadTimeout("timeline timeout", request=request)
        return httpx.Response(429, headers={"Retry-After": "1200"})

    fallback = FakeProvider("fallback", [])
    with (
        Store(settings.database_path) as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
    ):
        tracker = Tracker(
            settings,
            store,
            client,
            providers=[CodexResetProvider(clock=lambda: NOW), fallback],
            clock=Clock(),
        )
        assert tracker.poll()
        assert fallback.calls == ["fallback"]
        assert store._metadata("forecast:initialized") is None
        assert store._metadata("confirmed:initialized") is None
        assert store.provider_ready("codex_reset", 1001) == (failure != "http")
    assert calls == ["/api/forecast", "/api/timeline"]


def test_invalid_timeline_retains_exception_cause():
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    ) as client:
        with pytest.raises(ProviderError, match="invalid timeline response") as error:
            CodexResetProvider(clock=lambda: NOW).fetch(client)
    assert isinstance(error.value.__cause__, ValueError)


@pytest.mark.parametrize("stale_forecast", [False, True])
def test_same_post_forecast_then_confirmation_is_delivered_once_each_after_restart(
    make_settings, stale_forecast
):
    settings = make_settings()
    state = {"confirmed": False}
    sent = []
    forecast = {
        "official_signal": {
            "tweet_id": RESET["id"],
            "at": RESET["announced_at"],
            "summary": "Reset coming soon",
            "url": RESET["url"],
            "score": {"value": 93},
        },
    }

    def handle(request):
        if request.method == "POST":
            sent.append(json.loads(request.content)["event"])
            return httpx.Response(200)
        if request.url.path == "/api/forecast":
            payload = (
                {**forecast, "last_reset_at": RESET["observed_at"]}
                if state["confirmed"] and not stale_forecast
                else forecast
            )
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"events": [RESET] if state["confirmed"] else []})

    def once():
        with (
            Store(settings.database_path) as store,
            httpx.Client(transport=httpx.MockTransport(handle)) as client,
        ):
            tracker = Tracker(
                settings,
                store,
                client,
                providers=[CodexResetProvider(clock=lambda: NOW)],
                clock=Clock(),
            )
            assert tracker.once()
            return store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    assert once() == 1
    state["confirmed"] = True
    assert once() == 2
    assert once() == 2
    assert [e["kind"] for e in sent] == ["forecast", "confirmed"]
    assert [e["probability"] for e in sent] == [93, None]
    assert sent[-1]["effective_at"] == "2026-01-02T12:00:00.000000Z"


def test_independent_initial_latest_and_chronological_confirmed_catch_up(tmp_path, make_event):
    path = tmp_path / "state.sqlite3"
    forecasts = [make_event(100), make_event(200)]
    resets = [make_event(i, kind="confirmed", reset_id=f"reset-{i}") for i in (2, 1)]
    with Store(path) as store:
        assert (
            store.record_snapshots({"forecast": forecasts, "confirmed": resets}, ["first"], 1000)
            == 2
        )
        head = store.heads(["first"])[0]
        assert (head.event.kind, head.event.id) == ("confirmed", "2")
        store.delivered(head.id, "first")
        assert store.heads(["first"])[0].event.id == "200"
        store.delivered(store.heads(["first"])[0].id, "first")
    with Store(path) as store:
        later = [make_event(i, kind="confirmed", reset_id=f"reset-{i}") for i in (4, 3)]
        assert (
            store.record_snapshots(
                {"forecast": forecasts, "confirmed": resets + later}, ["first"], 1001
            )
            == 2
        )
        for number in ("3", "4"):
            head = store.heads(["first"])[0]
            assert head.event.id == number
            store.delivered(head.id, "first")
        assert store._metadata("forecast:watermark") == forecasts[-1].to_dict()["announced_at"]
        assert store._metadata("confirmed:watermark") == later[0].to_dict()["announced_at"]
        same_time = replace(
            later[0], id="5", url="https://example.com/resets/5", reset_id="reset-5"
        )
        assert store.record_snapshot([same_time], ["first"], 1002, cache_namespace="confirmed") == 1
        assert store.record_snapshot(resets, ["first"], 1003, cache_namespace="confirmed") == 0
        assert store._metadata("confirmed:watermark") == later[0].to_dict()["announced_at"]


def test_late_confirmation_uses_effect_time_for_order_and_watermark(tmp_path, make_event):
    older_post = make_event(1, kind="confirmed", effective_at=NOW)
    newer_post = make_event(2, kind="confirmed", effective_at=NOW - timedelta(hours=1))
    with Store(tmp_path / "state.sqlite3") as store:
        store.record_snapshot([newer_post], ["first"], 1000, cache_namespace="confirmed")
        store.delivered(store.heads(["first"])[0].id, "first")
        assert (
            store.record_snapshot([older_post], ["first"], 1001, cache_namespace="confirmed") == 1
        )
        assert replace(store.heads(["first"])[0].event, occurrence_id=None) == older_post
        assert store._metadata("confirmed:watermark") == timestamp_text(NOW)


def test_both_streams_and_deliveries_roll_back_together(tmp_path, make_event):
    with Store(tmp_path / "state.sqlite3") as store:
        store.connection.execute(
            "CREATE TRIGGER fail_confirmed BEFORE INSERT ON deliveries "
            "WHEN NEW.event_identity LIKE 'confirmed:%' "
            "BEGIN SELECT RAISE(ABORT, 'simulated queue failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.record_snapshots(
                {"forecast": [make_event(1)], "confirmed": [make_event(2, kind="confirmed")]},
                ["first"],
                1000,
            )
        assert store._metadata("forecast:initialized") is None
        assert store._metadata("confirmed:initialized") is None
        assert store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert store.pending_count(["first"]) == 0


@pytest.mark.parametrize("legacy_kind", [None, "confirmed", "committed"])
@pytest.mark.parametrize("status", ["pending", "delivered"])
def test_legacy_confirmation_not_replayed_but_commitment_does_not_hide_it(
    tmp_path, make_event, legacy_kind, status
):
    confirmed = make_event(1, kind="confirmed")
    payload = confirmed.to_dict()
    if legacy_kind is None:
        del payload["kind"]
    else:
        payload["kind"] = legacy_kind
    bare_identity = confirmed.identity.removeprefix("confirmed:")
    with Store(tmp_path / "state.sqlite3") as store:
        with store.connection:
            store.connection.execute(
                "INSERT INTO events VALUES (?, ?, ?)",
                (bare_identity, timestamp_text(confirmed.announced_at), json.dumps(payload)),
            )
            store.connection.execute(
                "INSERT INTO deliveries(event_identity, webhook_id, detected_at, status) "
                "VALUES (?, ?, ?, ?)",
                (bare_identity, "first", timestamp_text(NOW), status),
            )
        queued = store.record_snapshot([confirmed], ["first"], 1000, cache_namespace="confirmed")
        assert queued == (1 if legacy_kind == "committed" else 0)
        assert (
            store.connection.execute(
                "SELECT status FROM deliveries WHERE event_identity = ?", (bare_identity,)
            ).fetchone()[0]
            == status
        )


@pytest.mark.parametrize("preset", ["generic_json", "discord"])
def test_confirmation_webhooks_label_actual_reset_and_include_effect_time(preset):
    (event,) = parse(RESET)
    webhook = WebhookConfig("test", "https://example.com/webhook", preset=preset)
    payload = json.loads(render_request(webhook, event, "detected-now").body)
    if preset == "generic_json":
        assert payload == {"event": event.to_dict(), "detected_at": "detected-now"}
    else:
        embed = payload["embeds"][0]
        assert embed["title"] == "Codex reset confirmed"
        assert embed["description"] == event.summary
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["Reset effective at (UTC)"] == "2026-01-02T12:00:00.000000Z"
        assert not any("probability" in name.lower() for name in fields)
        assert payload["allowed_mentions"] == {"parse": []}
