# Substage 5 — Arena-over-CapIR

Detailed implementation plan for substage 5 of
[stage 8c](overview.md#substage-5--arena-over-capir).

**Goal.** Let an entity arena cross the Cap-IR boundary so fxtr data flows
through `IRHandle` dispatches the same way live capabilities will (substage
6). Two new classes plus one small new lease-layer type:

- **`ArenaIRHandleAdapter[B]`** wraps a `ReadableArena[B]` and exposes it as
  an `IRHandle[B]`. Its token aliases the wrapped arena's token; its
  request methods are modelled on the HTTP / SQLite client protocols
  (`load_flat`, `abandon`, `copy_to_new_arena`). All
  responses are plain `IRValue` shapes — no on-the-wire schema, only
  byte-level conventions for `FxtrID` and `FxtrEntityFlatData`.

- **`CapIRArena[B_Z, B_X, B_Y, B_K]`** is the symmetric client: a
  `ReadableArena[B_Z]` whose remote is an `IRHandle[B_Y]` (or an
  `IRFuture[IRHandle[B_Y], …]` for the `copy_to_new_arena`-fork case). Its
  Z-lease is converted into a (possibly pipelined) Y-lease via
  `DerivedLocalLifetime.exchange_reverse` plus an optional remote
  converter handle, then the substage-4 pipelined-dispatch-lease form is
  used so converter + remote-arena dispatches both go out without a
  synchronous round-trip in between. Provenance reads are served from a
  local precomp cache (the HTTP-style precomp map lives inside every
  `load_flat` reply); `is_tracked` is served from a local liveset.

- **`PipelinedLifetimeConverter[B_X, B_Y, B_K]`** type alias in the lease
  layer (`fxtr2/leases/leased_cap_common.py`). Sync signature, pipelined-
  capable return — a CapIR-backed converter (dispatch, wrap the
  `(future, lease)` pair) fits the type, and any sync `LifetimeConverter`
  fits trivially by never returning the pipelined branch. Substage 6
  consumes it on scope cap-bridges.

This plan is structured: mental model → wire protocol → server →
client → tests → CODE_OVERVIEW updates.

---

## Mental model

A `CapIRArena` is the client view of a `ReadableArena` whose data lives on
the other side of a Cap-IR dispatch path. The four token roles:

- **`B_Z`** — the arena's *own* token (what `is_tracked`, `load_flat`,
  etc. take leases on). Either equal to `B_X` (no derivation) or a
  pre-existing derived token whose inputs include `B_X` (and `B_K` if
  applicable). The caller owns the lifetime; the arena does **not**.
- **`B_X`** — the input token the caller already holds leases on.
- **`B_Y`** — the *remote* arena's token (what the wire dispatch needs a
  lease on).
- **`B_K`** — the optional key token gating the remote converter.

Three valid configurations (from
[overview.md](overview.md#substage-5--arena-over-capir)):

| Mode | Z | X | Y | K | Converter | DerivedLocalLifetime |
|---|---|---|---|---|---|---|
| 1 | == X == Y | — | == Z | — | None | None |
| 2 | derived from {X} | — | == X | — | None | yes |
| 3 | derived from {X, K} | — | distinct | — | yes | yes |

Per-call dispatch flow, given a caller-supplied `Lease[B_Z]`:

1. **Mode 1.** Use `Lease[B_Z]` directly as the `Lease[B_Y]`.
2. **Mode 2.** `derived.exchange_reverse(z_lease)` →
   `{X_token: Lease[B_X]}`. `Lease[B_Y] = Lease[B_X]` (since Y == X).
3. **Mode 3.** `derived.exchange_reverse(z_lease)` →
   `{X_token: Lease[B_X], K_token: Lease[B_K]}`. Synchronously
   `(fut_y_lease, fut_lease) = converter_handle.dispatch(k_lease,
   Call("convert", x_lease))`. Use
   `UnwrapForPipelining(fut_y_lease, fut_lease)` as the dispatch lease
   on the remote arena handle (substage 4).

Then the request itself goes out on the remote handle. In mode 3 both
pushes are issued before either resolves; the receiver pipelines the
arena dispatch on the converter's resolved lease.

Three additional design points worth noting up-front:

- **`CapIRArena` owns no leases.** All leases come from the per-call
  `z_lease`. This means `CapIRArena.close()` has no key-lease cleanup
  to do — it only flushes local caches and (in the
  `IRFuture`-remote case) discards a held future_lease.
- **`copy_to_new_arena` returns a `CapIRArena` whose `_remote` is an
  `IRFuture[IRHandle[B_Y], …]` plus a held future_lease.** Subsequent
  dispatches on the fork use `IRFuture.pipeline_dispatch` instead of
  `IRHandle.dispatch`. Forks-of-forks compose: a `pipeline_dispatch`
  on the future of a fork yields a future of a sub-fork, etc.
- **Provenance reads never round-trip.** The wire-level `load_flat`
  reply carries a closed precomp map for every dep; the client
  ingests it into three local dicts and serves `unwrap_provenance` /
  `c_node_to_context_data` / `p_node_to_pathroot` /
  `step_back_p_ctx` synchronously. This mirrors `HttpReadableArena`'s
  precomp-cache pattern.

---

## New module: `fxtr2/data/cap_ir_arena.py`

Houses both `ArenaIRHandleAdapter` and `CapIRArena`, plus the small
private wire-encoding helpers (`_encode_fxtr_id`, `_decode_fxtr_id`,
`_decode_flat_data`, `_compute_precomp_closure`,
`_ingest_precomp`). The module is self-contained — no other file
imports from it except the substage-5 tests; substage 6 will start
consuming it from a yet-unknown module path.

`PipelinedLifetimeConverter` lives in
`fxtr2/leases/leased_cap_common.py` (next to the rest of the
leased-cap utility surface, with a docstring cross-reference back to
`LifetimeConverter` in `fxtr2/data/abstract_arena.py`).

---

## Wire protocol

### Method names and request shapes

The `ArenaIRHandleAdapter`'s `_run_request_async` dispatches on
`request.method` (a string), accepting:

| Method | Arg | Return |
|---|---|---|
| `"load_flat"` | tagged FxtrID | `_FlatDataReply` (see below) |
| `"abandon"` | tagged FxtrID | `None` |
| `"copy_to_new_arena"` | `Sequence[tagged FxtrID]` | new `IRHandle[B_Y]` |

Three methods, not four. The abstract `ReadableArena` exposes
`unwrap_provenance` / `c_node_to_context_data` / `p_node_to_pathroot`
/ `step_back_p_ctx` as sync calls, but the closed precomp returned by
`load_flat` covers every provenance read the client could
legitimately make: the abstract API requires those methods' inputs to
be IDs the arena already knows about, which means the client must
have `load_flat`-ed (and thus precomp-cached) the relevant
provenance chain. There's no scenario where the client legitimately
needs to round-trip for provenance, so we don't add wire methods
for it.

Anything else raises `IRException(category="UnknownArenaMethod")`. We
do not use `Call.method` as a sequence path — single string only.

### `FxtrID` encoding

```python
FXTR_ID_TAG = "fxtr_id"

def _encode_fxtr_id(id: FxtrID) -> IRTagged[str, bytes]:
    # Send only the data-level identity. ``_provenance_parent`` is a
    # per-process marker that is never persisted and is not part of
    # equality / hashing — strip it on the wire.
    return IRTagged(FXTR_ID_TAG, bytes(id))  # bytes(id) returns id._raw_data_id

def _decode_fxtr_id(value: IRValue) -> FxtrID:
    if not isinstance(value, IRTagged) or value.tag != FXTR_ID_TAG:
        raise IRException(message=..., category="MalformedArenaPacket")
    if not isinstance(value.value, bytes):
        raise IRException(message=..., category="MalformedArenaPacket")
    return FxtrID(raw_data_id=value.value)
```

Two clarifications on the provenance question (this is subtle —
see `README.md`'s "ID system already supports this" paragraph and
`fxtr2/data/entity_core.py:FxtrID`):

- **Provenance *parents* (`_provenance_parent`)** are a per-process
  Python-level marker that the FxtrID drags along to enable
  automatic provenance threading. They are **not** part of the
  data-level identity (equality and hashing ignore them) and are
  never persisted to disk. We strip them on the wire — every
  decoded ID arrives without a provenance parent.
- **Provenance *nodes*** (the `c` / `p` / `u` / `n` tags inside
  `_raw_data_id`) are first-class data-level identities that
  participate in the graph (they're what `unwrap_provenance` /
  `c_node_to_context_data` / `p_node_to_pathroot` /
  `step_back_p_ctx` operate on). The first byte of `_raw_data_id`
  is the node tag, so a `c`-node ID's bytes start with `b"c"`.
  These survive the wire encoding above unchanged: an ID whose
  `_raw_data_id` represents a `c`-node round-trips through
  `bytes(id)` / `FxtrID(raw_data_id=...)` exactly.

So encode/decode is a single tagged-bytes pair. No string round
trip, no `@`-separated provenance, no special handling for
provenance-node IDs.

### `FxtrEntityFlatData` encoding

`load_flat`'s reply is a plain (untagged) dict — the receiver knows
what to expect from the method name, so we save the tag overhead:

```python
{
    "entity_id": <tagged FxtrID>,
    "entity_bytes": bytes | None,
    "deps": [{"id": <tagged FxtrID>, "kind": <DepKind.value str>}, ...],
    "precomp": [
        {
            "id":               <tagged FxtrID>,
            "unwraps_to":       <tagged FxtrID>,   # optional, only present if applicable
            "ctx_node":         <tagged FxtrID>,   # optional
            "step_back_p_ctx":  <tagged FxtrID>,   # optional
        },
        ...
    ],
}
```

`precomp` is a flat list of entries, each carrying its own `id`
field plus whichever of `unwraps_to` / `ctx_node` /
`step_back_p_ctx` are applicable for that node tag. Optional fields
are omitted entirely when the relationship doesn't apply (e.g. a
`d`-node entry only has `id`; a `c`-node entry has `id` +
`ctx_node` + (if known) `unwraps_to`). The server builds the
*closed* list at `load_flat` time using the same BFS as
`experimental/http/asgi_app.py:_compute_precomp_closure`: seed
with `[node_id] + [d for d, _ in flat.deps]`, walk to all
reachable provenance targets. Entries with no relationships at
all (purely-leaf IDs the BFS visits) are omitted, matching the
HTTP server's behaviour.

Client-side ingestion (`_ingest_precomp`) iterates the list once,
populating three local dicts keyed by `FxtrID`:

- `_precomp_unwraps_to: dict[FxtrID, FxtrID]` — from `unwraps_to`.
- `_precomp_ctx_node: dict[FxtrID, FxtrID]` — from `ctx_node`.
- `_precomp_step_back_p_ctx: dict[FxtrID, FxtrID]` — from
  `step_back_p_ctx`.

`FxtrID` is already hashable (equality on `_raw_data_id`), so no
string detour is needed. This matches the substage-6 calling
convention's `IRTagged("fxtr_id", bytes)` encoding exactly: a
single FxtrID encode/decode pair handles every site that carries
an ID across the wire.

`copy_to_new_arena`'s reply is the new `IRHandle[B_Y]` directly (no
wrapper). `abandon` returns `None`.

