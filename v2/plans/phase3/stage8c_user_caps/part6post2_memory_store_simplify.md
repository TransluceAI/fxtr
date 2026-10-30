# Memory store simplification (part 6, post 2)

## Motivation

The current `MemoryStore` (`fxtr2/data/memory_arena.py`) carries three layers
of bookkeeping that have accreted across refactors:

1. **Per-token DAG**: `register_token(parents=...)` records each token's
   parents and full ancestor closure, holding lease clones for each ancestor
   so parents structurally outlive children.
2. **Antichain ownership**: each node tracks an antichain of tokens that
   "own" it, with three implicit access cases (case 1 = ancestor in the
   antichain, case 2 = inheritance arena claim, case 3 = direct ownership +
   dep claims). Adding a claim runs domination checks; adding an ancestor
   evicts dominated descendants.
3. **Foreign-arena bridges**: each `(X_reg, Y)` pair stores a
   `(key_lease, converter)` to mint `Lease[Y]` per load, with its own
   break-callback machinery.

This produces several costs:

- **The parent-loads-from-child cycle**
  (`parent_loads_from_child_inheritance_cycle.md`). When a same-store ancestor
  installs an inheritance bridge that loops back to a descendant, the
  domination logic deletes the descendant's case-3 ownership, and loads spin
  re-establishing it forever. The fix surface is structural — domination is
  too aggressive in same-store/cross-bridge configurations.
- **Service composition friction**. Lease bridges (`lease_bridges.py`) now
  exist in their own right and have an established semantic for
  Independent/Dependent × Local/Pipelined; MemoryStore reproduces a parallel
  bespoke shape with `(key_lease, converter)`.
- **FxtrScope already owns parent-tracking** — at the `_cap_bridges` level,
  every scope holds a clone of each ancestor's main lease. The MemoryStore
  duplicates that with `parent_leases` / `ancestor_leases`, and the two
  views drift on edge cases.

The substages in `plans/phase3/stage8c_user_caps/` (services, RPC dispatch)
now require richer cross-scope lifetime structure than the antichain model
expresses cleanly. Rather than adding more special-cases, this plan replaces
the antichain + DAG + bespoke bridges with three simpler ideas:

- **Independent per-token ownership.** Each token owns nodes directly. No
  domination, no antichain, no ancestry walk.
- **Bridges are `LocalBridge`.** Foreign-arena attaches use the same
  `IndependentLocalBridge` / `DependentLocalBridge` taxonomy as `FxtrScope`
  cap-bridges.
- **Same-store inheritance is a transparent sentinel.** When the bridged
  source is "this same MemoryStore under another token Y," we elide the
  arena fork and store a sentinel; loads through the sentinel just recurse
  under Y. Callers don't see the distinction.

After this lands, `parent_loads_from_child_inheritance_cycle.md`'s `xfail`
goes away and `track_from_memory_arena` works for arbitrary bridge sets
(not only memory-store ancestry).

## Conceptual model after the refactor

- A **token** is registered with `MemoryStore.register_token(token, *, name="")`.
  The store is a subscriber to the token's end-callback; it does not record
  parents, ancestors, or hold any external lease clones for the token.
- Each **node** has two independent token-keyed sets:
  - `owned_tokens: set[LifetimeToken]` — tokens that have direct ownership
    (data is in memory, deps are also covered for that token). Same as
    today's "case 3," renamed for clarity.
  - `inheritance_bridges: dict[X_reg, _InheritanceEntry]` — for each token
    `X_reg`, *if* X_reg accesses this node by going through some other
    source, the entry says how. Same role as today's `inheritance_claims`,
    but each entry carries both a *source descriptor* and points at a
    *bridge record* keyed by `(X_reg, Y)`.
- A token T has access to node N **iff** `T in N.owned_tokens` or
  `T in N.inheritance_bridges`. There is no implicit ancestor access:
  ancestry is no longer modelled inside the store.
- The **lifetime ordering** between scopes (parent must outlive child) is
  enforced entirely by `FxtrScope`'s ancestor `_cap_bridges`, which already
  hold parent-lease clones independent of the MemoryStore. Removing the
  store's `parents=` doesn't relax any guarantee — it removes a redundant
  copy.

