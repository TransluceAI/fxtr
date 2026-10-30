# Scratch Notes: Git Namespace Indexing

## Current Architecture

### GitNamespaceBackend
- Implements PersistentPinningBackend + ObservableStore (NOT IndexablePinningBackend)
- Has two internal SQLite namespaces: `_index_ns` (backing store) and `_worktree_ns` (current state)
- `_worktree_ns` is a `SQLiteNamespaceBackend` which already implements `IndexablePinningBackend`
- TSV files are the git-tracked source of truth for pins
- Backing SQLite (in `.fxtr/`) is gitignored

### Sync Process
- `sync_tsv_to_sqlite`: calls `worktree_ns.pin_entity_from_arena` and `worktree_ns.unpin_entity`
- These already have CAS-loop indexing built in
- So if indexers are registered on worktree_ns, pin sync automatically maintains indexes

### Export/Import
- `export_archive` creates a temp SQLite with an "archive" namespace
- All pins are copied to the archive namespace with same keys
- Can be tarball or raw files
- Import reads the archive namespace and pins entities into the backing (index) namespace

### Gitignore Patterns (store root)
- `fxtr_store.local.toml` (local config)
- `*.sync_lock` (sync locks)
- `*.fxtr.tsv.dirty/*` with exception for sentinel
- NOT gitignored: `*.fxtr.tsv` files, `fxtr_store.toml`
- So `fxtr_store_indexes.tsv` would NOT be gitignored by default - good

## Key Design Decisions

### Index File Format
- `fxtr_store_indexes.tsv` at store root
- Format: `{indexer_fxtr_id}\t{json_encoded_key_prefix}` per line
- Sorted lexicographically by full line
- JSON encoding: empty prefix → `""`, non-empty → `"data/models:"`
- Not a unique-key TSV (same indexer can appear with multiple prefixes)

### File Locking
- Use `AsyncFileLock.acquire(index_file_path)` for mutual exclusion
- Lock during register/unregister operations (including building loop)
- Register is infrequent, blocking is acceptable

### Sync Timing
- On open: during initial full sync
- On access: list_indexers, iter_matches, register_indexer, unregister_indexer
- Simple approach: re-read file, compare to last known state, apply diffs if different

### Sync Order During Full Sync
1. Remove dead indexers (not in file but in worktree_ns) - cheap, just GARBAGE marks
2. Sync pin TSVs (surviving indexers participate in CAS loop, dead ones don't)
3. Add new indexers (building loop runs on final pin state)

### Export Metadata
- Export creates a "metadata" namespace in the archive
- Pin key "indexers" maps to a metadata entity
- Metadata entity has strong refs to all indexer entities + records key prefixes
- Archive doesn't apply indexers (no index tables), just stores the config

### Import
- Read metadata namespace from archive
- Load indexer entities into backing (index) namespace
- Registration file is NOT updated during import (that's a git-tracked file)
- The indexer entities are available for when the registration file triggers registration

Wait - actually, the user said "then at import time for the git store we also can load those indexers
into the backing store." So import loads the entity data, and the git-tracked file controls
what's actually registered. The file might already list these indexers (if it was committed),
and the next sync will register them (which requires the entity data to be in the backing store).

### Edge Cases
- Fresh clone without import: backing store doesn't have indexer entities
  - Sync sees registrations in file but can't find entities → error (like FxtrSyncError for pins)
  - User needs to import archive first
- Two worktrees with different indexers: each has own fxtr_store_indexes.tsv, own worktree_ns
  - Backing SQLite is shared, but registrations are per namespace_id
  - Works fine

## Entity Type for Export Metadata

Options considered:
1. Custom entity with ChildFxtrID + parallel lists → works but ugly
2. Custom entity with Child[IndexRegistration] entries → clean but many entities
3. Pin indexers directly in metadata namespace → simpler, no custom type needed

Going with option 3: pin each indexer in the metadata namespace. The key encodes the
registration info. Key format: `{indexer_id_string}\t{json_prefix}` (or similar).
Wait, pin keys have restrictions... let me check.

Actually, the metadata namespace is a plain SQLiteNamespaceBackend, not a git store.
Its pin keys are just strings with no colon/path restrictions. We could use any format.

Simplest: pin each indexer under its id_string. Then have a separate metadata entity
that lists the (indexer_id_string, key_prefix) pairs. The pins keep the entity data alive
in the archive, and the metadata entity records the configuration.

Or even simpler: use a convention like `indexer:{id_string}:{json_prefix}` as the pin key.
Then the pin list itself IS the registration list. No extra entity type needed.

But wait - pinning the same entity under multiple keys (same indexer, different prefixes)
is fine. And the id_string in the key makes it unambiguous.

Let me go with this approach: in the metadata namespace, pin each indexer entity under
key `idx/{indexer_id_string}/{json_prefix}`. The pin list encodes the full registration state.

Actually even simpler - we could use the same format as the TSV file for pin keys:
`{indexer_id_string}\t{json_prefix}`. But tabs in pin keys... that might cause issues.

Let me just use `/` as separator: `{indexer_id_string}/{json_prefix}`.

On import, we parse these keys to extract the registration info and load the entities.

Hmm, but the json prefix might contain `/` (though in practice prefixes like "data/models:"
do contain slashes). So the format needs to be unambiguous.

Better: use a fixed separator that won't appear in either part. FxtrID strings don't contain
tabs. JSON strings don't contain raw tabs. So tab works: `{id_string}\t{json_prefix}`.

But the `validate_tsv_key` and pin key validation might reject tabs. For the metadata
namespace (plain SQLiteNamespaceBackend), pin keys are just strings - there's no TSV
validation. The only restriction is from SQLiteNamespaceBackend.pin_entity_from_arena
which doesn't validate key format at all.

OK, let me use tab: pin key = `{id_string}\t{json_prefix}`.

Actually, this is getting complicated. Let me just define a simple entity type. It's one
dataclass, and it's the cleanest approach:

```python
@dataclass(frozen=True)
class NamespaceIndexMetadata(DataclassEntity, fxtr_type=...):
    # Each entry: (indexer_entity_id_as_strong_link, key_prefix_string)
    indexer_ids: tuple[ChildFxtrID, ...]
    key_prefixes: tuple[str, ...]
```

The ChildFxtrID creates strong links to the indexer entities, keeping them alive in the
archive. The parallel tuples are simple to serialize/deserialize. On import, we iterate
through the pairs to extract the registration info.

One entity, one pin ("indexers"), done.

Or use a frozendict mapping indexer id to tuple of prefixes... but an indexer can have
multiple prefixes and the same prefix can have multiple indexers, so it's really a list
of pairs.

I'll go with parallel tuples for simplicity. In the plan I'll note the approach without
getting bogged down in the exact field types.
