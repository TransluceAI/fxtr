# Stage 3 follow-up: filling test coverage gaps

After landing 3c, an audit of test coverage surfaced several invoke-side
behaviors that have no end-to-end test. This stage writes the missing
tests. The goal is purely additive — no behavior changes are planned.
If a test reveals a real bug we'll fix it, but the priors say the code
paths are correct.

## Prelude: shared session-cleanup helper

Before adding more tests we untangled an RPC-drain hang that masked
the test that triggered this stage in the first place. Each test
created a session but never closed it, so any session that pinned an
RPC-registered service blocked the surrounding ``connect_rpc``'s
graceful drain on exit (per the 3b.3 RPC-lifetime semantics).

Resolution: a shared ``create_and_subscribe`` async-context-manager in
``tests/_session_helpers.py``. On enter it does the same create +
subscribe + snapshot dance the per-file inline helpers used to. On
exit it opens a *fresh* subscription on the same client to read the
current ``last_state.seq``, then sends a close mutation against that
fresh subscription. Cleanup is best-effort — if the test already
closed the session itself, the fresh subscribe gets an
``ErrorResponse`` and the helper returns silently.

All seven previous duplicates of the inline helper across the session
test files were replaced by importing this CM. Tests now use:

```python
async with (
    expose_service(...) as svc_short_id,
    create_and_subscribe(client) as (sid, sub_id),
):
    ...
```

— ``create_and_subscribe`` is always the *last* entry in the
parenthesized group so it cleans up first, releasing session-pinned
services before the surrounding ``connect_rpc`` / ``expose_service``
drain.

While migrating, one pre-existing bug surfaced in
``test_chained_invoke_via_service_returned_from_invoke``: the second
invoke advanced ``ack_seq`` past the first result event, and the
retention prune released the new service before the second invoke
could dispatch on it. Fixed by keeping ``ack_seq=0`` for the chained
invoke (so the first result event stays inside the retention horizon
and its event-ref keeps the service alive).

## Scope

End-to-end tests via `/client` that exercise:

- The "service-from-invoke-result" code path (currently has no direct
  test).
- The cross-cutting refcount/retention behavior for services (the
  3c.4 file already covers this for entities; mirroring it for
  services).
- Failure paths in the invoke pipeline (bridge errors,
  close-during-in-flight) that are structurally simple but
  unexercised.
- Op-shape combinations (multiple ops in one mutation; mixed
  load/invoke; results with both entity and service refs).
- Cross-session service sharing.
- Load results with explicit user-level child entity references
  (current child tests rely on the implicit `$fxtr.type` /
  `$fxtr.ctx` markers).

## Out of scope

- Anything that requires changing production code, except minor
  fixture additions.
- Tests for the panel viewer / debug UI (covered separately).
- Tests for callbacks (`["callback", N]`) — those are stage 5.

## Test fixtures to add (or already added)

**Already added** in `tests/_fixtures.py` during the first attempt:

- `NoteWithChild(DataclassEntity)` — Note that holds an explicit
  `EntityRef[Note]` field. Used by test M.
- `EchoFactoryProto` / `EchoFactoryImpl` with three methods:
  - `make_echo() -> ServiceRef[EchoProto]` — returns a freshly-
    minted service. Used by A, B, D, H.
  - `echo_via(echo: ServiceRef[EchoProto], msg: str) -> str` —
    accepts a ServiceRef as an argument and dispatches through it.
    Used by G.
  - `make_pair() -> dict[str, Any]` — returns a dict with both an
    entity and a service. Used by L.
- `SlowEchoProto` / `SlowEchoImpl` — has an `echo(msg, delay)`
  method that sleeps before returning. Used by F.

The original `make_echo` form
(`mount(EchoImpl()).replace_service_type(EchoProto)` on
`get_active_scope()`) turned out to be correct as written — the
"hang on first use" we attributed to the framework was actually
the session-cleanup hang documented in the prelude. Once the
shared `create_and_subscribe` CM was in place, the fixture worked
unchanged.

## Tests

Listed roughly in dependency order. Each test exists in either
`tests/test_sessions_3c3b.py` (basic invoke functionality) or
`tests/test_sessions_3c4.py` (cross-cutting refcount/retention).

### [x] A — Invoke that returns a `ServiceRef` workstation-tracks it

**Goal:** verify the previously-untested code path where the invoke
worker's result contains a `ServiceRef`. The new ref should:

- Get a workstation `TrackedServiceRecord`.
- Have a `service_record_worker` spawned with the invoke worker's
  `call_scope` as the source.
