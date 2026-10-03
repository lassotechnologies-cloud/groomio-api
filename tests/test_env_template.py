"""The env template must match the settings the app actually reads.

`env.example` is what someone copies to configure a deployment, so every name in
it is a promise. When a name drifts, nothing warns: the setting falls back to its
default, and the failure surfaces later as a broken feature in production rather
than a startup error. That is the worst time to find out.

These tests exist because the template had already drifted. `Settings` reads
`r2_account_id` (so the variable is `R2_ACCOUNT_ID`) while the template shipped
`CLOUDFLARE_R2_ACCOUNT_ID`. Uploads were configured, complete, and would have
returned "not configured" for every request in production.

The names checked here are the ones a broken value breaks *silently* — an
authentication or database mistake crashes loudly, so those need no test.
"""

import os
import re
from pathlib import Path

import pytest

from app.core.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]


def _resolve_env_example() -> Path | None:
    """Locate the env template without assuming a monorepo layout.

    This used to be `parents[2] / "groomio-infra" / "env.example"` — a path that
    only resolves when the five repositories sit side by side in one folder. The
    split made that assumption false: `groomio-api` is now its own repository, so
    the template it pins has to be one it actually owns.

    Resolution order:

      1. `GROOMIO_ENV_EXAMPLE` — set by CI, which checks the `groomio-infra`
         repository out next to this one so the deployment-wide superset template
         is the thing under test.
      2. `.env.example` in this repository — the standalone copy. This is what a
         developer who cloned only `groomio-api` gets, and it is why the guard
         still runs rather than skipping in the common case.
      3. `../groomio-infra/env.example` — the sibling-checkout layout.
    """
    override = os.environ.get("GROOMIO_ENV_EXAMPLE")
    if override:
        return Path(override)
    for candidate in (
        REPO_ROOT / ".env.example",
        REPO_ROOT.parent / "groomio-infra" / "env.example",
    ):
        if candidate.is_file():
            return candidate
    return None


ENV_EXAMPLE = _resolve_env_example()

requires_template = pytest.mark.skipif(
    ENV_EXAMPLE is None,
    reason=(
        "no env template found — clone groomio-infra alongside this repository, "
        "or set GROOMIO_ENV_EXAMPLE to the path of one"
    ),
)


def _template_keys() -> set[str]:
    """Variable names declared in the template, comments and blanks ignored."""
    text = ENV_EXAMPLE.read_text()
    keys = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.add(line.split("=", 1)[0].strip())
    return keys


# Settings whose default is a working local value, so a missing variable in a
# production deployment fails quietly rather than at boot.
SILENTLY_BREAKING = [
    "mpesa_shortcode",
    "mpesa_passkey",
    "pesapal_merchant_id",
    "r2_account_id",
    "r2_access_key_id",
    "r2_secret_access_key",
    "vapid_private_key",
    "at_api_key",
    "sentry_dsn",
]


@requires_template
def test_env_template_exists():
    assert ENV_EXAMPLE.exists(), f"missing {ENV_EXAMPLE}"


@requires_template
@pytest.mark.parametrize("field", SILENTLY_BREAKING)
def test_critical_setting_is_present_in_the_template(field: str):
    """`Settings` has no `env_prefix`, so the variable is the uppercased field."""
    expected = field.upper()
    assert expected in _template_keys(), (
        f"{expected} is read by app.core.config.Settings but is absent from "
        f"env.example — it would silently fall back to its empty default"
    )


@requires_template
def test_no_template_variable_is_invented():
    """A template name the app never reads is a setting someone thinks is on.

    Either the template is stale or the code is missing a setting. Both are worth
    failing on, because the operator's mental model is what's wrong.
    """
    # `model_fields` keys are lowercase (the field name); the template uses the
    # uppercased variable name, so the template side is lowercased to compare.
    readable = {name.lower() for name in Settings.model_fields}
    # Names consumed by other tooling rather than by pydantic-settings: the
    # frontend, Railway, and the superseded names the old template carried. Kept
    # as an explicit list so removing one is a deliberate act.
    consumed_elsewhere = {
        "next_public_supabase_url",
        "next_public_supabase_publishable_key",
    }

    unknown = {k.lower() for k in _template_keys()} - readable - consumed_elsewhere
    assert not unknown, (
        f"env.example declares variables nothing reads: {sorted(unknown)}. "
        f"Either the template is stale or these settings were never added."
    )


@requires_template
def test_database_url_template_uses_the_asyncpg_driver():
    """`postgresql://` with asyncpg raises at first connect.

    The app's engine is created with the async driver, so a copied template that
    says plain `postgresql://` fails on the first request, not at boot.
    """
    text = ENV_EXAMPLE.read_text()
    for line in text.splitlines():
        if line.strip().startswith("DATABASE_URL="):
            assert "+asyncpg" in line, (
                f"DATABASE_URL must include the +asyncpg driver: {line.strip()}"
            )


@requires_template
def test_mpesa_callback_url_matches_the_registered_route():
    """A wrong callback URL means Safaricom pushes into a 404 and no payment lands.

    The route is the single source of truth; the template must agree with it.
    """
    from app.main import app

    paths = app.openapi()["paths"]
    assert "/v1/payments/webhooks/daraja" in paths

    text = ENV_EXAMPLE.read_text()
    match = re.search(r"^MPESA_CALLBACK_URL=(.+)$", text, re.MULTILINE)
    assert match, "MPESA_CALLBACK_URL missing from env.example"
    assert match.group(1).strip() == "https://api.groomio.app/v1/payments/webhooks/daraja"


@requires_template
def test_pesapal_ipn_url_matches_the_registered_route():
    from app.main import app

    assert "/v1/payments/webhooks/pesapal" in app.openapi()["paths"]

    text = ENV_EXAMPLE.read_text()
    match = re.search(r"^PESAPAL_IPN_URL=(.+)$", text, re.MULTILINE)
    assert match, "PESAPAL_IPN_URL missing from env.example"
    assert match.group(1).strip() == "https://api.groomio.app/v1/payments/webhooks/pesapal"


@requires_template
def test_template_commits_no_real_secret():
    """Placeholders only. A real key in here is a key in everyone's repo."""
    text = ENV_EXAMPLE.read_text()
    suspicious = re.findall(r"^([A-Z0-9_]*(?:SECRET|KEY|PASSWORD)[A-Z0-9_]*)=(.+)$", text, re.M)

    for name, value in suspicious:
        # Supabase's `sb_publishable_` key replaces the old anon key and is meant
        # to be public — row-level security is what protects the data. The secret
        # form (`sb_secret_`) is the one that must never be committed.
        if name.endswith("SUPABASE_PUBLISHABLE_KEY"):
            assert value.startswith("sb_publishable_") and "your_key" in value, (
                f"{name} should be an obvious placeholder"
            )
            continue
        assert "your-" in value or value.startswith("change") or value == "", (
            f"{name} looks like a real credential in env.example"
        )


@requires_template
def test_no_supabase_secret_key_is_committed():
    """`sb_secret_` bypasses row-level security and must never be committed.

    Scans assignments rather than the whole file: a comment explaining *why* the
    secret form is forbidden necessarily contains the string, and matching prose
    would make the warning itself fail the check.
    """
    for name, value in re.findall(
        r"^([A-Z0-9_]+)=(.+)$", ENV_EXAMPLE.read_text(), re.MULTILINE
    ):
        assert "sb_secret_" not in value, (
            f"{name} holds a Supabase secret key, which bypasses RLS"
        )
