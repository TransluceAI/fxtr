# Stage 9 — Provenanced Scopes

## Goal

Make automatic provenance tracking actually flow through fxtr2. Today the
machinery exists in pieces — `FxtrID._provenance_parent` is part of the ID
type, the memory arena already knows how to compute and store `unwraps_to`
chains, and provenance node types (`n` / `c` / `p` / `u`) are well-defined
— but nothing in `FxtrScope` (or the wire layer below it) actually
propagates provenance. The goal of this stage is to plumb provenance
through the scope-level API so that a process running fxtr code can
automatically build up a provenance trace alongside its data, and so that
that trace survives store / load / lift / copy / abandon and crosses the
service-cap RPC boundary.

External persistence (`FxtrNamespace`) is **out of scope** for now: pinning
into a namespace continues to drop provenance, and retrieving from a
namespace continues to produce IDs without provenance parents. Users who
want to persist provenance information will explicitly extract the
provenance parent (a regular `n` / `c` / `p` / `u` ID) and pin that.

## Background — what already exists

- **`FxtrID._provenance_parent`**: an optional sidecar FxtrID. Equality /
  hashing ignore it, but `id_string` / `summary` print it.
- **Provenance nodes**: `n` (contentful), `c` (context), `p` (path), `u`
  (untracked) — each unwraps to a corresponding data node. The
  storage-level relationship is captured by `MemoryStoreNode.unwraps_to`,
  and the arena exposes `unwrap_provenance` / `c_node_to_context_data` /
  `p_node_to_pathroot` / `step_back_p_ctx` for sync provenance reads.
- **`u`-node creation**: `MemoryArena.create_u_node(lease, wrapped_id)` —
  creates a contentless `u`-node wrapping any tracked node.
- **The provenance form is already storable**: `_main_arena.store(cbor)`
  on a CBOR with provenance-level IDs and a provenance-level `$fxtr.ctx`
  produces an `n`-node and automatically populates the `unwraps_to`
  chain to the corresponding `d`-node.
- **`FxtrScope.get_context_marker_id()`**: returns the scope's
  `$fxtr.ctx`. Today this is hard-coded to `"."` for every scope.
- **Cap-IR wire**: `cap_ir_arena._encode_fxtr_id` deliberately strips
  `_provenance_parent` because today it carries no semantics. The precomp
  closure shipped with each `load_flat` reply makes provenance reads sync
  on the receiver side.

## What's missing

`FxtrScope` does not look at `_provenance_parent` anywhere. As a result
provenance can be _attached to a single ID by hand_, but does not survive
even one round-trip through the scope API. Users have no way to express
"this scope represents the c-node X" — every scope's marker is `.` with
no provenance — and the `DataclassEntity` mechanism that auto-fills
`$fxtr.ctx` always reads from the active scope at construction time, not
the destination scope at store time, so an entity stored into a scope
with a different context would silently get the wrong marker.

## Design — what the scope layer should do

The scope layer should behave from the user's perspective as if the
provenance parent isn't there: `store_cbor(cbor) -> id`, `load(id)`,
`is_tracked(id)`, etc. all work in terms of "the data ID" (the value the
user manipulates). But _internally_, the scope always tracks the full
(data, provenance) pair, and the underlying memory arena always stores
the provenance form (which automatically captures the data form via
`unwraps_to`).

The user-facing rule of thumb: **calling code never has to think about
provenance unless it explicitly opts in by attaching a provenance parent
to a scope's context marker, or by manually constructing a FxtrID with a
provenance parent.** When provenance is on, it flows automatically;
when it's off, everything behaves as before.

### Per-scope context entity

A scope can be constructed with a `provenance_context: FxtrID | None =
None` — required to be a `c`-node. The scope unwraps it (must unwrap to
`"."`) and uses `FxtrID(".", _provenance_parent=c_node)` as its context
marker. If `provenance_context` is None the scope copies its parent's
marker; the global scope's marker has no provenance parent.

The `c`-node must be tracked in some scope at construction time. By
default we lift it from a parent scope (same logic as `FxtrScope.lift`);
callers who built the c-node in a sibling can pass
`provenance_context_from_scope: FxtrScope` explicitly. The c-node ends
up tracked in the new scope's main arena from the start, available for
every subsequent `store_cbor`.

