# Part 5 — Bug fix 1: precomp/liveset divergence

Adjacent to [`part5_plan.md`](./part5_plan.md), which introduced
`ArenaIRHandleAdapter` / `CapIRArena` / `CapIRArenaRemoteState` and the
HTTP-style precomp map. This bugfix addresses a subsequent gap that
appeared once the precomp map was extended to ride alongside arena
packets at construction time (`initial_precomp` on the client,
`compute_precomp_closure` on the server).

Some sections of `part5_plan.md` may be slightly out of date with the
current code; this doc treats the **current code** as the starting
point and fixes the divergence it allowed.

## The bug

A workstation client receives a layered FxtrID `(d, u(d))` from a cap
method that pinned to a SQLite namespace, restarted the server, and
retrieved. Round-trip through `cap.retrieve` produces the layered ID,
but a follow-up `EntityRef(layered, Note).load(scope)` raises:

```
ValueError: Node d… is not tracked in the arena!
  at memory_arena.py:2543 (MemoryArena.load_flat)
  via cap_ir_arena.py:502 (ArenaIRHandleAdapter._handle_load_flat)
```

That is: the wrapped arena on the server side does not track `d`, even
though the client's local state correctly thought `d` was reachable
(via the precomp it received). When the client does
`load_flat(d)` through the bridge, the server rejects.

Tracked in `projects/fxtr_workstation/plans/phase0/part1_ws_server/stage7_persistence.md`
as `test_preserve_prov_restart_durability` — currently `xfail`.

## What's being violated

The system targets three invariants:

- **(a) Sync queries on tracked IDs work, and their result becomes
  tracked.** This is what precomp is for: the client can synchronously
  unwrap / step provenance on any tracked ID without a wire round-trip,
  and the result of those reads gets adopted into the local liveset.
- **(b) Tracked IDs are loadable.** Anything in the local liveset can
  be `load_flat`'d (sync precondition + the wire request goes through).
- **(c) Server tracks ⊇ client tracks.** Whatever the client thinks it
  has access to, the server's wrapped arena must currently be tracking
  too — otherwise the server can reject a wire request that the
  client's contract says should succeed.

`compute_precomp_closure` was added to enable (a) without needing a wire
round-trip per provenance read. But the way it's currently wired
into arena packet construction lets (a) silently break (c): the server
ships a precomp that promises "you can sync-walk to ID Y" without
having committed Y to its own arena's liveset. The client then walks
to Y locally (via precomp) and tries to `load_flat(Y)` (per (b)) — the
server says "I don't track Y" and rejects.

### The exact divergence point

In both `_emit_call_result_packet` (`fxtr2/services/service_cap_common.py`)
and `fork_arena_handle_for_ids` (`fxtr2/data/entity_ref_scope.py`):

```python
sub_arena = source_arena.copy_to_new_arena(lease, list(seed_ids))
fxtr_arena_handle = ArenaIRHandleAdapter(sub_arena, ...)
precomp = compute_precomp_closure(source_arena, lease, seed_ids)
fxtr_arena_packet = _build_fxtr_arena_packet(
    handle=fxtr_arena_handle,
    tracked_ids=seed_ids,
    initial_precomp=precomp,
)
```

`copy_to_new_arena` only tracks `seed_ids` in `sub_arena`'s liveset.
`compute_precomp_closure` walks the source arena, auto-tracking visited
IDs in the *source's* liveset (via `MemoryArena.unwrap_provenance`'s
`_track_from_store_or_none`), but `sub_arena` is already a separate
fork with its own liveset — those auto-tracks don't reach it.

On the wire, the receiver gets `tracked_ids` (as initial liveset for
the `CapIRArena`) plus a precomp map that reaches IDs *outside*
`tracked_ids`. Any of those out-of-band precomp values is a "ghost":
the client knows about it via precomp, but the server's wrapped arena
doesn't track it.

