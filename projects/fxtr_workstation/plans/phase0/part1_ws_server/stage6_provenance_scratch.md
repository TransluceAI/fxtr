# Stage 6 — Provenance support — scratch notes

Working notes for `stage6_provenance.md`. Captures design decisions
and rationale that came up while drafting the plan, plus things to
keep in mind during implementation.

## Confirmed design decisions

- **Record key**: a frozen dataclass `ProvenancedKey(data_id: FxtrID,
  prov_parent: FxtrID | None)`. Both fields are *bare* (no nested
  `_provenance_parent`). Constructed from a possibly-layered FxtrID
  via a small helper that splits `_provenance_parent` from the data
  half and strips both.
- **Short-ID format** (rewrite of `short_ids.py`):
  - Prefixable (`d`/`n`/`c`):
    - First-seen `(data, *)` for a given `data` → bare prefix
      (e.g. `da1b2c`).
    - 2nd, 3rd, … distinct `(data, prov)` pairs → `da1b2c@2`,
      `da1b2c@3`, ….
    - `(data, None)` after `(data, prov)` was seen first → `da1b2c@0`.
    - `@1` is never allocated (the bare form is implicitly @1).
  - Non-prefixable (`!`/`.`/`p`/`u`): `(data, None)` → bare
    `data.id_string()`; `(data, prov)` →
    `f"{data.id_string()}@{shortid_of((prov, None))}"`. No `@0` form
    for non-prefixable — the bare id-string is already unambiguous.
- **Children of a layered load already carry their own provenance**:
  `_load_arena_cbor` rewraps each child via `unwrap_provenance`
  during the load walk, so loaded CBOR's child IDs are layered with
  their own (different) prov parent. We just need to stop discarding
  `_provenance_parent` on the workstation side.
- **`unwrap_provenance`**:
  - Sync; safe to call on any FxtrID.
  - Returns `None` for non-provenance-tag nodes (`d`, `.`, `!`).
  - For provenance-tag nodes (`c`/`n`/`p`/`u`), **never returns None
    when the source ID is tracked in the scope** — assert non-None
    and treat None as a bug.
- **Per-record scope** holds the layered FxtrID. `copy_from_scope`'s
  layered-ID handling already tracks both the prov parent and the
  data half, so the per-record scope's liveset has both.
- **Metadata query op** rides the existing slot/event pipeline: a new
  `MutationOp` variant + a new `SessionEvent` variant; sync-resolved
  in the commit phase; refs in the metadata payload participate in
  retention via `event_refs[seq]` exactly like load results.
- **`call_scope` reaches per-record scopes via `include`** — for any
  workstation method dispatch that takes entity args, the per-call
  `call_scope` is opened with `include=[..., arg_record_X.scope, …]`
  for each entity-typed arg. This mirrors the existing service-bridge
  pattern. **No `copy_from_scope` at the dispatch boundary.**
- **`WorkstationState.entities_scope` is removed.** Per-record scopes
  replace it. Connection scopes drop `entities_scope` from their
  `include` list.
- **Per-record scope shape**: `isolated=True, mountable=False`,
  `include=[root_scope]`. Lifetime managed via bare `FxtrScope` +
  `force_close()` on release.

## `get_entity` bridging — resolved

Use `call_scope.track_from_scope(record.fxtr_id,
source_scope=record.scope)` inside the `get_entity` method body. The
common root memory store means `track_from_scope` is sync, same-
store, and only extends lifetime claims (no byte copy). The framework
chain-bridges the returned FxtrID out via the call_scope as today.

This is the entity analog of `bridge_service_from_scope` for the
service path.

## Things to be careful about during implementation

- `_provenance_parent` propagation through `_walk_refs_into`: the
  walker preserves prov-parent on the FxtrIDs it extracts (via
  `value.inner_id` for `RawWeakFxtrID` etc., which carry the layered
  form after `_load_arena_cbor`'s rewrap). No change needed. Add a
  test in 6b.
- `wire_escape.encode_entity` callbacks today receive a *bare*
  FxtrID; the workstation's `encode_entity` callback maps this to a
  short_id. After the refactor, the callback receives a layered
  FxtrID and must look up the short ID via the
  `(data, prov)` allocator. Symmetric on the decode side.
- Session-state `tracked_entities` (the user-facing resource list,
  surfaced in `StateUpdateEvent.tracked_entities`) is keyed on
  `ProvenancedKey`. The wire shape is `list[str]` of short IDs, so
  the change is transparent on the wire — but tests that compare on
  `FxtrID` equality need to be updated.
- `pinned_by_sessions` on the entity resource snapshot: today derived
  from `TrackedEntityRecord.tracking_sessions` (a dict keyed on
  session UUID). The shape doesn't change, but the *records* are now
  per `(data, prov)` so a session that pins both `(data, None)` and
  `(data, prov_X)` will appear in two records.
- During 6a.1, intermediate state has the new key type but still uses
  the shared `entities_scope` for actual data tracking. Releasing one
  record will erroneously corrupt other records sharing data. **This
  is OK** because we don't run tests until 6b; just don't ship this
  intermediate state.
- Stage-7 persistence is independent but flagged in the stage-7 plan
  for layered-FxtrID round-tripping.

## Implementation order rationale

The user asked to combine 6.1+6.2+6.3 into one stage with no tests
in between. Reason given: the three pieces touch the same datatypes
and invariants, so partial states have broken invariants. Tests
land in 6b — both repairing existing-test fallout and adding new
provenance-aware coverage.

We can still pause for review between 6a.1 / 6a.2 / 6a.3 (and within
each, between numbered steps), we just don't run pytest in between.
