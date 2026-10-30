# Stage 10c — Dynamic dispatch improvements

Builds on [stage 10b](./stage10b_dynamic_dispatch.md). Three
improvements that make the dynamic dispatch system usable from the
full workstation shell, debuggable, and hot-reloadable:

1. **Shell integration.** Make a `DynamicDispatcher` usable directly
   as the `dispatcher` prop of `<WorkstationShell>`, so dynamically-
   loaded renderer modules can serve the workstation's `FullPanel` /
   `BlockLink` / `InlineLink` / `HoverPreview` slots.
2. **Dispatcher debug panel.** Optional `debugView()` method on the
   `Dispatcher` interface that returns a self-bound React element.
   Static + dynamic dispatchers both provide implementations; the
   workstation shell exposes the view via a new dockview panel kind
   reached from a button on the existing `WorkstationDebugPanel`.
3. **Hot reload.** New `subscribe_to_changes(listener)` method on
   `WorkstationDispatcherProto`. The backend implementation
   broadcasts `ReloadEverythingNotification`s through registered
   listeners; the frontend dispatcher reacts by wiping per-type
   session caches and re-resolving every entry. A new staleness
   signal on each entry lets `<RenderDispatch>` dim stale components
   at 50% opacity during the transition, so users don't see a flash.

The existing toy `@fxtr-ui/dynamic-dispatch-demo` is rewritten
in-place to be a workstation-shell-based demo seeding Note / ListNode
entities and three demo services, mirroring `@fxtr-ui/workstation-demo`
but with the renderers loaded dynamically.

## Why these changes

- **Shell integration.** Today the dynamic dispatcher works with the
  toy `DispatchGrid` only — the workstation shell has never been
  driven by it. To use it for real we need the workstation's four
  dispatch keys (`FullPanel` etc.) to be reachable from inside a
  renderer module compiled at runtime. The keys carry hardcoded
  UUIDs + non-trivial Props types; the renderer module needs both.
- **Debug panel.** The static dispatcher's behaviour is fully
  determined at construction (just look at the registry). The dynamic
  dispatcher's behaviour evolves at runtime through four
  session-backed caches + an in-memory compiled-module map + a
  per-entry state machine — none of which is currently inspectable.
  A reactive debug view makes the system tractable to debug live.
- **Hot reload.** Renderer code is part of the agent's loop. Iteration
  speed matters. Without hot reload, the user has to manually reload
  every tab to see code changes.

## Improvement 1 — Shell integration

### `@fxtr-ui/workstation-dispatch` (new package, externalized)

A renderer-author-facing package holding the canonical dispatch
surface for the workstation. Externalized via the v1 externals list,
so the host's instance is shared by both the workstation shell and
any runtime-loaded renderer module — one `DispatchKey` identity per
UUID, one fallback set, one `RefLink` component, one `JsonRefTree`.

```
frontend_lib/packages/fxtr-ui-workstation-dispatch/
├── package.json
├── tsconfig.json
└── src/
    ├── keys.ts                  # FullPanel / BlockLink / InlineLink / HoverPreview
    ├── props.ts                 # FullPanelProps / LinkProps / HoverPreviewProps
    ├── fallbacks/               # the per-key Loading / NoMatch / Error components
    ├── reflink/
    │   ├── RefLink.tsx          # thin component that forwards to the context impl
    │   └── impl_context.tsx     # RefLinkImplContext + DefaultRefLinkImpl
    ├── metadata/                # JsonRefTree, EntityMetadataView, ServiceMetadataView
    └── index.ts                 # re-exports
```

Exports:

- The four canonical `DispatchKey<...>` objects + Props types.
- The fallback components (one canonical implementation each).
- `RefLink` + `RefLinkProps` + `RefLinkImpl` + `RefLinkImplContext`
  + `DefaultRefLinkImpl`.
- `JsonRefTree`, `EntityMetadataView`, `ServiceMetadataView`.

Runtime deps: `react`, `@fxtr-ui/core`, `@fxtr-ui/react`,
`@fxtr-ui/react-dispatch`, `@fxtr-ui/state-util`, `jotai`. No
`@base-ui/react`, no `dockview`, no shell internals.

### `RefLink` as a context-delegated component

`RefLink` is a one-line forwarder: it reads a `React.ComponentType`
out of `RefLinkImplContext` and renders it with the props it
received. The impl decides what to wrap around the dispatched link
— no fixed taxonomy of "click/drag/hover/menu" baked into the
package, just a "render however you want around the contents"
contract.

```ts
export interface RefLinkProps {
  readonly stub: EntityRefStub | ServiceRefStub;
  readonly as: "inline" | "block";
  readonly linkText?: string;
}

export type RefLinkImpl = React.ComponentType<RefLinkProps>;

// Default: render the dispatched link with no extra wrappers.
const DefaultRefLinkImpl: RefLinkImpl = ({ stub, as, linkText }) => (
  <RenderDispatch
    as={as === "inline" ? InlineLink : BlockLink}
    stub={stub}
    linkText={linkText}
  />
);

export const RefLinkImplContext =
  React.createContext<RefLinkImpl>(DefaultRefLinkImpl);

export function RefLink(props: RefLinkProps) {
  const Impl = useContext(RefLinkImplContext);
  return <Impl {...props} />;
}
```

The default impl handles the renderer-only / no-shell-mounted case
gracefully: a renderer module's `<RefLink>` renders a plain dispatched
link (interactive at the `RenderDispatch` level — i.e. resolves to
the type-specific InlineLink/BlockLink renderer if one is registered,
else falls through to the canonical fallback), with no drag / hover /
menu affordances. Useful both for headless tests and as a sensible
"this got rendered outside the shell" default.

