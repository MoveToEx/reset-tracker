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
    assert config.confirmation_interval_seconds == 120
    assert config.request_timeout_seconds == 20
    assert config.confirmation_dedup_window_seconds == 1800
    assert config.providers == ("codex_reset",)
    assert config.database_path == config_path.parent / "state/tracker.sqlite3"
    assert config.webhooks[0].url.endswith("/SECRET")


def test_missing_environment_variable(config_path, monkeypatch):
    monkeypatch.delenv("WEBHOOK_URL")
    with pytest.raises(ConfigError, match="WEBHOOK_URL"):
        load_config(config_path)


@pytest.mark.parametrize("seconds", [0, 120, 3600])
def test_confirmation_grouping_window_is_configurable(config_path, seconds):
    config_path.write_text(
        f"confirmation_dedup_window_seconds = {seconds}\n" + BASE, encoding="utf-8"
    )
    assert load_config(config_path).confirmation_dedup_window_seconds == seconds


@pytest.mark.parametrize("seconds", [60, 120, 900])
def test_confirmation_polling_interval_is_configurable(config_path, seconds):
    config_path.write_text(f"confirmation_interval_seconds = {seconds}\n" + BASE, encoding="utf-8")
    assert load_config(config_path).confirmation_interval_seconds == seconds


@pytest.mark.parametrize(
    "change",
    [
        "interval_seconds = 59\n",
        "interval_seconds = true\n",
        "confirmation_interval_seconds = 59\n",
        "confirmation_interval_seconds = true\n",
        "confirmation_interval_seconds = 120.0\n",
        'confirmation_interval_seconds = "120"\n',
        "request_timeout_seconds = 0\n",
        "request_timeout_seconds = nan\n",
        "confirmation_dedup_window_seconds = -1\n",
        "confirmation_dedup_window_seconds = true\n",
        "confirmation_dedup_window_seconds = 30.5\n",
        'confirmation_dedup_window_seconds = "1800"\n',
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


@pytest.mark.parametrize("preset", ["discord", "generic_json"])
@pytest.mark.parametrize("source", ["inline", "file"])
def test_preset_templates_render_complete_json_objects(config_path, preset, source):
    template = """
{
  "embeds": [{
    "title": "A rich notification",
    "description": {{ event.summary | tojson }},
    "fields": [{"name": "Detected", "value": {{ detected_at | tojson }}}]
  }],
  "allowed_mentions": {"parse": []}
}
"""
    if source == "file":
        (config_path.parent / "rich.j2").write_text(template, encoding="utf-8")
        template_config = 'template_file = "rich.j2"\n'
    else:
        template_config = "body_template = '''" + template + "'''\n"
    config_path.write_text(BASE + f'preset = "{preset}"\n' + template_config, encoding="utf-8")
    webhook = load_config(config_path).webhooks[0]
    event = replace(sample_event(), summary='Rich "quotes"\nUnicode 你好')
    request = render_request(webhook, event, "now")
    assert json.loads(request.body) == {
        "embeds": [
            {
                "title": "A rich notification",
                "description": event.summary,
                "fields": [{"name": "Detected", "value": "now"}],
            }
        ],
        "allowed_mentions": {"parse": []},
    }
    assert httpx.Headers(request.headers)["Content-Type"] == "application/json"
    if preset == "discord":
        assert httpx.URL(request.url).params["wait"] == "true"


@pytest.mark.parametrize("template", ["[]", "[{}]", "null", "true", "42", '"plain text"'])
def test_json_templates_must_render_objects(config_path, template):
    webhook = WebhookConfig(
        "custom", "https://example.com", preset="custom", body_template=template
    )
    with pytest.raises(RenderError) as error:
        render_request(webhook, sample_event(), "now")
    assert "must render to a JSON object" in str(error.value.__cause__)
    config_path.write_text(
        BASE + 'preset = "custom"\n' + "body_template = '''" + template + "'''\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="invalid template"):
        load_config(config_path)


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
        '{"value": NaN}',
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
    embed = payload["embeds"][0]
    assert embed["title"] == "🔄 Codex reset forecast"
    assert len(embed["description"].encode("utf-16-le")) // 2 <= 4096
    assert embed["url"] == event.url
    assert embed["timestamp"] == event.to_dict()["announced_at"]
    fields = {field["name"]: field["value"] for field in embed["fields"]}
    assert fields["Announcement"] == event.url
    assert fields["Data"] == event.source_url
    assert fields["Signal probability"] == "93%"
    assert fields["24h cadence probability"] == "32%"
    assert fields["48h cadence probability"] == "53%"
    assert fields["Forecast window"] == event.window_label
    assert fields["Expected by (UTC)"] == event.to_dict()["expected_at"]
    assert all(len(value.encode("utf-16-le")) // 2 <= 1024 for value in fields.values())
    embed_text = (
        embed["title"]
        + embed["description"]
        + "".join(field["name"] + field["value"] for field in embed["fields"])
    )
    assert len(embed_text.encode("utf-16-le")) // 2 <= 6000
    assert payload["allowed_mentions"] == {"parse": []}
    assert httpx.URL(request.url).params["wait"] == "true"
    assert httpx.URL(request.url).params["thread_id"] == "123"


def test_discord_override_keeps_rich_payload_and_disables_mentions_by_default():
    webhook = WebhookConfig(
        "discord",
        "https://discord.com/api/webhooks/id/token",
        preset="discord",
        body_template='{"embeds": [{"description": {{ event.summary | tojson }}}]}',
    )
    request = render_request(webhook, sample_event(), "now")
    assert json.loads(request.body) == {
        "embeds": [{"description": sample_event().summary}],
        "allowed_mentions": {"parse": []},
    }


@pytest.mark.parametrize(("probability", "display"), [(None, None), (0, "0%"), (93.5, "93.5%")])
def test_discord_handles_optional_and_zero_probability(probability, display):
    event = replace(
        sample_event(),
        probability=probability,
        probability_24h=None,
        probability_48h=None,
        expected_at=None,
        window_label=None,
    )
    request = render_request(
        WebhookConfig("discord", "https://discord.com/api/webhooks/id/token", preset="discord"),
        event,
        "now",
    )
    fields = {
        field["name"]: field["value"] for field in json.loads(request.body)["embeds"][0]["fields"]
    }
    if display is None:
        assert "Signal probability" not in fields
    else:
        assert fields["Signal probability"] == display
    assert "24h cadence probability" not in fields
    assert "48h cadence probability" not in fields


def test_preview_prints_rich_json_object(config_path, capsys):
    config_path.write_text(BASE + 'preset = "discord"\n', encoding="utf-8")
    assert main(["--config", str(config_path), "preview", "--webhook", "first"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict)
    assert payload["embeds"][0]["title"] == "🔄 Codex reset forecast"
    fields = {field["name"]: field["value"] for field in payload["embeds"][0]["fields"]}
    assert fields["Signal probability"] == "93%"


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