## New types and field renames

In `fxtr2/data/memory_arena.py`:

```
@dataclasses.dataclass(frozen=True)
class _SameStoreSentinel:
    """Marker: 'load N under token Y in this same MemoryStore'."""
    target_token: LifetimeToken[Any]

@dataclasses.dataclass
class _InheritanceEntry:
    source: ReadableArena[Any] | _SameStoreSentinel
    # Y is derivable: source.token (foreign) or source.target_token (sentinel).
```

`MemoryStoreNode`:
- `inheritance_claims: dict[X_reg, ReadableArena]` → `inheritance_bridges: dict[X_reg, _InheritanceEntry]`.
- Drop the `antichain` property.
- Other fields unchanged.

`LifetimeTokenData` (drop / rename):
- Drop: `parents`, `parent_leases`, `ancestor_leases`.
- Rename: `nodes_in_antichain` → `node_associations` (set of node IDs this
  token has *any* presence on — owned or inheritance — used as the
  reverse index for token-end teardown).
- Replace: `foreign_leases: dict[Y, ForeignLeaseData]` → `bridges: dict[Y, _BridgeRecord]`.

`_BridgeRecord` (replaces `ForeignLeaseData`):
- `bridge: LocalBridge` (the `IndependentLocalBridge | DependentLocalBridge` union).
- `claimed_ids: set[FxtrID]` — node IDs whose `inheritance_bridges[X_reg]`
  uses this `(X_reg, Y)` record.
- `deregister_callbacks: list[Callable[[], None]]` — one per stored at-rest
  lease in the bridge; called on intentional teardown to detach the
  broken-callback subscriptions.
- `break_reason: BaseException | None`.

Removed helpers (no longer meaningful): `_is_strict_ancestor`,
`_is_ancestor_or_equal`, `_is_dominated_by_antichain`,
`_remove_dominated_from_antichain`, `_has_case3_access` (replaced by
`token in node.owned_tokens`), `_is_node_claimed_by` (replaced by
`token in node.owned_tokens or token in node.inheritance_bridges`),
`_make_base_lease` (no ancestor source for bases), the `parents=`
parameter on `register_token`.

## API changes (public surface)

### `MemoryStore.register_token(token, *, name="")`

No `parents`. Allocates an empty `LifetimeTokenData` row and subscribes
`_on_token_end`. Idempotent-on-collision behaviour unchanged (still raises
on re-register).

### `MemoryStore.bare_lazily_attach_from_arena(my_lease, node_ids, source, bridge: LocalBridge)`

Replaces today's `(key_lease, converter)` parameters with a single
`bridge: LocalBridge`.

- `source: ReadableArena[Y]` is still the foreign source.
- The store **detects same-store**: if
  `isinstance(source, MemoryArena) and source._store is self`, it switches
  to *sentinel mode* — does not call `source.copy_to_new_arena`, does not
  track in `source`'s liveset, and stores
  `_InheritanceEntry(source=_SameStoreSentinel(source.token))` per node.
- Otherwise (foreign mode): forks the source via `source.copy_to_new_arena`
  exactly as today and stores
  `_InheritanceEntry(source=fork)` per node.
- In both modes, registers `(X_reg, Y)` in `_token_data[X_reg].bridges`,
  taking ownership of every at-rest lease in `bridge` and installing
  break-callbacks. On `(X_reg, Y)` collision, the new bridge is discarded
  via `bridge.discard_at_rest()` and the existing record is reused.
  Bridges are fixed for the life of the X_reg row; mismatched re-attaches
  are silently de-duped on Y identity (consistent with the existing
  fungible-per-pair model).

### `MemoryArena.lazily_attach_from_arena(my_lease, node_ids, source, bridge)`

Adapter: same-store detection is internal to the store; the arena just
threads through and tracks `node_ids` in its liveset.

### `MemoryStore.bare_track_from_memory_arena(...)`

Stub that raises `NotImplementedError` in step A. New signature and
algorithm land in step C (see below).

## Internal helpers

