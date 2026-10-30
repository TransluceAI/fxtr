# Part 4 (stretch): `streaming_scheduler` — nested channels

A second, parallel demo living next to the migrated
`simple_scheduler`: a scheduler whose **tasks each receive a channel
sink and emit their own progress**, so the run channel carries
*sub-channel announcements* and you can watch every cell's interior
progress live. This exercises the two sketch features part 3 doesn't:
the **caller-passes-sink pattern** and **channels nested inside
channel messages**, end to end through the frontend.

Depends on parts 1–3 landed. This part is intentionally sketchier —
expect design adjustments once parts 1–3 are real.

## New packages (workdir template)

### `packages/streaming_tasks/` — contract package (marker pkg, like `simple_tasks`)

```python
STREAMING_TASKS_NS = uuid.UUID("<uuidgen>")

@dataclasses.dataclass(frozen=True)
class TaskProgressTag(
    DataclassEntity,
    StreamTag[FxtrServiceData, FxtrID],   # V: progress dicts, R: the task's output id
    fxtr_type=define_type(STREAMING_TASKS_NS, "task_progress_tag"),
):
    task_label: str
    input_name: str
    step_index: int

@service_protocol
class StreamingTaskProto(Protocol):
    @service_method
    async def run(
        self, input: FxtrID, progress: ServiceRef[FxtrChannelSink[FxtrServiceData]]
    ) -> FxtrID: ...
```

- The task does **not** open its own channel — the scheduler opens
  the per-cell channel (it owns the tag + lifecycle) and passes the
  *sink* in. This is the sketch's "caller passes in a temporary
  FxtrChannelSink" pattern in its simplest form; the callee just
  emits progress dicts (e.g. `{"kind": "progress", "text": ...,
  "fraction": ...}` / `{"kind": "partial", "text": ...}`).
- Message vocabulary documented in the package docstring like
  `simple_tasks` does for its events.
- Also defines `StreamingRunTag` (analog of `SchedulerRunTag`:
  run_id, step_labels, input_names) and the run-channel message
  vocabulary:
  - `{"kind": "run_inputs", ...}` (as in part 3)
  - `{"kind": "cell_channel", "input_name": ..., "step_index": ...,
    "tag": <FxtrID of the cell's TaskProgressTag>,
    "channel": <ServiceRef to the cell's FxtrChannelStorage>}` —
    **the nested-channel message**. (Correction during
    implementation: the message carries the cell's *tag* ref too —
    the run-grid renderer embeds
    `<RenderDispatch stub={cellTag} as={ChannelContentCard}>`, and
    putting the tag in the message avoids a per-cell `status()`
    round-trip.)
  - `{"kind": "cell_result", "input_name": ..., "step_index": ...,
    "ok": bool, "output": FxtrID | None, "error": str | None}`
- `StreamingSchedulerProto`: same shape as the simple scheduler's
  proto (`run_batched(steps, inputs, labels) -> FxtrID`, `subscribe`)
  but `steps: Sequence[ServiceRef[StreamingTaskProto]]`.

### `packages/streaming_task_lib/` — a worked task (wasm, like `simple_task_lib`)

One deterministic streaming task so the demo doesn't need LLM access:
`word_shout` — loads the input Note, then word-by-word (with small
`anyio.sleep`s) emits `{"kind": "partial", "text": <uppercased so
far>}`, returns the stored uppercased Note's id. This gives the
frontend a token-stream-like feed (the sketch's LLM example shape)
without nondeterminism. Optionally a second task wired through the
haiku service later.

Wasm spawn env: copy `simple_task_lib`'s `sandbox_spawn/` shape.

### `packages/streaming_scheduler/` — the scheduler (host-importable + `sandbox_spawn/`, like `simple_scheduler`)

Structure mirrors the migrated `simple_scheduler`:

- Per `run_batched`: store `StreamingRunTag`, open the run channel,
  emit `run_inputs`.
- Per cell (input × step): store a `TaskProgressTag`, open the cell
  channel **on the run's working scope**, emit `cell_channel` (with
  `cell.storage`) into the run channel, then
  `await task.run(input, cell.sink)`; on return,
  `await cell.finish(output_id)` and emit `cell_result`; on exception,
  `await cell.abort(str(exc))` and emit a failed `cell_result`.
- Cell-channel mount scope — **corrected during implementation**:
  cell channels can NOT mount on the run channel's own scope. Part
  1's retention guard rejects emitting a `ServiceRef` whose handle
  rides the emitting channel's own scope token (it would deadlock
  the scope's graceful close), and `cell_channel` carries the cell
  storage into the run channel. Instead the scheduler's lifespan
  opens one dedicated **cells scope** (the `_retention_holder`
  pattern: isolated, `include=[GLOBAL_SCOPE]`, held by a task on the
  lifespan task group, released in teardown after the hub flush);
  all cell channels mount there. Cell channels still abort on
  scheduler shutdown via their own lifespans on that scope.
