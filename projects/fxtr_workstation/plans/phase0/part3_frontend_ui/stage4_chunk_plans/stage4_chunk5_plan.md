# Stage 4, Chunk 5 — `@fxtr-ui/react` providers + hooks

New package wiring the per-session jotai store + the session-from-context delivery into React. The smallest set of providers and hooks needed to write a real component against `@fxtr-ui/core` + `@fxtr-ui/connector-workstation` + `@fxtr-ui/state-util`.

The high-level chunk description lives in [`stage4_react_bindings.md`](./stage4_react_bindings.md).

## Scope

In:

- New package `@fxtr-ui/react` at `frontend_lib/packages/fxtr-ui-react/`.
- `<FxtrSessionProvider session={s}>` — wraps a jotai `<Provider store={s.store}>` plus a React Context carrying the session.
- `useSession()` — reads the session from context; throws if no provider above.
- `useSessionState()` — `useAtomValue(useSession().userState)` sugar.
- `<ConnectorProvider connector={c}>` — wraps a jotai `<Provider store={c.store}>` plus a React Context carrying the connector. For the parts of the UI that read connector-scope reactive state (sidebar, status bar).
- `useConnector()` — reads the connector from context; throws if absent.
- `useSubAtom(parent, key)` — memoized `subAtom(parent, key)`.
- `useResult(stub)` — `useAtomValue(stub.result)` sugar.

Out (deferred):

- React-layer component tests (Provider hydration, multi-panel isolation, ctx-updater path through real Providers). Comes in the later test chunk.
- Demo wiring. Chunk 6.
- Async / Suspense affordances around stubs — `useResult` is explicitly result-object only.
- Multi-step path projections (still single-key).

## Implementation outline

### Package layout

`frontend_lib/packages/fxtr-ui-react/`:

- `package.json` — `@fxtr-ui/react`, depends on `@fxtr-ui/core`, `@fxtr-ui/state-util` (workspace), `jotai`. `react` as `peerDependencies` (`>=18`). Dev deps: `@types/react`, `react`, `vitest`.
- `tsconfig.json` — extends the base config like the other packages.
- `src/session_context.tsx` — React Context + `<FxtrSessionProvider>` + `useSession`.
- `src/connector_context.tsx` — React Context + `<ConnectorProvider>` + `useConnector`.
- `src/hooks.ts` — `useSessionState`, `useSubAtom`, `useResult`.
- `src/index.ts` — re-exports.

### Hydration timing decision

React Context delivers the session object synchronously at render time; the jotai store is wired at the same moment via the `<Provider store={session.store}>` wrapper. No `useHydrateAtoms` needed — the store already carries the session's authoritative state (the session pre-populated its own atoms in its constructor). One pass; no rehydration phase.

### `<FxtrSessionProvider>` shape

```tsx
function FxtrSessionProvider({ session, children }: {
  session: Session;
  children: ReactNode;
}) {
  return (
    <JotaiProvider store={session.store}>
      <SessionContext.Provider value={session}>{children}</SessionContext.Provider>
    </JotaiProvider>
  );
}
```

`useSession()`:

```ts
function useSession(): Session {
  const s = useContext(SessionContext);
  if (s === null) {
    throw new Error("useSession() called outside a <FxtrSessionProvider>");
  }
  return s;
}
```

The thrown error is intentional — silent-null would let stale `useAtomValue(session.userState)` reads happen against the default store, which is a confusing footgun. The throw lands on the obvious offender.

### `<ConnectorProvider>` shape

Same pattern, parameterized on `WorkstationConnector` (or, more generally, anything that has a `store: JotaiStore`). The connector's store is what holds `connectionStatus`, the resource lists, etc. — components under this provider can `useAtomValue(list.records)` etc.

### `useSubAtom`

```ts
function useSubAtom<T, K extends keyof T>(
  parent: FxtrSessionAtom<T>,
  key: K,
): FxtrSessionAtom<T[K]> {
  return useMemo(() => subAtom(parent, key), [parent, key]);
}
```

`parent` and `key` are the memo deps. If a consumer derives `parent` from `useSubAtom` itself, the inner memo is stable across renders (its identity is keyed on the input identity), so chained `useSubAtom(useSubAtom(...))` is identity-stable as long as the root parent is stable.

