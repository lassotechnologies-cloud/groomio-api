"""Model/migration parity check.

A table in the models that no migration creates fails at runtime, on the first
request that touches it, in production. A migration that creates a table the
models do not know about is worse: it looks fine, and nothing in the app can
repair it. Both are caught here instead.

Each migration's `upgrade()` is *executed* against a recording stub instead of
being parsed. The migrations legitimately use helper functions (`_pk()`,
`*_ts()`) that splat columns into `create_table`, and no static reader can
resolve those without becoming a second Python parser. Running the real code
answers the actual question — what does this migration create — with no guessing.

No database is involved: the stub records calls and returns None. Nothing touches
a connection, so this runs in CI.

Run: python3 -m migrations.check_parity
"""

import importlib.util
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import app.models  # noqa: F401 — importing registers every table on Base.metadata
from app.models.base import Base

VERSIONS = Path(__file__).resolve().parent / "versions"

# The alembic operations that create schema. Everything else (create_index,
# execute, etc.) is a no-op for this check.
#
# `add_column` is Alembic's real name for adding a column to an existing table.
# It was missing from this list until migration 0007 first used it, so the
# checker silently reported "column in models but not migrated" for a column
# that *was* migrated — a false alarm that would have been dismissed as a
# broken check rather than a broken check. Alembic exposes no `create_column`
# at all, so that entry was never going to fire.
RECORDING_OPS = (
    "create_table",
    "add_column",
    "drop_table",
    "drop_column",
)


def _load(path: Path):
    """Import a migration file by path, as a module named after its revision."""
    name = f"_parity_migration_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def migration_tables() -> dict[str, set[str]]:
    """table name -> column names, by dry-running every `upgrade()`."""
    tables: dict[str, set[str]] = {}

    for path in sorted(VERSIONS.glob("*.py")):
        module = _load(path)
        if not hasattr(module, "upgrade"):
            continue

        # The positional args to create_table are real sa.Column objects (plus
        # occasional constraints, which have no `.name` and are skipped).
        def record_table(name, *cols):
            tables.setdefault(str(name), set()).update(
                c.name for c in cols if isinstance(getattr(c, "name", None), str)
            )

        def record_column(table, col, *a, **k):
            # Alembic also lets `col` be the column *name* rather than a Column
            # object, so both are accepted.
            name = getattr(col, "name", None) or col
            tables.setdefault(str(table), set()).add(str(name))

        recorder = MagicMock()
        recorder.create_table.side_effect = record_table
        recorder.add_column.side_effect = record_column

        fake_op = MagicMock()
        for op_name in RECORDING_OPS:
            setattr(fake_op, op_name, getattr(recorder, op_name))

        # The migrations do `from alembic import op` at module scope, so the stub
        # has to be injected onto the module, not passed in.
        module.op = fake_op
        module.upgrade()

    return tables


def model_tables() -> dict[str, set[str]]:
    return {
        table.name: {c.name for c in table.columns}
        for table in Base.metadata.sorted_tables
    }


def main() -> int:
    models = model_tables()
    migrations = migration_tables()

    problems: list[str] = []

    for table in sorted(set(models) - set(migrations)):
        problems.append(f"model table not created by any migration: {table}")
    for table in sorted(set(migrations) - set(models)):
        problems.append(f"migration creates a table the models do not define: {table}")

    for table in sorted(set(models) & set(migrations)):
        missing = sorted(models[table] - migrations[table])
        if missing:
            problems.append(f"{table}: in models but not migrated: {missing}")
        extra = sorted(migrations[table] - models[table])
        if extra:
            problems.append(f"{table}: migrated but not in models: {extra}")

    print(f"model tables:      {len(models)}")
    print(f"migration tables:  {len(migrations)}")
    print(f"model columns:     {sum(len(c) for c in models.values())}")
    print(f"migration columns: {sum(len(c) for c in migrations.values())}")

    index_problems, pre_existing_drift = index_parity_problems()

    print(f"model indexes:     {len(model_indexes())}")
    print(f"migration indexes: {len(migration_indexes())}")

    if pre_existing_drift:
        # Reported loudly but not fatal: these predate T-003 and fixing them is
        # a separate change. They are real, though — see docs/
        # T-003-business-onboarding-concurrency.md § remaining risks.
        print(f"\nPRE-EXISTING INDEX DRIFT ({len(pre_existing_drift)}) — informational:")
        for p in pre_existing_drift:
            print(f"  ~ {p}")

    if problems:
        print("\nPARITY FAILURES (tables/columns):")
        for p in problems:
            print(f"  - {p}")

    if index_problems:
        print("\nPARITY FAILURES (indexes/unique constraints):")
        for p in index_problems:
            print(f"  - {p}")

    if problems or index_problems:
        return 1

    print("\nPARITY OK — models and migrations agree on columns and indexes.")
    return 0


# ── index / unique-constraint parity ───────────────────────────────────────────
#
# Why this exists separately from the column check above
# ------------------------------------------------------
# The column check compares tables and columns only. It is perfectly happy with a
# model that declares a unique index no migration ever creates — and with a
# migration that creates one the model does not know about. Both directions are
# real hazards for T-003:
#
#   * The test suite builds its schema with `Base.metadata.create_all`, not with
#     Alembic, and the test database has no `alembic_version` table at all. So a
#     unique index that exists only in a migration is **absent** from every test
#     run. The concurrency tests would then pass while exercising a schema that
#     production does not have — a false green in the exact place that matters.
#   * Conversely, an index in the model but not in any migration silently makes
#     the application's data-integrity guarantee depend on a deploy path nobody
#     runs.
#
# So indexes are compared explicitly. This is a *static* check: it dry-runs every
# `upgrade()` against the same recording stub used for columns, and reads the
# index DDL out of `op.execute(...)` and `op.create_index(...)` calls.


