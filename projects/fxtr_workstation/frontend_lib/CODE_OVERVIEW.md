# frontend_lib

A family of `@fxtr-ui/*` TypeScript packages providing the frontend
substrate for fxtr-based UIs. Designed to talk to either the
workstation server's `/client` WebSocket (reactive, reducer-based,
session-scoped) or a static HTTP fxtr namespace (read-only).
Framework-agnostic at the core; React bindings live in their own
package.

Frontend code consuming these packages never imports `@fxtr/core` or
`@fxtr/react` — those remain for backend / background-job code that
needs full fxtr semantics.

TODO: once the surface stabilizes, the framework-agnostic packages
here will relocate to `v2/ts/packages/`. See
[`../plans/phase0/part3_frontend_ui/overview.md`](../plans/phase0/part3_frontend_ui/overview.md)
for the package split and design rationale, and
[`../plans/phase0/part3_frontend_ui/design_notes/`](../plans/phase0/part3_frontend_ui/design_notes/)
for the per-topic design notes (reactive model, connector
abstraction, stub types, session state layout, dispatch, schema
decision).

## Workspace integration

These packages are part of the **repo-root pnpm workspace** (see
[`../../../pnpm-workspace.yaml`](../../../pnpm-workspace.yaml)),
alongside the core fxtr TS packages in `v2/ts/packages/*`, the
report-builder CLI, the workstation-demo frontend, and the archived
`spikes/old_typescript/*` packages. One `pnpm install` from the repo
root links everything; one `pnpm check` / `pnpm lint` / `pnpm test`
from there covers all of it. See
[`../../../v2/ts/CODE_OVERVIEW.md`](../../../v2/ts/CODE_OVERVIEW.md)
for the workspace's tooling conventions (TypeScript project
references, biome, vitest).

## Package layout

```
frontend_lib/
├── CODE_OVERVIEW.md
└── packages/
    ├── fxtr-ui-core/                    # @fxtr-ui/core — abstract stub classes, structured value types, abstract Session + Connector, memoryStorage, shared wire codec
    ├── fxtr-ui-state-util/              # @fxtr-ui/state-util — framework-free path projection helpers (subAtom, atomWithDefault, makeLoadResultAtom, makeInvokeResultAtom)
    ├── fxtr-ui-connector-workstation/   # @fxtr-ui/connector-workstation — protocol types, state walks, WebSocket connector (re-exports the shared codec from core)
    ├── fxtr-ui-connector-http/          # @fxtr-ui/connector-http — stateless HTTP/ws connector (codec, shortId table, ws pool, entity decode, local-reducer session, storage persistence) — part 7a
    ├── fxtr-ui-react/                   # @fxtr-ui/react — React providers + hooks
    ├── fxtr-ui-react-dispatch/          # @fxtr-ui/react-dispatch — type-driven dispatch over EntityRefStub / ServiceRefStub (static + dynamic dispatcher, staleness wrap)
    ├── fxtr-ui-react-dispatch-dynamic-shim/ # @fxtr-ui/react-dispatch-dynamic-shim — runtime import-map shim for dynamic-dispatcher renderer modules
    ├── fxtr-ui-workstation-dispatch/    # @fxtr-ui/workstation-dispatch — canonical workstation DispatchKeys + fallbacks + RefLink + JsonRefTree + MetadataViews (externalized for renderer-author sharing)
    ├── fxtr-ui-rd-types-stream/         # @fxtr-ui/rd-types-stream — StreamEntry dispatch key + fallbacks (reusable across systems that render ordered streams of typed entries)
    ├── fxtr-ui-rd-types-channel/        # @fxtr-ui/rd-types-channel — output-channel (fxtr2.common.channels) reader rendering: ChannelReaderView + tag-dispatched keys + generic card (w/ debug subchannel list) + SubchannelView + channel-reader service renderers
    ├── fxtr-ui-panel-viewer/            # @fxtr-ui/panel-viewer — generic, connector-agnostic dockview panel viewer: per-panel sessions, chain/breadcrumb/history nav, reflink interaction impls + ShellRefLinkImpl, sidebar, layout persistence, injected panel registrations, system-type renderers (see LOCAL_CODE_OVERVIEW.md)
    ├── fxtr-ui-workstation/             # @fxtr-ui/workstation — the four workstation-connector panels (roots + three debug) as injected registrations over @fxtr-ui/panel-viewer; re-exports the viewer surface for back-compat (see LOCAL_CODE_OVERVIEW.md)
    ├── fxtr-ui-reports/                 # @fxtr-ui/reports — Figure dispatch key + RootReport/SubReport renderers (see LOCAL_CODE_OVERVIEW.md)
    ├── fxtr-ui-agent-plugin-renderer/   # @fxtr-ui/agent-plugin-renderer — FullPanel for agent-MCP session service + StreamEntry renderers for the four log-event types
    ├── fxtr-ui-demo/                    # @fxtr-ui/demo — Vite dev app exercising the @fxtr-ui/* substrate against a real workstation
    ├── fxtr-ui-dispatch-demo/           # @fxtr-ui/dispatch-demo — Vite dev app exercising the dispatch package end-to-end
    ├── fxtr-ui-dynamic-dispatch-demo/   # @fxtr-ui/dynamic-dispatch-demo — Vite dev app for the dynamic dispatcher; serves both a legacy DispatchGrid view and a workstation-shell view
    └── fxtr-ui-workstation-demo/        # @fxtr-ui/workstation-demo — Vite dev app exercising the workstation shell end-to-end
```

TODO (planned, not yet present — see the overview doc above):

- `fxtr-ui-connector-http` — HTTP connector against the existing
  fxtr-data server (read-only; single virtual session).

## Packages

### `@fxtr-ui/core` (`packages/fxtr-ui-core/`)

Cross-connector foundation: abstract stub classes, the structured
state value type, the abstract `Session` + `Connector` surfaces, and
`memoryStorage`. Nothing connector-specific lives here. Source modules
in `src/`:

- `stubs.ts` — abstract base classes for the client-side stub
  hierarchy: `EntityRefStub<T>`, `ServiceRefStub<T>`,
  `OpResultStub<T>`, `CallbackInboxStub<R>`, and `CallbackHandleStub<R>`.
  Concrete subclasses live in connector packages; the abstract
  classes exist for `instanceof` checks and structural typing.
  Phantom-symbol brands give each class nominal identity and make the
  type parameter variance-meaningful.

  `EntityRefStub` / `ServiceRefStub` expose a user-facing `shortId`.
  `OpResultStub` exposes three abstract atoms — `result:
  Atom<ResultStatus<T>>`, `committed: Atom<boolean>`, `done:
  Atom<boolean>` — implemented by connector-side subclasses;
  `ResultStatus<T>` is the `pending | filled | errored` union.
  `CallbackInboxStub` exposes `offset: number` (the cursor's absolute
  event-consumed count), `events: Atom<readonly R[]>`, `state:
  Atom<"opening" | "open" | "closed">`, `connectionState:
  Atom<"idle" | "connected" | "broken">` (the `subscribe_to` feed's
  connection state — orthogonal to `state`), `subscriptionBreakReason:
  Atom<string | null>` (the reason behind a `broken` connection),
  `committed: Atom<boolean>`, a
  `handle: CallbackHandleStub<R>` getter (the send-side companion),
  and `sliced(n)` (returns a fresh stub at `offset + n`). Clearing
  visible events is `inbox.sliced(events.length)` — there is no
  dedicated `cleared()` method, so the snapshot point (render time
  vs. action time) is always explicit in user code. The underlying
  slot integer is *not* exposed on the abstract base — it's
  workstation-specific identity.

  `CallbackHandleStub` is the opaque send-side counterpart (wire form
  `["callback", slot]`); pass `inbox.handle` (not the inbox itself)
  as an invoke arg when the callee needs to notify into the inbox.
- `types.ts` — three structured-value families with different
  admitted stub kinds:
  - **`ResultValue`** — payloads decoded out of result / callback
    events; primitives + entity/service refs only.
  - **`StateValue`** — `Session.userState`-shaped; adds
    `OpResultStub` and `CallbackInboxStub` (the receive-side client
    stubs).
  - **`InvokeArgValue`** — allocate-invoke arg-shaped; adds
    `CallbackHandleStub`, drops `OpResultStub` and
    `CallbackInboxStub`.

  `ResultValue ⊆ StateValue ∩ InvokeArgValue` structurally. Plus
  `JsonValue` (plain JSON). Connector-specific wire shapes live in
  the connector that owns them.

  Also exports the typed-protocols type layer:
  `Method<Args, Kwargs, Returns>` (descriptor for one method on a
  service protocol), `ServiceProto` (a record of `Method`
  declarations), and the `MethodNames` / `MethodArgs` /
  `MethodKwargs` / `MethodReturns` extraction helpers consumed by
  `FxtrSessionUpdateCtx.allocateInvoke`'s signature. Purely type-only
  — no runtime metadata. Consumers declare a protocol as `interface
  MyProto { foo: Method<[string], {}, number>; ... }`, type a service
  ref via `connector.serviceRef<MyProto>(...)`, and
  `ctx.allocateInvoke(svc, "foo", [...])` typechecks the method
  name, arg/kwarg shape, and return type against the declaration.
