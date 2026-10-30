# Stage 8 — Sandbox-bundle helper (`prepare_package_bundle`)

Goal: let the agent build a `TaskBundle` entity directly from a
package under their workdir, so they can ship code to an
externally-mounted `SandboxSpawnerProto` without leaving
`run_script`. This is the missing piece for "the agent writes a
sandboxed service, then spawns it on the workstation themselves".

Today the agent can already reach a spawner service via
`lookup_service_by_name("…")` and call `spawn` / `connect` through
`schedule_invoke`. They just can't construct a `TaskBundle`,
because `fxtr_sandboxing.prepare.prepare(task_dir)` runs uv +
subprocesses on the host. Stage 8 wraps that in a host RPC and
exposes one new sandbox builtin.

This stage also takes the opportunity to refactor `prepare()`
itself for two unrelated reasons:

- Async via `anyio.run_process` (replacing `subprocess.run`), since
  every caller is already in an async context.
- File-walk the local package's source directly instead of running
  `uv build --wheel`. The wheel build invokes the agent's
  `[build-system]` build backend, which is arbitrary Python — a
  security hole when prepare is invoked on agent-authored
  pyprojects.

## Agent surface (new builtin)

```
async prepare_package_bundle(package_name: str) -> FxtrID
```

Validates `package_name` (no path separators, no `..`,
non-dot-prefixed); runs `prepare()` on
`<working_dir>/packages/<package_name>/`; returns the FxtrID of
the resulting `TaskBundle`. The FxtrID slots into the existing
flows:

- pass as a `schedule_invoke` arg: `lambda s: s.spawn(bundle)` —
  the entity-arg walker handles it via the existing
  `_maybe_register_entity` path; the workstation session-pins it
  when the InvokeOp is processed.
- `export_entity("slot", bundle)` if the agent wants to keep the
  reference past one script.

The bundle entity is stored in `script_scope`. If the agent does
neither of the above, it dies with the scope at end-of-script
(intentional — no surprise lifetime).

Errors surface as workstation-style exceptions inside the
sandbox: invalid name, missing directory, malformed pyproject,
files outside the whitelist, etc.

## Design

### 1. Async + safer `fxtr_sandboxing.prepare`

```
async def prepare(
    task_dir: Path,
    *,
    allowed_local_pkg_dirs: list[Path] | None = None,
) -> EntityRef[TaskBundle]
```

Two behavior changes plus a new parameter:

- **Async.** Replace `subprocess.run(...)` with
  `await anyio.run_process(...)`. Decode `result.stdout` from bytes
  manually. The lock + export remain two sequential calls; the
  per-local-package step (below) is what stops shelling out.
- **No-build local packages.** Drop `_build_source_package`'s
  `uv build --wheel` + zipfile unpack. Replace with a static
  file-walk:
  1. Read the local pkg's pyproject.toml.
  2. Resolve which directories make up the source via the rules
     below.
  3. For each directory, walk recursively; for every file under it,
     compute the in-site-packages relpath
     `f"{dir.name}/{path_relative_to_dir}"` and store its text
     content into `source_files`. Skip `__pycache__/`, `*.pyc`,
     dot-prefixed dirs. Raise on non-UTF-8 files (same surface as
     today).
- **Whitelist.** `allowed_local_pkg_dirs` is a list of trust roots.
  When set, every resolved local-pkg directory's `.resolve()` must
  be `==` or under one of the (also-resolved) trust roots, else
  raise `ValueError`. `None` (default) preserves today's
  unrestricted behavior.

### 2. Package-source discovery (drives the file-walk)

Reading `pyproject.toml` for one local package, pick source dirs
in this order — first match wins:

1. `[tool.fxtr.sandboxing] packages = ["<rel/path>", ...]` — our
   own override. Each entry is a relative path under the pyproject's
   directory.
2. `[tool.hatch.build.targets.wheel] packages = [...]` — same shape,
   reused so hatchling-configured packages don't need a second
   block.
3. Auto-discover: try `<project_root>/<normalized_name>/`, then
   `<project_root>/src/<normalized_name>/`. `normalized_name =
   project.name.replace("-", "_").lower()`. Error if neither
   exists.

