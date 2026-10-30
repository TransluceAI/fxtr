# Stage 4, Chunk 1 — Core type additions

Pure-additive change to `@fxtr-ui/core`. Defines the new ctx-aware updater types that later chunks (connector, state-util, React) will consume. No behavior changes; no existing exports renamed or removed.

The high-level chunk description lives in [`stage4_react_bindings.md`](./stage4_react_bindings.md). Read that first for the locked design decisions across the stage.

## Scope

In:

- New interface `FxtrSessionUpdateCtx`.
- New type alias `FxtrUpdate<T>`.
- New type alias `FxtrSessionAtom<T>`.
- Re-exports from `@fxtr-ui/core`'s `index.ts`.

Out (deferred):

- Removing `allocateLoad` / `allocateInvoke` / `allocateCallback` from the abstract `Session` interface. → Chunk 2.
- Changing `Session.userState`'s write signature to use `FxtrUpdate<UserState | null>`. → Chunk 2.
- Connector adoption — passing `(prev, ctx)` to function-form updaters, minting per-session stores. → Chunk 2 (writes) + Chunk 3 (stores).
- `@fxtr-ui/state-util` package, `subAtom`, React-layer hooks (including the `SessionContext`). → Chunks 4, 5.

Everything in this chunk is additive. After landing, the connector and all existing tests still compile and pass with no changes.

## New types

### `FxtrSessionUpdateCtx`

The context object the connector passes as the second argument to function-form updaters of a `FxtrSessionAtom`. Carries the session-scoped allocate methods + an escape-hatch reference to the session itself.

```ts
interface FxtrSessionUpdateCtx {
  allocateLoad<T>(ref: EntityRefStub<T>): OpResultStub<T>;
  allocateInvoke<P, M extends MethodNames<P>>(
    svc: ServiceRefStub<P>,
    method: M,
    args: MethodArgs<P, M>,
    kwargs?: MethodKwargs<P, M>,
  ): OpResultStub<MethodReturns<P, M, StructuredValue>>;
  allocateCallback<R>(): CallbackInboxStub<R>;
  readonly session: Session;
}
```

Method signatures mirror the existing public ones on `Session` (which chunk 2 then removes from the abstract). Docstrings carry over the same same-tick / dedup contracts.

`session` is the escape hatch: lets advanced consumers read other session atoms (e.g., `lifecycle`) inside the updater, or pass the session reference to a helper. Most consumers won't touch it.

### `FxtrUpdate<T>`

```ts
type FxtrUpdate<T> = T | ((prev: T, ctx: FxtrSessionUpdateCtx) => T);
```

Mirrors React's `SetStateAction<T>` shape, extended with the ctx argument. Static values still work — the function form is purely additive.

### `FxtrSessionAtom<T>`

```ts
type FxtrSessionAtom<T> = WritableAtom<
  T,
  [FxtrUpdate<T>, UserStateWriteOptions?],
  void
>;
```

The shape we want consumers to see for any session-bound writable atom. Chunk 2 retypes `Session.userState` to this. Chunk 4's `subAtom` produces values of this type when given a parent of this type.

## File touches

- `frontend_lib/packages/fxtr-ui-core/src/session.ts`: add `FxtrSessionUpdateCtx`, `FxtrUpdate<T>`, `FxtrSessionAtom<T>` near the existing `UserStateWrite` / `UserStateWriteOptions` types. Add cross-references in the existing JSDoc but don't yet retype anything.
- `frontend_lib/packages/fxtr-ui-core/src/index.ts`: re-export the new types. Keep existing exports.

No new files; this is small enough to live alongside the existing types in `session.ts`.

## Tests

No new runtime tests in this chunk — there's no behavior yet, only types. A small type-only spot check (perhaps via a `tests/types.test-d.ts` or just a TS-checked `.ts` example) could confirm that:

- `FxtrUpdate<number>` accepts both `5` and `(prev: number, ctx: FxtrSessionUpdateCtx) => number`.
- `FxtrSessionAtom<{ foo: string }>` is assignable to `WritableAtom<{ foo: string }, [FxtrUpdate<{ foo: string }>, UserStateWriteOptions?], void>`.

If a type-only test runner isn't already wired up in the package, defer this to chunk 2 where connector-side adoption gives us real call sites that exercise the types.

## Implementation steps

1. [x] Add `FxtrSessionUpdateCtx`, `FxtrUpdate<T>`, `FxtrSessionAtom<T>` to `frontend_lib/packages/fxtr-ui-core/src/session.ts`. Place near the existing `UserStateWrite` / `UserStateWriteOptions` types; add brief JSDoc cross-references.
2. [x] Update `frontend_lib/packages/fxtr-ui-core/src/index.ts` to re-export the new types.
3. [x] Sanity-build: `pnpm check` from `v2/ts`. Confirm both `@fxtr-ui/core` and `@fxtr-ui/connector-workstation` compile clean; `pnpm test` + `pnpm lint` also green.
4. [x] Update CODE_OVERVIEW.md — note the new types in the relevant section, with a one-line forward reference to chunk 2 for adoption.

## What chunk 2 will do with this

- Replace `UserStateWrite` (in `@fxtr-ui/core`'s `session.ts`) with `FxtrUpdate<UserState | null>`. The existing type alias either stays as a deprecation re-export or gets removed outright; decide during chunk-2 planning.
- Retype `Session.userState` to `FxtrSessionAtom<UserState | null>`.
- Remove `allocateLoad` / `allocateInvoke` / `allocateCallback` from the abstract `Session` interface entirely.
- In `WorkstationSession`: make `allocateLoad` / `allocateInvoke` / `allocateCallback` `private`. The reducer keeps calling them via `this.X`; the new ctx wrapper also calls them via `this.X`.
- In `WorkstationSession.userState`'s write fn: when `value` is a function, call it with `(prev, ctx)` where ctx is a constant-per-session lazily-constructed object wrapping the private methods. Static values keep working unchanged.

These are all small mechanical changes once chunk 1's types are in place.
