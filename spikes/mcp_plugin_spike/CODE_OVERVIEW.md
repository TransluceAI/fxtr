# mcp_plugin_spike Code Overview

Prototype Claude Code plugin that bundles a Python MCP server with two
facilities:

1. **Channel-based scheduling.** A `schedule_message(delay_seconds,
   message)` tool delivers a delayed self-directed follow-up via the
   `notifications/claude/channel` JSON-RPC notification.
2. **Sandboxed Python script execution.** `set_working_dir(path)` +
   `run_script(script)` exec user-supplied Python inside an eryx WASM
   sandbox via `fxtr_sandboxing.direct_launcher`. Scripts can declare
   values back to the agent, queue scheduled messages from inside the
   sandbox, call back into a host service over fxtr RPC, and import
   from a `packages/` subdir of the working dir.

Status: phase-1 implementation per
`plans/phase1/1_basic_sandboxing.md`. The sandboxed-execution layer is
designed as a thin slice of the eventual workstation flow described
in `projects/fxtr_workstation/plans/phase0/sketch.md` — clean seams for
the workstation integration are already in place
(`McpHostProto`, the `host_ref` on `McpRunnerConfig`, etc.).

## Key concepts

### How a `run_script` call works end-to-end

```
agent → tools/call run_script(script)
   ↓
scheduler_mcp.server.run_script (host)
   ↓
   reads <working_dir>/preamble.py
   builds (volumes, sys_path_additions) for the sandbox
   enters host_scope.as_active_scope()    ← scope-discipline guard
   ↓
DirectSandboxEnvironment.open_sandbox(
   entry_point="_fxtr_mcp_runner.entry:run",
   volumes=[(plugin/scripts, /sandbox-stub),
            (<workdir>/packages, /local-pkgs)],
   sys_path_additions=["/sandbox-stub", "/local-pkgs"])
   ↓
   eryx.SandboxFactory.create_sandbox
   fxtr_sandbox_rpc_adapter.main runs in the WASM process,
   imports `_fxtr_mcp_runner.entry` from /sandbox-stub
   ↓
sandbox.connect(McpRunnerConfig(preamble_src, script_src, host_ref))
   _GuestImpl is mounted on the connect dispatch scope
   ↓
runner.run() (RPC call from host to sandbox)
   _GuestImpl.run() (in sandbox):
      resolves host_ref → host proxy
      defines declare_value, schedule_message, host_info closures
      run_preamble_and_script(preamble, script,
                              builtins={"declare_value": ...,
                                        "schedule_message": ...,
                                        "host_info": host.host_info})
        exec preamble into globals
        compile(script, flags=PyCF_ALLOW_TOP_LEVEL_AWAIT)
        if CO_COROUTINE: await eval(code, globals)
        else: exec(code, globals)
        sys.modules['_fxtr_mcp_builtin'] is the per-call module
        with the closures above
      returns {"declared": {...}, "scheduled": [{...}]}
   ↓
back on the host:
   for entry in scheduled:
       task_group.start_soon(_deliver_after, ctx.session, ...)
   return JSON dump of {"declared": ..., "scheduled_count": N}
   ↓
agent receives the tool result (and ~delay_seconds later, any
scheduled channel notifications fire)
```

### Lifespan resource ordering (in `scheduler_mcp.server.lifespan`)

```
anyio.create_task_group()                     ← deliveries outlive tools
  FxtrScope(isolated=True,                    ← host_scope; explicit parent
            include=[GLOBAL_SCOPE],             chain (scope discipline)
            mountable=True)
    host_scope.mount(McpHostImpl(...))        ← host_ref loadable here
    with host_scope.as_active_scope():        ← prepare() needs active
        bootstrap_factory(...)                  scope to store bundle
    create_direct_sandbox_environment(
        factory=factory)
        yield AppContext(task_group, env,
                         host_scope, host_ref)
```

### Scope discipline

Per `v2/notes/scope_discipline.md`. Key application points:

- `host_scope` is `isolated=True` with explicit `include=[GLOBAL_SCOPE]`
  — no implicit parent inheritance from `ACTIVE_SCOPE`.
- `host_ref` is stored on `AppContext` *alongside* `host_scope` (the
  ref is loadable only there).
- `lifespan` enters `host_scope.as_active_scope()` around
  `bootstrap_factory()` because `prepare()` uses the active scope to
  store the bundle entity (the user-of-fxtr `as_active` carve-out).
- `run_script` enters `host_scope.as_active_scope()` around
  `open_sandbox()` so the per-call FxtrScope inside
  `open_sandbox` inherits `host_scope` as a parent — required so
  `host_ref` is encodable into the sandbox's connect-config wire.

This avoids relying on contextvars surviving FastMCP's request-handling
path.

### `_fxtr_mcp_runner` package — host/guest shared, mounted via volume

Lives at `plugin/scripts/_fxtr_mcp_runner/`. Imported on both sides:

- **Host:** installed as part of `scheduler-mcp`'s wheel; imports
  `McpRunnerConfig`, `McpRunnerProto`, `McpHostProto` for typing the
  connect call.
- **Sandbox:** the parent `plugin/scripts/` is mounted at
  `/sandbox-stub` and added to `sys.path`; `import _fxtr_mcp_runner`
  resolves to the same files. The fxtr `define_type` IDs match
  because they share UUIDs + names.

Three modules:

- **`script_runner.py`** — pure-stdlib primitive. `run_preamble_and_
  script(preamble_src, script_src, *, builtins)` constructs a fresh
  `_fxtr_mcp_builtin` module from the supplied mapping, installs it
  in `sys.modules`, exec's preamble + script (top-level await via
  `PyCF_ALLOW_TOP_LEVEL_AWAIT` + `eval` on `CO_COROUTINE`-flagged
  code), removes the module on exit. No MCP semantics.
- **`protocols.py`** — wire-format types: `McpRunnerProto`,
  `McpHostProto`, `McpRunnerConfig` (plain frozen dataclass holding
  `preamble_src`, `script_src`, `host_ref: ServiceRef[McpHostProto]`).
- **`entry.py`** — the `module:fn` entry point passed to
  `direct_launcher.open_sandbox`. `_GuestImpl.run()` resolves
  `host_ref`, defines `declare_value`/`schedule_message`/`host_info`
  closures with validation and per-call collection containers,
  passes them as `builtins=` to `run_preamble_and_script`, packs the
  result as `{"declared": ..., "scheduled": [{...}]}`. All MCP
  conventions live here.

### Bootstrap factory

`scheduler_mcp/sandbox_factory.py::bootstrap_factory()` runs
`fxtr_sandboxing.prepare()` against `spikes/mcp_plugin_spike/sandbox_runtime/`
to assemble the dep closure (`fxtr-sandbox-rpc-adapter[fxtr2-portable]` +
transitives), materializes that into a site-packages dir via
`build_site_packages_for_bundle`, and constructs an
`eryx.SandboxFactory(site_packages=...)`. ~3 s, paid once at MCP
startup. The `sandbox_runtime` task's declared entry-point is a
placeholder that is never invoked — its only purpose is the dep
closure.

`build_runner_volumes()` returns the standard
`(volumes, sys_path_additions)` for one `open_sandbox` call: the
`/sandbox-stub` mount (always) + the `/local-pkgs` mount (only if the
working dir has a `packages/` subdir).

## Package structure

```
spikes/mcp_plugin_spike/
  README.md                       end-user docs (loading the plugin, tools)
  CODE_OVERVIEW.md                this file
  .claude-plugin/
    marketplace.json              local marketplace catalog
  sandbox_runtime/                bootstrap stub task (dep closure source)
    pyproject.toml                fxtr-sandbox-rpc-adapter[fxtr2-portable];
                                  uv sources point at projects/fxtr_sandboxing/
    mcp_sandbox_runtime/__init__.py   placeholder (never invoked)
  sample_workdir/                 minimal <working_dir> for tests + manual
    README.md                     four sample scripts + manual smoke walkthrough
    preamble.py                   imports demo_pkg, binds ANSWER = 42
    packages/demo_pkg/__init__.py GREETING constant + doubler() function
  plans/
    phase1/1_basic_sandboxing.md  design + checked-off implementation steps
  plugin/                         the actual Claude Code plugin
    .claude-plugin/plugin.json    manifest, declares the channel
    .mcp.json                     stdio launcher (uv run --directory ...)
    skills/delayed-followup/SKILL.md   custom instructions
    scripts/                      Python packages
      pyproject.toml              scheduler-mcp; deps on mcp, fxtr2,
                                  fxtr-sandboxing (path); dev group has
                                  pytest + pytest-anyio (NOT pytest-asyncio
                                  — see plan for why)
      scheduler_mcp/              host-side
        __init__.py
        server.py                 ChannelFastMCP + lifespan + tools.
                                  Lifespan: anyio task group → host_scope
                                  (FxtrScope, isolated) → mount
                                  McpHostImpl → bootstrap_factory →
                                  DirectSandboxEnvironment. Tools:
                                  schedule_message (original spike),
                                  set_working_dir, run_script.
        host_impl.py              McpHostImpl (TypedServiceImpl[McpHostProto]).
                                  Currently exposes one method,
                                  host_info() -> str.
        sandbox_factory.py        bootstrap_factory + build_runner_volumes.
      _fxtr_mcp_runner/           host/guest shared; mounted via volume
        __init__.py               package docstring
        script_runner.py          pure-stdlib exec primitive (see above)
        protocols.py              wire-format types (see above)
        entry.py                  fxtr.service entry-point + _GuestImpl
      tests/
        __init__.py
        test_script_runner.py     8 unit tests of the exec primitive
        test_sandbox_integration.py    3 integration tests:
                                       end-to-end sandbox + script,
                                       host_info round-trip,
                                       declare_value validation propagation
        test_server_tools.py      6 tests of set_working_dir + run_script
                                  via lifespan + hand-rolled fake Context
        test_mcp_subprocess.py    1 subprocess test: spawns scheduler-mcp,
                                  speaks MCP over stdio via mcp.client
```

## Testing

`pytest-anyio` (not `pytest-asyncio`). Tests need
`pytestmark = pytest.mark.anyio` at module level.

Why: `pytest-asyncio` runs each fixture phase as a separate
`asyncio.Task`; `anyio.create_task_group()` (used in `lifespan`) is
strict about task affinity on its cancel scope and rejects exit from
a different task than entry. `pytest-anyio` drives setup → test →
teardown in one task per test, matching how the lifespan runs in
production (FastMCP drives it from a single task).

Run:

```bash
cd plugin/scripts
uv run pytest tests/ -v
```

18 tests, ~70 s total (factory bootstrap + sandbox spawn dominate).

## Loading into Claude Code

See `README.md`. Two paths: ad-hoc via `--plugin-dir ./plugin` or
install from the local marketplace. Both need
`--dangerously-load-development-channels` for the channel.

Once loaded, the agent has three tools: `schedule_message`,
`set_working_dir`, `run_script`.