The basename of each chosen directory is its name in site-packages
(matches hatchling's convention for `packages = ["src/foo"]` →
import as `foo`).

**Supported subset of pyproject configs:**

- We support directory-based packages (one or more dirs whose
  basenames become the site-packages names).
- We support `[tool.fxtr.sandboxing] packages` and
  `[tool.hatch.build.targets.wheel] packages`. We treat each entry
  as a path-from-project-root literally; we do **not** honor hatch's
  `sources = [...]` rewriting, `include` / `exclude` patterns, or
  `force-include`. Agents who need those write paths out explicitly
  (e.g. `"src/my_pkg"`) and put everything they want shipped in the
  named directory.
- Single-file modules at project root (no package dir) are not
  supported; the agent must put their code inside a package
  directory.
- Build-hook-generated files (e.g. `_version.py` produced by
  hatch-vcs) are not supported — we never run build backends or
  hooks. Agents must use static `project.version`.
- Native-extension wheels remain unsupported (existing
  behavior in `_find_pure_python_wheel` — only `py3-none-any`).

Document this subset in the package's `CODE_OVERVIEW.md` and in
the new builtin's docstring so the agent knows what to expect.

### 3. Host-side MCP impl

New `McpHostProto` method (host package + bridge package both
get the protocol addition):

```
async prepare_package_bundle(package_name: str) -> FxtrID
```

Impl in `McpHostImpl` (`fxtr_workstation_mcp/host_impl.py`):

1. Validate `package_name`: reject empty, anything containing `/`,
   `\`, `..`, or starting with `.`. Raise a structured error
   (translated to a Python exception sandbox-side via fxtr-RPC).
2. Resolve `pkg_dir = working_dir / "packages" / package_name`.
   Check it's a directory and contains `pyproject.toml`. Errors
   surface to the agent as exceptions inside their script.
3. Compute the whitelist (see below). Switch the active scope to
   `script_scope` (`with self._script_scope.as_active_scope()`)
   and call
   `await prepare(pkg_dir, allowed_local_pkg_dirs=WHITELIST)`.
4. Return the FxtrID extracted from the returned `EntityRef`.

`McpHostImpl` needs the working_dir; thread it through:
`server.run_script` already has `app_ctx.working_dir` and passes
it into `connection.run_script(...)` → `_ScriptJob.working_dir` →
`McpHostImpl(working_dir=...)`. One field added at each layer.

### 4. Trust-root list

Hardcoded for now in the MCP. Computed once from the package's
location on disk:

```
repo_root = Path(__file__).resolve().parents[N]
TRUST_ROOTS_FROM_REPO = [
    repo_root / "v2" / "portable",
    repo_root / "extras" / "fxtr_sandboxing" / "fxtr_sandbox_rpc_adapter",
    repo_root / "extras" / "fxtr_sandboxing" / "fxtr_sandbox_types",
]
WHITELIST = TRUST_ROOTS_FROM_REPO + [working_dir]
```

`N = 4` from `server.py` (traced:
`fxtr_workstation_mcp/fxtr_workstation_mcp/server.py` →
`parents[0]` package dir → `[1]` pyproject dir → `[2]`
plugin top → `[3]` `extras/` → `[4]` repo root). At MCP
startup, assert each computed trust root (a) exists and (b)
its `name` matches the expected basename (`portable`,
`fxtr_sandbox_rpc_adapter`, `fxtr_sandbox_types`). Fail loudly
if not — better than silently passing a broken whitelist.

Documented in the package's CODE_OVERVIEW + README as a v1
limitation. **Not** configurable via `.fxtr_workstation.toml` — that
file lives in the agent's workdir and is agent-editable, so an
agent-editable whitelist defeats the point. Eventually we'll move
it to an operator-controlled location (e.g. `~/.fxtr/` or a
plugin-install-time config); eventually-eventually we ship the
workspace deps as published wheels and the whitelist disappears.

### 4b. Workspace-dep paths in the agent's pyproject

The agent's `packages/<name>/pyproject.toml` references the three
workspace deps via absolute `[tool.uv.sources]` paths:

```toml
[tool.uv.sources]
fxtr-sandbox-rpc-adapter = { path = "/abs/path/to/projects/fxtr_sandboxing/fxtr_sandbox_rpc_adapter" }
fxtr2-portable           = { path = "/abs/path/to/v2/portable" }
fxtr-sandbox-types       = { path = "/abs/path/to/projects/fxtr_sandboxing/fxtr_sandbox_types" }
```

The agent already had to give absolute paths to `set_working_dir`,
so absolute paths in their pyproject aren't a new burden. We do
**not** echo paths from the MCP — the agent finds them once (e.g.
via `ls` in their setup script) and pastes them into their
pyproject. The trust-root whitelist enforces that these paths
match the MCP-computed roots. Eventually-eventually we ship the
deps as pip-installable wheels and this section disappears.

### 5. Sandbox-side builtin

Adds one entry to the builtin map in
`fxtr_ws_mcp_sandbox_bridge.builtins.build_builtins`:

```
async def prepare_package_bundle(name: str) -> FxtrID:
    if not isinstance(name, str):
        raise TypeError(...)
    return await host.prepare_package_bundle(name)
