# Stage 10 aside 1 — Dispatch-key fallback refactor

A targeted refactor of `@fxtr-ui/react-dispatch` landed between
[stage 10b chunks 4 and 5](./stage10b_dynamic_dispatch.md). Moves the
per-`(DispatchKey, RefKind)` fallback components
(`LoadingFallback` / `NoMatchFallback` / `ErrorFallback` / `Builtin?`)
**from per-dispatcher registries onto the `DispatchKey` declaration
itself**, with optional per-dispatcher overrides.

This is independent of the per-stage chunk sequence — it cleans up the
abstraction the dynamic dispatcher inherited rather than adding
functionality.

## Motivation

After landing the `createDynamicDispatcher` skeleton, the consumer
still has to register every `DispatchKey` the dispatcher might handle
ahead of time, via a `DynamicDispatchRegistry` carrying per-key
fallback components. That partly defeats the point of *dynamic*
dispatch — the whole appeal of the dynamic path is that renderers
arrive from the wire at runtime, with no prior knowledge required.

The hard reason the fallbacks were local in the first place: the
dispatcher has to render *something* into the slot synchronously
(LoadingFallback) while it kicks off async work, and again on
failure (ErrorFallback) or terminal-no-match (NoMatchFallback), and
those need to be available before the wire round-trip resolves —
which might fail or never resolve at all.

The observation that unlocks the refactor: those fallback components
are *intrinsic to the dispatch key*, not to the resolution strategy.
The author of a `DispatchKey<Props>` is exactly the right person to
write fallback components matching the key's `Props` shape. The
dispatcher's job is to resolve a renderer; presenting "loading" /
"no-match" / "error" UI for a given slot is the key's job.

So: declare the fallbacks at the key, dispatchers read them from
there, optional per-key overrides on a dispatcher cover the
"override Loading to show dispatch-internal progress" case.

## Consequences

- **Dynamic dispatcher** loses its required `registry`. Just
  `{session, dispatchServiceRef, overrides?}`. It can handle any
  `DispatchKey` thrown at it.
- **Static dispatcher**: the static helpers (`staticEntityTypeDispatch`,
  `staticServiceTypeDispatch`) no longer take fallback options. They
  just supply the state machine + lookup map. The `DispatchRegistry`
  still maps `(key, ref-kind) → state-machine-recipe`.
- **Static dispatcher gracefully handles unknown keys**: instead of
  throwing `UnregisteredDispatchKeyError`, it renders the key's
  `NoMatchFallback`. Behaviourally identical to the dynamic
  dispatcher's "service returned no renderer" — the two dispatchers
  become interchangeable from the consumer's point of view, with the
  static one just being "every renderer is already in memory."
- **Cross-module dispatch keys**: a dynamically-loaded renderer
  module can declare its own `DispatchKey` (with its own fallbacks)
  and have nested `<RenderDispatch>` calls flow through the host's
  dynamic dispatcher with zero coordination. The shell doesn't need
  to know about the key in advance.

## Wire / public-API shape

### `DispatchKey` declaration

```ts
const NoteCardKey = declareEntityDispatchKey<NoteCardProps>({
  uuid: "...",
  description: "NoteCard",
  fallbacks: {
    Loading: NoteCardLoading,
    NoMatch: NoteCardNoMatch,
    Error: NoteCardError,
    Builtin: NoteCardBuiltin,  // optional, entity-only
  },
});

const CardKey = declareDispatchKey<CardProps>({
  uuid: "...",
  description: "Card",
  fallbacks: {
    Loading, NoMatch, Error,
    Builtin: ...,  // optional; only consulted for entity stubs
  },
});

const InvokeButtonKey = declareServiceDispatchKey<InvokeProps>({
  uuid: "...",
  description: "InvokeButton",
  fallbacks: {
    Loading, NoMatch, Error,
    // Builtin field is rejected at the type level for service-only keys
  },
});
```

Three matching fallback-type aliases:

