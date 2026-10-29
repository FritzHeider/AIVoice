"""Transactional email: SMTP_URL parsing, settings, templates, MIME, and the SMTP / in-memory senders."""

from __future__ import annotations

import email
import email.policy
import socket
from collections.abc import Iterator
from datetime import UTC, datetime
from email.message import EmailMessage as MimeMessage
from typing import Any

import aiosmtplib
import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import SMTP, Envelope, Session
from pydantic import ValidationError
from service_helpers import settings_of
from structlog.testing import capture_logs

from va_common.email import (
    SMTP_TIMEOUT_S,
    TEMPLATES,
    EmailDeliveryError,
    EmailMessage,
    EmailSettings,
    InMemoryEmailSender,
    SmtpConfig,
    SmtpEmailSender,
    agent_key_request,
    invitation,
    parse_smtp_url,
    password_reset,
    to_mime,
    verify_email,
)
from va_common.time import FrozenClock

NOW = datetime(2026, 10, 29, 12, 30, tzinfo=UTC)
SENDER = "VoiceAgents <no-reply@voiceagents.local>"
LINK = "https://app.voiceagents.local/verify-email?token=abc_DEF-123&next=%2F"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("smtp://localhost:1025", SmtpConfig("smtp", "localhost", 1025, None, None, False, None)),
        ("smtp://mail.internal", SmtpConfig("smtp", "mail.internal", 25, None, None, False, None)),
        (
            "smtp+starttls://user%40corp:p%40ss%2Fw@smtp.example.com",
            SmtpConfig("smtp+starttls", "smtp.example.com", 587, "user@corp", "p@ss/w", False, True),
        ),
        (
            "SMTPS://apikey:s3cret@smtp.example.com:2465",
            SmtpConfig("smtps", "smtp.example.com", 2465, "apikey", "s3cret", True, False),
        ),
        ("smtps://smtp.example.com", SmtpConfig("smtps", "smtp.example.com", 465, None, None, True, False)),
    ],
)
def test_parse_smtp_url(url: str, expected: SmtpConfig) -> None:
    assert parse_smtp_url(url) == expected


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://smtp.example.com", "scheme"),
        ("smtp.example.com:25", "scheme"),
        ("smtp://", "host"),
        ("smtps://:pw@:465", "host"),
        ("smtp://smtp.example.com:99999", "port"),
    ],
)
def test_parse_smtp_url_rejects(url: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_smtp_url(url)


def test_smtp_config_repr_hides_password() -> None:
    config = parse_smtp_url("smtps://user:hunter2-secret@smtp.example.com")
    assert "hunter2-secret" not in repr(config)


def test_email_settings_defaults_match_spec() -> None:
    settings = settings_of(EmailSettings)
    assert settings.smtp_url.get_secret_value() == "smtp://localhost:1025"
    assert settings.email_from == SENDER
    assert settings.smtp_config() == SmtpConfig("smtp", "localhost", 1025, None, None, False, None)
    assert "localhost:1025" not in repr(settings)


def test_email_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMTP_URL", "smtps://relay:pw@smtp.example.com")
    monkeypatch.setenv("EMAIL_FROM", "alerts@example.com")
    monkeypatch.setenv("VA_ENV", "production")
    settings = settings_of(EmailSettings)
    assert settings.smtp_config().hostname == "smtp.example.com"
    assert settings.email_from == "alerts@example.com"


@pytest.mark.parametrize("env", ["staging", "production"])
def test_email_settings_refuse_plain_smtp_outside_local(env: str) -> None:
    with pytest.raises(ValidationError, match="smtp://"):
        settings_of(EmailSettings, va_env=env, smtp_url="smtp://smtp.example.com")
    starttls = settings_of(EmailSettings, va_env=env, smtp_url="smtp+starttls://smtp.example.com")
    assert starttls.smtp_config().start_tls is True


@pytest.mark.parametrize("env", ["local", "test"])
def test_email_settings_allow_plain_smtp_locally(env: str) -> None:
    assert settings_of(EmailSettings, va_env=env).smtp_config().scheme == "smtp"


@pytest.mark.parametrize(
    "overrides",
    [
        {"smtp_url": "ftp://smtp.example.com"},
        {"email_from": "VoiceAgents"},
        {"email_from": "VoiceAgents <no-reply>"},
        {"email_from": ""},
    ],
)
def test_email_settings_reject_invalid(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        settings_of(EmailSettings, **overrides)


def test_verify_email_template() -> None:
    message = verify_email(name='<script>alert("x")</script>', link=LINK, to="jane@example.com")
    assert message.template == "verify_email"
    assert message.template in TEMPLATES
    assert message.subject == "Verify your VoiceAgents email address"
    assert message.to == "jane@example.com"
    assert "<script>" not in message.html
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in message.html
    assert '<script>alert("x")</script>' in message.text
    assert LINK in message.text
    assert 'href="https://app.voiceagents.local/verify-email?token=abc_DEF-123&amp;next=%2F"' in message.html
    assert "expires in 24 hours" in message.text
    assert "expires in 24 hours" in message.html
    assert "$" not in message.text
    assert "$" not in message.html


def test_password_reset_template() -> None:
    link = "https://app.voiceagents.local/reset-password/abc_DEF-123?x=1&y=2"
    message = password_reset(name="<b>Ravi</b>", link=link, to="ravi@example.com")
    assert message.template == "password_reset"
    assert message.template in TEMPLATES
    assert {"verify_email", "password_reset", "invitation", "agent_key_request"} == TEMPLATES
    assert message.subject == "Reset your VoiceAgents password"
    assert message.to == "ravi@example.com"
    assert "<b>Ravi</b>" not in message.html
    assert "Hi &lt;b&gt;Ravi&lt;/b&gt;," in message.html
    assert message.text.startswith("Hi <b>Ravi</b>,\n")
    assert link in message.text
    assert 'href="https://app.voiceagents.local/reset-password/abc_DEF-123?x=1&amp;y=2"' in message.html
    assert ">Choose a new password</a>" in message.html
    for body in (message.text, message.html):
        assert "expires in 60 minutes" in body
        assert "your password stays the same." in body
        assert "$" not in body
    assert message.text.rstrip().endswith("The VoiceAgents team")


def test_invitation_template() -> None:
    link = "https://app.voiceagents.local/invite/abc_DEF-123?x=1&y=2"
    message = invitation(
        org_name="<i>Acme</i> & Sons $org",
        inviter_name='<script>alert("x")</script>',
        role="developer",
        link=link,
        to="ravi@example.com",
    )
    assert message.template == "invitation"
    assert message.template in TEMPLATES
    assert message.subject == "You have been invited to VoiceAgents"
    assert message.to == "ravi@example.com"
    assert message.text.startswith("Hi there,\n")
    assert (
        '<script>alert("x")</script> invited you to join <i>Acme</i> & Sons $org on VoiceAgents with the '
        "developer role. Accept the invitation here:"
    ) in message.text
    assert "<script>" not in message.html
    assert "<i>" not in message.html
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; invited you to join" in message.html
    assert "&lt;i&gt;Acme&lt;/i&gt; &amp; Sons $org on VoiceAgents with the developer role." in message.html
    assert link in message.text
    assert 'href="https://app.voiceagents.local/invite/abc_DEF-123?x=1&amp;y=2"' in message.html
    assert ">Accept invitation</a>" in message.html
    for body in (message.text, message.html):
        assert "expires in 7 days and works once" in body
        assert "nothing changes until you accept it." in body
        assert "$inviter" not in body
        assert "$role" not in body


def test_agent_key_request_template() -> None:
    link = "https://app.voiceagents.local/agent-onboarding/abc_DEF-123?x=1&y=2"
    message = agent_key_request(
        agent_name='<script>alert("x")</script>',
        purpose="Build <b>support</b> line $agent",
        scopes=["calls:read", "calls:write"],
        link=link,
        to="ravi@example.com",
    )
    assert message.template == "agent_key_request"
    assert message.template in TEMPLATES
    assert message.subject == "An AI agent asked for a VoiceAgents API key"
    assert message.to == "ravi@example.com"
    assert message.text.startswith("Hi there,\n")
    assert (
        '<script>alert("x")</script> asked for a VoiceAgents test API key with the scopes calls:read, '
        "calls:write (purpose: Build <b>support</b> line $agent)."
    ) in message.text
    assert "<script>" not in message.html
    assert "<b>" not in message.html
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; asked for" in message.html
    assert "(purpose: Build &lt;b&gt;support&lt;/b&gt; line $agent)." in message.html
    assert link in message.text
    assert 'href="https://app.voiceagents.local/agent-onboarding/abc_DEF-123?x=1&amp;y=2"' in message.html
    assert ">Review request</a>" in message.html
    for body in (message.text, message.html):
        assert "calls:read, calls:write" in body
        assert "expires in 60 minutes" in body
        assert "no key is created unless you approve it." in body
        assert "$scopes" not in body


def test_agent_key_request_template_without_purpose() -> None:
    message = agent_key_request(
        agent_name="Builder", purpose="", scopes=["org:read"], link=LINK, to="ravi@example.com"
    )
    assert "with the scopes org:read. Approve or deny" in message.text
    assert "purpose" not in message.text
    assert "$" not in message.text


def test_to_mime_multipart_alternative() -> None:
    message = verify_email(name="Jane", link=LINK, to="jane@example.com")
    mime = to_mime(message, sender=SENDER, now=NOW)
    assert mime.get_content_type() == "multipart/alternative"
    assert mime["From"] == SENDER
    assert mime["To"] == "jane@example.com"
    assert mime["Subject"] == message.subject
    assert mime["Date"] == "Thu, 29 Oct 2026 12:30:00 +0000"
    assert mime["Message-ID"].endswith("@voiceagents.local>")
    text_part, html_part = list(mime.iter_parts())
    assert isinstance(text_part, MimeMessage)
    assert isinstance(html_part, MimeMessage)
    assert text_part.get_content_type() == "text/plain"
    assert html_part.get_content_type() == "text/html"
    assert text_part.get_content() == message.text
    assert html_part.get_content() == message.html


async def test_in_memory_sender_records_and_filters() -> None:
    sender = InMemoryEmailSender()
    first = verify_email(name="Jane", link=LINK, to="Jane@Example.com")
    second = verify_email(name="Ravi", link=LINK, to="ravi@example.com")
    await sender.send(first)
    await sender.send(second)
    assert sender.sent == [first, second]
    assert sender.to("jane@example.com") == [first]
    assert sender.to("nobody@example.com") == []
    sender.clear()
    assert sender.sent == []


class _Collector:
    """aiosmtpd handler that keeps every envelope it accepts."""

    def __init__(self) -> None:
        self.envelopes: list[Envelope] = []

    async def handle_DATA(  # noqa: N802  (aiosmtpd hook name)
        self,
        server: SMTP,  # noqa: ARG002  (aiosmtpd hook signature)
        session: Session,  # noqa: ARG002  (aiosmtpd hook signature)
        envelope: Envelope,
    ) -> str:
        self.envelopes.append(envelope)
        return "250 Message accepted for delivery"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@pytest.fixture
def smtp_server() -> Iterator[tuple[_Collector, int]]:
    collector = _Collector()
    port = _free_port()
    controller = Controller(collector, hostname="127.0.0.1", port=port)
    controller.start()
    try:
        yield collector, port
    finally:
        controller.stop()


async def test_smtp_sender_delivers(smtp_server: tuple[_Collector, int]) -> None:
    collector, port = smtp_server
    sender = SmtpEmailSender(
        parse_smtp_url(f"smtp://127.0.0.1:{port}"), sender=SENDER, clock=FrozenClock(NOW)
    )
    message = verify_email(name="Jane", link=LINK, to="jane@example.com")
    with capture_logs() as logs:
        await sender.send(message)
    assert len(collector.envelopes) == 1
    envelope = collector.envelopes[0]
    assert envelope.mail_from == "no-reply@voiceagents.local"
    assert envelope.rcpt_tos == ["jane@example.com"]
    assert isinstance(envelope.content, bytes)
    received = email.message_from_bytes(envelope.content, policy=email.policy.default)
    assert isinstance(received, MimeMessage)
    assert received["Subject"] == message.subject
    assert received["Date"] == "Thu, 29 Oct 2026 12:30:00 +0000"
    bodies = {part.get_content_type(): part for part in received.iter_parts()}
    text_part, html_part = bodies["text/plain"], bodies["text/html"]
    assert isinstance(text_part, MimeMessage)
    assert isinstance(html_part, MimeMessage)
    assert LINK in text_part.get_content()
    assert "Verify email address" in html_part.get_content()
    assert logs == [
        {"event": "email_sent", "template": "verify_email", "to": "j***@example.com", "log_level": "info"}
    ]


@pytest.mark.parametrize(
    ("url", "use_tls", "start_tls", "port"),
    [
        ("smtps://relay%40corp:p%40ss@smtp.example.com", True, False, 465),
        ("smtp+starttls://relay:pw@smtp.example.com", False, True, 587),
    ],
)
async def test_smtp_sender_passes_tls_and_credentials(
    monkeypatch: pytest.MonkeyPatch, url: str, use_tls: bool, start_tls: bool, port: int
) -> None:
    captured: list[tuple[Any, dict[str, Any]]] = []

    async def fake_send(message: Any, **kwargs: Any) -> None:
        captured.append((message, kwargs))

    monkeypatch.setattr(aiosmtplib, "send", fake_send)
    config = parse_smtp_url(url)
    await SmtpEmailSender(config, sender=SENDER).send(verify_email(name="J", link=LINK, to="j@example.com"))
    [(mime, kwargs)] = captured
    assert isinstance(mime, MimeMessage)
    assert kwargs == {
        "hostname": "smtp.example.com",
        "port": port,
        "username": config.username,
        "password": config.password,
        "use_tls": use_tls,
        "start_tls": start_tls,
        "timeout": SMTP_TIMEOUT_S,
    }
    assert SMTP_TIMEOUT_S == 10.0


async def test_smtp_failure_raises_delivery_error_without_pii() -> None:
    sender = SmtpEmailSender(parse_smtp_url("smtp://127.0.0.1:1"), sender=SENDER)
    message = verify_email(name="Jane", link=LINK, to="jane.private@example.com")
    with capture_logs() as logs, pytest.raises(EmailDeliveryError) as excinfo:
        await sender.send(message)
    text = str(excinfo.value)
    assert "verify_email" in text
    assert "jane.private" not in text
    assert "token" not in text
    assert excinfo.value.__cause__ is None
    [entry] = logs
    assert entry["event"] == "email_send_failed"
    assert entry["to"] == "j***@example.com"
    assert entry["log_level"] == "warning"
    assert "jane.private" not in repr(entry)


async def test_smtp_rejection_reports_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refuse(message: Any, **kwargs: Any) -> None:
        raise aiosmtplib.SMTPRecipientsRefused(
            [
                aiosmtplib.SMTPRecipientRefused(
                    550, "no such user jane.private@example.com", "jane.private@example.com"
                )
            ]
        )

    async def reject_data(message: Any, **kwargs: Any) -> None:
        raise aiosmtplib.SMTPDataError(554, "rejected for jane.private@example.com")

    sender = SmtpEmailSender(parse_smtp_url("smtp://127.0.0.1:1025"), sender=SENDER)
    message = EmailMessage(
        to="jane.private@example.com", subject="s", text="t", html="<p>t</p>", template="x"
    )
    monkeypatch.setattr(aiosmtplib, "send", reject_data)
    with pytest.raises(EmailDeliveryError, match=r"SMTPDataError \(SMTP 554\)") as excinfo:
        await sender.send(message)
    assert "jane.private" not in str(excinfo.value)
    monkeypatch.setattr(aiosmtplib, "send", refuse)
    with pytest.raises(EmailDeliveryError, match=r"SMTPRecipientsRefused$"):
        await sender.send(message)
