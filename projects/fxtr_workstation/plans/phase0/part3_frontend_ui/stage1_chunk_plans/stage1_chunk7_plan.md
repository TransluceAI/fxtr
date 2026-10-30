# Stage 1, Chunk 7 — Integration test (stage-1 deliverable)

The final stage-1 chunk: a live, end-to-end test that exercises
the connector + session + reducer against a real workstation
server. Confirms that chunks 1–6 actually round-trip on the wire
in the way the unit tests claim.

This is the stage-1 deliverable per
[`stage1_foundation.md`](./stage1_foundation.md) §"Deliverable".
Tests cover the deliverable scenarios plus a small set of
extensions agreed during planning.

## Scope

In:

- A trimmed demo workstation Python script that boots a server,
  registers a handful of test entities + services, prints its
  port, and shuts down on signal.
- Vitest harness that spawns the script as a child process per
  suite and tears it down on `afterAll`. Runs by default with
  `pnpm test` (no separate `:integration` command).
- An integration test file that covers the scenarios listed
  below.
- Doc cleanup: update `CODE_OVERVIEW.md`, scrub stale items in
  the stage-1 plan.

Out:

- Reset / reconnect coverage against a live socket. Unit tests
  cover the mechanics deterministically; the live test focuses
  on wire-protocol correctness, not transport-level resilience.
- Connection-lost / connector-closed rollback paths. Same
  reason.
- Stage-2 surfaces (`allocateLoad` etc.). They still throw
  `NotImplementedError`.

## The demo workstation script

Lives at
`projects/fxtr_workstation/frontend_lib/packages/fxtr-ui-connector-workstation/tests/_demo_workstation.py`.
Modeled on the workstation-MCP plugin's `_running_workstation`
pattern (`projects/fxtr_workstation_agent_plugin/fxtr_workstation_mcp/tests/test_mcp_in_memory.py`)
rather than the larger `run_demo_workstation.py` — the MCP test
fixture is the closest existing precedent for "boot a workstation
+ register a couple of entities + tear down for tests."

The pattern:

1. `async with WorkstationServer() as ws:` — workstation binds to
   `127.0.0.1:0` (ephemeral), exposes `ws.bound_url` /
   `ws.rpc_url`.
2. `async with connect_peer_rpc(ws.rpc_url) as peer:` — separate
   `/rpc` connection acting as the entity/service owner.
3. Register entities + services via `peer.scope.mount()` /
   `Entity(...).store(peer.scope)`, then expose under named
   external_names with `expose_entity` / `expose_service`
   async-context managers.
4. Once everything is up, print `READY <port>\n` to stdout.
5. Wait on a SIGTERM/SIGINT signal; on receipt, fall out of the
   async-context stack and exit cleanly.

We can lift `connect_peer_rpc` from the MCP plugin (or copy its
~20-line implementation; trying to import across plugin
boundaries is messier than copying).

Registered fixtures:

- One entity (a small Note or similar — we just need a
  short_id). External name `"e-test"`.
- One service. External name `"s-test"`.

These give the JS test concrete short_ids it can resolve via
`listEntities()` / `listServices()` records, then pass to
`connector.entityRef(record.short_id)`.

Invoked via `uv run python tests/_demo_workstation.py` — `uv`
auto-discovers the workstation package via the parent project
hierarchy.

Anticipated size: 80–120 lines.

## Vitest harness

Lives at `tests/_integration_harness.ts`. Provides:

- `startDemoWorkstation(): Promise<{ port: number; close():
  Promise<void> }>`. Spawns the Python child process, reads
  stdout until it sees `READY <port>`, returns the port + a
  teardown function that sends SIGTERM and awaits exit.
- Suite-level helpers (`beforeAll` / `afterAll`) that wrap the
  above.

Failure modes the harness handles:

- Script fails to start (Python missing, import error, etc.): the
  spawn's `error` event rejects `startDemoWorkstation`.
