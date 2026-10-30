# Stage 4, Chunk 2 — Ctx-style writes + abstract Session cleanup

The new types from chunk 1 (`FxtrSessionUpdateCtx`, `FxtrUpdate`, `FxtrSessionAtom`) become load-bearing here. `Session.userState` retypes to `FxtrSessionAtom<UserState | null>`; `allocate*` move off the abstract surface and become private on `WorkstationSession`; the workstation reducer's write fn passes ctx to function-form updates.

No backwards-compat shims. `UserStateWrite` is deleted outright; tests rewritten to call allocate methods via ctx.

The high-level chunk description lives in [`stage4_react_bindings.md`](./stage4_react_bindings.md).

## Scope

In:

- Delete `UserStateWrite` from `@fxtr-ui/core`'s `session.ts` and its re-export.
- Retype `Session.userState` to `FxtrSessionAtom<UserState | null>` (write signature uses `FxtrUpdate`).
- Delete `allocateLoad` / `allocateInvoke` / `allocateCallback` from the abstract `Session` interface.
- In `WorkstationSession`: make `allocateLoad` / `allocateInvoke` / `allocateCallback` `private`. Construct a per-session `FxtrSessionUpdateCtx` instance once (in the constructor) that delegates to those private methods.
- In `WorkstationSession`'s `userState` custom-atom write fn: when the value is a function, call it with `(prev, ctx)` instead of just `(prev)`. Static values still work unchanged.
- Update all existing test files that use `session.allocate*` to use the ctx-style pattern: `store.set(session.userState, (prev, ctx) => ({ ...prev, x: ctx.allocateLoad(ref) }))`. Files affected: `invoke_reducer.test.ts`, `slot_reducer.test.ts`, `session.test.ts`, `integration.test.ts`.
- Update small JSDoc references on `WorkstationSession`'s public surface (`terminate`'s docstring, etc.) where they mention `allocate*` as public.

Out (deferred):

- Per-session jotai store. → Chunk 3.
- `@fxtr-ui/state-util` package, `subAtom`. → Chunk 4.
- React layer. → Chunk 5.
- Adding new tests beyond translating existing ones.

## Implementation outline

### `@fxtr-ui/core/src/session.ts`

1. Delete the `UserStateWrite` type alias.
2. Retype `Session.userState`:

   ```ts
   readonly userState: FxtrSessionAtom<UserState | null>;
   ```

3. Delete the abstract `allocateLoad` / `allocateInvoke` / `allocateCallback` declarations and their JSDoc. The same JSDoc effectively lives on the corresponding `FxtrSessionUpdateCtx` methods already.
4. Update the existing JSDoc on `Session.userState`'s comment block to reference `FxtrUpdate` / `FxtrSessionUpdateCtx` for the write side.
5. Drop the now-unused docs around `userState`'s old `UserStateWrite` signature.

### `@fxtr-ui/core/src/index.ts`

Remove the `UserStateWrite` re-export. Keep the new types exported.

### `@fxtr-ui/connector-workstation/src/session.ts`

1. In the constructor, after the atoms are wired up, build:

   ```ts
   this.updateCtx = {
     allocateLoad: <T>(ref: EntityRefStub<T>) => this.allocateLoad(ref),
     allocateInvoke: <P, M extends MethodNames<P>>(...args) =>
       this.allocateInvoke<P, M>(...args),
     allocateCallback: <R>() => this.allocateCallback<R>(),
     session: this,
   };
   ```

   `updateCtx: FxtrSessionUpdateCtx` is a private readonly field.
2. Change the visibility of `allocateLoad` / `allocateInvoke` / `allocateCallback` from `public` (implicit, via `Session` implementation) to `private`. Drop the `override`/Session-implements docs; the methods are now an internal protocol shared between the reducer and `updateCtx`.
3. In the `userState` custom-atom write fn, retype the value param to `FxtrUpdate<UserState | null>` and pass `(prev, this.updateCtx)` when calling a function-form value.
4. In `handleWrite`, same retyping.
5. Strip `UserStateWrite` from the imports in this file.

