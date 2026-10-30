# Stage 3 scratch — code map (grounding for the plan)

Consolidated from a read of the three subsystems (2026-05-30). File:line
refs are anchors for implementation; verify before editing (line numbers
drift). Repo root: `/Users/daniel/fxtr-worktrees/fxtr`.

> **Chunk order (2026-05-31 revision):** 1 harness → 2 mkWasmService →
> 3 sandbox_smoke rework (retires the git-ref unknown) → 4 nix.py →
> 5 NixWasmServiceDefn → 6 spawner arm + tests → 7 deprecation/orphan
> removal. See `stage3_bundle_and_spawner.md`.

## Test harness — `prepare_workdir_repo` (chunk 1)

New shared test util; **none exists today**. Build a tempdir workdir repo
from a template: copy → marker substitution → `uv lock` + `nix flake
lock` → `git init`/commit → capture HEAD.

- **Existing primitives to extend** — `nix/py_testing/tests/conftest.py`
  has `flake_ref` (`attr -> "git+file://<repo-root>#<attr>"`) + `nix_build`
  (`nix build --no-link --print-out-paths`). `prepare_workdir_repo` lives
  alongside (shared util importable by the `fxtr_sandboxing` spawner
  tests, `py_testing`'s sandbox-smoke test, and the agent-plugin E2E).
- **Substitution markers** — `# fxtr-lib-sub: flake-input` (placeholder
  URL in `flake.nix`), `# fxtr-lib-sub: active-in-template` /
  `active-in-workdir` (the uv-source comment-flip pair). `@FXTR_LIB_FLAKE@`
  / `@FXTR_LIB_URL@` / `@FXTR_LIB_REV@` tokens derive from `fxtr_ref`.
- **`fxtr_ref`** — default `git+file://<repo-root>` (no `?rev=` → locks to
  current HEAD); `FXTR_TEST_LIB_FLAKE` env override. Uncommitted-edits
  stderr warning via `git -C <repo> status --porcelain`.
- **`on_branch=False`** — leave the workdir commit reachable by no branch
  (commit, drop the branch ref) for the off-branch `?rev=` case.

## sandbox_smoke rework (chunk 3)

- `nix/sandbox_smoke/pyproject.toml:41-43` — the hardcoded
  `git = "file:///Users/daniel/fxtr-worktrees/fxtr", rev = "<sha>"` for
  `fxtr-sandbox-rpc-adapter` / `fxtr-sandbox-types` / `fxtr2-portable`.
  → convert to the comment-flip convention (relative paths active in
  template; git-source line substituted in the workdir copy).
- Add a trivial `[project.entry-points."fxtr.service"]` (`run`) to
  `src/fxtr_sandbox_smoke/__init__.py` (currently empty) + a small
  `flake.nix` building `fxtr.services.smoke = mkWasmService { src = ./.; }`.
- **Repo-root `flake.nix`** — remove the `guest-site-packages-check`
  target (`flake.nix:121-124`) + the direct `_mkGuestSitePackages` import
  (`flake.nix:43-45`). Keep the `_mkGuestSitePackages.nix` helper.
- **`nix/py_testing/tests/test_sandbox_import.py`** — migrate the
  bind-mount-into-eryx `import fxtr2` check onto the
  `prepare_workdir_repo` tempdir build (was the module-scoped
  `guest-site-packages-check` fixture).

## Spawner path (`projects/fxtr_sandboxing/`)

- **`SandboxSpawnerImpl.spawn`** — `fxtr_sandboxing/src/fxtr_sandboxing/spawner.py:207–258`.
  `@service_method async def spawn(self, bundle: EntityRef[TaskBundle |
  PyContainerTaskBundle | DockerTaskBundle]) -> ServiceRef[SandboxConnectProto[…]]`.
  Dispatch = `await bundle.load()` then `isinstance` → picks a
  `runner` (`_spawn_runner_wasm` / `_spawn_runner_py_container` /
  `_spawn_runner_container`), then `await self._env_tg.start(runner,
  loaded, teardown_event)`, `bridge_from_scope`, mounts a
  `_SpawnConnectWrapperImpl(raw_guest_ref, bundle)` on the call scope,
  returns the wrapper ref.
  → **Stage 3:** add `NixWasmServiceDefn` to the union + an
  `isinstance` arm → `_spawn_runner_nix_wasm`. API shape unchanged.
- **`_spawn_runner_wasm`** — `spawner.py:112–140`. `async with
  _run_wasm(bundle, self._cache_dir, transport_server=…) as session:`
  then JSON stream + `open_rpc_scope`, `task_status.started(...)`,
  `await teardown_event.wait()`.
- **`_run_wasm`** — `runner.py:260–296`. mkdtemp →
  `build_site_packages_for_bundle(bundle, cache_dir, site_packages_path)`
  → `eryx.SandboxFactory(site_packages=str(site_packages_path))`.
  → **Stage 3:** the nix arm replaces the mkdtemp+materialize with a
  `nix build` store path, then the same `SandboxFactory(...)`. Factor a
  shared `_run_from_site_packages(path, entry_point, …)` core; the nix
  arm and `_run_wasm` both call it.
- **entry-point injection** — `run_in_factory`, `runner.py:471`:
  `runner_parts.append(f"_ENTRY_POINT = {entry_point!r}\n")`. nix arm
  feeds `entryPoint` (from `nix eval`) the same way.
- **`SandboxSpawnerImpl.__init__`** — `spawner.py:91–105`. kw-only:
  `cache_dir, env_tg, transport_server, insecure_registry=False,
  target_arch`. Instantiated in **`create_sandbox_environment`**,
  `spawner.py:521–527`, mounted on `host_scope`.
  → **Stage 3:** add `git_repo_path: Path` (the configured repo) here +
  thread through `create_sandbox_environment`.
- **No nix usage anywhere in fxtr_sandboxing; no `nix.py` exists.**
  → **Chunk 4** creates `fxtr_sandboxing/nix.py` (after chunk 3 validates
  the raw git-ref CLI calls).
- **Entity types** — `fxtr_sandbox_types/src/fxtr_sandbox_types/entities.py`:
  `TaskBundle` (119–139), `PyContainerTaskBundle` (180–217),
  `DockerTaskBundle` (234–244), all `DataclassEntity` w/
  `fxtr_type=define_type(SHARED_SANDBOX_NS, …)` (`_namespace.py`).
  → **Stage 3:** add `NixWasmServiceDefn(git_commit, flake_attr,
  flake_path)` here (canonical), `define_type(SHARED_SANDBOX_NS,
  "nix_wasm_service_defn")`.

## Sandbox builtins / type exposure (`projects/fxtr_workstation_agent_plugin/`)

- **`_fxtr_mcp_builtin`** is built per-script by `build_builtins()` in
  `sandbox/fxtr_ws_mcp_sandbox_bridge/.../builtins.py` and installed by
  `script_runner.py` before the preamble. It **re-exports types** (e.g.
  `ServiceRef`, `EventListenerProto`, `UnknownService`).
  → **Stage 3:** re-export `NixWasmServiceDefn` from here.
- **Sandbox-local type duplicates are an established pattern** — the
  bridge's `protocols.py` defines a sandbox-local `EventListenerProto`
  (CODE_OVERVIEW lines 1258–1266 call it a harmless cosmetic dup; the
  dialect strips `target_type`). fxtr2-portable fully supports type
  descriptors (`DataclassEntity`, `define_type`); the demo constructs
  typed entities sandbox-side extensively.
  → **Stage 3:** duplicate `NixWasmServiceDefn` into the bridge
  (`protocols.py`), **same `define_type` + fields** as the
  `fxtr_sandbox_types` canonical copy → same wire type; workstation
  decodes via its copy.
- **`.store()` is the resolution primitive.** A fresh sandbox-constructed
  `DataclassEntity` is made host-resolvable by `.store()`, after which it
  passes through the normal entity-arg pipeline. Demo idiom
  (`examples/fxtr_workstation_demo/agent_workdir/README.md`):
  `note = Note(...).store(); await present_to_user(note)` and
  `cfg = DelayedEchoConfig(label="demo").store(); schedule_invoke(..., lambda s: s.connect(cfg))`.
  → **Stage 3 agent flow:** `defn = NixWasmServiceDefn(...).store();
  schedule_invoke("running", spawner, lambda s: s.spawn(defn))`.
- **`prepare_package_bundle` (the model for `current_commit()`, Stage 4)**
  — host RPC `McpHostImpl.prepare_package_bundle` (`host_impl.py:242–291`)
  constructs host-side in a transient scope, copies to `script_scope`,
  returns `FxtrID`; sandbox passthrough at `builtins.py:579–598`.
- **entry-point convention** — `read_entry_point` in
  `fxtr_sandboxing/.../_prepare_common.py:44–63` parses
  `[project.entry-points."fxtr.service"]` → single `module:function`.
  → **Stage 3:** `mkWasmService` replicates this in nix via `fromTOML`
  (no Python writer needed).
- **type id_string (relevant to Stage 5, noted here):** the agent
  reads `MyType.fxtr_type.id_string()` directly in a `run_script` (demo
  README "Writing a custom visualizer"). That's how the Stage-5 preset
  key is obtained — the once-planned `entityIdString` nix helper was
  dropped (see `stage5_ui_presets.md`).

## Nix helpers (`nix/`, repo-root `flake.nix`)

- **`_mkGuestSitePackages.nix`** — `nix/lib/_mkGuestSitePackages.nix:1–82`.
  `{ workspaceRoot, name?, system?, sourcePreference? }` → flat
  site-packages at `$out` (wasi/wasm32 markers handled). Imported
  directly in repo-root `flake.nix:43–45`, used by the
  `guest-site-packages-check` target (`flake.nix:121–124`).
  → **Stage 3 `mkWasmService`** is its real consumer.
- **`nix/lib/default.nix`** — real: `canonicalSystem`, `pkgsFor`,
  `pkgs`, `mkVenv`, `runFxtrWriter`, `mkEntityFromPython`. Stubs via
  `notYet` (47–52): `mkWasmPackageBundle` (→ rename `mkWasmService`),
  `mkRendererEntity`, `esbuildRendererEntity`, `mkDispatcherPreset`
  (Stage 5), `mkAll` (drop), `mkDockerPackageBundle`.
- **repo-root `flake.nix`** — `lib.fxtr = libFxtr` (48);
  `packages.${canonicalSystem}.*` target pattern (50–125:
  `smoketest`, `fxtr2-import-check`, `hello-world-layer`,
  `sample-type-entity`, `guest-site-packages-check`);
  `TODO(mkWasmPackageBundle)` marker at 39–42.
- **`mkWasmService` shape:** pure-nix (NOT via `fxtr_nix_writer`/
  `runFxtrWriter` — no entity written). `{ src }` →
  `{ sitePackages = _mkGuestSitePackages { workspaceRoot = src; };
  entryPoint = <fromTOML src/pyproject.toml>; }`.

## Orphaned closure-scan code to delete (Chunk 7)

All purely orphaned (built for the dead `.drv`-pointer design):

- `HelloWorldEntity.drv_ref` field — `nix/fxtr_nix_writer/src/fxtr_nix_writer/entities.py:33`.
- `writers.emit_drv_ref_sidecar` — `…/writers.py:16–27`; and
  `write_hello_world`'s optional `cbor_drv_ref`/`sidecar_drv_ref` params
  + their use — `…/writers.py:36–50`.
- `hello-world-layer` probe `.drv` wiring — `flake.nix:85–99` (probe
  derivations 88–89, `unsafeDiscardOutputDependency` embeds 96–97).
- tests `test_drv_refs_in_layer_closure` + `test_probe_recipes_pinned_without_building_outputs`
  — `nix/py_testing/tests/test_hello_world.py:44–93`.

**Keep:** `HelloWorldEntity` core, `write_hello_world` core,
`test_hello_world_round_trip`, the `hello-world-layer` target minus the
probe wiring.

## Deprecation moves (Chunk 7)

Per note 12: prepare modules → `fxtr_sandboxing/deprecated/`; old entity
types → `fxtr_sandbox_types/deprecated_entities.py`; deprecation
docstrings on the old `_spawn_runner_*` arms (kept working); low-level
launchers (`runner.py` etc.) NOT moved (nix arm reuses
`run_in_factory`).
