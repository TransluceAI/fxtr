# Stage 4, Chunk 3 — Per-session jotai store

Right now every `WorkstationSession` shares its parent `WorkstationConnector`'s jotai store. After this chunk each session owns its own `createStore()`, exposed as `session.store`. Connector keeps its own store for connection status and resource lists. Two sessions of the same connector are then fully isolated jotai-side; two panels backed by separate `Session` objects share nothing but the wire transport.

The high-level chunk description lives in [`stage4_react_bindings.md`](./stage4_react_bindings.md).

## Scope

In:

- `WorkstationSession` mints its own `JotaiStore` in the constructor; no `store` option, no store passed by the connector. Public read-only `session.store: JotaiStore` field for consumers (`<FxtrSessionProvider>` will plug this into a `<JotaiProvider>`).
- `WorkstationSessionOptions.store` removed.
- `WorkstationConnector.createSession` / `attachSession` stop passing `this.store` to the session.
- Test helpers (`makeConnector`, `setupConnectedSession`, the few inline equivalents) update to return `session.store` as the `store` field. Test bodies are largely unchanged because they already do `store.set(session.userState, …)` — the store reference just now points at the session's, not the connector's.
- A couple of tests that intentionally exercise multi-session-shared-state assumptions (cross-session smuggling, `attachSession`-rebuild) need an eye to confirm they still mean what they did — those paths shouldn't change in substance since the wire is the shared layer.

Out (deferred):

- `<FxtrSessionProvider>` and the React-layer story. → Chunk 5.
- `subAtom` and `@fxtr-ui/state-util`. → Chunk 4.
- Any "two sessions sharing the same store" escape hatch. If a real need shows up, add a `store?: JotaiStore` option later — we're not designing for it now.

## Implementation outline

### `WorkstationSession`

1. Drop `store` from `WorkstationSessionOptions`.
2. In the constructor, call `createStore()` (imported from `jotai`) and store it as `this.store`. Mark the field `readonly`.
3. Existing internal `store` usage stays — same field, just now session-owned.
4. `session.store` becomes part of the public surface so the React layer can read it.

### `WorkstationConnector`

1. `createSession` and `attachSession` no longer pass `store: this.store` to `new WorkstationSession(...)`. The session's constructor handles its own store.
2. Connector retains its own `store` for `connectionStatus` (re-exposed via `this.connectionStatus`) and the resource list cache. No public-API change to the connector itself.

### Tests

- `setupConnectedSession()` in `slot_reducer.test.ts`, `invoke_reducer.test.ts`, `session.test.ts`: change the returned `store` field to `session.store`.
- `makeConnector()` returns the connector's store (used for resource-list / connection-status tests, none of which touch session state).
- Where tests construct a session inline via `attachSession` and then use the connector's store for session ops, switch to `session.store`.
- Where a test creates two sessions (e.g. cross-session smuggling), each session gets its own store; tests use the right one for each.
- Integration tests: same swap (`session.store` for session ops).

### CODE_OVERVIEW.md

- Note the per-session store on the `WorkstationSession` description: "owns its own `JotaiStore`, exposed as `session.store`; the React layer's `<FxtrSessionProvider>` will plug this into a `<JotaiProvider>` so component-internal atoms inside one session's panel are isolated from another's."
- `WorkstationSessionOptions` no longer carries `store`.
- Connector description: clarify that `connector.store` is for connection / resource-list state only; sessions are independent.

## Implementation steps

1. [x] `WorkstationSession`: drop `store` option, mint internal store, expose `readonly store`.
2. [x] `WorkstationConnector`: stop passing `store` to sessions. Also exposed `connector.store: JotaiStore` publicly (was previously `private`) since the resource-list helpers in integration tests need it.
3. [x] Update test helpers (`makeConnector` keeps connector-store, `setupConnectedSession` returns session-store) and any inline test setups. `awaitLifecycle(session, target)` shrunk to just-the-session args since it now reads `session.store` internally; `resolveTestEntityShortId(connector)` / `resolveTestServiceShortId(connector)` drop their `store` arg and use `connector.store`.
4. [x] `pnpm check && pnpm test && pnpm lint` all green.
5. [x] Update CODE_OVERVIEW.md.

## Notes from implementation

- Exposing `connector.store` publicly was a small scope extension over the plan: the integration tests' resource-list helpers needed a store, and reaching into a private field was the alternative. `connector.store` now sits next to `session.store` as a parallel public surface, with docs making clear they hold different state and can't be substituted for each other.
- The bulk of the test churn was bot-driven via perl substitution. Two iterations: first pass introduced a few `sessionA.sessionA.store.X` malformed patterns from greedy regex matching; cleaned up by a follow-up substitution.
- Unit test helpers (`setupConnectedSession`, `makeConnectedSession`) now return `store: session.store` so test bodies that already used `store.set(session.userState, …)` keep working unchanged. Integration tests had to be migrated individually since they sometimes interact with both connector-level and session-level state.

## What chunk 4 will do

`@fxtr-ui/state-util` package: `subAtom<T, K extends keyof T>(parent, key)` and a `useSubAtom` companion in the React package. Framework-free, depends only on `@fxtr-ui/core` + `jotai`. Tests against a mock `FxtrSessionAtom` confirm reads project, writes propagate, and ctx threads through.
