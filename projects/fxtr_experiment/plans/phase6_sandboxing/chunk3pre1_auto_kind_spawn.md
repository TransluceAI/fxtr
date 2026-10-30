# Chunk 3 pre-commit 1 — kind-agnostic spawn (`NixServiceDefn`) in `fxtr_sandboxing`

*Standalone, self-contained commit in `projects/fxtr_sandboxing`,
landed before the chunk-3 serving work. Rationale: the chunk-3 round
in `discussion_log.md` (this directory).*

## Why

The durable sandboxed-binding record stores the defn reference triple
(`git_commit`, `flake_attr`, optional `flake_path`) with **no runtime
discriminator** — per `notes/sandboxed_bindings.md`, "the attr's
`kind` is read at spawn time". But `SandboxSpawnerImpl.spawn`
dispatches on the *entity type* of the reference
(`NixWasmServiceDefn` / `NixContainerServiceDefn` /
`NixPyodideServiceDefn`), so a triple-only caller has no way to spawn
without knowing the kind in advance. The two rejected alternatives:

- storing `kind` in the binding record — duplicates a fact the flake
  attr owns (drift risk; the spawner's kind check would catch a drift
  only at spawn, after the record already misreported it), and bumps
  the just-landed schema for a cache-able derivable fact;
- having `fxtr_experiment` eval the kind itself — puts nix knowledge
  in the wrong system; "what kind is this attr" is the sandboxing
  system's question.

So the spawner gains an **auto mode**: a kind-agnostic defn entity it
dispatches by evaluating the attr's `kind` — machinery it already has
(`nix.eval_service_kind`, used today by every arm's
`_check_service_kind`).

## Design

- **`NixServiceDefn(git_commit, flake_attr, flake_path?)`** — new
  entity in `fxtr_sandbox_types/entities.py`, the same triple as the
  concrete defns, `define_type(SHARED_SANDBOX_NS, ...)`. Documented as
  the kind-agnostic spawn reference: "spawn whatever kind the attr
  declares".
- **The auto arm**: `SandboxSpawnerImpl.spawn` on a `NixServiceDefn`
  resolves the attr's kind, constructs the matching concrete defn
  dataclass from the triple, and dispatches to the existing runner arm
  (`_spawn_runner_nix_{wasm,container,pyodide}`). The bundle
  `EntityRef` threaded into `_SpawnConnectWrapperImpl` (and hence the
  per-connect provenance c-node) stays the caller's `NixServiceDefn`
  ref — provenance records what the caller actually spawned from.
- **One per-env kind cache**, keyed on the immutable triple —
  `(git_commit, flake_attr, flake_path) → kind` — with the same
  validity argument as the existing per-env build-pipeline cache.
  Both the auto arm and `_check_service_kind` read + populate it, so
  an auto dispatch does **one** `nix eval` total (the arm's own check
  hits the cache), and a repeat spawn of the same triple evals
  nothing. The auto arm's eval runs through `_build_with_recycle`
  (the warm-builder borrow + one dead-builder retry), like every
  other nix call.
- **Refuse to guess**: a missing `kind` keeps the existing
  refuse-to-guess error (message extended to mention the auto path);
  an *unknown* kind string raises naming it and the known kinds.
  `_require_git_repo` applies as usual.

Out of scope (noted, not done here): the workstation sandbox bridge's
wire-identical defn dups don't grow a `NixServiceDefn` twin until an
in-script consumer needs one.

## Steps

- [x] `fxtr_sandbox_types/entities.py`: add `NixServiceDefn` (+
      package re-export), docstring stating the auto-dispatch
      contract.
- [x] `fxtr_sandboxing/spawner.py`: the per-env kind cache;
      `_check_service_kind` consults/populates it; the auto arm in
      `spawn` (eval kind → concrete defn → existing runner arm);
      `spawn`'s annotation/docstring grow the new type.
- [x] Tests: auto-spawn e2e over the greeter workdir fixture
      (`nix_runtime`-gated, mirroring `test_nix_spawner.py`) asserting
      connect + call works; unknown-kind refusal covered at whatever
      seam is cheapest (a unit on the kind→defn mapping, or a fixture
      attr with a bogus `kind`).
- [x] Update `projects/fxtr_sandboxing/CODE_OVERVIEW.md`
      (entities + spawner sections).
