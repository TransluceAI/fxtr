# Part 3: Migrate `simple_scheduler` + `simple_tasks`

In-place migration of the simple (non-streaming) demo scheduler to the new
reader/writer channel API. `simple_scheduler` has **no subchannels** — one root
channel per run, owned and finished by the scheduler. This part is the "does the
basic redo work end to end" exercise; part 4 adds subchannels.

Packages: `examples/fxtr_workstation_demo/agent_workdir/packages/{simple_tasks,
simple_scheduler}` + `renderers/simple_scheduler` + the impl test.

**Chunking (tentative — revisit when we get here):** two small chunks (each a
`jj pre`/commit) — (a) backend (`simple_tasks` + `simple_scheduler`) + impl tests,
then (b) frontend renderer + end-to-end.

## `simple_tasks` (contract)

- `SchedulerRunTag` changes base: `ChannelTag[None, FxtrServiceData,
  EntityRef[SimpleRunResult]]` — `S = None` (no status concept yet; part 5 may give
  it a real one), `V = FxtrServiceData` (the existing `run_inputs` / `task_*` delta
  dicts), `R = EntityRef[SimpleRunResult]`. Fields unchanged (run_id, step_labels,
  input_names).
- Scheduler-level event-contract docstring: the announcement now carries the run
  **reader** ref, not a storage ref:
  `{"kind": "run_channel", "run_id": ..., "reader": <ServiceRef[FxtrChannelReader]>}`
  (+ `scheduler_closing` unchanged). The channel-message vocab (`run_inputs`,
  `task_started`/`task_result`/`task_failed`) is unchanged.

## `simple_scheduler` (impl)

- Per run: store the `SchedulerRunTag`, then
  `writer, reader = await create_root_channel(tag_type=SchedulerRunTag, scope=<lifespan scope>)`
  and `await start_channel_and_infer_types(writer, tag_ref, initial_status=None)`
  (or `writer_proxy.start(tag_ref, None)`). The scheduler **owns** the writer.
- Resolve the writer proxy once; emit messages via `writer.emit(events=[msg])`
  (batched API — can also batch several per call). `run_inputs` first, then per-cell
  `task_*`.
- On completion: store `SimpleRunResult`, `await writer.finish(result_ref)`
  (`final_status` defaults). On run-level exception: `await writer.abort(reason)` (the
  scope-teardown / writer-release net is the fallback).
- Announce `{"kind": "run_channel", "run_id", "reader": reader_ref}` over the
  scheduler's own `SubscriberHub` (unchanged — it bridges the reader ref like it
  bridged the storage ref before). `subscribe` seeds with the current/most-recent
  run's announcement.
- `_RunBookkeeping` keeps only `run_id` + the channel `(writer, reader)` refs + the
  encoded announcement. Channels stay mounted on the lifespan scope across runs
  (finished channels are cheap — log dropped, final retained on the mount scope).

**Note:** the scheduler remains a plain `EventSource` announcing run readers — it is
*not* itself turned into a root channel whose runs are subchannels. (That's a
plausible future simplification, but out of scope; flagged in the overview's open
questions.)

## Frontend `renderers/simple_scheduler`

- Thin scheduler FullPanel: subscribe to the scheduler, keep the latest `run_channel`
  announcement, dispatch the channel FullPanel on the announced **reader** stub
  (routes through part 2's reader-type → tag dispatch). "No runs yet" placeholder
  otherwise.
- The grid stays a `ChannelContentCard` renderer for `SchedulerRunTag`: it uses the
  part-2 `useChannelSubscription` hook (now `subscribe_full`), folds
  `run_inputs` + `task_*` from the **batched** `update` events into the grid model,
  and renders the result reflink row on `complete`. The fold is fold-from-start (the
  snapshot replays raw messages). Per-channel carved-slot keying by reader shortId
  (as today, but keyed on the reader stub).

## Tests (`tests/test_simple_scheduler_impl.py`)

Reworked against the same harness:
1. Subscriber gets `run_channel` with a usable **reader** ref; resolve →
   `get_tag()` returns the tag (loads to run_id/labels/inputs); `peek_state()` →
   `stage="open"`.
2. Subscribe to the run channel (`subscribe_full`) mid-run → snapshot + batched
   deltas fold to the same grid state as from-start.
3. Post-run subscribe → `snapshot_complete` carrying the `SimpleRunResult` ref; it
   loads. Post-run `peek_state` → `stage="complete"`.
4. Post-run subscribe to the *scheduler* → seeded `run_channel` for the latest run.
5. Failing-cell run still finishes (result records the failure); `scheduler_closing`
   on teardown.

End-to-end (scaffolded workdir): scheduler spawn, run, panel renders the live grid
via the reader, post-completion renders from the final snapshot.

## Implementation steps
- [x] `simple_tasks`: `SchedulerRunTag` → `ChannelTag[None, …]`; announcement-contract
      docstring (reader ref). **`run_inputs` lost its `steps` ServiceRefs** (channel
      messages are data-only) — step labels come from the tag instead. ✓
- [x] `simple_scheduler`: `create_root_channel` per run, scheduler-owned
      `start`/`emit`/`finish`/`abort`, announce reader; slim `_RunBookkeeping`. The
      writer is bridged into the run dispatch scope so emits copy the freshly-produced
      output entities from there. ✓
- [x] `renderers/simple_scheduler`: reader-stub dispatch + batched-update fold +
      reader-shortId carved slots; step columns labelled from the tag (no per-step task
      reflink, since steps no longer ride the stream). ✓
- [x] Tests reworked + passing (9, incl. entities). End-to-end via the live workstation
      is deferred to the batched review after parts 3–5. ✓
- [x] Docs: `simple_workflow_guide.md` (reader-based two-level subscribe). README's
      channel mentions were the verb "streams" (no API terms) — left as is. ✓
