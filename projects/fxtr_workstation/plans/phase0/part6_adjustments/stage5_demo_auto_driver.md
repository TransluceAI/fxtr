# Stage 5: Self-contained demo auto-driver

A way to exercise the workstation demo's services — and *look at* the UI they
render, especially the output-channel-redo grids — **without** attaching an agent
over MCP. Three independent pieces, wired into `examples/fxtr_workstation_demo/`:

1. An **`--auto-workdir`** mode for `run_demo_workstation.py` that re-scaffolds a
   throwaway runtime workdir at a fixed cache path on every launch, so you get a
   current workdir with one command.
2. A lightweight host-side **`WorkstationDemoHelper`** service mounted into the demo
   workstation. It exposes workdir introspection plus per-scenario *spawn-and-connect*
   methods and entity factories — the fiddly plumbing a human can't do from the
   browser.
3. A **driver renderer** for that service: a small in-panel UI that runs a handful
   of **scenario flows**. Each flow is a state machine of steps; clicking a step
   invokes a method on the helper (or on a service a prior step produced) and shows
   the result as a clickable `RefLink`. This is a stand-in for "ask the agent to
   spawn X and run Y" — except the human drives it by clicking, and results come
   back the way an agent would surface them (a reflink the user opens in a panel).

See `stage5_demo_auto_driver_scratch.md` for the orientation findings and the
load-bearing frontend constraints this design rests on.

## Design model (read first)

The driver panel is deliberately **thin**: it spawns services, **presents** their
refs, invokes methods, and **presents** the results — nothing more. It never
subscribes to or re-renders a run's live channel/grid. That mirrors how an agent
works: its only affordance for showing the operator a result is to hand back a
reflink. So:

- A spawn step's result is a **service ref** → rendered as a `RefLink`. Click it to
  open that service's *existing* renderer panel (e.g. the `simple_scheduler` grid),
  where the live streaming subscription lives.
- An "interesting invoke" step (e.g. `run_batched`) runs *on* that service ref. Its
  live progress streams on the scheduler's own panel; its **final return value** (a
  `SimpleRunResult` entity ref) comes back to the driver as the invoke result →
  another `RefLink` (opens the durable-grid renderer).

Two hard constraints shape who-does-what (details in scratch):
- `allocateInvoke` args may be ref stubs but **not** `OpResultStub`s — so chaining =
  read a prior result's resolved ref via `useResult`, then feed that stub forward.
- the frontend **cannot mint entities** — so spawn defns, connect configs, and note
  inputs are all produced by helper methods.

## Part A — `--auto-workdir`

**File:** `examples/fxtr_workstation_demo/run_demo_workstation.py`.

Add a flag that scaffolds a fresh runtime workdir instead of requiring `--workdir`.

- Reuse the scaffold logic directly: import `prepare_workdir_repo` from
  `fxtr_workdir_harness` (same `sys.path.insert` for
  `nix/fxtr_workdir_harness/src` that `scaffold_workdir.py` uses), and the
  `_TEMPLATE = .../agent_workdir` path.
- argparse: put `--workdir` and `--auto-workdir` in a **mutually-exclusive group**;
  exactly one required. `--auto-workdir` takes no value.
- Fixed dest: `~/.cache/fxtr_demo_workdir` (expanduser). On `--auto-workdir`:
  `shutil.rmtree(dest, ignore_errors=True)` then
  `await prepare_workdir_repo(_TEMPLATE, dest)`; use the returned `.path` as the
  `workdir` for the rest of `_run`. Always re-scaffold (no staleness detection — too
  hard to get right; re-scaffold is a few seconds of `uv lock` + `nix flake lock`).
