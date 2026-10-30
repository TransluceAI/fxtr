# Chunk 7 part 2 — task/step output channels (deferred, needs scoping)

**Status: not started; deferred — needs more scoping before
implementation.** Optional; may be dropped or moved to a later phase
without affecting chunks 1–6 or
[chunk7_part1_task_logging.md](chunk7_part1_task_logging.md). Design
sketch: the task-level-channels entry under "Deferred followups" in
`notes/monitoring_ui.md`. Part 1 already landed the string `log()`
surface (task + step) and the runtime-only run-log stream
(`notes/event_system.md`); this part is the rich extension over fxtr2
output channels (`fxtr2.common.channels`) — the `open_channel()`
methods on `TaskContext`/`StepContext` (today NotImplementedError
stubs) become real.

## Why it needs more scoping

The hard questions are about **integration and rendering**, not the
channel primitive itself. Settle these before implementing:

- **Reader plumbing.** How does the running cell's channel-reader ref
  reach the drawer — a presence-class query (`get_task_log_reader(sel)`)
  or an announcement riding the cell watch's event stream? The watch
  system delivers *served ids / diffed state*; a live channel reader is
  a service ref with its own subscription lifecycle, so it doesn't fold
  like the other sub-topics. Decide whether the cell watch carries a
  "has-live-channel + reader ref" marker (event-native, like the log
  buffer) that the client then `subscribe_to(reader, "subscribe_full")`s
  separately.
- **Task vs step subchannels.** A task run and its steps both want to
  emit; fxtr2 channels are a tree with subchannels. How do step
  channels nest under the owning task's channel, and how does the
  drawer render that tree — one merged channel view, or the
  step-checkpoints table gaining per-step channel readers? The current
  drawer folds a *flat* run-log; a channel tree is a different shape.
  This cross-task/step rendering is the main open design question.
- **Relationship to the run-log buffer.** Does the plain-line buffer
  (part 1) stay as the cheap path and channels layer on top, or do the
  `log()` lines route *into* the run's channel as leaf entries
  (superseding the buffer)? Settle whether channels replace or
  complement the buffer.
- **Lifecycle.** One live channel per (cell, generation) run, restarted
  by every run (retry/resume), never persisted; the reader is gone when
  the run ends. Replay must produce no channel effects (safety pass +
  record-hit replays write to a discarded sink — the same structural
  silence part 1 relies on).

## Sketch (superseded by the scoping above)

- **API**: `TaskContext.open_channel()` / `StepContext.open_channel()`
  return a live `FxtrChannelWriter` (the stubs today raise).
- **Runner**: the per-cell run-log registry generalizes to (or is
  superseded by) a live-channel registry; the reader ref is served per
  the reader-plumbing decision above.
- **UI**: the drawer renders the running cell's channel reader with the
  existing `@fxtr-ui/rd-types-channel` machinery
  (`subscribe_to(reader, "subscribe_full")`), per the task/step
  rendering decision above; gone when the run ends.

## Tests (once scoped)

- Emission during a live run; restarted across a retry; terminal
  behavior at run end.
- Safety pass and resume-replay produce no channel effects (no
  nondeterminism findings from channel use).
- Step channels nest under the owning task's channel (per the settled
  design).
- Renderer smoke via the demo's slow checkpointed scenario.
