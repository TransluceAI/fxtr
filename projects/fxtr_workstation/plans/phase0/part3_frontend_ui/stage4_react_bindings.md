# Stage 4 (was stage 3) — Minimal React bindings

A React layer on top of `@fxtr-ui/core` + `@fxtr-ui/connector-workstation`. Each card / panel in a tree binds to one session; multiple sessions multiplex over one connector websocket; per-panel jotai state is strictly isolated. Provides the smallest set of types, helpers, and hooks needed to write a real component, plus a demo app exercising the multi-panel-multi-session case.

## What gets built

- New types in `@fxtr-ui/core` (`FxtrSessionUpdateCtx`, `FxtrUpdate<T>`, `FxtrSessionAtom<T>`).
- `@fxtr-ui/connector-workstation` adopts ctx-style writes, removes its public `allocate*` methods (they become private), and mints a fresh jotai store per session.
- New package `@fxtr-ui/state-util` — `subAtom` helper plus future state-projection utilities; depends only on `@fxtr-ui/core` + `jotai`.
- New package `@fxtr-ui/react` — owns the session React Context. Provides `<ConnectorProvider>`, `<FxtrSessionProvider>`, `useSession`, `useSessionState`, `useSubAtom`, `useResult`.
- New package `@fxtr-ui/demo` — multi-panel demo app. Each panel binds one session; panels can open new "views" onto an existing session via the connector (each view = a fresh `attachSession`, fully isolated jotai-side).

The demo replaces (or sits next to) `projects/fxtr_workstation/debug_ui` for sanity checking. A small Python harness sets up known demo entities + a tiny demo service so the load / invoke flows have something to hit.

## Locked design decisions

### 1. Ctx-style updater on writes

`Session.userState`'s write fn takes either a direct value or a function `(prev, ctx) => next`, where `ctx: FxtrSessionUpdateCtx` exposes `allocateLoad` / `allocateInvoke` / `allocateCallback` + an escape-hatch `session` reference. ctx is bound to the session that owns the atom.

```ts
type FxtrSessionUpdateCtx = {
  allocateLoad<T>(ref: EntityRefStub<T>): OpResultStub<T>;
  allocateInvoke<P, M extends MethodNames<P>>(
    svc: ServiceRefStub<P>,
    method: M,
    args: MethodArgs<P, M>,
    kwargs?: MethodKwargs<P, M>,
  ): OpResultStub<MethodReturns<P, M, StructuredValue>>;
  allocateCallback<R>(): CallbackInboxStub<R>;
  readonly session: Session;
};

type FxtrUpdate<T> = T | ((prev: T, ctx: FxtrSessionUpdateCtx) => T);

type FxtrSessionAtom<T> = WritableAtom<
  T,
  [FxtrUpdate<T>, UserStateWriteOptions?],
  void
>;
```

Rationale: the same-tick contract + the session-binding constraint already mean "allocate is only meaningful inside a write that immediately stores the slot." Threading the session imperatively is just bookkeeping for a constraint the API can enforce structurally. The public `allocateLoad` / `allocateInvoke` / `allocateCallback` methods are **removed** from the abstract `Session` interface entirely; ctx is the only path. The workstation connector keeps the implementations as private methods (the reducer + ctx need them internally).

`Session.userState` becomes a `FxtrSessionAtom<UserState | null>`. The existing `UserStateWrite` type alias is replaced by `FxtrUpdate<UserState | null>`.

### 2. `subAtom` projection helper, no fluent chain

A single function in `@fxtr-ui/state-util`:

```ts
function subAtom<T, K extends keyof T>(
  parent: FxtrSessionAtom<T>,
  key: K,
): FxtrSessionAtom<T[K]>;
```

Works for object keys and array indices (arrays have numeric `keyof`). Reads project; writes propagate to the parent. When the user's child-updater is function-form, the parent's wrapper invokes it inside its own updater so the ctx threads through unchanged.

`@fxtr-ui/react` adds `useSubAtom(parent, key)` to memoize the child atom across renders. We can add array-mapping helpers (`splitAtom`-style) later when a real use case shows up.

### 3. Session delivered via React Context (in `@fxtr-ui/react`)

The session-from-the-component-tree concern lives entirely in the React layer:

```ts
// @fxtr-ui/react
const SessionContext = createContext<Session | null>(null);

export function FxtrSessionProvider({ session, children }: {
  session: Session; children: ReactNode;
}) {
  return (
    <JotaiProvider store={session.store}>
      <SessionContext.Provider value={session}>{children}</SessionContext.Provider>
    </JotaiProvider>
  );
}

export function useSession(): Session {
  const s = useContext(SessionContext);
  if (s === null) {
    throw new Error("useSession() called outside FxtrSessionProvider");
  }
  return s;
}
```

