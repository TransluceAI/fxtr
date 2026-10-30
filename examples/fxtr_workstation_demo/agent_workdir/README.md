# Demo agent workdir

This is your working directory for the assembly-stage demo (the parent
directory's [`README.md`](../README.md) has the operator setup). It's a
**git repo** scaffolded out of the in-repo template, with a `flake.nix`
declaring the nix-backed affordances:

- **`fxtr.entities.*`** — DATA entities you pull into a script with
  `retrieve_nix_entity` (built on demand from the **live** workdir; no
  commit needed — tracked-file edits take effect immediately, new files
  just need `git add`).
- **`fxtr.services.*`** — sandboxed services you spawn **by committed git
  ref** (`NixWasmServiceDefn(current_commit(), …)`).
- **`fxtr.uiPresets.default`** — the renderer preset the UI
  dispatcher loads, also **by committed git ref** (`set_preset(...)`).

Two loops, by design: **DATA iterates uncommitted** (edit + `run_script`);
**CODE is pinned** (commit, then reference by `current_commit()`). The
host-built sandbox (`preamble.py` + `packages/`) is unchanged — the flake
governs only the nix entities/services/presets.

## What's here

- `.fxtr_workstation.toml.example` — copy to `.fxtr_workstation.toml`
  before connecting. The example already has the right URL +
  persistence-cap name for the demo backend.
- `flake.nix` — declares the sandboxed services (CODE, git-ref spawn):
  `fxtr.services.delayed_echo`, `fxtr.services.haiku_service_wasm`,
  `fxtr.services.simple_task_lib`, `fxtr.services.prompt_task`, and
  `fxtr.services.simple_scheduler` (wasm) and
  `fxtr.services.haiku_service_oci` (container);
  `fxtr.entities.demo_note` (DATA, built on demand by
  `retrieve_nix_entity`); and `fxtr.uiPresets.default` (the
  renderer preset, pushed by git ref). The preset is assembled from the
  per-renderer `fxtr_renderer.nix` slices + `lib.fxtr.entities.*` (the
  library type entities for the builtin renderer).
- `type_entities/` — a virtual uv aggregator (no code of its own) that
  makes the type packages importable so the flake can build each renderer
  type's `typeEntity` layer. You won't touch it unless you add a new
  type package.
- `preamble.py` — exec'd once before each `run_script` call.
  Empty for now; add shared helpers / imports as you go.
- `packages/README.md` — the three venv contexts a package here can
  be consumed in, and the packaging rules that fall out (marker-style
  no-deps packages, the `sandbox_spawn/` split, container deps).
  **Read it before adding dependencies to a package.**
- `packages/presentables/` — entity types you can construct and
  `present_to_user(...)`. `Note(title, body)` and
  `TodoList(title, items)`. Also exposes `DEMO_NOTE`, materialized as the
  DATA entity `fxtr.entities.demo_note`.
- `packages/delayed_echo/` — a sandboxed service (echoes its argument
  back after a delay). The flake builds it as
  `fxtr.services.delayed_echo`; you spawn it **by git reference** (see the
  spawn walkthrough below). Split packaging: the package root is
  **host-importable** (marker-style, no deps — so the aggregator venv can
  build its service type's typeEntity for the renderer), while
  `sandbox_spawn/pyproject.toml` is the wasm spawn-env declaration (the
  wasi-pinned deps + the `fxtr.service` entry point; it's what the flake's
  `mkWasmService` points at). Use this shape when a service's types need
  to be importable outside the sandbox; otherwise the single-pyproject
  shape is simpler.
- `packages/haiku_service_wasm/` — a sandboxed service that calls
  Claude Haiku (`ask(message) -> str`), built as
  `fxtr.services.haiku_service_wasm`. The **python-only sandbox**
  half of the network-access pair: connect it to the
  `anthropic-authorizer` and it talks to the Messages API with raw
  httpx through the host-side tunnel (no SDK — native deps don't fit
  the wasm lock). See "Talk to the network" below.
- `packages/haiku_service_oci/` — the same service as a **container**
  (`fxtr.services.haiku_service_oci`, spawned via
  `NixContainerServiceDefn`): real Linux CPython, so it depends on
  the actual `anthropic` SDK (native wheels and all) and uses it
  directly — network still flows through the same authorizer tunnel.
- `packages/simple_tasks/` + `packages/simple_task_lib/` +
  `packages/prompt_task/` + `packages/simple_scheduler/` — the
  **simple workflow system**: a batch scheduler service that runs
  named inputs through a sequential chain of `SimpleTaskProto` tasks
  and streams progress to a live grid panel. See
  [`simple_workflow_guide.md`](simple_workflow_guide.md) for the
  end-to-end walkthrough (spawn, run, watch the grid, write your own
  task). `simple_tasks/` is the marker package (protocols, pinned
  type ids, the `SimpleRunResult` / `NoteWithLinks` /
  `PromptTemplate` entities, and the event-shape docs);
  `simple_task_lib/` ships three basic wasm tasks picked by connect
  config; `prompt_task/` is a Note → Note LLM step (connects to a
  haiku service + a stored `PromptTemplate`); `simple_scheduler/` is
  the scheduler impl (split packaging like `delayed_echo/`).
- `packages/streaming_tasks/` + `packages/streaming_task_lib/` +
  `packages/streaming_scheduler/` — the **streaming workflow
  system** (nested output channels): same scheduler shape, but each
  task receives a channel *sink* and emits its own progress
  (`streaming_task_lib`'s `word_shout` streams the uppercased text
  word by word), so the run channel carries per-cell sub-channel
  announcements and the grid panel shows every cell's interior
  stream live. Spawn `fxtr.services.streaming_task_lib` (connect
  config `"word_shout"`) + `fxtr.services.streaming_scheduler`
  (connect config `{}`, or `{"cache": False}` to disable caching).
  Its `run_batched` takes **named steps** — `[(name, task_ref), …]`
  — where each `name` is the column label *and* the cache identity:
  the scheduler caches each cell's output under `(step input id,
  name)` and, on a later run, serves a matching cell from cache
  without re-running the task (a minimal "durable workflow" — cached
  cells are badged in the grid + flagged in `SimpleRunResult.cached`).
  `clear_cache()` empties it. Event shapes are on `streaming_tasks/`'s
  module docstring.
- `packages/streaming_prompt_task_oci/` — a `StreamingTaskProto`
  that streams a **real LLM** reply into the grid: it runs a
  `PromptTemplate` over each input note through Claude (the
  `anthropic` SDK's native streaming) and emits the reply token
  chunk by chunk through the per-cell sink. A **container** flavor
  (`fxtr.services.streaming_prompt_task_oci`, spawned via
  `NixContainerServiceDefn`) so it can use the SDK directly; network
  flows through the `anthropic-authorizer` tunnel. Connect config is
  `{"authorizer": <anthropic-authorizer ref>, "prompt_template":
  <PromptTemplate FxtrID>}`; import the ready-made
  `VIVID_DESCRIPTION_PROMPT` from the package to skip authoring a
  template. Use it as a step in `streaming_scheduler`'s
  `run_batched`.
- `packages/report_builder/` — agent-side handle for the
  report-builder service. Exposes
  `schedule_build_root_report(slot, ...)` and
  `schedule_build_sub_report(slot, ...)` plus
  `ReportBuilderProto`. The host service compiles MDX + entity
  inputs into `RootReport` / `SubReport` entities; the builtin
  renderer module handles their `FullPanel` / `BlockLink` /
  `InlineLink` / `HoverPreview` / `Figure` slots out of the box.
- `packages/docent_fxtr_proxy/` — agent-side handle + entity types
  for the demo's read-only Docent SDK proxy. Exposes
  `schedule_execute_dql(slot, ...)`, `schedule_get_dql_schema(slot,
  ...)`, `schedule_get_metadata_fields(slot, ...)`, the entity
  dataclasses each call returns (`DqlResult` / `DqlSchema` /
  `MetadataFields`), and a `dql_result_to_dicts` helper. See the
  package docstring for DQL syntax + table-shape notes.
- `packages/transcripts/` — defines the `Transcript` entity type
  (plus a `TranscriptMessage` value type) and a
  `build_transcript(...)` helper that parses a Docent
  `transcripts` row (where `messages` is a JSON string) into a
  ready-to-`store()` entity. Paired with the `renderers/transcript/`
  renderer module.
- `packages/judge_constants/` — frozen lists of judge / transcript
  names for the mental-health WildChat collection
  (`2c4ecb4e-...`): `PRIMARY_ADDITIONAL_TRANSCRIPT_NAMES` (the
  full per-model transcript names matching the primary + additional
  taxonomies), `CHAINED_JUDGES` (all 46 judge "type" names seen in
  the `chained-judges` group, model suffix stripped), and
  `CHAINED_JUDGE_MODELS`.
- `packages/demo_helper/` — marker package (host-importable, no
  deps) carrying the **type id + protocol** for the host-side
  `WorkstationDemoHelper` service (the self-contained auto-driver;
  impl lives at `examples/.../demo_helper_service.py`). You won't
  spawn this — it's mounted host-side under the external name
  `demo-helper`. Listed here because its `renderers/demo_helper/`
  visualizer + the `demoHelperType` flake wiring live in this
  template.
- `renderers/` — TSX renderer modules routed by the `default` UI preset.
  Each subdirectory has `index.tsx` + a `fxtr_renderer.nix` (its slice of
  the preset — the renderer drv + which `(type, dispatch_key)` pairs it
  covers; the root `flake.nix` merges the slices via `lib.fxtr.mkUiPreset`).
  `renderers/builtin/` ships with the demo (covers the agent panel +
  log-event renderers + the `!basic_type` link slots + the report and
  output-channel-storage slots) — **don't edit it**.
  `renderers/note/` is a worked example for the `Note` entity (from
  `presentables/`) you can crib when writing a renderer for a new type.
  `renderers/docent_fxtr_proxy/` covers the `DqlResult` / `DqlSchema` /
  `MetadataFields` entities (FullPanel / HoverPreview / BlockLink /
  InlineLink — link slots use a black-outlined `tag · shortId` pill).
  `renderers/transcript/` renders the `Transcript` entity — FullPanel
  shows a metadata header plus role-colored message cards; the link slots
  use a blue `transcript · <id>·<description>` pill and HoverPreview shows
  a snippet of the first user message.
  `renderers/delayed_echo/` renders the `delayed_echo` **service**
  (from `packages/delayed_echo/`) — unlike the others it's keyed
  by the service's `_service_type_id().id_string()`, not an entity
  type, so its components receive a `ServiceRefStub` instead of an
  `EntityRefStub`. There's no payload to load: FullPanel drives
  invokes via `ctx.allocateInvoke(stub, "echo", [value, delay])`
  (value + delay inputs, an invoke button, and a result-history of
  cards that flip from pending to the echoed value), while the
  link slots are static `delayed_echo · <shortId>` pills. It's the
  worked example to crib from when writing a renderer for a
  service rather than an entity.
  `renderers/simple_scheduler/` renders the scheduler **service** as
  a live run grid: on mount it subscribes the panel's callback inbox
  to the scheduler's event stream (same-tick `allocateCallback()` +
  callback-target `allocateInvoke(inbox.handle, "subscribe_to",
  [schedulerRef])`), follows the latest `run_channel` announcement,
  and renders the run's output channel as a live grid via a custom
  `ChannelContentCard` renderer for the `SchedulerRunTag` type
  (overriding `@fxtr-ui/rd-types-channel`'s generic stream card) —
  the worked example for subscription-driven rendering AND for
  per-stream-type channel cards.
  `renderers/simple_run_result/` renders the durable
  `SimpleRunResult` entity (same grid, loaded from the stored
  record); `renderers/note_with_links/` covers the `NoteWithLinks` /
  `PromptTemplate` entities the tasks produce/consume.
  `renderers/demo_helper/` renders the host-side `demo-helper`
  SERVICE as the **self-contained auto-driver** — a scenario picker
  whose flows spawn services + drive invokes from the UI (a
  human-clicked stand-in for an agent), presenting each result as a
  `RefLink`. Not part of the agent's own workflow; it's the
  operator-facing testing UI. Everything else is yours to add.

## What the agent does

1. `set_working_dir` to this directory.
2. `connect_workstation` — the MCP plugin reads the toml,
   attaches to the workstation, and registers an
   `agent_mcp:{session_id}` service.
3. `open_session` — creates a session on the workstation. From
   here, every `run_script` / callback / invoke result the MCP
   processes lands in the typed event log (chain head pinned to
   the `agent_mcp_store` persistence cap).
4. The operator opens the `agent_mcp:{session_id}` service in
   the browser-side workstation shell. The agent-plugin renderer
   subscribes + streams events into a live scrolling view; the
   operator can also send messages through the panel's text-box,
   which arrive in the agent's `workstation_inbox` callback.

## Quick start once connected

### Present something to the operator

```python
# in run_script
from presentables import Note
from _fxtr_mcp_builtin import present_to_user

note = Note(
    title="hello from the agent",
    body="If you're seeing this in the panel, the round-trip works!",
).store()
await present_to_user(note)
```

The frontend renders the `present_to_user` payload in the
emphasized "Presented to user" block at the bottom of the
script-execution card.

### Send a message back

Type something in the panel's text-box and click Send. The next
chain extension is a `CallbackNotificationLogEvent` with
`slot_key="workstation_inbox"` — the frontend renders it as a
"User said" card.

To consume it on the agent side:

```python
from _fxtr_mcp_builtin import retrieve_callback_notification

msg = await retrieve_callback_notification(
    slot_key="workstation_inbox",
    notif_index=...,
)
```

(The `notif_index` comes from the `notifications/claude/channel`
frame the MCP plugin pushes when the notification lands.)

### Spawn the delayed-echo service + call it

Three steps: spawn the sandbox **by git reference**, connect to it
with a config to get the typed service handle, then invoke `echo`.

The spawner builds `fxtr.services.delayed_echo` from your committed
flake at the commit you pin — so **commit first** (`current_commit()`
errors on a dirty tree, incl. untracked files). No bundling.

```python
# Step 1: schedule the spawn (after `git add -A && git commit`).
from _fxtr_mcp_builtin import (
    NixWasmServiceDefn,
    current_commit,
    lookup_service_by_name,
    schedule_invoke,
)

spawner = await lookup_service_by_name("sandbox-spawner")
defn = NixWasmServiceDefn(
    git_commit=await current_commit(),
    flake_attr="fxtr.services.delayed_echo",
).store()
await schedule_invoke("running", spawner, lambda s: s.spawn(defn))
```

Wait for the spawn-result `notifications/claude/channel` frame
(or check the panel — the spawn result lands as an
`InvokeResultLogEvent`). `retrieve_result("running")` returns a
`_SpawnConnectWrapperImpl` stub — it only exposes `.connect(config)`,
not the service methods themselves. Call `.connect(cfg)` to get
the actual typed service:

```python
# Step 2: connect with a config to get the real echo service.
from delayed_echo import DelayedEchoConfig
from _fxtr_mcp_builtin import retrieve_result, schedule_invoke

wrapper = await retrieve_result("running")
cfg = DelayedEchoConfig(label="demo").store()
await schedule_invoke("echo_svc", wrapper, lambda s: s.connect(cfg))
```

Wait for `echo_svc` to land its `invoke_result`, then call
`echo`:

```python
# Step 3: invoke echo on the connected service.
from _fxtr_mcp_builtin import retrieve_result, schedule_invoke

echo_svc = await retrieve_result("echo_svc")
await schedule_invoke(
    "delayed_echo_result", echo_svc, lambda s: s.echo("hello", 3.0)
)
```

After ~3 seconds you'll see a second `InvokeResultLogEvent` in
the panel: the echoed value comes back. The frontend renders the
payload through `JsonRefTree` (so refs in the payload would show
as clickable links). You can also pass an entity ref (e.g. a
`Note`) as the echoed value — it round-trips through the
service and comes back as the same ref, which you can
`present_to_user` on the other side.

### Talk to the network (the two haiku services)

Sandboxed services have no ambient network — neither the wasm
sandbox nor the container has its own egress. The capability comes
from the operator: the demo backend (launched with
`--allow-llm-apis` and `ANTHROPIC_API_KEY` set) registers an
`anthropic-authorizer` service — a `NetworkHandleAuthorizerProto`
that pins upstream to `https://api.anthropic.com` and injects the
real `x-api-key` host-side. You hand that ref to a spawned
service's `connect(...)`, and the service opens tunneled
connections through it
(`fxtr_sandbox_rpc_adapter.get_client(authorizer_ref)` gives back a
normal httpx client). Neither you nor the sandboxed code ever see
the key. (`internet-authorizer` is the same mechanism as an
HTTPS-passthrough — no URL pin, no injected headers.)

Two worked examples, one per sandbox kind:

- **`fxtr.services.haiku_service_wasm`** (python-only wasm sandbox,
  spawned with `NixWasmServiceDefn`): the wasm lock only takes
  pure-python wheels, so the `anthropic` SDK (native Rust
  extensions) can't come along — the service calls the Messages
  API with raw httpx over the tunnel. The lightest-weight shape;
  use it when pure-python deps suffice.
- **`fxtr.services.haiku_service_oci`** (OCI container, spawned with
  `NixContainerServiceDefn`): the image runs real Linux CPython, so
  the actual `anthropic` SDK is an ordinary dependency, pointed at
  the same tunnel via `http_client=get_client(...)`. Use this shape
  when a service needs native deps. **First spawn is slow** — nix
  builds the image and loads it into the container runtime; cached
  after that.

The spawn → connect → ask flow is the same three-step dance as
delayed_echo's, with the authorizer ref as the connect config:

```python
# Step 1 (after `git add -A && git commit`): spawn by git ref.
from _fxtr_mcp_builtin import (
    NixWasmServiceDefn,  # or NixContainerServiceDefn + ..._oci below
    current_commit,
    lookup_service_by_name,
    schedule_invoke,
)

spawner = await lookup_service_by_name("sandbox-spawner")
defn = NixWasmServiceDefn(
    git_commit=await current_commit(),
    flake_attr="fxtr.services.haiku_service_wasm",
).store()
await schedule_invoke("haiku_running", spawner, lambda s: s.spawn(defn))
```

```python
# Step 2: connect with the authorizer to get the typed service.
from _fxtr_mcp_builtin import (
    lookup_service_by_name, retrieve_result, schedule_invoke,
)

wrapper = await retrieve_result("haiku_running")
anthropic = await lookup_service_by_name("anthropic-authorizer")
await schedule_invoke("haiku_svc", wrapper, lambda s: s.connect(anthropic))
```

```python
# Step 3: ask.
from _fxtr_mcp_builtin import retrieve_result, schedule_invoke

svc = await retrieve_result("haiku_svc")
await schedule_invoke(
    "haiku_answer", svc, lambda s: s.ask("Write a haiku about workstations.")
)
```

If `lookup_service_by_name("anthropic-authorizer")` fails, the
backend wasn't launched with `--allow-llm-apis` (or the key wasn't
set) — ask the operator.