- Have `bridge_ready` set (worker successfully bridged) and no
  `bridge_error`.
- Be added to the calling session's `service_refcounts`.
- Be recorded in `event_refs[seq]` for the `InvokeResultEvent`.

**Where:** `test_sessions_3c3b.py::test_invoke_returning_service_workstation_tracks_it`.

### [x] B — Service refcount drops when retention drops the invoke result

**Goal:** mirror `test_child_released_when_retention_drops_load_result`
but for services. After the invoke result event passes the retention
horizon, the session's refcount on the new service drops; with no
other holders, the service record is released, the worker exits,
and `service_scope` drains.

**Where:** `test_sessions_3c4.py::test_service_released_when_retention_drops_invoke_result`.

### ~~C — Service-from-invoke survives source RPC drop~~ (dropped)

This test was originally specified by analogy to
`test_load_sourced_child_kept_alive_after_rpc_drops`, but the analogy
breaks down. Entities are **copied** into `entities_scope`; once the
copy is done, the data is genuinely independent of the source RPC.
Services are different: an invoke-result-minted service is just a
workstation-side bridge pointing at a remote `IRHandle` imported from
the registering RPC connection. The impl object lives in the peer's
process. The workstation's only handle on it is a lease that goes
*back through* the RPC connection.

So dropping the source RPC while a session still pins the new
service can't "leave the service alive" — the lease the session
holds is exactly what makes the RPC's graceful drain block until the
session releases (the 3b.3 rule). Verified empirically: the test
hangs at `connect_rpc.__aexit__` until the session-close path runs.

A short comment in `test_sessions_3c4.py` records this so we don't
try to reintroduce it.

### [x] D — Explicit `track_service` preserves the new service through retention

**Goal:** mirror `test_explicit_pin_keeps_child_alive_through_retention_drop`
but for services. The session pins the new service via
`TrackServiceOp` in the same mutation that acks past the invoke
result event. Refcount stays at 1 (event-ref dropped, explicit
pin added), record alive.

**Where:** `test_sessions_3c4.py::test_explicit_pin_keeps_service_alive_through_retention_drop`.

### ~~E — Bridge error during invoke phase 2 → `InvokeErrorEvent` + rollback~~ (deferred)

**Goal:** test the rolling_back path inside
`deliver_invoke_result_to_slot` when a freshly-minted service
record's `service_record_worker` reports `bridge_error`. The helper
should decref every in-flight ref (including any entities
attached in the same delivery) and emit an `InvokeErrorEvent`.

**Final decision: skip.** Engineering an organic
`bridge_error` is hard — `bridge_service_from_scope` succeeds for any
valid ref, and the obvious "broken ref" fixtures we tried propagate
the failure through dispatch (caught before phase 2 starts) rather
than through the bridge. The remaining viable approach is a
test-only seam to inject `bridge_error` directly into a record, but
that's intrusive enough that we'd rather rely on code review for
the rollback path. Revisit if a future stage forces us to exercise
this path through some other natural trigger.

### [x] F — Close-during-invoke leaves no leaks

**Goal:** mirror `test_close_during_in_flight_load_leaves_no_leaks`
but for an invoke that's slow (uses `SlowEchoImpl`). Send the
invoke, immediately send a close mutation. Race-tolerant: regardless
of whether the close runs before, during dispatch, during phase 2,
or after the result-event commit, the final state has no leaked
records / refcounts.

**Where:** `test_sessions_3c4.py::test_close_during_in_flight_invoke_leaves_no_leaks`.

**Note:** this exercises the `aborted_orphan` code path
(session-close drains in-flight refs before the helper's phase 3
runs). The "close during phase 2 bridge wait" sub-case is hard to
engineer specifically; the "close during dispatch" path covers the
same orphan-handling code.

### [x] G — Invoke with a `ServiceRef` arg dispatches through it

**Goal:** verify the `arg_service_scopes` plumbing in
`invoke_worker`. Pass a `ServiceRef` as an argument to a method
that uses it (`echo_via(echo, msg)`); the method resolves the ref
in its dispatch scope and forwards the call. Result should equal
`f"echo:{msg}"`.

**Where:** `test_sessions_3c3b.py::test_invoke_with_service_arg_dispatches_via_arg`.

### [x] H — Chained invoke (service from invoke is invokable)

**Goal:** end-to-end chain: invoke `make_echo` to get a new
short_id → in a follow-up mutation, invoke `echo` on that new
short_id. Exercises A + G together. The new service must work as
the *target* of a later invoke, not just as a result.

