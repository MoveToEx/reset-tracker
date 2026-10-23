from dataclasses import replace
from datetime import timedelta

import pytest

from reset_tracker.config import Settings, WebhookConfig
from reset_tracker.diagnostics import reset_redaction
from reset_tracker.models import sample_event


@pytest.fixture(autouse=True)
def isolated_redaction():
    reset_redaction()
    yield
    reset_redaction()


@pytest.fixture
def make_event():
    def create(number=1, **changes):
        event = replace(
            sample_event(),
            id=str(number),
            announced_at=sample_event().announced_at + timedelta(seconds=number),
            url=f"https://example.com/announcements/{number}",
            summary=f"Confirmed reset {number}",
        )
        return replace(event, **changes)

    return create


@pytest.fixture
def make_settings(tmp_path):
    def create(**changes):
        settings = Settings(
            user_agent="test-tracker/1.0 (+mailto:test@example.com)",
            database_path=tmp_path / "tracker.sqlite3",
            webhooks=(WebhookConfig(id="first", url="https://hooks.example.com/SECRET"),),
        )
        return replace(settings, **changes)

    return create


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeProvider:
    def __init__(self, name, result, calls=None):
        self.name = name
        self.result = result
        self.calls = calls if calls is not None else []

    def fetch(self, client):
        self.calls.append(self.name)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result
