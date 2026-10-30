# Stage 9 — Stage 3: Services / wire crossing (+ CapIRArena local-fork refactor)

After Stage 2, provenance flows correctly through `FxtrScope` operations
in a single process. This stage extends the propagation across the
`ServiceRef` boundary so that a service call in one process can pass
or return a `FxtrID` carrying a provenance parent, and the receiver
sees the same `(data_id, prov_id)` pair on its side.

This stage also folds in a related optimization: the `CapIRArena` ↔
`ArenaIRHandleAdapter` pairing is changed from "1:1 with a wire `fork`
method" to "1:N (many client arenas share one wire handle) with local
refcounted forks." The two changes interact closely (initial-fork
precomp delivery, removal of the wire-fork reply, and the
self-describing arena packet for sync-decoding prov-bearing IDs) so
they ship together.

## Background — what's there today

- `cap_ir_arena._encode_fxtr_id(id)` strips `_provenance_parent`
  unconditionally and ships `IRTagged("fxtr_id", bytes(id))`. Decoding
  reconstructs `FxtrID(raw_data_id, _provenance_parent=None)`.
- `services/service_cap_common.py:_encode_fxtr_service_data` /
  `_decode_fxtr_service_data` use the `FXTR_ID_TAG` for any
  `FxtrID` / `EntityRef` leaf.
- `ArenaIRHandleAdapter` exposes `Call("load_flat", ...)`,
  `Call("abandon", ...)`, and `Call("copy_to_new_arena", ...)` over
  the wire. `load_flat` computes a closed precomp closure and ships
  it inside the reply, which the client's `CapIRArena._ingest_precomp`
  uses to populate three sync caches (`_unwraps_to_cache`,
  `_ctx_node_cache`, `_step_back_p_ctx_cache`). These caches make the
  abstract `ReadableArena`'s sync provenance reads
  (`unwrap_provenance`, `c_node_to_context_data`, `p_node_to_pathroot`,
  `step_back_p_ctx`) work over the wire.
- The wire fork (`copy_to_new_arena`) creates a fresh server-side
  `MemoryArena` view + `ArenaIRHandleAdapter`, shipped back as the
  reply's IRFuture. The client wraps it with a sibling `CapIRArena`
  whose `_remote` is that future.
- The arg-packet flow in `service_cap_common.py` forks an input arena
  off the home scope's main arena, wraps it with
  `ArenaIRHandleAdapter`, and ships it as `fxtr_arena` on the receiver.
  The receiver's `CapIRArena.direct(...)` (or `derived` /
  `with_converter`) reads from this remote handle. Today the receiver's
  CapIRArena gets the initial liveset from `initial_tracked` (a
  Python-side list passed in at construction) but does **not** receive
  any precomp for those IDs — it just knows the IDs exist. That's fine
  today because nothing in the receiver's path needs to sync-read
  provenance on initial liveset IDs without a `load_flat` round-trip.

After Stage 2, however, the receiver decoder will want to call
`unwrap_provenance` on a wire-supplied provenance parent ID
synchronously, before any `load_flat`. So the precomp closure has to
travel _with the arena packet_, not just with later load replies.

## CapIRArena refactor — shared `CapIRArenaRemoteState` + local forks

### Motivation

The current `CapIRArena` does a wire dispatch per fork and per abandon.
Forks create a server-side `MemoryArena` sub-view plus a wrapping
adapter; abandons cross the wire individually. Almost none of this
information is actually needed on the server side: the server just
needs to know which nodes the *aggregate of all clients* still cares
about, so it can answer `load_flat` and eventually let the wrapped
`MemoryArena` go.

The new design: lift the bookkeeping that distinguishes "different
client-side views of the same server-side arena" entirely to the
client. The server keeps a flat liveset (managed via wire `abandon`s);
the client decomposes that into refcounted shares across as many
sibling `CapIRArena`s as the user wants.

### `CapIRArenaRemoteState`

A new internal class owning everything that's shared across siblings —
including the lifetime/brand state, since all siblings forked from
one factory call share the same Z/X/derived configuration:

