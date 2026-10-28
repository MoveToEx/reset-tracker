import logging
import math
import threading
import time
from collections.abc import Callable

import httpx

from .config import Settings
from .diagnostics import log_exception, remember_webhook
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
        for webhook in settings.webhooks:
            remember_webhook(webhook.url, webhook.headers)

    def poll(self) -> bool:
        return self._poll("forecast")

    def poll_confirmations(self) -> bool:
        return self._poll("confirmed")

    def _poll(self, stream: str) -> bool:
        eligible = False
        for provider in self.providers:
            fetch = (
                getattr(provider, "fetch_confirmations", None)
                if stream == "confirmed"
                else provider.fetch
            )
            if fetch is None:
                continue
            eligible = True
            state_id = f"{provider.name}:{stream}"
            # Honor old whole-provider cooldowns until they expire. New failures
            # cool down only the affected endpoint/stream.
            if not all(
                self.store.provider_ready(key, self.clock()) for key in (provider.name, state_id)
            ):
                logger.info("provider=%s stream=%s outcome=cooldown", provider.name, stream)
                continue
            try:
                events = fetch(self.client)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                delay = retry_delay(exc.response, self.clock())
                if status == 429 or delay is not None:
                    self.store.cool_provider(state_id, self.clock() + max(60, delay or 0))
                log_exception(
                    logging.WARNING,
                    "provider=%s stream=%s outcome=http_error status=%s",
                    provider.name,
                    stream,
                    status,
                )
                continue
            except (httpx.HTTPError, ProviderError) as exc:
                log_exception(
                    logging.WARNING,
                    "provider=%s stream=%s outcome=failed error=%s",
                    provider.name,
                    stream,
                    type(exc).__name__,
                )
                continue
            if stream == "confirmed":
                snapshots = {"confirmed": [event for event in events if event.kind == "confirmed"]}
            else:
                latest_reset = self.store.latest_confirmed_at()
                events = [
                    event
                    for event in events
                    if event.kind != "forecast"
                    or latest_reset is None
                    or event.announced_at > latest_reset
                ]
                namespaces = getattr(provider, "cache_namespaces", None)
                if namespaces is None:
                    snapshots = {getattr(provider, "cache_namespace", None): events}
                else:
                    snapshots = {
                        namespace: [event for event in events if event.kind == namespace]
                        for namespace in namespaces
                    }
            queued = self.store.record_snapshots(snapshots, list(self.webhooks), self.clock())
            logger.info(
                "provider=%s stream=%s outcome=success resets=%s queued=%s",
                provider.name,
                stream,
                len(events),
                queued,
            )
            return True
        if stream == "confirmed" and not eligible:
            return True
        logger.warning("poll stream=%s outcome=all_providers_unavailable", stream)
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
                log_exception(
                    logging.WARNING,
                    "endpoint=%s delivery=%s exception=%s",
                    webhook.id,
                    delivery.id,
                    reason,
                )
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                reason = type(exc).__name__
                log_exception(
                    logging.WARNING,
                    "endpoint=%s delivery=%s exception=%s",
                    webhook.id,
                    delivery.id,
                    reason,
                )
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
        confirmed = self.poll_confirmations()
        fetched = self.poll()
        while True:
            attempted, _ = self.dispatch_due()
            if not attempted:
                break
        return fetched and confirmed and self.store.pending_count(list(self.webhooks)) == 0

    def run(
        self,
        stop: threading.Event,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        started_at = monotonic()
        next_polls = {}
        intervals = {
            "confirmed": self.settings.confirmation_interval_seconds,
            "forecast": self.settings.interval_seconds,
        }
        polls = {"confirmed": self.poll_confirmations, "forecast": self.poll}
        # Confirmations run first when both streams are due, so a newly confirmed
        # occurrence can suppress an already fulfilled forecast using durable state.
        if any(getattr(provider, "fetch_confirmations", None) for provider in self.providers):
            next_polls["confirmed"] = started_at
        next_polls["forecast"] = started_at
        while not stop.is_set():
            for stream, next_poll in next_polls.items():
                if stop.is_set():
                    break
                if monotonic() >= next_poll:
                    polls[stream]()
                    interval = intervals[stream]
                    # Preserve each cadence and skip its missed ticks independently.
                    next_polls[stream] += (
                        math.floor((monotonic() - next_poll) / interval) + 1
                    ) * interval
            if stop.is_set():
                break
            self.dispatch_due()
            heads = self.store.heads(list(self.webhooks))
            wait = min(next_polls.values()) - monotonic()
            if heads:
                wait = min(wait, min(head.due_at for head in heads) - self.clock())
            stop.wait(max(0, min(60, wait)))
