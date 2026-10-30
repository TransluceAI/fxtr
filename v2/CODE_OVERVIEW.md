# Fxtr2 Code Overview

This document describes the current state of the fxtr2 codebase, focusing on the data layer implementation.

## What is Fxtr?

Fxtr is a system for tracking provenance of data and performing reproducible experiments. Key features:

- **Content-addressed storage**: Data is stored as immutable objects identified by content-based hashes
- **Provenance tracking**: The system can track where data came from and how it was derived
- **Composable IDs**: Node IDs encode both the data and optionally its provenance, allowing automatic metadata propagation

The eventual goal is to add task orchestration on top (inspired by build systems like Bazel), using content-addressed entities to cache task results. Currently the focus is on the data format and persistence layer.

## Terminology

The following terms are used somewhat interchangeably in the codebase:
- **Entity** / **Node**: A piece of data in the fxtr graph
- **ID** / **Node ID** / **FxtrID**: A unique identifier for an entity
- **Store**: The backing storage that holds actual entity data
- **Arena**: A "lease" on a set of nodes - a promise that certain data will remain accessible

## Core Concepts

### Scopes

A **Scope** (`FxtrScope`) is a higher-level abstraction that governs the lifetime of retrievable IDs. Scopes provide guarantees that:
- Anything saved into a scope is retrievable from that scope later
- Any children of something retrieved from the scope are also retrievable
- If scope B is a child of scope A, anything in scope A is also accessible in scope B
- After a scope ends, it cannot be used to retrieve entities
- Child scopes keep parent scopes alive

Scopes sit on top of the lower-level arena concept and make it easier to manage entity lifetimes without explicitly managing arena state. The active scope is accessed via `fxtr_scope.ACTIVE_SCOPE` context variable.

Key scope operations:
- `store()` / `store_cbor()`: Store entities into the scope
- `load()` / `load_cbor()`: Load entities from the scope
- `lift()`: Ensure a node from a parent scope is tracked in this scope
- `copy_from_scope()`: Copy entities from another scope into this one
- `abandon()`: Release an entity from the scope

### Ground vs Provenance Nodes

Nodes in fxtr fall into two categories:

- **Ground nodes**: Nodes that represent "pure data" without provenance attached. They cannot be unwrapped. Types: `d` (data), `.` (here), `!` (primitive/builtin)
- **Provenance nodes**: Nodes that wrap data with provenance information. They can be "unwrapped" to get the underlying data node. Types: `n` (normal), `c` (context), `p` (path), `u` (untracked)

### Unwrapping

"Unwrapping" a provenance node strips away its provenance metadata and returns the underlying data node ID. This is conceptually like "stripping metadata to get the pure data."

If you have a provenance node and unwrap it, you get a different node (sometimes referred to as a "data node"). The provenance node and its unwrapped data node will compare differently (different IDs). Conceptually, they represent the same application-level data, but the provenance node represents the view from a higher, meta-application perspective (the view that this data was produced inside this application), whereas the data node represents the view from within the application. It is possible for two different provenance nodes to unwrap to the same data node; this might happen if the same data was produced by two different invocations of the same algorithm.

### Strong vs Weak Links

Entities can reference other entities via:
- **Strong links** (CBOR tag 33002): Ownership/containment relationships. Usually implies uniqueness - each piece of data typically has one strong link pointing to it.
- **Weak links** (CBOR tag 33001): Reference relationships. An entity can have many weak links pointing to it.

This distinction is mostly for frontend/visualization purposes. Both link types keep the referenced entity alive for storage purposes.

### Content Addressing

Entities are identified by content-based hashes:
- For `d` and `n` nodes: SHA-256 hash of the canonical CBOR serialization
- For derived nodes (`c`, `p`, `u`): ID is computed from the referenced nodes

This means identical data always gets the same ID (within the same node type).

## Node Types

