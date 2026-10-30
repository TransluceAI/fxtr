# Stage 10a — Dispatcher refactor

Preparation for the dynamic dispatcher in stage 10b. Pivots the
dispatch system from "registry-as-data + provider-owns-runtime-
state" to "Dispatcher instance owns everything".

Conceptually, today's `DispatchKeyRegistryEntry` is a recipe; the
runtime atoms, the dispatcher's userState projection chain, the
per-`(stub, key)` entry cache, and the in-memory progress / component
atoms all live in `<DispatchProvider>` / `createDispatchEntry`.
After this refactor, all of that moves into a `Dispatcher` instance
the consumer constructs and threads through.

The motivating constraint: the dynamic dispatcher (10b) needs to
maintain **cross-entry caches** that don't fit cleanly into the
current per-entry shape — a session-backed `type_id_renderer_cache`
spanning many entries, an in-memory ESModule cache, dedup of
concurrent dispatch-service invokes. Folding all storage into a
dispatcher instance lets us add those naturally; trying to layer
them on top of the current shape would require special-cased
provider plumbing every time.

## High-level model

The consumer constructs a dispatcher at app startup and threads it
through:

```ts
const dispatcher: Dispatcher = createStaticDispatcher({
  session: dispatcherSession,
  registry,
});
```

```tsx
<WorkstationShell connector={connector} dispatcher={dispatcher} ... />
// or, for non-shell consumers:
<DispatcherProvider dispatcher={dispatcher}>
  <RenderDispatch stub={...} as={CardKey} />
</DispatcherProvider>
```

The dispatcher owns:

- Its session (ownership transferred at construction;
  `dispatcher.detach()` detaches the session as part of its cleanup).
- All session-backed state (the `userState` projection chain that
  currently lives in `buildDispatcherRootAtom` + `createDispatchEntry`).
- All in-memory state (per-entry component / progress atoms; for the
  dynamic dispatcher, cross-entry caches too).
- The per-`(stub, key)` entry cache.
- Its own state-machine implementations (the static dispatcher still
  consumes a registry of effect-fn factories; the dynamic dispatcher
  in 10b has its own built-in state machine, not a registry of
  user-supplied ones).

`<DispatcherProvider>` becomes a thin React context wrapper.
`<RenderDispatch>` reads the dispatcher from context, calls
`dispatcher.getEntry(key, stub)`, subscribes to the entry's
`componentAtom`, and renders.

## Key design decisions

- **Dispatcher owns the session.** `createStaticDispatcher({session, ...})`
  takes a `Session` and assumes lifecycle ownership from that point.
  `dispatcher.detach()` detaches the session as part of its cleanup;
  consumers must NOT call `session.detach()` directly after handing
  it to a dispatcher. The single ownership point eliminates the
  coordination footgun of two-cleanup-call ordering and avoids
  silent-bug states where one is called and the other isn't.

  Session-id persistence (the localStorage write of `sessionId` so
  reloads re-attach the same dispatcher session) stays the consumer's
  concern — that's app-level config, not lifecycle coordination. The
  consumer writes the id between `createSession` resolving and
  handing the session to the dispatcher.

  The workstation shell stops owning dispatcher session lifecycle in
  this stage; consumers (currently just the demo App) acquire the
  dispatcher session, transfer it to a dispatcher, and pass the
  dispatcher to the shell. The shell never sees a raw `Session`.

- **Dispatcher exposes its own status.** `dispatcher.status: Atom<DispatcherStatus>`
  abstracts over "is the dispatcher ready to serve `getEntry`
  requests." For the static dispatcher this is just a thin renaming
  over `session.lifecycle`; for the dynamic dispatcher (10b) it can
  ALSO reflect "dispatch service resolved" state. Consumers (the
  shell's ShellLoading / DispatcherUnavailable branches, the debug
  panel) branch on this atom rather than reading `session.lifecycle`
  directly.

