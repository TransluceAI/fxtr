# Lease system Local Code Overview

Detailed documentation for the lease system. For a high-level overview, see the root
`CODE_OVERVIEW.md`.

## What this package provides

The lease system gives fxtr a single primitive for expressing **resource lifetimes** across both
local code and remote (RPC) boundaries. A resource owner hands out **leases**, which are clonable,
discardable rights to use a resource for a bounded period of time. The owner retains the ability to
**revoke** the right (breaking any outstanding leases) if the resource has to go away early.
Because the abstraction is transport-independent, the same lease types flow through local Python
code, through the cap-IR layer in `fxtr2/leases/leased_cap_ir.py`, and across an RPC connection in
`fxtr2/rpc/`.

The design is heavily inspired by structured concurrency and by the conditional-grant pattern
sketched in the project's design docs. Its goals:

- **Safe cleanup.** An owner is guaranteed that as long as any lease is outstanding, the resource
    remains valid; once all leases are discarded (or the owner explicitly revokes), cleanup is
    safe.
- **Holder autonomy.** A holder can discard its lease whenever it wants, without needing the owner
    to participate, and gains a local error if it accidentally uses a broken lease.
- **Cross-boundary reasoning.** The same shape works for resources on the other end of a network:
    the RPC layer re-grants incoming leases with its own connection-scoped tokens.
- **Derived-lifetime composition.** `DerivedLocalLifetime` lets you produce derived leases whose
    lifetime ends whenever *any* of a set of input lifetimes ends, which in turn enables robust
    promise pipelining and mutual-dependency patterns between two parties without requiring an
    unwinding handshake. Callers compose higher-level "key-gated" shapes by including a private
    `UnconditionalLocalLifetime` of their own in the input set.

## File overview

| File | Purpose |
|---|---|
| `lease_types.py` | Abstract `LifetimeToken`, `Grant`, `Lease` ABCs; exception types (`LeaseBrokenError`, `LeaseDiscardedError`, `LeaseTransferError`); phantom `FreshLifetime` branding machinery. |
| `local_lease.py` | Concrete local lifetime primitives: `LocalLifetimeToken`, `LocalGrant`, `LocalLifetimeLease`, `UnconditionalLocalLifetime`, `LocalDerivedTokenLease`, `_DerivedOutputGrant`, `DerivedLocalLifetime`. Plus the internal `_LeaseOwner` protocol that lease tracking is built on, and the module-level `_LEASE_LEAK_COUNT` counter (readable via `get_lease_leak_count()`) that the test-suite's autouse `_assert_no_lease_leaks` fixture uses to detect GC-without-discard leaks. Also exposes the `STATIC_LIFETIME` singleton (never-ending token + process-wide static grant) with `lease()` returning lightweight, never-breaking lease handles for data whose lifetime is gated entirely by Python object ownership (e.g. `TransferEntitySource`). |
| `lease_util.py` | `lease_cancel_scope` — structured-concurrency helper that cancels tasks when a lease breaks mid-context. |
| `leased_cap_ir.py` | Cap-IR type definitions: `IRValue`, `IRTagged`, `IRException`, `IRHandle`, `IRFuture`, `UnwrapForPipelining`, and the request types. |
| `leased_cap_common.py` | Local Cap-IR implementation: `copy_ir_value`, `discard_leases_in`, `wrap_exception`, `PendingResultBox` / `LocalPendingResultBox`, `IRFutureLocalImpl`, `spawn_ir_future_in_task_group`, `IRHandleLocalImplBase` (concrete `request` / `dispatch`), plus private helpers (`_copy_request`, `_descend_and_copy`). |
| `lease_bridges.py` | Cap-bridge taxonomy: four dataclasses on the `Local`/`Pipelined` × `Independent`/`Dependent` axes (`IndependentLocalBridge`, `IndependentPipelinedBridge`, `DependentLocalBridge`, `DependentPipelinedBridge`) plus the `LocalBridge` / `LocalOrPipelinedBridge` unions and the two converter callable types. Each bridge has `clone()` / `discard_at_rest()` methods. |

Tests live in `tests/test_leases/`. For design context on the conditional-grant primitive (`DerivedLocalLifetime` — an arbitrary symmetric input set), see `plans/phase3/stage8c_user_caps/part1_plan.md`.

## Core concepts

### Tokens, grants, and leases

| Concept | Role |
|---|---|
| `LifetimeToken[B]` | An abstract *name* for a lifetime. Exposes `ended()` and `register_end_callback()`. Does not itself grant access. |
| `Grant[B]` | An opaque identity for an equivalence class of leases. Hashable; two leases are clones of each other iff their grants compare equal. A `Grant` itself carries no authority: holding one does **not** let you mint new leases or revoke anything. The authority lives on the lifetime owner that issued the grant (e.g. `UnconditionalLocalLifetime`). |
| `Lease[B]` | A clonable, discardable holder-side right. Tagged with a `Grant` (for equality). Supports `clone`, `take`, `discard`, `ensure_active`, `is_active`, `register_broken_callback`, and a context-manager form that discards on exit. |