### Shell-side `@fxtr-ui/workstation`

Stays where it is, **NOT externalized**. Drops the duplicated keys +
fallbacks + RefLink internals (now provided by
`@fxtr-ui/workstation-dispatch`) and instead provides:

- `ShellRefLinkImpl` — a `RefLinkImpl` component encapsulating the
  current drag / click / hover / context-menu behavior. Reads from
  `PanelContext`, `WorkstationContext`, the `HoverCloseContext`;
  uses `@base-ui/react` for the popover + menu primitives; renders
  whatever wrapper tree it wants around the dispatched
  `<RenderDispatch as={InlineLink} | {BlockLink}>` call.
- `<ShellRefLinkProvider>` — mounted near the top of
  `<WorkstationShell>`'s tree, sets
  `<RefLinkImplContext.Provider value={ShellRefLinkImpl}>`.

All current shell-only modules (`shell/`, `panel/`, `panels/`,
`sidebar/`, `breadcrumb/`, `persistence/`, `dispatch/`-registry-helper,
`reflink/use*` hooks, `reflink/DraggableRefHandle.tsx`,
`reflink/HoverContext.tsx`, `reflink/DebugRefChip.tsx`) stay.
`combined_fallbacks.tsx` is no longer needed (the package owns the
canonical fallbacks).

The shell re-exports `FullPanel` / `BlockLink` / `InlineLink` /
`HoverPreview` + Props from `@fxtr-ui/workstation-dispatch` for
backwards compatibility with existing demo code that imports them
from `@fxtr-ui/workstation`. New code can import from either.

### Externalization

`@fxtr-ui/workstation-dispatch` is added to the v1 externals list
(`DEFAULT_EXTERNALS` in
`fxtr_workstation/dynamic_dispatch/basic_dynamic_dispatch/compile.py`).
The host's `installFxtrExternals` call gains one entry mapping
`"@fxtr-ui/workstation-dispatch"` to the host's imported instance.
At runtime, a renderer module's
`import { FullPanel } from "@fxtr-ui/workstation-dispatch"` resolves
through the import map to the host's instance — the renderer and the
shell see the **same** `FullPanel` object.

A renderer module's source ends up shaped like:

```ts
import {
  FullPanel, type FullPanelProps,
  BlockLink, type LinkProps,
  InlineLink,
  RefLink, JsonRefTree,
} from "@fxtr-ui/workstation-dispatch";

const NoteFullPanel: DispatchedComponent<EntityRefStub, FullPanelProps> = ({stub, ownStorage}) => {
  // Can render <RefLink stub={childRef} as="inline" /> — picks up
  // the shell's drag/click/hover/menu behavior automatically via
  // RefLinkImplContext when mounted inside the shell, falls back
  // to a plain dispatched link when mounted bare.
};
const NoteBlockLink: DispatchedComponent<EntityRefStub, LinkProps> = ...;

export default {
  default: {
    [FullPanel.uuid]: NoteFullPanel,
    [BlockLink.uuid]: NoteBlockLink,
    // ... etc
  },
};
```

Note: importing `RefLink` from `@fxtr-ui/workstation-dispatch` doesn't
pull `@base-ui/react` or dockview into the renderer module. The
externals contract means the renderer's `RefLink` IS the host's
`RefLink`, and the host's `RefLink` reads its impl out of context —
so the heavy deps live entirely on the host side.

## Improvement 2 — Dispatcher debug panel

### `Dispatcher.debugView()` method

Optional method added to the `Dispatcher` interface in
`@fxtr-ui/react-dispatch/src/dispatcher.ts`:

```ts
interface Dispatcher {
  // ... existing fields ...
  debugView?(): React.ReactElement;
}
```

Method form (not property) so the dispatcher's `this` is already
bound when it returns the element — the closed-over atoms read from
the dispatcher's instance state without the consumer having to thread
the dispatcher reference through props. Each call returns a fresh
element; the consumer is responsible for the React subtree's
lifetime (typically by rendering it inside a dockview panel that
unmounts on close).

The method is optional; consumers branch on its presence to decide
whether to surface a "debug view" affordance for that dispatcher.

### Static dispatcher impl

Returns a small component that lists, per dispatch key, the entries
the registry holds. Reactively shows the current per-`(stub, key)`
entry cache (size + which `id`s are mounted). Useful for "is the
registry wired up the way I expected", "which entries have been
materialised so far", and "is the dispatcher in the closed state."

### Dynamic dispatcher impl

Reactive view of:

- **The four session-backed caches.** `service_type_cache`,
  `service_type_active_lookups`, `type_renderer_cache`,
  `type_renderer_active_lookups`. Rendered through `JsonRefTree`
  from `@fxtr-ui/workstation/metadata` so `EntityRefStub` leaves
  surface as `<RefLink>`s (or `<DebugRefChip>`s — TBD; the panel is
  inside the dispatcher's React context, so `<RefLink>` would work,
  but probably we want `<DebugRefChip>` to avoid spawning yet more
  dispatcher entries for the refs in the cache).
- **The in-memory compiled-module map.** One row per cached renderer
  short_id, with status (`loading` / `ready` / `errored`), the inner
  keys of the compiled module, and the listener health (see below).
