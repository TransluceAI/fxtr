# Part 3 — Frontend UI redesign

A family of `fxtr-ui-*` TypeScript packages that supersede the
existing `@fxtr/react` substrate for the frontend, plus a rewrite
of `projects/fxtr_workstation/ts/packages/fxtr-workstation-core` on
top of them.

The new packages are designed to talk to either a static
HTTP-served fxtr namespace (the existing read-only path) or to the
workstation server's `/client` WebSocket endpoint as a reactive,
reducer-based client. Frontend code never imports `@fxtr/core` or
`@fxtr/react`; those remain for backend / background-job code that
needs full fxtr semantics.

## Reading order

- `design_notes/00_carve_up.md` — overview of the design space and
  pointers to all the per-topic notes.
- `design_notes/01_reactive_model.md` through `06_no_schema_for_now.md`
  — design sketches for the reactive model, connector abstraction,
  stub types, session state layout, dispatch, and schema decision.
- `design_notes/discussion_topics.md` — running tracker of
  resolved / open / deferred questions.

**The design notes are an initial sketch.** As we implement we
expect to revisit decisions. Use them as a proposed starting point,
not a binding spec; each stage file flags points likely to want
re-examination.

**Each part's work should be focused.** The design notes reference many
parts of the existing system, and plug into them in various ways. By
default, you should focus only on the parts relevant to what you are
directly doing. This means that you will likely want to read the
interfaces that are directly in scope for integrating with at the
current stage, but you do _not_ need to read all of the details of the
full existing workstation code, and you do _not_ need to worry about the design
of old frontend components that this design replaces. References to these
other systems are mostly here for context, and you should treat them as
all you need to know about them unless directed otherwise, or unless you
are strictly required to know more about the system in order to implement
the specific task at each phase.

## Package layout

For phase 0, packages live in a NEW location
`projects/fxtr_workstation/frontend_lib/packages/`. This will be a
self-contained typescript tree that co-locates the new code and
focuses on this new design. It can share its pnpm state with `v2/ts` in
the same way `projects/fxtr_workstation/ts/tsconfig.base.json` does.

Once the surface stabilizes we will eventually
(a) combine with the old `fxtr-workstation-core` in `projects/fxtr_workstation/ts/packages/`
(b) relocate the foundational packages (`fxtr-ui-core`, the connectors, react bindings) into `v2/ts/packages/` since they're reusable beyond the workstation.

Anticipated packages:

- `fxtr-ui-core` — abstract types, wire codec, stub classes,
  framework-agnostic session interface.
- `fxtr-ui-connector-workstation` — WebSocket connector against
  the workstation `/client` endpoint.
- `fxtr-ui-connector-http` — HTTP connector against the existing
  fxtr-data server.
- `fxtr-ui-react` — provider + hooks; path-aware atom projections.
- `fxtr-ui-react-dispatch` — type-driven entity / service
  dispatch (added in stage 7).

`@fxtr-workstation/core` and the example packages stay where they
are, but their dependencies shift over to the new ones.

## Stages

Stages roughly correspond to mergeable capability slices. Each
ends with something tested and usable on its own.

1. [Stage 1 — foundation: refs, mutations, no slots](./stage1_foundation.md)
2. [Stage 2 — entity loading + slot mechanics](./stage2_entity_loading.md)
3. [Stage 3 — service invokes](./stage3_invokes.md) (originally stage 5, pulled forward)
4. [Stage 4 — minimal React bindings](./stage4_react_bindings.md) (originally stage 3)
5. [Stage 5 (deferred) — HTTP connector](./stage5_http_connector.md) (originally stage 4)
6. [Stage 6 — callback inboxes](./stage6_callback_inboxes.md)
7. [Stage 7 — dispatch system](./stage7_dispatch.md)
8. [Stage 8 — `fxtr-workstation-core` rewrite (visual parity)](./stage8_workstation_core_rewrite.md)
9. [Stage 9 — workstation features in panels](./stage9_workstation_features.md)
10. [Stage 10a — dispatcher refactor](./stage10a_dispatch_refactor.md)
11. [Stage 10b — dynamic dispatch](./stage10b_dynamic_dispatch.md)

## Process

Working through stages is a pair-programming process, not a
top-down execution plan. For each stage we'll:

1. Re-read the relevant design notes and this stage's stub.
2. Discuss what's still open; expand the stub into a real plan
   (or amend the design notes) before any code lands.
3. Implement in chunks with breaks for review.
4. Update the stage file and design notes as we learn things.

Things to keep visible while we work:

- `discussion_topics.md` — open / deferred questions, kept current
  as we go.
- The stage files themselves — the questions sections expand as
  we hit decisions; resolved questions get crossed off with a
  pointer.

The end of each stage should leave: working code, tests proving
the capability, updated `CODE_OVERVIEW.md`, and a clean handoff
state for the next stage.

# Follow ups
- stage3 OQ-β call syntax svc.method("foo").call(arg1, arg2) and a similar load syntax for entities
- split up session.ts