### `store_cbor` — provenance-aware path

When `store_cbor(cbor)` is called and `cbor["$fxtr.ctx"].inner_id._provenance_parent`
is set:

1. Walk the cbor and project every `FxtrID` (in marker / weak / strong
   slots) to its "provenance form": if it has a `_provenance_parent`,
   use that; otherwise create (or look up) a `u`-node wrapping it via
   `_main_arena.create_u_node`.
2. The result is a fully-provenance-level CBOR whose `$fxtr.ctx` is the
   scope's c-node. Hand it to `_main_arena.store(...)`. This produces an
   `n`-node ID; the arena automatically tracks the `unwraps_to` link to
   the corresponding `d`-node.
3. Return `FxtrID(d_id, _provenance_parent=n_id)` to the caller.

The underlying memory arena remains unchanged: it sees only data-level
and provenance-level IDs as separate entries (linked by `unwraps_to`),
both tracked under this scope's token.

### `load` / `load_cbor` — provenance-aware path

When the requested `node_id._provenance_parent` is set:

1. Load the cbor for the provenance form (`node_id._provenance_parent`).
2. Walk the result and rewrap each child ID: replace `FxtrID(child_prov)`
   with `FxtrID(arena.unwrap_provenance(child_prov),
   _provenance_parent=child_prov)`. The unwrap is a sync arena read.
3. Return that cbor (or the deserialized entity, for `load`).

When `node_id._provenance_parent` is None we fall through to the existing
behavior unchanged.

### Other scope ops — provenance-aware path

For every other user-facing scope op, an ID-with-provenance-parent is
treated as a single conceptual unit. Because the memory arena tracks
the `unwraps_to` chain as a hard inheritance dependency, attaching
the provenance node automatically also attaches the data node — so
inbound flows only need to mention the provenance ID. Outbound flows
explicitly reference both, since `abandon` doesn't cascade through
`unwraps_to`.

- `is_tracked(id)`: True iff **both** the data ID and the provenance ID
  are tracked in the arena.
- `abandon(id)`: abandons both the data ID and the provenance ID
  (no cascade — abandoning is a deliberate release).
- `lift(id)`: lifts the provenance ID; the arena auto-attaches the
  data ID via `unwraps_to`.
- `copy_from_scope(id, source)` / `track_from_scope(id, source)`: copy
  / track the provenance ID; same auto-attach as `lift`.
- `_ref_loaded_value_cache`: keyed on `(data_id, provenance_parent)`
  rather than `data_id` alone, so the same data ID under two different
  provenance traces caches separately.

### Helper functions for crossing the layers

Two new sync helpers on `FxtrScope`, both 1-1 inverses:

- `scope.extract_provenance_parent(data_with_prov: FxtrID) -> FxtrID`:
  given a FxtrID that carries a provenance parent, return the bare
  provenance-level FxtrID (no `_provenance_parent` set; the caller is
  now operating on that node as data). Raises if there's no parent.
- `scope.embed_as_provenance_parent(prov_node: FxtrID) -> FxtrID`:
  given a provenance node tracked in this scope, look up its
  `unwraps_to` (sync), and return `FxtrID(unwrapped,
  _provenance_parent=prov_node)`. Raises if the node isn't a provenance
  node.

These are how user code (and downstream pipeline machinery) explicitly
moves between "the trace is automatic" and "the trace is data."

### `DataclassEntity.fxtr_ctx` side fix

`DataclassEntity.fxtr_ctx` becomes `RawFxtrEntityMarkerID | None = None`.

- New entities don't capture a context at construction time. At
  `_fxtr_store_impl` time, if `fxtr_ctx is None`, we read
  `scope.get_context_marker()` and inject it into the cbor before
  calling `scope.store_cbor`.
- Loaded entities preserve whatever `fxtr_ctx` was in the cbor (so a
  load → store round-trip retains the original marker). The load
  handler enforces that the cbor has a `$fxtr.ctx` field.

This is independently useful — it fixes the latent bug that an entity
constructed in one scope and stored into a different scope would store
the wrong marker — and it's a prerequisite for provenance flow because
the marker is what carries the scope's c-node into `store_cbor`.