```
class CapIRArenaRemoteState[B_Z: LifetimeBrand]:
    # Lifetime / brand state (shared across siblings).
    _z_token: LifetimeToken[B_Z]
    _derived: DerivedLocalLifetime[Any] | None
    _x_token: LifetimeToken[Any] | None
    _bridge_state: _CapIRArenaBridgeState | None     # at-rest leases for Z→Y conv

    # Wire endpoint.
    _remote: IRHandle[Any]                           # concrete handle (no IRFuture)
    _remote_lease: Lease[Any] | None                 # discarded when refcounts empty

    # Tracked-id bookkeeping (shared across siblings).
    _refcounts: dict[FxtrID, int]                    # per-ID share count

    # Sync provenance caches (shared across siblings).
    _precomp_unwraps_to: dict[FxtrID, FxtrID]
    _precomp_ctx_node: dict[FxtrID, FxtrID]
    _precomp_step_back_p_ctx: dict[FxtrID, FxtrID]

    _closed: bool
```

Responsibilities:

- **Lifetime / lease conversion.** `precheck(z_lease)` validates the
  caller's lease subject against `_z_token`; `make_y_lease(z_lease)`
  is the existing `CapIRArena._make_y_lease` logic, now centralized
  here since the bridge state is shared.
- **Remote dispatch.** `request_remote(z_lease, request)` —
  combination of `_make_y_lease` + `_dispatch_remote` + await; same
  shape as today's `CapIRArena._request_remote`.
- **Refcount management.** `acquire(ids)` and `release(ids)` adjust
  the counts. When a release drops a count to zero, the wire
  `abandon` is dispatched as a single batched call covering every ID
  that hit zero in that release pass, and the precomp entries for
  those IDs are purged.
- **Lease discipline.** When the last per-ID refcount drops to zero,
  `_remote_lease` (if any) is discarded; the remote `IRHandle` is
  released as soon as the last referencing `CapIRArena` goes away.
- **Precomp ingestion.** `ingest_precomp(entries)` — same shape as
  today's module-level `_ingest_precomp` helper, just operating on
  the shared caches.

The state's `close()` is idempotent and is invoked when the last
refcount drains (lease + bridge state get torn down there).

### `CapIRArena` becomes a thin share

Per-arena fields are just the local liveset slice + a strong ref to
the shared state:

```
class CapIRArena[B_Z: LifetimeBrand](ReadableArena[B_Z]):
    _remote_state: CapIRArenaRemoteState[B_Z]   # shared with siblings
    _liveset: set[FxtrID]                       # local subset of remote_state._refcounts
    _closed: bool
```

Public-API methods are thin wrappers: validate the lease + that the
ID is in the local liveset, then delegate to `_remote_state`.
`token` returns `self._remote_state._z_token`.

### Factory shape (unchanged from the user's POV)

`CapIRArena.direct(...)`, `CapIRArena.derived(...)`,
`CapIRArena.with_converter(...)` continue to take the same args.
Internally each constructs one fresh `CapIRArenaRemoteState`
(populating its lifetime/brand fields per-mode, the same way today's
`CapIRArena.__init__` does) and then one `CapIRArena` referencing it.
The `remote` parameter type is narrowed from `IRHandle |
IRFuture[IRHandle, ...]` to plain `IRHandle` (the fork-of-fork
IRFuture variant is no longer produced, and we have no external
caller of that shape).

`initial_tracked` populates both `_remote_state._refcounts` (each ID
starting at 1) and the new arena's `_liveset`.

### `copy_to_new_arena` becomes local

The whole method is a sync local op:

```
def copy_to_new_arena(self, lease: Lease[B_Z],
                     node_ids: Collection[FxtrID]) -> CapIRArena[B_Z]:
    self._precheck(lease)
    with lease.take():
        for nid in node_ids:
            if nid not in self._liveset:
                raise ValueError(...)
        sibling = CapIRArena._sibling_of(self, initial_tracked=node_ids)
        # _sibling_of bumps _remote_state._refcounts[nid] for each nid
        # and seeds sibling._liveset with the same set.
        return sibling
```