- **`<WorkstationShell>` takes a `dispatcher` prop.** Required, not
  optional. The shell's existing `useState<WorkstationSession | null>`
  + acquire-via-useEffect block moves out to the consumer. The
  workstation-demo's App.tsx grows the lifecycle effect inlined; if a
  second consumer needs the same wiring later we can lift a
  `useDispatcherSession(connector, opts)` hook into a package, but
  10a doesn't add it.

- **Component-atom reset is a `Dispatcher` invariant.** "Writing a
  different value to `componentAtom` also writes `null` to
  `sharedComponentStateAtom` in the same flush" is part of the
  dispatcher contract, enforced by a shared base-entry helper inside
  the package. Concrete dispatchers (static, dynamic) consume that
  helper rather than re-implementing the wrapping.

- **Registry stays a passive bag.** The data types `DispatchKey`,
  `DispatchRegistry`, `DispatchKeyRegistryEntry`, the `entityEntry` /
  `serviceEntry` / `createDispatchRegistry` builders, and the
  `staticEntityTypeDispatch` / `staticServiceTypeDispatch` factories
  keep their existing shapes. They feed into `createStaticDispatcher`
  instead of `<DispatchProvider>`. The dynamic dispatcher won't
  consume a registry — it takes a service-name string instead.

- **`DispatchEntry` field renames.** External-facing fields renamed
  for clarity:
  - `outputAtom` → `componentAtom` (read-only externally).
  - `sharedAtom` → `sharedComponentStateAtom`.
  - `progressStateAtom` → `dispatchInternalStateAtom` (read-only).
  - `progressMessageAtom` → `dispatchProgressMessageAtom` (read-only).

  Consumer hooks keep their names (`useDispatchSharedAtom`,
  `useDispatchProgressState`, `useDispatchProgressMessage`); only the
  fields they read are renamed.

- **External `componentAtom` is read-only.** The renamed entry field
  is `Atom<Component>`. The dispatcher's internal writable wrapper
  (the one the state machine writes through, and the one that
  enforces the shared-state reset on value-change) is a private
  implementation detail. The effect-fn factory contract for static
  dispatchers still receives a writable handle, just named
  `componentAtom` inside the factory's parameter scope. The external
  read-side is what `<RenderDispatch>` subscribes to.

## Public API sketch

```ts
// src/dispatcher.ts — the abstract surface
interface Dispatcher {
  readonly status: Atom<DispatcherStatus>;
  readonly session: Session;     // read-only inspection (sessionId / userState / lifecycle); DO NOT call .detach() / .terminate()
  getEntry(
    key: DispatchKey<object, RefKind>,
    stub: EntityRefStub | ServiceRefStub,
  ): DispatchEntry<...>;
  detach(): Promise<void>;        // flips status to "closed", drops caches, awaits session.detach(); idempotent
}

type DispatcherStatus =
  | "connecting"
  | "connected"
  | "stale"
  | "closed"
  | "unavailable";

interface DispatchEntry<Stub, Props> {
  readonly id: string;
  readonly stub: Stub;
  readonly componentAtom: Atom<DispatchedComponent<Stub, Props>>;
  readonly sharedComponentStateAtom: FxtrSessionAtom<StateValue | null>;
  readonly dispatchInternalStateAtom: Atom<unknown>;
  readonly dispatchProgressMessageAtom: Atom<string>;
  readonly ErrorFallback: ErrorFallbackComponent<Stub, Props>;
}

// src/static_dispatcher.ts
function createStaticDispatcher(opts: {
  session: Session;
  registry: DispatchRegistry;
}): Dispatcher;
```

`DispatchKey`, `DispatchRegistry`, `DispatchKeyRegistryEntry`,
`entityEntry` / `serviceEntry` / `createDispatchRegistry`, and the
`staticEntityTypeDispatch` / `staticServiceTypeDispatch` factories
all keep their existing shapes unchanged. They become input to
`createStaticDispatcher`.

## Internal shape (static dispatcher)

The static dispatcher's internals match what
`<DispatchProvider>` + `createDispatchEntry` build today, just moved
inside one object:

