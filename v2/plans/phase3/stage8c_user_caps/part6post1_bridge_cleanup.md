# Substage 6, post-1 — Bridge taxonomy cleanup

> **Status: planning.** Refactor of the cap-bridge representation
> introduced in part6. No new behaviour — purely a representation
> change to set up future work (MemoryStore unification with the
> bridge system, restricted to the local cases only).

For background: see [part6_plan.md §1.1](part6_plan.md) for the
existing `_IndependentBridge` / `_DependentBridge` design, and
[lifetime_conversion_mechanics.md](lifetime_conversion_mechanics.md)
for the cycle-avoidance rules the bridge taxonomy is shaped around.

---

## 1. Motivation

Today's bridge representation has two variants:

- `_IndependentBridge(lease: StoredLeaseValue)` where
  `StoredLeaseValue = Lease[Any] | UnwrapForPipelining[Lease[Any], Any]`.
- `_DependentBridge(leases: dict[..., StoredLeaseValue], converter: DependentBridgeConverter)`
  where the converter returns `StoredLeaseValue`.

That is, the *Local vs Pipelined* axis (whether the lease is concrete
or a future-of-lease) is hidden inside `StoredLeaseValue` everywhere
it appears, both at-rest and in converter outputs. Consumers that
need to discriminate (e.g. `_build_return_resources`, the upcoming
MemoryStore unification) have to runtime-check at every use site.

We want to make that axis explicit by splitting into **four** bridge
variants on two orthogonal axes:

| | Independent | Dependent |
|---|---|---|
| **Local** (concrete output) | `IndependentLocalBridge` | `DependentLocalBridge` |
| **Pipelined** (output is a future-of-lease) | `IndependentPipelinedBridge` | `DependentPipelinedBridge` |

The motivating downstream change: parents/ancestors of a scope, and
the `CapIRArena` that the MemoryStore mounts, are always known to be
local — pipelining is hidden inside `CapIRArena` itself. A future
MemoryStore unification with the bridge system will only support
the two `Local` variants. Making the distinction static now makes
that work straightforward.

While we're in here we also consolidate some user-facing API rough
edges that accumulated during part6:

- `attach_independent_leased_lowlevel_cap` and
  `attach_dependent_keyed_lowlevel_cap` collapse into a single
  `attach_bridged_lowlevel_cap(handle, target_type, bridge)` that
  takes any of the four bridge variants directly.
- The user-facing `PipelinedLifetimeConverter` type alias and the
  `_make_single_key_converter` adapter are dropped — callers now
  build a bridge directly. `pipelined_lifetime_converter_from_handle`
  is dead code; remove it.
- `StoredLeaseValue` / `clone_stored_lease` / `discard_stored_lease`
  / `DependentBridgeConverter` move out of `leased_cap_common.py`.
  Each new bridge dataclass owns its own `clone()` /
  `discard_at_rest()` methods, replacing the two free helpers.

---

## 2. New module: `fxtr2/leases/lease_bridges.py`

Top-level public types:

- **`IndependentLocalBridge(lease: Lease[Any])`** — concrete lease
  on a token whose lifetime is independent of the holder's. Cloned
  on each lookup.
- **`IndependentPipelinedBridge(pipelined: UnwrapForPipelining[Lease[Any], Any])`**
  — a `(future, lease)` pair where the lease keeps the shared
  future alive. The lease is on a token independent of the
  holder's. Each "clone" reuses the shared future and clones the
  inner lease.
- **`DependentLocalBridge(leases: dict[LifetimeToken[Any], Lease[Any]], converter: DependentLocalBridgeConverter)`**
  — strictly concrete inputs (per Q1), strictly concrete output.
  Used when the holder's own lifetime gates lease minting and the
  conversion is purely local (e.g. `mount_lowlevel_cap_as_service`'s
  identity converter, the case-3 `derived.exchange_reverse` adapter).
- **`DependentPipelinedBridge(leases: dict[LifetimeToken[Any], Lease[Any] | UnwrapForPipelining[Lease[Any], Any]], converter: DependentPipelinedBridgeConverter)`**
  — possibly-pipelined inputs, pipelined output. Used by chain
  bridges that dispatch a wire converter handle.

Converter callable types:

