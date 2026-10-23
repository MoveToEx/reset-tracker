import logging
import re
import sys
import traceback
from collections.abc import Mapping
from urllib.parse import parse_qsl, unquote, urlsplit

_logger = logging.getLogger("reset_tracker")
_urls = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_secrets: set[str] = set()


def reset_redaction() -> None:
    _secrets.clear()


def remember_secret(value: str) -> None:
    if value:
        _secrets.add(value)
        # Exception messages and traceback source lines can contain escaped strings.
        _secrets.add(repr(value)[1:-1])


def remember_webhook(url: str, headers: Mapping[str, str] | None) -> None:
    remember_secret(url)
    parts = urlsplit(url)
    if parts.password:
        remember_secret(unquote(parts.password))
    for key, value in parse_qsl(parts.query):
        if re.search(r"token|key|secret|password|auth", key, re.IGNORECASE):
            remember_secret(value)
    if parts.hostname in {"discord.com", "discordapp.com"} and "/api/webhooks/" in parts.path:
        remember_secret(parts.path.rstrip("/").rsplit("/", 1)[-1])
    for value in (headers or {}).values():
        remember_secret(value)
        scheme, separator, credential = value.partition(" ")
        if separator and scheme.lower() in {"bearer", "basic"}:
            remember_secret(credential)


def log_exception(level: int, message: str, *args: object) -> None:
    """Log the active exception and its chain without exposing raw exception records."""
    exception = sys.exception()
    details = "".join(traceback.format_exception(exception)) if exception is not None else ""
    text = (message % args if args else message) + "\n" + details.rstrip()
    # Redact before creating the LogRecord so every handler, including capture handlers,
    # receives the same protected detail. No local variable values are captured.
    text = _urls.sub("[REDACTED_URL]", text)
    for secret in sorted(_secrets, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    _logger.log(level, "%s", text)