### Build a report (MDX → RootReport / SubReport)

The host runs an MDX-compiling service under the external name
`"report-builder"`. The agent supplies the MDX source + named
entity inputs; the service compiles, wraps the result in a
`RootReport` or `SubReport`, and returns the entity ref. The
frontend renders it through the builtin renderer module
(`FullPanel` shows the full document; `BlockLink` /
`InlineLink` / `HoverPreview` show the title; for `SubReport`,
the `Figure` slot renders the overview inline inside a parent
report).

```python
# Step 1: schedule the build.
import report_builder
from _fxtr_mcp_builtin import lookup_entity, present_to_user, retrieve_result

note_ref = await lookup_entity("demo-note")  # any entity ref
await report_builder.schedule_build_root_report(
    "my_report",
    title="A demo report",
    summary="Compiled from MDX with a Note inline.",
    source=(
        "# Hello\n\n"
        "Here's the seeded demo note: <RenderLink entity={fxtr_inputs.note}/>\n"
    ),
    inputs={"note": note_ref},
)
```

Wait for `my_report` to land (via `wait_seconds=N` on the
enclosing `run_script` or a follow-up call) and then:

```python
# Step 2: retrieve the ref + show it to the operator.
report_ref = await retrieve_result("my_report")
await present_to_user(report_ref)
```

