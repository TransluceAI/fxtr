# Stage 8c — User-facing capability layer

Build the user-facing analog to `IRHandle` / `IRFuture`: a way for applications
(and RPC peers) to mount live capabilities into scopes, pass capability
references around, and invoke them — with transparent bridging across processes.

The starting point is [`sketch.md`](sketch.md). This doc concretises the design,
resolves a set of open design questions we discussed ahead of time, and breaks
the work into seven substages, each of which will later get its own detailed
implementation plan.

## High-level picture

The existing fxtr2 codebase has a clean story for *data* capabilities:

- A `FxtrScope` owns a main arena + main lease, tracks a set of FxtrIDs, and
  composes via `include=`.
- An `EntityRef[T]` is a portable, lazily-resolvable reference that is
  `.load()`ed against a scope.

Stage 8c adds the symmetric story for *live* capabilities:

- A mountable `FxtrScope` can host one or more `FxtrServiceImpl[T]`s. Each mount
  becomes an internal `IRHandle[B]` whose token is the scope's token (or is
  bridged to one via a converter).
- A `ServiceRef[T]` is a portable reference to a live capability,
  `.resolve()`d against a scope into a user-facing typed object.
- The same `ServiceRef`s survive unchanged through an `fxtr2/rpc/` connection,
  so cross-process capability passing is free on top of the primitives below.

Three layers of new infrastructure make this possible:

1. A generalised **derived lifetime** primitive that lets one token be tied to
   an arbitrary set of other tokens (replacing `AttachedLocalLifetime`).
2. **Arena-over-CapIR**: entity arenas travelling through `IRHandle`s so that
   fxtr data flows across RPC using the same machinery as live capabilities.
3. **`ServiceRef` + mountable scopes**: mount / attach / resolve APIs, with
   initial `UntypedService` support and a later mashumaro-driven typed-service
   layer.

## Locked-in design decisions

Settled in discussion before writing this doc:

- **Derived fully replaces Attached.** `AttachedLocalLifetime` and
  `LocalAttachedTokenLease` are deleted. `DerivedLocalLifetime` takes an
  arbitrary set of input tokens, all treated symmetrically — there is no
  privileged "key" token in the primitive itself. A caller who wants
  key-gating just passes in a token of their own.
- **`_KeepAlive` and weak scopes are deleted.** Scope lifetime ordering is
  expressed purely through lease drain. `new_weak_child_scope`, `_share_token`,
  `_KeepAliveToken` / `_KeepAliveWaiter`: all removed and their call sites
  migrated to normal child scopes. Weak scopes were an experiment that is not
  load-bearing anywhere except a handful of tests.
- **Mountable and derived are orthogonal.** Two independent flags:
  - `FxtrScope(..., mountable=True)` opens a fresh anyio task group on
    `__aenter__` and closes it after token drain on exit; non-mountable
    scopes have no task group and can't have services mounted on them.
    This is purely about *hosting* services and sub-tasks — nothing to
    do with the scope's backing token.
  - Separately, a scope's backing lifetime is normally an
    `UnconditionalLocalLifetime`. A low-level alternative constructor
    (`FxtrScope._with_derived_lifetime(derived_leases=..., ...)`) binds
    the scope to a fresh `DerivedLocalLifetime` whose inputs are the
    supplied external leases plus an internal key ULL. This constructor
    is internal; normal user scopes use the default unconditional
    lifetime.
  - The two flags compose freely:
    - Unconditional + non-mountable: plain child scope (most current
      tests / user code).
    - Unconditional + mountable: user scope hosting a service.
    - Derived + non-mountable: internal, e.g. a plain child of a
      derived-scope parent.
    - Derived + mountable: internal, e.g. the per-call child scope spun
      up by a service dispatch body.
- **`aclose()` is public but the async CM is preferred.** `async with scope:`
  wraps `try: yield; finally: await self.aclose()`. Non-CM usage
  (`scope = FxtrScope(); ...; await scope.aclose()`) is supported but
  secondary.
- **Active-scope is a pointer, not a lifetime claim.** `ACTIVE_SCOPE` stays
  as today — a contextvar holding a `FxtrScope` reference, set on scope
  entry and on `as_active_scope()`. Setting an active scope does *not*
  hold a lease on it. If a caller wants to extend a scope's lifetime
  beyond its natural drain they should open a child scope that references
  it, not rely on `as_active_scope`. Operations through a draining active
  scope raise via the normal mutation guard; no special handling of the
  contextvar itself is needed.
- **Scope cap-bridge storage mirrors the memory-store foreign-lease pattern.**
  Each mountable scope holds a `dict[LifetimeToken, _CapBridge]`; resolving a
  `ServiceRef` walks up through parents at resolve time. Bridges are not
  eagerly copied down into child scopes.
- **Untyped services use a fixed recursive `FxtrServiceData` vocabulary.**
  `FxtrServiceData = None | bool | int | float | str | bytes | FxtrID |
  EntityRef[Any] | ServiceRef[Any] | Mapping[str, FxtrServiceData] |
  Sequence[FxtrServiceData]`. Anything else raises on conversion. Raised
  exceptions are marshalled as `IRException` and re-raised on the caller.
- **`@service_method` is a module-level tagging decorator.** Default
  `_service_exposed_methods` walks a `FxtrServiceImpl` for tagged methods;
  subclasses may override.
- **`ServiceRef` shape.** Frozen dataclass `(handle: IRHandle[Any],
  target_type: type)`, value equality on both fields. The resolving scope is
  supplied at `.resolve()` time, not baked in.
- **Call convention.** Two wire shapes — an input packet and a (different)
  return packet.

  *Input*:
    `IRTagged("fxtr_call", {
        "method": str,
        "args": Sequence[IRValue],
        "kwargs": Mapping[str, IRValue],
        "leases": Sequence[Lease],
        "fxtr_arena": IRHandle | None,
    })`.
  `args` / `kwargs` are the lowered `FxtrServiceData` trees (positional
  and keyword respectively), with `EntityRef → tagged FxtrID bytes` and
  `ServiceRef → bare IRHandle ref` (no leases). Keeping them separate
  lets the receiver dispatch as `impl_method(*args, **kwargs)` without
  re-deciding which fields are positional. All leases referenced by
  anything in the packet — handle refs inside `args` / `kwargs` *and*
  the `fxtr_arena` token if present — sit in the flat `leases` list,
  de-duplicated so there is at most one lease per distinct subject
  token. The receiver rebuilds a `{lease.grant.subject: lease}` map on
  arrival; `fxtr_arena`'s lease is just the map entry for
  `fxtr_arena.token`.

  *Return*:
    `IRTagged("fxtr_return", {
        "result": IRValue,
        "fxtr_arena": IRHandle | None,
        "returned_refs": Sequence[IRTagged("ret_ref", {
            "token": LifetimeToken,
            "key_lease": Lease,
            "converter": IRHandle | None,
        })],
    })`.
  `result` is the lowered `FxtrServiceData` return value (same lowering
  as args). `fxtr_arena` covers any `FxtrID`s in `result`. The
  `returned_refs` list has one entry per distinct subject token
  referenced anywhere in the return (handles inside `result` plus
  `fxtr_arena.token` if present). Each entry carries *two* slots
  because a returned token may or may not predate the call scope:
  - **`converter is None`**: the token predates the call (e.g. a
    ServiceRef that was passed in as an arg and is now being handed
    back). `key_lease` is a direct lease on `token`, same shape as
    input leases; caller uses it directly.
  - **`converter is not None`**: the token is local to the call scope
    and the caller does not already have a way to mint leases on it.
    `key_lease` is a lease on a key token the callee owns (not
    `token` itself); `converter` is an IRHandle whose dispatch, given
    `key_lease` + fresh leases on the caller-supplied input tokens,
    produces a fresh `Lease[token]` (DerivedLocalLifetime semantics
    bridged through Cap-IR). The caller wires this into its scope
    via `attach_leased_lowlevel_cap` (substage 6) or the arena
    equivalent (`CapIRArena` in substage 5).
  For errors, `RejectMessage` with `IRException` flows through the
  normal Cap-IR path — no special return-packet shape.