- Finish the run channel with the stored run-result entity ref
  (reuse `SimpleRunResult` if the shape fits, else a small
  `StreamingRunResult`).
- Scheduler-level hub: identical to part 3 (`run_channel`
  announcements + `scheduler_closing`).

## Frontend

1. **`ChannelContentCard` renderer for `StreamingRunTag`**
   (`renderers/streaming_scheduler/`): a grid like the simple
   scheduler's, but each *running* cell embeds the cell channel
   inline — `<RenderDispatch stub={cellTagStub} as={ChannelContentCard}
   channel={cellChannelStub} ...>` — recursive use of the same key.
   Completed cells collapse to the result reflink.
2. **`ChannelContentCard` renderer for `TaskProgressTag`**: the
   string-appending streaming view (the sketch's "chat message"
   override example) — renders the latest `partial` text, appending
   live; on complete, swaps to the output reflink.
3. Both registered in `renderers/streaming_scheduler/fxtr_renderer.nix`
   against `streamingRunTagType` / `taskProgressTagType` for
   `channelContentCard`; flake gains the two type entities.
4. No new dispatch keys, no `@fxtr-ui/*` changes — part 4 is purely a
   consumer of parts 1–2, which is itself a useful validation that
   the extension surface is right.

## Tests

`examples/fxtr_workstation_demo/tests/test_streaming_scheduler_impl.py`:

1. Task receives a working sink: `word_shout` run directly with a
   hand-opened channel; subscriber sees monotonically-growing
   partials, then the channel finishes with the output id.
2. Scheduler run: run-channel subscriber receives `cell_channel`
   messages; subscribing to a cell channel mid-task replays partials
   correctly (nested replay through a seeded ref — the key
   integration risk, also covered abstractly by part 1 test 7).
3. Failing task → cell channel aborted + `cell_result` records the
   error; run still finishes.
4. Late subscriber to a completed run channel: gets the complete
   snapshot; `cell_channel` refs inside replayed messages still
   resolve (retention of service refs in finished... note: messages
   are *dropped* at finish — so late subscribers see only the final
   value, and per-cell channels are reachable only while the run is
   open or via whatever the final result records. This is the
   designed behavior; the test pins it rather than fighting it.)

## Open design notes

- Whether `cell_result` is needed at all vs. deriving cell completion
  from the cell channel's own complete event — keeping it makes the
  run-grid renderer's fold independent of subscribing to every cell.
- Backpressure: queue-mode hub is unbounded; a chatty task ×
  many cells could pile up. Out of scope for the prototype, but
  `word_shout`'s sleep keeps the demo tame. Note it in the package
  docstring.
- If `run` taking a raw sink ref feels heavy for task authors,
  a later convenience could wrap sink refs in a small typed writer
  helper on the fxtr2 side; not needed for the prototype.

## Implementation steps

- [x] `packages/streaming_tasks/` (marker contract pkg: NS, tags,
      protos, message vocabulary; reuses `SimpleRunResult` for the
      run's final value)
- [x] `packages/streaming_task_lib/` (wasm pkg: `word_shout`)
- [x] `packages/streaming_scheduler/` (split shape + sandbox_spawn).
      Second correction found by test: the cell *sink* must be
      bridged from the cells scope into the `run_batched` call scope
      before the task invoke — invoke args can only carry service
      refs registered on the calling scope or an ancestor (the
      bridge dies with the dispatch).
- [x] `type_entities/pyproject.toml` + `tests/conftest.py` gain the
      new packages
- [x] `tests/test_streaming_scheduler_impl.py` + passing (4 tests:
      direct sink hand-off, mid-task replay composition +
      cell_result agreement, failing-task abort, final-value-only
      late subscriber)
- [x] `renderers/streaming_scheduler/` (thin scheduler panel — added
      beyond the plan for UX parity with the simple scheduler — plus
      the run-grid card with embedded cell cards and the
      `TaskProgressTag` streaming view) + `fxtr_renderer.nix`
- [x] `flake.nix`: two `mkWasmService` entries, THREE `mkType` type
      entities (the scheduler service type too, for the thin panel),
      slice import
- [x] Docs: README package/renderer listings + a guide section on
      the streaming variant (incl. the third subscribe level)
- [x] Update CODE_OVERVIEW.md — nothing to change (consumer-only of
      parts 1–2; demo docs above)
- [x] Headless e2e (2026-06-12, same drill as part 3): scaffold
      locks both new wasi envs; both services nix-build + spawn;
      live nested flow over the workstation wire (run channel
      `snapshot_open` replays `[run_inputs, cell_channel]`,
      subscribing to the announced cell channel mid-task replays the
      growing partials then streams live deltas, both channels
      `complete`); tag entities load *directly from the notification
      payloads* (preserved-events retention — no `status()` round
      trip needed agent-side); all three dispatcher routes resolve
      (`streaming_scheduler` / `streaming_run_tag` /
      `task_progress_tag`), compiling the new renderer module via
      the preset. Browser-visual check still pending, as for part 3.
      The close-session teardown crash reproduced (see
      followups.md).
