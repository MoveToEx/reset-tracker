import json
from dataclasses import replace

import httpx
import pytest

from reset_tracker.cli import main
from reset_tracker.config import ConfigError, WebhookConfig, load_config
from reset_tracker.http import retry_delay
from reset_tracker.models import sample_event
from reset_tracker.templates import RenderError, render_request

BASE = """
user_agent = "reset-tracker/0.1.0 (+mailto:${CONTACT_EMAIL})"
database_path = "state/tracker.sqlite3"
[[webhooks]]
id = "first"
url = "${WEBHOOK_URL}"
"""


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTACT_EMAIL", "test@example.com")
    monkeypatch.setenv("WEBHOOK_URL", "https://hooks.example.com/SECRET")
    path = tmp_path / "config.toml"
    path.write_text(BASE, encoding="utf-8")
    return path


def test_load_defaults_and_resolve_database_path(config_path):
    config = load_config(config_path)
    assert config.interval_seconds == 600
    assert config.request_timeout_seconds == 20
    assert config.providers == ("codex_reset",)
    assert config.database_path == config_path.parent / "state/tracker.sqlite3"
    assert config.webhooks[0].url.endswith("/SECRET")


def test_missing_environment_variable(config_path, monkeypatch):
    monkeypatch.delenv("WEBHOOK_URL")
    with pytest.raises(ConfigError, match="WEBHOOK_URL"):
        load_config(config_path)


