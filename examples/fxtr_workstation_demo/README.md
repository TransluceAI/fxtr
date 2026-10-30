# fxtr_workstation_demo

The main demo for the workstation — brings together the
workstation server, the agent MCP plugin, and the agent-plugin
renderer so you can launch a session, connect Claude Code, and
watch the agent work in real time (with a text-box at the bottom
to send messages back).

## Layout

```
examples/fxtr_workstation_demo/
├── README.md                          this file (operator setup)
├── scaffold_workdir.py                copy agent_workdir/ → its own runtime git repo
├── run_demo_workstation.py            orchestrator: boots workstation + spawns Vite
├── report_builder_service.py          MDX → RootReport / SubReport builder service
├── docent_service.py                  read-only Docent SDK proxy service
├── demo_helper_service.py             host-side driver helper (spawn+connect + entity factories)
├── pyproject.toml                     Python deps for the backend script (+ pytest dev group)
├── start_demo.sh                      one-command launcher (takes the workdir path)
├── tests/                             host-side tests for the agent_workdir packages
│   ├── conftest.py                    sys.path inserts for packages/*/src + lease-leak guard
│   ├── test_simple_tasks_entities.py  entity round-trips for the simple_tasks types
│   ├── test_simple_scheduler_impl.py  scheduler impl tests (guest-style mount harness)
│   └── test_streaming_scheduler_impl.py  nested-channel scheduler impl tests
├── frontend/                          Vite-based frontend (in the v2 pnpm workspace)
│   ├── package.json
│   └── src/client/{main,App}.tsx      installs externals + boots createDynamicDispatcher
├── agent_workdir/                     TEMPLATE for the runtime workdir (scaffolded out)
│   ├── README.md                      (agent-facing — quick start + visualizer howto)
│   ├── simple_workflow_guide.md       (agent-facing — scheduler/task-system walkthrough)
│   ├── .fxtr_workstation.toml.example
│   ├── .gitignore
│   ├── preamble.py
│   ├── flake.nix                      fxtr.services / fxtr.entities / uiPresets.default
│   ├── type_entities/                 aggregator venv root for the typeEntities
│   ├── packages/
│   │   ├── presentables/              entity types the agent constructs + present_to_user
│   │   ├── delayed_echo/              a sandboxed service (host-importable pkg + sandbox_spawn/ wasm env)
│   │   ├── haiku_service_wasm/        Claude Haiku via raw httpx through the anthropic authorizer (wasm)
│   │   ├── haiku_service_oci/         Claude Haiku via the real anthropic SDK (nix2container OCI image)
│   │   ├── simple_tasks/              workflow-demo marker pkg: protocols + entities + event docs
│   │   ├── simple_task_lib/           basic SimpleTask transforms (wasm; connect config picks the task)
│   │   ├── prompt_task/               Note → Note through an LLM ask-service + stored PromptTemplate (wasm)
│   │   ├── simple_scheduler/          the batch scheduler service (host-importable pkg + sandbox_spawn/)
│   │   ├── streaming_tasks/           nested-channel demo marker pkg: streaming protocols + tag types
│   │   ├── streaming_task_lib/        streaming tasks (wasm; word_shout emits partials through a sink)
│   │   ├── streaming_scheduler/       nested-channel scheduler (host-importable pkg + sandbox_spawn/)
│   │   ├── report_builder/            agent-side handle for the report-builder service
│   │   ├── docent_fxtr_proxy/         agent-side handle + entity types for the docent-proxy
│   │   └── demo_helper/               marker pkg: type id + protocol for the demo-driver helper SERVICE
│   └── renderers/                     TSX renderer modules + per-dir fxtr_renderer.nix slices
│       ├── builtin/                   shipped — covers agent panel + log-event + report + channel-storage slots
│       ├── note/                      sample renderer for the Note entity
│       ├── transcript/               renderer for the Transcript entity
│       ├── docent_fxtr_proxy/         renderers for DqlResult / DqlSchema / MetadataFields
│       ├── delayed_echo/              renderer for the delayed_echo SERVICE (invoke-driven)
│       ├── simple_scheduler/          thin scheduler panel + the run-tag channel grid card
│       ├── streaming_scheduler/       nested-channel panel + run grid w/ embedded per-cell streams
│       ├── simple_run_result/         grid view of the durable SimpleRunResult entity
│       ├── note_with_links/           NoteWithLinks / PromptTemplate entity renderers
│       └── demo_helper/               the self-contained auto-driver UI (scenario flows)
└── .data/                             SQLite-backed agent_mcp_store (gitignored)
```

