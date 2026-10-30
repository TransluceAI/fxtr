# Spike findings

Things uncovered while trying to run the spawner spike. We're pausing
the spike to fix these in the framework before continuing.

---

## Finding 1: `_install_returned_resources` install order is wrong for fresh-type-id returns

**Symptom.** When a service method returns a `ServiceRef` whose
`service_type_id` is *not already tracked on the caller's home scope*,
the caller's RPC layer aborts with:

```
RPCConnectionAborted: exception in connection block:
service_type_id <FxtrID !echo-config-spike> must be tracked in
<FxtrScope 'host_rpc' at …> before attaching.
```

**Reproduction.** In a single-process two-party RPC bridge:

- Side B's main is some `UntypedServiceImpl` (type_id
  `untyped_service`).
- Side B exposes a method that returns a *freshly mounted* service
  whose type_id is some other builtin, e.g.
  `FxtrID.builtin("echo-config-spike")`.
- Side A calls that method.

Bootstrapping installs B's main type_id (`untyped_service`) on A's
session scope via `_install_peer_main_service`'s
`lazily_attach_from_arena(..., [packet.type_id], …)`. The freshly-
returned ref's type_id (`echo-config-spike`) is *not* tracked on A,
because nothing has bridged that id over yet — the wire packet's
own result-arena would track it, but…

…the install order in
`fxtr2/services/service_cap_common.py::_install_returned_resources`
is:

```
Pass 1: build fresh bridges
Pass 2: install handles      ← runs `attach_bridged_lowlevel_cap(..., type_id, bridge)`
Pass 3: install arena        ← would `lazily_attach_from_arena(..., result_ids, …)`
                                making `is_tracked(type_id)` true
```

Pass 2 fails the strict precondition
`attach_bridged_lowlevel_cap` enforces:

```python
if not self.is_tracked(service_type_id):
    raise ValueError(
        f"service_type_id {service_type_id!r} must be tracked in "
        f"{self!r} before attaching."
    )
```

…because pass 3 (which is what tracks the type_id via the result
arena) hasn't run yet.

**Why the existing tests don't trip this.** Every test in
`tests/test_rpc/test_scope_bridge.py` and the
ServiceRef-as-arg/return tests in `test_typed_service.py` returns
a ref whose type_id is *either*:

- the same as the bootstrap peer-main type_id (e.g. `_SpawnerService`
  returning a `_EchoService` — both `untyped_service`), in which case
  bootstrap already tracked it; or
- a pass-through of an arg ref (e.g. `CallbackUserProto.hand_back`),
  in which case `_bridge_input_arena` had already lazy-attached the
  type_id before the dispatch even ran (see the comment in
  `_bridge_input_service_ref`).

The spike trips it because `_EchoConfigImpl`'s type_id
(`echo-config-spike`) is freshly minted on the guest side and never
crosses the wire as an arg.

**Why the user's "switch to UntypedServiceImpl" workaround is
incidental, not principled.** `UntypedServiceImpl`'s type_id is
`FxtrID.builtin("untyped_service")` — *not* automatically tracked on
every scope. It happens to work in the spawn_echo test because the
peer's main is also `UntypedServiceImpl`-typed and thus the bootstrap
arena already pulled it onto the caller's session scope. If the
peer's main were e.g. typed, switching the returned service to
untyped would not save the caller.

### Proposed fix

Reorder `_install_returned_resources` so the arena pass runs *before*
the handles pass:

```python
# Pass 3 (now first): arena install → tracks `result_ids` on home_scope.
if arena_token is not None:
    _install_arena_for_token(
        home_scope=home_scope,
        arena_handle=fxtr_arena,
        arena_token=arena_token,
        new_ids=result_ids,
        fresh_bridges=fresh_bridges,
    )

# Pass 2 (now second): handles. is_tracked(type_id) precondition is
# now satisfied because pass-3 attached the result arena.
for token, keys in keys_by_token.items():
    _install_handles_for_token(...)
```

