# Phase 1: basic sandboxed script execution from MCP

## Goal

Wire `mcp_plugin_spike` to `fxtr_sandboxing` so that an agent can:

1. Set a working directory (a folder containing `preamble.py` and a
   `packages/` subdir) once per session.
2. Submit a Python script string. The MCP server runs it inside a fresh
   eryx WASM sandbox, with the preamble exec'd first and the script
   wrapped to support top-level `await`. The script can `from
   _fxtr_mcp_builtin import declare_value, schedule_message`.
3. After the sandbox completes, the MCP server applies any
   `schedule_message` side effects (using its existing channel-based
   delivery) and prints declared values back to the agent.

This is a thin slice of the eventual workstation flow described in
`projects/fxtr_workstation/plans/phase0/sketch.md`. We deliberately do not
build the workstation yet; the design just leaves clean seams where the
workstation will plug in (in particular, an `McpHostProto` host service
ref bridged into the sandbox).

## Architecture overview

Three layers:

```
mcp_plugin_spike (MCP tools, builtin stub, host service impl)
   └── uses ──>
fxtr_sandboxing (direct launcher: long-lived factory + per-spawn ServiceRef[SandboxConnectProto])
   └── uses ──>
fxtr_sandbox_rpc_adapter (existing; runs guest entry-point as a service)
```

### Direct launcher (in `fxtr_sandboxing`)

A new module — call it `direct_launcher.py` — that gives callers
low-level control over WASM sandbox spawning, mirroring the shape of
`create_sandbox_environment` + `SandboxSpawnerProto.spawn` but **without
the spawner indirection** and without the bundle-prep flow. "Direct"
because the caller is talking to the launcher directly, not through a
spawner ref.

API shape (mirrors `create_sandbox_environment` so scope reasoning
carries over):

```python
@asynccontextmanager
async def create_direct_sandbox_environment(
    *,
    cache_dir: Path,
    factory_packages: list[Path] = [],   # wheels baked into the snapshot
    factory_imports: list[str] = [],     # modules pre-imported into the snapshot
) -> AsyncIterator[DirectSandboxEnvironment]: ...

class DirectSandboxEnvironment:
    @asynccontextmanager
    async def open_sandbox(
        self,
        *,
        entry_point: str,                       # "module:fn"
        volumes: list[tuple[Path, str, bool]],  # extra read-only dirs
    ) -> AsyncIterator[ServiceRef[SandboxConnectProto[FxtrServiceData, UnknownService]]]:
        ...
```

Lifetime nesting (matches existing convention; see CODE_OVERVIEW.md ›
"Lifetime nesting"):

```
caller scope
  └─ create_direct_sandbox_environment (env_tg + exit_stack + factory)
       └─ open_sandbox call_scope
            └─ _spawn_runner_wasm task on env_tg
                 └─ open_rpc_scope conn to one guest process
                      └─ guest's GuestMain mounted on guest_host
            yields ServiceRef[SandboxConnectProto[...]] bridged into call_scope
```

The yielded ref is the same shape `SandboxSpawnerProto.spawn` returns,
so callers `replace_service_type(SandboxConnectProto[MyConfig, MyProto])`
+ `resolve()` + `connect(my_config)` exactly the same way. The
`open_sandbox` async context manager owns the sandbox lifetime; on
exit, the chain-bridges drop and the sandbox process tears down.

Internally:

- `create_direct_sandbox_environment` builds **one** `eryx.SandboxFactory`
  for the lifetime of the env, with `factory_packages` + `factory_imports`.
  This is the ~3 s build that the MCP server pays once at startup, in
  exchange for ~10–20 ms `create_sandbox` per call.
- `open_sandbox`: `factory.create_sandbox(callbacks=..., volumes=...)`,
  sets up the JSON callback channel, runs `_runner.py` (existing) which
  calls `fxtr_sandbox_rpc_adapter.main(ctx, entry_point=...)`. The
  adapter mounts a `_GuestMainImpl` exposing
  `SandboxConnectProto[FxtrServiceData, UnknownService]`. We bridge
  that ref back into the caller's scope and yield it.