- Script never prints `READY`: 10-second timeout, kill, reject.
- Test crash mid-suite: `afterAll` always runs, sending SIGTERM.
- Hung child after SIGTERM: SIGKILL after a short grace period.

## Integration test scenarios

In `tests/integration.test.ts`. Each scenario is one `it` block;
shared setup in `beforeAll`/`afterAll`.

### Scenario 1 — connect + create + snapshot

- Open connection to `ws://127.0.0.1:<port>/client`.
- `connector.createSession()` → resolves with session id.
- Session lifecycle transitions `connecting → connected` once
  the snapshot arrives.
- `userState` is `null` (fresh session, empty envelope).

### Scenario 2 — single-session mutation cycle

- Write `{ foo: 1 }`; await commit.
- `userState` reflects `{ foo: 1 }`.
- Write `{ foo: 2, bar: "x" }`; await commit.
- `userState` reflects the new state.

### Scenario 3 — Track/Untrack via entity refs

The workstation exposes a `subscribe_session_debug` channel that
pushes a `SessionDebugSnapshotEvent` with per-session
`entity_refcounts` / `service_refcounts` (plus open invokes,
preserve sets, etc.). Each refcount snapshot is sent on every
refcount change, so we can use it to deterministically verify
Track/Untrack landed.

- Find the entity's short_id from `listEntities()` (matching by
  `external_names` includes `"e-test"`).
- Subscribe to `session_debug` for our session — this uses the
  raw `connection.registerSubscription({ kind: "session_debug",
  session_id })` path (not exposed on the connector today; the
  test reaches into the connection layer directly).
- Write `{ ref: connector.entityRef(shortId) }`; await commit.
- Wait for a `session_debug_snapshot` whose
  `entity_refcounts[shortId] >= 1`.
- Write `{}` (remove the ref); await commit.
- Wait for a `session_debug_snapshot` whose `entity_refcounts`
  no longer mentions `shortId`.
- Same flow for the service ref (`"s-test"`).

The `listEntities({ debug: true })` route was an alternative
(via `pinned_by_sessions?`) but `subscribe_session_debug` is
more direct — it scopes to the specific session and pushes
refcount changes, vs. the entity-list path which requires
finding the right record then filtering.

### Scenario 4 — microtask coalescing live

- Make three synchronous writes in the same tick.
- Await commit.
- Confirm exactly one mutation hit the server's seq counter (the
  next `state_update` we receive from a second session has seq
  N+1, not N+3). Verified via Scenario 6's two-session setup.
- Final `userState` reflects all three writes.

### Scenario 5 — two sessions, foreign update propagation

- Open second session B via `connector.attachSession(sessionId)`.
- B lifecycle transitions `connecting → connected` (snapshot from
  the workstation's current state).
- B's `userState` matches the current committed state (from
  earlier scenarios).
- A writes `{ fromA: 1 }`. Await commit.
- B's `userState` updates to reflect A's write (via foreign
  `state_update` broadcast).

### Scenario 6 — race + reject + rollback + retry

The conflict-detection scenario. A and B share the *same
connector* (and therefore the *same WebSocket connection*),
attached twice to the same `session_id`. This makes the race
deterministic: on a single connection, mutations are serialized
on the wire in the order our flush microtasks dispatch them,
and the server processes them in that order on its single
per-connection queue.

- Bring committed state to a known baseline (e.g.
  `{ counter: 0 }`) via a single commit through one of the
  sessions, awaited.
- *Synchronously* in the same tick:
  - `A.set(userState, prev => ({ ...prev, fromA: 1 }), { onRollback: rA });`
  - `B.set(userState, prev => ({ ...prev, fromB: 1 }), { onRollback: rB });`
- Both writes schedule flush microtasks. A's flush runs first
  (queued first), sends A's mutation. B's flush runs next, sends
  B's mutation. Both go out on the same socket in A-then-B order
  with the same `ack_seq` (B hadn't seen A's commit yet).