- `session.ts` — the abstract `Session` interface and the types that
  ride alongside it:
  - `UserState` — the user-facing contents of the wire `{user: ...}`
    envelope.
  - `SessionLifecycleState` — the lifecycle union (`connecting /
    connected / stale / closed / unavailable`); all backends share the
    type, and HTTP-like backends simply never enter `"stale"` /
    `"unavailable"`.
  - `RollbackInfo` — `{ kind: "retryable" | "permanent"; message:
    string }`; passed to write `onRollback` callbacks and carried by
    `MutationRolledBackError.info`.
  - `MutationRolledBackError` — thrown when a Promise-returning
    mutation method like `terminate()` is rolled back.
  - `UserStateWriteOptions` — write API options.

  The ctx-style write surface: `FxtrSessionUpdateCtx` (passed as the
  second arg to function-form updaters of a session-bound atom;
  carries `allocateLoad` / `allocateInvoke` / `allocateCallback` /
  `allocateGetEntityMetadata` / `allocateGetServiceMetadata`
  bound to the owning session, plus a `session` escape hatch),
  `FxtrUpdate<T> = T | ((prev: T, ctx: FxtrSessionUpdateCtx) => T)`,
  and `FxtrSessionAtom<T> = WritableAtom<T, [FxtrUpdate<T>,
  UserStateWriteOptions?], void>`. There is **no** public
  `session.allocate*` method — slot allocation is only reachable
  through ctx, which structurally enforces the same-tick
  "allocate-and-store" contract (the slot must be referenced from
  the value the updater returns, or it's dead after the next flush).
- `metadata.ts` — typed shapes for `allocateGetEntityMetadata` /
  `allocateGetServiceMetadata` returns:
  - `EntityMetadata` — `id_string` (always) plus optional
    `unprovenanced` / `provenance_parent` /
    `provenance_parent_id_string` / `unwraps_to` /
    `unwraps_to_id_string` / `context_data` / `backstep`. Ref-typed
    fields are `EntityRefStub` instances (decoded from the workstation's
    list-escape-tagged metadata payload via `unescapeWire`).
  - `ServiceMetadata` — `type_id_string` (only field today; shape
    exists so future fields slot in without API churn).

  `Session` exposes `sessionId`, `lifecycle: Atom<...>`, `userState:
  FxtrSessionAtom<UserState | null>` (writes route through the
  connector's mutation reducer; function-form updaters get ctx;
  static values pass through unchanged), `detach(): Promise<void>`
  (releases the consumer's handle; drains pending mutations first), and
  `terminate(finalState?, options?): Promise<void>` (destroy the
  session — the counterpart to `detach`'s "release but keep"). Both
  connectors implement `terminate`: the workstation ships a `CloseOp`
  mutation (rejectable); the HTTP connector destroys local state +
  deletes the session's persisted storage. `finalState` writes a
  parting user-state change observable before lifecycle goes `closed`;
  `onRollback` fires only on backends that can reject the close. The
  generic panel-viewer's terminate-on-panel-close contract consumes it.
  See [`design_notes/02_connector_abstraction.md`](../plans/phase0/part3_frontend_ui/design_notes/02_connector_abstraction.md).

  Also defines the **`Connector`** abstraction (sibling to `Session`):
  the consumer-facing post-construction surface shared by every
  connector — `store`, `url`, `connectionStatus: Atom<ConnectionStatus>`,
  `entityRef` / `serviceRef` (idempotent interning), `classifyShortId`,
  `loadEntityData`, `createSession` / `attachSession`, `close`. The
  *constructor* side stays per-connector; concrete connectors are
  declared `implements Connector` and may expose supersets. Plus
  `ConnectionStatus` (`connecting / open / reconnecting / closed`), the
  transport-connectivity union (distinct from a session's lifecycle).
- `storage.ts` — `memoryStorage(): Storage`, a `Map`-backed
  implementation of the standard Web Storage interface for tests / SSR
  (real `localStorage` / `sessionStorage` pass in directly; this is the
  one variant with no built-in). The connector + PanelViewer share one
  `Storage` handle.
- `escape.ts` — the shared list-based escape codec and the `WireValue`
  type (relocated here from `@fxtr-ui/connector-workstation` so a second
  connector can share it; it operates on the abstract stubs through
  caller-supplied callbacks, so it's connector-agnostic). TS port of
  `fxtr_workstation/wire_escape.py`. `escapeWire(value, opts)` /
  `unescapeWire(value, opts)` round-trip between `StateValue |
  InvokeArgValue` and `WireValue` (the list-escape JSON form). Reserved
  tags: `entity`, `service`, `bytes`, `callback`, `inbox`,
  `broken_inbox`, `op_result`, `broken_op_result`. The two `broken_*`
  tags are frontend conventions for client-side cross-session-
  substitution bookkeeping; the server stores wire state opaquely and
  doesn't interpret them. `unescapeWire` is overloaded so the absence of
  `lookupOpResult` / `lookupInbox` in the opts narrows the return to
  `ResultValue`; supplying them widens to `StateValue`.

  Also exports `escapeInvokeArgs(args, kwargs, opts)` for encoding an
  invoke's positional + keyword arguments — walks each value through
  `escapeWire`, rejects `OpResultStub` and `CallbackInboxStub` at any
  depth (the type system already rejects them via the `InvokeArgValue`
  constraint; runtime is defense-in-depth), and routes
  `CallbackHandleStub` through `opts.encodeCallbackHandle`.

  `["op_result", slot]` (slot > 0; encoded via `opts.encodeOpResult`,
  decoded via `opts.lookupOpResult`) carries the receive side of a
  load / invoke / metadata op. `["broken_op_result", slot]` (slot ≥ 0)
  carries a known-broken slot reference: `slot = 0` is the universal
  dead-sentinel, `slot > 0` identifies a flagged-broken slot from the
  receiving session's namespace — `lookupOpResult` is called with
  `broken = true` so the session can propagate the brokenness side
  effect. Throws `WireEscapeError` on any malformed input (including
  `["op_result", 0]` — slot 0 is reserved for the dead-sentinel and only
  valid under the `broken_op_result` tag).

  The codec supports two **modes**, selected entirely by the
  caller-supplied callbacks (the HTTP connector uses "resolved mode" for
  the server wire; everyone uses "shortId mode" for persisted state):
  the **entity** payload is always a string (a server short_id, or a
  content-addressed `id_string` in resolved mode); the **service**
  payload is either a short_id **string** (`encodeService` returns a
  string → decoded via `lookupService`) or a self-describing
  **descriptor object** (`encodeService` returns an object → decoded via
  `lookupServiceDescriptor`); the **callback** payload is a slot int or
  an inbox uuid string. `lookupService` / `lookupServiceDescriptor` are
  both optional — a decode context supplies whichever payload shape it
  expects, and the decoder throws if a service tag arrives whose shape
  has no matching callback.
- `index.ts` — public re-exports.

Depends on `jotai` (the abstract `Session` exposes jotai atoms).

### `@fxtr-ui/state-util` (`packages/fxtr-ui-state-util/`)

Framework-free path-projection helpers for session-bound atoms.
Depends on `@fxtr-ui/core` via `workspace:*` and on `jotai` +
`jotai-effect`. No React dependency — React-side memoization
(`useSubAtom`) lives in `@fxtr-ui/react`. Source modules in `src/`:

- `atom_with_default.ts` — `atomWithDefault<T>(parent, defaultValue,
  validityCheck?)` lifts a `FxtrSessionAtom<T | null>` to a
  `FxtrSessionAtom<T>` by substituting the default on null reads.
  Writes pass through; for function-form updates the inner updater
  sees `prev = parent ?? default` so the first write off a null
  underlying naturally seeds with defaults plus the consumer's
  layered changes. No phantom writes — the default is read-time
  fallback only. The optional `validityCheck(value)` guards against
  *stale* persisted state (e.g. an `ownStorage` slot written by an
  older renderer build): a non-null value failing the check is
  treated exactly like null, on both reads and the function-updater
  `prev`. The usual pattern is a `kind: "my-renderer/v1"` version
  tag on the stored shape. The React layer's
  `useSessionRootStateAtom` (`@fxtr-ui/react`) is the typical entry
  point; this helper exists for non-React or lower-level use.
- `sub_atom.ts` — `subAtom<T, K extends keyof T>(parent, key)`
  returns a child `FxtrSessionAtom<T[K]>` whose reads project into
  the parent and whose writes propagate as a structural update with
  `K` replaced. Function-form updaters threaded into the child
  receive the same `ctx` the parent's session would supply, so
  `ctx.allocateLoad(...)` inside a sub-atom updater binds the new
  slot to the parent's session at flush time. Branches on
  `Array.isArray(parent)` so array indices preserve array shape on
  write. `UserStateWriteOptions` (e.g. `onRollback`) ride along to
  the parent's write call. The returned atom is a fresh instance per
  call — React consumers should memoize via `useSubAtom` for stable
  identity.
- `load_result_atom.ts` — `makeLoadResultAtom<T>(slotAtom, stub)`
  + `LoadResult<T>` (`loading | filled | errored` discriminated
  union). Canonical "drive a load slot + observe its result"
  helper. Internally builds a `jotai-effect`-managed driver that
  allocates the load when the slot is null on first subscribe;
  the returned atom projects the slot's result into the
  three-state union. Used from inside a renderer's `useMemo`
  with the slot atom coming from `useDispatchSharedAtom` or
  carved out of an owning panel's `ownStorage` via `subAtom`.
  Replaces the older `useEntityLoad` React hook (which
  monopolized `useDispatchSharedAtom` and used `useEffect` for
  allocation). The helper used to live in `@fxtr-ui/reports`;
  the reports package re-exports it for back-compat.
- `invoke_result_atom.ts` — `makeInvokeResultAtom<T>(slotAtom, svc,
  method, args)`, the dual of `makeLoadResultAtom` for `allocateInvoke`:
  same `jotai-effect` driver shape, but issues the invoke (once) and
  projects its `OpResultStub` result into a `LoadResult<T>`. `T extends
  ResultValue`. Persisting the slot keeps a service returned from the
  invoke alive (its result event's ref stays pinned) — how a UI holds a
  channel subchannel reader open. Returns the shared `LoadResult<T>`.
- `stringify_for_debug.ts` — `stringifyForDebug(value, indent?)` and
  `debugReplacer`. `JSON.stringify` wrapper that flattens any object
  exposing a `debugSummary(): string` method into its summary string.
  The abstract stub classes in `@fxtr-ui/core` define this method
  with sensible defaults; concrete subclasses with richer identity
  override (e.g. `WorkstationOpResultStub` emits `<OpResultStub
  slot=N>`). The duck-typed dispatch means consumers can opt their
  own types in without registration. Side-steps the stub classes'
  circular session backrefs at the flatten boundary. For inspection
  logging / debug-UI displays — *not* a wire codec.
- `index.ts` — re-exports `subAtom`, `atomWithDefault`,
  `makeLoadResultAtom`, `makeInvokeResultAtom`, `LoadResult`,
  `stringifyForDebug`, `debugReplacer`.

Tests in `tests/sub_atom.test.ts` build a mock `FxtrSessionAtom`
backed by a primitive jotai atom + a hand-rolled write fn that
simulates the ctx-passing contract. Cases cover read projection,
static + function-form writes, array-index handling, nested
`subAtom(subAtom(...))`, and `onRollback` pass-through.

### `@fxtr-ui/connector-workstation` (`packages/fxtr-ui-connector-workstation/`)

WebSocket connector against the workstation's `/client` endpoint.
Depends on `@fxtr-ui/core` via `workspace:*` and on `jotai`.
Source modules in `src/`:

- `connection.ts` — wire-facing transport layer.
  `WorkstationServerConnection` owns the WebSocket, the
  request/response correlation map, the subscription routing map,
  the outbound queue (for sends before the socket is open), and the
  reconnect timer. Public API:
  - `connectionStatus: Atom<ConnectionStatus>` — read-only atom of
    `"connecting" | "open" | "reconnecting" | "closed"`.
  - `sendRequest(body)` — RPC-style; the connection allocates
    `request_id`. Returns the typed response via the `ResponseFor<op>`
    mapped type. Rejects with `ProtocolError`, `ConnectionLostError`
    (mid-request drop), or `ConnectionClosedError` (close() called
    while pending). Per-request typed failures
    (`MutationRejectedResponse`, `LoadEntityErrorResponse`) resolve
    in the response union, not reject.
  - `registerSubscription(spec, handler)` — streaming-style; returns
    a `SubscriptionHandle` synchronously. The handler gets every
    routed frame via `onFrame` and exactly one terminal callback via
    `onClose(reason, error?)`. Reasons: `"subscribe_failed"`,
    `"reset"` (socket dropped), `"unsubscribed"` (consumer
    `handle.close()`), `"connector_closed"`. `subscribe_failed` is
    the only reason that carries an `error`.
  - `close()` — rejects pending requests with
    `ConnectionClosedError`, fires `onClose("connector_closed")` on
    every active subscription, and closes the socket.
  - Socket reconnection is automatic (linear backoff,
    `[100, 250, 500, 1000, 2000, 3000]` ms). Subscription replay is
    *not* — the higher-level consumers (`WorkstationConnector`,
    `WorkstationSession`) decide whether to re-register on
    `onClose("reset")`.
- `connector.ts` — `WorkstationConnector`, the consumer-facing
  wrapper around a `WorkstationServerConnection`. Single
  constructor; options are a discriminated union:
  - `new WorkstationConnector({ url, store? })` — mints and owns its
    own connection (closes it on `connector.close()`).
  - `new WorkstationConnector({ connection, store? })` — borrows an
    externally-owned one (the path tests take to inject a mock
    `WebSocket` at the connection layer; the connection itself is
    left alive on `connector.close()`).

  The connector's `store` holds connector-level reactive state
  (`connectionStatus`, resource-list atoms) and is exposed publicly
  as `connector.store` so consumers can plug it into a
  `<JotaiProvider>` for connector-scope UI. By default, sessions
  also use this store; consumers who want per-panel isolation pass a
  fresh `createStore()` per session via the `{ store }` option.

  Exposes `connectionStatus`, the underlying `connection`
  (public so debug surfaces can open their own
  session-scoped subscriptions via `registerSubscription`),
  `url` (the WebSocket URL the underlying connection is using,
  surfaced for the shell's status header), four resource-list
  methods (`listRpcConnections`, `listServices`, `listEntities`,
  `listSessions`), each accepting `{ debug?: boolean }` for the
  workstation's debug projection, plus `createSession(metadata,
  { store? })` (async — sends `CreateSessionRequest`, awaits the new
  `session_id`, constructs a `WorkstationSession`) and
  `attachSession(sessionId, { store? })` (sync — always returns a
  fresh `WorkstationSession` instance per call, no caching). Both
  throw `ConnectorClosedError` if called on a closed connector;
  `createSession` re-checks the flag after the round-trip in case
  the connector closed mid-flight.

  Lists are connector-owned singletons cached per (resource, debug)
  pair; duplicate `listX()` calls return the same instance. Lists
  have no public `close()` — they're torn down only by
  `connector.close()`. Sessions are NOT tracked by the connector:
  each session self-manages via its own subscription handler.
  `close()` is idempotent. The connector also subscribes to its
  underlying connection's `connectionStatus`: if the connection
  reaches terminal `"closed"` out from under the connector, the
  connector self-closes so subsequent calls throw rather than
  minting stillborn instances.

  The connector owns the entity/service intern maps and exposes
  `entityRef<T>(shortId)` / `serviceRef<T>(shortId)` for
  programmatically minting refs (interned per connector — two calls
  with the same shortId return the same instance, and wire-decoded
  refs from any session collapse to the same instance). These are
  pure value constructors and don't throw on a closed connector.

  `loadEntityData<T>(stub): Promise<T>` exposes the workstation's
  out-of-band direct-load path (`LoadEntityRequest`). No session, no
  refcounts, no jotai — sends one wire request, runs result-mode
  `unescapeWire` on the response with the connector's `entityRef` /
  `serviceRef` lookups, resolves with the decoded value. Returned
  `EntityRefStub`s in the result are connector-interned but **not
  server-side tracked** — the backend uses
  `get_or_assign_entity_inactive`, so children short_ids are
  descriptors for display and may not be lookupable for further
  direct loads. Callers wanting to traverse children should use the
  session system (`ctx.allocateLoad`). Service refs cannot appear in
  entity payloads by system invariant; `serviceRef` is passed as
  defense-in-depth. Rejection cases: `LoadEntityError` (server-side
  typed failure carrying `shortId` + `serverMessage`),
  `WireEscapeError` (malformed payload), `ConnectionLostError` /
  `ConnectionClosedError` / `ProtocolError` (transport),
  `ConnectorClosedError` (connector already closed — rejection, not
  sync throw, since the method is async).
- `resource_list.ts` — `ReactiveList<T>` interface (`status:
  Atom<ReactiveListStatus>`, `records: Atom<readonly T[]>` — no
  public `close()`) plus the internal `ReactiveResourceList`
  implementation. Status lifecycle: `"loading"` → `"ready"` after
  first snapshot; `"stale"` on `onClose("reset")` (records
  preserved, re-registers immediately — the connection's outbound
  queue holds the resubscribe until the socket reopens); `"closed"`
  is terminal. Records map keyed by the resource's primary-key
  field (`RESOURCE_KEY_FIELD`); each publish emits a fresh array so
  jotai's referential equality notifies subscribers.
  `subscribe_failed` additionally `console.error`s the underlying
  protocol error so it isn't silently lost.
- `session.ts` — `WorkstationSession`, the full read+write view of
  one workstation session subscription. Implements the abstract
  `Session` from core. Receives its jotai store via
  `WorkstationSessionOptions.store` and exposes it as `readonly
  store: JotaiStore`; it holds all of the session's internal atoms
  (`committedAtom`, `localAtom`, slot-state atoms, `lifecycle`,
  `userState`, etc.). The React layer's `<FxtrSessionProvider>`
  plugs `session.store` into a `<JotaiProvider>` for the panel's
  scope. Sessions share the connector's store by default; consumers
  who want per-panel isolation pass a fresh `createStore()` via
  `{ store }`.

  **FSM** with four states (`connecting / connected / stale /
  closed`) over: register-on-construction, snapshot decode, live
  `state_update` events, reset-and-re-register (immediate, no
  `connectionStatus` watching — connection's outbound queue handles
  the wait), terminal `state_update.closed=true`.

  **Mutation reducer.** Two private envelope atoms back the
  reducer: `committedAtom` (last server state) and `localAtom`
  (user view, may have edits past the inflight). An `inflight`
  struct (non-atom) groups the bookkeeping for an outstanding
  mutation: envelope, `updateId`, `sendPromise`, accumulated
  rollback callbacks — all four fields move together (`null` when
  nothing is in flight). Public `userState` is a custom writable
  atom: reads project `extractUser(local)`, writes route through
  the reducer's flush microtask via jotai's `set` callback. Public
  `lifecycle` is a derived read-only view. Writes coalesce across a
  tick; the flush computes a JSON patch over `wirePart(committed)`
  vs `wirePart(local)` + Track/Untrack ops via
  `collectStubsInState`, allocates an `update_id`, and ships a
  `MutateSessionRequest`. Empty-diff short-circuit avoids no-op
  mutations.

  On commit: promote inflight's envelope to committed and bump
  `lastProcessedSeq` (asserted strictly greater). On reject
  (`mutation_rejected`, `ConnectionLostError`,
  `ConnectionClosedError`, `ProtocolError`): revert `localAtom` to
  `committedAtom`, fire all accumulated `onRollback` callbacks in
  registration order with `RollbackInfo` classified via
  `classifyReject` / `classifyTransportError`; `session_gone`
  additionally flips lifecycle to `closed`.

  `lastProcessedSeq` invariant: tracks the highest fully-processed
  seq, updated on state_updates, commits, and on dropped non-state
  events. Snapshots are *baselines* (set `lastProcessedSeq`
  unconditionally; re-snapshot after reset can legitimately equal
  current); delta events are *increments* (strict `>`, stale
  duplicates dropped) — handled in two separate methods
  (`applySnapshot`, `applyStateUpdateEvent`) sharing an
  `absorbState` helper.

  `detach()` is async (`Promise<void>`); blocks new writes and
  drains pending mutations (awaiting `inflight.sendPromise` +
  `pendingFlushPromise`) before unsubscribing.
  `terminate(finalState?, options?): Promise<void>` rides the
  reducer: synthetic `local.terminated = true` field is translated
  to a `CloseOp` op (stripped from the patch); commit resolves the
  promise and flips lifecycle to `closed`; rollback rejects with
  `MutationRolledBackError` carrying the `RollbackInfo`. Repeat
  calls to terminate / writes during terminate / writes during
  detach all reject through a single `assertWritable` gate.

  **Slot machinery.** Session state for op-result and callback
  slots:
  - `opResultStubs: Map<number, WorkstationOpResultStub>` —
    per-session slot stub interning (append-only on mint). Kept
    distinct from `callbackInboxStubs` — mixing kinds would force
    `allocateLoad`'s dedup lookup to widen past `OpResultStub<T>`.
  - `pendingSlotSpecs: Map<number, OpSpec>` — specs for slots the
    user has allocated; consumed by the flush walk to emit
    `LoadEntityOp` / `InvokeOp` / `CreateCallbackOp`. Wiped at end
    of flush and on reject; this enforces the contract that
    `allocate*`'s returned stub must be written to `userState` in
    the same tick.
  - `localSlotCounter: number` — monotonic, advanced via `+1` on
    every `allocate*` and bumped via `max()` on every
    `state_update.slot_counter` and
    `snapshot.last_state.slot_counter`.
  - `committedAckSeq: number` — mirror of server's
    `last_state_ack_seq`. Updated on snapshot, foreign state_update
    absorb, and our own commit.
  - `committedSlotDedupInfo: ReadonlyMap<number,
    CommittedSlotDedupInfo>` — unified `slot →
    {kind, shortId}` reverse index spanning all three `DedupKind`
    forward buckets in `committed.dedup`. The tagged value answers
    "which dedup namespace owns this slot?" with a single lookup;
    the flush walk reads `info.kind` to address the matching
    forward bucket. Single-match-per-slot invariant: a slot
    integer is minted from one monotonic counter shared across all
    op kinds, so at most one bucket ever holds it. Rebuilt by
    `reindexCommittedState` by iterating `DEDUP_KINDS` over the
    forward record. The forward buckets stay separate (entity /
    service / load shortIds inhabit distinct key spaces) — only
    the reverse is keyed by slot integer alone, which has a single
    answer per slot.
  - Private primitive atoms exposed as read-only views
    (`slotFillsAtom`, `committedSlotSetAtom`, `brokenSlotsAtom`,
    `committedInboxSlotSetAtom`, `inboxLogsAtom`,
    `inboxSubscriptionStateAtom`, etc.) that the
    stub classes' atoms read from.
  - `committedPreservedSeqsPrivateAtom` mirrors the server's
    `preserved_events` sidecar, re-derived on every committed state
    change (no-drift invariant: slots in committed × `slotFills` ×
    `committedAckSeq`).
  - A single dead-sentinel `WorkstationOpResultStub` with `slot = 0`
    is minted at construction. Substituted into local user state by
    the flush walk when it encounters cross-session stubs; resolved
    by `lookupOpResult` for incoming `["broken_op_result", 0]`
    tags. The inbox variant has its own `deadInboxSentinel`.

  Slot-allocation is **private** on `WorkstationSession` — callers
  reach it through the `FxtrSessionUpdateCtx` passed to
  function-form `userState` updaters. A constant-per-session
  `updateCtx: FxtrSessionUpdateCtx` field, built once in the
  constructor with arrow-fn wrappers that preserve `this`, delegates
  to the five private impls below (`allocateLoad`, `allocateInvoke`,
  `allocateCallback`, `allocateGetEntityMetadata`,
  `allocateGetServiceMetadata`). Static `set(userState, value)`
  writes bypass ctx entirely — only function-form updaters see it.

  `allocateLoad(entityRef)` allocates a slot or returns an existing
  stub via two-stage dedup: scan `pendingSlotSpecs` for a same-tick
  prior allocate, then read `local.loadDedup[shortId]` (wire-visible
  — covers our past commits, foreign mutators' commits, and the
  snapshot we got on `attachSession`). On miss, increments
  `localSlotCounter`, mints + interns a stub, records the load spec.
  Throws unless `lifecycle === "connected"`.

  `allocateInvoke(svc, method, args, kwargs?)` mints a fresh invoke
  slot — no dedup, every call produces a new slot. Args and kwargs
  are validated + wire-encoded **at allocate time** via
  `escapeInvokeArgs` (rejects `OpResultStub` / `CallbackInboxStub`
  with path-pointed errors at runtime; the type system rejects them
  upstream via the `InvokeArgValue` generic constraint).
  `CallbackHandleStub` is allowed and encodes to `["callback",
  slot]`; the session's `encodeCallbackHandle` opt also enforces
  cross-session smuggling rules (throws if `handle.session !==
  this`). The encoded form lands in an `InvokeOpSpec` in
  `pendingSlotSpecs`; the flush walk emits `InvokeOp` with the
  captured args verbatim. No auto-tracking of refs that appear only
  in args — the workstation pins arg refs for the duration of the
  in-flight invoke independently. A rejection during arg validation
  does not advance `localSlotCounter`. Signature uses the
  protocol-aware type helpers from `@fxtr-ui/core` (`MethodNames` /
  `MethodArgs` / `MethodKwargs` / `MethodReturns`): when `svc`
  carries a protocol type, the method name, args, kwargs, and
  return type are checked / inferred from the protocol; when `svc`
  is `ServiceRefStub<unknown>`, everything falls back to loose
  types. The *target* may also be a `CallbackHandleStub` from this
  session (`inbox.handle`) — a callback-target invoke: validated
  for cross-session smuggling before any slot is burned, recorded
  as a `CallbackInvokeOpSpec`, and emitted by the flush as a
  `CallbackInvokeOp(slot, callback_slot, ...)`. The handle's slot
  integer is known even same-tick, so allocate-inbox +
  invoke-on-it ride one flush (the slot-sorted op emission puts
  the `CreateCallbackOp` first). Handle targets use loose types.

  `allocateCallback<R>()` mints a fresh callback inbox slot,
  constructs a `WorkstationCallbackInboxStub` at offset=0, and
  records a `CallbackOpSpec` in `pendingSlotSpecs`.

  All three cacheable allocators (`allocateLoad`,
  `allocateGetEntityMetadata`, `allocateGetServiceMetadata`)
  share one private `allocateDeduped<T>(operation, kind, shortId)`
  helper that captures the lifecycle gate + two-stage dedup
  (same-tick `pendingSlotSpecs` scan → wire-visible
  `local.dedup[kind][shortId]` lookup → cold mint). The public
  methods are thin wrappers picking the right `DedupKind` and
  shortId source. The three buckets share the wire-visible
  `dedup` field as a `Record<DedupKind, Map<shortId, slot>>`
  (separate forward namespaces; see `DedupForwards`) but
  participate in a unified slot pool (one monotonic
  `localSlotCounter`). A single entity yields *different* slots
  for `allocateLoad` vs. `allocateGetEntityMetadata` since the
  result shapes differ. Metadata methods return
  `OpResultStub<EntityMetadata>` / `OpResultStub<ServiceMetadata>`
  (typed shapes from `@fxtr-ui/core`). Result events
  (`entity_metadata_result` / `service_metadata_result`) decode
  their `metadata` dict through `unescapeWire` (the server emits
  ref fields in list-escape-tagged form) so the decoded value
  carries real `EntityRefStub` instances at ref leaves.

  `lookupOpResult(slot, broken)` returns the session's
  dead-sentinel for `slot === 0`, otherwise interns + returns the
  slot's stub; for `broken === true` (slot > 0), also adds the slot
  to `brokenSlots`. A tripwire throws if `slot > localSlotCounter`
  — counter advances run *before* state decode in
  `applySnapshot` / `applyStateUpdateEvent` so the tripwire sees
  correct values. The same tripwire fires on fill events.

  `flushWalk(local)` is the pre-flush single-pass over local user
  state. It:
  - Substitutes cross-session op-result and inbox stubs with the
    per-session dead-sentinels (writing the substituted user state
    back to the local atom in-flush). The encoder emits
    `["broken_op_result", 0]` / `["broken_inbox", 0]` for these
    sentinels — frontend conventions; the server stores wire state
    opaquely and doesn't interpret the tags.
  - Classifies each op-result slot as `legitimate` /
    `new-with-spec` / `broken` (our-but-broken — slot in our
    namespace but no spec and not in committed, or
    `slot > localSlotCounter`).
  - Reconciles the unified wire-visible `dedup` record
    (`Record<DedupKind, Map<shortId, slot>>`) from the walked
    tree. For each slot stub: an `isDedupSpec` type-predicate on
    `pendingSlotSpecs[slot]` writes the spec's `shortId → slot`
    into `forwards[spec.kind]`; for slots already in committed,
    `committedSlotDedupInfo[slot]` carries `{kind, shortId}` and
    `forwards[info.kind].set(info.shortId, slot)` is the single
    write. Adding a future dedup kind is a one-line addition to
    `DedupKind` plus one line in `EMPTY_DEDUP_FORWARDS` — the
    walk loops, no switch arms to update.
  - Collects `slotsInLocal` for the preserve diff, and
    `inboxSlotsInLocal` (per-slot min-offset across all inbox stubs
    for that slot) for the create/close diff and the
    preserve-events calc. Inbox stubs with a `{kind: "callback"}`
    pending spec are added to the shared `newWithSpecSlots` set so
    `computeOps` emits `CreateCallbackOp` via its kind-dispatch.

  `reindexCommittedState()` runs after every committed state change
  (commit / state_update absorb / snapshot apply). It walks
  committed user state, rebuilds the slot-set atoms, re-derives
  `committedPreservedSeqs` per the no-drift invariant (covering
  both load/invoke/metadata fill seqs and per-slot inbox event seqs
  at-or-past `min_offset` filtered by `seq <= committedAckSeq`),
  trims `inboxLogs` (advances `firstIndex` and slices off front
  events when the committed `min_offset` advances; drops the entire
  entry when no inbox stub for the slot remains in committed),
  purges `slotFills` entries the server has dropped from retention,
  and rebuilds the three slot→shortId inverse indexes
  (`committedSlotShortIds`, `committedEntityMetadataSlotShortIds`,
  `committedServiceMetadataSlotShortIds`).

  `applyEvent` routes the eight `OpResultStub`-backed fill kinds —
  load / invoke / entity-metadata / service-metadata result + error
  variants — through `handleFillEvent` → `slotFills`,
  `CallbackEvent` through `handleCallbackEvent` → appends to the
  per-slot `inboxLogs[slot].events`, and `CallbackSubscriptionEvent`
  through `handleCallbackSubscriptionEvent` → reduces a per-slot
  `inboxSubscriptionStates` map (latest-wins: `{state, reason}`;
  slot absent → "idle"), which `WorkstationCallbackInboxStub.connectionState`
  / `subscriptionBreakReason` read. `handleFillEvent` dispatches
  via `decodeFillEvent`: load + invoke result events use
  `decodeResultPayload` (which reads `event.payload`), metadata
  result events use `decodeMetadataPayload` (which reads
  `event.metadata`, same `unescapeWire` pipeline), and any `*_error`
  variant synthesizes an errored `FillRecord` carrying
  `event.message`. `handleCallbackEvent` uses a result-mode
  `unescapeWire` (callback payloads are `ResultValue`, no
  client-only stubs). Decode failures for fills synthesize errored
  `FillRecord`s; decode failures for callback payloads log + skip
  the offending event. `applySnapshot` calls
  `rebuildSlotFillsFromSnapshot` BEFORE `absorbState` (the reindex
  reads `slotFills`) and `rebuildInboxLogsFromSnapshot` AFTER
  `absorbState` (needs the new committed user state to derive
  per-slot `firstIndex = min(offset)`) but BEFORE
  `reindexCommittedState` (which reads `inboxLogs` for the
  preserve-set re-derive). Events are sorted by `seq` per slot to
  guarantee delivery order matches the workstation's event log.
  `rebuildInboxSubscriptionStatesFromSnapshot` likewise re-derives the
  per-slot connection state from the snapshot's
  `CallbackSubscriptionEvent`s (latest-wins). A transition acked past
  the retention horizon is absent from a cold snapshot — accepted (same
  characteristic as callback messages).

  `flush()` runs the walk, writes back substituted local, flags
  broken slots into `brokenSlots`, reconciles the three dedup maps
  (`loadDedup`, `entityMetadataDedup`, `serviceMetadataDedup`) via
  the shared `mapEqual` helper, computes the JSON patch (with
  `encodeOpResult` consulting `brokenSlots` to emit
  `["broken_op_result", slot]` for broken slots), emits
  `LoadEntityOp` / `InvokeOp` / `CreateCallbackOp` /
  `GetEntityMetadataOp` / `GetServiceMetadataOp` for
  `new-with-spec` slots (dispatching on
  `pendingSlotSpecs[slot].kind`), emits `CloseCallbackOp` for slots
  in `committedInboxSlotSet` no longer in `inboxSlotsInLocal`, and
  emits `PreserveEventsOp` / `UnpreserveEventsOp` from the
  desired-vs-mirror diff. `pendingSlotSpecs` wiped at end of flush;
  the reject path also wipes it. Any `validation_failed` reject
  reason triggers a loud `console.error` — covers multi-mutator
  preserve drift, unknown short_ids, non-monotonic slots, etc.; the
  reducer can't recover programmatically so we surface uniformly.

  **Slot-order emission**: `computeOps` sorts
  `newWithSpecSlots` by slot number before iterating the spec
  buckets, so the resulting wire op sequence is monotonic in
  slot regardless of the JSON-tree order the walk encountered
  the stubs in. Necessary when two unrelated code paths write
  to userState in the same flush (e.g. the dynamic dispatcher's
  reload-subscribe writes land in low slots at a tree position
  later than entries' load writes); without the sort, ops can
  emerge out of slot order and the server's per-mutation
  monotonicity validator rejects.

  Entity / service stub interning lives on the connector — the
  session takes `lookupEntity` / `lookupService` callbacks via its
  options.
- `stubs.ts` — concrete connector-side stub classes.
  `WorkstationEntityRefStub` / `WorkstationServiceRefStub` are thin
  subclasses (just a `shortId` field); interning lives on
  `WorkstationConnector` because pure refs have no per-session
  state and sharing across sessions gives instance equality
  regardless of origin (wire-decoded inside a session vs.
  programmatically minted).

  `WorkstationOpResultStub<T>` carries `slot: number` and `session`
  (a `WorkstationStubSession` backref — one shared interface that
  `WorkstationSession` implements so both stub classes avoid a
  circular import on the session class), plus the three atoms from
  the abstract `OpResultStub` surface. The stub is stateless past
  its slot integer; `result` reads `session.brokenSlotsAtom`
  (taking precedence — errored) then `session.slotFillsAtom`
  (pending → filled / errored fill); `committed` reads
  `session.committedSlotSetAtom`; `done` is derived. `slot === 0`
  is the dead-sentinel: its atoms are hardwired-errored regardless
  of session state. The `FillRecord` type
  (`filled | errored` with `seq` and a `ResultValue` payload) lives
  here.

  `WorkstationCallbackInboxStub<R>` carries `(slot, offset,
  session)`. `events` reads `session.inboxLogsAtom.get(slot)` and
  slices from `offset - log.firstIndex` onward (throws on
  cursor-behind-firstIndex, an invariant violation). `state` is
  `"open"` if `session.committedInboxSlotSetAtom.has(slot)`,
  `"opening"` otherwise (no server-driven `"closed"` signal today).
  `committed` reads the same. `connectionState` /
  `subscriptionBreakReason` read `session.inboxSubscriptionStateAtom.get(slot)`
  (absent → `"idle"` / `null`). `handle` lazily mints a paired
  `WorkstationCallbackHandleStub<R>`. `sliced(n)` returns a fresh
  inbox at `offset+n` — the sole cursor primitive. `slot === 0` is
  the inbox dead-sentinel.

  `WorkstationCallbackHandleStub<R>` is slot-only with a session
  backref for cross-session smuggling checks at encode time. The
  `InboxLog` / `InboxEntry` / `InboxSubscriptionState` types live here
  too.
- The list-escape **wire codec** (`escapeWire` / `unescapeWire` /
  `escapeInvokeArgs` / `WireValue` / the tag constants) used to live
  here; it now lives in `@fxtr-ui/core` (shared with other connectors —
  it operates on the abstract stubs through caller-supplied callbacks,
  so nothing connector-specific leaks in). `index.ts` re-exports it from
  core for backward compatibility. See the core section for the full
  description. `escapeWire`'s object branch silently skips
  explicit-`undefined` field values (matches `JSON.stringify`);
  `collectStubsInState` (in `state.ts`) walks with the same convention.
- `protocol.ts` — wire protocol type unions mirroring
  `fxtr_workstation/client_protocol.py`: `ClientRequest`,
  `ServerFrame`, `MutationOp`, `SessionEvent`, per-resource record
  shapes, the `MutationRejectReason` literal type, plus
  `RESOURCE_KEY_FIELD` for snapshot-delta merging. Wire-encoded
  fields (`StateUpdateEvent.state`, the various `*Event.payload`
  fields, `InvokeOp.args` / `InvokeOp.kwargs`,
  `LoadEntityResponse.payload`, `JsonPatchWireOp.value`) are typed
  as `WireValue` directly — the server's session reducer guarantees
  the shape, so consumers can feed them straight into `unescapeWire`
  without a cast. `metadata` fields stay broader
  (`Record<string, unknown>`) since they're plain JSON, not
  wire-encoded.
- `state.ts` — structural state walks and the `JsonPatchOp` type
  (RFC 6902, add/remove/replace subset).
  `collectStubsInState(value)` returns `{entityShortIds,
  serviceShortIds, opResultSlots, inboxSlotOffsets}`.
  `opResultSlots` surfaces slot integers from
  `WorkstationOpResultStub` leaves for preserve-events derivation
  and the `committedSlotSet` index; the dead-sentinel
  (`slot === 0`) is excluded. `inboxSlotOffsets` surfaces the
  per-slot minimum offset across all `WorkstationCallbackInboxStub`
  leaves — used by `reindexCommittedState` and the callback
  preserve-events calc.
  `diffStateToPatch(prev, next, opts)` emits a JSON Patch with
  wire-encoded `value` fields. Stub equality: entity / service refs
  by `shortId`; `WorkstationOpResultStub` by `slot` integer;
  `WorkstationCallbackInboxStub` by `(slot, offset)` (cursor
  advancements diff as replace patches; the wire form `["inbox",
  slot, offset]` carries both); foreign-subclass stubs fall back to
  reference identity.
- `index.ts` — public re-exports.

Tests in `tests/`:

- `_mock_websocket.ts` — `MockWebSocket` spec-compatible test
  helper. Records sent messages, exposes `simulateOpen` /
  `simulateMessage` / `simulateClose` for driving lifecycle events
  synchronously, and registers every constructed instance in a
  module-level `createdSockets` array so tests can grab the
  "current" socket without weaving a factory through every test.
  Injected via `WorkstationServerConnection`'s `webSocketCtor`
  option.
- `escape.test.ts` — round-trip and error-path coverage for the
  wire codec: primitives, containers, bytes, entity / service refs,
  callback tag, `op_result` / `broken_op_result` encoding, decode
  dedup via `lookupOpResult` interning, rejection of `["op_result",
  0]` and of negative / non-integer slot values, plus
  `WorkstationOpResultStub` atom-behavior tests.
- `state.test.ts` — state-walk coverage: `collectStubsInState`'s
  slot collection (dedup, dead-sentinel exclusion, mixed with
  entity / service refs) and `diffStateToPatch`'s slot equality
  (same slot ⇒ no patch, different slot ⇒ replace, add / remove
  with correct `op_result` / `broken_op_result` encoding).
- `slot_reducer.test.ts` — load-lifecycle smoke + edge-case suites
  for the slot reducer: allocate + write emits `LoadEntityOp`; fill
  event transitions `stub.result` from pending → filled and flips
  `committed`/`done`; the next flush after a fill arrives emits
  `PreserveEventsOp`; removing the slot emits `UnpreserveEventsOp`
  and `slotFills` purges on commit; the fill-event slot-counter
  tripwire fires. Edge cases cover rollback flows, reconnect
  rebuild from snapshot, dedup mid-flight, foreign state_update
  reconciliation, cross-session-stub substitution + wire-decode of
  `["broken_op_result", slot>0]`, malformed-payload decode-error
  fallback, and the `validation_failed` reject canary.
- `invoke_reducer.test.ts` — invoke-slot reducer.
  `ctx.allocateInvoke` emits `InvokeOp` with the expected slot /
  service / method / args / kwargs; omitted kwargs → empty dict;
  allocate-and-discard inside an updater never emits an `InvokeOp`;
  no dedup (two `ctx.allocateInvoke` calls mint distinct slots);
  arg / kwarg encoding round-trips through `escapeInvokeArgs`;
  forbidden stub kinds reject at allocate time with no slot burned;
  Track ops fire for refs in state but NOT for refs that appear only
  in args. Fill-event coverage: `invoke_result` / `invoke_error`
  flip `stub.result`; result payloads with refs decode through
  `unescapeWire`; malformed payloads synthesize errored fills;
  fill-event slot-counter tripwire fires for out-of-range slots;
  `applySnapshot` rebuilds `slotFills`. Typed-protocols cases cover
  return-type inference, untyped fallback, and `@ts-expect-error`
  rejection of unknown method names / mismatched args / mismatched
  kwargs.
- `metadata_reducer.test.ts` — metadata-slot reducer
  (`allocateGetEntityMetadata` / `allocateGetServiceMetadata`).
  Allocate + write emits `GetEntityMetadataOp` /
  `GetServiceMetadataOp`; same-tick and cross-tick (wire-visible
  `entityMetadataDedup` / `serviceMetadataDedup`) dedup; distinct
  slot pool from `allocateLoad` (same entity → different slots for
  load vs. metadata); optimistic-remove + re-add reuses the
  pending slot; lifecycle gate. Fill events flip `stub.result` to
  filled with decoded `EntityMetadata` / `ServiceMetadata` values
  (ref fields → `EntityRefStub` instances via `unescapeWire`);
  error events flip to errored; malformed payloads synthesize
  errored fills; fill-event slot-counter tripwire fires for
  out-of-range slots. Snapshot rebuild surfaces preserved
  metadata events into `slotFills`. Attach-session bootstrap and
  foreign-mutator dedup hits via the wire-visible maps.
- `callback_reducer.test.ts` — callback-slot reducer.
  `ctx.allocateCallback` emits `CreateCallbackOp`; cursor advance
  via `sliced` on a committed slot emits a patch but neither create
  nor close; removing the only inbox stub for a slot emits
  `CloseCallbackOp`; cross-session inbox-stub substitution emits
  `broken_inbox` in the wire patch and writes the
  dead-inbox-sentinel back to local state. `handleCallbackEvent`
  appends payloads to `inbox.events` (decoded through result-mode
  `unescapeWire`); entity / service refs in payloads decode through
  the connector's lookups; malformed payloads log + skip.
  `PreserveEventsOp` fires for callback seqs `<= ack_seq` when the
  cursor is at offset=0; `UnpreserveEventsOp` fires for the seqs
  `sliced` advanced past. Callback-target invokes
  (`ctx.allocateInvoke(inbox.handle, ...)`): same-tick
  create+invoke emits `CreateCallbackOp` then `CallbackInvokeOp`
  in slot order; cross-tick targeting a committed inbox emits a
  bare `CallbackInvokeOp`; a cross-session handle target rejects
  at allocate time without burning a slot. `reindex` trims front events on cursor
  advance and drops the `inboxLogs` entry entirely when the inbox
  stub is removed from committed state. Snapshot rebuild populates
  `inboxLogs` from preserved + subsequent `CallbackEvent`s.
- `connection.test.ts` — exercises `WorkstationServerConnection`'s
  lifecycle (open / drop / reconnect / close), `sendRequest`
  correlation + every error class, `registerSubscription`'s four
  close reasons, handler exception isolation, the reconnect backoff
  schedule (vitest fake timers), and the warning-on-
  unexpected-condition behavior.
- `connector.test.ts` — exercises `WorkstationConnector` +
  `ReactiveList`: snapshot population, delta merging keyed by the
  resource's primary-key field, list caching by (resource, debug),
  `subscribe_failed` terminal behavior, socket-drop reset →
  re-register → resnapshot, borrowed-connection close vs
  owned-connection close, connector self-close when the underlying
  connection closes out from under it, post-close rejection, close
  idempotence.
- `session.test.ts` — exercises `WorkstationSession` and the
  connector's session-creation surface: `createSession` round-trip
  + subscribe + snapshot, `attachSession` sync construction +
  two-with-same-id-are-independent, post-close surface rejection,
  snapshot decode (fresh empty-state, populated state, live
  `state_update` updates, decode-error path), stub interning across
  snapshot + delta, FSM transitions (connected → stale → connected
  with userState retained, connecting → connecting on early reset,
  stale → stale on chained drops), terminal paths, runtime
  rejection of writes to the lifecycle atom, the ctx-style
  `allocateLoad` surface, and that `ctx.allocateCallback` mints a
  `CallbackInboxStub`. (Detailed `allocateLoad` reducer coverage
  lives in `slot_reducer.test.ts`; `allocateInvoke` coverage lives
  in `invoke_reducer.test.ts`.)
- `mutation.test.ts` — writable `userState` flow (write → flush →
  `MutateSessionRequest`), optimistic read, microtask coalescing of
  multiple writes, updater form using prev, pending-while-inflight
  queuing, reject + forward-order rollback with simplified
  `RollbackInfo`, `session_gone` flipping lifecycle to closed,
  connection-lost rollback (classified retryable), foreign update
  absorb (snap when local-clean, leave alone when local-dirty +
  subsequent stale_ack rebase), Track/Untrack op derivation,
  cancel-out diff short-circuit, connector-level stub interning,
  `terminate(): Promise<void>` shipping a `CloseOp`,
  terminate-reject rejecting the promise with
  `MutationRolledBackError` and recovering for future writes,
  async `detach()` drain semantics, and stale-event handling.
- `_demo_workstation.py` — Python script that boots a
  `WorkstationServer`, registers one entity (`e-test`) and one
  service (`s-test`) via peer RPC, prints `READY <url>` to stdout,
  parks until SIGTERM. The service exposes four methods exercised
  by the integration tests: `echo(value)`, `combine(value, *,
  name="default")` (positional + keyword args), `fail(message)`
  (raises `RuntimeError`), and `notify_n(cb, n, prefix)` (calls
  `cb.notify(f"{prefix}-{i}")` `n` times in sequence, resolving the
  callback ref against the active call scope and awaiting each
  notify inline so all `CallbackEvent`s fire before the invoke
  result). Used by the live integration tests; invoked via
  `uv run`.
- `_integration_harness.ts` — `startDemoWorkstation()` spawns the
  Python script as a child process, reads `READY <url>`, returns a
  `{ clientUrl, close() }` handle. Graceful SIGTERM with a 5-second
  SIGKILL fallback (the fallback uses a closure-tracked `exited`
  flag since `child.killed` flips to `true` immediately after any
  signal); 15-second READY timeout.
- `integration.test.ts` — live scenarios against the demo
  workstation. Covers connect+snapshot, single-session mutation,
  Track/Untrack via `subscribe_session_debug`, microtask coalescing,
  foreign-update propagation, race + reject + rollback + retry
  (deterministic via same-connector WebSocket FIFO), terminate;
  allocate + load lifecycle, load dedup, preserve / unpreserve via
  `session_debug`, preserved fill survives attach-to-same-session,
  invalid short_id surfaces as `permanent` rollback; invoke happy
  path (`echo(42)`), invoke with refs nested in args, invoke with
  kwargs (`combine(value, name="alice")`), invoke with default
  kwarg, invoke error path (`fail(message)` → `invoke_error` →
  errored stub); callback inbox happy path
  (`notify_n(inbox.handle, 3, "msg")` → 3 `CallbackEvent`s arrive
  in order → `inbox.events` emits the payloads), cursor advance
  (`sliced(2)` triggers `UnpreserveEventsOp` that drops exactly
  the prefix seqs from `session_debug.preserved_events` —
  asserted via set containment against the seqs read from local
  `inboxLogs`, not magic counts), `CloseCallbackOp` on stub
  removal (server-side `open_callbacks` shrinks), and preserved
  callback events surviving attach to the same `session_id`
  (snapshot rebuild populates `inboxLogs` so the inbox stub in
  committed state emits the same payloads); entity / service
  metadata happy paths (decoded `EntityMetadata` carries
  `EntityRefStub`-valued ref fields, `ServiceMetadata` exposes
  `type_id_string`), metadata dedup, metadata-vs-load distinct
  slot pools, metadata fill survives attach-to-same-session, and
  invalid short_id surfaces as `permanent` rollback. Requires
  `uv` and the workstation Python package to be installed
  locally.

`protocol` and `state` are pure types / a structural diff helper
best exercised end-to-end.

### `@fxtr-ui/connector-http` (`packages/fxtr-ui-connector-http/`)

The stateless, CDN-friendly connector: it implements the `@fxtr-ui/core`
`Connector` + `Session` surfaces over plain HTTP/ws request→response
endpoints (no server-side session state — all session state is
client-local). Wire identity is self-describing: entities travel as
their full `id_string`, services as descriptor objects; the connector
mints client-side short IDs for the UI/persistence layers and keeps a
table to map back. Uses the shared codec's **resolved mode** on the
server wire and its **shortId mode** for persisted state. All
client-local session state (userState, fills, inbox logs, slot/seq
counters, the shortId table) persists through a standard Web `Storage`
handle so a reload rehydrates via `attachSession`. Built as part 7a
(`plans/phase0/part7a_stateless_ws_server/`).

An end-to-end demo of this connector + the `@fxtr-ui/panel-viewer` shell
over a Python stateless backend (entities, GET/POST services, WS dynamic
dispatch with hot reload, and a WS stopwatch subscription) lives in
[`examples/fxtr_http_demo/`](../../../../examples/fxtr_http_demo/) — 7a
Part 5. It's the cross-cutting integration check for the whole stack.

- `descriptor.ts` — the `ServiceDescriptor` wire type (the TS twin of
  the Python `service_codec.ServiceDescriptor`: `{kind: http | ws |
  transient, url?, method?, name?, id?, service_type_id?}`) plus
  `descriptorFromWire` / `descriptorToWire` and
  `canonicalizeDescriptor(desc, baseUrl)`. Canonicalization (for shortId
  identity) resolves the URL to absolute, **drops** the non-identity
  `service_type_id` annotation, and sorts keys — so two descriptors
  differing only in `service_type_id` (or relative-vs-absolute URL)
  share one identity.
- `shortid_table.ts` — `ShortIdTable`, the persisted shortId ↔
  stable-identifier map. Allocation **mirrors the workstation's
  `short_ids.py`** so the UI sees workstation-shaped IDs:
  `shortIdForEntity(idString)` mints a *prefix of the entity's own
  `id_string`* (prefixable `d`/`c`/`n` tags → first 7 chars, extended
  one char at a time on collision; non-prefixable `.`/`!`/`p`/`u` tags →
  the full `id_string`), so the shortId stays consistent with the
  content-addressed identity and carries the real tag char;
  `shortIdForService(descriptor)` mints an ascending `s{N}` per canonical
  descriptor (the high-water mark is persisted + restored so post-reload
  allocations keep ascending). `lookup` /
  `classifyShortId` are answered **authoritatively from the table**
  (never by parsing the shortId); `classifyShortId` backs the
  `Connector` method. `toJSON()` / the constructor's `persisted` arg
  round-trip it through the connector's `Storage` handle (it shares one
  backend with the sessions + layout, design_scratch §7). Transient
  descriptors are interned too (they appear in persisted state and
  rehydrate born-broken).
- `entity_decode.ts` — `cborToResultValue(cbor, opts)`: a single local
  walk turning a decoded `/data` `FxtrCbor` tree into the same
  `ResultValue` shape the workstation produces (the workstation's
  `load_cbor → escape → unescapeWire`, minus the JSON round trip). Every
  `RawWeakFxtrID` / `RawStrongFxtrID` / `RawFxtrEntityMarkerID` leaf
  becomes an interned `EntityRefStub` (via `opts.entityRefForIdString`
  on the leaf's `innerId.idString()`); `Uint8Array` and primitives pass
  through; lists/dicts recurse; `$fxtr.ctx` / `$fxtr.type` survive as
  dict entries. (Depends on `@fxtr/core` for the `Raw*FxtrID` leaf
  classes + `FxtrCbor` — the first `frontend_lib → v2/ts` dependency;
  host-side only, kept out of renderer bundles.)
- `metadata.ts` — local metadata resolution. `buildServiceMetadata`
  reads `type_id_string` off the descriptor's `service_type_id`
  (throwing `MissingServiceTypeError` if absent, → errored slot). Entity
  metadata is built with the connector (step F), reading the `/data`
  precomp closure off the `EntityFetcher` (design/03).
- `entity_data.ts` — `EntityFetcher`, the extracted fetch+decode+precomp
  core (design/03 weighed reusing `HttpReadableArena` and chose to
  extract — the arena's `ReadableArena`/scope surface isn't needed, and
  extracting drops the `@fxtr/http-client` dep). `load(idString)` GETs
  `/data/{id}`, decodes the CBOR `{bytes, precomp}` envelope, accumulates
  the precomp edges (`unwraps_to` / `ctx_node` / `backstep`, for entity
  metadata), and returns the entity's `FxtrCbor` tree (→ `cborToResultValue`).
  `EntityLoadError` on non-2xx. Payloads are not cached (content-addressed).
- `ws_pool.ts` — `WsPool`, the connect-on-demand ws transport: one
  connection per ws url, each with a monotonic **generation**, a
  pending-invoke map, and read/notify dispatch. `invoke(url, req, {
  expectedGeneration? })` opens the socket on demand, sends an `invoke`
  frame, and resolves/rejects on the matching `result`/`error` frame;
  `notify` frames route to `onNotify(inbox, payload)`. **No background
  reconnect**: a drop rejects in-flight invokes, bumps the generation,
  and fires `onDrop(url, deadGeneration)` (the session uses it to break
  transient stubs + flip inbox `connectionState`); the next invoke opens
  a fresh socket. Transient services are generation-bound —
  `expectedGeneration` rejects a stale transient ("transient service
  broken") before reconnecting. The `WebSocket` is injectable
  (`webSocketCtor`) for tests. `close()` rejects all in-flight invokes
  **and closes the underlying sockets** (so the server sees a disconnect
  — otherwise a parked server read loop keeps the connection, and any
  graceful server shutdown, alive). The session/connector layers wire
  `onNotify` / `onDrop` to inbox routing + `connectionState` synthesis.
- `stubs.ts` — the concrete stub classes (forked near-verbatim from the
  workstation connector; the slot/fill/inbox machinery is identical):
  `HttpEntityRefStub` / `HttpServiceRefStub` (interned per connector by
  shortId; the service stub's `state` derives from a connector-level
  broken-services set, into which the connector marks transient stubs
  whose ws generation died), and the per-session `HttpOpResultStub` /
  `HttpCallbackInboxStub` / `HttpCallbackHandleStub` (read through the
  `HttpStubSession` interface — the session's reactive read-surface).
  Plus `FillRecord` / `InboxLog` / `InboxEntry` / `InboxSubscriptionState`.
  The only HTTP-specific difference from the workstation is that the
  upstream atoms are filled locally (no server protocol) — a session
  concern, not the stubs'.
- `resource_list.ts` — `DiscoveryList<T>` + the `ReactiveList<T>`
  interface (`{status, records}` atoms). One-shot `GET /discovery`
  (`loading → ready`, or `loading → error` if the fetch fails — records
  keep their last value, a later `refresh()` recovers; re-fetched via the
  connector's `refresh()`; no live deltas, no `stale`).
  `HttpConnector`-specific — each connector ships its own roots panel
  over its own lists.
- `persistence.ts` — the persisted shapes + key schema for the storage
  layer. `SerializedSession` (version, metadata, createdAt, slot/seq
  counters, shortId-mode-escaped `userState`, dedup forward maps, invoke
  `slotFills`, `inboxes`), `SerializedFill`, `SerializedInbox`, and
  `SessionRegistryEntry`. Key schema (namespaced by a `baseUrl` hash):
  `fxtrhttp:<ns>:table` (shortId table), `:sessions` (registry blob),
  `:session:<uuid>` (one session). The connector owns the `Storage`
  I/O; the session knows how to (de)serialize itself.
- `connector.ts` — `HttpConnector`, the transport + identity core. Owns
  the jotai store, the persisted shortId table (through the `Storage`
  handle, debounced), the session registry + per-session blobs, the
  entity/service intern maps, the `EntityFetcher`, the `WsPool`, and the
  discovery lists. `entityRef`/`serviceRef` intern by shortId;
  `internEntityByIdString` / `internServiceByDescriptor` allocate
  shortIds (the latter records a transient's `(url, generation)`
  binding); `classifyShortId`, `loadEntityData` (fetcher →
  `cborToResultValue`), `getEntityMetadata` (precomp closure; entity ids
  are bare on this wire, so no provenance layering) / `getServiceMetadata`
  (`buildServiceMetadata`), `connectionStatus` (tracks **discovery
  reachability**: `connecting` → `open` on the first successful
  `/discovery`, `reconnecting` if a later `refresh()` fails (no
  auto-retry), `closed` on `close()`; ws-pool drops are deliberately *not*
  folded in — they self-heal on the next invoke and surface through the
  per-service broken status), `close` (flushes live sessions + table
  synchronously). Session-facing hooks: `wsPool`,
  `registerInbox`/`unregisterInbox` (notify routing by inbox uuid),
  `onConnectionDrop`, `markServiceBroken` (a ws drop marks the dead
  generation's transients broken via `handleDrop`), `persistSession` /
  `deleteSession` (storage writes the session debounces / terminate
  triggers). `createSession` (mints a UUID, registers + persists an
  initial blob) / `attachSession` (interns one live instance per
  sessionId, Q-B; rehydrates from the blob, but an id it **can't
  restore** — never persisted, unparseable, or a bad-version/corrupt
  blob — yields a terminal `unavailable` session, not a silently-created
  one, mirroring the workstation's attach-to-missing) +
  `removeSession` (detach: drop the live instance, keep storage) +
  `listPersistedSessions` + `implements Connector` land here.
- `session.ts` — `HttpSession`, the client-local session reducer
  (design/05; a clean reimplementation of the abstract `Session`, not a
  fork of the workstation session). Two-atom optimistic model
  (`local`/`committed`) with a microtask-coalesced **flush**:
  function-form `userState` writes run with a `FxtrSessionUpdateCtx`
  (the five `allocate*`), allocate mints a slot + records an `OpSpec` in
  `pendingSlotSpecs`; the flush runs a **flush-walk** (cross-session stub
  substitution → dead sentinels; slot classification via specs;
  load/metadata dedup forward maps), commits the local atom (no inflight
  envelope — there is no wire mutation), fires the newly-allocated ops,
  and GCs via `reindexCommittedState` (drops `slotFills`/`inboxLogs` for
  slots no longer in committed state). Op firing: load → `loadEntityData`,
  invoke → http fetch (`invokeHttpService`) or `wsPool.invoke` (decoded
  resolved-mode, transients bound to the ws (url, generation)), metadata
  → local resolve, callback → mint a uuid'd inbox registered with the
  connector's notify router. `subscribe_to` (a handle-targeted invoke) is
  reinterpreted as `source.subscribe(handle)`; on a ws source the inbox's
  `connectionState` flips to `connected` and back to `broken` when that
  ws generation drops (synthesized locally via `onConnectionDrop`).
  Remote breakage always surfaces as an errored fill (never a rejected
  write). **Persistence / rehydration** (design/05): `serialize()`
  produces a `SerializedSession` (committed `userState` + dedup + invoke
  fills + inbox logs, shortId-mode escaped), debounced after every
  commit / fill / notify and flushed on detach; `restoreFrom(blob)` (in
  the constructor) rehydrates it. Entity load + metadata payloads are
  *not* persisted — those slots rehydrate pending and refire (identified
  via the dedup reverse index); in-flight invokes rehydrate as errored
  ("interrupted") fills; transient services + previously-`connected`
  subscriptions rehydrate broken. `restoreFrom` returns `false` on an
  unrestorable blob (bad version / corrupt payload) — the session then
  lands terminal `unavailable` (loud `console.error`, no crash, no
  fabricated blank session). `terminate()` (the abstract-base destroy)
  tears down inboxes + deletes the persisted blob/registry entry, vs.
  `detach()` which keeps storage for a later re-attach.

Tests in `tests/`: MockWebSocket/mock-fetch **unit** suites mirroring the
connector-workstation suites (`shortid_table`, `entity_decode`,
`entity_data`, `metadata`, `stubs`, `ws_pool`, `connector`, and `session`
— the last covering the reducer, callbacks, terminate, and
persistence→rehydrate). Plus an **integration** suite
(`integration.test.ts`) that drives the real connector (global
fetch + WebSocket) against a kitchen-sink Python backend
(`_demo_backend.py`, built on the `fxtr2.experimental.http.service_*`
library: a pinned parent/child entity graph + GET/POST/WS services
returning entities, an undiscoverable descriptor, callbacks/transients,
and `$fxtr.type`) spawned over the wire by `_integration_harness.ts`
(via `uv run python` in the workstation env — it carries `fxtr2`; the
backend shuts down gracefully on `SIGTERM`, and the suite closes every
connector first so no server ws read loop stalls the drain).

### `@fxtr-ui/react` (`packages/fxtr-ui-react/`)

React bindings layer. Depends on `@fxtr-ui/core` +
`@fxtr-ui/state-util` (workspace), `jotai`, and `react` as a peer
dependency (>=18). Source modules in `src/`:

- `session_context.tsx` — `<FxtrSessionProvider session={s}>` wraps
  a jotai `<Provider store={s.store}>` plus a React Context
  (`SessionContext`) carrying the session. Inside the provider,
  `useAtomValue` / `useSetAtom` resolve against `session.store`,
  so the session's authoritative atoms (`session.userState`,
  `session.lifecycle`, …) and any component-internal atoms
  (`useMemo(() => atom(...))`) share that scope. Two panels
  backed by different `Session` instances have fully isolated
  component-internal state. `useSession()` reads the session from
  context; throws if called outside a provider (silent-null would
  let stale reads happen against the default store).
  `SessionContext` itself is exported so other packages (e.g.
  `@fxtr-ui/react-dispatch`) can write `null` into it at a
  subtree boundary, making `useSession()` inside that subtree
  throw — a deliberate way to "hide" the outer session.
- (Intentionally no `<ConnectorProvider>` — the `Connector` abstract
  surface ended up too thin to be useful at the package level;
  consumers wrap their own `<JotaiProvider store={connector.store}>`
  for connector-scope reactive state and pass the typed connector
  reference around however suits — prop drilling, their own React
  Context, a module-scoped singleton.)
- `hooks.ts` — three hooks:
  - `useSessionRootStateAtom<T>(initialDefault)` returns a
    `FxtrSessionAtom<T>` — the session's `userState` atom lifted
    via `atomWithDefault` so reads never see null. The generic `T`
    is the consumer's narrowed shape; `initialDefault` is captured
    once at mount (mirroring `useState(initialValue)`) to keep the
    returned atom's identity stable across re-renders. Accepts
    either a value or a lazy `() => T` factory. After calling this
    once at a panel's root, consumers do everything via vanilla
    jotai — `useAtomValue` / `useSetAtom` / `useSubAtom` — no
    privileged session-level read/write hooks.
  - `useSubAtom(parent, key)` memoizes `subAtom(parent, key)` so
    the returned atom is identity-stable across renders (chained
    calls are stable too).
  - `useResult(stub)` is one-line sugar over
    `useAtomValue(stub.result)` returning the discriminated
    `ResultStatus<T>` union.

  TypeScript can't verify that the consumer's `T` fits `StateValue`
  (named-interface-vs-index-signature limitation), so the
  underlying cast in `useSessionRootStateAtom` is unchecked. The
  connector's wire codec is the actual runtime gate on
  encodability.
- `index.ts` — re-exports `<FxtrSessionProvider>`, `useSession`,
  and the three convenience hooks.

React-layer component tests live in
**`packages/fxtr-ui-react-tests/`** — a sibling tests-only package.
They cover the provider, all four hooks, and the
multi-session-isolation case. The split dates to a (pre-reorg,
since-moot) vitest loading issue with `@vitest-environment jsdom` +
`.tsx` under the old directory tree; see that package's README.
Colocated jsdom `.tsx` tests work fine under
`frontend_lib/packages/` today (e.g. `fxtr-ui-rd-types-channel`
keeps its render tests in its own `tests/`).

### `@fxtr-ui/react-dispatch` (`packages/fxtr-ui-react-dispatch/`)

Type-driven component dispatch over `EntityRefStub` /
`ServiceRefStub` values. Consumers declare a `DispatchKey` (one
*rendering slot* — e.g. Card, DetailView, Inline), construct a
`Dispatcher` around a session, and render via
`<RenderDispatch stub={…} as={key} {…props}/>` inside a
`<DispatcherProvider dispatcher={…}>` subtree. The dispatcher
owns the per-`(stub, key)` state-machine effects, the
session-backed userState projection, the per-entry cache, and
its session's lifecycle (it detaches the session on
`dispatcher.detach()`).

The `Dispatcher` abstraction supports different resolution
strategies behind a single `getEntry(key, stub) -> DispatchEntry`
surface. Two are shipped:

- **Static dispatcher** (`createStaticDispatcher({session,
  registry, overrides?})`) — consumes a passive
  `DispatchRegistry` of `(DispatchKey → state-machine recipe)`
  pairs. The per-key recipes (typically
  `staticEntityTypeDispatch` / `staticServiceTypeDispatch`)
  carry a hardcoded `id_string → Component` lookup map.
  Unknown keys fall through to the key's `NoMatchFallback`.
- **Dynamic dispatcher** (`createDynamicDispatcher({session,
  dispatchServiceRef, loadEntityData, compileRendererBytes?,
  overrides?})`) — no registry. For each entry, runs a
  three-phase state machine: (1) get the type entity ref
  (entity-side: load `$fxtr.type`; service-side: invoke
  `$fxtr.type()`), (2) invoke `resolve(type_ref)` on a remote
  dispatcher service that returns
  `{subtype_key, renderer: EntityRef} | null` (a null is the
  backend's "no renderer registered" signal and lands on the
  key's `NoMatchFallback`), (3) out-of-band-load the renderer
  entity's bytes, compile them to ESM, look up
  `module[subtype_key][key.uuid]` for the component.
  Cross-entry caches (session-backed for the type→renderer
  decision, in-memory for the compiled module) dedup
  concurrent work; cache fills drop the wire slot in the
  same flush as the cache write. See
  `plans/phase0/part3_frontend_ui/stage10b_dynamic_dispatch.md`
  for the full design + the four sibling cache fields.

**Fallbacks live on the `DispatchKey`.** Every key's declaration
carries its `Loading` / `NoMatch` / `Error` (and optional
entity-side `Builtin`) fallback components alongside the uuid +
description. The dispatcher reads those directly when it needs
to render synchronously (before / instead of the resolved
component); state-machine helpers like `staticEntityTypeDispatch`
just supply the resolution recipe and never see the fallbacks.
This makes dispatchers `Props`-agnostic: a key declared anywhere
(including in code that arrives at runtime) carries everything a
dispatcher needs to render. Per-dispatcher per-key customisation
goes through an optional `overrides` array constructed via
`overrideKeyFallbacks(key, partial)`. See
`plans/phase0/part3_frontend_ui/stage10aside1_dispatch_key_fallback_refactor.md`
for the design rationale.

A consequence: the static dispatcher renders the key's
`NoMatchFallback` for keys with no matching registry entry
instead of throwing — symmetric with how the dynamic
dispatcher renders NoMatch when the resolve service returns
no renderer (or the compiled module has no matching
`subtype_key` / `key.uuid` entry). Static and dynamic
dispatchers degrade interchangeably.

Depends on `@fxtr-ui/core` via `workspace:*` and on `jotai` (for
atom types in the effect-factory signature). Peer-depends on
`react` (>=18).

Source modules in `src/`:

- `dispatcher.ts` — the abstract `Dispatcher` surface:
  `Dispatcher` (with `status: Atom<DispatcherStatus>`,
  `session: Session`, `getEntry(key, stub) -> AnyDispatchEntry`,
  `detach(): Promise<void>`, and optional
  `debugView?(): ReactElement`), the five-state
  `DispatcherStatus` union (`connecting | connected | stale |
  closed | unavailable`), the `DispatchEntry<Stub, Props>`
  interface (read-only `componentAtom`, writable session-backed
  `sharedComponentStateAtom`, read-only
  `dispatchInternalStateAtom` and `dispatchProgressMessageAtom`,
  plus the `id` / `stub` / `ErrorFallback` carry), the erased
  `AnyDispatchEntry` cache view, and the `DispatcherClosedError`
  thrown by post-`detach()` `getEntry` calls. `debugView()` is
  method-style so the dispatcher's `this` is already bound when
  the element is built — concrete impls close over their own
  atoms and wrap the returned element in
  `<Provider store={session.store}>` so atom reads scope to the
  dispatcher's session store regardless of the surrounding
  consumer context.
- `keys.ts` — `DispatchKey<Props, Allowed>` type +
  `declareDispatchKey<Props>({uuid, description, fallbacks})`
  (allows both ref kinds), `declareEntityDispatchKey` (entity-
  only), `declareServiceDispatchKey` (service-only). Each
  factory takes a single options object; the `fallbacks` field
  carries the per-key UI components the dispatcher renders for
  Loading / NoMatch / Error (and optional `Builtin` for entity-
  allowed keys). The `Allowed` type parameter is a phantom-
  branded union of `RefKind = "entity" | "service"`. Each
  declared key carries a caller-supplied wire-stable UUID (used
  by the dispatcher's `userState.entries[uuid][shortId]` keying
  scheme); UUID uniqueness is the consumer's responsibility (no
  runtime collision check to avoid HMR false positives). The
  `Props` generic defaults to the exported `NoProps = object`
  alias — a deliberate name pointing at the foot-gun this
  avoids: `Record<string, never>` looks like the natural "empty
  props" type but its index signature collapses intersections
  and blocks JSX-level attributes like React's `key`. Also
  exports `AnyDispatchKey = DispatchKey<any, RefKind>` (used as
  the erased upper bound by `<RenderDispatch>` / `Dispatcher.getEntry`;
  `any` here is load-bearing because `Props` participates
  contravariantly through `fallbacks`, so a natural
  `DispatchKey<object, RefKind>` upper bound rejects keys with
  non-trivial `Props`), plus `ExtractDispatchProps<K>` /
  `ExtractDispatchAllowed<K>` helpers and the
  `RequireEntityAllowed` / `RequireServiceAllowed` conditional
  types that gate the registry-side narrowing.
- `fallbacks.ts` — the per-key fallback-bundle types:
  `EntityKeyFallbacks<Props>` (Loading + NoMatch + Error +
  optional Builtin, stubs typed as `EntityRefStub`),
  `ServiceKeyFallbacks<Props>` (Loading + NoMatch + Error, no
  Builtin), `AnyKeyFallbacks<Props>` (both-allowed; Builtin
  stays narrowed to `EntityRefStub`), and the dispatch-time
  selector `KeyFallbacksFor<Props, Allowed>` (tuple-wrapped
  conditional picking the right of the three based on the
  key's `Allowed` parameter — entity-only → entity-fallbacks,
  service-only → service-fallbacks, both → any-fallbacks).
- `overrides.ts` — per-dispatcher per-key fallback overrides.
  `overrideKeyFallbacks(key, partial)` builds a typed
  `KeyFallbackOverride` opaque carrier whose `partial` is
  narrowed against the key's `Props` + `Allowed`. Dispatchers
  accept `overrides?: ReadonlyArray<KeyFallbackOverride>` and
  consult them at `getEntry` time via the internal
  `buildOverrideMap` + `resolveKeyFallbacks` helpers (uuid-
  keyed lookup; partial-override fields replace the key's,
  absent ones fall through to the key's defaults).
- `registry_types.ts` — `DispatchKeyRegistryEntry<Stub, Props,
  S>` (just `effectFn` + `initialState` + optional
  `initialProgressMessage`; UI fallbacks live on the key, not
  the entry) and the surrounding aliases: `DispatchedComponent<Stub,
  Props>` (the resolved component's prop shape, `{stub:
  Stub} & Props`), `ErrorFallbackComponent<Stub, Props>` (the
  component the internal error boundary renders when a
  dispatched component throws; receives the stub, the caught
  `error`, and the key's Props), `ComponentAtom<Stub, Props>`
  (the writable atom holding the currently-rendered component),
  `ProgressMessageAtom` (plain in-memory `WritableAtom<string,
  ...>` for human-readable status), and the `DispatchEffectFactory`
  signature `(params: {stub, stateAtom, componentAtom,
  progressMessageAtom, fallbacks}) => (get, set) => Cleanup | void`.
  The `fallbacks` param is `StubFallbacks<Stub, Props>` (also
  exported here) — a stub-kind-narrowed view of the key's
  fallback bundle that the state machine reads directly when
  it needs to write `componentAtom = fallbacks.NoMatch` etc.
  `EntityDispatchEntry<Props, S>` and `ServiceDispatchEntry<Props,
  S>` are concrete-`Stub` aliases.
- `registry.ts` — the abstract `DispatchRegistry` interface:
  two route-tables (`forEntity<Props>(key)` /
  `forService<Props>(key)`) returning the matching
  `EntityDispatchEntry` / `ServiceDispatchEntry` (or
  `undefined`). The contract only; concrete builders live in
  their own modules so the surface isn't coupled to one
  construction style.
- `static_registry.ts` — one specific way to build a
  `DispatchRegistry`: from a list of `(key, entry)` pairs.
  `entityEntry(key, entry)` / `serviceEntry(key, entry)` build
  the pairs and statically reject mismatched ref-kinds via
  tuple-wrapped conditional types (`["entity"] extends [A] ?
  DispatchKey<P, A> : never`). `createDispatchRegistry({
  entityEntries, serviceEntries })` assembles a registry,
  throwing `DuplicateDispatchKeyError` if two pairs in the
  same table share a UUID (the same key in both tables —
  entity entry + service entry — is the intended use case
  and not an error). The pair types
  `EntityDispatchRegistryForKey` /
  `ServiceDispatchRegistryForKey` are exported for
  intermediate-value typing but most consumers only touch the
  factories.
- `static_entity_registry.ts` — `staticEntityTypeDispatch({lookupMap})`
  builds a `DispatchKeyRegistryEntry<EntityRefStub, Props,
  StaticEntityDispatchState>` driving a `init →
  loading-entity → loading-type-metadata → resolved | errored`
  state machine: allocate a load, extract `$fxtr.type` from
  the loaded value, allocate metadata on that type ref, look
  up `metadata.id_string` in `opts.lookupMap`. The state
  machine reads `params.fallbacks.NoMatch` / `.Builtin` /
  `.Error` directly when transitioning to a terminal arm that
  needs to render one — fallback components come off the key
  (or the dispatcher's overrides), not the helper's opts.

  Builtin handling: `init` short-circuits to a terminal
  `resolved-builtin` phase for entity shortIds beginning with
  `.` (anonymous), `!` (builtin), or `c` (context provenance)
  — entities the workstation can't load directly. c-tag nodes
  are a system-defined provenance form (carrying metadata
  only) that users can't replace with their own renderer, so
  they share the unloadable-builtin treatment. The phase
  renders `fallbacks.Builtin` if the key set one, falling
  through to `fallbacks.NoMatch` otherwise.

  Terminal phases re-check the `componentAtom` on every
  effect run so a reattach-to-existing-session (where
  `userState` already carries the terminal state but the
  in-memory atom is fresh) syncs back up. Terminal `errored`
  rendering goes through a synthetic `ErrorThrower` component
  that closes over the entry's `stateAtom`, reads the current
  error message via `useAtomValue`, and throws during render
  — the per-entry React error boundary catches it and renders
  the key's `fallbacks.Error`. Progress messages
  (`"loading entity…"` → `"resolving renderer…"` → cleared)
  are pushed to `progressMessageAtom` for generic
  `useDispatchProgressMessage`-driven fallbacks.
- `static_service_registry.ts` — `staticServiceTypeDispatch({lookupMap})`,
  same shape but one fewer hop (`init →
  loading-type-metadata → resolved | errored`), using
  `ctx.allocateGetServiceMetadata` and reading
  `metadata.type_id_string`. No `Builtin` arm — services have
  no unloadable-tag convention. State machine reads
  `params.fallbacks.NoMatch` / `.Error` from the key. Exports
  `StaticServiceDispatchState`.
- `base_entry.ts` — the kernel both static and any future
  dispatchers use to build a single `DispatchEntry` from
  pre-wired session atoms. `createBaseEntry({stub, stateAtom,
  sharedComponentStateAtom, initialComponent,
  initialProgressMessage, effectFactory, ErrorFallback,
  fallbacks, id})` owns the in-memory machinery: the
  progress-message atom, the writable component sink wrapped
  with the **shared-state reset invariant** (any write that
  changes the component value also writes `null` to
  `sharedComponentStateAtom` in the same flush), the
  `atomEffect`-mounted effect-fn body, and the public
  read-only `componentAtom` that depends on both the
  effect-atom and the sink (subscribing to it mounts the
  effect). The `fallbacks` (already merged with any
  dispatcher overrides) is threaded into the effect-fn
  factory's `params.fallbacks` field verbatim;
  `initialComponent` and `ErrorFallback` are listed
  separately so the helper doesn't need to interpret the
  bundle's shape. Also exports the shared `EntryUserStateSlot`
  interface (`dispatcher_state` + `component_shared_state`)
  — the per-`(stub, key)` shape every dispatcher's userState
  uses under `entries[keyUuid][shortId]`.
- `static_dispatcher.ts` — `createStaticDispatcher({session,
  registry, overrides?})` returns a `Dispatcher`. Holds: the
  transferred session, the registry, an internal uuid-keyed
  override map built from `overrides`, the dispatcher root
  atom (an `atomWithDefault` over `session.userState`
  substituting `EMPTY_DISPATCHER_USER_STATE` on null), the
  per-`(keyUuid, shortId)` entry cache, a parallel
  `mountedIdsAtom: PrimitiveAtom<readonly string[]>` reactive
  index of the cache contents (consumed by `debugView()`), and
  a `closedAtom` flag. `getEntry` does stub-kind dispatch by
  `instanceof`, registry table lookup, session sub-atom
  projection down to the entry's slot, fallback resolution
  (merge `key.fallbacks` with any matching override), and
  hands everything to `createBaseEntry` — then caches the
  result by `${keyUuid}::${shortId}` via a small `cacheSet`
  helper that also bumps `mountedIdsAtom`. Unregistered keys
  land on a degenerate-entry path: a no-op state machine that
  writes `fallbacks.NoMatch` to `componentAtom` once and stays
  terminal, with `dispatcher_state: {phase: "no-match"}` in
  session state so the decision survives reattach.

  `debugView()` returns the `<StaticDispatcherDebugView>`
  component (in `static_debug_view.tsx`) wrapped in
  `<Provider store={session.store}>`. The view renders the
  dispatcher status, session id / lifecycle, and a reactive
  table of cached entries with each one's currently-published
  component identity.

  `status` is a derived atom — `"closed"` if `detach()` has
  run, else `session.lifecycle`. `detach()` is idempotent via
  a shared in-flight promise: flips the closed flag, clears
  the entry cache, awaits `session.detach()`. Subsequent
  `getEntry` calls throw `DispatcherClosedError`. Also exports
  the `DispatcherUserState` shape (top-level `entries` only —
  the static dispatcher's specific layout) and
  `EMPTY_DISPATCHER_USER_STATE`.
- `dynamic_dispatcher.ts` — `createDynamicDispatcher({session,
  dispatchServiceRef, loadEntityData, compileRendererBytes?,
  overrides?})` returns a `Dispatcher`. Holds: the transferred
  session, the dispatch service ref, the four session-backed
  cache sub-atoms (`service_type_cache`,
  `service_type_active_lookups`, `type_renderer_cache`,
  `type_renderer_active_lookups`), a `reloadInboxAtom`
  sub-atom for the reload-subscription inbox, an in-memory
  primitive `compiledModulesAtom: Map<rendererShortId,
  CompiledModuleStatus>` for compile dedup, an in-memory
  `compiledLoadInFlight` map, the per-`(keyUuid, shortId)`
  entry cache, parallel `staleAtoms: Map<id,
  PrimitiveAtom<boolean>>` (per-entry private staleness flags)
  and `mountedIdsAtom` (reactive view of the cache contents
  for `debugView`), and a `closedAtom`.

  Per-entry state machine, three phases:
  1. **Type discovery.** Entity: `allocateLoad(stub)` →
     extract `$fxtr.type` ref. Service:
     `allocateInvoke(stub, "$fxtr.type", [])` → result is the
     type ref. Service-side dedups concurrent invokes across
     entries via `service_type_active_lookups`; entity-side
     relies on `allocateLoad`'s wire-layer dedup.
  2. **Renderer resolution.** Read
     `type_renderer_cache[typeRef.shortId]?.[key.uuid]` —
     `type_renderer_cache` and `type_renderer_active_lookups` are
     both nested `Record<typeShortId, Record<keyUuid, ...>>` so
     the backend can route different dispatch keys for the same
     type to different renderer modules. On hit with a
     `RendererResolution`, transition to phase 3; on hit with
     `null` (cached no-match for this key), transition straight
     to the terminal `no-match` phase, which publishes the key's
     `NoMatchFallback`. On miss, share or allocate a
     `resolve(typeRef, key.uuid)` invoke on the dispatcher
     service via `type_renderer_active_lookups`. On fill
     (resolution or `null`), promote into the cache and drop the
     active_lookups entry in the same flush — drops the slot's
     wire footprint immediately. `setNestedKey` / `removeNestedKey`
     helpers handle the nested-record updater shape; the outer
     key is reaped when its inner record empties.
  3. **Bytes + compile + lookup.** `ensureCompiled(rendererRef)`
     synchronously marks `{status: "loading"}` in
     `compiledModulesAtom` and kicks an async chain:
     `loadEntityData(rendererRef)` (out-of-band wire load) →
     extract `module: Uint8Array` → `compileRendererBytes(bytes)`
     → cache `{status: "ready", module}`. The state machine then
     publishes `module[subtype_key][key.uuid]` to
     `componentAtom`. Subtype-key miss or key.uuid miss render
     the key's `NoMatchFallback` and `console.warn` to surface
     the backend/frontend disagreement.

  Errors at any phase produce a terminal `errored` state that
  surfaces through the entry's `ErrorFallback` via the same
  synthetic-throw + per-entry error boundary pattern the static
  helpers use.

  **Wrap-on-write (staleness affordance).** Every
  `set(componentAtom, C)` site in the state-machine effect
  factories — including `createBaseEntry`'s `initialComponent`
  — goes through a per-entry `wrap` helper built via
  `makeEntryWrapper(staleAtom)` from `staleness_wrapper.tsx`.
  The wrap stamps a `StaleAware(InnerComponent)` HOC around the
  inner; the HOC subscribes to the per-entry private
  `staleAtom: PrimitiveAtom<boolean>` and renders a `<div>` with
  `display: contents` (fresh) or `display: block; opacity: 0.5`
  (stale). The wrap is `WeakMap`-memoised per inner-component
  identity so `wrap(C) === wrap(C)` — the base entry's
  "componentAtom value changed ⇒ reset sharedComponentStateAtom"
  invariant fires exactly when the underlying component
  changes, and React unmount/remount happens at the same
  boundary it would without wrapping. Terminal phases
  (`resolved` / `resolved-builtin` / `no-match` / `errored`)
  clear `staleAtom` to `false` after publishing. Tests use
  `getStaleAwareInner(wrapped)` to recover the inner identity
  through a hidden `Symbol("StaleAware.inner")` property the
  wrap stamps on each HOC.

  **Reload reception.** `DynamicDispatcherUserState` carries a
  `reload_inbox: CallbackInboxStub<EntityRefStub> | null` +
  `reload_subscribe_result: OpResultStub<null> | null` pair.
  An `atomEffect` mounted at dispatcher construction watches
  `session.lifecycle` for `"connected"`; once connected and
  `reload_inbox` is null (or its `state` reads `"closed"`, in
  which case it logs a warning before re-allocating), the
  effect allocates a fresh callback inbox + dispatches a
  callback-target `subscribe_to(dispatchServiceRef)` invoke on
  the inbox's own handle in one updater (one flush, one wire
  mutation — the workstation subscribes the inbox to the
  dispatcher's change events and owns the lifecycle). The wipe is
  factored into a pure `wipeRootCaches(prev)` (empties the four type
  caches + resets every entry's `dispatcher_state` to `{phase:
  "init"}`; leaves `reload_inbox` and the in-memory compiled-module
  cache alone) plus a `flipAllStale(setter)` helper, shared by all
  three reload paths below. On each delivered inbox event the effect:
  (a) `flipAllStale` (so the wrap immediately dims), then (b) in one
  userState updater applies `wipeRootCaches` and consumes the events
  via `inbox.sliced(events.length)`. The state machine then
  re-resolves each entry; terminal writes clear `staleAtom` back to
  `false`. The `reload_subscribe_result` field anchors the subscribe
  invoke's slot in userState so the session reducer doesn't GC it
  before the wire emission.

  **Reload-on-break.** The same effect also reads
  `reload_inbox.connectionState`. When the durable subscription
  breaks (a ws drop / source-grant revocation surfaces as
  `connectionState: "broken"` while `state` stays `"open"`, which the
  event-reception path can't see — no events arrive and the slot
  never goes `"closed"`), the effect wipes the caches and resubscribes
  by dispatching `subscribe_to` on the **existing** inbox handle (no
  new `allocateCallback`). A closure-scoped `lastReloadConnState` edge
  tracker fires this once per `broken` transition (no resubscribe spin
  on an unreachable source) and re-arms when the connection recovers.
  This is uniform across connectors — the workstation drives
  `connectionState` via `CallbackSubscriptionEvent`s; a stateless HTTP
  connector synthesizes it on ws drop. There is **no** corresponding
  method on the generic `Dispatcher` interface — the trigger lives
  entirely inside `createDynamicDispatcher`.

  **Debug surface.** `debugView()` returns
  `<DynamicDispatcherDebugView>` (in `dynamic_debug_view.tsx`)
  wrapped in `<Provider store={session.store}>`. The view
  shows: status / session info, the reload-inbox state
  (`opening` / `open` / `closed` + cursor offset + pending
  event count), per-cache key listings for all four type
  caches (renderer caches flatten the nested
  `(typeShortId, keyUuid)` form to `"typeShortId::keyUuid"`
  strings; `null` entries annotated as `(null = no match)`),
  the compiled-module map
  (per renderer short_id status + subtype keys for ready
  modules), and the per-entry table (stub + key + current
  phase + stale flag + inner-component identity via
  `getStaleAwareInner`). A bug-guard branch renders an error
  row if `staleAtoms.get(id)` ever returns `undefined` for an
  id in `mountedIds` (dispatcher contract violation). A **"Reload
  everything" button** (the view's one action, via an
  `onReloadEverything` prop the dispatcher fills from `debugView()`
  with an internal closure) runs the same `wipeRootCaches` +
  re-resolve, and resubscribes if the inbox is currently broken —
  a manual trigger of the reload path, kept off the `Dispatcher`
  interface.

  Default `compileRendererBytes`: wraps the bytes in a Blob,
  mints a blob URL, `await import(url)`s it, returns
  `module.default`. Browser-only (jsdom has no native ESM
  loader for blob URLs); tests inject a mock. Production
  consumers must arrange for the bare-specifier import map to
  be installed via `installFxtrExternals` from
  `@fxtr-ui/react-dispatch-dynamic-shim` *before* any renderer
  bytes-load fires.

  Also exports `DynamicDispatcherUserState` (the four sibling
  caches + `entries` + `reload_inbox` + `reload_subscribe_result`;
  `type_renderer_cache` values are nested
  `Record<keyUuid, RendererResolution | null>`),
  `EMPTY_DYNAMIC_DISPATCHER_USER_STATE`,
  `RendererResolution` (`{subtype_key: string, renderer: EntityRefStub}`),
  `CompiledModuleStatus` (loading / ready / errored),
  `CompiledRendererModule` (the 2-level
  `[subtype_key][dispatch_key_uuid] → Component` map shape),
  `RendererEntityLoadedShape` (the Python `RendererEntity`
  wire shape), and the entity / service state-machine state
  unions.
- `staleness_wrapper.tsx` — `makeEntryWrapper(staleAtom)`
  returns a `WeakMap`-memoised wrap function mapping an inner
  `DispatchedComponent` to its `StaleAware(*)` HOC. The HOC
  subscribes to `staleAtom` and renders the inner inside a
  styled `<div>`. `getStaleAwareInner(wrapped)` recovers the
  inner identity via a hidden `Symbol("StaleAware.inner")`
  property the wrap stamps on each HOC — used by tests that
  want identity-based assertions about which component the
  dispatcher published. The `EntryWrapper` type captures the
  wrap function's signature. Lives in a `.tsx` file so the
  HOC body can be plain JSX; `dynamic_dispatcher.ts` is
  `.ts`-only and imports the helper.
- `static_debug_view.tsx` — `<StaticDispatcherDebugView>`,
  the React component returned (wrapped in
  `<Provider store={session.store}>`) by
  `createStaticDispatcher`'s `debugView()`. Renders the
  dispatcher status, session id / lifecycle, and a reactive
  table of mounted entries (subscribed to the dispatcher's
  internal `mountedIdsAtom`) with the currently-published
  component's `displayName` for each.
- `dynamic_debug_view.tsx` — `<DynamicDispatcherDebugView>`,
  returned by `createDynamicDispatcher`'s `debugView()`.
  Renders a "Reload everything" button (calls the
  `onReloadEverything` prop the dispatcher supplies) plus
  reactive sections for the reload inbox state, the four type
  caches (`service_type_*` + `type_renderer_*`), the
  compiled-module map, and the per-entry table with each
  entry's current phase / stale flag / inner-component
  identity (via `getStaleAwareInner`).
- `error_boundary.tsx` — `DispatchErrorBoundary` class
  component. React error boundaries must be classes — this
  is the one non-functional component in the package. Catches
  render-time throws from the dispatched component (including
  the synthetic ones from the static helpers' `errored`
  terminal phase) and renders the entry's `ErrorFallback`
  with `{stub, error, ...extraProps}`.
- `dispatcher_provider.tsx` — `<DispatcherProvider dispatcher>`,
  the top-of-subtree component. A thin React-context wrapper:
  `DispatcherContext` exposes the dispatcher;
  `useDispatcher()` hook reads it (throws outside the
  provider). All entry-resolution machinery lives on the
  dispatcher itself — the provider doesn't touch sessions,
  registries, or caches.
- `render_dispatch.tsx` — `<RenderDispatch>` plus the
  dispatched-component-side `DispatchEntryContext` /
  `useDispatchEntryContext()`. The component generic
  constraint is `K extends AnyDispatchKey` (see `keys.ts` for
  why this uses the erased-`Props` form). The prop type uses
  a tuple-wrapped conditional (`StubFor<K>`) to narrow the
  allowed `stub` kind from the key's `Allowed` — an
  entity-only key statically rejects a service stub at the
  call site. Reads the dispatcher via `useDispatcher()`,
  calls `dispatcher.getEntry(dispatchKey, stub)`, subscribes
  to `entry.componentAtom`, and renders the resolved
  component inside three nested wrappers: the entry context,
  a `<SessionContext.Provider value={null}>` (hides the
  parent's `useSession()` so the dispatched component can't
  smuggle slot allocations into the outer session), and the
  error boundary. The whole subtree is keyed on
  `entry.id` so a change of stub or key remounts cleanly.
- `hooks.ts` — `useDispatchSharedAtom<T>(default)` (mirrors
  `useSessionRootStateAtom`: lifts the entry's
  `sharedComponentStateAtom` through `atomWithDefault`,
  captures default once at mount, identity-stable return),
  `useDispatchProgressState<S>()` (typed view of the entry's
  `dispatchInternalStateAtom`, unchecked cast — consumers
  pass the state union exported by the helper they used),
  `useDispatchProgressMessage()` (the in-memory
  progress-message atom as a plain string).
- `index.ts` — public re-exports.

Tests in `tests/`:

- `_mocks.ts` — `TestEntityRefStub` / `TestServiceRefStub` /
  `TestOpResultStub` (concrete subclasses of the abstract core
  stubs with controllable identities), plus
  `makeMockSessionAtom` which produces a `FxtrSessionAtom<S>`
  whose function-form-updater ctx dispenses pre-queued
  `TestOpResultStub`s from `allocateLoad` /
  `allocateGetEntityMetadata` / `allocateGetServiceMetadata`.
- `_integration_harness.ts` — spawns
  `@fxtr-ui/dispatch-demo`'s Python workstation as a child
  process, captures the `TYPE_IDS` + `READY` stdout lines, and
  returns a `{clientUrl, typeIds, close()}` handle.
- `keys.test.ts` — `declareDispatchKey` / `declareEntityDispatchKey`
  / `declareServiceDispatchKey` shape + freeze + per-call
  identity + the new `fallbacks` field round-trip.
- `registry.test.ts` — `entityEntry` / `serviceEntry` /
  `createDispatchRegistry` route selection, miss returns
  `undefined`, `DuplicateDispatchKeyError` carries the
  conflicting key + table.
- `static_entity_registry.test.ts` /
  `static_service_registry.test.ts` — drive each state machine
  by invoking the inner effect-fn directly with a jotai store's
  `get` / `set`, threading a hand-built fallback bundle through
  `params.fallbacks`. Covers the happy path (init →
  loading-entity → loading-type-metadata → resolved on entity;
  init → loading-type-metadata → resolved on service), error
  paths (load fails, missing/invalid `$fxtr.type`, metadata
  fails, unknown type-id falls back to `fallbacks.NoMatch`),
  and the terminal-state re-sync that handles
  reattach-to-existing-session.
- `static_dispatcher.test.ts` — smoke tests for
  `createStaticDispatcher`'s own machinery: `getEntry` returns
  the same entry for repeat lookups and distinct entries for
  distinct stubs; the unregistered-key path renders
  `key.fallbacks.NoMatch` instead of throwing; `status`
  mirrors `session.lifecycle` and flips to `"closed"` after
  `detach()` (regardless of the underlying session's later
  state); `detach()` is idempotent via a shared in-flight
  promise (concurrent calls return the same promise, only one
  `session.detach()` fires); post-`detach()` `getEntry` throws
  `DispatcherClosedError`. Uses a hand-rolled `Session` mock
  with a controllable `lifecycle` primitive and a call-
  counting `detach()`.
- `integration.test.ts` — end-to-end against the demo
  workstation. Boots the Python backend once in `beforeAll`,
  then for each demo entity / service builds a
  `StaticDispatcher` around the shared session and asks for
  the entry via `dispatcher.getEntry(CardKey, stub)`,
  subscribes to mount the effect, and polls until the state
  machine resolves. Asserts the right component identity for
  Note / Photo / Echo / Counter, plus a no-match fallback
  case (the dispatcher's unregistered-key path renders the
  key's `NoMatch`).
- `dynamic_dispatcher.test.ts` — state-machine tests for
  `createDynamicDispatcher` (entity / service phase 1, phase 2
  renderer resolution, phase 3 bytes + compile + lookup,
  errored / no-match terminal paths, reload-subscription
  closed-inbox recovery, and reload-on-break: a
  `connectionState → broken` flip wipes the caches + resubscribes
  on the same handle, once per broken edge). Uses a `staleAwareInnerOf` helper
  that calls `getStaleAwareInner` + asserts the wrap is
  present, so assertions check both "the dispatcher wrapped
  the component" and "the inner is/isn't X."
- `staleness_wrapper.test.ts` — `WeakMap` memoisation + symbol
  round-trip for `makeEntryWrapper` / `getStaleAwareInner`,
  without React rendering.
- `dynamic_integration.test.ts` — live end-to-end against the
  dynamic-dispatch-demo Python backend; the dispatcher resolves
  Note / Photo entities through the full wire path. Uses a
  mocked `compileRendererBytes` to skip the real ESM loader
  (browser-only); the compile pipeline itself is covered by
  the demo's `pnpm dev` smoke test.
- `dynamic_reload.test.ts` — live end-to-end against the
  dedicated stdin-controlled reload-harness backend
  (`_reload_harness_workstation.py`). Triggers reload via
  stdin command, polls until the dispatcher's inbox cursor
  advances, then asserts the entry re-resolves. Deterministic
  (no mtime / timing dependency).

The React-render-layer tests for `<DispatcherProvider>`,
`<RenderDispatch>`, the dispatched-component-side hooks, the
staleness wrap's render behaviour, and the `debugView()`
surfaces live in a sibling tests-only package at
`packages/fxtr-ui-react-dispatch-tests/` (same historical split
as `@fxtr-ui/react`'s tests — see that package's README).

### `@fxtr-ui/workstation-dispatch` (`packages/fxtr-ui-workstation-dispatch/`)

The renderer-author-facing dispatch surface for the workstation:
canonical `DispatchKey<...>` objects + Props types + per-key
fallback components, the per-key registry builder, the `RefLink`
/ `JsonRefTree` / metadata-view primitives, and the
`RefLinkImplContext` that lets the surrounding shell (or any
other consumer) install richer interaction behaviour around
dispatched links.

**Externalised** via the v1 externals list (added to
`DEFAULT_EXTERNALS` in
`fxtr_workstation/dynamic_dispatch/basic_dynamic_dispatch/compile.py`),
so runtime-loaded renderer modules and the host workstation
shell share a single identity per UUID — one `FullPanel`
object, one fallback set, one `RefLink` component. The host's
`installFxtrExternals` call registers `"@fxtr-ui/workstation-dispatch"`
alongside the other externals; a renderer module's
`import { FullPanel } from "@fxtr-ui/workstation-dispatch"`
resolves through the import map to the host's instance.

Runtime deps: `react`, `@fxtr-ui/core`, `@fxtr-ui/react`,
`@fxtr-ui/react-dispatch`, `@fxtr-ui/state-util`, `jotai` —
all already in the v1 externals list. No `@base-ui/react`, no
`dockview`, no shell internals; importing the package from
inside a renderer module doesn't drag any new transitive
dependency into the bundle.

Source modules in `src/`:

- `dispatch/keys.ts` — `FullPanel` / `BlockLink` /
  `InlineLink` / `HoverPreview` `DispatchKey<...>` objects with
  their wire-stable UUIDs + `FullPanelProps` / `LinkProps` /
  `HoverPreviewProps` Props types. `FullPanel` declares a
  `Builtin` fallback (the other three rely on their existing
  load-free `NoMatch` for unloadable shortIds).
- `dispatch/combined_fallbacks.tsx` — per-key wrappers that
  branch on stub `instanceof` and forward to the matching
  kind-specific fallback in `fallbacks/`.
- `dispatch/registry.ts` — `createWorkstationDispatchRegistry`
  builder that assembles a `DispatchRegistry` from per-`(kind,
  key)` lookup maps.
- `fallbacks/` — 14 fallback components (4 keys × entity /
  service + the loading variants + `DispatchErrorFallback`,
  plus `EntityFullPanelBuiltinFallback` for the `FullPanel`
  key's `Builtin` slot — does a metadata call instead of a
  load so it works on `c` / `!` / `.` stubs and surfaces
  `context_data` etc. via the shared `MetadataFields`
  component). Plain React + CSS-token styled; no
  shell-context dependencies.
- `reflink/RefLink.tsx` — thin context-forwarder. Reads a
  `RefLinkImpl` (a `React.ComponentType<RefLinkProps>`) out of
  `RefLinkImplContext` and renders it with the props it
  received. The impl decides what to wrap around the
  dispatched content (drag handlers, hover preview, context
  menu, …); no fixed taxonomy of interactions baked in.
- `reflink/impl_context.tsx` — `RefLinkImpl` type +
  `RefLinkImplContext` + `DefaultRefLinkImpl`. The default
  impl just calls `<RenderDispatch as={InlineLink | BlockLink}
  stub={stub} {...rest} />` with no wrappers, suitable for
  tests / headless rendering / use outside any shell.
- `reflink/ref_stub_resolver.ts` — `RefStubResolver` interface
  + `RefStubResolverContext` (default null) +
  `useRefStubResolver()` hook. Lets renderer modules mint
  connector-interned `EntityRefStub` / `ServiceRefStub` instances
  by `shortId` and disambiguate a bare `shortId` between entity
  / service namespaces via `classifyShortId`. The shell installs
  a concrete impl (`ShellRefStubResolverProvider` in
  `@fxtr-ui/workstation`) wrapping `connector.entityRef` /
  `serviceRef` + the connector's resource lists; outside a shell
  the hook returns `null` and consumers degrade gracefully.
- `dnd/` — drag-and-drop primitives shared with the shell.
  - `dnd/constants.ts` — `DRAG_MIME_FXTR_REF` (the
    `application/x-fxtr-ref` MIME type carried by ref drags out
    of the shell).
  - `dnd/parse.ts` — `parseRefDragData(dataTransfer): {kind,
    shortId} | null`, a minimal renderer-facing parser that
    extracts just `{kind, shortId}` from either shell drag-payload
    variant (`LinkDragPayload` / `SnapshotDragPayload`). Returns
    `null` on a malformed payload or non-ref drag.
  - `dnd/drop_region.tsx` — `RefDropRegionContext` /
    `RefDropRegionProvider` / `useRefDropRegion()`. Tracks a
    shared counter of how many renderer-owned drop regions are
    currently being dragged over. The shell's `DockviewLayout`
    consults `isActive` and skips its drop-overlay accept when
    a region is active, so the renderer's own drop handler
    receives the drop instead. `useRefDropRegion()` returns
    `{regionProps: {onDragEnter, onDragLeave, onDrop}}` for the
    renderer to spread onto its wrapper element; the hook tracks
    a per-region nested-enter counter so child-traversal noise
    doesn't flip the shared flag, plus an unmount-cleanup
    `useEffect` that decrements the shared counter if the region
    unmounted mid-enter.
- `metadata/JsonRefTree.tsx`, `metadata/EntityMetadataView.tsx`,
  `metadata/ServiceMetadataView.tsx` — collapsible JSON tree +
  per-stub-kind metadata views. Ref leaves render through
  `RefLink` (so they pick up the surrounding context impl's
  interactivity).
- `metadata/MetadataFields.tsx` — extracted label/value table
  for `EntityMetadata` (ref-typed fields render as inline
  `RefLink`s; id-string fields render as monospace). Shared by
  `EntityMetadataView` and the FullPanel `Builtin` fallback in
  `fallbacks/EntityFullPanelBuiltinFallback.tsx`.
- `index.ts` — public re-exports.

Tests in `tests/`: `keys.test.ts` snapshots the four UUID
constants (wire-stable across releases). React-render tests for
`RefLink` + `RefLinkImplContext` (default impl vs. context
override) live in
`packages/fxtr-ui-workstation-dispatch-tests/` (same historical
sibling-tests split as the other React-render test packages).

### `@fxtr-ui/rd-types-stream` (`packages/fxtr-ui-rd-types-stream/`)

A single dispatch key — `StreamEntry` — plus its default
fallback components. The package is intentionally minimal:
*just* the key identity (`uuid`, `description`, fallback bundle)
and the three fallback components. No layout machinery, no
list-iteration helpers, no scrolling primitive — the owning
panel component (e.g. the agent-plugin renderer's `FullPanel`)
drives those concerns; each entry's actual card is dispatched
through this key.

The key carries `Props = NoProps` and admits both entity and
service stubs. Per-renderer data loads typically ride on
`useDispatchSharedAtom` + `makeLoadResultAtom` (the same pattern
the report-link renderers use), so the empty Props surface
isn't a limitation in practice.

Fallback components (in `src/fallbacks.tsx`):

- `StreamEntryLoadingFallback` — shown while the dispatcher's
  state machine is resolving the entry's type-to-renderer
  mapping.
- `StreamEntryNoMatchFallback` — shown when resolution returns
  "no registered renderer for this type"; surfaces the stub's
  short id so the operator can still identify the entry.
- `StreamEntryErrorFallback` — rendered by the per-entry error
  boundary; surfaces the thrown error's `message` verbatim.

All three use the same outer card chrome (rounded corners,
subtle border, padded body) styled via the workstation theme's
CSS custom properties, so fallback cards sit visually alongside
registered renderers without breaking the stream's rhythm.

Source modules in `src/`:

- `keys.ts` — `StreamEntry: DispatchKey<NoProps>` declared via
  `declareDispatchKey` with a hardcoded wire-stable UUID; also
  re-exports `StreamEntryProps` as a `NoProps` alias for symmetry
  with the workstation-dispatch convention.
- `fallbacks.tsx` — the three fallback components + shared card
  chrome.
- `index.ts` — public re-exports.

Tests in `tests/keys.test.ts` snapshot the UUID + assert the
fallback bundle shape (Loading + NoMatch + Error present,
Builtin absent — the key admits services, so it has no
entity-only shortcut).

Depends on `@fxtr-ui/core` + `@fxtr-ui/react-dispatch` via
`workspace:*`; peer-depends on `react`. No render-layer
dependencies on `dockview`, `@base-ui/react`, or the workstation
shell — consumers in any context (including renderer modules
loaded at runtime) can import the key without dragging
infrastructure in.

### `@fxtr-ui/rd-types-channel` (`packages/fxtr-ui-rd-types-channel/`)

Frontend support for fxtr2 **output channels**
(`v2/fxtr2/common/channels.py`; design in
`v2/plans/phase3/stage13post1_output_channel_redo/`). A channel is
fronted only by a **reader** service; all readers share one fixed
service type id (`CHANNEL_READER_TYPE_ID`), so per-stream rendering
dispatches on the **tag's entity type**, not the service type. The
tag is fetched from the reader via `get_tag` (it is not carried in the
event stream), and a subchannel is referenced by a string **key**
(opened via `open_subchannel_reader`), never by a service ref in a
message. The package is built around one idea — render any channel
from its reader — and never interprets message contents:

- `reader_view.tsx` — the reusable units. `ChannelReaderView` ({reader,
  ownStorage}): fetch the tag via `makeInvokeResultAtom`(`get_tag`)
  (showing "starting…" until the channel starts, or an aborted note if
  it aborted before starting), then re-dispatch `ChannelContentCard` on
  the tag's entity type. `useSubchannelReader(parentReader, key, slot)`:
  `open_subchannel_reader(key)` → child reader as a `LoadResult`
  (the persisted invoke slot keeps the child alive). `SubchannelView`
  ({parentReader, channelKey, ownStorage}): open + render via
  `ChannelReaderView` — the headline subchannel API; the recursion
  (open → reader view → card → …) closes here.
- `keys.ts` — the two entity-only dispatch keys with wire-stable UUIDs:
  `ChannelContentCard` (props `{channel: reader, ownStorage}` — full
  live view) and `ChannelStatusPreview` (props `{state:
  ChannelStateEventWire | null, channel}` — compact *live* status).
  Both dispatch on the tag stub; the reader ref rides in as a prop.
- `types.ts` — the wire types (`ChannelUpdateWire` / `ChannelStateWire`
  / `ChannelStateEventWire` / `SubchannelOpenedWire`, mashumaro
  encodings as they arrive through a callback inbox, ref leaves already
  stubs) and the pure folds: `foldChannelUpdates(events) ->
  ChannelViewState` (snapshot-then-batched-deltas; `update` appends a
  batch and honors `replace_history`; status threads through;
  `connecting / open / complete / aborted`; malformed → diagnostic
  `aborted`) and `latestChannelState(events)` (state stream → latest).
- `subscription.ts` — three hooks over a shared inbox helper, differing
  only in the `subscribe_to` method name: `useChannelSubscription`
  (`subscribe_full` → `foldChannelUpdates`), `useChannelStateSubscription`
  (`subscribe_state` → `latestChannelState`, the cheap live status/badge
  signal), `useDebugSubchannels` (`subscribe_debug_subchannels` → the
  `SubchannelOpened` list). Each persisted in its own versioned slot.
- `fallbacks.tsx` — `GenericChannelCard`, the **generic channel card**
  that doubles as `ChannelContentCard.NoMatch` (tag reflink header +
  phase badge; message rows through `JsonRefTree` / `RefLink`; result
  view; abort banner) — an unregistered tag type still gets a working
  live view, **plus a schema-blind subchannel list** while open: each
  `SubchannelOpened` is a lazily-opened child reader rendered as a
  `RefLink` (which routes through the compact preview → a live state
  pill, clickable to open the full stream). Plus the card Loading
  placeholder and the live status-pill fallbacks for
  `ChannelStatusPreview`.
- `reader_renderers.tsx` — renderers for the fixed channel-**reader**
  service type covering the four shell keys. FullPanel: panel chrome
  around `ChannelReaderView`. BlockLink / InlineLink / HoverPreview:
  one shared wrapper that subscribes to the cheap **state** stream
  (`useChannelStateSubscription`, the primary live lifecycle signal)
  + a best-effort `get_tag`, re-dispatching `ChannelStatusPreview` once
  the tag fills (else a generic state-driven pill). Exported as
  `channelReaderRenderers`, a dispatch-key-uuid-keyed record; the
  consuming renderer slice picks the subtype key (the demo's
  `renderers/builtin/` slice registers it as `channel_reader`, with the
  type entity minted via `nix/lib/entities.nix`'s `channelReader` from
  the `nix/fxtr_lib_type_ids` `CHANNEL_READER_TYPE_ID` re-export).

Tests in `tests/`: `keys.test.ts` (UUID stability + fallback-bundle
shape), `fold.test.ts` (`foldChannelUpdates` incl. batched updates /
`replace_history` / protocol-error paths, + `latestChannelState`),
`generic_card.test.tsx` (jsdom render smoke: the two subscribed feeds,
the phases, `replace_history`, and a debug subchannel → opened child
reader reflink; `RefLink` stubbed via `RefLinkImplContext`), with local
`_mocks.ts` (scriptable `CallbackInboxStub` + mock session atom
dispensing inbox / invoke stubs).

Depends on `@fxtr-ui/core`, `@fxtr-ui/react-dispatch`,
`@fxtr-ui/state-util`, `@fxtr-ui/workstation-dispatch`, `jotai`;
peer-depends on `react`. Externalised for renderer modules: hosts
that may load a channel renderer add it to `installFxtrExternals`
(the demo's `main.tsx` does), and compiled slices that import it
add it to their `fxtr_renderer.nix` externals.

### `@fxtr-ui/panel-viewer` (`packages/fxtr-ui-panel-viewer/`)

The generic, **connector-agnostic** dockview panel viewer — the
top-level chrome a fxtr UI drops in. Provides an open-panels
sidebar, a dockview panel grid, ref-stub dispatch through
configurable type renderers, drag-and-drop link navigation,
breadcrumb / history-root menu inside ref panels, layout
persistence, an always-on connection status header, hardcoded
entity / service metadata views (the "metadata-mode" alternative to
dispatched rendering), and the **injected panel-registration**
mechanism. It types against the abstract `Connector` / `Session`
from `@fxtr-ui/core`; the concrete connector flows in at runtime via
`<ConnectorProvider>`. App-specific panels (roots / debug / custom)
are not built in — they are supplied as `PanelRegistration`s (the
workstation set lives in `@fxtr-ui/workstation`).

Per-ref-panel sessions carry chain state in their `userState`. The
viewer takes a `Connector`, a `Dispatcher`, a
`panelRegistrations: PanelRegistration[]`, and a `storage: Storage`
(the standard Web Storage interface, into which it persists the dockview
layout + sidebar state — `localStorage` in the workstation demo, or the
*same* handle as the connector in the HTTP demo so sessions and the
layout that embeds their ids age together). The consumer constructs
the dispatcher externally — typically via `createStaticDispatcher` or
`createDynamicDispatcher` — and the viewer branches on
`dispatcher.status` for loading / unavailable / connected rendering).
The dispatcher-session-id `localStorage` persistence lives on the
consumer side (the package re-exports `readDispatcherSessionId` /
`writeDispatcherSessionId` / `clearDispatcherSessionId`). The
`DispatcherUnavailable` card calls the optional `onClearAndReload`
prop after clearing its own layout state.

**Panel registrations.** The viewer owns only the built-in **ref
panel** (`PanelWrapper` + the chain/breadcrumb/navigation machinery).
Every other panel is injected as a `PanelRegistration { id; title:
string | (params) => string; component; sidebarButton?; forSession? }`.
`DockviewLayout` builds its dockview component map from the
registrations; the sidebar renders one find-or-open button per
registration with a `sidebarButton`; and `PanelViewerApi.openPanel(id,
params?)` opens (or focuses) a registered panel programmatically —
keyed by `id` for singletons, by `<id>-<sessionId>` for `forSession`
panels (reading `params.sessionId`). On an empty boot the viewer opens
the registration named by its `defaultPanelId` prop (or starts blank
when omitted).

The package re-exports the canonical workstation dispatch keys
(`FullPanel` / `BlockLink` / `InlineLink` / `HoverPreview`), Props
types, fallback components, `RefLink`, `JsonRefTree`, and
`EntityMetadataView` / `ServiceMetadataView` from
`@fxtr-ui/workstation-dispatch` for convenience.

**`ShellRefLinkImpl` + `<ShellRefLinkProvider>`** (in
`src/reflink/ShellRefLinkImpl.tsx`) supply the viewer's interactive
`RefLinkImpl` — a `(props: RefLinkProps) => ReactNode` that wraps the
dispatched link with drag / click / hover-preview / context-menu hooks
(using `@base-ui/react` popovers + menus, the viewer's `PanelContext`
/ `PanelViewerApi`). The provider mounts the impl via
`<RefLinkImplContext.Provider>` near the top of the `<PanelViewer>`
tree; everything below — including dynamically-loaded renderer modules
— picks up the interactive behaviour automatically. Outside the
viewer, the dispatch package's default `RefLinkImpl` renders a plain
dispatched link.

The same `<ShellRefLinkProvider>` mount also installs two sibling
indirections published by `@fxtr-ui/workstation-dispatch`:

- **`<ShellRefStubResolverProvider>`** (in
  `src/reflink/ShellRefStubResolver.tsx`) — concrete impl of
  `RefStubResolver` reading the connector via `useConnector()`.
  `entityRef` / `serviceRef` forward to `connector.entityRef` /
  `serviceRef`; `classifyShortId` **delegates to
  `connector.classifyShortId`** (a `Connector` method — the
  workstation answers it from its resource lists, the HTTP connector
  from its shortId table). All three are on the abstract `Connector`,
  so the resolver is connector-agnostic. Renderer modules read it via
  `useRefStubResolver()`.
- **`<RefDropRegionProvider>`** — installs the shared drop-region
  counter. `DockviewLayout`'s `onUnhandledDragOverEvent` reads
  `isActive` via context (mirrored through a ref) and skips its
  `e.accept()` when a renderer-owned drop region is being dragged
  over. Renderers install regions via `useRefDropRegion()`.

`DRAG_MIME_FXTR_REF` is owned by `@fxtr-ui/workstation-dispatch/dnd`;
`reflink/drag.ts` re-exports it for back-compat.

Chain entries are stub-bearing `PanelViewEntry = {stub, display}`
where `stub` is a connector-interned `EntityRefStub` or
`ServiceRefStub` and `display: "default" | "metadata"` selects between
the dispatched `FullPanel` renderer and the hardcoded metadata view.
The same shape is used for breadcrumb segments, follow-link targets,
and history-root prefixes. For drag payloads, which cross a
`JSON.stringify` boundary, a sister `PanelViewDragData = {kind,
shortId, display}` is used at `setDragData` / `parseDragData` time,
with `dragDataOfEntry` / `entryOfDragData(connector)` mediating.

Per-entry renderer state lives in `RefPanelChainState.storages:
unknown[]`, indexed parallel to `chain`. The metadata view and
dispatched FullPanel renderers narrow their slot through
`atomWithDefault` to their own shapes. The storages are a pure cache —
liveness rides on the chain stubs, which `collectStubsInState` walks
into Track ops on every flush.

**`src/system_renderers/`** — default dispatched renderers for fxtr
system-defined entity types. Currently just `BasicTypeBadge`:
`BasicTypeBlockLink` / `BasicTypeInlineLink` display a `FxtrBasicType`
ref as a `type {type_name}` badge. Consumers register them at id_string
`BASIC_TYPE_ID_STRING` (`"!basic_type"`) on the `BlockLink` /
`InlineLink` keys.

See
[`packages/fxtr-ui-panel-viewer/LOCAL_CODE_OVERVIEW.md`](./packages/fxtr-ui-panel-viewer/LOCAL_CODE_OVERVIEW.md)
for the mental model, important invariants (StrictMode + dockview
remount, dispatcher `"closed"`-vs-`"unavailable"`, metadata-mode
rendering branch, the viewer-owns-creation flow), and the
per-directory module guide.

Depends on `@fxtr-ui/core`, `@fxtr-ui/react-dispatch`,
`@fxtr-ui/state-util`, `@fxtr-ui/workstation-dispatch`, plus
`dockview`, `@base-ui/react`, `jotai` — **not** on any connector
package. The CSS bundle ships inside the package under `src/styles/`;
consumers do `import "@fxtr-ui/panel-viewer/styles/all.css"`.

### `@fxtr-ui/workstation` (`packages/fxtr-ui-workstation/`)

The **workstation-connector panels** for the panel viewer — reduced to
the four panels expressed as injected `PanelRegistration`s, plus their
debug tables. Depends on `@fxtr-ui/panel-viewer` and re-exports its
full surface (`export *`) for back-compat, so existing consumers that
import the shell / navigation / dispatch helpers from
`@fxtr-ui/workstation` keep resolving.

- **`RootsPanel`** — registry browser over
  `WorkstationConnector.listEntities()` / `listServices()`.
- **`WorkstationDebugPanel`** — four `listX({debug:true})` resource
  tables; its sessions-table row buttons and "Open dispatcher debug"
  button open panels via `workstation.openPanel(id, params?)`.
- **`SessionDebugPanel`** — per-session debug surface (raw
  `connection.registerSubscription`), opened as a `forSession` panel
  keyed by session id.
- **`DispatcherDebugPanel`** — renders `dispatcher.debugView()`.

`workstationPanelRegistrations` is the assembled array the demos pass
to `<PanelViewer panelRegistrations={...} />`. The session-debug
registration derives its tab title from `params.sessionId`.

Because these panels reach `WorkstationConnector` members beyond the
abstract `Connector` surface (the four `listX` lists, the raw
`connection`), they get the concrete connector via
**`useWorkstationConnector()`** — a thin hook narrowing the viewer's
`useConnector()` (sound because they only ever mount inside a
`<PanelViewer>` given a `WorkstationConnector`).

See
[`packages/fxtr-ui-workstation/LOCAL_CODE_OVERVIEW.md`](./packages/fxtr-ui-workstation/LOCAL_CODE_OVERVIEW.md).
Depends on `@fxtr-ui/core`, `@fxtr-ui/panel-viewer`,
`@fxtr-ui/react-dispatch`, `@fxtr-ui/state-util`,
`@fxtr-ui/connector-workstation`, `@fxtr-ui/workstation-dispatch`,
plus `dockview`, `jotai`.

### `@fxtr-ui/reports` (`packages/fxtr-ui-reports/`)

A 5th dispatch key (`Figure`) plus renderers for the Python-side
`RootReport` / `SubReport` entities (defined in
`projects/fxtr_report_builder/`). Builds on top of
`@fxtr-ui/workstation` — consumers compose the new renderers
into their own dispatch registry via
`createDispatchRegistry` from `@fxtr-ui/react-dispatch`.

The `Figure` key has Props `{ ownStorage: FxtrSessionAtom<unknown> }`
mirroring `FullPanel`. The default `FigureNoMatchFallback`
wraps the entity's `FullPanel` rendering in a shaded container
with a `RefLink` header, so every type "just works" as a figure
even without explicit configuration.

`ReportTreeRenderer` walks the wire-decoded MDX tree (an array
of `[type, props, children]` tuples), dispatching `RenderFigure`
nodes through the `Figure` key (threading per-figure storage
keyed by the MDX-builder-assigned `figureKey` string) and
`RenderLink` nodes through `<RefLink as="inline">`. HTML/SVG
elements are rendered via `createElement`.

See
[`packages/fxtr-ui-reports/LOCAL_CODE_OVERVIEW.md`](./packages/fxtr-ui-reports/LOCAL_CODE_OVERVIEW.md)
for the module breakdown, the consumer-side dispatch-registry
wiring example, and the layering rationale.

Depends on `@fxtr-ui/core`, `@fxtr-ui/react`,
`@fxtr-ui/react-dispatch`, `@fxtr-ui/state-util`,
`@fxtr-ui/workstation`, plus `jotai`.

### `@fxtr-ui/agent-plugin-renderer` (`packages/fxtr-ui-agent-plugin-renderer/`)

Renderer family for the agent MCP plugin's event log: a
`FullPanel` for the `agent_mcp_running_session` service type
that drives a live subscription + scrolling stream view, plus
four `StreamEntry` renderers (one per log-event payload entity
type). Consumers register the package's pre-built lookup maps
with their dispatch registry; the rest is automatic.

**Panel architecture.** `AgentMcpFullPanel` mounts an atom-effect
state machine in a `useMemo` over `[ownStorage, stub]`:

1. **`init`** — allocate a callback inbox + dispatch a
   callback-target `subscribe_to(agentServiceStub)` invoke on
   its handle (same tick; the workstation owns the subscription).
2. **`subscribing`** — wait for the subscribe invoke's ack
   (`subscribe_to` returns `None`); transition to `running`.
3. **`running`** — drain inbox events; each delivered head
   appends a fresh `(stub, allocateLoad)` entry to the panel's
   live list. The current head (when the log is non-empty)
   arrives as the subscription's **first event** — the backend
   hub's deliver-current seed, race-free per its
   no-dup-no-miss window.

The list is rendered as a scrolling column. Each entry shows a
timestamp + a `<RenderDispatch as={StreamEntry} stub={entry.event} />`
that resolves the event ref through the consumer's registered
StreamEntry renderer. Gaps (when the watcher skipped intermediate
heads) surface as a "Load missing entries" affordance that, when
clicked, splices a fresh load entry for the missing head.

At the bottom of the panel, a `<RefChipEditor>` (CodeMirror 6) +
Send button fires `send(...)` against the agent service. The
editor's source-of-truth text uses literal `@[shortid]` tokens
to mark refs; a CodeMirror `ViewPlugin` decorates each match
with a chip widget whose DOM placeholder gets a portal-rendered
`<RefLink as="inline">` from the React layer (inheriting
`RefLinkImplContext` so shell click / drag / hover wrapping
applies). Drops carrying `application/x-fxtr-ref` insert a
literal `@[shortid]` at the drop position; the wrapper div
spreads `useRefDropRegion().regionProps` so the shell's
`DockviewLayout` defers to it (no panel-open overlay).

Send-time payload shape:

- No ref tokens in the draft → `send(<bare string>)` (back-compat
  with peers that pre-date the chip feature).
- One or more ref tokens → `send({message, attached})` where
  `attached` is the ordered list of connector-interned
  `EntityRefStub` / `ServiceRefStub` for every matched shortId.
  Including stubs in `attached` pins the refs server-side for
  the lifetime of the invoke, so the agent's session can resolve
  them after the workstation routes the callback. Unknown
  shortIds are dropped from `attached` (their literal text stays
  in `message`).

Shortid disambiguation reads `useRefStubResolver()` (installed
by the shell — see `@fxtr-ui/workstation`'s
`ShellRefStubResolverProvider`). Outside a shell the editor still
mounts; chip decorations render as unresolved literal text and
the send path falls back to bare-string mode.

A status line above the input surfaces "sending: …" / "send
failed: …" feedback with a retry button on errored sends.
Successful sends round-trip to the chain as a
`workstation_inbox` callback notification, which the
`CallbackNotificationStreamEntry` renders as a "user said" card
(with inline `<RefLink>`s spliced into `message` for each
matching stub in `attached` — see Phase D).

**Per-event-type renderers.** Each is a
`DispatchedComponent<EntityRefStub, StreamEntryProps>` that uses
`useDispatchSharedAtom` + `makeLoadResultAtom` to load its event
payload, then renders a `<CollapsibleCard>` (the shared primitive
from `entries/shared.tsx`). All cards default to **closed**; the
collapsed `<summary>` shows a triangle, the event-type tag, and a
one-line summary computed per event type via `entries/summarize.ts`.

- `ScriptExecutionStreamEntry` — header ("script ran" / "script
  errored" / "script rejected"), `script_src` / `stdout` / `stderr`
  / error / rejected sections, and a non-`present_to_user` ops
  summary. When the script's `ops` include any `present_to_user`
  records, the renderer emits **two siblings inside a single
  column** — the main script block (closed) and a companion
  `<CollapsibleCard emphasis defaultOpen>` "presented to user"
  block holding each payload through `<JsonRefTree>`. The row's
  single timestamp prefix still applies to the whole column, so
  the agent's intentional outputs are visible at a glance while
  script internals are hidden until clicked.
- `InvokeResultStreamEntry` — slot identity (tag + detail) +
  one-line payload summary; expand to see the full payload
  through `<JsonRefTree>`.
- `InvokeErrorStreamEntry` — slot identity (red-tinted tag via
  `tagStyle` override) + one-line error summary; expand to see
  the full error message in error styling.
- `CallbackNotificationStreamEntry` — branches on `slot_key`:
  `workstation_inbox` → "user said" `<CollapsibleCard>` with a
  three-way payload-shape branch in the body:
  - Structured `{message: string, attached: ReadonlyArray<refs>}`
    (the send-box's chip-message format) → `<StructuredMessageBody>`
    tokenizes `message` via the shared `tokenizeMessage` helper,
    looks up each `@[shortid]` token in `attached`, and emits text
    spans interleaved with inline `<RefLink>`s. Unresolved tokens
    render as muted literal `@[shortid]`.
  - Bare string (legacy / no-chip send) → plain text body.
  - Anything else → `<JsonRefTree>` (third-party callers that
    overload `workstation_inbox`).

  Other slot keys → generic "callback notification" card with the
  payload through `<JsonRefTree>`.

**Type-id mirroring.** The package hardcodes the five Python
type-id strings (`agent_mcp_running_session` + the four
`log_*` entity types) as constants in `lookup_maps.ts`,
hand-mirrored from
`fxtr_workstation_mcp/agent_log_types.py` /
`agent_mcp_proto.py`'s `system_entities.define_type(WS_AGENT_NS,
...)` calls. Defensive consumers (the demo) can assert these
match a runtime-injected `TYPE_IDS` blob from the Python
backend's stdout.

Source modules in `src/`:

- `proto.ts` — TS-side `AgentMcpRunningSessionProto` (a
  `ServiceProto`) declaring `get_state` / `send` with typed
  `Method<...>` descriptors. Hand-mirrored from the Python
  protocol; the Python side's `subscribe` (the fxtr2
  `EventSource.subscribe` re-declaration) is deliberately not
  declared — the frontend passes the agent service as the
  event-source *arg* of its inbox's `subscribe_to` instead of
  invoking it directly.
- `data_shapes.ts` — TS interfaces for the wire-decoded forms of
  `WsAgentLogEventChain`, `ScriptExecutionLogEvent`,
  `InvokeResultLogEvent`, `InvokeErrorLogEvent`,
  `CallbackNotificationLogEvent`. The `ScriptOpRecord` flat
  dataclass surfaces as a discriminated union (one variant per
  `kind`) so consumer code narrows cleanly.
- `state_machine.ts` — `PanelState` + `PanelPhase` types,
  `INITIAL_PANEL_STATE`, and `makeAgentPanelDriver(panelStateAtom,
  serviceStub)` returning an `atomEffect`-backed driver atom.
- `panel.tsx` — `AgentMcpFullPanel` (the `FullPanel` renderer),
  `EntriesStream` / `EntryRowWithGap` / `StreamEntryRow`
  subcomponents.
- `send_box/` — text input + Send button + status line.
  - `send_box/index.tsx` — `<SendBox>` component; owns the
    payload-building logic (`buildSendPayload`) that consults
    `useRefStubResolver()` to mint stubs for `attached` and
    picks bare-string vs `{message, attached}` based on whether
    the draft contains chips.
  - `send_box/ref_chip_editor.tsx` — `<RefChipEditor>`, the
    CodeMirror 6 wrapper. Plain text doc, controlled
    value/onChange, Enter-to-submit keymap, drop handler that
    inserts `@[shortid]` text at the drop position, drag-over
    highlight, subscription to the chip widget registry for
    portal rendering.
  - `send_box/chip_widget.ts` — `RefChipWidget` (CodeMirror
    `WidgetType`) + module-local `widgetRegistry`. Each widget
    registers its DOM placeholder at construction and
    unregisters at destruction; the React layer subscribes to
    the registry and mounts a portal for each entry.
  - `send_box/chip_content.tsx` — React content rendered into
    each chip placeholder; reads `useRefStubResolver()` and
    forwards to `<RefLink as="inline">` (or a muted unresolved
    fallback when the shortId can't be classified).
  - `send_box/parse_refs.ts` — `REF_TOKEN_RE` +
    `tokenizeMessage(doc)` + `collectShortIds(doc)`. Shared
    with the receive-side rendering in
    `CallbackNotificationStreamEntry` (Phase D).
- `load_more.tsx` — the "Load missing entries" button + its
  splice updater.
- `load_effect.ts` — local copy of `makeLoadResultAtom` (the
  "allocate-load + observe result" helper); inlined rather than
  depending on `@fxtr-ui/reports` for a 30-line utility.
- `entries/shared.tsx` — common card chrome + timestamp
  formatter shared across the per-event-type renderers, plus the
  `<CollapsibleCard>` primitive used by every entry: a
  `<details>`/`<summary>` wrapper with custom triangle, inline
  one-line summary (visible only while collapsed), optional
  `tagStyle` override (used by `invoke_error` to red-tint its
  tag), and an `emphasis` accent-bordered variant (used by the
  `script_execution` "presented to user" companion block).
- `entries/summarize.ts` — one-line summary helpers per event
  type (`summarizeScript`, `summarizeInvokeResult`,
  `summarizeInvokeError`, `summarizeCallbackNotif`) + a generic
  `summarizeValue(v: ResultValue)` short-form repr (primitives by
  value, refs as `<entity shortId>` / `<service shortId>`, lists
  as `list[N]`, dicts as `{K keys}`). Targeted at glance-level
  collapsed-row signals.
- `entries/script_execution.tsx`,
  `entries/invoke_result.tsx`, `entries/invoke_error.tsx`,
  `entries/callback_notification.tsx` — one per event payload
  type. All four wrap their bodies in `<CollapsibleCard>`;
  `script_execution` splits into two siblings when
  `present_to_user` ops are present.
- `lookup_maps.ts` — `AGENT_PLUGIN_TYPE_IDS` (the five
  hand-mirrored type-id strings) +
  `agentPluginFullPanelServiceMap` (for the workstation's
  `FullPanel` key) + `agentPluginStreamEntryEntityMap` (for the
  `StreamEntry` key).
- `index.ts` — public re-exports.

Tests in `tests/`:

- `_mock_session.ts` — `TestEntityRefStub` / `TestServiceRefStub`
  / `TestOpResultStub` / `TestCallbackInboxStub` /
  `TestCallbackHandleStub` plus `makeMockSessionAtom` with
  controllable `allocateCallback` / `allocateInvoke` /
  `allocateLoad` queues.
- `state_machine.test.ts` — 7 tests covering each phase
  transition: `init → subscribing` allocates the inbox + invokes
  `subscribe_to` on its handle (the agent service riding as the
  event-source arg); `subscribing → running` on the ack, with
  the current head arriving as the first inbox event; errored
  subscribe transitions to `errored`; `running` appends an entry
  per delivered inbox event and slices the inbox; idle when no
  events; errored is terminal.
- `summarize.test.ts` — pure-function tests for the per-event-type
  summary helpers (`summarizeScript`, `summarizeInvokeResult`,
  `summarizeInvokeError`, `summarizeCallbackNotif`) + the generic
  `summarizeValue` + `truncate` utility. Covers primitives, refs,
  containers, error / rejected / present_to_user branches, and the
  workstation_inbox structured / bare-string / generic dichotomy.
- `parse_refs.test.ts` — `tokenizeMessage` round-trips: plain
  text, single ref, multiple refs, adjacent refs without spurious
  text tokens, rejection of disallowed shortId chars, charset
  acceptance (`._:-`), no over-match on trailing `@[`, duplicate
  preservation. `collectShortIds` ordered-extraction smoke.
- `send_payload.test.ts` — `buildSendPayload` (extracted from
  `<SendBox>`'s `onSend`) driven through the mock session atom
  with a fake `RefStubResolver`. Asserts: no-chip drafts emit
  bare-string `args`; chip drafts emit `{message, attached}`;
  entity / service / mixed kinds preserved in order;
  same-shortId refs are interned; unresolved shortIds drop from
  `attached` with a `console.warn`; all-unresolved → bare-string
  fallback; null resolver (outside a shell) → bare-string
  fallback.

Depends on `@fxtr-ui/core`, `@fxtr-ui/rd-types-stream`,
`@fxtr-ui/react`, `@fxtr-ui/react-dispatch`,
`@fxtr-ui/state-util`, `@fxtr-ui/workstation-dispatch`, plus
`jotai`, `jotai-effect`, and the trio of CodeMirror 6 packages
`@codemirror/state`, `@codemirror/view`, `@codemirror/commands`
(used only by the send-box editor; the rest of the package is
React + jotai). Peer-depends on `react`. Does NOT depend on
`@fxtr-ui/workstation` (the shell) — the package can be
consumed in any panel-viewer context that supplies a
`FullPanelProps.ownStorage` slot.

### `@fxtr-ui/demo` (`packages/fxtr-ui-demo/`)

A Vite-based end-to-end demo of the `@fxtr-ui/*` stack against a
real workstation backend. Depends on all four sibling packages plus
`vite`, `react`, `tsx`. Not a library — never published; lives here
as the manual smoke test for the stack.

Run via `pnpm dev` from the package directory. The orchestrator:

- `src/server.ts` — `tsx`-runnable Node script. Spawns
  `src/demo_workstation.py` via `uv run`, reads its `READY <ws-url>`
  line, then starts Vite's dev server (port 3461) with
  `import.meta.env.VITE_WORKSTATION_URL` set to the workstation's
  `/client` URL. SIGINT / SIGTERM tears down both halves.
- `src/demo_workstation.py` — boots a `WorkstationServer`, registers
  a trivial `_Note` entity under name `demo-note` and a tiny
  `_DemoServiceImpl` under name `demo-service` (with `echo` /
  `combine` / `fail` methods), prints `READY`, parks until killed.

UI modules in `src/client/`:

- `main.tsx` — React entry; mounts `<App />` under `StrictMode`.
- `App.tsx` — instantiates `WorkstationConnector` against
  `VITE_WORKSTATION_URL`, wraps the tree in a jotai
  `<Provider store={connector.store}>` (connector-scope), tracks
  open panels with local component state, renders the layout
  (status bar across top, sidebar left, panel grid filling the
  rest).
- `ConnectionStatusBar.tsx` — colored badge bound to
  `connector.connectionStatus`, plus the workstation URL.
- `SessionSidebar.tsx` — live list from `connector.listSessions()`
  + "New session" button calling `connector.createSession({})`,
  per-row "open" button calling `connector.attachSession(id)`.
- `Panel.tsx` — one panel = one session. Wraps
  `<FxtrSessionProvider session={...}>`. The root drives state
  through `useSessionRootStateAtom<PanelUserState>({ invokes: [] })`,
  lifting the session's nullable userState atom to a typed non-null
  `FxtrSessionAtom<PanelUserState>`. From there, three `useSubAtom`
  calls (`"message"` / `"loaded"` / `"invokes"`) carve the tree into
  slices; reads + writes are vanilla `useAtomValue` / `useSetAtom`.

  PanelUserState shape:
  ```
  { message?, loaded?, invokes: ReadonlyArray<InvokeSlot> }
  InvokeSlot = { id: number, label?, result?: OpResultStub }
  ```

  The "load demo-note" button writes through `loadedAtom` via a
  function-form updater that calls
  `ctx.allocateLoad(loadTarget.ref)` — same-tick allocate-and-store
  is enforced by construction (ctx only exists inside an updater
  function). The "+ new invoke" button appends an empty
  `InvokeSlot` to `invokesAtom`. Each slot is rendered by
  `<InvokeCard idx={idx} invokesAtom={…} />`; sibling cards are
  isolated. The serviceRef is typed via `_DemoServiceProto`, so
  `ctx.allocateInvoke(svc, "echo", [42])` checks the method name +
  arg shape + return type at compile time. When a slot's `result`
  is set, an `<OpResultDisplay label=… stub={…} />` sub-component
  reads `useResult(stub)` and renders the discriminated union
  (pending / filled / errored).

  Invokes are explicitly *not* deduped — every click mints a fresh
  slot, every click produces a new server-side call. Loads dedupe
  at the wire layer via the workstation's `loadDedup` map: two
  panels clicking load for the same short_id share one preserved
  fill. Two panels on the same `session_id` (via two
  `attachSession` calls) coordinate via the server.
- `_demo_service_proto.ts` — TS protocol shape mirroring the
  Python `_DemoServiceProto` (echo / combine / fail). Used to type
  the demo's `serviceRef` so invoke calls check at compile time.
  Mirrors the Python side by hand; a contract test or shared-schema
  generator is future work.

The demo is the manual smoke test for the React layer wiring;
runtime verification is "load the page in a browser and exercise
the flow."

### `@fxtr-ui/dispatch-demo` (`packages/fxtr-ui-dispatch-demo/`)

End-to-end demo of the dispatch package. Boots its own
workstation backend with multiple distinct entity types
(`Note`, `Photo`) and service types (`echo`, `counter`), then
renders each registered entity / service as a dispatched card.
Each card is rendered twice in a side-by-side layout to
demonstrate per-`(stub, key)` shared-state coordination —
clicking "+1" on the left counter card updates the right one
because both share the same `useDispatchSharedAtom` slot.

Runs on port 3462 (vs. `@fxtr-ui/demo`'s 3461) so both demos
can run in parallel against different workstation processes.

Orchestrator (`src/server.ts`) extends the basic spawn-Python-
then-Vite pattern with a `TYPE_IDS <json>` line on the Python
stdout protocol — the type-id strings of the registered entity
and service type entities are emitted from Python (via
`FxtrID.id_string()`) and threaded through to the TS dispatch
registry as `import.meta.env.VITE_DISPATCH_TYPE_IDS`.

UI modules in `src/client/`:

- `App.tsx` — top-level. One module-level `WorkstationConnector`,
  one dispatcher session created via `connector.createSession({})`
  on mount, immediately wrapped in
  `createStaticDispatcher({session, registry})`. The dispatcher
  goes into `<DispatcherProvider dispatcher>` around the grid.
  StrictMode-safe cleanup goes through `dispatcher.detach()`
  (which detaches the session too, per the ownership-transfer
  model). Single jotai store (the connector's default) for the
  whole tree, satisfying the dispatch system's single-store
  assumption.
- `DispatchGrid.tsx` — reads `connector.listEntities()` /
  `listServices()` and renders each ref via `<RenderDispatch
  as={CardKey} stub={...}/>` in two parallel columns ("mount A"
  / "mount B") so the shared-state behavior is visible.
- `dispatch_keys.ts` — declares `CardKey` (no extra props beyond
  `stub`, declared with `Props = object`).
- `dispatch_registry.ts` — assembles the `DispatchRegistry`
  from the type-id strings the orchestrator injected. Uses
  `staticEntityTypeDispatch` + `staticServiceTypeDispatch` to
  build the entity-side and service-side entries.
- `cards/NoteCard.tsx` / `PhotoCard.tsx` — entity cards. The
  static entity dispatcher drops its load slot when it
  transitions to terminal `resolved`, so each card re-issues a
  load to render the entity's contents. They use the canonical
  pattern: `useDispatchSharedAtom<OpResultStub<T> | null>(null)`
  + `useMemo(() => makeLoadResultAtom(sharedAtom, stub))` +
  `useAtomValue` (the helper lives in `@fxtr-ui/state-util`).
  Renders `title`/`body` (note) or `caption`/image (photo).
- `cards/EchoServiceCard.tsx` / `CounterServiceCard.tsx` —
  service cards. Hold the last `OpResultStub` from an invoke in
  `useDispatchSharedAtom`; clicking a button writes a fresh
  invoke result to that slot, which both column-mounts of the
  card display together.
- `cards/CardFallback.tsx` — generic fallback shown while the
  dispatcher's state machine is still resolving. Uses
  `useDispatchProgressMessage()` to display the helper's
  status text ("loading entity…", "resolving renderer…").
- `cards/CardErrorFallback.tsx` — generic error fallback shown
  when the dispatched card throws (or when the static helper's
  `errored` synthetic-throw fires).

Python backend in `src/dispatch_demo_workstation.py` defines
two `DataclassEntity` types (`_Note`, `_Photo`) with distinct
`fxtr_type=` parameters, two `TypedServiceImpl` services with
distinct `_service_type_id`s (`_EchoServiceImpl`,
`_CounterServiceImpl`), exposes a handful of each, and emits
the type-id-string map as the `TYPE_IDS` stdout line.

Run via `pnpm dev` from the package directory. As with
`@fxtr-ui/demo`, runtime verification is "load the page in a
browser and exercise the flow."

### `@fxtr-ui/dynamic-dispatch-demo` (`packages/fxtr-ui-dynamic-dispatch-demo/`)

End-to-end demo of the dynamic dispatcher. Boots its own
workstation backend with two entity types (`Note`, `Photo`),
mounts a `BasicDynamicDispatchImpl` as the
`renderer-dispatcher` external service, and serves **two
routes** through Vite:

- **Default route** (`/`) — the legacy `<DispatchGrid>` view:
  each registered entity rendered through a single `CardKey`
  dispatch slot. Hand-built renderer templates under
  `src/renderers/<label>/index.tsx` export
  `{ default: { [CARD_KEY_UUID]: <Component> } }` keyed off the
  demo's CardKey uuid. Used by the existing
  `dynamic_integration.test.ts` against the live workstation.
- **Workstation-shell route** (`/shell`) — mounts
  `<PanelViewer>` against the same dynamic dispatcher.
  The renderer templates additionally export entries under the
  four `@fxtr-ui/workstation-dispatch` dispatch-key UUIDs
  (`FullPanel` / `BlockLink` / `InlineLink` / `HoverPreview`)
  so the shell's `<RenderDispatch>` calls resolve through the
  dynamically-loaded modules. Provides a manual smoke test of
  the workstation-shell-via-dynamic-dispatcher path including
  the dispatcher-debug panel.

The Python backend uses `mount_basic_dynamic_dispatch` for
the impl + a background mtime-polling task that calls
`impl.recompile_changed_and_reload()` periodically (default
15s; CLI/env-overridable for tests). The mtime poller is the
dev-iteration ergonomics layer — touch a renderer template
and the running browser sees the update within one poll
cycle. The poller is intentionally not under test (the
reload-trigger contract is exercised at the unit-test level
in `BasicDynamicDispatchImpl`'s tests; the deterministic
reload-reception integration test
(`dynamic_reload.test.ts` in `@fxtr-ui/react-dispatch`) uses
a separate stdin-driven harness backend instead).

The TS client's `main.tsx` calls `installFxtrExternals` with
the v1 externals list — including
`@fxtr-ui/workstation-dispatch` so the workstation-shell route's
runtime-loaded renderers share the host's `FullPanel` /
`BlockLink` / etc. identities.

Stdout protocol: `TYPE_IDS {...}` / `RENDERER_DISPATCHER_NAME
renderer-dispatcher` / `READY ws://...` / `DEBUG_UI
http://...`, consumed by `src/server.ts` and forwarded to
Vite as `VITE_*` env vars.

### `@fxtr-ui/workstation-demo` (`packages/fxtr-ui-workstation-demo/`)

End-to-end demo of the `@fxtr-ui/workstation` shell. Boots its
own workstation backend, registers a small set of demo entity
types (`_DemoNote`, `_DemoListNode`, plus a `RootReport` + a
`SubReport` built via `fxtr_report_builder`'s MDX helpers) and
three toy services (`echo`, `counter`, `notify_n`) via peer-RPC
self-loopback under external names, then mounts
`<PanelViewer>` against the live backend. The client
constructs its `DispatchRegistry` manually via
`createDispatchRegistry` from `@fxtr-ui/react-dispatch` so it
can configure all five keys — the workstation's four plus
`@fxtr-ui/reports`'s `Figure`. Per-type renderers are pulled in
from `./handlers` (demo entities + services) and from
`@fxtr-ui/reports` (report + sub-report renderers across all
five keys). `BlockLink` and `InlineLink` also register
`BasicTypeBlockLink` / `BasicTypeInlineLink` from
`@fxtr-ui/workstation` at `BASIC_TYPE_ID_STRING`, so every
entity's `$fxtr.type` ref surfaces as a `type {name}` badge.
The `Figure` slot for `_DemoNote` is intentionally
unregistered so the demo report's `<RenderFigure entity={note}/>`
exercises the default `FigureNoMatchFallback` path.

The demo's `App.tsx` owns the dispatcher-session lifecycle —
reads `readDispatcherSessionId()` synchronously on mount,
either `connector.attachSession(id)` (sync) or `await
connector.createSession({role: "dispatcher"})` (async +
`writeDispatcherSessionId`), wraps the session in
`createStaticDispatcher({session, registry})`, and passes the
dispatcher to `<PanelViewer dispatcher={…} onClearAndReload={…}>`.
StrictMode-safe via a cancel flag: a cancelled in-flight
construction immediately detaches the dispatcher (which
detaches the session too, per the ownership transfer model).
The `onClearAndReload` callback clears the persisted id and
calls `window.location.reload()` so the next mount creates a
fresh session.

Same orchestrator shape as `@fxtr-ui/dispatch-demo`: Python
process emits `TYPE_IDS <json>` + `READY <ws-url>` +
`DEBUG_UI <http-url>`, Node side spawns Vite on port 3463 with
those values injected as `VITE_*` env vars.

Run via `pnpm dev` from the package directory. Runtime
verification is "load the page in a browser and exercise the
flow" — same as the other demos.

## Consumer-level demos (outside `frontend_lib/`)

The packages under `frontend_lib/packages/*-demo/` are
**library-level** demos — each tied to one `@fxtr-ui/*`
package's surface, used to smoke-test the package end-to-end.
The **consumer-level** demo — bringing the workstation + the
agent MCP plugin + the agent-plugin renderer together as one
operator-facing experience — lives outside the workspace at
[`projects/fxtr_workstation_demo/`](../../fxtr_workstation_demo/).

It rides in the same pnpm workspace via a workspace yaml entry
for `frontend/` so its `workspace:*` deps on `@fxtr-ui/*`
resolve cleanly, but the Python backend + sample agent workdir
+ persistence-cap data live alongside the frontend as siblings
(flat layout) rather than as another `frontend_lib/packages/`
entry. See that directory's `README.md` for the operator setup.

## Plans / design notes

The current frontend-lib design and the planned future work live in
[`../plans/phase0/part3_frontend_ui/`](../plans/phase0/part3_frontend_ui/):

- [`overview.md`](../plans/phase0/part3_frontend_ui/overview.md) —
  the staged roadmap (foundations → entity loading → React bindings
  → HTTP connector → invokes → callbacks → dispatch →
  workstation-core rewrite).
- [`stage2_entity_loading.md`](../plans/phase0/part3_frontend_ui/stage2_entity_loading.md) +
  [`stage2_post1_wire_load_dedup.md`](../plans/phase0/part3_frontend_ui/stage2_post1_wire_load_dedup.md)
  — design and implementation notes for the slot machinery.
- [`stage6_callback_inboxes.md`](../plans/phase0/part3_frontend_ui/stage6_callback_inboxes.md)
  — design decisions and invariants for the callback-inbox
  machinery: cursor-as-cursor identity, `firstIndex` derived from
  committed-state min-offset, trim-on-commit, the send-side handle
  as a separate stub kind, cross-session smuggling handling, and
  the `["broken_inbox", 0]` wire convention.
- [`design_notes/01_reactive_model.md`](../plans/phase0/part3_frontend_ui/design_notes/01_reactive_model.md),
  [`design_notes/02_connector_abstraction.md`](../plans/phase0/part3_frontend_ui/design_notes/02_connector_abstraction.md),
  [`design_notes/04_session_state_layout.md`](../plans/phase0/part3_frontend_ui/design_notes/04_session_state_layout.md)
  — the conceptual model behind the reducer and state shape.

The plans are the canonical source for "why is it like this" and
"what's still TODO." This file describes what's there now.
