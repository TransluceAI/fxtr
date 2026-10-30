# Simple workflow guide

How to run batched workflows with the `simple_scheduler` service and
watch them fill a live grid — and how to define a task of your own
from scratch. Generic mechanics (spawn-by-git-ref, renderer
authoring, preamble/package conventions) are covered in
[`README.md`](README.md); this guide assumes you've read its quick
start.

## The pieces

- **`SimpleTaskProto`** (`packages/simple_tasks/`) — one opaque
  `run(FxtrID) -> FxtrID` transform. Tasks compose without knowing
  each other's types.
- **`SimpleTaskSequenceSchedulerProto`** (impl in
  `packages/simple_scheduler/`) — `run_batched(steps, inputs,
  labels)` runs every named input through a sequential chain of
  tasks (rows fully concurrent, one run at a time), streams progress
  events to subscribers, and returns a durable `SimpleRunResult`
  entity holding every intermediate output + per-input failures.
- **Ready-made tasks**: `packages/simple_task_lib/` (a wasm spawn
  whose connect config — a string — picks `transcript_to_note`,
  `wrap_entity`, or `note_transform`; connect to the same spawn
  several times to pull out several tasks) and
  `packages/prompt_task/` (Note → Note through an LLM ask-service
  with a stored `PromptTemplate`).
- **Per-run output channels** — each `run_batched` creates a channel
  (`create_root_channel`, `fxtr2.common.channels`) tagged with a
  `SchedulerRunTag`; the scheduler owns the **writer** and emits the
  per-task progress through it. The scheduler's own event stream only
  *announces* each run's channel **reader**
  (`{"kind": "run_channel", "run_id", "reader": <ref>}`).
- **The grid panel** (`renderers/simple_scheduler/`) — subscribes to
  the scheduler, follows the latest announcement, and renders the
  run's channel as a live grid: rows = inputs, columns = steps,
  cells = waiting / running / output link / error. A panel opened
  mid-run replays the channel's message log first, so it always
  shows the full picture; once the run finishes the channel keeps
  only the durable result ref, so a panel opened later shows the
  `SimpleRunResult` link instead of the replay.

