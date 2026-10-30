# Fxtr2 Code Overview

This document describes the current state of the fxtr2 codebase, focusing on the data layer implementation. Supplementary notes (gotchas, benchmarks, test plans, Thingwatcher, related docs) live in [CODE_OVERVIEW_EXTRAS.md](CODE_OVERVIEW_EXTRAS.md).

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
- **Lifetime Token**: An object representing the lifetime of a scope or external resource; used for GC

## Core Concepts

### Scopes

A **Scope** (`FxtrScope`) governs the lifetime of retrievable IDs. Scopes serve two main purposes:

1. **Limiting memory usage** — entities tracked only in a scope are cleaned up when the scope exits.
2. **Maintaining the complete-subgraph invariant for external storage** — ensuring that entities referencing lazily-loaded data from a namespace don't outlive that namespace.

Guarantees:
- Anything stored into a scope can be loaded from that scope.
- Loading an entity makes all its referenced children loadable.
- Child scopes inherit from their parents — anything accessible in a parent scope is accessible in a child scope.
- After a scope ends it cannot retrieve entities; child scopes structurally keep their parents alive (each scope installs an ancestor `IndependentLocalBridge` holding a clone of every direct/transitive parent's main lease).

There's a **`GLOBAL_SCOPE`** that is always active, so simple usage doesn't need explicit scope setup. New scopes inherit from the currently-active scope by default; entering a scope makes it the active scope for code running inside it. The active scope is accessed via `FxtrScope.get_active()`.

**User-facing scope shape**:

- **Construction is inert.** `FxtrScope(...)` only records configuration — no token, arena, parent leases, or marker setup. A constructed-but-unentered scope is GC-safe and no method works on it; it becomes usable only once *entered* (`async with scope:` or `scope.lowlevel_start_lifespanless()`). There is no autostart and no `mountable=` axis.
- `isolated=True` — scope does *not* inherit from the active scope and does not become the active scope when entered. Useful for background work that shouldn't affect the main scope.
- `include=[other_scope, ...]` — explicitly attach other scopes as parents. Useful when you need access to entities from a namespace opened in a different async task or with `isolate_scope=True`.
- **Entry**: `async with scope:` opens an `anyio` task group + closing event (every CM-entered scope is *within-lifespan* and can host services / lifespans), mints the scope's lifetime, and (non-isolated) becomes the active scope. `scope.lowlevel_start_lifespanless()` starts without a task group (the root scope + lowlevel callers; cannot host services); pair with `discard_base_authorization()` or `force_close()`.
- `provenance_context: FxtrID | None` — sets the scope's `$fxtr.ctx` marker (see *The `_provenance_parent` Pattern* below). When set, every entity stored / loaded through this scope round-trips through provenance form.
- `store_provenance: Literal["auto", "if_marked", "strip"] = "auto"` — controls how `store_cbor` handles child provenance. `"strip"` discards layered children on store and refuses to inherit a parent's provenance marker (incompatible with `provenance_context`). `"if_marked"` takes the prov path only when the cbor's `$fxtr.ctx` marker carries a provenance parent (the previous default). `"auto"` (current default) additionally scans the cbor: when any non-marker child carries a provenance parent but the marker is bare, it substitutes the marker with the scope's own c-node (if it has a `provenance_context`) or a lazily-minted fallback c-node built from a `FallbackContextMarker` keyed on the scope's `name`. Two scopes with the same `name` share the same fallback c-node — dedup is intentional. Loads always preserve provenance regardless of mode.
- `name: str | None` — debug name; surfaces in `repr(scope)`, in lifetime-token names, and as the `scope_name` payload of any fallback c-node. Defaults to the caller's `co_name` via `inspect.currentframe()`. An empty string is kept verbatim; only `None` triggers inference.

Internally **every** scope's lifetime is a `DerivedLocalLifetime` over a private key ULL plus each direct parent's main token (uniform — no ULL-vs-derived split). A live `Lease[child.main]` carries each `Lease[parent.main]` as a via, so child-keeps-parent-alive is structural and a parent revoke cascades to children. Authorization is split into a **scope-level base** (`_scope_auth_lease`, an attribute, usable from any task while open) plus **per-context overrides** (`with scope.authorize(lease):`, one shared `_AUTH_OVERRIDES` contextvar holding a copy-on-write map). The entry primitive `open_with_explicit_leases(parent_leases=…, extra_derived_leases=…)` lets callers mint the token from explicitly-supplied leases (cross-task / draining-parent / dispatch arg-token cases) rather than from in-context authorization.

**Movement patterns** (extending completeness across scope boundaries):

- `lift(node, scope=None)` — bring an entity from a parent scope into a child scope, making it explicitly tracked there. Lightweight (data is already accessible via inheritance).
- `copy_to(scope)` / `copy_from_scope(other)` — copy an entity and its transitive subgraph between scopes that don't share a parent relationship. Async because they may need to load entities along the way. Pinning to a namespace triggers a copy automatically.
- `track_from_scope(other)` — sync alternative to `copy_from_scope` for cases where every reachable node either has in-memory data or is claim-held by a scope/arena bridged into the destination (e.g. a shared ancestor); the latter are adopted lazily as same-store inheritance entries targeting the claim-holder, with no data transfer. Errors if the walk hits a node that would require an async load.
- `abandon(node)` — release the scope's claim on a node.

**Drain semantics**: `await scope.aclose()` drops the base authorization lease (→ `None`, which rejects new ambient use in every task) and awaits the derived token's natural drain; `scope.force_close()` revokes the derived token **and** the key ULL synchronously (used by `GLOBAL_SCOPE`'s atexit hook). The async CM wraps these in `try: yield; except: force_close; raise; else: aclose`. A scope is *draining* while its token is alive but the base lease has been dropped; an open-but-unauthorized context is rejected by `check_authorized`, while in-flight contexts holding overrides keep working. Closing a never-opened (inert) scope raises. `is_open()` (token created & not ended) and `is_authorized()` (this context resolves a live override-or-base) replace the old `is_alive()`. For the authorization resolution order, decompose/compose helpers, and the cap-bridge graph, see `data/LOCAL_CODE_OVERVIEW.md`.

### Global Memory Store and Lifetime Tokens

All scopes share a single global `MemoryStore` (accessed via the `GLOBAL_MEMORY_STORE` contextvar in `entity_ref_scope.py`). Entity lifetimes are managed via **lease-backed `LifetimeToken`s** from `fxtr2.leases.lease_types`:

- Each `FxtrScope` owns its own lifetime: at entry it mints a `DerivedLocalLifetime` (`_derived`) over a private "key" ULL (`_key_ull`) plus each direct parent's main token. The scope calls `MemoryStore.register_token(token, name=...)` so the store subscribes to the token's end-callback for GC. The store does *not* hold a lease on the token, cache its grant, or track parent/ancestor relationships — grant uniqueness is the lifetime owner's invariant, and parent-outlives-child is structural: a live `Lease[child.main]` carries each `Lease[parent.main]` as a via (the per-parent ancestor cap-bridges are now case-3 `DependentLocalBridge`s that `subtake` a parent lease from the child's own authorization, holding nothing at rest).
- Each token is a fully independent storage layer. Each node tracks two independent token-keyed maps:
  - `owned_tokens`: tokens with direct ownership (data in memory + every dep covered).
  - `inheritance_bridges: dict[X_reg, _InheritanceEntry]`: per-token entries describing the source `X_reg` reaches this node through. The source is either a foreign `ReadableArena[Y]` or a `_SameStoreSentinel(target_token=Y)` (for nodes inherited from another token in this same store, e.g. parent-scope lifts).
- Bridges are `LocalBridge` (`IndependentLocalBridge | DependentLocalBridge` from `fxtr2.leases.lease_bridges`). Each `(X_reg, Y)` pair has one shared bridge record on `_token_data[X_reg].bridges[Y]`, holding the bridge plus the set of node IDs that use it. The at-rest leases inside the bridge structurally pin Y alive while X_reg is alive.
- A token T has access to node N **iff** `T in N.owned_tokens` or `T in N.inheritance_bridges`. There is no implicit ancestor access, no antichain, no domination logic: callers that want T to reach a node go through `lazily_attach_from_arena` (or the FxtrScope `lift` path that wraps it).
- When a scope exits (`aclose`), it drops its base authorization lease (`_scope_auth_lease`). Once the last `Lease[main]` clone anywhere is released, the token's last key via drops, the key ULL ends, and the derived output token ends (input-end cascade) — the store's end-callback then fires: it walks the token's `node_associations` reverse index, drops the token from every node's `owned_tokens` and `inheritance_bridges`, tears down every bridge it registered (deregister broken-callbacks + `bridge.discard_at_rest()`), and frees nodes whose claim maps both become empty. No explicit "invalidate" call is needed.

Nodes in the store are represented by a unified `MemoryStoreNode` type that tracks:
- Optional `NodeData` (in-memory bytes and dependency info)
- `inheritance_bridges: dict[X_reg, _InheritanceEntry]` — per-token inheritance entries (one per token that reaches this node through some source).
- `owned_tokens: set[LifetimeToken]` — tokens with direct ownership.
- An `unwraps_to` field (available even without data, for provenance chain queries)

**Safe arena attachment**: `ReadWriteArena[B].lazily_attach_from_arena(lease, node_ids, source, bridge)` registers a source arena and bridge for one or more nodes. `node_ids` accepts a single `FxtrID` or a `Collection[FxtrID]`; the batched form does *one* `source.copy_to_new_arena` fork covering every supplied ID, instead of one per ID. The `bridge` is a `LocalBridge` that mints a fresh `Lease[Y]` per load; for foreign arenas the bridge variant matches the case-1/2/3 model in `plans/phase3/stage8c_user_caps/lifetime_conversion_mechanics.md`. When `source` is a `MemoryArena` bound to this same `MemoryStore`, the store automatically switches to *sentinel mode*: it stores a `_SameStoreSentinel(target_token=source.token)` instead of forking the source, and loads recurse through `source.token` directly. Callers don't see the distinction. The store watches each at-rest lease in the bridge for breakage and re-surfaces stored break reasons on subsequent loads.

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

### Data Integrity Invariants

Fxtr enforces several invariants on stored data — together they're what makes a `FxtrID` a useful unit of reference:

**Content addressing.** Every entity ID is derived from its content. Identical data always gets the same ID — independently storing the same entity yields the same `FxtrID`, so deduplication is automatic and hermetic caching is possible. For `d` and `n` nodes the ID is the SHA-256 of the canonical CBOR; for derived nodes (`c`, `p`, `u`) the ID is computed from the IDs they reference.

**Immutability.** Once an entity is stored its contents never change. The `FxtrID` is a permanent, stable reference.

**Complete subgraphs.** If you have a valid `FxtrID` in scope, you can load that entity *and* everything it transitively references. The store enforces this on the way in: you cannot store an entity that references an ID that isn't already tracked. So a single `FxtrID` is effectively a handle to its entire subgraph, and loading from one ID is sufficient to materialize the whole closure (lazily, transitively). This is the property scopes work to preserve when entities cross scope / namespace boundaries — `lift` / `copy_to` / `copy_from_scope` / `track_from_scope` all walk the DAG so the destination is left covering the closure.

