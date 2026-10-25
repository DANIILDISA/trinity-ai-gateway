"""tr-dfem: security alert fan-out — log always, email only when configured,
and an email failure must never propagate."""
import logging

import pytest

from app.core.security_alerts import notify_security_event
from app.services.email_service import MockEmailService

pytestmark = pytest.mark.asyncio


class _ExplodingEmailService:
    async def send_email(self, to: str, subject: str, body: str) -> None:
        raise RuntimeError("smtp down")


async def test_logs_and_emails_when_configured(monkeypatch, caplog):
    from app.config import settings
    monkeypatch.setattr(settings, "security_alert_email", "sec@trinity.local")
    svc = MockEmailService()
    with caplog.at_level(logging.WARNING):
        await notify_security_event(
            svc, "admin_login_failed",
            email="root@x.com", ip_address="1.2.3.4", detail="attempt 2/3",
        )
    assert any("admin_login_failed" in r.getMessage() for r in caplog.records)
    assert len(svc.sent) == 1
    assert svc.sent[0]["to"] == "sec@trinity.local"
    assert "root@x.com" in svc.sent[0]["body"]


async def test_no_email_when_unconfigured(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "security_alert_email", "")
    svc = MockEmailService()
    await notify_security_event(svc, "account_blocked", email="u@x.com", ip_address=None, detail="")
    assert svc.sent == []


async def test_email_failure_is_swallowed(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "security_alert_email", "sec@trinity.local")
    # Must not raise:
    await notify_security_event(
        _ExplodingEmailService(), "admin_login_failed",
        email="root@x.com", ip_address="1.2.3.4", detail="",
    )