No wire dispatch; no `IRFuture[IRHandle, ...]` plumbing; no bridge
snapshot (the bridge state lives on the shared `_remote_state` and
just accumulates one more user). The lease is consumed only because
the abstract `ReadableArena` interface requires it (validates the
arena is alive and the lease subject matches).

### `abandon`

```
def abandon(self, lease, node_id):
    self._precheck(lease)
    with lease.take():
        if node_id not in self._liveset:
            raise ValueError(...)
        self._liveset.discard(node_id)
        self._remote_state.release([node_id])    # batched API; single-id list
        if not self._liveset:
            self.close()                          # cascades the rest
```

`release([nid])` decrements refcounts and, if any hit zero, dispatches
a single wire `Call("abandon", [list_of_ids])`. The wire `abandon` is
extended to accept a list; the existing single-ID form is dropped (one
caller, easy to migrate).

### `close`

```
def close(self):
    if self._closed: return
    self._closed = True
    leftover = self._liveset
    self._liveset = set()
    self._remote_state.release(leftover)
```

The remote state's release path handles dispatching abandons for
zero-hit IDs *and* discarding the remote_lease + bridge state when
everything is drained.

### Server-side: `ArenaIRHandleAdapter`

- Drop the `"copy_to_new_arena"` method entirely (the `_handle_fork`
  helper goes too). Document `ArenaIRHandleAdapter` as supporting only
  `load_flat` and `abandon` (with a list-of-IDs arg for the latter).
- Extend `_handle_abandon` to decode a `Sequence[IRTagged(FXTR_ID_TAG,
  bytes)]` and call `arena.abandon(lease.clone(), nid)` per ID. Keep
  the operation idempotent on already-abandoned IDs by tolerating
  the underlying-arena `KeyError`/`ValueError` shape (TBD during
  implementation; concrete failure mode picked once the tests run).

### Tests

- Update `test_cap_ir_arena.py`'s `TestArenaIRHandleAdapterFork`
  class: assertions about the wire fork go away; replaced by tests
  that the wire method is rejected as unknown.
- Add `TestCapIRArenaLocalFork`: forking does not touch the wire,
  refcounts move correctly across siblings, the wire abandon fires
  exactly once when the last sibling drops a node, the
  remote_lease drops after the last refcount hits zero, etc.
- Existing `TestForkForm` tests (which exercise pipelined remote =
  IRFuture) have nothing to assert anymore once the IRFuture variant
  is gone — most of that file's "fork-form" coverage migrates to
  TestCapIRArenaLocalFork.

## Stage 3 pieces (provenance over the wire)

### 1. New wire form for FxtrID-with-provenance

**File: `fxtr2/data/cap_ir_arena.py`**

Introduce a new tag, `FXTR_ID_WITH_PROV_TAG = "fxtr_id_with_prov"`,
carrying just the provenance parent's `_raw_data_id` as bytes:

```
encode(id):
    if id._provenance_parent is None:
        return IRTagged(FXTR_ID_TAG, bytes(id))
    else:
        return IRTagged(FXTR_ID_WITH_PROV_TAG, bytes(id._provenance_parent))

decode(value, *, arena, lease):
    if tag == FXTR_ID_TAG:
        return FxtrID(raw_data_id=bytes_value)
    if tag == FXTR_ID_WITH_PROV_TAG:
        prov_id = FxtrID(raw_data_id=bytes_value)
        data_id = arena.unwrap_provenance(lease, prov_id)
        if data_id is None:
            raise IRException(category="MalformedPacket",
                              message="provenance ID not known to arena")
        return FxtrID(raw_data_id=bytes(data_id),
                      _provenance_parent=prov_id)
```

Decoding requires `arena + lease` in scope. In the load_flat reply
path the arena is the receiver's `CapIRArena` itself; in the service
arg path it's the receiver's call-scope arena (the one wrapped via
`CapIRArena.direct` / `derived` / `with_converter`).