**Roundtrip guarantee.** For any correctly-implemented entity, `store → load → store` yields the same `FxtrID`. The serialized representation is deterministic. This holds even when a custom load handler decodes the same wire form into a different Python type than the original, as long as the new Python type re-serializes to the same CBOR.

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
├── conversion/              # Type-annotation-based conversion registry
│   ├── base_registry.py     # Generic TypeAnnotationHandlerRegistry with union/override support
│   └── converters/          # Concrete conversion handler implementations
│       ├── legacy_entity_abc.py    # SelfLoadingLegacyEntity bridge for old Entity ABC
│       ├── mashumaro_entities.py   # DataclassEntity, Child, LinkedEntity, mashumaro strategies
│       └── unknown_entity.py       # UnknownEntity fallback + LoadAsUnknownByDefault
├── common/                  # Higher-level building blocks shared across fxtr2 consumers
│   ├── channels.py          # Output channels (tree of reader/writer services): ChannelTag, ChannelUpdate/ChannelState/SubchannelOpened dataclasses, FxtrChannelWriter/FxtrChannelReader protocols, create_root_channel, iter_channel
│   └── task_utils.py        # start_with_scope / start_soon_with_scope — task starts with a chosen (or deliberately absent) active-scope context
├── debug/                   # Diagnostics: the scope-drain report + its always-on hooks
│   ├── scope_instrumentation.py # Leaf state the core feeds: live-scope registry, activation-task record, creation-site/tb capture, FXTR_SCOPE_REPORT_TB
│   └── scope_report.py      # dump_scope_report, fail_after_and_report_hung_scopes, report_hung_scopes_on_signal
├── services/                # User-facing service-capability layer — see LOCAL_CODE_OVERVIEW.md
│   ├── __init__.py
│   ├── service_cap_common.py # Wire shape + dispatch: FxtrServiceData, CommonServiceImpl, walk_service_methods, invoke_service_method, chain converter
│   ├── untyped_service.py    # @service_method, SimpleAsyncServiceImpl, UntypedServiceImpl, UnknownServiceProxy
│   ├── service_dialect.py    # FxtrServicesDefaultDialect + EntityRef/ServiceRef/FxtrID/frozenset/frozendict/FxtrServiceData / OverrideThisTypeBeforeUsing strategies + codec cache + encode_to_service_data/decode_from_service_data (+ service-resolver registration for the placeholder alias)
│   ├── typed_service.py      # @service_protocol, TypedServiceImpl, TypedServiceProxy factory (mashumaro-driven typed services)
│   ├── subscription_types.py # Subscriber / UnsubscribeHandle / EventSource — explicit subscribe/unsubscribe protocol primitives
│   ├── subscription_util.py  # SubscriberHub — reusable broadcast helper for EventSource impls (origin-lease event residency, non-pinning subscriptions, coalesce/queue modes)
│   ├── cap_ir_arena.py       # ArenaIRHandleAdapter (server) + CapIRArena (client) — wire arenas across Cap-IR
│   └── LOCAL_CODE_OVERVIEW.md
├── leases/                  # Lease-based lifetime primitives + Cap-IR — see LOCAL_CODE_OVERVIEW.md
│   ├── lease_types.py       # Abstract LifetimeToken, Grant, Lease + FreshLifetime branding
│   ├── local_lease.py       # Local concrete types: Unconditional/DerivedLocalLifetime, tokens, leases, STATIC_LIFETIME
│   ├── lease_util.py        # lease_cancel_scope helper for structured concurrency
│   ├── leased_cap_ir.py     # Cap-IR types: IRValue, IRHandle, IRFuture, requests, UnwrapForPipelining
│   ├── leased_cap_common.py # Local Cap-IR implementation helpers: result box, future impl, handle base
│   ├── lease_bridges.py     # Cap-bridge taxonomy: 4 dataclasses on Local/Pipelined × Independent/Dependent
│   └── LOCAL_CODE_OVERVIEW.md
├── rpc/                     # Cap-IR lowering / RPC protocol — see LOCAL_CODE_OVERVIEW.md
│   ├── __init__.py
│   ├── lowering_types.py    # RPCMessage + Expression sums (wire protocol v1 definition)
│   ├── lowering.py          # LeasedCapRPCConnection: tables, reader/writer, _LeaseOwner impl, lifecycle
│   ├── lowering_state.py    # Entry dataclasses, reserved ID constants, ConstantExpr
│   ├── lowering_errors.py   # RPCConnectionError / HandshakeError / RPCConnectionAborted
│   ├── lowering_remote_proxies.py  # RemoteLifetimeToken / RemoteGrant / RemoteIRHandle / RemoteIRFuture
│   ├── lowering_null_main.py       # NullMainInterface (placeholder main for as_client)
│   ├── lowering_devaluate.py       # Outbound IRCallArg → Expression translation
│   ├── lowering_evaluate.py        # Inbound phase-1 substitution + phase-2 evaluators
│   ├── lowering_message_handlers.py # Per-message _on_* handlers + dispatch_message
│   ├── json_serdes.py        # JSON wire codec: RPCMessage ↔ JSONTypes, wrap_json_stream
│   ├── scope_bridge.py       # open_rpc_scope CM: bridges a ServiceRef across an RPC transport, yields RPCSession
│   └── LOCAL_CODE_OVERVIEW.md
├── rpc_deprecated_v1/       # DEPRECATED: Previous RPC implementation (being replaced by leases/ + rpc/)
│   └── (same files as before — see rpc_deprecated_v1/LOCAL_CODE_OVERVIEW.md)
├── cli/                     # Command-line interfaces
│   ├── __init__.py
│   └── tsv_ns_cli.py      # fxtr-tsv-ns CLI
├── data/                    # Core data layer — see LOCAL_CODE_OVERVIEW.md
│   ├── entity_core.py       # FxtrID, CBOR encoding, node type definitions
│   ├── abstract_arena.py    # Abstract interfaces for arenas and stores
│   ├── arena_util.py        # Visitor functions for traversing entity data
│   ├── entity_ref_scope.py  # Entity, EntityRef, FxtrScope, ServiceRef, ServiceProxyContext, load handler registry
│   ├── fxtr_context_api.py  # User-facing API (namespaces)
│   ├── memory_arena.py      # Lease-backed global MemoryStore, MemoryArena, MemoryStoreNode
│   ├── system_entities.py   # Built-in entity types (ContextEntity, PathRootEntity, FxtrBasicType)
│   ├── repo_storage.py      # RepoEntityStore: standalone by-id entity store for a repo (DeepFetchable; exposes .token / .grant / get_lease()); TsvNamespaceBackend is a view over it
│   ├── repo_storage_config.py  # Lower-layer config/discovery: RepoStoreConfig, find_vcs_root/find_fxtr_dir, .fxtr/ repo-config parsing, load_repo_store_config, FxtrConfigError (tsv_namespace.config composes + re-exports these)
│   ├── LOCAL_CODE_OVERVIEW.md
│   ├── file_util/           # File utilities shared across modules
│   │   ├── __init__.py
│   │   └── lock.py          # Async and sync file locking with flock
│   ├── tsv_namespace/       # TSV-file fxtr namespace (pin mappings in git-tracked TSV files, a view over a repo entity store)
│   │   ├── __init__.py      # Re-exports key classes
│   │   ├── tsv_namespace.py # Main TsvNamespaceBackend (IndexablePinningBackend + DeepFetchable); holds a RepoEntityStore internally, exposes .store
│   │   ├── tsv_file.py      # TSV file reading/writing
│   │   ├── key_utils.py     # Pin key parsing and validation
│   │   ├── config.py        # Namespace-marker + repo-store configuration (RepoStoreConfig), discovery, init
│   │   ├── dirty.py         # Dirty mark management
│   │   ├── sync.py          # TSV ↔ SQLite sync
│   │   ├── index_file.py    # Index registration file (fxtr_namespace_indexes.tsv) I/O
│   │   ├── index_sync.py    # Index registration sync between file and worktree namespace
│   │   └── LOCAL_CODE_OVERVIEW.md  # Detailed module documentation
│   ├── remote/              # Remote entity transfer protocol and helpers
│   │   ├── __init__.py
│   │   ├── remote_client.py    # RemoteClient protocol (negotiate/push/pull)
│   │   ├── transfer_format.py  # CBOR encode/decode for entity streams
│   │   ├── transfer_entity_source.py  # TransferEntitySource: FlatEntitySource wrapping received data
│   │   └── LOCAL_CODE_OVERVIEW.md  # Detailed module documentation
│   ├── store_indexing/      # Namespace index construction and querying
│   │   ├── __init__.py      # Package init
│   │   ├── indexing_types.py # Core types: Indexer, IndexerConstraint, IndexablePinningBackend
│   │   ├── indexers.py      # Path elements, concrete indexers, unpack logic
│   │   ├── path_parse.py    # Path expression parser (e.g. "!(as '!type').x[*: int]: float")
│   │   ├── sql_expr.py      # SQL expression parsing, validation, rewriting (sqlglot-based)
│   │   ├── query_gen.py     # SQL query generation from resolved constraints
│   │   ├── index_computer.py # IndexComputer: scope-managed bridge for running indexers on entities
│   │   └── LOCAL_CODE_OVERVIEW.md  # Detailed module documentation
│   ├── layered_file_store/  # Layered file entity store (bridges fxtr ↔ nix-style layered derivations)
│   │   ├── __init__.py      # Re-exports reader / writer / pin-arena seam / errors / format constants
│   │   ├── errors.py        # LayerFormatVersionError, LayerRemoteNotConfiguredError, LayerEntityMissingError, ...
│   │   ├── layer_format.py  # On-disk format constants + sync read/write helpers (atomic tmp+rename)
│   │   ├── search.py        # _LayerSearchSet: dynamic parent-walking search set shared by reader+writer (+ expand_fully)
│   │   ├── arena.py         # LayeredFileArena (ReadableArena over disk + same-branded source fallback; holds a Grant, no leases)
│   │   ├── pin_arena.py     # Return-an-arena seam: arena_over_search_set / open_pin_arena / LayeredStoreDeepFetchable (by-id over a layer set)
│   │   ├── reader.py        # LayeredFileStoreReader: FxtrReadNamespace over a layer dir (on the pin_arena seam)
│   │   ├── writer.py        # LayeredFileStoreIsolatedWriter (write-only async CM; mark_as_remote / pin_to_this_layer / preload_data)
│   │   └── nix_builder.py   # run user python that writes into a layer writer from inside a nix build (backs lib.fxtr.mkFxtrFnBuilder / mkFxtrScriptBuilder); details in LOCAL_CODE_OVERVIEW.md
│   ├── sqlite_store/        # Fast sqlite3-based SQLite store with segment file storage
│   │   ├── __init__.py      # Re-exports public classes
│   │   ├── sqlite_util.py   # Connection pooling with capacity limiting
│   │   ├── sqlite_ops.py    # SQL strings and sync database operations
│   │   ├── sqlite_index_ops.py  # Index-related SQL operations (leaf module)
│   │   ├── sqlite_migrations.py  # Schema migrations (v2→v3, v4→v5, v5→v6)
│   │   ├── sqlite_namespace.py  # Public API classes (SQLiteFxtrSession, etc.)
│   │   ├── entity_blob.py   # Entity blob encoding/decoding for segment files
│   │   ├── segment_io.py    # Segment file I/O (SegmentManager, append, flush)
│   │   ├── lmdb_blob.py     # LMDB blob format (used only by v5→v6 migration)
│   │   └── LOCAL_CODE_OVERVIEW.md  # Detailed module documentation
│   └── deprecated/          # Deprecated modules (kept for reference)
│       ├── __init__.py
│       ├── sqlite_arena.py  # Old SQLAlchemy-based implementation (7-10x slower)
│       └── sqlite_arena_migrations.py  # Schema migrations (still used by sqlite_store)
├── legacy_compat/           # Compatibility layer for fxtr v1
│   ├── __init__.py          # Re-exports key classes
│   ├── data.py              # LegacyCompat wrapper, UpgradeCache, CBOR helpers
│   ├── transitional_rundata.py  # TaskWithPredecessors, RunWithPredecessors, etc.
│   ├── transitional_session_manager.py  # TransitionalSessionManager, RunCacheManager
│   └── runset_conversion.py # convert_legacy_runset_to_run_cache()
├── experimental/            # Experimental features (HTTP serving, read-only namespaces)
│   ├── __init__.py
│   ├── read_namespace.py    # FxtrReadNamespace ABC (read-only subset of FxtrNamespace)
│   ├── http/                # HTTP API for serving/consuming fxtr entities
│   │   ├── __init__.py
│   │   ├── asgi_app.py      # Starlette ASGI app (preview, data, pin, pins routes)
│   │   ├── server.py        # Hypercorn wrapper (serve_until, serve_forever, serving_context)
│   │   ├── client.py        # HttpReadableArena, HttpDeepFetchable, HttpFxtrReadNamespace
│   │   ├── workspace_namespace.py  # WorkspaceNamespace (FxtrNamespace over HTTP to a hub workspace)
│   │   └── service_*.py     # stateless service layer: codec / dispatch / rest + ws endpoints / registry (see LOCAL_CODE_OVERVIEW)
│   └── LOCAL_CODE_OVERVIEW.md  # Detailed module documentation
└── prototyping/             # Experimental prototypes (not part of main system)
    ├── gql_spike/           # GraphQL prototype
    └── panel_spike/         # Panel viewer prototype
