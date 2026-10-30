# 02 — Connector abstraction

Builds on `01_reactive_model.md`. Defines the surface a session
connector exposes to the rest of the world, and how the HTTP and
workstation backends both fit it.

## Decision

There is **one** session-level interface. Both the HTTP and
workstation connectors implement it. The HTTP connector runs as a
"degenerate session": a session-shape object whose only meaningful
op is `LoadEntity`. Anything that would require service refs,
callbacks, or invokes simply can't be requested against an HTTP
session because no `ServiceRefStub` or `CallbackInboxStub` will ever
exist inside its state — the user has no way to construct one
without a workstation in the loop. If a stub from a foreign session
somehow ends up in HTTP-session state, the connector raises.

This means panel code can be backend-agnostic at the type level:
"give me an entity and let dispatch render it" works against either
backend. Active interaction (invokes, callbacks) naturally only
works against the workstation, and that's enforced by *what stubs
can exist*, not by branching on connector kind.

## Connector vs. session

There's a one-to-many relation here:

- A **connector** is the underlying transport / lifecycle handle.
  - HTTP connector = `FetchFn` + base URL + scope of cached entity
    arenas.
  - Workstation connector = single shared websocket + the
    workstation-level subscriptions (rpc_connections, services,
    entities, sessions).
  - Connectors have connector-specific shapes, there is not a shared
    interface for connectors. Rather, each connector gives you a
    session, and all generic code assumes it is running on top of a
    session.
- A **session** is one atom + its slot machinery.
  - HTTP session = degenerate, single per connector (or multiple if
    we want isolated caches), no wire-level identity.
  - Workstation session = a real workstation session UUID, with
    explicit create / subscribe / mutate / close.

Open: do we let multiple HTTP sessions coexist on one connector?
Probably yes — useful for "this panel is an isolated read context"
parity with workstation panels.

## Surface (sketch)

```ts
interface WorkspaceConnector {
  listSessions?(): ReactiveList<SessionRecord>;
  listRpcConnections?(): ReactiveList<RpcConnectionRecord>;
  listServices?(): ReactiveList<ServiceRecord>;
  listEntities?(): ReactiveList<EntityRecord>;
  createSession(metadata): Session;
  attachSession(session_id): Session;
}

// TODO: some HTTP connector. Simpler because it does not require any
// remote connection state.

interface Session {
  readonly atom: Atom<SessionView>;          // the source-of-truth atom
  readonly snapshot: SessionView;            // sync read of current value

  // Imperative slot allocation. Returns an OpResultStub<...> that the
  // caller stores into state to actually fire the op.
  allocateLoad<T>(stub: EntityRefStub<T>): OpResultStub<T>;
  allocateInvoke<T>(svc: ServiceRefStub, m: string, args: ...): OpResultStub<T>;
  allocateCallback<R>(): CallbackInboxStub<R>;

  // Bracketed transaction primitive? (see 01 OQ-3).
  transaction<T>(fn: (tx: TxHandle) => T): T;

  close(): Promise<void>;
}
```

`SessionView` is the structured state tree from 01 plus the merged
slot-fill projection.

## HTTP connector behavior

- `allocateLoad` works. The connector translates this into a fetch
  for the entity's data; result slot fills with the decoded entity.
- `allocateInvoke` raises (`InvokeNotSupported` or similar). No
  service stubs can exist in an HTTP session, so this path should
  only be hit if the user manufactured a stub manually.
- `allocateCallback` raises.
- The "wire" is conceptually a no-op: the JSON patch + ops bundle
  is interpreted locally, with `LoadEntityOp` translated to a fetch.
  The committed state is stored in memory; restart loses it (fine
  for HTTP).

We can frame the HTTP connector as "implements the same reducer,
but the 'send to wire' step is replaced by 'execute locally
against an HTTP fetcher' for the load-entity case." That keeps the
reducer logic shared between connectors.

## Open questions

### OQ-A — HTTP entity entry point

Without sessions on the wire, how do you enter? Plausible shapes:
- `createSession({ pin: "demo/section" })` — connector resolves the
  pin and seeds the session's state with `{ root: EntityRefStub }`.
- `createSession({ entityIdString: "..." })` — direct entry by ID.
- A namespace-shaped abstraction layered above sessions: the HTTP
  connector provides `listPins()` + a way to create a session
  rooted at a pin, mirroring how `NamespacePanel` works today.

### OQ-B — Where does fxtr CBOR decoding live?

For HTTP, `LoadEntityOp` produces an entity data blob that is fxtr
CBOR (the existing `/data/:id_string` response). For workstation,
it produces JSON-list-escape via the wire codec. Both need to land
as the same `OpResultStub<T>` value type. So the connector layer
has two decoders that converge on the same in-memory shape. The
schema system has to support both wires.

This is what `04_schema_system.md` will pin down.

### OQ-C — Multi-session reactivity scoping

Browser clients will run many sessions concurrently (one per
panel). Do we want one big "connector store" with all sessions
keyed by id, or each session a standalone atom?

Standalone seems right; the connector can expose a `listSessions`
atom that's separate from any individual session atom.

### OQ-D — Reconnect / resume

The workstation's `subscribe_session` already replays the last
committed state + the unacked-suffix events. So on reconnect we
re-subscribe and the state pops back into shape. The atom should
absorb this without component-visible glitching where possible.

Two-phase logic:
1. On reconnect, the local atom freezes (read-only) until
   re-subscription completes.
2. On `session_snapshot` arrival, we reconcile: if our pending
   patch can still apply against the new committed state, retry
   it; otherwise, drop it and surface a rejection.