Update both the helpers in `cap_ir_arena.py` (used by
`_compute_precomp_closure`, `_decode_flat_data`, etc.) and the helpers
in `service_cap_common.py:_encode_fxtr_service_data` /
`_decode_fxtr_service_data` to use the new encoding.

The provenance parent itself is just a regular FxtrID (tag `n` / `c`
/ `p` / `u`) — when collecting `fxtr_ids` for the input-arena fork in
the service path, we add the provenance parent (not the data ID) to
the set, since that's the ID the arena needs to make available. The
data ID is implicit (recoverable via sync `unwrap_provenance` once
the receiver has the prov parent + its precomp).

**`_compute_precomp_closure` is unchanged.** It already encodes only
arena-side bare FxtrIDs (`FxtrID` instances without a
`_provenance_parent`). The places that produce IDs *with* prov
parents are upstream of the BFS (i.e. service args), and the BFS
itself walks bare provenance / data IDs.

**Decode order.** With the new tag, every `FXTR_ID_WITH_PROV_TAG`
leaf needs an arena populated *enough that* `unwrap_provenance(prov)`
returns the data ID at the moment of decode. The arena is forked off
the home scope by the sender, so the sender knows up-front exactly
which IDs are reachable; we ship that information explicitly with the
arena packet (see §2 below) so the receiver can construct its
`CapIRArena` fully populated *before* args are decoded.

### 2. Self-describing `fxtr_arena` packet

**File: `fxtr2/services/service_cap_common.py`**

The wire form of `fxtr_arena` changes from a single `IRHandle` to a
mapping:

```
"fxtr_arena": IRTagged("fxtr_arena", {
    "handle": IRHandle[...],
    "tracked_ids": [tagged FxtrID, ...],   # bare prov IDs (no provenance parent)
    "initial_precomp": [<precomp entries>], # closure over tracked_ids
})
```

Sender (proxy + return-resources path) builds it as:

```
fxtr_ids_for_arena = {prov_or_data_id for ...args/return}
sub_arena = home_arena.copy_to_new_arena(home_lease.clone(), fxtr_ids_for_arena)
adapter = ArenaIRHandleAdapter(sub_arena, ...)
initial_precomp = _compute_precomp_closure(sub_arena, sub_lease.clone(), fxtr_ids_for_arena)
fxtr_arena_value = IRTagged("fxtr_arena", {
    "handle": adapter,
    "tracked_ids": [_encode_fxtr_id(i) for i in fxtr_ids_for_arena],
    "initial_precomp": initial_precomp,
})
```

Receiver (`_run_user_method` and `_install_returned_resources`) does:

```
arena_packet = packet["fxtr_arena"]
# Parse: handle, tracked_ids, initial_precomp.
remote_state = CapIRArenaRemoteState(...,
                                     remote=handle,
                                     refcounts={i: 1 for i in tracked_ids})
remote_state.ingest_precomp(initial_precomp)
receiver_arena = CapIRArena._from_remote_state(remote_state, ...)
# Now decode args against receiver_arena:
decode(arg_iv, arena=receiver_arena, lease=receiver_arena_lease.clone())
```

(The internal-only `_from_remote_state` builder is the entry the
service path uses; the public `direct`/`derived`/`with_converter`
factories construct a fresh remote state, but the service path needs
to construct one from a pre-populated state to ingest the initial
precomp.)

### 3. `service_cap_common.py` — collect provenance parents into the arena

**File: `fxtr2/services/service_cap_common.py`**

Current behavior in `_encode_fxtr_service_data`: for each `FxtrID` /
`EntityRef` leaf, add the data ID to `fxtr_ids` (which then gets
forked into the input arena).

New behavior: for each `FxtrID` / `EntityRef` leaf, add the provenance
parent (when present) **instead of** the data ID, since:

- The provenance parent is the ID the receiver needs in its arena to
  decode the wire form (`FXTR_ID_WITH_PROV_TAG` requires a sync
  unwrap on the parent).
- The data ID is automatically reachable via `unwraps_to` once the
  parent is in the receiver's arena.