### `DepKind` encoding

We send `DepKind.value` (the string the enum already uses:
`"strong_ref"`, `"weak_ref"`, `"ctx_node"`, `"backstep"`,
`"unwraps_to"`). On decode we round-trip via `DepKind(value_str)`.

---

## Server side: `ArenaIRHandleAdapter[B]`

```python
class ArenaIRHandleAdapter[B: LocalLifetimeBrand](IRHandleLocalImplBase[B]):
    """IRHandle wrapping a ReadableArena[B], exposing it over Cap-IR."""

    def __init__(
        self,
        arena: ReadableArena[B],
        *,
        task_group: anyio.abc.TaskGroup,
    ) -> None:
        self._arena = arena
        self._token = arena.token  # per overview: token aliases the wrapped arena's
        self._task_group = task_group

    async def _run_request_async(self, lease, request):
        if isinstance(request, GetItem):
            raise IRException(message=..., category="GetItemUnsupported")
        # request: Call[UnwrappedCallArg]
        method = request.method
        if not isinstance(method, str):
            raise IRException(...)
        with lease.take() as lease:
            match method:
                case "load_flat":     return await self._handle_load_flat(lease, request.arg)
                case "abandon":       return self._handle_abandon(lease, request.arg)
                case "copy_to_new_arena":
                    return self._handle_fork(lease, request.arg)
                case _:               raise IRException(category="UnknownArenaMethod")
```

Key decisions:

- **Task group is caller-provided.** Same pattern as any
  `IRHandleLocalImplBase` subclass — typically the caller co-manages
  it with the wrapped arena's lifetime via
  `open_lifetime_task_group`. The plan does not provide a helper
  function; tests construct adapters directly.
- **Lease ownership.** `_run_request_async` takes the lease on entry
  (`with lease.take() as lease:`). Each per-method handler clones the
  lease and forwards to the underlying arena method (which itself
  takes the clone), so the outer `with` block discards on exit. For
  `copy_to_new_arena` the arena's `lease`-passing API is sync, so the
  pattern is identical.
