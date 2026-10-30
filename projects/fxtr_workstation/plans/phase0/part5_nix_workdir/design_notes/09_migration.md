# Coexistence & migration

How the nix-flake path coexists with the existing `packages/` +
`preamble.py` workstation sandbox.

> **Revised (2026-05-29):** there is **no hard cut** and no
> workstation-sandbox migration. The `packages/` + `preamble.py`
> path is the *permanent* mechanism for the workstation sandbox, not
> a legacy fallback we're replacing. `flake.nix` is **optional** — a
> workdir adds one only to expose nix entities / UI presets. Most of
> the "what changes in the codebase / demos to migrate" detail below
> assumed the cancelled MCP cutover; it's struck through conceptually
> by `../stage2_sandbox_runtime.md` "Revised direction". The parts
> that remain real: adding `retrieve_nix_entity`, the bundle/spawner
> changes, and the UI-preset dispatcher.

> **Revised (2026-05-30):** adds the git-repo requirement for the CODE
> slices. Data (`retrieve_nix_entity`) needs no git — the MCP resolves
> the bare workdir path (auto-git in a repo / `path:` otherwise),
> non-frozen, so uncommitted tracked edits count and no commit is
> needed. But `fxtr.services` / `fxtr.workstation.uiPresets`
> are referenced **by git commit**, so using them requires the workdir
> to live in a git repo (and a *remote* spawner/dispatcher requires the
> agent to `git push`). v0 commit/push is **manual** (agent's shell);
> the only MCP affordance is `current_commit()` (Stage 4) — HEAD + a
> clean-tree guard.

## Strategy: additive, flake-optional

No cutover of the workstation sandbox. The existing `packages/` +
`preamble.py` path stays as-is and keeps running. A workdir **without**
a `flake.nix` runs scripts normally; it simply can't call
`retrieve_nix_entity` or use nix services / UI presets. A workdir
**with** a `flake.nix` gains those nix-backed affordances on top.

**Git for code; git-aware-but-soft for data.** `retrieve_nix_entity`
works against any flake-bearing workdir — the MCP resolves it via the
**bare path** (auto-git in a repo / `path:` otherwise), **non-frozen**,
so it needs no commits: tracked-file edits are picked up live and new
files just need `git add` (the MCP surfaces a short count-only notice
when untracked files are present — Area 04). Spawning a service or
pushing a preset additionally requires the workdir to be inside a **git
repo** *and committed*: the spawner/dispatcher resolve `(commit, attr)`
git refs `--no-update-lock-file`, so the agent commits before referencing, and pushes
to a remote consumer's repo if the consumer isn't local. The branch is
unconstrained — a local/private repo and any commit (not necessarily on
`main`) is fine. The flake may also live at a repo subpath, in which
case the spawner/dispatcher are told the `flakePath` (Area 01).

Rationale: nix-controlling the workstation venv bought too little to
justify replacing a working path (see the revised-direction note). By
keeping the sandbox host-built we avoid the MCP/venv fxtr2 coupling and
keep the migration friction near zero — adopting nix is opt-in,
per-affordance.

## What changes for an agent / human authoring a workdir

The base workdir is **unchanged** — `preamble.py` + `packages/` still
drive the workstation sandbox exactly as today:

```
<workdir>/
  .fxtr_workstation.toml
  preamble.py
  packages/
    my_pkg/
      pyproject.toml
      src/my_pkg/...
```

Adopting nix-backed entities / UI presets is **additive** — drop in a
`flake.nix` (and the files it references):

```
<workdir>/
  .fxtr_workstation.toml
  preamble.py          # still the prelude — MCP reads this directly
  packages/...         # still walked by the MCP for local source
  flake.nix            # optional; declares fxtr.entities / fxtr.workstation.uiPresets
  flake.lock
  .gitignore           # excludes .tmp_outputs/
  .tmp_outputs/        # gitignored; nix build error dumps land here
```

The MCP **still** reads `preamble.py` and walks `packages/`; the flake
governs only nix entities/presets. There is no `prelude.py` rename and
no agent-authored venv recipe for the workstation sandbox.

`.fxtr_workstation.toml` stays as-is. No new fields. Existing
fields (`active_session`, `agent_mcp_store`, urls) all still apply
unchanged.

## What changes in the codebase

Affected packages and their cuts (high level — exact file-by-file
work lives in the implementation plan):

> The next two bullet groups are **revised** — they assumed the
> cancelled workstation-sandbox cutover. Corrected here:

- **`fxtr_workstation_mcp/`** (the agent-MCP host package):
  - **Unchanged:** `sandbox_factory.build_runner_volumes`'s
    `packages/` walk, `<workdir>/preamble.py` read, the per-package
    src-layout warning logic — all stay. (`prepare_package_bundle`
    plumbing is handled in Areas 05/12, unrelated to this.)
  - **Added:** `fxtr_workstation_mcp/nix.py` (Area 08, already
    landed); lazy nix eval/build inside the `retrieve_nix_entity`
    path (Area 04) and the preset path (Area 06); fenced nix-error
    rendering + `.tmp_outputs/` writer (Area 11), wired where nix
    actually runs.
  - **No** `SandboxEnvSupervisor` / eryx-factory change — it keeps
    building from the `sandbox_runtime` closure.

- **`fxtr_workstation_agent_plugin/sandbox/`** (the sandbox-side
  bridge):
  - **Kept:** the `sandbox_runtime` package (it supplies fxtr2 + the
    adapter to the host-built sandbox).
  - **Removed:** the `prepare_package_bundle` builtin closure
    (Area 04/05).
  - **Added:** `retrieve_nix_entity` + `current_commit()` builtins
    (Area 04 / open-concerns #14).

- **`fxtr_sandboxing/`** (the underlying sandbox machinery):
  - **Added:** `NixWasmServiceDefn` spawn-ref type +
    `_spawn_runner_nix_wasm` dispatch arm + `fxtr_sandboxing/nix.py`
    git-flake-ref eval/build helper (Q12.E). (Area 12.)
  - **Moved to `deprecated/`:** old prepare modules + the entity
    types they produce. (Area 12.)
  - **Kept (with deprecation docstrings):** existing dispatch arms
    in `SandboxSpawnerImpl.spawn` for `TaskBundle`,
    `DockerTaskBundle`, `PyContainerTaskBundle`. They keep
    working; we just won't add new helpers for them.

- **`fxtr` repo top-level / `projects/fxtr_workstation/`** (per Area 10):
  - **Added:** a flake exposing `fxtr2` derivation + `lib.fxtr.*`
    helpers.

## Demos

Neither demo needs a sandbox migration — their existing
`preamble.py` + `packages/` workdirs keep working unchanged. They
gain a `flake.nix` only to *demonstrate* nix-backed affordances:

- **`projects/fxtr_workstation_agent_plugin/examples/basic_test_demo/agent_workdir/`** —
  also backs `tests/test_mcp_in_memory.py` and
  `tests/test_mcp_subprocess.py`. These stay green as-is in Stage 2.
  A `flake.nix` declaring a sample entity / preset is added later
  (Stages 4-6) to exercise `retrieve_nix_entity` + presets.
- **`projects/fxtr_workstation_demo/`** — the assembly-stage demo.
  Same: unchanged sandbox; gains nix entities/presets in later
  stages where those features land.

## Template + scaffold (the runtime workdir is its own repo)

The runtime workdir is its **own git repo**, scaffolded *out* of this
monorepo — *not* a `flakePath` into the fxtr repo. Two reasons (detail
in `stage6_workstation_demo.md`): `current_commit()`'s clean-tree guard
would otherwise check the whole monorepo (never clean in dev), and the
agent would be committing into the library/source repo. Conceptually:
the fxtr repo is a flake *input* to the workdir, not its container.

What we ship (per Q1.C / Q10.D):

- **In-repo template/source.** The `agent_workdir/` directories
  (`basic_test_demo/agent_workdir/`, the assembly demo's) are the
  canonical, version-controlled examples.
- **Scaffold script.** Copies a template `agent_workdir/` to a
  configurable location, `git init`s it, makes an initial commit →
  ready to run. This is the supported "make a runnable workdir" path
  (and the concrete Q1.C scaffold). It **parameterizes the workdir
  flake's `fxtr` input** and commits the resulting `flake.lock`:
  `git+file:///abs/local/fxtr` for dev, `github:…` for prod.

**How the `fxtr` input resolves (and why `--override-input` is
test-only).** The committed `flake.lock` is the source of truth, baked
in at scaffold time per above. This matters because the **code path**
(spawner/dispatcher) builds `--no-update-lock-file` (Area 12), and `--override-input`
is a lock modification that conflicts with `--no-update-lock-file`. So
`--override-input fxtr git+file:<repo>` stays an **in-repo-test-only**
mechanism — used only on the MCP's *non-frozen* `path:` **data** builds,
where the local checkout may carry commits the remote lacks. In normal
operation neither the MCP nor the spawner/dispatcher overrides anything;
the committed workdir flake decides `fxtr`.
- **(Optional) `nix flake init -t …` template** — a thinner alternative
  that points at the same in-repo source. The scaffold script is the
  primary path; flake-init mechanics are an implementation detail.

Test fixtures use the same "copy template → `git init`" logic into a
tempdir, so tests get a clean, committable workdir repo without
touching the monorepo (and the Stage-3 fixture can capture `HEAD`).

## Coexistence of flake + legacy files

A `flake.nix` and `preamble.py` / `packages/` coexist by design,
not by accident: the MCP reads `preamble.py` + walks `packages/` for
the sandbox, and reads the flake (if present) for entities/presets.
They govern disjoint concerns, so there's nothing to ignore or warn
about.

## MCP-workdir version skew

Largely moot now that `flake.nix` is optional. A new MCP against an
old (flake-less) workdir runs fine — it just exposes no nix entities.
An old MCP against a workdir that *relies on* nix entities would lack
`retrieve_nix_entity`; scripts calling it fail at that call. We accept
this — version skew is a general problem and isn't worth special
handling here.

## Open questions

- Q9.A: Exact mechanics of the `nix flake init` template pointing
  at the in-repo example. Detail of the template's `flake.nix` —
  to pin during implementation.
- Q9.B: Whether the migrated `basic_test_demo` should keep its
  current package name (`demo_pkg` / `hello_service`) or be
  renamed to reflect the new structure. Cosmetic; defer to demo
  migration stage.
