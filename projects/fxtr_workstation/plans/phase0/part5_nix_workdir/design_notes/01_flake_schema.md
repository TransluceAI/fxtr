# Flake schema & consumption contract

How the agent's workdir `flake.nix` exposes structure, and how the
three consumers (MCP, spawner, dispatcher) read from it.

> **Revised (2026-05-30):** the namespace now has three slices — a
> DATA slice (`fxtr.entities`, consumed by the MCP) and two CODE slices
> (`fxtr.services`, consumed by the spawner; `fxtr.workstation.uiPresets`,
> consumed by the dispatcher). Code is **not** wrapped in entities; it
> is referenced by git commit + flake attr and built by the consuming
> service. See `overview.md` "The pitch".
> **Revised (2026-05-29):** there is no `fxtr.workstation.sandbox` —
> the workstation sandbox is host-built. `flake.nix` is *optional*
> (needed only for nix entities / services / presets).

## Shape

The flake exposes a single top-level attr `fxtr`:

```
fxtr
  .entities                   # DATA (MCP, resolved via bare workdir path: auto-git / path:)
    .<name>
      .type      string discriminator, initially only "fxtr_layer_entity"
      .layer     derivation → layered-file-store dir
      .layerKey  string
  .services                   # CODE (spawner, resolved by git commit)
    .<name>
      .sitePackages  derivation → flat guest site-packages dir
      .entryPoint    string "module:function"
  .workstation
    .uiPresets                # CODE (dispatcher, resolved by git commit)
      .<name>                 # plain attrset of renderer recipes (see 06)
```

### Why three slices

- `fxtr.entities` is "general fxtr data." `fxtr.services` is "code I
  spawn as a sandbox." `fxtr.workstation.uiPresets` is "code that
  renders my types in the browser." Splitting them keeps the DATA and
  CODE domains logically distinct (the central reframing of the revised
  direction) and leaves room to grow each additively. A future hub
  could enumerate "all entities" or "all services" in a flake without
  caring about workstation config.
- `services` sits at top level (like `entities`) because a service is a
  general fxtr artifact, not workstation-specific. `uiPresets` stays
  under `workstation` because it's specific to the browser dispatcher.

## Two resolution mechanisms

The split DATA/CODE domains are resolved differently — this is
deliberate, not incidental.

- **DATA (`fxtr.entities`) → bare workdir path (auto-git), non-frozen.**
  The MCP resolves entity layers by handing nix the **bare workdir path
  with no `path:` prefix**, so nix resolves it adaptively: inside a git
  repo it uses `git+file://` (walking up to `.git`, inferring `dir=`
  itself); with no repo it falls back to `path:` semantics. This is the
  *same fetcher* as the code path, but **non-frozen** — uncommitted
  *modifications to tracked files* take effect on the next
  `retrieve_nix_entity` (fast iteration, no commit needed). Two
  consequences of the git-form: **new untracked files are invisible to
  nix until `git add`** (the MCP surfaces a notice when untracked files
  are present — see Area 04), and source copies are tracker-clean (only
  tracked files, not `.venv`/caches/junk — the win note 10 Q10.E wanted).
  Data-only use with no git repo still works via the `path:` fallback.
- **CODE (`fxtr.services`, `fxtr.workstation.uiPresets`) → git commit.**
  The spawner/dispatcher resolve code via a **nix git flake ref**:
  `git+file://<repo>?rev=<commit>&dir=<flakePath>#<attr>` (and
  `git+ssh://…` for a remote service). The code must be **committed**
  (and, for a remote consumer, **pushed**). The commit content-pins the
  whole flake (source + committed `flake.lock`), so the build is
  reproducible and portable.

Rationale: scratch data you iterate on uncommitted; code that gets
shipped to a long-running service must be pinned and reproducible. The
workdir must be a git repo to use the CODE slices (see
`09_migration.md`).

### `flakePath`

`flakePath` exists *only* to map a git commit → which flake inside the
repo. It rides along wherever a git commit does — i.e. on
`NixWasmServiceDefn` (Area 05) and `set_preset` (Area 06), never on the
MCP's data path (the workdir is already a filesystem path that points
directly at the flake dir).

