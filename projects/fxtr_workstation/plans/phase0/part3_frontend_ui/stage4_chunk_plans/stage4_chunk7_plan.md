# Stage 4, Chunk 7 — Demo: load

Extend the chunk 6 demo with entity-load mechanics. Each panel gets a "load demo-note" button that allocates a `LoadEntityOp` via ctx, stores the slot in the session's user state, and displays the eventual result through `useResult`. Two panels on the same session demonstrate the wire-level dedup: one `LoadEntityOp` shipped, both panels show the same filled value.

The high-level chunk description lives in [`stage4_react_bindings.md`](./stage4_react_bindings.md).

## Scope

In:

- Resolve the demo entity's short_id in `App`: subscribe to `connector.listEntities()`, find the record whose `external_names` contains `"demo-note"` (the name `demo_workstation.py` exposed it under), construct an `EntityRefStub` via `connector.entityRef(record.short_id)`.
- Pass the resolved entity ref + record (for showing the name) to each `Panel`. Until the entity is resolved, the panel's load button is disabled.
- `Panel`: add a "load demo-note" button that writes the allocated slot into `session.userState.loaded` via ctx. Render a `<LoadedDisplay>` sub-component when the slot is present.
- `LoadedDisplay`: `useResult(stub)` and render the discriminated union (`pending` / `filled` / `errored`). Filled shows the payload as JSON; errored shows the message.

Out (deferred):

- Picking which entity to load (UI for entity selection). The demo backend exposes only `demo-note` today; chunk 8 may add more.
- Multiple loads per panel.
- Invokes. → Chunk 8.
- Per-session-store isolation between panels viewing the same session_id (so they don't share component-internal atoms). Add when it's a real footgun in the demo.

## Design decisions

### Slot lives in `session.userState.loaded`

Extending the user-state shape from `{ message: string }` to `{ message?: string; loaded?: OpResultStub<unknown> }`. The slot is just another field in the dict.

The mutate handler writes via the existing `setUserState` pattern, but now uses the function form to allocate via ctx:

```ts
setUserState((prev, ctx) => ({
  ...(prev as Record<string, unknown> ?? {}),
  loaded: ctx.allocateLoad(entityRef),
}));
```

When the load completes, the same `OpResultStub<unknown>` stays in state; its `.result` atom transitions from `pending` to `filled`.

### `LoadedDisplay` is a sub-component so `useResult` is unconditional

React hooks can't be called conditionally, so reading `useResult` on the slot has to happen unconditionally in some component. The cleanest way: only mount `<LoadedDisplay stub={loaded} />` when `loaded` is present. Inside `LoadedDisplay`, the hook runs unconditionally.

This also keeps the Panel free of hook ordering concerns around the optional slot.

### Two-panels-same-session dedup

Two panels backed by separate `WorkstationSession` instances (via two `attachSession` calls) operate independently. Each panel can click "load" once, each calls `ctx.allocateLoad` on its own session, each writes a slot stub into its own session's user state.

The dedup happens at the workstation server: the second `LoadEntityOp` for the same short_id reuses the first's preserved fill (per `stage2_post1_wire_load_dedup.md`'s wire-visible `loadDedup` map — once one panel's load commits, the wire `loadDedup` shows `demo-note → slot_N`, and the second panel attaching to the same session_id sees that map in its snapshot and dedups locally).

Watching the workstation's bundled debug UI (the link in the demo's status bar) should show one entry-load worker spawn for the first request, none for subsequent ones.

### Entity resolution timing

`listEntities()` is reactive; the records list is empty until the workstation's subscribe-resources response lands. The load button should be disabled during this gap.

The simplest UI: pass a single `LoadTarget | null` prop from App down to each Panel. `LoadTarget = { ref: EntityRefStub<unknown>; name: string }` is null until resolved, then constant. The button text reflects state ("loading entities…" → "load demo-note").

## Implementation steps

1. [x] In `App.tsx`, subscribe to `connector.listEntities()`, find the demo-note record, build the `EntityRefStub`. Pass `{ ref, name, shortId }` to each `Panel` as `loadTarget` (nullable until resolved).
2. [x] In `Panel.tsx`: add the load button (disabled when `loadTarget === null`, `lifecycle !== "connected"`, or `loaded` is already set). Handler calls `setUserState((prev, ctx) => ({...prev, loaded: ctx.allocateLoad(loadTarget.ref)}))`.
3. [x] Add `<LoadedDisplay stub={loaded} />` sub-component. Read `useResult` + `useAtomValue(stub.committed)`, render the three branches. Filled → pretty-print the payload; errored → show the message; pending → italic placeholder.
4. [x] Conditionally render `<LoadedDisplay>` only when `userState.loaded` is set.
5. [x] `pnpm check && pnpm lint` green. (Manual smoke test pending — the user has been driving these.)
6. [x] Update `frontend_lib/CODE_OVERVIEW.md` `@fxtr-ui/demo` section.

## What chunk 8 will do

Add an "invoke demo-service.echo" (and friends) affordance per panel. Same pattern: button → `ctx.allocateInvoke(svc, "echo", [arg])` → slot stored in user state → `<InvokeResult>` sub-component using `useResult`. Backend already exposes the `demo-service` with `echo`/`combine`/`fail` methods.