- **userState shape**: `{entries: Record<keyUuid, Record<shortId, EntryUserStateSlot>>}`
  — same as today. The dispatcher's constructor calls
  `buildDispatcherRootAtom(session)` once and holds the result.
- **Per-entry atoms**: built by a shared `createBaseEntry({stub,
  stateAtom, sharedComponentStateAtom, dispatchProgressMessageAtom,
  initialComponent, effectFactory, ErrorFallback})` helper:
  - Builds the writable `componentAtom` wrapper with the
    shared-state reset invariant.
  - Mounts the registry-supplied effect-fn factory and wraps the
    result with `atomEffect` from `jotai-effect`.
  - Returns the public `DispatchEntry` (with `componentAtom` typed as
    `Atom<Component>` — the writable is captured in closure for the
    internal effect-fn factory contract).
- **Per-`(keyUuid, shortId)` entry cache**: `Map<string, DispatchEntry>`,
  same key shape as today (`${keyUuid}::${shortId}`).
- **Status atom**: a derived `atom((get) => closed ? "closed" : get(session.lifecycle))`
  — the static dispatcher adds no states beyond the session's own,
  except that an explicit `dispatcher.detach()` flips to `"closed"`
  unconditionally (so a closed-by-detach dispatcher reports "closed"
  even if the underlying session is mid-drain).
- **detach()**: idempotent async cleanup — sets the internal
  `closed` flag (so `status` flips and future `getEntry` throws),
  clears the entry cache, then `await this.session.detach()`. The
  cache-clear is a GC nicety for the static case (entries hold no
  out-of-session resources); for the dynamic dispatcher in 10b the
  same hook releases in-memory ESModule caches and aborts in-flight
  bytes-loads.

## DispatcherProvider + RenderDispatch

- Rename `<DispatchProvider session registry>` →
  `<DispatcherProvider dispatcher>`. Hard rename (no deprecated
  alias; the package is private to the workspace and we touch all
  call sites in this stage).
- The provider's React context exposes the `Dispatcher` directly. No
  more `getEntry` on the context — `<RenderDispatch>` calls
  `dispatcher.getEntry(...)` directly.
- `<RenderDispatch>` reads the dispatcher, calls
  `dispatcher.getEntry(key, stub)`, subscribes to
  `entry.componentAtom`, renders inside the same wrapper stack
  (entry context, null `SessionContext`, error boundary).
- `DispatchEntryContext` keeps its shape; the entry object handed
  through it has the renamed fields.

## WorkstationShell

- `<WorkstationShell>` adds a required `dispatcher: Dispatcher` prop.
- Removes the `useState<WorkstationSession | null>` +
  acquire-via-useEffect block.
- Removes the `WaitForDispatcherReady` component; replaces with a
  `dispatcher.status`-driven `<ShellLoading>` / `<DispatcherUnavailable>`
  gate that branches on `"connecting" | "stale" | "unavailable" |
  …`. Same UX as today, different read source.
- The session-debug-panel surface that displays the dispatcher
  session's id / state reads `dispatcher.session` and goes through
  the normal session-debug subscription path.

## Demos

- `@fxtr-ui/dispatch-demo` App.tsx: replaces the inline
  `<DispatchProvider session={session} registry={registry}>` with
  `createStaticDispatcher({session, registry})` +
  `<DispatcherProvider dispatcher>`.
