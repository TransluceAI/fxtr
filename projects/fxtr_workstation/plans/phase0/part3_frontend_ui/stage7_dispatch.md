# Stage 7 — Dispatch system

A new `@fxtr-ui/react-dispatch` package that brings the "declare a
rendering slot → look up the component for a given (slot, type) →
render it" pattern of the old `@fxtr/react-dispatch` (see
[`v2/ts/packages/fxtr-react-dispatch/LOCAL_CODE_OVERVIEW.md`](../../../../../../v2/ts/packages/fxtr-react-dispatch/LOCAL_CODE_OVERVIEW.md))
into the workstation/session world. Dispatches over an
`EntityRefStub` or a `ServiceRefStub` rather than a raw `FxtrID`,
and is built around the session/atom substrate from stages 1-6
rather than the older fxtr arena.

## High-level model

A consumer renders:

```tsx
<RenderDispatch stub={someRefStub} as={CardKey} title="hello" />
```

…inside a `<DispatchProvider>` subtree. The provider owns a
*dispatcher session* (separate from any consumer's session) and a
*registry* mapping `(DispatchKey, ref-kind) → effect-driven state
machine`. For each unique `(stub, dispatchKey)` pair the provider:

1. Maintains a session sub-atom holding the state-machine's
   internal state (load slots, metadata stubs, intermediate
   bookkeeping — anything `StateValue`-shaped).
2. Maintains a second session sub-atom holding the per-instance
   *shared cache* that the rendered component reads/writes via
   `useDispatchSharedAtom`.
3. Holds a local jotai atom whose value is the **component to
   render**, initialized to the dispatch-key's *fallback*
   component.
4. Mounts a `jotai-effect`-style effect that drives the state
   machine forward and writes the resolved component to the
   component-atom when it has one.
5. Auto-resets the shared cache atom to `null` whenever the
   component-atom's value changes — so when the resolution swaps
   in a new component, that component sees a fresh "default"
   shared state.

`<RenderDispatch>` looks up the entry for the current `(stub,
dispatchKey)`, subscribes to its component-atom, wraps the
resolved component in a per-key error boundary, and renders it.
The rendered component sees the stub, the dispatch-key's props,
and (via context) the shared atom and a read-only view of the
state machine's progress state. It does **not** see any
`Session` (the React `SessionContext` is set to `null` inside the
dispatch boundary), so it can't accidentally allocate slots into
a consumer's session.

## Key design decisions (resolved during planning)

- **Single `DispatchKey` declaration, two registry routes.** A
  `DispatchKey` is declared once and carries a type-level
  `Allowed extends "entity" | "service"` parameter narrowing
  which stub kinds it accepts. The full dispatch registry has
  separate route-tables for entity vs. service entries; the
  runtime route is picked by `stub instanceof EntityRefStub` /
  `ServiceRefStub`. This lets a single `CardKey` cover both kinds
  with independent fallback / error / effect-fn for each, while
  letting `EntityOnlyKey` statically reject service-typed stubs
  at `<RenderDispatch>`.

- **Dispatcher session passed in, not created.** The consumer
  builds (or attaches) a workstation session externally and
  passes it via `<DispatchProvider session={...}>`. The provider
  doesn't manage session lifecycle. (Future connector
  abstraction may relax this.)

- **Shared cache is a `FxtrSessionAtom<StateValue | null>`,
  lifted via `atomWithDefault` in the consumer.** The rendered
  component calls `useDispatchSharedAtom(default)` and gets a
  typed `FxtrSessionAtom<T>` — same surface as
  `useSessionRootStateAtom`. Function-form writes get `ctx`, so
  the rendered component can do nested loads / invokes whose
  result-slots stay alive via the dispatcher session.

- **State machine state is a `FxtrSessionAtom<S | null>` where
  `S` is the registry-entry author's choice.** Initial value is
  the entry's `initialState`. The effect-fn reads/writes this
  via function-form updates (so it can `ctx.allocateLoad`,
  `ctx.allocateGetEntityMetadata`, etc., inline).

- **Component-atom shared-cache reset.** The component-atom is
  exposed to the effect-fn as a custom-write wrapper: writing a
  value that is `!==` the previous value also writes `null` to
  the shared sub-atom in the same flush. This is how
  "rerender-with-new-component implies fresh defaults" is
  enforced structurally.

