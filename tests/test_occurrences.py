import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from conftest import Clock

from reset_tracker.config import WebhookConfig
from reset_tracker.models import timestamp_text
from reset_tracker.service import Tracker
from reset_tracker.storage import Store

BASE = datetime(2026, 1, 2, 12, tzinfo=UTC)


@pytest.fixture
def post(make_event):
    def create(number, *, seconds=0, **changes):
        return make_event(
            number,
            kind="confirmed",
            announced_at=BASE + timedelta(seconds=seconds),
            probability=None,
            probability_24h=None,
            probability_48h=None,
            expected_at=None,
            window_label=None,
            **changes,
        )

    return create


def record(store, events, endpoints=None):
    return store.record_snapshot(
        events, endpoints if endpoints is not None else ["first"], 1000, cache_namespace="confirmed"
    )


def count(store, table):
    assert table in {"events", "reset_occurrences", "reset_sources", "deliveries"}
    return store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def drain(store, endpoint="first"):
    result = []
    while heads := store.heads([endpoint]):
        head = heads[0]
        result.append(head)
        store.delivered(head.id, endpoint)
    return result


def test_multiple_posts_one_reset_one_delivery_and_later_source_after_restart(tmp_path, post):
    path = tmp_path / "state.sqlite3"
    with Store(path) as store:
        record(store, [])
        assert record(store, [post(2, seconds=600), post(1)]) == 1
        assert count(store, "events") == count(store, "reset_sources") == 2
        assert count(store, "reset_occurrences") == 1
        (delivery,) = drain(store)
        assert delivery.event.id == "1"
        occurrence_id = delivery.event.occurrence_id
        assert occurrence_id.startswith("reset-")
        assert record(store, [post(1), post(2, seconds=600)]) == 0
    with Store(path) as store:
        assert record(store, [post(3, seconds=1700)]) == 0
        assert store.heads(["first"]) == []
        assert count(store, "reset_sources") == count(store, "events") == 3
        occurrence = store.connection.execute("SELECT * FROM reset_occurrences").fetchone()
        assert occurrence["id"] == occurrence_id
        assert occurrence["anchor_at"] == timestamp_text(BASE)
        assert store._metadata("confirmed:watermark") == timestamp_text(BASE)


def test_fixed_anchor_prevents_a_chain_of_posts_extending_the_window(tmp_path, post):
    with Store(tmp_path / "state.sqlite3") as store:
        record(store, [])
        assert record(store, [post(3, seconds=2400), post(2, seconds=1200), post(1)]) == 2
        assert [head.event.id for head in drain(store)] == ["1", "3"]
        assert count(store, "events") == 3
        assert count(store, "reset_occurrences") == 2


def test_first_initialization_sends_only_latest_occurrence_and_keeps_all_posts(tmp_path, post):
    with Store(tmp_path / "state.sqlite3") as store:
        events = [post(4, seconds=4200), post(2, seconds=600), post(3, seconds=3600), post(1)]
        assert record(store, events) == 1
        assert [head.event.id for head in drain(store)] == ["3"]
        assert count(store, "events") == count(store, "reset_sources") == 4
        assert count(store, "reset_occurrences") == 2
        assert record(store, events) == 0


def test_chronological_catch_up_with_several_posts_per_reset(tmp_path, post):
    with Store(tmp_path / "state.sqlite3") as store:
        record(store, [post(1)])
        drain(store)
        assert (
            record(
                store,
                [
                    post(5, seconds=7800),
                    post(3, seconds=4200),
                    post(4, seconds=7200),
                    post(2, seconds=3600),
                ],
            )
            == 2
        )
        assert [head.event.id for head in drain(store)] == ["2", "4"]
        assert count(store, "reset_occurrences") == 3


@pytest.mark.parametrize("window", [0, 1800])
def test_shared_explicit_reset_id_groups_posts_outside_window(tmp_path, post, window):
    with Store(tmp_path / "state.sqlite3", confirmation_dedup_window_seconds=window) as store:
        record(store, [])
        assert record(store, [post(1, reset_id="global-reset")]) == 1
        first = drain(store)[0]
        assert record(store, [post(2, seconds=86400, reset_id="global-reset")]) == 0
        assert count(store, "reset_occurrences") == 1
        assert (
            store.connection.execute("SELECT id FROM reset_occurrences").fetchone()[0]
            == first.event.occurrence_id
        )


