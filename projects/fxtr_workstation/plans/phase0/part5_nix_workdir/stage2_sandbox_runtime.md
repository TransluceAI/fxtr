# Stage 2 — Sandbox runtime cutover

> **Originally:** replace the MCP's `run_script` direct path (which
> today walks `<workdir>/packages/` + reads `<workdir>/preamble.py`)
> with a nix-driven combined sandbox derivation
> (`fxtr.workstation.sandbox`). **This cutover was cancelled** partway
> through the stage — see "Revised direction" immediately below. The
> original scope/chunk text is kept for the record, annotated with
> what changed.

## Revised direction (2026-05-29) — don't nix-control the workstation sandbox

After Chunks 1-3 landed, we decided **not** to put the workstation
sandbox runtime under nix. The `fxtr.workstation.sandbox` attr is
removed from the design and considered unused; the MCP's `run_script`
direct path stays exactly as it is today.

**Why.** Nix-controlling the workstation venv bought us essentially
one thing — dependency-resolved third-party PyPI packages in the
workstation sandbox — and that's a capability:

- the workstation tier doesn't currently support or need (agents use
  `packages/` for local source; anything needing real deps spawns a
  service / bundle entity), and
- the bundle tier (Stage 3, `mkWasmPackageBundle`) *does* keep, via
  its own nix-built guest venv.

Against that thin benefit, the nix cutover added real cost: a
per-`run_script` `nix build` + its failure UX, the wasm32/wasi
marker dance for the workstation venv, and — most importantly — a
new MCP/venv **fxtr2 version-coupling** problem (old Q2.D): once fxtr2
lives in the agent's flake-declared venv, the agent must pin an fxtr2
rev matching the MCP's injected `_fxtr_mcp_builtin`. Keeping the
sandbox host-built means the MCP owns fxtr2 directly (via the
`sandbox_runtime` closure) and that whole class of skew disappears.

**The reframing.** Nix is the *data/config bridge* — entities (Area
04), UI presets (Area 06), and sandboxed bundle venvs (Area 05). The
workstation sandbox's *base runtime* (fxtr2 + adapter + the agent's
local packages + prelude) stays host-controlled. Clean separation of
concerns, and reversible: if rich workstation deps ever become a real
need, we can reintroduce an **optional** `fxtr.workstation.sandbox`
that the MCP uses when present and falls back to the host path
otherwise — no re-architecture required.

**Decisions taken (the Q&A that drove this):**

1. Giving up dependency-resolved third-party PyPI packages in the
   workstation sandbox is acceptable — already unsupported today;
   spawn a service/bundle for deps.
2. `flake.nix` becomes **optional**. It is required only when a script
   calls `retrieve_nix_entity` (Area 04) or when using nix UI presets
   (Area 06). A workdir with just `preamble.py` + `packages/` runs
   fine. The "hard cut" migration (Area 09) softens accordingly.
3. **Delete** `mkSandbox.nix` and **retarget** the eryx smoke test:
   the "does a `_mkGuestSitePackages`-built venv import under eryx
   3.14?" risk is still live for bundle venvs, so the test moves to
   exercising `_mkGuestSitePackages` directly instead of via
   `mkSandbox`.
4. Stage 2 keeps its number/identity (no renumbering); this section
   records that the MCP cutover was planned here and abandoned.

**What survives from Stage 2 (now justified by Stages 3-5):**

- `mkVenv` (uv2nix wrapper) and the internal `_mkGuestSitePackages`
  flat-site-packages extraction — consumed by `mkWasmPackageBundle`
  (Stage 3).
- The wasm32/wasi `environments` + `environ` marker handling baked
  into `mkVenv` / the smoke workspace (Chunk 2) — bundle guests run
  under eryx too, so this is still required.
- `fxtr_workstation_mcp/nix.py` (Chunk 3) — `build_attr` is generic
  and feeds `retrieve_nix_entity` (Stage 4) + `set_preset` (Stage 5).
  The `has_sandbox` field in `eval_flake_structure` is now vestigial
  (drop it, or leave it unread until cleaned up in Stage 4/5).
- The retargeted eryx import smoke test.

**What is dropped / does not happen:**

- `mkSandbox.nix`, the `fxtr.workstation.sandbox` attr, and the
  `sandbox-import-check` target *as a `mkSandbox` test* (retargeted).