- **Errors propagate as `IRException`.** `_run_request_async`'s
  outer wrapper (the one in `IRHandleLocalImplBase`'s runner) already
  wraps non-`IRException`-`Exception` raises in
  `IRException(category=type(exc).__name__)`. Domain errors that we
  want to label distinctly (`UnknownArenaMethod`,
  `MalformedArenaPacket`, `GetItemUnsupported`) raise `IRException`
  directly from the handler.

### Per-method handlers

#### `load_flat`

```python
async def _handle_load_flat(self, lease, arg) -> dict[str, IRValue]:
    node_id = _decode_fxtr_id(arg)
    flat = await self._arena.load_flat(lease.clone(), node_id)
    precomp_entries = self._compute_precomp_closure(
        seed_ids=[node_id, *[d for d, _ in flat.deps]],
        lease_for_arena=lease.clone(),
    )
    return {
        "entity_id":    _encode_fxtr_id(flat.entity_id),
        "entity_bytes": flat.entity_bytes,
        "deps":         [{"id": _encode_fxtr_id(d_id), "kind": d_kind.value}
                         for d_id, d_kind in flat.deps],
        "precomp":      precomp_entries,
    }
```

`_compute_precomp_closure(seed_ids, lease_for_arena)` ports the HTTP
server logic from `experimental/http/asgi_app.py:_compute_precomp_closure`
into a private helper on `ArenaIRHandleAdapter`. Same BFS shape (walk
seeds + recursively reachable provenance targets, calling
`unwrap_provenance` / `c_node_to_context_data` / `p_node_to_pathroot` /
`step_back_p_ctx` synchronously on the wrapped arena with cloned
leases), but it builds and returns a `list[dict[str, IRValue]]` (one
entry per visited ID with at least one relationship) instead of an
id-string-keyed dict:

```python
entry = {"id": _encode_fxtr_id(node_id)}
if tag in ("c", "p", "u", "n"):
    if (uw := arena.unwrap_provenance(lease.clone(), node_id)) is not None:
        entry["unwraps_to"] = _encode_fxtr_id(uw)
        frontier.append(uw)
if tag == "c":
    ctx = arena.c_node_to_context_data(lease.clone(), node_id)
    entry["ctx_node"] = _encode_fxtr_id(ctx); frontier.append(ctx)
elif tag == "p":
    ctx = arena.p_node_to_pathroot(lease.clone(), node_id)
    entry["ctx_node"] = _encode_fxtr_id(ctx); frontier.append(ctx)
    if (sb := arena.step_back_p_ctx(lease.clone(), node_id)) is not None:
        entry["step_back_p_ctx"] = _encode_fxtr_id(sb); frontier.append(sb)
if len(entry) > 1:  # has at least one relationship beyond "id"
    entries.append(entry)
```

The output list is *closed*: every `<tagged FxtrID>` referenced as a
relationship target is itself the `id` of some entry in the list (or
is a leaf `d` / `!`-node that never has precomp data). The client
therefore never needs to round-trip for provenance — reads either
hit the local dicts populated from this list, or are leaf-node
queries that legitimately return `None`.

Note `_compute_precomp_closure` consumes `lease_for_arena` over the
course of the walk by cloning it before each arena call; the helper
discards its received lease at the end. The outer
`_handle_load_flat` clones the input lease once per use (one for
the `load_flat` call, one for the precomp helper).

#### `abandon`

```python
def _handle_abandon(self, lease, arg) -> None:
    node_id = _decode_fxtr_id(arg)
    self._arena.abandon(lease.clone(), node_id)
    return None
```

`abandon` is sync on the arena. The client side fires-and-forgets via
`dispatch` (no await) — see "client `abandon`" below.

#### `copy_to_new_arena`

```python
def _handle_fork(self, lease, arg) -> IRHandle[B]:
    node_ids = [_decode_fxtr_id(v) for v in arg]
    forked = self._arena.copy_to_new_arena(lease.clone(), node_ids)
    return ArenaIRHandleAdapter(forked, task_group=self._task_group)
```

The new adapter wraps a forked `MemoryArena` view that has its own
liveset row on the underlying `MemoryStore`. Nobody else holds a
reference to that view: when the receiver eventually drops the
future_lease and the IR future discards its stored value, the new
adapter is GC'd, and with it the wrapped view.

### Fork cleanup

We deliberately do **not** add our own `weakref.finalize` on the
fork adapter. `MemoryStore.new_arena` already registers
`weakref.finalize(arena, self._finalize_arena_object, key)`
(`fxtr2/data/memory_arena.py:1927/1947`); when the wrapped view is
GC'd, that existing finalize fires and calls `close_arena(key)`,
releasing the liveset. So the only consequence of relying on GC is
the INFO log emitted by `_finalize_arena_object` when a non-empty
arena is finalized while open (`memory_arena.py:1955`):

> `Arena {key} was finalized while open! ({live_nodes} nodes live)`

That log line will be emitted once per fork lifetime in normal
operation. The substage-5 acceptance test counts this as expected
behaviour rather than failing on it. There's no resource leak —
the liveset row is reclaimed regardless.

A deterministic close-via-the-wire would be the right long-term
fix, but `ReadableArena.close` doesn't currently take a lease, so
it can't be invoked across an RPC boundary in a lease-gated way.
Tracked as a follow-up in `overview.md`'s "Open follow-ups"
section: re-shape `close()` to take a lease, then propagate close
signals across `CapIRArena` to the server-side adapter so the
underlying view closes promptly at scope drain.

---

## Client side: `CapIRArena[B_Z]`

### Type parameters

The class is generic only on `B_Z`, the user-visible brand exposed by
every public method. Internal X / Y / K tokens are stored as
`LifetimeToken[Any]` and validated at construction time by the
factory classmethods (each of which has its own type parameters that
precisely capture the brand relationships of its mode). This keeps
the class signature simple while still catching mismatches like
"converter brand doesn't match k_token" at the call site.

### Internal state

```python
class CapIRArena[B_Z: LifetimeBrand](ReadableArena[B_Z]):
    # User-visible brand.
    _z_token: LifetimeToken[B_Z]

    # Internal token / handle / lifetime state. Erased to `Any` —
    # invariants documented as field comments and validated in
    # `__init__` (and in the classmethod factories' type parameters).
    _derived: DerivedLocalLifetime[Any] | None
    _x_token: LifetimeToken[Any]
    _k_token: LifetimeToken[Any] | None
    _remote: IRHandle[Any] | IRFuture[IRHandle[Any], Any]
    _remote_lease: Lease[Any] | None  # held only when `_remote` is an IRFuture
    _converter: IRHandle[Any] | None

    # Local caches, populated lazily by load_flat replies.
    _liveset: set[FxtrID]
    _precomp_unwraps_to: dict[FxtrID, FxtrID]
    _precomp_ctx_node: dict[FxtrID, FxtrID]
    _precomp_step_back_p_ctx: dict[FxtrID, FxtrID]

    _closed: bool
```

### Constructors

Three factory classmethods, one per mode. Each captures the mode's
brand relationships in its own type parameters:

```python
@classmethod
def direct[B: LifetimeBrand](
    cls,
    *,
    token: LifetimeToken[B],
    remote: IRHandle[B] | IRFuture[IRHandle[B], Any],
    initial_tracked: Collection[FxtrID],
    remote_lease: Lease[Any] | None = None,
) -> CapIRArena[B]:
    """Mode 1: Z = X = Y = B. The shared `B` catches mismatched
    remote brands at the type-check level."""

@classmethod
def derived[
    OutBrand: LocalLifetimeBrand,
    XBrand: LifetimeBrand,
](
    cls,
    *,
    derived: DerivedLocalLifetime[OutBrand],
    x_token: LifetimeToken[XBrand],
    remote: IRHandle[XBrand] | IRFuture[IRHandle[XBrand], Any],
    initial_tracked: Collection[FxtrID],
    remote_lease: Lease[Any] | None = None,
) -> CapIRArena[OutBrand]:
    """Mode 2: Y = X via the shared `XBrand`. Runtime check
    verifies `x_token in derived.inputs`."""

@classmethod
def with_converter[
    OutBrand: LocalLifetimeBrand,
    XBrand: LifetimeBrand,
    YBrand: LifetimeBrand,
    KBrand: LifetimeBrand,
](
    cls,
    *,
    derived: DerivedLocalLifetime[OutBrand],
    x_token: LifetimeToken[XBrand],
    k_token: LifetimeToken[KBrand],
    remote: IRHandle[YBrand] | IRFuture[IRHandle[YBrand], Any],
    converter: IRHandle[KBrand],
    initial_tracked: Collection[FxtrID],
    remote_lease: Lease[Any] | None = None,
) -> CapIRArena[OutBrand]:
    """Mode 3: `converter: IRHandle[KBrand]` and
    `k_token: LifetimeToken[KBrand]` share `KBrand` so a mismatched
    converter is caught at type-check time. Runtime checks verify
    `x_token` and `k_token` are both in `derived.inputs`."""
```

**`initial_tracked` is required, not optional.** A `CapIRArena`'s
local liveset must be initialised to exactly what the remote
`ArenaIRHandleAdapter` is currently tracking. The caller knows this
— either:

- the sender of the handle picked the set of IDs to expose (so they
  pass that set to the receiver alongside the handle), or
- it's the result of `copy_to_new_arena` (which seeds with the
  requested `node_ids` — already wired in the plan).

