# Substage 4 — Pipelined dispatch leases

Detailed implementation plan for substage 4 of
[stage 8c](overview.md#substage-4--pipelined-dispatch-leases).

**Goal.** Widen `IRHandle.dispatch` / `IRHandle.request` so the *outer* lease
(the one that authorises the dispatch on the handle itself) may be either a
concrete `Lease[B]` *or* an `UnwrapForPipelining[Lease[B], K]`. The local
runner unwraps the outer lease inside the spawned task (before invoking the
subclass's `_run_request_async`); the wire protocol re-uses the existing
`AwaitFutureExpr` shape (no schema change, no codec change); the inbound
evaluator passes the pipelined lease *through* to `handle.dispatch` rather
than awaiting it eagerly, so a peer that feeds us an `AwaitFutureExpr` in the
push lease slot doesn't gate the dispatch on a synchronous round-trip.

This is a small, self-contained extension. It unblocks substages 5 (the
`CapIRArena` wants to dispatch a converter and chain its result-lease
straight into the remote-arena dispatch in a single non-blocking step) and 6
(scope cap-bridges produce `Lease | UnwrapForPipelining[Lease, …]` from a
synchronous `lease_provider()`).

---

## Public surface

### `fxtr2/leases/leased_cap_ir.py`

Two signature changes on the abstract base class. Nothing else.

```python
class IRHandle[B: LifetimeBrand](abc.ABC):
    async def request(
        self,
        lease: Lease[B] | UnwrapForPipelining[Lease[B], Any],
        request: Request,
    ) -> IRValue: ...

    def dispatch[RB: FreshLifetime[Any]](
        self,
        lease: Lease[B] | UnwrapForPipelining[Lease[B], Any],
        request: Request,
    ) -> tuple[IRFuture[Any, RB], Lease[RB]]: ...
```

The `B` brand parameter on the future inside `UnwrapForPipelining` matches
the handle's brand, but in practice callers will be working with brand-erased
`Any` (the future producing the lease lives on a *different* lifetime — its
own Cap-IR call's result lifetime — and the bound is on the resolved value
type, not the future's own brand). The `K` in the substage description
corresponds to the second type parameter of `UnwrapForPipelining`; we use
`Any` in the signature for consistency with `pipeline_dispatch`'s existing
`inner_handle_lease: Lease[Any] | UnwrapForPipelining[Lease[Any], Any]`
shape.

`IRFuture.pipeline_dispatch.future_lease` is **not** widened — the substage's
new pipelining hop is on the inner handle's outer lease, not on the future
that's being walked into a handle. (`inner_handle_lease` was already
pipelined-capable.)

### Docstring updates

- `IRHandle.dispatch` / `IRHandle.request`: add a paragraph explaining the
  pipelined-lease form, with the same ownership rules as the concrete-lease
  form (taken at entry; if `UnwrapForPipelining`, both the inner lease *and*
  the future-of-lease's resolution-lease are owned by the implementation
  for the duration of the call).
- `UnwrapForPipelining` docstring: note that the lease slot of
  `IRHandle.dispatch` / `IRHandle.request` now accepts this type, listing
  it alongside the existing call-arg / `pipeline_dispatch.inner_handle_lease`
  use-sites.

---

## Local impl: `IRHandleLocalImplBase`

`fxtr2/leases/leased_cap_common.py`. The existing `dispatch` body splits
into "synchronously take ownership of inputs", "spawn the runner",
"runner unwraps and calls `_run_request_async`". Substage 4 only widens
the first and last steps.

### `dispatch` entry

```python
def dispatch(
    self,
    lease: Lease[B] | UnwrapForPipelining[Lease[B], Any],
    request: Request,
) -> tuple[IRFuture[Any, RB], Lease[RB]]:
    if isinstance(lease, UnwrapForPipelining):
        taken_lease: Lease[B] | UnwrapForPipelining[Lease[B], Any] = (
            UnwrapForPipelining(lease.future, lease.lease.take())
        )
    else:
        taken_lease = lease.take()
    taken_request = _copy_request(request, take=True)

    async def work() -> IRValue | UnwrapForPipelining[IRValue, Any]:
        # Cleanup-on-failure: if either pre-`_run_request_async` step
        # raises, we still own `taken_lease` and any unconsumed leases
        # in `taken_request` — `_run_request_async` would normally take
        # ownership but never gets called. Discard explicitly so the
        # autouse leak fixture stays green and rejection paths don't
        # spuriously leak. Also fixes a pre-existing leak on
        # `_unwrap_request_futures` failure (e.g. a `WaitAndIgnoreFutureThen`
        # whose `wait_for` future rejects).
        try:
            unwrapped_request = await self._unwrap_request_futures(taken_request)
            concrete_lease = await _resolve_pipelined_lease(taken_lease)
        except BaseException:
            if isinstance(taken_lease, UnwrapForPipelining):
                taken_lease.lease.discard()
            else:
                taken_lease.discard()
            # `taken_request` is the pre-unwrap structure; some leases may
            # have been consumed by partial `_unwrap_request_futures`
            # progress. `discard_leases_in` is idempotent on
            # already-discarded leases, so a partial walk is fine.
            discard_leases_in(taken_request)
            raise
        return await self._run_request_async(concrete_lease, unwrapped_request)

    return spawn_ir_future_in_task_group(
        name=f"{type(self).__name__}.dispatch",
        work=work,
        task_group=self._task_group,
    )
```

**Pre-existing leak fix.** The try/except above also closes a pre-existing
leak: today, if `_unwrap_request_futures` raises (e.g. a
`WaitAndIgnoreFutureThen.wait_for` future rejects), the leases inside
`taken_request` that the unwrapper hadn't yet consumed leak out via the
weakref-finalizer warning path. With substage 4's autouse leak fixture, that
would surface as a CI failure on any test that exercises rejection during
unwrap. The unified cleanup runs in both pre-`_run_request_async` failure
modes (lease unwrap *and* request unwrap), so existing tests stay green and
new rejection-path tests don't spuriously leak. Worth a mini-section in the
"deviations from the substage plan" log when this lands.

`_resolve_pipelined_lease` is a small module-private helper:

```python
async def _resolve_pipelined_lease(
    lease: Lease[B] | UnwrapForPipelining[Lease[B], Any],
) -> Lease[B]:
    if not isinstance(lease, UnwrapForPipelining):
        return lease
    resolved = await lease.future.wait(lease.lease)
    if not isinstance(resolved, Lease):
        # Resolved value isn't a Lease; it's a deep copy we own and may
        # contain leases buried inside (handles, nested leases) — release
        # them before raising.
        discard_leases_in(resolved)
        raise IRException(
            message=(
                "IRHandle.dispatch: outer lease UnwrapForPipelining resolved to "
                f"{type(resolved).__name__}, not Lease"
            ),
            category="PipelineTypeError",
        )
    return cast(Lease[B], resolved)
```

The helper lives next to `_unwrap_request_futures` in
`leased_cap_common.py` and is only called from the `dispatch` runner.

**Ordering note.** The existing `work()` does request-future unwrapping
*before* the user's call. The lease-future unwrap fits naturally **after**
the request unwrap and **before** `_run_request_async`. Order of those
two awaits doesn't matter for correctness (both are independent async
ops on distinct futures); the chosen order matches the existing pattern
of "resolve all pipelined inputs, then call user code".

**Failure paths.**

- The `lease.lease.take()` call at entry can raise (already-discarded
  lease). In that case the request leases haven't been consumed yet —
  re-raise lets the caller's reference still own them; the existing
  `IRHandleLocalImplBase.dispatch` body has the same precondition and
  shape today (it `take()`s the lease before `_copy_request`). To match
  symmetry with the inner unwrap, take order is: outer-lease (take or
  build pipelined wrapper) → request copy. If the request copy raises,
  we own the lease (or pipelined wrapper) and need to discard. Mirrors
  what `pipeline_dispatch` does today; lift that error-rollback shape
  into `dispatch`.
  - Concretely: wrap the take-and-copy in a try/except; on except,
    discard either `taken_lease` (concrete) or `taken_lease.lease`
    (pipelined wrapper), then re-raise.
- Inside `work`, an exception from `_resolve_pipelined_lease` (lease
  future rejected, or resolved value type-mismatch) propagates out of
  `work` as an `IRException` — `spawn_ir_future_in_task_group`'s
  outcome routing already converts that into
  `box.set_exception(...)`, so the caller's dispatched future
  rejects with the appropriate `IRException`. No special handling.
- `_run_request_async`'s own ownership contract is unchanged:
  `concrete_lease` is the value handed in; the impl must
  discard / forward / embed it exactly as before.

### `request`

`request` is implemented as `dispatch + wait + discard` and just
inherits the new wider signature; no code change beyond the type
annotation. (Cancellation of the wait still doesn't cancel the
underlying dispatch; the runner continues until `_run_request_async`
returns.)

### `_run_request_async` (subclass contract)

Stays narrow: `lease: Lease[B]`. Subclasses don't need to know about
pipelining — that's resolved by the runner before they're called. The
docstring gets a sentence pointing this out.

### Test plumbing in `tests/test_leases/test_leased_cap_common.py`

Existing `_TestImpl` fixtures take `Lease[B]` directly; no change.

---

## RPC outbound: `RemoteIRHandle.dispatch` + devaluator

`fxtr2/rpc/lowering_remote_proxies.py` + `fxtr2/rpc/lowering_devaluate.py`.

### `RemoteIRHandle.dispatch`

Three changes:

1. Type annotation widens to
   `lease: Lease[B] | UnwrapForPipelining[Lease[B], Any]`.
2. The "take ownership up front" block mirrors the local impl: detect
   `UnwrapForPipelining`, take its inner lease into a fresh wrapper,
   else take the concrete lease. The existing
   `pipeline_dispatch`'s inner-handle-lease take in the same file is
   the template.
3. Pass the (possibly-wrapped) lease to
   `devaluate_handle_request(handle_lease=...)`. The devaluator's
   `_build_handle_request_expr` already accepts
   `Lease[Any] | UnwrapForPipelining[Any, Any]` for its `handle_lease`
   parameter (it's reused by `devaluate_pipeline_dispatch`); the only
   change is widening the public `devaluate_handle_request` signature
   to advertise this.

The error-recovery branch (devaluator threw) needs a small refactor:
today it unconditionally calls `handle_lease.discard()`. With the
pipelined form we need to discard `lease.lease` instead (the inner
`Lease`), matching the take pattern. Concretely:

```python
except BaseException:
    if isinstance(handle_lease, UnwrapForPipelining):
        handle_lease.lease.discard()
    else:
        handle_lease.discard()
    _discard_request_leases(unwrapped_request)
    for unwrap in wait_for:
        unwrap.lease.discard()
    raise
```

### `devaluate_handle_request`

Widen signature:

```python
def devaluate_handle_request(
    conn: "LeasedCapRPCConnection",
    *,
    handle_import_id: int,
    handle_lease: Lease[Any] | UnwrapForPipelining[Any, Any],
    request: Call[IRCallArg] | GetItem,
    wait_for: Sequence[UnwrapForPipelining[Any, Any]] = (),
) -> tuple[Expression[Any], list[Lease[Any]]]:
```

The body change is one line: pass `handle_lease` straight through to
`_build_handle_request_expr`. `_devaluate_walk` already turns
`UnwrapForPipelining` into `AwaitFutureExpr(future=…, lease=…)` (see
`lowering_devaluate.py` `_devaluate_walk` `if isinstance(value,
UnwrapForPipelining)`), with the bookkeeping we need:
`_bump_await_future_obligation` is invoked when the inner future is
one of our exports (so the resolve-obligation accounting on outbound
`AwaitFutureExpr` is preserved verbatim).

### `_run_request_async`/devaluator obligation symmetry

No new obligation work. The outbound `AwaitFutureExpr` we emit for the
lease slot:

- If the inner `future` is a *bounce-back* (`ImportedIRFutureExpr`),
  it's a reference to one of our future imports. Today
  `pipeline_dispatch`'s `inner_handle_lease` UnwrapForPipelining
  exercises the same path; no change.
- If the inner `future` is one of our future exports (the converter we
  just dispatched and whose result we're feeding back through the
  same connection — the substage-5 CapIRArena pattern), the
  devaluator's `_bump_await_future_obligation` increments
  `FutureExportEntry.resolve_obligation_count` for that export, and
  the outbound write of `ResolveMessage` later zeroes it. Already
  works.

In short, the outbound bookkeeping is *literally* the same as
`pipeline_dispatch`'s inner-handle-lease today; we're just exposing a
new caller for it.

---

## RPC inbound: phase-2 evaluator

`fxtr2/rpc/lowering_evaluate.py`.

The current `_dispatch_handle_request` eagerly awaits the lease via
`_eval_lease_subexpr`, then calls `handle.dispatch(concrete_lease,
request)`. We want the lease slot to be permitted to evaluate to
`UnwrapForPipelining[Lease, …]` so the dispatch returns immediately
(the spawned task on the receiving side is what does the await), and
the request runs as soon as both lease *and* request are resolved.

### New helper: `_eval_lease_or_pipelined`

Sibling of `_eval_lease_subexpr`, but exclusively for the
`HandleRequestExpr.lease` / `HandleDispatchExpr.lease` slot:

```python
async def _eval_lease_or_pipelined(
    conn: "LeasedCapRPCConnection",
    expr: Expression[Any],
    *,
    context: str,
) -> Lease[Any] | UnwrapForPipelining[Lease[Any], Any]:
    """Evaluate the dispatch-lease slot, returning either a concrete Lease
    (the common case — Imported/ExportedLeaseExpr resolves through
    phase-1 to a ConstantExpr) or an UnwrapForPipelining that
    handle.dispatch will resolve in its spawned task.

    Mirrors :func:`eval_as_call_arg` for the shapes that produce
    :class:`UnwrapForPipelining`: ``AwaitFutureExpr`` and
    ``HandleRequestExpr``. (``HandleDispatchExpr`` evaluates to a
    ``[future, lease]`` list, not a ``Lease``, so it has no pipelined
    form here.) Any other expression resolving to a Lease falls back
    to the eager :func:`_eval_lease_subexpr` path.
    """
    if isinstance(expr, AwaitFutureExpr):
        future, lease = await _eval_await_fut_and_lease(conn, expr)
        return UnwrapForPipelining(future=future, lease=lease)
    if isinstance(expr, HandleRequestExpr):
        future, result_lease = await _dispatch_handle_request(conn, expr)
        return UnwrapForPipelining(future=future, lease=result_lease)
    return await _eval_lease_subexpr(conn, expr, context=context)
```

The `HandleRequestExpr` branch matches the symmetric branch in
:func:`eval_as_call_arg` (lines ~624–626 of `lowering_evaluate.py`):
inbound `HandleRequestExpr` recursively dispatches and the resulting
`(future, result_lease)` becomes an `UnwrapForPipelining`. This means a
peer that nests one inbound dispatch's result-lease into another's
lease slot pipelines through without an intermediate await on our side
— same as nesting it as a call arg today.

`_eval_lease_subexpr` itself stays narrow (`-> Lease`); other callers
(`_eval_await_fut_and_lease`, anything reading `AwaitFutureExpr.lease`)
must continue producing concrete leases.

### `_dispatch_handle_request`

```python
async def _dispatch_handle_request(
    conn: "LeasedCapRPCConnection",
    expr: HandleRequestExpr | HandleDispatchExpr,
) -> tuple[IRFuture[Any, Any], Lease[Any]]:
    handle = await _eval_handle_subexpr(conn, expr.handle, context=...)
    lease_or_pipelined = await _eval_lease_or_pipelined(
        conn, expr.lease, context=f"{type(expr).__name__}.lease"
    )
    try:
        request = await _eval_request(conn, expr.request)
        try:
            await _eval_wait_for(conn, expr.wait_for)
        except BaseException:
            discard_leases_in(request)
            raise
    except BaseException:
        if isinstance(lease_or_pipelined, UnwrapForPipelining):
            lease_or_pipelined.lease.discard()
        else:
            lease_or_pipelined.discard()
        raise
    # handle.dispatch consumes lease_or_pipelined per the standard
    # IRHandle.dispatch transfer convention (now widened to accept
    # UnwrapForPipelining[Lease, …]).
    return handle.dispatch(lease_or_pipelined, request)
```

The `with await _eval_lease_subexpr(...) as lease:` `with`-block
shape today auto-discards the lease on raise. Switching to
`UnwrapForPipelining`-or-`Lease` precludes the `with` shorthand, so we
expand to an explicit try/except. (We could add `__exit__` semantics
to `UnwrapForPipelining`, but that's a wider API change for a small
gain — the explicit try/except is fine.)

### Forwardable / call-arg modes

`eval_as_call_arg` and `eval_forwardable` both call
`_dispatch_handle_request` for `HandleRequestExpr` / `HandleDispatchExpr`
nodes. The change is transparent to them — `_dispatch_handle_request`
now happens to pass a possibly-pipelined lease into the local impl,
but its return shape (`tuple[IRFuture, Lease]`) is unchanged.

### Invariants that stay the same

- Phase-1 substitution still recurses into `HandleRequestExpr.lease`
  (`_phase1_walk`'s existing branch, line ~230). An incoming
  `AwaitFutureExpr` whose `future` slot references one of our
  imports goes through `_phase1_bump_await_future_obligation`, which
  bumps `resolve_obligation_refcount` / `local_consumer_count` on
  the target `FutureImportEntry` exactly as for an
  `AwaitFutureExpr` in any other position. The phase-2 evaluator
  doesn't need to know about this bump.
- Phase-2 spawn `finally` block in `_on_push` / `_on_resolve`
  decrements the local-consumer counts collected during phase-1.
  No change.
- `eval_as_value`'s existing `AwaitFutureExpr` path (eager wait) still
  applies wherever a concrete value is required — only
  `_eval_lease_or_pipelined` adds the pipelined bypass.

### Cleanup hazard worth noting

If `_eval_lease_or_pipelined` returns `UnwrapForPipelining(future,
lease)` and `_eval_request` then raises, we need to discard *both*
slots of the pipelined wrapper (the inner concrete lease, and
implicitly the future). The future itself owns no lease that we'd
need to release here — the lease *for* the future is the
`UnwrapForPipelining.lease` slot, which we do discard. So the
explicit try/except as written is sufficient; no additional cleanup
helpers.

If a downstream `handle.dispatch` raises synchronously (it shouldn't —
it's a sync-take-then-spawn), the same discard logic applies; we
re-raise into the phase-2 spawn machinery which then routes the error
into the appropriate future's rejection.

---

## Wire protocol & codec

**No changes.** Confirmed inspection points:

- `lowering_types.py`: `HandleRequestExpr.lease: Expression[Lease[Any]]`
  already permits any `Expression` evaluating to a `Lease`.
  `AwaitFutureExpr[T]` is a generic `Expression[T]`, so
  `AwaitFutureExpr[Lease[Any]]` is well-typed in the slot.
- `json_serdes.py`: `_decode_expr_request` calls `decode_expression(value
  [2])` on the lease field, which dispatches to `_decode_expr_await`
  for `["await", future, lease]` — already round-trips. No
  schema bump, no codec change.

The protocol-version pin (`HelloMessage` carries version 1) is
unaffected.

---

## Tests

### Local: `tests/test_leases/test_leased_cap_common.py`

Add a new test class `TestDispatchPipelinedLease` with:

1. **Happy path: concrete Lease forms produced by an upstream future
   are passed straight in via `UnwrapForPipelining`.** Construct an
   `IRHandleLocalImplBase` subclass that records its received lease's
   subject token; build a second handle whose dispatch returns a
   fresh lease (via `set_result(some_lease)`); pipeline the outer
   handle's dispatch on that result. Assert the second handle's
   `_run_request_async` received the lease produced by the first
   handle's dispatch, and that the dispatched future resolves
   without exception. Variants:
   - Outer lease is a future-of-lease whose lease is the *first*
     handle's result lifetime (so the unwrap and the dispatch are
     on different lifetimes — the realistic CapIRArena shape).
2. **Type-mismatch path.** First handle's dispatch resolves to a
   non-Lease (e.g. `42`); pipeline-dispatch the second handle on it.
   Verify the second handle's future rejects with
   `IRException(category="PipelineTypeError")`. The first handle's
   future and lease are still discarded cleanly.
3. **Future-rejection path.** First handle's dispatch raises
   `IRException(category="UpstreamRejected")`. Pipeline the second
   handle's outer lease on it. Verify the second handle's future
   rejects with the same `IRException` category propagated through
   `_resolve_pipelined_lease` → `box.set_exception`. The second
   handle's `_run_request_async` is *not* called.
4. **Concrete-lease form still works.** A direct
   `dispatch(concrete_lease, request)` call (no pipelining) goes
   through unchanged. Single regression test as a smoke check.
5. **Cancellation / lease break of the outer lease's source.** Build
   a future-of-lease, pipeline the dispatch, then revoke the
   future's lifetime before resolution. Verify the dispatched
   future's wait raises `LeaseBrokenError` (or gets converted to
   `IRException` per the
   `spawn_ir_future_in_task_group`-non-`Exception` rules — confirm
   which is correct for `LeaseBrokenError`, since the existing
   `pipeline_dispatch` test for inner-handle-lease covers this exact
   case in `test_pipeline_dispatch_rejects_mismatched_grant` and
   adjacent suites).

Each test asserts post-test that all leases are discarded and tokens
ended (via the existing autouse `_assert_no_lease_leaks` fixture).

### Transport: `tests/test_rpc/test_lowering.py`

Add `TestDispatchOuterLeasePipelining`:

1. **Outbound pipeline + inbound pipeline-through.** Set up an
   in-process connection. Server side mounts a handle that records
   its received-lease's grant. Client side dispatches a "make a
   lease" call on the server's main interface (returns a lease as
   the IRValue result) → producing
   `(future_of_lease, future_lease)`; client then calls
   `RemoteIRHandle.dispatch(UnwrapForPipelining(future_of_lease,
   future_lease), Call("noop", None))` on a second remote handle
   exposed by the server. Verify:
   - The second push goes out *before* the first resolves (capture
     `PushMessage`s on a probe stream; assert ordering by message
     index, not by wall clock).
   - The server's recorded grant matches the lease the first
     dispatch produced. (We can use the connection's regrant
     equality — both leases share an export-side grant.)
   - The dispatched future resolves successfully on the client.
   - After `await rpc_conn.aclose()` both sides drain to empty
     tables, confirming no orphaned `AwaitFutureExpr` obligations.
2. **Bounce-back outer lease.** The pipelined lease's inner future is
   *the client's own* future export (i.e. a future the server
   handed back). The outbound devaluator emits an
   `AwaitFutureExpr` referencing one of *our* exports → bumps
   `FutureExportEntry.resolve_obligation_count`. Assert the count
   bumps and is later zeroed by the resolve. (Coverage for the
   resolve-obligation-bookkeeping path that's exercised by
   `pipeline_dispatch`'s inner-handle-lease today, but now via the
   outer-lease widening.)