@pytest.mark.parametrize(
    "change",
    [
        "interval_seconds = 59\n",
        "interval_seconds = true\n",
        "request_timeout_seconds = 0\n",
        "request_timeout_seconds = nan\n",
        "providers = []\n",
        'providers = ["unknown"]\n',
        'providers = ["codex_reset", "codex_reset"]\n',
        "unknown = 1\n",
    ],
)
def test_invalid_root_settings(config_path, change):
    config_path.write_text(change + BASE, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(config_path)


@pytest.mark.parametrize(
    "change",
    [
        'preset = "unknown"\n',
        'body_format = "xml"\n',
        'body_format = "text"\n',
        'body_template = "{}"\n',
        'preset = "custom"\n',
        'preset = "custom"\nbody_template = "{{ missing }}"\n',
        'preset = "custom"\nbody_template = "{% malformed"\n',
        'preset = "custom"\nbody_template = "not json"\n',
        'preset = "custom"\nbody_template = "{}"\ntemplate_file = "anything"\n',
        'preset = "discord"\nmethod = "GET"\n',
        "unknown = true\n",
        'headers = { Authorization = "${MISSING_TOKEN}" }\n',
    ],
)
def test_invalid_webhook_settings(config_path, change):
    config_path.write_text(BASE + change, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(config_path)


@pytest.mark.parametrize(
    "user_agent",
    [
        "anonymous",
        "reader/1.0",
        "reader/1.0 (+mailto:invalid)",
        "reader/1.0 (+https://test)\r\nx",
    ],
)
def test_identifying_user_agent_required(config_path, monkeypatch, user_agent):
    text = BASE.replace('"reset-tracker/0.1.0 (+mailto:${CONTACT_EMAIL})"', '"${AGENT}"')
    monkeypatch.setenv("AGENT", user_agent)
    config_path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(config_path)


def test_relative_template_file_headers_and_environment(config_path, monkeypatch):
    monkeypatch.setenv("TOKEN", "secret-token")
    (config_path.parent / "body.j2").write_text(
        '{"message": {{ event.summary | tojson }}, "when": {{ detected_at | tojson }}}',
        encoding="utf-8",
    )
    config_path.write_text(
        BASE
        + """
preset = "custom"
template_file = "body.j2"
[webhooks.headers]
Authorization = "Bearer ${TOKEN}"
""",
        encoding="utf-8",
    )
    webhook = load_config(config_path).webhooks[0]
    assert webhook.headers["Authorization"] == "Bearer secret-token"
    assert "tojson" in webhook.body_template


def test_json_template_escapes_strings_and_retains_types():
    event = replace(sample_event(), summary='Quotes " newline\n Unicode 你好')
    webhook = WebhookConfig(
        "custom",
        "https://example.com",
        preset="custom",
        body_template='{"text": {{ event.summary | tojson }}, "count": {{ 3 }}, "ok": true}',
    )
    request = render_request(webhook, event, "now")
    assert json.loads(request.body) == {"text": event.summary, "count": 3, "ok": True}
    assert httpx.Headers(request.headers)["Content-Type"] == "application/json"


@pytest.mark.parametrize(
    "template",
    [
        "{{ missing }}",
        '{"text": "{{ event.summary }}"}',
        "NaN",
        "{{ event.__class__.__mro__ }}",
    ],
)
def test_render_rejects_undefined_invalid_json_and_unsafe_access(template):
    webhook = WebhookConfig(
        "custom", "https://example.com", preset="custom", body_template=template
    )
    with pytest.raises(RenderError):
        render_request(webhook, replace(sample_event(), summary='"quotes"'), "now")


def test_text_template():
    webhook = WebhookConfig(
        "custom",
        "https://example.com",
        preset="custom",
        body_format="text",
        body_template="{{ event.summary }}\n{{ detected_at }}",
        method="PUT",
    )
    request = render_request(webhook, sample_event(), "now")
    assert request.method == "PUT"
    assert request.body.decode().endswith("\nnow")
    assert httpx.Headers(request.headers)["Content-Type"].startswith("text/plain")


def test_generic_json():
    request = render_request(WebhookConfig("generic", "https://example.com"), sample_event(), "now")
    assert json.loads(request.body) == {"event": sample_event().to_dict(), "detected_at": "now"}


@pytest.mark.parametrize("summary", ["@everyone " * 1000, "\U0001f600" * 2000])
def test_discord_is_bounded_credited_and_preserves_query_parameters(summary):
    webhook = WebhookConfig(
        "discord",
        "https://discord.com/api/webhooks/id/token?thread_id=123&wait=false",
        preset="discord",
    )
    event = replace(sample_event(), summary=summary)
    request = render_request(webhook, event, "now")
    payload = json.loads(request.body)
    assert len(payload["content"]) <= 2000
    assert len(payload["content"].encode("utf-16-le")) // 2 <= 2000
    assert event.url in payload["content"]
    assert event.source_url in payload["content"]
    assert event.announced_at.isoformat() in payload["content"]
    assert payload["allowed_mentions"] == {"parse": []}
    assert httpx.URL(request.url).params["wait"] == "true"
    assert httpx.URL(request.url).params["thread_id"] == "123"


@pytest.mark.parametrize(
    "header,expected",
    [
        ("10.5", 10.5),
        ("Thu, 01 Jan 1970 00:01:00 GMT", 50),
        ("invalid", None),
        ("nan", None),
        ("-1", None),
    ],
)
def test_retry_after_header(header, expected):
    response = httpx.Response(429, headers={"Retry-After": header})
    assert retry_delay(response, 10) == expected


def test_validate_preview_and_presets_are_offline(config_path, capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline commands must not create a network client")

    monkeypatch.setattr(httpx, "Client", forbidden)
    assert main(["presets"]) == 0
    assert main(["--config", str(config_path), "validate"]) == 0
    assert main(["--config", str(config_path), "preview", "--webhook", "first"]) == 0
    output = capsys.readouterr().out
    assert "SECRET" not in output
    assert "sample-reset" in output
    assert not (config_path.parent / "state").exists()


def test_cli_invalid_configuration_does_not_echo_secret(config_path, capsys, monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", "SECRET invalid URL")
    assert main(["--config", str(config_path), "validate"]) == 2
    assert "SECRET" not in capsys.readouterr().out
