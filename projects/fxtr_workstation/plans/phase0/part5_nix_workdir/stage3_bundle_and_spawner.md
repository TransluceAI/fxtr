# Stage 3 — Service bundles + git-ref spawner

Add the git-referenced service path: a `mkWasmService` helper that
builds a `fxtr.services.<name>` attr, a `NixWasmServiceDefn` spawn
reference, and a spawner dispatch arm that resolves it via a nix git
flake ref. Build the **workdir test harness** (`prepare_workdir_repo`)
the git-ref tests need, and use it to **rework `sandbox_smoke`** onto
the new system (killing its hardcoded abs-path+rev). Move the old
in-band-bytes bundle path to `deprecated/`, and **remove the orphaned
Stage-1 `.drv`-pointer / closure-scan code**.

> **Revised (2026-05-30):** this stage was reshaped by the code/entity
> decoupling. There is **no** `write_wasm_bundle`, no `.drv`-pointer
> entity, no `unsafeDiscardOutputDependency` / sidecar / closure-scan.
> A service is a plain flake attr the spawner builds from a git commit.
> See `design_notes/05_sandbox_bundle.md`, `12_nix_drv_reference.md`.
>
> **Revised (2026-05-31):** added the `prepare_workdir_repo` test/scaffold
> harness (see "Test & scaffold workdir setup" below) and folded the
> `sandbox_smoke` de-hardcoding into this stage. The git-flake-ref unknown
> is now retired against the *reworked sandbox_smoke* workdir build (chunk
> 3) before `nix.py` / the spawner build on it, so the chunk order changed
> — see "Implementation chunks". No separate pre-work stage.

## Scope

**`mkWasmService` helper.** Implement `lib.fxtr.mkWasmService { src,
... }` in the fxtr flake. It produces the `fxtr.services.<name>` attr
shape `{ sitePackages = <drv>; entryPoint = "…"; }`:

1. `_mkGuestSitePackages` (Stage 2) over the package's `pyproject.toml`
   (git-dep on fxtr pulls `fxtr-sandbox-rpc-adapter[fxtr2-portable]`)
   → the flat, eryx-runnable site-packages `out`. `mkWasmService` is
   the real consumer of `_mkGuestSitePackages` (reconcile the
   `TODO(mkWasmPackageBundle)` / `guest-site-packages-check` placeholder
   left in the repo-root `flake.nix` — rename it as needed).
2. `entryPoint` read from the package's `pyproject.toml`
   `[project.entry-points."fxtr.service"]`.
3. Return `{ sitePackages = <site-packages drv>; entryPoint = "…"; }`.
   **No entity write, no second "writer" derivation.**

Replace the `notYet` stub for `mkWasmPackageBundle` in
`nix/lib/default.nix` with `mkWasmService` (drop the old name), and drop
the `mkAll` stub (no longer in the design). The preset-helper stubs
(`mkRendererEntity` / `mkDispatcherPreset` → removed;
`esbuildRendererEntity` → renamed `esbuildRenderer`) are handled when
Stage 5 lands.

**Spawn reference type.** `NixWasmServiceDefn(git_commit: str,
flake_attr: str, flake_path: str | None = None)` — a `DataclassEntity`
(like `TaskBundle`) that *references* code; `entry_point` is **not** on
it (lives on the flake attr). Two copies, same `define_type(
SHARED_SANDBOX_NS, "nix_wasm_service_defn")` + fields → same wire type:

- **Canonical** in `fxtr_sandbox_types/entities.py` (alongside
  `TaskBundle`) — the workstation/spawner decode against this.
- **Duplicate** in the sandbox bridge (`fxtr_ws_mcp_sandbox_bridge/
  protocols.py`), re-exported from `_fxtr_mcp_builtin` so the agent can
  `from _fxtr_mcp_builtin import NixWasmServiceDefn`. This is the
  established sandbox-local-dup pattern (cf. the `EventListenerProto`
  dup); fxtr2-portable supports the type-descriptor machinery.

