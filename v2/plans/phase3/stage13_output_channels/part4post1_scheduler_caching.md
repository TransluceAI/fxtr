# Part 4-post-1: Streaming scheduler output caching (minimal durable workflows)

## Context

The streaming scheduler (`examples/fxtr_workstation_demo/agent_workdir/
packages/streaming_scheduler/`) re-runs every cell on every
`run_batched`, even when an identical `(input, step)` was computed
moments earlier. This part adds a **minimal form of durable
workflows**: the scheduler keeps a cache from `(step input id, step
name)` to the entity that step produced, and on a cache hit it skips
the task entirely and serves the stored output, marked as cached.

This is deliberately the *simple* version — an in-memory cache on the
scheduler instance, cleared on respawn — laying groundwork for a more
sophisticated (disk-durable, content-addressed) implementation later.

## Decisions (settled with the requester)

1. **In-memory, scheduler-lifetime cache.** Lives on the mount
   lifespan state; survives across `run_batched` calls but not across
   respawn / server restart. (Disk durability is future work.)
2. **Key = `(input_id_string, step_name)`.** Task `ServiceRef`s carry
   no stable name (all `StreamingTask`s share one `service_type_id`;
   the proxy handle is per-respawn), so the caller supplies a stable
   per-step name. `run_batched`'s `steps` becomes a list of
   `(name, ref)` pairs; `name` doubles as the cache identity and the
   grid column label (the separate `labels` param is dropped on the
   streaming protocol). Reusing a name = "same transform, shared cache
   slot"; in a linear chain the inputs differ per position, so a
   repeated name across positions does not collide.
3. **A cache hit emits no channel** — just a `cell_result` with
   `cached: true` (and the output id). No `TaskProgressTag`, no
   `FxtrOutputChannel`, no task invocation. The grid renders the cell
   directly from that event.
4. **`SimpleRunResult` gains an additive optional `cached` map**
   (`dict[str, dict[str, bool]]`, default `{}`), so the *simple*
   scheduler (which shares the type) is unaffected.
5. **Caching defaults ON; toggled via the connect config.** The
   streaming scheduler's connect config becomes an optional dict with
   an optional `"cache"` bool (`None` / missing → on;
   `{"cache": false}` → off — never stores or serves).

Other settled points: only **successful** cells are cached (a failed
cell always re-runs); `clear_cache()` empties the whole cache and
returns the count cleared; the grid frontend visibly badges cached
cells.

## Type / protocol changes

`packages/streaming_tasks/` (marker — protocols + tags + the message
vocab docstring):

- `StreamingSchedulerProto.run_batched` signature →
  `run_batched(steps: list[tuple[str, ServiceRef[StreamingTaskProto]]],
  inputs: dict[str, FxtrID]) -> FxtrID`. The leading `str` is the
  step's cache name / column label.
- Add `StreamingSchedulerProto.clear_cache() -> int` (entries cleared).
- `cell_result` event gains `"cached": bool`. Document that a cached
  cell is announced by a `cell_result` with `cached: true` and **no
  preceding `cell_channel`** for that `(input_name, step_index)`.
- `run_inputs` keeps carrying the step refs; it additionally carries
  the step names (so a watcher/grid has them before the tag loads).

`packages/simple_tasks/` (marker):

- `SimpleRunResult` gains `cached: dict[str, dict[str, bool]] = {}`
  (input_name → str(step_index) → was-cached). Mirrors the shape of
  `cells`. Default empty keeps `simple_scheduler` untouched.

## Scheduler implementation (`packages/streaming_scheduler/`)

State (`_SchedulerState`, the mount lifespan object):

- `cache: dict[tuple[str, str], FxtrID]` — `(input_id_string,
  step_name) -> output id`.
- `cache_scope: FxtrScope` — a dedicated retention scope (sibling of
  the existing `cells_scope`, opened by a holder task on the lifespan
  task group) holding every cached output's entity closure so the
  stored ids stay loadable for the scheduler's life. `clear_cache`
  drops + reopens it.
- `cache_enabled: bool` — from the connect config.

`run()` entry point: parse `config` as an optional dict; set
`cache_enabled = config.get("cache", True)` when it's a dict, else
`True`. Pass into the impl → lifespan state.

`_run_cell(name, i, current)` gains a cache check at the top
(pseudocode):

```
key = (current.id_string(), step_names[i])
if cache_enabled and key in cache:
    out = cache[key]
    emit cell_result {input_name, step_index=i, ok:true,
                      output: out, error:null, cached:true}   # no channel
    return out
# ... existing path: open TaskProgressTag channel, announce cell_channel,
#     run task, finish/abort, emit cell_result {..., cached:false} ...
if cache_enabled:
    retain out's closure into cache_scope; cache[key] = out
```

`_chain` additionally records `cached[name][str(i)] = was_cached` so
the run result reflects per-cell cache status. The final
`SimpleRunResult(...)` passes the new `cached=` map.

