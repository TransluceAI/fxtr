# 05 — Dispatch and inline loading

Supersedes an earlier `05_dispatch_and_entity_box.md` (the
`EntityBox` recursive-self-similar value type is **punted** as
premature — see "What we considered and dropped" below).

## The simple model

Components that render entity refs receive a pair:

```
(refstub: EntityRefStub<MyComponentEntity>, ownStorage: WritableAtom<MyComponentState>)
```

- `refstub` is the entity reference itself (untyped on wire,
  optionally given a type-level-only concrete type).
- `ownStorage` is a sliced/projected atom into session state where
  *this component* can store whatever it wants, including a load
  slot for `refstub`'s data.

If the component wants to render the loaded entity, it allocates a
load slot (`session.allocateLoad(refstub)`) and writes the slot
into `ownStorage` (probably under a conventional key like
`.loaded`). On re-render it reads the slot's `result` atom; once
that flips to `filled`, the loaded value is available.

For nested entity references inside the loaded value, the
component renders nested `<EntityLink>` (or analogue) components,
passing each a *further-sliced* sub-atom of its own storage. Each
nested component manages its own loading the same way.

This pushes the "where do I store the load slot" decision to the
component, and the component decides the *shape* of its own
storage. The entity reference itself does _not_ need to manage its
own state at the session level.

## Session-level slot dedup

The session deduplicates load allocations: if entity `abc1234` has
already been loaded and its slot is still alive (preserved server-
side, referenced from somewhere in state), a second call to
`session.allocateLoad(stub)` for the same short_id returns the
*same* `OpResultStub` (reusing the slot).

Concretely:

- Two unrelated components both want to render entity X. Each
  allocates a load, each writes the slot to their own storage
  location. Both writes turn into the *same* slot int on the wire,
  routed through the existing preservation.
- Only one `LoadEntityOp` is emitted (when the first storage
  location takes and saves a reference). When the second location adds its
  reference, the reducer sees the slot was already preserved —
  no new op.
- When all storage references drop, the load event is unpreserved and
  the dedup entry is removed. A future allocate for the same
  short_id gets a fresh slot (the data would need to be re-fetched
  if the user re-references the entity later).

This gives most of the benefit of an EntityBox shared cache (no
double-fetching, panels share loaded state) without inventing a
new value type and without any reserved state region.

### When dedup misses

If component A loads entity X, then completes and removes its
slot reference from state, the fill event gets unpreserved. If component
B later allocates a load for X, it gets a *fresh* slot (the
previous one is gone) and a fresh wire fetch.

A future extension could add local caching of loaded data, independent
of session lifecycle. Some helpers for this already exist on the workstation
server, but we can punt on this for v1. Note that even if we cache local
entity data, we still need to dispatch a remote event to make sure that the
server has loaded and prepared the entity data, since the server may lazily
determine where to fetch children from when it loads the parent.

## Dispatching on loaded values

Once a slot fills, the result is a structured value: arrays /
objects / primitives / stub instances at leaves. Dispatch:

1. The dispatch registry maps **entity type IDs** (opaque strings)
   to React components. Type IDs come from the loaded entity's
   `$fxtr.type` field (e.g. each entity carries a small type-id
   field, similar to fxtr's `system_entities.define_type`). This is
   a fully _runtime-level_ type descriminator, which may or may not
   correspond to a typescript type; if it does, it is the dispatch
   registry's job to associate the ID with a component with an
   appropriately narrowed type.
2. The renderer for a type receives the loaded value (and the
   `(refstub, ownStorage)` pair for the entity it renders, plus a
   way to slice further into `ownStorage` for nested children).
3. Renderers walk the loaded value and decide which nested
   entity refs / op-result slots / etc. to render inline.

Fallback: an unknown type falls back to a JsonTree-style view
that walks the loaded value and renders stubs as
`<EntityLink>` components.

## What we considered and dropped

**`EntityBox<T>` (the recursive-self-similar value type).** The
idea was that loading an entity returns a value where every nested
entity ref is itself a box that knows how to lazily load itself.
Dispatch would consume `Atom<EntityBox<T>>`.

Reasons we dropped it:

- Adds a new value type that has to be threaded through dispatch,
  schema, and rendering.
- Required a session-managed cache (extra reserved state region)
  to deduplicate loads. The simpler "session dedup + component-
  owned storage" pattern gives the same result without the new
  type.
- Premature for phase 0; can be layered later as a convenience
  wrapper over the simpler model if it proves valuable.

If we ever do bring it back, it would be a higher-level helper
over `(refstub, ownStorage)` rather than a wire / state concept.

## Open questions

### OQ-ii — Storage sub-slicing API

What does the API for "give me a sliced atom for my component's
storage" look like? Something like
`ownStorage.scope("notes-list").scope(`item-${id}`)`? Or
positional keys? Path-aware projections are coming anyway (jotai
splitAtom adaptation); this is a use case that needs to flow
through.

### OQ-iii — Dispatch component contract

A renderer signature roughly like:

```ts
function MyTypeRenderer(props: {
  refstub: EntityRefStub<MyType>,
  value: MyType,                        // the loaded value
  ownStorage: WritableAtom<MyState>,    // for nested loads
}) { ... }
```

Confirm shape. Renderers for "unknown" types still receive
`refstub` and `ownStorage`; `value` is generically
`UnknownValue` (the loaded but unfamiliar structure) but the
MyTypeRenderer can assert at the type level that values tagged with
a particular type ID have a particular shape.

### OQ-iv — How does the parent know what `MyState` shape to allocate?

For a typed renderer, `MyState` is the renderer's own concern.
But the *parent* component that mounts this renderer in its tree
has to allocate storage of the right shape for it. Two options:

- (a) Each renderer declares its initial state shape (so the
  parent can call `renderer.initialState()`).
- (b) Storage starts empty (`{}`) and renderers handle the
  "no state yet" case as part of their first render.

(b) is simpler and matches how React component state works. Lean
toward (b) unless renderers need more structured allocation.