`_invoke_bridge(x_reg, y, base_lease) -> Lease[Y]`:
- Reads the `_BridgeRecord` at `_token_data[x_reg].bridges[y]`.
- Raises `record.break_reason` after discarding `base_lease` if any input
  lease has broken.
- `IndependentLocalBridge`: discards `base_lease`; returns
  `record.bridge.lease.clone()`.
- `DependentLocalBridge`: clones every value in `record.bridge.leases`;
  calls `record.bridge.converter(cloned_inputs, base_lease)`.

`_register_bridge(x_reg, y, bridge) -> _BridgeRecord`:
- Idempotent on `(x_reg, y)`: if a record exists, calls
  `bridge.discard_at_rest()` and returns the existing record.
- Otherwise, takes ownership: registers a `register_broken_callback` on
  every at-rest lease (just `bridge.lease` for Independent; every entry of
  `bridge.leases.values()` for Dependent). Each callback sets
  `record.break_reason`, logs the affected `claimed_ids`, and remembers
  its deregister handle in `record.deregister_callbacks` for clean
  teardown.

`_release_bridge_claim(x_reg, y, node_id)`: drops `node_id` from
`record.claimed_ids`; if the set becomes empty, walks
`record.deregister_callbacks`, then `bridge.discard_at_rest()`, and
deletes the entry.

`_on_token_end(token)`:
- Walks `token_row.node_associations`. For each node:
  - Discards `token` from `node.owned_tokens`.
  - Pops `node.inheritance_bridges[token]` (if any) — for foreign mode,
    collects the source arena to close at the end (same as today's
    "arenas_to_close" set, deduped by `id`); for sentinel mode, just
    drops the entry (no arena to close).
  - If both maps are now empty for the node, frees it.
- Walks `token_row.bridges` and tears each down (deregister callbacks +
  `bridge.discard_at_rest()`).
- No parent/ancestor lease cleanup.

## Loading

`_ensure_own(node_id, token, caller_lease)`:
- Fast path: `if token in node.owned_tokens: return node`.
- Look up `entry = node.inheritance_bridges.get(token)`. If `None`, error
  (no implicit ancestor access).
- `y = entry.source.token` (foreign) or `entry.source.target_token`
  (sentinel).
- `y_lease = self._invoke_bridge(token, y, caller_lease.clone())`.
- **Sentinel branch**:
  - Recursively `await self._ensure_own(node_id, y, y_lease)` (or call
    `_ensure_own_derived` for derived tags). Y now owns the node.
  - For each dep `D` that's now in `node_y.data.requires` (and the
    `unwraps_to`, ctx, backstep links per node tag), if `token`
    doesn't already have access, install a sentinel inheritance entry
    on `D` for `(token, y)` — cloning the same bridge once per dep.
    This mirrors today's recursive `_adopt_inheritance_arena_claim`,
    differing only in that the source is the sentinel, not a forked
    arena.
  - Add `token` to `node.owned_tokens` (data already in memory; deps now
    have token-level inheritance entries).
- **Foreign branch** (existing logic, lightly retargeted):
  - `flat = await entry.source.load_flat(y_lease, node_id)`.
  - Adopt every dep into `token`'s view as inheritance entries pointing
    at `entry.source` with the same `(token, y)` bridge record.
  - `_bare_store_flat_impl(flat, token)` to materialise data and grant
    `token` ownership.

`_ensure_own_derived` (sync, for c/p/u/. /!) follows the same shape but
uses the source arena's sync provenance helpers — confirmed behaviour
unchanged per Q2(c) in the discussion (recursion still happens; needed
for sync provenance methods).

## Storing

`_bare_store_impl`'s p-node branch today walks `_pick_inheritance_claim`
(which already returns a single arena) to find the parent p-node when
`ctx_node` isn't owned. After the refactor, the same path uses
`node.inheritance_bridges[token]` directly — no ancestor walk. The
"data exists from another token without inheritance arena" branch
(currently around line 1450) is **removed**: in the new model, "another
token" never has implicit access here, so the only ways to reach this
point are (a) the token has its own inheritance entry — handled — or
(b) the token has nothing — error.

`_add_owned_token` collapses to `node.owned_tokens.add(token);
token_row.node_associations.add(node.my_id)`. Dedup is just set
membership; no domination check, no eviction.

