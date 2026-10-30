# Scratch: Index Computer Speedup

## Current bottleneck analysis

When pinning an entity into an indexed namespace, `_pin_entity_from_arena_impl` does:

1. Copy entity into SQLite (fast — one-time bulk copy)
2. Read indexed registrations (fast — single SQL read)
3. **Compute index entries** (slow):
   - Create a `SQLiteNamespaceArena` for entity + all indexers (SQLite reads for precomp data)
   - `IndexComputer.compute_indexer_entries`:
     - Create an isolated `FxtrScope`
     - `unsafe_lazily_attach_from_arena` every entity + indexer
     - Load every indexer as `WellKnownBuiltinIndexer` (deserialize each time)
     - For each (indexer, entity) pair: create a child scope, lift entity, `load_cbor`, `unpack_entity`
     - Tear down all scopes
   - Convert results to flat SQL rows
4. Atomic write pin + index rows (fast — single transaction)

**Per-pin cost for 20 indexers:**
- 1 SQLiteNamespaceArena creation (precomp reads for 21 entities)
- 1 isolated FxtrScope created and destroyed
- 20 indexer entities deserialized
- 20 child scopes created/destroyed
- 20 `load_cbor` calls (each goes through UnknownEntity load handler)
- All of this repeated for every single pin

## Where time is wasted

1. **Indexer re-loading**: The same 20 indexer entities are deserialized from scratch on every pin.
   The scope that holds them is destroyed after each pin.

2. **SQLiteNamespaceArena creation**: Precomp data for indexer entities is re-read from SQLite
   on every pin, even though it never changes.

3. **Mutable CBOR loading**: `load_cbor` uses `UnknownMutableEntity`, creating new mutable copies each time.
   With frozen CBOR (`load_frozen_cbor`), the scope's cache would return the same frozen object
   across loads of the same entity, avoiding repeated deserialization.

4. **Scope overhead**: Creating/destroying an isolated scope + 20 child scopes per pin has
   non-trivial overhead (MemoryStore allocation, context var manipulation, GC).

## What the plan addresses

- **Persistent scope for indexers** (#1, #4): IndexComputer keeps a long-lived scope with loaded
  indexers. Only new indexers need to be loaded.

- **Skip SQLiteNamespaceArena for indexers** (#2): If all indexers are cached, we don't need to
  create an arena containing them at all. Only the entity arena is needed.

- **Frozen CBOR for entity data** (#3): Using `load_frozen_cbor` instead of `load_cbor` in the
  indexer path means cached frozen dicts are reused. Also means `_walk_path` works on immutable
  structures (frozendict + tuple instead of dict + list).

## Type compatibility for frozen CBOR in _walk_path

`FrozenFxtrCbor` uses `frozendict` (not a `dict` subclass!) and `tuple` (not a `list` subclass).

Current `_walk_path` uses:
- `isinstance(data, dict)` → needs `isinstance(data, frozendict)`
- `isinstance(data, list)` → needs `isinstance(data, tuple)`
- `data.get(elem.key, _SENTINEL)` → frozendict supports `.get()` ✓
- `data[elem.index]` → tuple supports indexing ✓
- `for key in data` → frozendict supports iteration ✓
- `for i, item in enumerate(data)` → tuple supports enumerate ✓

So just the isinstance checks need updating.

`_check_initial_lookup` similarly uses `isinstance(entity_data, dict)` and `.get()`.

## Interaction with register_indexer

`register_indexer` also calls `compute_indexer_entries` in a batched loop. The persistent
scope means indexers loaded during registration will be cached for subsequent pin operations.
The batched loop itself benefits from cached indexers across batches.

## IndexComputer lifecycle

Currently `IndexComputer()` is created in `SQLiteNamespaceBackend.__init__` (no async lifecycle).
Since FxtrScope requires async context management, we need to:
- Make IndexComputer an async context manager
- Tie its lifetime to SQLiteFxtrSession (which has start/aclose lifecycle)
- The persistent scope opens on __aenter__ and closes on __aexit__

But wait — `SQLiteFxtrSession` doesn't use async context manager pattern for its core lifecycle.
It uses `start()` / `aclose()`. And `SQLiteNamespaceBackend` is created dynamically via
`get_or_create_namespace()`. Multiple namespaces share the same session.

Options:
1. Make IndexComputer a session-level resource (one per session, shared by namespaces)
2. Keep IndexComputer per-namespace but tie its scope to the namespace's registration in the session

Option 1 seems better — indexers are namespace-scoped but the FxtrScope mechanics are namespace-agnostic.
The IndexComputer just needs to know which indexers it has loaded. Different namespaces might have
different indexers, but the persistent scope can hold indexers from any namespace.

Actually, since the IndexComputer's scope just holds loaded indexer Python objects (which are the
deserialized WellKnownBuiltinIndexer instances), and those are keyed by FxtrID, there's no conflict.
Two namespaces using the same indexer FxtrID will share the cached load.

Plan: Add `_index_computer_scope` to `SQLiteFxtrSession`. Open it during `start()`, close it during
`aclose()`. Pass it to IndexComputer. Actually simpler: make IndexComputer own its scope and have
start/close methods called from session lifecycle.
