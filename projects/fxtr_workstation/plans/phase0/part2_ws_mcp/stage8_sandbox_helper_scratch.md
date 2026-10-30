# Stage 8 scratch — sandbox-bundle helper

## Goal restated

Today the agent can `lookup_service_by_name("some-spawner")` to reach
an externally-mounted `SandboxSpawnerProto`, and `schedule_invoke` to
call `spawn(bundle)` / `connect(config)` on it. The piece missing is:
how does the agent *construct* the `TaskBundle` entity? Currently
that's a host-side `fxtr_sandboxing.prepare.prepare(task_dir)` call
that requires uv, subprocess, file IO — none of which the sandbox
has.

Plan: expose one new sandbox builtin (`prepare_package_bundle`) that
RPCs back to the host; the host runs (a safer version of) `prepare()`
against `<workdir>/packages/<name>/` and returns the `FxtrID` of the
resulting `TaskBundle`. The returned ID slots cleanly into the
existing entity-arg pipeline — the agent can pass it as a
`schedule_invoke` arg or `export_entity` it.

## Two unrelated changes that ride along

The plan touches two areas:

### A. `fxtr_sandboxing.prepare` overhaul

1. Refactor `prepare()` to `async def`, using `anyio.run_process` in
   place of `subprocess.run`. Touch the existing callers
   (`sandbox_factory.bootstrap_factory`, the e2e tests) to `await
   prepare(...)`.