```ts
type EntityKeyFallbacks<Props> = {
  Loading: DispatchedComponent<EntityRefStub, Props>;
  NoMatch: DispatchedComponent<EntityRefStub, Props>;
  Error: ErrorFallbackComponent<EntityRefStub, Props>;
  Builtin?: DispatchedComponent<EntityRefStub, Props>;
};
type ServiceKeyFallbacks<Props> = {
  Loading: DispatchedComponent<ServiceRefStub, Props>;
  NoMatch: DispatchedComponent<ServiceRefStub, Props>;
  Error: ErrorFallbackComponent<ServiceRefStub, Props>;
  // No Builtin
};
type AnyKeyFallbacks<Props> = {
  Loading: DispatchedComponent<EntityRefStub | ServiceRefStub, Props>;
  NoMatch: DispatchedComponent<EntityRefStub | ServiceRefStub, Props>;
  Error: ErrorFallbackComponent<EntityRefStub | ServiceRefStub, Props>;
  Builtin?: DispatchedComponent<EntityRefStub, Props>;  // narrowed
};
```

### `DispatchKey` field

```ts
interface DispatchKey<Props, Allowed> {
  readonly uuid: string;
  readonly description: string;
  readonly fallbacks: KeyFallbacksFor<Allowed, Props>;
  // existing phantom-brand still present for nominal identity
}
```

Where `KeyFallbacksFor<Allowed, Props>` picks the right alias above.

### Per-dispatcher overrides

```ts
createDynamicDispatcher({
  session,
  dispatchServiceRef,
  overrides?: ReadonlyArray<KeyFallbackOverride>,
});

createStaticDispatcher({
  session,
  registry,
  overrides?: ReadonlyArray<KeyFallbackOverride>,  // same shape
});
```

`KeyFallbackOverride` is built via a typed helper that narrows the
partial shape against the key's `Props`:

```ts
const overrides = [
  overrideKeyFallbacks(NoteCardKey, { Loading: CustomLoading }),
];
```

Internally the dispatcher holds an `idx: Map<string /* key.uuid */, Partial<...>>`
keyed by `key.uuid`; lookup at `getEntry` time. The `Map` key is the
`DispatchKey` object at the API level but the dispatcher just reads
`key.uuid` internally — same single source of identity as everywhere
else in the system.

### `DispatchKeyRegistryEntry` shape

Loses `Fallback` / `ErrorFallback`. Just the state-machine recipe:

```ts
interface DispatchKeyRegistryEntry<Stub, Props, S = unknown> {
  readonly effectFn: DispatchEffectFactory<Stub, Props, S>;
  readonly initialState: S;
  readonly initialProgressMessage?: string;
}
```

### Static dispatcher: unknown-key handling

Today: `getEntry(key, stub)` throws `UnregisteredDispatchKeyError`
when there's no registry entry for `(key, stub-kind)`.

After: `getEntry` returns an entry whose state machine is a no-op
that publishes `key.fallbacks.NoMatch` (or override) to
`componentAtom`. Same UX as a real no-match terminal in the static
state machines. `UnregisteredDispatchKeyError` is removed.

This makes the two dispatchers behaviourally symmetric: pass either
one a key it can't resolve, get `NoMatchFallback`.

### Static helpers: trimmed surface

```ts
// Before:
staticEntityTypeDispatch({lookupMap, LoadingFallback, NoMatchFallback, ErrorFallback, Builtin?})
// After:
staticEntityTypeDispatch({lookupMap})
```

The state machine inside `staticEntityTypeDispatch` no longer needs
`NoMatchFallback`/`Builtin`/`ErrorFallback` parameters — those come
off the key through the dispatcher's threading. `staticServiceTypeDispatch`
similarly drops its three fallback parameters.

## Backwards compatibility

**None.** `declareEntityDispatchKey(uuid, description)` becomes
`declareEntityDispatchKey({uuid, description, fallbacks})`; the
`staticEntityTypeDispatch` / `staticServiceTypeDispatch` opts shapes
narrow; `DynamicDispatchRegistry` and its builders go away (the
existing `createDynamicDispatchRegistry` / `dynamicEntityEntry` /
`dynamicServiceEntry` exports are removed).

Same precedent as stage 10a: workspace-private packages, hard-rename
all call sites.

## Implementation note: `AnyDispatchKey` and `Props`-contravariance

After the fallbacks moved onto the key, the natural
`K extends DispatchKey<object, RefKind>` upper bound stopped admitting
keys with non-trivial `Props` (e.g. `DispatchKey<FullPanelProps, RefKind>`).
The `fallbacks` field carries components contravariant in `Props`
(via `ComponentType`), so the narrower-Props key isn't a subtype of
the `object`-Props one.

