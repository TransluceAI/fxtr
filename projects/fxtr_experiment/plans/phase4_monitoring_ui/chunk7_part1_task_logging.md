# Chunk 7 part 1 — task-level logging

**Status: complete** (2026-07-14). Pulled forward from the chunk-7
task-channels sketch: the small, self-contained half — the
`TaskContext` logging surface + task-level `log()` plumbing — landed
now, with the richer output-channel system deferred to
[chunk7_part2_channels.md](chunk7_part2_channels.md) (needs more
scoping). Step-level `log()` and the run-log stream already exist
(chunk 6 / `notes/event_system.md`); this adds the outer (task) level.

## Scope

- **Task-side API** (`task_api.py`): `TaskContext` gains `log(message)`
  and `open_channel()`, mirroring `StepContext`. `open_channel` is a
  **stub** — it raises `NotImplementedError` locally (no wire method),
  exactly as `StepContext.open_channel` already does; the channel
  system is part 2.
- **Wire + engine**: `RunCapabilityProto` gains `log(message)` (the
  outer-level counterpart to `StepCapabilityProto.log`);
  `RunCapabilityImpl.log` forwards to a new `OpsEngine.log`, which is a
  run-log `log` entry with **no step identity** (`_note("log", …)`).
  Records nothing, not an effectful op → silent on the safety pass /
  record-hit replays (the frozen engine's discarded sink), like the
  step log.
- **Context wiring** (`task_service.py`): `_TaskContext` carries the
  run-capability proxy (the same one the views run against, loadable in
  the task fn's dispatch scope) and forwards `log` to it;
  `open_channel` raises.
- **Surfacing**: no new surface — task `log()` lines ride the existing
  cell-watch run-log stream (`step is None` → rendered untagged in the
  drawer's run-log section, distinct from the step-tagged notes).
- **Demo**: `SlowSteps` gets an outer-level `ctx.log(...)` so both a
  task-level line and its per-step lines stream into the drawer.

## Tests

- `tests/test_watches.py`: the checkpointed-task streaming case asserts
  a `log` entry with `step is None` (`"task starting"`) arrives on the
  cell watch alongside the step-tagged lifecycle notes. Full experiment
  suite (250) green; safety-pass silence is covered structurally (the
  frozen engine's `run_log is None`).

## Steps

- [x] `TaskContext.log` / `open_channel` (protocol) *(2026-07-14)*
- [x] `RunCapabilityProto.log` + `RunCapabilityImpl.log` +
      `OpsEngine.log` *(2026-07-14)*
- [x] `_TaskContext` proxy + `log`/`open_channel` *(2026-07-14)*
- [x] Demo `ctx.log` at the task level; test *(2026-07-14)*
