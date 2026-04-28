"""
Small SMTP wrapper used by the self-service forgot-password flow.

Reads Gmail SMTP credentials from the environment (populated by .env):
    SMTP_USER       — the Gmail address sending the email
    SMTP_PASSWORD   — a 16-char Google App Password (2FA must be on)
    SMTP_FROM_NAME  — optional display name; defaults to "MatrixMatch"

Exposes one function: send_email(to, subject, body_text).
"""

import os
import smtplib
from email.message import EmailMessage
from typing import Optional


SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_TIMEOUT_SECONDS = 15


class MailerError(RuntimeError):
    """Raised when SMTP credentials are missing or Gmail rejects the send.
    Routes should catch this and surface a user-friendly flash message."""


def _from_header() -> str:
    user = os.environ.get("SMTP_USER")
    if not user:
        raise MailerError("SMTP_USER is not set in .env")
    name = os.environ.get("SMTP_FROM_NAME", "MatrixMatch")
    return f"{name} <{user}>"


def send_email(to_addr: str, subject: str, body_text: str) -> None:
    """Send a plain-text email. Raises MailerError on any failure."""
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    if not user or not password:
        raise MailerError("SMTP_USER/SMTP_PASSWORD not configured")

    msg = EmailMessage()
    msg["From"] = _from_header()
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body_text)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(user, password)
            s.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise MailerError(
            "SMTP authentication failed — check SMTP_PASSWORD (must be a Google "
            "App Password with no spaces) and that 2FA is on the account."
        ) from e
    except Exception as e:
        raise MailerError(f"SMTP send failed: {type(e).__name__}: {e}") from e


def send_password_reset(to_addr: str, first_name: Optional[str], reset_url: str, ttl_minutes: int) -> None:
    """Compose and send the forgot-password email."""
    greeting = f"Hi {first_name}," if first_name else "Hi,"
    body = (
        f"{greeting}\n\n"
        "You (or someone) asked to reset your MatrixMatch password.\n\n"
        "Click this link to choose a new password:\n\n"
        f"    {reset_url}\n\n"
        f"This link expires in {ttl_minutes} minutes and can only be used once. "
        "If you didn't ask for this, ignore this email — your password won't "
        "change.\n\n"
        "— MatrixMatch (ISAT U)\n"
    )
    send_email(to_addr, "Reset your MatrixMatch password", body)