- `@fxtr-ui/workstation-demo` App.tsx: gains the dispatcher-session
  lifecycle effect (lifted from the shell). Constructs a
  `StaticDispatcher` once the session is ready, renders
  `<WorkstationShell connector dispatcher ... />`. The transfer-of-
  ownership model collapses cleanup to a single call:

  ```tsx
  useEffect(() => {
    let cancelled = false;
    let dispatcher: Dispatcher | null = null;
    void (async () => {
      const persistedId = readPersistedDispatcherSessionId();
      const session = persistedId
        ? connector.attachSession(persistedId)
        : await connector.createSession({role: "dispatcher"});
      writePersistedDispatcherSessionId(session.sessionId);
      const built = createStaticDispatcher({session, registry});
      if (cancelled) { void built.detach(); return; }
      dispatcher = built;
      setDispatcher(built);
    })();
    return () => {
      cancelled = true;
      if (dispatcher) void dispatcher.detach();
    };
  }, [connector, registry]);
  ```

  Notice there's no direct `session.detach()` anywhere — even the
  StrictMode-cancellation orphan path goes through
  `dispatcher.detach()` by always wrapping the session before the
  cancel check.
- `@fxtr-ui/demo` doesn't use dispatch — unchanged.

## Chunk plan

Chunks are organizational only — they're how the work splits into
review-sized pieces, not independently mergeable units. Between
chunks the tree may not typecheck and tests may fail; the whole
refactor lands together at the end. CODE_OVERVIEW updates happen
once at the end, not per chunk.

### Chunk 1 — Abstract surface + field renames

- New `src/dispatcher.ts` with the `Dispatcher` interface,
  `DispatcherStatus` type, renamed `DispatchEntry` interface.
- Rename fields on the existing entry shape and update internal
  call sites (`render_dispatch.tsx`, `hooks.ts`) to match.
- Update `src/index.ts` exports.

### Chunk 2 — `createStaticDispatcher`

- Extract the entry-creation runtime work into
  `src/base_entry.ts` (or fold into `entry.ts`): exposes
  `createBaseEntry({stub, stateAtom, sharedComponentStateAtom,
  dispatchProgressMessageAtom, initialComponent, effectFactory,
  ErrorFallback})`. This is what both static and (future) dynamic
  dispatchers call to build a single `DispatchEntry` — owns the
  `componentAtom` wrapping, the `atomEffect` mount, and the
  shared-state reset invariant.
- New `src/static_dispatcher.ts` with `createStaticDispatcher`:
  - Holds `session`, `registry`, the dispatcher root atom, the
    per-`(keyUuid, shortId)` entry cache, the status atom.
  - `getEntry`: stub-kind dispatch (`instanceof`), registry table
    lookup, `createBaseEntry` call, cache install.
  - `status`: derived atom — `"closed"` if `detach()` has run,
    else `session.lifecycle`.
  - `detach()` (async, idempotent): flips internal `closed` flag,
    clears the entry cache, awaits `session.detach()`. Subsequent
    `getEntry` throws `DispatcherClosedError`. The cache-clear is a
    GC nicety for the static case; the same hook is where 10b's
    dynamic dispatcher will release ESModule caches and abort
    in-flight bytes-loads.
- Delete the old `createDispatchEntry` export — `createBaseEntry`
  replaces it and the new entry-creation path goes through
  `createStaticDispatcher`.

### Chunk 3 — `<DispatcherProvider>` + `<RenderDispatch>` refactor

- Rename `<DispatchProvider>` → `<DispatcherProvider>`; takes
  `dispatcher` prop, drops `session` + `registry`.
- `<DispatcherProvider>` context value becomes
  `{dispatcher: Dispatcher}` — strip the `session` / `registry` /
  `getEntry` fields.
- `<RenderDispatch>` reads dispatcher from context, calls
  `dispatcher.getEntry(key, stub)`, subscribes to
  `entry.componentAtom`, renders inside the existing wrapper stack
  (renamed fields).
- Hooks (`useDispatchSharedAtom`, `useDispatchProgressState`,
  `useDispatchProgressMessage`) read the renamed fields.

### Chunk 4 — `<WorkstationShell>` dispatcher prop

- Add required `dispatcher: Dispatcher` to `WorkstationShellProps`.
- Remove the dispatcher session acquire / persist / detach effect.
- Replace `WaitForDispatcherReady` with a `dispatcher.status`-driven
  loading gate.
- Update the unavailable branch to read `dispatcher.status === "unavailable"`.

### Chunk 5 — Demo updates

