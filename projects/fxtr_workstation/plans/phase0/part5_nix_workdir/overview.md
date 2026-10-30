# Part 5 — Nix-driven workstation working directory

This is the orientation doc for Part 5. It replaces `sketch.md` as
the starting point: read this first for the mental model, then dive
into the per-area design notes (`design_notes/01`–`12`) and the
per-stage plans (`stage1`–`stage6`) for detail. `sketch.md` is kept
as the original brain-dump; where it and this doc disagree, this doc
wins.

## The system we're building on

A fxtr **workstation** is a multi-client server where human users
and AI agents collaborate on data generation and analysis.

- One **workstation server** owns the state of all resources and
  hosts long-running, stateful **services**. It offloads *data* to
  disk where it can, but does not checkpoint all of its in-memory
  state.
- **Sessions** are isolated, persistent connections that hold local
  state. Clients are mostly reactive views onto server-side state —
  you can close and reopen a client and pick up where you left off.
- Two client kinds, same universe of objects and the same
  affordances (trigger actions, subscribe to results):
  - **Browser clients** — humans in a browser, usually one session
    per panel, driven by type-specific UI code.
  - **Agent clients** — agents over MCP, usually one session each,
    driven by the agent running sandboxed code that manipulates
    session state. Agents accomplish tasks by *writing code*: they
    package code to run in a sandbox, and build/register visualizers
    for the browser clients to enable human–agent collaboration.

A basic version of all this exists already. The relevant pieces:

- agent MCP — `@projects/fxtr_workstation_agent_plugin/CODE_OVERVIEW.md`
- workstation server — `@projects/fxtr_workstation/CODE_OVERVIEW.md`
  (assembled in the `projects/fxtr_workstation_demo` package)
- sandboxing — `@projects/fxtr_sandboxing/CODE_OVERVIEW.md`
- browser frontend + "dynamic dispatch" — `@projects/fxtr_workstation/frontend_lib/CODE_OVERVIEW.md`
- fxtr2 core — `@v2/CODE_OVERVIEW.md`

## The pitch

Use **nix + git** as the way an agent bridges its work into the
remote workstation and the sandboxes it spawns — but keep **two
logically distinct domains** rather than coupling them:

- **Data** is carried as fxtr **entities** (layered-file-store
  objects). An agent declares data entities in its flake and pulls
  them into a running script with `retrieve_nix_entity`. The MCP
  resolves these against the live working directory (the **bare path**,
  which nix auto-resolves to git-form in a repo / `path:` otherwise),
  **non-frozen** — so uncommitted edits to *tracked* files take effect
  immediately (new untracked files need `git add`; the MCP notices).
  Entities are also how the agent *interacts* with services once
  they're running.
- **Code** (sandbox service bundles, UI renderers) is **never wrapped
  in an entity**. It is referenced by a **git commit + flake
  attribute**, and the *consuming service builds it itself* from that
  reference. Nothing is half-built on the client and shipped as an
  opaque artifact; the spawner / dispatcher rebuild from source at a
  pinned commit.

> **Revised direction (2026-05-30):** this doc supersedes the earlier
> "everything is an entity, including code" model. Previously a
> sandbox bundle was a `.drv`-pointer **entity** (`NixWasmTaskBundle`)
> the agent retrieved and handed to the spawner, and a UI preset was a
> serialized `DispatcherPreset` **entity** the dispatcher loaded by
> name. We've **decoupled code from entities**: code now travels as
> `(git_commit, flake_attr, flake_path?)` references that the
> spawner/dispatcher resolve via nix git flake refs. This deletes a
> whole category of "make a built `.drv` portable and keep it
> reachable in the flake closure" machinery (the
> `unsafeDiscardOutputDependency` + CBOR-closure-scan + sidecar-symlink
> trio of the old Area 12) — git already content-pins the whole flake
> and ships it with a plain `push`. The `.drv` hash is not gone, but it
> is repurposed: see "Spawn reference vs. service identity" below.
> Affected notes: 01, 03, 05, 06, 10, 12 (heavily); 04, 07, 08, 09, 11
> (lightly). An earlier revision (2026-05-29) had already reverted
> nix-control of the *workstation* sandbox runtime; that still holds
> (see the banner two sections down).

