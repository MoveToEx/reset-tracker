import json
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from conftest import Clock, FakeProvider

from reset_tracker.config import WebhookConfig
from reset_tracker.providers import CodexResetProvider, ProviderError
from reset_tracker.service import Tracker
from reset_tracker.storage import Store


def test_ordered_fallback_stops_on_valid_empty(make_settings):
    calls = []
    providers = [
        FakeProvider("broken", ProviderError(), calls),
        FakeProvider("empty", [], calls),
        FakeProvider("unused", ProviderError(), calls),
    ]
    settings = make_settings()
    with Store(settings.database_path) as store, httpx.Client() as client:
        assert Tracker(settings, store, client, providers=providers).poll()
    assert calls == ["broken", "empty"]


def test_rich_json_template_is_delivered_as_an_object(make_settings, make_event):
    settings = make_settings(
        webhooks=(
            WebhookConfig(
                "first",
                "https://discord.com/api/webhooks/id/token",
                preset="discord",
                body_template="""
{
  "embeds": [{
    "description": {{ event.summary | tojson }},
    "color": 5793266,
    "fields": [{"name": "Provider", "value": {{ event.provider | tojson }}, "inline": true}]
  }],
  "tts": false
}
""",
            ),
        )
    )
    event = make_event(1, summary='Rich "quotes"\nUnicode 你好')
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200)

    with (
        Store(settings.database_path) as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
    ):
        store.record_snapshot([event], ["first"], 1000)
        assert Tracker(settings, store, client, clock=Clock()).dispatch_due() == (1, 0)
        assert store.pending_count(["first"]) == 0
    assert len(requests) == 1
    assert requests[0].headers["Content-Type"] == "application/json"
    assert requests[0].url.params["wait"] == "true"
    assert json.loads(requests[0].content) == {
        "embeds": [
            {
                "description": event.summary,
                "color": 5793266,
                "fields": [{"name": "Provider", "value": event.provider, "inline": True}],
            }
        ],
        "tts": False,
        "allowed_mentions": {"parse": []},
    }


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("timeout"),
        httpx.HTTPStatusError(
            "upstream failed",
            request=httpx.Request("GET", "https://example.com"),
            response=httpx.Response(502),
        ),
    ],
)
def test_http_and_network_failures_advance_to_next_provider(error, make_settings):
    settings = make_settings()
    calls = []
    providers = [
        FakeProvider("unavailable", error, calls),
        FakeProvider("working", [], calls),
    ]
    with Store(settings.database_path) as store, httpx.Client() as client:
        assert Tracker(settings, store, client, providers=providers).poll()
    assert calls == ["unavailable", "working"]


def test_provider_cooldown_survives_restart(make_settings, make_event):
    settings = make_settings()
    clock = Clock()
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "1200"})

    with (
        Store(settings.database_path) as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
    ):
        providers = [CodexResetProvider(), FakeProvider("fallback", [make_event(1)])]
        assert Tracker(settings, store, client, providers=providers, clock=clock).poll()
    clock.advance(600)
    with (
        Store(settings.database_path) as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
    ):
        providers = [CodexResetProvider(), FakeProvider("fallback", [])]
        assert Tracker(settings, store, client, providers=providers, clock=clock).poll()
    assert len(requests) == 1


def test_forecasts_are_not_hidden_by_legacy_timeline_watermark(
    make_settings,
    make_event,
):
    settings = make_settings()
    completed = make_event(9, kind="confirmed")
    forecast = make_event(1)
    payload = {
        "official_signal": {
            "tweet_id": forecast.id,
            "summary": forecast.summary,
            "at": forecast.to_dict()["announced_at"],
            "url": forecast.url,
            "window": {
                "label": forecast.window_label,
                "target_at": forecast.to_dict()["expected_at"],
            },
        },
        "probabilities": {
            "signal_percent": 93,
            "rounded_24h": 32,
            "rounded_48h": 53,
        },
    }
    provider = CodexResetProvider(clock=lambda: datetime(2026, 1, 1, 13, tzinfo=UTC))
    with (
        Store(settings.database_path) as store,
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json=payload if request.url.path == "/api/forecast" else {"events": []}
                )
            )
        ) as client,
    ):
        store.record_snapshot([completed], ["first"], 1000)
        previous = store.heads(["first"])[0]
        store.delivered(previous.id, "first")
        tracker = Tracker(settings, store, client, providers=[provider], clock=Clock())
        assert tracker.poll()
        assert store.heads(["first"])[0].event == forecast
        assert store._metadata("watermark") == completed.to_dict()["announced_at"]
        assert store._metadata("forecast:watermark") == forecast.to_dict()["announced_at"]
    with (
        Store(settings.database_path) as store,
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json=payload if request.url.path == "/api/forecast" else {"events": []}
                )
            )
        ) as client,
    ):
        assert Tracker(settings, store, client, providers=[provider], clock=Clock()).poll()
        assert store.pending_count(["first"]) == 1