The 1:1 invariant — **one `CapIRArena` per `ArenaIRHandleAdapter`,
no other party mutates the remote arena's liveset** — keeps the
two livesets in sync after construction: `load_flat` adds dep IDs
on both sides, `abandon` removes on both sides, `copy_to_new_arena`
forks under fresh adapters. So local prechecks are authoritative;
no client/server divergence is possible.

The constructors validate that `derived.token is z_token`,
`remote_lease` is set iff `remote` is an `IRFuture`, etc., raising
`ValueError` on misconfiguration.

### Lease-conversion helper: `_make_y_lease(z_lease)`

```python
def _make_y_lease(
    self,
    z_lease: Lease[B_Z],
) -> Lease[B_Y] | UnwrapForPipelining[Lease[B_Y], Any]:
    """Take ownership of `z_lease`; return a Y-lease (possibly pipelined)."""
    if self._derived is None:
        # Mode 1: B_Z == B_X == B_Y, just return z_lease.
        return cast(Lease[B_Y], z_lease.take())
    # Modes 2 / 3: split via reverse-exchange.
    inputs = self._derived.exchange_reverse(z_lease.take())
    x_lease = inputs.pop(self._x_token)
    if self._converter is None:
        # Mode 2: discard any spurious extras (defensive — shouldn't happen),
        # return x_lease as Lease[B_Y].
        for stray in inputs.values():
            stray.discard()
        return cast(Lease[B_Y], x_lease)
    # Mode 3.
    assert self._k_token is not None
    k_lease = inputs.pop(self._k_token)
    for stray in inputs.values():
        stray.discard()  # any extra inputs to the derived lifetime
    fut_y, fut_lease = self._converter.dispatch(
        k_lease, Call("convert", x_lease)
    )
    return UnwrapForPipelining(fut_y, fut_lease)
```

Ownership: `z_lease` is taken on entry. Mode-1 returns it directly.
Modes 2/3 consume it through `exchange_reverse`, which clones each via
into the returned dict — those clones are owned by the
helper's caller (the per-method handler). In mode 3 the converter's
dispatch takes `k_lease` and the embedded `x_lease`, and the
caller's eventual `request`/`dispatch` of the remote arena takes the
returned `UnwrapForPipelining` (substage 4).

### Remote-dispatch helper: `_dispatch_remote(y_lease_or_pipelined, request)`

```python
def _dispatch_remote[RB](
    self,
    lease,           # Lease[B_Y] | UnwrapForPipelining[Lease[B_Y], Any]
    request: Request,
) -> tuple[IRFuture[Any, RB], Lease[RB]]:
    if isinstance(self._remote, IRFuture):
        # Forks: pipeline_dispatch on the future of the handle, with the
        # future_lease as the outer authority.
        return self._remote.pipeline_dispatch(
            self._remote_lease.clone(), lease, request,
        )
    return self._remote.dispatch(lease, request)
```

Each call clones the held `_remote_lease` (when present) — `pipeline_dispatch`
takes ownership of it (along with `lease`).

### `_request_remote(z_lease, request)`

```python
async def _request_remote(self, z_lease, request) -> IRValue:
    y_lease = self._make_y_lease(z_lease)
    fut, fut_lease = self._dispatch_remote(y_lease, request)
    try:
        return await fut.wait(fut_lease.clone())
    finally:
        fut_lease.discard()
```

Mirrors `IRHandle.request` semantics (dispatch + await + discard).
Used by every remote arena method. The helper does **not** validate the
z_lease's subject — that's done by the per-method handler (just like
`MemoryArena._precheck_lease_for_arena` does).

### Per-method behaviour

#### `is_tracked(lease, node_id)` — sync, no round-trip

```python
def is_tracked(self, lease, node_id):
    self._precheck(lease)
    with lease.take() as lease:  # discard on exit
        if self._closed:
            return False
        return node_id in self._liveset
```

`_precheck(lease)` does the same subject-identity + liveness check as
`MemoryArena._assert_lease_matches_arena`.

#### `load_flat(lease, node_id)` — async, full round-trip

```python
async def load_flat(self, lease, node_id):
    self._precheck(lease)
    arg = _encode_fxtr_id(node_id)
    reply = await self._request_remote(lease, Call("load_flat", arg))
    flat = _decode_flat_data(reply)
    self._ingest_precomp(reply["precomp"])  # mirrors HttpReadableArena
    self._liveset.add(node_id)
    for d_id, _ in flat.deps:
        self._liveset.add(d_id)
    return flat
```

Mirrors the contract: returned dep IDs are added to the local
liveset, so subsequent `is_tracked` calls succeed.

#### Provenance reads — sync, served from precomp cache only

`unwrap_provenance` / `c_node_to_context_data` / `p_node_to_pathroot`
/ `step_back_p_ctx` are all sync in `ReadableArena` and cache-only on
`CapIRArena`. They look exactly like the `HttpReadableArena` shape:

```python
def unwrap_provenance(self, lease, node_id):
    self._precheck(lease)
    with lease.take():
        return self._precomp_unwraps_to.get(node_id)  # or None

def c_node_to_context_data(self, lease, c_node_id):
    self._precheck(lease)
    with lease.take():
        result = self._precomp_ctx_node.get(c_node_id)
        if result is None:
            raise ValueError(f"c-node {c_node_id} not in precomp cache")
        return result
# similarly for p_node_to_pathroot, step_back_p_ctx
```

`unwrap_provenance` returns `None` on miss (matching the
"not-a-provenance-node" semantics of the abstract API);
`c_node_to_context_data` and `p_node_to_pathroot` raise on miss
(those are documented as "assumes the ID is already known to the
store"); `step_back_p_ctx` returns `None` on miss (matches its
"not-a-`$fxtr.ctx`-entry" return).

