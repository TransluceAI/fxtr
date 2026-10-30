# Chunk 5 — watch subscriptions: backend + wire

**Status: complete** (2026-07-12; see the checked steps and the
notes/log). Consequence until chunk 6 lands: the dashboard panel calls
the deleted `subscribe`/`attach_path` and is broken against a current
backend (noted in `frontend/LOCAL_CODE_OVERVIEW.md`); the renderer
rewrite is the fix. Design: `notes/event_system.md` (the canonical
statement — this plan implements its backend half; the renderer half is
[chunk6_watch_frontend.md](chunk6_watch_frontend.md)). Scoped in the
2026-07-12 round (`notes/discussion_log.md`), superseding chunk 2's
wire-level hint stream: hints stay as the internal emission layer
(`change_hints.py` unchanged), and the wire grows path-addressed
**watch** subscriptions — per-query server-side mirrors delivering an
initial snapshot as the first diff and keyed patches thereafter.

## Scope

1. **Workstation `subscribe_to` args**
   (`projects/fxtr_workstation`): extend
   `WorkstationInboxImpl.subscribe_to(event_source, method)` with
   `args: Sequence = ()`, forwarded after the subscriber ref —
   `getattr(source_proxy, method)(subscriber_ref, *args)` in the
   parker's step 2. Wire args pass through the standard untyped→typed
   dispatch decode, so dataclass/scalar spec args just work.
   Backward-compatible (default empty). Workstation-side test +
   CODE_OVERVIEW touch. This is the only change outside
   `fxtr_experiment`.

2. **`watch_api.py`** — the event schema module (a leaf like
   `change_hints.py`): watch event unions per family as mashumaro
   dataclasses discriminated by `kind`, Google-style docs per variant.
   Shared vocabulary: patch batches (keyed `upsert`/`remove` items for
   summary rows and cells; `replace` for the scalar sub-topics),
   `recomputing`, `subject_gone`, and the cell watch's log events
   (`log_appended`, plus the new-run reset marker). Imported by
   `user_api.py` (method declarations) and the engine; mirrored TS-side
   in chunk 6. TODO comment on the union for the chunk-7 channel
   extension.