For the workstation persistence cap: a result of `(d, u(d))` is
encoded with `_arena_id(layered) = u(d)` — only the prov parent goes
into `seed_ids`. The precomp closure visits `u(d) → d`, so the client
learns about `d` and can sync-unwrap. But the sub_arena tracks only
`{u(d)}`. When the client's `EntityRef.load` chain ultimately wants
`d`'s bytes, it dispatches `load_flat(d)` over the wire — the server's
sub_arena rejects.

The same pattern exists for `_handle_load_flat`'s reply precomp
(`cap_ir_arena.py:506`), but there the precomp is computed on the
*same* arena that's serving subsequent loads, so the auto-tracking
from `unwrap_provenance` lands in the right place. Adapter-construction-
time precomp (the divergent case above) is the buggy site.

## The fix, three layers

### Layer 1 — server-side: precomp ⇒ arena tracks

Every ID in any precomp the server has shipped to the client must be
tracked in the wrapped arena, for as long as that precomp is in scope
on the client. That's the contract restoration for (c).

Concretely:

- **Rename and re-target `compute_precomp_closure` →
  `compute_and_track_precomp_closure(arena, lease, seed_ids)`.** Same
  walk; now explicitly documents that it auto-tracks every visited
  ID in `arena`'s liveset (which `MemoryArena` / `SQLiteNamespaceArena`
  already do via their `unwrap_provenance` etc.; the doc + name pin
  the contract). Returns the IR-encoded entry list as before.

- **At the two adapter-construction sites
  (`_emit_call_result_packet`, `fork_arena_handle_for_ids`), call the
  helper on the sub_arena fork rather than the source arena.** The
  fork's token aliases the source's, so the lease validates and the
  precomp answers identically. The fork's liveset gains every visited
  ID before the handle is exposed.

- **Document the contract on `ArenaIRHandleAdapter`.** Its wrapped
  arena must track every ID that has been emitted in any precomp that
  the connected `CapIRArena` may still hold — including the
  initial-precomp shipped at construction, and the per-`load_flat`
  reply precomps. Already true for the load_flat path; this fix makes
  it true for construction too.

### Layer 2 — client-side: precomp values count toward refcount

Today, `CapIRArenaRemoteState._refcounts` counts only "this id is
tracked by some sibling `CapIRArena`'s liveset". It does not count
the per-precomp-entry references. That makes
`release` over-eager: an ID could hit zero on the local-arena side
while still being reachable via precomp values, and the resulting
wire `abandon` removes it from the server arena while the client
still believes it's available.

Fix:

- **`_refcounts` accounts for precomp.** Per ID, the count is
  `(number of sibling arenas tracking it) + (number of precomp
  entries whose value points at it)`. Concretely: `_ingest_precomp`
  bumps the count on every `unwraps_to` / `ctx_node` /
  `step_back_p_ctx` *value* (each one is a +1; an ID appearing as a
  value in three different entries gets +3 — these are independent
  cited references). The `id` field of an entry doesn't add to the
  count by itself; the entry's owner (the arena fork or load_flat
  reply that prompted us to ingest) is the one that contributes.
  The shipped `initial_tracked` field still seeds +1 per id as
  today; the precomp-value contribution is on top.

- **`release` only fires `abandon` when the full count hits zero.**
  Today, hitting zero in `_refcounts` triggers `_drop_to_zero`
  immediately. With precomp values counted, the count won't hit
  zero until the precomp entries citing this ID are also dropped
  (which happens via `_drop_to_zero` itself for the entry's own
  `id`). The cascade naturally orders: an entry's `id` only drops
  to zero when nothing else references it; that drop releases the
  +1 contributions on its values; those values' counts then drop;
  etc.

- **Sync ops on `CapIRArena` verify the result is already in
  `_refcounts`.** A defense-in-depth check: if `unwrap_provenance` /
  `c_node_to_context_data` / `p_node_to_pathroot` /
  `step_back_p_ctx` returns an ID whose precomp entry is present
  but whose refcount is somehow zero, that's a bug in the
  bookkeeping above and we want to fail loudly rather than ship a
  silent UAF to the wire.

