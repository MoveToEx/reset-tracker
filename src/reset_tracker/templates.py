from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment

from .models import ResetEvent

if TYPE_CHECKING:
    from .config import WebhookConfig


class RenderError(Exception):
    pass


@dataclass(frozen=True)
class RenderedRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes


_environment = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
_environment.globals.clear()


def _limit(value: str, units: int) -> str:
    # Count UTF-16 units, including two units for astral Unicode characters.
    return value.encode("utf-16-le")[: units * 2].decode("utf-16-le", errors="ignore")


def render_request(webhook: WebhookConfig, event: ResetEvent, detected_at: str) -> RenderedRequest:
    headers = httpx.Headers(webhook.headers or {})
    url = webhook.url
    try:
        if webhook.preset == "discord":
            url = str(httpx.URL(url).copy_set_param("wait", "true"))
            # Bound links as well as the summary to stay below Discord's content limit.
            message = (
                "Codex reset confirmed\n"
                f"{event.announced_at.isoformat()}\n\n"
                f"{_limit(event.summary, 1000)}\n\n"
                f"Announcement: <{_limit(event.url, 500)}>\n"
                f"Data: <{_limit(event.source_url, 250)}>"
            )
            payload = {"content": _limit(message, 2000), "allowed_mentions": {"parse": []}}
        elif webhook.preset == "generic_json":
            payload = {"event": event.to_dict(), "detected_at": detected_at}
        else:
            rendered = _environment.from_string(webhook.body_template or "").render(
                event=event.to_dict(), detected_at=detected_at
            )
            if webhook.body_format == "text":
                headers.setdefault("Content-Type", "text/plain; charset=utf-8")
                return RenderedRequest(webhook.method, url, dict(headers), rendered.encode("utf-8"))
            payload = json.loads(rendered)
        headers.setdefault("Content-Type", "application/json")
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return RenderedRequest(webhook.method, url, dict(headers), body)
    except (TemplateError, ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise RenderError("webhook body could not be rendered") from exc
