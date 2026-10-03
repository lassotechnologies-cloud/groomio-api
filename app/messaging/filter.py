"""Chat content safety (Doc 2.11, CU6).

Two protections, both enforced **server-side** because a filter the client can
bypass is not a filter:

  1. **Phone numbers are masked.** CU6 exists so a customer can ask the shop a
     question without handing over their number. The moment a shop replies with
     "call 0722 123 456", the guarantee is gone and the customer has been
     exposed without consent. Digit runs are therefore rewritten on the way in,
     not on the way out — masking at render time would leave the real number
     readable in the database, and in any other API that forgot to mask.

  2. **Abuse patterns are refused.** Crude words are caught as whole tokens, so
     "class" and "Scunthorpe problem" words do not trip the filter.

The masker is deliberately aggressive about digits and deliberately timid about
language: a false positive that mangles "0722" costs a customer one clarifying
message, whereas a false negative exposes their number permanently.
"""

import re
from typing import NamedTuple

# Kenyan formats, and the international ones people paste from WhatsApp.
#   0722 123 456 / 0712345678 / +254722123456 / 254 722 123 456
_PHONE = re.compile(
    r"""
    (?<!\w)
    (?:
        \+?254[\s-]?          # country code
        | 0                    # local prefix
    )
    (?:7|1)\d{2}[\s-]?\d{3}[\s-]?\d{3}   # 3-3-3 subscriber digits
    (?!\w)
    """,
    re.VERBOSE,
)

# A bare run of 9+ digits, which is almost always a phone number written without
# its prefix — worth catching even though the strict pattern above would miss it.
_BARE_DIGITS = re.compile(r"(?<!\d)\d{9,}(?!\d)")

# Whole-word abuse terms.
#
# `\b` alone is not enough: Python treats a hyphen as a word boundary, so
# "nonsense-free" and "idiot-proof" would both trip the filter. The lookahead
# rejects a following hyphen or letter, which is what makes these whole words
# inside hyphenated compounds.
_ABUSIVE = re.compile(
    r"""
    (?<![\w-])(?:
        idiot | stupid | fool | nonsense | rubbish | garbage | trash
        | shutup | shut\ up | hate\ you | kill\ you
    )(?![\w-])
    """,
    re.VERBOSE | re.IGNORECASE,
)


class FilterResult(NamedTuple):
    text: str
    had_phone: bool
    was_abusive: bool
    masked: str  # what the customer actually sees, for the "we removed this" note


def mask_phone(text: str) -> tuple[str, bool]:
    """Replace phone numbers with a masked placeholder.

    `+254722123456` → `+254••• ••• •••`. The country code survives because it is
    not identifying on its own and it tells the reader this *was* a number.
    """
    found = False

    def _mask_prefixed(match: re.Match) -> str:
        nonlocal found
        found = True
        raw = match.group(0)
        prefix = "+254" if "254" in raw else "0"
        spacer = " " if " " in raw else ("-" if "-" in raw else "")
        return f"{prefix}•••{spacer}•••{spacer}•••"

    def _mask_bare(match: re.Match) -> str:
        nonlocal found
        found = True
        return "•••••••••"

    masked = _PHONE.sub(_mask_prefixed, text)
    masked = _BARE_DIGITS.sub(_mask_bare, masked)
    return masked, found


def contains_abuse(text: str) -> bool:
    return bool(_ABUSIVE.search(text))


def filter_message(text: str) -> FilterResult:
    """Apply both protections and report what happened.

    The caller decides what to do about a hit. Masking is silent and automatic;
    refusal is a product decision, so this returns the flag rather than raising.
    """
    masked, had_phone = mask_phone(text)
    return FilterResult(
        text=masked,
        had_phone=had_phone,
        was_abusive=contains_abuse(text),
        masked=masked if had_phone else "",
    )