### Tests

For each `session.allocate*` call site, rewrite to ctx-style:

- Common pattern:
  ```ts
  const stub = session.allocateInvoke(svc, "echo", [1]);
  store.set(session.userState, { result: stub });
  ```
  →
  ```ts
  let stub!: OpResultStub<...>;
  store.set(session.userState, (prev, ctx) => {
    stub = ctx.allocateInvoke(svc, "echo", [1]);
    return { ...(prev as any), result: stub };
  });
  ```
- "Allocate but don't store" pattern: rewrite as an updater that allocates and returns `prev` unchanged. The slot still advances; same semantics for the pendingSlotSpecs wipe.
- Error-throwing patterns: the throw now propagates from inside `store.set` (the updater throws). The assertion shape changes from `expect(() => session.allocateX(...)).toThrow(...)` to `expect(() => store.set(session.userState, (_, ctx) => ctx.allocateX(...))).toThrow(...)`.

Test files in scope:
- `tests/invoke_reducer.test.ts` (~35 sites)
- `tests/slot_reducer.test.ts` (~21 sites)
- `tests/session.test.ts` (~17 sites)
- `tests/integration.test.ts` (~12 sites)

If a test helper makes sense (one is unlikely; most call sites are too varied), it lives in the existing test-helpers module.

### CODE_OVERVIEW.md

Update the `session.ts` section to reflect:
- `Session.userState` is now `FxtrSessionAtom<UserState | null>` (not `WritableAtom<..., [UserStateWrite, ...], void>`).
- The abstract `Session` no longer has `allocateLoad` / `allocateInvoke` / `allocateCallback`. The ctx-based path is now the only public surface.
- `UserStateWrite` no longer exists.
- The corresponding workstation-side section: `WorkstationSession` now has a private `updateCtx` field; its `allocate*` methods are private.

## Implementation steps

1. [x] Core type changes: delete `UserStateWrite`, retype `Session.userState`, delete abstract `allocate*` methods.
2. [x] Update `@fxtr-ui/core/src/index.ts` re-exports.
3. [x] Workstation session: build `updateCtx` in constructor, make `allocate*` private, update write fn signatures + delegation.
4. [x] Run `pnpm check`. Triage the cascade of errors (tests, src/types.ts references in JSDoc, etc.).
5. [x] Rewrite test files to use ctx-style allocations. Iterate file-by-file, run tests per file.
6. [x] `pnpm check && pnpm test && pnpm lint` all green.
7. [x] Update CODE_OVERVIEW.md.

## Notes from implementation

- Deleted the test `"allocate does not schedule a flush (no mutation sent)"` from `session.test.ts`: its underlying assumption ("bare allocate doesn't produce wire traffic") doesn't survive the API shift, since ctx-style allocates only exist inside `set(userState, …)` calls. Equivalent "no-`InvokeOp`-emitted-on-discard" coverage stays in `invoke_reducer.test.ts`.
- One related test (`"allocate without writing → no op emitted and no patch shipped"`) was adapted rather than deleted: it now checks that no `InvokeOp` lands in any mutation that does ship, since a `set(userState, prev => prev)` against a fresh-snapshot envelope (user undefined → null) can still produce a wire patch.
- `handleWrite` was not changed to detect that case as a no-op. We considered hiding the `undefined → null` envelope shift but decided against — it's a fundamental shape of the new API (allocates only run inside an actual `set`), and the affected test reads more honestly when it doesn't paper over that.

## What chunk 3 will do

- Move from "session uses the connector's shared store" to "each session mints its own jotai store" in `WorkstationConnector.createSession` / `attachSession`. Connector keeps its own store for resource lists / connection status. Test setups that share a store across sessions get updated.