- `DependentLocalBridgeConverter = Callable[[dict[LifetimeToken[Any], Lease[Any]], Lease[Any]], Lease[Any]]`
- `DependentPipelinedBridgeConverter = Callable[[dict[LifetimeToken[Any], Lease[Any] | UnwrapForPipelining[Lease[Any], Any]], Lease[Any]], UnwrapForPipelining[Lease[Any], Any]]`

Both follow the existing transfer convention: every input
(`leases` dict entries + `base` lease) must be consumed by the
converter; the return value is the new owner's responsibility.

Type aliases:

- `LocalBridge = IndependentLocalBridge | DependentLocalBridge`
- `LocalOrPipelinedBridge = LocalBridge | IndependentPipelinedBridge | DependentPipelinedBridge`

Per-bridge methods:

- `bridge.clone() -> Self` — produces a fresh independent at-rest
  copy (clones every stored lease; converter is shared by reference;
  for pipelined wrappers, the inner future is shared and the inner
  lease cloned).
- `bridge.discard_at_rest() -> None` — discards every stored lease
  on `self`. Idempotent — safe to call after a previous discard
  (the underlying `Lease.discard()` is idempotent and we clear the
  dict on `Dependent*` so repeat calls are no-ops).

These two methods replace `clone_stored_lease` / `discard_stored_lease`
and `_snapshot_bridge` / `_discard_bridge_resources` in `untyped_service.py`.

---

## 3. Removals from `leased_cap_common.py`

- `StoredLeaseValue` (no callers after migration).
- `clone_stored_lease`, `discard_stored_lease`.
- `DependentBridgeConverter`.
- `PipelinedLifetimeConverter` (no callers after migration — the
  `attach_dependent_keyed_lowlevel_cap` user-facing entry point is
  going away).
- `pipelined_lifetime_converter_from_handle` (already unused — only
  self-references in its own docstring).

This drops the `__all__` surface of `leased_cap_common.py` to its
core Cap-IR implementation: `copy_ir_value`, `discard_leases_in`,
`LocalPendingResultBox`, `IRFutureLocalImpl`, `spawn_ir_future_in_task_group`,
`IRHandleLocalImplBase`, `UnwrappedCallArg`. (We also tidy the module
docstring's "Common utilities …" wording so it no longer claims to
host bridge plumbing.)

---

## 4. `fxtr2/data/entity_ref_scope.py` migration

### 4.1 Imports / type changes

- Imports come from `fxtr2.leases.lease_bridges` instead of
  `leased_cap_common`.
- `_CapBridgeEntry.bridge: LocalOrPipelinedBridge`.

### 4.2 Drop these names

- `_IndependentBridge`, `_DependentBridge` (replaced by the four new types).
- `_identity_lease_converter` (now an inline `lambda leases, base: base`
  closure inside `mount_lowlevel_cap_as_service`).
- `_make_single_key_converter` (no longer needed — see API
  consolidation in §4.4).

### 4.3 `mount_lowlevel_cap_as_service`

Today: builds a `_DependentBridge(leases={STATIC: STATIC.lease()}, converter=_identity_lease_converter)`.

After: builds a `DependentLocalBridge(leases={}, converter=lambda leases, base: base)`.

The STATIC dummy entry was only there because the dict-form converter
contract required at least one entry; with the new types nothing
forces that and we can simplify.

### 4.4 API consolidation: `attach_bridged_lowlevel_cap`

Drop `attach_independent_leased_lowlevel_cap` and
`attach_dependent_keyed_lowlevel_cap`. Add:

```python
def attach_bridged_lowlevel_cap[T](
    self,
    handle: IRHandle[Any],
    service_type: ServiceTypeAnnotation[T],
    bridge: LocalOrPipelinedBridge,
) -> ServiceRef[T]:
    """Attach a low-level cap with a caller-supplied bridge.

    Takes ownership of every lease inside `bridge`. Idempotent on
    `handle.token` collision: if a bridge already exists for the
    token, the supplied `bridge` is discarded via
    `bridge.discard_at_rest()` and the existing entry is reused.
    """
```

Internally: validate `mountable`, look up `_cap_bridges[handle.token]`;
on miss create a fresh `_CapBridgeEntry(bridge=bridge, handles=set())`,
on hit call `bridge.discard_at_rest()` and reuse. Either way add the
handle to `entry.handles` and `_known_handles`, then return a fresh
`ServiceRef`.

