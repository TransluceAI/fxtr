# Remaining discussion topics

Live kanban for Part 5 design. New questions get added here; resolved
ones move into a per-topic design note and disappear from this list.
Each top-level area below also names the design-note file it will end
up in once filled out.

> **Note (2026-05-30) — code/entity decoupling:** code (services, UI
> renderers) is no longer wrapped in entities. Services are
> `fxtr.services.<name>` attrs referenced by `(git_commit, flake_attr,
> flake_path?)` and built by the spawner; presets are plain attrsets
> referenced the same way and built by the dispatcher. The `.drv`-pointer
> entity, `unsafeDiscardOutputDependency`/closure-scan/sidecar machinery,
> `mkDispatcherPreset`/`mkRendererEntity`, and the `write_*` entity
> writers are gone. Area 5/6/12 summaries below are rewritten;
> `overview.md` is the canonical statement. Renames: `mkWasmPackageBundle`
> → `mkWasmService`; `NixWasmTaskBundle` → `NixWasmServiceDefn`;
> `esbuildRendererEntity` → `esbuildRenderer`;
> `LiveConfigurableDispatcherProto` → `NixDispatcherProto`.

> **Note (2026-05-29):** the workstation sandbox is no longer
> nix-built (no `fxtr.workstation.sandbox`, no combined prelude+venv
> derivation, no per-`run_script` build trigger). Items below that
> discuss those are superseded — see `../stage2_sandbox_runtime.md`
> "Revised direction" and the banners in notes 01/02/04/08/09/11.

## Area 1 — Flake schema & MCP build contract  → `01_flake_schema.md` (drafted)
Mostly resolved (see file): top-level shape (`fxtr` attr, leaves
are derivations, intermediate is plain nix), discovery (`nix eval`
for structure + `nix build --json` per leaf).
Still open:
- Q1.A (resolved): no `${system}` in path — principled single
  architecture. `lib.fxtr.canonicalSystem` default + optional
  per-helper `system`; `lib.fxtr.pkgs`/`pkgsFor` exposed. See
  `overview.md` + OQ3.
- Q1.B (resolved): no `lib.fxtr.mkEntity` helper. Entity attrs
  are constructed inline / by producing helpers.
- Q1.C: agent authoring story — template, in-repo example, or both?
- Q1.D: eval-time failure handling — same path as build failure.

## Area 2 — Sandbox prelude + venv  → `02_prelude_and_venv.md` (drafted)
Combined into one derivation at `fxtr.workstation.sandbox`,
producing `out/prelude.py` + `out/venv/`. Effectively required
(fxtr2 now ships in the venv). `sandbox_runtime` package goes away;
eryx factory becomes bare CPython. Single flat `/venv` mount.
Still open:
- Q2.B: bind-mount + factory-reuse lifecycle across nix rebuilds —
  confirm during implementation.
- Q2.D: MCP/venv fxtr2 version-mismatch handling. Accept implicit
  coupling for v0; add version check if it bites.

## Area 3 — Entity declarations + layered file stores  → `03_entity_declarations.md` (drafted; revised 2026-05-30)
Resolved: `entities.<name> = { type, layer, layerKey }` is **DATA
only** (services + presets are not entities — Areas 05/06); no
`entityType` discriminator; hybrid layer/entity model expressed via
attr-sharing. The `fxtr.all` "build everything" linkFarm (`mkAll`) is
**dropped for v0** — consumers build lazily from git refs, so it lost
its rationale; CI rot-checking is a follow-up.
Still open:
- Q3.A (resolved): no `lib.fxtr.mkEntity` helper; future `meta`
  slot would live on individual producing helpers if wanted.
- Q3.B: Set of layer-writer helpers we ship (TaskBundle first; what
  else?).
- Q3.C: Layer size conventions / ergonomics.

## Area 4 — `retrieve_nix_entity` semantics  → `04_retrieve_nix_entity.md` (drafted)
Resolved: signature mirrors `lookup_entity` (`name`,
`result_type=None`); readers opened per-script-call (not
per-cycle); bridges into `script_scope`; per-script flow refreshes
`nix eval`'d structure + builds prelude/venv up front, builds
entity layers lazily on each `retrieve_nix_entity`; eval-fail /
prelude-build-fail abort `run_script` before script body,
entity-build-fail raises inside the script.
Still open:
- Q4.A: nested name paths (`"foo.bar"`)? Leaning no.
- Q4.C: large-layer optimization (load-key-subset mode). Probably
  fine for v0.

