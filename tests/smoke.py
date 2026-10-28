"""Run with Python locally, or mount in the built container and pass --data-dir /data/smoke."""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def smoke(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "state.sqlite3"
    if database.exists():
        raise RuntimeError("smoke directory must not already contain state.sqlite3")
    state = {
        "latest": 2,
        "confirmed": False,
        "follow_up": False,
        "next_reset": False,
        "fail": True,
        "posts": [],
        "gets": 0,
    }
    base_time = datetime.now(UTC) - timedelta(hours=1)
    deadline = (base_time + timedelta(days=1)).isoformat(timespec="microseconds")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            assert self.path in {"/api/forecast", "/api/timeline"}
            state["gets"] += 1
            if self.path == "/api/timeline":
                payload = {
                    "events": [
                        {
                            "id": "4",
                            "group": "reset",
                            "announcement_state": "announced",
                            "announced_at": (base_time + timedelta(seconds=4)).isoformat(),
                            "observed_at": (base_time + timedelta(seconds=5)).isoformat(),
                            "summary": "Reset 4 is now in effect",
                            "url": "https://example.com/resets/4",
                        }
                    ]
                    if state["confirmed"]
                    else []
                }
                if state["confirmed"]:
                    payload["events"].append(
                        {
                            "id": "4-other",
                            "group": "reset",
                            "announcement_state": "announced",
                            "announced_at": (base_time + timedelta(seconds=6)).isoformat(),
                            "observed_at": (base_time + timedelta(seconds=6)).isoformat(),
                            "summary": "Another post about reset 4",
                            "url": "https://example.com/resets/4-other",
                        }
                    )
                if state["follow_up"]:
                    payload["events"].insert(
                        0,
                        {
                            "id": "4-later",
                            "group": "reset",
                            "announcement_state": "announced",
                            "announced_at": (base_time + timedelta(seconds=60)).isoformat(),
                            "summary": "Follow-up post about reset 4",
                            "url": "https://example.com/resets/4-later",
                        },
                    )
                if state["next_reset"]:
                    payload["events"].insert(
                        0,
                        {
                            "id": "5",
                            "group": "reset",
                            "announcement_state": "announced",
                            "announced_at": (base_time + timedelta(seconds=600)).isoformat(),
                            "observed_at": (base_time + timedelta(seconds=600)).isoformat(),
                            "summary": "A genuinely separate reset",
                            "url": "https://example.com/resets/5",
                        },
                    )
            else:
                payload = {
                    "official_signal": {
                        "tweet_id": str(state["latest"]),
                        "kind": "signal",
                        "at": (base_time + timedelta(seconds=state["latest"])).isoformat(),
                        "summary": f"Forecast reset {state['latest']}",
                        "url": f"https://example.com/resets/{state['latest']}",
                        "window": {
                            "target_at": deadline,
                            "end_at": deadline,
                            "label": "within 24 hours",
                        },
                    },
                    "last_reset_at": (
                        base_time + timedelta(seconds=5)
                        if state["confirmed"]
                        else base_time - timedelta(days=1)
                    ).isoformat(),
                    "probabilities": {
                        "signal_percent": 93,
                        "rounded_24h": 32,
                        "rounded_48h": 53,
                    },
                }
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            event = payload["event"]
            if event["kind"] == "forecast":
                assert event["probability"] == 93
                assert event["probability_24h"] == 32
                assert event["probability_48h"] == 53
                assert event["expected_at"] is not None
                assert event["effective_at"] is None
            else:
                assert event["kind"] == "confirmed"
                assert event["probability"] is None
                assert event["expected_at"] is None
                assert event["effective_at"] is not None
                assert event["occurrence_id"].startswith("reset-")
            state["posts"].append((event["kind"], event["id"]))
            self.send_response(503 if state["fail"] else 200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    config = directory / "config.toml"
    config.write_text(
        'user_agent = "smoke/1.0 (+mailto:smoke@example.com)"\n'
        'database_path = "state.sqlite3"\n'
        "confirmation_dedup_window_seconds = 120\n"
        '[[webhooks]]\nid = "test"\n'
        f'url = "{base_url}/webhook"\n',
        encoding="utf-8",
    )
    # Only the mock endpoint is substituted. All CLI, HTTP, parsing and persistence code is real.
    bootstrap = (
        "from reset_tracker.providers import CodexResetProvider; "
        f"CodexResetProvider.endpoint = {base_url + '/api/forecast'!r}; "
        f"CodexResetProvider.timeline_endpoint = {base_url + '/api/timeline'!r}; "
        "from reset_tracker.cli import main; raise SystemExit(main())"
    )
    environment = {
        **os.environ,
        "NO_PROXY": "localhost,127.0.0.1",
        "no_proxy": "localhost,127.0.0.1",
    }

    def invoke(expected):
        result = subprocess.run(
            [sys.executable, "-c", bootstrap, "--config", str(config), "once"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != expected:
            raise AssertionError(f"Unexpected CLI result: {result.stdout}\n{result.stderr}")

    try:
        invoke(1)
        assert state["posts"] == [("forecast", "2")], state
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT attempts, status FROM deliveries").fetchone() == (
                1,
                "pending",
            )
            # Advance the persisted retry deadline instead of sleeping for 30 seconds.
            connection.execute("UPDATE deliveries SET next_attempt_at = 0")
            connection.execute("UPDATE endpoint_state SET cooldown_until = 0")
        state["fail"] = False
        state["latest"] = 3
        invoke(0)
        assert state["posts"] == [("forecast", n) for n in ("2", "2", "3")], state
        state["latest"] = 4
        invoke(0)
        assert state["posts"] == [("forecast", n) for n in ("2", "2", "3", "4")], state
        invoke(0)
        assert state["posts"] == [("forecast", n) for n in ("2", "2", "3", "4")], state
        state["confirmed"] = True
        invoke(0)
        expected = [("forecast", n) for n in ("2", "2", "3", "4")] + [("confirmed", "4")]
        assert state["posts"] == expected, state
        state["follow_up"] = True
        invoke(0)
        assert state["posts"] == expected, state
        state["next_reset"] = True
        invoke(0)
        expected += [("confirmed", "5")]
        assert state["posts"] == expected, state
        invoke(0)
        assert state["posts"] == expected, state
        assert state["gets"] == 16, state
        with sqlite3.connect(database) as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM deliveries WHERE status = 'delivered'"
                ).fetchone()[0]
                == 5
            )
            assert connection.execute("SELECT COUNT(*) FROM reset_occurrences").fetchone()[0] == 2
            assert connection.execute("SELECT COUNT(*) FROM reset_sources").fetchone()[0] == 4
        print(
            "Smoke passed: HTTP collection, failed-send recovery, process restart, "
            "chronological catch-up, forecast-to-confirmation transition, "
            "persistent multi-post deduplication, and no replay."
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    if args.data_dir:
        smoke(args.data_dir)
    else:
        with tempfile.TemporaryDirectory(prefix="reset-tracker-smoke-") as temporary:
            smoke(Path(temporary))