3. **Forward direction (lease future on remote side).**
   Server dispatches a "give me a lease" call against an arena-style
   handle the client mounts; client returns a lease. Server then
   dispatches a second call back at the client with the
   pipelined lease. Same drain checks.
4. **Lease-future rejection across the wire.** Configure the
   "produce a lease" handler on the server to throw an
   `IRException`. Verify the pipelined dispatch on the client
   surfaces the IRException through the dispatched future's
   `wait()`. Drain assertion.
5. **Resolve-obligation accounting of return leases.** Verify
   that the *return* lease (from the second dispatch) is
   minted synchronously on the dispatcher side and is **not**
   pipelined — i.e. the result lease isn't an `AwaitFutureExpr`
   on the wire. (Test against captured outbound messages.)

### Devaluator unit-test

In `tests/test_rpc/test_devaluate_phase1.py` (or sibling), one small
test that `devaluate_handle_request(handle_lease=UnwrapForPipelining
(...))` produces `HandleRequestExpr.lease == AwaitFutureExpr(...)`.
This documents the encoding without going through the connection.

---

## Files touched

- `fxtr2/leases/leased_cap_ir.py` — widen `IRHandle.dispatch` /
  `IRHandle.request` signatures + docstrings; widen
  `UnwrapForPipelining` docstring.