The cache is populated solely by `load_flat`'s closed precomp
reply. By construction every ID a caller could legitimately ask
about must already have been `load_flat`-ed (the abstract API
requires the ID to be known to the arena), so the cache always
has it. We don't add a wire fallback for cache misses.

#### `abandon(lease, node_id)` — sync, fire-and-forget dispatch

```python
def abandon(self, lease, node_id):
    self._precheck(lease)
    with lease.take() as lease:
        if node_id not in self._liveset:
            raise ValueError(...)
        self._liveset.discard(node_id)
        self._precomp_unwraps_to.pop(node_id, None)
        self._precomp_ctx_node.pop(node_id, None)
        self._precomp_step_back_p_ctx.pop(node_id, None)
        # Synchronously dispatch the abandon, then drop the result lease.
        # No await — the abandon completes in the background.
        y_lease = self._make_y_lease(lease.clone())  # clones lease so `with` still discards
        _fut, fut_lease = self._dispatch_remote(
            y_lease, Call("abandon", _encode_fxtr_id(node_id)),
        )
        fut_lease.discard()
```

Subtlety: `_make_y_lease` consumes the z_lease; we want the outer
`with`'s discard to still fire on the `lease` slot. Easiest is to
call `.clone()` before the helper (cost: one extra clone per abandon
— acceptable). The dispatch result-lease is discarded immediately;
the in-flight task continues regardless (per substage-4
`IRHandleLocalImplBase` semantics).

#### `copy_to_new_arena(lease, node_ids)` — sync, returns new `CapIRArena`

```python
def copy_to_new_arena(self, lease, node_ids):
    self._precheck(lease)
    with lease.take() as lease:
        for nid in node_ids:
            if nid not in self._liveset:
                raise ValueError(...)
        # Synchronously dispatch the fork.
        arg = [_encode_fxtr_id(nid) for nid in node_ids]
        y_lease = self._make_y_lease(lease.clone())
        fut, fut_lease = self._dispatch_remote(
            y_lease, Call("copy_to_new_arena", arg),
        )
        # Cast the typed Any back to IRHandle[B_Y] in the future contract.
        new_remote = cast(IRFuture[IRHandle[B_Y], Any], fut)
        # Build a sibling CapIRArena that shares this arena's Z/X/Y/K
        # configuration but uses the future-form remote.
        return CapIRArena[B_Z, B_X, B_Y, B_K](
            z_token=self._z_token,
            x_token=self._x_token,
            y_token=self._y_token,
            k_token=self._k_token,
            remote=new_remote,
            remote_lease=fut_lease,           # already a fresh lease, not cloned
            derived=self._derived,
            converter=self._converter,
            initial_tracked=set(node_ids),    # matches the server-side fork's liveset
        )
```

Forks-of-forks compose: the new arena's `_remote` is a future, so
`_dispatch_remote` uses `pipeline_dispatch`; nested forks chain
through `pipeline_dispatch` without ever needing a synchronous
round-trip on the parent.

#### `close()`

```python
def close(self):
    if self._closed:
        return
    self._closed = True
    self._liveset.clear()
    self._precomp_unwraps_to.clear()
    self._precomp_ctx_node.clear()
    self._precomp_step_back_p_ctx.clear()
    if self._remote_lease is not None:
        self._remote_lease.discard()
        self._remote_lease = None
```

The arena owns no `B_Z` lease, so there's nothing to drain there.
The `_remote_lease` (only present for fork-form arenas) is the one
piece of cleanup needed.

### Constructor entry-point variant for `lazily_attach_from_arena`

`lazily_attach_from_arena`'s third overload (sync `LifetimeConverter`)
is unchanged — `CapIRArena` plugs in there as `source` exactly like a
local `MemoryArena` would. The caller supplies the
`LifetimeConverter` (typically wrapping `derived.exchange_forward`
per the existing case-2 pattern), and the memory store stores it +
the key lease as today. The pipelining inside the
`CapIRArena`'s remote dispatch is invisible to the memory store.

This is the substage-5-noted observation that `CapIRArena` is the
sync→async bridge: callers see a sync `ReadableArena` interface,
internally CapIRArena uses pipelined Cap-IR dispatches.

---

## `PipelinedLifetimeConverter` type alias

Lives in `fxtr2/leases/leased_cap_common.py`:

```python
type PipelinedLifetimeConverter[
    B_X: LifetimeBrand, B_Y: LifetimeBrand, B_K: LifetimeBrand
] = Callable[
    [Lease[B_K], Lease[B_X]],
    Lease[B_Y] | UnwrapForPipelining[Lease[B_Y], Any],
]
```

Docstring: cross-references `LifetimeConverter` in
`fxtr2/data/abstract_arena.py` and notes that any sync converter
trivially satisfies the pipelined one (by never returning the
pipelined branch). Substage 6's scope cap-bridges consume this type;
substage 5 only *introduces* it (no consumer in this substage —
`CapIRArena`'s internal converter dispatch is open-coded rather than
going through `PipelinedLifetimeConverter`, because the converter
*itself* is an `IRHandle`, not a Python callable). We add a small
helper:

```python
def pipelined_lifetime_converter_from_handle[
    B_X, B_Y, B_K
](
    handle: IRHandle[B_K],
) -> PipelinedLifetimeConverter[B_X, B_Y, B_K]:
    """Wrap an `IRHandle[B_K]` whose `Call("convert", Lease[B_X])` returns
    `Lease[B_Y]` into a `PipelinedLifetimeConverter`.
    """
    def converter(key: Lease[B_K], base: Lease[B_X]):
        fut, fut_lease = handle.dispatch(key, Call("convert", base))
        return UnwrapForPipelining(fut, fut_lease)
    return converter
```

This isn't needed by `CapIRArena` (which uses `_make_y_lease`'s
inline equivalent), but it's the exact wrapper substage 6 will need
when it bridges a returned converter handle into a scope cap-bridge.
Including it here keeps the lease-layer surface coherent.

---

## Wire encoding helpers

Private to `cap_ir_arena.py`:

```python
def _encode_fxtr_id(id: FxtrID) -> IRValue: ...
def _decode_fxtr_id(value: IRValue) -> FxtrID: ...
def _decode_flat_data(reply: IRValue) -> tuple[FxtrEntityFlatData, list[dict[str, IRValue]]]: ...
def _compute_precomp_closure(
    arena: ReadableArena[Any], lease: Lease[Any], seed_ids: Iterable[FxtrID],
) -> list[dict[str, IRValue]]: ...
def _ingest_precomp(target: CapIRArena, precomp_entries: Sequence[Mapping[str, IRValue]]) -> None: ...
```

`_compute_precomp_closure` ports the BFS from
`experimental/http/asgi_app.py:_compute_precomp_closure`, but
returns a `list` of entries (each with an `"id"` field plus
applicable relationships, all values tagged FxtrIDs) instead of an
id-string-keyed dict. Consumes the supplied lease (via cloning per
arena call; discards the original at the end).