- **In-memory progress-message atom per entry.** Alongside the
  state-atom, the effect-fn receives a plain
  `WritableAtom<string, [string], void>` (in-memory, not session-
  backed). State machines write short human-readable status
  strings (`"loading entity…"`, `"looking up renderer…"`, etc.).
  Consumers read it via `useDispatchProgressMessage()` from a
  fallback or error component. This lets a *generic* fallback
  (defined alongside a dispatch key, but agnostic about which
  state-machine drives it) show a sensible loading message
  without knowing the machine's tagged-union state shape.
  `useDispatchProgressState<S>()` remains available for fallbacks
  that want the typed state.

- **No teardown / refcounting in v1.** Dispatch entries are
  created on first `<RenderDispatch>` for a `(stub, dispatchKey)`
  and persist until the provider unmounts. Rationale: robust
  subscriber tracking across rapid unmount/remount is fiddly;
  entries are small; the wire's existing load-dedup gives cheap
  re-acquisition anyway. Earmarked as future work — LRU or
  idle-eviction can slot in without changing the public surface.

- **No jotai-store override at the dispatch boundary.** The
  rendered component continues to read from whatever
  `<JotaiProvider>` is active in the surrounding tree. **The
  consumer must arrange for the parent tree and the dispatcher
  session to share a jotai store** (the default when neither is
  given a custom `{store}` option to `createSession` /
  `attachSession` is `connector.store`, which is the easy path).
  This will be documented; mismatched stores will produce
  silently-broken shared atoms, treated as user error.

- **Inner `SessionContext` set to `null`.** A rendered component
  calling `useSession()` throws — matches the existing
  `useSession()` behavior outside a provider. If the component
  wants a session for slot-allocation purposes, the path is via
  the `ctx` parameter on `useDispatchSharedAtom`'s function-form
  writes, not via `useSession()`.

- **Static registry helpers cover the common case.** A
  `staticEntityTypeRegistry(key, lookupMap, opts)` and
  `staticServiceTypeRegistry(key, lookupMap, opts)` build the
  "load → metadata-on-`$fxtr.type` → lookup" (entity) and
  "metadata → `type_id_string` lookup" (service) state machines
  on top of the underlying effect-fn primitive. They cover the
  initial-target use case in `05_dispatch_and_inline_loading.md`.

- **`useDispatchProgressState<S>()` hook for fallbacks.** The
  fallback component is co-located with the effect-fn that
  drives `S`, so the type is in scope. This hook gives the
  fallback (and optionally the error-boundary) a read-only view
  of the current `S` so it can render intermediate progress
  states ("loading entity…" → "looking up renderer…" etc.). The
  hook is generic and unchecked; consumers cast to the right
  `S` themselves. Sole boundary requirement: it must be called
  inside a `<RenderDispatch>` subtree.

## Public API sketch

