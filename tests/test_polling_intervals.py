import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from conftest import Clock

from reset_tracker.providers import CodexResetProvider
from reset_tracker.service import Tracker
from reset_tracker.storage import Store

NOW = datetime(2026, 1, 2, 13, tzinfo=UTC)
FORECAST = json.loads((Path(__file__).parent / "fixtures/forecast.json").read_text())
TIMELINE = {
    "events": [
        {
            "id": "reset-1",
            "group": "reset",
            "announcement_state": "announced",
            "announced_at": "2026-01-02T10:00:00Z",
            "observed_at": "2026-01-02T12:00:00Z",
            "summary": "Reset is in effect",
            "url": "https://example.com/resets/reset-1",
        }
    ]
}


class StopAt:
    def __init__(self, clock, deadline):
        self.clock = clock
        self.deadline = deadline

    def is_set(self):
        return self.clock() >= self.deadline

    def wait(self, seconds):
        self.clock.advance(min(seconds, self.deadline - self.clock()))


@pytest.mark.parametrize(
    ("forecast_interval", "confirmation_interval", "until", "forecasts", "confirmations"),
    [
        (600, 120, 601, [0, 600], [0, 120, 240, 360, 480, 600]),
        (300, 180, 901, [0, 300, 600, 900], [0, 180, 360, 540, 720, 900]),
        (60, 240, 241, [0, 60, 120, 180, 240], [0, 240]),
    ],
)
def test_endpoints_run_on_independent_configured_cadences(
    make_settings, forecast_interval, confirmation_interval, until, forecasts, confirmations
):
    settings = make_settings(
        interval_seconds=forecast_interval, confirmation_interval_seconds=confirmation_interval
    )
    clock = Clock(0)
    requests = []

    def handle(request):
        requests.append((request.url.path, clock()))
        return httpx.Response(
            200,
            json={"events": []}
            if request.url.path == "/api/timeline"
            else {"official_signal": None},
        )

    with (
        Store(settings.database_path) as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
    ):
        Tracker(settings, store, client, providers=[CodexResetProvider()], clock=clock).run(
            StopAt(clock, until), monotonic=clock
        )
    assert [t for path, t in requests if path == "/api/forecast"] == forecasts
    assert [t for path, t in requests if path == "/api/timeline"] == confirmations
    assert requests[:2] == [("/api/timeline", 0), ("/api/forecast", 0)]


@pytest.mark.parametrize(
    ("slow_path", "duration", "until", "forecasts", "confirmations"),
    [
        ("/api/timeline", 250, 601, [250, 600], [0, 360, 480, 600]),
        ("/api/forecast", 650, 1201, [0, 1200], [0, 650, 720, 840, 960, 1080, 1200]),
    ],
)
def test_slow_streams_skip_missed_ticks_without_overlapping_or_shifting_other_cadence(
    make_settings, slow_path, duration, until, forecasts, confirmations
):
    settings = make_settings()
    clock = Clock(0)
    requests = []
    delayed = False

    def handle(request):
        nonlocal delayed
        requests.append((request.url.path, clock()))
        if request.url.path == slow_path and not delayed:
            delayed = True
            clock.advance(duration)
        return httpx.Response(
            200, json={"events": []} if request.url.path == "/api/timeline" else {}
        )

    with (
        Store(settings.database_path) as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
    ):
        Tracker(settings, store, client, providers=[CodexResetProvider()], clock=clock).run(
            StopAt(clock, until), monotonic=clock
        )
    assert [t for path, t in requests if path == "/api/forecast"] == forecasts
    assert [t for path, t in requests if path == "/api/timeline"] == confirmations


def test_delivery_retries_do_not_trigger_either_provider_stream(make_settings, make_event):
    settings = make_settings()
    clock = Clock(0)
    requests, sends = [], []

    def handle(request):
        if request.method == "POST":
            sends.append(clock())
            return httpx.Response(503 if len(sends) == 1 else 200)
        requests.append((request.url.path, clock()))
        return httpx.Response(
            200, json={"events": []} if request.url.path == "/api/timeline" else {}
        )

    with (
        Store(settings.database_path) as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
    ):
        store.record_snapshot([make_event(1)], ["first"], clock())
        Tracker(settings, store, client, providers=[CodexResetProvider()], clock=clock).run(
            StopAt(clock, 121), monotonic=clock
        )
    assert sends == [0, 30]
    assert requests == [("/api/timeline", 0), ("/api/forecast", 0), ("/api/timeline", 120)]