- **`_track_discovered`** (called from sync ops) becomes a
  no-op-or-bump rather than always-bump: the precomp-value bump
  already added the +1, so adopting an ID into a sibling's liveset
  is a "first sibling" check (idempotent for the second / third
  sibling that picks up the same id).

### Layer 3 — race-safe abandon (Cap-IR style)

Independent of the precomp issue, the wire abandon protocol is
vulnerable to a TOCTOU race between abandon and re-shipping: the
server can be in the middle of dispatching a load_flat reply (or
an arena-construction packet) that re-introduces id X, while the
client's release-side path concurrently fires `abandon(X)`. On
arrival, the abandon would remove the (now legitimately re-tracked)
X from the server arena.

`fxtr2/rpc/lowering_types.py` already solves this for cap dispatch
via "introduction refcount":

- The server stamps each shipping with an introduction refcount
  (how many times *this server* has handed `X` to *this client*).
- The client maintains a local "remote refcount": +1 per stamped
  introduction observed.
- Wire abandon includes the client's current remote refcount; the
  server's shared state decrements by exactly that amount. Any
  introductions in flight at abandon time were stamped with a
  later number and survive the decrement.

We adopt the same shape here for the arena protocol:

- **`ArenaIRHandleAdapter` stamps each emitted introduction.**
  Every `_handle_load_flat` reply, every `compute_and_track_precomp_closure`
  ingestion (i.e., the initial-precomp shipped at construction
  via the new helper), and any future ID-shipping path must
  carry an introduction refcount per id. Concretely: the server
  keeps a `_total_introductions: dict[FxtrID, int]` and
  emits `introduced[id] = current_count + 1` (post-bump) for
  every id newly cited.

- **`CapIRArenaRemoteState` tracks the remote refcount.** A new
  `_remote_refcounts: dict[FxtrID, int]` field mirrors the
  server's `_total_introductions` from the client's perspective.
  Bumped on each ingestion observed, decremented to zero on
  abandon dispatch.

- **Wire abandon carries the remote refcount.** Today
  `"abandon"` ships `Sequence[tagged FxtrID]`; we extend the arg
  to `Sequence[(tagged FxtrID, int)]`. Server's
  `_handle_abandon` decrements `_total_introductions` by the
  shipped count and, if the result is zero, abandons on the
  underlying arena.

This protects against the race wholesale and aligns the arena
protocol with cap protocol.

## What stays the same

- `compute_and_track_precomp_closure`'s walk is unchanged. The
  rename + sub_arena retargeting is the only on-server semantic
  change at Layer 1.
- `_handle_load_flat`'s precomp-on-self code path needs no change
  (it already auto-tracks because it runs on `self._arena` =
  sub_arena).
- The wire shapes for `tracked_ids` and `initial_precomp` are
  unchanged.
- `ingest_precomp`'s job is still "populate the three precomp
  caches"; the refcount-bump side effect is added at the
  call site (`CapIRArenaRemoteState._ingest_precomp`) rather
  than inside the helper (which has other callers in the
  experimental http path).

## Test plan

Three layers of test, in order:

### Red test (lands first)

`tests/test_cap_ir_arena.py::TestPrecompTracksOnServer::test_load_flat_for_precomp_value_succeeds`

Pure-MemoryArena reproduction:

1. Source MemoryStore + arena. Store a d-tag `Note`; create `u(d)` via
   `arena.create_u_node(d)`. Both tracked.
2. `seed_ids = {u(d)}` (mirrors `_arena_id` for a returned
   layered FxtrID).
3. Build the wire packet manually: `sub_arena =
   source.copy_to_new_arena(lease, seed_ids)`,
   `precomp = compute_precomp_closure(...)` — current code.
4. Wrap `sub_arena` in `ArenaIRHandleAdapter`. Build a `CapIRArena`
   with `initial_tracked=seed_ids`, `initial_precomp=precomp`.