```

## Typical Usage Flow

The system has a default root scope that's used automatically, so simple cases don't require explicit scope setup:

1. **Define entity types** (typically at module level):
   ```python
   import uuid
   from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
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
   from fxtr2.data.entity_ref_scope import FxtrScope

   async with FxtrScope() as scope:
       # Entities stored here are tracked in this scope
       ref = MyEntity(name="temp", data=[]).store()

       # Copy results to the outer scope before exiting
       await ref.copy_to(scope.outer)
   # Scope exits, unreferenced entities and cache are cleaned up
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

6. **Cross-namespace operations** (nesting namespace context managers):
   ```python
   # Nesting namespace context managers means the inner active scope can
   # see entities from both. No explicit scope wiring needed.
   async with HttpFxtrReadNamespace(client) as http_ns:
       async with SQLiteFxtrNamespace(db_path, "local") as sqlite_ns:
           # Retrieve from http_ns — entity is tracked in http_ns.scope,
           # which is a parent of sqlite_ns's active scope.
           fxtr_id = await http_ns.retrieve("some_pin")
           # Pin into sqlite_ns — lift finds the entity via scope hierarchy.
           await sqlite_ns.pin("some_pin", fxtr_id, overwrite=True)
   ```

7. **For isolated scopes** (no inheritance from active scope):
   ```python
   async with FxtrScope(isolated=True) as iso_scope:
       # This scope doesn't inherit from the active scope
       # Must explicitly include parent scopes if needed
       ...
   ```

## Module Details

### conversion/base_registry.py

Generic type-annotation-based handler registry. `TypeAnnotationHandlerRegistry[K, V]` maps type annotations (plain types, `TypeAliasType`, `NewType`) to handlers and supports:

- **Type unwrapping**: Walks through type aliases, `NewType`, `GenericAlias`, and `Annotated` wrappers to find handlers
- **MRO-based dispatch**: For plain types, walks the class MRO to find handlers registered on base classes
- **Union support**: Automatically resolves unions to one handler per branch
- **Override rules**: Less-specific handlers must opt in via `allow_implicit_overrides=True`; `OverrideFxtrHandlers[T]` provides explicit override that short-circuits conflict checking
- **Generic substitution**: Parameterized aliases like `type Foo[T] = A[T] | B[T]` correctly substitute type parameters
- **`typing.Never`**: After plain-type handling, uninhabited ``Never`` is recognized: if lookup already matched a handler on an enclosing alias (e.g. a PEP 695 alias defined as ``Never``), that handler is returned; otherwise lookup raises ``TypeError``.

The `MatchedHandler` result includes both the handler and the `subquery` — the specific type annotation the handler should work with (preserving subclass types and generic parameters).

### entity_core.py

The foundation of the system. Defines:

- **`FxtrID`**: Immutable identifier class with:
  - `_raw_data_id`: The actual ID bytes (tag byte + payload)
  - `_provenance_parent`: Optional provenance-level ID (for automatic provenance tracking)
  - Methods for parsing/formatting ID strings, splitting tag/payload

- **CBOR helpers**: `RawWeakFxtrID`, `RawStrongFxtrID`, `RawFxtrEntityMarkerID` wrapper classes
- **Encoding/decoding**: `fxtr_cbor_dumps()`, `fxtr_cbor_loads()`, `fxtr_cbor_dumps_with_marker_check()`

### abstract_arena.py

Defines the abstract arena and namespace-backend interfaces. Arenas are the lease-gated tier: every arena is parameterized on a lifetime brand `B`, exposes a `token: LifetimeToken[B]` property, and takes a `Lease[B]` as the first argument of every method (consumed on each call). Namespace backends (`PersistentPinningBackend` / `IndexablePinningBackend` / `DeepFetchable`) are also brand-parameterized but their own methods don't take a backend lease — backends manage drain via their context-manager state.

The hierarchy: `FlatEntitySource[B]` → `ReadableArena[B]` (adds `load`, `unwrap_provenance`, etc.) → `ReadWriteArena[B]` (adds sync `store` and `lazily_attach_from_arena` for mounting foreign arenas via a `LocalBridge`). `CopyTargetArena[B]` adds `copy_from_arena`. `DepKind` enumerates dep relationships (STRONG_REF, WEAK_REF, CTX_NODE, BACKSTEP, UNWRAPS_TO); `FxtrEntityFlatData` is the flat (bytes + deps) representation. See `data/LOCAL_CODE_OVERVIEW.md` for full method-by-method details.

### arena_util.py

Visitor functions for traversing entity data — `visit_ids`,
`visit_and_project_ids`, plus canonical-CBOR-order variants (sync and
async). Also home to the shared **`compute_precomp_closure`** walker
(used by the HTTP server's `/data` route and the layered file store
writer) along with its `PrecompEntry` (TypedDict) /
`PrecompMap = dict[str, PrecompEntry]` types. See
`data/LOCAL_CODE_OVERVIEW.md` for details.

### entity_ref_scope.py

The unified core module for entities, entity references, scopes, and the
service-capability primitives. Public surface:

- **`Entity`** — abstract base class for serializable entities
  (`_fxtr_store_impl` returns an `EntityRef[Self]`).
- **`EntityRef[T]`** — lazily-loadable reference holding `entity_id` +
  `target_type`. Equality / hashing are provenance-blind; the provenance
  dimension lives in scope-side cache keys.
- **`EntityLoadHandler[T]`** + **`register_entity_load_handler`** —
  pluggable deserialization, dispatched by entity type id.
- **`FxtrScope`** — async-CM-based scope governing the lifetime of
  retrievable IDs. Construction is inert; entry (`async with` or
  `lowlevel_start_lifespanless()`) mints a uniform derived token, the base
  authorization lease, and the arena. `isolated` controls inheritance from /
  activation of the active scope; a CM-entered scope opens a task group and can
  host services, a lifespanless one cannot. A scope owns its main arena,
  exposes the standard `store/load/lift/copy_from_scope/track_from_scope/abandon`
  surface, the authorization helpers (`authorize` → `AuthorizationHandle` / `discard_base_authorization` /
  `clone`/`decompose`/`compose_authorization`), `attach_lifespan_to_scope(cm)`
  (enter an async-CM for the scope's lifetime — setup authorized + active,
  teardown after the token drains and unauthorized; `aclose` waits for attached
  lifespans to tear down), and `freeze_scope()` for immutability.
- **`ServiceRef[T]` / `ServiceKey` / `FxtrServiceImpl[T]` /
  `ServiceProxyContext`** — live-capability analogs of `Entity` /
  `EntityRef`. A `ServiceRef` carries
  `(handle, target_type, service_type_id)`; `ServiceKey` is the
  `(handle, type_id)` unit of identity used by scope-side mount /
  resolve bookkeeping. `ServiceProxyContext` is the scope-side API a
  resolved proxy uses (mint leases, fork arenas) without reaching into
  scope private members. Concrete dispatch + method-discovery
  (`@service_method`, `UntypedServiceImpl`, etc.) lives in
  `fxtr2/services/`. Cross-tree bridging via
  `dest.bridge_service_from_scope(ref, source_scope, keepalive_via=…)`
  registers `ref` on `dest`; the required `keepalive_via`
  (`"independent_lease"` — source closes after dest; or
  `"concurrent_child_bridge"` — source/dest close together in concurrent
  tasks) declares the keep-alive contract (see the method docstring).
- **Globals / helpers**: `FxtrScope.get_active()`, `ACTIVE_SCOPE`,
  `GLOBAL_SCOPE`, `GLOBAL_MEMORY_STORE`, `get_active_context_marker`,
  `track_builtin_id`.

**Key invariants (must understand to use scopes correctly)**:

- *Complete-subgraph invariant.* A tracked `FxtrID` always lets you
  load the entity and all transitively-referenced entities. The store
  rejects writes that reference IDs not yet tracked, so completeness
  is enforced on the way in. `lift` / `copy_to` / `copy_from_scope` /
  `track_from_scope` exist to extend completeness across scope
  boundaries (see *Data Integrity Invariants* above).
- *Roundtrip guarantee.* `store → load → store` yields the same
  `FxtrID`. `Entity` subclasses must serialize deterministically and
  custom load handlers must re-serialize identically.
