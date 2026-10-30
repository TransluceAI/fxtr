# The event system — watch subscriptions

Standing design note for the workflow's **watch subscriptions**: the
mechanism by which UI (and any other observer) receives a live,
incrementally-updated mirror of workflow state, instead of polling or
refetching. This note owns the event *mechanics* — the subscription
surface, the event schemas, the delivery contract, and the server-side
engine; what the dashboard does with the mirrors is
`notes/monitoring_ui.md`. Terms used without definition (grids, cells,
currency, grounding, targets, sourcing, hints) are defined in
`view_spec.py`, `notes/execution_semantics.md`, `notes/grounding.md`,
and `change_hints.py`.

## The two layers

**Emission.** All mutations flow through `WorkflowSession`, and every
mutating body reports what it touched via the ambient hint collector
(`change_hints.py`): coarse, at-least-once, post-commit
`(kind, grid-path)` invalidation facts, with the stamp-clearing walks
reporting every grid whose derived verdicts they touched. Hints are the
system's internal change-detection substrate; they never cross the
wire. Generation-scoped op-record writes (observations, request edges,
checkpoints, grid assertions) additionally hint the **owning task
cell** (`state_store._note_generation_activity`, resolving the owner
via `exec_id`), so anything a running generation records dirties its
own cell watch — the drawer's generation report streams incrementally
as the run progresses rather than refreshing only at run end. This is a
pure watch signal, orthogonal to the grounding/currency marks the same
writes make.

**Watching.** A per-mount **watch engine** consumes the hints and
maintains, per active subscription, a server-side mirror of one query's
result: on each hint window it recomputes the queries whose subjects
intersect the hints, diffs the fresh result against the last-sent
state, and publishes only the keyed patch. A subscriber holds a fold of
these patches — an always-current mirror — and never refetches.

The consistency argument, in three claims:

1. *Every committed change is eventually reflected.* Hints are emitted
   post-commit and at-least-once; a hint marks every intersecting watch
   dirty; a dirty watch is recomputed against a session snapshot no
   older than the commit; diffing against last-sent turns "state
   changed" into "patch emitted".