**Lease transfer convention.** Passing a lease as an argument is considered a *transfer*: the
callee takes ownership and the caller must not use the lease after. The typical pattern for
callee-side code is `with lease.take() as lease:` — this replaces the lease with a fresh clone the
caller cannot touch, and guarantees it is discarded on context exit. Callers that want to retain
their copy should pass `lease.clone()` instead.

**Revoke is synchronous.** For local lifetimes, revoking the owner of a grant synchronously breaks
all outstanding leases and fires their broken-callbacks before `revoke()` returns. Similarly,
closing a token fires end-callbacks synchronously. Registering a callback on an already-closed
token / already-broken lease invokes it immediately. This makes local reasoning straightforward:
"by the time `revoke()` returns, everyone has been told."

Over an RPC connection there can be a propagation delay: revoking a grant on one side triggers a
`BreakMessage`, and the other side synchronously re-breaks its local re-grant at the moment it
*receives* that message. Any in-flight operations gated by the lease may still complete or fail
with an `IRException`.

### `FreshLifetime[Literal[...]]` branding

The lifetime type parameters (`B: LifetimeBrand`, `RB: FreshLifetime[...]`) are phantom: they exist
purely to let the typechecker distinguish one lifetime from another. They carry no runtime content.
Usage:

- **Accepting** a lease of unknown lifetime: parameterize the function on a generic bound
    `B: LifetimeBrand` and take `Lease[B]`.
- **Producing** a fresh lifetime: parameterize the return type on a caller-chosen output brand
    `RS: FreshLifetimeSymbol`, and return `Lease[FreshLifetime[RS]]`.
- **At the call site** where a fresh lifetime is introduced and does *not* escape: annotate the
    result with `FreshLifetime[Literal["some_unique_name"]]`, giving it a distinct identity so it
    can't be confused with other lifetimes in that scope.

By convention, output lifetime parameters appear before input lifetime parameters in the generic
parameter list. See the module-level docstring in `lease_types.py` for a worked example.

### `UnconditionalLocalLifetime`

Owns exactly one `LocalLifetimeToken`, one `LocalGrant`, and one eagerly-constructed root lease.
`root_lease` is a **stable reference**: it returns the same lease object on every access. Clone
or `take` the returned lease to get additional copies. The lifetime closes automatically as soon
as the last live lease (the root and all its clones) is discarded — end-callbacks fire
synchronously at that point, giving subscribers a clear reclaim signal. Alternatively,
`revoke(reason)` breaks all live leases synchronously and closes the token. `revoke` is
idempotent and is a no-op on an already-closed lifetime.

Use `UnconditionalLocalLifetime` when you want a vanilla "resource exists from now until I'm done
with it" lifetime — for example, the lifetime of a task, of a handle, or of the result of a
dispatch call. Typical usage: construct, optionally hand out clones of `root_lease`, then
`root_lease.discard()` (or let your last clone go) to let the lifetime close cleanly — or call
`revoke(reason)` for an error path.

**Scoped-usage context managers.** Two patterns are provided:

- `with UnconditionalLocalLifetime(name).revoke_after_block() as lt:` (sync) — revokes the
    lifetime synchronously on block exit. Use when the lifetime is self-contained and nothing
    is expected to outlive the block. Tests typically use this form.
- `async with UnconditionalLocalLifetime(name) as lt:` (async, via
    `anyio.AsyncContextManagerMixin`) — on normal exit calls `await self.aclose()` which
    discards the root lease and waits for the token to drain; on any `BaseException`
    (including cancellation during the drain wait) revokes the lifetime instead for fast close.
    Use when you're handing out leases and want holders to drain before the scope unwinds.

**`await lt.aclose()`** — same drain semantics as the async CM, exposed as a standalone
async method so callers can compose drains explicitly (see the Derived composition recipe
below). Idempotent on an already-ended lifetime; revokes on cancellation.

### `DerivedLocalLifetime`

Ties a new lifetime to a *set* of input lifetimes, all treated symmetrically. The output
lifetime is valid only while *every* input is still alive — so `DerivedLocalLifetime` is
the primitive for intersecting and coordinating multiple independent lifetimes into a
single downstream handle. All input tokens contribute equally; there is no privileged
"key" or "requirement" split in the primitive itself.