The two passes are commented as "ignores handles" / "ignores the
arena" so they should be order-independent on their own state — the
only coupling is the implicit one through `home_scope`'s tracked-id
set, which the new order satisfies cleanly.

### Open questions on the fix

1. **Does the encoder always include the type_id of every returned
   ServiceRef in `fxtr_ids`?** Yes — `_encode_fxtr_service_data`'s
   `service_ref` branch adds both `ServiceKey(handle, type_id)` to
   `service_keys` and `type_id` to `fxtr_ids`. So whenever there's
   a returned key, its type_id is in `result_ids` and will ride the
   result arena.

2. **What if a returned ServiceRef shares a token with the home
   scope's main / an existing entry, and there's no result arena
   provided?** `_install_arena_for_token` is only called when
   `arena_token is not None`, which is `fxtr_arena.token if (fxtr_arena
   is not None and result_ids) else None`. If `fxtr_arena` is omitted
   from the wire packet (e.g. the encoder skipped it because every
   id was already known to the receiver), then the type_id has to
   already be tracked on home_scope — same as today, just the failure
   surface gets clearer.

   Worth verifying that the encoder's "every id already known"
   short-circuit is consistent with the receiver's expectation: if
   the encoder doesn't fork an arena, the receiver had better not
   need one. There's currently no test that exercises this path
   with a fresh type_id, which is precisely how this finding hid.

3. **`_install_handles_for_token`'s "parent-walk lift" branch
   (existing is None and fresh is None).** That branch calls
   `_lift_bridge_for_service_key`, which does its own type_id
   handling (it lifts type_id alongside the key). Independent of
   this finding; just confirming the reorder doesn't break it.

4. **Is there a cleaner shape than reorder?** Alternative: drop
   the `is_tracked` precondition from `attach_bridged_lowlevel_cap`
   and do the track inline inside the install path. Probably
   *worse* — the precondition is documented to catch user errors at
   user-facing call sites, and the user explicitly noted that the
   "you have to track builtins" requirement is intentional. Reorder
   keeps the user-facing contract intact and just fixes the
   framework-internal ordering.

### Suggested test for the fix

```python
class _CustomTypedService(UntypedServiceImpl):
    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(
            FxtrID.builtin("test-custom-type-id")
        )

class _CustomReturningSpawner(UntypedServiceImpl):
    @service_method
    async def make_custom(self) -> ServiceRef[Any]:
        return FxtrScope.get_active().mount(_CustomTypedService())

# In a two-party rpc bridge: B mounts _CustomReturningSpawner; A calls
# make_custom() and asserts the returned ref resolves and dispatches.
# Without the reorder fix this raises the abort above.
```

---

## Finding 2 (minor): contextvar inheritance in `start_soon` propagates the host's active scope into the guest task

**Symptom.** In the spike, `_spawn_runner` does
`guest_tg.start_soon(guest_runtime, ...)`. `start_soon` snapshots the
current contextvars, so `guest_runtime` sees the host's spawn
call_scope as its initial active scope.

`guest_runtime` then opens an `isolated=True, mountable=True`
`FxtrScope` (named `guest_host`). Because `isolated=True`, that scope
does not auto-activate. So when it calls
`open_rpc_scope(transport, main_ref)`, the eager precondition lift
walks `include + active`, and `active` is still the *host's* spawn
call_scope. main_ref is registered on `guest_host`, not on the host's
scope — so the precondition errors with "not registered on any
candidate parent scope".

**Workaround applied in the spike.** Wrap the open_rpc_scope call in
`with guest_host.as_active_scope(): …`. Clean and local.

**Whether this is a real bug.** Probably not — the contextvar
inheritance is structural-concurrency-correct, and any cross-process
version of this would not have a shared contextvar at all. Worth
calling out as a sharp edge specific to single-process spike code,
not a framework bug.

This is a known issue. None of the obvious mitigations are
clearly the right thing from a user perspective:

