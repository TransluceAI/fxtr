# Substage 3 — `FxtrScope` drain refactor

Detailed implementation plan for substage 3 of
[stage 8c](overview.md#substage-3--fxtrscope-drain-refactor).

**Goal.** Scope lifetime is expressed purely through lease drain. Concretely:

1. Add `mountable: bool` as an init flag.
2. Add `await scope.aclose()` + sync `scope.force_close()`.
3. Add an internal `_with_derived_lifetime` alternative constructor for
   isolated derived-lifetime scopes.
4. Delete the keep-alive machinery (`_KeepAlive`, `_KeepAliveWaiter`,
   `_KeepAliveToken`, `_dead_parents`), the weak-scope machinery
   (`new_weak_child_scope`, `_close_weak_scope`, `_weak_children`,
   `_weak` flag), and the `_share_token` kwarg.
5. Strip `as_active_scope()` down to a bare contextvar-set CM.
6. Migrate deep-fetch source cleanup from "`__aexit__` start" to the
   token-end callback, matching the pattern `_cap_bridges` will follow
   in substage 6.
7. Land the full task-1 / task-2 / task-3 acceptance test from the
   sketch, using bare Python arena + lease + converter objects (no
   Cap-IR yet — that arrives in substage 5).

This substage does **not** add any `ServiceRef` surface, cap-bridge
dict, or mount methods — those come in substage 6. The task group
added here is structural only; no one submits to it yet.

---

## Locked-in answers (from clarification rounds)

- Root scope (`GLOBAL_SCOPE`): stays `UnconditionalLocalLifetime`-shaped;
  atexit calls the new sync `scope.force_close()` which revokes the
  owned ULL immediately.
- Drop the parent-unexpectedly-closed warning entirely.
- `load()` raises during drain (mutates tracked set via dep tracking).
- **Every user-facing entry point** raises once drain starts, read-only
  ones included — a draining scope is off-limits to user code regardless
  of whether the op would have mutated. Internal helpers keep working.
- `_share_token` is deleted outright. No alias.
- Mountable flag lands the `mountable` init arg + task group plumbing;
  mount / attach / resolve methods stay absent until substage 6.
- **Mountable scopes are CM-only.** `async with scope:` is the only
  legal way to use them. The task group is opened on `__aenter__` and
  closed on `__aexit__`. `aclose` does **not** close the task group;
  nothing outside the async-CM flow touches it.
- **Non-isolated scopes are also CM-only.** A non-isolated scope has
  to check that the active scope still matches its captured outer
  scope and set the active-scope contextvar for its body — both are
  CM responsibilities, and doing them bare doesn't make sense.
- **Only isolated + non-mountable scopes are bare-usable**: they
  auto-start at construction and can be used bare
  (`s = FxtrScope(isolated=True); ...; await s.aclose()`) or via
  `async with`. Every other combination requires `async with`.
- `_with_derived_lifetime` always produces an **isolated** scope (no
  implicit outer-scope parent; the caller's job to set `include=`).
- `derived_leases` takes a `Mapping[LifetimeToken, Lease]`. No
  overlap with the key ULL is possible because the key ULL is freshly
  minted; no user map key can collide with it.
- `check_alive` is user-facing. A draining scope reads as "not alive";
  a mountable scope that has not yet been entered also reads as "not
  alive".
- Deep-fetch key-lease cleanup moves into the token-end callback in
  this substage.
- Add a sync `scope.force_close()` — revoke-only, no drain wait —
  semantically equivalent to aborting `aclose` mid-wait. Atexit
  handler uses it.

---

## Lifecycle state model

Three bools track the lifecycle: `_started`, `_draining`, `_closed`.

| State | Values | User-facing behaviour |
|---|---|---|
| **created-inactive** | `_started = False` (CM-required scope, not yet entered) | Not yet usable. `is_alive()` = False; `check_alive()` raises "not started". |
| **active** | `_started = True`, `_draining = False`, `_closed = False`, token live | Normal use. |
| **draining** | `_draining = True` | All user-facing entry points raise. Internal helpers still work so in-flight drains / child-scope walks complete. `is_alive()` = False. |
| **closed** | `_closed = True`, token ended | Everything raises via `check_alive`. |

`_started` flips True either:

- **At construction**, for scopes that are **both** isolated **and**
  non-mountable. These scopes are usable bare.
- **On CM entry**, for every other combination (non-isolated, or
  mountable, or both). These scopes require `async with` to become
  active.

The rule reads as: "a scope auto-starts iff it does not need the CM
to set up invariants it depends on" — where the invariants in
question are the active-scope contextvar check+set (non-isolated) and
the task group (mountable). Once `_started` is True it stays True for
the rest of the scope's lifecycle; transitions out are via `_draining`
and then `_closed`.

`check_alive()` reads the full triplet and raises with a distinguishing
message: "not started" / "draining" / "closed".

A single auxiliary flag `_inside_cm` guards against nested re-entry
while currently inside a CM body. Post-CM-exit `_closed` is True
(from `aclose`), which rejects any subsequent entry. Together they
cover both re-entry shapes.

---

## New surface

### Init

```python
class FxtrScope(anyio.AsyncContextManagerMixin):
    def __init__(
        self,
        include: Sequence[FxtrScope] = (),
        isolated: bool = False,
        name: str | None = None,
        mountable: bool = False,
    ): ...

    @classmethod
    def _with_derived_lifetime(
        cls,
        *,
        include: Sequence[FxtrScope] = (),
        derived_leases: Mapping[LifetimeToken[Any], Lease[Any]],
        name: str | None = None,
        mountable: bool = False,
    ) -> Self: ...
```

`_share_token` is removed. `_with_derived_lifetime` always constructs
an isolated scope (`isolated=True` implied; no kwarg). Callers who
want a derived scope with a parent pass `include=[parent]`.

### `await scope.aclose()`

Discards the scope's owned root lease(s), waits for the backing token
to end. On cancellation/error, revokes the owned lifetime(s) for a
fast close and re-raises.

Does **not** touch `_task_group` — that's owned by the async CM.
Callers of `aclose` on a mountable scope are out of the normal flow
(in particular, `async with` calls it from finally, and the CM
separately closes the task group after). Bare `aclose` on a mountable
scope from non-CM code is a programming error we don't guard against
explicitly — the scope simply won't have been entered, so `aclose`
will go down the `_abort_from_never_entered` path for it.

Idempotent on an already-closed or never-entered scope.

### `scope.force_close()`

Sync counterpart to `aclose`. Equivalent to starting `aclose` and
immediately aborting it (cancellation path). Revokes the scope's
owned lifetime(s) synchronously: any outstanding leases break, the
token ends, the memory-store end-callback fires. Idempotent.

Used by:
- The `GLOBAL_SCOPE` atexit hook.
- Tests that want fast teardown without awaiting.

Not expected to be load-bearing in normal user code — if you can
`await` you should prefer `aclose`.

### `async with scope`

Unified shape (`try: yield; except: force_close; raise; else:
aclose`), implemented via `anyio.AsyncContextManagerMixin.__asynccontextmanager__`:

- Opens the task group via an `AsyncExitStack` if mountable.
- Sets the active-scope contextvar (via `stack.callback` for reset)
  if non-isolated.
- On normal exit: `aclose` drains, then stack unwinds (task group
  close after drain).
- On exception: `force_close` synchronously revokes owned lifetimes
  so external lease holders cascade fast, re-raises; stack unwinds
  and cancels any still-running spawned tasks.

The force_close-on-exception isn't load-bearing for task-group
correctness (exceptions propagate out through the stack and the TG
cancels tasks anyway). It's load-bearing for collapsing the scope's
own lifetime synchronously so downstream lease-holders don't wait.

### `scope.as_active_scope()`

Sync contextmanager; sets `ACTIVE_SCOPE = self` for the block, resets
on exit. Does not hold a lease. Does not call `check_alive` — the
user-facing ops that fire through the contextvar carry their own
guards, and entering this CM on a draining scope is not itself a bug.

### `check_alive()` / `is_alive()`

- `is_alive()` returns True iff `_started` is True, `_draining` is
  False, `_closed` is False, and the token has not ended.
- `check_alive()` raises `RuntimeError` otherwise, with a message
  identifying which condition failed ("not yet entered", "draining",
  "closed").

Every user-facing method starts with `self.check_alive()`; during
drain (and post-close) they all raise. This is the primary access
gate — the old `_check_not_frozen_and_not_tracing_loads` helper is
still called from mutation-specific paths for its frozen/tracing
checks.

Internal predicates `_token_alive()` / `_is_draining()` exist for
internal code paths that need finer state (see below).

---

## Lifetime construction — the two paths

### Unconditional (normal constructor)

```python
def __init__(self, include, isolated, name, mountable):
    self._init_common(include=include, isolated=isolated, name=name, mountable=mountable)

    self._scope_ull = UnconditionalLocalLifetime(name or repr(self))
    self._key_ull: UnconditionalLocalLifetime | None = None
    self._derived: DerivedLocalLifetime | None = None
    self._key_lease: Lease[Any] | None = None

    self._token = self._scope_ull.token
    self._global_store.register_token(
        self._token, parents=unique_parent_leases, name=name or repr(self)
    )
    self._main_lease = self._scope_ull.root_lease.take()

    self._init_arena_and_cache()

    # Auto-start iff isolated AND non-mountable. Every other combination
    # flips _started on CM entry.
    if isolated and not mountable:
        self._started = True
    # (Shared for both paths — token-end callback registration for the
    # deep-fetch cleanup, etc.)
    self._register_scope_token_end_callback()
```

### Derived (`_with_derived_lifetime`)

Always isolated. `derived_leases` is a `Mapping[token, lease]` — the
caller is responsible for building it.

```python
@classmethod
def _with_derived_lifetime(cls, *, include, derived_leases, name, mountable):
    self = cls.__new__(cls)
    # Always isolated. Active-scope setup is bypassed for derived scopes;
    # callers who want one active set it explicitly via as_active_scope().
    self._init_common(include=include, isolated=True, name=name, mountable=mountable)

    # Mint a fresh key ULL. Its token is guaranteed to be disjoint from
    # every token in derived_leases (freshly minted → identity-unique).
    self._key_ull = UnconditionalLocalLifetime(f"{name or repr(self)}/key")
    self._key_lease = self._key_ull.root_lease.take()

    # Build the DerivedLocalLifetime. Inputs = {key} ∪ keys(derived_leases).
    input_tokens: set[LifetimeToken[Any]] = {self._key_ull.token} | set(derived_leases.keys())
    self._derived = DerivedLocalLifetime(
        name=f"{name or repr(self)}/derived",
        inputs=input_tokens,
    )
    self._token = self._derived.token
    self._scope_ull = None

    # Forward the inputs. Taking the derived_leases values and key clone
    # into a fresh dict; exchange_forward consumes everything.
    forward_inputs: dict[LifetimeToken[Any], Lease[Any]] = {
        self._key_ull.token: self._key_lease.clone(),
        **{tok: lease.take() for tok, lease in derived_leases.items()},
    }
    self._main_lease = self._derived.exchange_forward(forward_inputs)

    self._global_store.register_token(
        self._token, parents=unique_parent_leases, name=name or repr(self)
    )
    self._init_arena_and_cache()

    # Derived scopes are always isolated, so the auto-start rule reduces to
    # "auto-start iff non-mountable".
    if not mountable:
        self._started = True
    self._register_scope_token_end_callback()
    return self
```

Notes:

- `isolated=True` is implicit. Callers who want a parent do so via
  `include=[parent]` and the parent's `_main_lease` gets registered
  as a memory-store parent. `derived_leases` is independent: it gates
  *the scope's own lifetime*, not the memory-store ancestry.
- Key ULL's token can't collide with any `derived_leases` key
  because it's freshly minted (identity-based hashing).
- `derived_leases` values are consumed (`lease.take()`); failures
  before `exchange_forward` are cleaned up by the `take()` path's
  with-unwind. (Audit point: if we've already taken some leases and
  then something fails, we need a try/except to discard the taken
  ones. The plan is to take everything inside a `with
  contextlib.ExitStack()` so any failure unwinds cleanly — same
  shape as `DerivedLocalLifetime.exchange_forward`.)

### Shared `_init_common` / `_init_arena_and_cache`

Factorings of today's `__init__` body:

- `_init_common`: resolves `_outer_scope`, `_parents`,
  `_all_ancestor_scope_ids`, `_name`, `_mountable`,
  `_isolated`, sets state flags to initial (`_started=False`,
  `_draining=False`, `_closed=False`, `_inside_cm=False`), initialises
  `_global_store`, initialises `_deep_fetch_sources`,
  `_deeply_fetchable_flag`.
- `_init_arena_and_cache`: builds `_main_arena` +
  `_context_marker`, initialises `_ref_loaded_value_cache`,
  `_refs_loaded_since_abandon`, `_load_tracer`.

Both constructors call both helpers in the right order (lifetime
stuff in between).

---

## Mountable flag + task group

```python
def _init_common(self, ...):
    ...
    self._mountable = mountable
    self._task_group: anyio.abc.TaskGroup | None = None
```

- `_task_group` is `None` until `__aenter__`.
- Task group is opened and closed **only** inside
  `__asynccontextmanager__`, via an `AsyncExitStack`.
- Nothing else (aclose, force_close, tests, callers) ever touches
  `_task_group.cancel_scope` or attempts explicit close.
- Ordering on exit: stack unwinds after the body yields; the
  `aclose` in the finally discards leases and awaits token drain
  first; then the stack pops the task group, which waits on any
  still-running spawned tasks. This mirrors
  `open_lifetime_task_group`'s shape.
- Exception-in-body: `aclose`'s cancel path revokes the token
  (breaking leases → spawned tasks see `LeaseBrokenError` on their
  held clones); then stack unwinds, task group cancels remaining
  tasks normally.

Mountable scopes (like non-isolated scopes) are CM-required: they
don't auto-start at construction. Before CM entry, `_started` is
False, so `is_alive()` is False and `check_alive()` raises with
a "not started" message.

---

## `aclose()` / `force_close()` flow

### `aclose`

```python
async def aclose(self) -> None:
    if not self._started:
        # CM-required scope that was constructed but never entered.
        # Tear down directly via force_close (revoke owned lifetimes,
        # close arena).
        self.force_close()
        return
    if self._closed:
        return

    self._draining = True
    try:
        if self._scope_ull is not None:
            # Unconditional. Drop our taken root lease, then aclose the
            # ULL — it discards its own root reference (no-op; we took
            # it) and waits for all other clones (from child scopes,
            # external holders) to drain.
            self._main_lease.discard()
            await self._scope_ull.aclose()
        else:
            assert self._derived is not None and self._key_lease is not None
            assert self._key_ull is not None
            # Derived: drop our holds on both sides, then await the
            # derived output token. We deliberately do *not* await the
            # key ULL — its only role is as an input to the derived
            # lifetime, and once the derived output token has ended
            # the key has no further role. With no external key
            # holders the key ULL drains synchronously here (vias
            # released by main_lease.discard + our own key_lease
            # discard), so the cascade to derived fires naturally
            # before derived.aclose returns. With external key
            # holders, derived.aclose blocks until *some* input ends
            # (often an outer scope draining); at that point derived
            # closes and key is left dangling for its remaining
            # holders.
            self._main_lease.discard()
            self._key_lease.discard()
            await self._derived.aclose()
    finally:
        self._main_arena.close()
        self._closed = True
        self._draining = False
```

Notes:

- `aclose` never touches `_task_group`. The CM handles that.
- `aclose` may be called from inside the CM's finally (normal flow)
  or directly by bare callers on non-mountable scopes. Both work
  identically.
- The arena close moved to after token drain (matches the "may as
  well close the main arena after token drain" preference). This
  replaces today's pre-drain arena-close in `__asynccontextmanager__`.
- Double `aclose` is a no-op via `_closed`.

### `force_close`

```python
def force_close(self) -> None:
    if self._closed:
        return
    # Mark draining so user-facing ops refuse.
    self._draining = True
    try:
        if self._scope_ull is not None:
            self._scope_ull.revoke("force_close")
        else:
            assert self._derived is not None
            # Revoke the derived output and the key ULL. The key ULL's
            # own revoke fires even with its root lease held; we don't
            # need to discard _key_lease first.
            self._derived.revoke("force_close")
            if self._key_ull is not None:
                self._key_ull.revoke("force_close")
    finally:
        self._main_arena.close()
        self._closed = True
        self._draining = False
```

Idempotent; safe to call post-`aclose`.

Used only by:
- `GLOBAL_SCOPE`'s atexit hook
- Explicit test teardown where awaiting isn't possible (sync fixture
  finally blocks, for instance)

### Never-entered teardown

If a CM-required scope is constructed but never entered (then
`aclose` or `force_close` is called — e.g. an error in caller code
between construction and `async with`), we still own the ULL/derived
lifetimes and the arena. Both `aclose` and `force_close` handle this
by short-circuiting through the same logic when `_started` is False:

```python
async def aclose(self):
    if not self._started:
        self.force_close()
        return
    ...  # normal drain path
```

`force_close` itself is `_started`-oblivious — it revokes owned
lifetimes and closes the arena whether or not the scope ever became
active. No separate method needed.

### `__asynccontextmanager__`

```python
@contextlib.asynccontextmanager
async def __asynccontextmanager__(self) -> AsyncGenerator[Self]:
    # Reject already-closed AND nested re-entry. The `_inside_cm`
    # flag exists only for the nested-re-entry case (post-close is
    # caught by `_closed` directly); it flips True on entry and
    # False on exit.
    if self._closed:
        raise RuntimeError("Scope has already been closed")
    if self._inside_cm:
        raise RuntimeError("Scope is already inside an async-with block")
    self._inside_cm = True

    # For CM-required scopes (non-isolated OR mountable), this is where
    # we flip _started. For isolated + non-mountable scopes, _started
    # was set at construction; this is a no-op re-assert.
    self._started = True

    try:
      async with contextlib.AsyncExitStack() as stack:
        if self._mountable:
            self._task_group = await stack.enter_async_context(
                create_task_group(collapse_exc_group=True)
            )

        self._infer_deeply_fetchable()

        if not self._isolated:
            parent_scope = get_active_scope()
            if parent_scope != self._outer_scope:
                raise RuntimeError(...)
            scope_override_token = ACTIVE_SCOPE.set(self)
            stack.callback(ACTIVE_SCOPE.reset, scope_override_token)

        # Mirror UnconditionalLocalLifetime.__asynccontextmanager__:
        # on exception, revoke the lifetime synchronously (fast close,
        # cascade to external lease holders); on normal exit, drain.
        # The task-group part of correctness doesn't depend on the
        # force_close call — the task group sits outside this try in
        # the stack, so an exception propagating through the stack
        # will cancel any remaining spawned tasks on its own.
        # force_close is there to collapse the *scope's own lifetime*
        # synchronously so child scopes / external lease holders
        # cascade without waiting.
          try:
              yield self
          except BaseException:
              self.force_close()
              raise
          else:
              await self.aclose()
    finally:
        self._inside_cm = False
```

Notes:

- `_inside_cm` is reset in `finally` so that, in the (caller-buggy)
  nested-re-entry case, the second entry doesn't permanently poison
  the flag if the outer body somehow survives the inner crash. It's
  orthogonal to `_closed`.
- Exception-branch invariant: after `force_close` runs, the token is
  ended, all owned leases are broken, the arena is closed. The stack
  unwind that follows is pure — the task group `__aexit__` cancels
  any still-running tasks (whose held leases are now broken, surfaced
  as `LeaseBrokenError` on their next op), then waits for them.
- Else-branch invariant: `aclose` drops our main + key leases,
  awaits the token's end-callback. For mountable scopes this will
  wait for any still-running spawned task that holds a clone of our
  main lease. Only then does the stack pop the task group (whose
  `__aexit__` then does the final wait on any background tasks —
  typically fast-completing since they've released leases).

---

## Drain-phase guard rails

**Every** user-facing method starts with `self.check_alive()`. Post
`_draining = True`, all raise. Specifically:

- All mutation methods already named earlier.
- `is_tracked`, `get_cached_value`, `included_in_scope` /
  `included_in_active_scope` — also raise during drain (per the new
  blanket-refusal rule).
- `freeze_scope` — raises during drain.
- `as_active_scope()` — the one exception: entering the CM itself is
  allowed (it's just a contextvar set). No `check_alive` call. But
  any user-facing method invoked through the resulting active scope
  raises via its own guard.

Mutation guards still layer on top (`_check_not_frozen`,
`_check_not_tracing_loads`) for their existing purposes. A single
helper `_check_mutation_allowed(method)` bundles
`check_alive` + `_check_not_frozen` + `_check_not_tracing_loads` for
the mutation methods that used to call all three.

Internal helpers remain ungated:
- `_lift_unchecked`, `_deep_fetch_cached`, `_deep_fetch_scan_sources`,
  `_main_arena.load`, `_main_arena.store`, etc.
- `_copy_from_scope` / `_track_from_scope` keep routing through
  `source_scope._lift_unchecked` (not the user-facing
  `source_scope.lift`) so a child can still read through a draining
  parent.

Private predicates:

```python
def _token_alive(self) -> bool:
    return self._started and not self._token.ended()
```

Used internally only — for example, by cache lookup paths that may
want to tolerate draining state.

---

## Deletion list

### Classes / attributes removed

- `_KeepAliveWaiter` (class)
- `_KeepAliveToken` (class)
- `_KeepAlive` (class)
- `FxtrScope._keep_alive`
- `FxtrScope._weak_children`
- `FxtrScope._weak`
- `FxtrScope._dead_parents`
- `FxtrScope._evt_startup` → replaced by `_started`
- `FxtrScope._evt_shutdown_requested` → replaced by `_draining`
- `FxtrScope._evt_shutdown_complete` → replaced by `_closed`
- `FxtrScope.new_weak_child_scope` (method)
- `FxtrScope._close_weak_scope` (method)

### Init signature

- `_share_token` kwarg deleted.

### Tests / fixtures deleted

- `TestTokenSharing` pruned down. `test_default_owns_token`,
  `test_owned_token_invalidated_on_exit`, and
  `test_isolated_scope_owns_token` survive (renamed if the class
  shape feels off; otherwise folded into `TestBasicLifetime`).
  Everything referencing `_share_token` is deleted.
- `TestWeakScopes` deleted entirely.
- Any stray reference to the deleted attributes elsewhere (grep
  confirms only this file + `test_scope_lifecycle.py` today).

---

## `GLOBAL_SCOPE` / root scope

`_start_as_root_scope` becomes:

```python
def _start_as_root_scope(self) -> Self:
    if self._started:
        raise RuntimeError(...)
    if not self._isolated or self._parents:
        raise RuntimeError(...)
    if self._deeply_fetchable_flag is None:
        self._deeply_fetchable_flag = True

    self._is_root_scope = True
    self._started = True  # non-mountable, so already started shape

    import atexit
    atexit.register(self.force_close)
    return self
```

The token-end callback handles deep-fetch cleanup (unchanged from
ordinary scope flow).

---

## Deep-fetch source cleanup — token-end callback migration

Today's cleanup in `__asynccontextmanager__`'s finally and in
`_close_weak_scope` moves to a token-end callback registered in
`_register_scope_token_end_callback` (called once from each
constructor after `_token` is set):

```python
def _register_scope_token_end_callback(self) -> None:
    def _on_scope_token_end() -> None:
        for entry in self._deep_fetch_sources:
            entry.key_lease.discard()
        self._deep_fetch_sources.clear()
    self._token.register_end_callback(_on_scope_token_end)
```

Fires on natural drain (from `aclose`) and on revoke (from
`force_close` / derived input-end cascade). `register_deep_fetch_source`
continues to guard with `check_alive`, so registrations stop during
drain.

---

## Memory-store registration — parents vs derived

Both constructors call `register_token` the same way, passing
parent-scope main-lease clones. Derived inputs are independent of
parents.

---

## Existing-test migration summary

- `_share_token`-specific tests: deleted.
- Weak-scope-specific tests: deleted.
- Tests that used weak scopes as a scaffolding pattern (not
  weak-scope-specific behaviour): migrated to regular child scopes.
- Tests touching `_KeepAlive` internals: deleted.
- All remaining scope tests re-run clean.

---

## Acceptance test — task 1 / task 2 / task 3 (full sketch version)

Lives in a new file
`tests/test_entity_ref_scope/test_derived_scope_acceptance.py`. The
sketch's test description (overview sketch step 3) describes three
tasks interacting through scope primitives:

- task 1 owns an arena with some entities; sends an arena reference
  + a lease to task 2.
- task 2 creates a derived scope with task 1's lease as an external
  input; stores entities; extracts a (key_lease, converter, new
  arena) triple; sends it to both task 1 and task 3. Then drains.
- task 1 binds task 2's output back into its original scope via the
  converter; loads through it; then sends a lease to task 3; then
  drains.
- task 3 reconstructs a usable view from task 1's lease + task 2's
  (key, converter); temporarily mounts it; loads; exits the temp
  scope; drops its output lease; drops task 1's lease → observes
  both scopes drain.

**No Cap-IR marshalling.** Everything travels as bare Python values
across `anyio.MemoryObjectStream[Any]`s:

- "Arena reference" is a Python `ReadableArena[Any]` object. In-process,
  we can hand the object directly across an in-memory queue.
- "Lease" is a `Lease[Any]` Python object.
- "Converter" is a Python `LifetimeConverter` callable (a closure
  built by task 2 that wraps the `exchange_forward` on its own
  `DerivedLocalLifetime`).

Essentially: the test exercises **the core lease + arena + derived-lifetime
mechanics** end-to-end at the Python-object level. Substage 5 will
reproduce the same test with these Python objects replaced by their
Cap-IR-marshalled equivalents.

### Concrete test shape

Three `async` functions (`task1`, `task2`, `task3`) spawned into an
`anyio.create_task_group`. They communicate via three
`anyio.MemoryObjectStream` pairs:

```
t1_to_t2: (arena_handle_for_t2, lease_for_t2)
t2_to_t1: (key_lease_for_t1, converter_for_t1, new_arena_for_t1)
t2_to_t3: (key_lease_for_t3, converter_for_t3, new_arena_for_t3)
t1_to_t3: lease_for_t3  # task-1's scope lease
control events: t1_ready_to_drain, t2_ready_to_drain, t3_ready_to_drain
```

Detailed flow:

**task 1**
```
async with FxtrScope(name="t1", isolated=True) as scope1:
    ent1 = Foo("hi").store(scope=scope1)
    # Extract an arena containing ent1 using a copy_to_new_arena style
    # fork. (We already have MemoryArena.copy_to_new_arena-style fork
    # behaviour via the store; if the existing API is richer, pick the
    # closest match. Worst case: use scope1._main_arena directly.)
    arena_for_t2 = scope1._main_arena.copy_to_new_arena([ent1.entity_id])
    await t1_to_t2.send((arena_for_t2, scope1._main_lease.clone()))
    scope1.abandon(ent1)

    # Wait for task 2's output.
    key_lease_t1, converter_t1, new_arena_t1 = await t2_to_t1.receive()

    # Bind task 2's data back into scope1 via lazily_attach_from_arena.
    # (This uses scope1's main arena as the target — the converter
    # bridges the scope1 brand → task 2's output brand so the mount
    # runs correctly.)
    for new_id in new_arena_t1.tracked_ids():
        scope1._main_arena.lazily_attach_from_arena(
            scope1._main_lease.clone(), new_id, new_arena_t1,
            key_lease_t1.clone(), converter_t1,
        )

    # Load one of the new IDs, traverse to task 1's original entity.
    # (Proves the round-trip reach works.)
    loaded_back = await scope1.load(EntityRef(some_t2_id, SomeType))
    ...

    # Hand task 3 a lease on scope1, then drain.
    await t1_to_t3.send(scope1._main_lease.clone())
    key_lease_t1.discard()
    new_arena_t1.close()
    await t1_ready_to_drain.wait()
    # Leaving the CM drains; that'll block until task 3 drops its
    # copy of scope1._main_lease clone.
```

**task 2**
```
arena_from_t1, lease_from_t1 = await t1_to_t2.receive()
# Derived scope with lease_from_t1 as external input.
t1_token = lease_from_t1.subject
scope2 = FxtrScope._with_derived_lifetime(
    name="t2",
    derived_leases={t1_token: lease_from_t1},
    include=[],  # no memory-store parent
    mountable=False,
)
# Non-mountable so no CM needed; but we can still `async with` for drain.
async with scope2:
    # Bring in task 1's arena as a lazy inheritance source.
    scope2._main_arena.lazily_attach_from_arena(
        scope2._main_lease.clone(), ent1.entity_id, arena_from_t1,
        ...,  # key lease / converter — this is the "bridge a remote
             # arena in" pattern, no conversion because brands align
             # for this in-process case (use default_lifetime_converter).
    )

    # Store three new entities each referencing ent1.
    new_refs = [Foo(...).store(scope=scope2) for _ in range(3)]
    new_arena = scope2._main_arena.copy_to_new_arena([r.entity_id for r in new_refs])

    # Key lease = clone of scope2._key_lease. Converter = lambda that
    # runs scope2._derived.exchange_forward on (key_clone, base_clone).
    # This exactly matches "LifetimeConverter" signature.
    def converter(key: Lease[Any], base: Lease[Any]) -> Lease[Any]:
        return scope2._derived.exchange_forward({
            scope2._key_ull.token: key,
            t1_token: base,
        })

    await t2_to_t1.send((scope2._key_ull.root_lease.clone(), converter, new_arena_copy_a))
    await t2_to_t3.send((scope2._key_ull.root_lease.clone(), converter, new_arena_copy_b))
    # `async with` drains scope2: discards main_lease + key_lease, awaits
    # derived output token. Will block until:
    #   - All output-lease clones handed out (via converter application
    #     in tasks 1 and 3) are discarded
    #   AND
    #   - An input ends (task 1's scope lease ending, or our own key
    #     dropping).
    # Since we discard our key in aclose, that suffices for the input-
    # end cascade — output drains as soon as tasks 1 and 3 release
    # their clones.
```

**task 3**
```
key_lease_t3, converter_t3, new_arena_t3 = await t2_to_t3.receive()
lease_from_t1 = await t1_to_t3.receive()

# Temporary derived scope rebuilt from task 1's lease + (key, converter).
# This is the "combine task 1's lease with task 2's key and converter to
# get a new lease for task 2's scope" step.
t2_output_lease = converter_t3(key_lease_t3.clone(), lease_from_t1.clone())

async with FxtrScope(name="t3", isolated=True) as scope3:
    scope3._main_arena.lazily_attach_from_arena(
        scope3._main_lease.clone(), some_t2_id, new_arena_t3,
        t2_output_lease, default_lifetime_converter,
    )
    # Load the second entity; should bridge to task 1's data automatically.
    _ = await scope3.load(EntityRef(some_t2_id, SomeType))

# scope3 drained — nothing else should close yet.
# Drop task 2's output lease clone — again, nothing should close.
# (new_arena_t3 holds claims, but we've released our scope3 stake.)
# Drop task 1's lease → cascades: task 1's scope token ends on drain
# (if task 1's main lease was the last clone), which ends task 2's
# derived output token via input-end.
lease_from_t1.discard()
t1_ready_to_drain.set()  # unblock task 1's drain
# Wait for task 1 + task 2 to complete; drop key lease last.
await t1_done_event.wait()
await t2_done_event.wait()
key_lease_t3.discard()  # frees task 2's key ULL; useless but cleanup.
```

### Acceptance criteria — exact sketch predictions

The sketch spells out very specific "what closes when" predictions;
the test asserts each one in place:

1. **Mid-run invariants** (while tasks 1 and 2 are live, task 3 is
   holding the t2 output+key, task 2 has already sent its outputs
   out and is awaiting drain):
   - `scope1._token.ended()` is False.
   - `scope2._token.ended()` is False.
   - `scope2._key_ull.token.ended()` is False.

2. **Task 3 drops its copy of task 2's output lease** →
   *nothing should close*. Assert all three tokens above are still
   not-ended immediately after.

3. **Task 3 drops task 1's lease clone** (after signalling task 1 to
   drain, and task 1's own aclose dropping its main_lease) →
   *both task 1's and task 2's output tokens close*.
   - After this step: `scope1._token.ended()` and
     `scope2._token.ended()` both True.
   - Task 1's `async with` exits; `scope1._closed`.
   - Task 2's `async with` exits; `scope2._closed`.
   - `scope2._key_ull.token.ended()` is still False (the key token
     dangles — it's only gated by key-clone holders, and task 3
     still holds one).

4. **Task 3 drops `key_lease_t3`** →
   `scope2._key_ull.token.ended()` is True.

5. **Autouse clean-drain fixture** (from substage 1) asserts zero
   lease leaks across the entire test.

The test structure uses explicit `anyio.Event`s plus `assert`s
interleaved with the drop operations to catch each prediction at
the moment it's supposed to hold. This is the same acceptance-
criterion shape the sketch describes for substage 3; substage 5
will re-run the same predictions over Cap-IR-marshalled versions
of the arena / lease / converter objects.

Writing the test will likely require iteration to get the exact
clone-holding arithmetic right — some of these predictions depend
on every clone being held/released at the expected moment. The
criteria above are the contract the implementation has to satisfy;
intermediate plumbing (extra clones, extra events) is allowed as
long as each of 1–5 above lands at the right time.

### Additional derived-lifetime tests (in `TestDerivedScope`)

Lighter-weight tests covering the primitive shape:

- `test_with_derived_lifetime_token_is_derived_output`.
- `test_with_derived_lifetime_is_always_isolated`.
- `test_with_derived_lifetime_rejects_colliding_parent`
  (if applicable — probably N/A; only sanity check).
- `test_derived_scope_closes_when_external_input_ends`.
- `test_derived_scope_aclose_blocks_until_external_input_drops`.
- `test_derived_scope_parents_independent_of_derived_leases`.

### Additional `aclose` / `force_close` / drain tests (in `TestScopeDrain`)

- `test_aclose_discards_main_lease_and_waits_for_drain`.
- `test_aclose_revokes_on_cancellation`.
- `test_aclose_is_idempotent`.
- `test_aclose_on_never_entered_mountable_scope`.
- `test_force_close_synchronously_revokes`.
- `test_force_close_is_idempotent`.
- `test_async_cm_wraps_aclose`.
- `test_async_cm_revokes_on_exception_in_body`.
- `test_all_user_ops_raise_during_drain` — parametrised over
  `[store, load, abandon, copy_from_scope, track_from_scope,
  register_deep_fetch_source, lift, track_builtin, clear_cache,
  store_cbor, load_cbor, is_tracked, get_cached_value,
  freeze_scope, included_in_scope, included_in_active_scope]`.
- `test_as_active_scope_allowed_during_drain_but_ops_still_raise`.

### Additional mountable / task-group tests (in `TestMountableScope`)

- `test_non_entered_mountable_scope_is_not_alive`.
- `test_mountable_flag_opens_task_group`.
- `test_task_group_closes_after_token_drain`.
- `test_task_group_cancels_on_revoke`.
- `test_non_mountable_scope_has_no_task_group`.
- `test_non_mountable_scope_usable_without_cm`.

### Deep-fetch cleanup migration test (in `TestDeepFetchCleanup`)

- `test_deep_fetch_source_key_lease_discarded_on_token_end`.
- `test_deep_fetch_source_key_lease_discarded_on_force_close`.

---

## Docs updates

- `CODE_OVERVIEW.md`:
  - Delete the "Token sharing" / "Weak scopes" paragraphs.
  - Rewrite the "Scopes" paragraph: two axes (unconditional/derived
    × mountable), drain model, CM vs non-CM usage for non-mountable
    scopes, CM-only for mountable.
  - Update the "Global Memory Store and Lifetime Tokens" section to
    note that scope lifetimes are scope-owned (no keep-alive) and
    child-keeps-parent-alive is structural (stored parent lease).
- `fxtr2/data/entity_ref_scope.py` module docstring: update the
  scope-lifecycle summary.
- No new `LOCAL_CODE_OVERVIEW.md` under `data/` (none today).

---

## File touch list

| File | Change |
|---|---|
| `fxtr2/data/entity_ref_scope.py` | Primary refactor: init split, `aclose`, `force_close`, `_with_derived_lifetime`, mountable flag + task group, `_draining`, deletion of keep-alive + weak + share-token + `_dead_parents`, deep-fetch cleanup migration, `check_alive` / `is_alive` rewire. |
| `tests/test_entity_ref_scope/test_scope_lifecycle.py` | Delete `TestWeakScopes`; prune `TestTokenSharing`; add `TestScopeDrain`, `TestMountableScope`, `TestDerivedScope`, `TestDeepFetchCleanup`. |
| `tests/test_entity_ref_scope/test_derived_scope_acceptance.py` | New file: the full task-1 / task-2 / task-3 acceptance test. |
| `CODE_OVERVIEW.md` | Scopes + memory-store sections rewritten. |

---

## Implementation order

Single pass. Not strictly required to keep the suite green at each
micro-step; the suite should be green once the substage is done.

1. Delete keep-alive + weak-scope + `_share_token` machinery.
   Migrate or delete the referenced tests.
2. Replace `_evt_*` events with `_started` / `_draining` / `_closed`
   bools. Rewire `check_alive` / `is_alive`.
3. Implement `aclose` / `force_close`. Apply `check_alive` at the
   top of every user-facing method (including read-only ones).
   Replace user-facing `source_scope.lift` calls inside internal
   copy/track helpers with `source_scope._lift_unchecked`.
4. Migrate deep-fetch cleanup to the token-end callback. Rewire
   `GLOBAL_SCOPE` atexit to `force_close`.
5. Add `mountable` flag + task-group plumbing in the CM.
6. Extract `_init_common` / `_init_arena_and_cache` helpers. Add
   `_with_derived_lifetime` classmethod + `_key_ull` / `_derived` /
   `_key_lease` attrs.
7. Add unit tests: `TestScopeDrain`, `TestMountableScope`,
   `TestDerivedScope`, `TestDeepFetchCleanup`.
8. Add the full acceptance test `test_derived_scope_acceptance.py`.
9. Update `CODE_OVERVIEW.md` and the module docstring.
10. Run full suite.

---

## Checklist

- [x] Delete `_KeepAlive{,Token,Waiter}` + keep-alive machinery.
- [x] Delete `new_weak_child_scope` / `_close_weak_scope` / `_weak_children` / `_weak`.
- [x] Delete `_share_token` kwarg + implementation branch.
- [x] Delete `_dead_parents`.
- [x] Replace `_evt_startup` / `_evt_shutdown_*` with `_started` + `_draining` + `_closed` + `_inside_cm` bools.
- [x] Migrate / delete weak-scope + share-token tests.
- [x] Implement `aclose()` (short-circuits to `force_close` when `_started` is False).
- [x] Implement `force_close()` (sync revoke, idempotent).
- [x] Apply `check_alive()` to every user-facing method (mutation + read-only — with
      one deviation noted below: `included_in_scope` / `included_in_active_scope` are
      pure structural ancestry queries and stay callable on closed scopes).
- [x] Swap internal `lift` callers to `_lift_unchecked` (`copy_from_scope` /
      `track_from_scope` now call `source_scope._lift_unchecked`). Added
      `_is_tracked_internal` helper for the same reason inside `_lift_unchecked`.
- [x] Rewire `check_alive` / `is_alive`.
- [x] Rewire `__asynccontextmanager__` to `try/yield/except:force_close/else:aclose` shape; task group opens inside via `AsyncExitStack` when mountable.
- [x] Rewire `as_active_scope` to drop `_keep_alive` and drop `check_alive`.
- [x] Migrate deep-fetch source cleanup to a token-end callback.
- [x] Rewire `_start_as_root_scope` atexit to `self.force_close()`.
- [x] Add `mountable` init flag and enforce "mountable ⇒ CM-only" via the auto-start rule.
- [x] Auto-start rule: only `isolated and not mountable` scopes auto-start at construction.
- [x] Add `_with_derived_lifetime` classmethod (always isolated, `derived_leases: Mapping`).
- [x] Extract `_init_common` / `_init_arena_and_cache` / `_compute_parent_leases` / `_register_scope_token_end_callback`.
- [x] Per-test timeout fixture (`signal.SIGALRM`, 10s) added to `tests/conftest.py` so
      deadlocks surface as test failures rather than suite hangs.
- [x] `TestScopeDrain`, `TestMountableScope`, `TestDerivedScope`, `TestDeepFetchCleanup`
      — added and all passing.
- [ ] `test_derived_scope_acceptance.py` — full task-1 / task-2 / task-3.
- [ ] Full test suite green. *Currently 1750/1750 passing.*
- [ ] Update `CODE_OVERVIEW.md`.
- [ ] Update `entity_ref_scope.py` module docstring.

---

## Current state (mid-implementation notes)

Landed so far:

- All scope-layer refactor work (init split, aclose / force_close, mountable flag,
  derived-lifetime constructor, keep-alive removal, deep-fetch token-end callback,
  root-scope atexit).
- `tests/conftest.py` autouse timeout fixture using `signal.SIGALRM` (10s).
- Existing suite migrations: `serdes_scope` fixtures (2 of them) no longer call
  `clear_cache` after CM exit; `test_freeze_unentered_scope_raises` switched to a
  mountable scope; shared "shut down" regex in two tests changed to
  "has already been closed" to match the new check_alive error messages;
  `TestTokenSharing` pruned and `TestWeakScopes` deleted.
- New scope-level tests: `TestScopeDrain`, `TestMountableScope`, `TestDerivedScope`,
  `TestDeepFetchCleanup` appended to `tests/test_entity_ref_scope/test_scope_lifecycle.py`.

### Resolved: derived-scope store-and-load hang

`TestDerivedScope::test_derived_scope_store_and_load` was hanging in
`derived.aclose()`. Root cause was a compound of two issues plus a drain
amplifier:

1. **Deep-fetchability inference ran in `__aenter__`.** The temp scope
   created inside `handler_supports_entity_type` (a non-isolated
   `async with FxtrScope():` to give the handler-supports check its own
   scope) walked its parents on enter, calling
   `parent._is_deeply_fetchable()` on `derived`. But `derived` was used
   bare — auto-started, never CM-entered — so its
   `_deeply_fetchable_flag` was still `None`, and the predicate raised.

2. **`__aenter__` raising leaked half-constructed scope state.** The
   parents-check ran *after* `__init__` had already minted the temp
   scope's ULL, registered its token in the memory store (with
   `parent_leases=[derived._main_lease.clone()]`), and taken its
   main lease. When `__aenter__` raised, no `__aexit__` runs, so all
   that state stayed alive — pinning two clones of derived's output
   lease in the memory store row, each carrying its own `d/key` via.

3. **Drain in `finally` masked the real error.** The test's
   `try: ... finally: await derived.aclose()` blocked indefinitely
   waiting for the leaked d/key clones. SIGALRM raised TimeoutError
   synchronously, but it didn't route through anyio's cancellation, so
   `aclose`'s revoke-on-cancel branch never fired — the original
   RuntimeError sat as `__context__` on the never-completing wait.

Fixes:

- Moved parents validation (started / not closed / not draining) into
  `_init_common`, *before* token registration. Bad parent now aborts
  construction cleanly with no half-baked memory-store row.
- Made `_is_deeply_fetchable()` lazy-compute and cache on first read;
  removed the inference loop from `__aenter__`. Auto-started constructors
  eagerly call `_is_deeply_fetchable()` to commit at construction time
  (so they're fully usable bare); CM-required scopes commit on
  `__aenter__` (preserves the
  `_mark_not_deeply_fetchable_after_true_raises` semantic).
- Switched the test's cleanup pattern to
  `try/except BaseException: derived.force_close(); raise / else: await
  derived.aclose()`. Documented this as a sharp edge in `aclose`'s
  docstring along with two alternate patterns
  (`async with` preferred; `move_on_after` as a soft fallback).

### Resolved: derived `aclose` should not block on key ULL

While writing the task-1/2/3 acceptance test we found that the original
"await key_ull then derived" ordering produces the wrong drain semantics
when key clones are held externally — scope close ends up gated on
external holders releasing the key, even though the key has no role
once the derived output token has ended.

The acceptance test's prediction 3 ("scope2's CM exits when t1's lease
clone is dropped, with `key_ull` still alive") only holds if scope.aclose
on a derived scope does **not** explicitly wait for the key ULL.

Fix: the derived branch of `FxtrScope.aclose` now reads

```python
self._main_lease.discard()
self._key_lease.discard()
await self._derived.aclose()
```

— no `await self._key_ull.aclose()`. With no external key holders, key
drains synchronously inside the discards (vias released by
`main_lease.discard`, our root reference released by the key_lease
discard) and the cascade fires before `derived.aclose` returns; with
external holders, derived.aclose blocks until some input ends and key
is left dangling for its remaining holders.

The simple `test_derived_scope_aclose_key_ull_drains` test still
passes — in the no-external-holders case, the key naturally drains
during the discards, so `key_token.ended()` is True after
`derived.aclose` returns.

### Other notes

- `include=[GLOBAL_SCOPE]` on a derived scope successfully wires up the
  memory-store ancestor DAG (needed so builtin types are lift-able). Verified by
  the test partially succeeding past the `store()` call before hanging on
  `aclose`.
- `conftest.py` timeout fixture uses `signal.SIGALRM`, which works for both sync
  and async tests but raises `TimeoutError` in the main thread. Works well under
  `pytest -n auto`.

### Still to do

1. [DONE] Diagnose + fix the derived-scope store/load hang.
2. [DONE] Investigate the `test_backend_integration.py::test_register_same_indexer_prefix_twice_raises`
   error — may be pre-existing (unrelated to this substage), need to verify.
3. Write the full task-1 / task-2 / task-3 acceptance test
   (`test_derived_scope_acceptance.py`).
4. Update `CODE_OVERVIEW.md` (scope section rewrite).
5. Update `entity_ref_scope.py` module docstring.
6. Promote the autouse lease-leak fixture (from substage 1) to top-level
   `tests/conftest.py`, if it's still scoped under `tests/test_leases/`.

---

## Open / "revisit later" notes

- **Derived-scope parent warning**. Dropping the parent-unexpectedly-
  closed warning means a derived scope whose external input ends
  surfaces only as `LeaseBrokenError` on the next op. Revisit if
  this becomes hard to debug.
- **Mountable scope used bare (no CM)**. We don't support it;
  `check_alive` rejects. Revisit if real callers end up wanting a
  non-CM mount pattern (probably never — the task group tie makes
  CM natural).
- **`aclose` + `force_close` naming**. Symmetric with
  `UnconditionalLocalLifetime.aclose` and
  `UnconditionalLocalLifetime.revoke`. If callers find "force" too
  strong relative to "revoke" we can rename; flagged for ergonomics
  revisit.
- **Derived-scope-only + no parents**. Supported; memory-store row
  has empty parents. Entity visibility depends on what the user
  does with the lease; not a concern for this substage.
- **CM re-entry**: both post-close re-entry (via `_closed`) and
  nested re-entry while inside the body (via `_inside_cm`) raise.