- **Each substage leaves the test suite green.** Where existing tests are
  rewritten (weak-scope removal, scope aclose, arena token shape), the
  rewrites ship in the same substage that changes the API. Note that this does
  not require test suite to be green throughout the implementation of the
  substage, only at the _end_ of each substage.
- **Clean-drain assertion in every substage's tests.** Each new test asserts
  that after the test body completes, all leases are discarded and all tokens
  have ended with no lease-leak warnings. Shared `conftest.py` fixtures
  (leak-allocation tracking enabled, lease-leak capture asserting empty) are
  the expected vehicle. Introduced in substage 1 and carried through.

## Substage layout

| # | Substage | Primary surface | Deliverable |
|---|---|---|---|
| 1 | Derived lifetime primitive | `fxtr2/leases/local_lease.py` | `DerivedLocalLifetime` + migration |
| 2 | Memory store token-ownership refactor | `fxtr2/data/memory_arena.py` | Store accepts externally-owned tokens |
| 3 | `FxtrScope` drain refactor | `fxtr2/data/entity_ref_scope.py` | `aclose()`, mountable flag, keep-alive removal |
| 4 | Pipelined dispatch leases | `fxtr2/leases/` + `fxtr2/rpc/` | `IRHandle.dispatch` accepts `UnwrapForPipelining[Lease, K]` |
| 5 | Arena-over-CapIR | New module | `ArenaIRHandleAdapter` + `CapIRArena` + `PipelinedLifetimeConverter` |
| 6 | `ServiceRef` fundamentals + `UntypedService` | New module | Mount / attach / resolve + dynamic proxy |
| 7 | `FxtrScope` ↔ RPC bridge | `fxtr2/rpc/` + scope API | `open_rpc_scope` CM |
| 8 | Mashumaro-driven typed services | `fxtr2/conversion/` + new module | `@service_protocol`, typed proxy / impl |
| 9 | Lease-leak ratchet cleanup | `tests/` + various | Fix the remaining pre-existing leaks marked `allow_lease_leak`; restore full leak-free floor across the suite |

Substages 1–3 are internal refactors that enable the ServiceRef work. 4–7 add
the new user surface, each building on the previous. Substage 9 is the
ratchet-forward cleanup of pre-existing lease leaks that surfaced when the
autouse `_assert_no_lease_leaks` fixture was promoted to top-level scope in
substage 3.

---

## Substage 1 — Derived lifetime primitive

**Goal.** Delete `AttachedLocalLifetime`; replace with `DerivedLocalLifetime`,
which ties a new lifetime to a *set* of input tokens.

**Primitive.**

```
class DerivedLocalLifetime[RB_Out, ...]:
    def __init__(self, name: str, inputs: Collection[LifetimeToken[Any]]): ...

    @property
    def token(self) -> LocalLifetimeToken[RB_Out]: ...

    def exchange_forward(
        self, input_leases: Mapping[LifetimeToken[Any], Lease[Any]]
    ) -> Lease[RB_Out]: ...

    def exchange_reverse(
        self, out_lease: Lease[RB_Out]
    ) -> dict[LifetimeToken[Any], Lease[Any]]: ...

    def revoke(self, reason: str) -> None: ...
```

**Cascade rules.**

| Trigger | Output leases | Output token | Inputs |
|---|---|---|---|
| Any input token ends | broken ("input X ended") | ends | — |
| All output leases discarded *and* at least one input has ended | — | ends | untouched |
| All output leases discarded while all inputs still live | still usable for fresh `exchange_forward` | stays alive (idle) | untouched |
| `revoke(reason)` | broken | ends | untouched |

