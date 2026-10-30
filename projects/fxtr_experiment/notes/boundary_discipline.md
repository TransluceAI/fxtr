# Boundary discipline — design note

Every seam in this system is an fxtr2 service boundary: user ↔ user
capability, executor ↔ task fn, task ↔ run capability, run ↔ step, and
(optionally) RPC peers around any of them. The same few kinds of things
cross every one of these boundaries — entity ids, service refs, wire
values, errors — and each kind has rules that must hold at *every*
crossing. This note is the consolidated statement of those rules; each
points at the module docstring carrying its mechanics. Read it before
adding any wire method or mounting anything new.

(Background: `v2/notes/scope_discipline.md` for explicit-scope style and
`v2/notes/scope_service_lifetimes.md` for mounts, bridges, and the
keep-alive contract. This note is the workflow layer's application of
both.)

## 1. Entity ids: coverage is tracked-ness, not durability

A call or return carrying an `FxtrID` must *cover* it: the node has to be
tracked somewhere in the sending side's scope ancestry so the packet can
ship arena coverage. Two rules make this total:

- **The liveness invariant (producer side).** An id is recorded in the
  workflow state db only after its closure is pinned in the entity store
  — the combined session helpers (`write_cell`, `record_result`,
  `record_checkpoint`, `add_target`, ...) do persist-then-record in that
  order, so the state db never references anything the store doesn't
  hold. Writeup: `state_store.py`.
- **The re-attach rule (server side).** Being pinned is not being
  tracked: within the writing process, served ids are coverable only by
  the *residue* of the original persist, and after a session reopen that
  residue is gone. So **every wire method that returns ids re-attaches
  them first** — `WorkflowSession.ensure_loadable`, a lazy, idempotent,
  copy-free pin lookup into the entity scope (which every dispatch chain
  sits below). The liveness invariant is what makes the lookup total.
  This is deliberately a per-call-site rule, not an automatic layer: the
  namespace scope tracks only what was explicitly retrieved, and the
  maintenance stays at the point of use rather than half-hidden in a
  layer that might miss a case silently.

The failure signature when either rule is broken: ``Node ... was not
found in any parent scope!`` raised from the return-packet lift — often
only after a session reopen, because in-process residue masks it.

## 2. Persistence: pin from the scope that holds the closure

Values arrive at a service impl in the **dispatch call scope** (captured
at method entry — the documented service-method exception in scope
discipline) and results produced by a nested call live in that call's
**return arena**, which dies with its scope. Persisting must therefore
name its source scope explicitly, and happen *before* that scope closes:

- engine ops persist incoming values ``from_scope=`` the captured
  dispatch scope (`ops_engine.py`, `_values_scope`);
- a run's results persist from inside the run scope, a step's result from
  inside the per-step scope — each before its ``async with`` exits
  (`executor.py`, `ops_engine.run_step`; note ``record_checkpoint``
  exists precisely because a step's args and value pin from *different*
  scopes).

## 3. Service refs: never durable, lifetimes are explicit

- **Refs never enter durable state.** `ServiceRef`s are rejected in step
  args and results (`StepPayloadError`); handles cross as previously-read
  *keys* and are exchanged for live services only through `connect`,
  whose authority follows the generation's handle-read record
  (`runner_api.py`; the authority-and-trust section of
  `design_overview.md`).
- **Returned refs ride the dispatch scope.** The framework keeps a call's
  dispatch scope alive while the caller holds any ref the call returned —
  which makes the call scope the natural mount point for
  caller-interest-bounded services: mount there, and a
  ``_fxtr_mount_lifespan`` teardown is the drop-detection hook (the
  binding release capability is the worked example,
  `user_service._BindingReleaseImpl`). Granularity caveat: "the caller
  dropped the ref" means *the scope holding the caller's ref bridge
  drained* — in-process through `open_local` that is the runner scope;
  over RPC the client session scope. Design for "a stranded capability
  cannot wedge things", not for promptness within one shared scope.
- **One impl, many mounts** is sanctioned (per-mount state rides the
  mount lifespan value, never ``self``), which is what lets decorated
  task/step fns be long-lived singletons mounted per use
  (`view_runtime.StepFnAdapter`).
- **RPC peers drain concurrently.** Each side's connection drain waits
  for the peer to release its grant exports at the *peer's own* exit —
  so never gate one side's exit on the other side's *completed* exit
  (a cross-wait cycle that hangs). Signal peers before leaving your rpc
  scope; the keep-alive contract is
  `v2/notes/scope_service_lifetimes.md`.

## 4. Errors: transit is informational, typing is reconstructed

Uncaught exceptions cross boundaries as ``IRException`` with the original
class name as ``category``. Three rules keep that sound:

- **Nothing durable is classified from a transit form.** Poisoning
  persistence and the errored/interrupted split happen runner-side, from
  typed exceptions raised in-process (engine ops at their raise sites;
  the executor for the escape paths). A typed exception that *did not*
  cross a wire — e.g. the task-fn spawn's `CellFaultError` — may be
  classified, because in-process typing is trustworthy.
- **Consuming boundaries re-raise typed.** The view runtime's effectful
  ops and the `Workflow` wrapper route calls through
  `errors.typed_wire_call`, reconstructing the documented exception type
  chained from the transit form (unknown categories pass through). Pure
  ergonomics — callers catch `WriteConflictError`, not a category string
  — never an input to system state.
- **Signals are not poisonings.** An exception type whose contract says
  "constructed only for an already-recorded cause"
  (`StepInvocationFailedError`) is never recorded by op wrappers — one
  failure row per originating raise site, however many boundaries the
  news crosses.

## 5. The checklist for a new wire method

- Returns ids (bare, or inside dataclasses)? Re-attach each
  (`ensure_loadable`) before returning.
- Accepts values to store? Capture the dispatch scope at entry; persist
  ``from_scope=`` it, persist-then-record via the session helpers.
- Returns a ref? Decide its lifetime home: call scope (caller-interest
  bounded, mount-lifespan teardown for cleanup) vs a long-lived scope
  (explicit release only).
- Raises workflow errors? They're typed at the raise site; consuming
  boundaries reconstruct — don't classify anything durable from what
  crossed a wire.
- Testing it? An in-process pass proves little about coverage — add a
  session-reopen case (kills residue) and, for ref lifetimes, a
  drop-the-scope case.