| Tag | Name | Contentful | Ground/Prov | Description |
|-----|------|------------|-------------|-------------|
| `.` | here | No | Ground | Anchor node, empty payload. Used as default `$fxtr.ctx` |
| `!` | primitive | No | Ground | Builtin type names (e.g., `!ctx`, `!pathroot`) |
| `d` | data | Yes | Ground | Content-addressed data node. SHA-256 of CBOR content |
| `n` | normal | Yes | Provenance | Content-addressed provenance node. Unwraps by replacing all child IDs with their unwrapped versions |
| `c` | context | No | Provenance | Points to a d-node with a "value" field. Used to mark execution context |
| `p` | path | Depends | Provenance | Path into a subgraph. Payload is `[hash, path_indices...]`. Used for hermetic caching |
| `u` | untracked | Depends | Provenance | Wraps another ID without additional provenance tracking |
| `l` | lift | - | - | *Not implemented yet* - planned for simplifying p-nodes |

## CBOR Encoding

Entities are serialized using canonical CBOR (RFC 8949) with custom tags:
- **Tag 33000**: Entity marker (`$fxtr.ctx` field) - exactly one per entity
- **Tag 33001**: Weak reference to another entity
- **Tag 33002**: Strong reference to another entity

Every entity is a CBOR dictionary with:
- `$fxtr.ctx`: The context marker (determines if this is a ground or provenance node)
- `$fxtr.type` (optional): Type identifier for the entity

## File Structure

```
fxtr2/
├── __init__.py
├── cli/                     # Command-line interfaces
│   ├── __init__.py
│   └── git_store_cli.py     # fxtr-git-store CLI
├── data/                    # Core data layer
│   ├── entity_core.py       # FxtrID, CBOR encoding, node type definitions
│   ├── abstract_arena.py    # Abstract interfaces for arenas and stores
│   ├── arena_util.py        # Visitor functions for traversing entity data
│   ├── entity_serdes.py     # High-level serialization with mashumaro
│   ├── fxtr_scope.py        # FxtrScope for managing entity lifetimes
│   ├── fxtr_context_api.py  # User-facing API (namespaces)
│   ├── memory_arena.py      # In-memory arena implementation
│   ├── system_entities.py   # Built-in entity types (ContextEntity, PathRootEntity, FxtrBasicType)
│   ├── file_util/           # File utilities shared across modules
│   │   ├── __init__.py
│   │   └── lock.py          # Async and sync file locking with flock
│   ├── git_store/           # Git-backed namespace
│   │   ├── __init__.py      # Re-exports key classes
│   │   ├── git_namespace.py # Main GitNamespaceBackend class
│   │   ├── tsv_file.py      # TSV file reading/writing
│   │   ├── key_utils.py     # Pin key parsing and validation
│   │   ├── config.py        # Store configuration and init
│   │   ├── dirty.py         # Dirty mark management
│   │   ├── sync.py          # TSV ↔ SQLite sync
│   │   └── LOCAL_CODE_OVERVIEW.md  # Detailed module documentation
│   ├── sqlite_store/        # Fast sqlite3-based SQLite store
│   │   ├── __init__.py      # Re-exports public classes
│   │   ├── sqlite_util.py   # Connection pooling with capacity limiting
│   │   ├── sqlite_ops.py    # SQL strings and sync database operations
│   │   ├── sqlite_namespace.py  # Public API classes (SQLiteFxtrSession, etc.)
│   │   └── LOCAL_CODE_OVERVIEW.md  # Detailed module documentation
│   └── deprecated/          # Deprecated modules (kept for reference)
│       ├── __init__.py
│       ├── sqlite_arena.py  # Old SQLAlchemy-based implementation (7-10x slower)
│       └── sqlite_arena_migrations.py  # Schema migrations (still used by sqlite_store)
└── prototyping/             # Experimental prototypes (not part of main system)
    ├── gql_spike/           # GraphQL prototype
    └── panel_spike/         # Panel viewer prototype
```

## Module Details

### entity_core.py

The foundation of the system. Defines:

- **`FxtrID`**: Immutable identifier class with:
  - `_raw_data_id`: The actual ID bytes (tag byte + payload)
  - `_provenance_parent`: Optional provenance-level ID (for automatic provenance tracking)
  - Methods for parsing/formatting ID strings, splitting tag/payload