- *Child-keeps-parent-alive* is structural — every scope installs an
  `IndependentLocalBridge` ancestor entry holding a clone of its
  parent's main lease. The memory store does not track
  parent/ancestor relationships.
- *Frozen scopes reject all mutations* including `clear_cache`. All
  parents of a frozen scope must themselves be frozen; root scopes
  cannot be frozen.
- *The active scope always has a `$fxtr.ctx` marker*. With
  `provenance_context`, that marker carries a provenance parent and
  every store/load round-trips entities through provenance form (see
  *The `_provenance_parent` Pattern* below).
- *A task group is required to host services* — every CM-entered scope opens
  one; lifespanless scopes (root, lowlevel) have none and can host ancestor
  cap-bridges but not service mounts/resolves.
- *Resolution walks self → parents* for `ServiceKey` matches; the
  `ServiceProxyContext` brokers all lease-minting / arena-forking that
  proxies need.

For the full surface — internal alternative constructors, lifecycle
booleans, lease-passing rules, the cap-bridge / `_known_handles`
structure, deep-fetch source registration, drain semantics,
cross-tree bridging via `bridge_service_from_scope`, and the cache key
shape under provenance — see `data/LOCAL_CODE_OVERVIEW.md`.

### conversion/converters/mashumaro_entities.py

Mashumaro-based entity serialization, decoupled from the core entity system:

- **`DataclassEntity`**: Base for dataclass-based entities. Define one as a frozen dataclass subclass passing `fxtr_type=FxtrID...`; store/load via `EntityRef`. Fields may be primitives, collections, `bytes`, `FrozenFxtrCbor`, entity references, and inline entities (see tables below). Two framework-managed fields are added automatically and you normally don't set them: `fxtr_type` (the type tag) and `fxtr_ctx` (the provenance marker, captured from the active scope at store time). Requirements / behavior worth knowing as a user:
  - **Reference nested entities, don't embed them**: use `Child[T]` / `LinkedEntity[T]` (inline + auto-loaded) or `EntityRef[T]` / `ChildRef[T]` (lazy). A bare `DataclassEntity`-typed field is rejected at store time with a hint to wrap it in `Child[T]`.
  - **`Config` defaults** (inherited by every entity): `forbid_extra_keys` (decode rejects unknown keys) is on. `omit_default` (serialization drops default-valued fields) is *off* by default; an entity can opt in per-class with `class Config(DataclassEntity.Config): omit_default = True`. The framework-managed fields (`$fxtr.type` / `$fxtr.ctx`) survive the opt-in because they are spliced in by `__post_serialize__` rather than serialized by mashumaro (they carry `metadata=_OMIT_FROM_MASHUMARO`).
  - **Tagged decoding** (resolving a stored `$fxtr.type` to a concrete type) is handled by the load machinery in `entity_ref_scope`, not a mashumaro discriminator; `__post_init__` validates the decoded type.
- **Inline entity types**: `Child[T]` (strong, auto-loaded), `LinkedEntity[T]` (weak, auto-loaded), `ChildRef[T]` (strong, lazy), `ChildFxtrID` (strong, raw)
- **Serialization strategies**: `EntityRefSerializationStrategy`, `FxtrIDSerializationStrategy`, `InlineEntitySerializationStrategy`, `FrozendictSerializationStrategy`, `SetSerializationStrategy`, `FrozensetSerializationStrategy`. Raw `bytes` fields are pass-through (`mashumaro.helper.pass_through`) rather than mashumaro's default base64-to-`str` encoding — CBOR has a native byte-string type and the workstation's `wire_escape` tags raw bytes as `["bytes", "<base64>"]`, so frontends decode `bytes`-typed fields directly into a `Uint8Array`.
- **`DataclassEntityLoadHandler`**: Registered handler that uses `BasicDecoder(target_type)` for proper generic parameter substitution, with two-phase deserialization for inline entities.

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

| Type | Serializes To | Notes |
|------|---------------|-------|
| `set[T]` | sorted list | Canonically ordered for deterministic content-addressing |
| `frozenset[T]` | sorted list | Canonically ordered for deterministic content-addressing |
| `frozendict[K, V]` | dict | Type preserved through roundtrip |
| `dict[K, V]` | dict | Native mashumaro support |

**Canonical ordering**: Sets and frozensets are serialized as sorted lists to ensure deterministic byte output regardless of insertion order. This is critical for content-addressing (same data = same hash).

**Two-phase deserialization** for inline entities (`Child[T]`, `LinkedEntity[T]`):
1. Phase 1 (collecting): Deserialize once to collect all inline entity refs (returns `DeferredLoadPlaceholder` sentinels)
2. Load all collected refs
3. Phase 2 (rebuilding): Deserialize again, substituting loaded entities

Users with `__post_init__` validation must handle `DeferredLoadPlaceholder` values during the collecting phase.

### conversion/converters/unknown_entity.py

- **`UnknownEntity`**: Fallback entity wrapping raw CBOR for entities whose type is not known. Accepts any `$fxtr.type`.
- **`LoadAsUnknownByDefault[T]`**: Type alias that bypasses existing handlers and loads as `UnknownEntity`. Useful as a generic type bound: `class MyClass[T: LoadAsUnknownByDefault[Entity]]`.

### services/

The user-facing service-capability layer. Sits on top of the `ServiceRef` / `FxtrServiceImpl` / `_cap_bridges` primitives in `entity_ref_scope.py` and the Cap-IR machinery in `fxtr2/leases/`. Core modules — `service_cap_common.py` (shared dispatch / wire / packet code), `untyped_service.py` (the dynamic-vocabulary layer), `service_dialect.py` (mashumaro-driven type conversion), and `typed_service.py` (`@service_protocol` + `TypedServiceImpl`) — plus the subscription layer built on top (`subscription_types.py` / `subscription_util.py`). See `fxtr2/services/LOCAL_CODE_OVERVIEW.md` for full detail.

**ABC stack**: `FxtrServiceImpl[T]` (in `entity_ref_scope.py`) → `CommonServiceImpl[T]` (locks in the wire shape; abstracts are `_handle_invoke`, `_target_type_annotation`, and `_service_type_id(scope)`) → `{SimpleAsyncServiceImpl[T], TypedServiceImpl[T]}` (siblings; share only `@service_method` discovery via the `walk_service_methods` helper). `UntypedServiceImpl` = `SimpleAsyncServiceImpl[UnknownService]`. `TypedServiceImpl[T]` adds per-method mashumaro conversion driven by a class-level `_service_dialect` and a per-instance `_own_full_type_for_serialization` hook for generic impls.

#### services/service_cap_common.py

Shared dispatch + wire-shape plumbing:

