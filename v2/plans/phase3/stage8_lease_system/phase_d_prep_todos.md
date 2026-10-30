# Stage D prep: follow-ups before filling in bodies

> **Archive note (§2k doc pass).** This doc is **historical / archived**. It was the
> scratch / working doc written during the Stage D types-and-docstrings sketch pass,
> before the main plan at `phase_d_plan.md` existed. Its contents have been fully
> migrated into the main plan:
>
> | Section here | Where it lives now |
> |---|---|
> | §1 small fixes (A/B/C/D/E/F/G/H/I/J/L) | Landed directly in `lowering_types.py` / `lowering.py` during the sketch pass. |
> | §1 small fix G-partial (`UnpullMessage` wording) | Superseded by the §2h obligation-accounting reshape in `phase_d_plan.md`; current `UnpullMessage` docstring reflects the final rule. |
> | §2 / §K committed design (phase-1 substitution + phase-2 evaluators) | `phase_d_plan.md` §2c (phase 1) and §2d (phase 2). Implemented there. |
> | §3a `_LeaseCleanupInfo` generification | Promoted into `phase_d_plan.md` §1c. Implemented. |
> | §3b `LifetimeToken` / `Grant` hashability | Promoted into `phase_d_plan.md` §1d. Implemented. |
>
> Kept around for historical reference only — **do not extend this doc for new
> work**; edit `phase_d_plan.md` (or a new plan) instead. The per-item status below
> was correct at the time of writing and is not updated going forward.

This doc captures the punch list of clarifications and cleanups that came out of the
types-and-docstrings sketch pass for `fxtr2/rpc/lowering.py`, plus the design decisions
pinned down before writing bodies. Workflow:

1. ✅ Apply the small fixes in §1 (mechanical; done).
2. ✅ Agree on the async evaluator flow (§K). Committed design below.
3. Start implementation of Stage D bodies on top of the committed shape.
4. Clean up `_LeaseCleanupInfo` + `LifetimeToken` hashability (§3; can wait until
    Stage D bodies are mostly in place).

---

## 1. Small fixes to apply now

### `lowering_types.py`

- **A. Refcount wording.** Update `DiscardMessage` docstring. From the importer's
    perspective, an import's refcount is incremented by exactly two things:
    1. Allocating the import (via `PushMessage` — token/future/grant each +1).
    2. Receiving an `Exported…Expr` from the exporter.
    Sending an `Imported…Expr` back to the exporter does **not** re-assert anything and
    must not be counted — the recipient is free to do whatever they like with values
    they receive. IDs 0/1/2 each start at refcount 1 by the act of opening the
    connection (no message carries those introductions).
- **G-partial. Pull refcount wording.** Update `UnpullMessage` docstring. From the
    importer's perspective, the pull-refcount is incremented by exactly two things:
    1. Sending a `PullMessage`.
    2. Receiving an `AwaitFutureExpr` (from the exporter) that references one of the
        importer's own imports (because the only way to evaluate the expression is to
        pull). `AwaitFutureExpr` the importer **sends** does **not** count — the
        exporter is free to resolve or forward via pipelining, and is not obligated
        to deliver a resolution just because the expression appears.

### `lowering.py`

- **B. Drop `_ImportKind`.** Redundant now that we have four entry dataclasses; discriminate
    by `isinstance` / structural pattern matching.
- **C. Track live imported leases on the grant entry.** Add a
    `live_leases: dict[int, LocalLifetimeLease[Any]]` (keyed by `id(lease)`) on
    `_GrantImportEntry`. `_register_lease` inserts; `_clean_up_lease_data` removes. This
    also lets abort iterate the grant's live leases directly to break them. Avoids
    having to thread a `lease_id → grant_import_id` map through the connection.
