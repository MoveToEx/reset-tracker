import argparse
import json
import logging
import signal
import sqlite3
import sys
import threading
import time
from collections.abc import Sequence

import httpx

from .config import PRESETS, ConfigError, load_config
from .models import sample_event
from .service import Tracker
from .storage import Store
from .templates import render_request


class UTCFormatter(logging.Formatter):
    converter = time.gmtime


def _logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        UTCFormatter("%(asctime)sZ %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S")
    )
    logger = logging.getLogger("reset_tracker")
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # HTTPX's INFO logs include full webhook URLs, so keep dependency loggers quiet.
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect Codex resets and deliver webhook alerts")
    parser.add_argument("--config", default="config.toml", help="TOML configuration path")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "once", "validate", "presets"):
        subparsers.add_parser(name)
    preview = subparsers.add_parser("preview", help="Render a sample without making any requests")
    preview.add_argument("--webhook", required=True, help="Configured webhook ID")
    retry = subparsers.add_parser("retry-failed", help="Resume paused endpoints without sending")
    retry.add_argument("--webhook", help="Only resume this configured webhook ID")
    args = parser.parse_args(argv)
    _logging()
    logger = logging.getLogger("reset_tracker")
    if args.command == "presets":
        for name, description in PRESETS.items():
            print(f"{name}: {description}")
        return 0
    try:
        settings = load_config(args.config)
        if args.command == "validate":
            print("Configuration and sample payloads are valid.")
            return 0
        if args.command == "preview":
            webhook = next((item for item in settings.webhooks if item.id == args.webhook), None)
            if webhook is None:
                raise ConfigError("unknown webhook ID")
            request = render_request(webhook, sample_event(), "2026-01-01T12:00:00.000000Z")
            # Preview intentionally excludes URLs and headers, which may contain credentials.
            body = request.body.decode("utf-8")
            if webhook.body_format == "json":
                body = json.dumps(json.loads(body), ensure_ascii=False, indent=2)
            print(body)
            return 0
        with Store(settings.database_path) as store:
            if args.command == "retry-failed":
                ids = [item.id for item in settings.webhooks]
                if args.webhook:
                    if args.webhook not in ids:
                        raise ConfigError("unknown webhook ID")
                    ids = [args.webhook]
                print(f"Resumed {store.retry_failed(ids, time.time())} endpoint(s).")
                return 0
            with httpx.Client(
                timeout=settings.request_timeout_seconds,
                headers={"User-Agent": settings.user_agent},
                follow_redirects=False,
            ) as client:
                tracker = Tracker(settings, store, client)
                if args.command == "once":
                    return 0 if tracker.once() else 1
                stop = threading.Event()

                def shutdown(signum: int, frame: object) -> None:
                    stop.set()

                for signum in (signal.SIGINT, signal.SIGTERM):
                    signal.signal(signum, shutdown)
                logger.info("service outcome=started interval=%s", settings.interval_seconds)
                tracker.run(stop)
                logger.info("service outcome=stopped")
                return 0
    except ConfigError as exc:
        logger.error("configuration error: %s", exc)
        return 2
    except (OSError, sqlite3.Error):
        logger.error("service outcome=failed reason=storage_or_filesystem_error")
        return 1
    except httpx.HTTPError as exc:
        logger.error("service outcome=failed reason=%s", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
