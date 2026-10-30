# Bulk Copy Scratch Notes

Key implementation notes gathered during design discussion:

1. Entity table uses `INTEGER PRIMARY KEY AUTOINCREMENT` — IDs are auto-incremented and
   consecutive within a transaction.

2. `build_closed_precomp_map` (lmdb_blob.py:163) opens its own read-only LMDB txn at line 193.
   The new `build_closed_precomp_map_cached` takes an existing txn + cache dict instead.

3. `_add_entity_to_namespace_if_needed` (sqlite_ops.py:1227) validates that all deps are
   already in namespace before adding backrefs. The CTE approach handles this by adding ALL
   ownership rows first, then ALL backref rows, so ordering is automatically correct.

4. LMDB write txns can read their own uncommitted writes. This is crucial for the precomp
   building during save queue processing — deps written earlier in the same txn are readable.

5. Entity-table GC never runs while sessions are open (stronger invariant than originally
   designed), so reservations are unnecessary.

6. The `namespace_entity_backref` table has UNIQUE(requirer_id, required_id, namespace_id, kind),
   so INSERT OR IGNORE handles any edge cases with duplicates.

7. The connection pool group has separate `read_pool` (max_connections=5) and `write_pool`
   (max_connections=1). The discover sweep should use `read_pool`.

8. In the LMDB Python library, `Environment.begin(write=True)` context manager commits on
   `__exit__` without exception, aborts on exception. Nesting sqlite txn (outer) with LMDB
   txn (inner) gives LMDB-first commit order naturally.
