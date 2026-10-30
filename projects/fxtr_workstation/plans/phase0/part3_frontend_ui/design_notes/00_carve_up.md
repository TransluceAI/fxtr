# Carving up the frontend redesign

This is a landing page for the design notes. It enumerates the major
sub-designs we need to flesh out, the ordering dependencies between
them, and the key cross-cutting questions.

## Goals (re-stated)

1. Frontend code (panels, dispatch, react bindings) does **not** depend
   on `@fxtr/core` or `@fxtr/react`. Those stay for backend / batch /
   sandboxed-job code that needs full fxtr semantics.
2. A new family of TS packages, `fxtr-ui-*`, lets a frontend talk to:
   - a static HTTP fxtr-data server (current `projects/fxtr_workstation/ts`
     experience), and
   - a workstation server's `/client` websocket route (the new path).
3. The model is **reactive + reducer-based**: mutations are events on a
   queue, never synchronous. Components subscribe to state; state flows
   through context/props; effects schedule async work that lands as
   slot fills.
4. We keep the parts of the v2 TS port that aren't tied to fxtr
   semantics: type-driven dispatch, bicodec-style entity definitions
   (adapted to handle the workstation wire's list-escape JSON in
   addition to fxtr CBOR), and the panel/dispatch UI patterns from the
   existing `fxtr-workstation-core`.
5. `projects/fxtr_workstation/ts/packages/fxtr-workstation-core` is
   rewritten on top of the new `fxtr-ui-*` packages, with each panel
   owning its own session and dispatching dynamic type-driven content.

## Proposed package split

- **`fxtr-ui-core`** — types and abstract interfaces; framework-agnostic.
  - `EntityRefStub<T>`, `ServiceRefStub<T>`, `CallbackInboxStub<R>`,
    `OpResultStub<T>` interfaces.
  - The "model" of a connection: what state looks like, how mutations
    are dispatched, how slots are reified.
  - List-escape JSON codec (port of `wire_escape.py`) and a way to
    plug an entity type definition into both the workstation wire and
    fxtr CBOR.
- **`fxtr-ui-connector-http`** — read-only HTTP connector.
  - Implements `EntityRefStub<T>` (URL-backed) and `OpResultStub<T>`
    (a one-shot fetch). No sessions, services, or callbacks.
- **`fxtr-ui-connector-workstation`** — websocket connector to the
  workstation `/client` endpoint.
  - Owns the websocket; implements all four stub types in terms of
    workstation short-IDs and session slots.
  - Surfaces sessions, RPC connection lists, etc.
- **`fxtr-ui-react`** — idiomatic React bindings.
  - Hooks for binding component state to a connector-managed reactive
    store (likely jotai-backed).
  - Higher-level "schedule a load" / "schedule an invoke" effects.
- **`fxtr-ui-react-dispatch`** — type-driven component dispatch.
  - Adapted from `@fxtr/react-dispatch`; same key/registry shape, but
    keyed off the new stub types instead of `EntityRef`.
- (downstream) **`@fxtr-workstation/core`** — rewritten on top of the
  above. Still owns dockview shell, panels, breadcrumbs, sidebar.

## Status snapshot

See `discussion_topics.md` for the live tracker of open / resolved
questions across all sub-designs.

Current notes in this folder:

- `00_carve_up.md` — this overview.
- `01_reactive_model.md` — locked decisions A–E plus rollback /
  open OQ-6, OQ-7.
- `02_connector_abstraction.md` — uniform Session interface; HTTP as
  degenerate session.
- `03_stub_types.md` — the four stub types' identity, types, and
  wire forms.
- `04_session_state_layout.md` — concrete wire / atom layout,
  reducer responsibilities. Uses the preserve-events primitive
  instead of a stub_results state map.
- `05_dispatch_and_inline_loading.md` — `(refstub, ownStorage)`
  pattern for dispatch + session-level slot dedup. EntityBox
  punted.
- `06_no_schema_for_now.md` — phase 0 ships without a schema
  system; TS annotations + runtime stub `instanceof`.
- `discussion_topics.md` — running tracker.

## General topics to discuss

1. **The reactive state model**
   - What is the unit of state? Per-connector? Per-session?
     Per-component?
   - Slots: how represented in state, how filled, how dereferenced.
   - jotai vs. custom store; how to project / subscribe efficiently.
   - Is mutation always-async, or are some mutations local-only?
2. **Connector abstraction**
   - The shared surface vs. workstation-only extensions.
   - How does an HTTP connector model "no sessions"? Does it have a
     single virtual session, or a different shape entirely?
   - How session-aware ops (invoke, callback) are surfaced as
     unavailable when using HTTP.
3. **Stub type semantics**
   - Identity vs. live state: is `EntityRefStub<T>` just `(short_id,
     codec)`, or does it carry a slot/atom?
   - How does loading happen? Imperative `connector.load(stub)` →
     OpResult, or method on the stub itself?
   - How are types validated / coerced when crossing connector
     boundaries (an entity ref handed from the workstation back into
     a panel that expected a different type).
4. **Bicodec adaptation**
   - One bicodec that can decode either CBOR or list-escape JSON?
   - What's the canonical entity definition surface for the frontend?
   - How does this interact with the v2 bicodec (do we share code or
     fork?).
5. **Refcount + tracker mechanics**
   - Track-entity / track-service ops are session-scoped. When does
     the frontend issue them?
   - Implicit (refcounting based on what's referenced from state) vs.
     explicit (component declares pins).
   - How does this interact with React strict mode / unmount churn?
6. **Mutation API surface**
   - What does an event handler look like in idiomatic frontend code?
   - How do we batch a JSON patch + side-effect ops into a single
     mutation request?
   - Optimistic vs. pessimistic state updates.
7. **React bindings**
   - Provider shape. Suspense vs. result-object. Hooks vs. atom
     primitives.
   - How a panel wires up a session and its content.
8. **Dispatch system**
   - Adapted dispatch keys / registry on top of the new stub types.
   - How fallbacks / unknown types are handled (JsonTree analogue).
9. **`fxtr-workstation-core` rewrite**
   - Per-panel sessions, breadcrumbs, sidebar.
   - Migration path: do we delete + rewrite, or stage it?

## Cross-cutting tensions / decisions to revisit

- **Suspense vs. result objects**: existing fxtr-react uses Suspense.
  The user wants a more reactive feel where slots are explicit. These
  may not be incompatible, but it's a major shape choice (sub-design 1
  + 7).
- **Session boundary**: in workstation everything interesting flows
  through a session. Should `fxtr-ui-core` make sessions a first-class
  concept (and HTTP just have a degenerate "no session" mode)? Or
  should the HTTP and workstation connectors look quite different at
  the surface, with `fxtr-ui-core` only sharing the entity type / stub
  shape and leaving session ops to the workstation package alone?
- **Type erasure on the wire**: workstation wire drops EntityRef target
  types; types are reapplied client-side at use. We need a clean
  pattern for this — likely "you can supply a codec at use time, like
  `useEntity(stub, MyTypeDef)`".
- **Authoring an entity type once**: a user defining a `DemoNote`
  should not have to write parallel codecs for CBOR and list-escape
  JSON. The bicodec needs to handle both wire shapes from one schema.