## FxtrScope migration (step B)

- Drop `parents=` from `FxtrScope.__init__`'s `register_token` call.
  `_populate_ancestor_bridges` (already runs at construction) keeps
  installing `IndependentLocalBridge(parent_main_lease.clone())` entries
  in `_cap_bridges` for each parent + transitive ancestor — these are
  what enforces parent-outlives-child after the store stops doing it.
- Rename `_CapBridgeEntry.is_ancestor` → `is_ancestor_or_arena_owner`.
  Update `forget_service`'s skip-on-cleanup check accordingly. Same
  semantics, broader name.
- Rewrite `lift(node)`: walk parents (existing logic) to find the parent
  whose arena tracks the node and which ancestor token owns it; clone
  the matching `IndependentLocalBridge` from `self._cap_bridges[anc_token]`;
  call
  `self._main_arena.lazily_attach_from_arena(
       self._main_lease.clone(), [node_id], parent_arena, cloned_bridge)`.
  The store auto-detects same-store and stores a sentinel; downstream
  loads find the entry via the new flat lookup.
- Add `_register_external_arena_bridge(token, lease)` (or similar) for
  namespaces / external arena owners that previously relied on the
  `parents=` mechanism. Installs an `is_ancestor_or_arena_owner=True`
  entry in `_cap_bridges` so the bridge survives empty `handles` sets.
- Rework the remote-arena mount path
  (`_install_arena_with_bridge` in `fxtr2/conversion/converters/untyped_service.py`):
  the synthesized local token (`D = derive({home_main})` per the existing
  flow) is registered via `register_token` (no parents); the
  `CapIRArena.with_converter` shape stays; the `(STATIC_LIFETIME.lease(),
  passthrough_converter)` / `(STATIC_LIFETIME.lease(), bridge_dispatch_converter)`
  argument pair becomes a single `LocalBridge` (`IndependentLocalBridge`
  for the passthrough case, a `DependentLocalBridge` carrying the chain
  inputs for the bridged case). The arena+token pair lives only in the
  MemoryStore-side bridge record; no FxtrScope-level entry needed (per
  Q4 — "bound to the memory store directly and not saved on the scope").
- Update `_deep_fetch_scan_sources` (`entity_ref_scope.py:2904`) to
  construct a `LocalBridge` from each `DeepFetchSourceEntry`'s
  `(key_lease, converter)` instead of passing them directly. Where the
  source arena is structurally a parent / outer scope (case 1 in
  `lifetime_conversion_mechanics.md`), this becomes
  `IndependentLocalBridge(key_lease)`; otherwise it stays
  `DependentLocalBridge` carrying the original key lease and converter
  closure. Audit each existing call site during the implementation.

## `track_from_memory_arena` rewrite (step C)

New signature on `MemoryStore`:

```
def bare_track_from_memory_arena(
    self,
    dest_lease: Lease[B_dest],
    src_lease: Lease[B_src],
    entity_id: FxtrID,
    *,
    dest_inheritance_bridges: Mapping[LifetimeToken, LocalBridge],
) -> None
```

`dest_inheritance_bridges` is the set of bridges `dest_token` is allowed
to adopt for free, keyed by the bridge's target token (the Y the bridge
mints leases on). Cloned-in copies will be installed as inheritance
records under `dest_token` if the walk needs them.

Algorithm — greedy expansion with sentinel-loading (per the user's
"less complicated way"):

