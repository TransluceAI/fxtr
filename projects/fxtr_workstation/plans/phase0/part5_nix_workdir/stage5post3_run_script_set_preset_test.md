# Stage 5 post-3 — end-to-end `run_script` `set_preset` test

The agent-driven counterpart to Stage 5's *direct* dispatcher test
(`test_nix_dispatch_preset.py`), and the dispatcher analog of Stage 3's
`run_script`-driven spawn test (`test_in_memory_e2e_nix_spawn`). It was
scoped into Stage 5 (`stage5_ui_presets.md` decision #4 deferred it to
the demo stage), but since it rides on `basic_test_demo` — which already
ships `fxtr.workstation.uiPresets.demo_preset` and the nix test
infra — it has **no dependency on the Stage 6 big-demo work** and pulls
forward here. (It also doesn't depend on `stage6pre1`: `demo_preset`
keys only `demo_data`, a layer entity, not a builtin.)

## What it proves

The real agent path, all the way through the MCP → RPC → workstation
invoke stack:

1. The agent (in a `run_script`) looks up the dispatcher service by
   name, `resolve`s it, `replace_service_type(NixDispatcherProto)`s the
   ref (the agent-side `set_preset`-only dup re-exported from
   `_fxtr_mcp_builtin`, shipped in Stage 5), and
   `schedule_invoke("p", dispatcher, lambda d: d.set_preset(
   current_commit(), "fxtr.workstation.uiPresets.demo_preset"))`.
2. The workstation's `NixConfigurableDispatchImpl` evaluates the
   table, builds the renderer ESM + `typeEntity` layer, validates,
   mints the `RendererEntity`, swaps the table, and broadcasts a
   reload.

Stage 5's direct test drove `set_preset` through an in-process proxy;
this drives it through the agent's real wire path, which the direct
test "deliberately did not cover."

## The missing piece: a dispatcher in the test workstation

`basic_test_demo/run_demo_workstation.py` mounts a spawner + a couple
of services but **no dispatcher** (it has no frontend). The MCP plugin's
`test_mcp_in_memory.py` builds its own in-process workstation fixtures;
`_running_workstation_with_nix_spawner` is the spawner one. We add a
sibling that also mounts the dispatcher.

### Fixture: `_running_workstation_with_nix_dispatcher`

Mirrors the spawner fixture (`prepare_workdir_repo` →
`WorkstationServer` with the external service bridged in), but:

- Opens `mount_nix_configurable_dispatch(repo=prepared.path)` — **no**
  `initial_commit`/`initial_attr`, so the dispatcher starts with an
  empty table; the agent's `set_preset` is what fills it.
- Bridges the yielded `mounted.service_ref` + `mounted.scope` in as an
  `ExternalServiceConfig` under a name the agent looks up (e.g.
  `"renderer-dispatcher"`).
- **Yields `mounted.impl` to the test** (alongside the workdir + head)
  so the assertions can inspect dispatcher-internal state directly —
  the same handle Stage 5's direct test asserts on. (The agent side of
  `NixDispatcherProto` is `set_preset`-only — no `resolve` — so the
  resolution assertions run against the in-process impl, exactly as in
  the direct test.)

### The test

`test_in_memory_e2e_nix_set_preset(tmp_path)`:

- Boot the fixture; connect the MCP; `open_session`.
- `run_script` that does the lookup → resolve → `replace_service_type`
  → `schedule_invoke(set_preset(current_commit(), demo_preset))` (with
  `wait_seconds` so the invoke result lands inline, like the spawn
  test). `current_commit()` returns the prepared workdir's clean
  committed HEAD (`.fxtr_workstation.toml` is gitignored).
- Assert the invoke succeeded (slot promoted to `invoke_result`, not
  `invoke_error`).
- Assert on the impl handle: `impl.current_ref == (head,
  "fxtr.workstation.uiPresets.demo_preset", None)`, and
  `impl.resolve(<DemoDataEntity type ref>, FULL_PANEL)` returns a
  `RendererResolution` whose `renderer` loads to non-empty ESM bytes
  (the frontend path — copy the `type_ref` construction from
  `test_nix_dispatch_preset.py`).

Slow + nix-gated (same `@pytest.mark.slow` + generous
`@pytest.mark.deadline` as the sibling nix tests — the `demo_data` host
venv build dominates a cold run).

## Implementation steps

- [x] Add `_running_workstation_with_nix_dispatcher` to
  `test_mcp_in_memory.py` (sibling of
  `_running_workstation_with_nix_spawner`), yielding the mounted dispatch
  handle (`mounted.impl` / `mounted.service_ref` / `mounted.scope`).
- [x] Add `test_in_memory_e2e_nix_set_preset` driving `set_preset`
  through `run_script` + asserting via the impl handle (`current_ref`)
  and a proxy `resolve` into the impl's scope. **Passes** (37s warm
  against the `virby-vm` x86_64-linux builder).
- [ ] (Optional, skipped) mount the same dispatcher in
  `basic_test_demo/run_demo_workstation.py` under `"renderer-dispatcher"`.
  Test-fixture coverage is the requirement and is met; left out to keep
  the change focused.
- [x] Update CODE_OVERVIEW.md (`fxtr_workstation_agent_plugin` — the new
  E2E test is described alongside the spawn / retrieve / direct-dispatch
  ones in the test inventory).

## Relevant code

- `projects/fxtr_workstation_agent_plugin/fxtr_workstation_mcp/tests/test_mcp_in_memory.py`
  — `_running_workstation_with_nix_spawner` + `test_in_memory_e2e_nix_spawn`
  are the templates to mirror.
- `projects/fxtr_workstation_agent_plugin/fxtr_workstation_mcp/tests/test_nix_dispatch_preset.py`
  — the direct dispatcher test; copy its `type_ref` setup + resolution
  assertions.
- `projects/fxtr_workstation/src/fxtr_workstation/dynamic_dispatch/nix_configurable_dispatch/service_impl.py`
  — `mount_nix_configurable_dispatch` + the `impl` surface
  (`current_ref`, `resolve`).
- `examples/.../basic_test_demo/agent_workdir/flake.nix` — declares
  `fxtr.workstation.uiPresets.demo_preset`.

## End-of-stage check

- `test_in_memory_e2e_nix_set_preset` passes: a `run_script`-driven
  `set_preset(current_commit(), demo_preset)` resolves + reloads, and
  the resulting table resolves the `demo_data` type to a real
  renderer.
- No change to the dispatcher library surface (Stage 5 shipped it all).