**Key-gating, as a pattern on top.** Sometimes a caller wants the *ability* to cleanly
shut down the output lifetime on demand — independent of whether the real upstream
lifetimes are ready to end. The idiom for that is to create a private
`UnconditionalLocalLifetime` ("the key") whose only purpose is to appear in the input
set and be retained by the caller. Because output drain waits for every input, the
caller can trigger drain simply by dropping their hold on the key's root lease; the
Derived's output token ends as soon as the key ends (via the "any input ends → output
ends" cascade). This is the building block for both (a) structured cleanup — a
try/finally that tears down a derived capability before its real upstream resources —
and (b) cross-boundary coordination, e.g. "party A retains a key lease on a capability
it exposes to party B; A drops the key to rescind access without having to wait for B
to volunteer." See the "Key-gated conditional-grant" recipe in "Common patterns" below.

Callers who don't need this opt-out simply don't add a key to the input set. The
primitive itself stays purely symmetric.

Type parameter: a single output brand `RB_Out`. Input tokens are kept as
`LifetimeToken[Any]` since they can vary in both number and brand.

- `__init__(name, inputs: Collection[LifetimeToken[Any]])` — snapshots `inputs` into a
    `frozenset`, creates an output token (name `"{name}/output"`), and subscribes an
    input-end handler to every input. An already-ended input fires its callback
    synchronously → the output is DOA.
- `token` — the output `LocalLifetimeToken[RB_Out]`.
- `exchange_forward(input_leases: Mapping[LifetimeToken, Lease]) -> Lease[RB_Out]` —
    `input_leases.keys()` must exactly match the declared input set (missing or extra keys
    raise `RuntimeError`). Takes ownership of every supplied lease. Returns a
    `LocalDerivedTokenLease` whose grant is a frozen `_DerivedOutputGrant` with value-based
    equality on `(derived, frozenset(input_lease.grant for ...))` — same input grants
    produce equal output grants, regardless of mapping iteration order.
- `exchange_reverse(out_lease) -> dict[LifetimeToken, Lease]` — consumes the output lease
    and returns fresh clones of each via, one per input token. Because any input ending
    synchronously breaks outputs (see cascade), a live output implies live vias, so reverse
    is always safe.
- `subclone(out_lease, target) -> Lease[B]` — like `exchange_reverse` but returns just
    the via for `target` and *does not* consume `out_lease`. Useful for "I want one input's
    lease and the output lease should stay alive."
- `subtake(out_lease, target) -> Lease[B]` — like `subclone` but consumes `out_lease`,
    discarding every via except the requested one. Equivalent to
    `exchange_reverse(out_lease)` plus pop-one-discard-rest, in a single call.
- `revoke(reason)` — break all outputs, end the output token. Idempotent.
- `await aclose()` — wait for the output token to end; revoke on cancellation. Note: the
    output only ends when an input ends or on `revoke`, so callers must signal drain via
    their own input leases before awaiting `aclose`.
- `revoke_after_block()` — sync CM that revokes on block exit.

**Cascade rules:**

| Trigger | Output leases | Output token | Inputs |
|---|---|---|---|
| Any input token ends | broken (`"input X ended"`) | ends | untouched |
| `revoke(reason)` | broken | ends | untouched |
| All output leases discarded while all inputs still live | — | stays alive (idle); fresh `exchange_forward` ok | untouched |

Simpler than today's keys/requirements model: output ends **iff** any input ends or
`revoke`. A live output lease always implies every via is live.

Construction with an already-ended input is **dead on arrival** (output closed
synchronously in `__init__`). Empty input set is a degenerate-but-legal configuration:
the output never closes except via `revoke`.

### Internal implementation notes

A few implementation details of `local_lease.py` worth knowing about:

- **`_LeaseOwner` protocol**: lifetime objects that own break-groups of leases (both
    `UnconditionalLocalLifetime` and `DerivedLocalLifetime`) implement `_register_lease`,
    `_deregister_lease`, and `_clean_up_lease_data`. `LocalLifetimeLease.__init__` calls
    `_register_lease` on its owner, and `discard` calls `_deregister_lease`. Both methods
    include structural checks (grant identity for unconditional, grant classification for
    derived) so that routing a lease to the wrong owner raises `RuntimeError` loudly rather
    than silently misbehaving.
- **`LocalGrant`** is a pure-identity object: just a reference to its subject token, identity
    equality, identity hash. Holding one confers no authority.
- **`_DerivedOutputGrant`** is a `@dataclass(frozen=True)` whose fields
    `(_derived, _input_grants: frozenset[Grant])` give value-based equality:
    `exchange_forward` calls on equivalent input leases produce output leases with equal
    grants, regardless of input iteration order. No deduplication dict needed.
- **`LocalDerivedTokenLease`** extends `LocalLifetimeLease[RB_Out]` with a
    `_vias: dict[LifetimeToken, Lease]` field — each output lease carries an equivalent
    lease on every input. `clone` clones every via; `exchange_reverse` also clones every
    via into the returned dict. All bookkeeping for `_vias` (discard on normal discard,
    discard on break, discard on leak-cleanup) lives on the owner in
    `_clean_up_lease_data`; the cleanup info stores the **same dict reference** the lease
    holds, so both paths operate on consistent state.
