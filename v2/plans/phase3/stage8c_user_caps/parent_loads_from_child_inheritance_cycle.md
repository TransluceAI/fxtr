# Parent-loads-from-child inheritance cycle

A latent issue in `MemoryStore`'s antichain semantics surfaced while
implementing substage 6 step 5 (chain-converter return path). Captured
here so the fix can be addressed orthogonally.

## Symptom

The test `tests/test_conversion/test_untyped_service.py::TestFxtrIdRoundTrip::test_returning_new_fxtrid_loads_through_chain_bridge`
hangs in the `same_scope` layout (mount scope == caller scope) at
`await typed.load(caller_scope)`. Confirmed via `anyio.fail_after(10)`.

Under the hang, `CapIRArena.load_flat` is invoked 10,000+ times in a
busy loop on the same node ID. The chain converter dispatches succeed
each time; the loop is in the *load path*, not the lease/converter
machinery.

## Diagnosis

Trace (debug prints, abridged):

```
[ADD_OWNED]   node=dmq2r77… token=<call_scope.main> ADDED         ← user store inside the call
[STORE]       stored entity under token=<call_scope.main>
[REMOVE_DOM]  node=dmq2r77… dom_token=<call_scope.main>           ← caller-side bridge install
              (dominated by token=<caller_scope.main>)
[ENSURE_OWN]  node=dmq2r77… token=<caller_scope.main> owned_tokens=set()
[ENSURE_OWN]  node=dmq2r77… token=<call_scope.main>   owned_tokens=set()  ×N
```

**Setup, in same-process / same-store:**

- `mount_scope == caller_scope`.
- The dispatch creates `call_scope` with `include=[mount_scope]`, so
  `call_scope.main_token` is a memory-store child of
  `caller_scope.main_token`.
- The user method body stores a new entity under the active scope
  (`call_scope`); the node lands in the global `_entity_states` with
  `owned_tokens = {call_scope.main_token}`.
- The proxy receives the return packet, installs the cap_ir bridge
  on `caller_scope.main_arena` via
  `lazily_attach_from_arena(my_lease=Lease[caller_scope.main], …,
  cap_ir, …)`. This calls
  `_adopt_inheritance_arena_claim(node, …, x_reg=caller_scope.main, …)`.

**The `_adopt_inheritance_arena_claim` path** (line 994 of
`fxtr2/data/memory_arena.py`) walks the existing antichain and runs
`_remove_dominated_from_antichain(node, x_reg)`. Because
`caller_scope.main_token` is an *ancestor* of `call_scope.main_token`
in the memory-store DAG, the antichain rule says the ancestor
dominates the descendant. The descendant's claim (`call_scope.main`)
is removed from `owned_tokens` to keep the antichain a true
antichain.

**Post-install state on the node:**

- `owned_tokens = {}` — `call_scope.main` was kicked out.
- `inheritance_claims = {caller_scope.main: cap_ir}` — the new bridge.

**The cycle.** The test then performs `caller_scope.load(...)`:

1. Caller-side `_ensure_own(node, caller_scope.main_token)` —
   `_has_case3_access` False (no owned tokens), walk inheritance,
   pick `cap_ir`, dispatch.
2. Server-side adapter receives the dispatch, calls
   `sub_arena.load_flat(Lease[call_scope.main], node_id)`.
3. Server-side `_ensure_own(node, call_scope.main_token)` —
   `_has_case3_access` False (because step 0 removed it!), walk
   inheritance. `_pick_inheritance_claim` looks for any token in the
   antichain that is an ancestor of `call_scope.main_token` — finds
   `caller_scope.main_token` and **the same cap_ir** is its arena.
4. Goto 1.

Each iteration successfully dispatches the chain handle and the arena
handle, returns flat data, runs `_bare_store_flat_impl` → `_add_owned_token`
on `call_scope.main_token` — which then immediately gets dominated
out again by `caller_scope.main_token` already sitting in the
inheritance side of the antichain. The owned slot never sticks.

## Why it doesn't show up in the cross-process case

In substage 7 (cross-process RPC), the caller-side and server-side
each have their own independent `MemoryStore`. Installing the cap_ir
bridge on the caller's store can't observe (or clobber) the data
sitting in the server's store. There's no shared antichain to confuse.

It's specifically the same-process / same-store case where this
fires. In `same_scope`, the dispatch scope and caller scope are
literally one and the same global `MemoryStore` with caller as
ancestor of call_scope.

## Where to fix

Three plausible directions, all of which are orthogonal to substage
6's chain-converter wiring:

1. **`_adopt_inheritance_arena_claim` selectivity.** The current rule
   removes any descendant case-3 claim when an ancestor case-2 claim
   is added. That's correct in the abstract antichain sense, but it
   breaks the case where the ancestor's bridge is a Cap-IR proxy
   *back to* the same store's data. A more nuanced rule (e.g. don't
   remove case-3 ownership in the same-store-shared case, or detect
   the cycle and refuse the install) would avoid the data loss.
2. **Skip the bridge install when the data is already locally
   reachable.** On install, walk the consumer scope's existing
   antichain for each ID; if the data is already case-3 in a
   descendant, skip the inheritance claim. Cheap to detect; only
   helpful in-process.
3. **Memory-store isolation between caller and call_scope.** Treat
   the dispatch scope as memory-store-isolated even in the same
   process; data flow caller→call_scope via the same
   bridging machinery used cross-process. Bigger structural change
   but unifies in-process and cross-process behaviour.

## Substage 6 stance

This is a `MemoryStore` design issue, not a chain-converter design
issue: the chain converter, the wire shape, and the caller-side
install all behaved correctly under the trace; the `_adopt_inheritance_arena_claim`
removal of `call_scope.main` is what broke things.

For now, the affected test parametrisation
(`test_returning_new_fxtrid_loads_through_chain_bridge[same_scope]`)
is `xfail`'d with a reason pointing at this doc. The `child_scope`
and `sibling_attach` parametrisations are expected to pass, since
in those layouts caller_scope ≠ call_scope's memory-store ancestor
(or has a different relationship that doesn't trigger the
domination). If other tests in step 5 hit the same cycle, they'll
get the same `xfail` mark.

## Resolution

The cycle described above was resolved by the part6post2 memory-store
simplification (see
`plans/phase3/stage8c_user_caps/part6post2_memory_store_simplify.md`).
After that refactor, the memory store no longer performs antichain
domination — every token has independent ownership of every node,
and `_adopt_inheritance_arena_claim` no longer evicts descendant
case-3 ownership. The specific spinning load loop documented above
can no longer occur.

The `same_scope` parametrisation of
`test_returning_new_fxtrid_loads_through_chain_bridge` remains
xfailed, but for an independent reason: the chain-bridge drain
cycle documented in `resolve_into_mount_scope_drain_cycle.md`. That
cycle is structurally separate (it's about ancestor cap-bridges,
not antichain domination) and requires its own fix.