- This means the scaffold runs *inside* `anyio.run(_run, ...)` (it's async), so move
  the scaffold call to the top of `_run` (gated on a new `auto_workdir: bool` param +
  an optional `workdir`), or scaffold in a tiny async prelude before the
  `create_sandbox_environment` block. Print a line noting the auto-scaffold path +
  HEAD, mirroring `scaffold_workdir.py`'s output.

Pseudocode (top of `_run`):

```
if auto_workdir:
    dest = (Path.home() / ".cache" / "fxtr_demo_workdir").resolve()
    shutil.rmtree(dest, ignore_errors=True)
    prepared = await prepare_workdir_repo(_TEMPLATE, dest)
    workdir = prepared.path     # else: workdir = the --workdir value
head = _workdir_head(workdir)
... # unchanged from here
```

**README:** document `uv run python run_demo_workstation.py --auto-workdir`
(+ `--allow-llm-apis` etc. still forward). `start_demo.sh` is unchanged (it stays
workdir-positional for the explicit-workdir path).

## Part B — the `WorkstationDemoHelper` service

Three sub-pieces: a host-importable marker package (for the type id + protocol + any
helper-specific entity types), the host impl + mount CM, and the flake/aggregator
wiring.

### B1. Marker package `agent_workdir/packages/demo_helper/`

Modeled on the host-importable half of `packages/delayed_echo/` (marker-style, no
deps — `fxtr2` supplied by whichever venv embeds it). Contents
(`src/demo_helper/__init__.py`):

- `DEMO_HELPER_NS` (fresh stable UUID) + `DEMO_HELPER_TYPE_ID =
  define_type(DEMO_HELPER_NS, "demo_helper")`.
- `@service_protocol class WorkstationDemoHelperProto` — the method surface
  (see B2). Declared here so both the host impl and (conceptually) the renderer's
  type id agree; wire dispatch is by method name.
- Any helper-only entity types needed as factory return types live here too if
  they aren't already defined elsewhere (notes come from `presentables.Note`;
  prompt templates from `simple_tasks.PromptTemplate` — reuse those, don't
  redefine).

No `sandbox_spawn/` half — the helper is a **host** service (never spawned into a
sandbox), so unlike `delayed_echo` it needs only the host-importable package.

### B2. Host impl + mount — `examples/fxtr_workstation_demo/demo_helper_service.py`

Modeled on `report_builder_service.py`. A `TypedServiceImpl[WorkstationDemoHelperProto]`
constructed with everything it needs to do spawns host-side:

```
@dataclass
class DemoHelperDeps:
    workdir: Path
    head_commit: str
    spawner_ref: ServiceRef           # sandbox_env.spawner_ref
    host_scope: FxtrScope             # sandbox_env.host_scope (to load defns/cfgs against)
    anthropic_authorizer: ServiceRef | None
    internet_authorizer: ServiceRef | None
```

`mount_demo_helper(deps) -> async CM` yielding `(scope, service_ref, impl)` — same
shape as `mount_report_builder`. Called from `run_demo_workstation._run` *inside* the
`create_sandbox_environment` / authorizer block where `spawner_ref` + `host_scope` +
authorizer refs exist, and added to `external_services` under a new
`DEMO_HELPER_NAME = "demo-helper"`.

**Method surface** (the division: plumbing + factories here, interesting invokes in
the frontend). All spawn methods do the documented dance internally:
`NixWasmServiceDefn(git_commit=self.head_commit, flake_attr=...).store(call_scope)` →
`spawner.spawn(defn)` → `wrapper.replace_service_type(SandboxConnectProto[Cfg,Proto])`
→ `wrapper.connect(cfg)` → `.replace_service_type(Proto)` → return the ref (stored in
the dispatch `call_scope`, copied back to the caller by the RPC layer).

Introspection:
- `workdir_path() -> str` — `str(self.workdir)`.
- `current_commit() -> str` — `self.head_commit`.

