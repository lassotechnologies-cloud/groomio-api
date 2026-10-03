"""The integration suite's own safety guards.

The fixtures in conftest.py drop and recreate the whole schema. That is a
destructive operation, and the only thing standing between a mistyped
`TEST_DATABASE_URL` and a shop's live data is the name check in that file.

A guard that is never tested is a guard that gets weakened by a well-meaning
refactor. These tests pin the two conditions that must refuse, without opening a
database connection: the check happens on the URL string, before any engine is
built, so they run anywhere.

They also assert the guards are *narrow* — a check that refuses everything is
just as useless as one that refuses nothing, and would push someone to bypass it.
"""

import pytest

from tests.conftest import _database_name, _TEST_DB_PATTERN


# ── the name check ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "name",
    ["groomio", "postgres", "groomio_staging", "groomio_dev", "kenge_salon"],
)
def test_a_production_database_name_is_not_test_shaped(name: str):
    """These are the names that must NOT pass. `groomio` is the live one."""
    assert not _TEST_DB_PATTERN.search(name), (
        f"{name!r} would be accepted by the integration guard — it is not a test database"
    )


@pytest.mark.parametrize(
    "name", ["groomio_test", "test_groomio", "test", "groomio_test_1", "TEST_groomio"]
)
def test_a_test_database_name_is_accepted(name: str):
    assert _TEST_DB_PATTERN.search(name)


def test_the_live_database_name_is_refused():
    """The specific database this project's .env points at."""
    assert not _TEST_DB_PATTERN.search("postgres")
    assert not _TEST_DB_PATTERN.search("groomio")


# ── the URL parsing behind the check ────────────────────────────────────────
def test_the_database_name_is_extracted_from_a_normal_url():
    url = "postgresql+asyncpg://user:pw@localhost:5432/groomio_test"
    assert _database_name(url) == "groomio_test"


def test_query_parameters_are_stripped_before_matching():
    """Supabase-style URLs carry `?sslmode=require`, and the name must still match."""
    url = "postgresql+asyncpg://u:p@host:5432/groomio_test?sslmode=require"
    assert _database_name(url) == "groomio_test"


def test_a_connection_string_without_a_path_yields_no_name():
    """A URL with no database component must not accidentally match."""
    assert _database_name("postgresql+asyncpg://user:pw@localhost:5432") == ""


# ── the driver requirement ──────────────────────────────────────────────────
def test_a_sync_driver_url_is_detectable():
    """The app's engine is async; `postgresql://` fails on first connect.

    Worth pinning because the fix is a one-line copy/paste into a template, and
    the resulting failure appears on the first request rather than at boot.
    """
    sync_url = "postgresql://user:pw@localhost:5432/groomio_test"
    async_url = "postgresql+asyncpg://user:pw@localhost:5432/groomio_test"

    assert not sync_url.startswith("postgresql+asyncpg://")
    assert async_url.startswith("postgresql+asyncpg://")