```
worklist = [entity_id]
visited = set()
while worklist:
    nid = worklist.pop()
    if nid in visited: continue
    node = self._entity_states[nid]

    # (1) Already covered by dest.
    if dest_token in node.owned_tokens: visited.add(nid); continue
    if dest_token in node.inheritance_bridges:
        visited.add(nid); continue

    # (2) Dest can adopt one of node's inheritance bridges.
    for x_reg, entry in node.inheritance_bridges.items():
        y = entry.source_token()
        bridge = dest_inheritance_bridges.get(y)
        if bridge is not None:
            # Adopt: install a (dest_token, y) inheritance record on
            # this node, cloning `bridge`. Same-store detection still
            # applies — if the source is a sentinel, the new entry is
            # also a sentinel; if foreign, we register against the same
            # source arena (or a fresh fork; see open question below).
            self._adopt_dest_inheritance(node, dest_token, y, entry, bridge)
            visited.add(nid)
            break
    else:
        # (3) No dest-adopt match. Need to expand.
        if node.data is None:
            # No data; try sentinel-loading any same-store inheritance
            # entries to bring the node into the source's view (sync
            # because sentinels never block).
            progressed = self._sentinel_load_any(node, src_token)
            if not progressed:
                raise RuntimeError(
                    f"track_from_memory_arena: node {nid} has no in-memory "
                    "data and no dest-bridgeable inheritance source "
                    "(would require async load)."
                )
            # After sentinel-load, retry the same node next iteration.
            worklist.append(nid)
            continue
        # Has data — we can grant dest ownership; recurse on deps.
        self._add_owned_token(node, dest_token)
        for dep in node.data.requires:
            if dep not in visited: worklist.append(dep)
        if node.unwraps_to is not None and node.unwraps_to not in visited:
            worklist.append(node.unwraps_to)
        visited.add(nid)
```

Notes:
- **Sentinel-load** means: pick any inheritance entry whose source is a
  `_SameStoreSentinel`, run `_ensure_own` (sync derived path or sync
  shortcut for d/n with already-loaded data) under the sentinel's
  target token Y. Because Y lives in this same store, this is a sync
  pointer-chase; if it turns out Y itself doesn't have access, the call
  raises (which surfaces as the same "would require async load" error).
- **Dest-adopt with foreign source.** When an inheritance entry's source
  is a foreign `ReadableArena[Y]` and dest has a bridge for that same Y,
  we register the adopted bridge against the same arena object — the
  arena is fungible per-Y under the same-source-arena-binding invariant
  the docstring already references. (Confirm / refine during
  implementation.)

`MemoryArena.track_from_memory_arena` and
`FxtrScope.track_from_scope` thread the dest's bridges through. For
`FxtrScope.track_from_scope`, the bridges are the
`is_ancestor_or_arena_owner=True` entries from `self._cap_bridges`,
each cloned at call time.

## Implementation steps

### Step A — MemoryStore refactor (memory-store tests only)

The breakage radius is intentionally large here; we don't try to keep
FxtrScope working. Run `tests/test_memory_arena/` in isolation while
implementing.

1. Add `_SameStoreSentinel`, `_InheritanceEntry`, `_BridgeRecord` types.
2. Rewrite `LifetimeTokenData` (drop parents/ancestor fields, rename
   `nodes_in_antichain` → `node_associations`, replace `foreign_leases`
   with `bridges`).
3. Rewrite `MemoryStoreNode` (`inheritance_claims` → `inheritance_bridges`,
   drop `antichain`).
4. Rewrite `register_token` — drop `parents`. Don't update FxtrScope's
   call site if it imports cleanly without; only patch FxtrScope here if
   it would otherwise break the memory-store test imports.
5. Replace `bare_lazily_attach_from_arena` signature; add same-store
   detection branch.
6. Add `_invoke_bridge`, `_register_bridge`, `_release_bridge_claim`
   helpers.
7. Rewrite `_ensure_own` and `_ensure_own_derived` with the new
   sentinel/foreign branches (no ancestor walk).
8. Rewrite `_adopt_inheritance_arena_claim` (probably renamed
   `_adopt_inheritance_entry`) for the new entry shape; drop
   antichain/domination.
9. Rewrite `_bare_store_impl`'s p-node branch for the no-ancestor
   model; drop the "data from another token, no inheritance" raise.
10. Rewrite `_on_token_end` (no parent/ancestor lease cleanup; walk
    `node_associations`; tear down `bridges`).
11. Stub `bare_track_from_memory_arena` (and `MemoryArena.track_from_memory_arena`)
    to `raise NotImplementedError`. **Preserve the old impl** as a
    commented block (or at the bottom of the file under a clear
    "removed" header) so step C can reference the worklist shape.
12. Drop dead helpers: `_is_strict_ancestor`, `_is_ancestor_or_equal`,
    `_is_dominated_by_antichain`, `_remove_dominated_from_antichain`,
    `_has_case3_access`, `_is_node_claimed_by`, `_make_base_lease`.