### `useResult`

```ts
function useResult<T>(stub: OpResultStub<T>): ResultStatus<T> {
  return useAtomValue(stub.result);
}
```

A direct one-liner. We could leave it out and have consumers write the longer form, but the import surface savings + readability of `useResult(stub).status === "filled"` won out.

### React peer-dep

`peerDependencies: { react: ">=18" }`. Matches `@fxtr/react`'s convention. Devs depend on `react@19` for the type-check + tests in this package.

### Edge case: session changes mid-render

If a parent component swaps `session` on the same `<FxtrSessionProvider>`, the jotai `<Provider>` swaps `store`, and the context value updates. All `useAtomValue` calls inside re-read against the new store. This is the expected behavior; the user explicitly opted in by changing the prop.

If a component holds a `useSubAtom(session.userState, "x")` and the session changes, the parent atom reference changes (it's `session.userState`, a different atom on the new session), so `useMemo`'s deps re-run and the sub-atom is rebuilt. Identity-stable across no-op renders, freshly-computed on actual change.

## Implementation steps

1. [x] Create `frontend_lib/packages/fxtr-ui-react/` with `package.json`, `tsconfig.json`, `src/`, `tests/` (empty for now).
2. [x] Register in `tsconfig.json`'s `references` list. Run `pnpm install`.
3. [x] Implement `session_context.tsx` + `useSession` + `<FxtrSessionProvider>`.
4. [x] Implement `connector_context.tsx` + `useConnector` + `<ConnectorProvider>`.
5. [x] Implement `hooks.ts` with `useSessionState`, `useSubAtom`, `useResult`.
6. [x] `src/index.ts` re-exports.
7. [x] `pnpm check && pnpm test && pnpm lint` all green.
8. [x] Update `frontend_lib/CODE_OVERVIEW.md` with the new package.

## Notes from implementation

- The package needs `"jsx": "react-jsx"` in its `tsconfig.json` — `tsconfig.base.json` doesn't set it (only React-bearing packages do). Easy to miss; flagged for future packages.
- The abstract `Session` interface in `@fxtr-ui/core` was missing `readonly store: JotaiStore` — it was added on `WorkstationSession` in chunk 3 but not propagated to the abstract base. Added it as part of this chunk so `<FxtrSessionProvider>` can read `session.store` against the abstract type.
- React-layer tests are deferred per the stage's "test once the surface is stable" convention. The package compiles and lints clean; runtime behavior gets verified end-to-end by the demo in chunk 6 and the test chunk later.

## Post-implementation revisions

After the initial implementation landed, two things shifted on review:

1. **`<ConnectorProvider>` / `useConnector` / `Connector` removed.** The minimum abstract `Connector` interface (`{ readonly store: JotaiStore }`) was too thin to be useful at the React-package level — you can't call `listSessions()`, read `connectionStatus`, etc. from just a store. Consumers wrap their own `<JotaiProvider store={connector.store}>` and pass the typed connector reference around however suits (prop drilling, their own React Context, a module-scoped singleton). The session provider keeps its place because `Session` is a well-defined type in core and the store-scoping property is load-bearing.
2. **Session store ownership inverted.** Chunk 3's "each session mints its own store" rule was reverted: `WorkstationSessionOptions` now requires `store: JotaiStore` again, and `connector.createSession` / `attachSession` take an optional `{ store? }` arg that defaults to `connector.store`. Default behavior — sessions share the connector's store — keeps the common case simple. Consumers who want stronger per-panel isolation (e.g. two `attachSession` calls on the same `session_id` whose UI state shouldn't intermix) explicitly pass `createStore()`. Module-level shared atoms intermix by default; component-internal atoms via `useMemo(() => atom(...), [])` are still naturally isolated by reference regardless of store, so the day-to-day React story doesn't change.

## What chunk 6 will do

`@fxtr-ui/demo` package + Python harness: connection status bar, sidebar with sessions list, "create session" button, panel grid. Each panel = one session via `<FxtrSessionProvider>` with a text-bound mutate field. Confirms the providers + `useSubAtom` end-to-end against a running workstation.
