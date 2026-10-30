# Stage 4, Chunk 4 — `@fxtr-ui/state-util` + `subAtom`

New framework-free package that depends only on `@fxtr-ui/core` and `jotai`. Ships the path-aware projection helper `subAtom` that the React layer (and any other consumer) uses to scope a session atom down to a single field or array element while preserving the `FxtrSessionAtom` shape — reads project, writes propagate to the parent, ctx threads through unchanged.

The high-level chunk description lives in [`stage4_react_bindings.md`](./stage4_react_bindings.md).

## Scope

In:

- New package `@fxtr-ui/state-util` at `frontend_lib/packages/fxtr-ui-state-util/` (package.json, tsconfig, src/).
- `subAtom<T, K extends keyof T>(parent: FxtrSessionAtom<T>, key: K): FxtrSessionAtom<T[K]>`.
- A small runtime test exercising the common patterns (static write, ctx-bearing updater write, array-index, nested subAtoms).

Out (deferred):

- React hook `useSubAtom` — sits in `@fxtr-ui/react`, comes in chunk 5.
- Array-mapping (`splitAtom`-style) helpers — wait for a real use case.
- Multi-step path projection / fluent chain — start with the single-key form.

## Implementation outline

### Package layout

`frontend_lib/packages/fxtr-ui-state-util/`:

- `package.json` — `@fxtr-ui/state-util`, depends on `@fxtr-ui/core` (workspace) and `jotai`. Mirror the existing packages' shape.
- `tsconfig.json` — extends `../../../../../tsconfig.base.json` like the others.
- `src/index.ts` — re-exports `subAtom`.
- `src/sub_atom.ts` — the implementation.
- `tests/sub_atom.test.ts` — runtime tests.

### `subAtom` signature + body

```ts
function subAtom<T, K extends keyof T>(
  parent: FxtrSessionAtom<T>,
  key: K,
): FxtrSessionAtom<T[K]>
```

Read: `(get) => get(parent)[key]`.

Write: takes a `FxtrUpdate<T[K]>`. Internally `set(parent, (prevParent, ctx) => ...)`. Inside the parent updater, resolves the child update:
- function-form → call with `(prevParent[key], ctx)`;
- static → use as-is.
Then return a new parent value with the child slot replaced. Branch on `Array.isArray(prevParent)`:
- array: copy + assign at numeric index;
- object: `{ ...prev, [key]: newChild }`.

Pass through `options` (the `UserStateWriteOptions`) to the parent's set call so `onRollback` rides along.

Two subtle points to keep in mind:
- The `T[K]` extraction works for both object keys and array element types (where `K = number`), but consumers need to be aware that `parent` being `T | null` (e.g. `FxtrSessionAtom<UserState | null>`) means `subAtom` is *not* applicable to a possibly-null root. Callers narrow first with their own helper (typically inside a hook that checks `useSessionState() !== null`).
- The new sub-atom is a fresh jotai atom instance per call; callers must memoize (or use `useSubAtom` from chunk 5) to avoid spurious re-renders.

### Tests

A single test file `tests/sub_atom.test.ts`. Builds a minimal mock `FxtrSessionAtom<{ a: number; b: string[]; c: { x: number } }>` using `jotai`'s primitive atom + a hand-written write fn that simulates the ctx-passing contract (we don't need a real session for this — just a function that synthesizes a ctx with stub `allocate*` methods). Cases:

- Read projects (`subAtom(parent, 'a')` returns the field).
- Static write propagates (`set(child, 5)` updates parent).
- Function-form write threads ctx (the test inspects that the ctx passed to the inner updater is the same instance the test stubbed).
- Array-index `subAtom(parent, 1)` against `parent.b` works: read projects element, write replaces in a new array (parent reads array of same length with index replaced).
- Nested: `subAtom(subAtom(parent, 'c'), 'x')` works end-to-end.
- `options.onRollback` rides along (mock parent records the rollback callback).

No React in this chunk.

### `pnpm` workspace registration

The frontend_lib packages already get picked up via the workspace glob — just dropping the new package directory in should be enough. Check `pnpm-workspace.yaml` or equivalent and add an entry if needed. `pnpm install` once after to wire it up.

## Implementation steps

1. [x] Create `frontend_lib/packages/fxtr-ui-state-util/` with `package.json`, `tsconfig.json`, `src/sub_atom.ts`, `src/index.ts`.
2. [x] Ensure workspace picks the package up (`pnpm install` from the workspace root). Also added the package to `tsconfig.json`'s `references` list so `tsc --build` sees it.
3. [x] Implement `subAtom` per the spec above.
4. [x] Write `tests/sub_atom.test.ts` with the cases listed.
5. [x] `pnpm check && pnpm test && pnpm lint` all green (702 total, 10 new).
6. [x] Update `frontend_lib/CODE_OVERVIEW.md` with the new package.

## Notes from implementation

- Workspace auto-glob picked up the new package, but I also had to add it to `tsconfig.json`'s `references` list so `tsc --build` walks it. Easy to miss; flagged here for future package additions.
- `vitest` needed to be a `devDependency` of the new package even though it's hoisted at the workspace root — without it, the package's own type-check couldn't resolve the `vitest` import.

## What chunk 5 will do

`@fxtr-ui/react`: `<ConnectorProvider>`, `<FxtrSessionProvider>`, `useSession`, `useSessionState`, `useResult`, and `useSubAtom(parent, key)` which `useMemo`s `subAtom(parent, key)` so identity is stable across renders.
