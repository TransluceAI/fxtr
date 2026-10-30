
### Thingwatcher Integration

Visualization of fxtr entities is provided by [thingwatcher](../spikes/thingwatcher). See `thingwatcher/fxtr/` for the integration module that allows viewing fxtr entity graphs in thingwatcher's debugging UI.


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

6. **Scopes vs arenas**: Scopes are the user-facing abstraction; arenas are the lower-level implementation. Most user code should work with `FxtrScope` (from `entity_ref_scope`), not `MemoryArena` directly. Non-isolated scopes automatically become the active scope when entered and inherit from their parent scope.

7. **Namespace scopes**: Each `SQLiteFxtrNamespace` has its own scope. Entities retrieved from a namespace are bound to the namespace's scope. To load them, either use `include=[ns.scope]` when creating a child scope, or use `lift()` to bring them into another scope.

8. **Deep fetchability**: Scopes can be "deeply fetchable," meaning any entity reachable from the scope's tracked set can be found by ID alone (via `_deep_fetch`). This is used by the experimental HTTP server. A scope is deeply fetchable if all its parents are; `DeepFetchable` backends (for example `SQLiteNamespaceBackend`) are registered on the scope with `register_deep_fetch_source` to enable this.

For more details, see [notes/implementation_notes.md](notes/implementation_notes.md).

## Planned Features

See [notes/planned_features.md](notes/planned_features.md) for features that are designed but not yet implemented.

## Test plans

Test plans (in `plans/`):
- `test_plan_entity_core.md` - Tests for FxtrID and CBOR encoding
- `test_plan_arena_util.md` - Tests for visitor functions
- `test_plan_memory_arena.md` - Tests for in-memory storage
- `test_plan_sqlite_arena.md` - Tests for SQLite persistence
- `test_plan_entity_serdes.md` - Tests for high-level serialization

## Benchmarks

Performance benchmarks live in `examples/benchmarks/store_benchmarks/`. They measure entity transfer throughput between scopes and namespaces. Cases have flat curated names: `tree_pin`, `tree_retrieve`, `flat_pin`, `flat_retrieve`, `flat_indexed`.

CLI uses subcommands: `uv run python -m examples.benchmarks.store_benchmarks {run,trace,list}`. All subcommands accept `-k PATTERN` to filter cases by name substring. `run` adds `-n NUM` and `--json FILE`. `trace` adds `-o DIR` (saves viztracer traces, requires `uv sync --group prof`). See `examples/benchmarks/README.md` for details.

## Related Documentation

- `plans/` - Implementation plans and test plans
- `plans/reorg_tracker.md` - Current progress on codebase organization
- `scratch_notebooks/` - Development notebooks (may be out of date, useful as test inspiration)
- `notes/` - Design notes and detailed explanations
  - `notes/implementation_notes.md` - Deep dive into non-obvious implementation details
  - `notes/planned_features.md` - Features that are designed but not yet implemented
  - `notes/reorg_questions.md` - Q&A from initial codebase review
  - `notes/roundtrip_serdes.md` - Detailed documentation on entity serialization/deserialization design
