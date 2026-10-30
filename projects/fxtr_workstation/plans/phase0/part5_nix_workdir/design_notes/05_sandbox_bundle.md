# Sandbox service bundles (git-referenced code)

How an agent's flake declares a sandbox service and how it spawns one.
Replaces the old `prepare_package_bundle` builtin **and** the old
`.drv`-pointer bundle entity.

> **Revised (2026-05-30):** a service is no longer an **entity** the
> agent retrieves. It's a `fxtr.services.<name>` flake attr referenced
> by `(git_commit, flake_attr, flake_path?)` and built by the spawner.
> `mkWasmPackageBundle` → `mkWasmService` (no entity, no `.drv` pointer,
> no `write_wasm_bundle` writer, no closure-scan machinery). The
> structural model lives in `12_nix_drv_reference.md`.

## Mental model

The agent's flake declares a service:

```nix
fxtr.services.my_service = lib.fxtr.mkWasmService {
  src = ./packages/my_service;   # pyproject.toml + src/
};
# → { sitePackages = <drv>; entryPoint = "my_service.main:run"; }
```

`mkWasmService` produces the `fxtr.services.<name>` attr shape: a
buildable `sitePackages` leaf + a plain `entryPoint` string. It does
**not** write any entity and does **not** reference a `.drv` path.

To spawn it, the agent commits the flake (and pushes, for a remote
spawner), then constructs the spawn reference by hand in a script:

```python
from _fxtr_mcp_builtin import NixWasmServiceDefn   # bridge-dup of the type
svc = NixWasmServiceDefn(
    git_commit=current_commit(),         # MCP builtin: HEAD, errors if tree dirty
    flake_attr="fxtr.services.my_service",
    flake_path=None,                     # or repo-subpath of the flake
).store()                                # make the fresh entity host-resolvable
sandbox_ref = await spawner.spawn(svc)
```

`NixWasmServiceDefn` is a `DataclassEntity` — canonical in
`fxtr_sandbox_types`, **duplicated into the sandbox bridge** (same
`define_type` + fields → same wire type) and re-exported from
`_fxtr_mcp_builtin` so the agent can import it. `.store()` is the
standard "make a freshly-constructed entity host-resolvable" idiom; the
ref then flows through the **unchanged** `spawn` (its `EntityRef[…]`
union just gains `NixWasmServiceDefn`, `isinstance`-dispatched to a new
arm — no new spawner method).

No `retrieve_nix_entity` is involved — the agent doesn't pull the
service into the script; it hands the spawner a reference and the
**spawner builds it** from the pinned commit (Area 12). `entry_point`
is read by the spawner from the flake attr, not supplied here.

For v0 the commit/push are **manual** — the agent uses its shell to
`git commit` (and `git push` for a remote spawner). The one MCP
affordance is **`current_commit()`** (Stage 4): a read-only builtin
returning the workdir's git HEAD that **errors if the working tree has
uncommitted changes (incl. untracked files)**. That makes the common
path correct-by-default — you can't accidentally pin a stale commit
while sitting on uncommitted edits (which nix git flakes would ignore
anyway, since they only see tracked files). It does **not** auto-commit
and **cannot** confirm a remote `push` happened (a forgotten push
surfaces as a git-fetch failure at spawn). A richer helper is a later
option (Q5.F). See `09_migration.md`, open-concerns #14.

## `fxtr.services.<name>` attr shape

```
fxtr.services.<name> = {
  sitePackages = <derivation>;   # flat guest site-packages dir (single `out`)
  entryPoint   = "module:function";
};
```

- `sitePackages` is the buildable leaf — built lazily by the spawner.
- `entryPoint` is plain metadata, read via `nix eval`. Putting it on
  the attr (rather than on `NixWasmServiceDefn`) keeps the spawn ref
  minimal and means the entry point is versioned with the code.

## v0 vertical slice: `mkWasmService` (wasm only)

```
mkWasmService {
  src,           # path to package source dir (pyproject.toml + src/)
  ...            # additional knobs TBD; see Q5.B
} → { sitePackages = <drv>; entryPoint = "<read from pyproject>"; }
```

Internally:

1. **Resolve uv-locked deps via uv2nix** through the internal
   `_mkGuestSitePackages` helper (Stage 2), producing the flat, pure-
   python, eryx-runnable site-packages output. The agent's
   `pyproject.toml` lists `fxtr-sandbox-rpc-adapter[fxtr2-portable]`
   (and whatever else) as a git dep on the fxtr repo. `mkWasmService`
   is the **real consumer** of `_mkGuestSitePackages` (the Stage-2
   `guest-site-packages-check` smoke target was the placeholder).
2. **Read `entryPoint`** from the package's `pyproject.toml`
   `[project.entry-points."fxtr.service"]` (same convention as today's
   `prepare.read_entry_point`).
3. **Return the attr** `{ sitePackages = <drv>; entryPoint = "…"; }`.
   No entity, no layered file store, no `.drv`-pointer write.

This is dramatically simpler than the old helper: there's no second
"writer" derivation, no `unsafeDiscardOutputDependency`, no sidecar
symlink, no CBOR closure-scan to validate. The `sitePackages`
derivation *is* the artifact; the spawner builds it directly from the
git ref.

## Spawner-side consumption

Lives in `12_nix_drv_reference.md`. Summary: `SandboxSpawnerImpl.spawn`
gets a `NixWasmServiceDefn` arm that forms a nix git flake ref
(`git+file://<repo>?rev=<commit>&dir=<flake_path>#<attr>`), `nix eval`s
`entryPoint`, `nix build`s `sitePackages`, and feeds the realized path
into the existing eryx-factory launcher. The spawner is configured with
one git repo; locally it shares the agent's nix store (cache hits).

## What gets deprecated

See `12_nix_drv_reference.md` for the full layout (prepare modules +
old entity types → `deprecated/`, `prepare_package_bundle` removed).
v0 cuts the WASM path over to the git-ref model; Docker / py_container
deprecation lands with `mkDockerService`.

## Docker (later) — ✅ landed as `mkContainerService` (Stage 7, 2026-06-04)

Landed per `stage7_nix_container.md`: same git-ref spawn shape
(`NixContainerServiceDefn`), `fxtr.services.<name>` gains a `kind`
discriminator (`"wasm"` / `"container"`; `mkWasmService` emits it too),
and the container attr's buildable leaf is a nix2container `image` with
the entry point baked into the image `Cmd` (no `entryPoint` leaf).

## Open questions

- Q5.A (resolved): one helper per bundle kind. v0 = wasm only.
- Q5.B: `mkWasmService`'s full input schema. v0 starts with `src`; add
  knobs (dep overrides, force-include paths, entry-point override) as
  need arises.
- Q5.C: uv2nix → flat-site-packages reshape details (Stage 2 settled
  the wasm32/wasi marker handling; `mkWasmService` inherits it).
- Q5.D: Docker variant — adapt from
  `extgit/spikes/2026_05_15_nix_spike` under the git-ref model.
  Deferred.
- Q5.E (resolved): hand-built `fxtr.services.<name>` attrs bypassing
  the helper are fine — they just have to expose a buildable
  `sitePackages` + a string `entryPoint`.
- Q5.F (partly resolved): v0 ships `current_commit()` (read-only HEAD +
  clean-tree guard; Stage 4). A *richer* helper (auto-commit, or
  construct the whole defn) is deferred. See `09_migration.md`,
  open-concerns #14.