The handle-vs-bridge token consistency check (lease subject ==
handle.token, currently in
`attach_independent_leased_lowlevel_cap`) is dropped — the new entry
point is genuinely low-level and the caller is expected to construct
a bridge whose converter produces leases on the right token.

### 4.5 `_mint_lease_via_ancestor_bridge`

Becomes a four-way branch:

| Bridge type | Behaviour |
|---|---|
| `IndependentLocalBridge` | clone `bridge.lease` → return `Lease[Any]` |
| `IndependentPipelinedBridge` | clone `bridge.pipelined` (share future, clone inner lease) → return `UnwrapForPipelining[Lease[Any], Any]` |
| `DependentLocalBridge` | clone every entry of `bridge.leases`, source `base` (per existing logic), call `bridge.converter(clones, base)` → return concrete `Lease[Any]` |
| `DependentPipelinedBridge` | same, return `UnwrapForPipelining[Lease[Any], Any]` |

Return type stays the same: `Lease[Any] | UnwrapForPipelining[Lease[Any], Any]`.

### 4.6 Ancestor handling — `_populate_ancestor_bridges`, `_install_ancestor_entry`

Today: ancestor bridges are restricted to `_IndependentBridge` (and
that bridge's stored lease must be concrete, not pipelined). The
`Dependent` and pipelined cases raise `NotImplementedError`.

After:
- `IndependentLocalBridge` — supported, the canonical case.
- `IndependentPipelinedBridge` — `TypeError` (a stronger statement
  than `NotImplementedError`: "ancestor bridges must be local"; per
  the user's preference in Q12).
- `DependentLocalBridge` / `DependentPipelinedBridge` —
  `NotImplementedError` (existing TODO; planned for once the
  MemoryStore unification lands).

### 4.7 Cleanup paths

- `forget_service` and `_on_token_end`'s cleanup loop call
  `entry.bridge.discard_at_rest()` instead of branching on bridge
  type and calling the free helpers.

---

## 5. `fxtr2/data/cap_ir_arena.py` migration

`_CapIRArenaBridgeState` today is structurally `_DependentBridge` plus
a `base_token` field (which records *which* derived input the per-call
`base` lease is sliced out of). Migration:

- `_CapIRArenaBridgeState(base_token, bridge)` — new shape; `bridge`
  is a `DependentLocalBridge | DependentPipelinedBridge`.
- `discard()` → `self.bridge.discard_at_rest()`.
- `snapshot()` → `_CapIRArenaBridgeState(base_token, bridge.clone())`.
- `CapIRArena.with_converter` factory: takes
  `bridge: DependentLocalBridge | DependentPipelinedBridge` directly
  (replacing the separate `bridge_leases` / `bridge_converter`
  parameters).
- `_make_y_lease`: dispatches on `bridge` type for the with-converter
  branch — concrete `Lease[Any]` for `DependentLocalBridge`,
  `UnwrapForPipelining[Lease[Any], Any]` for `DependentPipelinedBridge`.
  Same return type as today.

---

## 6. `fxtr2/conversion/converters/untyped_service.py` migration

### 6.1 `_build_return_resources` — four-way dispatch

Today: branches on `_IndependentBridge` (await any pipelined value,
ship as `new_indep_leases`) vs `_DependentBridge` / absent (ship as
chain handle).

After:
- `IndependentLocalBridge`: clone `bridge.lease` → `new_indep_leases`.
- `IndependentPipelinedBridge`: `await bridge.pipelined.future.wait(bridge.pipelined.lease.clone())`
  to resolve to a concrete lease → `new_indep_leases`.
- `DependentLocalBridge` / `DependentPipelinedBridge` / absent: ship
  via `_make_chain_converter_handle` (chain handle).

This matches Q7's "pre-resolved-Independent vs remote-converter-Dependent"
distinction the wire shape commits to.

### 6.2 `_ChainConverterHandle` — store a bridge snapshot

- Stores a `DependentLocalBridge | DependentPipelinedBridge` snapshot
  (in place of separate `_snapshot_leases` and `_bridge_converter`
  fields).
- `_run_request_async` dispatches on the bridge variant: clones the
  bridge's at-rest entries, invokes the converter, returns its result
  (concrete `Lease` or `UnwrapForPipelining[Lease, Any]`). Same return
  shape as today (`IRValue | UnwrapForPipelining[IRValue, Any]`).
- Token-end cleanup calls `bridge.discard_at_rest()`.

### 6.3 `_make_chain_converter_handle`

- "Absent bridge" case (no entry for `target_token` and `target_token
  is call_scope._token`) is normalized to a synthesized
  `DependentLocalBridge(leases={}, converter=lambda leases, base: base)`
  — same identity-with-empty-inputs shape as
  `mount_lowlevel_cap_as_service`.
- Otherwise, for `DependentLocalBridge` / `DependentPipelinedBridge`
  inputs, snapshot via `bridge.clone()`.
- An `IndependentLocalBridge` / `IndependentPipelinedBridge` here is
  an internal invariant violation (those ship as `new_indep_leases`
  in `_build_return_resources`, never reach this function).

### 6.4 `_build_chain_bridge` — always produces `DependentPipelinedBridge`

Its converter dispatches a wire handle and returns
`UnwrapForPipelining(...)`, so it's structurally pipelined.

The `_DependentMintSpec` splits in two:

- `_DependentLocalMintSpec(inner_converter: DependentLocalBridgeConverter, required_tokens)`.
- `_DependentPipelinedMintSpec(inner_converter: DependentPipelinedBridgeConverter, required_tokens)`.

Plus the existing `_UseBaseSpec` and `_IndependentMintSpec`. Build-time
dispatch on the input bridge variant: `IndependentLocalBridge` /
`IndependentPipelinedBridge` → `_IndependentMintSpec` (the merged
dict carries either kind under that token, see §6.5);
`DependentLocalBridge` → `_DependentLocalMintSpec`;
`DependentPipelinedBridge` → `_DependentPipelinedMintSpec`.

The closure dispatches on each spec's type when minting input leases
for the wire dispatch — both Local and Pipelined inner-converter
results are valid arg shapes for `wire_handle.dispatch` (which accepts
`Lease | UnwrapForPipelining[Lease]` per the standard contract).

### 6.5 Merged-leases dict — invariant note

The chain bridge's `merged_leases` dict (which becomes
`DependentPipelinedBridge.leases`) is typed
`dict[LifetimeToken[Any], Lease[Any] | UnwrapForPipelining[Lease[Any], Any]]`.
Sources:

- `_IndependentMintSpec` cloned from an `IndependentLocalBridge` →
  concrete `Lease`.
- `_IndependentMintSpec` cloned from an `IndependentPipelinedBridge` →
  pipelined.
- `_DependentLocalMintSpec` source — the inner bridge's `leases` dict
  is concrete (per Q1), so cloned entries are concrete.
- `_DependentPipelinedMintSpec` source — clones may be either kind.

The existing rare-collision behaviour (first-writer-wins on the
merged-dict slot) is preserved. If a `_DependentLocalMintSpec`'s
required tokens later resolve to a pipelined merged-dict entry (only
possible under collision), the closure raises a runtime
`AssertionError` rather than silently passing a pipelined value to a
local-typed converter. We document this as a known edge case (no
known callers exercise it; mainly a guard against future regressions).

### 6.6 `_install_arena_with_bridge` — accept `LocalOrPipelinedBridge | None`

The two arms today (Independent → wrap as a single-entry
`bridge_leases` dict + `indep_converter` closure; Dependent → pass
through) become four arms (or normalized through bridge construction):

- `bridge is None` (arena_token == home_main): unchanged passthrough.
- `IndependentLocalBridge(lease)`: build a `DependentLocalBridge`
  with a closure that discards `base`, returns the stored clone.
- `IndependentPipelinedBridge(pipelined)`: build a
  `DependentPipelinedBridge` with a closure that discards `base`,
  returns the cloned pipelined wrapper.
- `DependentLocalBridge` / `DependentPipelinedBridge`: pass through.

Hand the resulting `Dependent*Bridge` to
`CapIRArena.with_converter(..., bridge=...)` (per §5).

### 6.7 `_bridge_input_service_ref` — use `attach_bridged_lowlevel_cap`

Today: calls `attach_dependent_keyed_lowlevel_cap(handle,
UntypedService, STATIC_LIFETIME.lease(), case3_converter)`.

After: builds a `DependentLocalBridge(leases={STATIC.subject: STATIC.lease()}, converter=case3_adapter)`
and calls `attach_bridged_lowlevel_cap(handle, UntypedService,
bridge)`. The `case3_adapter` discards the STATIC entry and forwards
to `_make_case3_converter`'s callable.

(Alternative: drop the STATIC key entirely and use a no-input
converter — same shape as `mount_lowlevel_cap_as_service`. The
case-3 converter's `key` arg is unused today, so this is fine.)

### 6.8 Removed helpers

- `_snapshot_bridge` (replaced by `bridge.clone()`).
- `_discard_bridge_resources` (replaced by `bridge.discard_at_rest()`).
- The local `bridge_leases: dict[..., StoredLeaseValue]` /
  `bridge_converter: DependentBridgeConverter` aliasing in
  `_install_arena_with_bridge` (subsumed by the bridge-types branch).

---

## 7. Test rewrites

- `tests/test_entity_ref_scope/test_cap_bridges.py`:
  - Replace each `attach_independent_leased_lowlevel_cap(handle, T, lease)`
    with `attach_bridged_lowlevel_cap(handle, T, IndependentLocalBridge(lease=lease))`.
  - Replace each `attach_dependent_keyed_lowlevel_cap(handle, T, key_lease, converter)`
    with `attach_bridged_lowlevel_cap(handle, T, DependentLocalBridge(leases={key_lease.subject: key_lease}, converter=adapted_converter))`
    or, where the test's converter is statically pipelined,
    `DependentPipelinedBridge(...)`.
  - Where the test was constructed against the old `(key, base) -> ...`
    signature, adapt by wrapping in a
    `(leases_dict, base) -> ...` closure that pulls the key by token
    and forwards. (Same pattern as the old `_make_single_key_converter`
    but inlined per call site.)
- `tests/test_conversion/test_untyped_service.py`: the
  `attach_independent_leased_lowlevel_cap` calls (lines 342, 386, 1150)
  become `attach_bridged_lowlevel_cap(..., IndependentLocalBridge(lease=...))`.

No new tests are added for this refactor (it's pure representation
churn — the existing 1420+ tests are the regression net). If
implementation surfaces a behaviour gap we'll add a targeted test for
it.

---

## 8. Doc updates

- `fxtr2/leases/LOCAL_CODE_OVERVIEW.md`:
  - Add `lease_bridges.py` to the file overview table.
  - Drop the `PipelinedLifetimeConverter` /
    `pipelined_lifetime_converter_from_handle` subsection.
  - Add a brief subsection describing the four bridge variants and
    their Local/Pipelined × Independent/Dependent matrix.
- `CODE_OVERVIEW.md`:
  - Update the live-capability bridges paragraph (around line 399) to
    reference the four new types and the unified
    `attach_bridged_lowlevel_cap` API.
  - Update the "See also" paragraph that mentions
    `PipelinedLifetimeConverter` (around line 657) — the alias is gone;
    replace with a pointer to the new bridge types.

---

## 9. Implementation steps

1. [x] Create `fxtr2/leases/lease_bridges.py` with the four bridge
       dataclasses, type aliases, and converter-callable types. Each
       dataclass has `clone()` / `discard_at_rest()` methods.
2. [x] Update `leased_cap_common.py`: remove `StoredLeaseValue`,
       `clone_stored_lease`, `discard_stored_lease`,
       `DependentBridgeConverter`, `PipelinedLifetimeConverter`, and
       `pipelined_lifetime_converter_from_handle`. Refresh module
       docstring.
3. [x] Update `cap_ir_arena.py`:
   - [x] Replace `_CapIRArenaBridgeState` field shape (use a stored
         bridge object).
   - [x] Update `with_converter` factory signature and body.
   - [x] Update `_make_y_lease` to dispatch on bridge type.
4. [x] Update `entity_ref_scope.py`:
   - [x] Rewrite imports.
   - [x] Drop `_IndependentBridge` / `_DependentBridge` /
         `_identity_lease_converter` / `_make_single_key_converter`.
   - [x] Rewrite `mount_lowlevel_cap_as_service` for the new bridge
         shape.
   - [x] Replace `attach_independent_leased_lowlevel_cap` and
         `attach_dependent_keyed_lowlevel_cap` with
         `attach_bridged_lowlevel_cap`.
   - [x] Rewrite `_mint_lease_via_ancestor_bridge` four-way branch.
   - [x] Update `_populate_ancestor_bridges` /
         `_install_ancestor_entry` for the
         IndependentLocal-only restriction (TypeError on pipelined,
         NotImplementedError on dependent).
   - [x] Switch cleanup paths (`forget_service`, `_on_token_end`,
         `_lift_bridge_for_handle`'s "share existing local entry"
         path) to call `bridge.discard_at_rest()` /
         `bridge.clone()` directly.
5. [x] Update `untyped_service.py`:
   - [x] Rewrite imports and drop `_snapshot_bridge` /
         `_discard_bridge_resources`.
   - [x] Update `_build_return_resources` four-way dispatch.
   - [x] Update `_ChainConverterHandle` to store a bridge snapshot.
   - [x] Update `_make_chain_converter_handle` for the new bridge
         shape (synthesized `DependentLocalBridge` for the absent
         case).
   - [x] Update `_build_chain_bridge`: split `_DependentMintSpec`,
         return `DependentPipelinedBridge`, dispatch on input bridge
         variant when building specs.
   - [x] Update `_install_arena_with_bridge` — but in the cleanup
         pass we instead extended `CapIRArena.with_converter` to
         accept any `LocalOrPipelinedBridge` directly, so the
         Independent→Dependent wrapping is no longer needed (see
         §11).
   - [x] Rewrite `_bridge_input_service_ref` to use
         `attach_bridged_lowlevel_cap` directly.
6. [x] Update tests:
   - [x] `tests/test_entity_ref_scope/test_cap_bridges.py`.
   - [x] `tests/test_conversion/test_untyped_service.py`.
   - [x] `tests/test_cap_ir_arena.py` (the `_make_irhandle_bridge`
         helper switched to returning a `DependentPipelinedBridge`).
7. [x] Run the test suite (`uv run pytest -n auto`) and resolve any
       regressions.
8. [x] Update `fxtr2/leases/LOCAL_CODE_OVERVIEW.md` (file overview
       table + bridge-types subsection; drop the
       `PipelinedLifetimeConverter` block).
9. [x] Update `CODE_OVERVIEW.md` (bridge paragraph + "see also"
       paragraph + tree listing).
10. [x] Final test run; confirm all green.

## 11. Cleanup pass (post-migration consolidation)

After the bare migration was working, did a second pass to
consolidate APIs the user flagged as iteratively-sketched:

- **`CapIRArena.with_converter(... bridge=...)`** now accepts any
  `LocalOrPipelinedBridge`. `_make_y_lease` dispatches on all four
  variants — Independent paths discard the unused `base` and clone
  the stored at-rest value; Dependent paths run the converter.
  Removed the Independent→Dependent wrapping closures from
  `_install_arena_with_bridge` (~50 lines of synthesized closures
  collapsed to a single pass-through).
- **`_make_case3_converter` / `_Case3Converter` deleted.** The
  shared work was the validation step + the per-call
  `exchange_reverse` walk. Replaced with two small helpers
  (`_validate_case3_inputs`, `_case3_extract_via`) called inline
  from `_bridge_input_arena` (which builds a sync
  `LifetimeConverter` for the memory store) and
  `_bridge_input_service_ref` (which builds a `DependentLocalBridge`
  directly). No more wrap-then-rewrap.
- **`_snapshot_bridge` / `_discard_bridge_resources` deleted.**
  Replaced by per-bridge `clone()` / `discard_at_rest()` methods at
  every call site.

---

## 10. Known unknowns / things to revisit

- The `_DependentLocalMintSpec` runtime assertion against pipelined
  merged-dict entries (§6.5) is a guard for a state that no current
  code path produces. If the assertion ever fires in practice we'll
  need to upgrade the merged-dict policy from "first-writer-wins" to
  something like "promote-to-strictest" — out of scope here.
- The `attach_bridged_lowlevel_cap` API drops the
  `lease.subject == handle.token` consistency check. If we find we
  miss it in practice (e.g. tests start failing in confusing ways
  because callers built ill-typed bridges) we can add a per-variant
  invariant check at attach time. Out of scope unless it bites.
