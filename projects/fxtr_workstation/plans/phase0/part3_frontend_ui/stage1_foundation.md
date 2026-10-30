# Stage 1 — Foundation: refs, mutations, no slots

The minimal-viable workstation client: connect, subscribe to a
session, mutate session state, observe foreign mutations. Refs
work as opaque trackable values. No slot mechanics yet — those
land in stage 2.

## What gets built

- `@fxtr-ui/core` package: core types and framework-agnostic
  interfaces.
  - `EntityRefStub<T>`, `ServiceRefStub<T>` classes (no inner
    atoms yet beyond what's needed for identity).
  - `OpResultStub<T>` / `CallbackInboxStub<R>` *shells* (typed
    class skeletons, no behavior) so the reducer's "walk state
    for stubs" pass has the right shape for stage 2 to fill in.
  - Stub interning per session.
  - Structured state representation (JSON tree with stub
    instances at leaves).
  - List-escape JSON codec (port of `wire_escape.py`), for the
    four existing wire tags (`entity`, `service`, `bytes`,
    `callback`).
  - Abstract `Session` and `Connector` interfaces (with stage-2+
    method signatures present, throwing `NotImplementedError`
    where stage 1 doesn't cover them).
- `@fxtr-ui/connector-workstation` package: WebSocket connector.
  - Single shared WebSocket per connector.
  - Resource subscriptions (rpc_connections, services, entities,
    sessions) plumbed through as reactive lists (jotai atoms over
    `Record[]`, keyed-merge of snapshot + deltas).
  - Session create / subscribe / unsubscribe / close.
  - **State reducer** including:
    - structured state representation + atom
    - state-diff to JSON patch derivation
    - implicit `Track*` / `Untrack*` ops from ref-set diff
    - optimistic pending patch + rollback path
    - foreign state update absorption
  - Reconnect with small built-in backoff; connection status
    surfaced as an atom.

## Deliverable

A TS-level integration test that:

1. Opens a workstation connection.
2. Creates a session, subscribes to it.
3. Mutates state with embedded entity / service ref stubs;
   confirms the server-side session sees the expected track ops.
4. A second connection subscribes to the same session and sees
   the mutations as foreign state updates.
5. Disconnect / reconnect cleanly resumes.

The workstation server is booted by a small dedicated demo
script (modeled on
`projects/fxtr_workstation_agent_plugin/examples/basic_test_demo/run_demo_workstation.py`
but trimmed to what these tests need); vitest spawns it as a
child process and tears it down per suite.

## Resolved up front (pair-programming session, before chunk 1)

These are locked for stage 1; revisit only if implementation
surfaces a real problem.

- **Reactive primitive.** Real `jotai` dependency. Custom
  `splitAtom` / path-aware projection helpers are deferred to a
  pass at the *end* of stage 1, once the surrounding mechanics
  are settled.
- **Mutation API shape.** Jotai-style: components hold writable
  atom handles and call `set(atom, value)` or `set(atom, prev =>
  ...)`. Cross-atom reads use the standard `atom(null, (get,
  set, ...) => ...)` write-atom pattern. No immer-style draft
  API in stage 1.
- **Wire envelope hidden from users.** The session's
  user-facing state atom is the contents of the `{user: ...}`
  envelope; the reducer wraps/unwraps when serializing.
- **JSON Patch.** Small in-house diff (one file in
  `@fxtr-ui/core`). Bundled with the stub-collection walk so we
  don't traverse the structured state twice. Library swap is
  open for later if the diff gets gnarly.
- **Test setup.** Vitest spawns a dedicated demo workstation
  Python process per suite. We write the demo runner ourselves
  rather than reusing the agent-plugin one.
- **Package names.** `@fxtr-ui/core`,
  `@fxtr-ui/connector-workstation`. Future connectors / react
  bindings follow the same scope.
- **Many sessions per connector.** Interfaces shaped for it from
  day one; not stress-tested in stage 1's deliverable.

## Still open (revisit during the chunks)

- **Mutation transaction primitive vs. microtask coalescing.**
  Tracked in `discussion_topics.md`. Current plan: start with
  microtask coalescing; users who need transactional behavior
  call their writes in a single sync block and handle rollback
  themselves. Likely fine; flag if it forces awkward APIs.
- **Jotai update idiom for cross-atom reads.** Updater form
  (`set(atom, prev => …)`) covers the common case; write-atom
  pattern (`atom(null, (get, set, …) => …)`) is the answer when
  you need to read other atoms. Document both; pick an
  ergonomic default once we have real consumers.
- **Path-aware writes.** For stage 1 (no slots, no
  splitAtom-style projections yet) a simple deep-diff is fine.
  Revisit at the end of stage 1 when projections come in.
- **Reconnect strategy details.** Small built-in retry + an
  exposed status atom. The exact backoff curve / when to
  surface failures is a small thing to pin down during the
  connector chunk.

## Implementation chunks

Each chunk is sized to be reviewed and committed on its own.
"Right shape end-to-end" — stage-2+ pieces are typed and
stubbed, not silently absent, so future chunks slot in without
restructuring.

### Chunk 1 — Scaffolding ✅

- [x] Create `projects/fxtr_workstation/frontend_lib/` with
  `packages/fxtr-ui-core/` and
  `packages/fxtr-ui-connector-workstation/` skeletons
  (`package.json`, `tsconfig.json` extending the v2 base,
  empty `src/index.ts`, empty `tests/` directory).
  `@fxtr-ui/connector-workstation` depends on `@fxtr-ui/core`
  via `workspace:*`.
- [x] Wire into:
  - `pnpm-workspace.yaml`
  - `tsconfig.json` references
  - `vitest.config.ts` include glob
  - `package.json` lint/format scripts (extend the
    `packages/` arg list)
- [x] Verify `pnpm install` / `pnpm check` / `pnpm lint` / `pnpm
  test` all see the new packages (20 workspace projects, 441
  tests still green, biome covers 208 files across all three
  package roots).
- [x] Update `v2/ts/CODE_OVERVIEW.md` and
  `projects/fxtr_workstation/CODE_OVERVIEW.md` to list the new
  location.

### Chunk 2 — wire codec, protocol types, stubs, state walks ✅

The split between `@fxtr-ui/core` and `@fxtr-ui/connector-workstation`
landed during this chunk: anything workstation-specific (wire codec,
`/client` protocol types, state walks for JSON-patch derivation)
lives in the connector package; only the cross-connector pieces
(abstract stubs, `StructuredValue`, abstract `Session`) live in
core.

**`@fxtr-ui/core`:**

- [x] `stubs.ts` — abstract `EntityRefStub<T>` / `ServiceRefStub<T>`
  (with user-facing `shortId`) and `OpResultStub<T>` /
  `CallbackInboxStub<R>` (no public surface today; the eventual
  public surface is jotai atoms, see file TODOs). Phantom-symbol
  brands per class so the four stub types are nominally distinct
  and `T` has real variance instead of being silently bivariant.
- [x] `types.ts` — `StructuredValue` (with stubs at leaves) and
  `JsonValue`.
- [x] `session.ts` — abstract `Session` interface with
  slot-allocation methods declared; `NotImplementedError` for use
  by impls that don't cover everything; `UserState` aliased to
  `StructuredValue` (the protocol doesn't constrain the root to a
  dict). No `Connector` interface — connectors are too divergent
  to share a useful one.

**`@fxtr-ui/connector-workstation`:**

- [x] `escape.ts` — port of `fxtr_workstation/wire_escape.py`,
  including the `WireValue` type. Exports `escapeWire` /
  `unescapeWire` (renamed from `escape`/`unescape` to avoid
  shadowing the deprecated JS globals — biome flags this).
  Reserved tags: `entity`, `service`, `bytes`, `callback`.
- [x] `protocol.ts` — wire protocol type unions mirroring
  `client_protocol.py`: requests, responses, session-event /
  mutation-op shapes, resource record shapes,
  `MutationRejectReason`, `RESOURCE_KEY_FIELD`.
- [x] `state.ts` — `collectStubsInState` (pin-diff input),
  `diffStateToPatch` (JSON Patch with wire-encoded values), and
  the `JsonPatchOp` type. Stub equality: entity / service by
  `shortId`; op-result / inbox by reference identity (relying on
  per-slot interning). Two walks instead of one — kept simple
  because trees are small and fusing them adds nontrivial
  complexity; flagged as a future optimization.
- [x] Wire-codec unit tests (24 tests covering primitives,
  containers, literal-list escape, bytes, entity / service
  round-trips, callback tag, error paths). The other modules
  (`protocol`, `state`) are exercised end-to-end via the
  integration test in chunk 7.
- [x] `pnpm check` / `pnpm lint` / `pnpm test` all green (465
  tests total, 24 added).
- [x] Update `frontend_lib/CODE_OVERVIEW.md` with the concrete
  file layout.

### Chunk 3 — `WorkstationServerConnection` (wire layer) ✅

This chunk is just the wire-facing layer: WebSocket transport,
request/response correlation, subscription frame routing,
reconnection. It knows nothing about resources, sessions, or
state structure — those land in chunks 4-5 as a layered
`WorkstationConnector` that consumes this connection.

The first jotai dependency lands here for `connectionStatus`.

#### Public surface

```ts
class WorkstationServerConnection {
  readonly connectionStatus: Atom<
    "connecting" | "open" | "closed" | "reconnecting"
  >;

  // RPC-style: send a request, await its correlated response.
  // Response type is determined by the request shape via a
  // ResponseFor<R> mapped type:
  //   SubscribeRequest               -> AckResponse
  //   UnsubscribeRequest             -> AckResponse
  //   SubscribeSessionRequest        -> AckResponse
  //   SubscribeSessionDebugRequest   -> AckResponse
  //   CreateSessionRequest           -> SessionCreatedResponse
  //   LoadEntityRequest              -> LoadEntityResponse | LoadEntityErrorResponse
  //   MutateSessionRequest           -> MutationCommittedResponse | MutationRejectedResponse
  sendRequest<R extends ClientRequest>(req: R): Promise<ResponseFor<R>>;

  // Streaming-style: register a subscription. Returns a handle
  // synchronously; the underlying subscribe request is still in
  // flight. The handler receives the snapshot + every subsequent
  // frame via `onFrame`, and any termination via `onClose`.
  registerSubscription(
    spec: SubscriptionSpec,
    handler: SubscriptionHandler,
  ): SubscriptionHandle;

  close(): Promise<void>;
}
```

#### Subscription model

```ts
type SubscriptionSpec =
  | { kind: "resource"; resource: ResourceKind; debug?: boolean }
  | { kind: "session"; session_id: string }
  | { kind: "session_debug"; session_id: string };

type SubscriptionCloseReason =
  | "subscribe_failed"   // initial subscribe rejected by server
  | "reset"              // socket dropped; handle dead, re-register if desired
  | "unsubscribed"       // consumer called handle.close()
  | "connector_closed";  // connection torn down

interface SubscriptionHandler {
  onFrame(frame: SubscriptionFrame): void;
  onClose(reason: SubscriptionCloseReason, error?: Error): void;
}

interface SubscriptionHandle {
  close(): void; // idempotent
}
```

All termination paths flow through `onClose` — there is no
separate Promise-rejection path for subscribe failures. The
consumer's state machine has one place to handle "this
subscription ended (for this reason)."

#### Behavioral contract

- **Socket reconnection is automatic.** Linear backoff, clamped to
  a small ceiling (a few seconds). `connectionStatus` reflects
  state. Reconnect does not re-issue subscriptions — see next
  point.
- **Subscription replay is NOT automatic.** When the socket drops,
  the connection fires `onClose("reset")` on every active
  subscription, then forgets them. Consumers (the chunk-4/5
  abstractions) are responsible for calling `registerSubscription`
  again with the same spec if they want the subscription back.
  This keeps the connection layer simple and pushes the policy
  decision ("should we resubscribe? clear state? give up?") to
  the consumer who knows the right answer for its own
  abstraction.
- **Outbound queue.** If the socket isn't open when `sendRequest`
  / `registerSubscription` is called, the request queues and is
  sent when the socket opens. Same for the first send after a
  reconnect: queued sends drain in order.
- **Error categories on `sendRequest`:**
  - `ProtocolError` (transport-level — server's `ErrorResponse`,
    malformed frame, etc.) — promise rejects.
  - `ConnectionLostError` (socket dropped mid-request) — promise
    rejects.
  - `ConnectorClosedError` (connection closed via `close()` while
    request pending) — promise rejects.
  - Per-request typed errors (`MutationRejectedResponse`,
    `LoadEntityErrorResponse`) — promise *resolves* with the
    typed response in the union. Callers dispatch on
    `reason` / etc.
- **`handle.close()` is idempotent.** Calling it on an already-
  closed subscription is a no-op. Sends `UnsubscribeRequest`
  fire-and-forget when the subscription is still active and the
  socket is alive; otherwise just removes the handler locally.
- **Jotai store.** Takes an optional `store?: Store` (jotai's
  `createStore` return type); defaults to `getDefaultStore()`.
  Standard pattern for store-scoped libraries, required for
  test isolation.

#### Inbound frame dispatch

A single read-loop pass per text frame:

1. `JSON.parse(text) as ServerFrame`.
2. Switch on `frame.type`:
   - `ack` / `error` / `session_created` / `mutation_committed`
     / `mutation_rejected` / `load_entity_response` /
     `load_entity_error` → look up `request_id` in the
     correlation map; resolve / reject the pending promise.
     `error` rejects with `ProtocolError`.
   - `resource_snapshot` / `resource_changes` / `session_snapshot`
     / `session_event` / `session_debug_snapshot` → look up
     `subscription_id` in the routing map; call
     `handler.onFrame(frame)`.
   - Anything unrecognized: log + drop. We don't want a single
     bad frame to kill the connection.

The handler is registered *before* the ack arrives, so the
snapshot (which the server sends immediately after the ack)
routes to the right place.

#### Files

- `src/connection.ts` — the class itself plus the error types
  (`ProtocolError`, `ConnectionLostError`, `ConnectorClosedError`).
- Export from `src/index.ts`.

#### What this chunk does *not* include

- Resource subscription APIs (chunk 4).
- Session creation / attachment (chunk 5).
- `WorkstationConnector` (the consumer-facing layer that wraps
  this connection with resource lists + session methods) —
  introduced in chunk 4.

#### Implementation notes

- [x] `connection.ts` written; all public surface above lands in
  one file. `jotai` added as a runtime dependency on the
  workstation package.
- [x] `webSocketCtor` option on `WorkstationServerConnectionOptions`
  lets tests inject a mock WebSocket constructor (defaults to the
  global `WebSocket`). Mock lives in
  `tests/_mock_websocket.ts` and exposes
  `simulateOpen` / `simulateMessage` / `simulateClose` for
  synchronous lifecycle control.
- [x] `tests/connection.test.ts` covers the lifecycle, sendRequest
  correlation + every error class, subscription's four close
  reasons (subscribe_failed / reset / unsubscribed /
  connector_closed), handler exception isolation, reconnect
  backoff (with vitest fake timers), and warning-on-unexpected-
  condition behavior. 34 tests total.
- [x] `pnpm check` / `pnpm lint` / `pnpm test` all green (499
  tests across the workspace).
- [x] Update `frontend_lib/CODE_OVERVIEW.md` with the
  `connection.ts` + tests summary.

### Chunk 4 — Resource subscriptions ✅

Introduces `WorkstationConnector`, the higher-level wrapper that
consumes a `WorkstationServerConnection`. Adds the four resource
list APIs.

- [x] `WorkstationConnector` with one constructor; options are a
  discriminated union — `{ url, store? }` mints and owns its own
  connection, `{ connection, store? }` borrows an externally-owned
  one (the path tests take, since the URL-based form deliberately
  doesn't expose `webSocketCtor`). Initially just exposes the
  resource-list methods; chunk 5 grows the session-creation API.
- [x] `connector.listRpcConnections()` / `listServices()` /
  `listEntities()` / `listSessions()` each return a
  `ReactiveList<RecordShape>` — a thin wrapper around a primary-
  key-keyed jotai map atom, applying snapshot + added/updated/
  removed deltas. Each accepts `{ debug?: boolean }`; debug and
  non-debug lists are cached independently. Lists are
  connector-owned singletons: repeated calls return the same
  instance, and there's no public `close()` on `ReactiveList` —
  teardown is centralized through `connector.close()`, which
  cascades onClose("connector_closed") for the owned-connection
  case or sends an explicit unsubscribe per list for the
  borrowed-connection case. The factory hands the connector a
  private `release()` fn alongside the public list so this
  asymmetry doesn't leak through the consumer-visible surface.
- [x] Each `ReactiveList` owns its `SubscriptionHandle` and on
  `onClose("reset")` flips status to `"stale"` (preserves last
  records so consumers don't UI-flash), then re-registers
  immediately — the connection's outbound queue holds the
  resubscribe across the reconnect. On `"connector_closed"` /
  `"unsubscribed"` / `"subscribe_failed"` it marks itself
  `"closed"` (terminal); a subsequent `listX()` call returns the
  same closed instance rather than retrying.
- [x] Real-workstation round-trip deferred to chunk 7 (the
  stage-1 integration test). Chunk 4 ships with `MockWebSocket`-
  driven unit tests covering snapshot, deltas, debug-split
  caching, subscribe-failure → closed-cache-retention,
  reset/re-register, multi-list independent resubscribe,
  borrowed- vs. owned-connection close semantics (the owned-
  connection test stubs `globalThis.WebSocket` to `MockWebSocket`
  for that one case), and post-close rejection.
- [x] Update `frontend_lib/CODE_OVERVIEW.md` with `connector.ts`
  + `resource_list.ts` + `connector.test.ts` summaries.

### Chunk 5 — Session read side ✅

Adds session creation / attachment to `WorkstationConnector` and
introduces the `WorkstationSession` class. The session is the
first place in the design with an explicit finite state machine,
since it coordinates (a) the in-flight subscription, (b) the
source-of-truth atom, (c) reset / resubscription, and (d)
terminal close events.

Landed:

- [x] `WorkstationConnector.createSession(metadata)` (async) and
  `attachSession(sessionId)` (sync). Both throw
  `ConnectorClosedError` on a closed connector;
  `createSession` re-checks the flag after the round-trip in
  case the connector closed mid-flight.
- [x] No connector-side session tracking. Sessions self-manage
  via their `onClose` handlers; owned-connection close cascades
  through, borrowed-connection close leaves them alone.
- [x] `WorkstationSession` class with 4-state FSM (`connecting /
  connected / stale / closed`), re-register-on-reset (immediate;
  no `connectionStatus` watcher — connection's outbound queue
  holds the resubscribe across reconnect).
- [x] `detach()` fire-and-forget unsubscribe; idempotent;
  flips to terminal `closed`. `terminate()` (server-side
  delete via `CloseOp`) declared, throws `NotImplementedError`
  pending chunk 6.
- [x] Abstract `Session` in `@fxtr-ui/core` now declares
  `lifecycle`, `userState`, `detach()`,
  `SessionLifecycleState`. Jotai is a runtime dep of core.
- [x] Public `lifecycle` / `userState` atoms are derived
  read-only views (`atom((get) => get(privateAtom))`) so
  consumers can't write via cast or smuggled `store.set`.
  Chunk 6 will swap `userState` for a custom writable atom
  whose write function runs the mutation reducer.
- [x] Per-session entity / service stub interning via
  `unescapeWire` lookup callbacks. New concrete
  `WorkstationEntityRefStub` / `WorkstationServiceRefStub` in
  `stubs.ts`.
- [x] Protocol types tightened: wire-encoded fields
  (`StateUpdateEvent.state`, `*Event.payload`, `InvokeOp.args`,
  `LoadEntityResponse.payload`, `JsonPatchWireOp.value`) typed
  as `WireValue` instead of `unknown` / `Record<string,
  unknown>`. Removes a per-use-site cast at the session
  boundary. Metadata fields stay broader since they're plain
  JSON, not wire-encoded.
- [x] Empty-state envelope (`state: {}`) handled silently —
  fresh sessions are normal. Only warn for actual protocol
  violations (non-dict root after decode).
- [x] 24 new tests; 541 green total.
- [x] Update `CODE_OVERVIEW.md` for the new files and the
  protocol changes.

Open questions surfaced during implementation:

- `JsonPatchWireOp` (protocol) and `JsonPatchOp` (state) are now
  structurally identical. Left both defined to preserve the
  `protocol.ts ⊥ state.ts` separation; could consolidate later
  if a single canonical home emerges.

#### `WorkstationConnector` additions

```ts
class WorkstationConnector {
  // ...resource list methods from chunk 4...
  createSession(metadata: Readonly<Record<string, unknown>>): Promise<WorkstationSession>;
  attachSession(session_id: string): WorkstationSession;
}
```

`createSession` is async because it sends `CreateSessionRequest`
and awaits the new `session_id` before constructing the session.
`attachSession` is sync — it constructs the session immediately;
the session's own state machine handles the in-flight subscribe.
Both throw `ConnectorClosedError` if called on a closed connector
(same pattern as `listX`).

The connector does **not** track sessions in any collection. Each
session self-manages via its `onClose` handler: in the owned-
connection mode, `connector.close()` closes the connection, which
fans `onClose("connector_closed")` to every active subscription,
and each session transitions to terminal `closed` on its own. In
the borrowed-connection mode, `connector.close()` leaves the
connection and sessions alone — the consumer detaches them
explicitly when they're done. `attachSession` always returns a
fresh instance (two `attachSession("abc")` calls = two
independent subscriptions to the same `session_id` — sessions
carry consumer-visible state so a shared-cache model doesn't fit
cleanly).

#### `WorkstationSession` finite state machine

```
States:
  connecting   - SubscribeSessionRequest in flight; no snapshot yet
  connected    - snapshot received; applying live frames
  stale        - had a snapshot, socket dropped; re-registered and
                 awaiting the next snapshot. `userState` retains
                 its last value for UI continuity.
  closed       - terminal: detach()-ed, subscribe_failed,
                 connector_closed, or server-side close
                 (state_update.closed=true). Also terminal after
                 `terminate()` once that lands in chunk 6.

Transitions:
  (init)         -> connecting    constructor sends subscribe
  connecting     -> connected     first onFrame (snapshot) arrives
  connecting     -> connecting    onClose("reset") - re-register,
                                  no userState yet so no "stale"
  connecting     -> closed        onClose("subscribe_failed"
                                          | "connector_closed")
                                  or terminal state_update
                                  or detach()
  connected      -> stale         onClose("reset") - re-register,
                                  keep last userState
  connected      -> closed        terminal state_update
                                  or onClose("connector_closed")
                                  or detach()
  stale          -> connected     next snapshot arrives
  stale          -> stale         onClose("reset") (chained drops)
  stale          -> closed        onClose("connector_closed")
                                  or detach()
```

Like `ReactiveList`, re-registration on reset happens
immediately — the connection's outbound queue holds the
subscribe across the reconnect, so the session never has to
watch `connection.connectionStatus`.

Lifecycle exposed as `Atom<SessionLifecycleState>`. `userState`
is `Atom<UserState | null>`: `null` until the first snapshot
arrives, populated thereafter, retained across `stale` so
consumers can render last-known state with a "reconnecting"
banner. The `stale` state gives consumers a single check
("is this data current?") without forcing them to inspect
`userState`'s null-ness in combination with lifecycle.

#### Session responsibilities

- **State atom.** On `SessionSnapshotEvent`, decode
  `last_state.state` through `unescapeWire` with the per-session
  intern lookups and seed the atom with the `user` field of the
  envelope (the rest of the envelope is server bookkeeping).
  On `SessionEventFrame.state_update`, replace the committed
  state the same way and bump `last_state_seq` / `ack_seq`. On
  reset, `userState` is left untouched.
- **Non-state events** (load/invoke/callback/metadata results).
  Routed to a no-op handler in chunk 5 so the shape's in place
  for chunk 6 (mutation reducer's optimistic-pending machinery)
  and stage 2 (slot mechanics) to fill in.
- **Per-session stub interning.** Two `Map<string, WorkstationEntityRefStub>` /
  `Map<string, WorkstationServiceRefStub>` owned by the session.
  `unescapeWire` calls back into the session's `lookupEntity` /
  `lookupService` callbacks, which mint-and-intern.
- **Resubscribe on reset.** `register()` runs from the
  constructor and again on `onClose("reset")`. New
  subscription_id, fresh snapshot from the server, FSM handles
  it like a normal connecting/stale → connected transition.

#### Public API (read-only in chunk 5)

```ts
class WorkstationSession extends Session {
  readonly sessionId: string;
  readonly lifecycle: Atom<SessionLifecycleState>;
  readonly userState: Atom<UserState | null>;

  detach(): void;
  // chunk 6+: terminate(): Promise<void>  // CloseOp + await state_update.closed=true
}
```

`detach()` is fire-and-forget: sends `UnsubscribeRequest` via
the connection (which itself doesn't await an ack), flips
lifecycle to `closed` synchronously. Idempotent. The server's
session lives on; another client could attach to it.

`terminate()` (chunk 6+) is the "actually delete the session
on the workstation" operation: rides the mutation reducer with
a `CloseOp`, awaits `state_update.closed=true`. Declared on
`WorkstationSession`, not on the abstract `Session` — HTTP
sessions have no server-side analogue. The abstract `Session`
in `@fxtr-ui/core` adds `detach()` to the existing
`allocateLoad` / `allocateInvoke` / `allocateCallback` surface,
since "I'm done watching" is a portable concept across
backends. TODO comment in core flags that `terminate()` may
move there if HTTP grows a meaningful semantic.

Mutation methods (`allocateLoad`, `allocateInvoke`,
`allocateCallback`) declared on the abstract `Session` from
`@fxtr-ui/core` throw `NotImplementedError` until chunk 6.

### Chunk 6 — Session write side (the mutation reducer) ✅

Detailed spec in
[`stage1_chunk6_plan.md`](./stage1_chunk6_plan.md).

Landed:

- [x] Whole-atom write API for `userState` (replace + updater
  forms) with optional `onRollback` per write. Path-aware
  projections deferred to a possible chunk 8.
- [x] Two private envelope atoms (`committedAtom`, `localAtom`)
  over `LocalEnvelope` (wire envelope + synthetic
  `terminated?: true`). Bookkeeping for the outstanding mutation
  (envelope, `updateId`, `sendPromise`, accumulated rollback
  callbacks) lives in a single `inflight: Inflight | null`
  struct (no reactive observer needs it, so no atom). Public
  `userState` is a custom writable atom routing writes via
  jotai's `set` callback; public `lifecycle` is a derived
  read-only view.
- [x] Microtask-coalesced flush, tracked by a
  `pendingFlushPromise: Promise<void> | null` (resolves when the
  scheduled flush microtask runs). Diff `wirePart(committed)`
  vs `wirePart(local)` for the patch; Track/Untrack ops via
  `collectStubsInState`; `CloseOp` when `local.terminated &&
  !committed.terminated`. Empty-diff short-circuit avoids no-op
  mutations.
- [x] Commit / reject / connection-lost paths.
  `MutationCommittedResponse` promotes inflight to committed
  with strict `lastProcessedSeq` assertion.
  Failure paths revert `local` to `committed` and fire
  accumulated `onRollback`s in forward (oldest-first)
  registration order with a `RollbackInfo` of `kind: "retryable"
  | "permanent"` (classified by `classifyReject` /
  `classifyTransportError`). `session_gone` reject also flips
  lifecycle to closed.
- [x] Foreign `state_update` integration: `applyStateUpdateEvent`
  decodes + replaces `committed` with a strict-`>` seq check;
  brings local along if no local divergence; leaves local alone
  if dirty/in-flight (next mutation will stale_ack-reject
  naturally). `lastProcessedSeq` advanced on every processed
  event. Stale duplicate state_updates dropped with warn.
  Non-state events (load/invoke/callback/metadata) are dropped
  *but* their seq is still acked — "decided to do nothing"
  still bumps `lastProcessedSeq` so the server can garbage-
  collect. Snapshots take a separate `applySnapshot` path that
  sets `lastProcessedSeq` unconditionally (baseline, not
  increment); both share an `absorbState` helper.
- [x] `terminate(finalState?, options?): Promise<void>`: sets
  `local.terminated = true` (+ optional finalState), schedules
  the flush. On commit the promise resolves and lifecycle flips
  to closed. On rollback the promise rejects with
  `MutationRolledBackError` (carrying the `RollbackInfo`). The
  shared `assertWritable("terminate")` gate makes repeat
  terminate / write-during-terminate / write-during-detach all
  reject uniformly.
- [x] `detach()` is async (`Promise<void>`); drains
  in-flight (`inflight.sendPromise`) + pending flushes
  (`pendingFlushPromise`) before sending unsubscribe. Writes
  after detach throw. Idempotent.
- [x] Stub interning moves from `WorkstationSession` to
  `WorkstationConnector`: `connector.entityRef(shortId)` /
  `connector.serviceRef(shortId)` mint-and-intern. The session
  takes lookup callbacks via its construction options; wire
  decoders delegate. Stubs are now `===` across sessions for
  the same shortId.
- [x] Core (`@fxtr-ui/core`) adds `RollbackInfo` (collapsed to
  `{ kind: "retryable" | "permanent"; message: string }`),
  `MutationRolledBackError`, `UserStateWrite`,
  `UserStateWriteOptions`. `Session.userState` tightened to
  `WritableAtom<UserState | null, [UserStateWrite,
  UserStateWriteOptions?], void>`. `Session.detach()` is now
  `Promise<void>`.
- [x] `SubscriptionHandle` from the connection layer now
  exposes `subscriptionId` (the underlying subscribe request's
  request_id) so consumers building subscription-scoped
  requests like `MutateSessionRequest` can read it.
- [x] Slot-allocation surfaces (`allocateLoad`,
  `allocateInvoke`, `allocateCallback`) still throw
  `NotImplementedError`; they land with the rest of slot
  machinery in stage 2.
- [x] 30 new mutation tests in `mutation.test.ts`; 570 green
  total.
- [x] Update `CODE_OVERVIEW.md`.

### Chunk 7 — Integration test (stage-1 deliverable) ✅

Detailed spec in
[`stage1_chunk7_plan.md`](./stage1_chunk7_plan.md).

Landed:

- [x] `tests/_demo_workstation.py` — trimmed Python demo
  workstation: `WorkstationServer` + peer RPC + registered
  entity (`e-test`) + registered service (`s-test`). Prints
  `READY <url>` on stdout; parks until SIGTERM. Modeled on the
  MCP plugin's `_running_workstation` test fixture.
- [x] `tests/_integration_harness.ts` — `startDemoWorkstation()`
  spawns the script via `uv run`, reads `READY <url>`, returns
  a handle that does graceful SIGTERM + 5s SIGKILL fallback.
  15s timeout on READY.
- [x] `tests/integration.test.ts` — 7 live scenarios:
  - **1**: connect + create + reach `connected` with
    `userState=null`.
  - **2**: single-session writes commit and update userState.
  - **3**: Track/Untrack — write entity+service refs into
    state, verify `subscribe_session_debug`'s
    `entity_refcounts`/`service_refcounts` go to 1 then back
    to 0 when refs are removed.
  - **4**: microtask coalescing — three sync writes
    commit as one logical update.
  - **5**: foreign update propagation — A's write visible
    to B via state_update broadcast.
  - **6**: race + reject + rollback + retry — A and B
    write concurrently on same baseline; A commits, B
    rejects with `retryable`, B retries with rebase and
    succeeds.
  - **7**: terminate — `await session.terminate()` flips
    lifecycle to closed; another session observes the
    terminal state_update.
- [x] Doc cleanup: stale "split chunks 5/6" hedge removed;
  scrubbed dropped "NotImplementedError cleanup" bullet (those
  are stage-2 work, they stay). Update `CODE_OVERVIEW.md`.

577 tests green total (29 chunk-6 mutation tests + 7 chunk-7
integration tests + the pre-existing chunk-1-5 unit suite).

## Design notes to consult

- `design_notes/01_reactive_model.md` — overall reducer shape.
- `design_notes/02_connector_abstraction.md` — Session /
  Connector surfaces.
- `design_notes/03_stub_types.md` — `EntityRefStub`,
  `ServiceRefStub` shape (and `OpResult` / `CallbackInbox`
  shells for stage 1).
- `design_notes/04_session_state_layout.md` — state layout and
  reducer responsibilities (note: slot machinery is stage 2).
- `design_notes/06_no_schema_for_now.md` — runtime data model.