def test_same_explicit_effect_time_groups_even_with_window_disabled(tmp_path, post):
    with Store(tmp_path / "state.sqlite3", confirmation_dedup_window_seconds=0) as store:
        record(store, [])
        events = [
            post(i, seconds=i * 3600, effective_at=BASE, effective_at_source="effective_at")
            for i in (1, 2)
        ]
        assert record(store, events) == 1
        assert count(store, "events") == 2
        assert count(store, "reset_occurrences") == 1


@pytest.mark.parametrize("strong_field", ["reset_id", "effective_at", "scope"])
def test_conflicting_strong_evidence_or_scopes_are_not_merged(tmp_path, post, strong_field):
    first = post(1)
    second = post(2, seconds=10)
    if strong_field == "reset_id":
        first = replace(first, reset_id="reset-a")
        second = replace(second, reset_id="reset-b")
    elif strong_field == "effective_at":
        first = replace(first, effective_at=BASE, effective_at_source="effective_at")
        second = replace(
            second, effective_at=BASE + timedelta(seconds=10), effective_at_source="effective_at"
        )
    else:
        second = replace(second, scope="team")
    with Store(tmp_path / "state.sqlite3") as store:
        record(store, [])
        assert record(store, [second, first]) == 2
        assert count(store, "reset_occurrences") == 2


def test_reset_id_is_authoritative_over_effect_time_corrections(tmp_path, post):
    with Store(tmp_path / "state.sqlite3") as store:
        record(store, [])
        first = post(1, reset_id="same", effective_at=BASE, effective_at_source="effective_at")
        later = post(
            2,
            reset_id="same",
            effective_at=BASE + timedelta(days=1),
            effective_at_source="effective_at",
        )
        assert record(store, [first]) == 1
        drain(store)
        assert record(store, [later]) == 0
        assert count(store, "reset_occurrences") == 1


def test_unrelated_providers_reusing_an_id_are_not_an_exact_match(tmp_path, post):
    with Store(tmp_path / "state.sqlite3") as store:
        record(store, [])
        first = post(1, reset_id="1")
        second = post(
            2, seconds=86400, reset_id="1", source_url="https://another-tracker.example.com/"
        )
        assert record(store, [first, second]) == 2


def test_observation_time_is_not_an_exact_occurrence_key(tmp_path, post):
    with Store(tmp_path / "state.sqlite3", confirmation_dedup_window_seconds=0) as store:
        record(store, [])
        assert (
            record(
                store,
                [post(i, effective_at=BASE, effective_at_source="observed_at") for i in (1, 2)],
            )
            == 2
        )


def test_configured_window_boundary_and_late_earlier_source(tmp_path, post):
    with Store(tmp_path / "state.sqlite3", confirmation_dedup_window_seconds=120) as store:
        record(store, [])
        assert record(store, [post(1)]) == 1
        drain(store)
        assert record(store, [post(2, seconds=120), post(3, seconds=-60)]) == 0
        assert record(store, [post(4, seconds=121)]) == 1
        assert count(store, "reset_occurrences") == 2


def test_supporting_sources_never_replay_to_newly_added_endpoints(tmp_path, post):
    with Store(tmp_path / "state.sqlite3") as store:
        record(store, [post(1)])
        drain(store)
        assert record(store, [post(2, seconds=600)], ["second"]) == 0
        assert record(store, [post(3, seconds=3600)], ["second"]) == 1
        assert store.heads(["second"])[0].event.id == "3"
        assert store.pending_count(["first"]) == 0


