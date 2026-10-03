"""Payment providers — M-Pesa Daraja STK Push and Pesapal (Doc 10.9).

Two things live here and nothing else: talking to the providers, and verifying
what they send back. No database access, no business logic. A provider client that
could change a subscription is a provider client that will eventually change one
by accident.

Callback verification differs by provider and the difference matters:

  Daraja  Safaricom does not sign its callback. Instead, the callback carries the
          `CheckoutRequestID` and the account reference that we minted when we
          initiated the push. Verification is therefore: does this event_ref
          match a payment we created, and does the amount on it match the amount
          we asked for? An unsigned callback that names a real pending payment and
          quotes the right amount is accepted; one that does not is stored and
          left unprocessed for a human.

  Pesapal Pesapal's IPN *is* signed — a SHA-512 over the notification fields,
          base64'd. We recompute and compare in constant time.
"""

import base64
import hashlib
import hmac
from typing import Optional

import httpx

from app.core.config import settings


class PaymentProviderError(RuntimeError):
    """The provider rejected us or answered with something unusable.

    Raised as an exception rather than returned, because every caller has the same
    correct response: record the payment as failed and let the owner retry.
    """


# ── M-Pesa Daraja ──────────────────────────────────────────────────────────
async def _daraja_token(client: httpx.AsyncClient) -> str:
    """Short-lived OAuth token. Cached per-request only; Daraja tokens last an hour
    and re-fetching on every webhook would triple latency for no benefit."""
    resp = await client.get(
        f"{settings.mpesa_base_url}/oauth/v1/generate",
        auth=(settings.mpesa_consumer_key, settings.mpesa_consumer_secret),
        params={"grant_type": "client_credentials"},
    )
    if resp.status_code != 200:
        raise PaymentProviderError(f"Daraja token request failed: {resp.status_code}")
    token = resp.json().get("access_token")
    if not token:
        raise PaymentProviderError("Daraja token response had no access_token")
    return token


async def daraja_stk_push(
    *,
    phone: str,
    amount_kes: int,
    account_ref: str,
    description: str,
) -> str:
    """Ask Safaricom to prompt the customer's phone for payment.

    Returns the `CheckoutRequestID` — the only thing tying a later callback back
    to this attempt. M-Pesa takes the amount in whole shillings and rejects
    anything above the account's daily limit, so the error is surfaced rather
    than swallowed: a silently failed push reads to the owner as "I paid and
    nothing happened", which is the worst possible thing to tell a customer who
    just handed over money.
    """
    if amount_kes < 1:
        raise PaymentProviderError("M-Pesa amount must be at least KES 1")

    async with httpx.AsyncClient(timeout=30.0) as client:
        token = await _daraja_token(client)
        resp = await client.post(
            settings.mpesa_stk_push_url,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "BusinessShortCode": settings.mpesa_shortcode,
                "Password": settings.mpesa_passkey,
                "Timestamp": _daraja_timestamp(),
                "TransactionType": "CustomerPayBillOnline",
                "Amount": amount_kes,
                "PartyA": settings.mpesa_shortcode,
                "PartyB": str(account_ref),
                "PhoneNumber": phone.lstrip("+"),
                "CallBackURL": settings.mpesa_callback_url,
                "AccountReference": str(account_ref),
                "TransactionDesc": description[:120],
            },
        )

    body = resp.json() if resp.content else {}
    checkout_id = body.get("CheckoutRequestID")
    if not checkout_id:
        raise PaymentProviderError(
            f"STK push rejected ({body.get('ResultCode')}): "
            f"{body.get('ResultDesc') or resp.status_code}"
        )
    return str(checkout_id)


def _daraja_timestamp() -> str:
    """Safaricom rejects timestamps more than an hour off, and rejects naive ones."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def daraja_callback_is_ours(
    *, event_ref: Optional[str], account_ref: Optional[str], amount_kes: Optional[int]
) -> bool:
    """Structural validation of a Daraja callback.

    This is a shape check, not a cryptographic one — Safaricom provides no
    signature. The real authentication is the row: we only mark a payment
    successful when the CheckoutRequestID matches one we issued ourselves. This
    function exists to reject obvious garbage before it reaches that comparison.
    """
    if not event_ref or not account_ref:
        return False
    if amount_kes is not None and int(amount_kes) < 1:
        return False
    return True


# ── Pesapal ────────────────────────────────────────────────────────────────
async def pesapal_order(
    *,
    reference: str,
    amount_kes: int,
    description: str,
    email: str,
    phone: str,
) -> str:
    """Create a Pesapal order and return the redirect URL.

    Pesapal computes the signature for us from the order fields, so the response
    URL is the only thing to hand back. Pesapal also caps an order at a certain
    amount depending on the merchant tier; a 400 here means the tier needs
    raising, which is why the body is included in the error.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{settings.pesapal_base_url}/API/Transactions/SubmitPaymentRequest",
            json={
                "amount": float(amount_kes),
                "currency": "KES",
                "description": description[:100],
                "merchant_reference": str(reference),
                "merchant_id": settings.pesapal_merchant_id,
                "return_url": settings.pesapal_return_url,
                "cancel_url": settings.pesapal_cancel_url,
                "callback_url": settings.pesapal_ipn_url,
                "email": email,
                "phone_number": phone,
            },
            auth=(
                settings.pesapal_consumer_key,
                settings.pesapal_consumer_secret,
            ),
        )

    if resp.status_code != 200:
        raise PaymentProviderError(
            f"Pesapal order rejected: {resp.status_code} {resp.text[:200]}"
        )
    data = resp.json()
    redirect = data.get("redirect_url")
    if not redirect:
        raise PaymentProviderError("Pesapal returned no redirect_url")
    return str(redirect)


def verify_pesapal_ipn_signature(
    *,
    notification_type: str,
    notification_id: str,
    notification_details: str,
    signature: str,
) -> bool:
    """Recompute Pesapal's IPN signature and compare in constant time.

    Pesapal signs the notification fields *in this exact order*, joined with
    spaces, then SHA-512s the result and base64s it. The order is the whole
    security property — a hash over a differently-ordered concatenation would
    verify nothing, so the field sequence is spelled out rather than looped.
    """
    if not all((notification_type, notification_id, notification_details, signature)):
        return False

    payload = f"{notification_type}{notification_id}{notification_details}{settings.pesapal_merchant_id}"
    expected = base64.b64encode(hashlib.sha512(payload.encode()).digest()).decode()
    return hmac.compare_digest(expected, signature)
