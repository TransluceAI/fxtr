# Nix infrastructure & fxtr2 source

Where the helpers live, how user flakes consume them, and how the
spike's uv2nix-path-walk problem gets dodged.

> **Revised (2026-05-30):** helper inventory updated for the
> code-is-git-referenced redesign — `mkWasmPackageBundle` →
> `mkWasmService` (builds a `fxtr.services.<name>` attr, no entity);
> `esbuildRendererEntity` → `esbuildRenderer` (ESM derivation, no
> entity); `mkRendererEntity` / `mkDispatcherPreset` **removed** (the
> dispatcher wraps/assembles at runtime).
> See `05_sandbox_bundle.md`, `06_ui_presets.md`,
> `12_nix_drv_reference.md`.

## Shape

A nix flake lives at the **fxtr repo root** (OQ1a / Q10.A). Its job:

- Provide an **internal** fxtr2-bearing venv, used only as a build
  input to lib-internal writer derivations — *not* a user-facing
  output. There is no `.#fxtr2` (OQ1b). Concretely this is `mkVenv`
  pointed at `fxtr_nix_writer`'s own uv workspace
  (`nix/fxtr_nix_writer/`), which declares fxtr2 as a relative
  path-dep `{ path = "../../../v2" }`. Because the flake is at the
  repo root, the whole repo is copied into the nix store, so that
  upward `..` resolves to `<store>/v2` *inside* the copy — the same
  in-store path-dep pattern the spike's `nix_prototype_augment →
  ../lib` relies on. This is what dodges the uv2nix path-walk problem
  documented in
  `v2/plans/phase3/stage11_nix_layers/part2_nix_exploration_findings.md`
  (whose failures were `..` escaping a *shallow* store root, not
  path-deps as such). *User* flakes, being outside the fxtr repo,
  can't path-dep into it — they get fxtr2 via a uv **git-dep** on the
  fxtr repo, through the same `mkVenv`.
- Expose `lib.fxtr.pkgs` (canonical-system `legacyPackages` over the
  flake's pinned nixpkgs) + `lib.fxtr.pkgsFor system`, and
  `lib.fxtr.canonicalSystem` (`x86_64-linux` for the prototype), so
  downstream workdir flakes write their own layer derivations against
  a pinned `pkgs` with **no** nixpkgs input of their own. (See the
  single-architecture stance in `overview.md` / OQ3.)
- Export `lib.fxtr.*` — the helper functions user flakes use to
  build their `fxtr = { ... }` attrset. Each buildable-producing
  helper takes an optional `system` arg defaulting to
  `canonicalSystem`. Minimum set:
  - `mkVenv { workspaceRoot, ... }` — run uv2nix
    (`loadWorkspace → mkVirtualEnv`) over a uv workspace and return
    the venv derivation. Adapts from the spike's working uv2nix setup.
    The workspace's own `pyproject.toml` decides how it reaches fxtr2 —
    a relative path-dep for in-repo lib workspaces (`fxtr_nix_writer`,
    `py_testing`), or a git-dep on the fxtr repo for downstream user
    flakes. **Implemented in Stage 1** (pulled forward from Stage 2),
    so `mkEntityFromPython` builds against the real mechanism. Its
    internal companion `_mkGuestSitePackages` extracts a flat,
    pure-python, eryx-runnable `site-packages` dir (Stage 2); both are
    now consumed by the **bundle** path (`mkWasmPackageBundle`,
    Area 05), not a workstation sandbox.
  - *(Removed: `mkSandbox { prelude, venv }`.* The workstation
    sandbox is no longer nix-built — see `../stage2_sandbox_runtime.md`
    "Revised direction" and Area 02's superseded banner.)
  - `mkEntityFromPython { venv, fromPython }` — materialize a
    Python-resolved **entity object** into a layer; `fromPython`
    names it via a dotted `module:attr` / `module:Class.attr` chain
    (e.g. a type-id `FxtrID` exposed as `my_pkg.my_module:my_type_id`
    or `my_pkg.my_module:MyType._fxtr_known_type_id`), *not* a class.
    Agent provides the `venv` (which supplies fxtr2). General-purpose,
    used by Area 06 for the `typeEntity` slots, and for any DATA entity
    an agent wants to write from Python.
  - `mkWasmService { src, ... }` — build a `fxtr.services.<name>`
    attr `{ sitePackages = <drv>; entryPoint = "…"; }` (v0 vertical
    slice; see Area 05). **No entity, no `.drv` pointer, no writer.**
    Internally uses `_mkGuestSitePackages`; takes its guest venv's
    fxtr2 from the agent's `pyproject.toml` (git-dep).
  - `esbuildRenderer { src, externals ? ... }` — run esbuild on
    `src/index.tsx` and produce an **ESM derivation** (a `.mjs`).
    Returns a derivation, not an entity — the dispatcher wraps the
    bytes in a `RendererEntity` at serve time (Area 06). No venv arg.
    (An agent gets a type's id_string for the preset key from Python in
    a `run_script` — `print(MyType.fxtr_type.id_string())` — so there is
    no nix-side `entityIdString` helper.)
  - `mkDockerService` — later, after v0. Subsumes both of the old
    Docker and py_container helpers (see Area 12).

  *(Removed relative to the old design: `mkRendererEntity`,
  `mkDispatcherPreset`, `mkAll`, and the `write_wasm_bundle` /
  `write_renderer_entity` / `write_dispatcher_preset` writer kinds.
  Code is no longer written into entities, and the `mkAll`
  build-everything linkFarm lost its rationale once consumers build
  lazily from git refs — see Area 03. `fxtr_nix_writer` keeps
  `write_hello_world` + DATA-entity writing only.)*

**Venv convention.** Helpers that take a `venv` arg trust the
venv to supply fxtr2 (we never inject it — agent code can't build
entities without it anyway). Helpers without a `venv` arg use the
lib-internal fxtr2 venv (`mkVenv` over `fxtr_nix_writer`'s workspace).
This keeps signatures honest about what each helper actually needs.
Note this is a *who-supplies-the-venv* distinction, orthogonal to
path-dep-vs-git-dep: both kinds of venv are built by the same `mkVenv`
/ uv2nix machinery.

User flakes consume it as an input:

```nix
inputs.fxtr.url = "github:TransluceAI/fxtr";   # or path:... for dev
outputs = { self, fxtr }: { fxtr = { ... }; };
```

## Why this dodges the spike's path-walk problem

The earlier blocker (`part2_nix_exploration_findings.md`) was *not*
"uv2nix can't handle path-deps outside the workspace root" — the
spike's own `nix_prototype_augment` path-deps `../lib` and works fine.
The real cause was uv2nix's `projectRoot = workspaceRoot + "/${localPath}"`
over-normalizing *past the store root* when a `..` reached above the
flake's store-copied source. That only happened because that flake sat
at a *deep* subdirectory, so `nix build path:$(pwd)` copied just that
subdir and the `../../../..` deps pointed above it.

With our flake at the **repo root**, the whole repo is store-copied,
so every `..` path-dep that stays within the repo (fxtr2 at
`../../../v2`, fxtr v1 at the repo root) resolves *inside* the copy.
No fxtr2 source needs to be fetched out-of-band for lib-internal
workspaces. The same applies to anything else we ship through the
helpers (e.g. `fxtr-sandbox-rpc-adapter`).

The one residual escape is fxtr v1's optional `picoview` source
(`../clarity/...`, above the repo root), but it's gated behind the
`ui` extra and stays out of `workspace.deps.default`, so uv2nix never
computes its `projectRoot`. Confirm during implementation.

## Build-time fxtr2 availability

DATA-entity writers (`fxtr_nix_writer`, `mkEntityFromPython`) need
fxtr2 importable inside the nix build sandbox, so the layer writer
(which uses `fxtr2.data.layered_file_store.writer`) can function. For
the no-venv path that's the lib-internal venv (`mkVenv` over
`fxtr_nix_writer`); for venv-arg helpers it's the caller's venv.

`mkWasmService` does **not** write an entity, so it needs no fxtr2 at
*build* time for a writer — it only assembles a guest site-packages dir
(whose *runtime* fxtr2 comes from the agent's `pyproject.toml`).
`esbuildRenderer` produces ESM and needs no fxtr2 at all. The
entity-wrapping for renderers happens in the dispatcher at runtime
against the workstation's own fxtr2.

## Open questions

- Q10.A (resolved): flake lives at the **fxtr repo root** (OQ1a),
  to minimize path acrobatics from the flake to fxtr2's source and
  because it touches both fxtr v1 and v2.
- Q10.B (resolved): the fxtr flake builds its fxtr2-bearing venv via
  **uv2nix** (`mkVenv` over `fxtr_nix_writer`'s workspace, fxtr2 as a
  relative path-dep) — *not* the spike-findings' uv-in-derivation
  fallback. The repo-root-flake + in-store path-dep pattern makes
  uv2nix work cleanly here.
- Q10.C: CI gating — does CI need nix installed? If we're shipping
  helpers that user flakes will consume, we'll want CI to at least
  build the v0 wasm-bundle helper end-to-end to keep it from
  rotting. Tied to Area 11. (Deferred until the helpers work; CI will
  also need a reachable linux builder.)
- Q10.D: Template / scaffold for new workdirs — `nix flake init
  -t ...`? An in-repo example to copy? Both? See Q1.C in
  `01_flake_schema.md`.
- Q10.E (resolved): reference the flake via **git, not `path:`**, for
  dev and tests. `path:` copies the *entire* working tree into the
  store with no ignore filtering (build artifacts, venvs, caches) and
  re-scans it on every build; a git flake reference (`nix build .#…`
  or `git+file:<repo>`) copies only VCS-tracked files, keeping the
  source copy small and fast. Caveat (general to git flakes): dirty
  edits to *tracked* files are picked up, but a brand-new file is
  invisible to nix until it is tracked. Which concrete checkout
  satisfies this is environment-specific — see "Implementation
  environment note".

## Implementation environment note

This section is specific to the current working setup, *not* part of
the design — it only records which checkout satisfies Q10.E's
git-reference requirement here.

The fxtr repo is managed by **jj**. The primary checkout
`/Users/daniel/fxtr` is a jj worktree with a *stub* `.git` (a marker
dir, not a real git repo), so nix can't use it as a git flake there.
Develop instead in the **colocated jj↔git worktree**
`/Users/daniel/fxtr-worktrees/fxtr` (real `.git`), building with
`nix build .#…` or `git+file:///Users/daniel/fxtr-worktrees/fxtr#…`.
Because jj auto-snapshots, a newly-created file becomes visible to nix
after any `jj` command (or an explicit `git add`); editing existing
tracked files needs no snapshot.