- A `set_active=True` flag on `FxtrScope(isolated=True, mountable=True)`
  would force activation, but it conflates two orthogonal axes
  (isolation vs activation) and the user may legitimately want
  isolated scopes that don't take over the contextvar.
- A "fresh-context" task-spawn helper (e.g. wrapping `start_soon` to
  reset scope-related contextvars) shifts the burden to whoever spawns
  the task; users would have to know to reach for it.

For now, the workaround pattern (`with scope.as_active_scope():`) is
documented in the spike and is the recommended approach for
single-process tests / spikes that span multiple notional "processes".

---

## Follow-ups

(none open — the audit below resolved the open follow-up.)

---

## Audit: stale-imports assumption across `_on_*` handlers

Triggered by Finding 3. Both `_imports` and `_exports` are not
append-only — fully-drained entries get removed by the
`_maybe_remove_*_import` helpers (in `lowering.py` and
`lowering_remote_proxies.py`) and the `_tear_down_*_export` helpers
(in `lowering.py`, called from `_on_discard` / `_on_break`). A handler
that aborts on `_imports.get(id) is None` (or the analogous
`_exports.get(id) is None`) conflates "peer references state we never
had" (real protocol violation) with "peer references state we already
drained" (teardown race the protocol-spec docstrings say to tolerate).

| Handler | Lookup | On `entry is None` | Status |
|---|---|---|---|
| `_on_hello` | n/a | n/a | ✓ |
| `_on_push` | (creates entries) | n/a | ✓ |
| `_on_pull` | `_exports[future_import_id]` | abort | **bug** (see note) |
| `_on_pull` | `_exports[grant_import_id]` | abort | **bug** |
| `_on_resolve` | `_imports[future_export_id]` | abort | **bug** |
| `_on_reject` | `_imports[future_export_id]` | abort | **bug** |
| `_on_watch` | `_exports[token_import_id]` | abort | ✓ (see note) |
| `_on_closed` | `_imports[token_export_id]` | silent no-op | ✓ |
| `_on_break` | `_imports[grant_export_id]` | silent no-op | ✓ (post-fix) |
| `_on_discard` | `_exports[import_id]` | silent no-op | ✓ |
| `_on_unpull` | `_exports[future_import_id]` | silent no-op | ✓ |
| `_on_abort` | n/a | n/a | ✓ |

**`_on_watch` note.** A WatchMessage from a peer who has torn down
their token import would only land on us after their DiscardMessage,
which would already have torn down our token export. But since the
single-channel reader is FIFO, Watch can never arrive *after* the
peer's own follow-up Discard from the same connection. So entry-is-
None for Watch genuinely means "peer references state we never had",
and aborting is correct. Could still be worth a defensive log if we
ever hit a path where this fires, but no behavior change is needed.

**`_on_pull` future-import note.** `FutureExportEntry` is *not* torn
down on resolve/reject — `_zero_future_export_obligation` only sets
`resolved_sent = True`. The entry stays in `_exports` until peer's
`DiscardMessage` triggers `_tear_down_future_export` via
`_on_discard`. Combined with single-channel FIFO ordering
(peer can only emit `DiscardMessage` *after* they stop emitting
`PullMessage` from the same connection), the future-import-id lookup
in `_on_pull` *in legitimate execution* always finds the entry. The
existing `if future_entry.resolved_sent: return` branch is the
complete tolerance story for late pulls.

But for *consistency* with the silent-no-op policy used by every
other table-lookup handler (`_on_closed` / `_on_break` / `_on_discard`
/ `_on_unpull`), the table marks this lookup as a bug too: aborting
on entry-is-None catches peer misbehavior at the cost of being
inconsistent with the tolerance policy elsewhere, and "peer
misbehaved" is detected from many other angles already. Treating
all four lookups identically (silent-no-op on `None`, abort on
wrong-kind) is the simpler invariant.

