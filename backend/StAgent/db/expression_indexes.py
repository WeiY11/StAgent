"""SQLite expression indexes for the UUID-compat predicates.

``uuid_column_equals`` / ``uuid_column_in`` wrap UUID columns in
``lower(replace(CAST(col AS VARCHAR), '-', ''))`` so dashed and hex stored
text compare equal. That expression defeats every plain B-tree index, turning
each filtered query into a full table scan. This module creates expression
indexes with the *identical* expression — SQLite's planner matches expression
trees exactly, so the wrapped predicates become index lookups.

Rule: every UUID-typed column that is a primary key or already carries
``index=True`` gets an expression twin. The schema's own index flags are the
signal for "this column is queried", so future models are covered without a
hand-maintained list. A few hot columns additionally get a composite
(expression, sort-column) form so filter+ORDER BY queries avoid a sort.

Raw-SQL call sites must use the same ``CAST(... AS VARCHAR)`` form (not
``AS TEXT``) or the planner will treat them as different expressions.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.sql.elements import TextClause

logger = logging.getLogger(__name__)

_EXPR = "lower(replace(CAST({col} AS VARCHAR), '-', ''))"

# (table, column) → trailing real columns appended after the expression, so a
# single index serves equality + ORDER BY. Replaces the single-column twin.
_COMPOSITE_SUFFIXES: dict[tuple[str, str], tuple[str, ...]] = {
    # Chat history: filter by session, order by time.
    ("messages", "session_id"): ("created_at",),
    # Sidebar session list: filter by user, order by recency.
    ("chat_sessions", "user_id"): ("updated_at",),
}


def uuid_expression_index_targets(metadata: Any) -> list[tuple[str, str, tuple[str, ...]]]:
    """Return ``(table, column, suffix_columns)`` for every indexed UUID column."""
    targets: list[tuple[str, str, tuple[str, ...]]] = []
    for table in metadata.sorted_tables:
        for col in table.columns:
            try:
                if col.type.python_type is not UUID:
                    continue
            except Exception:  # noqa: BLE001 - non-typed columns can't match
                continue
            if not (col.primary_key or col.index):
                continue
            suffix = _COMPOSITE_SUFFIXES.get((table.name, col.name), ())
            targets.append((table.name, col.name, suffix))
    return targets


def uuid_expression_index_ddl(metadata: Any) -> list[TextClause]:
    """Build idempotent ``CREATE INDEX IF NOT EXISTS`` statements."""
    stmts: list[TextClause] = []
    for table, col, suffix in uuid_expression_index_targets(metadata):
        parts = [_EXPR.format(col=col), *suffix]
        suffix_tag = "".join(f"_{s}" for s in suffix)
        name = f"ixu_{table}_{col}{suffix_tag}"
        stmts.append(
            text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({', '.join(parts)})")
        )
    return stmts


def ensure_sqlite_uuid_expression_indexes(sync_conn: Any) -> int:
    """Create missing expression indexes on a synchronous SQLite connection."""
    from sqlmodel import SQLModel

    created = 0
    for ddl in uuid_expression_index_ddl(SQLModel.metadata):
        try:
            sync_conn.execute(ddl)
            created += 1
        except Exception as exc:  # noqa: BLE001 - index creation is best-effort
            logger.warning("uuid_expression_index_failed: %s (%s)", exc, ddl)
    return created
