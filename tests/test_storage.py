import sqlite3
from dataclasses import replace

import pytest

from reset_tracker.storage import Store


def complete(store, webhook="first"):
    head = store.heads([webhook])[0]
    store.delivered(head.id, webhook)
    return head


def test_initial_latest_only_and_restart(tmp_path, make_event):
    path = tmp_path / "state.sqlite3"
    events = [make_event(3), make_event(1), make_event(2)]
    with Store(path) as store:
        assert store.record_snapshot(events, ["first"], 1000) == 1
        assert complete(store).event.id == "3"
    with Store(path) as store:
        assert store.record_snapshot(events, ["first"], 2000) == 0
        assert store.pending_count(["first"]) == 0


def test_empty_initial_snapshot_then_catch_up(tmp_path, make_event):
    with Store(tmp_path / "state.sqlite3") as store:
        assert store.record_snapshot([], ["first"], 1000) == 0
        assert store.record_snapshot([make_event(2), make_event(1)], ["first"], 1001) == 2
        assert complete(store).event.id == "1"
        assert complete(store).event.id == "2"


def test_catch_up_equal_timestamps_and_watermark(tmp_path, make_event):
    with Store(tmp_path / "state.sqlite3") as store:
        store.record_snapshot([make_event(1)], ["first"], 1000)
        complete(store)
        assert store.record_snapshot([make_event(3), make_event(2)], ["first"], 1001) == 2
        assert complete(store).event.id == "2"
        assert complete(store).event.id == "3"
        same_time = make_event(4, announced_at=make_event(3).announced_at)
        assert store.record_snapshot([same_time], ["first"], 1002) == 1
        assert complete(store).event.id == "4"
        watermark = store._metadata("watermark")
        assert store.record_snapshot([make_event(0)], ["first"], 1003) == 0
        assert store._metadata("watermark") == watermark
        assert store.record_snapshot([make_event(5)], ["first"], 1004) == 1


def test_shared_source_url_identity_across_providers(tmp_path, make_event):
    with Store(tmp_path / "state.sqlite3") as store:
        original = make_event(1, url="https://twitter.com/example/status/1?s=20")
        store.record_snapshot([original], ["first"], 1000)
        complete(store)
        alternate = replace(
            original,
            id="another-id",
            provider="fallback",
            url="https://x.com/example/status/1",
        )
        assert store.record_snapshot([alternate], ["first"], 2000) == 0


def test_snapshot_and_delivery_insert_are_atomic(tmp_path, make_event):
    with Store(tmp_path / "state.sqlite3") as store:
        store.connection.execute(
            "CREATE TRIGGER abort_queue BEFORE INSERT ON deliveries "
            "BEGIN SELECT RAISE(ABORT, 'simulated queue failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.record_snapshot([make_event(1)], ["first"], 1000)
        assert store._metadata("initialized") is None
        assert store._metadata("watermark") is None
        assert store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_added_and_removed_webhooks_do_not_replay_history(tmp_path, make_event):
    with Store(tmp_path / "state.sqlite3") as store:
        store.record_snapshot([make_event(1)], ["first"], 1000)
        assert store.heads(["second"]) == []
        assert store.record_snapshot([make_event(1)], ["second"], 1001) == 0
        assert store.record_snapshot([make_event(2)], ["second"], 1002) == 1
        assert store.heads(["second"])[0].event.id == "2"
        assert store.heads(["first"])[0].event.id == "1"


def test_duplicate_snapshot_events_are_coalesced(tmp_path, make_event):
    with Store(tmp_path / "state.sqlite3") as store:
        assert store.record_snapshot([make_event(1)] * 3, ["first"], 1000) == 1