`clear_cache` (new `@service_method`): clear the dict, tear down +
reopen `cache_scope` (releasing the retained closures), return the
count.

Notes:
- The cache key uses the *step's actual input* (`current`), so a fully
  re-run identical `(inputs, steps)` short-circuits the whole chain:
  step 0 hits → its output feeds step 1's key → step 1 hits, etc.
- Retain-into-`cache_scope`: reuse the closure-retention mechanism the
  cell channel already uses for its final value (see
  `fxtr2.common.streams._retain_into` / the cells-scope pattern); the
  exact call is settled during implementation.

## Frontend (`renderers/streaming_scheduler/index.tsx`)

- Read the new `step_names` from `run_inputs` (or keep using the tag's
  `step_labels`, which now hold the names) for column headers.
- Handle a `cell_result` that arrives for a cell with **no
  `cell_channel`**: render it as a finished cell directly (output
  reflink), rather than waiting for a channel/snapshot.
- Show a **"cached"** badge on any cell whose `cell_result.cached` is
  true (both the no-channel cached cells and, defensively, any marked
  cell). Keep the existing live-stream rendering for non-cached cells.

## Agent-facing usage change

`run_batched` callers pass named steps and (optionally) toggle the
cache at connect:

```python
# connect with caching on (default) — or {"cache": False} to disable
await schedule_invoke("scheduler", sched_wrapper, lambda s: s.connect({}))
# named steps; name is the cache identity + column label
await schedule_invoke("run", scheduler,
    lambda s: s.run_batched([("describe", spt)], inputs))
# later: clear it
await schedule_invoke("cleared", scheduler, lambda s: s.clear_cache())
```

## Implementation steps

- [x] `simple_tasks`: add `cached` field to `SimpleRunResult` (+ docstring).
- [x] `streaming_tasks`: change `run_batched` signature (named steps),
      add `clear_cache`, add `cached` to the `cell_result` vocab +
      `step_names` to `run_inputs`; update the module docstring.
- [x] `streaming_scheduler`: cache dict + `cache_scope` holder +
      `cache_enabled` from config; cache check / store in `_run_cell`;
      `cached` map through `_chain` into `SimpleRunResult`; `clear_cache`
      method; named-steps handling (derive `step_labels` from names).
- [x] Update `renderers/streaming_scheduler/index.tsx`: no-channel
      cached cells + cached badge + named columns.
- [x] Rebuild + re-spawn + re-run end to end (no `uv.lock` regen
      needed — deps unchanged). Verified: cache miss → hit (identical
      output ids, no task re-run), `clear_cache` (returned 2) → re-run
      misses, `{"cache": False}` never caches, and a mixed run shows
      cached badges next to a live-streaming fresh cell. Grid + durable
      `simple_run_result` renderers both badge cached cells.
- [x] Update docs: `agent_workdir/README.md`,
      `agent_workdir/simple_workflow_guide.md` (the streaming section).
      Also badged cached cells in `renderers/simple_run_result/` (the
      durable result view), not just the live grid.

## Future / out of scope

- Disk-durable cache (survive restart) via the persistence cap;
  content-addressed keys; cross-scheduler sharing.
- Cache invalidation finer than "clear all" (e.g. per step name).
- Whether the *simple* scheduler should gain the same caching (the
  shared `SimpleRunResult.cached` field already permits it).

## Followup: lower-level workstation bugs (fix separately)

Surfaced while building/debugging this part. Both are in the
workstation/spawner machinery, not the caching feature — track and fix
on their own.

- **(a) Opaque spawn/build errors.** ✅ Fixed in
  `projects/fxtr_workstation/plans/phase0/part6_adjustments/stage6_spawn_exception_fix.md`
  (spawner `except*` collapse + workstation invoke-error `repr` → full traceback).
  Spawning a git-ref service whose
  package has no committed `uv.lock` (the nix build fails at eval with
  `Path 'packages/<pkg>/uv.lock' does not exist in Git repository`)
  surfaces over the MCP only as `IRException(' (1 sub-exception)')`
  with an *empty* inner message — undebuggable from the agent side.
  The real error is only visible by building the flake attr directly
  (`nix build .#fxtr.services.<svc>.image`). The spawner/RPC layer
  should propagate the nix build's stderr (or at least the
  sub-exception's message) back through the invoke error instead of
  collapsing it to an empty exception group.

- **(b) Hang when batching invokes against a freshly-connected
  service.** The `simple_workflow_guide` gotcha ("don't batch invokes
  against a freshly-connected service in the same mutation — warm it
  with a trivial invoke first") is currently a *hard hang*, not a
  graceful error: batching `debug_state` + `run_batched` against a
  just-connected scheduler ref in one mutation wedged the run
  permanently (run never announced, `run_lock` held forever, slot
  never resolved, even `debug_state` then blocked). Reproduced by
  contrast — the identical scheduler code + task works when warmed in a
  separate mutation first. The connect/warm path should either be
  safe to batch against, or fail fast with a clear error, rather than
  deadlocking.
