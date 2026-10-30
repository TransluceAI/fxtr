# Part 5: Job-status badge (cleanup)

A cleanup pass that gives the channel's **status** dimension (`S`) a real payload and
surfaces it in the frontend badge. Up to this point the migrated schedulers use
`S = None`; here at least one of them gets a typed status. Scoped small — it's a
demonstration of the status channel, not a core change.

**Note — `subscribe_state` plumbing already exists.** The frontend state-subscription
machinery (`useChannelStateSubscription` + the state fold + the live compact previews)
is built in **part 2**, where the preview keys need it. So part 5 is now just **(a)**
the backend status payload and **(b)** rendering it — the end-to-end `subscribe_state`
path is already exercised by the live previews before we get here.

**Chunking (tentative — revisit when we get here):** single chunk (one
`jj pre`/commit).

## Status payload

- Define a small status type (an `Entity`/dataclass that serializes to
  `FxtrChannelData`), e.g. `RunStatus{phase: Literal["queued","running","idle",
  "done","error"], detail: str | None}`. Pin it as the tag's `S` (so
  `SchedulerRunTag` / `StreamingRunTag` become `ChannelTag[RunStatus, …]`).
- The scheduler (or task, for cell channels) updates status via
  `emit(status=RunStatus("running", …))` / on `finish(final_status=RunStatus("done"))`.
  Since every full update also carries the current status (part 1), full subscribers
  see it too; the badge uses the cheap `subscribe_state` view.

## Frontend badge

- A status-badge component subscribes via the part-2 hook **with the
  `"subscribe_state"` method name** (the workstation/connector already forward the
  method arg — see part 1). It folds `ChannelState{status, stage}` (coalesce — latest
  wins) into a small badge (running spinner / idle / done / error), shown on the
  channel preview + panel header.
- Reuses the existing tag-type dispatch: a tag-specific status renderer can be
  registered (the `ChannelStatusPreview` key from the stage-13 followups) so each
  stream kind styles its own badge; the generic fallback shows phase text.

## Tests
- Python: `subscribe_state` delivers `ChannelState` snapshots that coalesce; status
  threads through `subscribe_full` updates; `peek_state` reflects the latest.
- vitest: the badge hook folds state events; coalesce drops stale; stage transitions
  (open→complete/abort) flip the badge.

## Implementation steps
- [x] `RunStatus` type in `simple_tasks` (a plain frozen dataclass, `phase:
      Literal["running","done","error"]` + `detail`; serializes inline as a dict — the
      dialect handles it, confirmed by test). `SchedulerRunTag` → `ChannelTag[RunStatus,
      …]`; the simple scheduler `start`s `running` and `finish`es `done` (with a detail).
      **Applied to the simple scheduler only** (the plan's "at least one"); streaming
      stays `S=None`. ✓
- [x] Frontend badge: `SchedulerRunCard` header shows a live `RunStatus` badge driven
      off part 2's `useChannelStateSubscription`. (No tag-specific `ChannelStatusPreview`
      renderer added — the generic state pill + this header badge suffice for the demo.) ✓
- [x] Tests: py — `subscribe_state` delivers the typed `RunStatus` (coalesce running →
      done; status threads onto `subscribe_full`; `peek_state` reflects latest). vitest —
      `latestChannelState` coalesce/transitions covered in part 2's `fold.test.ts`. ✓
- [x] Docs: `simple_workflow_guide.md` (subscribe_state carries `RunStatus`); the
      `RunStatus` contract is documented on `simple_tasks`. ✓

## Resolved
- **Status vocabulary** (was open): run channel uses `RunStatus{phase:
  running|done|error, detail}`. **Cell-channel statuses stay the task's choice** (the
  streaming tasks `start` their cell channels with `S=None` today; a task is free to use
  a typed status). Picked `RunStatus` as a plain dataclass (not an `Entity`) so it
  serializes inline as channel data.