Each output lease internally holds one clone of every input lease (analogous to
`_via` on today's `LocalAttachedTokenLease`, generalised to a map). As a
result, inputs can't drain until all output leases are released; this is what
makes "drop outputs, wait for an input to end" a clean drain sequence.

**Scope-usage context managers.** Parallel today's attached pattern:

- `with DerivedLocalLifetime(name, inputs).revoke_after_block() as d:` (sync).
- `async with DerivedLocalLifetime(name, inputs) as d:` (async): on normal
  exit, waits for the output token to drain; on exception, revokes.

**Internal state.** `_DerivedOutputGrant` is a frozen dataclass
`(_derived, _input_grants: frozenset[Grant])` giving value-based equality;
`LocalDerivedTokenLease` extends `LocalLifetimeLease` with
`_vias: dict[LifetimeToken, Lease]`. Clone clones every `_via`; owner-side
`_clean_up_lease_data` discards the `_via` map. Leak detection via
`weakref.finalize` follows today's pattern.

**Migration.** Touches:

- `fxtr2/leases/local_lease.py` — delete Attached, add Derived.
- `fxtr2/leases/leased_cap_common.py` — if anything used Attached, update.
- `fxtr2/rpc/lowering.py` — regrant paths use `DerivedLocalLifetime` with
  `inputs={subject_token}` for each regrant (or a simpler Unconditional
  depending on whether the bounce-back identity still needs the derived
  semantics; this is a minor call made during the substage plan).
- Tests in `tests/test_leases/` — rewritten or added.

**Checklist.**

- [x] Audit: grep `AttachedLocalLifetime` / `LocalAttachedTokenLease` in
      `fxtr2/rpc/` and elsewhere; decide per-site whether migration is a
      no-op (just unused) or a substantive switch to `DerivedLocalLifetime`.
      *Result: only stale docstring mentions in `fxtr2/rpc/lowering_state.py`
      and `fxtr2/data/abstract_arena.py`; no runtime code was using it.*
- [x] `DerivedLocalLifetime` + frozen-dataclass grant + token-lease subclass.
      Python API: `exchange_forward` / `exchange_reverse` take / return
      `Mapping[LifetimeToken, Lease]` (self-documenting). The flat-list shape
      is only on the wire call convention, per the locked-in decisions.
- [x] Migrate all call sites; delete `AttachedLocalLifetime` et al.
- [x] Unit tests: forward/reverse roundtrip; single-input equivalence to old
      attached; multi-input forward; input-ends-while-outputs-live; revoke
      semantics; idle state with zero output leases.
- [x] Clean-drain fixture in shared `conftest.py` (first substage, carries
      through the rest). *Landed in `tests/test_leases/conftest.py` — scoped
      to lease-level tests today because the memory-store / scope layers
      have pre-existing leaks that substages 2–3 will fix. Promote to the
      top-level `tests/conftest.py` as those substages land.*
- [x] Update `fxtr2/leases/LOCAL_CODE_OVERVIEW.md` + `CODE_OVERVIEW.md`.

**Deviations from the substage plan (all captured in `part1_plan.md`):**

- Cascade is simpler than sketched: the "all outputs drained + an input
  ended" row is absorbed into "any input ends", because an input ending
  synchronously breaks outputs. One consequence is that `exchange_reverse`
  just clones vias (no broken-input edge case).
- `DerivedLocalLifetime` has no `async with` context manager. It exposes
  `await aclose()` (wait for output token, revoke on cancel) and a sync
  `revoke_after_block()`; drain composition is caller-specific. The async
  CM stays on `UnconditionalLocalLifetime`, now implemented in terms of a
  newly-extracted `aclose()` method.
- `IRFutureLocalImpl.wait` / `wait_or_forward` / `pipeline_dispatch` now
  discard the passed-in leases on grant-mismatch (taken up-front, discarded
  via the with-unwind / explicit except clause). Honours the "lease is
  transferred in" contract in every branch. Unrelated to the primitive
  itself, but the autouse leak fixture surfaced the pre-existing leak.

---

## Substage 2 — Memory store token-ownership refactor

**Goal.** The memory store no longer owns the `ULL` for each token. Callers
mint their own lifetime object (an `UnconditionalLocalLifetime` today, or a
`DerivedLocalLifetime` from substage 3 onward) and register the resulting
token + parent leases with the store. The store subscribes to the token's
end-callback for GC, without holding a lease on the token itself.

**Why.** Substage 3 needs scopes to own their own lifetime tokens outright
— and more generally, "who mints the ULL" was never really the store's
concern. Keeping a convenience wrapper that mints-on-behalf would just
re-entrench the wrong mental model.

**API change.** Drop `create_token_from_leases`; replace with a single lower-
level entry point:

- `MemoryStore.register_token(token: LifetimeToken, *, parents: Sequence[Lease], name: str = "") -> None`
  — registers a pre-existing token with the store. Consumes each parent
  lease (`.take()`), stores it in the token's `parent_leases` row, computes
  `ancestor_leases`, and subscribes the store's `_on_token_end` handler to
  `token.register_end_callback`. Does not hold a lease on `token` itself.
  `name` is used only for logging.

No wrapper is kept; every caller mints its own lifetime.

**Callers affected.**

- `FxtrScope.__init__` — today's non-`_share_token` path becomes:
  ```python
  self._scope_ull = UnconditionalLocalLifetime(name or repr(self))
  self._token = self._scope_ull.token
  self._main_lease = self._scope_ull.root_lease.take()
  self._global_store.register_token(
      self._token,
      parents=[p._main_lease.clone() for p in unique_parents],
      name=name,
  )
  ```
  (Substage 3 then generalises this further for derived scopes.)
- Direct memory-arena tests that mint tokens by hand — migrated to the same
  pattern.

**Checklist.**

- [x] Replace `create_token_from_leases` with `register_token`; no wrapper.
- [x] Migrate `FxtrScope.__init__` to the mint-then-register pattern.
- [x] Migrate memory-arena tests.
- [x] Update internal helpers (`_make_base_lease`, `_on_token_end`, etc.)
      where they implicitly assume store-owned ULL semantics. *Result:
      they were oblivious, as expected. `_make_base_lease` didn't touch
      grants; `_on_token_end` was already driven by the end-callback.*
- [x] Update `CODE_OVERVIEW.md`.

**Deviations from the substage plan (all captured in `part2_plan.md`):**

- Dropped grant caching entirely (both `LifetimeTokenData.grant` and
  `MemoryArena._expected_grant`), rather than just threading `grant`
  through `register_token`. Rationale: the store is a subscriber, not
  a lifetime owner, so it cannot guarantee grant uniqueness; the same
  applies to `MemoryArena` as a view. Grant-level checks are left to
  lifetime owners (HTTP client, SQLite session, etc., which still use
  `validate_lease_grant`).
- Added a duplicate-registration guard on `register_token` (raises
  `RuntimeError` if the same token is registered twice). Today's
  `create_token_from_leases` couldn't hit this because it minted
  tokens; now that registration is caller-driven, the guard catches
  caller bugs cheaply.
- Added explicit `TestRegisterToken` unit tests for
  duplicate-registration and unknown-parent error paths (happy path
  continues to be exercised through the migrated fixtures).
- Leaves `LeaseGrantMismatchError` in place as the error raised on
  arena subject-mismatch (slight misnomer now; rename deferred to
  avoid touching the RPC layer).

---

## Substage 3 — `FxtrScope` drain refactor

**Goal.** Scope lifetime is expressed purely through lease drain. Add
`mountable` flag, `aclose()`, and delete the keep-alive / weak-scope stack.

**New surface.**

- `FxtrScope(..., mountable: bool = False)`.
- `await scope.aclose()` — drops the scope's root lease(s), waits for the
  backing token to drain; on error/cancel, revokes for fast close. Exact
  close sequence depends on the two orthogonal axes (lifetime type and
  mountable flag); see "`aclose` per lifetime type" and "Task group
  handling" below.
- `async with scope:` — `try: yield self; finally: await self.aclose()`.
- `scope.as_active_scope()` — contextmanager that sets `ACTIVE_SCOPE = self`
  for the block and resets on exit. Does not hold any lease on the scope
  and does not extend its lifetime; if the scope drains mid-block, user-
  facing operations through it raise as they would anywhere else.

**Two orthogonal axes.**

- **Lifetime type.** Default: unconditional — scope owns one
  `UnconditionalLocalLifetime` `_scope_ull`, with `_main_lease =
  _scope_ull.root_lease.take()`. Low-level alternative: derived — a
  separate internal constructor `FxtrScope._with_derived_lifetime(...,
  derived_leases=..., include=..., ...)` that mints a fresh key ULL
  (`_key_ull`) plus a `DerivedLocalLifetime` whose inputs are
  `{_key_ull.token} ∪ {lease.subject for lease in derived_leases}`,
  calls `exchange_forward` once to produce the scope's output
  `_main_lease`, and separately stashes `_key_ull.root_lease` so it
  can be discarded independently at close.
- **Mountable.** Independent `mountable: bool` init flag. When True,
  the scope opens a task group at `__aenter__` (co-managed so the task
  group outlives the token, per `open_lifetime_task_group`), stores it
  as a private attribute (`scope._task_group`), and allows
  `scope.mount(...)` / `scope.mount_lowlevel_cap_as_service(...)` /
  `scope.attach_leased_lowlevel_cap(...)`. Non-mountable scopes skip
  the task group and reject mount attempts. The task group is internal
  infrastructure — users aren't expected to interact with it directly,
  and it carries no user-visible guard against new-task submission.

The two axes compose freely (see table in the "Locked-in design
decisions" section). In particular, unconditional + mountable is the
normal "user scope hosting a service" case — it just has a task group
layered on top of today's scope-lifetime shape.

**`aclose` per lifetime type.**

- Unconditional: discard `_main_lease` → wait for the backing token's
  end-callback to fire.
- Derived: discard `_main_lease` *and* `_key_ull.root_lease` → wait for
  the *derived output* token to end (key token is free to dangle). Other
  output leases held elsewhere gate drain via `DerivedLocalLifetime`'s
  "ends once no outputs AND at least one input ended" rule.

If the scope is mountable, task-group close happens *after* the token
drain (or after revoke, on the error path).

On cancel / error, the locally-owned lifetime(s) are revoked for a fast
close.

**Drain-phase behaviour.** Between "`aclose` begins" and "token actually
ends" the scope has a live token but is no longer willing to accept new
work. Specifically:

- A `_draining` flag flips on as the first step of `aclose` (or on any
  async-CM exception before revoke).
- User-facing mutation APIs raise once `_draining` is set:
  `load`, `store`, `store_cbor`, `load_cbor`, `lift`, `copy_from_scope`,
  `track_from_scope`, `abandon`, `mount`, `mount_lowlevel_cap_as_service`,
  `attach_leased_lowlevel_cap`, `register_deep_fetch_source`, etc. All
  raise `RuntimeError("scope is draining")`.
- `as_active_scope()` does *not* participate in drain — entering it on a
  draining scope is allowed (it's just a contextvar set). Any subsequent
  user-facing operations through the now-draining active scope raise via
  the normal mutation guard, which is a good enough error signal.
- Internal access stays open: `scope._task_group.start_soon(...)` keeps
  working for the benefit of in-flight dispatches; child scopes of a
  draining parent can still `lift` / resolve cap-bridges via the
  parent's internal helpers (those don't go through the user-facing
  guarded methods). This is the "scope is draining but still functional
  for things already in motion" regime.
- Scope-owned resources that outlive drain (cap-bridge key leases in
  substage 6; the existing deep-fetch key leases — see follow-ups) are
  discarded in a registered token-end callback, **not** at `aclose`
  start. Mirrors how the memory store discards its `foreign_leases`
  key-lease clones in `_on_token_end`. This is the general rule for
  scope state that gates cross-scope bridging: alive while the token
  is alive, torn down synchronously once the token ends.

**Memory-store registration.** The store's parent set and the derived
lifetime's input set are two *separate* concepts:

- *Parent scopes* (`include=` in the normal constructor, or an analogous
  arg on `_with_derived_lifetime`): other `FxtrScope`s this scope is a
  child of. Their main-lease clones are passed to `register_token` as
  `parents`. This is how the memory store's inheritance / ancestor DAG
  gets built — same as today, same for unconditional and derived scopes.
- *Derived inputs* (`derived_leases=` on `_with_derived_lifetime`):
  arbitrary leases that the derived lifetime's output token is structurally
  tied to. They may or may not overlap with the scope's parent scopes'
  tokens; the memory store doesn't know about them.

So a derived scope's store row has `parents = [clone(p._main_lease) for
p in parent_scopes]`, completely independent of `derived_leases`. The
internal `_key_ull` is likewise not a memory-store parent — its only
role is to gate the output token's drain on the derived lifetime side.

**Weak-scope deletion.** `new_weak_child_scope`, `_share_token`,
`_weak_children`, `_close_weak_scope`, `_KeepAlive*`: all gone. Tests using
them are migrated to normal child scopes (most should work with no
structural change).

**Deep-fetch sources.** Unchanged API shape for now — they're still a flat
list on the scope. Whether to subsume them into the cap-bridge dict
introduced in substage 6 is an open follow-up (noted below).

**Acceptance test.** The "task 1 / task 2 / task 3" test from the sketch,
adapted to pure-scope primitives (no arena-over-CapIR yet).

**Checklist.**

- [x] Delete `_KeepAlive` / weak scopes; migrate callers + tests.
- [x] Add `aclose()` + wire the async CM to wrap it.
- [x] Add `mountable` flag + mountable-only task group.
- [x] Add internal `_with_derived_lifetime` constructor for derived
      scopes (independent of `mountable`).
- [x] Strip the keep-alive registration from `as_active_scope`; leave
      it as a bare contextvar-set contextmanager.
- [x] Task 1/2/3 scope-level acceptance test (exercises the derived
      constructor; can keep the mountable flag off at this stage).
      *Lives in `tests/test_entity_ref_scope/test_derived_scope_acceptance.py`.*
- [x] Update `CODE_OVERVIEW.md` + `entity_ref_scope.py` docstrings.
- [x] Promote the autouse lease-leak fixture from
      `tests/test_leases/conftest.py` to top-level
      `tests/conftest.py`. Substage 9 picks up the remaining
      pre-existing leaks marked `allow_lease_leak` (HTTP transport,
      one git race-path test).

**Deviations from the substage plan (all captured in `part3_plan.md`):**

- **Derived `aclose` does not wait on the key ULL.** The plan's first
  drafted shape did `await key_ull.aclose(); finally: await
  derived.aclose()`. With external key holders (e.g. acceptance-test
  task 3 hanging onto a key clone past scope close) that variant
  blocked scope teardown on the external holder. The shipped variant
  is `discard main + key; await derived.aclose()` — derived's input-
  end cascade ends the output token without waiting on the key, and
  the key drains naturally when its remaining holders release.
- **Parents validation moved into `_init_common`.** Bad-parent
  diagnostics now run before the scope mints its lifetime, so a
  failing constructor doesn't leave a half-baked memory-store row
  pinning external leases.
- **`_is_deeply_fetchable()` is lazy.** Deferred to first read; the
  unconditional constructor's auto-start path eagerly commits, but
  CM-required scopes commit on `__aenter__`. Removed the eager loop
  the plan called for in the CM.
- **`bare/aclose` sharp-edge documented.** A `try: ... finally: await
  scope.aclose()` shape can't surface the body's exception when the
  drain blocks; the docstring spells out three robust patterns
  (`async with` preferred; explicit `force_close` on the except path;
  `move_on_after` as a soft fallback).
- **Lease-leak fixture promotion is partial.** The fixture is
  promoted, but four files in experimental code (HTTP transport +
  one git race test) carry temporary `allow_lease_leak` marks with
  `TODO(stage8c-followup)` comments — the leaks are pre-existing,
  not introduced by this substage. Substage 9 cleans them up.

---

## Substage 4 — Pipelined dispatch leases

**Goal.** Extend the Cap-IR `IRHandle.dispatch` / `.request` path to accept
an `UnwrapForPipelining[Lease[B], K]` in the lease slot, so callers can
dispatch before the lease is concrete. Needed because substage 5's
`CapIRArena` wants to chain a converter-produced `Lease[Y]` into the
remote arena dispatch without a sequential round-trip, and substage 6's
scope cap-bridges need to produce leases for converter-bridged services
without making `lease_provider` async.

**The extension.**

- `IRHandle[B].dispatch(lease: Lease[B] | UnwrapForPipelining[Lease[B], K], request)`
  — only caller-visible change. `.request` inherits the same widening.
- Local impl (`IRHandleLocalImplBase`): the dispatch runner (already
  spawned in `_task_group`) first resolves the outer lease the same way
  `_unwrap_request_futures` resolves pipelined call args today, then
  proceeds with the concrete lease. The lease-unwrap step happens before
  grant validation on the underlying implementation's `_run_request_async`
  — i.e. the lease must resolve to a valid `Lease[B]` before the real
  call runs.
- Wire protocol: the `PushMessage`'s lease slot is already typed
  `Expression[Lease]`, and `AwaitFutureExpr[Lease]` is already a valid
  `Expression[T]` — no schema change, no JSON codec change, no protocol
  version bump. The evaluator is widened so `AwaitFutureExpr[Lease]`
  is accepted in the push message's lease position (today it only
  accepts `ImportedLeaseExpr` / `ExportedLeaseExpr`).
- Devaluator: when a `UnwrapForPipelining[Lease, ...]` appears as the
  outer dispatch lease on an outbound push, emit `AwaitFutureExpr[Lease]`.
  Mirrors the existing call-arg path.

**Error propagation.** If the outer-lease future rejects (e.g. the
upstream converter itself raised), the receiver can't evaluate the lease
`Expression`; this surfaces as an `IRException` on the dispatched
future's resolution, following the same path as any other
`AwaitFutureExpr` whose target future rejects.

**Acceptance tests.**

- Local: synthetic `IRHandle` + a local future-of-lease; dispatch
  pipelined, verify the runner waits for the lease before invoking
  `_run_request_async`; verify `LeaseBrokenError` / lease-rejected paths.
- Transport: round-trip push with `AwaitFutureExpr[Lease]` in the outer
  lease slot; verify the peer awaits the future, validates the resulting
  lease, and invokes the dispatch; verify `PullMessage` / `ResolveMessage`
  / `UnpullMessage` accounting stays correct (the pulled future is
  referenced by an outbound `AwaitFutureExpr` in our export table, which
  the existing resolve-obligation accounting already covers).
- Return lease is not pipelined (it's locally minted synchronously by
  the dispatcher); confirmed in a test.

**Checklist.**

- [x] Widen `IRHandle.dispatch` / `.request` signature; update
      `IRHandleLocalImplBase` runner to unwrap the outer lease.
- [x] Widen evaluator to accept `AwaitFutureExpr[Lease]` in the push
      lease slot; widen devaluator to emit same on outbound
      pipelined-lease pushes.
- [x] Local + transport tests per above.
- [x] Update `leases/LOCAL_CODE_OVERVIEW.md` and `rpc/LOCAL_CODE_OVERVIEW.md`.

This substage doesn't introduce a new `LifetimeConverter` variant — that
lives with its first client in substage 5.

**Deviations from the substage plan (all captured in `part4_plan.md`):**

- The widening also reached `NullMainInterface` (in `lowering_null_main.py`),
  which subclasses `IRHandle` directly — its discard path was updated to
  distinguish concrete vs pipelined forms.
- The runner's pre-`_run_request_async` cleanup also fixes a pre-existing
  leak: today, if `_unwrap_request_futures` raised (e.g. a
  `WaitAndIgnoreFutureThen.wait_for` future rejected), the runner's
  `taken_request` leases leaked. The substage-4 try/except covers both
  failure modes uniformly.
- The "lease-source revoke" local test constructs the future-of-lease
  manually (`LocalPendingResultBox` + `IRFutureLocalImpl` against a
  caller-owned ULL) so the source lifetime is externally revokable;
  `spawn_ir_future_in_task_group` would have buried it in an internal
  ULL we don't have a handle to. The dispatched future rejects with
  `IRException(category="LeaseBrokenError")` as expected.
- Inbound `_eval_lease_or_pipelined` mirrors `eval_as_call_arg`'s
  `AwaitFutureExpr` *and* `HandleRequestExpr` branches. Only the
  `AwaitFutureExpr` form is currently emitted on the wire (substage 5's
  `CapIRArena` is the first emitter); the `HandleRequestExpr` branch is
  there for consistency but lacks dedicated end-to-end coverage.

---

## Substage 5 — Arena-over-CapIR

**Goal.** Two new classes let arenas cross the Cap-IR boundary so fxtr data
flows through RPC via the same machinery as live capabilities. Builds on
substage 4's pipelined dispatch leases.

**Server side: `ArenaIRHandleAdapter[B]`.**

- Implements `IRHandle[B]` wrapping a `ReadableArena[B]`.
- Token identity: `handle.token is arena.token`.
- Request shape is modelled on the HTTP / SQLite-arena client protocols.
  Methods (expressed as `Call(method, arg)`):
  - `load_flat(node_id)` → flat bytes + deps + closed precomp map (IRValue-shaped).
  - `unwrap_provenance(node_id)` → provenance unwrap chain.
  - `abandon(node_id)` → drop the node on the server side.
  - `copy_to_new_arena(node_ids)` → fork a sub-arena and return it as a
    nested `ArenaIRHandleAdapter` (with the same token). Matches today's
    `MemoryArena.copy_to_new_arena` semantics — a same-token fork, no
    new lifetime introduced. The client-side `CapIRArena` returned from a
    fork shares its parent's `B_Z` / `B_Y` configuration; callers reuse
    existing leases unchanged.
- Lease handling: each request takes a `Lease[B]` through normal
  `IRHandle.request` / `.dispatch` semantics.

**Client side: `CapIRArena[B_Z, B_X, B_Y, B_K]`.**

- Implements `ReadableArena[B_Z]` wrapping a remote
  `IRHandle[B_Y]` ("remote arena handle").
- Parameters:
  - `B_Z` — this arena's own token. Either `B_Z == B_X` (no derivation) or
    `B_Z` is a pre-existing derived token that has `B_X` (and `B_K` if
    present) among its inputs.
  - `B_X` — input token the caller already has leases on.
  - `B_Y` — the remote arena's token.
  - `B_K` — optional key token, for converter-backed bridging.
- Carries the remote arena handle, optionally a converter handle
  (`IRHandle[B_K]` whose `Call("convert", Lease[B_X])` returns a fresh
  `Lease[B_Y]`) and the key lease.
- Arena methods use **substage-4 pipelined dispatch leases** to chain
  the converter call + the remote arena call in a single non-blocking
  step: dispatch the converter to get `(future_of_Y_lease, future_lease)`,
  then immediately dispatch the remote arena with
  `UnwrapForPipelining(future_of_Y_lease, future_lease)` in the lease
  slot. Both pushes go out before either resolves; the receiver
  pipelines the second on the first. No sequential round-trip.
- Single class; `convert_handle is None` selects the identity path
  (requires `B_X == B_Y`). The branching is folded into a private helper
  that builds the dispatch.
- Does **not** own any lifetime: the caller is responsible for the token
  lifetime backing `B_Z`.

**New type: `PipelinedLifetimeConverter[B_X, B_Y, B_K]`.**

Introduced here because the CapIRArena needs it and substage 6 consumes
it:

```
type PipelinedLifetimeConverter[B_X, B_Y, B_K] = Callable[
    [Lease[B_K], Lease[B_X]],
    Lease[B_Y] | UnwrapForPipelining[Lease[B_Y], Any],
]
```

Sync signature, pipelined-capable return. A CapIR-backed converter
(dispatch a converter `IRHandle` and wrap the resulting
`(future_of_lease, future_lease)` in `UnwrapForPipelining`) fits the
type; so does any today's sync `LifetimeConverter` (trivially, by never
returning the pipelined variant). The memory store's `LifetimeConverter`
is **unchanged** — it stays sync, produces a concrete `Lease[B_Y]`, and
is used only for local-arena bridges where sync is sufficient.

Scope cap-bridges (substage 6) accept `PipelinedLifetimeConverter`
as their one converter type — it subsumes sync converters via the
trivial-never-pipelined case.

**Deep-fetch source variant.** `lazily_attach_from_arena` with a `CapIRArena`
as `source` is the expected bridging pattern on the receiving side of RPC.

**Acceptance test.** The full sketch test: task 1 creates an arena and sends
its handle to task 2; task 2 creates a derived scope + `CapIRArena` over the
task-1 handle, stores new entities, sends a new arena + converter to tasks
1 and 3; task 1 binds the returned data back into its original scope via a
second `CapIRArena`; task 3 combines task 1 and task 2 leases to reconstruct
a usable view and loads through it. All bridging happens through
`IRValue` (`IRHandle` + lease) pairs — no live Python object references
escape between tasks. See the sketch file, and the related test from
substage 3, for details.

**Checklist.**

- [x] `ArenaIRHandleAdapter` with the full read / abandon / fork methods.
- [x] `CapIRArena` with all four token-role configurations, using
      substage-4 pipelined dispatch leases internally.
- [x] `PipelinedLifetimeConverter` type alias + converter-wrapping
      helper that turns an `IRHandle`-backed converter into a
      `PipelinedLifetimeConverter`.
- [x] Acceptance test + smaller unit tests for each dispatch path.
- [x] Update `CODE_OVERVIEW.md`.

**Deviations from the substage plan (all captured in `part5_plan.md`):**

- Test files live at the top-level `tests/` directory
  (`tests/test_cap_ir_arena.py` + `tests/test_cap_ir_arena_unit.py`)
  rather than under `tests/test_data/`, matching the existing
  convention of `tests/test_memory_arena.py`.
- The full task-1/task-2/task-3 acceptance test
  (`TestThreeTaskAcceptanceCapIR.test_three_tasks_drain_in_lockstep`)
  mirrors substage 3's `test_derived_scope_acceptance.py` exactly,
  asserting the same four cascade predictions. The cross-task
  arena handoffs flow through `ArenaIRHandleAdapter` / `CapIRArena`
  (server / client respectively), and the converter handoff flows
  through an `IRHandle[K]` (`_ExchangeForwardConverter`) that wraps
  `DerivedLocalLifetime.exchange_forward` on the issuer side. The
  receiver-side memory-store converters are local-only sync
  closures: in task 1's case-2 attach the CapIRArena has its own
  z_token D = `derive({A, B})` (a fresh local derived) and the
  memory-store converter is `(B, A) -> Lease[D]`; the
  D-to-C translation happens inside `CapIRArena._make_y_lease`
  via the IR-converter dispatch (substage-4 pipelined). In
  task 3's case-1 attach the IR-converter is dispatched once
  synchronously to mint a `Lease[scope2_token]`, which then drives
  a mode-1 CapIRArena under the default converter.
- `CapIRArena` is generic only on the user-visible `B_Z` brand
  (`class CapIRArena[B_Z: LifetimeBrand]`), not on B_X / B_Y / B_K.
  The internal token / handle / converter relationships are
  expressed at the field level via `LifetimeToken[Any]` (with
  invariants documented as comments on the fields and validated at
  construction time), and at the *constructor* level via
  three precisely-typed classmethod factories whose own type
  parameters capture the brand relationships of each mode:
  - `direct[B](*, token: LifetimeToken[B], remote: IRHandle[B] | …)`
    — Z = X = Y = B; the shared brand catches mismatched remotes.
  - `derived[OutBrand: LocalLifetimeBrand, XBrand: LifetimeBrand](
    *, derived: DerivedLocalLifetime[OutBrand],
    x_token: LifetimeToken[XBrand],
    remote: IRHandle[XBrand] | …)` — Y = X via the shared XBrand.
  - `with_converter[OutBrand: LocalLifetimeBrand, XBrand,
    YBrand, KBrand](*, derived: DerivedLocalLifetime[OutBrand],
    x_token: LifetimeToken[XBrand], k_token: LifetimeToken[KBrand],
    remote: IRHandle[YBrand] | …,
    converter: IRHandle[KBrand], …)` — converter and k_token share
    KBrand, catching wrong-brand converter handles.
  Runtime checks in `__init__` (`x_token in derived.inputs`,
  `k_token in derived.inputs`, `remote.token is x_token` when no
  converter, etc.) cover the parts the type system can't express
  (input-set membership of a `DerivedLocalLifetime`).
  `DerivedLocalLifetime` exposes a public `.inputs` property to
  make this validation cheap.

---

## Substage 6 — `ServiceRef` fundamentals + `UntypedService`

**Goal.** Land the full mount / attach / resolve API, a cap-bridge dict on
scopes, a service-resolver type registry, and a dynamic untyped-service
implementation on top.

**Core types.**

```
@dataclass(frozen=True)
class ServiceRef[T]:
    handle: IRHandle[Any]
    target_type: type[T]
    # value equality on both fields

    async def resolve(self, scope: FxtrScope | None = None) -> T: ...
```

```
class FxtrServiceImpl[T](abc.ABC):
    @abstractmethod
    def _mount_as_fxtr_service(self, scope: FxtrScope) -> ServiceRef[T]: ...
```

**Mountable-scope methods.** Error on non-mountable scopes:

- `scope.mount_lowlevel_cap_as_service(handle: IRHandle[B], service_type: type[T]) -> ServiceRef[T]`
  where `handle.token is scope.main_token`. Adds a `_CapBridge.Own` entry for
  `handle.token` (bridge produces leases from `scope._main_lease`). Returns
  `ServiceRef(handle, service_type)`.
- `scope.attach_leased_lowlevel_cap(handle: IRHandle[B2], service_type: type[T], lease: Lease[K], converter: PipelinedLifetimeConverter[B, B2, K] | None) -> ServiceRef[T]`
  where `B` is the scope's token brand. `converter` is the substage-5
  `PipelinedLifetimeConverter` (any today's sync `LifetimeConverter`
  trivially satisfies it by never returning the pipelined variant).
  `converter=None` selects the identity path (requires `B == B2`). Adds
  a `_CapBridge.Foreign` entry storing `(lease, converter)`. Scope-owned
  key-lease clones are discarded in the scope's token-end callback (not
  at `aclose` start) — mirroring how the memory store discards its
  `foreign_leases` key-lease clones in `_on_token_end`. During the drain
  window (after `aclose` starts, before the token actually ends) the
  bridges stay live so that in-flight dispatches and child-scope
  resolves walking up through this scope can still produce leases.
  Returns the ref.
- `scope.mount(impl: FxtrServiceImpl[T]) -> ServiceRef[T]` — thin wrapper
  around `impl._mount_as_fxtr_service(self)`.

**Scope cap-bridge storage.**

```
@dataclass
class _CapBridge:
    class Own:   # handle.token is scope's own main token
        pass
    class Foreign:
        key_lease: Lease[Any]
        converter: PipelinedLifetimeConverter[Any, Any, Any] | None
          # None ⇒ identity (handle.token == key_lease.subject)

scope._cap_bridges: dict[LifetimeToken, _CapBridge]
```

Resolution walks `self` → `self.parents` → ... looking for a matching
bridge. On a match, the bridge produces a fresh lease (possibly
pipelined) by cloning the relevant key + calling the converter; in the
`Own` case it just clones `scope._main_lease`. The `lease_provider`
handed to service resolvers is
`Callable[[], Lease[B] | UnwrapForPipelining[Lease[B], Any]]`, sync,
re-fetching each time — the convention explicitly avoids persistent
lease storage. The dispatch path absorbs the pipelining (substage 4),
so callers of `lease_provider()` don't need to distinguish the two
cases at the call site; they just pass the result straight to
`handle.dispatch(...)`.

**Service-resolver registry.**

Instantiate `TypeAnnotationHandlerRegistry` directly (no new registry
abstraction); this is the same pattern already used by
`register_entity_load_handler`.

```
type LeaseProvider[B] = Callable[
    [], Lease[B] | UnwrapForPipelining[Lease[B], Any]
]

type ServiceResolver[T] = Callable[
    [FxtrScope, IRHandle[Any], LeaseProvider[Any], type[T]],
    Awaitable[T],
]

_SERVICE_RESOLVER_REGISTRY: TypeAnnotationHandlerRegistry[type, ServiceResolver[Any]] = ...

def register_service_resolver[T](for_type: type[T], resolver: ServiceResolver[T]) -> None:
    _SERVICE_RESOLVER_REGISTRY.register(for_type, resolver)
```

`ServiceRef.resolve(scope)` queries the registry on `target_type` and
invokes the matched resolver with `(scope, handle, lease_provider,
matched.subquery)`. Union / `NewType` / `TypeAliasType` / generic-alias
unwrapping all come for free from the shared registry.

**Untyped service.**

- `type UntypedService = Any`; default resolver registered to produce an
  `UntypedServiceProxy`.
- `FxtrServiceData` recursive type alias (see decisions above).

**`UntypedServiceProxy(scope, handle, lease_provider)`.**

- Defines `__getattr__(name) -> Callable[..., Awaitable[FxtrServiceData]]`.
  Each returned callable runs:
  1. Walk the positional `args` tuple and the `kwargs` mapping
     separately through an isinstance dispatch, producing:
     - `args: list[IRValue]` — positional args, each lowered into a
       primitive / tagged-ref tree (`EntityRef` / `FxtrID` →
       tagged bytes; `ServiceRef` → bare IRHandle ref, no lease).
     - `kwargs: dict[str, IRValue]` — keyword args, lowered the same way.
     - `fxtr_arena: IRHandle | None` — if any FxtrID appears in `args`
       or `kwargs`, fork a sub-arena off the home scope's main arena
       containing exactly those IDs and wrap with
       `ArenaIRHandleAdapter` (substage 5); otherwise None.
     - `leases: list[Lease]` — one fresh lease per distinct subject
       token referenced anywhere in the packet: the handle-ref tokens
       from `args` / `kwargs` plus `fxtr_arena.token` (if the arena is
       present). Dedup by `subject` identity via scope cap-bridge
       lookup. The receiver rebuilds a subject→lease map by walking
       the list and looks up the arena lease by `fxtr_arena.token`.
  2. Package the call arg per the locked-in call convention
     (`IRTagged("fxtr_call", {...})`).
  3. `await handle.request(lease_provider(), IRRequest.Call(name, packet))`.
     `lease_provider()` is sync; its result may be a concrete `Lease`
     or an `UnwrapForPipelining[Lease, ...]` (substage 4). Either way
     it gets passed straight through to the dispatch path.
  4. Receive the `fxtr_return` packet. On `IRException`, re-raise as a
     Python exception. Otherwise, walk `result` to collect the set of
     distinct tokens referenced (handle tokens plus `fxtr_arena.token`
     if present). For each, look up the matching `returned_refs` entry:
     - `converter is None` → install into the home scope via
       `scope.attach_leased_lowlevel_cap(handle, UntypedService,
       lease=key_lease, converter=None)`. The token is already one
       the caller knows about (typically one it sent in `leases`);
       attaching idempotently no-ops on duplicate tokens.
     - `converter is not None` → the converter IRHandle bridges a
       call-scope-local token into the caller's world. Wrap it into a
       `PipelinedLifetimeConverter` (substage 5): given
       `(key_lease: Lease[key], base: Lease[X])`, dispatch
       `converter.dispatch(key_lease_clone, Call("convert",
       {input_leases...}))` and synchronously return
       `UnwrapForPipelining(future, future_lease)` — so subsequent
       service dispatches through this bridge pipeline-forward without
       per-call round-trips. Install via
       `scope.attach_leased_lowlevel_cap(handle, UntypedService,
       lease=key_lease, converter=<that wrapper>)`. Same pattern for
       the `fxtr_arena`'s token, feeding into
       `lazily_attach_from_arena` on the home scope for returned
       `FxtrID`s. Note: `lazily_attach_from_arena` expects a sync
       `LifetimeConverter`; the `CapIRArena` wrapper is how the
       pipelining still happens downstream (the arena's internal
       dispatch uses pipelined leases, per substage 5).
  5. Return a FxtrServiceData tree where handles become
     `ServiceRef[UntypedService]` and FxtrIDs stay as such.
- All operations use the resolver's "home scope" (the one passed to
  `ServiceRef.resolve`); args whose backing state is not reachable in that
  scope raise immediately.

**`UntypedServiceImpl(FxtrServiceImpl[UntypedService])`.**

- `@service_method` module-level decorator tags methods. Default
  `_service_exposed_methods` walks the class's own methods + MRO collecting
  tagged names; subclasses can override.
- `_mount_as_fxtr_service(scope)`:
  - Build a concrete `IRHandleLocalImplBase` subclass whose
    `_run_request_async(lease, request)`:
    - Unpacks the `fxtr_call` tagged packet.
    - Starts a fresh child scope with *both* axes set: `mountable=True`
      (so it can host any re-mounted incoming arenas / services and has
      a task group) *and* constructed via `_with_derived_lifetime`
      with `derived_leases` drawn from the incoming `leases` list (one
      per distinct subject token) — binding the child scope's lifetime
      to the caller's input tokens. Includes the mount-scope as a
      parent. Sets it as active. Binds the `fxtr_arena` (if any) via
      `lazily_attach_from_arena`. Brings ServiceRef args across via
      `attach_leased_lowlevel_cap` on the child scope.
    - Lifts `args` / `kwargs` back up into user-facing `FxtrServiceData`:
      `FxtrID` → stays, `ServiceRef` → stays (already built).
    - Resolves the method via `_service_exposed_methods`, invokes it.
    - On success: lower the user's return value to `result: IRValue`
      (same lowering as args). Fork a new arena from the child scope
      containing all returned `FxtrID`s; wrap with
      `ArenaIRHandleAdapter` → `fxtr_arena`. Walk `result` +
      `fxtr_arena` to collect the set of distinct tokens referenced,
      then for each build one `ret_ref` entry:
      - If `token` predates the call (i.e. it appeared among the
        input `leases` subjects): `key_lease = <fresh lease on
        token>`, `converter = None`.
      - If `token` is local to the call scope (e.g. the child scope's
        own derived token, or a token of a service mounted *inside*
        the child scope during the call): `key_lease = <fresh lease
        on the relevant key token>`, `converter = <IRHandle
        implementing the reverse-exchange: dispatch with `key_lease`
        and a map of input-token leases, return a lease on `token`>`.
        The converter handle is itself mounted on the child scope and
        travels back over the wire; its lifetime is gated by the
        child scope's key token, so the caller can invoke it even
        after the child scope's output token has begun to drain.
      Assemble the return packet per the locked-in call-convention
      shape and return as the `IRValue` result.
    - On exception: return an `IRException` with category =
      `type(exc).__name__`; normal Cap-IR `RejectMessage` path takes
      over from there (no `fxtr_return` packet emitted).
    - Drains the child scope in a `finally`.
  - `scope.mount_lowlevel_cap_as_service(handle, UntypedService)`.

**Acceptance tests.** Mount + resolve roundtrip; fxtr data in and out;
service as argument to service; service returned from service (spawner
pattern); exception propagation; lease-drain across boundary;
mutually-referencing scopes (S1 hosts service A referencing S2; S2 hosts
service B referencing S1) — expected to drain cleanly via the high-level
API because the resulting lease graph is a DAG, not a cycle. A
deliberately-cycled low-level setup would get stuck and not drain; that's
not a supported configuration and not asserted.

**Checklist.**

- [x] Core types: `ServiceRef`, `FxtrServiceImpl`, scope methods,
      resolver registry.
- [x] Scope `_cap_bridges` + `_get_lease_for_token` walk.
- [x] `UntypedServiceProxy` + `UntypedServiceImpl` + `@service_method`.
- [x] Acceptance tests.
- [x] Update `CODE_OVERVIEW.md` + new `LOCAL_CODE_OVERVIEW.md` for the
      service module if it warrants one.

---

## Substage 7 — `FxtrScope` ↔ RPC bridge

**Goal.** Wrap `LeasedCapRPCConnection` with a scope layer so services
transparently cross processes.

**New surface.**

```
@asynccontextmanager
async def open_rpc_scope[T](
    stream: ObjectStream[RPCMessage],
    our_main_impl: FxtrServiceImpl[T],
    *,
    remote_service_type: type[T2] = UntypedService,
    name: str = "rpc_scope",
) -> tuple[FxtrScope, ServiceRef[T2]]: ...
```

Behaviour:

- Opens a fresh mountable scope (child of the active scope).
- Mounts `our_main_impl` on it (`ServiceRef[T]`), passing the mount's
  underlying `IRHandle` + lease to `LeasedCapRPCConnection` as
  `our_main_interface` / `our_main_lease`.
- Receives `their_main_interface` / `their_main_lease` from the connection
  and `attach_leased_lowlevel_cap`s it to the scope as `ServiceRef[T2]`.
- Yields `(scope, their_service_ref)`.
- On exit, drains the scope (which drains any outstanding cross-process
  leases), then closes the connection.

**Acceptance test.** Two tasks both construct a mountable impl, connect via
in-process `memory_object_stream`, each call the other, swap services in
args and returns, drain cleanly.

**Checklist.**

- [x] `open_rpc_scope` CM.
- [x] Acceptance test (symmetric two-party setup).
- [x] Docs.

---

## Substage 8 — Mashumaro-driven typed services

**Goal.** Typed-service protocols with per-method type conversion.

**Approach.**

- `@service_protocol` decorator on a `Protocol` class registers a resolver
  for that protocol type → `UncheckedTypedServiceProxy`. Method signatures
  are introspected (annotations), and mashumaro builds per-method arg and
  return converters on top of `FxtrServiceData`-lowering.
- `UncheckedTypedServiceImpl[T]` base class — same responsibilities as
  `UntypedServiceImpl`, but applies the inverse per-method conversion so
  impl methods receive typed args and return typed values.
- Restrictions: method-level type parameters not allowed. Class-level
  type parameters are supported via substitution.
- "Unchecked" because the protocol identity is not checked across sides.
  Both sides independently apply conversion; if they disagree, a runtime
  error surfaces as `IRException`. A `ServiceRef[T]` that round-trips
  `S1 → RPC → S2 → RPC → S1` carries only the IRHandle on the wire;
  `target_type` is re-attached per side based on the receiving code's
  annotation, so the type you get back is whatever S1's second-hop
  annotation said, not necessarily the originally-sent `T`. A future
  `@checked_service_protocol` decorator could add cross-side type
  identity at the cost of extra handshake traffic; deferred.
- Shared internals with substage 6: the underlying dispatch + packet pack /
  unpack + scope-bridging code is refactored so `UntypedServiceProxy` and
  `UncheckedTypedServiceProxy` share everything except the per-method
  conversion shim.

**Checklist.**

- [x] Refactor substage-6 internals to expose the right extension points.
      *Pulled `walk_service_methods` + `_SERVICE_METHOD_MARKER` into
      `service_cap_common`; both untyped and typed strategies share them.*
- [x] `@service_protocol` decorator + mashumaro integration for per-method
      arg / return conversion. *Renamed from
      "UncheckedTypedServiceImpl" to `TypedServiceImpl` since the
      decorator now supports `service_type_id=` / `service_type_validator=`
      gating.*
- [x] `TypedServiceProxy` + `TypedServiceImpl` (sibling of
      `SimpleAsyncServiceImpl` rather than child of it; share only the
      tagged-method convention).
- [x] Typed-service acceptance tests (roundtrip, generics on both Protocol
      and impl side, nested typed / untyped interop, service_type_id
      gating, registration errors).
- [x] Docs (LOCAL_CODE_OVERVIEW.md + CODE_OVERVIEW.md).

See `part8_plan.md` for full design notes; spike findings recorded
there confirmed the wrapper-dataclass + mashumaro-TypeVar-propagation
approach is viable.

---

## Substage 9 — Lease-leak ratchet cleanup

**Goal.** Fix the pre-existing lease leaks that the autouse
`_assert_no_lease_leaks` fixture started catching once it was promoted to
top-level scope (in substage 3), and remove the temporary
`allow_lease_leak` markers introduced to keep the suite green during
that promotion.

**Why a separate substage.** Substage 3 fixed the four leaks in
`fxtr2/data/memory_arena.py` that fell within its own scope (error-path
leaks in `register_token`, `MemoryArena.store`, and
`track_from_memory_arena`). The remaining leaks live in code substages
1–3 didn't touch — primarily the experimental HTTP transport and one
git-namespace race-condition test — so fixing them belongs in its own
ratchet-forward substage rather than expanding substage 3's blast
radius.

**Files currently marked `pytest.mark.allow_lease_leak`** (each with a
`TODO(stage8c-followup)` comment pointing at this substage):

- `tests/test_experimental/test_http/test_asgi_app.py` (module-level)
- `tests/test_experimental/test_http/test_client.py` (module-level)
- `tests/test_experimental/test_http/test_workspace_namespace.py`
  (module-level)
- `tests/test_git_store/test_git_namespace.py::TestGitNamespaceBackendRaceConditions::test_concurrent_pin_and_flush_same_tsv`
  (per-test)

**Approach.** For each marked file / test:

1. Run the test under the leak fixture, capture the leak warning's
   allocation traceback (the existing `_on_lease_leaked` handler already
   logs the alloc site when `LEASE_LEAK_TRACK_ALLOCATIONS` is set).
2. Trace the leaked clone to its source — typically a `lease.take()` /
   `lease.clone()` call on an error path that doesn't `discard` on
   raise, or a missing `with lease:` / `ExitStack.enter_context`. The
   memory_arena fixes from substage 3 (`register_token` + `store` +
   `track_from_memory_arena`) are the canonical pattern.
3. Fix the underlying leak in the implementation (HTTP client / asgi
   server / workspace namespace / git race path), not in the test.
4. Remove the `allow_lease_leak` mark and re-run the file to confirm
   no leak.

**Out of scope.** Any *new* leaks introduced by substages 4–8 are the
responsibility of the substage that introduces them and should be
fixed in-substage (the autouse leak fixture will catch them in CI).
Substage 9 is purely about the pre-existing leak inventory frozen at
the end of substage 3.

**Acceptance.** Full suite passes with zero `allow_lease_leak` marks
remaining outside the leak-detector self-tests (`tests/test_leases/
test_local_lease.py::TestLeakWarnings`, which intentionally leak a
lease to verify the warning path itself — the conftest fixture's
docstring explicitly carves out this case). The leak-free floor is
then permanently enforced for every other test in the suite.

**Checklist.**

- [x] Diagnose + fix the HTTP-transport leak (single shared site:
      `_compute_precomp_closure` in `fxtr2/experimental/http/asgi_app.py`
      didn't consume the lease it was passed, and two of the three
      handlers also leaked a held `lv = scope._main_lease.clone()`).
      Fix: `_compute_precomp_closure` now does `with lease.take() as
      lease:` per the consume-on-call convention; handlers either
      inline the clone or wrap it in a `with`-block. Same fix covers
      `test_asgi_app.py`, `test_client.py`, and
      `test_workspace_namespace.py`.
- [x] Diagnose + fix the git-namespace race-path leak.
      Fix was test-side: `test_concurrent_pin_and_flush_same_tsv`
      took `_ull.root_lease.take()` per iteration and only called
      `arena.close()` in the finally block — added the matching
      `root_lease.discard()`.
- [x] Remove all `allow_lease_leak` module-level pytestmarks and
      per-test marks introduced in substage 3.
- [x] Two further stale `# TODO: Fix lease leak issue` markers in
      `tests/test_memory_arena.py::TestTrackFromMemoryArena` (the
      `test_track_different_store_raises` /
      `test_track_untracked_source_raises` error-path tests) turned
      out to be already-fixed by substage 3's
      `track_from_memory_arena` early-return discards — the markers
      were just stale. Removed.
- [x] Confirm `grep -rn "allow_lease_leak" tests/` returns no test
      occurrences outside the leak-detector self-tests in
      `tests/test_leases/test_local_lease.py` (and the marker
      registration in `pyproject.toml` plus the fixture's own check
      in `tests/conftest.py`).
- [x] Full suite green (2188 passed).

---

## Open follow-ups

Deliberately punted now, to revisit during the relevant substage's detail plan:

- **`unmount` API.** Not in initial design. Services currently live for the
  scope's lifetime. This may be genuinely tricky to get right under the
  lease semantics (a service has handed out leases that may still be in
  flight, and there's no obvious "detach" point in the lease model), so
  it may not even be expressible without extending the lease system.
  Revisit if a real use case surfaces.
- **Deep-fetch sources unification with `_cap_bridges`.** Deferred. Keep
  `FxtrScope._deep_fetch_sources` and `_cap_bridges` as separate
  structures for now; revisit if they converge in use. Related: today's
  deep-fetch key-lease cleanup runs at scope-exit (pre-drain); substage 3
  should move it to the same token-end-callback trigger the new
  `_cap_bridges` cleanup uses, so both kinds of scope-owned foreign key
  leases follow the same "alive until the token ends" rule.
- **Global scope + root-scope handling under derived lifetimes.** The
  process-lifetime `GLOBAL_SCOPE` uses `_start_as_root_scope` + atexit
  today; substage 3's plan will confirm its drain path stays
  `UnconditionalLocalLifetime`-shaped (it has no parents, so no derived
  setup needed).
- **`ReadableArena.close()` awkwardness**: Today `close()` is lease-less, which means
  there's no way to authorise a close call across an `IRHandle`
  boundary. As a result, `CapIRArena` (substage 5) cannot
  deterministically close the server-side fork view created by
  `copy_to_new_arena` — it relies on `MemoryStore`'s existing
  `weakref.finalize` GC fallback (and the resulting INFO log
  `"Arena {key} was finalized while open!"`). An attempt was
  made to refactor `close` to take a lease, but this does not
  propagate well when `close` is used during cleanup. Possible
  alternative: remove the INFO logs now that lifetimes are
  primarily handled by tokens anyways.
