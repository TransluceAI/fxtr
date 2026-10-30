# Stage 6, Chunk 4 — Integration tests against the live workstation

Final chunk of stage 6. Boots the demo workstation (Python) via the
existing `_integration_harness.ts` and exercises the callback-inbox
machinery end-to-end against a real `/client` socket. Covers what
the mock-based unit tests can't: actual `CreateCallback` /
`CloseCallback` server-side bookkeeping, real `notify` flow,
preserve / unpreserve via `session_debug`'s `preserved_events`
sidecar, and snapshot redelivery on attach.

Read [`../stage6_callback_inboxes.md`](../stage6_callback_inboxes.md)
for design decisions and the earlier chunk plans for context on
the surface being exercised.

## Scope

In:

- Extend the demo workstation Python service (`_TestServiceProto`
  in `tests/_demo_workstation.py`) with a `notify_n(cb, n, prefix)`
  method that resolves the callback ref against the active call
  scope and calls `cb.notify(f"{prefix}-{i}")` `n` times in
  sequence (in-call, no background tasks — keeps the test
  deterministic). Returns the number of successful notifies.
- 4 new integration tests in `tests/integration.test.ts`:
  1. **Happy path.** Allocate inbox, invoke `notify_n(handle, 3,
     "msg")`, wait for invoke result to fill, assert `inbox.events`
     emits `["msg-0", "msg-1", "msg-2"]`.
  2. **Cursor advance preserves the right seqs.** Allocate +
     receive 4 events, trigger a flush to ship the initial
     PreserveEventsOp, slice the cursor past 2 events, verify
     server-side `preserved_events` shrinks from 5 (4 callback +
     1 invoke result) to 3, and the local cursor sees only the
     remaining 2 payloads.
  3. **CloseCallbackOp on stub removal.** Allocate inbox,
     observe `session_debug.open_callbacks` populate; remove the
     stub from state, observe `open_callbacks` drop the slot.
  4. **Preserved CallbackEvents survive attach to same
     session_id** (the reset / resnapshot path pulled from
     chunk 3). Detach session A while holding preserved callback
     events, attach session B to the same `session_id`, verify
     B's snapshot rebuilds `inboxLogs` so the inbox stub in
     committed user state emits the same payloads.

Out (deferred):

- Server-driven "callback closed without our action" signal — no
  wire mechanism today.
- Auto-mutation on event arrival (eager `PreserveEventsOp`
  per fill) — same deferred-out as for load/invoke fills.

## File touches

- `frontend_lib/packages/fxtr-ui-connector-workstation/tests/_demo_workstation.py`:
  add `notify_n` to `_TestServiceProto` + `_TestServiceImpl`;
  import `ServiceRef` / `get_active_scope` /
  `EventListenerProto`.
- `frontend_lib/packages/fxtr-ui-connector-workstation/tests/integration.test.ts`:
  add 4 stage-6 integration tests at the end of the file;
  import `WorkstationCallbackInboxStub` from `../src`.

## Implementation steps

1. [x] Add `notify_n` method to `_TestServiceImpl` in the Python
   demo. Imports updated.
2. [x] Stage-6 happy-path integration test (notify_n with 3
   notifies).
3. [x] Cursor-advance integration test (sliced + preserve diff
   via session_debug).
4. [x] CloseCallbackOp integration test (open_callbacks shrinks
   on stub removal).
5. [x] Attach-resnapshot integration test (preserved
   CallbackEvents redelivered to session B).
6. [x] `pnpm check`, `pnpm test`, `pnpm lint` from `v2/ts`; all
   green (809 tests pass, up from 805).
7. [x] Update `CODE_OVERVIEW.md` with the new Python method +
   integration tests.