13. xfail every memory-store test that exercises `track_from_memory_arena`.
14. Update the remaining memory-store tests for the new APIs (any
    that pass `parents=`, exercise antichain/domination, or assert on
    case-1 behaviour). Some will need to construct sentinel inheritance
    explicitly via `bare_lazily_attach_from_arena` to set up the
    pre-conditions the old `parents=` provided implicitly.
15. Confirm `tests/test_memory_arena/` passes (modulo xfailed tests).
    The rest of the suite is expected to be broken at this point;
    don't run it.
16. Update `fxtr2/data/LOCAL_CODE_OVERVIEW.md` (if present; otherwise
    update `CODE_OVERVIEW.md`) for the new memory-store model.

### Step B — FxtrScope migration

Goal: everything except the xfailed `track_from_*` tests passes.

1. Drop `parents=` from `FxtrScope.__init__`'s `register_token` call.
2. Rename `_CapBridgeEntry.is_ancestor` → `is_ancestor_or_arena_owner`;
   update `forget_service`.
3. Rewrite `lift(node)` to install a same-store sentinel via the new
   `lazily_attach_from_arena` API using a clone of the matching
   ancestor `IndependentLocalBridge`.
4. Add `_register_external_arena_bridge` helper (mirroring
   `_install_ancestor_entry` but with `is_ancestor_or_arena_owner=True`
   for non-ancestor external arenas).
5. Update `_install_arena_with_bridge` to construct a `LocalBridge` and
   pass it through the new `lazily_attach_from_arena` signature; remove
   the `(STATIC_LIFETIME.lease(), …converter…)` shape. Verify the
   pass-through (`bridge=None` today) and bridged shapes still produce
   the expected store-side records.
6. Update `_deep_fetch_scan_sources` to build a `LocalBridge` from
   each `DeepFetchSourceEntry`. Audit each call site that registers a
   `DeepFetchSourceEntry` to confirm the right Independent vs Dependent
   shape.
7. xfail every `FxtrScope` test that exercises `track_from_scope`.
8. Update / fix all other FxtrScope and integration tests for the new
   lift mechanics and bridge shapes. Run the full suite (`uv run
   pytest -n auto`) and confirm only the `track_from_*` xfails are
   outstanding.
9. Remove the `xfail` on
   `test_returning_new_fxtrid_loads_through_chain_bridge[same_scope]`
   referenced from `parent_loads_from_child_inheritance_cycle.md`;
   verify it passes (this is the structural-cycle resolution).
10. Update `CODE_OVERVIEW.md` and `fxtr2/data/LOCAL_CODE_OVERVIEW.md` /
    `fxtr2/leases/LOCAL_CODE_OVERVIEW.md` (note the new
    MemoryStore/FxtrScope boundary; the lease-mechanics doc's three
    cases still apply unchanged — case 1 of that doc is unrelated to
    the deleted "case 1" of the antichain).

### Step C — `track_from_memory_arena` and `track_from_scope` ✅

Landed. Final shape:

1. ✅ `MemoryStore.bare_track_from_memory_arena(dest_lease, src_lease,
   entity_id, *, dest_inheritance_bridges)` implements the discover/save
   worklist (mirroring `bare_copy_from_arena`'s shape, sync). Helper
   `_dest_adopt_inheritance` handles the bridge-adopt case; foreign
   sources are forked one-per-node via `copy_to_new_arena`, sentinels
   propagate directly. New work classes:
   `_DiscoverTrackWork` / `_SaveOwnedTrackWork` / `_SaveAdoptTrackWork`.
2. ✅ `MemoryArena.track_from_memory_arena` takes `dest_inheritance_bridges`
   as a kwarg (defaults to `{}` for callers that don't need adopt).
3. ✅ `FxtrScope.track_from_scope` passes its `is_ancestor_or_arena_owner=True`
   cap-bridges (filtered to `LocalBridge` variants — pipelined silently
   skipped) directly to the arena call. The originals stay in
   `_cap_bridges`; `_register_bridge` clones internally on adopt.
4. ✅ Stub removed; no preserved old impl needed.
5. ✅ All `xfail` markers on `TestTrackFromMemoryArena` and the
   `test_track_from_scope_*` tests removed; full suite green
   (1932 passed, 37 xfailed — the remaining xfails are unrelated
   chain-bridge / cycle resolutions).
6. ✅ `CODE_OVERVIEW.md` updated.

### Step D — final docs pass

7. Update `CODE_OVERVIEW.md`.

## Open questions / things to revisit during implementation

- ✅ **Foreign-source dest-adopt in `track_from_memory_arena`.** Resolved
  in step C: we **fork** the source arena via `copy_to_new_arena` so
  dest gets its own private liveset. The arena-X_reg-binding invariant
  requires each arena to belong to exactly one X_reg so that token's
  loads can freely abandon nodes; sharing the source arena would
  violate that. One fork per node-adopt for now (batching per
  `(dest, Y)` is a known follow-up if the per-node cost matters).

- **`copy_to_new_arena` invocation in foreign-mode attach.** Currently
  `bare_lazily_attach_from_arena` always calls `copy_to_new_arena`
  once over all node_ids. The new API still does this in foreign mode.
  Confirm during step A that the per-call mint cost stays the same
  (i.e. one wire round-trip for `CapIRArena` sources, not N).

- **Break-callback semantics for multi-input `DependentLocalBridge`.**
  We register a callback per stored input lease and any one breaking
  marks the record as broken. Confirm this is the desired union
  behaviour (rather than, e.g., requiring a quorum). Consistent with
  today's single-key behaviour; surfaces during step A.

- **Tests that exercised `parents=` precisely to assert
  parent-outlives-child.** These need rewriting to assert the same
  property by holding scope-level lease clones rather than
  store-level parents. Audit during step A.

- **`fxtr2/conversion/converters/untyped_service.py`'s
  `_install_arena_with_bridge` assumes the
  `(STATIC_LIFETIME, passthrough_converter)` / converter-bridge
  bottoming-out at the memory store. After step B, the function
  builds a `LocalBridge` directly — verify with the substage 6 tests
  that the constructed `LocalBridge` produces equivalent
  `Lease[D]`-minting behaviour through `CapIRArena._make_y_lease`.

- **`MemoryArena._key`-keyed liveset cleanup.** The new model doesn't
  change the arena/liveset story — confirm that no antichain helper
  was implicitly relied on by `_track_from_store` /
  `track_in_arena`.

## Why this resolves the parent-loads-from-child cycle

Today's cycle hinges on `_remove_dominated_from_antichain` evicting
`call_scope.main_token` from `node.owned_tokens` when
`caller_scope.main_token` (its memory-store ancestor) installs an
inheritance entry. After the refactor:

- There is no `_remove_dominated_from_antichain`. Tokens own
  independently.
- `caller_scope` having an inheritance entry on the node does **not**
  affect `call_scope`'s ownership.
- The load loop terminates immediately once any token has direct
  ownership; no spinning re-establishment.

The same change also lifts the ancestor-only restriction in
`_pick_inheritance_claim` — there is no `_pick_inheritance_claim`
anymore, just a direct dict lookup keyed by the requesting token.

## Cross-references

- `parent_loads_from_child_inheritance_cycle.md` — the cycle this
  refactor resolves. Update or delete after step B.
- `lifetime_conversion_mechanics.md` — the three converter cases
  (case 1 / 2 / 3 of *that* doc) still apply; they describe shape of
  the converter inside a `LocalBridge`, which is the new wrapper. The
  doc itself doesn't need rewriting, only its two reference-table
  callers (the `LifetimeConverter` docstring in `abstract_arena.py`
  and the recipe in `fxtr2/leases/LOCAL_CODE_OVERVIEW.md`) need a
  signature touch-up.
- `part6post1_bridge_cleanup.md` — preceding bridge cleanup; this
  plan extends the same `LocalBridge`-as-canonical-shape direction
  into the MemoryStore.
- `fxtr2/leases/LOCAL_CODE_OVERVIEW.md` — describes the
  `LocalBridge` taxonomy; references will tighten once the
  MemoryStore is also a consumer of the same types.