- **CBOR helpers**: `RawWeakFxtrID`, `RawStrongFxtrID`, `RawFxtrEntityMarkerID` wrapper classes
- **Encoding/decoding**: `fxtr_cbor_dumps()`, `fxtr_cbor_loads()`, `fxtr_cbor_dumps_with_marker_check()`

### abstract_arena.py

Defines the abstract interfaces:

- **`ReadableArena`**: Can load entities, check tracking, unwrap provenance nodes
- **`CopyTargetArena`**: Can copy entities from another arena
- **`ReadWriteArena`**: Can also store new entities synchronously
- **`PersistentPinningBackend`**: Persistent storage with named pins and listing
- **`StreamSink`**: Experimental streaming output interface
- **`ObservableStore`**: Store that supports querying state and backrefs

### arena_util.py

Utility functions for traversing entity data:

- `visit_ids()`: Call a function for each ID in an entity
- `visit_and_project_ids()`: Transform all IDs in an entity
- `visit_and_project_ids_in_canonical_cbor_order()`: Same but in CBOR serialization order (important for p-nodes)
- `async_visit_and_project_ids_in_canonical_cbor_order()`: Async version
- `DepKind`: Enum for dependency relationship types (STRONG_REF, WEAK_REF, CTX_NODE, BACKSTEP, UNWRAPS_TO)
- `fetch_data_and_deps_from_arena()`: Fetch entity data and dependencies from an arena

### entity_serdes.py

