# Derived-scope fixes (part 6, post 3)

## Motivation

Two things are still wrong after the part6post2 memory-store simplification:

1. **The `same_scope` resolve drain cycle**
   (`resolve_into_mount_scope_drain_cycle.md`). When a service is mounted on
   the caller scope and then resolved back into it, `_dispatch_body` opens
   `call_scope` via `_with_derived_lifetime(include=[caller],
   include_leases=[outer_lease], derived_leases=...)`. The outer lease ends
   up as an `IndependentLocalBridge` ancestor entry on `call_scope`,
   pinning `Lease[caller_main]`. That clone is only released at
   `call_scope._on_token_end` → which fires after `call_scope.token` ends
   → which only happens after `K_call_A` drains → which only happens after
   the caller's chain bridge releases its `K_call_A` clone → which only
   happens at caller's `_on_token_end`. The chain pins `caller_main`
   indefinitely.

2. **Ancestor-bridge plumbing.** `_populate_ancestor_bridges` /
   `_mint_lease_via_ancestor_bridge` raise `NotImplementedError` on
   `DependentLocalBridge` ancestor entries today, so the obvious fix
   for (1) — back the ancestor entry by `derived.exchange_reverse`
   instead of holding `Lease[caller_main]` directly — isn't expressible.

The cycle breaks if `call_scope.main_token` is **derived from**
`caller_main_token` rather than independently ancestor-bridged to it. A
live `Lease[call_scope.main]` then carries `caller_main` as a via —
discarding `main_lease` (and `key_lease`) inside `call_scope.aclose`
releases the via, `caller_main` ends, caller's `_on_token_end` fires
and drops the chain-bridge clone of `K_call_A`, `K_call_A` ends,
derived output ends, `call_scope.aclose` returns. No deadlock.

To express this we need `_with_derived_lifetime` to register
"derived-from-parent" leases as `is_ancestor_or_arena_owner=True`
**Dependent** ancestor entries (case-3 / `exchange_reverse`), and the
ancestor-walk machinery has to compose Dependent entries when a parent
itself was constructed via `_with_derived_lifetime`.

## Scope

This plan covers:

- A small prep change that adds `DerivedLocalLifetime.subclone` and
  audits existing `exchange_reverse` call sites.
- A reshape of `_with_derived_lifetime`'s parameters (`include_leases`
  → `independent_leases`; `derived_leases` → `derived_from_leases`),
  with both kinds registering as `is_ancestor_or_arena_owner=True`
  ancestor entries.
- Generalization of `_populate_ancestor_bridges` /
  `_mint_lease_via_ancestor_bridge` to handle Dependent ancestor
  entries (Pipelined ancestors stay disallowed via `TypeError`).
- The dispatch fix in `_dispatch_body`: move the outer lease from
  `independent_leases` to `derived_from_leases`.
- Cleanup: drop the self-mount `NotImplementedError`, drop the
  `same_scope` xfail block, mark
  `resolve_into_mount_scope_drain_cycle.md` resolved.
- New tests for the derived-from-parent ancestor case and the
  transitive Dependent-ancestor walk.

Out of scope:

- Pipelined ancestors. Those continue to raise `TypeError` from
  `_populate_ancestor_bridges` and `_mint_lease_via_ancestor_bridge`;
  no callers need them.
- Memory-store changes. The store layer is unchanged.
- Typed-service work (substage 8) and the RPC bridge (substage 7).

---

## Step A — `DerivedLocalLifetime.subclone` prep ✅

A small, self-contained addition. Lands first so the bridge work in
later steps can use it.

**Two new methods on `DerivedLocalLifetime`:**

```
def subclone(
    self,
    out_lease: Lease[RB_Out],
    target: LifetimeToken[B],
) -> Lease[B]:
    """Clone the via for ``target`` from ``out_lease``, leaving ``out_lease`` alive."""

def subtake(
    self,
    out_lease: Lease[RB_Out],
    target: LifetimeToken[B],
) -> Lease[B]:
    """Like ``subclone`` but consumes ``out_lease`` (and discards the unused vias)."""
```

