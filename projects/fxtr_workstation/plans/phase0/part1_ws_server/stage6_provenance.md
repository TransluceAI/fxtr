# Stage 6 — Provenance support

Goal: make the workstation provenance-aware, so that two FxtrIDs with
the same data ID but different `_provenance_parent` are tracked as
distinct entities, and the prov-parent half flows transparently through
every workstation operation (RPC `copy_entity` / `get_entity`, invoke
args / results, callbacks, load results, metadata queries).

## Why now (and not later)

Provenance is intrinsic to fxtr — a load of a layered ID returns child
refs that are themselves layered with their own provenance, and a
correctly-tracked workstation should preserve that information end to
end so that downstream tools (the debug UI, the agent MCP, the future
panel viewer) can show users *which* version of a piece of data they
are looking at and trace it back.

Today the workstation projects `_provenance_parent` away at every
boundary:

- `TrackedEntityRecord` is keyed on `FxtrID`, whose `__eq__` ignores
  `_provenance_parent`. Two layered IDs with the same data half collide
  on the *same* record.
- `_get_or_create_entity_record` therefore mints exactly one record per
  data ID, regardless of provenance, and abandons it from the shared
  `entities_scope` on last release — which would corrupt any other
  record whose data is shared.
- The short-ID allocator hashes on the same `FxtrID` equality, so two
  prov variants would resolve to the same short ID and be
  indistinguishable on the wire.

The proposed changes give us a clean foundation for all later
provenance-aware work (and for the persistence cap, which now lives in
stage 7).

## Scope

In:

- New `ProvenancedKey(data_id, prov_parent)` value type used as the
  workstation-wide entity key.
- Short-ID allocator rewrite: `@N`-suffix family for prefixable IDs;
  `data_id_string@<short_of(prov)>` form for non-prefixable IDs.
- Per-record scopes: every `TrackedEntityRecord` owns its own isolated
  scope holding the layered FxtrID; release drains the scope. The
  shared `WorkstationState.entities_scope` is removed.
- Layered FxtrID propagation through every workstation operation
  (`copy_entity`, `get_entity`, invoke args / results, callback
  payloads, load results) — wired by switching the relevant lookups /
  walks to use `ProvenancedKey` and stop stripping prov parents.
- New mutation ops `GetEntityMetadataOp` / `GetServiceMetadataOp` +
  matching `SessionEvent` variants. Sync-resolved at commit time.
  Result references in the metadata participate in retention via
  `event_refs[seq]`.
- Debug-UI: minimal updates so the existing entity / session pin tables
  show short IDs in the new format (no behavior changes), plus a row
  for metadata-result events in the session event log.

Out:

- Persistence (was stage 6, now stage 7).
- Auto-attaching provenance to entities created in Python (this is a
  fxtr-core feature still under design per the v2 README).
- Indexing or querying based on provenance.

## Sub-stage breakdown

The work splits into three sub-stages. The first one is a single
large refactor (ProvenancedKey + per-record scopes + short-ID format
change) because the three pieces touch the same datatypes and
invariants — staging them separately would only produce intermediate
states with broken invariants. We pause for review between sub-stages
but defer **all** new tests, and any test-suite run, until 6b.

- **6a — Core refactor (no tests yet).** ProvenancedKey + per-record
  scopes + new short-ID format land together. Existing tests likely
  break during this work; we will fix them up in 6b, not in lockstep.
  We can still pause for review at each sub-section's boundary:
  - 6a.1 — ProvenancedKey + record-table re-key.
  - 6a.2 — Per-record scope refactor.
  - 6a.3 — Short-ID allocator rewrite.
- **6b — Test pass for 6a.** Run the existing test suite, repair
  fallout from the refactor (signature changes, key-type changes,
  short-ID format changes), and add new tests for the
  provenance-aware behaviour.
- **6c — Metadata query ops.** New `GetEntityMetadataOp` /
  `GetServiceMetadataOp` and matching event types, then tests.

## 6a — Core refactor (combined)

### 6a.1 — `ProvenancedKey` + record-table re-key

Introduce the new key type and switch every workstation-internal
lookup over to it. Layered FxtrIDs flow end to end.

Moving parts:

