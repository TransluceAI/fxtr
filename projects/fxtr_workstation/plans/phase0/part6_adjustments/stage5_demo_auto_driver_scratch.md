# stage5_demo_auto_driver — scratch

Working notes behind `stage5_demo_auto_driver.md`. Not a spec; the plan is.

## Orientation findings (so the plan's interfaces are accurate)

- **`run_demo_workstation.py`** is the orchestrator. Boots `WorkstationServer`
  with `external_services` (spawner, internet-authorizer, renderer-dispatcher,
  report-builder, optional llm-authorizers/docent) + the `agent_mcp_store`
  persistence cap, seeds `demo-note`/`demo-transcript` over peer-RPC, then
  spawns Vite. `--workdir` (required today) is the scaffolded runtime repo;
  `_workdir_head()` reads its committed HEAD; the dispatcher auto-loads the
  `default` preset at that rev.
- **`scaffold_workdir.py`** wraps `fxtr_workdir_harness.prepare_workdir_repo(template, dest)`
  (async) → copies `agent_workdir/`, rewrites the fxtr lib ref, `uv lock` +
  `nix flake lock`, `git init` + commit. Returns `.path` + `.head_commit`.
  `--force` rmtree's an existing dest. This is exactly what `--auto-workdir`
  needs to call.
- **Host-side service mount precedent** = `report_builder_service.py`:
  `@service_protocol` + `TypedServiceImpl` (`_target_type_annotation` +
  `_service_type_id`) + a `mount_*` async CM yielding `(scope, service_ref, impl)`,
  plugged into an `ExternalServiceConfig`. The helper copies this shape.
- **Service-renderer precedent** = `renderers/delayed_echo/`: keyed by the
  service's `_service_type_id().id_string()` (same table as entities), receives a
  `ServiceRefStub`, drives `ctx.allocateInvoke(stub, method, args)`, renders an
  `OpResultStub` history via `useResult`. `fxtr_renderer.nix` slice + a `mkType`
  line + a `rendererEntries //` merge in `flake.nix`; the type-id constant must be
  importable by the `type_entities/` aggregator venv (so add a host-importable
  marker package and list it in `type_entities/pyproject.toml`).
- **Spawner surface** (`SandboxSpawnerProto`, `projects/fxtr_sandbox_types`):
  `spawn(EntityRef[NixWasmServiceDefn|NixContainerServiceDefn|TaskBundle]) ->
  ServiceRef[SandboxConnectProto[Override, Override]]`. Caller must
  `replace_service_type(SandboxConnectProto[Cfg, Proto])` then
  `connect(cfg) -> ServiceRef[Proto]`. `create_sandbox_environment` exposes
  `spawner_ref` + `host_scope`.
- **Scheduler surface** (`simple_tasks.SimpleTaskSequenceSchedulerProto`):
  `run_batched(steps: list[ServiceRef[SimpleTaskProto]], inputs: dict[str, FxtrID],
  labels: list[str]|None) -> FxtrID` (a `SimpleRunResult`). So the *final result*
  is a returned entity ref — perfect for the "present a reflink back in the driver"
  model. Streaming scheduler mirrors this (per-cell subchannels; same return).

## Key frontend constraints (load-bearing)

- `FxtrSessionUpdateCtx.allocateInvoke(svc, method, args, kwargs?)` accepts args
  that are primitives / dicts / lists / `Uint8Array` / `EntityRefStub` /
  `ServiceRefStub` / `CallbackHandleStub`. **`OpResultStub` is rejected.** So to
  feed a prior step's *returned ref* into a later invoke you must first read the
  prior `OpResultStub`'s resolved value (a ref stub, after wire-unescape) via
  `useResult`, then pass that stub. This read-then-feed is the "state machine."
- The frontend **cannot mint entities**. Anything needed as an invoke arg that
  isn't already a ref on the workstation (a `NixWasmServiceDefn`, a config entity,
  a `Note` input) must be produced by a helper method. → helper owns spawn+connect
  (fiddly, uninteresting) AND entity factories (`make_note`, demo inputs); the
  frontend owns the *interesting* invokes (echo / ask / run_batched).