Semantics:

- Both validate `out_lease` is one of this Derived's output leases
  (same ownership / type checks as `exchange_reverse`).
- Both raise (clear domain error) if `target` is not in the input set.
- `subclone`: does **not** consume `out_lease` — it stays a live lease
  afterwards. Returns `out_lease._vias[target].clone()`. Read-only on
  the lease's state.
- `subtake`: consumes `out_lease` (same as `exchange_reverse`),
  returns the via for `target` directly (no extra clone), discards
  every other via. Equivalent to today's `_case3_extract_via` body but
  living on `DerivedLocalLifetime` itself.

Implementation: `subclone` is `exchange_reverse` minus the take-and-pop
step, returning a single clone. `subtake` is `exchange_reverse` plus an
in-place pop + discard-the-rest. Sharing a private helper that does the
common validation is fine.

Call sites pick the version that matches the ownership story most
naturally: when the caller already plans to discard `base` immediately
after extraction, `subtake` is one line shorter; when the caller wants
to keep `base` live (e.g. inside a converter that re-uses it), use
`subclone` and discard `base` separately.

**Audit + migration of `exchange_reverse` call sites:**

For each existing site, decide whether `subclone` (don't-consume,
single via) is a better fit than `exchange_reverse` (consume, dict
of all vias). The decision rule is "do we throw away every via
except one, and would we rather keep `base` alive than discard it?"

Sites to audit (from a grep):

- `fxtr2/conversion/converters/untyped_service.py:1025` —
  `_case3_extract_via`. Today consumes `base`, pops one via,
  discards the rest. Used as the converter body for case-3
  Dependent bridges. The transfer convention says the converter
  consumes `base`, so `subtake` is the natural fit — the helper
  collapses to a one-liner:
  ```
  return derived.subtake(base, target_token)
  ```
  At that point `_case3_extract_via` is just an alias for
  `subtake` and can be deleted; the dispatch-path call sites
  switch to `derived.subtake(...)` directly.
- `fxtr2/data/cap_ir_arena.py:840` — consumes `z_lease` and
  extracts the inputs to `exchange_forward` the next leg. The
  full dict is needed there (every input lease feeds the wire
  dispatch). Stay on `exchange_reverse`; no migration.
- `fxtr2/conversion/converters/untyped_service.py` `_ChainConverterHandle`
  — uses `exchange_forward`, not `exchange_reverse`. No change.
- Tests (`tests/test_leases/test_local_lease.py`,
  `tests/test_cap_ir_arena.py`,
  `tests/test_entity_ref_scope/test_derived_scope_acceptance.py`)
  — assertion-style use of `exchange_reverse`. Leave as-is so the
  primitive's existing semantics keep being exercised. Add a small
  block of unit tests for `subclone` itself.

**Tests for `subclone` / `subtake`** (add to
`tests/test_leases/test_local_lease.py`, in a new section after
`# exchange_reverse`):

- `subclone` returns a live lease on the requested input;
  `out_lease` stays usable afterwards (a follow-up `subclone` of
  the same lease for a different input still works; a follow-up
  `exchange_reverse` on the same lease still returns the full
  dict).
- `subtake` returns a live lease and consumes `out_lease`
  (subsequent operations on `out_lease` raise the same way as
  after `exchange_reverse`).
- `subtake` discards every via except the requested one (no
  lease leaks, fixture-checked).
- For both: target not in the inputs raises (clear error).
- For both: discarded / broken `out_lease` raises (same error
  surface as `exchange_reverse`).