2. Replace `_build_source_package`'s wheel-build with a static
   file-walk. Read the local package's pyproject.toml; figure out
   which directories are the package source; copy text files
   directly into `PySourcePackage.source_files`. Never invoke a
   build backend (which would be running arbitrary code from the
   agent's `[build-system]`).
3. New optional param `allowed_local_pkg_dirs: list[Path] | None`.
   When set, every resolved local-package source dir must be under
   one of these roots; otherwise raise. The MCP passes a hardcoded
   list (workdir + the three workspace deps fxtr2-portable /
   fxtr-sandbox-rpc-adapter / fxtr-sandbox-types) so the agent
   can't bundle arbitrary host paths.

### B. New MCP host-impl method + sandbox builtin

1. `McpHostProto.prepare_package_bundle(package_name: str) -> FxtrID`
   — new RPC; host-side impl runs `prepare()` against
   `<workdir>/packages/<package_name>/` and returns the FxtrID of
   the resulting TaskBundle.
2. Sandbox-side closure `prepare_package_bundle(name)`: trivial
   passthrough to `host.prepare_package_bundle(name)`.

## File-walk discovery rules

Read the local package's pyproject.toml; resolve which directories
constitute the source. Resolution order (first match wins):

1. `[tool.fxtr.sandboxing] packages = [...]` — our explicit override.
   Each entry is a path relative to the pyproject's dir; the entry's
   basename is the import-name in site-packages
   (matches hatchling's `packages = [...]` convention).
2. `[tool.hatch.build.targets.wheel] packages = [...]` — reuse hatch's
   config so the agent doesn't have to write two blocks for
   hatchling-built packages.
3. Auto-discover: try `<project_root>/<normalized_name>/`, then
   `<project_root>/src/<normalized_name>/`. `normalized_name` =
   `project.name.replace("-", "_").lower()`. Error if neither
   exists.

The walker iterates each resolved package dir recursively, builds
`{relpath: contents}` where `relpath` starts with the basename of
the package dir (so `src/my_svc/foo.py` becomes
`my_svc/foo.py` in site-packages). Skips:
- `__pycache__/` and `*.pyc`
- dot-prefixed dirs (`.venv`, `.pytest_cache`, etc.)
- files that don't decode as UTF-8 (current behavior — raise)

Open question: do we also need to support single-file modules at
project root (no package dir)? Probably skip for v1 and require a
proper package dir. Easy to relax later.

## Whitelist semantics

`allowed_local_pkg_dirs` is a list of "trust roots". A resolved
local-package source dir is allowed if its `.resolve()` is *under*
at least one whitelist entry (or equal to it). Each whitelist
entry is `.resolve()`'d first. Symlinks resolve to real paths
before the prefix check.

If `None` (the default), behavior matches today — no restriction.
The MCP always passes a non-None list:

```python
WHITELIST = [
    repo_root / "v2" / "portable",
    repo_root / "extras" / "fxtr_sandboxing" / "fxtr_sandbox_rpc_adapter",
    repo_root / "extras" / "fxtr_sandboxing" / "fxtr_sandbox_types",
    workdir,  # the agent's own workdir
]
```

Where `repo_root = Path(__file__).resolve().parents[4]` from
`fxtr_workstation_mcp/fxtr_workstation_mcp/server.py` (or
wherever we end up doing the path computation).

Eventually we'll let the user override the workspace-dep paths
via `.fxtr_workstation.toml`, and eventually-eventually ship via
PyPI. Out of scope for this stage.

## Async refactor details

`anyio.run_process(command, *, cwd=..., check=True, stdout=PIPE,
stderr=PIPE)` is a drop-in for `subprocess.run`. Returns
`subprocess.CompletedProcess` with `stdout` / `stderr` as bytes
(not text — anyio doesn't have a `text=True` analog; we decode
manually).

Existing sync callers in fxtr_sandboxing that need updating:
- `sandbox_factory.bootstrap_factory` — already async, just add
  `await`.
- `tests/test_spawner.py`, `tests/test_transport_*_e2e_wasm.py`,
  `tests/test_transport_http_e2e_wasm.py`,
  `tests/test_direct_launcher.py` (if it uses prepare) — async
  tests; add `await`.

`fxtr/providers/uv_py_provider.py:255` has a method named
`prepare` too but it's a different function. Not touched.

## Scope ordering for the new RPC

`prepare()` stores entities via `.store()`, which targets the
currently-active fxtr scope. The MCP's `McpHostImpl.prepare_package_bundle`
needs the resulting TaskBundle to be reachable from `script_scope`
(so the returned FxtrID stays alive for the rest of the script,
and so subsequent `copy_entity_in` calls referencing the same ID
work).

Easiest: enter `script_scope.as_active_scope()` inside the RPC
handler before calling `prepare()`. The handler runs on an RPC-
owned task whose ambient active scope is the per-call dispatch
scope (a child of script_scope); switching to script_scope for
the duration of the prepare call is fine because we're just
storing entities into a parent of where the RPC dispatch sits.
Return-value bridging then works the same way `lookup_entity`'s
does.

Open question: is the active-scope contextvar thread-local in a
way that breaks under anyio's `run_process` event-loop dispatch?
I don't think so — anyio's contextvars are propagated naturally
through `await` and the `Process` it returns is just an
async-iterator over stdout/stderr. The scope is set on the
calling task; the subprocess doesn't care.

## Bundle entity persistence after script ends

`prepare_package_bundle` doesn't auto-export. If the agent just
calls it and doesn't `export_entity` or pass the FxtrID to a
`schedule_invoke` arg, the bundle dies with `script_scope` at
end-of-script. That's the right default — no surprise lifetime.

When the agent does pass the FxtrID through `schedule_invoke`
(e.g. `lambda s: s.spawn(bundle_id)`), the existing
`_walk` / `_maybe_register_entity` path generates a fresh
`invoke_arg:` handle, the host's `copy_entity_in` is a no-op
because the entity is already in script_scope (parent of
dispatch scope), and the worker's `_register_exported_entities`
runs `expose_entity` over /rpc to get a short_id. The
workstation's invoke-worker session-pins the entity arg when it
processes the InvokeOp, so the bundle stays alive across the
spawn dispatch.

Hmm wait: `copy_from_scope` on an entity that's already in the
target scope — does it short-circuit or error? Need to verify
during implementation. If it errors, we can skip the
`copy_from_scope` call when the entity is already in scope. If
it short-circuits, nothing to do.

If `export_entity` is called on a `prepare_package_bundle`-derived
ID, the same logic applies — the entity is already in
`script_scope`, `host.copy_entity_in` just records the handle
mapping for `_register_exported_entities` to pick up.

## Test strategy

- Unit tests for the file-walk logic in fxtr_sandboxing/tests/:
  - Auto-discovery (top-level pkg dir, src layout).
  - Explicit `[tool.fxtr.sandboxing] packages`.
  - Hatch's `[tool.hatch.build.targets.wheel] packages`.
  - Whitelist enforcement (refused outside, allowed under each
    trust root).
- Async-prepare smoke: re-run an existing e2e
  (`test_spawner.test_spawn_then_connect_greet`) after the refactor
  to confirm the bundle still produces a working sandbox. (Since
  rpc_task uses hatchling, the file-walk has to produce a
  bundle indistinguishable from the old wheel-build version for
  the test to pass — that's the regression check we want.)
- Integration in fxtr_workstation_agent_plugin/tests/:
  - New test `test_prepare_package_bundle.py` that:
    1. Sets up an agent workdir with a `packages/inner_service/`
       containing a minimal pyproject + entry point + one method.
    2. Sets up a `SandboxSpawnerImpl` mounted on the in-process
       workstation under a known external_name.
    3. Drives the MCP through:
       - `prepare_package_bundle("inner_service")` →
         FxtrID returned to the agent
       - `schedule_invoke("running", spawner_stub,
         lambda s: s.spawn(bundle))` →
         `["exported_service", N]` (or the workstation's wire
         shape for invoke results that produce services —
         actually need to verify that invoking a method whose
         return type is `ServiceRef[T]` produces the right
         slot wire form)
       - retrieve, schedule_invoke on the spawned ref to call
         the user method, verify result
    4. Asserts a real spawn happened (e.g. method execution
       writes a sentinel)
- Whitelist negative test: agent's pyproject points at
  `path = "/etc/"` → MCP returns a clean error string, no
  bundle entity created.

## Things deferred

- DockerTaskBundle path — out of scope.
- Configurable trust roots via toml — out of scope; hardcode for
  now.
- Caching bundle results across `run_script` calls — out of
  scope; rely on uv's own caches + content-address dedup for
  most of the win.
- Shipping fxtr2-portable / adapter / sandbox-types as pip-
  installable packages — out of scope.
- Allowing single-file modules at project root with no package
  dir — out of scope.

## Risks / opens to flag during implementation

- Will the `script_scope.as_active_scope()` context manager
  compose cleanly inside an RPC handler? The dispatch scope is
  the active one when the handler starts; we switch to
  script_scope for prepare()'s `.store()` calls and switch back
  when prepare returns. The return-bridge for the FxtrID then
  encodes against whatever scope is active when the method
  function returns — needs to be a scope reachable from the
  sandbox side. script_scope is a child of host_scope which is
  the /rpc connect scope, so it should bridge fine. Verify
  empirically.
- `anyio.run_process` returns bytes; need to decode stdout
  manually. Easy.
- Per the user's later note: re-check whether `prepare_package_bundle`
  needs to be reentrant within one script (probably yes — the
  agent might prep two bundles). The function is naturally
  reentrant since it doesn't share state across calls.
- The hatchling `packages = [...]` config field has slightly
  different semantics: hatchling treats each entry as a
  "package directory to copy", with the package name derived
  from the basename. Verify this matches our intended
  interpretation. If hatchling has additional knobs we silently
  ignore (e.g. `force-include`), that's OK for v1 — we just
  document we support a subset.
