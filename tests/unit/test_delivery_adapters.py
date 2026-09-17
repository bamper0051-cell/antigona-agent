from __future__ import annotations

import json
import os
import smtplib
import urllib.request
from typing import Any

import pytest

from antigona.config import Settings
from antigona.delivery import (
    DiscordAdapter,
    EmailAdapter,
    ProgressEvent,
    SignalAdapter,
    SlackAdapter,
    TelegramAdapter,
    UnknownChannelError,
    WhatsAppAdapter,
    get_adapter,
)
from antigona.delivery.errors import (
    DeliveryConfigError,
    DeliveryPermanentError,
    DeliveryProviderRejected,
)


class MockHTTPResponse:
    def __init__(self, status: int = 200, body: bytes = b'{"ok": true}') -> None:
        self.status = status
        self._body = body

    def __enter__(self) -> MockHTTPResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        pass


@pytest.fixture
def sample_event() -> ProgressEvent:
    return ProgressEvent(
        task_id="task-123",
        session_id="session-456",
        correlation_id="corr-789",
        step_id="step-1",
        status="DONE",
        message="Task completed successfully",
    )


@pytest.mark.parametrize(
    "channel", ["discord", "slack", "whatsapp", "signal", "email", "telegram", "fake"]
)
def test_all_channels_deliver_in_mock(
    channel: str, sample_event: ProgressEvent, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_urlopen(*args: object, **kwargs: object) -> None:
        pytest.fail("Network call attempted during mock delivery!")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)

    settings = Settings.from_env()
    adapter = get_adapter(channel, settings)

    key = f"key-{channel}-1"
    adapter.deliver(sample_event, key)

    if hasattr(adapter, "delivered_events"):
        events = adapter.delivered_events
        assert len(events) == 1
        assert events[0][0] == sample_event
        assert events[0][1] == key
    elif hasattr(adapter, "events"):
        events = adapter.events
        assert len(events) == 1


def test_lazy_sdk_import() -> None:
    from antigona.delivery.adapters import discord, email_adapter, signal, slack, whatsapp

    assert discord.DiscordAdapter.name == "discord"
    assert slack.SlackAdapter.name == "slack"
    assert whatsapp.WhatsAppAdapter.name == "whatsapp"
    assert signal.SignalAdapter.name == "signal"
    assert email_adapter.EmailAdapter.name == "email"


def test_unknown_channel_raises() -> None:
    settings = Settings.from_env()
    with pytest.raises(UnknownChannelError, match="Unknown delivery channel: 'nope'"):
        get_adapter("nope", settings)


def test_adapter_failure_propagates(sample_event: ProgressEvent) -> None:
    discord_mock = DiscordAdapter(mock=True, fail_times=1)
    with pytest.raises(RuntimeError, match="discord delivery failure"):
        discord_mock.deliver(sample_event, "idem-fail")


def test_progress_maps_to_default_channel() -> None:
    settings = Settings.from_env()
    adapter = get_adapter("progress", settings)
    assert adapter.name == settings.delivery_default_channel


def test_disabled_channel_raises() -> None:
    settings = Settings(
        database_url="sqlite:///./antigona.db",
        workspace=Settings.from_env().workspace,
        delivery_enabled_channels=["telegram", "slack"],
    )
    with pytest.raises(UnknownChannelError, match="disabled in settings"):
        get_adapter("discord", settings)

    adapter = get_adapter("slack", settings)
    assert adapter.name == "slack"


