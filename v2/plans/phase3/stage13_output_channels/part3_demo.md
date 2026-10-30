# Part 3: Demo — migrate `simple_scheduler` to channels

In-place migration of the scheduler in
`examples/fxtr_workstation_demo` (no parallel copy). The scheduler's
**public API shape is preserved**: `run_batched` still blocks and
returns the result `FxtrID`, and the scheduler is still a normal
`EventSource` you subscribe to. What changes is the event
vocabulary: instead of fine-grained per-task deltas, the scheduler
publishes **per-run channel announcements**; the per-run deltas flow
through a `FxtrOutputChannel`.

## Backend: `simple_tasks` (the contract package)

`agent_workdir/packages/simple_tasks/src/simple_tasks/__init__.py`:

1. **New run-tag entity type:**

   ```python
   @dataclasses.dataclass(frozen=True)
   class SchedulerRunTag(
       DataclassEntity,
       StreamTag[FxtrServiceData, EntityRef[SimpleRunResult]],
       fxtr_type=system_entities.define_type(SIMPLE_TASKS_NS, "scheduler_run_tag"),
   ):
       run_id: str
       step_labels: tuple[str, ...]
       input_names: tuple[str, ...]
   ```

   - `V = FxtrServiceData`: messages keep the existing delta-event
     dict vocabulary (`task_started` / `task_result` / `task_failed`)
     — no new message dataclasses for the migration.
   - `R = EntityRef[SimpleRunResult]`: the final value is a ref to the
     durable result entity, which already has its own grid renderer
     (`simple_run_result`) — so the generic card's "result reflink"
     terminal state composes with existing rendering for free.
   - The data that used to ride in the `run_started` event
     (run_id, step_labels, input_names) moves into the tag. The
     `steps` ServiceRefs and `inputs` FxtrIDs do NOT go in the tag
     (tags are stored entities; live service refs can't be entity
     fields) — they're published as the first channel message instead
     (`{"kind": "run_inputs", "steps": [...], "inputs": {...}}`).

2. **Scheduler-level event contract** (docstring rewrite, lines
   36-73 today): events become
   - `{"kind": "run_channel", "run_id": ..., "channel": <ServiceRef to FxtrChannelStorage>}`
     — published when a run starts; also the seed for new subscribers
     while that run is current/most-recent.
   - `{"kind": "scheduler_closing"}` — unchanged.
   `run_completed` / `task_*` disappear from this level.

3. `SimpleTaskSequenceSchedulerProto` is otherwise unchanged
   (`run_batched`, `subscribe`).

## Backend: `simple_scheduler` (the impl package)

`agent_workdir/packages/simple_scheduler/src/simple_scheduler/__init__.py`:

1. **`_RunBookkeeping` shrinks drastically** — the fold-for-snapshot
   logic, `cells`, `completed`, `retention_scope`, and the
   release-event dance all move into `FxtrOutputChannel` (that's the
   point of the migration). What remains per run: `run_id`, the
   channel object, the encoded `run_channel` announcement for
   seeding.
2. **`run_batched`**:
   - store a `SchedulerRunTag`, then
     `channel = await FxtrOutputChannel.open(tag_ref, scope=<lifespan scope>)`
     (V/R inferred from the tag type — first real exercise of the
     inference path).
   - emit the `run_inputs` message, then per-cell
     `task_started` / `task_result` / `task_failed` messages via
     `channel.emit(...)` (local writer surface; replaces `_emit` +
     `hub.publish`).
   - publish `run_channel` (with `channel.storage`) to the
     scheduler's own hub, which stays exactly as it is today
     (`SubscriberHub`, queue mode) but now carries only run
     announcements.
   - on completion: store `SimpleRunResult`,
     `await channel.finish(result_ref)`, return the FxtrID as before.
   - on failure of the run as a whole: still store the result entity
     (it records per-cell failures today) and `finish` with it; let
     `abort` be the not-reached fallback (scope teardown).
3. **Mount scope / lifetime**: run channels mount on the scheduler's
   lifespan scope and stay mounted (complete channels are cheap —
   log dropped, only the final ref retained). The previous-run
   retention swap logic is deleted; the channel system owns
   retention now.
