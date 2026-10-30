# Stage 1: Namespace Indexing - Scratch Notes

Working notes from design discussion. Not a spec - see the overview doc for the clean version.

## Q&A from design discussion

**Q: `IndexerExpression` is imported in `indexers.py` from `indexing_types` but not defined there. What is it?**
A: Typo. Should be `IndexerConstraint`.

**Q: `unpack_entity` is sync but DerefMarker requires async entity loading. Should it be async?**
A: Yes, `unpack_entity` should be async.

**Q: Where does the concrete IndexablePinningBackend implementation live?**
A: `IndexablePinningBackend` replaces `PersistentPinningBackend` as the base class of
`SQLiteNamespaceBackend`. All SQLiteNamespaceBackend instances become queryable. The
`PersistentPinningBackend` base class remains for non-queryable backends.

**Q: How are indexer entities owned by the namespace without being pinned?**
A: The index registration table itself acts as a root. GC logic needs to treat indexer entities
the same as pinned entities - if an entity is referenced as an indexer in any non-GARBAGE registration,
it's a namespace root.

**Q: Serialization of path marker types in TypedPathValueIndexer?**
A: Need a custom serialization strategy for the union type `str | int | VarMarker | DerefMarker |
TypedDerefMarker` in the `path` field. Implement a custom codec that wraps each element with a
type discriminator tag.

**Q: `datetime` column type?**
A: Dropped. Not a native CBOR or SQLite type. Use string encoding if needed later.

**Q: Join semantics for shared binding variables?**
A: Correct - when two IndexerConstraints share a variable name (e.g., both reference `$t`), the
generated SQL JOINs on those columns being equal. This is the "variable unification" from the
datalog-style design. The BindingConstraint adds a global WHERE clause on top.

**Q: Index building timing?**
A: `register_indexer` builds the index synchronously (within the async call). Loop:
1. Fetch all pins matching prefix that aren't in NS_INDEX_PROCESSED__
2. For each pin, run indexer, write results to NS_INDEX__ and NS_INDEX_PROCESSED__
3. Check if all pins are indexed; if yes, flip to INDEXED; if no, loop again
The registration table tracks the building session so GC can mark BUILDING indexes as GARBAGE
if the session dies mid-build.

**Q: Pin-time indexing transaction scope?**
A: Must be in the same transaction. Compare-and-swap pattern:
1. Precompute index results for all INDEXED indexers on the Python side
2. In the transaction: insert pin + index rows + verify no new INDEXED indexers appeared
3. If verification fails: don't commit, report missing indexers, recompute, retry
4. Only commit when all invariants hold

**Q: sqlglot dependency?**
A: Not currently a dependency. Need to add it via `uv add sqlglot`.

**Q: FxtrQueryableNamespace API surface?**
A: Exposes `register_indexer`, `unregister_indexer`, `list_indexers`, AND `iter_matches`.

**Q: Backend API for register_indexer?**
A: Backend takes FxtrID + source arena (not Indexer object directly). The higher-level
FxtrQueryableNamespace stores the indexer entity and passes the FxtrID down.

## Key design insights

### IndexComputer helper
When running an indexer we need careful scope management:
- Create an isolated scope per indexer, attach the indexer entity + its children
- Create fresh isolated scopes for each value being indexed
- This can be encapsulated in an IndexComputer helper that exposes a clean arena-based API
  to the low-level store code

### Compare-and-swap pinning
Both `register_indexer` and `pin_entity_from_arena` use a compare-and-swap pattern:
- Set up state on the Python side
- Try to commit in a transaction, checking that invariants still hold
- If invariants don't hold, add missing items and retry
- The transaction only commits when everything is consistent

This avoids holding long-running transactions while doing potentially expensive indexer work.

### GC for indexes
- When unregistering, set state to GARBAGE (don't delete anything yet)
- In GC loop: check for GARBAGE registrations, delete rows for entities that don't match
  any live (INDEXED/BUILDING) prefix in the namespace, then delete the GARBAGE registration
- If a building session dies: GC marks its BUILDING indexes as GARBAGE
- Indexer entities are treated as namespace roots (like pins) - they're kept alive as long
  as they're referenced by any non-GARBAGE registration

### Backend API vs User-Facing API
- Backend (`IndexablePinningBackend`): takes FxtrID + ReadableArena for indexers
- User-facing (`FxtrQueryableNamespace`): takes Indexer objects, stores them, passes FxtrID down
- `IndexerConstraint` still holds `Indexer` objects (needed for schema info in query generation)
- The backend can resolve an Indexer to its registration via content-addressed ID

### Per-indexer SQL tables
Each indexer gets its own dynamically-created table (`NS_INDEX__{INDEXER_INTERNAL_ID}`) with
columns matching the indexer's key types. This is better than a generic EAV table because:
- SQLite can optimize queries with proper column types and indices
- The primary key structure matches the indexer's key structure
- The VALUE column gets its own index for efficient value-based filtering

### SQL expression safety
User-provided SQL condition strings are parsed by sqlglot:
- `$varname` is rewritten to `_VAR_varname`
- Column references are validated against the set of in-scope variables
- This prevents SQL injection while allowing expressive conditions

## Implementation order
1. Self-contained helpers (can be parallelized):
   - SQL expression parsing (sqlglot)
   - Indexer path expression parsing
   - Indexer serialization
   - Indexer unpack logic
   - SQL query generation
2. Core integration:
   - SQL table management
   - IndexComputer helper
   - register_indexer
   - Modified pin_entity_from_arena
   - Modified unpin_entity + GC changes
   - iter_matches
3. User-facing:
   - FxtrQueryableNamespace
   - SQLiteFxtrNamespace updates
   - CODE_OVERVIEW.md updates

## Things to watch out for
- The path union type serialization needs to be tested carefully for roundtrip correctness
- sqlglot may have edge cases with SQLite dialect that need testing
- The compare-and-swap pattern needs careful error handling for partial failures
- Index table creation is dynamic (per-indexer) which means we need to handle DDL carefully
- When multiple indexers share the same entity type, unpacking happens once per indexer
  (could be optimized later but not needed now)
- DerefMarker in paths means indexing might need to load child entities, which requires
  them to be in the namespace already (or at least in the store)