Each workstation working directory gets a single `flake.nix`. That
flake produces one top-level `fxtr` attrset with three slices:

```
fxtr
  .entities.<name>            # DATA — general-purpose named entities
      .type    = "fxtr_layer_entity"
      .layer   = <derivation → layered-file-store dir>
      .layerKey = "<key>"
  .services.<name>            # CODE — sandbox service bundles, two kinds
      # wasm kind:
      .kind         = "wasm"
      .sitePackages = <derivation → flat guest site-packages dir>
      .entryPoint   = "module:function"
      # container kind (stage 7):
      .kind         = "container"
      .image        = <derivation → nix2container JSON manifest>
                      # (entry point baked into the image Cmd; no entryPoint leaf)
  .workstation
    .uiPresets.<name>         # CODE — UI renderer recipes (plain attrset)
```

- `fxtr.entities` (DATA) is materialized by the **MCP** via
  `retrieve_nix_entity`, resolved against the **bare workdir path**
  (auto-git in a repo / `path:` otherwise), **non-frozen** — uncommitted
  edits to *tracked* files count; new files need `git add`.
- `fxtr.services` + `fxtr.workstation.uiPresets` (CODE) are
  materialized by the **workstation services that consume them** (the
  spawner; the dispatcher), resolved by **git commit** — the code must
  be committed (and, for a remote service, pushed). This split is
  deliberate: scratch data you iterate on uncommitted; shipped code is
  pinned and reproducible.

> **Revised direction (2026-05-29):** nix does **not** control the
> workstation sandbox runtime. There is no `fxtr.workstation.sandbox`
> attr; the workstation venv (fxtr2 + adapter + the agent's local
> packages + prelude) stays host-controlled via the existing
> `sandbox_runtime` factory + `packages/` walk + `preamble.py`. Nix's
> role is the *data/config bridge* — entities, UI presets, and
> sandboxed service bundles — not the base runtime. Rationale + full
> plan impact: `stage2_sandbox_runtime.md` ("Revised direction").

Two invariants make the buildable side work cleanly:

1. **Every leaf a consumer materializes is a buildable derivation**
   (each `entities.<name>.layer`; each `services.<name>.sitePackages`;
   the renderer ESM derivations a preset points at). The consumer
   targets them individually (`nix build .#fxtr.entities.foo.layer`,
   `nix build "<repo>?rev=<c>#fxtr.services.foo.sitePackages"`).
2. **Intermediate attrs + metadata leaves are plain nix values**
   (`type`, `layerKey`, `entryPoint`, the preset's type-keyed routing
   table, the set of names) — read cheaply via `nix eval --json`
   without building.

## Spawn reference vs. service identity

A deliberate design distinction (it's why we can drop the `.drv`
pointer without losing anything):

- **To *launch* a service**, you pass the *rich* reference
  `(git_commit, flake_attr, flake_path?)`. This is what the spawner
  needs to fetch + build the code, and what an agent can author by
  hand. Git makes it portable (push) and reproducible (the commit
  pins the whole flake, including its committed `flake.lock`).
- **To establish a service's *identity*** (equality / dedup — e.g.
  "are these two the same service?"), the canonical key is the
  resolved **`.drv` hash** of the built attribute. The `.drv` content-
  pins one recipe on one architecture, which is the right notion of
  "same service." This is derived *lazily* from `(commit, attr)` only
  when an equality question actually arises.

v0 implements only the spawn path; there is no route that surfaces
the identity `.drv` hash yet. The point of recording it here is that
the design must not preclude it — and the git-ref spawn form converts
cleanly to a `.drv` hash (it's just `nix eval` of the attr's
`drvPath`) whenever we want it.

## A principled single architecture

The `fxtr` namespace is **not** indexed by system — there is no
`fxtr.${system}.…` layer. This is on purpose: fxtr uses nix to make
**reproducible scientific workflows** ("what you said to run" must
match "what you ran"), so a flake declares a *single canonical source
of truth* for each object it produces, pinned to one architecture (we
prototype on `x86_64-linux`).

- The library flake's `lib.fxtr` carries a `canonicalSystem` default;
  helpers take an optional `system` arg, so an author *can* pin
  individual derivations to different arches. The default keeps the
  common case single-arch.