- For both: lease from a different `Derived` raises `TypeError`
  (mirrors `exchange_reverse`'s own check).

**Files touched in step A:**

- `fxtr2/leases/local_lease.py` — add `subclone`.
- `fxtr2/leases/LOCAL_CODE_OVERVIEW.md` — list `subclone` alongside
  `exchange_forward` / `exchange_reverse`.
- `fxtr2/conversion/converters/untyped_service.py` —
  `_case3_extract_via` rewrite.
- `tests/test_leases/test_local_lease.py` — new tests.

Step A is independently shippable; the rest of the plan builds on
it.

---

## Step B — `_with_derived_lifetime` API change ✅

The new signature:

```
@classmethod
def _with_derived_lifetime(
    cls,
    *,
    include: Sequence[FxtrScope] = (),
    independent_leases: Sequence[Lease[Any]] = (),
    derived_from_leases: Mapping[LifetimeToken[Any], Lease[Any]],
    name: str | None = None,
    mountable: bool = False,
) -> Self
```

Renames + behaviour:

- `include_leases` → `independent_leases`. Same semantics: each lease's
  subject is registered as an `IndependentLocalBridge` ancestor entry.
  Caller-owned, transferred in.
- `derived_leases` → `derived_from_leases`. Same input-set semantics
  for the underlying `DerivedLocalLifetime` (these tokens cascade-end
  the output), *plus* each entry now registers as a
  `DependentLocalBridge` ancestor entry whose converter does the
  case-3 reverse exchange via the new `subclone` (or
  `_case3_extract_via`).
- `independent_leases` and `derived_from_leases` are union-disjoint by
  subject token. Overlap is rejected at construction (a token can't be
  both independent and derived-from); raises `ValueError`.
- `include` validation: every parent's token must appear in either
  `independent_leases` or `derived_from_leases`. The union is
  consulted at construction; a parent whose token is missing raises
  the same `ValueError` shape as today.

**Ancestor-entry installation.** During construction, after the
derived lifetime is minted and `_main_lease` is taken:

- For each entry in `independent_leases`, install an
  `IndependentLocalBridge(lease=that_lease.take())` as
  `is_ancestor_or_arena_owner=True` on `_cap_bridges[subject]`. This
  reuses the existing `_install_ancestor_entry` helper.
- For each entry in `derived_from_leases`, install a
  `DependentLocalBridge` ancestor entry as
  `is_ancestor_or_arena_owner=True`:
  - `leases={}` (the converter doesn't depend on any other input
    lease — `base` carries everything via its vias).
  - `converter = lambda input_leases, base:
        _case3_extract_via(self._derived, target_token, base)`
    closing over `self._derived` and `target_token = subject`.
  - The original lease passed in is consumed by `exchange_forward`
    inside the construction sequence — the ancestor entry doesn't
    need its own copy (the converter mints fresh vias from `base`
    on every call).

The two installation paths share a small helper that takes the bridge
and does the `_install_ancestor_entry` dance under
`_cap_bridges[token]`.

**Transitive ancestor walk.** `_populate_ancestor_bridges` walks each
parent's existing `is_ancestor_or_arena_owner=True` entries and copies
them into `self._cap_bridges`. Today it bails on Dependent / Pipelined
parent entries. The new behaviour:

- `IndependentLocalBridge` parent entry → unchanged (clone the
  stored `Lease`, install).
- `IndependentPipelinedBridge` parent entry → still raises `TypeError`
  (per the design decision).
- `DependentLocalBridge` parent entry → install a *new* Dependent
  entry on `self` whose converter is the **composition** of (a)
  call_scope's `exchange_reverse` for the parent's main token and
  (b) the parent's bridge converter. Concretely:
  - Take `base = Lease[self._token]`.
  - Run `parent_lease = _case3_extract_via(self._derived,
    parent_token, base)` to mint `Lease[parent_main_token]`.
  - Run `result = parent_anc_bridge.converter(input_leases,
    parent_lease)` to mint `Lease[grandparent_token]`.
  - Stored `leases` are clones of the parent entry's `bridge.leases`
    (one clone per entry, taken at population time).
  - Stored `converter` is a closure capturing
    `(self._derived, parent_token, parent_anc_bridge.converter)` and
    invoked per the chain above. Static — no live references to
    `parent_scope` beyond the converter pointer itself.
- `DependentPipelinedBridge` parent entry → still raises `TypeError`.

This composition relies on the parent token having its own ancestor
entry on `self` (we just installed it for `parent_main_token` above),
so the parent walk and the new entries cooperate.

`_mint_lease_via_ancestor_bridge` already has a Dependent branch; it
needs the `NotImplementedError` for Dependent ancestor entries removed.
The base-sourcing logic stays the same: when `ancestor_main_token is
self._token` we use `self._main_lease.clone()` as base; otherwise we
walk `self._cap_bridges[ancestor_main_token]` for our own ancestor
entry. With Dependent ancestor entries now legal, the base-sourcing
recursion needs to handle them too — an ancestor entry on `self` for
some intermediate token can itself be Dependent, so to mint
`Lease[intermediate_token]` for use as `base` we run that Dependent
entry's converter against `self._main_lease.clone()` (or our own
ancestor entry for whatever token *its* converter wants as base).

