"""Run with Python locally, or mount in the built container and pass --data-dir /data/smoke."""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def smoke(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "state.sqlite3"
    if database.exists():
        raise RuntimeError("smoke directory must not already contain state.sqlite3")
    state = {"latest": 2, "fail": True, "posts": [], "gets": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            state["gets"] += 1
            body = json.dumps(
                {
                    "events": [
                        {
                            "id": str(number),
                            "group": "reset",
                            "announcement_state": "announced",
                            "announced_at": f"2026-01-01T12:00:0{number}Z",
                            "summary": f"Reset {number}",
                            "url": f"https://example.com/resets/{number}",
                        }
                        for number in range(1, state["latest"] + 1)
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["posts"].append(payload["event"]["id"])
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
        '[[webhooks]]\nid = "test"\n'
        f'url = "{base_url}/webhook"\n',
        encoding="utf-8",
    )
    # Only the mock endpoint is substituted. All CLI, HTTP, parsing and persistence code is real.
    bootstrap = (
        "from reset_tracker.providers import CodexResetProvider; "
        f"CodexResetProvider.endpoint = {base_url + '/api/timeline'!r}; "
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
        assert state["posts"] == ["2"], state
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT attempts, status FROM deliveries").fetchone() == (
                1,
                "pending",
            )
            # Advance the persisted retry deadline instead of sleeping for 30 seconds.
            connection.execute("UPDATE deliveries SET next_attempt_at = 0")
            connection.execute("UPDATE endpoint_state SET cooldown_until = 0")
        state["fail"] = False
        state["latest"] = 4
        invoke(0)
        assert state["posts"] == ["2", "2", "3", "4"], state
        invoke(0)
        assert state["posts"] == ["2", "2", "3", "4"], state
        assert state["gets"] == 3, state
        with sqlite3.connect(database) as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM deliveries WHERE status = 'delivered'"
                ).fetchone()[0]
                == 3
            )
        print(
            "Smoke passed: HTTP collection, failed-send recovery, process restart, "
            "chronological catch-up, and no replay."
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