```ts
// keys.ts
type RefKind = "entity" | "service";

interface DispatchKey<
  Props extends object,
  Allowed extends RefKind = RefKind,
> {
  readonly uuid: string;       // wire-stable identifier
  readonly description: string;
  // ...branded type-level fields...
}

// `uuid` is supplied by the caller (typically a hardcoded literal
// at module load) so the identifier survives bundler module reloads,
// session reattach, etc. — the in-memory map keying off it can be
// reconstructed without losing track of persisted entries.
function declareDispatchKey<Props>(uuid: string, desc: string): DispatchKey<Props, RefKind>;
function declareEntityDispatchKey<Props>(uuid: string, desc: string): DispatchKey<Props, "entity">;
function declareServiceDispatchKey<Props>(uuid: string, desc: string): DispatchKey<Props, "service">;

// registry_types.ts
interface DispatchKeyRegistryEntry<Stub, Props, S extends StateValue | null = StateValue | null> {
  effectFn: (params: {
    stub: Stub,
    stateAtom: FxtrSessionAtom<S>,
    componentAtom: WritableAtom<DispatchedComponent<Stub, Props>, [DispatchedComponent<Stub, Props>], void>,
    progressMessageAtom: WritableAtom<string, [string], void>,
  }) => (get: Getter, set: Setter) => Cleanup | void;
  initialState: S;
  initialProgressMessage?: string;  // defaults to ""
  Fallback: DispatchedComponent<Stub, Props>;
  ErrorBoundary: ErrorBoundaryComponent<Stub, Props>;
}

type DispatchedComponent<Stub, Props> = ComponentType<{ stub: Stub } & Props>;
type ErrorBoundaryComponent<Stub, Props> = ComponentType<{ stub: Stub; error: unknown } & Props>;

// registry.ts
interface DispatchRegistry {
  forEntity<Props>(key: DispatchKey<Props, "entity" | RefKind>):
    DispatchKeyRegistryEntry<EntityRefStub, Props> | undefined;
  forService<Props>(key: DispatchKey<Props, "service" | RefKind>):
    DispatchKeyRegistryEntry<ServiceRefStub, Props> | undefined;
}

function createDispatchRegistry(input: {
  entityEntries: ReadonlyArray<[DispatchKey<any, "entity" | RefKind>, DispatchKeyRegistryEntry<EntityRefStub, any>]>;
  serviceEntries: ReadonlyArray<[DispatchKey<any, "service" | RefKind>, DispatchKeyRegistryEntry<ServiceRefStub, any>]>;
}): DispatchRegistry;

// static_entity_registry.ts
// Fallback / ErrorBoundary are REQUIRED — Props is owned by the
// caller (i.e. whoever declared the dispatch key) and the helper
// has no way to construct a component of the right shape.
function staticEntityTypeRegistry<Props>(opts: {
  lookupMap: ReadonlyMap<string, DispatchedComponent<EntityRefStub, Props>>;  // id_string → component
  Fallback: DispatchedComponent<EntityRefStub, Props>;
  ErrorBoundary: ErrorBoundaryComponent<EntityRefStub, Props>;
}): DispatchKeyRegistryEntry<EntityRefStub, Props, StaticEntityDispatchState>;

// static_service_registry.ts  (analogous, simpler state)
function staticServiceTypeRegistry<Props>(opts: { ... });

// dispatch_provider.tsx
function DispatchProvider({ session, registry, children }: {
  session: WorkstationSession;
  registry: DispatchRegistry;
  children: ReactNode;
}): JSX.Element;

// render_dispatch.tsx
type RenderDispatchProps<K extends DispatchKey<any, any>> = {
  stub: K extends DispatchKey<any, "entity"> ? EntityRefStub
       : K extends DispatchKey<any, "service"> ? ServiceRefStub
       : EntityRefStub | ServiceRefStub;
  as: K;
} & ExtractProps<K>;

function RenderDispatch<K extends DispatchKey<any, any>>(props: RenderDispatchProps<K>): JSX.Element;

// hooks.ts
function useDispatchSharedAtom<T>(initialDefault: T | (() => T)): FxtrSessionAtom<T>;
function useDispatchProgressState<S>(): S;     // unchecked cast — typed shape
function useDispatchProgressMessage(): string; // generic loading text
```

## Internal shape

### Per-entry state in the dispatcher's `userState`

Keyed directly by `(DispatchKey.uuid, stub.shortId)` — no
in-memory index map. The session userState has shape:

```
{
  entries: {
    [keyUuid: string]: {
      [shortId: string]: { state: ..., shared: ... }
    }
  }
}
```

`state` is the state-machine sub-atom; `shared` is the
`useDispatchSharedAtom` sub-atom. Both are `FxtrSessionAtom<...>`
slices via nested `subAtom` calls.

Wire-stable identification by UUID matters for two reasons: (1)
on `attachSession` to a long-lived dispatcher session, persisted
entries can be re-bound to the registry's effect-fns without a
"who owns this entry" lookup; (2) bundler reloads or in-memory
state loss don't strand persisted entries with no way to address
them. The `(keyUuid, shortId)` pair fully identifies the entry's
location in `userState` and the in-memory caches reconstruct
deterministically from it.