- **`UnknownService`** (alias for `Any`) — phantom target type the wire decoder always produces for `service_ref` leaves; a typed-service layer rewraps with a richer annotation in its proxy/impl shim. The type alias lives in `service_cap_common.py` because the wire decoder produces it for every leaf.
- **`FxtrServiceData`** — recursive type alias for the values that may flow over a service call: primitives, `FxtrID`, `EntityRef[Any]`, `ServiceRef[Any]`, and nested `Mapping[str, ...]` / `Sequence[...]`. Wire encoding: `FxtrID`/`EntityRef` → `IRTagged("fxtr_id", bytes)` for bare IDs, or `IRTagged("fxtr_id_with_prov", prov_parent_bytes)` for layered IDs (FxtrIDs carrying a `_provenance_parent`). `ServiceRef` → `IRTagged("service_ref", {"handle": IRHandle, "type_id": <data-id bytes>})` — `type_id` is bare `bytes` (no envelope, no provenance, no fxtr_arena coverage); it's wire-portable identity, not arena-tracked content, so consumers needing the loadable form dispatch ``$fxtr.type``. Decoding produces a bare `FxtrID` for the bare tag, and a layered `FxtrID(unwrapped_data, provenance_parent=prov)` for the with-prov tag (via `FxtrScope.embed_as_provenance_parent` on the receiving scope); the `service_ref` `type_id` decodes to a bare `FxtrID(raw_data_id=...)`. `ServiceRef`s decode to `ServiceRef[UnknownService]` (the sender's specific target-type annotation is dropped on the untyped path).
- **`CommonServiceImpl[T]`** — abstract base for every service impl strategy; concrete subclasses implement `_handle_invoke(method_name, *args, **kwargs)` (per-call entry for *user* methods), `_target_type_annotation()` (the type the resulting `ServiceRef[T]` carries), and `_service_type_id()` (mints / looks up the runtime type identity in the active mount scope's main arena, returns the corresponding `FxtrID`). Provides `async _mount_as_fxtr_service(scope)` (no `task_group`) that attaches a **framework lifespan** via `await scope.attach_lifespan_to_scope(...)`: the lifespan runs the user `_fxtr_mount_lifespan(scope)` setup (authorized + with `scope` active), opens the impl's own dispatch task group nested inside it, builds a `CommonServiceImplHandle(self, scope, dispatch_tg, lifespan_state)`, registers it via `scope.mount_lowlevel_cap_as_service(handle, target_type, service_type_id)`, and yields the `ServiceRef`. Also provides the no-op-default `_fxtr_mount_lifespan` async-CM hook (yields per-mount lifespan state), the `_fxtr_get_mount_scope()` / `_fxtr_get_lifespan_state()` / `_fxtr_get_input_scope()` dispatch accessors (read the per-call `(handle, input_scope)` record from the module-level `_CURRENT_DISPATCH_CONTEXT` contextvar; raise outside a dispatch), and the concrete `_handle_reserved_invoke(method_name, *args, **kwargs)` helper that handles the reserved `$fxtr.*` wire-method-name prefix: `$fxtr.ctx` returns the active scope's `get_context_marker_id()`; `$fxtr.type` returns `self._cached_service_type_id`; other `$fxtr.*` raise `IRException(category="UnknownMethod")`. The dispatch is centralized in `_run_user_method`: a wire `method_name` matching `FXTR_FRAMEWORK_METHOD_PREFIX` routes to `_handle_reserved_invoke` and bypasses `_handle_invoke` entirely, so subclasses can't accidentally shadow / forget the framework paths.
- **`CommonServiceImplHandle`** — `IRHandleLocalImplBase` subclass bound to an `(impl, scope, dispatch_tg, lifespan_state)` tuple (per-mount state, including the yielded lifespan state, lives on the handle — mounting one impl into two scopes yields two independent handles). `_run_request_async` validates the `fxtr_call` packet shape and spawns `_dispatch_body` into the handle's own `dispatch_tg` (nested in the framework lifespan, not the mount scope's task group). Each call builds a per-call scope *pair*: a lifespanless **input scope** (parent: `GLOBAL_SCOPE`; owns the wire-input leases + decoded args; closes with the call unless the impl retains `_fxtr_get_input_scope().clone_authorization()` — the non-mount-pinning retention surface) and the **call scope** via `open_with_explicit_leases` (its main token *derived from* the mount scope's + the input scope's, so the mount-scope drain waits for in-flight calls), re-authorizes the mount scope for the call's duration, binds `_CURRENT_DISPATCH_CONTEXT`, invokes `impl._handle_invoke`, ships the result over an `anyio` memory object stream, drains on `async with` exit. Python exceptions become `IRException(category=type(exc).__name__)`; user-thrown `IRException` passes through unchanged.
- **`invoke_service_method(ctx, method_name, *args, **kwargs)`** — the proxy-side entry point: validates + encodes args, forks an input arena if any `FxtrID` / `EntityRef` leaves are referenced (ServiceRef `service_type_id`s do *not* contribute — they're wire-portable identity, shipped as raw bytes inside the `service_ref` payload), mints per-key leases via `ctx.mint_lease_for_key(ServiceKey)`, builds the `IRTagged("fxtr_call", …)` packet, dispatches, decodes the return, installs caller-side bridges via `_install_returned_resources`, returns the decoded `FxtrServiceData`. `UnknownServiceProxy.__getattr__` is just `functools.partial(invoke_service_method, ctx, name)`. Typed proxy layers wrap with per-method conversion before/after.
- **Wire shapes**: input is `Call(method_name, IRTagged("fxtr_call", {"args", "kwargs", "fxtr_arena", "leases"}))`; return is `IRTagged("fxtr_return", {"result", "fxtr_arena", "new_indep_leases", "new_dep_leases"})`. The `fxtr_arena` slot is a self-describing Mapping `{"handle": IRHandle, "tracked_ids": [<tagged FxtrID>, ...], "initial_precomp": [<precomp entries>]}` — the precomp closure travels with the handle so the receiver can sync-decode prov-bearing FxtrIDs in args / return without a `load_flat` round-trip. When args reference any `FxtrID`/`EntityRef`, the proxy forks a sub-arena off the home scope's main arena, wraps it with `ArenaIRHandleAdapter`, computes a precomp closure over the tracked IDs, and ships the assembled packet plus a lease on the home scope's token. When args contain `ServiceRef`s, the proxy mints a lease for each `ServiceKey`'s token via the home scope's bridge and adds it to `leases` (deduped by subject token). A call whose only ID-bearing args are ServiceRefs (no other FxtrID / EntityRef leaves) doesn't carry an `fxtr_arena` at all. The receiver builds the input + call scope pair, **bridges `fxtr_arena` into the *input scope* first** (so `embed_as_provenance_parent` can resolve any prov-bearing IDs in the args), then decodes args against the input scope (each decoded id is also sync-`lift`ed into the call scope, so strict same-scope ops on args keep working), then registers each ServiceRef arg's `(handle, type_id)` as a `ServiceKey` on the existing case-3 ancestor entry on the input scope (the call scope reaches both through its input-scope parent). Returned dependent refs ship the same `x_tokens` set as before the split (mount + arg tokens); their server-side chain converters re-mint `Lease[input.main]` per convert from at-rest key/GLOBAL leases plus the caller's arg leases (two-hop), so no standing server-side pin is introduced. The proxy's return-path mirror is the same: bridge the return arena into the home scope, decode the result, install caller-side handle bridges.
- **Chain-converter return path**: tokens whose dispatch-scope bridge is `Independent*` ship as `new_indep_leases` (concrete `Lease` rides directly on the wire); tokens whose bridge is `Dependent*` (or absent for `call_scope.main`) ship as `new_dep_leases` entries (`{token, key_lease, converter, input_tokens}`). `_make_chain_converter_handle` snapshots the dispatch-scope bridge into a `_ChainConverterHandle` keyed on `K_call`; the caller-side `_install_returned_resources` builds a `DependentPipelinedBridge` per entry via `_build_chain_bridge`, threading the caller's own bridges for non-base input tokens. Each call dispatches the wire converter and pipelines the result.

#### services/untyped_service.py

The dynamic-vocabulary layer:

- **`@service_method`** — decorator that tags a `SimpleAsyncServiceImpl` method as exposed; untagged methods are not reachable through `ServiceRef[UnknownService]`.
- **`SimpleAsyncServiceImpl[T]`** — `CommonServiceImpl[T]` whose `_handle_invoke` walks the MRO via `_service_exposed_methods` looking for `@service_method`-tagged methods, raising `IRException(category="UnknownMethod")` on misses. Subclasses still implement `_target_type_annotation` and `_service_type_id`. Generic on `T` so future variants can pin a richer target type.
- **`UntypedServiceImpl`** — `SimpleAsyncServiceImpl[UnknownService]` with `_target_type_annotation` returning `UnknownService` and `_service_type_id` returning `scope.track_builtin(FxtrID.builtin("untyped_service"))`. Override methods, tag with `@service_method`, mount via `await scope.mount(impl)`.
- **`UnknownServiceProxy`** — registered against `UnknownService` in the service-resolver registry. `__getattr__` returns `functools.partial(invoke_service_method, self._ctx, name)`; dunder names raise `AttributeError` so introspection works. Exposes an async `service_type_id()` method that wire-dispatches `$fxtr.type` and returns the impl's runtime type identity in *loadable* form — bridged into the home scope's main arena via the ordinary return-arena path, so callers can follow it through `EntityRef.load` (analogous to `UnknownEntity` letting you load any entity and inspect its type tag without specific deserialization). Advanced callers that only need the type_id as an opaque hash — no loading required — can read `proxy._ctx.service_type_id` directly: same `FxtrID`, no wire round-trip, but not tracked in the home scope. Untyped callers reach the framework `$fxtr.ctx` / `$fxtr.type` paths via `await getattr(proxy, "$fxtr.ctx")()` / `await getattr(proxy, "$fxtr.type")()` — the existing `__getattr__` accepts the strings unchanged.

#### services/service_dialect.py

The typed-service mashumaro dialect:

- **`FxtrServicesDefaultDialect`** — `mashumaro.dialect.Dialect` subclass with strategies for `EntityRef[T]` and `ServiceRef[T]` (encode-strips the type arg / decode re-attaches `T`), `FxtrID` (passthrough), `frozenset` / `set` / `frozendict` (collection conversions), `_UseRemoteDefault` (passthrough so the sentinel survives encode for the dispatcher to identity-strip), and `FxtrServiceData` (passthrough — opt-out of conversion for slots typed as untyped wire data). `tuple` is handled by mashumaro's built-ins. Subclass to extend.
- **`get_basic_encoder` / `get_basic_decoder`** — `(annotation, dialect, hooks)` codec cache. `pre_decoder_func` / `post_encoder_func` ride through to mashumaro for the single-field result-wrapper unwrap.
- **`encode_to_service_data(value, annotated_type, *, dialect=FxtrServicesDefaultDialect)` / `decode_from_service_data(value, annotated_type, *, dialect=FxtrServicesDefaultDialect)`** — user-facing converters that expose the same per-annotation conversion the typed-service layer uses for method args / returns. Useful from inside service methods that take `payload: FxtrServiceData` and decode the payload against a runtime-chosen annotation. Encode-side validates the result against the `FxtrServiceData` vocabulary; decode-side validates the input. `FxtrServiceData` and `NotFxtrServiceDataError` are re-exported from this module for convenience.

#### services/typed_service.py

The typed-service layer:

- **`@service_protocol(dialect=..., service_type_id=..., service_type_validator=..., allow_protocol_subtypes=...)`** — decorates a `Protocol` class. Walks `async def` methods (full MRO, first-wins, each reported with its defining class), generates per-method `_Args[*type_params]` / `_Result[*type_params]` plain `@dataclass`-frozen wrappers (parameterised by the Protocol's TypeVars) for codec-driven TypeVar substitution, and registers a resolver in the service-resolver registry. Type-id introspection is opt-in per Protocol via `@proxy_alias_method("$fxtr.type")` on a regular Protocol method (typically named `service_type_id`); the method's return annotation drives the typed decoder (`FxtrID` rides through unchanged; `EntityRef[T]` is re-attached on decode by the dialect's strategy). No reserved Protocol attribute. `allow_protocol_subtypes=True` marks the Protocol as a subclassable base: decorated subclasses register their own resolvers (most-specific wins at resolve; undecorated subclasses fall back to the base's resolver and method vocabulary, with inheritance-made TypeVar bindings still honored via `_proxy_parameterization`), and inherited methods get genuine TypeVar substitution — annotations are resolved against the defining class and mapped into the subclass's vocabulary by composing `types.get_original_bases` parameterization maps (keyed on TypeVar objects; an unparameterized base hop leaves base TypeVars unsubstituted → bound fallback). The same defining-class machinery covers `TypedServiceImpl` subclasses inheriting tagged methods from generic impl bases.
- **`@proxy_alias_method(wire_name)`** — Protocol-method decorator that overrides the wire method-name string the proxy ships when the method is invoked, while leaving the Python attribute name unchanged. The override rides as `__fxtr_proxy_wire_name__` on the function; `_build_method_wrappers` reads it into `_MethodSpec.wire_name`; `_make_proxy_method` passes `spec.wire_name` (not the Python name) to `invoke_service_method`. Generic — any non-empty wire name works, but the only end-to-end supported targets today are framework-reserved `$fxtr.*` paths (where the receiver's `_handle_invoke` prefix-check intercepts before user dispatch). Typical usage: declaring `async def service_mount_context(self) -> FxtrID` on a Protocol, decorated with `@proxy_alias_method("$fxtr.ctx")`, to surface the mount-scope context marker via a Pythonic name.
- **`TypedServiceImpl[T]`** — `CommonServiceImpl[T]` subclass. Subclasses tag methods with `@service_method` and supply `_target_type_annotation` / `_service_type_id`; generic impls override `_own_full_type_for_serialization` to return a parameterised form (e.g. `MyImpl[int]`) so their TypeVar-bearing method annotations can be substituted at mount time. Class-level `_service_dialect` defaults to `FxtrServicesDefaultDialect`.
- **Wire shape.** Both proxy and impl drive their wire shape via `inspect.BoundArguments`. Proxy: `sig.bind(...).apply_defaults()` → encode the `_Args` wrapper instance → strip `USE_REMOTE_DEFAULT` entries → hand the resulting field-keyed dict to a fresh `BoundArguments(sig_no_self, encoded)` → ship `.args` / `.kwargs` directly via `invoke_service_method`. Impl mirror: `impl_sig.bind(*wire_args, **wire_kwargs)` → decode the wrapper from `bound.arguments` → strip `USE_REMOTE_DEFAULT` (and an EMPTY variadic entry — the wrapper defaults it to `()` when the wire omits it, and after a stripped defaulted param `BoundArguments` would otherwise spill it into an illegal keyword) → call `method(*new_bound.args, **new_bound.kwargs)`. The wrapper is purely a per-side construct for TypeVar substitution; field names mirror the user's parameter names (no reserved names) and never appear on the wire as a structure. Cross-side variadic-rename tolerance is automatic because variadic content travels as bare positional/keyword args.
- **`USE_REMOTE_DEFAULT`.** Top-level named-parameter sentinel meaning "let the impl's default fire". The dialect's passthrough keeps the sentinel through encode; the dispatcher strips entries identity-equal to it before the `BoundArguments` split. Sentinels nested inside `*args` / `**kwargs` content / collections are caught by the underlying `FxtrServiceData` vocabulary check (`NotFxtrServiceDataError`); no extra typed-layer validation is needed. Layouts `BoundArguments` can't faithfully represent (e.g. `USE_REMOTE_DEFAULT` mid-positional-only) surface as `MalformedArgs` `IRException` from the impl side's `bind` step.

#### services/subscription_types.py + services/subscription_util.py

The explicit-subscription layer:

- **`subscription_types.py`** declares the three `@service_protocol` primitives: `Subscriber[T]` (`on_event(payload) -> None` — no return value, no in-band "stop" signal), `UnsubscribeHandle` (`unsubscribe()` — best-effort; doubles as the release signal letting the source drop its bridge to the subscriber), and `EventSource[T]` (`subscribe(subscriber) -> handle`). Contract: anyone subscribing for a duration must unsubscribe when done (e.g. in a `finally`); sources must drop subscriptions whose `on_event` raises (crash backstop). No type ids are declared here — type identity belongs to impls. `Subscriber` / `EventSource` are marked `allow_protocol_subtypes=True`: protocols that want to *be* event sources subclass `EventSource[<payload>]` and inherit `subscribe` with the substituted typed param. `T` is unbounded with a documented contract — it must *serialize to* `FxtrServiceData` under the dialect in play (wire conversion is asymmetric: sender's annotation drives encode, receiver impl's drives decode); `SubscriberHub` keeps its `FxtrServiceData` bound.
- **`subscription_util.py`** ships `SubscriberHub[T]`, the reusable source-side broadcast helper. The hub owns no scope: sync `publish(value, origin_scope=...)` captures a lease on the payload's origin scope per pending event (default: the active scope; `None` for entity-free payloads, usable from unauthorized contexts), and each delivery runs in a transient per-event scope parented on `{sub_scope, origin}` that consumes the lease — zero-copy delivery that works even after the origin drained, with the lease held only until the event is delivered or superseded. Per subscription there are two isolated scopes/tasks: a worker (`sub_scope`, subscriber bridge + delivery loop; `"coalesce"` latest-value skip-ahead / `"queue"` in-order FIFO modes) and a holder (`hold_scope`, the mounted unsubscribe-handle impl, parked in the scope's natural drain — drain completion is handle-drop detection). Subscriptions pin nothing of the source's: hold the source ref to keep the source, hold the handle to keep your subscription. `subscribe(..., seed=..., seed_origin=...)` seeds caller-chosen initial values ahead of the live stream (no-await no-dup-no-miss window); no dedup (every `subscribe` call is an independent subscription with its own handle). Teardown is forceful; any delivery failure kills the subscription (crash backstop — subscribers resubscribe for a fresh view). `aclose()` flushes all live subscriptions (unbounded; bounded close = cancel the owning task group) then tears everything down, after which `publish`/`subscribe` raise. Defines its own namespace UUID + `define_type` id for the handle impl.

### common/channels.py

Output channels — the standard interface for streaming state out of a computation across RPC, built on the subscription layer (`fxtr2.services`). A channel is a **tree** of distinct reader/writer services backed by one in-process state object:

- **Conceptual model.** A channel = a **tag** (an `EntityRef` whose *type* identifies the kind of stream; fetched once via `get_tag`, not carried in the event stream) + a replace-only **status** (`S`) + a sequence of **messages** (`V`) + a **final value** (`R`) that *replaces* the message log when the channel finishes. Channels form a tree: a writer can `spawn_subchannel` (minting a child keyed by a fresh string), a reader can `open_subchannel_reader(key)` to reach a direct child. **Channel payloads carry only data** (entity refs + scalars), never service refs — a sub-computation is exposed by emitting its subchannel *key*, which a consumer recognizes by convention. A child is reachable **iff its parent is open**; once a parent finishes/aborts, its children are reclaimed (the parent's final value is the complete record of them).
- **`ChannelTag[S, V, R]`** — abstract `Entity` base for tag types; a concrete tag (`class MyTag(DataclassEntity, ChannelTag[Status, Msg, Res], fxtr_type=...)`) pins `S`/`V`/`R`. `infer_channel_types` (a cached `get_original_bases` walk) recovers them at runtime; the typed helpers (below) use this to type their returned refs so `emit`/`finish`/`iter_channel` typecheck. Generic/unparameterized tags can't be inferred.
- **State + event dataclasses.** The cheap replace-only **state** stream delivers `ChannelStateEvent[S] = ChannelState[S] | ChannelAbortedBeforeStart` (`ChannelState` = `status` + lifecycle `kind` of open/complete/abort; `ChannelAbortedBeforeStart` = just a `reason`, for a channel aborted before it ever started — so it has no tag/status). The **full** stream is `ChannelUpdate[S, V, R]` — `OpenChannelSnapshot` / `CompleteChannelSnapshot` / `AbortedChannelSnapshot` / `ChannelUpdateEvent` (a **batch** of messages + status + `replace_history`) / `ChannelCompleteEvent` / `ChannelAbortedEvent` / `ChannelAbortedBeforeStart`. All discriminated by `kind` (mashumaro decodes directly). No event carries the tag; every started-channel event carries the current `status`. Wire form = the mashumaro encoding of these dataclasses; the manually-built seed envelopes are pinned byte-equal by test.
- **Service protocols.** `FxtrChannelWriter[T, S, V, R]` (`start` / `emit` / `finish` / `spawn_subchannel` / `abort`) handed to producers; `FxtrChannelReader[T, S, V, R]` (`get_tag` / `subscribe_full` / `subscribe_state` / `peek_full` / `peek_state` / `open_subchannel_reader` / `subscribe_debug_subchannels`) handed to consumers. All writers share the fixed `CHANNEL_WRITER_TYPE_ID`, all readers `CHANNEL_READER_TYPE_ID` (namespace `FXTR_STREAMS_NS`) — per-stream identity lives in the tag's entity type (how frontends dispatch renderers). The frontend reaches `subscribe_full` / `subscribe_state` / `subscribe_debug_subchannels` via the workstation inbox's `subscribe_to(source, method)` (the method-name arg).
- **Debug subchannel feed.** `subscribe_debug_subchannels` delivers a `SubchannelOpened{key, name}` per subchannel already spawned (the seed, in spawn order), then one per subsequent spawn — a schema-blind way to enumerate a channel's subchannels (the primary path is app convention: emit the key into the message stream + `open_subchannel_reader`). Append-only over the channel's open life; independent of `start` (it concerns children, not the tag). Closed when the channel finishes/aborts (children are then gone, so subscribing a terminal channel's feed errors rather than serving stale keys); the full/state hubs stay open to serve terminal snapshots.
- **Engine (private).** `_ChannelTree` (created on a mount scope by `create_root_channel`'s lifespan) owns the node registry + a task group hosting every node's three `SubscriberHub`s (full = queue, state = coalesce, debug-subchannels = queue, seeded from the node's `subchannel_log`) and residency-scope holders. Each `_ChannelNode` has a `durable_scope` (tag + final + terminal status; lives while its parent is open) and a `data_scope` (current log + status; swapped on `replace_history`, released on terminal). The `_WriterImpl` / `_ReaderImpl` are **thin untyped data-shuffler** `TypedServiceImpl`s — the *typed proxies* on each end do all encode/decode, so the impls just forward pre-encoded `FxtrServiceData`, copy entity closures into the right scope (rejecting any `ServiceRef` leaf — data-only), and publish envelopes. Impls mount on the per-call scope and ride the standard service-return path; the writer's `_fxtr_mount_lifespan` teardown aborts-if-open (release-without-finish net). Finish/abort run an explicit teardown walk reclaiming children (closing all three hubs); `peek_*`/`get_tag` block until the channel starts.
- **Helpers.** `create_root_channel(tag_type=...)` → a `(writer, reader)` pair (precisely typed from `tag_type`, or `_U`-placeholder if omitted); `start_channel_and_infer_types(untyped_writer, tag, initial_status)` starts a spawned writer and retypes it from the tag; `infer_channel_reader_type(reader, tag_type)` retypes a wire-received reader; `decode_channel_update` (dynamic per-event decode) and `iter_channel(reader, scope)` (async CM yielding decoded `ChannelUpdate`s; copies event entity closures into `scope` during delivery). The channel system itself uses **no** `bridge_service_from_scope` — nothing live flows through a channel.

Tests: `tests/test_common/test_channels.py`. Design + the frontend/demo migration plan: `plans/phase3/stage13post1_output_channel_redo/`.

### fxtr_context_api.py

User-facing API for working with persistent namespaces. Two-level ABC
hierarchy — `FxtrNamespace` (pin / unpin / retrieve) → `FxtrQueryableNamespace`
(adds indexer registration, `iter_matches`, and `sql()` against named
`QueryView`s) — with two concrete implementations: `SQLiteFxtrNamespace`
(SQLite-backed, supports `isolate_scope` and `read_only` modes) and
`TsvFxtrNamespace` (a tsv namespace: pin mappings in git-tracked TSV files,
backed by a repo-level entity store). Each namespace owns its own scope and
is used as an async context manager. See `data/LOCAL_CODE_OVERVIEW.md`
for method-level details.

### memory_arena.py

The global memory store with lease-backed lifetime management. All
scopes share a single `MemoryStore` instance; callers mint
lifetime tokens (`UnconditionalLocalLifetime` /
`DerivedLocalLifetime`) from `fxtr2.leases.lease_types` and register
them with the store. The store is a *subscriber*: it allocates a
per-token row and hooks an end-callback for GC, but holds no lease on
the token, caches no grant, and tracks no parent/ancestor
relationships. Each `MemoryArena[B]` is a view into the store
parameterised by a lifetime brand; tracked node IDs live in the
arena's liveset.

**Sync-Dep Closure invariant**: any token with access to a node also
has access to its sync-discoverable deps (`unwraps_to`,
`data.ctx_node`, `data.backstep_node`) — recursively. So a sync query
returning a dep ID always returns one the caller can immediately
track. Maintained by routing every gain-of-access through controlled
helpers that strict-assert dep access at the grant site.

**Same-store sentinels**: when a foreign-arena attach has a `MemoryArena`
source bound to this same `MemoryStore`, the store auto-detects the
case and stores a `_SameStoreSentinel(target_token=Y)` rather than
forking the source — loads recurse through `Y` directly. Callers don't
see the distinction.

For details on `MemoryStoreNode`, `_BridgeRecord`,
`LifetimeTokenData`, the bare `MemoryStore` API, and the per-method
shape of `MemoryArena`, see `data/LOCAL_CODE_OVERVIEW.md`.

### sqlite_arena_migrations.py

Database schema migrations for upgrading SQLite databases between versions:

- **Version tracking**: Uses SQLite's `PRAGMA user_version`
- **Automatic migration**: `init_database()` runs migrations when needed
- **Table definitions**: Uses SQLAlchemy table definitions from `sqlite_arena.py` for consistency
- Migrations use the "rename old table, create new, copy data, drop old" pattern since SQLite doesn't support `ALTER CONSTRAINT`

See `plans/phase2/stage7_sqlite_dep_duplication_fix.md` for details on the migration system.

### store_indexing/

Namespace index construction and querying. Enables efficient queries on pinned entities by their field values using datalog-style variable unification. See `store_indexing/LOCAL_CODE_OVERVIEW.md` for detailed documentation.

- **`indexing_types.py`**: Core types — `IndexerColumnType` enum, `Indexer` abstract base class, `IndexerConstraint` and `BindingConstraint` dataclasses, `ParsedViewColumn`/`ParsedView` for `sql()` backend handoff, `QueryView` for `sql()` view definitions, `IndexablePinningBackend` abstract interface, `NamespaceIndexMetadata` entity for export/import of indexer registrations
- **`indexers.py`**: Path element hierarchy (`PathElement` base with `PathKey`, `PathIndex`, `VarMarker`, `DerefMarker`, `TypedDerefMarker`), concrete indexer implementations (`TypedPathValueIndexer`, `TypedPathExistsIndexer`), `WellKnownBuiltinIndexer` type alias, and `unpack_entity` implementations with async path walking. `unpack_entity` takes a `FxtrID` and loads frozen CBOR internally via `scope.load_frozen_cbor()`. Path walking operates on `FrozenFxtrCbor` (frozendict/tuple). Two-axis strictness: indexer-level (schema) and TypedDerefMarker-level (type filtering).
- **`path_parse.py`**: Hand-written parser for path expressions like `"!(as '!type').turns[*: int].score: float"`. Supports unnamed (`*`) and named (`$var`) variables, `!(as ...)` strict and `!(as? ...)` lax type filters.
- **`sql_expr.py`**: SQL expression parsing/validation/rewriting using sqlglot — `parse_condition()` validates user-provided condition strings against a safe function allowlist, rejects injection attempts, and rewrites `$var` → `_VAR_var`. `validate_user_query()` provides lightweight syntax checking for `sql()`. Also provides `COLUMN_TYPE_TO_SQLITE` mapping and `SAFE_FUNCTIONS` allowlist.
- **`query_gen.py`**: SQL query generation — `generate_iter_matches_query()` for `iter_matches` and `generate_direct_sql_query()` for `sql()`. Both share the same variable-unification/equijoin pattern but `generate_direct_sql_query` adds CTE wrapping, value column renaming, entity LEFT JOIN, and db-alias-qualified table names.
- **`index_computer.py`**: `IndexComputer` helper — bridges between the store (FxtrIDs + SQL) and indexer logic (loaded entities + scopes). Used as an async context manager with a persistent isolated scope that caches loaded indexer objects across calls (avoiding repeated deserialization). `get_indexer_types()` returns column schema for an indexer. `compute_indexer_entries()` takes separate entity and indexer arenas, runs all indexers on all entities, and returns `(key_tuple, value)` results. The provided arenas are not modified. Tied to `SQLiteFxtrSession` lifetime (entered inside the session's lifecycle handler).

### sqlite_store/

High-performance sqlite3-based SQLite storage backend with append-only segment file storage (v6). Entity data (CBOR blobs + metadata) is stored in append-only segment files (`{db_path}.data2.shards/seg_{N:08d}.bin`) for fast, synchronous reads via `os.pread()`, while SQLite stores metadata (entity table, deps, namespaces, pins, sessions) plus segment locations (`seg_id`, `seg_offset`, `seg_length`).

**Architecture** (see `sqlite_store/LOCAL_CODE_OVERVIEW.md` for full details):
- **`entity_blob.py`**: Entity blob encoding/decoding for segment files
  - Fixed 24-byte header + fxtr_id + cbor + metadata sections
  - Metadata contains deps list and closed precomp map (all transitive dependencies in one blob)
  - 5-element precomp entries: `[fxtr_id_bytes, unwraps_to, ctx_node, backstep, seg_offset_or_none]`
  - `encode_blob()`, `decode_*()` functions, `build_closed_precomp_map_for_segments()` with caching

- **`segment_io.py`**: Low-level segment file I/O
  - `SegmentManager`: LRU cache of open fds, `read_record()` (with length), `read_record_at()` (offset only)
  - `append_record()`, `flush_segment()` (platform-aware: `fdatasync`/`F_FULLFSYNC`), `truncate_to_cursor()`

- **`lmdb_blob.py`**: LMDB blob format (used only by v5→v6 migration)

- **`sqlite_util.py`**: Connection pool infrastructure
  - `ConnectionWorkerPool`: Persistent worker threads holding DB connections. Submitted via shared queue.
  - `ConnectionWorkerPoolGroup`: Paired read/write worker pools + `SegmentManager` + `segments_dir`
  - `ConnectionPool` / `ConnectionPoolGroup`: Original per-call dispatch pools (kept for flexibility)

- **`sqlite_ops.py`**: All SQL strings and sync database operations
  - Module-level constant SQL strings for better SQLite caching
  - **Schema v6**: Segment file storage. `segment_metadata` table, `seg_id/seg_offset/seg_length` on entity table
  - **Single-transaction write**: `bulk_copy_commit()` with `flock(LOCK_EX)` on `write.lock` — allocates IDs on the fly, writes blobs to active segment, flushes before SQLite commits (crash-safe)
  - Segment rotation when active segment exceeds 512 MB (configurable)
  - Functions for: sessions, namespaces, pins, arenas, garbage collection, bulk copy

- **`sqlite_index_ops.py`**: Index-related SQL operations (leaf module — does not import `sqlite_ops`)

- **`sqlite_namespace.py`**: Public API classes
  - `SQLiteFxtrSession`: Session management with lifecycle and garbage collection. Supports `read_only=True` for invisible, zero-write sessions (no session row, no session files, no GC, `mode=ro` connections, no write pool).
  - `SQLiteNamespaceBackend`: Namespace with named pins implementing `IndexablePinningBackend` and `DeepFetchable[B]`
  - `_with_validated_session_lease`: Decorator on backend/arena methods — `validate_lease_grant` then `with lease.take() as held` for the full call (including async bodies / async generators), so the capability stays live until the method finishes.
  - `SQLiteNamespaceArena`: Lightweight, read-only arena implementing `ReadableArena`
    - **Synchronous segment reads**: `os.pread()` on cached fds — thread-safe, no dispatch overhead
    - Segment location cache with same-segment fast path (precomp offset → skip SQLite lookup)
    - Precomp dicts populated from segment blob metadata (closed precomp maps)

- **`sqlite_migrations.py`**: Schema migrations (v2→v3, v3→v4 error, v4→v5, v5→v6)

**Key optimizations:**
- **Thread-safe pread reads**: `os.pread()` on cached fds — no seek pointer contention, no thread dispatch
- **Closed precomp maps**: Single blob read provides all metadata for entity + dependencies
- **Same-segment fast path**: Precomp entries with non-null 5th element skip SQLite lookups
- Constant SQL strings cached by SQLite for faster repeated execution
- Read/write connection pools allow concurrent reads in WAL mode
- Single-transaction bulk copy with crash safety (flush before commit)

### file_util/

Shared file utilities:

- **`lock.py`**: File locking utilities using `fcntl.flock()`:
  - `async_file_lock_acquire()`: Async blocking lock with inode verification and retry
  - `sync_file_lock_acquire_nonblocking()`: Sync non-blocking lock attempt (raises `LockNotAcquiredError` if held by another process)
  - Used by both `sqlite_arena.py` (session file locking) and `tsv_namespace/` (dirty mark system)

### system_entities.py

Built-in entity types:

- **`FallbackContextMarker`**: D-node payload for the lazily-minted fallback c-node used by `store_provenance="auto"` when child IDs carry provenance but the scope has no explicit `provenance_context`. Has `scope_name: str` (taken from the scope's inferred / supplied `name`) and a hardcoded `value: FxtrID = "."` so the resulting c-node unwraps to the ground node.
- **`FxtrBasicType`**: A basic type definition with namespace UUID and type name (for user-defined types)
- **`define_type(namespace_uuid, type_name)`**: Helper function to define and register a new type ID

### legacy_compat/

Compatibility layer for storing fxtr v1 entities in fxtr v2's system:

See `legacy_compat/LOCAL_CODE_OVERVIEW.md`

### tsv_namespace/

The tsv namespace backend: a namespace defined via git-tracked TSV files, allowing pin mappings to be version-controlled alongside code. Uses TSV files for pin name → FxtrID mappings, with a shared repo-level entity store (SQLite) for the actual entity data.

See `data/tsv_namespace/LOCAL_CODE_OVERVIEW.md` for detailed documentation.

### layered_file_store/

Filesystem convention for representing "a layer of new fxtr entities on top of a stack of parent layers, with anything else assumed to live in a runtime-configured remote entity store." Bridges fxtr2 into and out of nix-style content-addressed derivation outputs (see `plans/phase3/stage11_nix_layers/`).

A layer directory holds `layer_format.json` + `pins.json` + `parents.json` + `data/<id>.cbor` files + `precomp/<id>.json` files. **Pins are layer-local** (a child's reader doesn't see parent pins); **data walks are transitive** through parents (the search set expands dynamically as deeper layers are touched). Pointers to entities that the layer asserts live in the remote entity store get a precomp file but no data file; readers resolve them through the injected `(DeepFetchable, Lease)` pair at load time.

Two public classes — `LayeredFileStoreReader` (an `FxtrReadNamespace` over a layer + its parents; the reader's scope is marked **not deeply fetchable**, remote resolution happens entirely inside `LayeredFileArena`) and `LayeredFileStoreIsolatedWriter` (write-only async CM with `pin_to_this_layer` / `mark_as_remote` / `preload_data`, plus an optional `validate_on_close` pass). The writer holds no backend lifetime / internal scope — it walks the caller's `from_scope` via `copy_to_new_arena` and talks to the caller-supplied remote directly.

`LayeredFileArena` holds **no leases** — only a `Grant[B]` for its brand; its optional same-branded `source` (a `DeepFetchable[B]`) is reached by threading the *caller's* incoming lease, so the arena never pins its own lifetime. The `pin_arena` seam exposes this lower-level: `open_pin_arena` / `open_seed_arena` / `arena_over_search_set` **return** a `LayeredFileArena` the caller mounts (via `lazily_attach_from_arena`) and whose lifetime the caller governs — used by the reader and by `fxtr_nix_runner`'s flake manager to keep built entities lazily loadable for the manager's lifetime. `LayeredStoreDeepFetchable` is a by-id `DeepFetchable` over a whole (multi-layer) set, fully expanded at construction, for registering as a scope-level deep-fetch source (serving). **Assumption:** layer dirs are neither deleted nor modified while any reader/arena/source over them is in use.

See `data/layered_file_store/LOCAL_CODE_OVERVIEW.md` for detailed documentation.

### leases/ — Lease-based lifetime primitives + Cap-IR

Defines fxtr's lease / grant / token abstraction and the abstract Capability-IR (Cap-IR) that sits
on top of it. A lease is a clonable, discardable holder-side right to use a resource; the owner
of the backing grant can revoke it synchronously to break all outstanding leases. Cap-IR layers a
transport-independent interface for capabilities (`IRHandle`, `IRFuture`, `IRValue`) over these
primitives so that local and remote resources look identical to application code.

See `fxtr2/leases/LOCAL_CODE_OVERVIEW.md` for full details, including the lease-transfer
convention, `UnconditionalLocalLifetime` vs `DerivedLocalLifetime`, the local Cap-IR
implementation shape (shared `PendingResultBox` / `LocalPendingResultBox` + path-walked
`IRFutureLocalImpl`), and
the cap-bridge taxonomy (`IndependentLocalBridge` / `DependentLocalBridge` /
`IndependentPipelinedBridge` / `DependentPipelinedBridge` in `lease_bridges.py`) — used both
by `data/cap_ir_arena.py` for at-rest store state and by scope cap-bridges for the
lease-minting machinery shared between mounted handles.

### rpc/ — Cap-IR lowering

Turns two Cap-IR endpoints into a pair of communicating processes over a bidirectional message
stream. Implements the "LeasedCapIR lowering protocol v1" — push/pull/resolve/reject/watch/
closed/break/discard/unpull/abort messages, with import/export tables and bounce-back identity
for tokens, grants, handles, and futures. The connection operates on `RPCMessage` objects and
is transport-agnostic.

`json_serdes.py` provides a Cap'n'Web-shaped JSON wire codec that converts
`RPCMessage` / `Expression` / `IRStatic` trees into JSON-serializable values and back, plus
a `wrap_json_stream` helper that adapts an `ObjectStream[JSONTypes]` transport into the
`ObjectStream[RPCMessage]` that `LeasedCapRPCConnection` expects. A byte-level framer (newline,
length-prefix, …) sits underneath this helper as a separate concern.

`scope_bridge.py` adds the user-facing `open_rpc_scope` async CM. Given a transport and an
already-mounted `ServiceRef`, it bootstraps the connection (each side sends a one-shot
`fxtr_export_main_service` packet carrying its main handle + service_type_id + an arena that
contains the type_id), opens a fresh child `FxtrScope`, installs the peer's main
service into it as an `IndependentLocalBridge`, and yields an `RPCSession(scope,
remote_service)`. The conn drain inside the CM is the rendezvous: each side's drain naturally
waits for the peer to release its grant exports, so symmetric two-party setups don't need
explicit coordination events. See `plans/phase3/stage8c_user_caps/part7_plan.md`.

See `fxtr2/rpc/LOCAL_CODE_OVERVIEW.md` for architecture details, and
`plans/phase3/stage8_lease_system/overview.md` + `phase_d_plan.md` for design history.

### remote/ — Remote Entity Transfer

Defines the protocol and helpers for transferring entities between a local store and a remote server. See `data/remote/LOCAL_CODE_OVERVIEW.md` for details.

**`remote_client.py`**: `RemoteClient` — a `@runtime_checkable` Protocol defining four methods: `negotiate` (check which entities the remote has), `push_entities` (upload), `pull_plan` (get transitive closure IDs with sizes), and `pull_data` (download specific entities). Pull uses a two-phase approach so response sizes stay bounded.

**`transfer_format.py`**: CBOR streaming encoder/decoder for entity data. Each entity is encoded as `[raw_id_bytes, entity_bytes_or_null, [[dep_id_bytes, dep_kind_int], ...]]`. Entities are expected in topological order.

**`transfer_entity_source.py`**: `TransferEntitySource` — a `FlatEntitySource[StaticBrand]` backed by an in-memory dict of `FxtrEntityFlatData`. Branded under `STATIC_LIFETIME`. Used on the receiving side of a pull to feed downloaded entities into `pin_entity_from_arena` (which accepts any `FlatEntitySource`).

### debug/ — the scope-drain report

Diagnostics for scope-lifetime hangs: a hung `FxtrScope` drain is usually a lease wedge (some holder never releases — in the worst class, a longer-lived scope silently adopted an ephemeral ambient scope as a structural parent via a fork-inherited implicit capture, so the ephemeral scope can never finish draining; see `v2/notes/scope_discipline.md`). The report names the wedge instead of leaving a bare timeout.

- `scope_instrumentation.py` — the always-on hooks (a **leaf** module the core imports): the live-scope `WeakSet` registry (added at scope entry), the activation-task record set beside every `ACTIVE_SCOPE.set` (so a non-isolated construction can classify its implicit parent capture as same-task vs fork-inherited), scope creation-site capture, and the `FXTR_SCOPE_REPORT_TB=1` env switch that enables both allocation-traceback capture (`local_lease.LEASE_LEAK_TRACK_ALLOCATIONS`) and scope-creation tracebacks for a debugging rerun. Costs are nanoseconds per hook (measured; see `v2/plans/extras/extra8_scope_safeguards/part1_drain_report.md`).
- `scope_report.py` — the user surface: `dump_scope_report()` (synchronous global dump: every parked drain, its outstanding leases with holder labels, structural blocker scopes with classified parent edges, a hazard line for implicit+fork-inherited edges, and a live-scope inventory); `fail_after_and_report_hung_scopes(seconds)` (the recommended hang guard: dumps at the deadline while the wedge is pristine, then cancels and raises `TimeoutError`); `report_hung_scopes_on_signal()` (opt-in CM to wrap a main loop — dump on `SIGUSR1` etc.; nothing installs globally).

Supporting hooks elsewhere: `FxtrScope` records creation site, implicit-capture classification, dispatch-scope tags, and drain timestamps; `local_lease` leases accept a diagnostic `set_debug_label` (the independent-lease bridge labels its minted leases with their destination scope).

The *prevention* counterpart is scope-context hygiene: `no_active_scope()` (`entity_ref_scope.py`) runs a block with a hidden poison value in `ACTIVE_SCOPE`, so any implicit active-scope use — including non-isolated `FxtrScope` construction — raises `NoActiveScopeError` at the offending line instead of silently adopting an inherited ambient scope; `fxtr2/common/task_utils.py`'s `start_with_scope` / `start_soon_with_scope` apply a chosen scope (or, with `scope=None`, that poison) to a whole task at start. Core fxtr2 does not itself poison any tasks; these are opt-in affordances the scope-discipline note recommends.

## The `_provenance_parent` Pattern

A `FxtrID` can optionally carry a `_provenance_parent` — another FxtrID that represents the provenance-level identity of this node. The parent always unwraps to the data ID, and equality / hashing only use `_raw_data_id`, so the provenance dimension is a pure sidecar at the FxtrID level.

A scope opts into automatic provenance tracking by being constructed with `provenance_context` — a c-node that becomes the provenance parent of the scope's `$fxtr.ctx` marker. The marker propagates to entities stored into the scope and is inherited by child scopes by default. When a scope has provenance enabled, every scope op treats a FxtrID-with-parent as a single conceptual unit: `store_cbor` / `load_cbor` project all child IDs into provenance form on the way in and rewrap them on the way out; `lift` / `copy_from_scope` / `track_from_scope` flow only the provenance parent (the data ID rides along via `unwraps_to`); `is_tracked` / `abandon` cover both halves. The entity-ref cache is keyed on `(data_id, provenance_parent)` so the same data ID under different traces caches independently.

Even without an explicit `provenance_context`, the default `store_provenance="auto"` mode preserves any provenance reaching the scope as child references: if `store_cbor` sees a bare marker but a layered child, it substitutes the marker with a lazily-minted **fallback c-node** (a `FallbackContextMarker` keyed on `scope.name`) and takes the prov path. Scopes that want the old strip-on-store behavior opt in via `store_provenance="strip"`; the old "if marker has prov, take prov path; otherwise strip" behavior is `store_provenance="if_marked"`.

`extract_provenance_parent` and `embed_as_provenance_parent` on `FxtrScope` are 1-1 inverses for moving an ID between the data and provenance layers explicitly (e.g. when persisting just the provenance node into a namespace).

Cross-wire support — provenance-aware service-cap RPCs and `FxtrNamespace` persistence — is not yet wired. See `plans/phase3/stage9_provenanced_scopes/` for the design.

## Experimental (`fxtr2/experimental/`)

Experimental additions to fxtr where the API may change. See `fxtr2/experimental/LOCAL_CODE_OVERVIEW.md` for full details. Current contents:

- **`read_namespace.py`**: `FxtrReadNamespace` ABC — read-only subset of `FxtrNamespace` (retrieve, try_retrieve, list_pins). Not HTTP-specific; can be implemented by any read-only data source.
- **`http/`**: HTTP API for serving and consuming fxtr entities over REST: Starlette ASGI app (`create_fxtr_app`), Hypercorn helpers, `HttpReadableArena` + `HttpDeepFetchable` (leased APIs, `UnconditionalLocalLifetime`), `HttpFxtrReadNamespace`, and `WorkspaceNamespace` / `WorkspacePinningBackend` for hub workspace pins over HTTP. Also a **stateless service layer** (`service_codec` / `service_dispatch` / `service_rest_endpoint` / `service_ws_endpoint` / `service_registry`) for the client-state HTTP connector — plain-async services + a list-escape invocation codec + discovery, no fxtr-RPC. **Provenance:** layered `(data, prov)` ids ride the wire explicitly — a 3-element `["entity", data, prov]` escape for service results, and a tag-less `[data, prov]` value for `/pins` / `/pin` / `/discovery`; emission is a per-server opt-in (`provenanced=` on `create_fxtr_app` / `ServiceRegistry` / the serve helpers, default off → bare). Every wire id_string is a bare local id (the `@`-form is rejected — wire-ingest points parse with `FxtrID.from_id_string(..., allow_provenance=False)`); `/pin` bundles the prov node's precomp closure for a layered pin and is fetch-or-fail (unfetchable pin → 500).

## Testing

Tests use pytest with pytest-anyio for async support. Run the full
suite with:

```bash
uv run pytest -n auto
```

For the layout of test packages and what each module covers, see
`tests/LOCAL_CODE_OVERVIEW.md`.

For test plans, benchmarks, Thingwatcher, gotchas, and related notes,
see [CODE_OVERVIEW_EXTRAS.md](CODE_OVERVIEW_EXTRAS.md).

## TypeScript Port

The `ts/` directory contains an idiomatic TypeScript port of the core fxtr2 system, enabling Python/TypeScript interoperability over HTTP. It ports entity IDs, CBOR encoding, arenas, scopes, entity refs, the codec system, and the HTTP client/server. See `ts/CODE_OVERVIEW.md` for full details.

The TS package is managed with pnpm and uses `cbor2` (CBOR), `hono` (HTTP server), `vitest` (tests), and `biome` (lint/format). The porting plan and design decisions are in `plans/track2/stage3_ts_port/overview.md`.