- Flake at the repo root → `flakePath` unused; everyone agrees.
- Flake *not* at the repo root → the workdir *is* the flake dir, and
  the server is told that dir's path relative to the enclosing git repo.
  That relative path is the `flakePath`, and it's what makes the
  spawner/dispatcher's git-ref resolution land on the same flake the MCP
  sees via `path:`.

## Buildable-leaf / metadata invariants

1. **Every leaf a consumer materializes is a buildable derivation:**
   `entities.<name>.layer`, `services.<name>.sitePackages`, and (Area
   06) each renderer's ESM derivation. The consumer targets them
   individually with `nix build`.
2. **Intermediate attrs + metadata leaves are nix-language values
   only:** `type`, `layerKey`, `entryPoint`, the preset's type-keyed
   routing table, the set of names — all read via `nix eval --json`
   without building anything.

This is idiomatic for flakes that expose structured output: an attrset
of plain values with derivations at the leaves you build.

## Consumer query patterns

- **MCP (data).** `nix eval --json` + `nix build --no-link
  --print-out-paths` of `fxtr.entities.<name>.layer` against the **bare
  workdir path** (auto-git, non-frozen — above). (`04`, `08`.)
- **Spawner (services).** Given `NixWasmServiceDefn(commit, attr,
  flakePath?)`: `nix eval` the attr's `entryPoint`, `nix build` its
  `sitePackages`, both against `git+file://<repo>?rev=<commit>&dir=<flakePath>`.
  (`05`, `12`.)
- **Dispatcher (presets).** Given `(commit, attr, flakePath?)`:
  `nix eval --json` the preset attr (type-id-string-keyed routing
  table — no builds needed for the keys), then `nix build` each
  renderer ESM lazily/eagerly. (`06`.)

`nix build --no-link --print-out-paths` is the canonical output-
discovery mechanism (the MCP convention from Stage 2; consumers never
leave `result*` symlinks).

The **code** consumers (spawner, dispatcher) additionally run eval/build
**`--no-update-lock-file`**: a git-ref flake with a
missing/stale committed `flake.lock` must **error**, not silently
re-lock in-memory. The committed `flake.lock` is the single source of
truth, paired with `current_commit()`'s clean-tree guard (Area 12).

## Layer-sharing convention (data)

The hybrid model (a few layers, many entities) is expressed naturally:
one derivation per layer, many `entities.<name>` manifest entries
sharing it via nix attr-sharing. Content-addressing dedups for free.

```nix
let
  myWheelsLayer = pkgs.callPackage ./build_my_wheels_layer.nix { ... };
in {
  fxtr.entities = {
    foo = { type = "fxtr_layer_entity"; layer = myWheelsLayer; layerKey = "abc"; };
    bar = { type = "fxtr_layer_entity"; layer = myWheelsLayer; layerKey = "def"; };
  };
}
```

## Versioning

No schema versioning fields for now. The `type = "fxtr_layer_entity"`
discriminator can change per entity if needed. General pattern: push
versioning / schema info "down" into the leaves.

## Open questions

- Q1.A (resolved): **no `${system}` in the path.** Principled single
  architecture (`overview.md`; `stage1_open_questions.md` OQ3). System
  pinned inside the flake via `lib.fxtr`'s `canonicalSystem`.
- Q1.E (resolved): `lib.fxtr.pkgs` / `lib.fxtr.pkgsFor system` let a
  downstream flake write layer derivations against a pinned `pkgs`
  without its own `nixpkgs` input.
- Q1.B (resolved): no `mkEntity` helper. Producing helpers construct
  entity attrs internally; hand-rolled literals are fine.
- Q1.C: agent authoring story — ship a template
  (`nix flake init -t …`)? in-repo example to copy? (Both, per Q9.)
- Q1.D: flake eval failure routes to the same surface as a build
  failure at whichever consumer hit it (`11_failure_and_ux.md`).
- Q1.F (new): exact `flakePath` plumbing through `NixWasmServiceDefn` /
  `set_preset` and into the nix git flake ref (`&dir=`). Pin in
  implementation (Stages 3/5).