The progress-message atom is *not* under `userState` — it's a
plain `atom("")` held in an in-memory entry-cache map keyed on
`(keyUuid, shortId)`. Progress messages are transient UI hints,
not persisted state; if the value is lost on reattach it's fine
(it'll be re-set as the state machine drives forward).

### Component-atom wrapping

For each entry, we hold:

- An inner `componentAtomInner = atom(Fallback)`.
- A wrapper `componentAtom` that:
  - Read: `get(componentAtomInner)`.
  - Write: if `newValue !== get(componentAtomInner)`, also write
    `null` to the shared sub-atom. Then write to inner.

The effect-fn receives `componentAtom`, so component writes
auto-reset shared via this wrapper. `<RenderDispatch>` subscribes
to a derived "output" atom that reads both `componentAtom` AND
the effect's mountable atom (so subscribing pulls the effect into
the mounted set and starts driving it).

### Static entity-dispatch state machine

`staticEntityTypeRegistry` uses a tagged union for its `S`:

```
type StaticEntityDispatchState =
  | { phase: "init" }
  | { phase: "loading-entity", load: OpResultStub<unknown> }
  | { phase: "loading-type-metadata", typeRef: EntityRefStub, meta: OpResultStub<EntityMetadata> }
  | { phase: "resolved", typeIdString: string }  // terminal
  | { phase: "errored", message: string }       // terminal
```

The effect-fn:

1. Reads `stateAtom`. If `init`, writes `{phase: "loading-entity",
   load: ctx.allocateLoad(stub)}` (function-form update) and
   sets `progressMessageAtom` to `"loading entity…"`.
2. If `loading-entity`, reads `load.result`. On `filled`, extracts
   `$fxtr.type` from the loaded value, writes `{phase:
   "loading-type-metadata", typeRef, meta:
   ctx.allocateGetEntityMetadata(typeRef)}` and sets the
   progress message to `"resolving renderer…"`. On `errored`,
   writes `errored`.
3. If `loading-type-metadata`, reads `meta.result`. On `filled`,
   reads `metadata.id_string`, writes `{phase: "resolved",
   typeIdString}` AND writes the component-atom to
   `lookupMap.get(typeIdString) ?? Fallback`. On `errored`, writes
   `errored`.
4. Terminal phases (`resolved` / `errored`): on every effect
   run, the machine re-checks the component-atom. If the
   current component-atom value doesn't match what the terminal
   state implies (`lookupMap.get(typeIdString) ?? Fallback` for
   `resolved`, `ErrorBoundary`-via-render-throw for `errored`),
   it writes the right value through. This handles the
   *reattach-to-existing-session* case: persisted `userState`
   already carries the terminal state, but the dispatcher's
   in-memory `componentAtom` is fresh and starts at `Fallback`.
   On the first effect run after reattach, the terminal-state
   check pulls everything back into sync.

The effect re-runs on any atom dependency change (the slot's
`result` atom, the state-atom itself). Side-effect ordering is
naturally serialized by jotai's batching.

The `errored` rendering path: terminal `errored` writes a
component to `componentAtom` that throws `state.message` during
render, which the per-key error boundary catches. Keeps the
errored path on the same "error boundary handles failures"
codepath as render errors in the resolved component.

The service variant is the same shape with one fewer hop.

## Chunk plan

### Chunk 1 — Package skeleton + core types (no runtime)

Lands `@fxtr-ui/react-dispatch` at
`projects/fxtr_workstation/frontend_lib/packages/fxtr-ui-react-dispatch/`
with:

- `package.json` — depends on `@fxtr-ui/core`,
  `@fxtr-ui/state-util`, `@fxtr-ui/react`,
  `@fxtr-ui/connector-workstation`, `jotai`, `jotai-effect`;
  peer-depends on `react`.
- `tsconfig.json` — same shape as the other React packages.
- `src/keys.ts` — `DispatchKey<Props, Allowed>` brand;
  `declareDispatchKey` / `declareEntityDispatchKey` /
  `declareServiceDispatchKey`; per-key auto-incrementing id.
- `src/registry_types.ts` — `DispatchKeyRegistryEntry`,
  `DispatchedComponent`, `ErrorBoundaryComponent` types.
- `src/registry.ts` — `DispatchRegistry` interface +
  `createDispatchRegistry` builder (in-memory two-bucket
  Symbol-keyed Map).
- `src/index.ts` — public re-exports.
- `frontend_lib/CODE_OVERVIEW.md` — add the new package's
  section.
- `tsconfig.json` — add the project reference.

No runtime behavior yet beyond pure data construction. Reviewable
in isolation as "the type-level surface."