Fix: an `AnyDispatchKey = DispatchKey<any, RefKind>` alias, used in:

- `Dispatcher.getEntry` (both concrete dispatcher signatures)
- `<RenderDispatch>`'s generic constraint
- The `StubFor<K>` helper

`any` here is intentional — it mirrors the existing
`AnyDispatchEntry = DispatchEntry<any, any>` alias and is the
established pattern for "erased over `Props` at this position." A
single `biome-ignore lint/suspicious/noExplicitAny` comment per
site, with a pointer back to the alias doc.

## Implementation steps

1. **`keys.ts`** — change `declare*` signatures to take
   `{uuid, description, fallbacks}` objects. Add the three
   `*KeyFallbacks<Props>` type aliases. Add `AnyDispatchKey`.
2. **`registry_types.ts`** — drop `Fallback` / `ErrorFallback` from
   `DispatchKeyRegistryEntry`.
3. **`static_entity_registry.ts` / `static_service_registry.ts`** —
   drop fallback options from the helper signatures and inner state
   machines; "no match" becomes a state-machine state whose terminal
   publishes whatever fallback the dispatcher hands it (via
   `componentAtom`).
4. **`static_dispatcher.ts`** — read fallbacks from key + overrides;
   render `NoMatchFallback` for unknown-key path; remove
   `UnregisteredDispatchKeyError`. Add `overrides?: ReadonlyArray<KeyFallbackOverride>`
   option.
5. **`dynamic_dispatcher.ts`** — drop `registry` from options; add
   `overrides?: ReadonlyArray<KeyFallbackOverride>`. Read fallbacks
   from key + overrides. Remove `DynamicDispatchKeyEntry`,
   `DynamicDispatchRegistry`, `createDynamicDispatchRegistry`,
   `dynamicEntityEntry`, `dynamicServiceEntry`,
   `UnregisteredDynamicDispatchKeyError`,
   `DuplicateDynamicDispatchKeyError`.
6. **`overrides.ts` (new)** — `overrideKeyFallbacks(key, partial)`
   typed helper, returns an opaque tagged structure. Internal
   lookup helper that merges base + override on demand.
7. **`index.ts`** — drop the removed exports, add the new ones.
8. **Test + demo migration** — every `declare*Key(uuid, desc)` call
   site grows a `fallbacks` block; every `staticEntityTypeDispatch`
   / `staticServiceTypeDispatch` call drops the four fallback args;
   every `createDynamicDispatchRegistry` consumer is rewritten to
   either drop the registry entirely or migrate to `overrides`.

## Why this is a side, not a chunk

This sits between the dynamic-dispatcher skeleton (chunk 4) and the
remote-service round trip (chunk 5) in the stage 10b plan, but
it's independent of the renderer-resolution work itself. Doing it
now means chunk 5 (and onward) inherits the simpler dispatcher
surface; doing it after would have us refactor everything we built
in chunks 5+6 too. Cheap to do, expensive to defer.

## Demos / tests touched

- `@fxtr-ui/dispatch-demo` (entity + service keys, registry, App)
- `@fxtr-ui/workstation-demo` (uses workstation dispatch registry)
- `@fxtr-ui/workstation` (shell) (declares dispatch keys for
  metadata / panel / block-link / inline / full)
- `frontend_lib/packages/fxtr-ui-react-dispatch/tests/` (all .ts
  state-machine tests — keys, static helpers, static dispatcher,
  registry, integration, dynamic dispatcher)
- `v2/ts/packages/fxtr-ui-react-dispatch-tests/tests/render_dispatch.test.tsx`
  (React render layer)

## What this does not change

- `<RenderDispatch>` API — `<RenderDispatch as={key} stub={...} {...props}/>`
  identical.
- `<DispatcherProvider>` API — identical.
- The `Dispatcher` interface (`status` / `session` / `getEntry` /
  `detach`) — identical.
- `createBaseEntry` — identical. The dispatcher passes in
  fallbacks (key or override-merged) as before, just sourced
  differently.
- The dispatch-key UUID system — identical.
- The session-state shape on the dispatcher's `userState` —
  identical (the per-entry `entries[keyUuid][shortId]` slot, the
  dynamic dispatcher's four cache fields, etc.).