`_ingest_precomp` is the symmetric client-side ingestor: walks the
list once and populates `target._precomp_unwraps_to`,
`target._precomp_ctx_node`, `target._precomp_step_back_p_ctx`,
each keyed by `FxtrID`. No string detour.

`_decode_flat_data` returns `(flat, precomp_entries)` — the caller
(the `load_flat` method body) wants both, and decoupling avoids
re-walking the reply dict. There's no separate `_encode_flat_data`
helper — the encoding is inlined in `_handle_load_flat` since it's
only used in one place.

---

## Acceptance test

Lives in `tests/test_data/test_cap_ir_arena.py` (new file, since the
module is new). The test mirrors substage 3's `task 1 / task 2 /
task 3` acceptance test
(`tests/test_entity_ref_scope/test_derived_scope_acceptance.py`) but
with all cross-task communication routed through `IRHandle` /
`Lease` / `IRTagged` IRValues rather than direct Python references.

Skeleton:

```python
async def test_cap_ir_arena_three_task_handoff():
    # 1. shared MemoryStore + co-managed task group for the IRHandle adapters.
    store = MemoryStore()
    async with open_lifetime_task_group("test-cap-ir-arena-tg") as (_, tg):
        # Use anyio memory streams for cross-task IRValue passing.
        ...

        async with anyio.create_task_group() as outer:
            outer.start_soon(_task1, store, tg, ...)
            outer.start_soon(_task2, store, tg, ...)
            outer.start_soon(_task3, store, tg, ...)
