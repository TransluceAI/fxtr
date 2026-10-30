# Resolve-into-mount-scope drain cycle

A latent issue in the parent-lease structure of `FxtrScope` surfaces
when a service is mounted on the same scope it is later resolved
against (the `same_scope` test layout is the canonical reproducer).
Captured here so the structural fix can be addressed alongside the
`parent_loads_from_child_inheritance_cycle` issue — both are rooted
in the same memory-store assumption that "parents outlive children
unconditionally."

## Symptom

In a layout where the *same* scope is both the mount scope of a
service and the home scope of the proxy that resolves the service's
returned ServiceRefs, the test body completes successfully but the
outer `async with` exit hangs. Concretely: any call body that
returns a fresh `ServiceRef` (the spawner pattern) or a fresh
`FxtrID` (substage 6 step 5's chain-converter return path) installs
caller-side resources whose teardown is gated on the caller's token
ending — and that token can't end while the dispatch scope's
memory-store row pins it.

## Diagnosis: the cycle

In a "self-mount + self-resolve" layout (`mount_scope == caller_scope`):

1. The caller's `proxy.<method>()` dispatches the spawner. The
   dispatch's outer lease is `Lease[mount_scope.main]`, which equals
   `Lease[caller_main]`.
2. `_dispatch_body` opens `call_scope_A` with `include=[mount_scope]`
   and `include_leases=[outer_lease.take()]`. The memory-store row
   for `call_scope_A.token` therefore holds a clone of
   `caller_main_lease` as its parent-lease.
3. The user method mounts a new service (or stores a new entity) on
   `call_scope_A` and returns it. The encoder ships a `dep_ret_ref`
   whose `key_lease` is `Lease[K_call_A]` (a clone of
   `call_scope_A._key_lease`).
4. The proxy installs a chain bridge on the caller scope for
   `call_scope_A.main`. The bridge's `leases` dict holds the
   `Lease[K_call_A]` clone.
5. Cap-bridge teardown runs in the caller scope's
   `_on_token_end` callback — i.e. *after* `caller_token` ends.

Now consider what it takes for `caller_scope.aclose` to complete:

- `aclose` discards `caller_scope._main_lease`.
- For `caller_token` to end, `caller_main`'s lease count must drop
  to zero. The remaining clone is the parent-lease in
  `call_scope_A`'s memory-store row (step 2).
- The row's parent-lease is discarded when `call_scope_A.token`
  ends.
- `call_scope_A.token` is a `DerivedLocalLifetime` output. It ends
  when one of its inputs ends — specifically `K_call_A`.
- `K_call_A.token` ends when its lease count reaches zero. The
  remaining clone is in the caller's chain bridge (step 4).
- The bridge's clone is discarded when the bridge tears down, i.e.
  in `caller_scope`'s `_on_token_end` callback (step 5).
- That callback fires when `caller_token` ends. Goto top.

**Cycle:** caller-token-end ← parent-lease released ← `call_scope_A`
drains ← `K_call_A` ends ← caller's bridge releases ← caller-token-end.
`aclose` blocks indefinitely.

## Why it doesn't show up in the cross-process or distinct-scope cases

In substage 7 (cross-process RPC), the caller and the dispatch
scope live in **different** `MemoryStore`s. The dispatch scope's
parent-lease is on its own store's mount-scope token, not on the
caller's. There's no shared cycle.

In single-process layouts where caller is *not* an ancestor of the
mount scope (e.g. `child_scope` and `sibling_attach`), the same
applies: the dispatch scope's parent-lease points at the
mount-scope token, which is unrelated to caller-main's lifecycle.
Caller's drain proceeds independently of the dispatch scope's drain.

It is specifically the case where caller is the **memory-store
ancestor** of the dispatch scope (the canonical case being caller
== mount scope) that introduces the cycle. The simple-case test
layout `same_scope` is the canonical reproducer; in principle a
"caller is a strict grandparent of mount" layout would also
trigger it, but no such pattern is reachable through the public
APIs without explicit low-level attaches into a grandparent.

## Why the obvious tactical fixes don't apply

- **Eager bridge teardown at drain start.** Would break the cycle
  by releasing `K_call_A` before waiting for `caller_token`. But
  child scopes need to walk their parent's bridges *during* the
  parent's drain (e.g. to resolve service refs whose chain
  bridges live up the parent chain), so tearing down at drain
  start is structurally wrong.

- **Move the chain bridge to a different scope.** No alternative
  scope is the right home for it — the bridge gates the caller's
  ability to dispatch the returned ref, which is exactly a
  caller-scoped concern.

## The relationship to `parent_loads_from_child_inheritance_cycle`

Both cycles share a root cause: the memory store's "parent" link
between scopes is a *hard pin* — the dispatch scope's memory-store
row holds an actual `Lease[parent.main]` clone, and the antichain
domination logic tied to that pin removes descendant case-3
ownership when a parent attaches an inheritance arena. Properly
fixing either issue requires the same memory-store redesign:

- Replace the parent-lease *pin* with a parent-token *observation*
  (callback-driven, with the inheritance read path producing fresh
  parent leases on demand via a converter — analogous to the chain
  converter machinery used for the input side of derived scopes).
- Treat parent inheritance the same way derived inputs are treated:
  cycle-safe-by-construction, with the parent able to drain even
  if a descendant continues to depend on it.

These two issues should be revisited together when that work
lands.

## Current workaround

`FxtrScope.resolve_service` rejects `ServiceRef`s whose
`handle.token is self._token` with `NotImplementedError` and a
pointer to this doc. The most common trigger is "resolve a service
into the same scope it was mounted into" — the caller is forced
to use a distinct scope for resolution.

The `same_scope` parametrization of the
`tests/test_conversion/test_untyped_service.py::service_layout`
fixture is `xfail(strict=True, raises=NotImplementedError)` for
the duration of this restriction. When the memory-store fix lands,
remove the restriction and the `xfail` together.

## Affected tests

Every test in `tests/test_conversion/test_untyped_service.py` that
uses the `service_layout` fixture, in its `same_scope`
parametrization. The xfail is applied centrally via the fixture.

## Resolution

Resolved by `part6post3_derived_scope_fixes.md`. The fix routes the
dispatch's outer lease into ``_with_derived_lifetime``'s
``derived_from_leases`` (rather than ``independent_leases``), so
``call_scope.main_token`` is *derived from* ``parent.main_token``
rather than ancestor-bridged via a held ``Lease[parent.main]``. A
live ``Lease[call_scope.main]`` then carries ``parent.main_token``
as a via; discarding ``main_lease`` on aclose releases the via, and
the parent is free to drain. The chain-bridge drain ordering that
previously deadlocked now resolves cleanly.

The ``same_scope`` parametrisation of the
``service_layout`` fixture is no longer xfail'd, and the
``NotImplementedError`` self-mount check in ``resolve_service`` is
removed.
