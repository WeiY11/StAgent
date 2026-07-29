"""SQLite FTS5 DDL for the document-chunk knowledge index.

The knowledge Storage Layer persists immutable chunks in ``document_chunks``
(see :mod:`leagent.db.models.document_chunk`). On SQLite we back BM25 ranking
and snippet extraction with an FTS5 *external-content* virtual table
(``document_chunks_fts``) kept in sync via triggers. PostgreSQL deployments
fall back to the lexical (``ILIKE`` / ``to_tsvector``) path instead.

A supplementary trigram virtual table (``document_chunks_trigram``) is created
for better CJK substring matching: the default ``unicode61`` tokenizer treats
each CJK character as a separate token, making multi-character phrase searches
unreliable. The trigram tokenizer (available in SQLite ≥ 3.34) indexes
3-character overlapping n-grams that handle CJK text naturally.

These strings are consumed by Alembic migration ``0006_document_chunks`` and by
:mod:`leagent.library.chunk_search` at query time. Keep the table/column names
here in lock-step with the migration.
"""

from __future__ import annotations

FTS_TABLE = "document_chunks_fts"
TRIGRAM_TABLE = "document_chunks_trigram"
SOURCE_TABLE = "document_chunks"

#: Ordered DDL that creates the external-content FTS5 index plus the sync
#: triggers. Executed only on SQLite (``upgrade()``).
FTS_DDL: tuple[str, ...] = (
    f"""
    CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5(
        text,
        content='{SOURCE_TABLE}',
        content_rowid='rowid'
    )
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS {SOURCE_TABLE}_ai
    AFTER INSERT ON {SOURCE_TABLE} BEGIN
        INSERT INTO {FTS_TABLE}(rowid, text) VALUES (new.rowid, new.text);
    END
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS {SOURCE_TABLE}_ad
    AFTER DELETE ON {SOURCE_TABLE} BEGIN
        INSERT INTO {FTS_TABLE}({FTS_TABLE}, rowid, text)
        VALUES ('delete', old.rowid, old.text);
    END
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS {SOURCE_TABLE}_au
    AFTER UPDATE ON {SOURCE_TABLE} BEGIN
        INSERT INTO {FTS_TABLE}({FTS_TABLE}, rowid, text)
        VALUES ('delete', old.rowid, old.text);
        INSERT INTO {FTS_TABLE}(rowid, text) VALUES (new.rowid, new.text);
    END
    """,
)

#: Reverse DDL (``downgrade()``): drop triggers first, then the FTS table.
FTS_DROP_DDL: tuple[str, ...] = (
    f"DROP TRIGGER IF EXISTS {SOURCE_TABLE}_au",
    f"DROP TRIGGER IF EXISTS {SOURCE_TABLE}_ad",
    f"DROP TRIGGER IF EXISTS {SOURCE_TABLE}_ai",
    f"DROP TABLE IF EXISTS {FTS_TABLE}",
)

#: DDL for the supplementary trigram index that improves CJK substring matching.
#: The trigram tokenizer does not support BM25 ranking, so it is only used as
#: a fallback when the main BM25 path returns no results.
TRIGRAM_DDL: tuple[str, ...] = (
    f"""
    CREATE VIRTUAL TABLE IF NOT EXISTS {TRIGRAM_TABLE} USING fts5(
        text,
        content='{SOURCE_TABLE}',
        content_rowid='rowid',
        tokenize='trigram'
    )
    """,
)

#: Reverse DDL for the trigram index.
TRIGRAM_DROP_DDL: tuple[str, ...] = (
    f"DROP TABLE IF EXISTS {TRIGRAM_TABLE}",
)


def escape_fts_query(raw: str) -> str:
    """Return an FTS5 MATCH expression that is safe for arbitrary user text.

    FTS5 has its own query grammar (``AND``, ``OR``, ``NEAR``, column filters,
    quotes). Rather than expose that surface to end users, each whitespace token
    is wrapped in double quotes (with embedded quotes doubled) and a trailing
    ``*`` prefix match is appended so partial words still hit. Empty input
    yields an empty string, which callers treat as "no FTS query".
    """
    tokens = [t for t in raw.replace('"', " ").split() if t]
    if not tokens:
        return ""
    return " ".join(f'"{token}"*' for token in tokens)


def escape_trigram_query(raw: str) -> str:
    """Escape user input for the trigram FTS5 table.

    The trigram tokenizer accepts plain substring queries — no phrase operators
    or wildcards. Each token is double-quoted to escape FTS5 syntax characters.
    Returns an empty string for blank input.
    """
    tokens = [t for t in raw.replace('"', "").split() if t]
    if not tokens:
        return ""
    return " OR ".join(f'"{token}"' for token in tokens)