- **D. Populate `_FutureImportEntry.resolution`.** Uncomment the field; hold a
    `LocalPendingResultBox` (reuse as-is unless it turns out to want a thinner variant
    that doesn't own a task group). `_on_resolve` / `_on_reject` drive it; waiters from
    `RemoteIRFuture.wait` subscribe to it.
- **E. `_TokenExportEntry.watch_received` → `watch_deregister`.** Store the deregister
    callback (`Callable[[], None] | None`) returned by `register_end_callback`, so abort
    can unsubscribe cleanly.
- **F. Google-style docstring on every entry field.** Flesh out per-attribute docs for all
    eight import/export entry dataclasses. Use `Attributes:` blocks, cross-referencing
    shared fields to one canonical description ("See `_GrantExportEntry`") to avoid
    duplicating long explanations.
- **G-partial. `_FutureExportEntry` resolver task state.** Add a field tracking the
    task/cancel-scope we spawn to deliver the resolution (so we can cancel it if
    `pending_pull_count` drops to zero via `UnpullMessage` before we've sent anything).
    Shape: probably `resolve_task_cancel_scope: anyio.CancelScope | None`.
- **H. Rename `Imported…` proxies → `Remote…`.** `ImportedLifetimeToken` →
    `RemoteLifetimeToken`, `ImportedGrant` → `RemoteGrant`, `ImportedIRHandle` →
    `RemoteIRHandle`, `ImportedIRFuture` → `RemoteIRFuture`. Also remove the explicit
    `"ImportedIRFuture[IRHandle[Any], B]"` string annotation on
    `pipeline_dispatch`'s `self:` — unnecessary given `from __future__ import
    annotations`.
- **I. Clarify "drain".** In the class docstring for `LeasedCapRPCConnection`: "the
    connection fully drains" ≡ neither side holds a non-broken lease imported from the
    other side.
- **J. Message handlers are non-blocking.** Document that each `_on_*` handler does only
    synchronous work: mutating tables, firing local callbacks, and spawning tasks into
    the connection's task group for anything that must await. Make the handlers `def`
    rather than `async def` to make this an enforced shape (see §K — once we pin down
    the evaluator shape, this should fall out naturally).
- **L. Re-export checker state.** Add connection fields mapping locally-owned values back
    to their existing export IDs so repeated sends of the same object produce equal
    proxies on the receiver. Likely shapes (to be confirmed while writing `_devaluate`):
    - `_grant_export_ids: dict[Grant[Any], int]` — local `Grant`s are hashable and
        compared by identity (via `LocalGrant`) or dataclass value (attached-output
        grants), so a plain dict works.
    - `_token_export_ids: dict[int, int]` — keyed by `id(token)`; cleaned up when the
        corresponding export entry is removed.
    - `_handle_export_ids: dict[int, int]` — same shape.
    - `_future_export_ids: dict[int, int]` — same shape.
    Identity-based keys are safe because the export entry itself keeps a strong
    reference to the object, so `id()` stays stable while the entry exists.

---

## 2. Committed design: two-phase evaluator

### K. Phase 1 (sync substitution) + Phase 2 (async three-mode execution)

Resolved. We went back and forth between a pure-sync evaluator returning
`IRValue | UnwrapForPipelining` and a two-phase sync-substitute + async-execute
design. The blocking counter-example for the pure-sync design is nested
`AwaitFutureExpr`: `UnwrapForPipelining(future, lease)` takes concrete runtime values,
so an `AwaitFutureExpr` whose `lease` is *itself* an `AwaitFutureExpr` forces a real
await before the outer can be packed. The two-phase design handles this cleanly and
also buys us free graceful error handling for ill-typed peer input (see §K.3).

#### K.1. Phase 1 — sync substitution, in the reader loop

Walks the incoming `Expression[T]` tree once and substitutes every `Imported…Expr` /
`Exported…Expr` leaf with a private `_ConstantExpr(value)` holding the already-resolved
runtime value.

- `ImportedXExpr(id)` — sender's import is our export. Look up our `_…ExportEntry`,
    return the concrete value (a cloned backing-lease for grants; the local handle /
    future / token for the other kinds).
- `ExportedXExpr(id, ...)` — sender's export is our import. If we already have an
    entry, validate that the kind and associated-token-id match (protocol violation →
    abort). Otherwise register a new `_…ImportEntry` + mint a `Remote…` proxy and
    return it.
- Everything else (`StaticExpr`, containers, `WalkFutureExpr`, `AwaitFutureExpr`,
    `HandleRequestExpr`, `HandleDispatchExpr`) passes through with its children
    substituted — the node itself stays in the tree for phase 2 to handle.
- `_ConstantExpr` lives privately in `fxtr2/rpc/lowering.py`. We may lift it to
    `lowering_types.py` later if we want to expose "expressions that evaluate to
    runtime values" as a user-facing concept, but private-for-now keeps the
    wire-protocol types cleanly separated from the runtime-only carrier type.

Phase 1 is the *only* place that mutates import/export tables. No async, no awaits.

#### K.2. Phase 2 — evaluator modes

Three async modes run inside the connection's task group (or handle-dispatch task
groups for work that flows through), plus one sync "static-only" mode that skips
phase 1 entirely.

```python
# Async modes — run on phase-1-substituted trees:
async def _eval_as_value(expr) -> IRValue: ...
async def _eval_as_call_arg(expr) -> IRCallArg: ...
async def _eval_forwardable(expr) -> IRValue | UnwrapForPipelining: ...

# Sync mode — runs on the raw expression; used for RejectMessage / AbortMessage:
def _eval_static(expr) -> IRStatic: ...
```

Per-node table (async modes use phase-1-substituted trees, so `Imported…Expr` /
`Exported…Expr` are "should never be seen here" cases; `_eval_static` runs on the
raw tree and outright rejects them along with every other non-static node):

| Node | `_as_value` (async) | `_as_call_arg` (async) | `_forwardable` (async) | `_static` (sync) |
|---|---|---|---|---|
| `StaticExpr(v)` | `v` | `v` | `v` | `v` |
| `_ConstantExpr(v)` | `v` | `v` | `v` | n/a (phase 1 not run) |
| `ListExpr` / `DictExpr` / `IRTaggedExpr` | recurse children `_as_value` | recurse children `_as_call_arg` | recurse children `_as_value` (container must be IRValue) | recurse children `_static` |
| `WalkFutureExpr(inner, path)` | `(await _as_value(inner)).walk(path)` | same | same | raise |
| `AwaitFutureExpr(fut, lease)` | `await fut_val.wait(lease_val)` | `UnwrapForPipelining(fut_val, lease_val)` | same as `_as_call_arg` | raise |
| `HandleRequestExpr(...)` | dispatch sync → `await future.wait(result_lease)` | dispatch sync → `UnwrapForPipelining(future, result_lease)` | same as `_as_call_arg` | raise |
| `HandleDispatchExpr(...)` | dispatch sync → `[future, result_lease]` | same | same | raise |
| `Imported…Expr` / `Exported…Expr` | raise `IRException` (phase-1 should have substituted) | same | same | raise (protocol violation — no stubs allowed in static contexts) |

`_forwardable`'s return type `IRValue | UnwrapForPipelining` is exactly the shape
`spawn_ir_future_in_task_group` already routes: `IRValue → box.set_result`;
`UnwrapForPipelining → box.resolve_from`; `IRException raised → box.set_exception`;
other `Exception → wrapped-IRException → box.set_exception`.

`_eval_static` returns `IRStatic` (no leases, handles, futures, or tokens). It runs
directly on the raw incoming expression — no phase-1 table mutation — because the
protocol guarantees reject / abort payloads are stub-free, and skipping phase 1
means a malformed peer can't trick us into allocating and then leaking imports we
were never going to consume. Any non-static node is a protocol violation; the
handler catches the resulting exception and synthesizes a wrapped `IRException`
instead (see §K.4).

#### K.3. Type mismatches → per-future failure, not connection abort

When phase 2 encounters a `_ConstantExpr` whose value doesn't match the current
mode's expected type (e.g. `_eval_as_value(AwaitFutureExpr(_ConstantExpr(not_a_future),
...))`), the evaluator raises an `IRException` describing the mismatch. Because we're
running inside `spawn_ir_future_in_task_group` (or the equivalent plumbing in
`_on_resolve` / `_on_reject`), the exception becomes a failure on the *specific*
push/resolve/reject future, not a connection abort. This is important: the peer may
have pushed untrusted malformed data, and rejecting the one affected future is a
better recovery than tearing down the whole connection.

Phase-2-encountered residual `Imported…Expr` / `Exported…Expr` nodes are a different
matter — those indicate a phase-1 bug rather than peer-driven malformed input — but
the same recovery path applies: raise an `IRException`, the box absorbs it, internal
error, connection stays up. (If we later find we'd rather abort on that case to
surface bugs faster, we can tighten it then.)

#### K.4. Top-level caller shapes

- **`_on_push(msg)`**: phase-1 substitute; allocate `(token_id, future_id, grant_id)`;
    call `spawn_ir_future_in_task_group(work=lambda: _eval_forwardable(substituted),
    task_group=self._tg)` to get `(local_future, root_lease)`; register the three
    export entries wired to that future's lifetime. No direct box-routing code at
    the call site — the spawn helper does it.
- **`_on_resolve(msg)`**: phase-1 substitute; spawn a task in the connection's task
    group that runs `_eval_forwardable` and routes the outcome into the existing
    `_FutureImportEntry.resolution` box. The routing is a ~5-line match on
    `IRValue` / `UnwrapForPipelining` / exception → `set_result` / `resolve_from` /
    `set_exception`. Not worth deduplicating with `spawn_ir_future_in_task_group`'s
    body.
- **`_on_reject(msg)`**: run `_eval_static(msg.expression)` directly (no phase 1, no
    spawn). If it returns an `IRException`, call `box.set_exception(result)`. If it
    raises or returns a non-`IRException`, synthesize an `IRException` describing the
    malformed payload and use *that* as the rejection reason. All sync — reject
    handling never needs the task group.
- **`_on_abort(msg)`**: same shape as `_on_reject`, but the resulting `IRException`
    is passed to `self._abort(...)` as the connection-level abort reason. Also all
    sync. The protocol (and the docstring on `AbortMessage`) promises abort payloads
    are static; malformed input falls back to a synthesized "peer sent malformed
    abort" reason and we still tear down cleanly.

#### K.5. Concurrency rule

Both sides that mutate tables — the reader's phase-1 sync pass, and the
user-facing devaluator invoked from `push` / `dispatch` — run synchronously without
awaits. On a single anyio event loop they cannot interleave with each other or with
each other's inner work. Async phase-2 evaluation never touches tables. The writer
loop drains the outbox and hits the transport; also no table mutations.

The spawned resolver task on the export side (see
`_FutureExportEntry.resolve_task_cancel_scope`) is async and can await the local
future; it only interacts with tables via `_enqueue_message`, which is sync and
goes through the outbox.

#### K.6. ResolveMessage forward-delegation (previously §3)

Subsumed by phase 2. `_on_resolve`'s spawned task runs `_eval_forwardable`, which
returns `UnwrapForPipelining` whenever the incoming expression can be forwarded
(top-level `AwaitFutureExpr` or `HandleRequestExpr`). The subsequent
`box.resolve_from` is exactly the spec's "forward" semantics, and it automatically
chains for multi-hop pipelining (A→B→C) because each hop's box ultimately forwards
to the next.

The spec wording in `lowering_types.py` currently says forwarding is "encouraged"
on top-level `AwaitFutureExpr`. We should tighten it to "required when the
recipient has a pending-result box capable of forwarding" (essentially always, for
us). Note for Stage D bodies: a two-hop pipelining test should exercise this.

---

## 3. Cleanup tasks (can wait)

Both of these are cleanups to `fxtr2/leases/` that unblock cleaner Stage D code, but
are not strictly required to start implementing the lowering layer. Batching them
together after Stage D bodies settle will avoid churning unrelated code.

### 3a. Generify `_LeaseCleanupInfo`

Currently `_LeaseCleanupInfo` is hard-coded to
`UnconditionalLocalLifetime`/`AttachedLocalLifetime`: its `via: Lease[Any] | None` and
`kind: _LeaseKind` fields only make sense for the attached case, and every unconditional
entry carries a dead `via=None` + `kind="unconditional"` triple. `LeasedCapRPCConnection`
will want its own owner-specific info (e.g. the grant import ID).

Proposed refactor:

```python
class _LeaseOwner[RB, I](Protocol):
    def _register_lease(self, lease: LocalLifetimeLease[RB]) -> None: ...
    def _deregister_lease(self, lease: LocalLifetimeLease[RB]) -> None: ...
    def _clean_up_lease_data(self, lease_id: int, info: I) -> None: ...
```

Owners define their own cleanup-info struct:

- `_UnconditionalLeaseCleanupInfo(lease_repr, alloc_tb)`
- `_AttachedLeaseCleanupInfo(kind, via, lease_repr, alloc_tb)`
- `_ConnectionLeaseCleanupInfo(grant_import_id, lease_repr, alloc_tb)` (future)

The weakref finalizer already captures the per-owner `info` value in its args, so the
refactor is mostly typing + splitting the existing struct.

### 3b. Make `LifetimeToken` / `Grant` hashable-safe

We want to key tokens (and grants, though they already are) directly by object in
connection-level dicts like the re-export checker. To support this cleanly:

- Make `LifetimeToken[B]` inherit from `collections.abc.Hashable` (currently only
    `abc.ABC`). Callers should be able to use a token as a dict key without thinking
    about it.
- Document the convention that **tokens and grants only hold weak references to
    mutable state** (e.g. end-callback lists, backing-grant lists). This means using
    a token or grant as a dict key doesn't keep heavy state alive, and doesn't create
    subtle reference cycles via the dict → token → state → back chain.
- Audit concrete implementations:
    - `LocalLifetimeToken` currently holds `_end_callbacks: list[...]` directly. Move
        it behind a weakref-holder (or push the list onto a separate `_TokenState`
        object that the token references weakly).
    - `LocalGrant` holds a strong ref to its subject token — that's fine (the token
        is small), but worth noting explicitly in the convention.
- Add an explicit `__hash__` declaration on `LocalLifetimeToken` matching
    `object.__hash__` (identity-based), so that value-equality subclasses in the
    future don't silently disable hashing.