- No `TaskBundle`, no `SandboxSpawnerImpl`, no transport server (phase 1
  doesn't need network from the sandbox).
- Local source packages **never** go through `factory.packages`
  (those would force a factory rebuild per change). They're always
  mounted via `volumes`. The runner adds those mount points to
  `sys.path` before calling adapter.main. (Hybrid: stable wheels baked
  + pre-imported into the snapshot, dynamic local packages mounted
  fresh per spawn.)

#### Reuse vs new code

Most of `runner.py` already does what we need (`eryx.SandboxFactory`,
the JSON callback bridge, `_runner.py` injection). Plan: factor out a
helper from `runner.py` that takes `(factory, volumes, entry_point)` →
`TaskSession` and skips the bundle-driven wheel/source-extraction. The
existing bundle-driven `run()` and the new `direct_launcher` both
delegate to this helper. Keep `_runner.py` unchanged (it reads
`_ENTRY_POINT` from globals already).

The bridge from `TaskSession` to `ServiceRef[SandboxConnectProto[...]]`
(`JSONObjectStream` + `wrap_json_stream` + `open_rpc_scope` +
chain-bridge) is the same dance `SandboxSpawnerImpl._spawn_runner_wasm`
does today — we extract that too into a helper both call sites use.

### Stub package + builtin module (in `mcp_plugin_spike`)

A small Python package shipped as part of the plugin, mounted into the
sandbox as a volume. Two responsibilities:

1. **Entry-point function** the direct launcher calls. Standard shape:

   ```python
   # _fxtr_mcp_runner/__init__.py
   async def run(config: McpRunnerConfig) -> ServiceRef[McpRunnerProto]: ...
   ```

   `McpRunnerConfig` is a plain dataclass carrying:
   - `preamble_src: str`
   - `script_src: str`
   - `host: ServiceRef[McpHostProto]` (see below — exposed for future
     workstation hooks; phase 1 has it but only uses it for `host_info`).

   `McpRunnerProto` exposes one method:

   ```python
   async def run() -> McpRunResult
   ```

   where `McpRunResult` is `{"declared": dict[str, JSON], "scheduled":
   list[ScheduledMessage]}`.

2. **`_fxtr_mcp_builtin` injectable module.** *Not* shipped as a real
   importable package on `sys.path`. Instead, the entry point
   constructs a fresh `types.ModuleType("_fxtr_mcp_builtin")`,
   populates its `declare_value` and `schedule_message` callables (each
   a closure over a per-run state dict), inserts it into `sys.modules`
   right before exec'ing the script, and removes it after.
   - `declare_value(key: str, value: JSON)`: appends to per-run dict.
     Errors on non-JSON.
   - `schedule_message(delay_seconds: float, message: str)`: appends a
     `ScheduledMessage(delay_seconds, message)` to a per-run list. No
     side effect — the host applies it after the sandbox tears down.
   - Each `run()` invocation gets its own state map (no cross-call leak).

#### Script wrapping for top-level await

`run()`:

1. Exec the preamble into a fresh globals dict (`prelude_globals`)
   using normal `compile(preamble_src, "preamble.py", "exec")` + `exec`.
2. Compile the script with `compile(src, "<script>", "exec",
   flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)`. The standard incantation
   (used by IPython) is to `exec` the resulting code object; if the
   module ends up being a coroutine factory, CPython exposes it as the
   eval result — we'll verify the exact mechanics. Fallback if needed:
   always wrap inside `async def __script__(): <body>` textually and
   `await __script__()`.
3. Install + remove `_fxtr_mcp_builtin` in `sys.modules` around the
   exec.
4. Return the collected `(declared, scheduled)`.

### `McpHostProto`

Defined in the stub package (since both host and guest need it).
Phase 1 has one method:

```python
@service_protocol
class McpHostProto(Protocol):
    async def host_info(self) -> str: ...
```

Returns some descriptive string ("mcp_plugin_spike vX.Y, working_dir=…").
Useful for: smoke-testing the bidirectional ref bridge works, and
giving us a clean seam to extend (the workstation's
`copy_entity` / `get_entity` / etc. land here).

The MCP server provides a `_McpHostImpl: TypedServiceImpl[McpHostProto]`,
mounts it on the env's host scope, and threads the ref through
`McpRunnerConfig` when calling `connect`.

### MCP server changes

Add two tools to the existing `ChannelFastMCP` server:

- `set_working_dir(path: str)`: Stores the path on the server's
  lifespan state. Validates that the path exists and contains
  `preamble.py` and `packages/`.
- `run_script(script: str)`: Reads `preamble.py`, opens a sandbox via
  `direct_launcher.open_sandbox(entry_point="_fxtr_mcp_runner:run",
  volumes=[stub_volume, packages_volume])`, calls `connect(McpRunnerConfig(...))`,
  invokes `run()`, applies any `schedule_message` results via the
  existing task-group machinery, and returns the `declared` dict as
  the tool result text.

The server's `lifespan` builds the long-lived
`DirectSandboxEnvironment` once at startup (with `factory_packages=[fxtr2-portable
wheel + stub if we choose to bake it]` and `factory_imports=["fxtr2_portable",
"fxtr_sandbox_rpc_adapter", ...]`) and tears it down on shutdown.

### Working dir layout (no PEP 723)

```
<working_dir>/
  preamble.py
  packages/
    pkg_a/                 # importable as `pkg_a`
    pkg_b/
```

No metadata, no parsing. Server mounts the entire `packages/` dir as a
single volume (e.g. `(packages_dir, "/local-pkgs", True)`); the runner
inserts `/local-pkgs` into `sys.path` so every subdirectory is
importable. Packages disappear/appear on disk → next spawn picks them
up automatically.

### Where fxtr2-portable comes from

The factory needs the fxtr2-portable wheel on disk to bake in. For
phase 1: env-var-or-config path, document setup. We can wire in the
proper `wheels.py::fetch_wheels`-based fetcher once the rest is
validated.

## Implementation steps

1. **Direct launcher in `fxtr_sandboxing`.** ✅
   - [x] Factor `runner.py`: extract `build_site_packages_for_bundle`
     + new `run_in_factory(factory, *, entry_point, volumes,
     sys_path_additions, transport_server)`. Old `run(bundle, ...)`
     keeps its signature; internally builds a per-spawn factory and
     delegates to `run_in_factory`.
   - [x] Add `direct_launcher.py` with
     `create_direct_sandbox_environment(*, factory)` and
     `DirectSandboxEnvironment.open_sandbox(*, entry_point, volumes,
     sys_path_additions)`. Yields
     `ServiceRef[SandboxConnectProto[FxtrServiceData, UnknownService]]`
     bridged into a per-call mountable FxtrScope. CM exit fully tears
     down the sandbox via per-sandbox task group.
   - [x] Unit test: `tests/test_direct_launcher.py` — three tests
     (greet/add/teardown) reusing the rpc_task example through the
     direct launcher path.
   - [x] Update CODE_OVERVIEW.md (runner.py entry, new
     direct_launcher.py entry, new test entry).
   - Notes from implementation:
     - Per-call FxtrScope must be `mountable=True` (otherwise
       `bridge_from_scope` raises). Worth flagging in any future docs
       on the launcher API.
     - Skipped factoring `spawner.py`'s rpc-bridge dance — the dance is
       small enough (3 lines) and the spawner's runner-task pattern
       differs (env_tg + teardown_event for cross-call lifetime) from
       the direct launcher's per-CM lifetime. Duplication is minor;
       extracting a helper would obscure both.

2. **Stub package + builtin in `mcp_plugin_spike`.** ✅
   - [x] New `_fxtr_mcp_runner` package at
     `plugin/scripts/_fxtr_mcp_runner/` (sibling of `scheduler_mcp/`).
     Four modules:
     - `script_runner.py`: pure-stdlib primitive.
       `run_preamble_and_script(preamble_src, script_src, *,
       builtins: Mapping[str, Any]) -> None`. Constructs a fresh
       `_fxtr_mcp_builtin` module from `builtins` (caller-supplied —
       no MCP-specific names baked in), installs it at
       `sys.modules`, exec's preamble + script with TLA support
       (`PyCF_ALLOW_TOP_LEVEL_AWAIT` + `eval` on `CO_COROUTINE`-flagged
       code → no textual wrapping → tracebacks stay at correct line
       numbers), removes from `sys.modules` in `finally`.
     - `protocols.py`: `McpRunnerProto`, `McpHostProto`,
       `McpRunnerConfig` plain frozen dataclass (carries
       `preamble_src`, `script_src`, `host_ref:
       ServiceRef[McpHostProto]`). Fresh `MCP_RUNNER_NS` UUID.
     - `entry.py`: `run(config) -> ServiceRef[McpRunnerProto]` mounts
       a `_GuestImpl` on the active scope. `_GuestImpl.run()` resolves
       `host_ref`, defines `declare_value` / `schedule_message`
       (validation closures over per-call containers) +
       `host_info` (bound method on the resolved host proxy — RPC
       bridge invisible to the user script), passes them as
       `builtins=` to `run_preamble_and_script`, packs the result as
       `{"declared": ..., "scheduled": [{...}]}`. All MCP wire-format
       conventions live here (not in script_runner).
     - `__init__.py`: docstring describing the package layout.
   - [x] Updated `plugin/scripts/pyproject.toml`: adds
     `_fxtr_mcp_runner` to the wheel's packages list, dev deps, pytest
     config. (Test framework switched to `pytest-anyio` later in
     step 3 — see step-3 notes.)
   - [x] Unit tests (no sandbox) at `plugin/scripts/tests/test_
     script_runner.py` — 8 tests of the primitive only (caller
     builtins reach script, preamble globals, top-level await,
     plain-exec branch, sys.modules installed/removed-on-error,
     fresh-module per call, traceback filenames). MCP-specific
     validation (declare_value JSON gate, schedule_message types)
     covered by integration tests in step 3.
   - Notes from implementation:
     - The TLA pattern (`compile(..., flags=PyCF_ALLOW_TOP_LEVEL_AWAIT)`
       + check `co_flags & inspect.CO_COROUTINE` + `await eval(code,
       globals)`) works on host CPython 3.14 and is confirmed to
       work under WASM Python via step 3's integration tests.
     - Initial design had script_runner own `declare_value`/
       `schedule_message` validation + a separate `extra_builtins`
       hook. Refactored to make script_runner pure-stdlib + MCP-agnostic
       — entry.py owns all MCP semantics. Cleaner separation;
       script_runner shrank to ~30 lines.

3. **Wire up MCP tools.** ✅
   Done in two chunks plus a refactor.

   **Chunk 1 — factory bootstrap + integration:**
   - [x] New `spikes/mcp_plugin_spike/sandbox_runtime/` task: minimal
     pyproject + placeholder entry-point, depends on
     `fxtr-sandbox-rpc-adapter[fxtr2-portable]`. Used purely for its
     dep closure; its declared entry-point is never invoked at runtime
     (we always go through `_fxtr_mcp_runner.entry:run` via volume).
   - [x] `scheduler_mcp/sandbox_factory.py`: `bootstrap_factory()`
     drives `prepare()` → `build_site_packages_for_bundle` →
     `eryx.SandboxFactory(site_packages=...)`. `build_runner_volumes()`
     returns the standard `(volumes, sys_path_additions)` for the
     `_fxtr_mcp_runner` mount + optional user `packages/` mount.
   - [x] `scheduler_mcp/host_impl.py`: `McpHostImpl` (one method,
     `host_info`).
   - [x] `plugin/scripts/pyproject.toml`: adds path deps on `fxtr2`
     (from `v2/`) and `fxtr-sandboxing` (from
     `projects/fxtr_sandboxing/`); bumps `requires-python` to 3.14.
   - [x] Integration tests in `tests/test_sandbox_integration.py`:
     `test_run_volume_mounted_runner_end_to_end` (real preamble +
     script with declare_value + schedule_message);
     `test_host_info_round_trips_through_user_script` (script calls
     `await host_info()` — full RPC round-trip back to host's
     McpHostImpl); `test_declare_value_validation_surfaces_to_host`
     (validation errors propagate across the bridge). All passing.

   **Chunk 2 — MCP server tools:**
   - [x] `scheduler_mcp/server.py` `lifespan`: opens task group →
     `host_scope` (FxtrScope) → mounts `McpHostImpl` → bootstraps
     factory → opens `DirectSandboxEnvironment`. Yields `AppContext`
     with all of these.
   - [x] `set_working_dir(path)` tool: validates `preamble.py`
     presence, optionally surfaces the `packages/` listing.
   - [x] `run_script(script)` tool: reads preamble, builds volumes,
     opens sandbox, drives `runner.run()`, replays scheduled
     messages via the existing channel task group, returns
     JSON-encoded `{"declared": ..., "scheduled_count": ...}`.
   - [x] `tests/test_server_tools.py`: 6 tests using a hand-rolled
     fake Context — set_working_dir validation, run_script returns
     declared values, scheduled-message delivery via the fake
     session's write stream, user `packages/` mount, missing
     working_dir error path.

   **Refactor — pytest-anyio + scope discipline:**
   - [x] Switched test framework from `pytest-asyncio` to
     `pytest-anyio`. Reason: `pytest-asyncio` runs each fixture phase
     as its own `asyncio.Task`, which trips `anyio.create_task_group()`'s
     cross-task cancel-scope guard when a long-lived task group is
     opened in fixture setup and torn down in fixture teardown — exactly
     what `lifespan()` does. `pytest-anyio` drives setup/test/teardown
     in one task per test, matching how the lifespan runs in production
     (FastMCP drives it from a single task).
   - [x] Adopted `v2/notes/scope_discipline.md` patterns:
     - `host_scope` is now `FxtrScope(isolated=True,
       include=[GLOBAL_SCOPE], mountable=True, name="mcp_lifespan")`
       — explicit parent, no implicit inheritance.
     - `AppContext` carries the `host_scope` alongside `host_ref`
       (per the "pass and store scopes alongside references" rule).
     - `lifespan` wraps `bootstrap_factory()` in `with
       host_scope.as_active_scope():` (the user-of-fxtr `as_active`
       carve-out — `prepare()` requires an active scope).
     - `run_script` wraps `open_sandbox()` in `with
       app_ctx.host_scope.as_active_scope():` so the per-call FxtrScope
       inside `open_sandbox` inherits `host_scope` as parent →
       `host_ref` is encodable across the connect-config wire.
     - Membership rationales documented in inline comments
       (where each ref is loadable, why each scope inheritance is OK).
   - **17 tests pass total** (script_runner 8 + sandbox_integration 3
     + server_tools 6).

   **Notes from implementation:**
   - The pytest-asyncio → pytest-anyio switch was the one real
     surprise; everything else was mechanical glue.
   - `direct_launcher.py` (in fxtr_sandboxing) wasn't refactored to
     scope discipline. It's pre-existing and works correctly given
     how callers use it; if we ever apply scope discipline broadly to
     fxtr_sandboxing, that refactor is independent of this plan.

4. **End-to-end smoke test.** ✅ (programmatic) / ⏳ (manual Claude session)
   - [x] Sample working dir at
     `spikes/mcp_plugin_spike/sample_workdir/` — `preamble.py`
     imports from `packages/demo_pkg/` and binds `ANSWER = 42`.
     README walks through four sample scripts (round-trip, scheduled
     message, host_info call, local-package use).
   - [x] Subprocess MCP test at
     `plugin/scripts/tests/test_mcp_subprocess.py` — spawns
     `scheduler-mcp` via `uv run`, speaks MCP over stdio via
     `mcp.client.session.ClientSession`, exercises:
     - initialize handshake (covers lifespan + factory bootstrap)
     - `set_working_dir` against the sample workdir
     - `run_script` with a script that uses `declare_value`,
       `schedule_message`, `host_info`, AND imports `doubler` /
       `GREETING` from the local `demo_pkg` (preamble bindings
       reach the script)
     - validation surface: a script that violates `declare_value`'s
       JSON gate returns an error string instead of crashing the
       server.
   - [ ] Manual smoke test in a real Claude Code session (validates
     the user-facing flow + channel-notification routing through
     Claude Code itself). Driver: README at
     `spikes/mcp_plugin_spike/sample_workdir/README.md`.

5. **Doc pass.** ✅
   - [x] README.md updated: lists both facilities (channel scheduling
     + sandboxed execution), describes the new tools and the
     `_fxtr_mcp_builtin` surface, points at `sample_workdir/` as the
     manual test harness, links to `CODE_OVERVIEW.md` for design.
   - [x] New `spikes/mcp_plugin_spike/CODE_OVERVIEW.md` describes the
     end-to-end flow, lifespan resource ordering, scope-discipline
     application points, the host/guest-shared `_fxtr_mcp_runner`
     package, the bootstrap factory, package structure, and the
     pytest-anyio rationale.
   - [x] `fxtr_sandboxing/CODE_OVERVIEW.md` updated for the direct
     launcher (during step 1).
   - [x] `sample_workdir/README.md` documents the four sample scripts
     and the manual smoke-test recipe.

## Open questions / things to revisit

- **Volume fan-out cost.** Phase 1 mounts a single `packages/` parent
  volume so this isn't an issue here; flagged for when individual
  per-package mounts come back on the table.
- **Scope discipline in `fxtr_sandboxing`.** The MCP-server side
  follows scope discipline; `direct_launcher.py` and `runner.py`
  predate it. Their callers handle scopes correctly via active-scope
  inheritance, but applying scope discipline to those modules would
  make their guarantees more legible.

## Resolved

- **Top-level await under WASM Python.** The `compile(...,
  flags=PyCF_ALLOW_TOP_LEVEL_AWAIT)` + `eval` pattern works inside
  eryx — confirmed by step 3's integration tests.
- **Where `_fxtr_mcp_runner` lives.** Sibling of `scheduler_mcp/`
  under `plugin/scripts/`. Host imports it as a regular package;
  sandbox mounts the parent (`plugin/scripts/`) at `/sandbox-stub`
  and adds it to `sys.path`. Other contents of `plugin/scripts/` are
  reachable but never imported by the sandbox.

## Last step

Update CODE_OVERVIEW.md (in fxtr_sandboxing) to describe the direct
launcher. There's no CODE_OVERVIEW yet in mcp_plugin_spike — we may
want to add one once this lands.