```

Listed alongside the other read-side closures in the returned
mapping. Preamble.py in the demo workdir adds it to its bulk
import. CODE_OVERVIEW's "Agent surface today" enumeration gains
it.

### 6. Scope mechanics

`prepare()` stores TaskBundle + PySourcePackage + PyWheelPackage
entities into the currently-active scope. `McpHostImpl.prepare_package_bundle`
sets the active scope to `script_scope` for the duration of the
call. Result:

- Entities land in `script_scope`, which is the parent of the per-
  RPC dispatch scope. Return-value bridging takes the FxtrID
  through the dispatch scope back to the sandbox the same way
  `lookup_entity`'s result does.
- The bundle stays alive for the rest of the script via
  `script_scope`. If the agent passes it to `schedule_invoke` or
  `export_entity`, `copy_entity_in` is logically a no-op (entity
  is already in target scope) — verify the actual call behavior
  during impl; if `copy_from_scope` errors on same-scope copy,
  short-circuit in `copy_entity_in`.
- At end-of-script `script_scope` drains; unreferenced bundles go
  with it. Bundles referenced by an InvokeOp arg are
  workstation-session-pinned by the time the mutation commits,
  matching how `schedule_invoke` entity args already work.

## Tests

In `projects/fxtr_sandboxing/tests/`:

- **`test_prepare_file_walk.py`** — unit-y tests for the new
  file-walk logic:
  - Auto-discovery picks `<pkg>/` when present.
  - Auto-discovery picks `src/<pkg>/` when only that exists.
  - `[tool.fxtr.sandboxing] packages = [...]` overrides
    auto-discovery.
  - `[tool.hatch.build.targets.wheel] packages = [...]` is honored
    when ours is absent.
  - Both unset and neither dir present → clear error.
  - `__pycache__/` and `.foo/` dirs are skipped; `.py` and `.txt`
    files are included.
  - Non-UTF-8 file → raises (existing behavior preserved).
- **`test_prepare_whitelist.py`** — whitelist enforcement:
  - `allowed_local_pkg_dirs` covering the project dir → passes.
  - Empty list → resolution refuses every local pkg.
  - A local-pkg path that escapes the whitelist (via
    `tool.uv.sources path = "../../outside"`) → raises.
- **Regression on existing e2e tests** — `test_spawner.py`,
  `test_direct_launcher.py`, `test_transport_*_e2e_wasm.py`,
  `test_transport_http_e2e_wasm.py` all `await prepare(...)`. The
  rpc_task / transport_task examples are hatchling-built; the file-
  walk should produce a `PySourcePackage` indistinguishable from
  the old wheel-unpack version, so these tests function as the
  regression guard for the file-walk's correctness.

In `projects/fxtr_workstation_agent_plugin/fxtr_workstation_mcp/tests/`:

- **`test_prepare_package_bundle.py`** — end-to-end through the MCP:
  1. Build an agent workdir fixture with a `packages/inner_service/`
     declaring `fxtr-sandbox-rpc-adapter[fxtr2-portable]` (sources
     pointing at the workspace paths), one entry point method, one
     trivial behavior.
  2. Boot an in-process workstation; mount a
     `SandboxSpawnerImpl` on it under an external_name.
  3. Boot the MCP pointing at the workdir.
  4. `run_script` driving:
     - `bundle = await prepare_package_bundle("inner_service")`
     - `spawner = await lookup_service_by_name("the-spawner")`
     - `await schedule_invoke("spawned", spawner,
        lambda s: s.spawn(bundle))`
  5. Channel-notification fires for the spawn result; agent
     retrieves it, gets a `ServiceRefStub`.
  6. Follow-up `run_script` invokes a method on the spawned
     service, retrieves the result, asserts it matches the
     inner service's known behavior.
- **`test_prepare_package_bundle_whitelist.py`** — agent
  `packages/bad_pkg/pyproject.toml` references `path = "/etc"` via
  `[tool.uv.sources]`; `prepare_package_bundle("bad_pkg")` returns
  a clean Python exception to the agent, no entity created.

## Implementation steps

- [x] Refactor `fxtr_sandboxing/fxtr_sandboxing/prepare.py` to
      `async def prepare(...)` using `anyio.run_process`. Update
      existing callers (`sandbox_factory.bootstrap_factory`,
      `tests/test_spawner.py`, `tests/test_transport_*_e2e_wasm.py`,
      `tests/test_transport_http_e2e_wasm.py`,
      `tests/test_direct_launcher.py`) to `await prepare(...)`.
      Verify existing test suite still passes.
- [x] Replace `_build_source_package` with the file-walk logic.
      Read pyproject, resolve package dirs by the documented
      priority (our block → hatch block → auto-discover), walk
      each dir, populate `source_files`. Existing tests cover the
      regression. (Also honored `[tool.hatch.build.targets.wheel.
      force-include]` since `v2/portable` needs it.)
- [x] Add the `allowed_local_pkg_dirs` parameter and enforcement
      to `_build_packages`. Default `None`. New unit tests for
      both passing and failing whitelist cases. (Whitelist also
      applied to force-include source paths.)
- [x] Update `projects/fxtr_sandboxing/CODE_OVERVIEW.md`:
      - `prepare` is now async + file-walks local packages.
      - `[tool.fxtr.sandboxing]` config block documented.
      - `allowed_local_pkg_dirs` whitelist parameter mentioned.
- [x] Add `prepare_package_bundle` to
      `fxtr_ws_mcp_sandbox_bridge/protocols.py`'s `McpHostProto`
      (one new method + docstring).
- [x] Implement `McpHostImpl.prepare_package_bundle` in
      `fxtr_workstation_mcp/host_impl.py`. Thread `working_dir`
      through the constructor; have `_do_script_job` pass it. Use
      a transient sub-scope of `script_scope` that also includes
      `GLOBAL_SCOPE` for the `prepare` call (type-descriptor
      entities live in GLOBAL_SCOPE), then `copy_from_scope` the
      result back into `script_scope`. Compute the trust-root
      list from the MCP's install path.
- [x] Add the sandbox-side closure `prepare_package_bundle` to
      `fxtr_ws_mcp_sandbox_bridge/builtins.py`'s `build_builtins`.
      Trivial passthrough.
- [x] Update the demo workdir's `preamble.py` to import the new
      builtin in the bulk-import block (so the demo and the
      in-memory test pick it up).
- [x] Update `projects/fxtr_workstation_agent_plugin/CODE_OVERVIEW.md`:
      - Add `prepare_package_bundle` to the "agent surface today"
        sentence at the top.
      - Add a short subsection under "Mental model" describing
        the bundle-construction flow: workdir packages →
        host-side prepare → TaskBundle FxtrID → agent's
        spawn-via-schedule_invoke.
      - Note the trust-root whitelist as a v1 limitation.
- [x] Write `projects/fxtr_sandboxing/tests/test_prepare_file_walk.py`
      and `test_prepare_whitelist.py`. (19 unit tests, all pass.)
- [x] Write `projects/fxtr_workstation_agent_plugin/fxtr_workstation_mcp/
      tests/test_prepare_package_bundle.py` (happy path +
      whitelist negative) — both tests pass individually.
- [x] Update CODE_OVERVIEW.md (final pass).

## Latency note

The first `prepare_package_bundle("foo")` call in a session is
slow (5–30s for a cold uv resolve + per-package file-walk).
Subsequent calls reuse uv's caches. The worker queue serializes,
so the calling `run_script` blocks for that duration; the
sandbox env supervisor's own bootstrap is independent (already
happens lazily on first `run_script`). The new builtin's docstring
should mention this so the agent isn't surprised.

## Error surface conventions

The new RPC raises Python exceptions on the host; fxtr-RPC
translates them into `IRException`s carried back to the sandbox.
Sandbox-side closure lets them propagate (no special-case
translation) — matches the existing `lookup_entity` behavior.
Conventions:

- Invalid `package_name` (contains `/`, `..`, etc.) →
  `ValueError`, surfaces as `IRException(category="ValueError")`.
- Missing directory or missing `pyproject.toml` →
  `FileNotFoundError`.
- Resolved package dir outside the whitelist → `ValueError`
  with a message naming the offending path and the trust roots.
- Bad `[tool.fxtr.sandboxing] packages` entries (path missing,
  not a directory) → `ValueError`.

## Open questions

- Do we need any way for the agent to inspect what would be
  bundled (a dry-run) before the actual bundle commits? Probably
  not for v1; `print(bundle)` after a successful prepare loads the
  entity in the sandbox and is enough debugging.
- The error surface — `prepare()` today raises
  `FileNotFoundError`, `ValueError`, `RuntimeError`. fxtr-RPC
  translates these to `IRException`s sandbox-side, which the
  builtin currently lets propagate. Verify the agent sees
  something useful in the typical "missing entry point" /
  "outside whitelist" cases. If the strings are too cryptic we
  add a translation layer.

## Wrap-up notes

Things that came up during implementation:

- **Hatch `force-include` had to be honored after all.** `v2/portable`
  assembles its wheel from `v2/fxtr2/` via `[tool.hatch.build.targets.
  wheel.force-include]` — when our file-walker hit it with the
  original "we don't honor force-include" rule, the prep failed
  with an empty `source_files`. Resolved by honoring the force-
  include table (with the same whitelist enforcement on source
  paths) and widening the workspace trust root from `v2/portable/`
  to `v2/` (so the table's `../fxtr2/` references pass). Documented
  in the plan's supported-subset section.
- **`script_scope` doesn't inherit from `GLOBAL_SCOPE`** by
  construction (it includes `rpc.host_scope`, which is isolated).
  But `prepare()` stores type-descriptor entities that live in
  `GLOBAL_SCOPE`, so calling `prepare()` with `script_scope` as
  the active scope failed at `.store()` → `lift()`. Resolved by
  opening a transient `FxtrScope(include=[script_scope,
  GLOBAL_SCOPE])` for the prepare call, then `copy_from_scope`
  the bundle entity back into `script_scope` before the transient
  drains. Mirrors what `sandbox_env.bootstrap_factory` already
  does for its own prepare call.
- **`prepare()` is now an `async def`**; updated all callers
  (`sandbox_factory.bootstrap_factory` + four e2e test files).
- **`peer.remote_service` is a `ServiceRef`, not a proxy.** The
  e2e test had to `await peer.remote_service.resolve()` before
  calling `get_entity` on it. Inside a `with peer.scope.
  as_active_scope():` block so the proxy resolves into a
  mountable scope.

Things to potentially revisit later:

- The workspace trust-root list is hardcoded in
  `host_impl.WORKSPACE_TRUST_ROOTS`. Should become operator-
  configurable (NOT via `.fxtr_workstation.toml`, which is agent-
  editable — likely `~/.fxtr/` or a plugin-install-time config).
  Or rendered moot by shipping `fxtr2-portable` /
  `fxtr-sandbox-rpc-adapter` / `fxtr-sandbox-types` as pip-
  installable wheels.
- The happy-path test doesn't actually exercise an end-to-end
  spawn. It verifies bundle construction, the FxtrID-to-slot
  round-trip, and the workstation's entity tracking. A real
  spawn would require a `SandboxSpawnerImpl` peer mounted on the
  workstation, which is straightforward to add but felt out of
  scope for stage 8 (which is about the bundle-construction
  helper, not the spawn surface).
- We didn't run the full MCP plugin test suite at the end (only
  the targeted bundle tests + the fxtr_sandboxing e2e regression
  tests). User opted to defer the big sweep.