**Update CODE_OVERVIEW.md** (frontend_lib + workstation root).

### Chunk 2 — Static registry helpers

- `src/static_entity_registry.ts` — `staticEntityTypeRegistry`
  effect-fn + state-machine state type. Wires
  `ctx.allocateLoad` + `ctx.allocateGetEntityMetadata` through
  the state-atom; threads progress messages through the
  `progressMessageAtom`. Implements the reattach terminal-state
  re-sync described in "Static entity-dispatch state machine"
  above.
- `src/static_service_registry.ts` — analogous, simpler
  (`ctx.allocateGetServiceMetadata` only).
- **No default Fallback / ErrorBoundary.** Each helper REQUIRES
  the caller to supply both. They're parameterized over `Props`
  and the helper has no way to know what shape the dispatch key
  wants. Whoever owns the dispatch key (a downstream package
  defining `CardKey` etc.) provides defaults if it wants
  consistent visuals across multiple dispatch entries for the
  same key.

Pure functions over the abstract types from Chunk 1; no React,
no provider. Reviewable as "given a session and an entry, the
machine runs correctly."

**Update CODE_OVERVIEW.md** (frontend_lib).

### Chunk 3 — Provider + RenderDispatch + hooks (the React layer)

- `src/dispatch_provider.tsx` — `<DispatchProvider>`. Holds the
  registry + dispatcher session in a React context. Manages the
  per-`(DispatchKey, shortId)` entry map (lazy creation,
  in-memory).
- `src/entry.ts` — internal `DispatchEntry` factory: allocates
  the per-entry session-sub-atoms (under
  `userState.entries[keyUuid][shortId].state` / `.shared`),
  constructs the component-atom + wrapper, allocates the
  in-memory `progressMessageAtom`, mounts the registry's
  effect-fn via `atomEffect` from `jotai-effect`, returns the
  output-atom bundle.
- `src/render_dispatch.tsx` — `<RenderDispatch>`. Looks up /
  creates the entry, wraps the resolved component in the
  registry-supplied error boundary, hides `SessionContext`
  inside.
- `src/hooks.ts` — `useDispatchSharedAtom` (mirrors
  `useSessionRootStateAtom` but reads the active entry's shared
  sub-atom from context); `useDispatchProgressState` (typed
  view of the state-atom, unchecked cast);
  `useDispatchProgressMessage` (the in-memory progress-message
  atom as a plain string).
- `src/error_boundary.tsx` — internal class-component wrapper
  that catches render errors and renders the registry-supplied
  `ErrorBoundary` component.

Documents the single-store assumption. No teardown logic.

**Update CODE_OVERVIEW.md** (frontend_lib).

### Chunk 4 — Demo

Fork the existing `fxtr-ui-demo` into a new
`@fxtr-ui/dispatch-demo` package (sibling under
`frontend_lib/packages/`). Share the Python workstation script
infrastructure but extend it (or write a new `dispatch_demo_workstation.py`)
to expose:

- Two entity types under distinct `$fxtr.type` IDs (e.g. `Note`
  and `Photo`), with distinguishing data.
- Two services with distinct `service_type_id`s (e.g. a generic
  `EchoService` and a separate `CounterService`).

On the TS side:

- Declare two dispatch keys: `CardKey` (renders entities or
  services as compact cards) and maybe `DetailKey` (full
  rendering).
- Build a `DispatchRegistry` registering custom components for
  each type-id-string, with `staticEntityTypeRegistry` +
  `staticServiceTypeRegistry` as the backbone.
- A `<DispatchPanel>` per session that shows a list of
  refs from `connector.listEntities()` / `listServices()` and
  renders each via `<RenderDispatch stub={ref} as={CardKey} />`.

Validates the full path end-to-end and serves as the manual
smoke test.

**Update CODE_OVERVIEW.md** (frontend_lib).

### Chunk 5 — Tests

After everything above is working, add:

- `tests/keys.test.ts` — `declareDispatchKey` branding, id
  uniqueness.
- `tests/registry.test.ts` — `createDispatchRegistry` route
  selection, instanceof routing, separate entity/service tables.
- `tests/static_entity_registry.test.ts` — drive the state
  machine against a mocked session, assert the right sequence
  of allocate calls + final component selection.