5. Sync `unwrap_provenance(u(d))` on the client → returns d (via
   precomp). d is now in client's liveset.
6. `await client.load_flat(d)` — currently raises
   "Node d… is not tracked in the arena" on the server. After Layer 1
   fix, succeeds.

### Refcount-correctness test

`tests/test_cap_ir_arena.py::TestPrecompTracksOnServer::test_precomp_values_keep_id_alive_through_release`

After Layer 2 lands:

1. Same setup, but on the client side after sync-unwrap, immediately
   abandon `u(d)` on the client.
2. Without Layer 2: u(d) refcount → 0, fires wire abandon, server
   removes u(d) from arena. The next `load_flat(d)` (still a valid
   precomp value) fails because the precomp citing d is gone /
   the abandon sweep purged the entries.
3. With Layer 2: `u(d)`'s refcount has +1 from the precomp entry's
   `id` field plus +1 for d's `unwraps_to` value, so the local
   abandon's release path sees count > 0 (still cited) and
   doesn't fire wire abandon. d remains loadable.

(Final shape may simplify; this test is the demonstration that the
refcount accounting is correct under the cited cases.)

### Race-safe abandon test

`tests/test_cap_ir_arena.py::TestPrecompTracksOnServer::test_concurrent_introduction_and_abandon`

After Layer 3 lands:

1. Pause the wire dispatch path so we can interleave.
2. Server is mid-emission of a second introduction of id X (e.g., via
   a second arena packet) while the client fires abandon(X) for the
   first introduction.
3. The wire abandon's introduction-refcount matches what the client
   observed; the server's `_total_introductions[X]` is greater
   (the new emission already bumped it); decrement leaves > 0;
   id stays tracked. The next load_flat(X) succeeds.

### Workstation re-enable

Drop the `xfail` on
`projects/fxtr_workstation/tests/test_persistence_7.py::test_preserve_prov_restart_durability`.
With Layer 1 fixed it should pass.

### Existing low-level tests

Audit `tests/test_cap_ir_arena*.py`, `tests/test_services/`, and any
other call sites of `compute_precomp_closure` for assertions that
encode the old (broken) semantics. Adjust assertions where they
implicitly relied on sub_arena.liveset = seed_ids only.

## Implementation steps

The plan is to land Layers 1+2+3 together — the user-facing fix is
Layer 1, but Layers 2+3 are correctness gaps that the same code
paths cover, so doing all three at once avoids touching the same
files twice.

- [x] **0** — Plan + red test landed. Test
      ``tests/test_cap_ir_arena.py::TestPrecompTracksOnServer
      ::test_load_flat_for_precomp_value_succeeds`` reproduces the
      divergence at the cap_ir_arena layer (no workstation/RPC).
- [x] **1.1** — Renamed ``compute_precomp_closure`` →
      ``compute_and_track_precomp_closure``. Docstring now calls
      out the auto-tracking contract. Both adapter-construction
      call sites
      (``service_cap_common._emit_call_result_packet``,
      ``entity_ref_scope.fork_arena_handle_for_ids``) now call
      the helper on the sub_arena fork. Internal ``cap_ir_arena``
      callsites updated for the rename.
- [x] **1.2** — ``experimental/http/asgi_app.py`` keeps its own
      private ``_compute_precomp_closure`` (no rename); it computes
      the closure on the same arena it just loaded from, so the
      Layer-1 divergence doesn't apply. Each HTTP request is
      independent; no long-lived bridge → Layers 2+3 don't apply.
- [x] **1.3** — Red test now passes; dropped xfail on
      ``projects/fxtr_workstation/tests/test_persistence_7.py
      ::test_preserve_prov_restart_durability`` and confirmed it
      goes green.
- [x] **2.1** — ``CapIRArenaRemoteState._ingest_precomp`` rewritten
      inline (no longer delegates to the public ``ingest_precomp``)
      so it can bump ``_refcounts`` for every newly-installed
      value (idempotent on re-ingest of the same entry).
      ``_drop_to_zero(nid, zero_queue)`` cascades through the
      precomp entries keyed by ``nid``, decrementing those values
      and recursing if any hits zero. Each id whose refcount hits
      zero in the cascade is appended to ``zero_queue`` for the
      caller to dispatch as a single batched wire abandon.
