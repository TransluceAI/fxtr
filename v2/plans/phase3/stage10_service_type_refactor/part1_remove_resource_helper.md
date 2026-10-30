# Stage 10 part 1 — Remove `ServiceResourceHelper`

## Motivation

`FxtrScope.bridge_service_from_scope` currently returns a sync
`ServiceResourceHelper[T]` that wraps `(dest, ref)` and calls
`dest.forget_service(ref)` on `close()` / context-manager exit. The
helper exists to give callers a way to release the bridge before
`dest`'s drain — but in practice this is a leaky abstraction:

- The bridge holds a clone of `Lease[ref.handle.token]` from
  `source_scope`. *Other* code in `dest` may also have minted leases
  on that token by the time the user calls `helper.close()` (e.g.
  resolve / call paths that returned ServiceRefs whose tokens chain
  back through the bridge), so closing the helper does not necessarily
  release the underlying anchor.
- The cleanup path is hard to follow (every helper-close needs to
  reason about whether anything else still keeps the bridge alive),
  and the surface area is hard to test reliably.
- The `with` form looks RAII-clean but the actual lifetime is
  scope-bound regardless.

The simpler model: when you call `bridge_service_from_scope`, the
destination scope keeps the bridged service's source scope alive
*until the destination scope drains*. There's no early-cleanup API.
If you need earlier release, drain a child scope. This is part1 of
the broader refactor — it doesn't touch type_id semantics at all.

## Scope

This part is purely about removing the `ServiceResourceHelper` API
and updating the lifetime documentation/tests. The
`copy_from_scope(service_type_id)` call inside
`bridge_service_from_scope` stays put (parts 2/3 deal with the
type-id tracking story). The mount/attach/resolve/encode-decode
machinery is unchanged.

Out of scope:

- `$fxtr.type` plumbing (part 2).
- Dropping the "type_id is tracked locally" invariant (part 3).
- Removing the `forget_service` public method. It stays — `mount` /
  `attach_bridged_lowlevel_cap` callers may still want explicit early
  cleanup for their own-mount path, and the method is well-defined.
  We just don't return a helper that wraps it.

---

## §1.1 — API change

### `FxtrScope.bridge_service_from_scope`

Signature change: returns `None` instead of `ServiceResourceHelper[T]`.

```python
async def bridge_service_from_scope[T](
    self,
    ref: ServiceRef[T],
    source_scope: FxtrScope,
) -> None: ...
```

Body change: drop the final `return ServiceResourceHelper(...)`. All
the verify/copy/mint/attach steps stay verbatim.

Docstring rewrite: the new lifetime contract is *scope-bound only*.

- "Holds the source scope alive: as long as `self` is alive, the
  cloned lease pins `ref.handle.token` in the source scope's
  cap-bridge graph. Source-scope cleanup blocks on `self`'s drain."
- "Recommended usage: only call this when you can guarantee `self`
  drains before `source_scope` needs to close. For shorter-lived
  bridges, build a child scope of `self` whose drain you control,
  bridge into the child, and let the child's drain release the
  bridge."
- Strict-on-duplicate clause stays (registering the same `ServiceKey`
  twice is still an error — keeps `forget_service` deterministic).
- Drop the "use the returned helper as a context manager" phrasing
  in every place it appears.

### `ServiceRef.bridge_from_scope`

Signature change: returns `None`.

```python
async def bridge_from_scope(
    self,
    source_scope: FxtrScope,
    into_scope: FxtrScope | None = None,
) -> None: ...
```

Convenience wrapper, same body (delegate to
`_resolve_scope(into_scope).bridge_service_from_scope(self, source_scope)`).

### `ServiceResourceHelper`

Delete the class entirely from `fxtr2/data/entity_ref_scope.py`.

Imports of `ServiceResourceHelper` from anywhere outside this file
(only the test module) are removed.

## §1.2 — Tests

### `tests/test_services/test_bridge_service_from_scope.py`

Rewrite the file to drop everything that depended on the helper:

- Delete `TestSourceLifetime.test_close_releases_source_lease` (the
  `helper.close()` followed by "bridge entry is dropped" assertion
  is no longer applicable — the bridge is now scope-bound).
- Delete `TestHelperIdempotency` entirely — there's nothing to be
  idempotent about.