- To build a flake authored for a foreign arch, you use a builder of
  that arch (remote builders, or transparent virtualization). The
  pinned arch determines *which builder you need*, by design.
- A service's identity `.drv` hash (above) is single-arch for the same
  reason: it pins one recipe on one system, and that's the recipe you
  reproduce.

Contrast with the `packages.${system}` convention in ordinary package
flakes, whose goal is "build for the user's machine, whatever it is" —
the opposite of what a pinned scientific workflow wants.

## Core concepts

**Entities are pointers into layered file stores (DATA only).** An
entity is a three-field attrset `{ type, layer, layerKey }`. `layer`
is a derivation producing a layered-file-store directory; `layerKey`
names the content inside it. The `type` discriminator
(`"fxtr_layer_entity"` for v0) marks the *encoding*, not the *kind* —
the kind lives inside the payload (`$fxtr.type`). Names are mutable,
git-branch-like pointers to immutable content.
(`design_notes/03_entity_declarations.md`.)

**Services are git-referenced code (CODE).** A
`fxtr.services.<name>` is *not* an entity — it's a plain attrset whose
`sitePackages` leaf builds a flat guest site-packages dir and whose
`entryPoint` is a plain string. The agent references it for spawning
as `NixWasmServiceDefn(git_commit, flake_attr, flake_path?)`; the
spawner resolves that ref against its configured git repo and builds
it. (`design_notes/05_sandbox_bundle.md`, `12_nix_drv_reference.md`.)

**UI presets are git-referenced render recipes (CODE).** A
`fxtr.workstation.uiPresets.<name>` is a plain attrset keyed by a
type's **id_string** (the attribute name), each entry pointing at ESM-
producing renderer derivations (plus the type's entity attached for
introspection). The dispatcher resolves a preset by
`(git_commit, flake_attr, flake_path?)`, builds the renderer ESM, and
wraps the bytes in a `RendererEntity` *at serve time*.
(`design_notes/06_ui_presets.md`.)

**Build on demand, trust nix's cache.** No consumer eager-builds
everything. `run_script` doesn't touch nix unless the script calls a
nix builtin. Entity layers build when `retrieve_nix_entity` is called;
a service's `sitePackages` builds when the spawner spawns it; a
renderer ESM builds when the dispatcher first needs it. We always
shell out and let nix's eval cache + content-addressed store make
repeats cheap. (`design_notes/08_build_trigger.md`.)

## Runtime flows

**`run_script` (the direct sandbox path).** Unchanged from today; does
not go through nix. The MCP builds an eryx factory from the
`sandbox_runtime` closure (fxtr2 + adapter), walks `<workdir>/packages/`
for local source, reads `<workdir>/preamble.py`, runs the body. Nix is
consulted only if the script calls a nix builtin.
(`design_notes/02_prelude_and_venv.md` — superseded note;
`08_build_trigger.md`.)

**`retrieve_nix_entity` (DATA entity → script).**
(`design_notes/04_retrieve_nix_entity.md`.)

```python
retrieve_nix_entity(name, result_type=None, nix_location="fxtr.entities")
```

Mirrors `lookup_entity`. Per call: look the name up in the cached
structure-eval, `nix build .#fxtr.entities.<name>.layer` (against the
**bare workdir path** — auto-git/`path:`, non-frozen), open a
layered-store reader over the result, resolve `layerKey` to an
`FxtrID`, bridge it into `script_scope`, close the reader. If the
workdir is a git repo with untracked files, a short count-only notice
warns they're invisible to nix (`git add` if an entity needs them).
`nix_location` is whitelisted; for v0 the only allowed location is
`fxtr.entities` (presets are no longer entities, so
`fxtr.workstation.uiPresets` is off the whitelist).

**Spawning a service (CODE → spawner).**
(`design_notes/05_sandbox_bundle.md`, `12_nix_drv_reference.md`.)