3. **The run-log buffer** (decisions settled 2026-07-12, second
   round): an **executor-owned** runtime-only registry of log entries
   keyed by cell — the executor owns the run lifecycle (`_run_once` is
   where clear-on-start goes), is keyed by the watch addressing
   already, and is reachable from the capability impl for the engine's
   snapshot + append-listener seam (the `set_event_listener` shape).
   The write path threads a per-run **sink through `StepHost`**: the
   live run gets the real sink; the safety pass's frozen engine gets a
   discarded one, so replay silence is structural. Entries are
   **structured**, not bare strings: timestamp, kind, an optional step
   identity (the step capability tags its own invocation — so it's
   visible which step is running), message. Two entry sources:
   - the step capability's `log()` (`ops_engine.py`) — its
     `append_event("step_log", ...)` state-db write moves here (the
     `events` table is an op-log for mutation/lifecycle provenance,
     not a message stream; step output in it was a miscommunication —
     the table and `append_event` stay, and the chunk adds a TODO
     comment on the schema noting mutation/lifecycle op-log entries as
     future work);
   - **engine lifecycle entries** emitted to the same sink by the ops
     engine's effectful ops — wrote cell, read cell, demanded a task
     output, step started / step record-hit — so a running cell's
     stream shows real progress with zero task-code cooperation.
     During a *resume*, hit-path entries show replay progress
     (desirable; runtime-only, so no replay-effect concern — the
     safety pass stays silent via the discarded sink).
   Buffers are cleared and restarted per run, retained after the run
   ends (an errored run's entries stay inspectable), dropped on runner
   restart. Note on the registry: a resume clears pre-crash entries
   and hits don't re-emit `log()` lines, so a resumed buffer holds
   post-frontier entries only.

4. **`watch_engine.py`** — the per-mount engine, wired by
   `UserCapabilityImpl._fxtr_mount_lifespan` (replacing
   `_EventBroadcaster`, which is deleted): the hint buffer fed by the
   session listener + binding-registry callback; the window worker
   (dirty-marking by hint intersection → recompute via the existing
   session query bodies → diff vs last-sent → pre-encode via the
   service dialect → publish); the watch table (per watch: spec,
   last-sent state, its own queue-mode `SubscriberHub`; reap on
   subscription death). Specifics:
   - snapshot = first diff from the empty mirror (register dirty, no
     seed path);
   - served ids re-attached (`ensure_loadable`) and published with
     `origin_scope=` the runner scope (membership rationale comments
     per `notes/boundary_discipline.md`);
   - `recomputing` emitted for the slow-flagged passes (summarize,
     lint), terminal batch (possibly empty) as the settle signal;
   - grant minting for watched grids (own grant + parent token in the
     `watch_cells`/`watch_cell` meta), through the same walk logic as
     attachment;
   - lint triggers on `intent` + `grids` + `cells` (findings move on
     materialization/deletion, not just intent edits).

5. **Capability surface** (`user_api.py`, `user_service.py`):
   `watch_workflow(subscriber)`, `watch_cells(path, subscriber)`,
   `watch_cell(path, coords, subscriber)` — each returning the
   standard `UnsubscribeHandle` ref; docstrings carry the delivery
   contract and the subscriber-side teardown contract (moved from
   `subscribe`'s Returns section). `Workflow` wrapper passthroughs.

6. **Deletions**: `UserCapabilityProto.subscribe` + its impl +
   wrapper passthrough (watches are the only subscription surface);
   `attach_path` (proto, impl, wrapper — the dashboard was its only
   consumer; `root`/`attach`/`attach_internals` stay).

## Tests

- Rewrite `tests/test_change_events.py` → the watch suite: snapshot as
  first diff; keyed upsert/remove diffs (and no event when a mutation
  leaves the queried state unchanged — the silence test, now stronger:
  diff suppression, not emission suppression); `subject_gone` +
  re-creation on the same live watch; `recomputing` + empty-batch
  settle; unsubscribe/handle-drop reap; log append + new-run reset +
  buffered entries in the first batch, step-identity tagging, engine
  lifecycle entries (incl. hit-path entries on resume; silence under
  the safety pass); the RPC smoke over a real wire (incl. the teardown
  contract).
- `tests/test_user_surface.py`: the `subscribe`/`attach_path` cases go;
  a watch smoke replaces the hint smoke.
- Workstation: `subscribe_to` args forwarding test.
- Everything else (introspection, currency, grounding, executor, …)
  passes unmodified — the session queries and hint emission are
  untouched.

## Steps

- [x] Workstation `subscribe_to` args (+ test + CODE_OVERVIEW touch)
      *(landed 2026-07-12 with an upstream fix it surfaced — below)*
- [x] `watch_api.py` event schemas *(2026-07-12; dialect round-trip
      smoke-checked incl. the `kind`-discriminated unions)*
- [x] Run-log buffer (executor-owned) + `log()` rerouting + engine
      lifecycle entries + the `events`-table TODO comment
      *(2026-07-12 — one deviation from the sketch: the sink rides an
      `OpsEngine.load(run_log=...)` param, NOT `StepHost` — the
      lifecycle notes come from reads/writes too, and `StepHost` is
      documented as step-miss authority only. Verified end to end via
      a scratch run: lifecycle entries + step-tagged `log()` + listener
      stream == buffer + safety-pass silence.)*
- [x] `watch_engine.py` + mount wiring (broadcaster deleted)
      *(2026-07-12 — one design simplification found during
      implementation: the engine recomputes through the capability's
      OWN wire methods (`@service_method` is a no-op at call time), so
      no query bodies were factored out — the engine is a pure consumer
      of the same surface any client drives, minting grants via the
      `attach_path` body retained as the private `_walk_path`.)*
- [x] Capability methods + wrapper passthroughs *(2026-07-12)*
- [x] Delete `subscribe` + `attach_path` *(2026-07-12; `change_hints`'s
      docstring re-pointed at the engine)*
- [x] Test suite rewrite per above *(2026-07-12 —
      `tests/test_watches.py`; the old attach_path pins moved into the
      user-surface internals test, now exercised THROUGH a live
      `watch_cells` meta; the RPC smoke's subscription leg is a typed
      workflow watch over the real wire)*
- [x] Update `CODE_OVERVIEW.md` (module list, user-surface paragraph)
      and trim the implemented parts from `notes/event_system.md`'s
      status TODO *(2026-07-12)*

## Follow-ups (noted at review, 2026-07-12; not scoped into the chunk)

- **Publish residency: replace the drain-race `RuntimeError` catch with
  an explicit contract.** Why the runner scope is involved at all:
  watch events carry served ids, which `ensure_loadable` makes
  loadable under the session's entity namespace *inside the runner
  scope*; `hub.publish(origin_scope=...)` captures a lease on that
  origin at publish time so the payload's entities stay loadable
  through (possibly later) delivery — and minting the lease requires
  the origin scope alive. Today's soundness is a *global lifetime
  argument* (base-first authorization + mount-teardown-follows-drain +
  the forceful subscription break), with a drain-window publish caught
  as `RuntimeError` and dropped — correct, but the catch is sloppy and
  the reasoning is non-local. Candidate by-construction designs:
  - *(a) engine-owned residency* — the fxtr2 channels precedent
    (`_ChannelNode.data_scope`): the engine copies each event's entity
    closures into isolated scopes it OWNS (no runner-scope parentage),
    publishes with its own scope as origin, and controls that scope's
    lifetime outright — no publish can ever race a drain, at the cost
    of per-event closure copies (channels pay this deliberately:
    nothing live flows through them).
  - *(b) owner-side shutdown contract* — whoever closes the runner
    scope must end the watch machinery FIRST (engine teardown as an
    explicit pre-drain step rather than post-drain mount-lifespan
    teardown), making "origin alive during every publish" a structural
    fact the owner opted into. Needs a lifespan variant or an explicit
    `close_watches()` handle on the capability owner's side.
  Revisit when next touching the engine, or immediately if the
  debug-level "publish dropped" log ever fires outside teardown.
- **A nominal queries interface.** The engine drives the capability's
  own introspection methods in-process (sound today; the
  dispatch-context-free discipline is stated in `watch_engine.py`'s
  docstring). Splitting that surface into a named interface (e.g.
  `WorkflowQueries`) consumed by both the wire dispatch and the engine
  would make the discipline structural instead of conventional and
  open extensibility (alternate query providers, per-watch query
  scoping). Mechanical whenever wanted; deliberately not done in this
  chunk.

Notes/log:

- **The engine must hold NO standing authorization lease on the runner
  scope** (found live: teardown deadlock). A retained
  `clone_authorization()` keeps the runner scope's graceful drain from
  completing, and the mount-lifespan teardown that would release it
  runs *post-drain* — exactly the documented lifetime consequence on
  `FxtrScope.clone_authorization`. Publishes instead rely on the
  runner scope's **base** authorization — scope-level state, valid
  from ANY task while the scope is open (the two-layer design in
  `entity_ref_scope`: `clone_authorization` resolves base-first; NOT
  context inheritance, which would be fragile). The invariant:
  publishes succeed exactly while the runner scope is
  open-and-not-draining; a drain-window publish fails authorization
  and is dropped, sound because the same drain's mount teardown then
  unconditionally force-breaks every subscription — a subscriber that
  missed a drain-window event always observes the break (`_publish`'s
  docstring carries the full argument).
- **Snapshot semantics for keyed collections**: a fresh watch on empty
  state delivers a first batch with no upserts — the batch itself is
  the snapshot marker; scalar sub-topics DO deliver their (possibly
  empty) current values, since diff-vs-`None`-sentinel fires on first
  compute. The renderer folds from empty either way.
- **A `watch_cells` on a checkpoint-grid path** hits the walk's
  checkpoint refusal inside recompute → logged, no delivery (the
  documented failure posture). The dashboard never subscribes to
  checkpoint grids (they don't attach by design), so this stays a
  non-surface; revisit only if a real client trips it.

- **The `subscribe_to` args stage surfaced a pre-existing fxtr2 typed-
  dispatch bug** (fixed in `v2/fxtr2/services/typed_service.py`, with a
  regression test in `test_typed_service.py`): a signature with a
  defaulted parameter FOLLOWED by a variadic — exactly
  `subscribe_to(source, method="subscribe", *args)` — broke on the
  impl-side rebind when the caller omitted both: the decoded wrapper
  carries the variadic's `()` default even when the wire omitted it,
  and `inspect.BoundArguments` can't emit a positional after the gap
  the stripped `method` leaves, so it spilled `args=()` as an illegal
  keyword argument. Fix: an empty variadic entry is call-equivalent to
  an absent one and is dropped alongside the `USE_REMOTE_DEFAULT`
  strip (a NON-empty variadic can only arrive with every earlier
  positional supplied, so no gap can precede it). Nothing hit this
  before because no wire protocol had a defaulted-then-variadic
  signature.