- [x] **2.2** — ``CapIRArena._track_discovered`` is unchanged in
      shape (still idempotent-bump per sibling); the per-precomp
      contribution is the +1 added by ``_ingest_precomp_slot``,
      which sits on top.
- [x] **2.3** — ``CapIRArena._track_discovered`` adds a
      defensive ``RuntimeError`` if the discovered id has no
      ``_refcounts`` entry — would indicate a Layer-2 bookkeeping
      bug, fail loudly.
- [x] **2.4** — Test
      ``test_precomp_value_keeps_id_alive_until_cited_by_disappears``
      validates the cascade.
- [x] **3.1** — Added ``_total_introductions: dict[FxtrID, int]``
      to ``ArenaIRHandleAdapter`` plus ``register_emission(ids)``
      method. Wire-packet builders
      (``service_cap_common._emit_call_result_packet``,
      ``entity_ref_scope.fork_arena_handle_for_ids``) and
      ``_handle_load_flat`` itself call ``register_emission``
      after assembling the payload, with the union of all ids
      mentioned (``tracked_ids`` ∪ precomp keys/values, or
      reply ids ∪ deps ∪ precomp keys/values). Helper
      ``collect_precomp_ids(precomp)`` and
      ``_collect_load_flat_reply_ids(flat, precomp)`` factor the
      enumeration so server and client use the same rule.
- [x] **3.2** — Wire schema bumped: ``"abandon"`` arg is now
      ``Sequence[Mapping[{"id", "count"}]]``. Encoder updated in
      ``CapIRArenaRemoteState.release``; decoder/handler updated
      in ``ArenaIRHandleAdapter._handle_abandon`` (validates each
      entry, decrements ``_total_introductions[id]`` by ``count``,
      and only abandons on the underlying arena when the counter
      reaches zero).
- [x] **3.3** — ``CapIRArenaRemoteState._remote_refcounts``
      mirrors observed introductions, bumped at construction
      (``_observe_emission`` over the initial packet's id union)
      and on every ``load_flat`` reply (``_observe_emission`` over
      the reply's id union). Wire abandon ships the local
      ``_remote_refcounts[id]`` per id and resets to zero.
- [x] **3.4** — Test
      ``test_abandon_does_not_reclaim_id_with_recent_introduction``
      validates the race protection.
- [x] **4** — Updated
      ``v2/fxtr2/services/LOCAL_CODE_OVERVIEW.md`` cap_ir_arena
      section with the new contracts. Existing
      ``data/LOCAL_CODE_OVERVIEW.md`` reference to
      ``fork_arena_handle_for_ids`` doesn't need text changes;
      the introduction-refcount + precomp-tracking story is
      covered in the cap_ir_arena LOCAL.

## Notes / open questions

- Layer 2 can in principle be split from Layer 1: the immediate
  user-visible bug closes at Layer 1 because the server-side
  arena tracks the precomp values, so even if the client's
  precomp-driven access miscounts, the server says yes. But
  Layer 2 closes a real correctness gap once the workstation has
  multiple in-flight calls aliasing the same id, so doing it now
  is cheaper than later.
- Layer 3 is mostly orthogonal but lives in the same files.
  Worth doing now to align the arena protocol with the cap
  protocol's race-safety pattern; otherwise the divergence is a
  trap for whoever next touches `_handle_abandon`.
- `compute_and_track_precomp_closure` running on the sub_arena
  fork relies on the source arena exposing `unwrap_provenance` /
  etc. that auto-track. `MemoryArena` does. `SQLiteNamespaceArena`
  does (via `_track_from_precomputed`). Other arena impls (HTTP
  client arena? CapIRArena itself?) need an audit. If any don't,
  document and either fix or scope-away the precomp helper for
  that arena.