def test_occurrence_and_sources_roll_back_with_queue_failure(tmp_path, post):
    with Store(tmp_path / "state.sqlite3") as store:
        store.connection.execute(
            "CREATE TRIGGER abort_queue BEFORE INSERT ON deliveries "
            "BEGIN SELECT RAISE(ABORT, 'queue failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError):
            record(store, [post(1), post(2, seconds=60)])
        assert all(
            count(store, table) == 0
            for table in ("events", "reset_sources", "reset_occurrences", "deliveries")
        )
        assert store._metadata("confirmed:initialized") is None


def test_retry_stays_one_delivery_per_occurrence_and_endpoint_after_restart(make_settings, post):
    settings = make_settings(
        webhooks=(
            WebhookConfig("first", "https://example.com/first"),
            WebhookConfig("second", "https://example.com/second"),
        )
    )
    clock = Clock()
    calls = []
    fail = True

    def handle(request):
        event = json.loads(request.content)["event"]
        calls.append((request.url.path, event["id"], event["occurrence_id"]))
        return httpx.Response(503 if request.url.path == "/first" and fail else 200)

    with (
        Store(settings.database_path) as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
    ):
        record(store, [post(1), post(2, seconds=600)], ["first", "second"])
        assert Tracker(settings, store, client, clock=clock).dispatch_due() == (2, 1)
        first_occurrence = calls[0][2]
    fail = False
    clock.advance(30)
    with (
        Store(settings.database_path) as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
    ):
        assert record(store, [post(3, seconds=900)], ["first", "second"]) == 0
        assert Tracker(settings, store, client, clock=clock).dispatch_due() == (1, 0)
        assert count(store, "deliveries") == 2
        assert store.pending_count(["first", "second"]) == 0
    assert calls == [
        ("/first", "1", first_occurrence),
        ("/second", "1", first_occurrence),
        ("/first", "1", first_occurrence),
    ]


def seed_v1(path, events):
    with sqlite3.connect(path) as connection:
        connection.executescript((Path(__file__).parent / "fixtures/schema-v1.sql").read_text())
        for event in events:
            payload = event.to_dict()
            for field in ("effective_at_source", "reset_id", "scope", "occurrence_id"):
                payload.pop(field)
            connection.execute(
                "INSERT INTO events VALUES (?, ?, ?)",
                (event.identity, timestamp_text(event.recorded_at), json.dumps(payload)),
            )
            for endpoint in ("first", "second"):
                status = "delivered" if endpoint == "second" and event.id == "1" else "pending"
                connection.execute(
                    "INSERT INTO deliveries(event_identity, webhook_id, detected_at, "
                    "status, attempts, next_attempt_at) VALUES (?, ?, ?, ?, 2, 1030)",
                    (event.identity, endpoint, timestamp_text(BASE), status),
                )
        connection.execute("INSERT INTO metadata VALUES ('confirmed:initialized', '1')")
        connection.execute(
            "INSERT INTO metadata VALUES ('confirmed:watermark', ?)",
            (timestamp_text(events[-1].recorded_at),),
        )
        connection.execute("INSERT INTO endpoint_state VALUES ('first', 1, 'http_400', 1100)")
        connection.execute("INSERT INTO provider_state VALUES ('codex_reset', 1200)")


def test_v1_upgrade_preserves_history_and_suppresses_pending_duplicates(tmp_path, post):
    path = tmp_path / "old.sqlite3"
    seed_v1(path, [post(1), post(2, seconds=600)])
    with Store(path) as store:
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert count(store, "events") == count(store, "reset_sources") == 2
        assert count(store, "reset_occurrences") == 1
        assert count(store, "deliveries") == 4
        assert store.pending_count(["first"]) == 1
        assert store.pending_count(["second"]) == 0
        assert not store.provider_ready("codex_reset", 1199)
        assert store.heads(["first"]) == []
        assert store.retry_failed(["first", "second"], 1200) == 1
        (first,) = drain(store)
        assert first.event.id == "1"
        assert first.attempts == 2
        assert record(store, [post(3, seconds=900)], ["first", "second"]) == 0
        assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert store._metadata("confirmed:watermark") == timestamp_text(
            BASE + timedelta(seconds=600)
        )
        assert [
            row[0] for row in store.connection.execute("SELECT status FROM deliveries ORDER BY id")
        ] == ["delivered", "delivered", "pending", "pending"]
    with Store(path) as store:
        assert store.pending_count(["first", "second"]) == 0
        assert record(store, [post(4, seconds=1000)], ["first", "second"]) == 0


def test_migration_rolls_back_columns_and_data_if_association_fails(tmp_path, post, monkeypatch):
    path = tmp_path / "old.sqlite3"
    seed_v1(path, [post(1), post(2, seconds=600)])
    original = Store._associate_confirmation

    def fail_second(self, identity, event):
        result = original(self, identity, event)
        if event.id == "2":
            raise sqlite3.OperationalError("simulated migration failure")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Store, "_associate_confirmation", fail_second)
        with pytest.raises(sqlite3.OperationalError, match="simulated migration failure"):
            Store(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert "occurrence_id" not in {
            row[1] for row in connection.execute("PRAGMA table_info(deliveries)")
        }
        assert connection.execute("SELECT COUNT(*) FROM reset_occurrences").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 4
    with Store(path) as store:
        assert count(store, "reset_occurrences") == 1