```

Per-task structure (matches the substage 3 narrative):

- **Task 1.** Open a normal scope with token `A`; store some data;
  extract a `MemoryArena` with token `A`; wrap it as
  `ArenaIRHandleAdapter(arena, task_group=tg)`. Send
  `(handle, lease_clone)` to task 2 via the cross-task channel.
  Abandon the original entity reference; data stays alive via the
  arena+lease task 2 holds.
- **Task 2.** Receive `(handle_A, lease_A)`. Mint outer key
  `B = UnconditionalLocalLifetime`, derived `C` (inputs `{A, B}`).
  Construct a `CapIRArena.direct(z=A, x=A, remote=handle_A,
  derived=None)` (mode 1) and bridge it into a scope with token `C`
  via the existing `lazily_attach_from_arena` (case-2 pattern with
  `derived.exchange_forward`). Store three new entities depending on
  task-1 data via the bridge. Extract a fresh `MemoryArena` with
  token `C`; wrap as `ArenaIRHandleAdapter(arena_C, task_group=tg)`.
  Build a *converter* `IRHandle[B]` whose
  `Call("convert", Lease[A])` does
  `derived.exchange_forward({A: a_lease, B: b_lease}) → Lease[C]`
  (this is a small bespoke `IRHandleLocalImplBase` subclass private
  to the test). Send `(b_lease, converter_handle, arena_C_handle)` to
  tasks 1 and 3.
- **Back in task 1.** Receive the triple from task 2. Mint local
  `D = DerivedLocalLifetime` with inputs `{A, B}`. Construct
  `CapIRArena.with_converter(z=D.token, x=A, y=C, k=B, remote=arena_C_handle,
  derived=D, converter=converter_handle)`. Bridge into task 1's
  original scope via `lazily_attach_from_arena` using a case-2 sync
  `LifetimeConverter` that wraps `D.exchange_forward(...)`. Load one
  of the three task-2 entities; verify it transitively reaches the
  task-1-stored entity. Send a clone of task 1's main lease to task 3.
  Drain task 1's scope (and `D.aclose()`).
- **Task 3.** Combine task 1's lease with task 2's converter handle
  + key lease to mint a `Lease[C]` (via the converter's
  `pipelined_lifetime_converter_from_handle` wrapper or by direct
  dispatch). Wrap `arena_C_handle` as `CapIRArena.direct(z=C, x=C,
  remote=arena_C_handle)` (mode 1, no derivation — task 3 already
  has a `Lease[C]`). Mount into a temporary scope; load the second
  entity (which deep-bridges through to task-1 data). Exit temp
  scope — nothing should close. Drop the output lease for task 2 —
  nothing should close. Then drop the main lease for task 1: both
  task 1 and task 2's output tokens should close.
  Drop the key lease for task 2 — task 2's key lifetime should
  close. Verify with end-callback subscriptions.

Three additional shorter tests, each focused on one capability:

1. `test_cap_ir_arena_direct_mode` — mode 1 only: an arena handed
   over the wire, exercise `load_flat`, `unwrap_provenance` (cache
   hit on a `c`/`p`/`u`-node loaded earlier; `None` for non-
   provenance IDs), `is_tracked`, `abandon`, clean drain.
2. `test_cap_ir_arena_copy_to_new_arena_pipelined` — fork an arena
   *from a fork* (so the second `copy_to_new_arena` runs against an
   `IRFuture[IRHandle[…]]` remote, exercising
   `pipeline_dispatch`), load through both forks, drain.
3. `test_cap_ir_arena_converter_pipelined` — mode 3 with a
   converter, single load, asserts that the converter dispatch and
   the arena dispatch issue concurrently (verified by intercepting
   the two pushes in the local IR runner — easiest is to assert
   `task_group.start_soon` is called twice synchronously, before
   either resolves).

Each test asserts no lease leaks via the substage-3 autouse fixture.

---

## Smaller unit tests

In `tests/test_data/test_cap_ir_arena_unit.py`:

- `_encode_fxtr_id` / `_decode_fxtr_id` round-trip across each
  node tag (`d`, `c`, `p`, `u`, `n`, `!`). Also: an ID with a
  non-`None` `_provenance_parent` round-trips as data-equal but
  with `_provenance_parent is None` on the decoded side
  (provenance parent stripped on the wire).
- `_compute_precomp_closure` shape: walk a synthetic arena with a
  chain `c → ctx_node → d`, assert the returned list has an entry
  for the `c`-node with the right `ctx_node` target, and that the
  closure includes every reachable provenance ID. Round-trip via
  `_ingest_precomp` into a stub `CapIRArena` and assert the three
  precomp dicts have the expected `FxtrID → FxtrID` mappings.
- `ArenaIRHandleAdapter._handle_load_flat` direct call (no Cap-IR
  dispatch yet): construct a `MemoryArena`, store some entities,
  call the handler with a hand-built `Lease` and `Call("load_flat",
  …)` arg, assert the returned dict shape.
- `CapIRArena._make_y_lease` mode dispatch: three cases (mode 1
  passes through, mode 2 reverse-exchanges, mode 3 dispatches the
  converter and returns `UnwrapForPipelining`). Use a stub
  `IRHandle[B_K]` for the mode-3 case.
- `CapIRArena.copy_to_new_arena` returns a sibling whose `_remote`
  is an `IRFuture` (not an `IRHandle`).
- `pipelined_lifetime_converter_from_handle` round-trip with a stub
  handle that resolves the inner future synchronously.
- Fork cleanup via the existing `MemoryStore` finalize: spin up
  an adapter, call `_handle_fork`, drop the returned adapter,
  force a `gc.collect()`, assert the underlying `MemoryStore`
  has dropped the fork's `ArenaKey` from `store._arenas`. The
  matching INFO log line is expected (asserted via `caplog`,
  not suppressed).

---

## Files touched

| File | Change |
|---|---|
| `fxtr2/data/cap_ir_arena.py` (new) | `ArenaIRHandleAdapter`, `CapIRArena`, wire helpers |
| `fxtr2/leases/leased_cap_common.py` | Add `PipelinedLifetimeConverter` type alias + `pipelined_lifetime_converter_from_handle` helper |
| `fxtr2/data/abstract_arena.py` | Cross-reference `PipelinedLifetimeConverter` from the `LifetimeConverter` docstring (one-line "see also" pointer) |
| `tests/test_data/test_cap_ir_arena.py` (new) | Acceptance test + 3 focused tests |
| `tests/test_data/test_cap_ir_arena_unit.py` (new) | Wire helpers + helper unit tests |
| `fxtr2/leases/LOCAL_CODE_OVERVIEW.md` | Add the new type alias to the file overview table; brief mention in "Cap-IR layer" section |
| `fxtr2/data/CODE_OVERVIEW.md` (and root `CODE_OVERVIEW.md`) | New module entry; brief design overview |

---

## Implementation order / checklist

1. [x] Add `PipelinedLifetimeConverter` + `pipelined_lifetime_converter_from_handle`
       in `fxtr2/leases/leased_cap_common.py`. Add cross-reference in
       `fxtr2/data/abstract_arena.py:LifetimeConverter` docstring.
2. [x] Update `fxtr2/leases/LOCAL_CODE_OVERVIEW.md` — file table, brief blurb.
3. [x] Land wire-encoding helpers (`_encode_fxtr_id` / `_decode_fxtr_id`,
       precomp encode / ingest, flat-data encode / decode). Unit-test in
       isolation via `tests/test_cap_ir_arena_unit.py`. *Tests live at the
       top-level `tests/` directory rather than `tests/test_data/`, matching
       the existing convention (see e.g. `tests/test_memory_arena.py`).*
4. [x] Land `ArenaIRHandleAdapter` (read methods first: `load_flat`,
       `abandon`); unit-test the handlers directly via hand-built
       `Call` args.
5. [x] Add `copy_to_new_arena` (server side); unit-test that
       GC of the returned adapter triggers `MemoryStore`'s
       existing finalize (and the expected INFO log).
6. [x] Update `CODE_OVERVIEW.md` (new module entry, design pointer).
7. [x] Land `CapIRArena` (state, factories, `_make_y_lease`,
       `_dispatch_remote`, `_request_remote`); read methods
       (`is_tracked`, `load_flat`, provenance reads); `abandon`;
       `close`. Unit-test mode dispatch and provenance cache.
8. [x] Add `CapIRArena.copy_to_new_arena` (returns fork-form sibling).
       Unit-test pipelining behaviour.
9. [x] Write the three short acceptance tests
       (`test_cap_ir_arena_direct_mode`,
       `…copy_to_new_arena_pipelined`,
       `…converter_pipelined`).
10. [x] Write the full task-1/task-2/task-3 acceptance test
        (`TestThreeTaskAcceptanceCapIR.test_three_tasks_drain_in_lockstep`).
        Mirrors `tests/test_entity_ref_scope/test_derived_scope_acceptance.py`
        with the same four lifetime-cascade predictions, but every cross-
        task arena handoff goes through `ArenaIRHandleAdapter`/`CapIRArena`
        and the converter handoff goes through an `IRHandle[K]`
        (`_ExchangeForwardConverter`) that wraps `derived.exchange_forward`
        on the issuer side. The receivers build local sync converters for
        the memory store (mode 3 in task 1's case-2 attach) or dispatch the
        IR-converter once synchronously to mint a `Lease[Y]` for the
        memory store's stored key (mode 1 in task 3's case-1 attach).
        The TaskNode entity-type import is at module level so its
        system-entity registration happens at pytest collection time
        (before the autouse `global_scope` fixture snapshots
        `initial_liveset`).
11. [x] Update `CODE_OVERVIEW.md` — final pass with any
        post-implementation deviations.

Each step leaves the test suite green. The module is self-contained,
so there's no in-tree caller depending on it during the substage —
substage 6 is the first consumer.

---

## Open questions / things to watch during implementation

- **Untested provenance method coverage.** `c_node_to_context_data`
  / `p_node_to_pathroot` / `step_back_p_ctx` are cache-only and the
  acceptance tests don't have a clear path to exercising them
  (the test entities are simple `d`-nodes). Add a focused test if
  the precomp closure walk turns out to need fine-tuning; otherwise
  rely on the precomp closure unit test plus the HTTP suite's
  existing coverage of the same pattern.
- **Fork close timing.** Cleanup is GC-driven via `MemoryStore`'s
  existing `weakref.finalize` on `MemoryArena`; the INFO log
  `"Arena {key} was finalized while open!"` is expected once per
  fork lifetime. If this gets noisy in the test suite, the
  follow-up is the `close()`-takes-a-lease refactor noted in
  `overview.md`'s "Open follow-ups" section, which lets us
  propagate close signals across `CapIRArena`. Until that lands,
  GC-plus-INFO-log is the substage-5 contract.
- **`_remote_lease`'s clone count under sustained load.** Every
  fork-form dispatch clones `_remote_lease`. If we ever build a
  CapIRArena whose remote is a long chain of pipelined forks, this
  is a per-call cost. Acceptable for substage 5; revisit if
  substage 6 finds it expensive in practice.

---

## Deviations from the plan during implementation

This section consolidates every place the implementation differs from
the original substage-5 plan above. Read this if you're cross-referencing
the plan against the landed code.

### Wire protocol

- **Three wire methods, not four.** The plan listed
  `load_flat` / `unwrap_provenance` / `abandon` / `copy_to_new_arena`.
  We dropped `unwrap_provenance` from the wire — every legitimate
  provenance read is covered by the closed precomp map returned by
  `load_flat`, since the abstract API requires the input ID to
  already be tracked. The server's `_compute_precomp_closure` helper
  still exists (it's how the precomp map is built); there's just no
  `unwrap_provenance` wire entry-point on `ArenaIRHandleAdapter`.
- **`FxtrID` encoding strips provenance parents.** `_provenance_parent`
  is a per-process Python-level marker that's never persisted and
  isn't part of equality/hashing, so we send only `bytes(id) ==
  id._raw_data_id` on the wire. Provenance *nodes* (c/p/u/n
  identities encoded inside `_raw_data_id`) survive unchanged.
  Documented in the `FxtrID encoding` section above.
- **Precomp encoding is a list of entries, not a dict** (Option B from
  the design discussion). Each entry has its own `id` field plus
  optional `unwraps_to` / `ctx_node` / `step_back_p_ctx` (all tagged
  FxtrIDs). No `id_string` round trip; client builds three local
  `dict[FxtrID, FxtrID]` caches by walking the list once. Same
  `IRTagged("fxtr_id", bytes)` encoding everywhere — no separate
  interned form.

### `CapIRArena` type signature

- **One class type parameter, not four.** The class is
  `class CapIRArena[B_Z: LifetimeBrand]`, generic only on the
  user-visible brand. Internal X / Y / K relationships are stored
  as `LifetimeToken[Any]` fields with documented invariants and
  validated at construction time. The plan's
  `CapIRArena[B_Z, B_X, B_Y, B_K]` would have forced every method
  signature to drag along three irrelevant type parameters; the
  one-parameter form keeps the public API tight while still
  enforcing brand-level safety at the *constructor* level via
  per-mode classmethod factories with their own type parameters.
- **No stored `_y_token`.** The original `_y_token` field turned
  out to be dead — `_make_y_lease` doesn't read it, and the
  no-converter validation (`remote.token is x_token`) is
  expressible without storing Y separately. Constructors don't
  take `y_token` either; the brand is captured via the typed
  `remote: IRHandle[YBrand]` parameter.

### Constructor design

- **Three classmethod factories, not two.** The plan had
  `direct` (modes 1+2) and `with_converter` (mode 3). We split
  `direct` into:
  - `direct[B](*, token, remote, …) -> CapIRArena[B]` — mode 1
    (Z = X = Y = B). The shared `B` brand catches mismatched
    remotes at the type-check level.
  - `derived[OutBrand: LocalLifetimeBrand, XBrand](*, derived,
    x_token, remote: IRHandle[XBrand] | …, …) -> CapIRArena[OutBrand]`
    — mode 2 (Y = X via shared `XBrand`).
  - `with_converter[OutBrand, XBrand, YBrand, KBrand](*, derived,
    x_token, k_token, remote: IRHandle[YBrand] | …,
    converter: IRHandle[KBrand], …) -> CapIRArena[OutBrand]` —
    mode 3. `converter` and `k_token` share `KBrand` so a
    wrong-brand converter handle fails to type-check.
  Splitting was necessary because the plan's single `direct`
  factory (with `z_token: LifetimeToken[B]`,
  `x_token: LifetimeToken[B]`) wrongly required Z and X to share
  a brand even in mode 2 (where Z is a derived lifetime distinct
  from X).
- **Factories use `cls: type["CapIRArena[OutBrand]"]` annotations
  to avoid casts.** Without the explicit `cls` typing, the `cls(…)`
  call inside a classmethod returns `Self@CapIRArena[B_Z]` (the
  class's own type var), which pyright treats as invariant against
  the factory's `OutBrand` and forces `cast`s to launder things
  through `Any`. Annotating `cls: type["CapIRArena[OutBrand]"]`
  specialises the cls binding at the factory's brand and lets
  every input flow through without a cast.
- **Public `inputs` property on `DerivedLocalLifetime`.** Added in
  `fxtr2/leases/local_lease.py` so `CapIRArena.__init__` can validate
  that `x_token` and `k_token` are members of the supplied
  `derived.inputs` without poking at the private `_input_tokens`
  attribute. Not in the original plan; small and self-contained.

### Server-side fork lifecycle

- **No `_owns_arena` flag, no own `weakref.finalize`.** The plan
  initially proposed flagging fork-adapter instances and registering
  our own `weakref.finalize(adapter, arena.close)`. We dropped this
  in favour of relying on the existing
  `MemoryStore.new_arena` finalize: when the receiver discards the
  fork's future_lease and the IR future drops the returned adapter,
  the wrapped `MemoryArena` is GC'd and the store reclaims the
  liveset row (with an INFO log on non-empty forks). Documented in
  the "Fork cleanup" section above. Long-term clean fix is the
  `close()`-takes-a-lease refactor in `overview.md`'s "Open
  follow-ups".

### `load` for derived nodes

- **`CapIRArena.load(...)` raises `NotImplementedError` for derived
  nodes.** d/n nodes work (CBOR-decode the bytes); c/p/u nodes
  raise. Their bytes-form synthesis is server-internal in the
  memory store, and re-implementing it on the client would
  duplicate substantial logic. Substage-5 callers should use
  `load_flat` and bridge via `lazily_attach_from_arena` to let
  the consumer scope's memory store re-synthesise — which is
  exactly what the acceptance tests exercise.

### Test layout / coverage

- **Tests live at `tests/` root**, not under `tests/test_data/`,
  matching the existing `tests/test_memory_arena.py` convention.
  Two files: `tests/test_cap_ir_arena.py` (32 tests; adapter +
  CapIRArena + acceptance) and `tests/test_cap_ir_arena_unit.py`
  (24 tests; wire-helper unit tests).
- **Three focused acceptance tests landed as planned**, but the
  one for converter pipelining (`TestAcceptanceConverterPipelined.
  test_converter_chain`) does *not* assert dispatch concurrency at
  the IR-runner level (the plan suggested intercepting
  `task_group.start_soon`). It just verifies the chain works
  end-to-end, which is a weaker but adequate claim. Final form
  uses `_ExchangeForwardConverter` (real `derive_Y.exchange_forward`
  pattern) over a distinct server-side `derived_Y` arena, with a
  separate client-side `derived_Z`; an earlier draft used a
  `_PassThroughConverter` stub which was removed as redundant.
- **Three-task acceptance test mirrors substage 3 closely**
  (`TestThreeTaskAcceptanceCapIR.test_three_tasks_drain_in_lockstep`).
  Asserts the same four cascade predictions as
  `tests/test_entity_ref_scope/test_derived_scope_acceptance.py`,
  with cross-task arena handoffs going through
  `ArenaIRHandleAdapter` / `CapIRArena` and the converter handoff
  through an `IRHandle[K]` (`_ExchangeForwardConverter`). The
  receiver-side memory-store converters are local-only sync
  closures: in task 1's case-2 attach the CapIRArena has its own
  z_token D = `derive({A, B})` (a fresh local derived) and the
  memory-store converter is `(B, A) -> Lease[D]`; the
  D-to-server-side-token translation happens inside
  `CapIRArena._make_y_lease` via the IR-converter dispatch
  (substage-4 pipelined). Task 3's case-1 attach dispatches the
  IR-converter once synchronously to mint a `Lease[scope2_token]`,
  which then drives a mode-1 CapIRArena under the default
  converter. The `TaskNode` import is at module level so its
  system-entity registration happens at pytest collection time
  (before the autouse `global_scope` fixture snapshots
  `initial_liveset`).
- **No `c_node_to_context_data` / `p_node_to_pathroot` /
  `step_back_p_ctx` test coverage.** These provenance methods are
  cache-only and the acceptance tests don't naturally reach them
  (the test entities are simple `d`-nodes). The precomp closure
  walk *is* exercised via the unit tests and the
  `TestAdapterLoadFlat.test_load_flat_through_provenance` adapter
  test (which loads a c-node and verifies the closure shape). Add a
  focused test if a substage-6 use case lights up these paths.

### Misc

- **Cast cleanup pass**: down to 4 casts in the whole module (one
  shape-narrowing in `_decode_flat_data` and three
  `cast(IRValue, arg)` calls where the source type
  `UnwrappedCallArg` is structurally wider than `IRValue`). All
  factory and `_make_y_lease` casts were eliminated by the
  `cls: type[…]` annotation trick and pyright's inference of
  `UnwrapForPipelining`'s `T` parameter from return-type context.