- New `state.ProvenancedKey` (frozen dataclass, `data_id: FxtrID`,
  `prov_parent: FxtrID | None`; both bare). Helper
  `provenanced_key_for(fxtr_id) -> ProvenancedKey` that splits a
  possibly-layered FxtrID and strips both halves of any nested
  `_provenance_parent`.
- `WorkstationState.entity_records: dict[ProvenancedKey, TrackedEntityRecord]`.
- `TrackedEntityRecord.fxtr_id: FxtrID` becomes the *layered* FxtrID
  (so `record.fxtr_id._provenance_parent` is the prov half).
- `SessionRecord.entity_refcounts`, `tracked_entities`, `event_refs`
  all switch from `FxtrID` to `ProvenancedKey`.
- `_get_or_create_entity_record(state, fxtr_id, ...)` derives the key
  from the FxtrID, allocates the short ID via the allocator, mints a
  record carrying the layered FxtrID. Source-scope handling lands in
  6a.2 — at the end of 6a.1, we still use the shared `entities_scope`
  internally; the entity-tracking-vs-abandon mismatch with overlap
  cases is acceptable in the intermediate state because we won't run
  tests until 6b.
- `_walk_refs_into` already preserves `_provenance_parent` (it appends
  `value.inner_id` from the CBOR wrapper types, which carry the
  layered FxtrID after `_load_arena_cbor`'s rewrap). No change.
- `wire_escape.encode_entity` / `lookup_entity` callbacks adapt to the
  new short-id ↔ layered FxtrID round-trip. The escape codec itself
  is unchanged — only the workstation-supplied callbacks change.
- Snapshot builders (`_build_entity_record`, `_build_record_for`) key
  on `ProvenancedKey`, not `FxtrID`, when looking up records.

### 6a.2 — Per-record scope refactor

Replace the shared `WorkstationState.entities_scope` with one isolated
scope per `TrackedEntityRecord`. The record's scope tracks the layered
FxtrID; release drains it via `force_close`.

Moving parts:

- `TrackedEntityRecord.scope: FxtrScope` (`isolated=True,
  mountable=False`, `include=[root_scope]`). Constructed when the
  record is minted.
- `_get_or_create_entity_record` gains a `source_scope: FxtrScope`
  parameter. On first mint:
  - Open the per-record scope.
  - Either `await record.scope.copy_from_scope(layered_fxtr_id,
    source_scope)` (for inbound copies — RPC `copy_entity`, invoke
    result delivery, callback payload delivery) or
    `record.scope.track_from_scope(layered_fxtr_id, source_scope)`
    (for in-memory same-store transfers — children minted from a
    load result).
  - Concrete shape: a thin async helper plus a sync helper variant,
    chosen explicitly at each call site so the dispatch shape is
    obvious.
- Load worker (`load_worker` / `deliver_load_result_to_slot`):
  1. Open a transient `load_scope` (`isolated=True, mountable=False`,
     `include=[parent_record.scope]`) and `await
     load_scope.load_cbor(layered_fxtr_id)`.
  2. For each FxtrID in the loaded CBOR, mint / get-or-create the
     child record and `track_from_scope` from `load_scope` into the
     child record's scope.
  3. Drain `load_scope` (it falls out of the `async with`).
- Invoke result delivery / callback payload delivery: per FxtrID in
  the result, copy from the worker's `call_scope` into the child
  record's scope.
- `service_impl.copy_entity` (RPC): copy from `call_scope` into the
  record's scope.
- **Invoke / callback dispatch into the workstation**: when the
  workstation builds a `call_scope` for a method invocation that
  takes entity args, the `call_scope` is opened with
  `include=[..., arg_record_X.scope, arg_record_Y.scope, ...]` — the
  per-record scopes for each entity-typed arg, alongside the
  service-record scopes already included. **No copying**: the
  call-scope inherits, and the target service's body reads through
  the inheritance chain.
  - Concretely this is the `invoke_worker` /
    `_deliver_result_as_event` family today; replace
    `entities_scope` in the `include=` list with the per-arg per-record
    scopes.
- `service_impl.get_entity` (RPC): use `call_scope.track_from_scope(
  record.fxtr_id, source_scope=record.scope)` to extend a lifetime
  claim on the layered FxtrID into `call_scope`. This is sync,
  same-store, no byte copy — just lifetime-claim bookkeeping —
  so it satisfies the "don't copy" rule. After the track call the
  framework's chain-bridge logic finds the FxtrID through
  `call_scope` and returns it to the caller as today.
- `_maybe_release_entity_record`: replace
  `state.entities_scope.abandon(...)` with `record.scope.force_close()`.
- `add_rpc_connection`: drop `entities_scope` from the connection
  scope's `include` list. The `state.entities_scope` field is
  removed.

### 6a.3 — Short-ID allocator rewrite

Rewrite `ShortIDAllocator.get_or_assign_entity` to use
`ProvenancedKey` and the new format.

Moving parts:

- Allocator state becomes per-`data_id`-bucket:

  ```
  _entity_buckets: dict[FxtrID, _EntityBucket]
  # _EntityBucket fields:
  #   prefix: str
  #     bare prefix allocated for this data_id (or full id-string for
  #     non-prefixable)
  #   prov_to_suffix: dict[FxtrID | None, int]
  #     1 = bare (no @ suffix); 0 = explicit @0; 2,3,... = enumerated
  #     prov-bearing variants
  #   next_suffix: int
  #     next @N to allocate (skips 1 by construction)
  ```

  - For prefixable (`d`/`n`/`c`): bucket prefix is the configured
    prefix (extended on collision, as today). The first
    `(data, *)` pair seen claims suffix 1 (rendered with no `@`).
    Subsequent distinct prov parents claim 2, 3, ….
    A late-arriving `(data, None)` claims suffix 0 (rendered as `@0`).
  - For non-prefixable: bucket prefix is the full `data.id_string()`.
    `(data, None)` always renders bare (no suffix). `(data, prov)`
    renders as `<prefix>@<short_of((prov, None))>`. No @0/@N
    enumeration — every distinct prov gets its own composite short.

- `lookup_active_entity(short_id) -> ProvenancedKey | None` parses the
  short ID back to a `ProvenancedKey`. Parser is bucketed by tag
  (look at the prefix's first char).
- Run-lifetime memory: same as today — the allocator never forgets a
  mapping. A re-registration of `(data, prov_X)` returns the same
  short ID it had before.
- Wire-protocol implications: short IDs are still `str`; the new shape
  passes through every existing `str`-typed field on
  `client_protocol.py` unchanged.

## 6b — Test pass for 6a

Two phases:

1. **Suite repair.** Run `uv run pytest -n auto`. Most fallout will
   be type / signature / short-ID format changes. Update existing
   tests to reflect the new key type, scope topology, and short-ID
   format. Aim for the existing suite to pass green before adding new
   tests.
2. **New tests.**
   - **6a.1 (key-type)**: two `copy_entity` calls with the same data
     ID but different prov parents create two records with distinct
     short IDs and distinct `tracking_sessions` sets; session
     refcounts isolate per `ProvenancedKey`.
   - **6a.2 (per-record scope)**: `(data, prov_X)` and
     `(data, prov_Y)` released independently keep each other
     loadable; `get_entity` after the registering RPC connection has
     dropped still works; transient `load_scope` drains cleanly even
     if a child-record mint partway through the loop raises;
     overlapping `(data, prov_*)` releases each drain only their own
     scope.
   - **6a.3 (short IDs)**: allocation order (first `(data, None)` →
     bare; then `(data, prov_A)` → `@2`; then `(data, prov_B)` →
     `@3`; then `(data, None)` repeat → bare again — run-lifetime
     stable); reverse order (first `(data, prov_A)` → bare; then
     `(data, None)` → `@0`; then `(data, prov_B)` → `@2`, skipping
     @1); non-prefixable `(., c-id)` shape; collision extension still
     works for prefixable buckets that share a prefix; round-trip
     `lookup_active_entity` for every form.

## 6c — Metadata query ops

Add the synchronous metadata-query variants to the mutation pipeline.

Moving parts:

- `client_protocol.py` adds:
  - `GetEntityMetadataOp(slot: int, short_id: str)` and
    `GetServiceMetadataOp(slot: int, short_id: str)` as members of
    the `MutationOp` tagged union.
  - `EntityMetadataResultEvent(seq, slot, metadata)` and
    `ServiceMetadataResultEvent(seq, slot, metadata)` as members of
    the `SessionEvent` tagged union; also matching error variants
    (`EntityMetadataErrorEvent` / `ServiceMetadataErrorEvent`,
    mirroring `LoadErrorEvent`).
  - `metadata: dict[str, Any]` on the wire — values are wire-escaped
    via `wire_escape.escape` so embedded short refs use the existing
    `["entity", short_id]` / `["service", short_id]` shape and
    refcounts hook in via the result-walk.
- `apply_mutation` op-validation accepts the two new ops alongside
  the existing slot-monotonicity check. Resolution is sync at commit
  time — the helper computes the metadata dict and the post-commit
  pipeline emits the event in the same `state_update` window.
- Metadata schemas:
  - `GetEntityMetadata` — input: `short_id` for `ProvenancedKey(data,
    prov)`. Output dict, fields populated *only when applicable*:
    - `id_string: str` — `record.fxtr_id.id_string(include_provenance=True)`.
    - `unprovenanced: str` — short ref of `ProvenancedKey(data, None)`,
      always emitted (creating that record on demand if it doesn't
      yet exist; short IDs are run-lifetime stable so this is safe).
    - `provenance_parent: str` — short ref of
      `ProvenancedKey(prov, None)` if `prov is not None`; omitted
      otherwise.
    - `provenance_parent_id_string: str` — `prov.id_string()` if
      applicable; omitted otherwise.
    - `unwraps_to: str` — short ref of the unwrap target. Computed by
      calling `unwrap_provenance(data)` on a transient child of
      `record.scope`. **For `c`/`n`/`p`/`u`-tagged data this never
      returns None** (any provenance-tag node has a known unwraps_to
      relation through arena propagation); we assert non-None and
      treat None as a bug. Result short ref is for
      `ProvenancedKey(unwrapped, data)` — the layered form where
      `data` is the unwrapped node's provenance parent. Omitted only
      when `data` itself is not a provenance-tag node (so
      `unwrap_provenance` returns None for non-provenance nodes,
      which is fine).
    - `unwraps_to_id_string: str` — full id-string of the layered
      `(unwrapped, data)`; emitted alongside `unwraps_to`.
    - `context_data: str` — short ref of `ProvenancedKey(c-data, None)`
      if `data` has tag `c` (computed via `c_node_to_context_data`,
      sync). Omitted otherwise.
    - `backstep: str` — short ref of `ProvenancedKey(p-prefix, None)`
      if `data` has tag `p` and the path has length ≥ 1, where
      `p-prefix` is the p-node with the last path component dropped.
      Pure structural from the FxtrID — sync, no scope needed.
      Omitted otherwise.
  - `GetServiceMetadata` — input: `short_id`. Output:
    - `type_id_string: str` — the service's type ID as a string
      (looked up via `record.service_key.service_type_id`).
- Refcount contributions: any `ProvenancedKey` that appears as a
  metadata value (`unprovenanced`, `provenance_parent`, `unwraps_to`,
  `context_data`, `backstep`) is increfed for the session at commit
  time and added to `event_refs[seq]` so retention drops it when the
  event ages out. The `_get_or_create_entity_record` helper for these
  cases uses `track_from_scope` from `record.scope` (sync, same
  store).
- Resolution helper `compute_entity_metadata(state, key) ->
  tuple[dict, list[ProvenancedKey]]`: returns the metadata dict (with
  short refs as plain strings) and the list of `ProvenancedKey`s to
  incref. Pure function over scope reads.
- Debug UI: render new event types as one-line rows in the session
  event log; payload as a JSON dump for now.

Tests (paired with the implementation, since 6c is small and
self-contained):

- `GetEntityMetadata` on a `(data, None)` for a `d`-tagged data:
  returns `id_string`, `unprovenanced`, no other fields.
- `GetEntityMetadata` on a `(data, prov_X)`: returns `id_string`,
  `unprovenanced`, `provenance_parent`, `provenance_parent_id_string`.
  `data` is non-provenance → no `unwraps_to`.
- `GetEntityMetadata` on a `u`-tagged record: `unwraps_to` populated.
- `GetEntityMetadata` on an `n`-tagged record after the entity has
  been loaded: `unwraps_to` populated.
- `GetEntityMetadata` on a `c`-tagged record: `context_data`
  populated, `unwraps_to` populated.
- `GetEntityMetadata` on a `p`-node with a path of length ≥ 1:
  `backstep` populated.
- Refs in the metadata participate in retention: after
  `GetEntityMetadata` returns a `provenance_parent` short ref, the
  session has a refcount on `(prov, None)` that the retention horizon
  prune drops when the metadata event ages out.
- `GetServiceMetadata`: returns `type_id_string`.
- Error path: `short_id` not active → `*MetadataErrorEvent` with a
  validation error; mutation still commits (the slot is consumed).

## Implementation steps

- [x] **6.0** — Move `stage6_persistence.md` → `stage7_persistence.md`;
      update `overview.md` to renumber. (Done before this plan was
      finalized.)
- [x] **6a.1.1** — Introduce `ProvenancedKey` + `provenanced_key_for`
      helper. (Initially in `state.py`; moved to `short_ids.py` in
      6a.3 to break the import cycle.)
- [x] **6a.1.2** — Switch `WorkstationState.entity_records` and the
      session refcount dicts to `ProvenancedKey` keys; update
      `_get_or_create_entity_record` (since split into
      `_ensure_entity_record_via_copy` / `_ensure_entity_record_via_track`),
      `_maybe_release_entity_record`,
      `_incref_session_entity` / `_decref_session_entity`,
      snapshot builders.
- [x] **6a.1.3** — Update `wire_escape` callbacks plumbing
      (`encode_entity` / `lookup_entity`) for layered FxtrIDs.
      (No-op in practice — the allocator's internal switch in 6a.3
      was sufficient since the callback signatures are FxtrID-based.)
- [x] **6a.2.1** — Add `TrackedEntityRecord.scope` and rewire the
      get-or-create / release primitives to manage per-record scopes.
      Add `_open_record_scope`, `_install_entity_record`,
      `_ensure_entity_record_via_copy` (async), and
      `_ensure_entity_record_via_track` (sync) helpers.
- [x] **6a.2.2** — Rewire `service_impl.copy_entity` /
      `service_impl.get_entity` /
      `service_impl.get_entity_by_external_name` for per-record
      scopes. ``get_entity`` uses
      ``call_scope.track_from_scope(record.fxtr_id, record.scope)``.
- [x] **6a.2.3** — Rewire load worker (`load_worker` /
      `_phase1_attach_load_refs` / `_phase3_commit_load_result`) for
      per-record scopes (transient `load_scope`, `track_from_scope`
      to children).
- [x] **6a.2.4** — Rewire invoke result delivery
      (`deliver_invoke_result_to_slot`, `_deliver_result_as_event`)
      and callback payload delivery for per-record scopes (via
      `_ensure_entity_record_via_copy` from `source_scope`).
- [x] **6a.2.5** — Rewire invoke / callback **arg side**:
      ``_invoke_worker_body`` opens ``call_scope`` with
      ``include=[target_scope, *arg_service_scopes,
      *arg_entity_scopes]``. Each entity-typed arg's per-record
      scope is in the include list — no copy.
- [x] **6a.2.6** — Drop the shared `entities_scope` from
      `WorkstationState`; remove from connection-scope `include`;
      update `server.py` (and add a defensive force-close sweep on
      shutdown so root-scope drain doesn't hang on stray records).
- [x] **6a.3.1** — Rewrite `ShortIDAllocator` for the new
      `ProvenancedKey`-keyed scheme: per-`data_id` `_EntityBucket`s
      with `prov_to_short_id` map, prefixable `@N` enumeration with
      `@1` skipped and `@0` reserved for late-arriving
      `(data, None)`, non-prefixable `data@<prov-short>` recursive
      composition, and `lookup_active_entity` /
      `lookup_known_entity` for active vs. parsing lookups.
- [x] **6b.1** — Run existing test suite; repair fallout
      (signatures, key types, short-ID format, `entities_scope`
      removal). All 252 existing tests green.
- [x] **6b.2** — Add new tests:
      - `tests/test_short_ids_6a.py` (13 unit tests covering the
        allocator's allocation rules, run-life stability,
        non-prefixable composites, prefix-collision extension, and
        lookup round-trips).
      - `tests/test_provenance_6a.py` (5 integration tests covering
        distinct records for distinct prov, session refcount
        isolation, abandon-isolation under shared data, transient
        load_scope drain, wire-format short-id sequence).
- [x] **6a.x.x — CODE_OVERVIEW.md** updates rolled into 6b after
      tests passed (per the deferred plan): record-keying,
      per-record scopes, short-ID format, new helpers, new test
      file listings.
- [x] **6c.1** — Add `GetEntityMetadataOp` / `GetServiceMetadataOp`
      and matching ``Result`` / ``Error`` event variants to
      `client_protocol.py`. Both joined the ``MutationOp`` union;
      events joined ``SessionEvent``.
- [x] **6c.2** — Add ``_compute_entity_metadata`` /
      ``_compute_service_metadata`` helpers + sync resolution path
      in ``apply_mutation``'s commit phase. Each metadata op
      consumes a slot during validation; commit-time resolution
      bumps ``event_seq``, builds the result event, and broadcasts
      via ``_append_session_event``. The orphan-record race window
      (record disappears between validation and commit) emits an
      ``EntityMetadataErrorEvent`` / ``ServiceMetadataErrorEvent``.
- [x] **6c.3** — Refcount contributions: each ``ProvenancedKey``
      surfaced in the metadata payload (``unprovenanced``,
      ``provenance_parent``, ``unwraps_to``) is incref'd against
      the session and tracked in ``event_refs[seq]``. Retention
      tested in ``test_metadata_refs_drop_when_event_passes_retention_horizon``.
- [x] **6c.4** — Debug UI: ``SessionEvent`` types extended with the
      four new variants; ``SingleEventRow`` renders ``metadata``
      as a JsonTree (alongside ``payload`` for result events and
      ``message`` for error events). ``isError`` covers the new
      error kinds.
- [x] **6c.5** — Tests for 6c (``tests/test_metadata_6c.py``, 9
      integration tests):
      - bare ``d``-tag returns just ``id_string`` + ``unprovenanced``;
      - layered ``(d, n-prov)`` adds ``provenance_parent`` +
        ``provenance_parent_id_string``;
      - ``n``-tagged data IDs surface ``unwraps_to`` +
        ``unwraps_to_id_string``;
      - ``c``-tagged data IDs surface ``context_data`` (the d-node
        the c-node was built from);
      - ``p``-tagged pathroot nodes correctly OMIT ``backstep``
        (verifies the ``None`` return path from
        ``step_back_p_ctx``);
      - ``GetServiceMetadata`` returns ``type_id_string``;
      - retention horizon decrements metadata-event refs;
      - validation rejects unknown short_ids;
      - slot-monotonicity within a single mutation enforced.
- [x] **6c.6** — Update CODE_OVERVIEW.md (metadata ops + helpers,
      new test file).
- [x] **6c.7** — ``context_data`` (for c-tag) + ``backstep`` (for
      p-tag) implemented via direct access to
      ``transient._main_arena.c_node_to_context_data`` /
      ``step_back_p_ctx``. Each returns a FxtrID that the
      transient scope already has access to through its parent
      chain; ``_ensure_entity_record_via_track`` then mints the
      corresponding records.

## Notes for stage 7 (persistence)

When stage-7 persistence is implemented later, `save(name, fxtr_id)`
needs to handle layered FxtrIDs: pin the prov-form (use
`extract_provenance_parent` + the prov half if layered), and on
`load(name)` re-attach via `embed_as_provenance_parent` so the
returned FxtrID is layered with the original prov. A TODO note has
been added in `stage7_persistence.md`.

## Resolved design questions

- **Connection scope reaching per-record scopes**: don't copy.
  Per-call `call_scope`s for invoke / callback / mutation-op
  dispatch include the per-record scopes of every entity arg in
  their `include` list, mirroring the existing service-bridge-scope
  pattern. For `get_entity` (no entity arg, but returns one):
  `call_scope.track_from_scope(record.fxtr_id, record.scope)` —
  sync, same-store, lifetime-claim only, no byte copy.
- **`unwrap_provenance` returning None for c/n/p**: cannot happen.
  Assert non-None for provenance-tag nodes; treat None as a bug.
  None is only legitimate for non-provenance-tag data, in which case
  we omit `unwraps_to` from the metadata response.
- **`entities_scope` umbrella**: removed entirely. Per-record scopes
  replace it; arg-side scope inheritance handles invoke / callback
  paths; `get_entity` handles its own bridging.
- **Per-record scope mountability**: `mountable=False` (no per-entity
  worker; lifetime managed via bare `FxtrScope` + `force_close`).
- **`unprovenanced` may create a `(data, None)` record on demand**:
  yes — short IDs are run-lifetime stable so creating the bare
  variant on demand from a metadata query is harmless.