### Service-cap wire crossing

When a service call ships a `FxtrID` that carries a `_provenance_parent`,
the wire form changes: instead of stripping the parent (today's behavior),
we ship _just the parent_ as a distinct tagged form. The receiver decodes
by sync-unwrapping the parent on its arena to recover the data ID, and
reconstructs `FxtrID(data, _provenance_parent=parent)`. This needs:

- A new wire tag (e.g. `IRTagged("fxtr_id_with_prov", bytes)`) carrying
  the provenance parent's `_raw_data_id`.
- The receiver-side arena must be able to serve `unwrap_provenance` on
  that parent ID synchronously at decode time. The `fxtr_arena` packet
  becomes self-describing: `(handle, tracked_ids, precomp)` ships
  alongside the handle, so the receiver can construct its `CapIRArena`
  fully populated **before** decoding any arg leaves. Same applies to
  `copy_to_new_arena` replies (forks need precomp too).
- Service-call dispatch scope inherits its c-node from the **mount
  scope** (not the caller scope) — provenance reflects "where the
  code is running." This falls out of the per-scope-context-entity
  inheritance rules from Stage 1 with no extra wiring.

## Out of scope (deferred)

- Persistence of provenance through `FxtrNamespace`. Pin/retrieve still
  drop provenance; users wanting to keep provenance must explicitly
  extract the parent and pin it.
- Stripping provenance on outbound store-to-namespace operations is
  already implicit (today's storage path drops `_provenance_parent`),
  so no change is required there — but we should add a regression test
  asserting it stays that way.
- Generalizing `provenance_context` beyond c-nodes (e.g. p-nodes,
  u-nodes). For now we require c.

## Staging

Four stages, each independently shippable:

1. **`stage1_context_plumbing.md`** — `DataclassEntity.fxtr_ctx`
   side fix; per-scope context entity override; helper functions for
   moving between provenance and data layers. No change to storage /
   load semantics yet — provenance parents on FxtrIDs still don't flow.
   This stage is mostly mechanical and lays the foundation.

2. **`stage2_single_process_provenance.md`** — extend `store_cbor` /
   `load` / `load_cbor` / `lift` / `is_tracked` / `abandon` /
   `copy_from_scope` / `track_from_scope` and the ref cache to handle
   `_provenance_parent`. Memory arena is untouched. End-to-end tests
   verify a single-process round-trip through provenanced scopes.

3. **`stage3_services_wire.md`** — wire-encoding for FxtrID-with-prov
   over service calls; `ArenaIRHandleAdapter` initial-fork precomp;
   `CapIRArena` decoder updates. End-to-end tests verify provenanced
   IDs survive a ServiceRef round-trip.

4. **`stage4_service_context_marker.md`** — reserves the wire
   method-name prefix `"$fxtr."` (with `"$fxtr.ctx"` as the first
   user) so callers can ask a resolved service for its mount scope's
   context marker. Server-side: every builtin impl handles it via a
   `_handle_invoke` prefix check delegating to a
   `_handle_framework_introspection` helper on `CommonServiceImpl`.
   Proxy-side: untyped callers use
   `getattr(proxy, "$fxtr.ctx")()` (existing `__getattr__` already
   supports it, no change needed); typed callers opt in via a new
   `@proxy_alias_method(wire_name)` decorator on a Protocol method.
   No changes to `ServiceRef`, mount, or attach. Marker is fetched
   lazily on demand, riding the existing fxtr_call/fxtr_return wire
   shape.

## Open questions / follow-ups

- The c-node-only restriction on `provenance_context`. Likely fine for
  this stage; lifting the restriction would need a story for what
  unwrapping a non-c node into `.` means.
- Caller-scope c-node propagation through service calls. The default
  is mount-scope inheritance (per the Service-cap section above);
  caller-scope propagation can be added as an opt-in extension once a
  concrete use case appears.
- Wire packet shape for the self-describing arena (explicit
  `tracked_ids` vs. walk-args-twice). Picked self-describing in
  Stage 3 §1 for clarity; revisit if precomp / tracked_ids size is a
  problem in practice.
