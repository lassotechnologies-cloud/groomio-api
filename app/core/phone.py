"""Phone number normalisation (Doc 9.1 — Kenyan numbers, E.164).

A salon in Nairobi holds its customers' numbers three ways: 0712345678 typed into
a form, +254712345678 pasted from a contact card, and 254712345678 from an old
export. All three are the same person. Storing one canonical form is what makes
`WHERE phone = ?` a reliable duplicate check rather than a coin flip.
"""

# Kenyan mobile numbers are 9 digits after the 0: 7xx (Safaricom) and 1xx (Airtel).
KENYA_COUNTRY_CODE = "254"


def normalize_phone(raw: str) -> str:
    """Return +254XXXXXXXXX for any reasonable Kenyan input.

    Best-effort by design: it normalises separators and the local prefix, but it
    does not reject a number that is the wrong length. A wrong-length number is
    almost always a typo the clerk will notice when the OTP does not arrive, and
    rejecting it at the form costs more goodwill than it saves typos.
    """
    digits = "".join(ch for ch in raw if ch.isdigit())

    if digits.startswith("0"):
        digits = KENYA_COUNTRY_CODE + digits[1:]
    elif digits.startswith(KENYA_COUNTRY_CODE) and not digits.startswith("+"):
        digits = KENYA_COUNTRY_CODE + digits[len(KENYA_COUNTRY_CODE) :]

    return "+" + digits
