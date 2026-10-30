# Tests Local Code Overview

Tests use pytest with pytest-anyio for async support. Run the full suite
with:

```bash
uv run pytest -n auto
```

Use pytest to infer test counts when needed (e.g. `pytest --collect-only`)
— this doc describes what each module covers, not how many tests it
contains.

## Test infrastructure

- `tests/conftest.py` — Fixtures for `memory_store`, `memory_arena`,
  `active_arena`, `tmp_db_path`, etc.

## Top-level test modules

- `test_smoke.py` — Basic smoke tests across the system.
- `test_entity_core.py` — `FxtrID` and CBOR encoding.
- `test_arena_util.py` — Visitor functions for traversing entity data.
- `test_memory_arena.py` — In-memory storage (includes `load_flat`
  coverage).
- `test_fxtr_context_api.py` — Context API surface (namespace pin /
  retrieve / SQL / indexers).
- `test_cap_ir_arena.py` — Adapter (`ArenaIRHandleAdapter`) and arena
  (`CapIRArena`) acceptance tests, mirroring substage-3-style scenarios
  with cross-task communication routed through IRValue handoffs.
- `test_cap_ir_arena_unit.py` — Unit tests for the wire-encoding
  helpers in `fxtr2/services/cap_ir_arena.py` (`_encode_fxtr_id` /
  `_decode_fxtr_id` round-trips, `compute_precomp_closure` BFS shape,
  `ingest_precomp` symmetry, `_decode_flat_data` parser).
- `test_transfer_format.py` — Remote CBOR entity stream
  encoding/decoding round-trips.
- `test_transfer_entity_source.py` — `TransferEntitySource` behaviour.
- `test_hub_client.py` — Integration tests for the hub client push/pull
  flow via the `RemoteClient` protocol.

## Test packages

### `test_entity_ref_scope/`

Organised by feature area:

- `test_entity_ref.py` — `EntityRef` creation, properties, validation.
- `test_scope_lifecycle.py` — `FxtrScope` lifecycle, hierarchy,
  tracking, isolation.
- `test_frozen_scope.py` — Scope freezing (mechanics, disallowed ops,
  allowed ops, inheritance, integration).
- `test_caching.py` — Entity caching and side-effect replay.
- `test_store_load.py` — `store` / `load`, deep load, copy operations.
- `test_custom_entities.py` — Custom `Entity` base class
  implementations.
- `test_load_union.py` — Union-type loading with mixed entity types.
- `test_provenance_context.py` — Per-scope context entity: default
  markers, explicit `provenance_context`, lift-from-parent, marker
  inheritance, `extract_provenance_parent` /
  `embed_as_provenance_parent` helpers.
- `test_provenance_flow.py` — FxtrID-level provenance flow through
  scope ops: `store_cbor` projection, `load_cbor` rewrap, `is_tracked`
  / `abandon` / `lift` / `copy_from_scope` / `track_from_scope` for
  layered IDs, cache-key isolation across provenance traces.

### `test_conversion/`

- `test_base_registry.py` — Type-annotation handler registry (lookup,
  aliases, unions, generics, overrides, `OverrideFxtrHandlers`).
- `test_mashumaro_entities.py` — `DataclassEntity` and mashumaro
  serialization (inline entities, collections, type parameters);
  includes `TestFxtrCtxCaptureAtStore` (default-sentinel, marker
  injection, layered-marker capture, round-trip preservation across
  distinct provenance contexts, sentinel-rejection on load).
- `test_legacy_entity_abc.py` — `SelfLoadingLegacyEntity` bridge.
- `test_unknown_entity.py` — `UnknownEntity` fallback.

### `test_services/`

- `test_untyped_service.py` — End-to-end tests for `UntypedServiceImpl`
  / `UnknownServiceProxy`. Most parametrise via a `service_layout`
  fixture across three configurations (`same_scope`, `child_scope`,
  `sibling_attach`) so each behaviour is asserted regardless of how the
  caller came to know the handle. Coverage: mount/resolve, primitives,
  exception propagation, drain, `FxtrID` / `EntityRef` round-trips with
  arena bridging, `ServiceRef` arg round-trips with case-3 cap-bridge
  installation on the call scope, `service_type_id` round-tripping
  (including ServiceRef equality with distinct type_ids).
- `test_protocol_subclassing.py` — Genuine `@service_protocol`
  subclassing (stage 13 part 0): `allow_protocol_subtypes` resolver
  semantics (most-specific wins, unmarked-base conflict at resolve,
  undecorated-subclass fallback), inherited-method TypeVar substitution
  (fully-bound / still-generic / multi-hop / unparameterized fallback /
  same-name collision / override-wins), end-to-end mount/resolve
  round-trips through inherited methods (including re-typing a ref to
  the base), generic `TypedServiceImpl` base inheritance, and the
  relaxed `EventSource` / `Subscriber` bounds.

