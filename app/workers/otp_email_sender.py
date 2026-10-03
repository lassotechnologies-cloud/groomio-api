"""OTP sender worker — email channel (Doc 10.2 A2).

SMS has first claim on signup verification: a Kenyan phone-first app should
default to the channel the customer is holding. Email is the fallback — a SIM
in another country, a phone left at the desk, or a customer who simply did not
get the text. The code is the same 6 digits and the security model is identical;
only the transport changes.

This is deliberately a separate task from `send_otp` (SMS) rather than one task
with a branch. A single task that sends both would couple the two transports,
so a broken SMTP config would also break SMS delivery, and vice versa. Each
channel fails independently and retries independently.

Delivery is fire-and-forget from the request endpoint: the code is stored
hashed before the task is queued, so the request returns immediately and the
worker owns the retry. See `otp_sender.send_otp` for the SMS counterpart.
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from celery import shared_task

from app.core.config import settings

logger = logging.getLogger(__name__)


def _configured() -> bool:
    """Whether SMTP credentials are present.

    Checked rather than assumed so a misconfigured deployment fails loudly in
    the worker log instead of queueing codes that never arrive.
    """
    return bool(settings.email_host and settings.email_user and settings.email_pass)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def send_otp_email(self, email: str, code: str, purpose: str = "signup_verification"):
    """Send the 6-digit OTP to an email address. Retries on transient failures."""
    if not _configured():
        # A permanent misconfiguration is not worth retrying — it would burn
        # three minutes per code. Raise so the failure is visible in the log.
        raise RuntimeError(
            "Email OTP is not configured: set EMAIL_HOST/EMAIL_USER/EMAIL_PASS"
        )

    purpose_label = (
        "signup verification"
        if purpose == "signup_verification"
        else "login verification"
    )
    expires_in = settings.otp_ttl_min

    text = (
        f"Your Groomio {purpose_label} code is {code}.\n"
        f"Valid for {expires_in} minutes.\n"
        "Do not share this code with anyone.\n"
        "If you did not request this, ignore this email."
    )
    html = (
        "<html><body style='font-family: sans-serif;'>"
        "<p>Your <strong>Groomio</strong> verification code is:</p>"
        f"<p style='font-size: 1.5em; letter-spacing: 0.1em;'><strong>{code}</strong></p>"
        f"<p>Valid for <strong>{expires_in} minutes</strong>.</p>"
        "<p>Do not share this code with anyone.</p>"
        "<p>If you did not request this, you can ignore this email.</p>"
        "</body></html>"
    )

    message = MIMEMultipart("alternative")
    message["Subject"] = f"Your Groomio {purpose_label} code"
    message["From"] = settings.email_from
    message["To"] = email

    message.attach(MIMEText(text, "plain"))
    message.attach(MIMEText(html, "html"))

    try:
        if settings.email_use_tls:
            server = smtplib.SMTP(settings.email_host, settings.email_port)
            server.starttls()
        else:
            server = smtplib.SMTP(settings.email_host, settings.email_port)
        try:
            server.login(settings.email_user, settings.email_pass)
            server.sendmail(settings.email_from, [email], message.as_string())
        finally:
            server.quit()
    except smtplib.SMTPAuthenticationError:
        # Permanent failure — no point retrying. Log without exposing credentials.
        logger.error(
            "Email OTP authentication failed for %s: check EMAIL_USER/EMAIL_PASS",
            settings.email_user,
        )
        raise
    except smtplib.SMTPException as exc:
        # Transient SMTP failure — retry.
        logger.warning("Email OTP SMTP error for %s: %s", email, exc)
        raise self.retry(exc=exc)
    except Exception as exc:
        logger.error("Email OTP unexpected error for %s: %s", email, exc)
        raise self.retry(exc=exc)

    logger.info("Email OTP sent to %s (purpose=%s)", email, purpose)
    return {"status": "sent", "email": email}