The operator can click the link in the rendered card to open
the report in a `FullPanel`. Inline `<RenderLink>` / `<RenderFigure>`
tokens dispatch back through the workstation's renderer registry —
so any entity type that has a registered `InlineLink` / `Figure`
renderer (e.g. `Note`, `SubReport`) shows up the right way
without further per-renderer work.

`schedule_build_sub_report(slot, label=, overview_source=,
detail_source=, ...)` follows the same shape but takes two MDX
bodies — the `overview` shown when embedded as a figure inside a
parent report, and the `detail` shown below it in `FullPanel`
mode.

On MDX compile or build failure the slot promotes to
`invoke_error` carrying the compiler's error message — fix the
source and resubmit.

## Hand-offs to remember

- `TodoList(title=..., items=("a", "b", "c"))` works the same
  way — useful when you want the agent to surface a structured
  plan rather than ad-hoc prose.

## Working notes (things that bit me; keep handy)

A grab-bag of surprises and habits learned from working in this
sandbox. Read before writing long scripts — most of these would
have been hard to guess from the surrounding docs.

### Where logic should live

**Keep `run_script` bodies short.** Each call's globals start
fresh and there's no editor-feedback on what you write inline.
Put real logic in:

- `preamble.py` — re-exec'd before every script; good for shared
  imports, small helpers, and constants you reuse.
