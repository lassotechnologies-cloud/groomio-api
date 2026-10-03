"""OTP sender worker — sends 6-digit codes via Africa's Talking (Doc 10.11)."""
from celery import shared_task

from app.core.config import settings


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def send_otp(self, phone: str, code: str):
    """Send OTP SMS via Africa's Talking. Retries on transient failures."""
    try:
        import africastalking

        africastalking.initialize(settings.at_username, settings.at_api_key)
        sms = africastalking.SMS

        message = f"Your Groomio verification code is {code}. Valid for 10 minutes."
        response = sms.send(message, [phone], sender_id=settings.at_sender_id)

        # Log response for debugging
        recipients = response.get("SMSMessageData", {}).get("Recipients", [])
        if recipients and recipients[0].get("statusCode") != 101:
            raise Exception(f"AT send failed: {recipients[0].get('status')}")

        return {"status": "sent", "phone": phone}

    except Exception as exc:
        # Retry on transient failures
        raise self.retry(exc=exc)
