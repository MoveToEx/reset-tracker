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

_BUILTIN_TEMPLATES = {
    "discord": """
{
  "embeds": [
    {
      "title": {{ ("Codex reset " ~ event.kind) | tojson }},
      "url": {{ event.url | tojson }},
      "description": {{ event.summary | limit_utf16(4096) | tojson }},
      "timestamp": {{ event.announced_at | tojson }},
      "color": 5793266,
      "fields": [
        {
          "name": "Announcement",
          "value": {{ event.url | limit_utf16(500) | tojson }}
        },
        {
          "name": "Data",
          "value": {{ event.source_url | limit_utf16(250) | tojson }}
        }
        {% if event.probability is not none %},
        {
          "name": "Signal probability",
          "value": {{ ("%g%%" | format(event.probability)) | tojson }},
          "inline": true
        }
        {% endif %}
        {% if event.probability_24h is not none %},
        {
          "name": "24h cadence probability",
          "value": {{ ("%g%%" | format(event.probability_24h)) | tojson }},
          "inline": true
        }
        {% endif %}
        {% if event.probability_48h is not none %},
        {
          "name": "48h cadence probability",
          "value": {{ ("%g%%" | format(event.probability_48h)) | tojson }},
          "inline": true
        }
        {% endif %}
        {% if event.window_label %},
        {
          "name": "Forecast window",
          "value": {{ event.window_label | limit_utf16(500) | tojson }}
        }
        {% endif %}
        {% if event.expected_at is not none %},
        {
          "name": "Expected by (UTC)",
          "value": {{ event.expected_at | tojson }}
        }
        {% endif %}
        {% if event.effective_at is not none %},
        {
          "name": "Reset effective at (UTC)",
          "value": {{ event.effective_at | tojson }}
        }
        {% endif %}
      ]
    }
  ],
  "allowed_mentions": {"parse": []}
}
""",
    "generic_json": """
{"event": {{ event | tojson }}, "detected_at": {{ detected_at | tojson }}}
""",
}


def _limit(value: str, units: int) -> str:
    # Count UTF-16 units, including two units for astral Unicode characters.
    return value.encode("utf-16-le")[: units * 2].decode("utf-16-le", errors="ignore")


_environment.filters["limit_utf16"] = _limit


def render_request(webhook: WebhookConfig, event: ResetEvent, detected_at: str) -> RenderedRequest:
    headers = httpx.Headers(webhook.headers or {})
    url = webhook.url
    try:
        template = webhook.body_template
        if template is None:
            template = _BUILTIN_TEMPLATES.get(webhook.preset, "")
        rendered = _environment.from_string(template).render(
            event=event.to_dict(), detected_at=detected_at
        )
        if webhook.body_format == "text":
            headers.setdefault("Content-Type", "text/plain; charset=utf-8")
            return RenderedRequest(webhook.method, url, dict(headers), rendered.encode("utf-8"))
        payload = json.loads(rendered)
        if not isinstance(payload, dict):
            raise ValueError("webhook body must render to a JSON object")
        if webhook.preset == "discord":
            url = str(httpx.URL(url).copy_set_param("wait", "true"))
            payload.setdefault("allowed_mentions", {"parse": []})
        headers.setdefault("Content-Type", "application/json")
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return RenderedRequest(webhook.method, url, dict(headers), body)
    except (TemplateError, ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise RenderError("webhook body could not be rendered") from exc