`run_demo_workstation.py` is the orchestrator: it boots the workstation,
mounts a `NixConfigurableDispatchImpl` against the **scaffolded runtime
workdir repo** (auto-loading the `default` UI preset at that repo's HEAD,
so the agent panel renders on boot), then spawns the Vite frontend as a
child. The frontend mints a `createDynamicDispatcher` against the
`renderer-dispatcher` service; the dispatcher resolves type-id lookups
from the preset. The agent extends the renderer set by adding a renderer
+ a `mkUiPreset` slice to the flake, committing, and calling
`set_preset(current_commit(), "fxtr.uiPresets.default")`.

> **The runtime workdir is its own git repo**, scaffolded *out* of this
> monorepo by `scaffold_workdir.py` (so `current_commit()`'s clean-tree
> guard works and the agent commits there, not into fxtr). The `agent_workdir/`
> here is the version-controlled **template**.

## Setup

### 1. Install the MCP plugin (one-time)

The plugin lives at
`projects/fxtr_workstation_agent_plugin/claude_plugin/`. From a
Claude Code session, anywhere:

```
/plugin marketplace add /abs/path/to/projects/fxtr_workstation_agent_plugin/claude_plugin
/plugin install fxtr_workstation@fxtr-workstation
```

(For iteration during development you can also pass `--plugin-dir`
instead; see
`projects/fxtr_workstation_agent_plugin/README.md` for the
alternate-flag tokens.)

### 2. Install workspace deps (one-time)

The frontend rides in `v2/ts`'s pnpm workspace:

```bash
cd /abs/path/to/v2/ts
pnpm install
```

The backend rides in this demo's own `pyproject.toml`:

```bash
cd /abs/path/to/examples/fxtr_workstation_demo
uv sync
```

### 3. Scaffold a runtime workdir (one-time per workdir)

The runtime workdir is its **own git repo**, copied out of the in-repo
`agent_workdir/` template. Pick a location outside the fxtr repo:

```bash
cd /abs/path/to/examples/fxtr_workstation_demo
uv run python scaffold_workdir.py ~/fxtr-demo-workdir
```

This copies the template, pins its `fxtr` flake input to *this* checkout,
locks (`uv lock` / `nix flake lock`), `git init`s + commits, and prints
the path + HEAD. Re-run with `--force` to re-scaffold from an updated
template.

