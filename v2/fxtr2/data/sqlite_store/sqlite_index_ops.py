"""SQLite index operations for namespace indexing.

Leaf module containing all index-related SQL helpers. Does NOT import from
sqlite_ops (to avoid circular dependencies). sqlite_ops imports FROM here.

All public functions take ``sqlite3.Connection`` as the first parameter and
manage their own transactions (same pattern as ``sqlite_ops.py``).  Private
helpers prefixed with ``_`` must be called within an existing transaction.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Sequence

from fxtr2.data import entity_core
from fxtr2.data.sqlite_store.sqlite_util import transaction
from fxtr2.data.store_indexing.indexing_types import IndexerColumnType
from fxtr2.data.store_indexing.sql_expr import COLUMN_TYPE_TO_SQLITE

logger = logging.getLogger(__name__)


# =============================================================================
# Table-name helpers
# =============================================================================


def _index_table_name(indexer_entity_iid: int) -> str:
    return f"NS_INDEX__{indexer_entity_iid}"


def _processed_table_name(indexer_entity_iid: int) -> str:
    return f"NS_INDEX_PROCESSED__{indexer_entity_iid}"


# =============================================================================
# Column-type encoding (stored in ns_index_registry)
# =============================================================================


def _encode_column_types(types: Sequence[IndexerColumnType]) -> str:
    """Encode column types as comma-separated names for storage."""
    return ",".join(t.name for t in types)


def _decode_column_types(s: str) -> tuple[IndexerColumnType, ...]:
    """Decode column types from comma-separated names."""
    if not s:
        return ()
    return tuple(IndexerColumnType[name] for name in s.split(","))


def _decode_column_type(s: str) -> IndexerColumnType:
    return IndexerColumnType[s]


# =============================================================================
# Prefix-matching helpers
# =============================================================================


def _escape_like(prefix: str) -> str:
    """Escape a string for use in ``LIKE ? || '%' ESCAPE '\\\\'``."""
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# =============================================================================
# Exceptions
# =============================================================================


class IndexMismatchError(Exception):
    """Raised when INDEXED registrations changed during pin CAS.

    ``current_registrations`` contains the *actual* set of INDEXED
    registrations at the time of failure so the caller can retry.
    """

    def __init__(self, current_registrations: list[tuple[int, int, str, bytes]]):
        # Each tuple is (reg_id, indexer_entity_iid, key_prefix, indexer_public_id)
        self.current_registrations = current_registrations
        super().__init__("INDEXED registrations changed during pin operation")


# =============================================================================
# Private helpers (called within existing transactions — no own transactions)
# =============================================================================


def _ensure_index_tables_exist(
    conn: sqlite3.Connection,
    indexer_entity_iid: int,
    key_types: Sequence[IndexerColumnType],
    value_type: IndexerColumnType,
) -> None:
    """CREATE TABLE IF NOT EXISTS for the per-indexer index and processed tables."""
    table = _index_table_name(indexer_entity_iid)
    processed = _processed_table_name(indexer_entity_iid)

    # Build column definitions for key columns
    key_col_defs = []
    key_col_names = []
    for i, kt in enumerate(key_types):
        col_name = f"IXKEY_{i}"
        key_col_names.append(col_name)
        key_col_defs.append(f"{col_name} {COLUMN_TYPE_TO_SQLITE[kt]}")

    val_type_sql = COLUMN_TYPE_TO_SQLITE[value_type]
    pk_cols = ", ".join(["namespace_id", "entity_internal_id"] + key_col_names)
    all_col_defs = (
        ["namespace_id INTEGER NOT NULL", "entity_internal_id INTEGER NOT NULL"]
        + key_col_defs
        + [f"VALUE {val_type_sql}"]
    )

    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {table} ("
        f"  {', '.join(all_col_defs)},"
        f"  PRIMARY KEY ({pk_cols})"
        ")"
    )
    conn.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_value ON {table}(VALUE)")

    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {processed} ("
        "  namespace_id INTEGER NOT NULL,"
        "  entity_internal_id INTEGER NOT NULL,"
        "  PRIMARY KEY (namespace_id, entity_internal_id)"
        ")"
    )


def _write_index_rows(
    conn: sqlite3.Connection,
    ns_id: int,
    indexer_entity_iid: int,
    entity_iid: int,
    rows: list[tuple],
) -> None:
    """INSERT rows into NS_INDEX__.

    Each row is ``(*key_values, value)`` — a flat tuple whose length equals
    the number of IXKEY columns plus one (for VALUE).
    """
    if not rows:
        return
    table = _index_table_name(indexer_entity_iid)
    num_key_cols = len(rows[0]) - 1  # last element is VALUE
    cols = ", ".join(
        ["namespace_id", "entity_internal_id"]
        + [f"IXKEY_{i}" for i in range(num_key_cols)]
        + ["VALUE"]
    )
    placeholders = ", ".join(["?"] * (2 + num_key_cols + 1))
    sql = f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({placeholders})"
    for row in rows:
        conn.execute(sql, (ns_id, entity_iid, *row))


def _write_processed_marker(
    conn: sqlite3.Connection,
    ns_id: int,
    indexer_entity_iid: int,
    entity_iid: int,
) -> None:
    """INSERT into NS_INDEX_PROCESSED__ (INSERT OR IGNORE for idempotence)."""
    processed = _processed_table_name(indexer_entity_iid)
    conn.execute(
        f"INSERT OR IGNORE INTO {processed} (namespace_id, entity_internal_id) VALUES (?, ?)",
        (ns_id, entity_iid),
    )


def _delete_index_entries_for_entity_if_unneeded(
    conn: sqlite3.Connection,
    ns_id: int,
    entity_iid: int,
) -> None:
    """Delete index entries per-indexer for an entity where no covered pin remains.

    For each indexer in this namespace, checks whether the entity is still
    pinned under any key covered by that indexer's INDEXED registrations.
    Deletes index entries and processed markers only for indexers where no
    covered pin remains.

    Must be called within an existing transaction, **after** the pin that
    triggered this call has already been removed or updated.
    """
    # Find indexers that still have at least one remaining pin for this entity
    # covered by one of their INDEXED registrations.
    cursor = conn.execute(
        """
        SELECT DISTINCT r.indexer_entity_id
        FROM ns_index_registry r
        JOIN namespace_pin p
          ON p.namespace_id = r.namespace_id
         AND p.entity_id = ?
         AND SUBSTR(p.name, 1, LENGTH(r.key_prefix)) = r.key_prefix
        WHERE r.namespace_id = ? AND r.state = 'INDEXED'
        """,
        (entity_iid, ns_id),
    )
    still_needed: set[int] = {row[0] for row in cursor.fetchall()}

    # Get all indexer IIDs with registrations in this namespace.
    cursor = conn.execute(
        "SELECT DISTINCT indexer_entity_id FROM ns_index_registry WHERE namespace_id = ?",
        (ns_id,),
    )
    all_iids = [row[0] for row in cursor.fetchall()]

    # Delete entries only for indexers that no longer need them.
    for iid in all_iids:
        if iid in still_needed:
            continue
        table = _index_table_name(iid)
        processed = _processed_table_name(iid)
        conn.execute(
            f"DELETE FROM {table} WHERE namespace_id = ? AND entity_internal_id = ?",
            (ns_id, entity_iid),
        )
        conn.execute(
            f"DELETE FROM {processed} WHERE namespace_id = ? AND entity_internal_id = ?",
            (ns_id, entity_iid),
        )


def _drop_index_tables_if_unused(
    conn: sqlite3.Connection,
    indexer_entity_iid: int,
) -> None:
    """DROP the per-indexer tables if no registrations reference this indexer (in any namespace)."""
    cursor = conn.execute(
        "SELECT 1 FROM ns_index_registry WHERE indexer_entity_id = ? LIMIT 1",
        (indexer_entity_iid,),
    )
    if cursor.fetchone() is not None:
        return  # still in use somewhere

    table = _index_table_name(indexer_entity_iid)
    processed = _processed_table_name(indexer_entity_iid)

    # Tables should have been fully cleared before we get here.
    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    assert row is not None and row[0] == 0, f"{table} is not empty before drop"
    row = conn.execute(f"SELECT COUNT(*) FROM {processed}").fetchone()
    assert row is not None and row[0] == 0, f"{processed} is not empty before drop"

    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"DROP TABLE {processed}")


def _get_indexed_registrations_for_key(
    conn: sqlite3.Connection,
    ns_id: int,
    key: str,
) -> list[tuple[int, int, str, bytes]]:
    """Return INDEXED registrations whose key_prefix covers *key*.

    A registration covers a key when ``key`` starts with ``key_prefix``
    (including empty prefix, which covers everything).

    Returns list of ``(reg_id, indexer_entity_iid, key_prefix, indexer_public_id)``.
    Must be called within an existing transaction.
    """
    cursor = conn.execute(
        """
        SELECT r.id, r.indexer_entity_id, r.key_prefix, e.public_id
        FROM ns_index_registry r
        JOIN entity e ON e.id = r.indexer_entity_id
        WHERE r.namespace_id = ? AND r.state = 'INDEXED'
          AND SUBSTR(?, 1, LENGTH(r.key_prefix)) = r.key_prefix
        """,
        (ns_id, key),
    )
    return [(row[0], row[1], row[2], bytes(row[3])) for row in cursor.fetchall()]


# =============================================================================
# Write-pool entry points (own transactions)
# =============================================================================


def register_indexer_start(
    conn: sqlite3.Connection,
    ns_id: int,
    indexer_entity_iid: int,
    key_prefix: str,
    session_id: int,
    key_types: Sequence[IndexerColumnType],
    value_type: IndexerColumnType,
) -> int:
    """Create or adopt a registration and ensure index tables exist.

    Behaviour on collision (same indexer + prefix already registered):
    - INDEXED → raise ``KeyError``
    - BUILDING with live (non-garbage) session → raise ``RuntimeError``
    - BUILDING with garbage session → take over the row
    - GARBAGE → take over the row

    Returns:
        The ``ns_index_registry.id`` of the registration.
    """
    with transaction(conn, "IMMEDIATE"):
        # Check for existing registration
        cursor = conn.execute(
            """
            SELECT r.id, r.state, r.building_session_id,
                   COALESCE(s.is_garbage, 1) AS session_is_garbage
            FROM ns_index_registry r
            LEFT JOIN session s ON r.building_session_id = s.id
            WHERE r.namespace_id = ? AND r.indexer_entity_id = ? AND r.key_prefix = ?
            """,
            (ns_id, indexer_entity_iid, key_prefix),
        )
        existing = cursor.fetchone()

        key_types_str = _encode_column_types(key_types)
        value_type_str = value_type.name

        if existing is not None:
            reg_id, state, _building_sid, session_is_garbage = existing

            if state == "INDEXED":
                raise KeyError(
                    f"Indexer {indexer_entity_iid} already registered at prefix {key_prefix!r}"
                )
            elif state == "BUILDING" and not session_is_garbage:
                raise RuntimeError(
                    f"Indexer {indexer_entity_iid} is currently being built at prefix"
                    f" {key_prefix!r} by an active session"
                )
            # BUILDING with garbage session or GARBAGE → take over
            conn.execute(
                """
                UPDATE ns_index_registry
                SET state = 'BUILDING', building_session_id = ?,
                    key_types = ?, value_type = ?
                WHERE id = ?
                """,
                (session_id, key_types_str, value_type_str, reg_id),
            )
        else:
            cursor = conn.execute(
                """
                INSERT INTO ns_index_registry
                    (namespace_id, indexer_entity_id, key_prefix, state,
                     building_session_id, key_types, value_type)
                VALUES (?, ?, ?, 'BUILDING', ?, ?, ?)
                """,
                (
                    ns_id,
                    indexer_entity_iid,
                    key_prefix,
                    session_id,
                    key_types_str,
                    value_type_str,
                ),
            )
            reg_id = cursor.lastrowid
            assert reg_id is not None

        # Ensure per-indexer tables exist (idempotent)
        _ensure_index_tables_exist(conn, indexer_entity_iid, key_types, value_type)

        return reg_id


def write_index_entries_batch(
    conn: sqlite3.Connection,
    ns_id: int,
    indexer_entity_iid: int,
    entries: dict[int, list[tuple]],
) -> None:
    """Write index rows + processed markers for a batch of entities.

    Args:
        entries: ``{entity_iid: [(key_val_0, ..., value), ...]}``
    """
    with transaction(conn, "IMMEDIATE"):
        for entity_iid, rows in entries.items():
            if rows:
                _write_index_rows(conn, ns_id, indexer_entity_iid, entity_iid, rows)
            _write_processed_marker(conn, ns_id, indexer_entity_iid, entity_iid)


def try_finish_registration(
    conn: sqlite3.Connection,
    reg_id: int,
    ns_id: int,
    indexer_entity_iid: int,
    key_prefix: str,
) -> bool:
    """Check whether all matching pins are processed; if yes flip BUILDING→INDEXED.

    Returns True if the registration was promoted to INDEXED.
    """
    with transaction(conn, "IMMEDIATE"):
        processed = _processed_table_name(indexer_entity_iid)

        # Find any unprocessed pin matching the prefix
        if key_prefix:
            escaped = _escape_like(key_prefix)
            cursor = conn.execute(
                f"""
                SELECT 1 FROM namespace_pin np
                WHERE np.namespace_id = ?
                  AND np.name LIKE ? || '%' ESCAPE '\\'
                  AND NOT EXISTS (
                      SELECT 1 FROM {processed} p
                      WHERE p.namespace_id = np.namespace_id
                        AND p.entity_internal_id = np.entity_id
                  )
                LIMIT 1
                """,
                (ns_id, escaped),
            )
        else:
            cursor = conn.execute(
                f"""
                SELECT 1 FROM namespace_pin np
                WHERE np.namespace_id = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM {processed} p
                      WHERE p.namespace_id = np.namespace_id
                        AND p.entity_internal_id = np.entity_id
                  )
                LIMIT 1
                """,
                (ns_id,),
            )
        if cursor.fetchone() is not None:
            return False  # still have unprocessed pins

        # All done — promote
        conn.execute(
            "UPDATE ns_index_registry SET state = 'INDEXED', building_session_id = NULL WHERE"
            " id = ?",
            (reg_id,),
        )
        return True


def set_registration_garbage(
    conn: sqlite3.Connection,
    ns_id: int,
    indexer_entity_iid: int,
    key_prefix: str,
) -> None:
    """Mark a registration as GARBAGE.

    Raises KeyError if no matching non-GARBAGE registration is found.
    """
    with transaction(conn, "IMMEDIATE"):
        cursor = conn.execute(
            """
            UPDATE ns_index_registry
            SET state = 'GARBAGE', building_session_id = NULL
            WHERE namespace_id = ? AND indexer_entity_id = ? AND key_prefix = ?
              AND state != 'GARBAGE'
            """,
            (ns_id, indexer_entity_iid, key_prefix),
        )
        if cursor.rowcount == 0:
            raise KeyError(
                f"No non-GARBAGE registration for indexer {indexer_entity_iid}"
                f" at prefix {key_prefix!r}"
            )


# =============================================================================
# Read-pool entry points
# =============================================================================


def get_indexed_registrations_for_prefix(
    conn: sqlite3.Connection,
    ns_id: int,
    key: str,
) -> list[tuple[int, int, str, bytes]]:
    """Return INDEXED registrations whose key_prefix covers *key*.

    Returns list of ``(reg_id, indexer_entity_iid, key_prefix, indexer_public_id)``.
    """
    with transaction(conn, "DEFERRED"):
        return _get_indexed_registrations_for_key(conn, ns_id, key)


def count_unprocessed_pins(
    conn: sqlite3.Connection,
    ns_id: int,
    indexer_entity_iid: int,
    key_prefix: str,
) -> int:
    """Count pins matching *key_prefix* that are not yet in NS_INDEX_PROCESSED__.

    Returns the count of unprocessed pins.
    """
    processed = _processed_table_name(indexer_entity_iid)

    with transaction(conn, "DEFERRED"):
        if key_prefix:
            escaped = _escape_like(key_prefix)
            cursor = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM namespace_pin np
                WHERE np.namespace_id = ?
                  AND np.name LIKE ? || '%' ESCAPE '\\'
                  AND NOT EXISTS (
                      SELECT 1 FROM {processed} p
                      WHERE p.namespace_id = np.namespace_id
                        AND p.entity_internal_id = np.entity_id
                  )
                """,
                (ns_id, escaped),
            )
        else:
            cursor = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM namespace_pin np
                WHERE np.namespace_id = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM {processed} p
                      WHERE p.namespace_id = np.namespace_id
                        AND p.entity_internal_id = np.entity_id
                  )
                """,
                (ns_id,),
            )
        return cursor.fetchone()[0]


def get_unprocessed_pins(
    conn: sqlite3.Connection,
    ns_id: int,
    indexer_entity_iid: int,
    key_prefix: str,
    limit: int,
) -> list[tuple[str, int]]:
    """Return pins matching *key_prefix* that are not yet in NS_INDEX_PROCESSED__.

    Returns list of ``(pin_name, entity_iid)``.
    """
    processed = _processed_table_name(indexer_entity_iid)

    with transaction(conn, "DEFERRED"):
        if key_prefix:
            escaped = _escape_like(key_prefix)
            cursor = conn.execute(
                f"""
                SELECT np.name, np.entity_id
                FROM namespace_pin np
                WHERE np.namespace_id = ?
                  AND np.name LIKE ? || '%' ESCAPE '\\'
                  AND NOT EXISTS (
                      SELECT 1 FROM {processed} p
                      WHERE p.namespace_id = np.namespace_id
                        AND p.entity_internal_id = np.entity_id
                  )
                LIMIT ?
                """,
                (ns_id, escaped, limit),
            )
        else:
            cursor = conn.execute(
                f"""
                SELECT np.name, np.entity_id
                FROM namespace_pin np
                WHERE np.namespace_id = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM {processed} p
                      WHERE p.namespace_id = np.namespace_id
                        AND p.entity_internal_id = np.entity_id
                  )
                LIMIT ?
                """,
                (ns_id, limit),
            )
        return [(row[0], row[1]) for row in cursor.fetchall()]


def get_indexed_registrations_for_namespace(
    conn: sqlite3.Connection,
    ns_id: int,
) -> list[tuple[int, str]]:
    """Return all INDEXED registrations for ``list_indexers``.

    Returns list of ``(indexer_entity_iid, key_prefix)``.
    """
    with transaction(conn, "DEFERRED"):
        cursor = conn.execute(
            """
            SELECT indexer_entity_id, key_prefix
            FROM ns_index_registry
            WHERE namespace_id = ? AND state = 'INDEXED'
            """,
            (ns_id,),
        )
        return [(row[0], row[1]) for row in cursor.fetchall()]


def resolve_indexer_for_query(
    conn: sqlite3.Connection,
    ns_id: int,
    indexer_entity_public_id: bytes,
    query_key_prefix: str,
) -> tuple[int, tuple[IndexerColumnType, ...], IndexerColumnType]:
    """Look up an indexer entity and return schema info for query generation.

    Verifies the indexer has at least one INDEXED registration in this namespace
    whose ``key_prefix`` covers *query_key_prefix* (i.e. the registration's
    prefix is equal to or a prefix of the query prefix).

    Returns:
        ``(indexer_entity_iid, key_types, value_type)``

    Raises:
        KeyError: If the indexer is not found or has no INDEXED registration
            covering the query prefix.
    """
    with transaction(conn, "DEFERRED"):
        # Look up entity internal ID
        cursor = conn.execute(
            "SELECT id FROM entity WHERE public_id = ?",
            (indexer_entity_public_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise KeyError(
                "Indexer entity not found:"
                f" {entity_core.FxtrID(raw_data_id=indexer_entity_public_id):q}. This usually means"
                " the requested indexer was not registered before trying to use it in a query."
            )
        indexer_entity_iid: int = row[0]

        # Find an INDEXED registration whose key_prefix covers the query prefix
        cursor = conn.execute(
            """
            SELECT key_types, value_type FROM ns_index_registry
            WHERE namespace_id = ? AND indexer_entity_id = ? AND state = 'INDEXED'
              AND SUBSTR(?, 1, LENGTH(key_prefix)) = key_prefix
            LIMIT 1
            """,
            (ns_id, indexer_entity_iid, query_key_prefix),
        )
        row = cursor.fetchone()
        if row is None:
            raise KeyError(
                "No INDEXED registration for indexer"
                f" {entity_core.FxtrID(raw_data_id=indexer_entity_public_id):q} in namespace"
                f" {ns_id} covering key prefix {query_key_prefix!r}. This usually means"
                " the requested indexer was not registered before trying to use it in a query."
            )
        key_types = _decode_column_types(row[0])
        value_type = _decode_column_type(row[1])

        return (indexer_entity_iid, key_types, value_type)


def execute_index_query(
    conn: sqlite3.Connection,
    sql: str,
    params: list[int | str],
) -> list[tuple]:
    """Execute a generated index query and return all result rows."""
    with transaction(conn, "DEFERRED"):
        cursor = conn.execute(sql, params)
        return cursor.fetchall()


# =============================================================================
# GC / session-death helpers
# =============================================================================


def _mark_building_registrations_garbage_for_session(
    conn: sqlite3.Connection,
    session_id: int,
) -> None:
    """UPDATE all BUILDING registrations owned by *session_id* to GARBAGE.

    Must be called within an existing transaction (from the session-death path
    in ``sqlite_ops.mark_session_garbage``).
    """
    conn.execute(
        """
        UPDATE ns_index_registry
        SET state = 'GARBAGE', building_session_id = NULL
        WHERE building_session_id = ? AND state = 'BUILDING'
        """,
        (session_id,),
    )


def _gc_cleanup_one_garbage_registration(
    conn: sqlite3.Connection,
) -> tuple[bool, int | None, int | None]:
    """Find and clean up one GARBAGE index registration.

    Deletes index rows, the registration entry, and drops tables if no longer
    used.  Does NOT perform orphan checks on the indexer entity — the caller
    must do that using the returned ``indexer_entity_iid``.

    Must be called within an existing transaction.

    Returns:
        ``(work_done, indexer_entity_iid, namespace_id)``
        If *work_done* is True the caller should check whether
        *indexer_entity_iid* is now orphaned in *namespace_id*.
    """
    # Find a GARBAGE registration
    cursor = conn.execute(
        """
        SELECT id, namespace_id, indexer_entity_id, key_prefix
        FROM ns_index_registry
        WHERE state = 'GARBAGE'
        LIMIT 1
        """
    )
    row = cursor.fetchone()
    if row is None:
        return (False, None, None)

    reg_id, ns_id, indexer_entity_iid, key_prefix = row
    table = _index_table_name(indexer_entity_iid)
    processed = _processed_table_name(indexer_entity_iid)

    # Check whether any *live* registration (INDEXED or BUILDING) exists
    # for the same indexer in the same namespace.  If so, only delete rows
    # for entities whose pin key does NOT match any live prefix.
    cursor = conn.execute(
        """
        SELECT key_prefix FROM ns_index_registry
        WHERE namespace_id = ? AND indexer_entity_id = ? AND state != 'GARBAGE'
        """,
        (ns_id, indexer_entity_iid),
    )
    live_prefixes = [r[0] for r in cursor.fetchall()]

    if not live_prefixes:
        # Simple case: no live registrations — wipe everything for this namespace
        conn.execute(
            f"DELETE FROM {table} WHERE namespace_id = ?",
            (ns_id,),
        )
        conn.execute(
            f"DELETE FROM {processed} WHERE namespace_id = ?",
            (ns_id,),
        )
    else:
        # Complex case: only delete rows for entities not covered by any live prefix.
        # An entity is "covered" if its pin key starts with any live prefix.
        # Build a condition that identifies covered entities.
        live_conditions = []
        live_params: list[int | str] = [ns_id]
        for lp in live_prefixes:
            if lp == "":
                # Empty prefix covers everything — nothing to delete
                live_conditions = None
                break
            escaped = _escape_like(lp)
            live_conditions.append("np.name LIKE ? || '%' ESCAPE '\\\\'")
            live_params.append(escaped)

        if live_conditions is None:
            # A live empty-prefix registration covers all entities — nothing to delete
            pass
        else:
            covered_clause = " OR ".join(live_conditions)
            # Delete entries for entities not covered by any live prefix
            conn.execute(
                f"""
                DELETE FROM {table}
                WHERE namespace_id = ? AND entity_internal_id IN (
                    SELECT p.entity_internal_id FROM {processed} p
                    WHERE p.namespace_id = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM namespace_pin np
                          WHERE np.namespace_id = p.namespace_id
                            AND np.entity_id = p.entity_internal_id
                            AND ({covered_clause})
                      )
                )
                """,
                (ns_id, ns_id, *live_params[1:]),
            )
            conn.execute(
                f"""
                DELETE FROM {processed}
                WHERE namespace_id = ? AND entity_internal_id IN (
                    SELECT p2.entity_internal_id FROM (
                        SELECT entity_internal_id FROM {processed}
                        WHERE namespace_id = ?
                    ) p2
                    WHERE NOT EXISTS (
                        SELECT 1 FROM namespace_pin np
                        WHERE np.namespace_id = ?
                          AND np.entity_id = p2.entity_internal_id
                          AND ({covered_clause})
                    )
                )
                """,
                (ns_id, ns_id, ns_id, *live_params[1:]),
            )

    # Delete the GARBAGE registration entry
    conn.execute("DELETE FROM ns_index_registry WHERE id = ?", (reg_id,))

    # Drop tables if this indexer has no registrations left (across all namespaces)
    _drop_index_tables_if_unused(conn, indexer_entity_iid)

    return (True, indexer_entity_iid, ns_id)
