# Stage 4, Chunk 10 — API polish + typed demo, subAtom-driven invokes

Three intertwined cleanups after chunk 8:

1. Lift the `T | null` → `T` narrowing into `@fxtr-ui/state-util`'s `atomWithDefault` and `@fxtr-ui/react`'s `useSessionRootStateAtom`. Retire the privileged `useSessionState<T>` / `useSetSessionState<T>` hooks — consumers grab a typed atom and use vanilla jotai operations on it.
2. Define `_DemoServiceProto` in the demo and switch its `serviceRef` to the typed variant, removing the `as readonly never[]` cast at the invoke call site.
3. Redesign the demo's invokes UI so each entry is a sub-component reading + writing its own slot via `useSubAtom`. Demonstrates the typical compose-via-sub-atom pattern that the demo wasn't really showing before.

The high-level chunk description lives in [`stage4_react_bindings.md`](./stage4_react_bindings.md).

## Scope

### 10a. `atomWithDefault` + `useSessionRootStateAtom`

In:

- `@fxtr-ui/state-util/src/atom_with_default.ts` — `atomWithDefault<T>(parent: FxtrSessionAtom<T | null>, defaultValue: T): FxtrSessionAtom<T>`. Read returns `parent ?? defaultValue`. Writes pass through to parent unchanged. Framework-free.
- `@fxtr-ui/state-util/tests/atom_with_default.test.ts` — read fallback, write passthrough, ctx threading on function-form writes, identity stability across the wrapper, composes with `subAtom`.
- `@fxtr-ui/react/src/hooks.ts` — `useSessionRootStateAtom<T>(initialDefault: T | (() => T)): FxtrSessionAtom<T>`. Captures `initialDefault` on first render via `useRef`; subsequent renders ignore changes (matches `useState(initialValue)`). Lazy-init form (`() => T`) supported. Internally: `useSession()` → cast `session.userState` to `FxtrSessionAtom<T | null>` → `atomWithDefault(...)` → `useMemo(..., [session])`.
- Drop `useSessionState<T>` and `useSetSessionState<T>` from `@fxtr-ui/react`'s public surface. Consumers go through `useSessionRootStateAtom` + vanilla jotai.

Out (deferred):

- A typed-session-atom helper for non-React consumers — `userStateAs<T>(session)` style. Inline cast is fine; revisit if a non-React consumer arrives.

### 10b. `_DemoServiceProto`

In:

- `fxtr-ui-demo/src/client/_demo_service_proto.ts` — TS protocol interface mirroring the Python `_DemoServiceProto` (echo / combine / fail).
- Demo's `App.tsx` calls `connector.serviceRef<_DemoServiceProto>(short_id)`, threads the typed ref into `Panel`.
- Removes the `spec.args as readonly never[]` + `kwargs as Readonly<Record<string, never>>` casts in the invoke handler.

Out:

- Type-checking the Python and TS protocols against each other at build time. Manual review for now.

### 10c. Invokes redesigned around `useSubAtom`

In:

- New entry shape: `InvokeSlot = { readonly id: number; readonly label?: string; readonly result?: OpResultStub<unknown> }`. `id` is a stable React key.
- `PanelUserState` changes: `invokes` becomes non-optional (`ReadonlyArray<InvokeSlot>`); the `useSessionRootStateAtom<PanelUserState>({ invokes: [] })` default covers the null-state case.
- Parent (PanelBody) reads `invokes` via `useSubAtom(userStateAtom, "invokes")`. Renders `<InvokeCard idx={i} invokesAtom={invokesAtom} invokeTarget={…} onRemove={…} />` per entry plus a "+ new invoke" button that appends a fresh empty slot.
- `<InvokeCard>` is the sub-component: `const entryAtom = useSubAtom(invokesAtom, idx);` then per-entry UI:
  - Three invoke buttons (echo / combine / fail) — clicking writes `{ ...prev, label, result: ctx.allocateInvoke(...) }` to `entryAtom`.
  - "clear" button — resets to `{ id, label: undefined, result: undefined }`.
  - "×" button — calls `onRemove(idx)`, parent splices the slot out of the array.
  - `<OpResultDisplay label={...} stub={entry.result} />` rendered when `result` is set.