4. **`subscribe`** seeds with the current/most-recent run's
   `run_channel` event (or nothing pre-first-run), via the channel's
   announcement — the `seed_origin` is the lifespan scope holding the
   channel mounts.

## Frontend: renderers

1. ~~New thin slice `agent_workdir/renderers/channel/`~~ — **done in
   part 2 instead**: the existing `renderers/builtin/` slice
   registers `channelStorageRenderers` (from
   `@fxtr-ui/rd-types-channel`) as its `channel_storage` subtype,
   with the type entity from `lib.entities.channelStorage`
   (`nix/lib/entities.nix`, backed by the `nix/fxtr_lib_type_ids`
   stub's re-export). Nothing to do here.
2. **`renderers/simple_scheduler/index.tsx` becomes two renderers:**
   - The scheduler-service FullPanel goes *thin*: subscribe to the
     scheduler (existing inbox + `subscribe_to` pattern), keep the
     latest `run_channel` event, render
     `<RenderDispatch stub={channelStub} as={FullPanel} ...>` on the
     storage service stub (which routes through the channel slice's
     FullPanel → tag dispatch → custom card below). Show a "no runs
     yet" placeholder otherwise.
   - **The grid becomes a `ChannelContentCard` renderer for
     `SchedulerRunTag`** — this is the demo of the override story
     ("you can override this to show your stream content in some
     other way"). It uses `useChannelSubscription(channel, slot)`
     from part 2, folds `run_inputs` + `task_*` messages into the
     existing grid model (`deriveState` largely survives, minus the
     `run_state`-snapshot arm — the channel snapshot replays raw
     messages, so the fold is fold-from-start always), and on
     `complete` renders the result reflink row (the existing
     completed-row UI keyed off the final `EntityRef`).
   - Registered in `renderers/simple_scheduler/fxtr_renderer.nix` as
     a second subtype entry: `forType = schedulerRunTagType`,
     `forDispatchKeys = [channelContentCard]` (and optionally
     `channelStatusPreview` later). The slice's `externals` gains
     `"@fxtr-ui/rd-types-channel"` (the module now imports it).
   - Successive runs hand the thin panel *different* channel
     storage stubs; the carved `ownStorage` slot passed down must
     not leak the previous channel's persisted subscription/status
     slots into the new card — key the carved slot by the channel's
     shortId (reset-on-change), don't reuse one bare slot.

## Flake / preset wiring

`agent_workdir/flake.nix`:

1. **New type entity** (hand-pasted id string validated against
   Python, the established `mkType` pattern):
   `schedulerRunTagType` ← `"simple_tasks:SchedulerRunTag.fxtr_type"`.
   (`channelStorageType` is not needed — part 2 registered the
   storage renderers via the builtin slice's
   `lib.entities.channelStorage`.)
2. **New key UUID in `keyIds`**: `channelContentCard =
   "830e2987-08a7-410f-8c23-e01f05878c50"` (part 2's
   `ChannelContentCard`; add `channelStatusPreview =
   "beb2e1df-699a-4a90-b5fb-490382ba2628"` only when something
   registers for it).

(`frontend/src/client/main.tsx` externals: done in part 2.)

## Docs to update

- `packages/simple_tasks/` module docstring — the event-contract
  section (already in the backend item above).
- `agent_workdir/simple_workflow_guide.md` — the grid-renderer
  description, the lifecycle notes (snapshot-first / retention
  bullets describe the old per-run retention + `run_state` seeding),
  and the agent-side worked example (`"sched_events"` inbox): the
  scheduler now announces `run_channel`; watching task progress
  means a second `subscribe_to` on the announced channel storage
  ref.
- `agent_workdir/README.md` — the `renderers/simple_scheduler/`
  blurb (describes deriving the grid from a `run_state` snapshot).

## Tests

`examples/fxtr_workstation_demo/tests/test_simple_scheduler_impl.py`
reworked against the same harness:

1. Subscriber to the scheduler receives `run_channel` with a usable
   storage ref (resolve + `status()` returns the tag; loading the tag
   yields run_id/step_labels/input_names).
2. Subscribe to the run channel mid-run → snapshot replay + deltas
   fold to the same grid state as a from-start subscriber (the
   mid-run-compose test, retargeted).
3. Post-run subscribe to the channel → `snapshot_complete` carrying
   the `SimpleRunResult` ref; the ref loads.
4. Post-run subscribe to the *scheduler* → seeded `run_channel` for
   the most recent run, whose channel is complete.
5. Failing-cell run still finishes (result entity records the
   failure); `scheduler_closing` still delivered on teardown.

Manual end-to-end check (the "see how it works" part): scaffolded
workdir → `start_demo.sh` → agent spawns the scheduler + runs a batch
→ open the scheduler panel → live grid streams via the channel; open
the channel storage service directly from the sidebar → generic
FullPanel → custom grid card; after completion, re-open → renders
from the final snapshot only.

Note the workdir-template loop: changes here are to the **template**;
re-scaffold (`scaffold_workdir.py --force`) and re-`set_preset` to see
them in a live demo.

## Implementation steps

- [x] `simple_tasks`: `SchedulerRunTag` + event-contract docstring
      rewrite (scheduler level = `run_channel` / `scheduler_closing`;
      channel messages = `run_inputs` + `task_*`)
- [x] `simple_scheduler`: migrate to `FxtrOutputChannel` (slim
      `_RunBookkeeping`, channel per run on the mount scope,
      `run_channel` announcements, `finish(result_ref)`; added an
      explicit `channel.abort` on run-level exception rather than
      leaving the channel open until scheduler shutdown — the plan's
      scope-teardown fallback still covers cancellation)
- [x] `tests/test_simple_scheduler_impl.py` reworked + passing (8
      tests; announcement-bridging recording subscriber replaces the
      old snapshot-fold assertions; new coverage: typed
      `status()` → tag load, channels-stay-mounted across runs)
- [x] Frontend: `renderers/simple_scheduler/index.tsx` split (thin
      scheduler panel + `SchedulerRunTag` grid card), per-channel
      carved-slot keying (`channelPanels` record keyed by the storage
      stub's shortId; the grid card loads the tag entity via
      `makeLoadResultAtom` for run_id / step_labels / input_names)
- [x] `renderers/simple_scheduler/fxtr_renderer.nix`: run-tag subtype
      + `@fxtr-ui/rd-types-channel` external; `flake.nix`:
      `schedulerRunTagType`
      (`diew446am36h3re2eg6beyzvrjytthkgjna6k74w7b6z2n24lrifa`) +
      `keyIds.channelContentCard`
- [x] Docs: `simple_workflow_guide.md` (incl. the two-level agent
      subscribe example) + `agent_workdir/README.md` blurb
- [x] End-to-end check via scaffolded workdir (headless, 2026-06-12):
      booted the demo workstation against a scaffolded workdir and
      drove it over the agent MCP — wasm scheduler spawn (with the
      new fxtr2.common in its nix env), `run_channel` announcement
      with usable storage ref, `status()` → tag entity loads with
      run_id/step_labels/input_names, post-run `snapshot_complete`
      carrying the `SimpleRunResult` id, two-level agent subscribe
      (the guide's worked example), second run announces a second
      channel while the first stays mounted + answerable, and all
      three dispatcher routes resolve (scheduler type → FullPanel →
      `simple_scheduler`; channel-storage type → FullPanel →
      `channel_storage` in the builtin module; run-tag type →
      ChannelContentCard → `scheduler_run_tag`), which compiles both
      renderer modules via the preset. **Not** covered: actually
      *looking* at the grid in a browser — needs a human with the
      frontend up.
      Discovery along the way: `fxtr2.common` was missing from the
      `fxtr2-portable` force-include list (part 1 omission) — every
      wasm sandbox lacked it. Fixed in `v2/portable/pyproject.toml`,
      squashed into the 3.13.1 streams commit. Caveat: the
      workstation MCP plugin's *own* script sandbox caches its
      materialized fxtr2-portable per plugin process — a running
      plugin doesn't pick the fix up until restarted (this session
      worked around it by avoiding `simple_tasks` imports in driver
      scripts).
- [x] Update CODE_OVERVIEW.md — nothing to change: no frontend_lib
      surface changed in part 3; the demo-side docs are the READMEs
      + guide above