The cleanest shape: add an internal helper
`_mint_ancestor_lease(token) -> Lease[token]` that:

- If `token is self._token`: return `self._main_lease.clone()`.
- Else look up `self._cap_bridges[token]`, require
  `is_ancestor_or_arena_owner=True`, then:
  - `IndependentLocalBridge`: `bridge.lease.clone()`.
  - `DependentLocalBridge`: clone every `bridge.leases` entry +
    recursively call `_mint_ancestor_lease(self._token)` for the
    base, hand both to `bridge.converter`. (For our own entries
    populated by `_populate_ancestor_bridges`, the converter
    sources from `base = Lease[self._token]` — same as today's
    case-3 use.)
  - `IndependentPipelinedBridge` / `DependentPipelinedBridge`:
    `TypeError` (Pipelined ancestors stay disallowed).

Then `_mint_lease_via_ancestor_bridge` drops its inline
base-sourcing logic in favour of `_mint_ancestor_lease`.

**`attach_bridged_lowlevel_cap` `bridge=None` shortcut.**
Once derived-from-leases install ancestor entries for every input
token, the dispatch's "install a Dependent bridge for each
ServiceRef arg" step now collides on already-existing
`is_ancestor_or_arena_owner=True` entries on the same token.
Resolve by:

- The `bridge` parameter stays *required* (no default value), but
  its type widens to `LocalOrPipelinedBridge | None`. Passing
  `None` explicitly means "register the handle on the existing
  ancestor entry; error if no pre-existing entry for
  `handle.token`". Callers always have to spell out their intent —
  there's no implicit fallback.
- Calls that supply a concrete `bridge` keep working as today
  (used by lift, by the chain-bridge install on caller's side, and
  by the few RPC paths that bring fresh tokens into a non-derived
  scope).
- The dispatch path's per-arg attach becomes
  `attach_bridged_lowlevel_cap(arg.handle, UntypedService,
  bridge=None)` — the ancestor entry populated by
  `_with_derived_lifetime` already does the case-3 exchange.

