import math
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .diagnostics import remember_secret, remember_webhook
from .models import http_url, sample_event
from .providers import PROVIDERS

PRESETS = {
    "generic_json": "Normalized event and detection timestamp as JSON",
    "discord": "Discord message with source attribution and mentions disabled",
}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class WebhookConfig:
    id: str
    url: str
    preset: str = "generic_json"
    method: str = "POST"
    headers: dict[str, str] | None = None
    body_format: str = "json"
    body_template: str | None = None


@dataclass(frozen=True)
class Settings:
    user_agent: str
    database_path: Path
    webhooks: tuple[WebhookConfig, ...]
    providers: tuple[str, ...] = ("codex_reset",)
    interval_seconds: int = 600
    request_timeout_seconds: float = 20


def _expand(value: str) -> str:
    def replace(match: re.Match) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ConfigError(f"missing environment variable: {name}")
        value = os.environ[name]
        remember_secret(value)
        return value

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} must be a nonempty string")
    return value


def _keys(value: dict, allowed: set[str], context: str) -> None:
    if value.keys() - allowed:
        raise ConfigError(f"unknown field in {context}")


def load_config(path: str | Path) -> Settings:
    from .templates import RenderError, render_request

    path = Path(path).resolve()
    try:
        with path.open("rb") as config_file:
            data = tomllib.load(config_file)
        _keys(
            data,
            {
                "user_agent",
                "database_path",
                "providers",
                "interval_seconds",
                "request_timeout_seconds",
                "webhooks",
            },
            "configuration",
        )
        user_agent = _expand(_string(data.get("user_agent"), "user_agent"))
        if (
            not re.match(r"^[^\s/]+/[^\s]+ ", user_agent)
            or not re.search(r"\(\+(?:https?://[^\s)]+|mailto:[^\s@)]+@[^\s@)]+)\)", user_agent)
            or any(ord(char) < 32 or ord(char) > 126 for char in user_agent)
        ):
            raise ConfigError(
                "user_agent must identify project/version and a (+URL or +mailto:email)"
            )
        interval = data.get("interval_seconds", 600)
        if type(interval) is not int or interval < 60:
            raise ConfigError("interval_seconds must be an integer of at least 60")
        timeout = data.get("request_timeout_seconds", 20)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ConfigError("request_timeout_seconds must be a positive finite number")
        providers = data.get("providers", ["codex_reset"])
        if (
            not isinstance(providers, list)
            or not providers
            or any(not isinstance(item, str) or item not in PROVIDERS for item in providers)
            or len(set(providers)) != len(providers)
        ):
            raise ConfigError("providers must be a nonempty list of unique registered provider IDs")
        database = Path(
            _expand(
                _string(data.get("database_path", "data/reset-tracker.sqlite3"), "database_path")
            )
        )
        if not database.is_absolute():
            database = path.parent / database
        entries = data.get("webhooks")
        if not isinstance(entries, list) or not entries:
            raise ConfigError("configure at least one webhook")
        webhooks = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ConfigError("each webhook must be a TOML table")
            _keys(
                entry,
                {
                    "id",
                    "url",
                    "preset",
                    "method",
                    "headers",
                    "body_format",
                    "body_template",
                    "template_file",
                },
                "webhook",
            )
            webhook_id = _string(entry.get("id"), "webhook.id")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", webhook_id):
                raise ConfigError(
                    "webhook.id must use 1-100 letters, digits, underscores or hyphens"
                )
            if any(webhook.id == webhook_id for webhook in webhooks):
                raise ConfigError("webhook IDs must be unique")
            try:
                url = http_url(_expand(_string(entry.get("url"), "webhook.url")))
            except ValueError as exc:
                raise ConfigError(f"invalid URL for webhook {webhook_id}") from exc
            preset = entry.get("preset", "generic_json")
            if not isinstance(preset, str) or preset not in {*PRESETS, "custom"}:
                raise ConfigError("webhook preset must be generic_json, discord or custom")
            method = _string(entry.get("method", "POST"), "webhook.method").upper()
            if not re.fullmatch(r"[A-Z]+", method) or (preset == "discord" and method != "POST"):
                raise ConfigError("invalid webhook method (Discord requires POST)")
            headers = entry.get("headers", {})
            if not isinstance(headers, dict):
                raise ConfigError("webhook.headers must be a string table")
            expanded_headers = {}
            for key, value in headers.items():
                if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key):
                    raise ConfigError("invalid webhook header name")
                value = _expand(_string(value, "webhook header"))
                if any(ord(char) < 32 or ord(char) > 126 for char in value):
                    raise ConfigError("webhook header must contain printable ASCII")
                expanded_headers[key] = value
            body_format = entry.get("body_format", "json")
            if body_format not in ("json", "text") or (
                preset != "custom" and body_format != "json"
            ):
                raise ConfigError("body_format must be json or text; presets require json")
            template = entry.get("body_template")
            template_file = entry.get("template_file")
            if preset == "custom":
                if (template is None) == (template_file is None):
                    raise ConfigError(
                        "custom webhook requires exactly one body_template/template_file"
                    )
                if template_file is not None:
                    template_path = Path(_expand(_string(template_file, "template_file")))
                    template = (path.parent / template_path).read_text(encoding="utf-8")
                else:
                    template = _string(template, "body_template")
            elif template is not None or template_file is not None:
                raise ConfigError("body templates require preset = custom")
            webhook = WebhookConfig(
                id=webhook_id,
                url=url,
                preset=preset,
                method=method,
                headers=expanded_headers,
                body_format=body_format,
                body_template=template,
            )
            remember_webhook(webhook.url, webhook.headers)
            try:
                render_request(webhook, sample_event(), "2026-01-01T12:00:00.000000Z")
            except RenderError as exc:
                raise ConfigError(f"invalid template for webhook {webhook_id}") from exc
            webhooks.append(webhook)
        return Settings(
            user_agent=user_agent,
            database_path=database,
            webhooks=tuple(webhooks),
            providers=tuple(providers),
            interval_seconds=interval,
            request_timeout_seconds=float(timeout),
        )
    except ConfigError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        # Do not echo TOML values, URLs, credentials or template contents.
        raise ConfigError("could not read or parse configuration/template files") from exc