High-level serialization using [mashumaro](https://github.com/Fatal1ty/mashumaro):

- **`Entity`**: Abstract base class for serializable entities
  - `_to_fxtr_cbor()`: Serialize to CBOR. May store child entities via `ACTIVE_SCOPE`.
  - `_from_fxtr_cbor()`: Deserialize from CBOR. May return `Self | Awaitable[Self]` for async loading.
- **`DataclassEntity`**: Convenient base for dataclass-based entities with automatic type discrimination
- **`EntityRef[T]`**: A reference to an entity that can be loaded lazily
  - `load(scope)`: Load the entity (optionally from a specific scope)
  - `copy_to(scope)`: Copy the entity to another scope
  - `copy_from(scope)`: Copy the entity from another scope
  - `lift(scope)`: Lift the entity to a given scope

**Type aliases for entity references:**
| Type | Link Strength | Loading | Description |
|------|---------------|---------|-------------|
| `EntityRef[T]` | weak | lazy (`.load()`) | Reference to entity |
| `ChildRef[T]` | strong | lazy (`.load()`) | Strong-linked reference |
| `LinkedEntity[T]` | weak | auto-loaded | Inline entity, auto-loaded during deserialization |
| `Child[T]` | strong | auto-loaded | Strong inline entity, auto-loaded during deserialization |

**Type aliases for raw FxtrIDs:**
| Type | Link Strength | Description |
|------|---------------|-------------|
| `FxtrID` | weak | Default raw ID |
| `ChildFxtrID` | strong | Strong-linked raw ID |

**Collection types with canonical serialization:**

The following collection types are supported with automatic serialization:

| Type | Serializes To | Notes |
|------|---------------|-------|
| `set[T]` | sorted list | Canonically ordered for deterministic content-addressing |
| `frozenset[T]` | sorted list | Canonically ordered for deterministic content-addressing |
| `frozendict[K, V]` | dict | Type preserved through roundtrip |
| `dict[K, V]` | dict | Native mashumaro support |

These collections can contain `EntityRef[T]` or `FxtrID` as values or keys:
- `set[EntityRef[T]]`, `frozenset[FxtrID]` - EntityRef/FxtrID as set elements
- `dict[EntityRef[T], V]`, `frozendict[FxtrID, V]` - EntityRef/FxtrID as dict keys
- Nested collections like `frozendict[str, frozenset[int]]` work correctly

**Canonical ordering**: Sets and frozensets are serialized as sorted lists to ensure deterministic byte output regardless of insertion order. This is critical for content-addressing (same data = same hash). Sorting is done by:
- `FxtrID`/`EntityRef`: sorted by `_raw_data_id` bytes
- `str`: sorted by UTF-8 bytes
- `int`: sorted by value
- Other types: sorted by repr

**Inline entities (`Child[T]` and `LinkedEntity[T]`):**
When a `DataclassEntity` has `Child[T]` or `LinkedEntity[T]` fields, those entities are:
- Automatically stored when the parent is stored
- Automatically loaded when the parent is loaded (via two-phase deserialization)

Two-phase deserialization works by:
1. Phase 1 (collecting): Deserialize once to collect all inline entity refs
2. Parallel load: Load all collected refs in parallel using `anyio.create_task_group()`
3. Phase 2 (rebuilding): Deserialize again, substituting loaded entities

Users with `__post_init__` validation must handle `DeferredLoadPlaceholder` values during the collecting phase.

**Helper functions:**
- **`build_converter_for(ty)`**: Build an async converter function for entity types, including unions
- **`store_entity(entity, as_type, scope)`**: Store an entity with optional explicit type annotation and scope
- **`deep_load(thunk, scope)`**: Load all EntityRefs accessed by a thunk
- **`visit_ids_in(value, callback)`**: Visit all IDs in a sendable/cbor value
- **`async_visit_ids_in(value, callback)`**: Async version of visit_ids_in
- **`preserve_orig_class`**: Decorator for frozen dataclasses that need to preserve `__orig_class__` for type parameters
- **`OutputStream`**: Experimental streaming output wrapper

See [notes/roundtrip_serdes.md](notes/roundtrip_serdes.md) for detailed documentation on the serialization/deserialization design.

### fxtr_scope.py

Defines the `FxtrScope` class for managing entity lifetimes:

- **`FxtrScope`**: A scope within which entities can be saved and retrieved
  - Used as an async context manager (`async with FxtrScope() as scope:`)
  - Can include parent scopes via `include` parameter
  - `isolated=True` creates a scope that doesn't inherit from the active scope
  - Key methods: `store_cbor()`, `load_cbor()`, `lift()`, `copy_from_scope()`, `abandon()`, `is_tracked()`
- **`ACTIVE_SCOPE`**: Context variable for the current scope (defaults to a root scope)
- **`GLOBAL_BACKING_STORE`**: Context variable for a shared LMDB backing store (optional)
- **`get_active_context_marker()`**: Helper to get the current context marker
- **`track_builtin_id(name)`**: Track a builtin ID in the active scope

### fxtr_context_api.py

User-facing API for working with persistent namespaces:

- **`FxtrNamespace`**: Abstract base for namespace operations (pin, unpin, retrieve)
  - Each namespace has its own `scope` property for managing entity lifetimes
- **`SQLiteFxtrNamespace`**: SQLite-backed namespace, used as an async context manager
  - `isolate_scope=True` creates an isolated scope for the namespace

### memory_arena.py

In-memory implementation:

- **`MemoryStore`**: Backing store holding actual entity data. Manages:
  - Node states (`StoredNode`, `LazyStoredNode`, `DerivedNode`, `InheritedNode`)
  - Reference counting for garbage collection
  - Arena lifecycle
  - `clear_all_state()`: Quickly release all state when a scope exits
  - External arenas can be attached via `unsafe_lazily_attach_from_arena()` on its arenas

- **`MemoryArena`**: A "view" into the store with a tracked liveset
  - `unsafe_lazily_attach_from_arena()`: Lazily attach entities from another arena (caller must ensure source stays open)
  - `copy_from_arena()`: Eagerly copy entities from another arena
  - `copy_to_new_arena()`: Create a new arena containing specified entities

Key concepts:
- **StoredNode**: Has actual CBOR bytes stored
- **LazyStoredNode**: Can reconstruct data from a provenance node that wraps it
- **DerivedNode**: Contentless nodes (`.`, `!`, `c`, `p`, `u`) computed on demand
- **InheritedNode**: Placeholder for data inherited from another arena

### sqlite_arena.py (DEPRECATED)

**DEPRECATED**: This SQLAlchemy-based implementation has been replaced by `sqlite_store/` which is 7-10x faster.

The old implementation is in `fxtr2/data/deprecated/sqlite_arena.py` and uses SQLAlchemy Core + aiosqlite. It is kept for reference and benchmark comparison only. All new code should use `sqlite_store` instead.

See `sqlite_store/` section above for the current implementation.

### sqlite_arena_migrations.py

Database schema migrations for upgrading SQLite databases between versions:

- **Version tracking**: Uses SQLite's `PRAGMA user_version`
- **Automatic migration**: `init_database()` runs migrations when needed
- **Table definitions**: Uses SQLAlchemy table definitions from `sqlite_arena.py` for consistency
- Migrations use the "rename old table, create new, copy data, drop old" pattern since SQLite doesn't support `ALTER CONSTRAINT`

See `plans/phase2/stage7_sqlite_dep_duplication_fix.md` for details on the migration system.

### sqlite_store/

High-performance sqlite3-based SQLite storage backend. Uses Python's built-in sqlite3 module with synchronous connections dispatched to a thread pool instead of SQLAlchemy + aiosqlite, with connection pooling and read/write separation for better WAL mode performance.

**Architecture** (see `plans/phase2/stage8_sqlite_speedup.md` for details):
- **`sqlite_util.py`**: Connection pool infrastructure
  - `ConnectionPool`: Pool of sqlite3 connections with capacity limiting via `anyio.CapacityLimiter`
  - `ConnectionPoolGroup`: Paired read/write pools for WAL mode concurrency
  - Connections are reused from a freelist; uncommitted transactions are rolled back on return
  - Uses `anyio.to_thread.run_sync()` to dispatch sync operations to thread pool

- **`sqlite_ops.py`**: All SQL strings and sync database operations
  - Module-level constant SQL strings for better SQLite caching
  - Sync functions taking `sqlite3.Connection` as first parameter
  - Each function manages one or more transactions
  - Uses `with transaction(conn, mode):` context manager for transaction control
  - Functions for: sessions, namespaces, pins, arenas, garbage collection, entity operations

- **`sqlite_namespace.py`**: Public API classes matching `sqlite_arena.py` interface
  - `SQLiteFxtrSession`: Session management with lifecycle and garbage collection
  - `SQLiteNamespaceBackend`: Namespace with named pins implementing `PersistentPinningBackend` and `ObservableStore`
  - `SQLiteNamespaceArena`: Arena for entity tracking implementing `CopyTargetArena`
  - Dispatches to `sqlite_ops` functions via `ConnectionPoolGroup.run_sync_with_connection()`
  - Supports both file-based and in-memory (`:memory:`) databases

**Key optimizations:**
- Constant SQL strings cached by SQLite for faster repeated execution
- Read/write connection pools allow concurrent reads in WAL mode
- Sync operations avoid async overhead for database I/O
- Small, focused transactions minimize lock contention

### file_util/

Shared file utilities:

- **`lock.py`**: File locking utilities using `fcntl.flock()`:
  - `async_file_lock_acquire()`: Async blocking lock with inode verification and retry
  - `sync_file_lock_acquire_nonblocking()`: Sync non-blocking lock attempt (raises `LockNotAcquiredError` if held by another process)
  - Used by both `sqlite_arena.py` (session file locking) and `git_store/` (dirty mark system)

### system_entities.py

Built-in entity types:

- **`ContextEntity`**: Represents execution context (for `c` nodes)
- **`PathRootEntity`**: Represents path root data (for `p` nodes)
- **`FxtrBasicType`**: A basic type definition with namespace UUID and type name (for user-defined types)
- **`define_type(namespace_uuid, type_name)`**: Helper function to define and register a new type ID

### legacy_compat/

Compatibility layer for storing fxtr v1 entities in fxtr v2's system:

- **`legacy_compat/data.py`**: Main implementation module
  - **`LegacyCompat[T]`**: Wrapper class that stores v1 `EntityBase` instances as v2 entities
    - `value`: The wrapped v1 entity
    - `legacy_cid`: The original v1 CID (for integrity verification)
    - Serializes v1 IPLD structure with CID links replaced by FxtrID strong links
    - Deserializes back to the original v1 entity, verifying CID matches
  - **`PostOrderEntityCollector`**: Custom visitor for collecting v1 entities in dependency order
    - Uses post-order traversal (dependencies before dependents)
    - Ensures CID→FxtrID mappings are available when processing each entity's IPLD
  - **CBOR traversal helpers**: `walk_and_collect_cids()`, `walk_and_replace_cids()`, `walk_and_collect_fxtr_ids()`, `walk_and_replace_fxtr_ids()`

- **`legacy_compat/__init__.py`**: Re-exports key classes and functions for convenience

The wrapped entity format uses type ID `!compat.legacy_v1` and stores the original IPLD in a `data` field with CIDs replaced by strong FxtrID links, plus a `legacy_cid` field for verification.

See `plans/phase2/stage4_fxtr1_compat_entities.md` for the full implementation plan.

### git_store/

Git-integrated storage backend allowing entities to be version-controlled alongside code. Uses TSV files for pin name → FxtrID mappings, with a shared SQLite store for actual entity data.

**Main API:**
- **`git_namespace.py`**: `GitNamespaceBackend` class - the main user-facing API implementing `PersistentPinningBackend` and `ObservableStore`

**Supporting modules:**
- **`tsv_file.py`**: Read/write TSV files with sorted order validation
- **`key_utils.py`**: Parse and validate pin keys (e.g., `data/models:encoder`)
- **`config.py`**: Store configuration, git directory detection, init/load
- **`dirty.py`**: Dirty mark system for batched writes to sqlite (uses `file_util/lock.py`)
- **`sync.py`**: TSV ↔ SQLite synchronization

See `data/git_store/LOCAL_CODE_OVERVIEW.md` for detailed documentation and `plans/phase2/stage1_git_namespace.md` for the full implementation plan.

### Thingwatcher Integration

Visualization of fxtr entities is provided by [thingwatcher](../extras/thingwatcher). See `thingwatcher/fxtr/` for the integration module that allows viewing fxtr entity graphs in thingwatcher's debugging UI.

## Typical Usage Flow

The system has a default root scope that's used automatically, so simple cases don't require explicit scope setup:

1. **Define entity types** (typically at module level):
   ```python
   import uuid
   from fxtr2.data.system_entities import define_type

   MY_NAMESPACE = uuid.UUID("...")  # Your project's namespace UUID

   @dataclass(frozen=True)
   class MyEntity(DataclassEntity, fxtr_type=define_type(MY_NAMESPACE, "my_type")):
       name: str
       data: list[int]
   ```

2. **Create and store entities** (uses default scope automatically):
   ```python
   entity = MyEntity(name="test", data=[1, 2, 3])
   ref = entity.store()  # Returns EntityRef
   ```

3. **Load entities**:
   ```python
   loaded = await ref.load()  # Returns the entity
   ```

4. **For scoped operations with automatic cleanup**:
   ```python
   from fxtr2.data.fxtr_scope import FxtrScope

   async with FxtrScope() as scope:
       # Entities stored here are tracked in this scope
       ref = MyEntity(name="temp", data=[]).store()

       # Copy results to the outer scope before exiting
       await ref.copy_to(scope.outer)
   # Scope exits, unreferenced entities are cleaned up
   ```

5. **For persistence, use the namespace API**:
   ```python
   from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace

   async with SQLiteFxtrNamespace("sqlite+aiosqlite:///db.sqlite", "myns") as ns:
       await ns.pin("my_pin", ref)  # Pin by EntityRef

       # Retrieve later - entities are bound to the namespace's scope
       loaded_ref = await ns.retrieve("my_pin", MyEntity)

       # Load from a scope that includes the namespace's scope
       async with FxtrScope(include=[ns.scope]) as scope:
           loaded = await loaded_ref.load()
   ```

6. **For isolated scopes** (no inheritance from active scope):
   ```python
   async with FxtrScope(isolated=True) as iso_scope:
       # This scope doesn't inherit from the active scope
       # Must explicitly include parent scopes if needed
       ...
   ```

## The `_provenance_parent` Pattern

A `FxtrID` can optionally carry a `_provenance_parent` - another FxtrID that represents the provenance-level identity of this node. Key properties:

- The `_provenance_parent` always unwraps to the "data" ID
- Equality/hashing only use `_raw_data_id` (ignores provenance)
- The provenance parent is used by the (not-yet-implemented) ProvenanceArena for automatic provenance tracking
- When the `ACTIVE_NODE_CONTEXT` has a provenance parent, newly created entities inherit it

## Gotchas and Non-obvious Details

Things that might surprise you when working with this codebase:

1. **FxtrID equality ignores provenance**: Two `FxtrID`s with the same `_raw_data_id` but different `_provenance_parent` are considered equal and hash the same. This is intentional for transparent provenance tracking.

2. **Canonical CBOR order matters for p-nodes**: Path indices in p-nodes refer to the position of child IDs in CBOR serialization order (sorted dict keys, then list items). Use `visit_and_project_ids_in_canonical_cbor_order` when working with p-nodes.

3. **Node type properties are semi-orthogonal**: "Ground vs provenance" and "contentful vs contentless" are separate axes:
   - `d`: ground + contentful
   - `n`: provenance + contentful
   - `.`, `!`: ground + contentless
   - `c`: provenance + contentless
   - `p`, `u`: provenance + depends on wrapped node

4. **The varint32 encoding is custom**: P-node paths use a custom base-32 varint encoding (not standard CBOR varint). See `_encode_varint32` / `_decode_varint32`.

5. **System entities must have "value" field**: `ContextEntity` and `PathRootEntity` must have a `value` field to work with `create_c_node` / `create_p_node`.

6. **Scopes vs arenas**: Scopes are the user-facing abstraction; arenas are the lower-level implementation. Most user code should work with `FxtrScope`, not `MemoryArena` directly. Non-isolated scopes automatically become the active scope when entered and inherit from their parent scope.

7. **Namespace scopes**: Each `SQLiteFxtrNamespace` has its own scope. Entities retrieved from a namespace are bound to the namespace's scope. To load them, either use `include=[ns.scope]` when creating a child scope, or use `lift()` to bring them into another scope.

For more details, see [notes/implementation_notes.md](notes/implementation_notes.md).

## Planned Features

See [notes/planned_features.md](notes/planned_features.md) for features that are designed but not yet implemented.

## Testing

Tests use pytest with pytest-anyio for async support. Run tests with:

```bash
uv run pytest
```

**Current test counts (672 tests total):**
- `test_entity_core.py` - 55 tests for FxtrID and CBOR encoding
- `test_arena_util.py` - 26 tests for visitor functions
- `test_memory_arena.py` - 48 tests for in-memory storage
- `test_entity_serdes.py` - 93 tests for high-level serialization (includes inline entity tests, collection types)
- `test_fxtr_scope.py` - 31 tests for scope management
- `test_smoke.py` - 5 basic smoke tests
- `test_legacy_compat/test_data.py` - 16 tests for v1 entity compatibility layer (tests `fxtr2.legacy_compat`)
- `test_git_store/` - 253 tests for git-backed namespace (includes lock tests and 33 CLI tests)
- `test_sqlite_store/` - 87 tests for sqlite3-based SQLite store (includes connection pool, session lifecycle, and all arena operations)
- `deprecated/` - 75 tests for old sqlite_arena implementation (kept for reference)

Test infrastructure:
- `tests/conftest.py` - Fixtures for memory_store, memory_arena, active_arena, tmp_db_path, etc.

Test plans (in `plans/`):
- `test_plan_entity_core.md` - Tests for FxtrID and CBOR encoding
- `test_plan_arena_util.md` - Tests for visitor functions
- `test_plan_memory_arena.md` - Tests for in-memory storage
- `test_plan_sqlite_arena.md` - Tests for SQLite persistence
- `test_plan_entity_serdes.md` - Tests for high-level serialization

## Related Documentation

- `plans/` - Implementation plans and test plans
- `plans/reorg_tracker.md` - Current progress on codebase organization
- `scratch_notebooks/` - Development notebooks (may be out of date, useful as test inspiration)
- `notes/` - Design notes and detailed explanations
  - `notes/implementation_notes.md` - Deep dive into non-obvious implementation details
  - `notes/planned_features.md` - Features that are designed but not yet implemented
  - `notes/reorg_questions.md` - Q&A from initial codebase review
  - `notes/roundtrip_serdes.md` - Detailed documentation on entity serialization/deserialization design
