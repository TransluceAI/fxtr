# Substage 1 — `DerivedLocalLifetime` primitive

Detailed implementation plan for substage 1 of
[stage 8c](overview.md#substage-1--derived-lifetime-primitive).

**Goal.** Replace `AttachedLocalLifetime` (one-requirement + one-key lifetime) with
`DerivedLocalLifetime` (a lifetime tied to an arbitrary *set* of input tokens, all
symmetric). All of the "key vs requirement" asymmetry that Attached baked in is
dissolved: callers who want key-gating just include their own ULL token in the
input set and retain its root lease.

This substage also introduces a test-wide clean-drain autouse fixture (carried
through the rest of stage 8c), extracts an `aclose()` method on
`UnconditionalLocalLifetime` in preparation for the scope refactor in substage 3,
and deliberately does **not** add an async context manager on
`DerivedLocalLifetime` — drain composition is caller-specific there.

---

## Primitive surface

```python
class DerivedLocalLifetime[RB_Out: LocalLifetimeBrand]:
    def __init__(
        self,
        name: str,
        inputs: Collection[LifetimeToken[Any]],
    ): ...

    @property
    def token(self) -> LocalLifetimeToken[RB_Out]: ...

    def exchange_forward(
        self,
        input_leases: Mapping[LifetimeToken[Any], Lease[Any]],
    ) -> Lease[RB_Out]: ...

    def exchange_reverse(
        self,
        out_lease: Lease[RB_Out],
    ) -> dict[LifetimeToken[Any], Lease[Any]]: ...

    def revoke(self, reason: str) -> None: ...

    async def aclose(self) -> None: ...

    @contextlib.contextmanager
    def revoke_after_block(self) -> Generator[Self, None, None]: ...
```

Only one brand parameter on the class — the output brand. The input tokens vary
in number and brand and are typed as `LifetimeToken[Any]` / `Lease[Any]`.
Call sites that need per-input brand precision will cast at the boundary.

### Construction

- Snapshots `inputs` into a `frozenset[LifetimeToken[Any]]`
  (call it `_input_tokens`). Deduplication uses the token's
  `__hash__` / `__eq__` (by-value, future-proof for remote tokens —
  today's `LocalLifetimeToken` defaults to identity, so this degrades
  gracefully). An empty input set is legal; see DOA note below.
- Creates the output `LocalLifetimeToken[RB_Out]` (name
  `f"{name}/output"`).
- Subscribes an `_on_input_ended(token)` handler to every input
  token's `register_end_callback`. If any input is already ended at
  construction, the callback fires synchronously → output is **dead
  on arrival** (same DOA semantics as today's Attached, generalised).
  With an empty input set the output stays alive forever except via
  `revoke`; this is a degenerate but legal configuration.
- Initialises `_output_tracking: dict[int, (_DerivedLeaseCleanupInfo,
  finalizer)]` empty. No eagerly-constructed root lease — leases come
  from `exchange_forward`.
- Stashes deregister callbacks from each input in
  `_input_dereg: dict[LifetimeToken, Callable[[], None]]` so that on
  `revoke` we detach from any still-live inputs.

### Cascade rules

| Trigger | Output leases | Output token | Inputs |
|---|---|---|---|
| Any input token ends | broken (`"input X ended"`) | ends | untouched |
| `revoke(reason)` | broken | ends | untouched |
| All output leases discarded while all inputs still live | — | stays alive (idle); fresh `exchange_forward` ok | untouched |

Simpler than today's Attached: the output token ends **iff** any
input ends or `revoke` is called. There is no "outputs drained + an
input ended" path because an input ending already closes the output
synchronously (the first trigger already covers that case).

A nice consequence: any live output lease implies all its `_vias`
are live. So `exchange_reverse` can simply clone the stored vias
without any brokenness edge cases.

`_on_input_ended` and `revoke` share a single helper (different
reason strings); see "Revoke / input-end" below.

### `exchange_forward(input_leases)`

- Takes ownership of every lease in `input_leases` via
  `lease.take()`. On any validation failure inside the block, the
  context-manager unwind discards them for us.
- Validates `set(input_leases.keys()) == self._input_tokens` (both
  directions — no missing keys, no extra keys). Uses `==`, which on
  `frozenset` uses `__hash__` / `__eq__` on each token. On mismatch:
  `RuntimeError("exchange_forward input token set mismatch ...")`
  with the diff.
- No separate "lease.grant.subject matches its map key" check — if
  the caller supplied a garbage lease under the right token key, the
  cascade-breaks-on-input-end still fires correctly via the stored
  via, and any misuse surfaces at `ensure_active` time. Skipping
  this keeps `exchange_forward` tight.
- If `self._output_token.ended()` (DOA, or already revoked),
  raises `LeaseBrokenError`. All taken leases have already been
  discarded by the context-manager unwind.
- Builds a `_DerivedOutputGrant` frozen dataclass
  `(_derived=self, _input_grants=frozenset(lease.grant for lease in
  input_leases.values()))` so two forward-exchanges over input
  leases with identical grants produce equal output grants.
  `frozenset` means input-order doesn't matter.
- Clones every input lease into `_vias: dict[LifetimeToken, Lease]`
  and constructs a `LocalDerivedTokenLease` holding `_vias`. The
  output lease self-registers via `_register_lease`.

### `exchange_reverse(out_lease)`

Because any input ending breaks outputs synchronously, a live output
lease always has live vias. So:

```python
def exchange_reverse(self, out_lease: Lease[RB_Out]) -> dict[...]:
    with out_lease.take() as out_lease:
        if (
            not isinstance(out_lease, LocalDerivedTokenLease)
            or out_lease._owner is not self
        ):
            raise TypeError("...does not belong...")
        return {tok: via.clone() for tok, via in out_lease._vias.items()}
```

This is the same shape as `LocalDerivedTokenLease.clone`, just
returning a dict instead of building a new lease. The taken copy
discards on block exit, which releases its via-clones; the returned
dict is independently owned.

### Revoke / input-end

Shared implementation:

```python
def revoke(self, reason: str) -> None:
    self._close_output(reason)

def _on_input_ended(self, token: LifetimeToken[Any]) -> None:
    self._close_output(f"input {token} ended")

def _close_output(self, reason: str) -> None:
    if self._output_token.ended():
        return
    tracking = self._output_tracking
    self._output_tracking = {}
    for info, finalizer in tracking.values():
        peek = finalizer.peek()
        if peek is not None:
            peek[0]._mark_broken(reason)
        finalizer.detach()
        # Discard surviving vias (others already broken by their owner).
        for via in info.vias.values():
            via.discard()
        info.vias.clear()
    # Detach from any still-live input tokens.
    for deregister in self._input_dereg.values():
        deregister()
    self._input_dereg = {}
    self._output_token._mark_ended()
```

Idempotent by the leading `ended()` check.

### `aclose()`

```python
async def aclose(self) -> None:
    if self._output_token.ended():
        return
    end_event = anyio.Event()
    deregister = self._output_token.register_end_callback(end_event.set)
    try:
        await end_event.wait()
    except BaseException:
        self.revoke("aclose cancelled during drain")
        raise
    finally:
        deregister()
```

Single try with except + finally. On cancel, revoke so a stuck wait
doesn't deadlock callers that bail out. Idempotent on an
already-ended lifetime.

Note on composition: because output only ends when an input ends or
on `revoke`, callers are expected to drop whatever holds an input
alive (e.g. their key ULL's root lease) *before* calling `aclose`.
The common pattern below makes this explicit.

### `revoke_after_block()`

Sync CM:

```python
@contextlib.contextmanager
def revoke_after_block(self):
    try:
        yield self
    finally:
        self.revoke("block exit")
```

No key root to discard (Derived has no root-held-by-owner lease), so
this is just a wrapper for revoke-on-exit. Useful for tests.

---

## Concrete types

### `_DerivedOutputGrant`

Frozen dataclass; value-based equality.

```python
@dataclasses.dataclass(frozen=True)
class _DerivedOutputGrant[RB_Out: LocalLifetimeBrand](Grant[RB_Out]):
    _derived: DerivedLocalLifetime[RB_Out]
    _input_grants: frozenset[Grant[Any]]

    @property
    def subject(self) -> LifetimeToken[RB_Out]:
        return self._derived._output_token
```

### `LocalDerivedTokenLease`

Extends `LocalLifetimeLease[RB_Out]` just like today's
`LocalAttachedTokenLease`.

```python
class LocalDerivedTokenLease[RB_Out: LocalLifetimeBrand](
    LocalLifetimeLease[RB_Out]
):
    __slots__ = ("_vias",)

    def __init__(
        self,
        owner: _LeaseOwner[RB_Out, Any],
        grant: _DerivedOutputGrant[RB_Out],
        vias: dict[LifetimeToken[Any], Lease[Any]],
    ):
        self._vias = vias
        super().__init__(owner, grant)

    def clone(self) -> Self:
        self.ensure_active()
        return type(self)(
            self._owner,
            self._grant,
            {tok: via.clone() for tok, via in self._vias.items()},
        )
```

### `_DerivedLeaseCleanupInfo`

The info object stores the *same dict reference* the lease holds,
so leak-cleanup and normal cleanup use the same object.

```python
@dataclasses.dataclass(frozen=True)
class _DerivedLeaseCleanupInfo(_LeaseCleanupInfo):
    vias: dict[LifetimeToken[Any], Lease[Any]]

    def discard_stranded_resources(self) -> None:
        for via in self.vias.values():
            via.discard()
        self.vias.clear()
```

Owner-side `_clean_up_lease_data` for the normal discard path does
the same: iterate `info.vias.values()`, `.discard()` each, clear.

---

## `UnconditionalLocalLifetime.aclose`

Small preparatory refactor (answer to Q6):

- Extract the existing async-CM body into `async def aclose(self) ->
  None:` — discards `self._root_lease`, then waits for the token's
  end-callback; revokes on cancellation. Idempotent on an
  already-ended lifetime.
- The async CM becomes `try: yield self; finally: await self.aclose()`.
- `revoke_after_block` unchanged.
- `_drain_async_scope` is collapsed — it was shared with Attached, is
  now only a ULL implementation detail, and the new `aclose` shape
  is small enough to inline.

Parallel substage-3 scope `aclose()` will use the same shape.

---

## Migration

### `fxtr2/leases/local_lease.py`

- Delete `AttachedLocalLifetime`, `LocalAttachedTokenLease`,
  `_AttachedOutputGrant`, `_AttachedLeaseCleanupInfo`,
  `_AttachedLeaseKind`.
- Add `DerivedLocalLifetime`, `LocalDerivedTokenLease`,
  `_DerivedOutputGrant`, `_DerivedLeaseCleanupInfo`.
- Keep the shared `_LeaseOwner` protocol, `_LeaseCleanupInfo` base,
  and leak-on-finalizer plumbing — all reusable.
- Extract `UnconditionalLocalLifetime.aclose`; reimplement the async
  CM on top. Collapse `_drain_async_scope`.
- Add `_LEASE_LEAK_COUNT: int = 0` module global; bump it inside
  `_on_lease_leaked` (after logging, before cleanup). Expose
  `get_lease_leak_count() -> int`.

### Stale docstrings

Only two live references to Attached outside `local_lease.py`:

- `fxtr2/rpc/lowering_state.py:188` — update to
  `UnconditionalLocalLifetime / DerivedLocalLifetime`.
- `fxtr2/data/abstract_arena.py:90, 513` — update the
  `LifetimeConverter` docstring to reference a small adapter around
  `DerivedLocalLifetime.exchange_forward` instead of
  `AttachedLocalLifetime.exchange_forward`. Inline example in the
  docstring.

No rpc/lowering.py regrant migration — `grep` confirms no runtime
uses of Attached there (the overview's hint about that turned out
to be speculative).

### `tests/test_leases/test_local_lease.py`

- Delete the `TestAttached*` / `TestExchange*` / `TestRequirement*` /
  `TestKeyEndCascade` / `TestDeadOnArrival` / `TestOutputViaLifecycle`
  test classes.
- Add the `TestDerived*` suite (see "Testing" below).
- Keep `TestLocalLifetimeToken`, `TestLocalGrant`,
  `TestLocalLifetimeLease`, `TestUnconditionalLocalLifetime`,
  `TestStaticLifetime` unchanged.
- `TestLeakWarnings.test_unconditional_leaked_lease_warns_and_auto_closes`
  keeps its current shape; mark it
  `@pytest.mark.allow_lease_leak`. Replace
  `test_attached_output_leak_releases_via` with the Derived
  equivalent (also marked).
- `TestHashability.test_attached_output_grant_is_dict_key` →
  `test_derived_output_grant_is_dict_key`.

### `tests/test_memory_arena.py::TestAttachedLocalLifetimeConverter`

Rewrite as `TestDerivedLocalLifetimeConverter`. Uses a separate
`key_ull` (per Q3) plus a 2-arg adapter closure:

```python
key_ull = UnconditionalLocalLifetime("att-test-key")
consumer_token = consumer_leased._lease.subject
derived = DerivedLocalLifetime("att-test",
                               inputs={key_ull.token, consumer_token})

def converter(key: Lease[Any], base: Lease[Any]) -> Lease[Any]:
    return derived.exchange_forward({
        key_ull.token: key,
        consumer_token: base,
    })

# rest: attach via (consumer_lease, key_ull.root_lease.clone(), converter)
```

Teardown: drop `key_ull.root_lease`, `derived.revoke(...)`,
`key_ull.revoke(...)`, close the source / consumer.

### `tests/conftest.py`

Add autouse fixture:

```python
@pytest.fixture(autouse=True)
def _assert_no_lease_leaks(request):
    import gc
    from fxtr2.leases import local_lease
    gc.collect()
    before = local_lease._LEASE_LEAK_COUNT
    yield
    gc.collect()
    delta = local_lease._LEASE_LEAK_COUNT - before
    if delta and "allow_lease_leak" not in request.keywords:
        pytest.fail(f"Test leaked {delta} lease(s) (no discard)")
```

Register `allow_lease_leak` in `pyproject.toml` under
`[tool.pytest.ini_options].markers`.

(Handling-wise this is slightly blunt — a deliberately-leaking test
has to opt the whole test out, not just a region. Flagged as a
"maybe revisit" in the open notes below; probably fine for now.)

### Docs

- `fxtr2/leases/LOCAL_CODE_OVERVIEW.md`:
  - Replace the Attached section with a Derived section (new cascade
    table, semantics).
  - Update the file-overview table's `local_lease.py` entry.
  - Add the composition pattern below to "Common patterns".
- `fxtr2/CODE_OVERVIEW.md`:
  - Replace `AttachedLocalLifetime` with `DerivedLocalLifetime` in
    the one-line summary of `local_lease.py`.

### Common pattern — emulating old Attached's key-gated shape

```python
key_ull = UnconditionalLocalLifetime("my_key")
derived = DerivedLocalLifetime(
    "my_derived",
    inputs={key_ull.token, *other_input_tokens},
)
try:
    # hand out exchange_forward outputs here
    yield derived
finally:
    # Signal drain: drop our hold on the key.
    key_ull.root_lease.discard()
    # Wait for the key ULL to drain, then for the Derived output
    # token to end. Nested try/finally so that cancelling the first
    # await still runs the second (which will revoke its own
    # lifetime on cancel).
    try:
        await key_ull.aclose()
    finally:
        await derived.aclose()
```

Ordering rationale: dropping `key_ull.root_lease` starts the key's
drain. The key stays alive until the last via (held inside derived
output leases) drops, which happens as output holders release
them. Once the key actually ends, the Derived's input-end cascade
closes the output token. So `key_ull.aclose()` completes first (the
thing we directly gated), then `derived.aclose()` is typically
already ended and returns immediately.

The nested `finally` is important: if the key's drain hangs and
the caller cancels, we want `derived.aclose()` to still run so its
own cancel-revoke branch fires and the output token doesn't
dangle. Either `aclose` revokes its own lifetime on cancel, so
unwind order in the presence of cancellation is well-defined.

---

## Testing

Unit tests for `DerivedLocalLifetime` in
`tests/test_leases/test_local_lease.py`.

### `TestDerivedOutputGrant`

- `test_equal_for_same_derived_and_input_grants` — frozenset
  equality carries.
- `test_equal_despite_input_grant_order` — dict-passed in different
  iteration orders produce equal grants.
- `test_not_equal_when_input_grants_differ`.
- `test_not_equal_when_derived_differs` — two Derived instances
  with the same input grants still produce distinct output grants.
- `test_subject_is_output_token`.
- `test_hashable_as_dict_key`.

### `TestDerivedBasics`

- `test_output_token_active_on_construction`.
- `test_empty_input_set_is_legal_and_idle` — no inputs; can call
  `exchange_forward({})`; output stays alive until revoke.
- `test_exchange_forward_produces_active_output_lease`.
- `test_forward_takes_ownership_of_all_inputs`.
- `test_forward_rejects_missing_input_key` → `RuntimeError`.
- `test_forward_rejects_extra_input_key` → `RuntimeError`.
- `test_forward_rejects_inactive_lease` → `LeaseDiscardedError` /
  `LeaseBrokenError` via the `take()` path.
- `test_cloned_output_clones_all_vias` — resulting output has
  independent clones of every input lease.
- `test_single_input_acts_like_lifetime_mirror` — construct
  `Derived(inputs={req_token})`; forward succeeds with a single-key
  map; input-end cascade breaks outputs.
- `test_multi_input_forward_succeeds`.

### `TestDerivedExchangeReverse`

- `test_reverse_returns_dict_of_all_input_leases` — keys match the
  Derived's declared input token set.
- `test_reverse_returned_leases_are_active_clones` — each returned
  lease `.is_active()` and `.subject` matches the corresponding
  token.
- `test_reverse_returned_leases_are_independent` — discarding one
  returned lease does not affect others.
- `test_reverse_discards_the_wrapper_cleanly` — after reverse, a
  subsequent `out_lease.discard()` on the original reference is a
  no-op (it's already been taken+discarded).
- `test_reverse_raises_on_non_derived_lease` → `TypeError`.
- `test_reverse_raises_on_foreign_output_lease` → `TypeError`.
- `test_reverse_raises_on_discarded_output_lease` →
  `LeaseDiscardedError`.

### `TestDerivedInputEndCascade`

- `test_single_input_end_breaks_all_output_leases` — reason string
  `"input <token> ended"` (or substring match on `"input "` +
  `"ended"`).
- `test_input_end_closes_output_token`.
- `test_input_end_leaves_other_inputs_untouched`.
- `test_input_end_releases_vias` — after cascade, surviving-input
  roots can be discarded without complaint; surviving-input tokens
  close naturally.
- `test_multi_input_first_one_to_end_wins` — two live inputs; end
  one; output breaks with that one's reason. Ending the second
  later is a no-op at the Derived level.

### `TestDerivedOutputIdle`

- `test_all_outputs_drained_while_inputs_live_keeps_output_alive` —
  forward, clone, discard all outputs; output token stays alive;
  new `exchange_forward` still succeeds.

### `TestDerivedRevoke`

- `test_revoke_breaks_all_outputs_and_ends_token`.
- `test_revoke_is_idempotent`.
- `test_revoke_after_natural_close_is_idempotent` (post-input-end
  revoke is a no-op).
- `test_revoke_does_not_touch_inputs`.

### `TestDerivedDeadOnArrival`

- `test_already_ended_input_closes_output_at_construction`.
- `test_forward_on_doa_raises`.
- `test_empty_inputs_is_not_doa` — covered above, but assert
  explicitly.

### `TestDerivedViasLifecycle`

- `test_output_lease_holds_claim_on_each_input` — generalises today's
  Attached `_via` test to N inputs.
- `test_cloned_output_holds_independent_claim_per_input`.
- `test_revoke_releases_all_vias`.

### `TestDerivedStructuralChecks`

- `test_foreign_output_lease_rejected_by_derived`.
- `test_unrecognized_grant_rejected`.

### `TestDerivedAclose`

- `test_aclose_returns_immediately_if_already_ended`.
- `test_aclose_waits_for_input_end` — spawn a task that calls
  `aclose`; assert it blocks until we end one of the inputs; verify
  it returns promptly after.
- `test_aclose_revokes_on_cancellation` — cancel the task running
  `aclose` mid-wait; assert output token ends, outputs broken.
- `test_aclose_is_idempotent`.

### `TestUnconditionalAclose`

- `test_aclose_discards_root_and_waits_for_drain`.
- `test_aclose_revokes_on_cancellation`.
- `test_aclose_is_idempotent`.

### Leak-warning tests

- `TestLeakWarnings.test_unconditional_leaked_lease_warns_and_auto_closes`
  — keep, mark `@pytest.mark.allow_lease_leak`.
- `TestLeakWarnings.test_derived_output_leak_releases_vias` — new,
  `@pytest.mark.allow_lease_leak`; direct rewrite of today's
  Attached-variant.

---

## Implementation steps

Per the Q7 note: we don't need green between every micro-step, only
between substages. So this rolls into two phases with testing
concentrated at the end.

1. **Land the whole thing.** In one pass:
    - Add `_LEASE_LEAK_COUNT` + autouse fixture + marker registration.
    - Extract `UnconditionalLocalLifetime.aclose` and rewire its
      async CM.
    - Add `_DerivedOutputGrant`, `LocalDerivedTokenLease`,
      `_DerivedLeaseCleanupInfo`, `DerivedLocalLifetime`.
    - Delete `AttachedLocalLifetime`, `LocalAttachedTokenLease`,
      `_AttachedOutputGrant`, `_AttachedLeaseCleanupInfo`,
      `_AttachedLeaseKind`. Collapse `_drain_async_scope` into
      `UnconditionalLocalLifetime.aclose` (inline).
    - Update the two stale docstrings (`lowering_state.py`,
      `abstract_arena.py`).
    - Rewrite `TestAttachedLocalLifetimeConverter` as
      `TestDerivedLocalLifetimeConverter` in `test_memory_arena.py`.
    - Rewrite the Attached-flavored test classes in
      `test_local_lease.py` to the Derived equivalents; mark the
      existing ULL leak test + new Derived leak test
      `allow_lease_leak`.
2. **Run the suite.** Fix any fallout (mostly type-check fallout +
   cascading doc references) until green.
3. **Docs.** Update
   `fxtr2/leases/LOCAL_CODE_OVERVIEW.md` (Derived section, common
   pattern with aclose ordering, updated file-overview table) and
   `fxtr2/CODE_OVERVIEW.md` (one-line summary). Re-run suite
   sanity-check.

---

## Checklist (tracking)

- [x] Leak counter + autouse fixture + marker registration.
- [x] `UnconditionalLocalLifetime.aclose` extracted; CM rewired.
- [x] `DerivedLocalLifetime` + types + tests.
- [x] `TestDerivedLocalLifetimeConverter` migration in
      `test_memory_arena.py`.
- [x] `AttachedLocalLifetime` et al. deleted; `_drain_async_scope`
      collapsed.
- [x] Stale docstrings updated (`rpc/lowering_state.py`,
      `data/abstract_arena.py`).
- [x] Full suite green (1736 passed).
- [x] `fxtr2/leases/LOCAL_CODE_OVERVIEW.md` updated.
- [x] `fxtr2/CODE_OVERVIEW.md` updated.

---

## Open / "revisit later" notes

- **Async CM on Derived was rejected** (Q6). Not reintroducing
  unless a clear use case surfaces. The composition pattern in
  `LOCAL_CODE_OVERVIEW.md` is the intended user shape.
- **Brand parametricity for inputs.** Input tokens are `Any`-branded
  in the Derived type signature because Python's generics don't
  support variadic brand parameters cleanly. Call sites that care
  about per-input brands will `cast` at the boundary. Output brand
  `RB_Out` stays precise.
- **Autouse leak fixture is per-test, not per-region** (Q4). A
  deliberately-leaking test must opt the whole test out via
  `@pytest.mark.allow_lease_leak`. If that starts hurting, we can
  switch to a context-manager / with-block opt-out or a counter
  API exposed as a fixture. Fine for now.
- **`exchange_reverse` unwrap-vs-clone** — we clone (Q2, revisited).
  The simpler "break outputs eagerly on any input-end" cascade
  makes cloning safe and keeps reverse's implementation small.