- For the surviving tests (`TestCrossTreeBridge`,
  `TestTypeIdPropagation`, `TestStrictOnDuplicate`,
  `TestPipelinedSource`, `TestActiveScopeDefault`):
  - Replace every `helper = await dest.bridge_service_from_scope(...)`
    with `await dest.bridge_service_from_scope(...)` (drop the
    binding).
  - Replace every `with await dest.bridge_service_from_scope(...)
    [as helper]: ...` with the bare async-call form. The body of the
    former `with` block stays put — it now lives in the surrounding
    `async with` for `scope_b` (the bridge releases when `scope_b`
    drains).
  - For `TestStrictOnDuplicate`'s "calling bridge after a prior
    `with` block exited (which forgot the ref) succeeds" sub-test:
    delete it. There's no longer a "prior with block exited"
    state — once bridged, the `ServiceKey` is registered for the
    rest of `dest`'s lifetime.
  - For `TestActiveScopeDefault`: same drop of `with`. The
    `await ref.bridge_from_scope(scope_a)` call is the only line
    that changes; the assertions on `scope_b._known_handles` and
    proxy resolve still apply.

### `tests/test_services/test_service_mount_context.py`

Lines 237 and 294 use `with await scope_b.bridge_service_from_scope(...):`.
Drop the `with`; the body of each former `with` block stays inside
the enclosing scope CM.

### `tests/test_services/test_typed_service.py`

Line 456:
```
helper = await layout.caller_scope.bridge_service_from_scope(ref, layout.service_scope)
```
Replace with the bare-call form (drop the `helper = ` binding).
Search for any later use of `helper` in that test — likely none, just
the `helper.close()` that goes away.

## §1.3 — Documentation

### `fxtr2/data/LOCAL_CODE_OVERVIEW.md`

- Delete the `ServiceResourceHelper` bullet under "Live-capability
  bridges" (line 141-144 today).
- Rewrite the "Cross-tree bridging (`bridge_service_from_scope`)"
  section (lines 332-347 today): remove every mention of the
  helper / `close()` / `with`. Add a sentence explicit that the
  bridge is scope-bound until `dest` drains, and that there is no
  early-cleanup API.

### `CODE_OVERVIEW.md`

The same `bridge_service_from_scope` section appears in the root
overview's services subsection. Same rewrite (drop helper, scope-
bound lifetime, no early-cleanup primitive).

### `fxtr2/services/LOCAL_CODE_OVERVIEW.md`

The "Cross-tree bridging" subsection is similar — rewrite for
scope-bound lifetime; drop the example `with helper:` snippet.

### Plan-doc cross-references

`plans/phase3/stage8c_user_caps/part6post4_service_ergonomics.md`
§3 introduced the helper. Add a one-line "Superseded by stage10
part1: helper removed; bridge is scope-bound." note at the top of
that §3 section so future readers don't get misled.

## §1.4 — Checklist

- [x] Rewrite `FxtrScope.bridge_service_from_scope` to return `None`;
      update its docstring for scope-bound lifetime.
- [x] Rewrite `ServiceRef.bridge_from_scope` to return `None`;
      update its docstring.
- [x] Delete the `ServiceResourceHelper` class from
      `fxtr2/data/entity_ref_scope.py`.
- [x] Update `tests/test_services/test_bridge_service_from_scope.py`:
      delete helper-specific tests, rewrite remaining tests for
      bare-async-call form.
- [x] Update `tests/test_services/test_service_mount_context.py`
      lines 237 and 294 to drop the `with` wrapper.
- [x] Update `tests/test_services/test_typed_service.py` line 456
      to drop the `helper = ` binding.
- [x] Update `fxtr2/data/LOCAL_CODE_OVERVIEW.md` (drop helper bullet,
      rewrite cross-tree section).
- [x] Update `CODE_OVERVIEW.md` (services subsection).
- [x] Update `fxtr2/services/LOCAL_CODE_OVERVIEW.md` (cross-tree
      section).
- [x] Add a "superseded" note at the top of part6post4 §3.
- [x] Run the full test suite (`uv run pytest -n auto` from `v2/`).
      Confirm green. (2310 passed.)

## Notes

- After this part lands, parts 2 and 3 can proceed in either order
  with respect to part 1 — the helper removal is independent of
  the type-id work. The recommended sequence stays part1 → part2 →
  part3 because parts 2 and 3 modify different proxy/dispatch
  surfaces and parts together produce the cleanest end state.