- `packages/<name>/src/<name>/` — anything non-trivial. Define
  entity types, async helpers, MDX sources, judge constants,
  etc. Then a `run_script` becomes a one-liner: `import foo;
  await foo.do_the_thing(...)`.

Long inline scripts are painful to iterate on (no syntax check
until you submit, no version control, can't be imported by other
scripts). The included `report_builder` README example is
inline-MDX-as-string for terseness, but in practice
**save MDX in a package** alongside the build helper that
schedules it — `packages/session_report/` is a worked example.
You edit the string and rerun a one-line script to rebuild.

### MDX in reports

- **Use `<RenderFigure entity={...}/>` for block-level entity
  embeds.** Figure has a panel fallback, so it works even if the
  entity's renderer module doesn't register a Figure key. Reserve
  `<RenderLink entity={...}/>` for inline references inside a
  sentence (it renders as a small pill).
- **Inputs are referenced as `props.fxtr_inputs.<name>`**, not
  bare `fxtr_inputs.<name>`. The bare form errors with
  `ReferenceError: fxtr_inputs is not defined`. (The
  report_builder package docstring's quick-start shows the bare
  form — wrong; the README example block above uses the bare
  form too — also wrong. Use `props.fxtr_inputs.<name>`.)

### Sandbox builtins