- Server processes A first → commits → response to A. Broadcasts
  state_update to B (B is foreign to A's mutation).
- Server processes B → ack_seq is now stale (server's committed
  seq advanced) → reject with `stale_ack`.

Assertions:

- `rA` is *never* called.
- `rB` fires exactly once with `info.kind === "retryable"`.
- A's `userState` reflects the committed `{ counter: 0, fromA: 1 }`.
- B's `userState` reflects the *same* (after rollback to the new
  committed, which absorbed A's update).
- Retry: `B.set(userState, prev => ({ ...prev, retried: true }))`.
  Await commit.
- Final state on both: `{ counter: 0, fromA: 1, retried: true }`.

Determinism rationale: writes scheduled in microtask order
within a single tick flush in that same order. Two sessions on
the same connector share the WebSocket, so the server's
per-connection FIFO matches our send order. If this somehow
turns out flaky in practice (e.g. anyio scheduling on the server
side reorders within a batch), we'd fall back to "exactly one
wins, the other rolls back, symmetric assertion" — but the
deterministic version is worth trying first because it produces
clearer test output and a single failure mode.

### Scenario 7 — terminate flow

- Fresh session.
- `await session.terminate({ status: "done" })`.
- Promise resolves.
- Lifecycle is `closed`.
- The other session (B, still attached) receives a state_update
  with `closed=true` and its lifecycle also flips to `closed`.

### Cleanup between scenarios

Scenarios 5–7 share a single workstation server (the harness
boots once per file). Each scenario uses its own
`createSession` to get an isolated session, except 5–7 which
intentionally chain. Sessions are `detach()`-ed in
`afterAll`/`afterEach` as appropriate.

## Doc cleanup

After the integration tests land:

- Drop the "Some lower-level unit tests for the reducer's diff
  logic" bullet from chunk 7's entry in
  `stage1_foundation.md` — already covered by
  `mutation.test.ts`.
- Drop the "Clean up `NotImplementedError` placeholders"
  bullet — the placeholders for `allocateLoad/Invoke/Callback`
  stay (stage 2).
- Drop the stale "If chunks 5 or 6 turn out to be too big" hedge
  at the bottom of the chunk 6 section.
- Update `CODE_OVERVIEW.md` to mention the integration test
  file and the demo workstation script.

## Implementation steps

1. Write `_demo_workstation.py`. Verify it runs standalone and
   prints `READY <port>`.
2. Write the harness (`_integration_harness.ts`). Verify spawn /
   teardown locally with a no-op test.
3. Scenario 1 (connect + snapshot). Confirms the wire layer +
   chunk 5's session bring-up are correct end-to-end.
4. Scenario 2 (single-session mutation). Confirms chunk 6's
   reducer hits the wire correctly.
5. Scenario 3 (Track/Untrack). Confirms ops are emitted +
   accepted; uses `listEntities({ debug: true })` to verify
   server-side state.
6. Scenario 5 (foreign update propagation). Confirms broadcast
   path works.
7. Scenario 4 (coalescing). Verified via the seq counter in
   Scenario 5's setup.
8. Scenario 6 (race + reject). The interesting one.
9. Scenario 7 (terminate).
10. Doc cleanup + `CODE_OVERVIEW.md` update.

## Resolved questions

- **Entity registration**: use the MCP plugin's
  `_running_workstation` pattern — `WorkstationServer` + peer
  RPC + `expose_entity`/`expose_service`. Lift / adapt
  `connect_peer_rpc` from
  `projects/fxtr_workstation_agent_plugin/fxtr_workstation_mcp/tests/_peer.py`.
- **Tracking unregistered short_ids is rejected by the server.**
  Our test must register real entities before writing refs into
  state — covered by the demo script's startup.
- **Per-session debug surface exists**:
  `subscribe_session_debug` returns `SessionDebugSnapshotEvent`
  carrying `entity_refcounts` / `service_refcounts`. Scenario 3
  uses this directly.
