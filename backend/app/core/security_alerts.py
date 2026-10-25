"""Security event fan-out (tr-dfem): structured WARNING log always; email to
settings.security_alert_email when set. Best-effort — an alert failure must
never break the request that triggered it."""
import logging

from app.config import settings
from app.services.email_service import EmailServiceProtocol

logger = logging.getLogger(__name__)


async def notify_security_event(
    email_service: EmailServiceProtocol,
    event: str,
    *,
    email: str,
    ip_address: str | None,
    detail: str,
) -> None:
    logger.warning(
        "security.%s account=%s ip=%s %s", event, email, ip_address, detail
    )
    if not settings.security_alert_email:
        return
    try:
        await email_service.send_email(
            settings.security_alert_email,
            f"[Trinity security] {event}",
            f"Событие: {event}\nАккаунт: {email}\nIP: {ip_address}\n{detail}",
        )
    except Exception:
        logger.exception("security alert email failed (event=%s)", event)