The agent commits its flake (and pushes, for a remote spawner), then
constructs `NixWasmServiceDefn(git_commit, flake_attr, flake_path?)`
in `run_script` — using the `current_commit()` builtin (HEAD + a
clean-tree guard that rejects uncommitted/untracked files) so a stale
commit can't be pinned by accident — and passes it to the spawner. The
spawner is configured with one git repo; it resolves the ref as a nix
git flake ref (`git+file://<repo>?rev=<c>&dir=<flake_path>#<attr>`),
`nix eval`s `entryPoint`, `nix build`s `sitePackages`, mounts the
result, and launches. No `.drv` is passed around; no entity wraps the
code. Trust is **connection-level** — the workstation trusts the agents
authorized to connect (like a nix remote builder), and building
git-referenced code is no more privileged than the `run_script` code
execution they already have. No repo/ref allowlist. Multiple agents
need no extra machinery (all push to a shared bare repo); remote is just
git reachability + a build environment, same trust model. See
`12_nix_drv_reference.md` "Trust model & topology".

**UI presets + the live-configurable dispatcher (CODE → dispatcher).**
(`design_notes/06_ui_presets.md`.)

A flake declares `fxtr.workstation.uiPresets.<name>` as a plain
attrset of renderer build recipes. The dispatcher impl
(`NixConfigurableDispatchImpl`) is configured with one git repo and
implements the renamed **`NixDispatcherProto`** (extends the unchanged
`WorkstationDispatcherProto` with
`set_preset(git_commit, flake_attr, flake_path?)`). On `set_preset` it
resolves the attr via a git ref, `nix eval`s the type-keyed routing
table (keys are type id_strings — no build needed for keys), builds
each renderer's ESM derivation (lazily or eagerly), wraps the bytes in
a `RendererEntity` it creates in the workstation store, swaps its
resolution table atomically, and broadcasts a reload. It optionally
auto-loads an initial `(commit, attr)` configured at construction so
restarts come up with a UI. The base `WorkstationDispatcherProto`
(`resolve` + `subscribe_to_changes`) is unchanged, so the frontend
needs no work. `BasicDynamicDispatchImpl` is deprecated.

## The helper library (`lib.fxtr.*`)

The fxtr repo owns a flake exposing the helper namespace user flakes
consume as an input. (`design_notes/10_nix_infrastructure.md`.)

- `mkVenv { workspaceRoot, ... }` — uv2nix output → host-runnable venv.
  Plus the internal `_mkGuestSitePackages` that extracts a flat,
  pure-python, eryx-runnable site-packages dir. Consumed by
  `mkWasmService`.
- `mkEntityFromPython { venv, fromPython }` — import a Python-named
  object and write it as a layered-store entity (used for type
  entities + general data entities).
- `mkWasmService { src, ... }` — build a wasm-kind `fxtr.services.<name>`
  attr (`{ kind = "wasm"; sitePackages; entryPoint }`). **No entity, no
  `.drv` pointer, no closure-scan machinery**.
- `mkContainerService { src, ... }` (stage 7) — build a container-kind
  `fxtr.services.<name>` attr (`{ kind = "container"; image }`, a
  nix2container manifest with the entry point baked into the image
  `Cmd`). Subsumes the deferred `mkDockerService` idea. See
  `stage7_nix_container.md`.
- `esbuildRenderer { src, externals ? ... }` — compile `src` to an ESM
  derivation (a `.mjs`). **Returns a derivation, not an entity** — the
  dispatcher wraps the bytes in a `RendererEntity` at serve time.

An agent obtains the type id_string it uses as a preset's type-keyed
attribute name from Python in a `run_script`
(`print(MyType.fxtr_type.id_string())`); there is no nix helper for it.

Removed relative to the old design: `mkRendererEntity`,
`mkDispatcherPreset`, `mkAll` (the build-everything linkFarm — its
rationale evaporated once consumers build lazily from git refs; CI
rot-checking is a follow-up), the `write_wasm_bundle` /
`write_renderer_entity` / `write_dispatcher_preset` writer kinds, and
the `.drv`-pointer / sidecar-symlink / closure-scan apparatus.
`fxtr_nix_writer` keeps `write_hello_world` + data-entity writing only.