The clear / fill / remove operations each touch only one slice of state, so the component decomposition is faithful to the data flow.

Out:

- Multiple-row layouts, drag-to-reorder, named slots. Stuff the demo doesn't need.

## Design notes

### Why `useRef`-capture, not deps-memo, for the default

If `useSessionRootStateAtom`'s `useMemo` deps included the default value, an inline object literal would be a fresh reference every render → fresh wrapped atom every render → all `useSubAtom` downstream re-derives → cascading subscription churn. `useRef`-capture freezes the default at mount, matching `useState(initialValue)`. Documented in the hook's JSDoc.

### Why lazy-init too

`useSessionRootStateAtom<T>(initialDefault: T | (() => T))` accepts either a value or a factory. The factory variant gets called once at mount; useful when the default is expensive to compute (allocations, derivations) or when it depends on a value the consumer wants to capture-but-not-memo. Same convention as `useState`.

### Why drop `useSessionState` / `useSetSessionState`

They were privileged session-level hooks that duplicated jotai's atom API. With `useSessionRootStateAtom` returning a regular `FxtrSessionAtom<T>`, every reading and writing pattern collapses to plain `useAtomValue` / `useSetAtom` / `useSubAtom`. The asymmetry between "top-level state" and "sub-atom state" disappears. Smaller import surface, fewer concepts.

### `InvokeSlot.id` as stable React key

When entries are removed mid-array, indices shift; using `idx` as the React key would remount sibling components. The stable `id` lets React preserve the right card. The `useSubAtom(invokesAtom, idx)` inside the card still uses `idx` (the current position), and re-derives if `idx` changes after a removal — that's fine, it's a cheap re-memo and the atom values land where they should.

### `nextId` strategy

A small local counter inside the panel (`useRef<number>(0)`) for generating slot ids. The id only has to be unique within one session's invokes list; it isn't a wire concept. Skip UUIDs.

## Implementation steps

1. [x] Add `atomWithDefault` + tests to `@fxtr-ui/state-util` (11 new tests covering read fallback, static + function writes, ctx threading, subAtom composition, options pass-through).
2. [x] Rewrite `@fxtr-ui/react/src/hooks.ts`: add `useSessionRootStateAtom` (lazy + non-lazy via `T | (() => T)`), drop `useSessionState` and `useSetSessionState`. Update the index re-exports.
3. [x] Add `fxtr-ui-demo/src/client/_demo_service_proto.ts`. Type the demo's `serviceRef` call.
4. [x] Redesign `Panel.tsx`:
   - New `InvokeSlot` type, `PanelUserState` with required `invokes`.
   - `useSessionRootStateAtom<PanelUserState>({ invokes: [] })` at the root + `useSubAtom` for `message` / `loaded` / `invokes`.
   - Extract `<InvokeCard>` as a sub-component that calls `useSubAtom(invokesAtom, idx)` for its own slice.
   - Removed all the `as readonly never[]` / `as unknown as StructuredValue` casts.
5. [x] Updated existing React-tests package (`v2/ts/packages/fxtr-ui-react-tests/tests/hooks.test.tsx`) to use the new hook surface — `useSessionRootStateAtom` + jotai operations instead of `useSessionState` / `useSetSessionState`. Added two tests for the new lazy + default-capture behavior.
6. [x] `pnpm check && pnpm test && pnpm lint` all green (764 tests; +13 from chunk 9's 751 — 11 atomWithDefault tests plus 2 lazy-default tests).
7. [x] Update `frontend_lib/CODE_OVERVIEW.md` — state-util's new helper, react's hook swap, demo's new invoke layout.

## What chunk 11 (or later) might look like

- React-layer tests (9a from the chunk-9 plan).
- The integration test for "array mutation + second-client attach" that would have caught the chunk-8 array-diff bug.
- Demo polish: shared state between panels showing reactive cross-panel updates, etc.