Event shapes are documented in `packages/simple_tasks/`'s module
docstring (announcements at the scheduler level; `run_inputs` +
`task_*` messages on each run's channel).

## Run the canned demo

### 0. Seed some inputs

Any entities work as inputs — the basic pipeline expects notes:

```python
# run_script
from simple_tasks import NoteWithLinks

inputs = {}
for i in range(10):
    note = NoteWithLinks(title=f"note {i}", body=f"the body of note {i}").store()
    inputs[f"note_{i}"] = note.entity_id
# stash for the next scripts
import json
print(json.dumps({k: str(v) for k, v in inputs.items()}))
```

(In practice keep the ids in a preamble helper or re-derive them —
script globals don't persist between `run_script` calls.)

### 1. Spawn the task lib + the scheduler (by committed git ref)

```python
# run_script (after `git add -A && git commit`)
from _fxtr_mcp_builtin import (
    NixWasmServiceDefn, current_commit, lookup_service_by_name, schedule_invoke,
)

spawner = await lookup_service_by_name("sandbox-spawner")
commit = await current_commit()
task_defn = NixWasmServiceDefn(
    git_commit=commit, flake_attr="fxtr.services.simple_task_lib"
).store()
sched_defn = NixWasmServiceDefn(
    git_commit=commit, flake_attr="fxtr.services.simple_scheduler"
).store()
await schedule_invoke("task_lib_running", spawner, lambda s: s.spawn(task_defn))
await schedule_invoke("sched_running", spawner, lambda s: s.spawn(sched_defn))
```

### 2. Connect (config picks the task; scheduler ignores its config)

```python
# run_script — wait for the spawn slots to land first
from _fxtr_mcp_builtin import retrieve_result, schedule_invoke

task_wrapper = await retrieve_result("task_lib_running")
sched_wrapper = await retrieve_result("sched_running")
await schedule_invoke("t_shout", task_wrapper, lambda s: s.connect("note_transform"))
await schedule_invoke("scheduler", sched_wrapper, lambda s: s.connect(None))
```

### 3. Hand the scheduler to the operator

```python
# run_script — after the connect slots land
from _fxtr_mcp_builtin import export_service, present_to_user, retrieve_result
from simple_tasks import SimpleTaskSequenceSchedulerProto

scheduler = (await retrieve_result("scheduler")).replace_service_type(
    SimpleTaskSequenceSchedulerProto
)
export_service("scheduler_panel", scheduler)
await present_to_user(scheduler)
```

The operator clicks the presented service link and opens the grid
panel. The panel subscribes itself (its callback inbox + a
`subscribe_to` invoke ride one mutation); before any run it shows
"no run yet".

### 4. Fire a run and watch the grid

```python
# run_script
from _fxtr_mcp_builtin import retrieve_result, schedule_invoke
from simple_tasks import SimpleTaskSequenceSchedulerProto, SimpleTaskProto

scheduler = (await retrieve_result("scheduler")).replace_service_type(
    SimpleTaskSequenceSchedulerProto
)
t_shout = (await retrieve_result("t_shout")).replace_service_type(SimpleTaskProto)
await schedule_invoke(
    "demo_run",
    scheduler,
    lambda s: s.run_batched([t_shout], inputs, ["shout"]),
)
```

Cells flip waiting → running → output links as the chains progress
(rows run concurrently). The same task ref may appear at several
positions in `steps`.

### 5. Load the durable result

```python
# run_script — after the demo_run slot lands
from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from _fxtr_mcp_builtin import present_to_user, retrieve_result
from simple_tasks import SimpleRunResult

result_id = await retrieve_result("demo_run")
result = await EntityRef(result_id, SimpleRunResult).load()
await present_to_user(result)   # renders via renderers/simple_run_result/
```

The `SimpleRunResult` strongly links every intermediate output, so
the run's data outlives the event stream (and the scheduler).

## Subscribing as the agent (optional)

The grid panel is just one subscriber — you can watch the same
streams yourself. It's a two-level subscription: the scheduler
announces each run's channel **reader**, and the reader carries the
per-task progress. The scheduler is an `EventSource` (`subscribe_to`
defaults to its `subscribe`); a channel reader exposes `subscribe_full`
(events) and `subscribe_state` (cheap lifecycle + the run's `RunStatus`:
`{"status": {"phase": "running"|"done"|"error", "detail"}, "kind"}`,
without the bulky event log) — you pick which by passing the **method
name** as `subscribe_to`'s second argument. The same `create_callback`
+ inbox-targeted-`subscribe_to` pattern works for each level.

Level 1 — run announcements:

```python
from _fxtr_mcp_builtin import create_callback, schedule_invoke

inbox = create_callback("sched_events")
await schedule_invoke(
    "sched_sub", inbox, lambda i: i.subscribe_to(scheduler)
)
```

Announcements arrive as callback notifications on the
`"sched_events"` slot (the first one is the seed: the
current/most-recent run's `run_channel`, if any).

Level 2 — once an announcement lands, subscribe to its channel:

```python
from _fxtr_mcp_builtin import (
    create_callback, retrieve_callback_notification, schedule_invoke,
)

ann = await retrieve_callback_notification(slot_key="sched_events", notif_index=0)
run_inbox = create_callback("run_events")
await schedule_invoke(
    "run_sub", run_inbox, lambda i: i.subscribe_to(ann["reader"], "subscribe_full")
)
```

The first notification on `"run_events"` is the channel snapshot:
`{"kind": "snapshot_open", "status", "values": [...]}` replaying the
messages so far for a live run (then `{"kind": "update", "events": [...],
"status", "replace_history"}` deltas, then `{"kind": "complete",
"result": <SimpleRunResult id>}`), or `{"kind": "snapshot_complete",
"result", "status"}` directly if the run already finished. (No event
carries the tag — fetch it once via the reader's `get_tag`.)
`forget(...)` closes an inbox, which unsubscribes that level
server-side.

## Lifecycle notes

- **`subscribe` returns promptly** (an `UnsubscribeHandle` ref); the
  workstation owns the panel-side subscription lifecycle. Awaiting a
  subscribe invoke is fine — there is no parked-dispatch trick
  anywhere anymore.
- **Seed-first**: a new scheduler subscription is seeded with the
  current/most-recent run's `run_channel` announcement (none if the
  scheduler never ran); a new *channel* subscription replays the
  message log as one snapshot, then streams deltas in order
  (replay-then-live — the channel system's contract).
- **Retention is per channel, until the run finishes**: outputs
  referenced by a run's messages live in that channel's retention
  scope; `finish` drops the log and its retention, keeping only the
  `SimpleRunResult` ref (which strongly links every intermediate).
  Completed channels stay mounted and cheap, so old runs' channels
  still answer with their final ref.
- **Subscriptions don't keep the scheduler alive.** Hold the
  scheduler ref to keep it; hold a subscription to keep receiving
  events; neither implies the other. When the last ref holder lets
  go (e.g. you `forget` the connect slot and the panel's export is
  withdrawn), the scheduler publishes a final
  `{"kind": "scheduler_closing"}` event to every subscriber, flushes,
  and unwinds — the panel shows a "scheduler shut down" badge.

## Use the LLM step (`prompt_task`)

`prompt_task` generalizes "run a prompt over each note". Its connect
config is a dict: an `ask(message) -> str` service ref (either haiku
service flavor — see the README's network section) plus the FxtrID
of a stored `PromptTemplate` (`str.format`-ed with `{title}` /
`{body}` of the input note). The reply's first line becomes the
output note's title.

```python
# run_script (haiku service spawned + connected as in the README;
# prompt_task spawned as fxtr.services.prompt_task)
from _fxtr_mcp_builtin import retrieve_result, schedule_invoke
from simple_tasks import PromptTemplate

haiku = await retrieve_result("haiku_svc")
tmpl = PromptTemplate(
    label="one-line summary",
    template="Summarize this note in ONE line. Reply with only that line.\n\n{title}\n{body}",
).store()
pt_wrapper = await retrieve_result("prompt_task_running")
await schedule_invoke(
    "t_summarize",
    pt_wrapper,
    lambda s: s.connect({"llm": haiku, "prompt_template": tmpl.entity_id}),
)
```

Then put `t_summarize` anywhere in a `run_batched` chain. Each
output carries `"input"` + `"prompt"` provenance links.

## Define a task of your own, from scratch

The full loop — new code, new entity type, new renderer — using the
established conventions:

1. **Write the task package.** Copy `packages/simple_task_lib/`'s
   shape (single-pyproject wasm spawn; see `packages/README.md`):
   a `run(config)` entry point that mounts a
   `TypedServiceImpl[SimpleTaskProto]` whose `run(input)` loads the
   input (`EntityRef(input, <YourInputType>).load()`), computes,
   stores its output entity, and returns `out.entity_id`. Declare
   `simple-tasks` (and whatever marker packages you need) as path
   deps; declare the `fxtr.service` entry point.
2. **Define output entity types** in a marker package
   (`simple_tasks/` itself or your own) — see the README's
   "Writing a custom visualizer" step 1. Strongly link provenance
   (`links={"input": input}`) so results are traceable.
3. **Add a renderer** for the output type (README steps 2–4):
   `renderers/<name>/index.tsx` + `fxtr_renderer.nix`, a `mkType`
   line + slice merge in `flake.nix`. Register at least the link
   keys plus `Figure` if you want the grid's output cells / reports
   to embed richly; `renderers/note_with_links/` is the closest
   crib.
4. **Wire the spawn** in `flake.nix`:
   `fxtr.services.<your_task> = lib.mkWasmService { src = ./packages/<your_task>; ... }`.
5. **Commit**, spawn at `current_commit()`, `connect(...)`, and pass
   the connected ref in `run_batched`'s `steps` — alongside the
   stock tasks if you like; chains mix tasks freely as long as each
   step's output type is loadable by the next step.

## Gotchas

- **Don't batch invokes against a freshly-connected service in the
  same mutation** — warm a new ref with a trivial invoke (e.g.
  `debug_state`) first. (Exported-service dispatches relay through
  the MCP chain; errors on that path surface as supervisor pauses.)
- `run_batched` serializes: a second call blocks until the first
  finishes (the panel swaps to the new run's channel on its
  `run_channel` announcement).
- `debug_state` on the scheduler returns
  `{live_subscriptions, has_run, run_in_flight, errors}` — handy to
  confirm the panel actually subscribed.

## The streaming variant (nested channels)

`packages/streaming_{tasks,task_lib,scheduler}/` are the same system
with one twist: each cell is a **subchannel** of the run channel,
**owned by the task**. The scheduler spawns the cell subchannel and
hands the task its un-started **writer** (caller-passes-writer); the
task starts it, emits progress, and finishes it. So the run channel
carries per-cell *subchannel* announcements (by key) and the grid
panel streams every cell's interior progress live (`word_shout`
uppercases a note word by word). Spawn
`fxtr.services.streaming_task_lib` (connect config `"word_shout"`)
and `fxtr.services.streaming_scheduler` (connect config `{}`).

Unlike the simple scheduler, the streaming scheduler takes **named
steps** and caches outputs: `run_batched(steps, inputs)` where
`steps` is `[(name, task_ref), …]`. Each `name` is the column label
*and* the step's **cache identity** — the scheduler stores each
cell's output under `(step input id, name)`, and on a later run a
matching cell is served straight from the cache **without running
the task** (marked `cached` in the grid + in
`SimpleRunResult.cached`). This is a minimal "durable workflow":
re-running an identical `(inputs, steps)` short-circuits the whole
chain. Caching is on by default — connect with `{"cache": False}`
to disable, or call `clear_cache()` to drop every entry (the cache
is in-memory and does not survive a respawn). Example:
`run_batched([("shout", shout_task)], inputs)`.

The two-level subscribe pattern above gains a third level if you
want to follow a cell: `cell_channel` messages on the run channel
carry each cell's subchannel **key**; call
`open_subchannel_reader(key)` on the run reader to get the cell
reader, then `subscribe_to(cell_reader, "subscribe_full")` it the
same way. (A *cached* cell emits no `cell_channel` — just a
`cell_result` with `cached: true`. Cells are reachable only while
the run is open.) Event shapes are on `packages/streaming_tasks/`'s
module docstring.

For a **real LLM** stream instead of the `word_shout` stand-in, use
`packages/streaming_prompt_task_oci/`: a `StreamingTaskProto` that
runs a `PromptTemplate` over each input note through Claude and emits
the reply (the `anthropic` SDK's native streaming) chunk by chunk
through its cell channel's writer — so the grid streams real tokens.
It's a
**container** spawn (`fxtr.services.streaming_prompt_task_oci`, via
`NixContainerServiceDefn`) so it can use the SDK directly; the
workstation must be launched with `--allow-llm-apis` (+
`ANTHROPIC_API_KEY`) so the `anthropic-authorizer` is mounted.
Connect config is `{"authorizer": <anthropic-authorizer ref>,
"prompt_template": <PromptTemplate FxtrID>}`; for a ready-made
template, `from streaming_prompt_task_oci import
VIVID_DESCRIPTION_PROMPT` and `.store()` it (the reply's first line
becomes the output note's title, the rest its body). Then drop the
connected ref into `streaming_scheduler`'s `run_batched` like any
other streaming task.

```python
# run_script (workstation launched with --allow-llm-apis)
from _fxtr_mcp_builtin import (
    NixContainerServiceDefn, current_commit, lookup_service_by_name,
    retrieve_result, schedule_invoke,
)
from streaming_prompt_task_oci import VIVID_DESCRIPTION_PROMPT

spawner = await lookup_service_by_name("sandbox-spawner")
defn = NixContainerServiceDefn(
    git_commit=await current_commit(),
    flake_attr="fxtr.services.streaming_prompt_task_oci",
).store()
await schedule_invoke("spt_running", spawner, lambda s: s.spawn(defn))
# wait for spt_running ...

authorizer = await lookup_service_by_name("anthropic-authorizer")
tmpl = VIVID_DESCRIPTION_PROMPT.store()
spt_wrapper = await retrieve_result("spt_running")
await schedule_invoke(
    "spt_task", spt_wrapper,
    lambda s: s.connect({"authorizer": authorizer, "prompt_template": tmpl.entity_id}),
)
# then use it as a named step: run_batched([("describe", spt_task)], inputs)
```
