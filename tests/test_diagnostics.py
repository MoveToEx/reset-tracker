import errno
import logging
import sqlite3
import ssl

import httpx
import pytest
from conftest import Clock

from reset_tracker.cli import main
from reset_tracker.config import WebhookConfig
from reset_tracker.diagnostics import log_exception, remember_webhook
from reset_tracker.providers import CodexResetProvider
from reset_tracker.service import Tracker
from reset_tracker.storage import Store


@pytest.fixture
def error_logs(caplog, monkeypatch):
    monkeypatch.setattr(logging.getLogger("reset_tracker"), "propagate", True)
    return caplog


def test_exception_chain_and_notes_are_logged_with_secrets_redacted(error_logs):
    url = "https://discord.com/api/webhooks/123/discord-secret?token=query-secret"
    remember_webhook(url, {"Authorization": "Bearer auth-secret", "X-Api-Key": "api-secret"})
    try:
        try:
            raise OSError(errno.EACCES, f"Permission denied at {url}")
        except OSError as cause:
            failure = RuntimeError("auth-secret api-secret discord-secret query-secret")
            failure.add_note("Additional diagnostic context")
            raise failure from cause
    except RuntimeError:
        log_exception(logging.ERROR, "endpoint=%s failed", "discord")

    text = error_logs.text
    assert "Traceback (most recent call last)" in text
    assert "PermissionError" in text
    assert "Permission denied" in text
    assert "RuntimeError" in text
    assert "direct cause" in text
    assert "Additional diagnostic context" in text
    assert "endpoint=discord" in text
    assert "[REDACTED_URL]" in text
    for secret in (url, "auth-secret", "api-secret", "discord-secret", "query-secret"):
        assert secret not in text
    # There is no unsanitized exc_info for a different handler to reformat.
    assert error_logs.records[-1].exc_info is None


def test_provider_parsing_logs_the_original_cause(make_settings, error_logs):
    settings = make_settings()
    with (
        Store(settings.database_path) as store,
        httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
        ) as client,
    ):
        assert Tracker(settings, store, client, providers=[CodexResetProvider()]).poll() is False
    assert "provider=codex_reset" in error_logs.text
    assert "ValueError: expected a feed object" in error_logs.text
    assert "ProviderError: invalid feed response" in error_logs.text


def test_provider_http_errors_include_the_exception(make_settings, error_logs):
    settings = make_settings()
    with (
        Store(settings.database_path) as store,
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as client,
    ):
        assert Tracker(settings, store, client, providers=[CodexResetProvider()]).poll() is False
    assert "outcome=http_error status=503" in error_logs.text
    assert "HTTPStatusError" in error_logs.text
    assert "Service Unavailable" in error_logs.text
    assert "https://" not in error_logs.text


def test_webhook_tls_details_are_logged_and_delivery_still_retries(
    make_settings,
    make_event,
    error_logs,
):
    settings = make_settings()

    def handle(request):
        try:
            raise ssl.SSLCertVerificationError("self-signed certificate")
        except ssl.SSLCertVerificationError as cause:
            raise httpx.ConnectError(
                f"certificate verification failed for {request.url}", request=request
            ) from cause

    with (
        Store(settings.database_path) as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
    ):
        store.record_snapshot([make_event(1)], ["first"], 1000)
        assert Tracker(settings, store, client, clock=Clock()).dispatch_due() == (1, 1)
        assert store.heads(["first"])[0].due_at == 1030
    assert "SSLCertVerificationError" in error_logs.text
    assert "self-signed certificate" in error_logs.text
    assert "ConnectError" in error_logs.text
    assert "SECRET" not in error_logs.text


def test_render_details_are_logged_and_endpoint_still_pauses(
    make_settings,
    make_event,
    error_logs,
):
    settings = make_settings(
        webhooks=(
            WebhookConfig(
                "first",
                "https://hooks.example.com",
                preset="custom",
                body_template="{{ missing_variable }}",
            ),
        )
    )
    with (
        Store(settings.database_path) as store,
        httpx.Client(
            transport=httpx.MockTransport(lambda request: pytest.fail("must not send"))
        ) as client,
    ):
        store.record_snapshot([make_event(1)], ["first"], 1000)
        assert Tracker(settings, store, client, clock=Clock()).dispatch_due() == (1, 1)
        assert store.heads(["first"]) == []
        assert store.pending_count(["first"]) == 1
    assert "UndefinedError: 'missing_variable' is undefined" in error_logs.text
    assert "RenderError: webhook body could not be rendered" in error_logs.text


@pytest.fixture
def logging_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'user_agent = "test/1.0 (+mailto:test@example.com)"\n'
        '[[webhooks]]\nid = "first"\nurl = "https://hooks.example.com/secret-token"\n',
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("failure", "reason", "detail"),
    [
        (
            PermissionError(errno.EACCES, "Permission denied", "data/tracker.sqlite3"),
            "storage_or_filesystem_error",
            "Permission denied",
        ),
        (
            sqlite3.OperationalError("database is locked"),
            "storage_or_filesystem_error",
            "database is locked",
        ),
        (httpx.ConnectError("proxy connection failed"), "ConnectError", "proxy connection failed"),
        (
            RuntimeError("unexpected invariant failed"),
            "unexpected_error",
            "unexpected invariant failed",
        ),
    ],
)
def test_cli_failure_boundaries_log_details_and_exit(
    failure,
    reason,
    detail,
    logging_config,
    capsys,
    monkeypatch,
):
    def fail_store(path):
        raise failure

    monkeypatch.setattr("reset_tracker.cli.Store", fail_store)
    assert main(["--config", str(logging_config), "once"]) == 1
    output = capsys.readouterr().out
    assert "Traceback (most recent call last)" in output
    assert type(failure).__name__ in output
    assert f"reason={reason}" in output
    assert detail in output


def test_invalid_configuration_logs_the_parse_error(logging_config, capsys):
    logging_config.write_text("invalid TOML!", encoding="utf-8")
    assert main(["--config", str(logging_config), "validate"]) == 2
    output = capsys.readouterr().out
    assert "TOMLDecodeError" in output
    assert "ConfigError" in output
    assert "Traceback (most recent call last)" in output


def test_configuration_environment_credentials_are_redacted(
    logging_config,
    capsys,
    monkeypatch,
):
    monkeypatch.setenv("CUSTOM_TOKEN", "config-secret-token")
    monkeypatch.setenv("MISSING_TEMPLATE", "missing-secret-template.j2")
    logging_config.write_text(
        logging_config.read_text()
        + 'preset = "custom"\ntemplate_file = "${MISSING_TEMPLATE}"\n'
        + '[webhooks.headers]\nAuthorization = "Bearer ${CUSTOM_TOKEN}"\n',
        encoding="utf-8",
    )
    assert main(["--config", str(logging_config), "validate"]) == 2
    output = capsys.readouterr().out
    assert "FileNotFoundError" in output
    assert "config-secret-token" not in output
    assert "missing-secret-template.j2" not in output