> **Shortcut for UI testing — `--auto-workdir`.** Instead of scaffolding a
> workdir yourself and passing `--workdir`, run
> `uv run python run_demo_workstation.py --auto-workdir`: it re-scaffolds a
> throwaway workdir at `~/.cache/fxtr_demo_workdir` on every launch (overwriting
> whatever's there) and boots against it. One command, always a current workdir —
> handy with the self-contained driver below (no agent / MCP needed). The two
> modes are mutually exclusive.

### 4. Launch the demo

```bash
./start_demo.sh ~/fxtr-demo-workdir
```

`run_demo_workstation.py` (the orchestrator) brings up both halves:

- **Workstation** on `ws://127.0.0.1:8642`, persistence cap at
  `.data/agent_mcp_store.sqlite` (`agent_mcp_store`), spawner +
  renderer-dispatcher pointed at the scaffolded workdir repo.
- **Frontend** (Vite on `http://localhost:3470`) — spawned as a child
  of the backend; the workstation shell with the agent-plugin renderer.

> **First boot is slow.** The dispatcher auto-loads the `default` preset,
> which batch-builds every renderer ESM + typeEntity layer (offloaded to
> the nix builder). Cached on subsequent boots.

Extra args forward to the backend, e.g.:

```bash
./start_demo.sh ~/fxtr-demo-workdir --allow-llm-apis   # bridge LLM-API authorizers
./start_demo.sh ~/fxtr-demo-workdir --no-frontend      # headless / debug-UI only
```

`--allow-llm-apis` bridges host-side authorizers for `api.anthropic.com`
/ `api.openai.com` (whichever of `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`
is set). Without it, the agent has the HTTPS-passthrough authorizer + the
sandbox spawner but no LLM access from sandboxed code. The workdir's two
haiku services are the worked examples that consume the anthropic
authorizer — `haiku_service_wasm` (python-only sandbox, raw httpx
through the tunnel) and `haiku_service_oci` (an OCI container using the
real `anthropic` SDK); see the agent README's "Talk to the network"
section.

> **Container spawns need a container runtime.** `haiku_service_oci`
> (and any other `NixContainerServiceDefn` spawn) requires a working
> container runtime on the backend host (Apple Container / docker) plus
> a builder for `x86_64-linux` images. On Apple Silicon the images run
> under Rosetta; on macOS < 15.5 pin the guest kernel to ≤ 6.1 (see
> `projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage7_nix_container.md`).
> The wasm services have no such requirement.

### 5. Launch Claude Code

In a second terminal, point the plugin at your **scaffolded workdir**
(not the in-repo template):

```bash
cd ~/fxtr-demo-workdir
cp .fxtr_workstation.toml.example .fxtr_workstation.toml
# (no edits needed unless you've changed the backend's port)
```

Then from anywhere:

```bash
claude --dangerously-load-development-channels plugin:fxtr_workstation@fxtr-workstation
```

(See
[`projects/fxtr_workstation_agent_plugin/README.md`](../fxtr_workstation_agent_plugin/README.md)
for the `--plugin-dir`-flavoured token and a couple of plugin
troubleshooting notes.)

In the Claude Code session:

> Use `set_working_dir` to point at your scaffolded workdir (e.g.
> `~/fxtr-demo-workdir`), then `connect_workstation` and
> `open_session`. After that, run a short script that calls
> `present_to_user({"hello": "from the agent"})`.

### 6. Open the agent panel in the frontend

In the browser at `http://localhost:3470`:

1. Open the workstation shell's sidebar (left).
2. Find `agent_mcp:<session_id>` under services (the session UUID
   is the one the agent's `open_session` created — it's printed
   in the agent's chat output).
3. Open it as a panel. The agent-plugin renderer subscribes and
   the live log starts streaming.
4. Send a message from the text-box at the bottom; it lands in
   the agent's `workstation_inbox` callback. The agent can read
   it via `retrieve_callback_notification`.

## Self-contained driver (no agent / MCP)

To exercise the workdir's services and *look at* their renderers without attaching
an agent, the backend mounts a **`demo-helper`** service (host-side, under that
external name) with a browser-driven renderer — the **auto-driver**. Launch with
`--auto-workdir` (or any scaffolded `--workdir`), open the frontend, and in the
workstation shell's sidebar open the `demo-helper` service as a panel. Its UI is a
picker of **scenarios**:

- **Delayed echo** — spawn the echo service, echo values back after a delay.
- **Simple workflow** / **Streaming workflow** — spawn a scheduler + task, make demo
  input notes, run `run_batched`; the final `SimpleRunResult` returns in the panel.
- **Streaming vivid-description** (LLM, heavy) / **LLM ask** — need
  `--allow-llm-apis` + `ANTHROPIC_API_KEY`.

Each scenario stands in for "ask an agent to spawn X and run Y": the helper does the
host-side spawn+connect plumbing and entity-minting a browser can't, and the driver
**presents every spawn/invoke result as a clickable link** (the agent's
`present_to_user` affordance). Click a returned scheduler ref to open *its* panel and
watch the live run grid — the driver itself does no channel rendering; only the final
invoke result comes back to it. LLM scenarios error in the panel if the backend wasn't
launched with `--allow-llm-apis`.

## Teardown

- `close_session()` from the agent (or just disconnect).
- Ctrl-C the `./start_demo.sh` process — both halves shut down.
- `rm -rf .data/` if you want to wipe the persisted event log.
  Otherwise the next run picks up where the last one left off
  (that's the whole point of the persistence cap).

## Known limitations

- **MCP-supervisor-cycle goes silent.** If the MCP supervisor
  reconnects (after a workstation restart, or the agent's
  `close_session` + reopen), it registers a fresh
  `agent_mcp:<session_id>` service with a fresh subscriber hub;
  subscriptions against the previous cycle's service die with
  it. The panel's open subscription goes silent — close the
  panel and reopen it against the new `agent_mcp:<session_id>`
  service to recover. (This is the supervisor-cycle case;
  plain frontend re-attach to a live session needs no
  re-subscribe — the workstation-side subscription survives.)
  A future stage may add session-lifecycle-driven re-subscribe.
- **Backend services are minimal.** The demo intentionally
  ships the sandbox spawner + HTTPS authorizer + persistence cap +
  report-builder + the `demo-helper` driver. The agent does
  whatever it wants via the spawner — including spawning the
  in-workdir `delayed_echo` service when it wants an exercising
  round-trip. If you want richer *pre-registered* services (à la
  `examples/basic_test_demo`'s callback-tester + pre-mounted
  delayed-echo + pre-mounted demo-note), they can be added back
  later.