- **`retrieve_result(slot_key)` returns an `FxtrID`, not an
  `EntityRef`.** To load the entity, wrap it:
  `await EntityRef(fid, MyEntityClass).load()`. The
  `docent_fxtr_proxy` package docstring shows
  `retrieve_result(slot_key, EntityRef[T])` — that overload
  doesn't exist; ignore it.
- **Slot keys must be unique per `run_script`.** Calling
  `forget(slot_key)` and re-using the same key in the **same**
  script raises `SlotKeyAlreadyExists` — the uniqueness check
  is per-script, so pick a new key (`"foo_v2"`) or split the
  forget into a prior script.
- `schedule_invoke`'s action lambda receives a recording proxy
  and must `return` (not just call) exactly one method
  invocation on it. The lambda runs synchronously to *record*
  the call shape; the actual invoke is scheduled.

### Entity refs / service refs

- The method to retype a `ServiceRef` is
  **`replace_service_type(Proto)`**, not `replace_target_type`.
  A couple of agent-side handle packages shipped with a stale
  `replace_target_type` call (`docent_fxtr_proxy`, `report_builder`)
  — fixed locally now, but expect the same drift in other workspace
  packages.

### DQL gotchas (extra to what the package docstring says)

- **`ORDER BY` on large tables triggers `canceling statement due
  to statement timeout`.** Filter to a small set first (e.g. by
  id, or by joining against a small table) before sorting.
- **`jsonb_object_keys` is rejected** — `"Function … is not
  allowed in Docent Query Language."` Despite the docstring
  listing JSON ops generically. Workaround: list known keys
  explicitly via `metadata_json->>'foo'`.
- **`COUNT(*)` is rejected** — use `COUNT(<column>)`.
- **`SELECT *` is rejected** — list columns explicitly.
- The `results` table is permission-denied in some collections;
  drop it from any cross-table union you build.