@pytest.mark.parametrize("failed_path", ["/api/forecast", "/api/timeline"])
@pytest.mark.parametrize("failure", ["http", "network", "malformed"])
def test_stream_failure_does_not_stop_other_stream_or_its_deliveries(
    make_settings, failed_path, failure
):
    settings = make_settings()
    sent = []
    requests = []

    def handle(request):
        if request.method == "POST":
            sent.append(json.loads(request.content)["event"]["kind"])
            return httpx.Response(200)
        requests.append(request.url.path)
        if request.url.path == failed_path:
            if failure == "network":
                raise httpx.ReadTimeout("upstream unavailable", request=request)
            return httpx.Response(503) if failure == "http" else httpx.Response(200, json=[])
        return httpx.Response(
            200, json=FORECAST if request.url.path == "/api/forecast" else TIMELINE
        )

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
        assert not tracker.once()
        assert store.pending_count(["first"]) == 0
        expected = "confirmed" if failed_path == "/api/forecast" else "forecast"
        assert store._metadata(expected + ":initialized") == "1"
        assert (
            store._metadata(
                ("forecast" if expected == "confirmed" else "confirmed") + ":initialized"
            )
            is None
        )
    assert sent == [expected]
    assert requests == ["/api/timeline", "/api/forecast"]


@pytest.mark.parametrize("limited_stream", ["forecast", "confirmed"])
def test_rate_limit_cooldown_is_scoped_to_one_stream_and_persists(make_settings, limited_stream):
    settings = make_settings()
    clock = Clock()
    requests = []
    limited_path = "/api/forecast" if limited_stream == "forecast" else "/api/timeline"

    def handle(request):
        requests.append(request.url.path)
        if request.url.path == limited_path:
            return httpx.Response(429, headers={"Retry-After": "1200"})
        return httpx.Response(
            200, json={"events": []} if request.url.path == "/api/timeline" else {}
        )

    for _attempt in range(2):
        with (
            Store(settings.database_path) as store,
            httpx.Client(transport=httpx.MockTransport(handle)) as client,
        ):
            tracker = Tracker(
                settings, store, client, providers=[CodexResetProvider()], clock=clock
            )
            assert tracker.poll() == (limited_stream != "forecast")
            assert tracker.poll_confirmations() == (limited_stream != "confirmed")
            assert not store.provider_ready("codex_reset:" + limited_stream, clock())
        clock.advance(120)
    assert requests.count(limited_path) == 1
    assert len(requests) == 3


def test_forecast_uses_persisted_confirmation_when_timeline_is_not_due(make_settings):
    settings = make_settings()
    provider = CodexResetProvider(clock=lambda: NOW)
    confirmed = provider.parse_confirmed(TIMELINE, now=NOW)
    with Store(settings.database_path) as store:
        store.record_snapshot(confirmed, ["first"], 1000, cache_namespace="confirmed")
        store.delivered(store.heads(["first"])[0].id, "first")
    stale = {
        **FORECAST,
        "official_signal": {**FORECAST["official_signal"], "at": "2026-01-02T11:00:00Z"},
    }
    requests = []

    def handle(request):
        requests.append(request.url.path)
        assert request.url.path == "/api/forecast"
        return httpx.Response(200, json=stale)

    with (
        Store(settings.database_path) as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
    ):
        assert Tracker(settings, store, client, providers=[provider], clock=Clock()).poll()
        assert store.pending_count(["first"]) == 0
    assert requests == ["/api/forecast"]


def test_legacy_provider_cooldown_is_honored_until_it_expires(make_settings):
    settings = make_settings()
    clock = Clock()
    paths = []

    def handle(request):
        paths.append(request.url.path)
        return httpx.Response(
            200, json={"events": []} if request.url.path == "/api/timeline" else {}
        )

    with Store(settings.database_path) as store:
        store.cool_provider("codex_reset", clock() + 120)
    with (
        Store(settings.database_path) as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
    ):
        tracker = Tracker(settings, store, client, clock=clock)
        assert not tracker.poll()
        assert not tracker.poll_confirmations()
        assert paths == []
        clock.advance(120)
        assert tracker.poll()
        assert tracker.poll_confirmations()
    assert paths == ["/api/forecast", "/api/timeline"]