## Area 5 — Sandbox service bundles (git-referenced)  → `05_sandbox_bundle.md` (rewritten 2026-05-30)
Decoupled from entities. A service is a `fxtr.services.<name>` attr
`{ sitePackages = <drv>; entryPoint = "…"; }` built by `mkWasmService`
(no entity, no `.drv` pointer, no writer). The agent commits, then
constructs `NixWasmServiceDefn(git_commit, flake_attr, flake_path?)`
by hand and passes it to the spawner, which builds it from the git
ref. v0 = wasm only; Docker later via `mkDockerService`.
Still open:
- Q5.B: full input schema for `mkWasmService`.
- Q5.C: uv2nix → flat-site-packages reshape (Stage 2 settled markers).
- Q5.D: Docker variant — defer; adapt `extgit/spikes/2026_05_15_nix_spike`.
- Q5.F: whether to add a thin MCP commit→spawn helper later (v0 manual).

## Area 6 — UI presets + nix/git dispatcher  → `06_ui_presets.md` (rewritten 2026-05-30)
Decoupled from entities. A preset is a **plain nix attrset** keyed by
type **id_string** (the attr name, which the agent pins by printing
`MyType.fxtr_type.id_string()` from a `run_script`), each entry
attaching the type's `mkEntityFromPython` entity for introspection + a
`byDispatchKey` map to `{ subtypeKey, renderer = <ESM derivation> }`.
Protocol split: `WorkstationDispatcherProto` unchanged; renamed
`NixDispatcherProto` adds `set_preset(git_commit, flake_attr,
flake_path?)`. `NixConfigurableDispatchImpl` takes a required git
`repo` + optional initial `(commit, attr)` at construction; on
`set_preset` it evals the routing table (keys + renderer drv paths from
eval, no build), builds each renderer ESM, wraps the bytes in a
`RendererEntity` it creates in the workstation store at serve time,
swaps its table, fires `trigger_reload()`. Helpers: `esbuildRenderer`
(ESM derivation, no entity). Removed: `mkRendererEntity`,
`mkDispatcherPreset`, the `write_*` writers, and (2026-06-01) the
once-planned `entityIdString`. `BasicDynamicDispatchImpl` kept in place,
docstrings softened to "no longer suggested" (not moved).
Still open:
- Q6.A: operator-driven preset selection. Out of scope for v0.
- Q6.B (resolved): hand-rolled preset attrs fine.
- Q6.C (resolved): `dynamic_dispatch/nix_configurable_dispatch/`.
- Q6.D: eager vs lazy renderer builds (v0 = eager on set_preset).
- Q6.E (dropped): `entityIdString` removed — Python id_string print.
- Q6.F: workstation-store GC of serve-time `RendererEntity`s.

## Area 7 — Remote entity store  → `07_remote_entity_store.md` (drafted)
Unaffected by the drv-pointer redesign — confirmed scope is "data
artifacts via `retrieve_nix_entity`", not spawner bundles.
Resolved: env-var contract is `FXTR_REMOTE_BASE_URL` (required when
remote is used) + `FXTR_REMOTE_BEARER_TOKEN` (optional). v0 scope:
no MCP-side rehost, no in-memory DeepFetchable — just the contract.
Still open:
- Q7.C: where the rehost lives in code.
- Q7.D: FOD-side HTTP API shape.
- Q7.E: in-memory DeepFetchable — new class vs subclass of existing
  `RemoteClient`.

## Area 8 — Build trigger  → `08_build_trigger.md` (drafted)
Resolved: implicit, top-of-`run_script`. Always shell out to nix;
trust nix's own eval cache (no stat shortcut). New MCP-side
`fxtr_workstation_mcp/nix.py` wrapper module (parallel to spawner-
side Q12.E). Async via anyio subprocess. No client-side caching.
No prefetch on `set_working_dir`. No streaming build-progress to
agent in v0 (stderr → MCP log; agent blocks). Friendly dedicated
error for missing `flake.nix`; eval/build failures surface fenced
stderr blocks (ties to Area 11).
Still open:
- Q8.A: shape of the `--apply <filter>` expression for the
  structure-eval call. To pin in implementation.