- **Leak detection via `weakref.finalize`**: on every `_register_lease`, the owner sets up a
    `weakref.finalize` on the lease pointing at a module-level `_on_lease_leaked` callback.
    If the lease is garbage-collected without being explicitly discarded, the finalizer logs
    a warning (with allocation traceback when the module-level `LEASE_LEAK_TRACK_ALLOCATIONS`
    flag is True) and runs the same cleanup path as `_deregister_lease` via the shared
    `_clean_up_lease_data(lease_id, info)` method — the token still closes if it was the
    last holder, and any via claims on the input tokens are released. The normal
    discard path calls `finalizer.detach()` to prevent the finalizer from firing later.
    ``_on_lease_leaked`` also bumps a module-level `_LEASE_LEAK_COUNT` (readable via
    `get_lease_leak_count()`) so the test-suite autouse `_assert_no_lease_leaks` fixture
    can detect leaks without competing with `caplog`-based assertions inside individual
    tests; tests that intentionally leak opt out via `@pytest.mark.allow_lease_leak`.
- **Unified cleanup path**: `_clean_up_lease_data(lease_id, info)` is the single
    implementation used by both `_deregister_lease` (after detaching the finalizer) and
    `_on_lease_leaked` (after the finalizer fires). This keeps the discard and leak paths
    in sync by construction. The finalizer is invoked with `lease_id` in its captured args,
    so cleanup is O(1) regardless of entry point.

### `lease_cancel_scope` (in `lease_util.py`)

A context manager that wraps a body in an `anyio.CancelScope` tied to a lease. If the lease breaks
mid-body, the cancel scope fires and any async tasks inside the body are cancelled. Both the lease
exception and any downstream exception that surfaces during cancellation are reported (grouped via
`ExceptionGroup` if both occur). Use this when you want a lease break to actively cancel work
rather than just surface as an error at the next use site.

## Cap-IR layer

`leased_cap_ir.py` defines the abstract intermediate representation. An `IRValue` is one of:

- Primitives (`int | float | str | bool | bytes | None`)
- Sequences / mappings of `IRValue`
- `IRTagged[tag, value]` — application-defined tag wrapping a sub-value (CBOR-like)
- `IRException` — an exception that flows through the system as a value. Carries
    `(message, category, data)` plus a `traceback_info: tuple[str, ...]` slot that
    accumulates formatted-traceback strings hop-by-hop: `wrap_exception` populates the
    initial entry from a caught Python exception's `__traceback__`, and the JSON wire
    codec (`fxtr2/rpc/json_serdes.py`) appends a fresh entry on each encode if any
    additional Python state has been added since the last hop. `__str__` includes the
    trace entries (separated by blank lines) when present
- `LifetimeToken` / `Lease` — the lifetime primitives above
- `IRHandle` — a handle supporting `request` (await) and `dispatch` (fire-and-forget)
- `IRFuture` — an owning reference to an async result, supporting `wait`, `walk`, and
    `pipeline_dispatch`

Request types: `Call(method, arg)`, `GetItem(attr)`, `WaitAndIgnoreFutureThen(wait_for, then)`.
`UnwrapForPipelining(future, lease)` can appear inside call arguments to mean "await this
future and treat the result as equivalent to the resolved value".

**Ownership rules.**

- Arguments to a dispatch are deep-copied at the dispatch point; leases inside are transferred.
    The caller can discard or mutate the original argument immediately after dispatch returns.
- Return values are deep-copied at return; there is no way for the callee to reach into a value
    after returning it.
- `IRFuture` owns every lease inside its resolved value; discarding the lease on the future
    discards all of them. `wait` returns a deep copy so callers can't mutate the owned tree.

See the module docstring of `leased_cap_ir.py` for the full rationale.

### Local Cap-IR implementation (`leased_cap_common.py`)

#### `copy_ir_value(value, *, take)` — the ownership-transfer primitive

Deep-copies an `IRValue` / `IRCallArg` tree. Mutable containers (lists, dicts, `IRTagged`,
`IRException`, `UnwrapForPipelining`) are reconstructed so mutations on the copy don't affect
the original; leases are *cloned* (`take=False`) or *taken* (`take=True`, consuming the
original); handles, futures, tokens, and primitives are shared by reference. Stored
containers are always plain `list`/`dict` (never user types that happen to satisfy
`Sequence`/`Mapping`), which matters for `_descend_and_copy` below.

Public siblings:

- `discard_leases_in(value)` — traverses an `IRValue` / `IRCallArg` / `Request` tree and
    `.discard()`s every `Lease` it finds. Used on cleanup paths (token-end, mid-dispatch
    failures, phase-2 evaluator partial-failure cleanup in `fxtr2/rpc/lowering_evaluate.py`).
    Discard is idempotent, so it's safe to call on values whose leases were already consumed
    downstream.