### `test_common/`

- `test_channels.py` — Output channels (`fxtr2.common.channels`).
  Smoke gates first (`ChannelTag` + `DataclassEntity` multiple
  inheritance round-trip; `ChannelUpdate` union decode by `kind`
  literal), then `infer_channel_types` (concrete / subclass / bound
  generic / rejection cases) and the engine over the `local_fxtr_scope`
  fixture (every call goes through `resolve` + a typed proxy — the
  dispatch path): replay-then-live lifecycle, mid-stream snapshot folds,
  status-only emits + `replace_history`, the pre-start window
  (peek/get_tag block; subscribe delivers after start), post-finish /
  post-abort snapshots, message residency through a transient source
  scope, subchannels (unknown key / per-parent / cascade clean abort /
  writer-release abort), lifecycle errors, the data-only `ServiceRef`
  rejection on emit/finish, the wire-envelope == dataclass-encoding pin,
  `iter_channel` typed-event decoding, and the debug subchannel feed
  (`subscribe_debug_subchannels`: replay-then-live, `name=None`,
  independence from `start`, per-parent namespace, teardown on parent
  finish).

### `test_legacy_compat/`

- `test_data.py` — V1 entity compatibility layer (tests
  `fxtr2.legacy_compat`).
- `test_transitional_rundata.py` — Transitional entity types
  (`TaskWithPredecessors`, `ImplProvenanceSet`, `RunWithPredecessors`,
  `TaskToRunMap`).

### `test_rpc/`

Capability-passing RPC:

- `test_local_caps.py` — Local cap internals (`_InternalFuture`,
  `LocalRPCContext`, `LocalHandle`, `LocalPending`, `LocalPendingHandle`,
  ownership/refcounting).
- `test_transport.py` — Transport-parameterized integration tests
  (basic ops, path walking, error handling, pipelining, lifecycle,
  teardown, context teardown, ownership).

### `test_leases/`

Tests for the lease / grant / token primitives in `fxtr2/leases/`.

### `test_experimental/`

HTTP experimental stack: ASGI app, client (`HttpReadableArena`,
`HttpFxtrReadNamespace`), workspace namespace, server wrapper.

### `test_tsv_namespace/`

TSV-file fxtr namespace (pin mappings in git-tracked TSV files, backed by a
repo-level entity store): lock tests, CLI tests, index-file tests,
index-sync tests, and indexing integration tests.

### `test_layered_file_store/`

Layered file entity store: format codec round-trips, single + multi-layer
walks (including dynamic parent expansion and cycle handling), layer-local
pin isolation, remote resolution via a stub `DeepFetchable`,
`mark_as_remote` / `pin_to_this_layer` / `preload_data` semantics,
writer cleanup on exception, and `validate_on_close` behavior.

### `test_store_indexing/`

Namespace indexing:

- `test_sql_expr.py` — SQL expression parsing, validation, rewriting,
  and safety.
- `test_query_gen.py` — SQL query generation (structural + end-to-end
  SQLite, `iter_matches` + direct SQL).
- `test_query_view.py` — `QueryView` validation (name collisions,
  reserved names).
- `test_indexer_serdes.py` — `PathElement` round-trip and indexer
  store/load serialization.
- `test_path_parse.py` — Path expression parser (basic, named vars,
  derefs, exists, errors).
- `test_unpack.py` — `unpack_entity` (extraction, variables, derefs,
  strictness, type checking).
- `test_index_computer.py` — `IndexComputer` (`get_indexer_types`,
  `compute_indexer_entries`, scope isolation).
- `test_backend_integration.py` — `SQLiteNamespaceBackend` indexing
  (register, pin, query, unpin, unregister, GC).
- `test_user_facing_api.py` — `FxtrQueryableNamespace` user-facing API
  (register, unregister, `list_indexers`, `iter_matches`, `sql`, scope
  handling, retrieve copy regression).

### `test_sqlite_store/`

`sqlite3`-based segment file store (v6):

- Connection pools, worker pools, session lifecycle.
- Entity blob encoding/decoding.
- Segment file I/O.
- Segment write operations and sealing.
- Arena operations, `load_flat`, migrations, archive export/import.
- Read-only mode: lifecycle, reads, write guards, error cases,
  concurrency.

### `test_cross_language/`

Cross-language compatibility tests against the TypeScript port (see
`ts/`).