- Chunk 4 (the MCP cutover) in its entirety — no factory rewrite to
  bare CPython, no `sandbox_runtime/` deletion, no `build_runner_volumes`
  rewrite, no per-`run_script` nix build trigger, no reshaping the
  demo workdir to drive the sandbox from nix. `sandbox_runtime/`,
  `preamble.py`, and the `packages/` walk all **stay**.
- Chunk 5's `--- nix ---` *per-`run_script` setup header* (there's no
  per-script sandbox build to report). The rest of Chunk 5 (failure
  fences, `.tmp_outputs/` writer) migrates to Stages 4-5, where nix
  eval/build can still fail.

**Concrete code-change checklist (the remaining Stage-2 work):**

- [x] Delete `nix/lib/mkSandbox.nix`; drop its entry from
  `nix/lib/default.nix`. (Also deleted the now-dead
  `sandbox_smoke/prelude.py`.)
- [x] Retarget the smoke test to build `_mkGuestSitePackages` over
  `sandbox_smoke/` and run `import fxtr2` under eryx against that flat
  dir. The repo-root flake target is renamed
  `sandbox-import-check` → `guest-site-packages-check` and imports
  `_mkGuestSitePackages.nix` directly (it's internal, not in
  `lib.fxtr.*`). `test_sandbox_import.py` mounts `$out` directly (no
  `venv/` subdir). Both tests green: `nix.py` unit tests (8) +
  `test_sandbox_import.py` (2, build on virby + eryx import).
- [x] Drop `has_sandbox` from `fxtr_workstation_mcp/nix.py` (eval
  filter, `FlakeStructure` field, `NoFlakeNixError` message reworded
  for the entity path) and its tests.
- [x] Update `nix/CODE_OVERVIEW.md` (no `mkSandbox`, retargeted
  smoke test) + the agent-plugin `CODE_OVERVIEW.md`'s `nix.py` line.

---

## Original scope (for the record — superseded by the section above)

`prepare_package_bundle` is **deliberately left alone** in this
stage — it keeps working via the existing
`fxtr_sandboxing.prepare` machinery; we'll port that in
Stages 3-4.

## Scope

**Helpers.** Two new ones implemented in the fxtr flake (both
stubbed in Stage 1):

- `lib.fxtr.mkVenv { pyproject, ... }` — wraps uv2nix's output
  into the flat site-packages-shaped directory `mkSandbox`
  consumes. Adapts from the working uv2nix setup in
  `/Users/daniel/extgit/spikes/2026_05_21_nix_fxtr_prototype/`'s
  preload + augment workspaces. Exact post-processing pinned
  during this stage's implementation — known concerns:
  - Pointing at uv2nix's `mkVirtualEnv`-shaped output
    (`lib/python3.X/site-packages` is already roughly flat per
    package; we may just expose that subdir).
  - Python version pinning to match eryx's CPython 3.14.
  - Possible trimming of installer metadata / `__pycache__`
    (nice-to-have, not required).
  - Possible build-time pure-python check (nice-to-have).
  The caller's `pyproject.toml` lists fxtr-side deps
  (`fxtr-sandbox-rpc-adapter[fxtr2-portable]`, etc.) as git-deps
  on the fxtr repo, mirroring the spike's approach.
- `lib.fxtr.mkSandbox { prelude, venv }` — builds the combined
  `fxtr.workstation.sandbox` derivation. Output layout:
  `out/prelude.py` (required, may be empty) + `out/venv/`
  (required, may be empty). Both files / dirs always present.

**MCP-side build trigger** (Area 08). New module
`fxtr_workstation_mcp/nix.py` wraps `nix eval` + `nix build`
calls via `anyio.run_process`. Every `run_script` begins with:

1. Stat `flake.nix` existence; friendly error if missing.
2. `nix eval` the flake's structure (cached entity-attr map,
   presence of `fxtr.workstation.sandbox`).
3. `nix build .#fxtr.workstation.sandbox` → resolve to store
   path.

The resulting store path's `prelude.py` is fed to
`script_runner.run_preamble_and_script`. `out/venv/` becomes a
single bind-mount; its path is the only entry in
`sys_path_additions`.

**Sandbox factory simplification.**
`fxtr_workstation_agent_plugin/sandbox/sandbox_runtime/` is
deleted; `SandboxEnvSupervisor` is rewritten to build a bare
eryx-CPython factory with no preloaded packages. fxtr2 +
`fxtr-sandbox-rpc-adapter` come in via the nix-built venv.
`sandbox_factory.build_runner_volumes` is replaced by trivial
"one bind-mount, one sys.path entry."