The agent constructs it in-script, calls **`.store()`** to make the
fresh entity host-resolvable (the standard idiom — `Note(...).store()`,
`DelayedEchoConfig(...).store()`), and passes it to the **unchanged**
`spawn`:

```python
defn = NixWasmServiceDefn(
    git_commit="<HEAD>",                  # Stage 4 swaps in current_commit()
    flake_attr="fxtr.services.hello_service",
).store()
await schedule_invoke("running", spawner, lambda s: s.spawn(defn))
```

**Spawner API is unchanged.** `spawn(bundle: EntityRef[TaskBundle | … ])`
just gains `NixWasmServiceDefn` in its union and `isinstance`-dispatches
to the new arm — the deprecated bundle types keep flowing through the
same method. No new spawner method/proto, no `make_nix_service` builtin,
no host-side construction step.

**Spawner-side git-ref wrapper.** New module `fxtr_sandboxing/nix.py`
wrapping the git-flake-ref calls (Q12.E):
- `eval_entry_point(repo, commit, attr, flake_path) -> str`
- `build_site_packages(repo, commit, attr, flake_path) -> StorePath`
Both form `git+file://<repo>?rev=<commit>&dir=<flake_path>#<attr>(.…)`
(`git+ssh://` / `git+https://` for remote repos), shell out via
`anyio.run_process`, and surface git-fetch / eval / build failures
distinctly (per `11_failure_and_ux.md`). **No manual checkout** — nix's
git fetcher does it. This wrapper is also the natural home for the
forward-looking `.drv`-hash identity helper (not built in v0).

