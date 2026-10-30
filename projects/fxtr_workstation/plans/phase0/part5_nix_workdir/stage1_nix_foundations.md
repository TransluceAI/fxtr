# Stage 1 — Nix foundations

Stand up the nix infrastructure the rest of Part 5 builds on. No
consumer in the workstation yet — this stage's deliverable is a
working flake in the fxtr repo that downstream stages import.

> **Already landed.** Note (2026-05-30): the code/entity decoupling
> **orphans** this stage's synthetic closure-scan validation (chunk 4 —
> `HelloWorldEntity.drv_ref`, `writers.emit_drv_ref_sidecar`, the
> `nix-store --query --references` / `nix derivation show` tests). It
> was built to de-risk the old `.drv`-pointer bundle, which no longer
> exists (code travels via git — see `overview.md` / `12_nix_drv_reference.md`).
> That code is **removed in Stage 3**; nothing new depends on it. The
> rest of Stage 1 (`mkVenv`, `mkEntityFromPython`, the `fxtr_nix_writer`
> hello-world scaffold, `_mkGuestSitePackages`) stands unchanged.

## Scope

Create a flake at the fxtr repo's top level. The flake's outputs:

- An **internal build-time venv** (fxtr2-bearing) used as a build
  input by the writer derivations. Built via `mkVenv` (the uv2nix
  `loadWorkspace → mkVirtualEnv` mechanism, the spike's pattern in
  `/Users/daniel/extgit/spikes/2026_05_21_nix_fxtr_prototype/`)
  pointed at **`fxtr_nix_writer`'s own uv workspace** under
  `nix/fxtr_nix_writer/`. That workspace declares fxtr2 as a
  *relative path-dep* — `fxtr2 = { path = "../../../v2" }` — which,
  because the flake sits at the **repo root** and the whole repo is
  copied into the nix store, resolves to `<store>/v2` *inside* the
  store-copied flake source. fxtr2's own upward `fxtr = { path = ".." }`
  likewise resolves to `<store>/` (the repo root). This is the proven
  spike pattern (`nix_prototype_augment` → `../lib`); no git-dep, no
  auth, no rev pinning, and `path:.` picks up uncommitted `v2/` edits.
  (Refines OQ1a, which framed the workspace as `./v2` itself; see
  `part2_nix_exploration_findings.md` for why its failures were `..`
  escaping a *shallow* store root, not path-deps per se.) "Build-time"
  = it is evaluated and built by the **host's nix** and *executed
  during a nix build* (on the canonical system, `x86_64-linux`) to
  write layered stores — as opposed to a **sandbox guest venv**, which
  nix builds as data but never runs (the spawner runs it inside an
  fxtr sandbox guest). It is not a user-facing package — there is no
  standalone `.#fxtr2` output (OQ1b); the `write_hello_world`
  round-trip below is the concrete thing that exercises this venv.
  (Downstream *user* flakes, being outside the fxtr repo, get fxtr2
  via a uv **git-dep** through this same `mkVenv` — path-dep for
  in-repo lib workspaces, git-dep for out-of-repo consumers.)
  - This internal build-time venv and Stage 2's eryx-targeting
    `mkVenv` share the uv2nix mechanism but differ in interpreter +
    post-processing (canonical-system CPython vs CPython-3.14-WASM,
    pure-python flat layout). Don't pin them as permanently separate —
    Stage 1 builds only what it needs; Stage 2 decides what to
    generalize (possibly a single `mkVenv` configurable for build-time
    vs eryx-guest). (OQ1c.)
  - **Vocabulary** (used throughout Part 5): *internal build-time
    venv* (lib's own, above); *user-authored build-time venv*
    (downstream `mkVenv`/`mkEntityFromPython` input, runs in the
    author's flake build on their chosen `system`); *sandbox guest
    venv* (built by nix, run only inside fxtr's sandbox guests).
- A `lib.fxtr.*` helper namespace. Two helpers are **implemented**
  in this stage:
  - `mkVenv { workspaceRoot, ... }` — pulled forward from Stage 2
    (OQ1c) so `mkEntityFromPython` builds against the real,
    final-shaped venv mechanism rather than a throwaway. Runs uv2nix
    (`loadWorkspace → mkVirtualEnv`) over a uv workspace and returns
    the venv derivation. Used three ways this stage: build
    `fxtr_nix_writer`'s build-time venv, build `py_testing`'s venv for
    the `mkEntityFromPython` test, and (later stages) serve as the
    user-facing venv builder. Stage 1 targets canonical-system
    CPython; Stage 2 decides what to generalize for the eryx-guest
    case (interpreter + flat-layout post-processing) — don't pin them
    as permanently separate.
  - `mkEntityFromPython { venv, fromPython }` — runs a python
    derivation that imports the object named by `fromPython` from
    `venv` and writes it into a fresh layered file store; returns the
    entity attr. `fromPython` resolves (via a dotted `module:attr` /
    `module:Class.attr` getattr chain) to an **entity object** — in
    practice a type-id `FxtrID` from `define_type(...)`, exposed
    either as a module-level attribute (`pkg.mod:my_type_id`) or as a
    class's `_fxtr_known_type_id`
    (`pkg.mod:MyClass._fxtr_known_type_id`) — not a class. The helper
    pins that id into the layer (the `define_type` call already stored
    its `FxtrBasicType` entity in the in-process scope, so the id is
    walkable). Used by Area 06 for type entities in `for_type_id`
    slots.
- All other helpers (`mkSandbox` — later deleted, see
  `stage2_sandbox_runtime.md` "Revised direction"; `mkWasmPackageBundle`,
  `mkRendererEntity`, `esbuildRendererEntity`,
  `mkDispatcherPreset`, `mkAll`, eventual `mkDockerPackageBundle`)
  are **stubbed**: defined as functions whose body `throw`s when
  *applied* (so the `lib.fxtr` namespace still evaluates and
  `nix flake check` passes) with a TODO message pointing at the stage
  that implements them. This lets later-stage callers reference the
  helpers by name without compile errors and gives us a single place
  to track what's left.
- A **`fxtr_nix_writer` package** (its own uv workspace at
  `nix/fxtr_nix_writer/`, fxtr2 via the relative path-dep
  above) + its shared nix builder. This is the scaffolding for the
  no-venv writer helpers (`mkWasmPackageBundle`, `mkRendererEntity`,
  `mkDispatcherPreset`) that get added in Stages 3 and 5. Lands in
  Stage 1 with one trivial entry point — `write_hello_world` — so we
  work out the packaging shape, nix-derivation invocation pattern,
  env-var / JSON-arg passing convention, `$out`-write contract, and
  sidecar-symlink emission *before* we start layering
  writer-kind-specific logic on top. fxtr2 (pulled in transitively
  through the path-dep) is the only Python dep; the package's
  `mkVenv`-built venv *is* the internal build-time venv. Distinct from
  the writer mechanism inside `mkEntityFromPython` (which runs in the
  caller-supplied venv; the two scaffoldings are separate by
  necessity — see Stage 3 for the rationale).
- A **`py_testing` package** (`nix/py_testing/`, its own uv
  workspace with relative path-deps `fxtr_nix_writer = { path =
  "../fxtr_nix_writer" }` + `fxtr2 = { path = "../../../v2" }`). It
  collects the Python side of the nix-integration tests
  (`nix/py_testing/tests/`) and the throwaway entity type(s)
  those tests need. The tests are the **pytest-shells-to-nix** harness
  (below): they shell out to `nix build`, then open the output with a
  fxtr2 layered-store reader and assert on the round-trip.
  Deserializing `write_hello_world`'s entity needs that entity's class
  importable, so the pytest venv depends on `fxtr_nix_writer`;
  `mkEntityFromPython`'s test points `mkVenv` at this package and
  imports a type-id defined here.

No `mkEntity` helper — entity attrs are three-field attrsets
constructed by the producing helpers themselves (or written
inline). See `design_notes/03_entity_declarations.md` Q3.A.

- **Validate the closure-scan mechanic (Q12.D; from
  open-concerns #2).** Fold a `nix-store --query --references`
  check into the `write_hello_world` task. Because the real
  `.drv`-pointer consumer doesn't exist until Stage 3, this test
  is **synthetic but faithful**: embed a *real* trivial
  derivation's `drvPath` (run through
  `builtins.unsafeDiscardOutputDependency`) inside the entity's
  CBOR payload, build the layer, and assert nix's closure scanner
  reports that path. A *fabricated* store-path string would never
  be detected — the scanner only reports paths already in the
  derivation's input closure — so it must be a real drv. Repeat for
  the sidecar `out/_drv_refs/<name>.drv` symlink fallback. (See
  "Implementation order" below for the full rationale.) Cheap to do
  here, expensive to discover broken in Stage 3 — the whole
  bundle-pinning story (Area 12) rests on this mechanic behaving
  as assumed.

Decided (see `stage1_open_questions.md`):

- **Flake location:** repo root. The build-time venv is built by
  `mkVenv` over `fxtr_nix_writer`'s own workspace, which path-deps
  `../../../v2`; the repo-root flake means that resolves inside the
  store copy (refines OQ1a's `workspaceRoot = ./v2`). No git-dep for
  in-repo lib workspaces.
- **fxtr2:** internal build-time tool, not user-facing; no
  standalone output (OQ1b/c).
- **`mkVenv` pulled forward** from Stage 2 into Stage 1 (implemented,
  not stubbed) so `mkEntityFromPython` builds against the real venv
  mechanism.
- **Per-system handling (OQ3):** principled single architecture. No
  `${system}` namespacing of `fxtr.*`; `lib.fxtr` carries a
  `canonicalSystem` default (`x86_64-linux` for the prototype) with
  an optional per-helper `system` override. The flake also exposes
  `lib.fxtr.pkgs` / `lib.fxtr.pkgsFor` so downstream flakes need no
  nixpkgs input. Rationale in `overview.md` ("A principled single
  architecture") + OQ3.
  - **Builder requirement (retired).** Pinning canonical =
    `x86_64-linux` means every Stage 1 build (`write_hello_world`
    round-trip, the synthetic closure-scan test, `mkEntityFromPython`)
    runs on a linux builder even when developing on macOS. This is now
    set up: a **virby** on-demand vfkit NixOS VM (via nix-darwin)
    serves `aarch64-linux,x86_64-linux`, verified end-to-end. Setup
    log: `/Users/daniel/extgit/spikes/2026-05-26-nix-darwin-linux-builder/`.
- **Consumer dev-input:** in-repo consumer flakes (example/test
  workdirs, Stage 2+) keep `github:TransluceAI/fxtr` committed and
  override `fxtr` to the local repo at test time via a **git** ref
  (`--override-input fxtr git+file:<repo>`), not `path:` — see
  `design_notes/10_nix_infrastructure.md` Q10.E for why `path:` is
  avoided. Exact lock-bootstrapping (whether a committed `flake.lock`
  needs the original input to resolve) is verified when the first
  consumer flake lands (~Stage 2 demo). (Supersedes OQ2's `path:`.)

Settle in-implementation: exact uv2nix wiring; CI gating (does CI
need nix installed *plus a linux builder* to test the flake's
helpers — likely yes, given the canonical-arch pin above). The
linux-builder mechanism is settled (virby, above).

## Why first

Every subsequent stage imports `lib.fxtr.*`. Stubbed helpers let
later stages add real bodies without simultaneously needing to
restructure callers. `mkEntityFromPython` specifically lands here
because Stage 5 (UI presets) needs it for type-entity layers, and
implementing it early lets us write tests against a real helper.

## Implementation order (chunk by risk)

The helper code is small; nearly all the risk is in the nix/uv2nix
integration. So we chunk to **front-load the two things that can sink
the stage if found late** (the linux builder, and packaging fxtr2 via
uv2nix through `fxtr_nix_writer`'s workspace and its path-dep chain),
and only build helper logic once those are retired.

0. ✅ **Linux-builder gate.** Build one trivial `x86_64-linux` derivation
   from this repo's flake via the virby builder — no fxtr code. The
   builder itself is already stood up (see above); this just confirms
   it's reachable through the `path:.` flow we'll use.
   *Done:* `packages.x86_64-linux.smoketest` built on `ssh-ng://virby-vm`
   → "built on Linux 6.6.129 x86_64".
1. ✅ **Flake skeleton.** Inputs (nixpkgs, uv2nix, pyproject-nix,
   build-systems); `lib.fxtr` with `canonicalSystem` / `pkgs` /
   `pkgsFor`; **all** helpers (incl. `mkVenv`) stubbed to throw-on-
   apply; the chunk-0 smoke derivation as a buildable output. Goal:
   `nix flake check` + `path:.` smoke build + a "stub raises when
   applied" check pass.
   *Done:* `flake.nix` + `nix/lib/default.nix` + committed
   `flake.lock`. `nix flake check path:.` evaluates `lib`/`packages`
   clean; `lib.fxtr.canonicalSystem` → `"x86_64-linux"`; applying a
   stub throws its TODO message.
2. ✅ **`mkVenv` + internal build-time venv — the keystone.** Implement
   `mkVenv` and stand up `fxtr_nix_writer`'s uv workspace (fxtr2 via
   the `../../../v2` path-dep); prove it with a derivation that only
   runs `python -c "import fxtr2"`. The residual risk is narrow: the
   only path-dep that escapes *above* the repo root is fxtr v1's
   optional `picoview` (`extra == 'ui'` in `v2/uv.lock`), which
   `workspace.deps.default` should exclude — confirm uv2nix never
   computes its `projectRoot`. (`../examples/...` and
   `../spikes/thingwatcher` are dev-group + in-repo; the `mashumaro`
   git-dep is handled by uv2nix as a git source.) Retire before
   anything is built on top.
   *Done:* `nix/lib/mkVenv.nix` + `nix/fxtr_nix_writer/`
   (its pyproject redeclares fxtr2's non-PyPI sources — fxtr v1 path +
   the `mashumaro` git fork — since uv only honors the workspace
   root's `[tool.uv.sources]`). `packages.x86_64-linux.fxtr2-import-check`
   builds the full fxtr2 venv on virby and runs `import fxtr2` clean;
   `picoview` stayed an unforced thunk (not in `deps.default`).
3. ✅ **`fxtr_nix_writer` + shared builder + `write_hello_world`** →
   round-trip read test (in `py_testing`).
   *Done:* `fxtr_nix_writer` Python (`entities` / `writers` / `cli`, console
   script `fxtr-nix-writer`) + shared builder
   `nix/lib/runFxtrWriter.nix` (writer spec handed over as a
   `__structuredAttrs` `writerSpec` attr, read from `$NIX_ATTRS_JSON_FILE`;
   `$out` is the layer dir). The console script dispatches on `spec.kind`. `packages.x86_64-linux.hello-world-layer`
   builds a self-contained layer (pins `hello` → a `HelloWorldEntity`,
   with its type closure walked in); `py_testing`'s pytest nix-builds it
   via a git flake ref and reads the message back.
4. ✅ **Synthetic closure-scan validation** (faithful form — see below).
   *Done:* `hello-world-layer` pins two real probe `.drv`s via
   `builtins.unsafeDiscardOutputDependency` — one embedded in the entity
   CBOR (`HelloWorldEntity.drv_ref`), one as the `_drv_refs/hello.drv`
   sidecar symlink (`writers.emit_drv_ref_sidecar`). `nix-store --query
   --references` over the layer reports both, validating each detection
   path independently; the CBOR-embedded `.drv` substring survives
   serialization intact (the property Area 12 assumes — Q12.D). A third
   test (`nix derivation show`) asserts the probes are input *sources*,
   not input derivations, so the layer never builds the probe outputs —
   confirming `unsafeDiscardOutputDependency` itself, not just that the
   `.drv` is referenced.
5. ✅ **`mkEntityFromPython`** real impl, tested via `mkVenv` over
   `py_testing` importing a type-id defined there → round-trip read
   test.
   *Done:* `nix/lib/mkEntityFromPython.nix` + standalone writer
   `nix/lib/mk_entity_from_python_writer.py` (runs in the caller's
   venv, resolves `module:attr` → FxtrID/EntityRef/Entity, pins it from
   `GLOBAL_SCOPE`; args via `__structuredAttrs`, default `layerKey="entity"`).
   `py_testing/sample_type.py` exposes `sample_type_id = define_type(...)`;
   `packages.x86_64-linux.sample-type-entity` writes it, and the test reads
   it back as a `FxtrBasicType`, asserting `ref.entity_id == sample_type_id`
   (content-addressing reproduces across the build) + the type fields.
6. ✅ Create `nix/CODE_OVERVIEW.md` (current-state only, per
   `v2/notes/code_overview_guide.md`: one line per file in the tree
   listing, no stage/chunk narrative — only plan-file links / TODOs).
   *Done:* covers the mental model, file tree, helper inventory (live +
   stubbed), the writer convention + `.drv`/closure-scan mechanic, the
   test harness, and "where to make changes". Conftest refactor folded
   the duplicated `nix_build`/`flake_ref` test helpers into a fixture.

### Decisions folded in

- **Package location.** `fxtr_nix_writer` and the flake's nix helper
  sources live under **`nix/`**.
- **Test harness.** **pytest-shells-to-nix**: tests run
  `nix build .#<attr>` from the colocated jj↔git worktree (a git flake
  reference, see `design_notes/10_nix_infrastructure.md` Q10.E), then
  open the output with a fxtr2 layered-store reader and assert on the
  round-trip. (CI consequence, Q10.C: CI needs nix *and* a reachable
  linux builder.)
- **Closure-scan test is faithful, not fabricated.** Nix's reference
  scanner only reports paths already in the derivation's input
  closure — a fabricated `/nix/store/<hash>-x.drv` string would never
  be detected, so it would prove nothing. Instead mirror the Stage 3
  mechanic: build a *real* trivial derivation (e.g. one that writes a
  text file), take its actual `drvPath`, run it through
  `builtins.unsafeDiscardOutputDependency`, embed *that* string in the
  hello-world entity's CBOR, build the layer, and assert `nix-store
  --query --references` reports it. Repeat for the sidecar
  `out/_drv_refs/<name>.drv` symlink. This is effectively a
  `mkWasmPackageBundle` dry run minus the package.
- **fxtr2 via relative path-dep, not git-dep (in-repo).** Because the
  flake is at the repo root, `fxtr_nix_writer`'s workspace reaches
  fxtr2 with a plain `fxtr2 = { path = "../../../v2" }` that resolves
  inside the store copy (the `augment → ../lib` pattern). This avoids
  the spike's git-dep auth/push/rev-pinning friction and (built from
  the colocated worktree via a git ref) picks up dirty `v2/` edits.
  Git-deps are reserved for *downstream* user flakes (outside the
  repo), via the same `mkVenv`.
- **Build-time venv scope.** Aim for a smallish fxtr2-bearing venv
  (`workspace.deps.default`, like the spike) but don't fight hard to
  trim it — the dependency surface is getting refactored anyway, and
  we may later depend on **`fxtr2-portable`** (a slimmed-down,
  fewer-deps fxtr2 variant meant for sandbox execution) instead.
  `fxtr_nix_writer`'s own workspace only declares fxtr2, so the
  example/thingwatcher dev-group editables (and the `ui`-extra
  `picoview`) shouldn't enter its lock at all; confirm at chunk 2.

## Relevant design notes

- `design_notes/01_flake_schema.md` — flake's top-level shape +
  MCP query pattern.
- `design_notes/03_entity_declarations.md` — entity attr shape
  + the layered-file-store convention.
- `design_notes/10_nix_infrastructure.md` — primary reference for
  this stage. Helper inventory, fxtr2-build approach, dodging
  the spike's path-walk problem.

## Relevant existing code

- `/Users/daniel/extgit/spikes/2026_05_21_nix_fxtr_prototype/` —
  the working uv2nix prototype to adapt from. Its in-workspace path-dep
  (`nix_prototype_augment` → `nix_prototype_lib = { path = "../lib" }`)
  is the exact upward-`..`-inside-the-store pattern we rely on; it
  git-deps fxtr/fxtr2 only because that spike's flake was *not* at the
  fxtr repo root.
- `/Users/daniel/extgit/spikes/2026-05-26-nix-darwin-linux-builder/`
  — the virby linux-builder setup log.
- `v2/plans/phase3/stage11_nix_layers/part2_nix_exploration_findings.md`
  — earlier findings; the path-walk failures there were `..` escaping
  a *shallow* store root, which the repo-root flake avoids.
- `v2/fxtr2/data/layered_file_store/` (per `v2/CODE_OVERVIEW.md`)
  — the layered file store reader/writer the helpers produce
  output for.

## End-of-stage check

- `mkVenv` builds `fxtr_nix_writer`'s build-time venv (exercised via
  `write_hello_world` below); no standalone `.#fxtr2` output.
- `lib.fxtr.mkEntityFromPython { venv = mkVenv { workspaceRoot =
  ./nix/py_testing; }; fromPython = "<py_testing type-id>"; }`
  produces a layered-file-store output that a fxtr2-side reader can
  open and read back as the expected type entity.
- `fxtr_nix_writer`'s `write_hello_world` entry point, invoked
  through the shared nix builder, produces a layered-file-store
  output containing the test entity. Round-trip read (in `py_testing`)
  confirms it.
- The synthetic closure-scan check passes: `nix-store --query
  --references` over the hello-world layer reports the embedded
  `.drv`-shaped string (and/or the sidecar symlink target).
- Stubbed helpers cleanly raise when applied.