**Two fxtr2-provisioning paths, by convention.** A helper that takes a
`venv` arg trusts that venv to supply fxtr2 (`mkVenv`,
`mkEntityFromPython`, `mkWasmService`'s guest venv). A helper without a
`venv` arg uses the fxtr flake's own lib-internal `fxtr2` derivation.
The spike at `/Users/daniel/extgit/spikes/2026_05_21_nix_fxtr_prototype/`
proved the git-dep approach works.

## Forward-looking pieces (contract now, build later)

**Remote entity store.** (`design_notes/07_remote_entity_store.md`.)
For DATA only. v0 is self-contained: every layer derivation builds what
it needs from scratch. We pin the build-time env-var contract —
`FXTR_REMOTE_BASE_URL` + optional `FXTR_REMOTE_BEARER_TOKEN` — so the
later rehost slots in without re-architecture. (Code no longer travels
through any store — it travels through git — so this is purely a data
concern.)

**Service identity `.drv` hash.** Forward-looking (above): equality /
dedup of services by their resolved `.drv` hash, derived lazily from
the git-ref spawn form. No v0 route.

## Failure & UX

(`design_notes/11_failure_and_ux.md`.) `run_script` does no nix work
itself, so it has no nix header. Nix failures surface where nix runs:
- `retrieve_nix_entity` — **MCP-side**, so it gets the
  `NixEntityBuildFailed` + `<workdir>/.tmp_outputs/<ts>_<id>.log`
  stderr-dump treatment (the MCP has the workdir).
- `spawn` / `set_preset` — **workstation services**, so they just
  **raise normally** with stderr in the message; the error rides back
  as an `invoke_error` through the agent's normal in-script exception
  path (captured by the existing `ExceptionLog`/`get_full_exception`).
  They write no dump file themselves (they may not even have the
  agent's workdir, esp. when remote).
No agent-facing build-progress in v0.

## Migration

(`design_notes/09_migration.md`.) **Additive, flake-optional.** The
existing `packages/` + `preamble.py` workstation sandbox is unchanged
and permanent. A workdir adds a `flake.nix` only to gain nix-backed
affordances. **New for code:** using `fxtr.services` / `uiPresets`
requires the workdir to live in a **git repo** (commits are the
transport), and a remote spawner/dispatcher requires the agent to push.
Data (`retrieve_nix_entity`) needs no commits — tracked-file edits are
picked up live, new files just need `git add` (or it works with no git
repo at all via the `path:` fallback). Both demos gain a `flake.nix` to
demonstrate the new affordances.

## v0 scope at a glance

**In:** the three-slice flake schema; `retrieve_nix_entity` (data);
the `current_commit()` builtin; `mkWasmService` + `NixWasmServiceDefn` +
the git-ref spawner arm; nix+git-driven UI presets +
`NixConfigurableDispatchImpl` / `NixDispatcherProto`; `esbuildRenderer`;
the `lib.fxtr.*` helpers above;
self-contained layers; the demos.

**Out (was in, now removed):** the `.drv`-pointer bundle entity, the
`unsafeDiscardOutputDependency` / CBOR-closure-scan / sidecar-symlink
machinery, `write_wasm_bundle`, the serialized `DispatcherPreset`
entity + `mkDispatcherPreset` + `mkRendererEntity`, and (from the
earlier 2026-05-29 revision) the `fxtr.workstation.sandbox` derivation
+ the per-`run_script` MCP build trigger.

**Landed post-v0:** nix-driven container sandboxing
(`mkContainerService` + `NixContainerServiceDefn` + the `kind`
discriminator — stage 7; was the deferred `mkDockerService`); podman
runtime standardization + removal of the deprecated in-band-bytes
container paths (stage 8).

**Deferred (see `followups.md`):** the remote-store rehost +
`DeepFetchable`; remote deployment (git reachability
+ build env — *no* allowlist; trust stays connection-level); the
service-identity `.drv`-hash route; operator-driven preset selection;
`get_full_exception` file-based migration.

## Implementation arc (stages)

Detail lives in each stage file; this is just the map.

1. **`stage1_nix_foundations.md`** — the repo flake, the `fxtr2`
   derivation, `mkEntityFromPython`, the `fxtr_nix_writer` scaffold.
   (Already landed. Its `.drv`-pointer closure-scan validation is now
   orphaned by the revised direction — removed in Stage 3.)
2. **`stage2_sandbox_runtime.md`** — *originally* the nix cutover of
   the workstation sandbox; **reverted**. What survives: `mkVenv`,
   `_mkGuestSitePackages`, the wasm-marker handling, and the MCP nix
   wrapper (`nix.py`). (Already landed.)
3. **`stage3_bundle_and_spawner.md`** — `mkWasmService`,
   `NixWasmServiceDefn`, the git-ref spawner dispatch arm, the
   `fxtr_sandboxing/nix.py` git-flake-ref wrapper, the
   `prepare_workdir_repo` test/scaffold harness + `sandbox_smoke` rework
   (de-hardcoding it onto the new git-ref system), deprecation of the
   old in-band-bytes path, and removal of the orphaned Stage-1
   closure-scan code.
4. **`stage4_retrieve_nix_entity.md`** — the `retrieve_nix_entity`
   builtin (data) + `current_commit()` + MCP plumbing; remove
   `prepare_package_bundle`. (The spawn path itself is already proven
   in Stage 3.)
5. **`stage5_ui_presets.md`** — `esbuildRenderer`, the nix+git-driven
   dispatcher (`NixDispatcherProto` / `NixConfigurableDispatchImpl`),
   softening the mtime impl to "no longer suggested".
   - **`stage5post1_ui_preset_transpose.md`** — `mkUiPreset` (the
     per-renderer-declaration → type-id table transpose). (Landed.)
   - **`stage5post2_nix_wrapper_simplify.md`** — collapse the nix-CLI
     wrappers (no shared lib). (Landed.)
   - **`stage5post3_run_script_set_preset_test.md`** — the agent-path
     `run_script` → `set_preset` E2E test on `basic_test_demo` (deferred
     out of Stage 5 decision #4; independent of the big-demo work).
6. **`stage6_workstation_demo.md`** — migrate the assembly-stage demo
   (`examples/fxtr_workstation_demo/`); it becomes the canonical in-repo
   example.
   - **`stage6pre1_builtin_and_lib_entities.md`** (prerequisite) — the
     `{ type = "builtin"; idString; }` entity shape (for `!basic_type`)
     + `lib.fxtr.entities.*` (ready-made `forType`s for the library
     types the demo's `builtin/` renderer routes).
7. **`stage7_nix_container.md`** (post-v0 follow-up) — nix-driven
   container sandboxing: `mkContainerService` (nix2container image,
   entry point baked into `Cmd`), the `kind` discriminator on
   `fxtr.services.<name>`, `NixContainerServiceDefn` + the spawner's
   container arm (skopeo `nix:` delivery into the container runtime),
   container + transporter test templates. Subsumes the deferred
   `mkDockerService`.
8. **`stage8_podman.md`** (post-v0 follow-up) — podman runtime
   standardization: default runtime flip (Apple Container retained as
   a supported non-default backend, docker as a compatibility escape
   hatch), tarless skopeo delivery into podman's API socket, the
   per-spawn sshd-sidecar pod for remote-podman socket plumbing
   (inetd-mode ssh transport — nothing listens anywhere),
   cache-prune/debris-sweep CLI, and removal of the deprecated
   in-band-bytes container paths (`DockerTaskBundle` /
   `PyContainerTaskBundle`).
   - **`stage8post1_runtime_sweep_tests.md`** (follow-up) — the
     container E2E tests sweep over all supported runtimes (podman +
     Apple Container; uninstalled ones skip), so the non-default
     backend can't rot silently; posture wording softened
     accordingly.
9. **`stage9_nix_runner.md`** (done) — move nix itself into a podman
   container so the host needs neither nix nor skopeo. A new
   `fxtr_nix_runner` backend (the shared layer beneath the three nix
   wrappers) owns the host-vs-podman switch, per-repo builder + lazy
   deliverer lifecycle, in-container `flake lock`, output materialization
   into a host-readable store, and in-container skopeo delivery. Verified
   host-nix-free across the full downstream E2E sweep (via the
   `run-with-masked-nix` tripwire). Both backends ship; the default is left
   *unset* — selection is an explicit, configured choice (see post-1).
   - **`stage9post1_backend_config_wizard.md`** (planned) — no implicit
     default backend: a unified backend+runtime config plus a one-stop
     setup wizard (detect → explain tradeoff → nudge → write config;
     `--detect` JSON mode) that makes the host-vs-podman choice explicit
     and informed, once.