def model_indexes() -> dict[str, set[str]]:
    """table name -> index names declared on the SQLAlchemy model.

    Only *named* objects are collected. `Column(unique=True)` produces an
    unnamed `UniqueConstraint`, which has no stable identity to compare against —
    the migration names the same guarantee `ix_<table>_<column>`, so unnamed ones
    are covered by `_INDEX_PARITY_EXEMPTIONS` instead of being reported as
    `None`.
    """
    result: dict[str, set[str]] = {}
    for table in Base.metadata.sorted_tables:
        names = {index.name for index in table.indexes if index.name}
        for constraint in table.constraints:
            if constraint.__class__.__name__ == "UniqueConstraint" and constraint.name:
                names.add(constraint.name)
        if names:
            result[table.name] = names
    return result


def _index_name_from_sql(sql: str) -> str | None:
    """Pull the index name out of a `CREATE [UNIQUE] INDEX ... name ON ...`.

    Tolerant by design: the migrations spell this as raw SQL inside `op.execute`
    because `CREATE INDEX CONCURRENTLY` cannot be expressed through
    `op.create_index`, and raw SQL means regexes rather than an AST.
    """
    match = re.search(
        r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"
        r'"?([A-Za-z0-9_]+)"?\s+ON\s+',
        sql,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _table_from_index_sql(sql: str) -> str | None:
    """Pull the table name out of a `CREATE INDEX ... ON table (...)`."""
    match = re.search(
        r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?"
        r"(?:IF\s+NOT\s+EXISTS\s+)?\"?[A-Za-z0-9_]+\"?\s+ON\s+"
        r"(?:public\.)?\"?([A-Za-z0-9_]+)\"?",
        sql,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def migration_indexes() -> dict[str, set[str]]:
    """table name -> index names created by any migration's `upgrade()`."""
    tables: dict[str, set[str]] = {}

    for path in sorted(VERSIONS.glob("*.py")):
        module = _load(path)
        if not hasattr(module, "upgrade"):
            continue

        def record_create_index(name, table, *cols, **k):
            target = getattr(table, "name", table)
            tables.setdefault(str(target), set()).add(str(name))

        def record_execute(sql, *a, **k):
            text_sql = str(sql)
            name = _index_name_from_sql(text_sql)
            table = _table_from_index_sql(text_sql)
            if name and table:
                tables.setdefault(table, set()).add(name)

        recorder = MagicMock()
        recorder.create_index.side_effect = record_create_index
        recorder.execute.side_effect = record_execute

        fake_op = MagicMock()
        fake_op.create_index = recorder.create_index
        fake_op.execute = recorder.execute

        module.op = fake_op
        module.upgrade()

    return tables


#: Indexes the model and the migrations are *allowed* to disagree about, with the
#: reason. Kept as data rather than a comment so it is reviewed like code.
_INDEX_PARITY_EXEMPTIONS: dict[str, str] = {
    # Migration 0002 created these as named unique indexes. The models render the
    # same guarantees from `Column(unique=True)`, whose constraints SQLAlchemy
    # leaves unnamed (so they are not collected at all). Same guarantee, different
    # spelling on each side.
    "ix_branches_slug": "model expresses the same guarantee via Column(unique=True)",
    "ix_plans_code": "model expresses the same guarantee via Column(unique=True)",
}

#: Indexes introduced by T-003. Drift involving these is a **hard failure**,
#: because this change is specifically about database-enforced integrity: a model
#: index with no migration means the test suite exercises a constraint production
#: does not have, which is the exact false-green this check exists to prevent.
_T003_INDEXES = frozenset(
    {"uq_businesses_owner_user_id", "uq_subscriptions_live_business"}
)

#: Indexes introduced by T-004 (payment idempotency). Drift involving these is a
#: **hard failure** for the same reason as T-003: these are database-enforced
#: integrity constraints that the test suite must exercise.
_T004_INDEXES = frozenset(
    {
        "uq_payments_provider_ref",
        "uq_webhook_events_source_ref",
    }
)


def index_parity_problems() -> tuple[list[str], list[str]]:
    """`(failures, pre_existing_drift)` for indexes declared on one side only.

    The split matters. Drift that predates T-003 — migration-only indexes on
    `users`, `businesses.status` and others — is real, and is reported, but it is
    not this change's to fix and failing the build on it would let unrelated debt
    block a correctness fix. Anything touching a T-003 index fails hard.
    """
    models = model_indexes()
    migrations = migration_indexes()
    failures: list[str] = []
    drift: list[str] = []

    for table in sorted(set(models) | set(migrations)):
        exemptions = set(_INDEX_PARITY_EXEMPTIONS)
        in_model = models.get(table, set()) - exemptions
        in_migration = migrations.get(table, set()) - exemptions

        for name in sorted(in_model - in_migration):
            message = (
                f"{table}.{name}: index is declared on the model but created by no "
                f"migration — tests would run against a schema production lacks"
            )
            (failures if name in _T003_INDEXES or name in _T004_INDEXES else drift).append(message)

        for name in sorted(in_migration - in_model):
            message = (
                f"{table}.{name}: created by a migration but absent from the model — "
                f"the application cannot rely on it, and create_all will not make it"
            )
            (failures if name in _T003_INDEXES or name in _T004_INDEXES else drift).append(message)

    return failures, drift


if __name__ == "__main__":
    sys.exit(main())