- `wrap_exception(exc, *, category=None)` — convert a non-IRException Python exception into
    an `IRException`, capturing `traceback.format_exception(...)` as the initial
    `traceback_info` entry. Identity for already-IRException values (their `__traceback__` /
    `__notes__` are picked up by encode-time enrichment in `fxtr2/rpc/json_serdes.py` instead).
    Used at every catch-and-wrap site that would otherwise drop the traceback —
    `spawn_ir_future_in_task_group` and the dispatch-body paths in `fxtr2/rpc/` and
    `fxtr2/services/`.

Internal sibling:

- `_copy_request(req, *, take)` — deep-copies a `Request`. Handles `Call` (copy `arg`),
    `GetItem` (shared — no leases), `WaitAndIgnoreFutureThen` (recursive).

#### `PendingResultBox[T]` / `LocalPendingResultBox[T, B]` — state container with forward-delegation

A plain helper (not an `IRFuture`) that holds the state for a pending dispatch. Views are
`IRFutureLocalImpl` instances (or, on the wire side, `RemoteIRFuture`s sharing a
`FutureImportEntry.resolution`) sharing a single box but with different `attr_path`s into
the resolved tree.

The base class `PendingResultBox[T]` is *token-less*: its lifecycle is managed entirely by
its owner via explicit `discard()`. The subclass `LocalPendingResultBox[T, B](token)` adds
a weak token-end subscription so the box auto-`discard`s when its observed token ends —
that's the right policy when the box's owner *is* the lifetime owner (as for
`IRFutureLocalImpl` / `spawn_ir_future_in_task_group`). The RPC lowering layer's
`FutureImportEntry`, in contrast, observes a *peer-controlled* token and uses the plain
`PendingResultBox` directly so a peer-driven `ClosedMessage` mid-`eval_forwardable`
doesn't kill an in-flight delivery (see `plans/extras/extra5_future_box_race_fix.md`).

- **Lifetime ownership is external**: the creator owns the underlying lifetime (and, for
    `LocalPendingResultBox`, supplies its `LifetimeToken`). The box does **not** validate
    `lease.grant` on `await_resolution`. Grant validation is the responsibility of the
    concrete `IRFuture` that holds the box — `IRFutureLocalImpl.wait` checks grant
    equality against its single expected grant; `RemoteIRFuture.wait` checks
    `grant.token_import_id == future.token_import_id`. Keeping validation out of the box
    lets remote proxies reuse the same state container with different rules.
- **State machine**: `pending | resolved | rejected | forwarded | discarded`. Public
    setters: `set_result(value)` (takes ownership via `copy_ir_value(..., take=True)`),
    `set_exception(exc)` (symmetric), `resolve_from(future, lease)` (delegate — see below).
    All three refuse second-commits on `resolved`/`rejected`/`forwarded` with
    `RuntimeError`, and gracefully drop leases on `discarded` (so a delivery that races a
    discard never leaks).