def test_forecast_for_previously_delivered_commitment_sends_probability_after_restart(
    make_settings,
    make_event,
):
    settings = make_settings()
    forecast = make_event(1)
    committed = replace(
        forecast,
        kind="committed",
        probability=None,
        probability_24h=None,
        probability_48h=None,
        window_label=None,
        expected_at=None,
    )
    with Store(settings.database_path) as store:
        store.record_snapshot([committed], ["first"], 1000, cache_namespace="committed")
        store.delivered(store.heads(["first"])[0].id, "first")
        assert store.record_snapshot([forecast], ["first"], 1001, cache_namespace="forecast") == 1
    requests = []
    with (
        Store(settings.database_path) as store,
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: requests.append(request) or httpx.Response(200)
            )
        ) as client,
    ):
        assert store.heads(["first"])[0].event == forecast
        assert Tracker(settings, store, client, clock=Clock(1001)).dispatch_due() == (1, 0)
        updated = replace(forecast, probability=94)
        assert store.record_snapshot([updated], ["first"], 1002, cache_namespace="forecast") == 0
    assert len(requests) == 1
    event = json.loads(requests[0].content)["event"]
    assert event["kind"] == "forecast"
    assert event["probability"] == 93
    assert event["probability_24h"] == 32
    assert event["probability_48h"] == 53
    assert event["expected_at"] == forecast.to_dict()["expected_at"]


def test_retry_recovery_endpoint_isolation_and_order(make_settings, make_event):
    settings = make_settings(
        webhooks=(
            WebhookConfig("first", "https://hooks.example.com/first"),
            WebhookConfig("second", "https://hooks.example.com/second"),
        )
    )
    clock = Clock()
    calls = []
    fail_first = True

    def handle(request):
        calls.append((request.url.path, json.loads(request.content)["event"]["id"]))
        return httpx.Response(503 if request.url.path == "/first" and fail_first else 200)

    with (
        Store(settings.database_path) as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
    ):
        store.record_snapshot([], ["first", "second"], clock())
        store.record_snapshot([make_event(2), make_event(1)], ["first", "second"], clock())
        tracker = Tracker(settings, store, client, clock=clock)
        assert tracker.dispatch_due() == (2, 1)
        assert tracker.dispatch_due() == (1, 0)
        assert calls == [("/first", "1"), ("/second", "1"), ("/second", "2")]
        assert store.heads(["first"])[0].due_at == 1030
    clock.advance(30)
    fail_first = False
    with (
        Store(settings.database_path) as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
    ):
        tracker = Tracker(settings, store, client, clock=clock)
        assert tracker.dispatch_due() == (1, 0)
        assert tracker.dispatch_due() == (1, 0)
        assert store.pending_count(["first", "second"]) == 0
    assert calls[-2:] == [("/first", "1"), ("/first", "2")]


@pytest.mark.parametrize("status", [308, 400, 401, 403, 404, 422])
def test_permanent_failure_pauses_endpoint_and_manual_retry_uses_current_config(
    status,
    make_settings,
    make_event,
):
    settings = make_settings()
    clock = Clock()
    with (
        Store(settings.database_path) as store,
        httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(status))
        ) as client,
    ):
        store.record_snapshot([make_event(1)], ["first"], clock())
        tracker = Tracker(settings, store, client, clock=clock)
        assert tracker.dispatch_due() == (1, 1)
        assert tracker.dispatch_due() == (0, 0)
        assert store.pending_count(["first"]) == 1
    settings = replace(
        settings, webhooks=(WebhookConfig("first", "https://hooks.example.com/NEW"),)
    )
    requests = []
    with (
        Store(settings.database_path) as store,
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: requests.append(request) or httpx.Response(200)
            )
        ) as client,
    ):
        assert store.retry_failed(["first"], clock()) == 1
        assert Tracker(settings, store, client, clock=clock).dispatch_due() == (1, 0)
    assert requests[0].url.path == "/NEW"


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503])
def test_transient_failure_backoff_is_persistent(status, make_settings, make_event):
    settings = make_settings()
    clock = Clock()
    with (
        Store(settings.database_path) as store,
        httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(status))
        ) as client,
    ):
        store.record_snapshot([make_event(1)], ["first"], clock())
        tracker = Tracker(settings, store, client, clock=clock)
        for expected in [30, 60, 120, 240, 480, 960, 1920, 3600, 3600]:
            assert tracker.dispatch_due() == (1, 1)
            head = store.heads(["first"])[0]
            assert head.due_at == clock() + expected
            clock.advance(expected)