**Failure UX** (Area 11). `--- nix ---` setup header
prepended to successful `run_script` responses (eval timing +
sandbox cached/duration). Eval / build failures surface as
fenced stderr blocks; long stderr written to
`<workdir>/.tmp_outputs/<ts>_<id>.log` referenced by path in
the message. Friendly errors for "no flake.nix" and
"sandbox attr missing."

**Demo update.** `projects/fxtr_workstation_agent_plugin/examples/
basic_test_demo/agent_workdir/` is reshaped to drive the new
path:

- New `pyproject.toml` listing the deps needed in the sandbox
  venv. At minimum `fxtr-sandbox-rpc-adapter[fxtr2-portable]`
  (provides `fxtr2` + the sandbox RPC adapter the
  `_fxtr_mcp_builtin` injection assumes is importable). Plus any
  packages the current `preamble.py` / scripts import (today's
  `demo_pkg`, `hello_service`, etc. as appropriate).
- New `uv.lock` from `uv lock`. Per `mkVenv`'s contract the
  pyproject's fxtr deps point at the fxtr repo as a git dep.
- New `flake.nix` calling `mkSandbox { prelude = ./prelude.py;
  venv = lib.fxtr.mkVenv { pyproject = ./pyproject.toml; }; }`.
- `preamble.py` moves to `prelude.py` (same content; the rename
  matches `mkSandbox`'s expected layout).
- `packages/` is left as-is — `prepare_package_bundle` callers
  still work because they walk arbitrary paths the agent passes
  in. (It's silently ignored by the MCP's per-script flow.)

Existing tests (`tests/test_mcp_in_memory.py`,
`tests/test_mcp_subprocess.py`) pass against the new path. The
key verification is that sandbox-side scripts can `import fxtr2`
and the `_fxtr_mcp_builtin` adapter machinery functions — i.e.
the fxtr2-from-nix-venv path is actually working end-to-end.

## Sequencing note

This stage leaves `prepare_package_bundle` untouched on purpose
— the cutover for that path lives in Stages 3-4. The MCP's
direct-path simplification is independent of the bundle path,
so both can coexist in this intermediate state.

**Internal ordering (from open-concerns #3 + #6).** This stage
bundles several independent changes (venv build mechanics, MCP
cutover, factory simplification, failure UX). Rather than split
it formally, sequence the work so the venv mechanics land and go
green *first*:

1. **Smoke-test `mkVenv`-in-eryx before anything else.** Build a
   minimal venv with `fxtr-sandbox-rpc-adapter[fxtr2-portable]`
   via `mkVenv`, bind-mount it into a freshly-built eryx sandbox,
   and verify `import fxtr2` works. This is essentially the
   end-of-stage check, done first. uv2nix's normal use case is
   "build a venv for the host," but here the venv runs under
   eryx's CPython 3.14 WASM — subtle issues (wheel arch tags,
   `.dist-info` layout, scripts dirs) could surface. We may be
   able to steer uv toward the right target via
   `--python-platform`. If this doesn't work, we learn before
   gutting `sandbox_runtime`.
2. **Then** do the broader MCP cutover (build trigger, factory
   simplification, `sandbox_runtime` removal, failure UX).

This is the forcing function for getting `mkVenv`'s
post-processing right.

## Stage-2 kickoff Q&A

Captured 2026-05-28 before implementation begins. Tweaks the
spec above where they conflict.

- **Chunk granularity.** Chunks don't need to land at green-tests
  boundaries; aim for "easy to review" instead. Larger chunks
  are fine when the change naturally clusters.
- **Sandbox-guest site-packages vs build-time venv.** Stage 1's
  `mkVenv` produces a host-runnable venv with `/bin/python` on the
  canonical arch — good for *running* writer python during a nix
  build. The eryx guest needs something different: a flat
  site-packages directory of pure-python wheels, no `/bin/python`,
  no interpreter (eryx provides CPython 3.14 inside WASM).
  Cross-checked with `fxtr_sandboxing/runner.py` +
  `direct_launcher.py`: `eryx.SandboxFactory(site_packages=…)`
  and per-spawn `volumes` + `sys_path_additions` are all the
  host-side venv has to deliver — no interpreter binary required.
  Chunk 1 resolved this as an **internal helper**
  `_mkGuestSitePackages.nix` (not in `lib.fxtr.*`) shared between
  Stage 2's `mkSandbox` and Stage 3's `mkWasmPackageBundle`. See
  `stage2_sandbox_runtime_scratch.md` for the full discussion.
- **Wheel arch / `--python-platform`.** Recent uv lockfiles can
  carry multiple platforms; rely on that for pure-python wheels
  and iterate if anything blows up. No upfront platform-pinning
  in the lock.
- **Python 3.14** confirmed for the eryx guest. `mkSandboxVenv`
  defaults to `pkgs.python314` (Stage 1's 3.12 default is for the
  build-time venv).
- **Bridge package location.** Initial direction: the bridge stays
  a host-controlled bind-mount, parallel to the venv mount
  (option (i)). Fallback if (i) hits friction: fold the bridge
  into the agent's venv via a dep on the bridge's importable
  protocols module (option (ii)). `_fxtr_mcp_builtin` injection is
  unchanged either way.
- **Demo migration: `demo_pkg` + `hello_service`.** Both go in
  the new `pyproject.toml` as path-deps so the agent can `import`
  their protocol modules. They stay in `packages/` as
  `prepare_package_bundle` sources (untouched in Stage 2).
- **Flake input for in-repo consumers.** The demo's `flake.nix`
  commits `github:TransluceAI/fxtr` as the `fxtr` input.
  Test fixtures + manual dev runs uniformly apply
  `--override-input fxtr git+file:<repo>` to retarget at the local
  worktree.
- **Smoke-test first.** Chunk 2 adds a `sandbox-import-check`
  (working name) derivation in the repo-root `flake.nix` that
  builds a minimal `mkSandbox` and drives a real eryx sandbox
  over the `${mkSandbox-output}/venv/` subdir, running
  `import fxtr2`. Exercises both helpers end-to-end. This lands
  *before* any MCP rip-out.
- **Setup header always.** `--- nix ---` block emitted at the top
  of every successful `run_script` response, including the
  all-cached steady-state case.
- **Explicit failure-mode tests.** Add unit tests for the four
  failure surfaces (no flake, missing `sandbox` attr, eval
  crash, build crash).
- **`--apply` filter for eval.** One combined `--apply` lambda
  returning `{ hasSandbox; entities = {...}; uiPresets = {...}; }`.
  Iterate the shape during implementation.
- **No `result*` symlinks, ever.** All nix invocations the MCP
  makes go through `--no-link --print-out-paths` (build) or
  `--json` (eval, which doesn't write a symlink anyway). The MCP
  never produces a `result` / `result-*` file in the agent's
  workdir. The demo workdir's `.gitignore` therefore needs only
  `.tmp_outputs/`, not `result*`.

## Implementation chunks

Chunks are sized for "easy to review," not necessarily for
"green tests at each boundary." Intermediate broken states are OK
mid-chunk. Sequenced so the biggest unknowns retire first.

### Chunk 1 — Sandbox-guest site-packages design discussion

Pure design; no production code. Output: a scratch note +
matching plan edits.

**Status: done.** Resolved as a shared internal helper. See
`stage2_sandbox_runtime_scratch.md` for the full option matrix +
decisions. Summary:

- Internal helper `nix/lib/_mkGuestSitePackages.nix` (not
  in `lib.fxtr.*`).
- Build approach: re-use `mkVenv` internally, extract its
  `lib/python3.X/site-packages/` via `cp -RL` to land flat at
  `$out/`. (Followup: lean implementation that skips
  `mkVirtualEnv`.)
- Inherits mkVenv's python-version default; no explicit knob.
- Trusts the agent's pyproject to declare only pure-python deps;
  no build-time check in v0.
- Consumed by `mkSandbox` (Stage 2) and `mkWasmPackageBundle`
  (Stage 3); agent flakes never call it directly.

### Chunk 2 — `_mkGuestSitePackages` + `mkSandbox` + smoke test

Land the helpers and prove they work end-to-end before touching
the MCP.

> **Partly superseded.** `_mkGuestSitePackages` + the wasm-marker
> handling survive (they feed Stage 3's bundle venvs). `mkSandbox` is
> **deleted** and the smoke test is **retargeted** to exercise
> `_mkGuestSitePackages` directly rather than via `mkSandbox` — see
> the revised-direction checklist above. The implementation notes
> below remain accurate for the surviving helpers.

**Status: done.** Build green on the virby builder; eryx smoke
test green locally (`uv run pytest tests/test_sandbox_import.py`
inside `nix/py_testing/`). Key implementation notes for
later chunks:

- The smoke-test workspace reaches its in-repo deps
  (`fxtr-sandbox-rpc-adapter`, `fxtr-sandbox-types`, `fxtr2-portable`)
  via a single `git = "file:///…", rev = "<HEAD>"` source. A plain
  `path = ...` dep tells uv to copy *only* the package directory into
  the build sandbox, which breaks `fxtr2-portable` because its
  hatchling `force-include` reaches `../fxtr2/*`. Path-dep + editable
  also has the gotcha that the child's `[tool.uv.sources]` propagates
  and clobbers workspace-root sources, so we'd lose control of
  resolution. Both problems are sidestepped by the git source. This
  is also closer to how downstream agent workdirs will configure their
  deps (Chunk 4 will use `github:TransluceAI/fxtr` + `rev = …`).
- Eryx's `$TMPDIR/eryx-embedded/` cache occasionally lands in a
  "directory exists but contents pruned" state that crashes
  `SandboxFactory()` with an opaque `InitializationError`. The smoke
  test's conftest auto-repair mirrors
  `fxtr_sandboxing.runner._check_and_maybe_repair_eryx_embedded_dir`
  inline. Worth keeping in mind for Chunk 4's MCP integration — the
  bare-CPython factory the MCP now builds will hit the same issue
  unprotected.
- The smoke venv is **strictly pure-python** by construction. Two
  knobs cooperate:
  - The agent workspace's `[tool.uv]` declares
    `environments = ["sys_platform == 'wasi' and platform_machine
    == 'wasm32'"]` (the restrict-set) +
    `required-environments = […same…]` (so uv guarantees a working
    lock for wasi). uv then picks only `py3-none-any` wheels for
    deps like cbor2 — there are no wasi-tagged C-extension wheels
    on PyPI.
  - `_mkGuestSitePackages.nix` passes `environ = { sys_platform =
    "wasi"; platform_machine = "wasm32"; }` through to uv2nix's
    `mkPyprojectOverlay` (via a new `environ` arg on `mkVenv`).
    Without this, uv2nix's supported-markers compatibility check
    rejects a wasi-only lock when the build host is linux/x86_64.
  Build runs on linux/x86_64; we only ever extract the
  site-packages dir, never run the venv on the host, so retargeting
  the marker eval is safe.
- This was not always the state: the first iteration of the smoke
  test landed with `_cbor2.cpython-312-x86_64-linux-gnu.so` in the
  output. uv has no first-class "prefer py3-none-any" knob (see
  Axis-4 followup), so we tried `[tool.uv] no-binary-package =
  ["cbor2"]` (forces sdist build, but cbor2's setup.py still
  compiled the .so since gcc was available) before landing on the
  `environments` + `environ` combo above.

- New `nix/lib/_mkGuestSitePackages.nix`:
  - Args: `{ workspaceRoot, name ? "fxtr-guest-site-packages",
    system ? canonicalSystem, sourcePreference ? "wheel" }`.
  - Internally calls `mkVenv` over `workspaceRoot`, then runs a
    `pkgs.runCommand` that `cp -RL`s the venv's
    `lib/python3.X/site-packages/.` into `$out/` (dereferencing
    symlinks so the inner venv stays out of the output closure).
  - The "3.X" version is read from the chosen python interpreter
    at nix-eval time, not hard-coded.
  - Not exported via `lib.fxtr.*`; wired into the two consumers
    below via a relative import.
- New `nix/lib/mkSandbox.nix`:
  - Args: `{ prelude, workspaceRoot, name ? "fxtr-sandbox",
    system ? canonicalSystem }` (exact arg shape settled during
    implementation — alternative: `{ prelude, venv, ... }` if it
    feels cleaner to let the caller construct the
    site-packages-derivation themselves).
  - Output: `$out/prelude.py` + `$out/venv/`, both always
    present (empty fine).
  - Replace the `notYet` stub in `nix/lib/default.nix`.
- New `packages.x86_64-linux.sandbox-import-check` (working
  name) in the repo-root `flake.nix`:
  - Builds a minimal `mkSandbox` whose `workspaceRoot` points
    at a tiny `nix/sandbox_smoke/` (or similar) workdir
    declaring `fxtr-sandbox-rpc-adapter[fxtr2-portable]` as the
    only dep and an empty `prelude.py`.
  - The check derivation (or a `py_testing` test) bind-mounts
    `${sandbox-out}/venv/` into a fresh eryx sandbox via the
    `direct_launcher` path (or its primitive equivalent) and
    runs `import fxtr2`. Exercises both `_mkGuestSitePackages`
    and `mkSandbox`.
  - Run via the existing virby linux builder.
- Update `nix/CODE_OVERVIEW.md` for the two new helpers
  and the smoke-test target. Note `_mkGuestSitePackages` as
  "internal helper" so future readers don't expect it under
  `lib.fxtr.*`.

### Chunk 3 — MCP nix wrapper module

Pure new code; no rip-out.

**Status: done.** `fxtr_workstation_mcp/nix.py` exists with the
two surface functions + four typed exceptions; nine tests cover
eval happy + failure paths and build happy + failure paths.
Notes for later chunks:

- The eval `--apply` filter is a multi-line nix expression stored
  as a module-level `_EVAL_APPLY` constant. It walks the `fxtr`
  attrset extracting only `type` + `layerKey` per entity/preset
  and checks `workstation ? sandbox` for presence — never touches
  `.layer`, so no derivation realization is forced. The
  `test_eval_layer_not_built` test pins this property with a
  `layer = throw …` entity that would fail eval if the filter
  ever regressed to forcing it.
- Flake refs use `path:<absolute>` form, NOT `git+file://`.
  Agent workdirs aren't necessarily git repos, and even if they
  are, agents iterate on uncommitted files between
  `run_script` calls — `path:` reflects on-disk state without
  needing commits.
  - **Revised (2026-05-30):** Stage 4 switches this to the **bare
    workdir path** (no `path:` prefix), so nix auto-selects
    `git+file://` in a repo (cleaner tracked-only copies, cache-shared
    with the code path) and falls back to `path:` with no repo — still
    **non-frozen**, so tracked-file edits are picked up uncommitted;
    new files need `git add` (the MCP surfaces a count notice). The
    "iterate on uncommitted files" goal is preserved for *tracked*
    files. See `stage4_retrieve_nix_entity.md` + notes 01/04/08.
- "Sandbox attr missing" is NOT raised from `nix.py` —
  `eval_flake_structure` just returns `has_sandbox=False`. Chunk 4
  (run_script wiring) is responsible for treating that as the
  distinct structural error Area 11 specifies. Keeping the policy
  out of `nix.py` lets the same wrapper serve other consumers
  later (e.g. a uiPresets-presence check in Stage 5).
- `nix build` always uses `--no-link --print-out-paths`. No
  `result*` symlinks are ever created in the agent's workdir.
- `--apply` filter shape may want extending in Stage 5 (uiPresets
  is already there structurally but unused). Q8.A is settled as
  "iterate as we go" — current shape matches the design note.

- New `fxtr_workstation_mcp/nix.py`:
  - `eval_flake_structure(working_dir)` →
    `nix eval --json .#fxtr --apply <combined-filter>`. Returns
    a typed dataclass capturing `{ has_sandbox, entities: dict,
    uiPresets: dict }` (uiPresets staged in but unused until
    Stage 5).
  - `build_attr(working_dir, attr_path)` →
    `nix build --no-link --print-out-paths .#<attr>`. Returns the
    resolved store path string. (If we end up needing cached-vs-
    fresh metadata for the setup header, switch to
    `--no-link --json` and parse.)
  - Captures + surfaces stderr; raises typed errors for the four
    failure cases.
  - All shell-outs via `anyio.run_process`.
- Unit tests against a small tmp-dir fixture flake — happy path
  + each typed-error path. Gate on nix + linux-builder presence
  (re-use the Stage-1 conftest pattern).
- Update `fxtr_workstation_agent_plugin/CODE_OVERVIEW.md` to
  mention the new module.

### Chunk 4 — MCP cutover (the heavy chunk)

> **CANCELLED** by the revised direction. None of this happens:
> `sandbox_runtime/`, `preamble.py`, the `packages/` walk, the eryx
> factory, and `build_runner_volumes` all stay as they are today. The
> demo workdir is *not* reshaped to drive the sandbox from nix (it may
> later gain a `flake.nix` purely to demo entities/presets, but that's
> Stage 4-6). Original text retained below for the record.

The actual refactor. Intermediate broken states are OK
mid-chunk.

- **Demo workdir reshaped** at
  `projects/fxtr_workstation_agent_plugin/examples/basic_test_demo/
  agent_workdir/`:
  - `preamble.py` → `prelude.py` (content unchanged).
  - New `flake.nix` driving `lib.fxtr.mkSandbox` with
    `prelude = ./prelude.py` and `venv = lib.fxtr.mkSandboxVenv {
    workspaceRoot = ./.; }`. `fxtr` input committed as
    `github:TransluceAI/fxtr`.
  - New `pyproject.toml`:
    `fxtr-sandbox-rpc-adapter[fxtr2-portable]` (via the fxtr
    git-dep) + path-deps on `packages/demo_pkg` and
    `packages/hello_service`. Generate `uv.lock`.
  - New `.gitignore` excluding `.tmp_outputs/`.
  - `packages/` untouched.
- **Sandbox factory simplified.**
  - Delete
    `fxtr_workstation_agent_plugin/sandbox/sandbox_runtime/`.
  - Rewrite `sandbox_factory.bootstrap_factory` to build a bare
    eryx CPython factory (no `prepare`, no
    `build_site_packages_for_bundle`, no cache_dir on this path).
  - Replace `build_runner_volumes` with a small helper that
    accepts the nix-built sandbox store path and returns
    `(volumes, sys_path_additions)`: bridge bind-mount stays
    (host-controlled, per Q&A); venv goes as a second bind-mount
    with one matching `sys_path_additions` entry; warnings list
    drops away (no more `packages/` walk).
  - `SandboxEnvSupervisor` simplifies (less to bootstrap; CMs +
    lifecycle unchanged in shape).
- **`_run_script_primary` wired to nix.**
  - Stat `flake.nix`; absent → friendly error (text from Chunk 5).
  - `nix.eval_flake_structure` → typed struct.
  - `has_sandbox is False` → structural error (text from Chunk 5).
  - `nix.build_attr(..., "fxtr.workstation.sandbox")` → store
    path. Read `{store}/prelude.py` text; build volumes / sys-path
    against `{store}/venv/`.
  - Hand off to `connection.run_script` as before.
- **Tests.** `tests/test_mcp_in_memory.py` +
  `tests/test_mcp_subprocess.py` updated for the new workdir
  layout. Conftest gains a helper that runs nix invocations with
  `--override-input fxtr git+file:<repo>` so tests don't need
  network. Mark tests requiring the linux builder accordingly.
- Update `CODE_OVERVIEW.md`s (workstation MCP package + nix
  extras if file tree shifts).

### Chunk 5 — Failure UX + `.tmp_outputs/` writer

> **Mostly deferred to Stages 4-5** by the revised direction. With no
> per-`run_script` sandbox build, the `--- nix ---` setup header and
> the no-flake / missing-`sandbox` friendly errors no longer apply
> here. The reusable parts — fenced `nix eval` / `nix build` error
> blocks and the `.tmp_outputs/<ts>_<id>.log` writer — move to where
> nix actually runs: `retrieve_nix_entity` (Stage 4) and `set_preset`
> (Stage 5). Original text retained below for the record.

Polish on top of Chunk 4.

- `--- nix ---` setup header in every successful `run_script`
  response: `eval: <time> (cached?)` + `sandbox:
  cached`/`<duration>`. Cached-vs-fresh detection mechanic
  settled here — likely "check whether
  `outputs.out` is already realized via
  `nix path-info` before invoking `build`," or read `--json`
  build output.
- Friendly errors:
  - "No flake.nix in working dir <path>."
  - "flake.nix evaluates, but fxtr.workstation.sandbox is not
    declared."
- Fenced blocks for `nix eval` / `nix build` failures (full
  stderr inline, pointer to dump file).
- Small `.tmp_outputs/` writer (in `fxtr_workstation_mcp`):
  `<workdir>/.tmp_outputs/<timestamp>_<id>.log`; lazy-create
  the dir.
- Tests for each failure surface (the no-flake / missing-attr
  ones don't need nix invocation at all; eval/build crashes
  use small intentionally-broken fixture flakes).

### Chunk 6 — Polish

- Final pass on `CODE_OVERVIEW.md`s (workstation MCP,
  workstation if touched, `nix`).
- Audit: every `nix` invocation the MCP makes uses `--no-link`.
- `basic_test_demo/README.md` reflects the new workflow.
- Note any deferred items at the bottom of this plan per the
  package CLAUDE.md convention.

### Sequencing rationale

Chunks 1+2 retire the largest unknown — "does
`mkSandboxVenv`-built output actually run under eryx?" — before
any MCP code gets rewritten. Chunk 3 lands the new nix wrapper
in isolation; Chunk 4 then cuts the MCP over (and is the only
chunk that depends on both 2 and 3). Chunks 5 and 6 are
additive polish on top of a working cutover.

## Relevant design notes

- `design_notes/02_prelude_and_venv.md` — primary reference.
- `design_notes/08_build_trigger.md` — cadence + nix wrapper +
  no-stat-shortcut decision.
- `design_notes/11_failure_and_ux.md` — setup header,
  `.tmp_outputs/`, error fences.
- `design_notes/09_migration.md` — hard-cut stance + legacy
  files silently ignored.

## Relevant existing code

- `projects/fxtr_workstation_agent_plugin/CODE_OVERVIEW.md` —
  `SandboxEnvSupervisor`, `sandbox_factory.build_runner_volumes`,
  `script_runner.run_preamble_and_script`, the `packages/`
  layout convention.
- `projects/fxtr_workstation_agent_plugin/sandbox/sandbox_runtime/`
  — what gets deleted.
- `projects/fxtr_sandboxing/CODE_OVERVIEW.md` —
  `direct_launcher.py` + `runner.run_in_factory` (the factory
  building still happens here, just simpler now).

## End-of-stage check

> Revised to match the reverted direction. `basic_test_demo` and the
> MCP `run_script` path are **untouched** in Stage 2 — they continue
> to run via the existing host-built sandbox, so there's nothing new
> to verify there. What this stage must leave green:

- `mkVenv` / `_mkGuestSitePackages` build on the virby builder and
  produce a flat, pure-python site-packages dir.
- The retargeted eryx smoke test passes: a `_mkGuestSitePackages`-built
  dir bind-mounted into a fresh eryx 3.14 sandbox can `import fxtr2`.
  (This is the forcing function for uv2nix-output-postprocessing
  issues, and it's the capability Stage 3's bundle venvs rely on.)
- `fxtr_workstation_mcp/nix.py` (`eval_flake_structure` + `build_attr`)
  passes its unit tests — it's the wrapper Stages 4-5 consume.
- `mkSandbox.nix` and the `fxtr.workstation.sandbox` attr are gone;
  no flake or test references them.
- The MCP's existing direct path, `sandbox_runtime`, `preamble.py`,
  the `packages/` walk, and `prepare_package_bundle` are all
  unchanged and still work.

## Deferred / moved out of Stage 2

Captured here per the package CLAUDE.md convention; the full rationale
is in "Revised direction" above.

- **Failure UX** (Chunk 5: fenced `nix eval` / `nix build` error
  blocks + the `.tmp_outputs/<ts>_<id>.log` writer) → **Stage 4**
  (`retrieve_nix_entity`), where it lands concretely as
  `NixEntityBuildFailed` + the dump-file writer (MCP-side). **Stage 5**
  (presets) builds on its own workstation-side nix wrapper and surfaces
  preset build/eval failures via the dispatcher (`set_preset` raises),
  not via this MCP-side `.tmp_outputs` writer. The per-`run_script`
  `--- nix ---` setup header is dropped entirely (no per-script nix
  work to report).
- **`_mkGuestSitePackages` has no in-tree consumer yet.** Its real
  caller is `mkWasmPackageBundle` (**Stage 3**); until then it's
  exercised only by the repo-root `guest-site-packages-check` smoke
  target (imported directly, not bound in `lib.fxtr.*`). The
  `flake.nix` `TODO(mkWasmPackageBundle)` flags the reconciliation.
- **Demo `flake.nix` + READMEs.** No Stage-2 change — the demos keep
  their host-built sandbox. `basic_test_demo`'s `flake.nix` (the
  `hello_service` entity) lands in **Stage 3**; its retrieve-based
  flow + README update in **Stage 4**; `fxtr_workstation_demo`'s
  `flake.nix` + README in **Stage 6**.
- **MCP/venv fxtr2 version-check (old Q2.D).** Moot under the revised
  direction — the MCP owns the sandbox's fxtr2 (host-built), so there
  is no agent-flake-vs-MCP skew to guard against.