- **Per-entry state.** One row per live cache entry: stub, key, phase
  string from `dispatcher_state`, component identity (the React
  component's `displayName` or `name`), staleness flag.

The dispatcher exposes the in-memory atoms (`compiledModulesAtom`,
the entry-cache map view) via a debug-only handle the component
reads from. The handle is **not** part of the public `Dispatcher`
interface — it's a private contract between the dispatcher's
constructor and the React component it returns from `debugView()`.

### Surfacing it in the workstation shell

New dockview panel kind `DispatcherDebugPanel` in
`fxtr-ui-workstation/src/panels/`:

- Reads the dispatcher off `<DispatcherProvider>` context.
- Calls `dispatcher.debugView()` and renders the result inside the
  panel.
- Subscribes to `dispatcher.status` so it can render a placeholder
  when the dispatcher is in `"closed"` / `"unavailable"`.

The existing `WorkstationDebugPanel` gains a button reading
"Open dispatcher debug" rendered only when
`dispatcher.debugView !== undefined`. Clicking it opens a new
dispatcher-debug dockview panel (find-or-focus by panel id, like
the existing "Workstation debug" button). Wired through the
existing `WorkstationApi`-on-shell pattern: add an `openDispatcherDebugPanel`
method to `WorkstationApi` parallel to `openSessionDebugPanel`,
implemented by the shell against dockview.

We don't surface the button on the sidebar — it's a sub-debug-tool
reached from the main debug panel, matching the user's flow of "start
from the normal debug panel and then drill in."

## Improvement 3 — Hot reload

### Protocol additions

New required method on `WorkstationDispatcherProto` (breaking change
for any other implementer; `BasicDynamicDispatchImpl` is the only one
today):

```python
async def subscribe_to_changes(
    self, listener: ServiceRef[EventListenerProto]
) -> None:
    """Register a listener that the dispatcher notifies whenever
    its renderer table changes. The notification payload is a
    `ReloadEverythingNotification` ref — the frontend should drop
    its per-type session caches and re-resolve every entry.
    """
```

New `DataclassEntity` carrying the notification payload — for v1 a
fresh empty entity, with later expansion in mind:

```python
@dataclass(frozen=True)
class ReloadEverythingNotification(
    DataclassEntity,
    fxtr_type=RELOAD_EVERYTHING_NOTIFICATION_TYPE_ID,
):
    """v1 reload notification: 'drop all per-type caches and
    re-resolve.' Empty body; future versions may add narrower
    signals (a list of changed type ids, a per-key reload, etc.)."""
```

### Backend impl pattern (mirrors `agent_mcp_impl.py`)

`BasicDynamicDispatchImpl` is restructured so that it:

1. Takes an `impl_tg: anyio.abc.TaskGroup` at construction (caller-
   owned), like `AgentMcpRunningSessionImpl`.
2. Maintains a `self._listeners: list[_ListenerState]` mirroring
   the MCP impl's `_watchers` list. Each `_ListenerState` carries
   `(key: ServiceKey, has_update: anyio.Event, done: bool)`. There
   is no per-listener `latest` field because the notification
   payload is constant (just "reload everything").
3. `subscribe_to_changes(listener)`:
   - Idempotency check by `ServiceKey` (same as MCP).
   - `await self._impl_tg.start(_listener_worker, st, self._scope, listener, call_scope)`.
   - On worker failure-before-`started()`, remove the entry.
4. New sync method `trigger_reload()` (called by callers like the
   demo's poller): reaps `done` listeners + sets `has_update` on
   every live listener.
5. New async method `recompile_changed_and_reload() -> bool`:
   walks `renderer_dirs`, computes the new (subdir → `index.tsx`
   mtime) map, compares to the stored snapshot, and (if anything
   differs) recompiles only the changed subdirs, mints new
   `RendererEntity`s in `self._scope`, updates `self._table`,
   drops table entries for vanished subdirs, then calls
   `trigger_reload()`. Returns `True` if anything changed.

`_listener_worker` mirrors `_watcher_worker` from `agent_mcp_impl`:

- Opens an isolated mountable `listener_scope` child of `parent_scope`.
- Bridges `listener` from `source_scope` into `listener_scope`.
- Signals `task_status.started()` so `subscribe_to_changes` can
  return.
- Loops:
  ```
  await state.has_update.wait()
  state.has_update = anyio.Event()   # replace before notify
  proxy = await listener.resolve(scope=listener_scope)
  notification = ReloadEverythingNotification().store(listener_scope)
  still_listening = await proxy.notify(notification)
  if not still_listening: return
  ```
- `finally`: set `state.done = True` so the next `trigger_reload`
  reaps it.

Dead-listener pickup is reactive only — we don't proactively probe
`listening()`. The next reload trigger finds a dead listener (notify
returns `False` or raises), the worker exits, `state.done` is set,
the subsequent reload reaps it. The cost is one wasted notify per
dead listener per reload — acceptable.

### Mounting helper (new context manager)

The impl needs an owner for its task group. We add:

```python
@asynccontextmanager
async def mount_basic_dynamic_dispatch(
    renderer_dirs: Sequence[Path],
    *,
    parent_scope: FxtrScope | None = None,  # defaults to GLOBAL_SCOPE
    externals: Sequence[str] = DEFAULT_EXTERNALS,
    subtype_key: str = DEFAULT_SUBTYPE_KEY,
) -> AsyncGenerator[MountedBasicDynamicDispatch, None]:
    """Open a scope + task group, construct the impl, compile +
    register all renderers, mount the impl, yield a handle. Cancels
    the task group on exit (which cancels all listener workers, which
    drops their scope bridges)."""
```

`MountedBasicDynamicDispatch` is a small dataclass-style handle:

```python
@dataclass
class MountedBasicDynamicDispatch:
    scope: FxtrScope           # for ExternalServiceConfig(scope=...)
    service_ref: ServiceRef[WorkstationDispatcherProto]  # mountable ref
    impl: BasicDynamicDispatchImpl                       # for trigger_reload / recompile_changed
```

Caller pattern (in the demo):

```python
async with mount_basic_dynamic_dispatch(renderer_dirs=[...]) as mounted:
    cfg = ExternalServiceConfig(
        external_name="renderer-dispatcher",
        service=mounted.service_ref,
        scope=mounted.scope,
    )
    async with WorkstationServer(external_services=[cfg]) as ws:
        async with anyio.create_task_group() as poll_tg:
            poll_tg.start_soon(_reload_poller, mounted.impl)
            # ... serve forever ...
```

The poller (lives in the demo, not in the dispatch package — it's
a debug aid, not a general primitive):

```python
async def _reload_poller(impl: BasicDynamicDispatchImpl, *, interval: float = 15.0):
    while True:
        await anyio.sleep(interval)
        try:
            changed = await impl.recompile_changed_and_reload()
            if changed:
                _log.info("dynamic dispatch: renderer changes detected; broadcast reload")
        except Exception:
            _log.exception("reload poller iteration failed; continuing")
```

### Frontend reload reception

`createDynamicDispatcher` is extended with:

1. **Reload-listener machinery.** At construction time:
   - Allocate a `CallbackInboxStub` via the session's
     `allocateCallback` ctx call (deferred until `lifecycle ===
     "connected"` — `WorkstationShell`'s `WaitForDispatcherReady`
     already gates the tree on that, but the dispatcher itself
     can't assume that, so it watches `session.lifecycle` and
     allocates the callback once connected).
   - Allocate a `subscribe_to_changes(inbox.handle)` invoke on
     `dispatchServiceRef`.
   - Subscribe to `inbox.events` (read-only atom). Each new event
     triggers the reload effect.
   - This runs once per dispatcher lifetime; the inbox is stored in
     `userState` under a new top-level field
     (`reload_inbox: CallbackInboxStub<ReloadEverythingNotification | EntityRefStub>`)
     so it survives reattach.

2. **Reload effect.** When a new event arrives, in one userState
   updater:
   - Empty `service_type_cache`, `service_type_active_lookups`,
     `type_renderer_cache`, `type_renderer_active_lookups`.
   - For each existing entry (walk the in-memory cache), set its
     in-memory `staleAtom` to `true` and reset its
     `dispatcher_state` back to `{phase: "init"}`. The state
     machine's effect re-runs immediately; the entry progresses
     through type discovery / renderer resolution / bytes load
     again.
   - Slice the inbox via `inbox.sliced(events.length)` so the
     reload doesn't refire on the next render.

3. **Staleness signalling — fully dispatcher-internal.** The
   dynamic dispatcher keeps a per-entry in-memory `staleAtom:
   PrimitiveAtom<boolean>`. It writes `true` on reload-trigger and
   the state machine writes `false` whenever it transitions to a
   terminal phase (`resolved`, `resolved-builtin`, `no-match`,
   `errored`). The atom never appears on the `DispatchEntry`
   interface — staleness is purely a dynamic-dispatcher concept,
   and the shared `<RenderDispatch>` surface doesn't grow to
   accommodate it.

### Wrap-on-write inside the dynamic dispatcher

Instead of teaching `<RenderDispatch>` about a new optional hook,
the dynamic dispatcher writes a *wrapped* component to its
existing `componentAtom`. `<RenderDispatch>` is unchanged; the
shared `DispatchEntry` interface is unchanged. The wrapping logic
is fully internal to `createDynamicDispatcher` — a future
dispatcher that wants similar chrome would do the same thing
locally, without touching the shared interface.

For each entry, the dispatcher constructs a small wrap helper that
takes a `DispatchedComponent` and returns an identity-stable
wrapped version, memoised per inner-component identity through a
`WeakMap`:

```tsx
function makeEntryWrapper(staleAtom: Atom<boolean>) {
  const cache = new WeakMap<DispatchedComponent, DispatchedComponent>();
  return (Component: DispatchedComponent): DispatchedComponent => {
    let wrapped = cache.get(Component);
    if (wrapped === undefined) {
      wrapped = (props) => {
        const stale = useAtomValue(staleAtom);
        return (
          <div style={stale ? STALE_STYLE : FRESH_STYLE}>
            <Component {...props} />
          </div>
        );
      };
      wrapped.displayName = `StaleAware(${Component.displayName ?? Component.name ?? "Component"})`;
      cache.set(Component, wrapped);
    }
    return wrapped;
  };
}

const FRESH_STYLE = { display: "contents" } as const;
const STALE_STYLE = { display: "block", opacity: 0.5 } as const;
```

`display: contents` ↔ `display: block` because `opacity` requires
a real box. The brief display-flip is the visible cue during
reload; the underlying component subtree is identity-stable across
flips.

**Where the wrap is applied:**

- At `createDynamicDispatcher.getEntry` time, the `initialComponent`
  passed to `createBaseEntry` is `wrap(fallbacks.Loading)` — so the
  very first component to land in `componentAtom` is already
  wrapped.
- Every `set(componentAtom, C)` site inside the dynamic state-
  machine effect factories writes `wrap(C)`. The existing
  short-circuit (`if (get(componentAtom) !== wrapped) set(componentAtom, wrapped)`)
  keeps working because the WeakMap means `wrap(C) === wrap(C)` is
  always true for a given C.

**Identity invariants preserved:**

- The wrapper's React identity is 1:1 with the inner component's
  identity (per entry), so React unmount/remount happens at the
  same boundary as it would without wrapping.
- The base entry's shared-state reset invariant ("componentAtom
  changes value ⇒ `sharedComponentStateAtom` resets to null in the
  same flush") fires exactly when the underlying component
  changes, because the wrapper identity tracks the inner one.
- Static dispatchers don't wrap — their `componentAtom` holds the
  raw component, with no extra DOM, no `useAtomValue` subscription,
  no wrapping overhead at all.

### Hot-reload no-flash behaviour

Key invariant: the dispatcher **never blanks** `componentAtom`
during a reload. The state machine writes `componentAtom` only in
the terminal arms (`resolved`, `resolved-builtin`, `no-match`,
`errored`) and the existing arms already short-circuit when the
target component value is already current. So during a reload:

- `dispatcher_state` flips from `resolved` → `init`. No
  `componentAtom` write.
- Re-run: `init` → `loading-entity` (or cached fast-paths). No
  `componentAtom` write.
- Eventually: terminal phase. Writes the resolved component (likely
  the same one as before, with reference-identity short-circuit
  swallowing the write). Clears `staleAtom`.

What the user sees: opacity goes to 0.5 briefly, the underlying
component never unmounts, then opacity goes back to 1. If a new
renderer module was uploaded, the new component swaps in
(re-mounts its subtree; that's expected because the React identity
changed).

## Improvement 4 (demo rewrite)

`@fxtr-ui/dynamic-dispatch-demo` is rewritten **in-place** to be a
workstation-shell-based demo:

- **Backend** (`src/dynamic_dispatch_demo_workstation.py`): seeds the
  same entity / service set as `@fxtr-ui/workstation-demo`
  (`_DemoNote`, `_DemoListNode`, three services), but instead of
  hard-mounting their renderers in TS, stages renderer directories
  into a temp dir keyed by the type-id strings and mounts a
  `BasicDynamicDispatchImpl` as the `renderer-dispatcher` external
  service. The poller is started in the same task group as the
  workstation server. The poller's interval (default 15s) is exposed
  as a CLI / env var so demo authors can crank it down for
  testing.
- **Renderer directories** under `src/renderers/`: one per type id,
  each containing an `index.tsx` whose default export is a
  `{ default: { [FULL_PANEL_UUID]: ..., [BLOCK_LINK_UUID]: ..., ... } }`
  map for the type. The renderers are forks of
  `@fxtr-ui/workstation-demo`'s `handlers.tsx` components.
- **Orchestrator** (`src/server.ts`): unchanged shape from the
  current dynamic-dispatch-demo; adjusts the stdout protocol to
  emit `DEBUG_UI` (matching the workstation-demo orchestrator) and
  forwards it through Vite as `VITE_DEBUG_UI_URL`.
- **Client** (`src/client/App.tsx`): replaces the current grid setup
  with a near-clone of `@fxtr-ui/workstation-demo`'s App.tsx —
  read/create dispatcher session, build the dynamic dispatcher,
  render `<WorkstationShell>`. The differences from the
  workstation-demo are confined to dispatcher construction
  (`createDynamicDispatcher` vs `createStaticDispatcher`) and the
  service-lookup-by-external-name step.
- The current `DispatchGrid` and per-type Card components are
  deleted; their concerns moved into the renderer modules' compiled
  shape.

`@fxtr-ui/workstation-demo` is unchanged. The two demos exist
side-by-side; both exercise the workstation shell, but one uses
static dispatch and one uses dynamic dispatch (with hot reload).

## Chunk plan

Chunks are organizational; CODE_OVERVIEW updates happen at the end
of the stage (with intermediate touch-ups on big sub-features).

### Chunk 1 — Backend protocol + impl restructure

Combined (small + dependent):

- Add `RELOAD_EVERYTHING_NOTIFICATION_TYPE_ID` +
  `ReloadEverythingNotification` to
  `fxtr_workstation/dynamic_dispatch/dynamic_dispatch_types.py`.
- Add `subscribe_to_changes(listener)` to
  `WorkstationDispatcherProto`.
- Restructure `BasicDynamicDispatchImpl`:
  - Add `impl_tg` constructor arg.
  - Add `_listeners` list + `_listener_worker` task (modeled on
    `_watcher_worker` in
    `projects/fxtr_workstation_agent_plugin/fxtr_workstation_mcp/fxtr_workstation_mcp/agent_mcp_impl.py`).
  - Add `trigger_reload()` (sync) + `recompile_changed_and_reload()`
    (async, mtime-checked).
  - Track the per-subdir mtime snapshot in `self._mtimes`.
- Add `mount_basic_dynamic_dispatch` context manager +
  `MountedBasicDynamicDispatch` handle.
- Update tests:
  - The existing service-impl tests need to be reframed against
    the new constructor (pass an `impl_tg`).
  - New tests: listener registration + idempotency, reload trigger
    fires every live listener exactly once, dead listener is reaped
    on the next trigger, mtime-based recompile is a no-op when
    nothing changed and recompiles + triggers when an `index.tsx`
    mtime advances.

### Chunk 2 — `@fxtr-ui/workstation-dispatch` (package split + RefLink refactor)

This is a real workstation-package refactor, not a constant-extract.
End state: there is exactly one canonical `DispatchKey` / fallback /
`RefLink` / `JsonRefTree` identity per UUID, shared via externals
between the shell and runtime-loaded renderer modules.

Steps:

1. **Create the new package** `frontend_lib/packages/fxtr-ui-workstation-dispatch/`
   with `package.json`, `tsconfig.json`, empty `src/`. Workspace
   wiring (`pnpm-workspace.yaml` picks it up; tsconfig references
   from the shared base + from `@fxtr-ui/workstation`'s tsconfig).

2. **Refactor `RefLink` behind a context.**
   - New `src/reflink/impl_context.tsx` exports `RefLinkImpl` type,
     `RefLinkImplContext`, and `DefaultRefLinkImpl` (the latter
     just calls `<RenderDispatch as={InlineLink | BlockLink} ...>`).
   - New `src/reflink/RefLink.tsx` exports `RefLink` (one-line
     forwarder: read context, render impl with received props).
     Same `RefLinkProps` shape as today.
   - `@fxtr-ui/workstation` (shell-side):
     - New `src/reflink/ShellRefLinkImpl.tsx` — a `RefLinkImpl`
       encapsulating today's interactive behavior (drag / click /
       hover / context-menu). Reads `PanelContext`, `WorkstationContext`,
       `HoverCloseContext`; uses `@base-ui/react` for popover + menu.
       Pulls in `useDraggableRef`, `useRefClick`, `useRefContextMenu`,
       `useRefHoverCard` — the existing hooks, mostly unchanged,
       just consolidated under one component.
     - New `<ShellRefLinkProvider>` mounted by `<WorkstationShell>`
       near the top of its tree, sets the context.
     - Delete the shell's standalone `src/reflink/RefLink.tsx` —
       superseded by the package's version + the shell's impl.

3. **Move the keys + Props + fallbacks** (`src/dispatch/keys.ts`,
   `src/dispatch/combined_fallbacks.tsx`, `src/fallbacks/*`) from
   `@fxtr-ui/workstation` into `@fxtr-ui/workstation-dispatch`.
   Now that `RefLink` lives in the same package, the `FullPanel`
   fallbacks' use of `JsonRefTree` → `RefLink` resolves cleanly
   inside the package (no cross-package dep cycle). The
   `combined_fallbacks` branch-on-instanceof wrappers stay; the
   per-kind fallbacks they delegate to stay too. The `dispatch/`
   directory in `@fxtr-ui/workstation` keeps only the registry
   helper (`registry.ts`, which is shell-specific in spirit but
   technically reusable; can re-export from there or relocate
   later).

4. **Move `JsonRefTree`, `EntityMetadataView`, `ServiceMetadataView`**
   from `@fxtr-ui/workstation/src/metadata/` into
   `@fxtr-ui/workstation-dispatch/src/metadata/`. These all use
   `RefLink`, so they belong with it.

5. **Re-export shims in `@fxtr-ui/workstation`.** The package
   re-exports `FullPanel` / `BlockLink` / `InlineLink` / `HoverPreview`
   + Props types + `RefLink` / `JsonRefTree` / `EntityMetadataView` /
   `ServiceMetadataView` from `@fxtr-ui/workstation-dispatch` so
   existing demo code (and the workstation-demo's `handlers.tsx`)
   keeps working without source changes.

6. **Add to externals.** Update
   `fxtr_workstation/dynamic_dispatch/basic_dynamic_dispatch/compile.py`
   so `DEFAULT_EXTERNALS` includes `"@fxtr-ui/workstation-dispatch"`.
   Update the rewritten `@fxtr-ui/dynamic-dispatch-demo`'s
   `main.tsx` to inject the new entry into its
   `installFxtrExternals` call. `@fxtr-ui/workstation-demo` is
   static-dispatch-only and doesn't load any external code at
   all, so it doesn't need to call `installFxtrExternals` — no
   change there.

7. **Tests:**
   - Snapshot test the exported UUID constants (wire-stable; same
     check that lived in the shell's keys test before).
   - Smoke test that `RefLink` renders the default impl when no
     `RefLinkImplContext` is provided, and renders the override
     when one is supplied — verifies the context plumbing.
   - Verify the package's runtime deps stay limited to `react` +
     `@fxtr-ui/core` + `@fxtr-ui/react` + `@fxtr-ui/react-dispatch` +
     `@fxtr-ui/state-util` + `jotai` (CI check: a `pnpm why
     @base-ui/react` / `pnpm why dockview` against the package
     should report no matches).
   - Existing workstation tests stay green; ones that touch
     RefLink may need provider-wrapping if they exercised the
     interactive paths.

### Chunk 3 — Dynamic dispatcher wrap-on-write + staleness atom

- **No changes** to the `DispatchEntry` interface, `<RenderDispatch>`,
  `createBaseEntry`, or the static dispatcher.
- Inside `createDynamicDispatcher`'s `getEntry`:
  - Mint a per-entry private `staleAtom: PrimitiveAtom<boolean>`.
  - Build a per-entry `wrap` helper via `makeEntryWrapper(staleAtom)`
    (WeakMap-memoised so `wrap(C) === wrap(C)`).
  - Pass `wrap(fallbacks.Loading)` to `createBaseEntry`'s
    `initialComponent`.
  - Thread `wrap` + `staleAtom` into the entity / service
    effect-factory builders so they can wrap on each
    `set(componentAtom, ...)` and clear `staleAtom` at terminal
    transitions.
- Inside the state-machine effect factories, every
  `set(componentAtom, C)` call site is rewritten to
  `set(componentAtom, wrap(C))`. The existing
  `if (get(componentAtom) !== wrapped) set(componentAtom, wrapped)`
  short-circuit keeps working because the WeakMap means
  `wrap(C)` is referentially stable.
- Terminal phases (`resolved`, `resolved-builtin`, `no-match`,
  `errored`) also call `set(staleAtom, false)` so the wrapper drops
  the dimming once the new component lands.
- The reload reception path (see Chunk 4) is what writes
  `set(staleAtom, true)` per entry.
- Tests:
  - Wrap a hand-built component, render it, flip the staleAtom,
    assert the surrounding div's style changes from
    `display: contents` to `display: block; opacity: 0.5` and back.
  - Wrap the same inner component twice — assert reference
    equality (WeakMap memo works).
  - Wrap two different inner components — assert distinct wrapped
    identities.
  - Inside the dynamic state machine, exercise a phase transition
    that writes `componentAtom`; assert the value is the wrapped
    component, not the bare one. Assert that consecutive writes
    of the same inner component don't trigger
    `sharedComponentStateAtom` resets (identity-stable wrap).

### Chunk 4 — Frontend hot reload reception

- Extend `DynamicDispatcherUserState` with a `reload_inbox` field
  holding a `CallbackInboxStub` (typed as receiving entity refs;
  the wire decodes notifications to `EntityRefStub` instances —
  the dispatcher just cares about the count).
- In `createDynamicDispatcher`, add a session-bound effect that:
  - Watches `session.lifecycle` for `"connected"`.
  - When connected and `reload_inbox` is null, allocates a fresh
    callback via `ctx.allocateCallback` and invokes
    `dispatchServiceRef.subscribe_to_changes(inbox.handle)` in the
    same flush.
  - If `reload_inbox` is non-null but its `state` atom reads
    `"closed"`, that's a bug — holding the callback slot in
    userState is supposed to keep it open. Log a `console.warn`
    with the inbox slot + dispatcher session id so it surfaces
    during development, then proceed as if it were null (allocate
    a fresh callback) so the system recovers rather than getting
    stuck.
  - When `inbox.events.length > 0` (events delivered):
    1. First, **sync in-memory pass**: for every entry currently in
       the dispatcher's cache, `session.store.set(entry.staleAtom,
       true)`. This gives immediate visual feedback at 0.5 opacity
       before the userState batch lands.
    2. Then, in one userState updater: wipe the four
       session-backed caches, reset every cached entry's
       `dispatcher_state` to `init`, and call
       `inbox.sliced(events.length)` to drop consumed events from
       the cursor.
- Update the per-entry state machine: on every terminal write to
  `componentAtom`, clear `staleAtom` to `false`. Terminal phases
  are `resolved`, `resolved-builtin`, `no-match`, `errored`.
- **Test infrastructure: a dedicated test-harness Python backend.**
  We don't want frontend tests to rely on timing or on the demo's
  mtime poller (the demo backend is throwaway-ish and shouldn't be
  on the critical path of frontend test correctness). Instead, the
  frontend tests use a purpose-built harness Python script that:
  - Boots a `WorkstationServer` + mounts a
    `BasicDynamicDispatchImpl` (or a thinner test impl that shares
    the listener-worker code — call out at impl time which makes
    more sense). The renderer table can be pre-staged with
    hand-built type-id → renderer-entity mappings, or accept
    table updates via the stdin protocol below.
  - Reads commands from stdin in a background task. Initial
    command set:
    - `TRIGGER_RELOAD\n` — calls `impl.trigger_reload()` and
      writes `OK\n` to stdout.
    - (Future) `SET_RESOLUTION <type_id> <subtype_key> <renderer_short_id>\n`
      — repopulates a row in the resolution table, useful for
      tests that want to assert "after reload, the new
      resolution lands."
  - Emits the workstation `READY <url>` line on startup, same as
    the other harness scripts.
  - **No mtime polling.** The demo backend (Chunk 7) has the
    mtime poller; the harness backend doesn't.
- Tests (deterministic, no sleeps beyond awaiting events):
  - `subscribe_to_changes` invoke fires exactly once after
    connection.
  - Stale-inbox case: construct a userState with a `reload_inbox`
    whose committed state says `closed`; verify the dispatcher
    logs the warning and re-allocates.
  - Send `TRIGGER_RELOAD` to the harness backend, await the inbox
    event arrival, assert: the cache wipe lands, each cached
    entry's `dispatcher_state` resets to `init` and its
    `staleAtom` flips to `true` mid-flight, then back to `false`
    once the next terminal phase commits.
  - Cursor advance via `sliced` produces the expected
    `UnpreserveEventsOp` (asserted via the session-debug stream).

### Chunk 5 — Dispatcher debug view

- Add optional `debugView?(): React.ReactElement` to the
  `Dispatcher` interface.
- Implement it on the static dispatcher: lists known dispatch keys
  + per-key registry presence + mounted entry cache size + dispatcher
  status.
- Implement it on the dynamic dispatcher: shows the four session-
  backed caches (via a `JsonRefTree` or simple table), the in-memory
  compiled-module map + listener health, and the per-entry phase /
  staleness / component-identity table. Components live in
  `fxtr-ui-react-dispatch/src/static_debug_view.tsx` /
  `dynamic_debug_view.tsx`.
- The dynamic-dispatcher debug component subscribes via
  `useAtomValue` to:
  - The four `subAtom`s already on the dispatcher
    (`serviceTypeCacheAtom`, etc.).
  - `compiledModulesAtom`.
  - A per-entry list of entry IDs (read from a small dispatcher-
    side primitive atom that we add to track the cache contents
    reactively — the existing `Map<string, AnyDispatchEntry>` isn't
    jotai-reactive).
- Tests: smoke render against a minimal dispatcher; assert the
  panels show what they should.

### Chunk 6 — Workstation shell wiring

- Add `DispatcherDebugPanel` + `DispatcherDebugPanelParams` to
  `fxtr-ui-workstation/src/panels/`.
- Extend `WorkstationApi` with `openDispatcherDebugPanel()`.
  Implementation in `WorkstationShell.tsx` mirrors
  `openWorkstationDebugPanel` (find-or-focus by stable panel id).
- Add a button to `WorkstationDebugPanel` that calls
  `workstation.openDispatcherDebugPanel()`. The button only renders
  when `dispatcher.debugView !== undefined`.
- Tests: visual / smoke only — the panel registers as a dockview
  component, the button shows/hides based on `debugView`'s
  presence.

### Chunk 7 — Rewrite `@fxtr-ui/dynamic-dispatch-demo`

- Replace `src/dynamic_dispatch_demo_workstation.py` with one that
  seeds the workstation-demo's entity / service set + mounts the
  dispatcher via `mount_basic_dynamic_dispatch` + spawns the reload
  poller.
- Replace `src/renderers/{note,photo}/index.tsx` with workstation-
  shell-style renderer modules under `src/renderers/` (one
  directory per type-id, each `index.tsx` exporting the
  four-dispatch-key map). The renderer source forks from
  `@fxtr-ui/workstation-demo`'s `handlers.tsx`.
- Replace `src/client/App.tsx` with a workstation-shell-based one
  mirroring `@fxtr-ui/workstation-demo`'s App.tsx but constructing
  a `DynamicDispatcher` instead of a `StaticDispatcher`.
- Delete `src/client/DispatchGrid.tsx` + `src/client/dispatch_keys.ts`
  + `src/client/cards/` — superseded by the workstation shell.
- Update `src/server.ts` orchestrator to thread through
  `DEBUG_UI`.
- Adjust the live integration test:
  - The existing `dynamic_integration.test.ts` is grid-shaped.
    Replace it with a smoke test that boots the demo backend,
    renders the shell against the live dispatcher, and asserts
    that one panel resolves through the dynamic dispatch. No
    reload roundtrip here — reload-path correctness lives in the
    Chunk-4 tests against the deterministic harness backend; this
    test exists only to confirm the demo's wiring stays plausible.
  - The demo's mtime poller is intentionally not exercised by any
    test. It's iteration-loop ergonomics for human developers,
    and the contract it depends on (`trigger_reload()` correctness)
    is covered by Chunk 1's unit tests.

### Chunk 8 — CODE_OVERVIEW updates

Two files (per the existing memory: describe current state, no
chunk / stage cross-refs in the body):

- `projects/fxtr_workstation/CODE_OVERVIEW.md` — update the
  `dynamic_dispatch/` section to mention `subscribe_to_changes`,
  the listener-worker pattern, `mount_basic_dynamic_dispatch`,
  the `ReloadEverythingNotification` shape.
- `projects/fxtr_workstation/frontend_lib/CODE_OVERVIEW.md` —
  describe `@fxtr-ui/workstation-dispatch-types`, the
  `Dispatcher.debugView()` surface, the staleness signal +
  `<RenderDispatch>` wrapper, the dynamic dispatcher's reload
  reception, the workstation shell's dispatcher-debug panel kind,
  and the new shape of `@fxtr-ui/dynamic-dispatch-demo`.

## Open questions (deferred)

- **Per-type reload notifications.** v1 is "drop everything." A
  later version may add `ReloadTypeIdsNotification(type_ids: list[str])`
  or `ReloadKeysNotification(dispatch_key_uuids: list[str])` for
  narrower invalidation. Wire shape kept simple for v1; expanding
  later is additive.
- **Renderer authoring story for dispatch keys not in the
  workstation shell's set.** Today `@fxtr-ui/workstation-dispatch-types`
  ships only the workstation shell's keys. If a third party wants
  dynamic dispatch for their own custom keys, they'll either need a
  parallel small-types package or to publish a v2 of the
  workstation-dispatch-types package. Out of scope.
- **Stale flash on first paint.** A freshly-attached session that
  carries a terminal-phase dispatcher_state in userState but a
  fresh in-memory `compiledModulesAtom` re-fetches the renderer.
  During that re-fetch the entry briefly shows the dispatch key's
  `Loading` fallback (no previous component to dim). We could
  preserve the previous render across reload by riding it through
  the persistence cap, but for v1 the reattach case loses no-flash
  benefits and just shows Loading like a fresh dispatch would.
- **Debug-view authentication.** The debug view exposes potentially-
  sensitive dispatcher internals (cache contents include refs).
  v1 assumes the user is trusted; no gating. If we later expose
  workstations across trust boundaries, the debug view should be
  permission-gated.
- **Cross-tab reload coordination.** If two tabs each subscribe to
  the same dispatcher service, each gets its own callback inbox + a
  separate notification per reload. Already correct (each frontend
  is independent), but if we want a tab to "lead" the reload + the
  others to follow without re-fetching renderer bytes, that's
  future work.

## Design notes to consult

- [`stage10b_dynamic_dispatch.md`](./stage10b_dynamic_dispatch.md)
  — the dispatcher this stage extends.
- `projects/fxtr_workstation_agent_plugin/fxtr_workstation_mcp/fxtr_workstation_mcp/agent_mcp_impl.py`
  — reference implementation for the per-listener worker pattern
  (`_watcher_worker` + `_WatcherState`).
- `projects/fxtr_workstation/fxtr_workstation/persistence.py`
  (`persistence_cap_worker`) — alternative reference for the same
  pattern but at the workstation-internal-service mount point.

## Implementation tasks

- [x] **Chunk 1** — Backend protocol + impl restructure (combined).
- [x] **Chunk 2** — `@fxtr-ui/workstation-dispatch` (package split + RefLink refactor).
- [x] **Chunk 3** — Dynamic dispatcher wrap-on-write + staleness atom.
- [x] **Chunk 4** — Frontend hot reload reception.
- [x] **Chunk 5** — Dispatcher debug view.
- [x] **Chunk 6** — Workstation shell wiring (debug panel kind + button).
- [x] **Chunk 7** — Extend `@fxtr-ui/dynamic-dispatch-demo` with a second `?shell=1` route (the original `/` route + `<DispatchGrid>` stays for back-compat with the existing integration test).
- [x] **Chunk 8** — CODE_OVERVIEW updates.