def test_discord_retry_after_and_successful_rate_limit_headers(make_settings, make_event):
    settings = make_settings(
        webhooks=(
            WebhookConfig("first", "https://discord.com/api/webhooks/id/token", preset="discord"),
        )
    )
    clock = Clock()
    responses = iter(
        [
            httpx.Response(429, json={"retry_after": 7200.5}, headers={"Retry-After": "1"}),
            httpx.Response(
                200,
                headers={
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset-After": "5.25",
                },
            ),
            httpx.Response(200),
        ]
    )
    with (
        Store(settings.database_path) as store,
        httpx.Client(transport=httpx.MockTransport(lambda request: next(responses))) as client,
    ):
        store.record_snapshot([], ["first"], clock())
        store.record_snapshot([make_event(1), make_event(2)], ["first"], clock())
        tracker = Tracker(settings, store, client, clock=clock)
        tracker.dispatch_due()
        assert store.heads(["first"])[0].due_at == clock() + 7200.5
        clock.advance(7200.5)
        assert tracker.dispatch_due() == (1, 0)
        assert tracker.dispatch_due() == (0, 0)
        clock.advance(5.25)
        assert tracker.dispatch_due() == (1, 0)


def test_render_failure_is_retained_and_pauses(make_settings, make_event):
    settings = make_settings(
        webhooks=(
            WebhookConfig(
                "first",
                "https://hooks.example.com",
                preset="custom",
                body_template="{{ missing_variable }}",
            ),
        )
    )
    with (
        Store(settings.database_path) as store,
        httpx.Client(
            transport=httpx.MockTransport(lambda request: pytest.fail("must not send"))
        ) as client,
    ):
        store.record_snapshot([make_event(1)], ["first"], 1000)
        tracker = Tracker(settings, store, client, clock=Clock())
        assert tracker.dispatch_due() == (1, 1)
        assert store.pending_count(["first"]) == 1
        assert store.heads(["first"]) == []


def test_network_errors_do_not_log_credentials(make_settings, make_event, caplog, monkeypatch):
    import logging

    monkeypatch.setattr(logging.getLogger("reset_tracker"), "propagate", True)
    settings = make_settings()

    def handle(request):
        raise httpx.ConnectError("failed request https://hooks.example.com/SECRET", request=request)

    with (
        Store(settings.database_path) as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
    ):
        store.record_snapshot([make_event(1)], ["first"], 1000)
        assert Tracker(settings, store, client, clock=Clock()).dispatch_due() == (1, 1)
    assert "SECRET" not in caplog.text
    assert "ConnectError" in caplog.text


def test_queued_delivery_is_attempted_when_all_providers_fail(make_settings, make_event):
    settings = make_settings()
    with (
        Store(settings.database_path) as store,
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client,
    ):
        store.record_snapshot([make_event(1)], ["first"], 1000)
        tracker = Tracker(
            settings,
            store,
            client,
            providers=[FakeProvider("broken", ProviderError())],
            clock=Clock(),
        )
        assert tracker.once() is False
        assert store.pending_count(["first"]) == 0


class FakeStop:
    def __init__(self, clock):
        self.clock = clock
        self.stopped = False

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.clock.advance(seconds)


def test_delivery_retry_does_not_trigger_extra_provider_poll(make_settings, make_event):
    settings = make_settings()
    clock = Clock(0)
    stop = FakeStop(clock)
    polls, sends = [], []

    class ScheduledProvider:
        name = "scheduled"

        def fetch(self, client):
            polls.append(clock())
            if len(polls) == 2:
                stop.set()
            return []

    def handle(request):
        sends.append(clock())
        return httpx.Response(503 if len(sends) == 1 else 200)

    with (
        Store(settings.database_path) as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
    ):
        store.record_snapshot([make_event(1)], ["first"], clock())
        Tracker(settings, store, client, providers=[ScheduledProvider()], clock=clock).run(
            stop, monotonic=clock
        )
    assert polls == [0, 600]
    assert sends == [0, 30]


def test_slow_poll_skips_missed_ticks(make_settings):
    settings = make_settings()
    clock = Clock(0)
    stop = FakeStop(clock)
    polls = []

    class SlowProvider:
        name = "slow"

        def fetch(self, client):
            polls.append(clock())
            if len(polls) == 1:
                clock.advance(650)
            else:
                stop.set()
            return []

    with Store(settings.database_path) as store, httpx.Client() as client:
        Tracker(settings, store, client, providers=[SlowProvider()], clock=clock).run(
            stop, monotonic=clock
        )
    assert polls == [0, 1200]
