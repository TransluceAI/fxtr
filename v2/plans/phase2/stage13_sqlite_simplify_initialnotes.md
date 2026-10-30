# Sqlite Simplification

Goal: Rewrite the full sqlite_store implementation with different logic, preserving the overall structure but making key changes.

## Why
Sqlite store remains the bottleneck even after dropping to sqlite3 and adding indices. Remaining sources of slowness:
- High overhead of scheduling large numbers of small tasks into anyio worker threads
- Significant time spent in "abandon" operations abandoning small items
- Significant time spent adding and removing values from arenas

## High level changes
- Stop tracking Python-level arenas in the DB. The only purpose of having arenas in the DB is to prevent data from being cleaned up if needed by someone else. However, currently GC only removes namespace metadata and doesn't remove data anyways. We can just enforce that entity data is never removed unless there are no active sessions. Then data can be loaded directly whenever a session is active, no need to track arena state in DB whatsoever.
    - However, we can still keep the arena membership table in the DB. SQLiteNamespaceBackend can still use the namespaces to manage its state, but these are no longer exposed to public API consumers.
- Change SQLiteNamespaceArena to be a ReadableArena and not have a copy method. Copying only happens in pin_entity_from_arena.
- Change SQLiteNamespaceArena to not be associated with a DB arena, and not do _any_ database write operations and not have any task scheduling queue. Instead, it should simply trust that the data tracked in it must be available in the database, and dispatch load operations to the read arenas. Cloning, abandoning, etc should simply modify the Python state.
- Still use DB arenas internally for invariant maintenance:
    - DB arenas for copying data into the store
    - DB arenas for holding garbage entities that are unpinned

## Rough implementation steps:
- Public API changes:
    - remove SQLiteNamespaceBackend.new_empty_arena
    - make SQLiteNamespaceArena a ReadableArena, not CopyTargetArena, and remove copy_from_arena
- make sure tests pass with API changes
- Internal changes: decouple python-side arenas from internal DB arena objects, simplify python-facing arena state, switch to read-only loads
- make sure tests pass

## (Possible future work?)
- Batch copying
- Streamline thread comms via persistent writer thread with batched updates
    - Single persistent writer thread which we start using anyio but then communicate with using normal threading queues
    - Writer thread prioritizes work:
        - Highest priority: Update the claim set "upward" to include more nodes when copying them into the DB
            - Variant 1: Data is not already in the db; we need to copy a node in and then update state and add a claim.
            - Variant 2: Data is already in the db: we need to add some partial subtree to the namespace  (rooted at the target node, possibly empty), then add a claim on the root
        - Lower priority: GC
    - (possibly low priority because writer thread does not need to do as much)