- `tests/static_service_registry.test.ts` — analogous.
- `tests/render_dispatch.test.tsx` — vitest + testing-library.
  Verifies: fallback renders initially, switches to resolved
  component when state-machine completes, error boundary
  catches render errors, `useDispatchSharedAtom` resets on
  component-atom swap, `useDispatchProgressState` reflects the
  state.
- `tests/integration.test.ts` — live workstation, end-to-end
  dispatch over real load+metadata.

Most edge-case coverage lives here. Skipped during chunks 1-4
unless a feature lands fully complete.

## Open questions / future work

- **Stuck-resolving on transitively-tracked type entities (live demo).**
  When the dispatch demo renders cards for every entity in
  `connector.listEntities()`, the d-tagged *type entity* refs
  (e.g. the FxtrBasicType for "Note") appear in the list
  because they're referenced from the Note instances'
  `$fxtr.type` fields. The static entity dispatcher tries to
  load them and never resolves — likely the workstation
  doesn't have the type entity's CBOR locally (it only
  tracked the reference) and the load hangs.

  Possible mitigations (not addressed in this stage):
  1. Have the demo filter `listEntities()` to only the
     externally-named entities (the four note + photo
     instances), so type entities don't show up.
  2. Add a load-timeout in `staticEntityTypeDispatch` so the
     state machine surfaces an `errored` instead of hanging
     indefinitely.
  3. Workstation-side: auto-pull referenced entity CBOR on
     copy, or surface "no source" loads as errors.

  The newly-added `Builtin` support handles `.`/`!`-tagged
  unloadable refs, but the stuck case here is d-tagged so
  doesn't qualify.



- **LRU / idle-eviction of dispatch entries.** Currently entries
  live until provider unmount. Acceptable for v1 with a small
  number of distinct refs; will want trimming as the surface
  grows.

- **Programmatic re-resolution.** No public way today to ask
  "throw away this entry and re-resolve from scratch" — useful
  when the user pushes a new registry or wants to invalidate
  cached lookups. Note: a dispatch helper *can* DIY this today
  by reading from a separate "re-resolve nonce" atom inside its
  effect and resetting `stateAtom` to its initial when the
  nonce changes. A first-class public surface for it can be
  added later if multiple helpers want the pattern.

- **Dynamic dispatch (stage 10).** A registry that loads
  components from the wire / compiles on demand rather than from
  a static map. The current shape (effect-fn over `stateAtom +
  componentAtom`) is general enough that this should just be a
  new `DispatchKeyRegistryEntry` shape, but we'll revisit when
  we get there.

- **Service-typed shared cache.** `useDispatchSharedAtom` returns
  a `FxtrSessionAtom<T>`. If `T` is typed `unknown`, the
  function-form ctx is happy. If a registry-entry author wants a
  *typed* shared atom shape (e.g. `Card.shared: {expanded:
  boolean}`), this is achievable via casting at the consumer
  boundary but unchecked. Mirror of the `useSessionRootStateAtom`
  caveat.

- **Single-store enforcement.** The plan documents the
  shared-store assumption but doesn't enforce it. A future
  helper could detect a store mismatch and throw a clear error
  rather than silently producing dead atoms.

- **Phase enrichment in `Fallback` props.** Currently the fallback
  reads progress state via `useDispatchProgressState`. An
  alternative would be to pass the state as a typed prop. The
  hook-based shape avoids re-renders on state-atom changes the
  fallback isn't subscribed to, but locks the type at the
  consumer site. Revisit if it gets awkward.

## Design notes to consult

- `design_notes/05_dispatch_and_inline_loading.md` — the
  `(refstub, ownStorage)` pattern, session slot dedup, dispatch
  contract.
- `design_notes/06_no_schema_for_now.md` — type ids and the
  no-schema runtime model.

## Implementation tasks

- [x] **Chunk 1** — Package skeleton + core types.
- [x] **Chunk 2** — Static registry helpers (entity + service).
- [x] **Chunk 3** — Provider + `RenderDispatch` + hooks.
- [x] **Chunk 4** — Dispatch demo (fork + Python extension).
- [x] **Chunk 5** — Tests.
- [ ] Final update of CODE_OVERVIEW.md (frontend_lib + workstation root).