- `get_dql_schema` upstream may return HTTP 504 HTML (broken
  endpoint in some deployments) — don't rely on it; the package
  docstring documents the five-table schema, which is the stable
  contract.

### Docent transcript shape (when fetched via DQL)

- `transcripts.messages` comes back as a **JSON string**, not a
  parsed structure. `json.loads(...)` it.
- Each message is `{role, content, id, metadata, tool_call_id}`.
  `content` is **sometimes a string, sometimes a list of parts**
  (each part is a dict with a `text` field). Normalize before
  storing — see `transcripts.build_transcript` for one approach.

### Renderer iteration

- Renderers are **CODE**: edit TSX under `renderers/<name>/`, then
  **commit** and re-push the preset by git ref:

  ```python
  # run_script (after `git add -A && git commit`)
  from _fxtr_mcp_builtin import (
      NixDispatcherProto, current_commit, lookup_service_by_name, schedule_invoke,
  )
  dispatcher = (await lookup_service_by_name("renderer-dispatcher")) \
      .replace_service_type(NixDispatcherProto)
  await schedule_invoke("set_preset", dispatcher, lambda d: d.set_preset(
      current_commit(), "fxtr.uiPresets.default"))
  ```

  The dispatcher rebuilds the changed renderer + broadcasts a reload.
  If anything fails (bad import, build error, a preset key that doesn't
  match its type), `set_preset` raises (`invoke_error`) and the
  **previous renderer table stays in effect** — fix, commit, re-push.
  (Adding a *new* renderer also needs a `fxtr_renderer.nix` slice wired
  into the flake — see "Writing a custom visualizer" below.)
- The four shell dispatch keys (`FullPanel`, `BlockLink`,
  `InlineLink`, `HoverPreview`) are usually enough for a custom
  entity. Add `Figure` only if you want a non-fallback inline
  render inside a parent report.

### Editor noise to ignore

Pyright in this workdir doesn't see the sandbox's `sys.path`,
so imports of `fxtr2.*`, `_fxtr_mcp_builtin`, and local
packages all show as unresolved. That's expected; the scripts
still run.

## Writing a custom visualizer

The frontend's panel uses *dynamic dispatch*: for each entity or
service it needs to render, it asks the backend's
`renderer-dispatcher` service "got a renderer for type X +
dispatch key Y?". You teach the dispatcher about a new entity type by
adding a TSX module + its `fxtr_renderer.nix` slice under
`renderers/<name>/`, wiring it into the flake, committing, and
re-pushing the preset (`set_preset`).

The flow:

### 1. Define a new entity type in a Python package

Add it to one of your `packages/` (e.g. extend `presentables/`):

```python
# packages/presentables/src/presentables/__init__.py
import dataclasses, uuid
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities

_NS = uuid.UUID("e6c9d5a3-4f72-4bf1-8a05-2d6b9c1e74af")  # existing namespace

@dataclasses.dataclass(frozen=True)
class Recipe(DataclassEntity, fxtr_type=system_entities.define_type(_NS, "recipe")):
    title: str
    ingredients: tuple[str, ...]
```

### 2. Look up the type's id-string

From a `run_script`:

```python
from presentables import Recipe
print(Recipe.fxtr_type.id_string())
# → e.g. "d7xfm...4ba"
```

The id-string is content-addressed and deterministic: it stays
the same as long as the namespace UUID + label
("`recipe`") stay the same.

### 3. Create the renderer folder

`renderers/recipe/` with two files. Two worked examples to crib
from:

- `renderers/note/` — covers all four shell dispatch keys
  (`FullPanel` / `BlockLink` / `InlineLink` / `HoverPreview`)
  for the `Note` entity using `makeLoadResultAtom` for the
  load + result-status plumbing. This is the simpler example.
- `@fxtr-ui/reports/handlers/{RootReport,SubReport}.tsx` —
  the canonical pattern, covering the same four shell keys
  plus a custom `Figure` key, with richer per-key UI variants
  and per-figure session storage. Use this as a model for
  anything more complex than a flat record.

**Key pattern.** Components don't get the loaded entity data as
a prop — only the stub. Each component allocates a wire load
itself, reads the result via an atom, and renders. The
`makeLoadResultAtom<T>(slotAtom, stub)` helper from
`@fxtr-ui/state-util` collapses the allocation + status-projection
into one call:

```tsx
import type { EntityRefStub, FxtrSessionAtom, OpResultStub } from "@fxtr-ui/core";
import { useDispatchSharedAtom } from "@fxtr-ui/react-dispatch";
import { atomWithDefault, makeLoadResultAtom, subAtom } from "@fxtr-ui/state-util";
import { BlockLink, FullPanel, type FullPanelProps, InlineLink, type LinkProps } from "@fxtr-ui/workstation-dispatch";
import { useAtomValue } from "jotai";
import { useMemo } from "react";

interface RecipeLoaded { readonly title: string; readonly ingredients: readonly string[] }

// FullPanel gets `ownStorage` (panel-scoped, survives panel-session lifetime).
function RecipeFullPanel({ stub, ownStorage }: { readonly stub: EntityRefStub } & FullPanelProps) {
    const resultAtom = useMemo(() => {
        const panelAtom = atomWithDefault<{ load: OpResultStub<RecipeLoaded> | null }>(
            ownStorage as FxtrSessionAtom<{ load: OpResultStub<RecipeLoaded> | null } | null>,
            { load: null },
        );
        return makeLoadResultAtom<RecipeLoaded>(subAtom(panelAtom, "load"), stub);
    }, [ownStorage, stub]);
    const result = useAtomValue(resultAtom);
    if (result.kind === "loading") return <span>loading…</span>;
    if (result.kind === "errored") return <span>error: {result.message}</span>;
    return <div>{result.value.title} ({result.value.ingredients.length} items)</div>;
}

// BlockLink / InlineLink / HoverPreview share via useDispatchSharedAtom.
function RecipeBlockLink({ stub, linkText }: { readonly stub: EntityRefStub } & LinkProps) {
    const sharedAtom = useDispatchSharedAtom<OpResultStub<RecipeLoaded> | null>(null);
    const resultAtom = useMemo(() => makeLoadResultAtom<RecipeLoaded>(sharedAtom, stub), [sharedAtom, stub]);
    const result = useAtomValue(resultAtom);
    return <span>{linkText ?? (result.kind === "filled" ? result.value.title : "…")}</span>;
}

export default {
    recipe: {
        [FullPanel.uuid]: RecipeFullPanel,
        [BlockLink.uuid]: RecipeBlockLink,
        [InlineLink.uuid]: RecipeInlineLink,
    },
};
```

The outer `recipe` key is a free-form **subtype key** — pick
something human-readable per type. The frontend treats it
opaquely; the dispatcher just needs the same string in the
renderer's `fxtr_renderer.nix` slice (below).

**`renderers/recipe/fxtr_renderer.nix`** — the slice declares which
`(type, dispatch_key)` pairs this renderer covers (the successor to the
old `fxtr_renderer.json`). It's a function the root `flake.nix` calls,
passing in the type's `forType`:

```nix
{ lib, recipeType }:
{
  recipe = {
    renderer = lib.esbuildRenderer { src = ./.; };
    subtypes."recipe" = {
      forType = recipeType;
      forDispatchKeys = [
        "21b2f3a8-7d54-4a1e-9c63-6a4b8e0f1234" # FullPanel
        "b3c4d5e6-7f89-4a01-9b23-4c5d6e7f8901" # BlockLink
        "c4d5e6f7-8901-4a12-9b34-5d6e7f890123" # InlineLink
      ];
    };
  };
}
```

The dispatch-key UUIDs are also accessible as `FullPanel.uuid` etc.
inside the TSX (the safer way to wire them there); the `.nix` slice is
the one place you hand-copy them. (`HoverPreview` is
`d5e6f7a8-9012-4a23-9b45-6e7f89012345`.)

Wire it into the flake (two small edits to `flake.nix`): build the
type's `forType` via the aggregator venv, and merge the slice in:

```nix
# add your package to type_entities/ first (so the type is importable),
# then in flake.nix's `let`:
recipeType = mkType "<paste the id-string you printed above>" "presentables:Recipe.fxtr_type";
# ...and add to rendererEntries:
//  (import ./renderers/recipe/fxtr_renderer.nix { inherit lib recipeType; })
```

### 4. Commit + push the preset

Renderers + the preset are **CODE** — commit, then re-push the preset by
git ref (the same loop as the renderer-iteration tip above):

```python
# run_script (after `git add -A && git commit`)
from _fxtr_mcp_builtin import (
    NixDispatcherProto, current_commit, lookup_service_by_name, schedule_invoke,
)
dispatcher = (await lookup_service_by_name("renderer-dispatcher")) \
    .replace_service_type(NixDispatcherProto)
await schedule_invoke("set_preset", dispatcher, lambda d: d.set_preset(
    current_commit(), "fxtr.uiPresets.default"))
```

The dispatcher evals the preset, builds each renderer's ESM + the
attached `typeEntity` layer, **validates that the preset key matches the
type's id** (so a stale paste is caught loudly), mints the renderer
entities, and broadcasts a reload. On any failure `set_preset` raises
(`invoke_error`) and the **previous renderer table stays in effect** —
fix, commit, re-push.

### Rendering a service instead of an entity

The steps above are identical for a **service** — the dispatcher
keys renderers by type-id-string regardless of whether the ref is
an entity or a service. `renderers/delayed_echo/` is the worked
example. Four deltas from the entity flow:

1. **The `forType` id is the service's type-id, not an entity's.**
   A typed service declares it in its impl
   (`_service_type_id()` in `packages/delayed_echo/`); print it the
   same way you would an entity type:

   ```python
   from delayed_echo import DELAYED_ECHO_TYPE_ID
   print(DELAYED_ECHO_TYPE_ID.id_string())
   ```

   Paste that into the flake's `mkType` call exactly as for an
   entity. The same four shell dispatch keys (`FullPanel` /
   `BlockLink` / `InlineLink` / `HoverPreview`) apply — there's no
   separate "service" dispatch key.