- For bare IDs (`_provenance_parent is None`), the data ID is what
  goes in the arena, as today.

Concretely:

```
if isinstance(value, FxtrID):
    fxtr_ids.add(value._provenance_parent or value)
    return _encode_fxtr_id(value)  # the new variant from §1
```

`ServiceRef.service_type_id` follows the same rule.

The receiver's `_decode_fxtr_service_data` calls the new
`_decode_fxtr_id` from §1, which needs an arena + lease in scope.
After arena bridging (§2's `receiver_arena`), the call-scope arena is
already populated with both the prov parents and (via the precomp)
the unwrap-to mappings, so each prov-bearing leaf can be decoded
synchronously.

### 4. Service-call scope inherits c-node from the **mount** scope (default)

**File: `fxtr2/services/service_cap_common.py`** —
`CommonServiceImplHandle._dispatch_body`

The dispatch body opens a per-call mountable+derived child scope. By
default it inherits its provenance context from the **mount scope**
(the scope that the service was mounted into), not the caller scope.
This works automatically once `_with_derived_lifetime` accepts
`provenance_context=None` and falls through to "inherit from
parent's marker" — the mount scope is one of the parents of the call
scope, so its c-node propagates naturally.

The rationale: the mount scope is where the service is actually being
served from, and it's already a child of that scope; tracking
provenance of the mount tells you "this is happening inside the X
service" which is more useful than blindly stamping the caller's
trace. Caller-trace propagation can be added later as an opt-in
extension (e.g. a call-site flag that ships the caller's c-node and
overrides the call scope's `provenance_context`).

No code changes are needed for this default behavior — it falls out
of Stage 1's per-scope context entity if the mount scope was
constructed with a `provenance_context`. A test should assert it:
mount a service in a scope with a c-node, call it from a scope with
a different c-node, verify entities stored inside the impl pick up
the **mount scope's** trace.

### 5. Tests

In `tests/test_services/test_untyped_service.py`:

- Extend a few of the existing `service_layout`-parameterized tests
  to also run with provenanced scopes:
  - Argument round-trip: pass a `FxtrID` with provenance parent, the
    receiver's handler observes the same `(data, prov)` pair, returns
    it, the proxy gets back the same pair.
  - `EntityRef` round-trip with provenance.
  - `ServiceRef.service_type_id` carries provenance correctly.

- New test: passing an arg with provenance into a service whose
  receiver has no provenance context (bare scope on the receiver
  side). The receiver should still be able to decode the arg —
  provenance flows through regardless of the receiver's own scope
  context.

In `tests/test_rpc/`:

- Extend the transport-parameterized tests so a provenanced FxtrID
  survives a real RPC round-trip (not just a single-process Cap-IR
  loop).

Possibly a new test file for the precomp-handoff logic, since it's a
distinct mechanism that's worth exercising independently of the
provenance machinery.

## Implementation steps

CapIRArena local-fork refactor:

- [x] Drop `IRFuture[IRHandle, ...]` from `CapIRArena.remote` types.
- [x] Introduce `CapIRArenaRemoteState` with refcounts, precomp,
      bridge state, lease management, and the wire dispatch helpers.
      Lifetime/brand state (`z_token`, `derived`, `x_token`,
      `bridge_state`) lives on the remote state too — siblings always
      share them.
- [x] Refactor `CapIRArena` fields/methods to delegate to remote state.
      `CapIRArena` reduces to `_remote_state + _liveset + _closed`.
- [x] Make `copy_to_new_arena` local (no wire dispatch).
- [x] Extend wire `abandon` to accept a list of IDs; update sender
      path (`CapIRArenaRemoteState.release`) and adapter
      (`_handle_abandon`).
- [x] Drop `copy_to_new_arena` wire method on `ArenaIRHandleAdapter`.
- [x] Update existing CapIRArena tests for new shape (replaced
      `TestAcceptanceCopyToNewArenaPipelined` with
      `TestAcceptanceLocalForks`; dropped `TestAdapterFork`).

Stage 3 (provenance):

- [x] Add `FXTR_ID_WITH_PROV_TAG` and the prov-aware encoder
      (`_encode_fxtr_id_with_prov`) / decoder (`_decode_fxtr_id_with_prov`)
      in `cap_ir_arena.py`. The bare-only helpers stay strict
      (asserts on no `_provenance_parent`) for arena-internal paths.
- [x] Decoder takes an `embed_prov: Callable[[FxtrID], FxtrID]`
      argument — typically `FxtrScope.embed_as_provenance_parent`
      from the receiving scope — rather than threading raw arena +
      lease. The scope handles all the invariants (prov is tracked,
      tag is provenance, sync unwrap).
- [x] Update the `fxtr_arena` packet shape to ship `{handle,
      tracked_ids, initial_precomp}` instead of a bare `IRHandle`.
- [x] Add an `initial_precomp` parameter to the
      `direct` / `derived` / `with_converter` factories; the remote
      state's precomp caches are pre-populated at construction. No
      separate public ingest method.
- [x] Update `_encode_fxtr_service_data` to add the provenance
      parent (when present) instead of the data ID to `fxtr_ids`.
- [x] Update `_decode_fxtr_service_data` to take a `scope` argument
      and use `scope.embed_as_provenance_parent` for prov-bearing
      leaves.
- [x] Restructure the receiver path (`_run_user_method`): bridge
      `fxtr_arena` into the call scope **before** decoding args, so
      `embed_as_provenance_parent` works during decode.
- [x] Restructure the proxy return path (`invoke_service_method`):
      split `_install_returned_resources` into pre-decode
      (`_install_return_arena`) and post-decode
      (`_install_return_handles`). Bridge the return arena into
      `home_scope` before decoding the result.
- [x] Sender-side precomp computation when forking the input arena
      (`fork_arena_handle_for_ids` returns `(handle, lease, precomp)`)
      and the return arena (`_build_return_resources` computes
      precomp over `new_ids`).
- [x] Tests for FxtrID round-trip and EntityRef round-trip with
      provenance through a service call.
- [x] Test asserting service-call scope inherits c-node from the
      mount scope (not the caller scope).
- [x] Test asserting `ServiceRef.service_type_id` round-trips with
      provenance preserved.
- [x] Test asserting `CapIRArena.close()` defers wire abandons
      (queues into `_pending_abandons`) and the next sibling's
      `release()` flushes them.
- [x] Update CODE_OVERVIEW.md and `services/LOCAL_CODE_OVERVIEW.md`.

## Things to watch / open questions

- **Wire compatibility.** We're changing both the `fxtr_arena` packet
  shape (field-of-handle becomes mapping) and the `abandon` arg shape
  (single-ID becomes list). These are silent breaks against any
  pre-stage-3 peer. We control both sides of the wire today and have
  no shipped versions to maintain compat with, so a clean break is
  fine.
- **Service-call scope's marker**: §4 makes the call scope inherit
  its c-node from the **mount** scope (which is one of its parents).
  This is a deliberate choice — provenance reflects "where this code
  is running", not "who called it." Caller-trace propagation is a
  potential future extension (opt-in only) and is explicitly out of
  scope here.
- **Arena type-id flow**: `ServiceRef.service_type_id` is itself a
  `FxtrID`, and may have a provenance parent under the new system.
  The collection in `_encode_fxtr_service_data` already adds it to
  `fxtr_ids`; we just need to make sure the receiver-side
  reconstruction uses the new decoder so the provenance parent
  survives.
- **Initial-precomp size**: the precomp closure for the whole forked
  liveset may be large for big arenas. If this becomes a problem we
  can switch to a "just-in-time" model where precomp is fetched on
  demand for IDs that aren't in the cache yet — but that defeats the
  sync invariant. For now, ship the closure unconditionally and
  revisit if it bites.
- **Hub server / namespaces**: the hub-client stack passes IDs over
  HTTP and explicitly drops provenance (matching the namespace rule).
  No change required, but worth a regression test asserting the drop
  remains explicit on the namespace boundary.
