import logging
import math
import threading
import time
from collections.abc import Callable

import httpx

from .config import Settings
from .http import retry_delay
from .providers import PROVIDERS, Provider, ProviderError
from .storage import Store
from .templates import RenderError, render_request

logger = logging.getLogger("reset_tracker")


class Tracker:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        client: httpx.Client,
        *,
        providers: list[Provider] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self.store = store
        self.client = client
        self.providers = (
            providers
            if providers is not None
            else [PROVIDERS[name]() for name in settings.providers]
        )
        self.clock = clock
        self.webhooks = {webhook.id: webhook for webhook in settings.webhooks}

    def poll(self) -> bool:
        for provider in self.providers:
            if not self.store.provider_ready(provider.name, self.clock()):
                logger.info("provider=%s outcome=cooldown", provider.name)
                continue
            try:
                events = provider.fetch(self.client)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                delay = retry_delay(exc.response, self.clock())
                if status == 429 or delay is not None:
                    self.store.cool_provider(provider.name, self.clock() + max(60, delay or 0))
                logger.warning("provider=%s outcome=http_error status=%s", provider.name, status)
                continue
            except (httpx.HTTPError, ProviderError) as exc:
                logger.warning(
                    "provider=%s outcome=failed error=%s", provider.name, type(exc).__name__
                )
                continue
            queued = self.store.record_snapshot(events, list(self.webhooks), self.clock())
            logger.info(
                "provider=%s outcome=success resets=%s queued=%s",
                provider.name,
                len(events),
                queued,
            )
            return True
        logger.warning("poll outcome=all_providers_unavailable")
        return False

    def dispatch_due(self) -> tuple[int, int]:
        """Attempt at most one event per endpoint, so polling cannot be starved by a backlog."""
        attempted = failed = 0
        for delivery in self.store.heads(list(self.webhooks)):
            if delivery.due_at > self.clock():
                continue
            attempted += 1
            webhook = self.webhooks[delivery.webhook_id]
            reason = None
            transient = False
            delay = 0
            try:
                request = render_request(webhook, delivery.event, delivery.detected_at)
                response = self.client.request(
                    request.method, request.url, headers=request.headers, content=request.body
                )
                delay = retry_delay(response, self.clock()) or 0
                if response.is_success:
                    self.store.delivered(
                        delivery.id, webhook.id, self.clock() + delay if delay else 0
                    )
                    logger.info(
                        "endpoint=%s delivery=%s outcome=delivered", webhook.id, delivery.id
                    )
                    continue
                reason = f"http_{response.status_code}"
                transient = response.status_code in (408, 429) or response.status_code >= 500
            except RenderError:
                reason = "render_error"
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                reason = type(exc).__name__
                transient = not isinstance(exc, (httpx.InvalidURL, httpx.UnsupportedProtocol))
            failed += 1
            backoff = min(3600, 30 * 2 ** min(delivery.attempts, 7))
            retry_at = self.clock() + max(backoff, delay) if transient else None
            self.store.failed(delivery.id, webhook.id, reason, retry_at)
            logger.warning(
                "endpoint=%s delivery=%s outcome=%s reason=%s",
                webhook.id,
                delivery.id,
                "retry" if transient else "paused",
                reason,
            )
        return attempted, failed

    def once(self) -> bool:
        fetched = self.poll()
        while True:
            attempted, _ = self.dispatch_due()
            if not attempted:
                break
        return fetched and self.store.pending_count(list(self.webhooks)) == 0

    def run(
        self,
        stop: threading.Event,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        next_poll = monotonic()
        interval = self.settings.interval_seconds
        while not stop.is_set():
            if monotonic() >= next_poll:
                self.poll()
                # Retain the original cadence and skip ticks missed during a slow fetch.
                next_poll += (math.floor((monotonic() - next_poll) / interval) + 1) * interval
            if stop.is_set():
                break
            self.dispatch_due()
            heads = self.store.heads(list(self.webhooks))
            wait = next_poll - monotonic()
            if heads:
                wait = min(wait, min(head.due_at for head in heads) - self.clock())
            stop.wait(max(0, min(60, wait)))