- Q8.B: edge behavior if the agent edits flake mid-script. Probably
  not worth special-casing.

## Area 9 — Coexistence / migration  → `09_migration.md` (drafted)
Resolved: **hard cut**, no toml flag, no parallel paths. Legacy
`packages/` / `preamble.py` silently ignored if present.
`.fxtr_workstation.toml` schema unchanged. Demos
(`basic_test_demo` + `fxtr_workstation_demo`) migrated as part of
this work; intermediate broken states acceptable. Ship both an
in-repo example workdir and a `nix flake init` template that
points at it.
Still open:
- Q9.A: `nix flake init` template mechanics — to pin in
  implementation.
- Q9.B: cosmetic package renaming in migrated demos.

## Area 10 — Nix infra & fxtr2 source  → `10_nix_infrastructure.md` (drafted)
Resolved: fxtr repo owns a flake exposing `fxtr2` derivation +
`lib.fxtr.*` helpers; user flakes consume via flake input;
spike's path-walk problem dodged by owning fxtr2 inside the
source-repo flake. Helper list updated for the drv-pointer
redesign (Area 12) and combined sandbox (Area 02).
Still open:
- Q10.A: flake location in fxtr repo (top-level vs `extras/...`).
  Lean: top-level.
- Q10.B: how the fxtr flake builds fxtr2 — uv2nix + git-dep on
  fxtr (per the working spike).
- Q10.C: CI/test gating on nix availability.
- Q10.D: scaffold/template for new workdirs.
- Q10.E: dev iteration workflow (path:input vs git input).

## Area 11 — Failure / UX  → `11_failure_and_ux.md` (drafted)
Resolved: success-path `--- nix ---` setup header at the top of
every `run_script` response (eval/sandbox each timed, cached
marked); distinct error paths for "no flake.nix" / missing
`fxtr.workstation.sandbox` / eval failure / up-front build failure
/ in-script `NixEntityBuildFailed`. Full nix stderr written to
`<workdir>/.tmp_outputs/<timestamp>_<id>.log`; agent reads via
their normal file tools. No agent-facing build-progress in v0;
nix stderr streams to MCP's own stderr.
Still open:
- Q11.A: tail-line count (lean N=20).
- Q11.B: `.tmp_outputs/` cleanup policy. v0 = no cleanup.
- Q11.C: candidate follow-up — migrate `get_full_exception` to the
  same file-based mechanism. Simplification; not v0 blocker.

## Area 12 — Service references: git-ref spawn, drv-hash identity  → `12_nix_drv_reference.md` (rewritten 2026-05-30)
Structural change reversed-and-replaced: code is **not** an entity and
**not** a shipped `.drv` — it's `(git_commit, flake_attr, flake_path?)`
the spawner builds from a nix git flake ref. This deletes the entire
`unsafeDiscardOutputDependency` / CBOR-closure-scan / sidecar-symlink
apparatus (git is the transport + the pin). The `.drv` hash is
repurposed as the *forward-looking identity* key (equality/dedup),
derived lazily from `(commit, attr)`; no v0 route. Spawner configured
with one git repo; trust is **connection-level** (the workstation
trusts authorized clients, like a nix remote builder — **no repo/ref
allowlist**, ever; building is no more privileged than `run_script`).
Multiple agents work via a shared bare repo (no extra machinery). Old
bundle path → `fxtr_sandboxing/deprecated/`. Stage-1 closure-scan code
is now orphaned — removed in Stage 3.
Still open:
- Q12.A: exact nix CLI/API for the spawner's git-ref eval+build.
- Q12.B: `mkDockerService` output shape.
- Q12.C: cosmetic location for deprecated entity types.
- Q12.E: wrapper module `fxtr_sandboxing/nix.py` (lean: yes; also home
  for the future `.drv`-hash identity helper).
- (Q12.D retired — the closure-scan mechanic it validated no longer
  exists.)

## Cross-cutting / not yet sorted
(Empty — add things here as we discuss.)