- **`discard()`**: explicit, idempotent teardown — releases stored leases, transitions
    state to `discarded`, wakes parked waiters with `LeaseBrokenError`. Owners (the
    lowering layer's `FutureImportEntry`, or the test suite directly) call this when the
    box is no longer needed.
- **`await_resolution(attr_path, register_break)`**: blocks until `_resolved_event`
    fires (cancellable via a `register_break`-installed callback in a local
    `CancelScope`), then either descends + deep-copies the stored value, raises a *fresh*
    copy of the stored exception, or delegates to the forwarded future.
- **`await_or_forward(attr_path, register_break)`**: same as `await_resolution`, except on
    the `forwarded` state it returns `UnwrapForPipelining(forward_future.walk(attr_path),
    forward_lease.clone())` instead of recursively awaiting. Callers that want to relay
    the forward elsewhere (e.g. the RPC lowering layer emitting a forwarding
    `ResolveMessage(AwaitFutureExpr(...))` to the peer) use this to get the forward
    target rather than fetching the value through this process. Return type is widened
    to `IRValue` because `attr_path` descends into an arbitrary sub-shape of `T`; the
    impl-level `IRFutureLocalImpl.wait_or_forward` re-narrows to `T` since its
    attr_path is baked in at construction.
- **`resolve_from[B2](future, lease)`**: delegates resolution to another `IRFuture`. This is
    the *promise-pipelining* optimization — when a pipelined call produces an
    already-dispatched future, we just forward our box to it instead of eagerly pulling the
    value through our process. `_await_forwarded` reuses the same cancel-on-lease-break
    pattern when delegating.
- **Token-end cleanup (`LocalPendingResultBox` only)**: when the observed token ends, the
    weak subscription's callback calls `self.discard()`, releasing any stored value /
    exception / forward lease and transitioning to `discarded`.

#### `IRFutureLocalImpl[T, B]` — thin view over a box

`(box, attr_path, task_group, expected_grant)`. Implements the `IRFuture` contract and
owns grant validation for the box:

- `token` → `box.token`.
- `wait(lease)` → validates `lease.grant == expected_grant`, then
    `await box.await_resolution(attr_path, lease)` (box takes the lease).
- `wait_or_forward(lease)` → grant-check + `await box.await_or_forward(attr_path, lease)`.
    Returns `T | UnwrapForPipelining[T, Any]`: the caller gets the resolved value on
    normal resolution, or the forward target (future + fresh clone of the forward lease)
    when the box is in the `forwarded` state. The `T` in the return is sound here because
    `IRFutureLocalImpl[T, ...]`'s type parameter describes the walked-to shape at
    `self._attr_path`; the box's wider return is re-narrowed with a cast. Used by the
    RPC lowering's `_on_pull` resolver loop to detect "forward terminates at a peer-side
    remote future on this same connection" and emit a forwarding `ResolveMessage`.
- `walk(attr_path)` → new `IRFutureLocalImpl` with extended `attr_path` (and same
    `expected_grant` / `task_group`). Accepts a single `str` / `int` or a sequence; a bare
    `str` is always a single step, never a sequence of characters.
- `pipeline_dispatch(future_lease, inner_handle_lease, request)` → synchronously takes the
    three inputs, schedules a runner that `await`s the box for the handle, optionally
    unwraps the inner handle lease, then calls `handle.dispatch(inner_lease, request)` and
    returns an `UnwrapForPipelining(inner_future, inner_result_lease)` — the runner forwards
    the box to the inner future (via `resolve_from`), avoiding an eager fetch-and-re-box.

#### `spawn_ir_future_in_task_group(*, name, work, task_group)` — the "normal" dispatch helper

Creates a fresh `UnconditionalLocalLifetime` + `LocalPendingResultBox`, schedules `work` in
the supplied task group, and returns `(IRFutureLocalImpl, root_lease)`. The runner routes
outcomes:

- `work` returns `IRValue` → `box.set_result(value)`.
- `work` returns `UnwrapForPipelining(future, lease)` → `box.resolve_from(future, lease)`.
- `work` raises `IRException` → passes through to `box.set_exception`.
- `work` raises any other `Exception` → wrapped via `wrap_exception(exc)` (category =
    `type(exc).__name__`; original Python traceback captured into
    `IRException.traceback_info`) and delivered via `box.set_exception`; the task group does
    not see the exception.
- `work` raises a non-`Exception` `BaseException` (cancellation, `SystemExit`) → the lifetime
    is revoked and the exception is re-raised into the task group.

This is the canonical shape a real `IRHandle.dispatch` uses: take inputs, schedule work,
return the `(future, lease)` pair.

#### `IRHandleLocalImplBase[B]` — concrete handle base class

Subclasses supply only `_run_request_async(lease, request) -> IRValue |
UnwrapForPipelining`, where `request` is already a `Call[UnwrappedCallArg] | GetItem`
(`UnwrapForPipelining` and `WaitAndIgnoreFutureThen` have been resolved ahead of time by
the base class). `token` / `request` / `dispatch` are all concrete on the base:

- **`request(lease, request)`** = `dispatch(lease, request) + wait + discard` — so
    abandoning a wait does **not** cancel the underlying dispatched task. "Wait is a read,
    not a control op."
- **`dispatch(lease, request)`** synchronously takes ownership of `lease` and any leases
    inside `request`, then spawns a runner in `self._task_group` via
    `spawn_ir_future_in_task_group`. The runner first calls `_unwrap_request_futures` (see
    below) to resolve pipelining in the request, then `_resolve_pipelined_lease` to
    resolve a pipelined outer lease (see "Pipelined outer lease" below), then delegates
    to `_run_request_async`. Whatever `_run_request_async` returns flows through
    `set_result` / `resolve_from` in the result box. Pre-`_run_request_async` failures
    (request unwrap, lease unwrap) discard `lease` and any unconsumed request leases
    before re-raising — `spawn_ir_future_in_task_group`'s outcome routing then converts
    the exception into the result box's rejection without leaking.

**Pipelined outer lease.** `dispatch` and `request` accept either a concrete
`Lease[B]` or `UnwrapForPipelining[Lease[B], Any]` for the outer-lease parameter.
The latter form lets a caller chain a future-of-lease (e.g. a Cap-IR converter
producing a fresh lease for a derived lifetime) directly into a dispatch without a
synchronous round-trip on the lease — the runner awaits the inner future inside its
own spawned task. If the resolved value isn't a `Lease`, `_resolve_pipelined_lease`
discards any leases buried in the resolved value and raises
`IRException(category="PipelineTypeError")`. Subclasses don't see the pipelined
form: `_run_request_async` is always called with a concrete `Lease[B]`.
- **`_unwrap_request_futures(request)`** handles `UnwrapForPipelining` in `Call.arg` (await
    + substitute) and `WaitAndIgnoreFutureThen(wait_for, then)` (await `wait_for` futures,
    discard their results, recursively unwrap `then`). `GetItem` passes through unchanged.

Ownership convention for `_run_request_async` inputs (standard transfer): each input lease
is used in *exactly one* of — discarded, forwarded to a downstream callee, or embedded in
the returned result (in which case the return-path `copy_ir_value(take=True)` in
`set_result`/`resolve_from` takes ownership from the implementation's reference). Leases
that are genuinely forgotten are cleaned up by the `LocalLifetimeLease` weakref finalizer,
which logs a warning. The wrapper does **not** defensively discard, so an implementation
cannot both store a reference to an input lease AND embed it in the result — the return
path's take would invalidate the stored reference. If you need both, `lease.clone()`
before embedding.

The `_task_group` must outlive `_token` — use `open_lifetime_task_group` (below) which
co-constructs both with that invariant.

#### `open_lifetime_task_group(name)` — co-manager helper (in `lease_util.py`)

```python
async with open_lifetime_task_group("my-dispatcher") as (lifetime, task_group):
    handle = MyHandle(token=lifetime.token, task_group=task_group)
    # Hand out clones of lifetime.root_lease to callers that need to reach `handle`.
    ...
```

The task group is the outer context manager so it outlives the lifetime, matching the
"`_task_group` must outlive `_token`" invariant used by `IRHandleLocalImplBase`. On normal
block exit, the lifetime drains (its async CM's default behavior — discard root, wait for
clones) before the task group awaits remaining tasks. On any exception in the body or in a
spawned task, the lifetime is revoked synchronously (breaking outstanding leases, which
surfaces to tasks as `LeaseBrokenError`), then the task group unwinds and re-raises.

Uses `fxtr2.util.anyio_util.create_task_group` under the hood with
`collapse_exc_group=True` by default, so a single-cause failure propagates as the inner
exception rather than a `BaseExceptionGroup[SingleCause]`. Pass
`collapse_exc_group=False` to preserve the raw group shape.

### Cap-bridge taxonomy (`lease_bridges.py`)

A *cap-bridge* is the at-rest lease-minting machinery a holder stores
to produce fresh leases on a token whose lifetime is independent of
the holder's. Two orthogonal axes, four variants:

| | Independent (single stored lease) | Dependent (inputs + converter) |
|---|---|---|
| **Local** (concrete output) | `IndependentLocalBridge(lease)` | `DependentLocalBridge(leases, converter)` |
| **Pipelined** (output is a future-of-lease) | `IndependentPipelinedBridge(pipelined)` | `DependentPipelinedBridge(leases, converter)` |

- *Independent* bridges store a pre-existing lease (concrete or
  pipelined `(future, lease)` pair) and clone it on each lookup.
- *Dependent* bridges store a `dict[LifetimeToken, Lease]` of input
  leases plus a converter callable that mints a fresh output lease
  per call from `(cloned_inputs, base_lease)`. Used when persistently
  holding a `Lease[handle.token]` would create a cycle.
- *Local* bridges produce a concrete `Lease`; *Pipelined* bridges
  produce an `UnwrapForPipelining(future, lease)` pair so downstream
  Cap-IR dispatches can chain the resolution without a synchronous
  round-trip.

Two converter callable types: `DependentLocalBridgeConverter` (strict
concrete inputs, concrete output) and `DependentPipelinedBridgeConverter`
(possibly-pipelined inputs, pipelined output).

Two convenience unions: `LocalBridge` (the two Local variants — used
where pipelining is statically ruled out, e.g. ancestor entries) and
`LocalOrPipelinedBridge` (all four — the canonical "cap-bridge" sum).

Each bridge dataclass exposes `clone()` (independent at-rest copy:
clones every stored lease, shares converter callables and pipelined
futures by reference) and `discard_at_rest()` (idempotent — discards
every stored lease and clears the inputs dict).

`FxtrScope.attach_bridged_lowlevel_cap(handle, target_type, bridge)`
is the low-level entry point that takes any of the four variants;
the higher-level `mount_lowlevel_cap_as_service(handle, target_type)`
builds a trivial identity `DependentLocalBridge`.

`CapIRArena.with_converter(... bridge=...)` uses the same bridge
types as its at-rest store, dispatching on the variant inside
`_make_y_lease`. `MemoryArena.lazily_attach_from_arena` also accepts
a `LocalBridge` directly — the four-variant taxonomy is the
canonical at-rest shape across the system.

## Common patterns

**Self-contained lifetime (revoke on block exit):**

```python
with UnconditionalLocalLifetime("my-lifetime").revoke_after_block() as lt:
    holder_a(lt.root_lease.clone())
    holder_b(lt.root_lease.clone())
    # On block exit: all outstanding leases break synchronously.
```

**Lifetime that hands out leases and drains on exit:**

```python
async with UnconditionalLocalLifetime("my-lifetime") as lt:
    spawn_worker(lt.root_lease.clone())
    ...
    # On normal exit: discard root, then wait for the worker to release its clone.
    # On exception (including cancellation): revoke instead of waiting.
```

**Callee takes a lease, uses it in multiple places:**

```python
def do_something(lease: Lease[T]):
    with lease.take() as lease:  # lease is now our own copy
        bar(lease.clone())        # hand clones out
        baz(lease.clone())
        # lease is discarded here
```

**Caller wants to keep its lease while passing one to a callee:**

```python
do_something(my_lease.clone())
# my_lease is still usable
```

**Cancel work when a lease breaks:**

```python
with lease_cancel_scope(my_lease) as cancel_scope:
    async with create_task_group() as tg:
        tg.start_soon(long_running_thing)
        ...
```

**Tie a derived capability to multiple input lifetimes:**

```python
derived = DerivedLocalLifetime(
    "my_derived",
    inputs={upstream_a.token, upstream_b.token},
)
# Anyone with leases for all declared inputs can call
#   derived.exchange_forward({upstream_a.token: a_lease, upstream_b.token: b_lease})
#   -> output_lease
# to mint an output lease. They can later recover the input leases via
#   derived.exchange_reverse(output_lease) -> dict[LifetimeToken, Lease]
# If any input ends → output leases break immediately.
# If all outputs are discarded but inputs remain live → Derived is idle and can
# mint more outputs later.
# derived.revoke(reason) → breaks all outputs and ends the output token.
```

**Key-gated conditional-grant shape:**

```python
# Keep the "key" as a private Unconditional we control; include it + the
# upstream "requirement" in the Derived's input set. Hand out key-lease clones
# to whoever is allowed to mint forward-exchanges.
key_ull = UnconditionalLocalLifetime("my_key")
derived = DerivedLocalLifetime(
    "my_derived",
    inputs={key_ull.token, upstream_token},
)
try:
    yield derived  # hand out exchange_forward outputs here
finally:
    # Signal drain by dropping our own hold on the key.
    key_ull.root_lease.discard()
    # Wait for the key ULL to drain (its only remaining holders are vias
    # embedded in Derived output leases). Nested try/finally so that if the
    # first aclose cancels, the second still runs and revokes cleanly.
    try:
        await key_ull.aclose()
    finally:
        # By now an input (the key) has ended, so derived's output token is
        # closed too; this returns promptly.
        await derived.aclose()
```

**Bridge a foreign arena into a memory store via `lazily_attach_from_arena`:**

The memory store's `lazily_attach_from_arena` API takes a `LocalBridge`
(an `IndependentLocalBridge` or a `DependentLocalBridge`) that mints
`Lease[Y]` per load. Three canonical patterns cover essentially every
legitimate attach. The quick reference below summarises them; **for the
full explainer** (the via mechanic that motivates the three cases, the
cycle hazards in cases 2 and 3, and a worked example tying the three to
one concrete test), see
`plans/phase3/stage8c_user_caps/lifetime_conversion_mechanics.md`:

1. **Y is a parent / outer scope of X.** Use
   `IndependentLocalBridge(lease=Lease[Y])` — the at-rest lease is on Y
   directly; each load just clones it.
2. **Y is derived from X (and possibly other gating tokens).** Use a
   `DependentLocalBridge` whose stored input is the gating key (a
   `Lease[K]`) and whose converter wraps `derived.exchange_forward`.
   Each load mints a fresh `Lease[Y]` from
   `(stored_key_clone, base=Lease[X])`, so we never persistently hold a
   `Lease[Y]` (which would cyclically pin X).
3. **X is derived from Y.** Use a `DependentLocalBridge` with no stored
   inputs (`leases={}`) whose converter wraps
   `derived.exchange_reverse(base)`, discards the unused inputs, and
   returns the `Lease[Y]` that was structurally present as a via inside
   `base`. No key needed — the converter has no real lifetime gating,
   just the closure's reachability.

Cases 2 and 3 are the cycle-avoidance patterns; mixing them up (e.g.
storing a `Lease[Y]` in an `IndependentLocalBridge` when Y is derived
from X) reintroduces the cycle.

## See also

- `plans/phase3/stage8_lease_system/overview.md` — the implementation plan for the lease system,
    including the staged breakdown (docs → local leases → local Cap-IR → lowering → wire codec).
- `fxtr2/rpc/LOCAL_CODE_OVERVIEW.md` — the RPC lowering layer, which layers on top of these
    primitives.