2. **The aggregator venv must be able to import the type-id
   constant.** `mkType` builds the typeEntity layer from the
   `type_entities/` venv (full host fxtr2) — but a sandboxed
   service package usually pins its deps to WASI, which can't
   coexist with that venv. `packages/delayed_echo/` shows the
   split that solves it: the package root is marker-style
   host-importable (no deps of its own) and joins the aggregator,
   while `sandbox_spawn/pyproject.toml` owns the wasm spawn env
   (the wasi-pinned deps + the `fxtr.service` entry point — it's
   what the flake's `mkWasmService` points at).

3. **Components receive a `ServiceRefStub`, not an `EntityRefStub`.**
   So `DispatchedComponent<ServiceRefStub, FullPanelProps>` etc.

4. **There's nothing to `load`.** A service ref has no entity
   payload, so you don't use `makeLoadResultAtom`. Instead the
   FullPanel *invokes* the service: hold per-mount state in the
   `ownStorage` atom (cast to your shape via `atomWithDefault`),
   and on a button click append an invoke allocated through the
   setter's context:

   ```tsx
   setState((prev, ctx) => ({
       invokes: [...prev.invokes, {
           id: nextId(prev.invokes),
           label: `echo(${JSON.stringify(value)}, ${delay})`,
           result: ctx.allocateInvoke(
               stub as ServiceRefStub<unknown>, "echo", [value, delay],
           ),
       }],
   }));
   ```

   `ctx.allocateInvoke(stub, methodName, args)` returns an
   `OpResultStub`; render each one with `useResult(stub)` (from
   `@fxtr-ui/react`) and switch on `status.status`
   (`pending` / `filled` / `errored`). The link slots have no data
   to show either, so they're static pills keyed off
   `stub.shortId`. See `EchoServiceFullPanel` in
   `@fxtr-ui-workstation-demo/src/client/handlers.tsx` for the
   canonical version (callbacks via `ctx.allocateCallback()` work
   the same way — see `NotifyNServiceFullPanel` there).

### Versioning persisted panel state (`ownStorage`)

The dispatcher keeps a FullPanel's `ownStorage` slot across a
`set_preset` reload. That means a slot written by an older build of
your renderer can be re-read by a newer one — and if you've
changed the stored shape in between, the persisted value is
non-null but wrong, so plain `atomWithDefault(slot, INITIAL)`
hands it back as-is and your component crashes reading a field
the stale object lacks. (This bit the `delayed_echo` renderer
during development: `Cannot read properties of undefined`.)

**Recommended for any renderer that persists `ownStorage`:** tag
the stored shape with a version string and pass a `validityCheck`
to `atomWithDefault` (its optional third argument). A non-null
value that fails the check is treated exactly like null, so the
default is substituted instead of the stale object:

```tsx
const STORAGE_KIND = "my_renderer/v1";

interface MyStorage {
    readonly kind: typeof STORAGE_KIND;
    readonly invokes: ReadonlyArray<MyInvokeSlot>;
}

const INITIAL: MyStorage = { kind: STORAGE_KIND, invokes: [] };

function isValidStorage(s: MyStorage): boolean {
    return s?.kind === STORAGE_KIND && Array.isArray(s.invokes);
}

// …in the component:
const typedStorage = useMemo(
    () =>
        atomWithDefault<MyStorage>(
            ownStorage as FxtrSessionAtom<MyStorage | null>,
            INITIAL,
            isValidStorage,
        ),
    [ownStorage],
);
```

Remember to write `kind` in every update (`setState({ kind:
STORAGE_KIND, … })`), and bump the version string (`"…/v2"`)
whenever you change the shape — old slots then auto-reset on the
next mount instead of crashing. `renderers/delayed_echo/` is the
worked example. (Renderers that don't persist `ownStorage` — the
link / hover keys, or FullPanels that only `makeLoadResultAtom`
off the stub — don't need this.)

### Import-allowance for renderer TSX

`renderers/` has no `node_modules`. esbuild compiles each
`index.tsx` with these specifiers marked external (resolved at
runtime via the frontend's import-map shim):

- `react`, `react/jsx-runtime`
- `jotai`, `jotai-effect`
- `@fxtr-ui/core`, `@fxtr-ui/state-util`,
  `@fxtr-ui/react`, `@fxtr-ui/react-dispatch`
- `@fxtr-ui/workstation`, `@fxtr-ui/workstation-dispatch`
- `@fxtr-ui/agent-plugin-renderer`, `@fxtr-ui/rd-types-stream`
- `@fxtr-ui/reports` (Figure dispatch key + RootReport / SubReport
  renderers — also the canonical example of the load-result
  pattern)

Anything else gets bundled, and since the renderer dir has no
`node_modules` for esbuild to resolve against, importing
anything outside the list above will fail the compile. Keep
your renderer files self-contained (inline-define components)
and use the curated externals above.