2. *A subscriber's fold equals the server's mirror.* Delivery rides a
   `"queue"`-mode `SubscriberHub` — FIFO, lossless, and ordered within
   a live subscription; any delivery failure kills the whole
   subscription rather than skipping an event (the hub's crash
   backstop), so a live stream never has gaps. A broken subscription is
   observable (the workstation inbox's `connectionState`), and the
   client reconciles by resubscribing for a fresh mirror.
3. *Subscribe races are safe by construction.* A watch registers in the
   engine (dirty) before its first recompute; a commit landing at any
   point after registration re-marks it dirty; and because emission is
   diff-vs-last-sent — not event forwarding — over-triggering is
   harmless and re-ordering of *hints* carries no information. The
   initial snapshot is simply the first diff from the empty mirror:
   one delivery path, no snapshot/stream handoff to get wrong.

Recomputes happen outside the mutating transaction (each is one
ordinary session-snapshot query), so mirrors are eventually consistent
at hint-window granularity.

## The watch surface

Three watch families, all **path-addressed** (absolute key-path text +
flattened-coords text — the same root-frame vocabulary the presence
queries emit and the hints use), all presence-class (record nothing;
nothing downstream can depend on them), each a method on the one user
capability taking the `subscriber` ref as its sole positional arg with
any spec (`path` / `coords`) **keyword-only**, and returning the
standard `UnsubscribeHandle`. That shape is dictated by the workstation
idiom (client integration, below): `subscribe_to` forwards as
`method(subscriber, **spec)`, so the subscriber is the leading
positional and the spec rides as kwargs.

- **`watch_workflow(subscriber)`** — everything workflow-level and
  always-visible: the grid summary rows (keyed by path — the tree),
  lint, targets, sourcing, bindings. One subscription backs the
  dashboard's header + tree. Sub-topics are triggered independently
  engine-side (summary by `grids`/`cells` hints; lint by
  `intent`/`grids`/`cells` — lint findings move on materialization and
  deletion, not just on intent edits; targets/sourcing by `intent`;
  bindings by `bindings`), but ride one stream.
- **`watch_cells(path, subscriber)`** — one grid's detail: the grid's
  meta (config id, axes, grounding/sourcing, **the minted grants** —
  below) plus its cells keyed by coords text (the value-free
  `CellInfo` rows).
- **`watch_cell(path, coords, subscriber)`** — one cell's drawer
  bundle: the `CellReport` (content id with verdicts), and for task
  cells the generation report, the reverse observers, and the **live
  log stream** (below). One method for all cell kinds — payload
  variants are kind-discriminated, mirroring `inspect_cell` /
  `CellReport`, and the subscriber already knows the kind from the
  summary mirror.

Path addressing means a watch survives subject *identity* changes: a
deleted grid emits an explicit tombstone (`subject_gone`) rather than
going silent, and a grid re-created at the same path arrives on the
same live watch as a fresh snapshot. That is the desired dashboard
semantics ("watch this location"), and it is what makes the addressing
sound despite paths not being stable identities.

**Grants ride the payloads.** Mutations speak `ViewSel`, so the
`watch_cells` / `watch_cell` meta includes the grants a client needs to
build them: the watched grid's own grant plus its parent's token
(grid-level mutations address parent + key). The engine mints these
through the same walk logic as attachment (internals crossings
read-only-forced), so authority semantics are identical to `attach`.
Lifetimes align by construction: tokens are per-mount vocabulary, and a
mount death breaks every subscription, forcing a resubscribe that
delivers fresh tokens — nothing token-shaped needs healing logic or
durability.

Watches are the capability's only subscription surface; `root` /
`attach` / `attach_internals` are the programmatic attachment surface,
and the one-shot introspection queries (`summarize`, `list_cells`,
`inspect_cell`, the intent lists) remain for programmatic clients —
a watch mirror and the corresponding one-shot query serve the same
shapes from the same session query bodies.

## Event schemas

Every watch family declares a **typed event union** — mashumaro
dataclasses discriminated by a `kind` field, with Google-style docs per
variant — in a leaf schema module (`watch_api.py`) imported by the
capability protocol and the engine, and hand-mirrored in the renderer's
`wire/types.ts` (pinned by the dialect round-trip harness, like every
other wire shape). The engine pre-encodes events to wire form via the
service dialect (`Subscriber[FxtrServiceData]` delivery — the
sender-encodes contract on `fxtr2.services.subscription_types`).

Shared vocabulary across families:

- **Patch batches.** One recompute pass emits one batch event per
  family: keyed `upsert(key, value)` / `remove(key)` items for the
  keyed collections (summary rows by path; cells by coords text), and
  `replace(value)` for the scalar sub-topics (lint, the intent lists,
  a drawer's report — small enough that keying inside them buys
  nothing). An empty batch is legal and meaningful (the settle marker
  after `recomputing`).
- **`recomputing`** — emitted when a recompute pass flagged as
  potentially slow begins (summarize's cold force-evaluation walk;
  lint), so a UI can show a working indicator; the pass's terminal
  batch (possibly empty) is the settle signal. Quiet windows emit
  nothing.
- **`subject_gone`** — the watched grid/cell no longer exists
  (tombstone, not silence). The watch stays live; a re-creation at the
  same path resumes with a fresh snapshot batch.
- **Log events** (cell watch, task cells) — **event-native, not
  diffed**: `log_appended(entries)` deliveries published directly by
  the runner's log buffer, plus a reset marker when a new run clears
  the buffer. Entries are structured — timestamp, kind, an optional
  `StepRef` (the `(grid_path, coords)` step identity — the same
  vocabulary the checkpoint entries use, so a client keys log entries
  to checkpoint entries by tuple, no string parsing), message —
  covering task-authored `log()` calls and the engine's lifecycle
  notes. The lifecycle kinds include the **step lifecycle**
  (`step_started` / `step_hit` / `step_errored`): a running step is a
  `step_started` with no checkpoint entry yet, an errored step a
  `step_errored` (the one step outcome with no checkpoint row —
  completion is signaled by the checkpoint entry itself, so there is no
  `step_completed`). A client folds these together with the generation
  report's checkpoints to show running/errored/complete steps. The
  subscription's first batch carries the buffer's current contents, so
  a subscriber sees the entries that predate it.

## The engine

Per capability mount (the `_fxtr_mount_lifespan` seam): a hint buffer
fed by the session's post-commit listener and the binding registry's
change callback; a worker that, per coalescing window, intersects
buffered hints with the active watch set, recomputes dirty watches,
diffs, and publishes; and a watch table — per watch, its spec, its
last-sent state, and its own `SubscriberHub` (one hub per subscription:
the hub's machinery is per-subscription anyway, and its
unsubscribe-handle / handle-drop / crash-backstop teardown is exactly
the lifecycle wanted here). Watches whose subscription died are reaped
from the table.

Payloads carry served ids (config ids, value ids, record fingerprints),
so — per `notes/boundary_discipline.md` — the engine re-attaches them
(`WorkflowSession.ensure_loadable`) and publishes with
`origin_scope=` the runner scope, under whose namespace served ids
resolve; deliveries then carry their own residency via the hub's
origin-lease machinery.

**The run-log buffer** is the engine's one event-native source: an
executor-owned, runtime-only registry keyed by cell holding the
current generation's log entries — cleared and restarted by each new
run (`Executor._run_once` owns the lifecycle), retained after the run
ends (an errored run's entries are exactly what the drawer wants)
until the next run or runner restart, never persisted. Entries are
structured (timestamp, kind, optional step identity, message) and come
from two sources: the step capability's `log()` (tagged with the
invoking step's identity, so it's visible which step is running), and
**engine lifecycle entries** the ops engine emits from its effectful
ops — wrote cell, read cell, demanded a task output, step started /
step record-hit — so a running cell's stream shows progress with no
task-code cooperation. The write path is a per-run sink threaded
through `StepHost`: the live run gets the real sink, the safety pass's
frozen engine a discarded one, so safety-pass silence is structural
(and `log()` re-emission can't happen on hits anyway — hits never
invoke step fns). During a resume, hit-path lifecycle entries show
replay progress; a resumed buffer holds post-frontier entries only
(pre-crash lines were memory-only). Appends publish straight to the
cell's live watches; snapshots serve the buffer.

The state db's passive `events` table is unrelated to this stream: it
is an op-log for durable provenance (imports today;
TODO: mutation/lifecycle op-log entries are future work, noted on the
schema) and carries no task/step output.
TODO: chunk 7 (`plans/phase4_monitoring_ui/chunk7_task_channels.md`)
extends this seam to `TaskContext.log()` / `open_channel()` over fxtr2
output channels (`fxtr2.common.channels`) with the channel reader
rendered by `@fxtr-ui/rd-types-channel`; the buffer + the event-native
publish path here are deliberately the shape that work plugs into.

## Client integration (the workstation idiom)

A client holds one **inbox per watch** and subscribes with the
workstation's `subscribe_to(source, method, **kwargs)` idiom — the
inbox parker owns the subscription end-to-end, and *dropping the inbox
stub from session state is the unsubscribe*: the session reducer emits
`CloseCallbackOp` for unreferenced inbox slots, the parker unsubscribes
from the source, and the engine reaps the watch. A view's watch set is
therefore just the inbox stubs its state holds; navigation drops them
and the server cleans up. (`subscribe_to`'s `**kwargs` forward to the
source's subscription method after the subscriber ref —
`method(subscriber, **kwargs)` — so the watch spec's path/coords travel
as the watch methods' keyword-only args; keyword forwarding keeps the
call order-independent, unlike positional args whose order must line up
with the forwarded subscriber-first slot.)

Within a live subscription the client folds events into its mirror in
order; on `connectionState: "broken"` (or a fresh capability stub) it
drops the mirror and resubscribes with a fresh inbox. There is no
client-side invalidation planning, no refetch scheduling, no
token-heal on the read path — the fold and the resubscribe edge are
the whole protocol.
