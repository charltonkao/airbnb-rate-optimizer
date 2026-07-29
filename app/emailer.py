"""SMTP delivery of the daily digest."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from .config import settings

log = logging.getLogger(__name__)


def send(subject: str, html: str, text: str = "") -> bool:
    if not settings.email_ready():
        log.warning("Email not configured; skipping send of %r", subject)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.sender()
    msg["To"] = settings.email_to
    msg.set_content(text or "This digest requires an HTML-capable mail client.")
    msg.add_alternative(html, subtype="html")

    try:
        if settings.smtp_ssl:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30) as s:
                if settings.smtp_user:
                    s.login(settings.smtp_user, settings.smtp_pass)
                s.send_message(msg)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as s:
                s.ehlo()
                if settings.smtp_starttls:
                    s.starttls()
                    s.ehlo()
                if settings.smtp_user:
                    s.login(settings.smtp_user, settings.smtp_pass)
                s.send_message(msg)
        log.info("Sent %r to %s", subject, settings.email_to)
        return True
    except Exception:  # noqa: BLE001 - a failed email must not kill the run
        log.exception("Failed to send email %r", subject)
        return False