**Where:** `test_sessions_3c3b.py::test_chained_invoke_via_service_returned_from_invoke`.

**Implementation note:** the second invoke must keep `ack_seq=0`
(or otherwise *not* advance past the first result event's seq),
because retention pruning would otherwise drop that event's
`event_refs` entry and release the new service before the second
invoke can target it. Surfaced as a real bug in the original
draft of this test.

### [x] I — Cross-session service usage

**Goal:** session A pins a service via `TrackServiceOp`; session B
invokes it. Verifies the workstation acts as a shared service
registry (the plan called this out as a primary motivator). Also
exercises the "service held by a session that did not register
it" lifetime path.

**Where:** `test_sessions_3c4.py::test_service_tracked_by_one_session_invokable_by_another`.

### [x] J+K — Multiple ops in one mutation, mixed load+invoke

**Goal:** combined test of slot monotonicity across kinds. A single
mutation contains two `InvokeOp`s and one `LoadEntityOp` with
strictly-increasing slots; all three deliver result events. A
parallel rejection test verifies that two ops at the same slot
rejects regardless of kind.

**Where:** `test_sessions_3c3b.py::test_multiple_invokes_and_load_in_one_mutation_share_slot_rule`.

### [x] L — Result with both entity and service refs

**Goal:** the dispatched method returns
`{"note": <EntityRef>, "echo": <ServiceRef>}`. Both refs become
workstation-tracked, the session takes refcounts on each, and
`event_refs[seq]` records both. Exercises the merged
`_walk_refs` path and both arms of the phase-2 task group.

**Where:** `test_sessions_3c3b.py::test_invoke_result_with_entity_and_service_pair`.

### [x] M — Load result with an explicit child entity reference

**Goal:** the existing 3c.4 child-entity tests rely on the
implicit `$fxtr.type` / `$fxtr.ctx` marker references. Add a test
that uses a real user-level child reference: `NoteWithChild(title,
child=parent_note_ref)`. Loading the parent should pull the
child's `EntityRef` into the result payload, mint a record for
the child, and refcount it on the session.

**Where:** `test_sessions_3c4.py::test_load_result_walks_explicit_user_level_child_ref`.

## Implementation order

- [x] 0. Add the shared `create_and_subscribe` cleanup CM and
      migrate all seven session-test files to use it. (See
      "Prelude" above.)
- [x] 1. Resolve the `make_echo` fixture issue. The hang was the
      RPC-drain blocking on session-pinned services, not a
      typed-service framework issue — once the cleanup CM was in
      place, `EchoFactoryImpl.make_echo` worked exactly as
      written.
- [x] 2. Test M (independent of fixtures: only needs `NoteWithChild`).
- [x] 3. Test G (consumes a ServiceRef arg; no new-service
      mounting concern).
- [x] 4. Test J+K (independent of fixtures).
- [x] 5. Test I (RPC-registered service shared across sessions).
- [x] 6. Tests A, H, L (depend on the new-service fixture).
- [x] 7. Tests B, D (depend on A). Test C dropped — see "Test C"
      section above for why.
- [x] 8. Test F (close-during-invoke).
- [x] 9. Test E — final decision: skip. See test E section.
- [x] 10. Update CODE_OVERVIEW.md with the test file additions.

## Status

**Done.** 215 tests passing across the suite (up from 210
pre-stage). New surface in this stage:

- `tests/_session_helpers.py` — the shared `create_and_subscribe`
  CM. All seven session test files (3a/3b1/3b2/3b3/3c2b/3c3b/3c4/3d)
  migrated to import it; all duplicates of the old inline helper
  removed.
- `test_sessions_3c3b.py` — covers A, G, H, J+K, L on top of the
  pre-existing primitive / entity-result / op-validation /
  dispatch-error / arg-passthrough cases (12 tests total).
- `test_sessions_3c4.py` — covers B, D, F, I, M on top of the
  pre-existing retention / explicit-pin / RPC-drop /
  close-during-load cases (9 tests total). Test C is documented
  as a comment in the file (and in the test C section above) so
  we don't re-add it.

Tests intentionally not implemented:

- **C** — service-from-invoke survives source RPC drop. The
  analogy to the entity case breaks down: services are bridged
  across the RPC, not copied. Documented in the test C section
  and inline in `test_sessions_3c4.py`.
- **E** — bridge-error rollback. Deferred; rollback path is
  structurally simple, organic triggers all surface as dispatch
  errors before phase 2. Documented in the test E section.
