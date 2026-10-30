# Part 4: Migrate `streaming_scheduler` + `streaming_tasks` + task libs

The subchannel exercise: per-cell channels become **subchannels** of the run
channel, and the caller-passes-sink pattern becomes **caller-passes-writer**. This
is where the redo's tree model + the data-only-messages rule pay off — the old
`cells_scope` + sink-bridging + inline-storage-ref machinery is deleted.

Packages: `agent_workdir/packages/{streaming_tasks, streaming_scheduler,
streaming_task_lib, streaming_prompt_task_oci}` + `renderers/streaming_scheduler` +
the impl test. Carries forward the part4post1 output cache.

**Chunking (tentative — revisit when we get here):** two chunks (each a
`jj pre`/commit) — (a) contract + scheduler + task-lib backend + impl tests (the
subchannel / caller-passes-writer wiring, where sync is most likely), then (b)
frontend renderer + end-to-end.

## Contract changes (`streaming_tasks` + task libs)

- `StreamingRunTag` → `ChannelTag[None, FxtrServiceData, EntityRef[SimpleRunResult]]`;
  `TaskProgressTag` → `ChannelTag[None, FxtrServiceData, FxtrID]` (the cell channel's
  final value is the cell output id). `S=None` for both (part 5 may add status).
- **Task signature: receive a writer, not a sink.** `StreamingTaskProto.run(...)`
  takes `channel: ServiceRef[FxtrChannelWriter[_U, _U, _U, _U]]` instead of
  `progress: ServiceRef[FxtrChannelSink[...]]`. The task is the **owner** of its cell
  channel: it `start_channel_and_infer_types(channel, TaskProgressTag(...), None)`,
  `emit`s `partial`/`progress` messages, and `finish`es with the output id. (Tasks
  pick their own tag + types — the scheduler doesn't know them.)
- Run-channel message vocab: `cell_channel` now carries the subchannel **key** (a
  `str`), not a storage ref:
  `{"kind": "cell_channel", "input_name", "step_index", "key": <str>}`. `cell_result`
  (with `cached`, output id) unchanged. `run_inputs` carries step refs + names as in
  part4post1.

## `streaming_scheduler` (impl)

- Run channel: `create_root_channel` per run on the lifespan scope; scheduler owns +
  finishes it (like part 3).
- Per cell (non-cached): `key, cell_writer = await run_writer.spawn_subchannel(
  name=f"{input_name}:{step_name}")`; emit `{"kind":"cell_channel", …, "key": key}`
  on the run channel; pass `cell_writer` to the task's `run(...)` (a method arg — the
  writer ref bridges into the task call; allowed, it's an arg not a channel message).
  The **task** starts/emits/finishes the cell channel.
- After the task returns: read the cell output id from the task's return (or from the
  finished cell channel's final value), `await cell_writer.abort("scheduler backstop")`
  (idempotent — no-op if the task finished it), emit
  `{"kind":"cell_result", …, output, cached:false}` on the run channel.
- **Cache hit (part4post1):** no `spawn_subchannel`, no task call — just emit
  `{"kind":"cell_result", …, cached:true, output}`. Unchanged from part4post1 except
  it now lives in the new flow.
- **Deletes** `cells_scope` and the sink-bridging entirely. Cell channels are tree
  bookkeeping under the run channel; their data lives in per-node retention scopes
  managed by the tree. `cache_scope` (cached-output retention) stays.

## Frontend `renderers/streaming_scheduler`

- Run channel rendered as in part 3 (reader dispatch + batched fold). The grid card
  folds `run_inputs` + `cell_channel` + `cell_result`.
- **Subchannel drill-in by key:** on a `cell_channel` message, the card calls the
  part-2 `useSubchannelReader(runReaderStub, key)` to obtain the cell **reader** stub,
  then dispatches `ChannelContentCard` on the cell tag / reader (the cell reader's
  `get_tag` yields `TaskProgressTag`, landing on `TaskProgressCard`). Replaces the old
  inline-storage-stub nesting.
- `cell_result` with no preceding `cell_channel` (cache hit) renders the finished cell
  directly + a "cached" badge (part4post1 behavior preserved).

## Tests (`tests/test_streaming_scheduler_impl.py`)

1. Run subscriber sees `cell_channel` with a **key**; `open_subchannel_reader(key)` on
   the run reader yields a cell reader; subscribing to it replays + delivers the
   task's `partial`/`progress` + `complete` (output id).
2. Mid-run vs from-start fold equality on the run channel (batched).
3. Cache hit: `cell_result{cached:true}` with no `cell_channel`, no subchannel
   spawned; `clear_cache` re-misses.
4. Cell channel **cascade**: if the scheduler aborts the run before a cell finishes,
   the open cell subchannel aborts (parent cascade); a cell the task finished stays
   complete.
5. The direct caller-passes-writer path (test opens its own root channel, spawns a
   subchannel, hands the writer to a stand-in task that starts/emits/finishes) — the
   stage-13 `test_streaming_scheduler_impl` direct-channel test, retargeted.

End-to-end: rebuild + respawn + run; live cell streaming via subchannels, cached
badges, run completes.

## Implementation steps
- [x] `streaming_tasks`: tags → `ChannelTag[None, …]`; `StreamingTaskProto.run` takes a
      **writer**; `cell_channel` carries a key; docstrings. **`TaskProgressTag` dropped
      `input_name`/`step_index`** (the task owns the tag and only knows `task_label`; the
      grid position rides the scheduler's `cell_channel` message). **`run_inputs` dropped
      the step ServiceRefs** (data-only) — `step_names` + `inputs` only. ✓
- [x] `streaming_scheduler`: run channel via `create_root_channel`; per-cell
      `spawn_subchannel` + pass writer to task + emit key + idempotent backstop abort +
      `cell_result`; cache short-circuit; deleted `cells_scope`/sink-bridging. ✓
- [x] `streaming_task_lib` + `streaming_prompt_task_oci`: own the cell channel —
      `start_channel_and_infer_types` + `emit(events=[…])` + `finish`. ✓
- [x] `renderers/streaming_scheduler`: `SubchannelView` drill-in by key + cached cells +
      batched fold; step columns labelled from the tag. ✓
- [x] Tests reworked + passing (5 streaming; 14 demo total). End-to-end via the live
      workstation deferred to the batched review after parts 3–5. ✓
- [x] Docs: `simple_workflow_guide.md` streaming section (writer-owned cells, key-based
      drill-in). README's "streams" mentions are the verb (no API terms) — left as is. ✓

**Note (deviation from plan):** test 4 doesn't directly assert the failed cell
channel's own abort event (opening a cell mid-run is racy with run-finish, and
post-finish the run reclaims its cells). It asserts the consumer-facing evidence
(`cell_result{ok:false}` + `SimpleRunResult.failures`) and that opening the cell by key
post-finish raises (cells reachable only while the run is open). The cache test reads
`SimpleRunResult.cached` (post-finish-safe) rather than racing the run-channel log.