- `fxtr2/leases/leased_cap_common.py` — widen `IRHandleLocalImplBase.
  dispatch` / `request` to accept `UnwrapForPipelining`, add
  `_resolve_pipelined_lease` helper, refactor the take-and-copy
  failure path to discard the right slot.
- `fxtr2/rpc/lowering_remote_proxies.py` — `RemoteIRHandle.dispatch`
  / `request` accept the wider type, take ownership of the inner
  lease for the pipelined form, error-rollback discards the right
  slot.
- `fxtr2/rpc/lowering_devaluate.py` — widen
  `devaluate_handle_request.handle_lease` parameter type. (Body
  unchanged: `_build_handle_request_expr` already accepts
  `Lease | UnwrapForPipelining`.)
- `fxtr2/rpc/lowering_evaluate.py` — add
  `_eval_lease_or_pipelined`, switch `_dispatch_handle_request` to
  use it, expand the cleanup `with`-block into try/except that
  discards either slot of the pipelined wrapper on error.
- `fxtr2/leases/LOCAL_CODE_OVERVIEW.md` — note the widened
  `IRHandle.dispatch` lease arg in the `IRHandleLocalImplBase`
  section.
- `fxtr2/rpc/LOCAL_CODE_OVERVIEW.md` — note that the inbound
  evaluator pipelines through a pipelined-lease push without
  awaiting it, in the "Push / pull / resolve" subsection.