**Spawner dispatch arm.** `SandboxSpawnerImpl.spawn` gets
`_spawn_runner_nix_wasm` for `NixWasmServiceDefn`: eval `entryPoint`,
build `sitePackages`, build an eryx factory over the realized
site-packages dir, inject `_ENTRY_POINT`, reuse the existing launcher
plumbing (`runner.run_in_factory` etc.). The spawner is constructed
with **one git repo** (init arg; locally the agent's workdir repo); the
defn's `flake_path` selects the flake within it.

Trust model: **connection-level** — the workstation builds + runs any
`(commit, attr)` from an authorized client against its configured repo,
like a nix remote builder. No repo/ref allowlist (building is no more
privileged than `run_script`). See `12_nix_drv_reference.md` "Trust
model & topology".

**Remove orphaned Stage-1 closure-scan code.** The `.drv`-pointer
validation Stage 1 built for the old design is now dead:
- Drop `HelloWorldEntity.drv_ref` (or the whole probe wiring) from
  `fxtr_nix_writer`'s hello-world entity + the `hello-world-layer`
  derivation's two probe `.drv`s.
- Drop `writers.emit_drv_ref_sidecar` and the `_drv_refs/` sidecar
  emission.
- Drop the `py_testing` tests asserting `nix-store --query --references`
  / `nix derivation show` over the embedded/sidecar `.drv`.
- Keep `write_hello_world` itself as the basic round-trip scaffold
  (minus the probe machinery).

**Deprecate old path.**
- Move `fxtr_sandboxing/prepare.py`, `_prepare_common.py`,
  `_bundle_materialize.py`, `wheels.py`, `_arch_tags.py`,
  `container_prepare.py`, `py_container_prepare.py`, `base_images.py`,
  `container_config.py`, `container_rebuild.py` into
  `fxtr_sandboxing/deprecated/`.
- Move old entity types (`TaskBundle`, `DockerTaskBundle`,
  `PyContainerTaskBundle`, supporting types) into
  `fxtr_sandbox_types/deprecated_entities.py` (cosmetic location).
- Old `SandboxSpawnerImpl.spawn` arms (`_spawn_runner_wasm`, etc.) stay
  with a "deprecated, prefer `NixWasmServiceDefn`" docstring.
  `runner.run_in_factory` and friends are NOT moved.
- `prepare_package_bundle` builtin keeps working (its removal is
  Stage 4) but imports from the deprecated path with a note.

**`sandbox_smoke` rework.** Today `nix/sandbox_smoke/` is a bare uv
workspace built *in place* by the repo-root flake's
`guest-site-packages-check` target, with a hardcoded
`git = "file:///Users/…", rev = "<sha>"` in its `[tool.uv.sources]`
(machine-specific + stale; its own comment admits it). Rework it onto
the new system rather than deleting it:

- Convert its `[tool.uv.sources]` to the **comment-flip** convention
  (see "Test & scaffold workdir setup") — relative paths active in the
  template, git-source line activated + rev-substituted in the workdir
  copy. This kills the abs-path+rev hardcode.
- Give it a trivial `[project.entry-points."fxtr.service"]` (a no-op
  `run`) + a small `flake.nix` so it builds through **`mkWasmService`**
  as `fxtr.services.smoke`. The smoke check then guards the exact helper
  the spawner builds (stronger than the old bare `import fxtr2`), with no
  lib-surface change. (Alternative considered: promote
  `_mkGuestSitePackages` to `lib.fxtr.*` — a tracked followup — and keep
  it serviceless. We chose the `mkWasmService` route.)
- Build it via `prepare_workdir_repo` → a tempdir workdir, `--no-update-lock-file`,
  against the local `fxtr` ref. This is where the git-flake-ref
  validation checklist (note 12) is retired — chunk 3, *before* `nix.py`
  / the spawner.
- **Remove** the repo-root `guest-site-packages-check` target + the
  direct `_mkGuestSitePackages` import in `flake.nix`; migrate
  `test_sandbox_import.py` (bind-mount into eryx, `import fxtr2`) onto the
  tempdir build. Keep the `_mkGuestSitePackages` *helper* (mkWasmService
  uses it).

**Demo update.** `basic_test_demo` gets its first `flake.nix`, declaring
`fxtr.services.hello_service = mkWasmService { src = ./packages/hello_service; }`,
with its `fxtr` flake input + the `hello_service` package's uv sources
authored per the comment-flip / placeholder convention. The existing
in-script `prepare_package_bundle` path is unchanged (it still works for
arbitrary paths; its removal is Stage 4).

Three end-of-stage tests:

- **Direct spawner integration test** (tighter mechanism check):
  `prepare_workdir_repo` builds a tempdir workdir from the demo template,
  then construct `NixWasmServiceDefn(git_commit=<HEAD>, flake_attr=…)` on
  the host and call `SandboxSpawnerImpl.spawn` against that repo; assert
  the sandbox comes up. Isolates the git-ref build + spawner arm from the
  MCP/RPC layers.
- **`run_script`-driven end-to-end test** (the real agent-facing path —
  now possible because the spawner no longer depends on
  `retrieve_nix_entity`): the fixture calls **`prepare_workdir_repo`** to
  copy the `agent_workdir/` template into a tempdir, substitute the
  `fxtr` ref, re-lock, `git init` + commit, and **capture `HEAD`** (keeps
  tests off the monorepo, gives a clean committed repo). The workstation's
  spawner is configured with `git_repo_path=<that tempdir>` (the
  out-of-band config — see Q2 in chat). The script then constructs
  `NixWasmServiceDefn(git_commit="<that hash>", flake_attr=…)` and
  `await spawner.spawn(...)`; asserts the sandbox comes up and the service
  responds. (`current_commit()` — Stage 4 — is just the ergonomic way a
  *real* agent would obtain that hash; the test bakes it in, so the flow
  doesn't depend on it.)
- **`flakePath` subpath test** — `prepare_workdir_repo` with the flake at
  a repo *subpath*, built via `&dir=<flakePath>`, exercising subpath
  resolution even though the demos keep their flake at the repo root.

## Test & scaffold workdir setup

The git-ref code path builds **`--no-update-lock-file`** against a committed workdir
repo whose `flake.lock` pins the `fxtr` input to a committed library rev.
nix's git fetcher only sees committed tree content at a rev, so the
library change under test must be reachable as a git object — intrinsic to
"shipped code is pinned." A shared helper makes this ergonomic for tests
*and* the Stage-6 scaffold without baking machine-specific paths into the
source tree, assuming a VCS, or mutating any source history.

**`prepare_workdir_repo(template_dir, dest, *, lib_repo=None, lib_rev=None,
on_branch=True, relock=True) -> PreparedWorkdir(path, head_commit, lib_ref)`**
(implemented in `nix/py_testing/src/py_testing/workdir_harness.py`):

1. Copy `template_dir` → `dest` (a fresh dir, outside the source tree;
   build/VCS junk excluded).
2. **Substitute** the library ref across marked lines (below).
3. `uv lock` each substituted guest package + `nix flake lock` the workdir
   flake (in `dest`). `relock=False` is a test-only escape that skips this
   (exercises copy/substitute/git mechanics without nix/uv/a builder).
4. `git init` + `add -A` + `commit` (plain git; `dest` is a fresh, non-jj
   repo). When `on_branch=False`, leave the commit reachable by no branch
   (detached HEAD, branch ref dropped) so the spawner must resolve `?rev=`
   for an **off-branch** object — the note-12 reachability case.
5. Return `dest` + captured `HEAD` + the resolved `LibRef`.

**Resolving the library ref.** The library is identified as
`LibRef(lib_repo, lib_rev)` — a local git repo at a rev — and *both* the
flake input URL (`git+file://<repo>?rev=<rev>`) and the uv git-source
(`git = "file://<repo>"`, `rev = "<rev>"`) derive from it, so they pin the
identical rev with no fragile flake-ref parsing. Defaults: `lib_repo` =
the fxtr repo root (walk up for `flake.nix`); `lib_rev` =
`git rev-parse HEAD` of it. In CI (plain git checkout) that HEAD is the
commit under test — nothing jj-specific, no history mutation, the library
repo is only *read*. Overridable per field via the `lib_repo` / `lib_rev`
args or the `FXTR_TEST_LIB_REPO` / `FXTR_TEST_LIB_REV` env vars (e.g. pin a
specific uncommitted-work rev locally). When the rev is taken from HEAD
(no explicit rev) **and** the repo has uncommitted changes (`git status
--porcelain` non-empty — fires under both git and jj, since on-disk edits
show as worktree-vs-HEAD), emit a **stderr warning** that the frozen build
uses committed HEAD and will ignore working-tree edits.

> *(Implementation note, 2026-05-31: refined from the single
> `fxtr_ref` / `FXTR_TEST_LIB_FLAKE` knob first sketched, to the
> `(lib_repo, lib_rev)` pair — uv needs `(git-url, rev)` and nix needs a
> flake-url, and deriving both from one local `(repo, rev)` avoids
> re-parsing a flake ref. A `github:`-style prod ref isn't needed by tests;
> the scaffold script will pass its own ref when it lands.)*

**Substitution convention (markers, not unresolvable placeholders where
it matters).** Templates stay valid/lockable in place; copies rewrite
themselves to a pinned library:

- **`flake.nix` `fxtr` input** — a placeholder URL is fine (we never
  `nix flake lock` the template in place, only the tempdir):
  ```nix
  inputs.fxtr.url = "@FXTR_LIB_FLAKE@";  # fxtr-lib-sub: flake-input
  ```
  Substitutor swaps the URL for the resolved `fxtr_ref` in the copy.
- **guest `pyproject.toml` `[tool.uv.sources]`** — a **two-line
  comment-flip**, because the template's uv workspace *is* locked in place
  and must resolve against the **local working code** (recent branch
  commits may be unpushed to `main`, so a `github:` git source would fail
  to lock):
  ```toml
  fxtr2-portable = { path = "../../../v2/portable", editable = true }  # fxtr-lib-sub: active-in-template
  # fxtr2-portable = { git = "@FXTR_LIB_URL@", rev = "@FXTR_LIB_REV@", subdirectory = "v2/portable" }  # fxtr-lib-sub: active-in-workdir
  ```
  In the copy, the substitutor comments the first line and uncomments +
  rev-substitutes the second; then `uv lock` regenerates the lock against
  the pinned git source. (The `@FXTR_LIB_URL@`/`@FXTR_LIB_REV@` derive from
  `fxtr_ref`: its repo path + the rev `nix flake lock` resolved.)

The committed template therefore: locks its uv workspace against current
local code (relative paths active), needs no committed `flake.lock`, and
carries nothing machine-specific. The Stage-6 scaffold is the same helper
with a different `fxtr_ref` (`github:…` for prod / `git+file:///abs/fxtr`
for dev), so tests and the real scaffold converge on one code path.

Helper home: a shared test util for now (used by the `fxtr_sandboxing`
spawner tests, the `py_testing` sandbox-smoke test, and the agent-plugin
E2E test); promoted into the Stage-6 scaffold script when that lands.

## Implementation chunks (ordered)

Concrete file targets / `file:line` anchors live in
`stage3_bundle_and_spawner_scratch.md` (the code map). Taste calls: the
git-flake-ref unknown is retired early (chunk 3, via the reworked
sandbox_smoke build) so `nix.py` / the spawner build on validated
ground; deprecation/orphan-removal is last.

1. **[DONE] Test harness — `prepare_workdir_repo` + substitutor.** New
   shared test util in `nix/py_testing/src/py_testing/workdir_harness.py`
   (see "Test & scaffold workdir setup"): copy template → dest, marker
   substitution (flake-input placeholder + uv-source comment-flip), `uv
   lock` + `nix flake lock`, `git init`/commit, capture HEAD. The
   `(lib_repo, lib_rev)` resolution (+ `FXTR_TEST_LIB_REPO` /
   `FXTR_TEST_LIB_REV` overrides), the `on_branch=False` knob, the
   `relock=False` test escape, and the uncommitted-edits stderr warning
   all land here. Mechanics covered by `tests/test_workdir_harness.py`
   (9 tests, `relock=False`); the real `--no-update-lock-file` build is exercised in
   chunk 3.
2. **[DONE] `mkWasmService` + `fxtr.services.<name>`.** Pure-nix helper
   (`nix/lib/mkWasmService.nix`) over `_mkGuestSitePackages` (+ `fromTOML`
   entry-point); wired into `default.nix` (imports `_mkGuestSitePackages`,
   exposes `mkWasmService`), dropped the `mkWasmPackageBundle` + `mkAll`
   `notYet` stubs. Reconciled the repo-root `guest-site-packages-check`
   target to go through `mkWasmService` (dropped the flake's direct
   `_mkGuestSitePackages` import + TODO); gave `sandbox_smoke` a no-op
   `fxtr.service` entry point so it builds as a service. Validated: entry-
   point read, `nix flake check`, and a real builder build of the flat
   site-packages (`fxtr2/` present).
3. **[DONE] Rework `sandbox_smoke` + retire the git-ref unknown.**
   Converted its `[tool.uv.sources]` to the comment-flip convention (the
   relative template form `uv lock`s in place; the git-source workdir form
   builds); added the trivial service entry point + a `mkWasmService`-based
   `flake.nix` (`fxtr.services.smoke`); migrated `test_sandbox_import.py`
   to build it through `prepare_workdir_repo` + a `--no-update-lock-file`
   git-ref build, then `import fxtr2` under eryx. Removed the repo-root
   `guest-site-packages-check` target (the direct `_mkGuestSitePackages`
   import was already gone). **Two findings (recorded in note 12):** the
   lock-freeze flag is **`--no-update-lock-file`**, and the pinned lib rev
   must be **ref-reachable** for `uv lock` (uv can't fetch an off-branch jj
   `@` commit — pin a committed rev). Retired note-12 items: rev reachability, `--no-update-lock-file`
   forces the committed lock, baked-in local `fxtr`, cache hit. (`&dir=`
   subpath + `on_branch=False` off-branch revs are covered by chunk 6's
   spawner tests.)
4. **[DONE] `fxtr_sandboxing/nix.py`.** New module. `build_flake_ref` +
   `eval_entry_point` + `build_site_packages` forming
   `git+file://<repo>?rev=&dir=#…` with `--no-update-lock-file`. Errors are
   **step-typed only** (`NixServiceEvalError` / `NixServiceBuildError`,
   both `NixServiceRefError`) carrying ref + full stderr + exit code — we
   deliberately do **not** parse nix stderr to classify the cause (brittle;
   bad rev / missing attr / missing flake all surface during eval with
   wildly different messages). Tests: `tests/test_nix_git_ref.py` — unit ref
   construction + live eval-step error paths (bad rev / missing attr / bad
   `flake_path`) against the repo-root flake. Good-path eval/build is
   covered by chunk 6's spawner tests. Future home of the `.drv`-hash
   identity helper.
5. **[DONE] `NixWasmServiceDefn`** — canonical `DataclassEntity` in
   `fxtr_sandbox_types/entities.py` (`define_type(SHARED_SANDBOX_NS,
   "nix_wasm_service_defn")`, fields `git_commit` / `flake_attr` /
   `flake_path=None`), re-exported from the package `__init__`. Wire-
   identical dup in the bridge `protocols.py` (same NS UUID + label +
   fields → same `fxtr_type`), re-exported from `_fxtr_mcp_builtin` via
   `builtins.py`. Test `fxtr_workstation_mcp/tests/test_nix_wasm_service_defn.py`
   asserts both the id-string **and** the field list match (since
   `define_type` keys only on NS+label, not fields). (`current_commit()`
   is **Stage 4** — the Stage-3 tests bake the hash in host-side.)
6a. **[DONE] Extract `prepare_workdir_repo` to its own package.** Moved the
   harness out of `py_testing` into a stdlib-only leaf package
   `nix/fxtr_workdir_harness/` (zero runtime deps) so `py_testing`,
   `fxtr_sandboxing`, and the agent plugin can all dev-dep it cheaply — no
   sys.path hack, no heavy/nested-source drag. `py_testing` dev-deps it +
   `test_sandbox_import` imports from it; harness + sandbox_import tests
   green.
6b. **[DONE] Spawner arm + repo config + direct/flakePath tests.** Extended
   `spawn`'s union + added `_spawn_runner_nix_wasm` (nix.py eval+build →
   bare `SandboxFactory()` + `run_in_factory` with the store path mounted
   read-only via `volumes` + `sys_path_additions`); `git_repo_path: Path |
   None` init arg threaded via `create_sandbox_environment` (nix arm raises
   if unset). Self-contained **greeter** workdir-flake fixture
   (`fxtr_sandboxing/tests/_workdir_templates/greeter/`, copied from
   `rpc_task`, no committed `uv.lock` — `prepare_workdir_repo` generates it
   from the git-source form); `fxtr_sandboxing` dev-deps
   `fxtr-workdir-harness`; conftest gained a `deadline(seconds=N)` marker (the
   default 30s SIGALRM is too short for a cold builder offload). Tests
   (`tests/test_nix_spawner.py`, all green): root-flake spawn → connect →
   `greet`/`add`, the `flake_path="svc"` subpath case, and the
   no-`git_repo_path` guard.
6c-i. **[DONE] Demo flake + `hello_service` conversion.** `basic_test_demo`'s
   `agent_workdir` gained a `flake.nix` declaring
   `fxtr.services.hello_service = mkWasmService { src = ./packages/hello_service; }`
   (fxtr input = substitution placeholder). `hello_service`'s pyproject
   converted to the comment-flip `[tool.uv.sources]` + wasi `[tool.uv]`
   env + `uv_build` shape (committed `uv.lock` removed — generated in the
   workdir copy). Verified: `prepare_workdir_repo(agent_workdir)` + a
   `--no-update-lock-file` build of `fxtr.services.hello_service.sitePackages`
   produces the flat site-packages (`fxtr2/` + `hello_service/`). Safe to
   convert — no existing test builds `hello_service` (the prepare-path test
   uses its own `inner_service`; `test_mcp_in_memory` doesn't spawn).
6c-ii. **[DONE] `run_script` E2E.** Added `test_in_memory_e2e_nix_spawn` to
   the agent plugin's `test_mcp_in_memory.py` (reusing its in-memory MCP
   harness): `_running_workstation_with_nix_spawner` `prepare_workdir_repo`s
   the demo workdir → temp repo and boots a `WorkstationServer` whose
   `sandbox-spawner` external service is a `create_sandbox_environment(...,
   git_repo_path=<temp workdir>)` (mirroring `run_demo_workstation.py`). The
   `run_script` flow constructs `NixWasmServiceDefn(git_commit=<workdir
   HEAD>, flake_attr="fxtr.services.hello_service")` in-sandbox, spawns →
   connects with `HelloConfig` → `greet()` → asserts "Hello, world!".
   (agent plugin dev-deps `fxtr-workdir-harness`.) Green (~21s).
7. **[DONE] Deprecation + orphan removal (last).** Part A — deleted the
   orphaned Stage-1 `.drv`-pointer / closure-scan code: `HelloWorldEntity.drv_ref`,
   `writers.emit_drv_ref_sidecar` + `write_hello_world`'s `cbor_drv_ref`/
   `sidecar_drv_ref` params, the repo-root `hello-world-layer` probe wiring
   (`unsafeDiscardOutputDependency`), and the two closure-scan tests in
   `test_hello_world.py` (kept the round-trip). Validated: `nix flake check`
   + a de-probed `hello-world-layer` build (0 probe refs) at the working-copy
   rev. Part B — moved the prepare/bundle modules to
   `fxtr_sandboxing/deprecated/` (10 modules) + the bundle entity types to
   `fxtr_sandbox_types/deprecated_entities.py` (10 types, still re-exported
   from the package root → stable import paths). Repointed importers
   (`runner.py`/`py_container_runner.py` relative imports, `protocols.py`'s
   spawn-proto union, the agent plugin's `host_impl`/`sandbox_factory` +
   `prepare_package_bundle`, and ~19 fxtr_sandboxing test files) to the
   `deprecated.*` / `deprecated_entities` paths; deprecation docstrings on
   the old `_spawn_runner_{wasm,container,py_container}` arms. `prepare_package_bundle`
   keeps working (removal is Stage 4). Green: full `fxtr_sandboxing` suite
   (167 passed, 10 image-skips), `test_hello_world` + `test_entity_from_python`,
   `test_prepare_package_bundle`, the nix/spawner suites.

## Sequencing note

This stage proves the **full spawn path end-to-end through `run_script`**
(see the two tests above) — newly possible because the git-ref spawner
doesn't depend on `retrieve_nix_entity`. What's left for Stage 4 is
purely MCP-builtin work, independent of the spawner mechanism: add
`current_commit()` (so a real agent self-serves the hash the Stage-3
test bakes in), add `retrieve_nix_entity` (DATA), and **remove**
`prepare_package_bundle`. So the spawner refactor lands and is fully
demonstrated here; Stage 4 doesn't re-prove spawning.

The git-flake-ref build is the **main new unknown** to retire early —
it replaces the old closure-scan mechanic as "the thing that sinks the
stage if found late." It's retired in **chunk 3** by building the
reworked `sandbox_smoke` workdir through `prepare_workdir_repo`
`--no-update-lock-file` (a real "separate workdir flake consumes `fxtr` as an input"
build, the actual spawner scenario), *before* `nix.py` (chunk 4) and the
spawner arm (chunk 6) build on it. Work through the validation checklist
in `design_notes/12_nix_drv_reference.md` ("Git-flake-ref resolution:
what to validate early") — `rev=` reachability incl.
detached/disconnected commits (the `on_branch=False` knob), bare vs.
worktree repo, `rev=`+`dir=` composition, dirty-tree-is-ignored,
**`--no-update-lock-file` forces the committed lock (no re-locking)**, shared-store
cache hits, the baked-in local `fxtr` input under `--no-update-lock-file` (no
`--override-input`), and `nix eval` of `entryPoint` without forcing the
build.

## Relevant design notes

- `design_notes/12_nix_drv_reference.md` — primary reference: spawn
  ref shape, git-ref resolution, trust model, identity-vs-spawn,
  deprecation layout, orphaned-code removal.
- `design_notes/05_sandbox_bundle.md` — `mkWasmService` internals +
  the manual commit→spawn flow.
- `design_notes/01_flake_schema.md` — `fxtr.services` shape + the
  git-ref consumption pattern + `flakePath`.
- `design_notes/09_migration.md` — the git-repo requirement for code.
- `design_notes/11_failure_and_ux.md` — spawner git-ref failure
  surfaces.

## Relevant existing code

- **`stage3_bundle_and_spawner_scratch.md`** — the code map: exact
  `file:line` anchors for the spawner path, the sandbox-bridge type
  exposure + `.store()` idiom, the nix helpers, and the orphaned-code
  deletions. Read it alongside this plan.
- `projects/fxtr_sandboxing/CODE_OVERVIEW.md` — `spawner.py`,
  `runner.py`, `direct_launcher.py`, `_bundle_materialize.py`,
  `prepare.py`. Dispatch arms + low-level launchers stay; prep modules
  move.
- `projects/fxtr_workstation_agent_plugin/CODE_OVERVIEW.md` — the
  sandbox bridge (`protocols.py` dup pattern, `builtins.py` /
  `_fxtr_mcp_builtin` re-exports) + `host_impl.prepare_package_bundle`
  (the model for Stage 4's `current_commit()`).
- `examples/fxtr_workstation_demo/agent_workdir/README.md` — the
  `.store()`-then-`schedule_invoke` idiom the agent flow follows.
- `nix/` (`CODE_OVERVIEW.md`) — `fxtr_nix_writer`, the hello-world
  layer + probe wiring to strip, `_mkGuestSitePackages`,
  `mkVenv`, the stubbed `mkWasmPackageBundle`; `nix/sandbox_smoke/`
  (the hardcoded abs-path+rev to rework) + the repo-root
  `guest-site-packages-check` target to remove; `nix/py_testing/tests/
  conftest.py` (the existing `nix_build` / `flake_ref` fixtures —
  `prepare_workdir_repo` lives alongside / extends these); `test_sandbox_
  import.py` to migrate onto the tempdir build.
- `/Users/daniel/extgit/spikes/2026_05_15_nix_spike/` — reference for
  the eventual Docker service (deferred).

## End-of-stage check

- `prepare_workdir_repo` produces a clean tempdir workdir repo from a
  template (substituted `fxtr` ref, re-locked, committed); the
  `FXTR_TEST_LIB_FLAKE` override, the `on_branch=False` off-branch case,
  and the uncommitted-edits warning all work.
- `nix build "<workdir-repo>?rev=<c>#fxtr.services.hello_service.sitePackages"`
  produces a flat eryx-runnable site-packages dir; `nix eval` of
  `.entryPoint` returns the expected string.
- `sandbox_smoke` builds through `prepare_workdir_repo` `--no-update-lock-file` (no
  hardcoded path/rev left); the migrated `import fxtr2` check passes;
  the repo-root `guest-site-packages-check` target + direct
  `_mkGuestSitePackages` import are gone.
- Direct test: spawning a `NixWasmServiceDefn` via
  `SandboxSpawnerImpl.spawn` resolves the git ref (`--no-update-lock-file`), builds,
  and launches a working sandbox.
- `run_script` test: a script constructs `NixWasmServiceDefn` in-sandbox
  (proving the type is sandbox-constructible) and spawns it; the
  service responds.
- **`flakePath` coverage:** a unit/integration test spawns a service
  whose flake lives at a repo *subpath* (built via `&dir=<flakePath>`),
  so the subpath resolution is exercised even though the demos keep
  their flake at the repo root.
- The orphaned `.drv`-pointer / closure-scan code is gone; `nix flake
  check` + the trimmed `py_testing` tests are green.
- Old `prepare_package_bundle` path still works (no regressions in
  `basic_test_demo` tests).