`@fxtr-ui/core` doesn't know or care how the session is delivered to components — that's React's job. The Provider does two things: scopes the jotai store to the session (for state-isolation) and exposes the session via context (for hooks).

Tradeoff vs. an atom: derived atoms can't pull the current session at evaluation time. In practice atoms close over the session at `useMemo` / construction time anyway (or take it as a factory argument), so this isn't load-bearing. If a real use case forces it, we add an atom-shaped accessor at that point.

### 4. Sessions take a store; default to the connector's

`WorkstationConnector` has its own jotai store (constructor's `store?` option, defaulting to `getDefaultStore()`). `connector.createSession(metadata, { store? })` and `connector.attachSession(id, { store? })` accept an optional store override; absent that, they pass the connector's store down to the new session. Every session exposes `readonly store: JotaiStore` regardless.

`<FxtrSessionProvider session={s}>` wraps `<Provider store={s.store}>`. Component-internal atoms via `useMemo(() => atom(...), [])` are naturally isolated per component instance — different `useMemo` calls produce different atom references → independent values in any store — so the day-to-day React story stays intuitive regardless of how stores are shared.

The "two panels on the same workstation session_id" case: the consumer calls `attachSession(id)` twice and gets two distinct `Session` objects that coordinate via the server. By default both use the connector's store and module-level atoms intermix; for strict isolation, pass a fresh `createStore()` per session.

Originally we had `<ConnectorProvider>` mirroring the session provider. Dropped because the abstract "thing with a store" surface ended up too thin to do anything useful with at the package level. Consumers wrap their own `<JotaiProvider store={connector.store}>` and pass the typed connector reference around however suits.

### 5. Result-object hooks; no Suspense

`OpResultStub.result` is already `Atom<ResultStatus<T>>` with `pending | filled | errored` states. The consumer reads via `useAtomValue(stub.result)` (or the `useResult(stub)` sugar). No Suspense integration — stubs are explicit, "no side channels" is the principle, and `useResult`'s return value is a discriminated union the consumer branches on. If someone wants Suspense semantics they can wrap with a helper that throws on `pending`.

### 6. Microtask coalescing only; no explicit `transaction(...)`

Chunk 6 already coalesces all writes in one tick into one mutation. With the ctx-style updater, a single updater can do multiple `allocate*` calls + state writes atomically. We don't need explicit transactions for stage 4. If a future case forces cross-atom batching beyond what microtask gets us, we add it then.

## Package layout

After this stage:

- `frontend_lib/packages/fxtr-ui-core` — abstract types + stubs. Adds `FxtrSessionUpdateCtx` / `FxtrUpdate` / `FxtrSessionAtom`. Removes public `allocate*` from `Session`. Changes `Session.userState` to `FxtrSessionAtom<UserState | null>`. Replaces `UserStateWrite`. No React, no session-context concern.
- `frontend_lib/packages/fxtr-ui-state-util` (new) — depends on `@fxtr-ui/core` + `jotai`. Exports `subAtom`. Framework-agnostic.
- `frontend_lib/packages/fxtr-ui-connector-workstation` — adapts to ctx-style write; private `allocate*` methods; per-session store.
- `frontend_lib/packages/fxtr-ui-react` (new) — depends on `@fxtr-ui/core` + `@fxtr-ui/state-util` + `jotai` + `react`. Owns the `SessionContext`. Providers + minimal hooks.
- `frontend_lib/packages/fxtr-ui-demo` (new) — depends on the above plus a tiny Python harness for the workstation backend.

## Chunks

Each chunk leaves a reviewable, mostly-runnable artifact. Tests are deferred to a final chunk per "stop adding tests until the surface is stable" guidance — except where a chunk's whole point is structural and a type-only spot-check is sensible.

1. **Core type additions** (additive only). `FxtrSessionUpdateCtx`, `FxtrUpdate`, `FxtrSessionAtom` land in `@fxtr-ui/core`. Existing types unchanged. Everything still compiles. See [`stage4_chunk1_plan.md`](./stage4_chunk1_plan.md).
2. **Connector ctx adoption + abstract Session cleanup.** Remove `allocateLoad` / `allocateInvoke` / `allocateCallback` from the abstract `Session`. Make their workstation impls private. Replace `Session.userState`'s write signature with `FxtrUpdate<UserState | null>`. Update the connector's write fn to pass `(prev, ctx)` to function-form updates. Update existing chunk-6 tests to match.
3. **Per-session store on `WorkstationSession`.** `createSession` / `attachSession` mint a fresh `JotaiStore` per session. Connector keeps its own store for resource lists. Update tests that assume a shared store.
4. **`@fxtr-ui/state-util`: `subAtom`.** New package, framework-free. Define + ship `subAtom`. Type-level tests on a mock `FxtrSessionAtom`; tiny runtime test that writes propagate correctly with ctx threading.
5. **`@fxtr-ui/react`: providers + hooks.** `<ConnectorProvider>`, `<FxtrSessionProvider>`, `useSession`, `useSessionState`, `useSubAtom`, `useResult`. No demo wiring yet — just the package. A per-chunk plan doc will pin down hydration timing (synchronous via `useHydrateAtoms` vs. effect-based) and the `useSubAtom` memoization story.
6. **Demo skeleton + mutate.** New `@fxtr-ui/demo` package + Python harness. Connection status bar, sidebar with sessions list, "create session" button, panel-grid main area, each panel = one session via `<FxtrSessionProvider>`. Each panel shows lifecycle + a text-bound mutate field. Confirms providers and `useSubAtom` end-to-end against a running workstation. Per-chunk plan doc to write here too.
7. **Demo: load.** Backend exposes a known demo entity (or several). Each panel gets an "load entity" button that allocates a load via ctx, stores the slot in card state, displays the result through `useResult`. Two panels on the same session show the dedup behavior (one wire load).
8. **Demo: invoke.** Backend registers a tiny demo service. Each panel gets an "invoke" button that allocates an invoke with a hardcoded method + args, stores the result slot, displays the result. Confirms the invoke path.
9. **Tests** (9a+9b done; 9c deferred — see [`stage4_chunk9_plan.md`](./stage4_chunk9_plan.md)). React-layer component tests (Provider hydration, multi-panel isolation, ctx-updater path) + extra `subAtom` runtime coverage. Demo-as-integration-test deferred. The React tests live in a sibling package `v2/ts/packages/fxtr-ui-react-tests/` rather than alongside `@fxtr-ui/react`'s source because of a vitest-loading issue with `@vitest-environment jsdom` + `.tsx` under the `projects/fxtr_workstation/frontend_lib/packages/` tree (see the package's README).