- Tests: see "Tests" above.

---

## Open questions / risks

- **Type-narrowing precision on `UnwrapForPipelining[Lease[B], …]`.**
  The first type parameter of `UnwrapForPipelining` is the resolved
  *value* type. Today `pipeline_dispatch.inner_handle_lease` is
  typed `Lease[Any] | UnwrapForPipelining[Lease[Any], Any]`, with
  `Lease[Any]` for the inner-T. Substage 4 mirrors that. We could
  tighten to `UnwrapForPipelining[Lease[B], Any]` (matching the
  outer-handle's brand `B`), but the future's brand is independent
  of the lease's, and pyright doesn't easily narrow nested generics
  through `isinstance(... , UnwrapForPipelining)`. Stick with
  `Lease[Any]` for the inner, matching the existing convention; if
  it becomes a problem, a follow-up can revisit.
- **Resolve-obligation symmetry.** Inbound: phase-1 already bumps
  `resolve_obligation_refcount` / `local_consumer_count` for an
  `AwaitFutureExpr` whose `future` references one of our
  exports — regardless of *where* in the expression tree it
  appears. So an `AwaitFutureExpr` in the lease slot of a
  `HandleRequestExpr` is not a special case for the bump-and-release
  side. The phase-2 spawn's `finally` block in
  `_on_push` releases the bumps. Outbound: the devaluator's
  `_bump_await_future_obligation` is symmetric. Cross-check via
  the transport tests' drain assertions (every test ends with both
  sides' `_active_resolve_obligation_*` sets empty).
- **Subclasses that override `dispatch` rather than implementing
  `_run_request_async`.** Grep `IRHandleLocalImplBase.dispatch` —
  the local impl is a concrete `dispatch` and subclasses are
  expected to only override `_run_request_async`. Confirm no
  subclasses in-tree override `dispatch` itself; if they exist,
  they need the same widening. (Fast check: `grep -rn "def
  dispatch" v2/fxtr2/`.)

---

## Checklist

- [x] Widen `IRHandle.dispatch` / `IRHandle.request` signatures +
      docstrings in `fxtr2/leases/leased_cap_ir.py`.
- [x] Widen `UnwrapForPipelining` docstring with the new use site.
- [x] Implement `_resolve_pipelined_lease` helper +
      widen `IRHandleLocalImplBase.dispatch` / `request` in
      `fxtr2/leases/leased_cap_common.py`. Update the take/copy
      failure rollback to handle either slot.
- [x] Confirm no in-tree `IRHandleLocalImplBase` subclass overrides
      `dispatch` / `request` (`grep -rn "def dispatch" v2/fxtr2/`,
      `grep -rn "def request" v2/fxtr2/`). *Result: only one
      override outside the base — `NullMainInterface` (subclass of
      `IRHandle` directly, not of `IRHandleLocalImplBase`). Widened
      its signatures alongside the base; pipelined-form path
      discards `lease.lease` instead of the wrapper.*
- [x] Widen `RemoteIRHandle.dispatch` / `request` in
      `lowering_remote_proxies.py`; matching error-rollback discard.
- [x] Widen `devaluate_handle_request.handle_lease` parameter type
      in `lowering_devaluate.py`.
- [x] Add `_eval_lease_or_pipelined` + switch
      `_dispatch_handle_request` to use it in
      `lowering_evaluate.py`. Replace the `with`-block with an
      explicit try/except that discards either slot.
- [x] Update `LOCAL_CODE_OVERVIEW.md` for `fxtr2/leases/` and
      `fxtr2/rpc/`.
- [x] Update root `CODE_OVERVIEW.md` if the pipelined-lease shape
      is mentioned at the IR level. *No update needed: root doc
      stays at the file/module level and doesn't enumerate
      `IRHandle` method signatures.*
- [x] Local tests: `TestDispatchPipelinedLease` in
      `tests/test_leases/test_leased_cap_common.py`. 6 cases:
      concrete-lease regression, pipelined happy path, type
      mismatch, rejection propagation, lease-source revoke
      (manual `LocalPendingResultBox` construction so the source
      lifetime is externally revokable), and the pre-existing
      `_unwrap_request_futures`-failure leak fix.
- [x] Transport tests: `TestDispatchPipelinedOuterLease` in
      `tests/test_rpc/test_lowering.py`. 3 cases: outbound
      pipelined-lease wire shape, lease-future rejection
      propagation, inbound pipelined-lease round-trip (symmetric
      A↔B). Bounce-back / return-lease / ordering tests rolled
      into the wire-shape test (it asserts both pushes are emitted
      from B before A's resolves arrive).
- [x] Devaluator unit test:
      `TestDevaluateHandleRequestPipelinedLease` in
      `tests/test_rpc/test_devaluate_phase1.py` — confirms
      `devaluate_handle_request` with an
      `UnwrapForPipelining(future, lease)` `handle_lease` produces
      `HandleRequestExpr.lease = AwaitFutureExpr(...)`.
- [x] Full suite green (1761 passed).

## Deviations from the substage plan

- **`NullMainInterface` widening.** The plan flagged that any in-tree
  override of `dispatch` / `request` would need the same widening, and
  this turned out to apply to `fxtr2/rpc/lowering_null_main.py` (a
  subclass of `IRHandle` directly, used as the placeholder main on
  client-only connections). Picked up in this substage: the discard
  path now distinguishes the two forms.

- **Pre-existing leak in `_unwrap_request_futures` failure path.**
  `IRHandleLocalImplBase.dispatch`'s spawned runner already had a
  latent leak: if `_unwrap_request_futures` raised (e.g. an inner
  `WaitAndIgnoreFutureThen.wait_for` future rejected), the leases
  inside the `taken_request` that the unwrapper hadn't yet consumed
  leaked out via the weakref-finalizer warning path. With substage
  4's autouse leak fixture, that would surface as CI failures on
  any rejection-path test. Fix landed alongside the substage-4
  pipelined-lease unwrap in the same try/except: both pre-
  `_run_request_async` failure modes now discard the runner-owned
  inputs symmetrically. Added a regression test
  (`test_unwrap_request_futures_failure_cleans_up`).

- **Local "lease-source revoke" test: manual-construction pattern.**
  The plan called for a test that revokes the future-of-lease's
  lifetime mid-resolve and asserts the dispatched future rejects with
  `LeaseBrokenError → IRException`. The obvious helper for "make a
  future-of-lease" — `spawn_ir_future_in_task_group` — mints its own
  internal ULL for each spawned future, and we don't have a clean
  handle on that ULL to revoke from outside. Instead, the test
  constructs the future-of-lease manually
  (`LocalPendingResultBox` + `IRFutureLocalImpl` against a ULL we
  control), dispatches with that as the outer-lease wrapper,
  yields once so the runner gets to start awaiting, then
  `src_lt.revoke()`. The dispatched future rejects with
  `IRException(category="LeaseBrokenError")` and
  `_run_request_async` is never called. Worth noting because: this
  pattern is also the cleanest way to write "future-of-X-where-we-
  control-X's-lifetime" tests for any future-of-lease scenario,
  and we may want to factor it into a small test helper if more
  substages need it.

- **JSON codec: confirmed no change needed.** The substage's
  "no schema bump, no codec bump" claim was double-checked against
  `json_serdes.py`: `_decode_expr_request` already uses
  `decode_expression(value[2])` for the lease slot, which dispatches
  to `_decode_expr_await` for `["await", future, lease]`. Round-
  trips without modification.

- **Tests for the inbound `HandleRequestExpr`-in-lease-slot path
  not yet exercised.** `_eval_lease_or_pipelined` mirrors
  `eval_as_call_arg` and pipelines through `HandleRequestExpr`
  (in addition to `AwaitFutureExpr`), but no test today emits a
  `HandleRequestExpr` in the lease slot of another
  `HandleRequestExpr` — substage 5's `CapIRArena` only emits the
  `AwaitFutureExpr` form. The branch is exercised by
  `eval_as_call_arg`'s existing tests; we'll get end-to-end
  coverage if a substage-5 / substage-7 pattern requires it.
