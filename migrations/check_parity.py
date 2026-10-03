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

    if problems:
        print("\nPARITY FAILURES:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("\nPARITY OK — models and migrations agree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
