# Stage 1 — open questions

## Settled (folded into `stage1_nix_foundations.md`)

- **OQ1 — flake location / uv root / fxtr2-over-nix.** Flake at repo
  root, `workspaceRoot = ./v2`; nix-built fxtr2 is internal build
  tooling only (no user-facing `.#fxtr2`); the **internal build-time
  venv** built via the uv2nix mechanism, shared-but-not-pinned with
  Stage 2's `mkVenv`.
- **OQ2 — consumer dev-input.** Bootstrap with an absolute `path:`
  input, swap to `github:` later; tests always `--override-input fxtr
  path:<repo>`.
- **OQ3 — per-system handling. DECIDED: principled single
  architecture.** No `${system}` namespacing of `fxtr.*`; the library
  flake's `lib.fxtr` carries a `canonicalSystem` default
  (`x86_64-linux` for the prototype) and helpers take an optional
  `system` override. Bundle `.drv` refs stay single-arch (Layer B0).
  Rationale + the full option analysis it replaces are below.

---

# OQ3 — per-system handling — DECIDED

**Decision (2026-05-26): a principled single architecture.** Rather
than treat multi-arch as a partial/deferred feature, fxtr takes an
explicit stance toward single-arch derivations, because the point of
fxtr-over-nix is **reproducible scientific workflows** — "what you
said to run" must match "what you ran." This is different from
building packages for users across environments.

Concretely:

- **Layer A — flake addressing: no system in the path.** `fxtr.*` is
  *not* wrapped in a `${system}` layer (no `forAllSystems`, no
  `--impure`). `nix build .#fxtr.workstation.sandbox` resolves
  directly. The system is pinned *inside* the flake: the library
  flake's `lib.fxtr` carries a `canonicalSystem` default
  (`x86_64-linux` for the prototype), and each helper (`mkVenv`,
  `mkSandbox`, `mkEntityFromPython`, …) takes an optional `system`
  arg defaulting to it. An author may pin individual derivations to
  *different* arches (e.g. a container bundle on one, an entity
  builder on another) — fxtr doesn't care, each is independently
  pinned. The common case stays single-arch via the default.
  - This is *simpler* than the A-flat / A-split options below: the
    MCP, spawner, and dispatcher inject **no** system into any attr
    path. They just build `.#fxtr.…`.
  - `lib.fxtr.*` (plain functions) sit top-level, unwrapped. The
    flake also exposes `lib.fxtr.pkgs` (canonical-system
    `legacyPackages` over fxtr's pinned nixpkgs) + `lib.fxtr.pkgsFor
    system`, so a downstream flake needs no nixpkgs input of its own.
- **Layer B — entity `.drv` portability: B0 (freeze one `.drv`), by
  principle.** A bundle entity pins exactly one recipe on one system
  — not because v0 is single-tenant, but because a pinned workflow
  step *should* name a specific recipe on a specific arch. The
  system of the `.drv` determines which builder reproduces it, by
  design. B1/B2/B3 are not "later upgrades" we're deferring; they'd
  weaken the pinning guarantee, so we're not pursuing them.
- **Reproducing a foreign-arch flake.** Use a builder of that arch:
  nix remote builders, or transparent virtualization (determinate-
  nix's linux-builder, Apple's `container`). The pinned arch
  determines the builder requirement — that's the intended contract,
  not a limitation.

The original option analysis is preserved below as the reasoning
this decision rests on. It made **no** recommendation at the time;
the recommendation above supersedes it. Where the analysis frames
B1/B2/B3 as possible future direction, read that as "considered and
rejected in favor of B0's stronger pinning."

---

# OQ3 — per-system handling (original analysis, superseded)

This note lays out the options and their trade-offs; it
deliberately makes **no recommendation**. The goal is a shared map of
the design space to think against.

## The core observation

Almost everything the fxtr flake produces is **system-agnostic data
produced by a system-specific build**:

- the sandbox venv is pure-python; the prelude is text;
- entity layers are CBOR; renderer modules are portable JS;
- even a WASM bundle's output is pure-python site-packages that runs
  under eryx (CPython-3.14-WASM), a runtime that doesn't depend on
  the host platform.

The *build process* needs a builder for some platform, but the
*result* is platform-independent. So the question is not "what runs
where" so much as "how do we express, in a pure flake, things that
are logically system-agnostic but must be built per-system — and how
do entities that point at build recipes behave when the consumer
(e.g. the spawner) may sit on a different system than the producer
(the MCP)?"

Two facts from nix that shape every option below:

- **A `.drv` is per-system and input-addressed.** Its store path
  hashes all inputs, including `system`. Two systems building
  byte-identical pure-python output still get *different* store
  paths. There is no first-class nix object meaning "a single
  derivation across all systems"; the idiom for "same thing, indexed
  by system" is the `packages.${system}.foo` convention — you
  reference by (flake, attr, system) and nix hands you the per-system
  derivation.
- **Evaluating a derivation is cheap; building it is expensive.**
  You can compute a drv path (even for a *foreign* system, via
  `legacyPackages.${otherSystem}`) without building it, and hold the
  path as a plain string using `builtins.unsafeDiscardOutputDependency`
  (the trick Area 12 already relies on). Realizing a foreign-system
  drv then needs either a builder of that system (remote builder) or
  substitution from a binary cache.

This question has two layers. Layer A (flake addressing) is the more
immediate one — it shapes the flake's output structure in Stage 1.
Layer B (entity drv portability) is an Area-12 concern that doesn't
gate Stage 1 but is the deeper design question.

---

## Layer A — how the flake exposes per-system outputs

A pure flake cannot pick the current system on its own; the system
has to come from *somewhere* in the attr path or via impurity.

### A-impure — `--impure` + `builtins.currentSystem`

The flake reads `builtins.currentSystem`, so the literal path
`.#fxtr.workstation.sandbox` resolves with no system level.

- **Pro:** cleanest literal attr path; agents could in principle type
  the real path.
- **Con:** requires impure evaluation, forfeiting the
  reproducibility/caching guarantees pure eval gives; widely
  considered non-idiomatic. (Daniel has ruled this out — recorded
  here only for completeness.)

### A-flat — everything under `fxtr.${system}.…`

`forAllSystems` (the spike's shape) wraps the *entire* `fxtr` attr,
including `lib`, in a per-system layer. Consumers address
`.#fxtr.${currentSystem}.workstation.sandbox`; tooling (MCP, spawner,
dispatcher) injects its own host system; the agent-facing story stays
system-free.

- **Pro:** pure; uniform — one rule for the whole namespace.
- **Con:** puts genuinely system-agnostic things (`lib.fxtr.*` helper
  functions, templates) under a `${system}` they don't need, which is
  slightly dishonest and means a consumer must pick *some* system even
  to read a system-agnostic helper.

### A-split — system-agnostic outputs top-level, buildables under `${system}`

Follows the common flake convention where `lib` / `overlays` /
`templates` live outside the per-system layer and only buildables sit
under it:

```
fxtr.lib                              # helpers — no system
fxtr.templates                        # scaffold — no system
fxtr.${system}.workstation.sandbox    # buildable — per system
fxtr.${system}.entities.<name>.layer  # buildable — per system
fxtr.${system}.uiPresets.<name>       # buildable — per system
```

Entity *metadata* (`type`, `layerKey`) rides under `${system}`
alongside its `layer` buildable, evaluating identically for every
system.

- **Pro:** honest about what is and isn't system-specific; matches
  widespread flake practice; system-agnostic helpers are reachable
  without naming a system.
- **Con:** the entity attr's metadata is "duplicated" across systems
  (it re-evaluates per system); the `fxtr` namespace is split across
  a system boundary, so a reader has to know which half a given path
  lives in. (nixpkgs duplicates-per-system this way and treats it as
  normal.)

### A-other?

Possibly a different cut entirely — e.g. metadata fully hoisted
out of `${system}` and only the raw derivations under it, so an
entity attr straddles the boundary (`fxtr.entities.foo.{type,
layerKey}` top-level + `fxtr.${system}.entities.foo.layer`). Keeps
metadata single-sourced but splits the `{type, layer, layerKey}` attr
that the design currently treats as one cohesive unit. Trade is
single-sourced metadata vs. a non-cohesive entity attr.

Note: whatever Layer A shape we pick, Area 01's current "no system in
path / internal" lean (Q1.A) needs updating to match.

---

## Layer B — what an entity's bundle reference pins (Area 12)

With any per-system Layer A shape, `retrieve_nix_entity("foo")` builds
`fxtr.${mcp_system}.entities.foo.layer`, so the stored
`NixWasmTaskBundle` naturally references *the MCP's system's* recipe.
If that entity later flows to a spawner on a different system, the
reference may not apply. The question: **what does the bundle entity
store, and how much does it freeze?** The axis across the options is
"how much is frozen at entity-write time vs. resolved later."

### B0 — freeze one system's drv (current Area 12 design)

```
NixWasmTaskBundle(
  derivation  = "/nix/store/abc-my_bundle.drv",   # one frozen drv
  entry_point = "my_pkg.main:run",
)
```
Spawner realizes that exact drv and mounts the output.

- **Pro:** simplest. Maximally pinned — the drv is a frozen recipe;
  editing the flake afterward does not change what this entity points
  at (the property Area 12 chose `.drv`-over-flake-attr to get).
  Sufficient when MCP and spawner are co-located on one system (v0).
- **Pro:** can still pin a *specific other* system if the producer
  deliberately emits a foreign-system drv (realized via remote
  builder / cache).
- **Con:** pins exactly *one* system, chosen at write time. If the
  consumer's system isn't known when the entity is written, it can't
  be served.

### B1 — freeze a map of system→drv

```
NixWasmTaskBundle(
  derivations = {
    "x86_64-linux"   = "/nix/store/abc-my_bundle.drv",
    "aarch64-darwin" = "/nix/store/def-my_bundle.drv",
    # ... one frozen drv per enumerated target system
  },
  entry_point = "my_pkg.main:run",
)
```
At write time the flake *evaluates* the bundle for each target system
(cheap; held unbuilt via `unsafeDiscardOutputDependency`) and freezes
the drv strings. The spawner picks `derivations[my_system]` and
realizes just that one, lazily.

- **Pro:** every drv is still frozen → keeps B0's pinning /
  edit-proof property. Multi-system. Lazy build (only the consumer's
  system is realized).
- **Con:** target systems must be *enumerated up front*; a system not
  in the map can't be served. Eval cost scales with the number of
  systems (instantiating nixpkgs per system is non-trivial eval, even
  though it's not a build).

### B2 — freeze nothing; re-resolve live per system

```
NixWasmFlakeBundle(
  flake_ref   = "github:TransluceAI/fxtr?rev=abc123",          # which flake
  attr_path   = "fxtr.{system}.entities.my_bundle.bundleDrv",  # system is a hole
  entry_point = "my_pkg.main:run",
)
```
The spawner (system S) fills in the hole and builds
`nix build "<flake_ref>#fxtr.${S}.entities.my_bundle.bundleDrv^out"`,
re-evaluating the flake fresh for S. ("The spawner figures out which
system to materialize.")

- **Pro:** any system, no enumeration; the recipe is expressed once
  as a function of system. No new per-system bookkeeping in the
  entity beyond the hole.
- **Con:** re-resolves against *the flake*, so pinning depends
  entirely on `flake_ref` being immutable. The agent's workdir flake
  is *mutable and constantly edited*; to keep an issued bundle ref
  stable you'd have to pin to an immutable locked rev / store-path
  flake, which fights the "edit the workdir flake freely" workflow.
  This is the exact property Area 12 chose raw `.drv` to avoid.
- **Con:** no first-class "set of derivations per system" primitive
  exists — this *is* the (flake, attr, system) lookup, just persisted;
  it inherently couples the entity to a flake reference rather than to
  a content-pinned recipe.

### B3 / CA — system-independent output identity

Orthogonal to B0–B2 rather than a fourth sibling. Because a WASM
bundle's output is deterministic pure-python, a content-addressed
(`ca-derivations`) or fixed-output derivation could yield an output
identifier that is *the same regardless of build system*, so one
reference works everywhere and outputs dedup/substitute across
machines.

- **Pro:** a single system-independent identifier; clean cache
  sharing ("build once, substitute everywhere"); content-pinned.
- **Con:** needs CA or FOD. CA-derivations were an *experimental*
  nix feature as of the knowledge cutoff (maturity should be verified
  before relying on it); FOD reintroduces the fixed-output-hash
  ("fakeHash") dance the self-contained v0 otherwise avoids.
- **Note:** for correctness you don't strictly *need* a shared
  identifier — B1/B2 already get the right bytes onto each system.
  B3/CA is about *output identity / cache sharing*, and could layer on
  top of B1 or B2 rather than replace them.

### Cross-cutting nix mechanisms relevant to all of B

- **Remote builders** (`nix.buildMachines`) — realize a foreign-system
  drv by delegating to a machine of that system. Lets B0/B1 serve a
  system the producer can't build locally.
- **Binary caches / substituters** — distribute build *outputs*;
  the practical "build once, use elsewhere" path, especially clean for
  deterministic outputs.
- **`legacyPackages.${otherSystem}` / `pkgsCross`** — how a flake on
  one system *evaluates* (and, with a builder, builds) recipes for
  another. Underlies B1's per-system drv computation and B2's
  re-resolution.

### What v0 actually needs

v0 is single-tenant, local, WASM-only, MCP and spawner co-located on
one system — so B0 is functionally sufficient for v0 regardless of
which option we eventually want. The live design question is whether
(and how) to shape the v0 entity / spawner-consumption so that moving
to B1, B2, or B3 later is additive rather than a rewrite — e.g.
whether the spawner's consumption should already be framed as "resolve
bundle-entity → drv for *my* system" with a trivial v0 resolver.