The ancestor entry's `handles` set is currently empty by convention
(structural entries don't carry handles). Allowing handle
registration on top of an ancestor entry requires:

- The handle is added to `entry.handles` *and*
  `self._known_handles`.
- `forget_service` skips the entry-tear-down branch on
  `is_ancestor_or_arena_owner=True` entries (it already does), but
  still removes the handle from `entry.handles` /
  `_known_handles`.
- `_on_token_end` cleanup is unchanged (clears both maps).

**Files touched in step B:**

- `fxtr2/data/entity_ref_scope.py`:
  - `_with_derived_lifetime` parameter rename + ancestor-entry
    install for `derived_from_leases`.
  - `_populate_ancestor_bridges` — add Dependent-parent composition
    branch; keep Pipelined disallowed.
  - `_mint_lease_via_ancestor_bridge` — drop `NotImplementedError`
    on Dependent; refactor to use a new
    `_mint_ancestor_lease(token)` helper.
  - `attach_bridged_lowlevel_cap` — widen `bridge` to
    accept an explicit `None` (still required positionally / by
    keyword); `None` means "use existing ancestor entry, error if
    absent".
  - `forget_service` — confirm the handle-removal-only branch on
    ancestor entries is correct.
- `fxtr2/conversion/converters/untyped_service.py` — call-site
  rename only at this step (no behaviour change yet); the dispatch
  fix is step C.
- `tests/test_cap_ir_arena.py`,
  `tests/test_entity_ref_scope/test_derived_scope_acceptance.py`,
  `tests/test_entity_ref_scope/test_scope_lifecycle.py` — kwarg
  rename from `derived_leases` / `include_leases`.
- `fxtr2/data/LOCAL_CODE_OVERVIEW.md` — describe the
  ancestor-entry split (Independent vs Dependent) plus the new
  helper.

---

## Step C — Dispatch fix ✅

Change `_dispatch_body` to source the parent-lease backing through
`derived_from_leases` instead of `independent_leases`:

- `parent_scope` stays in `include` (so the parent-walk for service
  lift still finds handles mounted on the mount scope).
- The dispatch's `outer_lease` (subject = `parent_scope.main_token`)
  goes into `derived_from_leases` keyed by that token.
- `_take_input_leases(packet)` continues to produce a
  `{lease.subject: lease}` map for the arg tokens; the dispatch
  merges the outer lease into that mapping.
- `independent_leases` is now empty for the canonical dispatch path.

The per-arg `attach_bridged_lowlevel_cap` call (substage-6
step 4) drops its explicit bridge construction and passes
`bridge=None` (the explicit-`None` form introduced in step B) — the
ancestor entry from `_with_derived_lifetime` already covers the
case-3 exchange.

**The `fxtr_arena` install path.** The receiver-side
`lazily_attach_from_arena` for the arena handle currently uses an
explicit case-3 converter (`_make_case3_converter`). That path is
internal to `MemoryArena` and unchanged by step B — keep it as-is
unless we find it benefits from the ancestor-entry-on-call_scope
side-effect (audit during implementation).

**Tests already covering the path:**

- The full `service_layout` matrix (all four `_LAYOUTS`) including
  `same_scope`, with the xfail removed (step D).
- `TestSpawnerPattern.test_spawn_and_invoke_echo`,
  `test_spawn_and_store_in_one_call`,
  `test_chain_of_chain_returns`,
  `test_multi_input_arena_attach[_with_dependent_peer]`,
  `TestMutuallyReferencingScopes.test_dag_drains_cleanly` —
  re-run on `same_scope` after the xfail comes off.

**Files touched in step C:**

- `fxtr2/conversion/converters/untyped_service.py`:
  - `_dispatch_body`: route `outer_lease` into
    `derived_from_leases`; drop `include_leases`.
  - The arg-side attach: use `bridge=None`.
  - Update the docstrings that describe the
    `include_leases`/transferred-parent-lease story.

---

## Step D — Cleanup ✅

- Drop the self-mount `NotImplementedError` block in
  `FxtrScope.resolve_service`. Keep the `mountable` check.
- Drop the `same_scope` xfail block in
  `tests/test_conversion/test_untyped_service.py::service_layout`.
  Remove the `applymarker(...)` branch entirely; all three
  parametrisations should now pass cleanly.
- Update `resolve_into_mount_scope_drain_cycle.md` with a
  "## Resolution" section pointing at this plan, mirroring how
  `parent_loads_from_child_inheritance_cycle.md` was updated.
- Update `parts6_plan.md`'s "self-mount restriction" entries:
  - The "self-mount restriction (NotImplementedError)" subsection
    in Step 5 gets a follow-up note ("removed in part6post3").
  - The status line for `same_scope` in the test-status block:
    flip from xfail to passing.

**Files touched in step D:**

- `fxtr2/data/entity_ref_scope.py` — drop the
  self-mount `NotImplementedError`.
- `tests/test_conversion/test_untyped_service.py` — drop the
  xfail block.
- `plans/phase3/stage8c_user_caps/resolve_into_mount_scope_drain_cycle.md`
  — add resolution.
- `plans/phase3/stage8c_user_caps/part6_plan.md` — status update.

---

## Step E — New tests ✅

Two targeted tests in `tests/test_entity_ref_scope/`:

1. **Low-level derived-from-parent ancestor.**
   Construct an outer `FxtrScope` (unconditional) and a
   `_with_derived_lifetime` child whose `derived_from_leases` keys
   the outer's `_main_lease`. Assert:
   - The child's `_cap_bridges[outer._token]` is a
     `DependentLocalBridge` with `is_ancestor_or_arena_owner=True`
     and an empty `leases` dict.
   - `_mint_ancestor_lease(outer._token)` returns a fresh
     `Lease[outer._token]` whose grant matches outer's main grant.
   - Lift behaviour works the same as for `independent_leases`
     (mount a service on outer, resolve from the child, verify
     the proxy works).
   - On child `aclose`, the cycle does not deadlock — outer
     drains promptly even when the child is the one dispatching
     a sub-task that returned a chain ref. Mirrors the
     `same_scope` cycle scenario at the scope level.

2. **Transitive Dependent-ancestor walk.** Three-deep chain:
   - `S1` unconditional.
   - `S2 = _with_derived_lifetime(include=[S1],
     derived_from_leases={S1._token: S1._main_lease.clone()})`.
   - `S3 = _with_derived_lifetime(include=[S2],
     derived_from_leases={S2._token: S2._main_lease.clone()})`.
   Assert:
   - `S3._cap_bridges` has `is_ancestor_or_arena_owner=True`
     entries for both `S2._token` (direct) and `S1._token`
     (transitive composed).
   - The `S1._token` entry is a `DependentLocalBridge`.
   - `_mint_ancestor_lease(S1._token)` on `S3` returns a working
     `Lease[S1._token]`. Discard each of the minted leases
     immediately afterwards (lease-leak fixture must stay green).
   - Drain in reverse order (`S3` then `S2` then `S1`) completes
     cleanly with no deadlock.

Both tests live in
`tests/test_entity_ref_scope/test_derived_scope_acceptance.py`
or a sibling `test_derived_ancestor_bridges.py` (TBD during
implementation; whichever is less crowded).

The `same_scope` parametrisation of every existing
`tests/test_conversion/test_untyped_service.py` test is the
end-to-end coverage; nothing extra to add there beyond the xfail
removal.

---

## Implementation order

1. **Step A** — `subclone` + audit. Self-contained, lands first.
   Update `CODE_OVERVIEW.md` afterward.
2. **Step B** — `_with_derived_lifetime` parameter shape +
   ancestor-bridge generalization +
   `attach_bridged_lowlevel_cap` `bridge=None`. Run the
   full suite after; expect everything green except the same_scope
   xfails (still xfailed at this point).
3. **Step C** — dispatch fix. Run full suite; same_scope xfails
   should now flip to "unexpected pass" (xpass).
4. **Step D** — cleanup. Drop xfail markers, drop the self-mount
   error, doc updates.
5. **Step E** — add the two new derived-ancestor tests.
6. **Update `CODE_OVERVIEW.md`** as the final pass.

Each step ends with `uv run pytest -n auto` green.

---

## Open questions to revisit during implementation

- **`forget_service` interaction with handle-bearing ancestor
  entries.** Once an arg ServiceRef registers its handle on an
  ancestor entry, we want the handle to drop on `forget_service`
  but the ancestor lease to survive. The existing skip-on-ancestor
  branch handles this for empty-`handles` cleanup; verify the
  handle-removal step still runs correctly when the entry is
  ancestor-flagged.
- **`_mint_ancestor_lease` recursion bound.** Composed ancestor
  entries chain one converter per ancestor level. For deep
  ancestor chains the closure stack could grow; in practice
  scopes are at most a handful deep so this is fine, but
  flag if profiling shows otherwise.
- **`independent_leases` coverage of derived inputs.** A token
  that's both an `independent_leases` subject and a
  `derived_from_leases` key is rejected. Confirm no existing call
  site relies on the overlap (audit during step B).