### Bug 1: `_on_pull` grant-import-id aborts on a local-revoke race

`GrantExportEntry` has *two* teardown sources:

1. `_on_discard` (peer's discard zeros `outstanding_count`).
2. The on-break callback subscribed by
   `_subscribe_backing_lease_break` — when our local backing lease
   breaks, the callback emits `BreakMessage` to the peer and tears
   down our export entry locally.

Source (2) creates a teardown race relative to inbound `PullMessage`s.
Sequence:

- Peer holds an active grant lease backed by our export.
- Peer sends `PullMessage(future_id, grant_id)` to us.
- *Before* our reader processes the pull, our backing lease breaks
  locally. The on-break callback fires synchronously, sending
  `BreakMessage(grant_id)` and removing `_exports[grant_id]`.
- Our reader processes the pull. `_exports.get(grant_id)` returns
  `None`. Current code aborts with `PullMessage: no grant export
  entry at id N`.

The peer will see our `BreakMessage` and clean up their pending pulls
on their side; our connection should not abort just because the
ordering interleaved this way.

**Fix.** Same shape as `_on_break`: distinguish `entry is None`
(silent no-op, teardown race) from `entry is wrong-kind` (still
abort).

### Bug 2: `_on_resolve` aborts on resolve targeting a drained future

`FutureImportEntry` is removed by `_maybe_remove_future_import` once
`live_proxy_count == 0`, `introduction_refcount == 0`,
`resolve_obligation_refcount == 0`, and `local_consumer_count == 0`.
Hitting that state simultaneously while a peer's `ResolveMessage` is
in flight is the race: peer's resolver completes and emits
`ResolveMessage` after we've sent our final `DiscardMessage`, but
before they processed our discard. Our handler runs
`_imports.get(...) → None`, current code aborts with
`ResolveMessage: no future import entry at id N`.

By symmetry with `_on_pull`'s already-resolved-late-pull tolerance
(and the same teardown-race policy used by `_on_closed` / `_on_break` /
`_on_discard` / `_on_unpull`), this should be a silent no-op. The
protocol nowhere says "I, the importer, still have this future" is a
precondition the exporter has to verify before resolving — and the
exporter typically can't (the resolve was already on the wire when
our discard was sent).

**Fix.** Same shape as Bug 1: `entry is None` → no-op; `entry`
wrong-kind → abort.

### Bug 3: `_on_reject` has the same bug as `_on_resolve`

`_on_reject` is structurally identical to `_on_resolve` for the
lookup pattern. Same fix.

### Suggested fix shape (all three)

Mirror the fix already applied in `_on_break`:

```python
entry = conn._imports.get(msg.future_export_id)  # or _exports
if entry is None:
    # Teardown race: we already drained and removed this entry.
    # Per the same policy used in _on_closed / _on_break /
    # _on_discard / _on_unpull, silently ignore.
    return
if not isinstance(entry, FutureImportEntry):  # or GrantExportEntry, etc.
    phase1_protocol_violation(
        conn,
        f"...: id N is not a {expected_kind} entry"
        f" (got {type(entry).__name__})",
    )
```

For `_on_pull` specifically, both lookups get this treatment: the
grant-import-id one fixes the local-revoke race, the future-import-id
one keeps the policy consistent across all table-lookup handlers.

### Suggested regression-test shape

For each bug: drive a real two-party connection through the race, or
use a raw-peer pattern like `TestBreakUnknownIdToleratedAsRace` that
injects a synthetic message for an id we never imported and asserts
no abort. The "never imported" form covers the same handler branch
as "drained" because the lookup can't distinguish them at the point
of the entry-is-None check, and the policy is to tolerate both.

- `_on_pull` (grant-import-id race): inject a `PullMessage` whose
  `grant_import_id` we never exported. Assert no abort.
- `_on_resolve`: inject a `ResolveMessage` whose `future_export_id`
  we never imported. Assert no abort.
- `_on_reject`: same as resolve.
