import math
from datetime import UTC
from email.utils import parsedate_to_datetime

import httpx


def _seconds(value: object) -> float | None:
    try:
        seconds = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def retry_delay(response: httpx.Response, now: float) -> float | None:
    """Use the longest valid server hint; headers can contain seconds or HTTP dates."""
    delays = []
    header = response.headers.get("Retry-After")
    if header:
        delay = _seconds(header)
        if delay is None:
            try:
                date = parsedate_to_datetime(header)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=UTC)
                delay = max(0, date.timestamp() - now)
            except (ValueError, TypeError, OverflowError):
                pass
        if delay is not None:
            delays.append(delay)
    if response.status_code == 429:
        try:
            payload = response.json()
            delay = _seconds(payload.get("retry_after")) if isinstance(payload, dict) else None
            if delay is not None:
                delays.append(delay)
        except ValueError:
            pass
    if response.headers.get("X-RateLimit-Remaining") == "0":
        delay = _seconds(response.headers.get("X-RateLimit-Reset-After"))
        if delay is not None:
            delays.append(delay)
    return max(delays) if delays else None
