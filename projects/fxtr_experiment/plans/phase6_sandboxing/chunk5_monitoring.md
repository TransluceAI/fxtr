# Chunk 5 — monitoring + dashboard

*(Pre-scoping record: settled constraints + the questions to answer at
chunk start. Implementation steps get written when the chunk begins.)*

The bindings surface becomes fully observable: both classes in the
bindings watch family with launch state and repo state, the dashboard
bindings panel grows the affordances, and the demo harness exercises a
sandboxed binding end to end.

## Settled constraints (from `notes/sandboxed_bindings.md`)

- `list_bindings` / the bindings watch serve both classes. The class
  discriminator + launchability ride one **`BindingState` enum** on
  `BindingInfo` (introduced in chunk 2: `external` /
  `sandbox_launchable` / `sandbox_repo_missing` — the enum prefix is
  the class). **Chunk 5 expands the `sandbox_*` arm** with the process
  states (`sandbox_launching` / `sandbox_running` / `sandbox_faulted`)
  and possibly `sandbox_misconfigured` (bad defn / build failure, or an
  aggregate pin mismatch). Chunk 2 left the following for here (see the
  chunk-2 deviations):
    - **The global repo-configured state is NOT on `BindingInfo`** (it
      is one runner-wide fact, awkward per row). Surface it on a
      **workflow-level / full-state watch field** (the natural home for
      workflow-wide state), not per-binding. `sandbox_repo_missing`
      already communicates the *consequence* per sandbox row; this is
      the positive "a repo is configured" signal for the panel header.
    - Per-dep **pin status** (`attached` / `mismatched` / `missing`)
      already ships per dep in chunk 2's `SandboxBindingInfo`; decide
      whether an aggregate pin-mismatch also promotes the top-level
      state to `sandbox_misconfigured`.
- Dashboard bindings panel: class badge, launch state, the
  per-binding shutdown affordance (`shutdown_sandbox`), and
  pin-mismatch/repo faults surfaced legibly.
- `lint` stays out of binding health (target-rooted intent only);
  binding problems live in the panel + fault messages.
- **The TS wire mirror owes a widening, and the harness will not ask
  for it.** `frontend/src/wire/types.ts`'s `BindingInfo` is still
  `{key, cells}`; the wire has carried `state` since chunk 2 and
  `deps` / `config` / `sandbox` since chunk 3. Widen it with the panel,
  and extend the dialect fixture
  (`examples/fxtr_experiment_demo/dialect_fixture_backend.py`) with a
  **sandboxed-class** binding — populated `deps` / `config` /
  `sandbox`, a `sandbox_*` state — plus assertions on those fields in
  `frontend/tests/dialect.test.ts`.
  Do not expect the harness to catch the gap for you: it decodes
  structurally (`import type` is erased; `invoke<T>` does no runtime
  validation), so it detects TS declaring what Python does not send,
  but is **blind to Python sending what TS does not mirror** — which is
  this direction. That blindness is why the mirror silently fell two
  chunks behind; the fixture only ever went red because `state` landed
  as a required field with no default, which is luck, not detection.
  TODOs are parked at all three sites.

## Open questions to settle at chunk start

1. **Watch mechanics for launch state**: durable-record mutations flow
   through the session's change hints already (chunk 2), but launch
   state is runtime-only executor/serving state — it needs its own
   hint path into the watch engine (precedent: the bindings registry's
   change listener and the run-log stream). Decide the event shape
   (extend the existing bindings watch kind vs a new sub-family) and
   the transition granularity worth emitting.
2. **`BindingInfo` / wire-dialect ripple**: the frontend mirrors wire
   shapes by convention (`frontend/LOCAL_CODE_OVERVIEW.md`) — scope
   the mirror + round-trip-harness updates alongside the schema
   change, not after.
3. **Panel UX**: repo state lands in the panel header (the workflow-
   level watch field above, per chunk 2's decision — not per-binding
   rows); whether shutdown wants a confirm step; how a dangling pin
   (mismatch) is displayed before any run has faulted on it — chunk 2
   already computes per-dep pin status in `list_bindings`, so the panel
   can show it without a run; decide if a "re-check pins" affordance is
   worth it beyond the always-live watch recompute.
4. **The demo scenario**: which `examples/fxtr_experiment_demo`
   scenario gains a sandboxed binding, and where its sandbox repo
   comes from (a scaffolded workdir-style repo à la
   `fxtr_workstation_demo`, or a fixture repo checked into the demo).
   The runner's sandbox repo is an `open_local(..., sandbox_repo=...)`
   construction kwarg (chunk 2 — there is no `set_sandbox_repo` wire
   method), so the demo boot path passes it at open.
5. Whether phase-6 needs anything in the plate view (bindings are not
   grid-graph edges; presumably no — confirm and note).

Closing the phase: retire the phase-6 TODO in `CODE_OVERVIEW.md` and
sweep the TODO qualifiers left by earlier chunks.