We'll write per-chunk plan docs as we approach each chunk past chunk 1 (chunk 5 and 6 in particular benefit from detailed plans before code lands).

## Open questions

### OQ-α — Multi-view-of-same-session demo affordance

Imperative "open view of session" beats a per-card boolean toggle. Concretely: the sidebar's session row shows "N views open"; "new view" mints a fresh `attachSession(id)` + adds a panel for it. Closing the panel detaches it. This makes the two flavors of multi-panel clear in the UI:

- "Two views of session X" = two `attachSession(X)` results, isolated jotai-side.
- "Two cards backed by the same Session object" = a rarer thing the demo doesn't need to expose in v1.

Decision punted until chunk 6 — flag here so we don't lose it.

### OQ-β — `ConnectorProvider` necessity

If the only thing the connector exposes reactively is the resource lists, and the demo's sidebar is the only consumer, we could read those via a one-off `useStore`/`useAtomValue` against `connector.store` without wrapping the sidebar in a Provider. Provider is more uniform; direct access is fewer moving parts. Decide during chunk 5.

### OQ-γ — `useResult(stub)` vs. `useAtomValue(stub.result)`

The sugar is fine but small. Decide in chunk 5 whether to ship it or just document the underlying pattern.

### OQ-δ — `useSubAtom` memoization keying

`useMemo(() => subAtom(parent, key), [parent, key])` is the obvious thing. If `parent` is itself a `useSubAtom` result, identity is preserved by the inner memo. Confirm during chunk 4 / 5 that the cascade works without forcing every layer to manually memoize.

### OQ-ε — Connection-status atom store-binding

`WorkstationServerConnection.connectionStatus` is bound to the connector's store. A session's store can't read it directly. For panels that want to show connection status, we either (a) bridge it into the session's store via a subscription helper, (b) read it imperatively at the demo's top level (outside any session provider), or (c) accept that connection status is connector-scope and panels don't display it. Decide during chunk 5 / 6.

## Design notes to consult

- `design_notes/01_reactive_model.md` — atom-shape decisions, ctx-style updater is the resolution of OQ-6.
- `design_notes/03_stub_types.md` — inner atoms on stubs (read via `useAtomValue(stub.result)`).
- `design_notes/04_session_state_layout.md` — session atom value shape.
- `design_notes/05_dispatch_and_inline_loading.md` — the `(refstub, ownStorage)` pattern this stage's hooks plug into.
- `discussion_topics.md` — running tracker.

## Deliverable

After chunk 8: a running demo at `frontend_lib/packages/fxtr-ui-demo` that opens a workstation connection, lets the user create / attach sessions, opens multiple panels (with multiple views of the same session demonstrating isolation), and exercises load + invoke against a known backend. After chunk 9: tests covering the React surface and the multi-panel isolation invariant.