Entity factories (invoke args the frontend can't mint):
- `make_note(title: str, body: str) -> EntityRef[Note]` — store a `presentables.Note`.
- `make_demo_inputs() -> FxtrServiceData` — a small canned `{name: noteRef}` dict for
  batch inputs (a couple of `Note`s).

Spawn+connect (return live connected ref, or a small `{role: ref}` dict when a
scenario needs more than one service):
- `spawn_delayed_echo(label: str) -> ServiceRef[DelayedEchoProto]` — flake
  `fxtr.services.delayed_echo`, connect cfg `DelayedEchoConfig(label)`.
- `spawn_simple_workflow() -> FxtrServiceData` — returns `{"scheduler": ref,
  "task": ref}`: spawn `fxtr.services.simple_scheduler` (cfg `{}`) + a task from
  `fxtr.services.simple_task_lib` (connect cfg picks the task).
- `spawn_streaming_workflow() -> {"scheduler", "task"}` —
  `fxtr.services.streaming_scheduler` (cfg `{}`) + `fxtr.services.streaming_task_lib`
  (cfg `"word_shout"`).
- `spawn_haiku() -> ServiceRef[HaikuProto]` — `fxtr.services.haiku_service_wasm`,
  connect cfg = `self.anthropic_authorizer`; **raise a clear error if it's None**
  (so the frontend shows an errored result — matches "offer all scenarios, error if
  the dependency is absent").
- `spawn_vivid_workflow() -> {"scheduler", "task"}` *(heavy/optional)* —
  `fxtr.services.streaming_scheduler` + `fxtr.services.streaming_prompt_task_oci`
  (`NixContainerServiceDefn`), connect cfg `{"authorizer": anthropic, "prompt_template":
  <PromptTemplate ref>}`. Raises without the authorizer. Mark in docs as slow on
  first spawn (OCI image build).

Notes:
- A method returning multiple refs returns them in a plain dict (`FxtrServiceData`);
  invoke-result delivery bridges each ref leaf. The frontend reads the dict and pulls
  each stub for use as RefLink + as a later invoke's target/arg.
- The "interesting invokes" (`echo`, `ask`, `run_batched`) are **not** helper methods
  — the frontend calls them directly on the returned service refs.
- `_target_type_annotation` / `_service_type_id` return the proto / `DEMO_HELPER_TYPE_ID`.

### B3. Flake + aggregator wiring

- Add `demo_helper` to `type_entities/pyproject.toml` (dependency + `[tool.uv.sources]`
  path) so the aggregator venv can import `DEMO_HELPER_TYPE_ID`.
- `flake.nix`: a `demoHelperType = mkType "<id-string>" "demo_helper:DEMO_HELPER_TYPE_ID"`
  line (paste the id-string printed from a `run_script` once, like the others), and a
  `// (import ./renderers/demo_helper/fxtr_renderer.nix { inherit lib demoHelperType keyIds; })`
  in `rendererEntries`.

## Part C — the driver renderer `agent_workdir/renderers/demo_helper/`

`index.tsx` + `fxtr_renderer.nix` (slice keyed on `demoHelperType`, the four shell
keys — really only `FullPanel` matters; link/hover are static pills like
`delayed_echo`'s). Components receive a `ServiceRefStub` (the helper).

### Architecture: one component per scenario + shared primitives

No uniform `Step[]` abstraction — each scenario is **its own React component** that
manages its own state directly. The only shared machinery is a couple of small
primitives so result-rendering and the invoke-button pattern aren't copy-pasted.

**Top-level `DemoHelperFullPanel`** holds just the picker state:

```
interface PanelStorage {
  kind: "demo_helper/v1";
  activeScenario: string | null;     // scenario id, or null = show picker
}
```

When `activeScenario` is null it renders a **picker** (one entry per scenario: title
+ blurb + "open"). Otherwise it renders that scenario's component, passing it the
helper `stub` and a **dedicated state sub-atom** so each scenario persists
independently:

```
// panelAtom = atomWithDefault(ownStorage, PANEL_INITIAL, isValidPanel)
// each scenario gets its own slice, keyed by id, under a `scenarios` record:
const scenarioAtom = subAtom(subAtom(panelAtom, "scenarios"), scenarioId);
<DelayedEchoScenario stub={stub} storage={scenarioAtom} onBack={() => setActive(null)} />
```

Each **scenario component** owns its slice via `atomWithDefault(storage, SCENARIO_INITIAL,
isValid)` with *its own* shape + version guard (README "Versioning persisted panel
state"), holds whatever `OpResultStub`s + text-field values it needs as plain named
fields (no generic slot map), wires its buttons with `setState((prev, ctx) => …)`
updaters that call `ctx.allocateInvoke(...)`, and reads results with `useResult`. A
**Reset** button sets its slice back to `SCENARIO_INITIAL` — which drops the stored
`OpResultStub`s, clearing the outputs (the same-tick contract reaps the unreferenced
slots on the next flush); **Back** calls `onBack` (and resets the slice). This is the
"clear outputs" affordance, per-scenario.

**Shared primitives** (defined once in the file, inline):
- `<ResultCard stub={OpResultStub} />` — `useResult`; renders pending → "running…",
  errored → message, filled → `<RefLink stub={value} as="block" />` when the value is
  a ref (entity/service), a small `<pre>`/`JsonRefTree` for scalars, and one `RefLink`
  per ref leaf for a `{role: ref}` dict result.
- (optional) a tiny `useInvokeSlot` helper wrapping the "store an `OpResultStub` field +
  read it via `useResult`" idiom, if it meaningfully cuts repetition.

**Example — `DelayedEchoScenario`** (concrete shape; others analogous):

```
interface State { kind; spawn: OpResultStub|null; echoes: OpResultStub[]; label; value; delay }
// "Spawn echo service" button:
setState((p, ctx) => ({ ...p, spawn: ctx.allocateInvoke(stub, "spawn_delayed_echo", [p.label]) }))
// once spawn is `filled` (its value is the echo ServiceRefStub), "echo" button reads
// that stub (captured from useResult(spawn)) and appends:
setState((p, ctx) => ({ ...p, echoes: [...p.echoes,
    ctx.allocateInvoke(echoStub, "echo", [p.value, p.delay])] }))
// render: <ResultCard stub={spawn}/> (the scheduler/echo reflink) + a card per echo.
```

`SimpleWorkflowScenario` reads a `{scheduler, task}` spawn result + a `make_demo_inputs`
result, then invokes `run_batched([taskStub], inputsDict, labels)` on the scheduler
stub; the returned `SimpleRunResult` ref renders through `<ResultCard>`. Streaming /
LLM / vivid scenarios follow the same skeleton with their own fields.

### Scenarios shipped (v1)

`delayed_echo`, `simple workflow`, `streaming workflow`, `LLM ask`, and (optional,
flagged heavy) `streaming vivid-description`. All are always listed; ones whose
helper method raises (missing authorizer) simply show an errored result slot.

### Renderer constraints

Imports restricted to the externals allow-list (react, jotai, the `@fxtr-ui/*`
packages incl. `@fxtr-ui/workstation-dispatch` for `RefLink` + `@fxtr-ui/react` for
`useResult`). Self-contained file. Version the `ownStorage` shape + `isValidStorage`
guard (README "Versioning persisted panel state").

## Implementation order

1. **Part A** (`--auto-workdir`) — independently testable: `uv run python
   run_demo_workstation.py --auto-workdir --no-frontend` should scaffold + boot.
2. **Part B1+B2** — marker package + host impl + `mount_demo_helper`, wired into
   `_run`'s `external_services` under `demo-helper`. Verify it appears in the
   workstation services list (no renderer yet → falls back to the generic service
   card).
3. **Print the type id** (a throwaway `run_script` or a tiny host print) and **B3**
   wire `demoHelperType` + the renderer slice into the flake.
4. **Part C** — the renderer (picker + per-scenario components + shared `ResultCard`).
   Iterate via commit + `set_preset` (or, since `--auto-workdir` re-scaffolds, just
   relaunch). Verify by driving each scenario in the UI (this replaces tests).
5. **Update docs** (`agent_workdir/README.md` package + renderer lists; demo top-level
   README for `--auto-workdir`; note the helper service). The
   `projects/fxtr_workstation` package CODE_OVERVIEW doesn't track the example demo's
   internals, so the README is the doc to keep current.

## Testing

**No explicit tests for this stage.** The driver *is* the test harness — verification
is driving the scenarios in the live UI (`--auto-workdir`, open the `demo-helper`
service, run each flow, confirm spawns/invokes/results render and the reflinks open
the right panels). The helper's spawn plumbing reuses the same flake/spawner paths the
existing demo + MCP already cover, and the renderer has no package-local test harness
in this demo. So we skip pytest/vitest here and rely on manual UI verification.

## Notes / things to revisit

- The vivid-description scenario (#5) needs a `PromptTemplate` host-side; plan is to
  construct one inline from `simple_tasks.PromptTemplate` rather than host-import the
  OCI package's `VIVID_DESCRIPTION_PROMPT`. Confirm the template shape at impl time.
- Returning `{role: ref}` dicts from spawn methods assumes invoke-result delivery
  bridges every ref leaf in a dict payload (it does for `delayed_echo`'s value
  round-trip). If a multi-ref payload misbehaves, fall back to one spawn method per
  service (more clicks).
- `mount_demo_helper` must be opened inside the `create_sandbox_environment` block
  (it needs `spawner_ref`/`host_scope`); make sure its scope is included in the
  workstation's `root_scope` via the normal `ExternalServiceConfig` bridge, and that
  it's torn down before the sandbox env (CM nesting order).

## Implementation checklist

- [x] Part A: `--auto-workdir` flag + scaffold-in-`_run` + README note
- [x] Part B1: `packages/demo_helper/` marker package (type id + proto) — id-string
      `dvtsrume4lojlinae4jbn4jdsdf7eemqxis3p4ryuo2ops5se4aqq`
- [x] Part B2: `demo_helper_service.py` impl + `mount_demo_helper`; wired into `_run`
      under `demo-helper`
- [x] Part B3: `type_entities/pyproject.toml` + `flake.nix` (`demoHelperType` +
      renderer slice merge)
- [x] Part C: `renderers/demo_helper/` (`index.tsx`: picker + per-scenario components
      + shared `ResultCard`; `fxtr_renderer.nix`) — compiles clean through esbuild
- [x] Update `agent_workdir/README.md` (package + renderer lists) and the demo
      top-level README (`--auto-workdir` + self-contained-driver section)