- `RefLink` (from `@fxtr-ui/workstation-dispatch`, on the externals allow-list):
  `<RefLink stub={someEntityOrServiceStub} as="block" />` renders a clickable link
  that opens the target in its own panel through the dispatch registry. This is the
  driver's *only* result-rendering primitive — it deliberately does **no** channel/
  grid rendering of its own. Clicking a returned scheduler `ServiceRef` opens the
  existing `simple_scheduler`/`streaming_scheduler` panel (live grid lives there).

## Division of labor (settled with Daniel)

Hybrid, leaning helper-side for plumbing:
- **Helper (Python, host-side):** workdir introspection; per-scenario spawn+connect
  returning live ref(s); entity factories for invoke args. NO duplicate grid
  subscription.
- **Frontend driver renderer:** scenario picker + a generic flow runner; each step
  is a button (+ optional text inputs) that `allocateInvoke`s against the helper
  stub or a prior result's ref; every result rendered as a `RefLink` ("present to
  user"). The scheduler's live streaming stays on its own panel; only the *final*
  `run_batched` result returns to the driver as a RefLink.

## Scenario list for v1 (always offered; error if a dependency is absent)

1. **delayed_echo** — `spawn_delayed_echo(label)` → echo svc ref (RefLink). Step 2:
   `echo(value, delay)` on it (text+number inputs) → echoed value (RefLink/JsonRefTree).
2. **simple workflow** — `spawn_simple_workflow()` → `{scheduler, task}` refs.
   `make_demo_inputs()` → `{a: note, b: note}`. Step: `run_batched([task], inputs,
   labels)` on scheduler → `SimpleRunResult` ref (RefLink → durable grid panel).
3. **streaming workflow** — `spawn_streaming_workflow()` (streaming_scheduler +
   streaming_task_lib `word_shout`) → same shape; `run_batched` with **named steps**
   → `SimpleRunResult` (cached badges). Open scheduler RefLink to watch per-cell
   live streaming.
4. **LLM ask** — `spawn_haiku()` (needs `anthropic-authorizer`; raises if absent) →
   haiku svc ref. Step: `ask(message)` (text input) → answer string.
5. **streaming vivid-description (heavy/optional)** — `spawn_vivid_workflow()`
   (streaming_scheduler + `streaming_prompt_task_oci` container, connect cfg
   `{authorizer, prompt_template}`) → refs; `make_demo_inputs()` notes; `run_batched`
   → result. Mark heavy (OCI first-spawn slow); offered, errors w/o authorizer.

Impl detail to settle: where the `PromptTemplate` for #5 comes from. `simple_tasks`
defines `PromptTemplate` (host-importable via the aggregator path); the helper can
construct one inline rather than host-importing the OCI package's
`VIVID_DESCRIPTION_PROMPT`.

## Open detail: returning multiple refs from one method

`spawn_simple_workflow` returns a dict `{"scheduler": ref, "task": ref}` as
`FxtrServiceData`; invoke-result delivery walks + bridges each ref leaf fine. The
frontend reads the filled dict and pulls each stub. Alternative (two separate spawn
methods) is more clicks; the dict is cleaner. Driver renders each leaf as a RefLink.

## Auto-workdir specifics (settled)

- New flag `--auto-workdir` (mutually exclusive with `--workdir`; argparse group).
- Fixed dest `~/.cache/fxtr_demo_workdir` (expanduser). **Always** re-scaffold:
  rmtree if present, then `prepare_workdir_repo(_TEMPLATE, dest)`. Reuse the
  scaffold logic (import `prepare_workdir_repo` like `scaffold_workdir.py` does, via
  the same `sys.path.insert` for `nix/fxtr_workdir_harness/src`).
- `_run` then proceeds with the returned `.path` as `workdir` exactly as `--workdir`
  does. Print a line noting it auto-scaffolded.
- `start_demo.sh` stays workdir-positional; add a sibling note / maybe a
  `start_demo_auto.sh` is unnecessary — `uv run python run_demo_workstation.py
  --auto-workdir` is enough. (Decide: keep one-liner in README.)

## Helper surfacing (settled)

External name `demo-helper`; renderer wired into `default` preset (renders on boot).
**No** auto-open (leave in services list, open manually — avoids editing the
workstation shell). Mounted by `run_demo_workstation.py` like report-builder, but
the mount needs the spawner ref + host_scope + optional authorizer refs + workdir
path + HEAD, so `mount_demo_helper(...)` takes those as args (constructed inside the
`create_sandbox_environment` block where they're available).