def test_real_telegram_adapter_dispatch(
    sample_event: ProgressEvent, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def mock_urlopen(req: urllib.request.Request, timeout: int = 10) -> MockHTTPResponse:
        nonlocal called
        called = True
        assert "bot12345/sendMessage" in req.full_url
        return MockHTTPResponse(200)

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    adapter = TelegramAdapter(bot_token="12345", chat_id="67890", mock=False)
    adapter.deliver(sample_event, "key-tg")
    assert called is True


def test_real_discord_adapter_dispatch(
    sample_event: ProgressEvent, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def mock_urlopen(req: urllib.request.Request, timeout: int = 10) -> MockHTTPResponse:
        calls.append(req)
        return MockHTTPResponse(200)

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    # Webhook branch
    webhook_adapter = DiscordAdapter(webhook_url="https://discord.com/api/webhooks/123", mock=False)
    webhook_adapter.deliver(sample_event, "key-disc-1")

    # Bot token branch
    bot_adapter = DiscordAdapter(token="bot_tok", channel_id="chan_123", mock=False)
    bot_adapter.deliver(sample_event, "key-disc-2")

    assert len(calls) == 2

    # Failure branch
    def mock_urlopen_fail(req: urllib.request.Request, timeout: int = 10) -> MockHTTPResponse:
        return MockHTTPResponse(500)

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen_fail)
    with pytest.raises(RuntimeError, match="discord webhook failed"):
        webhook_adapter.deliver(sample_event, "key-disc-3")
    with pytest.raises(RuntimeError, match="discord bot message failed"):
        bot_adapter.deliver(sample_event, "key-disc-4")


def test_real_slack_adapter_dispatch(
    sample_event: ProgressEvent, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def mock_urlopen(req: urllib.request.Request, timeout: int = 10) -> MockHTTPResponse:
        calls.append(req)
        return MockHTTPResponse(200)

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    # Webhook branch
    webhook_adapter = SlackAdapter(webhook_url="https://hooks.slack.com/services/123", mock=False)
    webhook_adapter.deliver(sample_event, "key-slack-1")

    # Bot token branch
    bot_adapter = SlackAdapter(token="slack_tok", channel="#general", mock=False)
    bot_adapter.deliver(sample_event, "key-slack-2")

    assert len(calls) == 2

    # Failure branch
    def mock_urlopen_fail(req: urllib.request.Request, timeout: int = 10) -> MockHTTPResponse:
        return MockHTTPResponse(500)

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen_fail)
    with pytest.raises(RuntimeError, match="slack webhook returned HTTP 500"):
        webhook_adapter.deliver(sample_event, "key-slack-3")
    with pytest.raises(RuntimeError, match="slack API returned HTTP 500"):
        bot_adapter.deliver(sample_event, "key-slack-4")


def test_real_whatsapp_adapter_dispatch(
    sample_event: ProgressEvent, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def mock_urlopen(req: urllib.request.Request, timeout: int = 10) -> MockHTTPResponse:
        nonlocal called
        called = True
        assert "graph.facebook.com" in req.full_url
        return MockHTTPResponse(200)

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    adapter = WhatsAppAdapter(token="wa_tok", phone_id="pid_123", to="+123456", mock=False)
    adapter.deliver(sample_event, "key-wa")
    assert called is True

    # Failure branch
    def mock_urlopen_fail(req: urllib.request.Request, timeout: int = 10) -> MockHTTPResponse:
        return MockHTTPResponse(400)

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen_fail)
    with pytest.raises(RuntimeError, match="whatsapp API returned HTTP 400"):
        adapter.deliver(sample_event, "key-wa-fail")


def test_real_signal_adapter_dispatch(
    sample_event: ProgressEvent, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def mock_urlopen(req: urllib.request.Request, timeout: int = 10) -> MockHTTPResponse:
        nonlocal called
        called = True
        assert "/v2/send" in req.full_url
        return MockHTTPResponse(201)

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    adapter = SignalAdapter(
        url="http://localhost:8080", sender="+111", recipient="+222", mock=False
    )
    adapter.deliver(sample_event, "key-sig")
    assert called is True

    # Failure branch
    def mock_urlopen_fail(req: urllib.request.Request, timeout: int = 10) -> MockHTTPResponse:
        return MockHTTPResponse(500)

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen_fail)
    with pytest.raises(RuntimeError, match="signal-cli daemon returned HTTP 500"):
        adapter.deliver(sample_event, "key-sig-fail")


def test_real_email_adapter_dispatch(
    sample_event: ProgressEvent, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = False

    class DummySMTP:
        def __init__(self, host: str, port: int, timeout: int = 10) -> None:
            pass

        def __enter__(self) -> DummySMTP:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def starttls(self) -> None:
            pass

        def login(self, user: str, passw: str) -> None:
            pass

        def send_message(self, msg: Any) -> None:
            nonlocal sent
            sent = True

    monkeypatch.setattr(smtplib, "SMTP", DummySMTP)

    adapter = EmailAdapter(
        smtp_host="mail.example.com",
        smtp_port=587,
        user="u",
        password="p",
        sender="a@example.com",
        recipient="b@example.com",
        use_tls=True,
        mock=False,
    )
    adapter.deliver(sample_event, "key-mail")
    assert sent is True


@pytest.mark.skipif(
    os.getenv("ANTIGONA_TEST_DELIVERY_DISCORD") != "1",
    reason="Real Discord API spawn disabled; enable with ANTIGONA_TEST_DELIVERY_DISCORD=1",
)
def test_real_discord_delivery_env_gated(sample_event: ProgressEvent) -> None:
    webhook = os.getenv("ANTIGONA_DELIVERY_DISCORD_WEBHOOK")
    if not webhook:
        pytest.skip("ANTIGONA_DELIVERY_DISCORD_WEBHOOK not provided")
    adapter = DiscordAdapter(webhook_url=webhook, mock=False)
    adapter.deliver(sample_event, "real-discord-test")


# ── P5.3: fail-closed credentials, timeouts, ok-semantics, sanitization ────────


@pytest.mark.parametrize(
    "make_adapter",
    [
        lambda: TelegramAdapter(mock=False),
        lambda: DiscordAdapter(mock=False),
        lambda: SlackAdapter(mock=False),
        lambda: WhatsAppAdapter(mock=False),
        lambda: SignalAdapter(mock=False),
        lambda: EmailAdapter(mock=False),
    ],
    ids=["telegram", "discord", "slack", "whatsapp", "signal", "email"],
)
def test_real_mode_missing_credentials_fails_closed(
    make_adapter: Any, sample_event: ProgressEvent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """delivery_mock=False with no credentials must raise, never silently mock-deliver."""

    def fail_urlopen(*args: object, **kwargs: object) -> None:
        pytest.fail("Network call attempted despite missing credentials!")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: pytest.fail("SMTP attempted"))

    adapter = make_adapter()
    with pytest.raises(DeliveryConfigError):
        adapter.deliver(sample_event, "key-missing-creds")
    assert not hasattr(adapter, "delivered_events") or adapter.delivered_events == []


def test_email_half_configured_auth_pair_is_config_error(sample_event: ProgressEvent) -> None:
    adapter = EmailAdapter(
        smtp_host="mail.example.com",
        sender="a@example.com",
        recipient="b@example.com",
        user="only-user-no-password",
        password=None,
        mock=False,
    )
    with pytest.raises(DeliveryConfigError, match="smtp_auth_pair"):
        adapter.deliver(sample_event, "key-half-auth")


def test_email_no_auth_when_both_absent_is_not_a_config_error(
    sample_event: ProgressEvent, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = False

    class DummySMTP:
        def __init__(self, host: str, port: int, timeout: int = 10) -> None:
            pass

        def __enter__(self) -> DummySMTP:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def starttls(self) -> None:
            pass

        def send_message(self, msg: Any) -> None:
            nonlocal sent
            sent = True

    monkeypatch.setattr(smtplib, "SMTP", DummySMTP)
    adapter = EmailAdapter(
        smtp_host="mail.example.com",
        sender="a@example.com",
        recipient="b@example.com",
        user=None,
        password=None,
        mock=False,
    )
    adapter.deliver(sample_event, "key-no-auth")
    assert sent is True


def test_config_error_carries_field_names_only_never_values() -> None:
    adapter = WhatsAppAdapter(token="TOPSECRET-VALUE", phone_id=None, to=None, mock=False)
    try:
        adapter.deliver(ProgressEvent("t", "s", "c", None, "DONE", "m"), "key-fields")
        pytest.fail("expected DeliveryConfigError")
    except DeliveryConfigError as exc:
        assert "phone_id" in exc.missing_fields
        assert "to" in exc.missing_fields
        assert "TOPSECRET-VALUE" not in str(exc)
        assert "TOPSECRET-VALUE" not in exc.code


@pytest.mark.parametrize(
    ("make_adapter", "timeout_attr"),
    [
        (
            lambda t: TelegramAdapter(bot_token="tok", chat_id="chat", mock=False, timeout=t),
            "timeout",
        ),
        (
            lambda t: DiscordAdapter(
                webhook_url="https://discord.test/hook", mock=False, timeout=t
            ),
            "timeout",
        ),
        (
            lambda t: SlackAdapter(webhook_url="https://slack.test/hook", mock=False, timeout=t),
            "timeout",
        ),
        (
            lambda t: WhatsAppAdapter(token="tok", phone_id="pid", to="+1", mock=False, timeout=t),
            "timeout",
        ),
        (
            lambda t: SignalAdapter(
                url="http://localhost:9999", sender="+1", recipient="+2", mock=False, timeout=t
            ),
            "timeout",
        ),
    ],
    ids=["telegram", "discord", "slack", "whatsapp", "signal"],
)
def test_configured_timeout_reaches_urlopen(
    make_adapter: Any,
    timeout_attr: str,
    sample_event: ProgressEvent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_timeout: dict[str, int] = {}

    def mock_urlopen(req: urllib.request.Request, timeout: int = 10) -> MockHTTPResponse:
        seen_timeout["timeout"] = timeout
        return MockHTTPResponse(200)

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    adapter = make_adapter(37)
    adapter.deliver(sample_event, "key-timeout")
    assert seen_timeout["timeout"] == 37


def test_configured_timeout_reaches_smtp(
    sample_event: ProgressEvent, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    class DummySMTP:
        def __init__(self, host: str, port: int, timeout: int = 10) -> None:
            seen["timeout"] = timeout

        def __enter__(self) -> DummySMTP:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def starttls(self) -> None:
            pass

        def send_message(self, msg: Any) -> None:
            pass

    monkeypatch.setattr(smtplib, "SMTP", DummySMTP)
    adapter = EmailAdapter(
        smtp_host="mail.example.com",
        sender="a@example.com",
        recipient="b@example.com",
        mock=False,
        timeout=41,
    )
    adapter.deliver(sample_event, "key-smtp-timeout")
    assert seen["timeout"] == 41


def test_telegram_provider_ok_false_is_not_delivered(
    sample_event: ProgressEvent, monkeypatch: pytest.MonkeyPatch
) -> None:
    def mock_urlopen(req: urllib.request.Request, timeout: int = 10) -> MockHTTPResponse:
        return MockHTTPResponse(200, body=b'{"ok": false, "description": "chat not found"}')

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    adapter = TelegramAdapter(bot_token="tok", chat_id="chat", mock=False)
    with pytest.raises(DeliveryProviderRejected):
        adapter.deliver(sample_event, "key-tg-rejected")


def test_telegram_real_delivery_bounds_long_result_to_provider_limit(
    sample_event: ProgressEvent, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def mock_urlopen(request: urllib.request.Request, timeout: int = 10) -> MockHTTPResponse:
        assert isinstance(request.data, bytes)
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return MockHTTPResponse(200, body=b'{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    adapter = TelegramAdapter(bot_token="token", chat_id="chat", mock=False, timeout=7)
    long_event = ProgressEvent(
        task_id=sample_event.task_id,
        session_id=sample_event.session_id,
        correlation_id=sample_event.correlation_id,
        step_id=sample_event.step_id,
        status=sample_event.status,
        message="x" * 5000,
    )

    adapter.deliver(long_event, "telegram-long-result")

    assert captured["timeout"] == 7
    assert len(captured["payload"]["text"]) == 4096
    assert captured["payload"]["text"].endswith("...[truncated]")


def test_telegram_real_delivery_bounds_astral_unicode_in_utf16_units(
    sample_event: ProgressEvent, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def mock_urlopen(request: urllib.request.Request, timeout: int = 10) -> MockHTTPResponse:
        assert isinstance(request.data, bytes)
        captured["payload"] = json.loads(request.data)
        return MockHTTPResponse(200, body=b'{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    adapter = TelegramAdapter(bot_token="token", chat_id="chat", mock=False)
    event = ProgressEvent(
        task_id=sample_event.task_id,
        session_id=sample_event.session_id,
        correlation_id=sample_event.correlation_id,
        step_id=sample_event.step_id,
        status=sample_event.status,
        message="🚀" * 5000,
    )

    adapter.deliver(event, "telegram-astral-result")

    text = captured["payload"]["text"]
    assert len(text.encode("utf-16-le")) // 2 <= 4096
    assert text.endswith("...[truncated]")


def test_telegram_real_delivery_restores_plain_text_entities(
    sample_event: ProgressEvent, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def mock_urlopen(request: urllib.request.Request, timeout: int = 10) -> MockHTTPResponse:
        assert isinstance(request.data, bytes)
        captured["payload"] = json.loads(request.data)
        return MockHTTPResponse(200, body=b'{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    adapter = TelegramAdapter(bot_token="token", chat_id="chat", mock=False)
    event = ProgressEvent(
        task_id=sample_event.task_id,
        session_id=sample_event.session_id,
        correlation_id=sample_event.correlation_id,
        step_id=sample_event.step_id,
        status=sample_event.status,
        message="if a &lt; b &amp;&amp; c &gt; d then &quot;ok&quot;",
    )

    adapter.deliver(event, "telegram-plain-text")

    text = captured["payload"]["text"]
    assert 'if a < b && c > d then "ok"' in text
    assert "&lt;" not in text


def test_slack_bot_token_provider_ok_false_is_not_delivered(
    sample_event: ProgressEvent, monkeypatch: pytest.MonkeyPatch
) -> None:
    def mock_urlopen(req: urllib.request.Request, timeout: int = 10) -> MockHTTPResponse:
        return MockHTTPResponse(200, body=b'{"ok": false, "error": "channel_not_found"}')

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    adapter = SlackAdapter(token="tok", channel="#general", mock=False)
    with pytest.raises(DeliveryProviderRejected):
        adapter.deliver(sample_event, "key-slack-rejected")


def test_unknown_channel_error_is_permanent() -> None:
    assert issubclass(UnknownChannelError, DeliveryPermanentError)


def test_telegram_bot_token_falls_back_to_bare_conventional_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTIGONA_DELIVERY_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ANTIGONA_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "conventional-bare-token")
    settings = Settings.from_env()
    assert settings.delivery_telegram_bot_token == "conventional-bare-token"


@pytest.mark.parametrize("env_var", ["ANTIGONA_DELIVERY_TIMEOUT", "ANTIGONA_DELIVERY_MAX_ATTEMPTS"])
def test_non_positive_delivery_settings_rejected_at_load(
    env_var: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(env_var, "0")
    with pytest.raises(RuntimeError):
        Settings.from_env()


def test_delivery_result_channels_default_and_dedup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTIGONA_DELIVERY_RESULT_CHANNELS", raising=False)
    assert Settings.from_env().delivery_result_channels == ["telegram"]

    monkeypatch.setenv("ANTIGONA_DELIVERY_RESULT_CHANNELS", "telegram, email,telegram, EMAIL")
    assert Settings.from_env().delivery_result_channels == ["telegram", "email"]


def test_delivery_result_channels_reject_unknown_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTIGONA_DELIVERY_CHANNELS", raising=False)
    monkeypatch.setenv("ANTIGONA_DELIVERY_RESULT_CHANNELS", "telegram,typo")
    with pytest.raises(RuntimeError, match="contains an unknown channel"):
        Settings.from_env()


def test_delivery_result_channels_reject_disabled_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTIGONA_DELIVERY_CHANNELS", "email")
    monkeypatch.setenv("ANTIGONA_DELIVERY_RESULT_CHANNELS", "telegram")
    with pytest.raises(RuntimeError, match="contains a disabled channel"):
        Settings.from_env()