- `@fxtr-ui/dispatch-demo` App.tsx: switch to
  `createStaticDispatcher` + `<DispatcherProvider>`.
- `@fxtr-ui/workstation-demo` App.tsx: lift the dispatcher-session
  lifecycle effect from the (now slimmer) shell into the demo's App;
  construct a `StaticDispatcher`; pass through to
  `<WorkstationShell>`.
- Manual browser smoke-test both demos.

### Chunk 6 — Tests + final cleanup

- Update existing tests that construct entries via the old
  `createDispatchEntry` to go through `createStaticDispatcher`
  instead (or, for tests that exercise the raw effect-fn
  state-machine, keep using a minimal harness — most state-machine
  tests in `static_entity_registry.test.ts` /
  `static_service_registry.test.ts` only need atoms, not a full
  dispatcher).
- New `static_dispatcher.test.ts`: smoke tests for the dispatcher
  API — `getEntry` returns a cached entry on repeat calls, `status`
  atom mirrors `session.lifecycle` until `detach()` runs, `detach()`
  flips status to `"closed"` and calls `session.detach()` (verified
  against a session-mock spy), `detach()` is idempotent,
  post-`detach()` `getEntry` throws `DispatcherClosedError`.
- React-render tests in
  `v2/ts/packages/fxtr-ui-react-dispatch-tests/` updated for the
  renamed provider + entry field names.

## Open questions / future work

- **Lift `useDispatcherSession` to a shared hook.** The
  dispatcher-session lifecycle (StrictMode-safe acquire, localStorage
  persistence, graceful detach on unmount) currently lives in the
  shell. 10a moves it to each consumer's App.tsx. If multiple
  consumers want it, lift to a hook in `@fxtr-ui/workstation` or
  `@fxtr-ui/react`. Defer until a second consumer needs it.

- **Status atom for `"unavailable"` distinct from `"closed"`.** The
  current shell already distinguishes these (genuine server-side
  delete vs. our own detach). In 10a the dispatcher exposes its own
  status atom; we should make sure the unavailable / closed
  distinction survives the abstraction (probably by reading the
  session's recent close reason).

- **LRU / idle eviction.** The per-entry cache lives until
  `dispatcher.detach()`. The entry-cache shape gives the dispatcher
  a natural place to hang LRU / idle-eviction later — no public API
  churn needed.

- **Narrower `Dispatcher.session` view.** Today the field is the
  full `Session`, and the contract is "don't call lifecycle
  methods." If misuse becomes a real concern, narrow to a
  `DispatcherSessionView` interface exposing only `sessionId` /
  `lifecycle` / `userState`. Cosmetic for now.

- **Single-store assumption.** Current `<DispatchProvider>` documents
  that the dispatcher session must share its jotai store with the
  surrounding React tree. The new `<DispatcherProvider>` inherits the
  same assumption (the dispatcher passes the session's atoms through;
  the surrounding `<JotaiProvider>` has to use the same store).
  Document; defer any enforcement helper.

## Design notes to consult

- `stage7_dispatch.md` — original dispatch design and the public
  types this refactor preserves.
- `stage10b_dynamic_dispatch.md` — the stage this refactor unblocks;
  validates the abstraction by showing it extends cleanly to a
  different kind of dispatcher.

## Implementation tasks

- [x] **Chunk 1** — Abstract surface + entry field renames.
- [x] **Chunk 2** — `createBaseEntry` + `createStaticDispatcher`.
- [x] **Chunk 3** — `<DispatcherProvider>` + `<RenderDispatch>` refactor.
- [x] **Chunk 4** — `<WorkstationShell>` dispatcher prop.
- [x] **Chunk 5** — Demo updates.
- [x] **Chunk 6** — Tests + final cleanup.
- [x] Final pass: CODE_OVERVIEW.md (frontend_lib) +
      `packages/fxtr-ui-workstation/LOCAL_CODE_OVERVIEW.md`. Single
      update at the end covering everything that landed.
