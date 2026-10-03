"""Group I tests — chat content safety, AI project rules, route surface.

The chat filter is the one piece of Group I where a silent error causes real harm
rather than an annoyance: a missed phone number is a customer's number sitting in
a database under a guarantee that said it would not be (CU6). Those cases are
exhaustively enumerated below, including the false positives, because a filter
that mangles ordinary English gets switched off by an owner and then the
guarantee is gone for a different reason.

The AI Studio tests cover the rules that protect the owner's time — photo minimum
and double-submit — not the visual backend, which is deliberately not wired up.
"""

import pytest

from app.ai_studio.schemas import MIN_PHOTOS, AIProjectCreateRequest
from app.messaging.filter import contains_abuse, filter_message, mask_phone


# ── CU6: phone numbers must never be stored ────────────────────────────────
@pytest.mark.parametrize(
    "raw",
    [
        "0722123456",
        "0722 123 456",
        "0712-345-678",
        "+254722123456",
        "254 722 123 456",
        "254722123456",
    ],
)
def test_every_kenyan_number_format_is_masked(raw: str):
    masked, found = mask_phone(f"Call me on {raw}")
    assert found is True
    # No run of digits from the original survives.
    assert "722123456" not in masked
    assert "712345678" not in masked
    # The country code stays: it identifies nobody and signals a number was here.
    assert "254" in masked or "0" in masked


def test_bare_nine_digit_run_is_masked():
    """Someone pasting from a contact card drops the prefix."""
    masked, found = mask_phone("WhatsApp 722123456")
    assert found is True
    assert "722123456" not in masked


def test_masking_leaves_ordinary_digits_alone():
    """A price or a queue number is not a phone number."""
    masked, found = mask_phone("It costs 1500 and you are number 12 in the queue")
    assert found is False
    assert masked == "It costs 1500 and you are number 12 in the queue"


def test_masking_keeps_the_rest_of_the_message():
    masked, _ = mask_phone("Please call 0722123456 about Saturday")
    assert masked.startswith("Please call ")
    assert masked.endswith(" about Saturday")


def test_two_numbers_in_one_message_are_both_masked():
    masked, found = mask_phone("Me on 0722123456 or the shop on 0712345678")
    assert found is True
    assert "0722123456" not in masked
    assert "0712345678" not in masked


# ── abuse: whole words only ─────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text",
    ["you are an idiot", "stupid haircut", "what nonsense", "this is rubbish"],
)
def test_actual_abuse_is_detected(text: str):
    assert contains_abuse(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "nonsense-free shampoo",
        "garbage-free disposal",
        "idiot-proof design",
        "we are class",
        "that was stupidity",
        "she is foolish",
        "a foolish decision",
    ],
)
def test_hyphenated_and_lookalike_words_are_not_abuse(text: str):
    """The Scunthorpe problem, concretely.

    A filter that fires on "class" or "foolish" is one an owner turns off, and a
    disabled filter protects nobody. The whole-word rule is what keeps this
    filter usable.
    """
    assert contains_abuse(text) is False


# ── filter_message: what actually reaches the database ──────────────────────
def test_filter_masks_on_write_not_on_read():
    """The original text must not be recoverable from the FilterResult."""
    result = filter_message("My number is 0722123456")
    assert result.had_phone is True
    assert "0722123456" not in result.text
    assert result.masked != ""


def test_clean_message_passes_through_untouched():
    result = filter_message("Can I come at 4pm for a cut?")
    assert result.text == "Can I come at 4pm for a cut?"
    assert result.had_phone is False
    assert result.was_abusive is False
    assert result.masked == ""


def test_abuse_is_flagged_but_not_rewritten():
    """The router decides to refuse; the filter only reports.

    Silently rewriting an insult into something nicer is a product decision that
    belongs to the shop, not to a regex module.
    """
    result = filter_message("you are an idiot")
    assert result.was_abusive is True
    assert result.text == "you are an idiot"


def test_abusive_message_with_a_number_reports_both():
    result = filter_message("idiot, my number is 0722123456")
    assert result.was_abusive is True
    assert result.had_phone is True
    assert "0722123456" not in result.text


def test_empty_message_is_safe():
    result = filter_message("")
    assert result.text == ""
    assert result.had_phone is False
    assert result.was_abusive is False


# ── AI Studio rules (I7/I8) ─────────────────────────────────────────────────
def test_five_photos_is_the_documented_minimum():
    assert MIN_PHOTOS == 5


def test_project_accepts_a_partial_photo_set():
    """I7 must not block an owner part-way through uploading."""
    payload = AIProjectCreateRequest(
        shop_name="Kenge Hair Studio", photo_urls=["r2://photos/1.jpg"]
    )
    assert len(payload.photo_urls) == 1


def test_project_requires_at_least_one_photo():
    with pytest.raises(Exception):
        AIProjectCreateRequest(shop_name="Kenge Hair Studio", photo_urls=[])


def test_ai_routes_are_registered():
    """Guards against the studio silently reverting to a stub."""
    from app.main import app

    paths = app.openapi()["paths"]
    assert "/v1/ai/projects" in paths
    assert "/v1/ai/projects/{project_id}" in paths
    assert "/v1/ai/projects/{project_id}/generate" in paths


def test_generate_returns_202_not_200():
    """Generation is queued; a 200 would invite the client to wait on it."""
    from app.main import app

    op = app.openapi()["paths"]["/v1/ai/projects/{project_id}/generate"]["post"]
    assert "202" in op["responses"]


# ── messaging routes are live ───────────────────────────────────────────────
def test_messaging_routes_are_registered():
    from app.main import app

    paths = app.openapi()["paths"]
    for path in (
        "/v1/threads",
        "/v1/threads/{thread_id}/messages",
        "/v1/public/threads/{thread_id}/messages",
        "/v1/push/subscribe",
        "/v1/push/unsubscribe",
        "/v1/notifications",
        "/v1/notifications/{notification_id}/read",
        "/v1/campaigns",
        "/v1/campaigns/{campaign_id}/send",
    ):
        assert path in paths, f"{path} missing"


def test_customer_facing_thread_endpoints_need_no_auth():
    """Customers have no account (Doc 2.14) — the booking portal is a link.

    If these ever gain an auth requirement, customers silently lose the chat
    feature and nothing in the shop app will report it.
    """
    from app.main import app

    op = app.openapi()["paths"]["/v1/public/threads/{thread_id}/messages"]["get"]
    assert "security" not in op
