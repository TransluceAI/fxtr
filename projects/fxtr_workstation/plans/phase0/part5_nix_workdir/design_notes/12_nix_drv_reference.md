# Service references: git-ref spawn, drv-hash identity

How code (sandbox service bundles) flows to a spawner. The structural
decision of the revised direction: code is **not** wrapped in an
entity and **not** shipped as a built `.drv`. It is referenced by
`(git_commit, flake_attr, flake_path?)`, and the spawner builds it
itself from a nix git flake ref.

> **Revised (2026-05-30):** this note previously described a
> `.drv`-pointer **entity** (`NixWasmTaskBundle(derivation,
> entry_point)`) that the agent retrieved and handed to the spawner,
> kept reachable in the flake closure via
> `builtins.unsafeDiscardOutputDependency` + a CBOR-closure-scan + a
> sidecar `.drv` symlink. **All of that is gone.** Git content-pins the
> whole flake and ships it with `push`; the spawner rebuilds from the
> commit. The `.drv` hash is repurposed as the (forward-looking)
> *identity* key, not the spawn reference.

## Scope

Applies to artifacts that today are "a collection of files packaged as
entity bytes and shipped to another fxtr service" — the sandbox bundle
types (`TaskBundle`, `DockerTaskBundle`, `PyContainerTaskBundle`) and
the `prepare_package_bundle` builtin that produces them.

**Not** affected:

- **MCP `run_script` direct path** (host-built sandbox, Area 02).
- **`fxtr.entities` / `retrieve_nix_entity`** (Area 04) — DATA, read
  by the MCP via `path:`; unrelated to shipping code.
- **Remote entity store** (Area 07) — DATA artifacts. Code no longer
  travels through any store; it travels through git.

## The spawn reference

```
NixWasmServiceDefn(
  git_commit: str,          # commit pinning the whole flake
  flake_attr: str,          # e.g. "fxtr.services.my_service"
  flake_path: str | None,   # repo-subpath of the flake (Area 01); None = repo root
)
```

`entry_point` is **not** on the defn — it lives on the flake attr
(`fxtr.services.<name>.entryPoint`) and the spawner reads it via
`nix eval`. The defn is the minimal "which code, at which commit"
reference; the agent constructs it by hand in `run_script` after
committing (and pushing, for a remote spawner). See `05_sandbox_bundle.md`.

It is a `DataclassEntity` (canonical in `fxtr_sandbox_types`, duplicated
into the sandbox bridge + re-exported from `_fxtr_mcp_builtin` so the
agent can construct it in-script). The agent `.store()`s the fresh
instance to make it host-resolvable, then passes it to the **unchanged**
`spawn` — whose `EntityRef[…]` union gains `NixWasmServiceDefn` and
`isinstance`-dispatches to the new arm. It *references* code rather than
carrying it.

### Why git commit + attr, not a `.drv` path

The old note rejected `flake#attr` references because an *unpinned*
attr "floats relative to the flake's lock." Pinning by **git commit**
removes the float entirely: the commit fixes the flake source *and* its
committed `flake.lock`, so `git+file://<repo>?rev=<commit>#<attr>`
re-resolves to exactly one derivation every time. We get the pinning
guarantee the `.drv` gave us, plus:

- **Portability via plain git.** `git push` moves the code to a remote
  spawner. No `nix copy` of a `.drv` closure, no out-of-band artifact
  transport.
- **No closure-reachability machinery.** The old design needed
  `unsafeDiscardOutputDependency` + CBOR-string closure scanning + a
  sidecar symlink purely to keep a built `.drv` reachable from the
  flake's closure so `nix copy` / GC roots wouldn't lose it. With git
  as the transport, none of that exists.
- **Single source of truth.** The commit *is* the identity of "what
  code, built how" — there's nothing half-built on the client.

### Single-arch, by principle

The `sitePackages` derivation is pinned to the flake's `canonicalSystem`
(see `overview.md`, OQ3). A workflow step names a specific recipe on a
specific arch; to serve a foreign arch you use a builder of that arch,
not a different reference shape.

### Single-output convention

`fxtr.services.<name>.sitePackages` produces one output (`out`): a flat
site-packages tree (the shape
`_bundle_materialize.build_site_packages_for_bundle` produced today,
now constructed by nix via `_mkGuestSitePackages`).

## Spawner-side resolution

The spawner is a workstation service configured (at construction) with
**one git repo** — locally, the agent's workdir repo; remotely, a
(possibly bare) repo the agent pushes to. `SandboxSpawnerImpl.spawn`
gains a dispatch arm for `NixWasmServiceDefn`, parallel to the
deprecated bundle arms. The new arm:

1. Forms the nix git flake ref
   `git+file://<repo>?rev=<git_commit>&dir=<flake_path>#<flake_attr>`
   (`git+ssh://…` / `git+https://…` for a remote repo). **No manual
   `git checkout`/worktree** — nix's git fetcher handles fetch + lock-
   respecting evaluation internally.
2. `nix eval` the attr's `.entryPoint`.
3. `nix build` the attr's `.sitePackages` → realized store path.
4. Uses that path directly as the site-packages dir (replacing the
   per-spawn extract-into-tempdir step `_spawn_runner_wasm` does today),
   builds an eryx factory over it, injects `_ENTRY_POINT` from the
   eval'd entry point, and reuses the rest of the launcher unchanged.

Locally the spawner shares the agent's nix store, so step 3 is a cache
hit whenever the agent already built the attr.

Spawner gains a hard nix dependency (the workstation process already
depends on nix once this work lands). Trust + repo topology: see
"Trust model & topology" below.

## Service identity vs. spawn reference (forward-looking)

A deliberate decoupling that earlier designs conflated:

- The **spawn reference** is the rich `(commit, attr, flakePath?)` —
  what the spawner needs and what an agent authors.
- The **identity** of a service (for equality / dedup) is the resolved
  **`.drv` hash** of `fxtr.services.<name>.sitePackages` — the content-
  pinned, single-arch recipe. Two defns that resolve to the same `.drv`
  are the same service regardless of which commit named it.

v0 implements only the spawn path; nothing surfaces the identity `.drv`
hash yet. Converting a spawn ref to its identity is just
`nix eval --json "<git-ref>#fxtr.services.<name>.sitePackages.drvPath"`,
so the design doesn't preclude it. Recorded so the eventual
equality/dedup story has a clean home.

## Trust model & topology

**Trust is connection-level.** The workstation trusts the agents
authorized to connect to it — like a **nix remote builder**, which
builds whatever a client with access sends it. Building git-referenced
code is **no more privileged than `run_script`**, which already lets an
authorized agent execute arbitrary code on the workstation. So the
boundary is "authorized to connect," and there is **no repo/ref
allowlist** — not in v0, not later. A future deployment hardens *who
can connect*, not *what they may build*. This makes v0's behavior the
intended permanent model, not a stopgap.

**Repo topology.** The spawner/dispatcher is configured with a git repo
and resolves `?rev=<commit>` against it. **Multiple agents need no
extra machinery:** they all push to one shared bare repo (git holds
disconnected histories fine) and each references its own commit by
hash. The one genuinely workstation-wide thing is the **dispatcher
preset** — the browser UI is shared, so "whose preset is active" is a
real question — but that's replace-semantics / UI ownership (Area 06),
not a repo-topology problem, and it exists regardless of repo count.

**Remote** is just git reachability (`git+ssh`/`git+https`) plus a
build environment on the consumer (build deps or a binary cache) —
*same* connection-level trust model, no new trust surface. v0 runs
locally (shared nix store → builds are cache hits); the remote build-
environment piece isn't built yet.

## Other modes (later) — ✅ landed as Stage 7 (2026-06-04)

`mkContainerService` / `NixContainerServiceDefn` (see
`stage7_nix_container.md`) subsume both `DockerTaskBundle` and
`PyContainerTaskBundle` (the latter existed only to avoid baking OCI
SHAs into entities — moot now that a service is a git-built recipe).
Output shape settled on **nix2container** (a JSON manifest referencing
store paths — no image tarball in the store; the spawner delivers via
the patched skopeo's `nix:` transport). The deprecated bundle arms
remain until a later removal cutover (`followups.md`).

## Deprecation of the old path

`prepare_package_bundle` (the sandbox builtin that walked
`<workdir>/packages/`, ran uv, built a `TaskBundle` inline) is removed
from the agent surface. The old machinery moves to
`fxtr_sandboxing/deprecated/`:

- `fxtr_sandboxing/deprecated/` holds `prepare.py`,
  `container_prepare.py`, `py_container_prepare.py`,
  `_prepare_common.py`, `_bundle_materialize.py`, `wheels.py`,
  `_arch_tags.py`, `base_images.py`, `container_config.py`,
  `container_rebuild.py`.
- Old entity types (`TaskBundle`, `DockerTaskBundle`,
  `PyContainerTaskBundle`, `PySourcePackage`, `PyWheelPackage`,
  `PyMultiWheelPackage`, `PyWheelArtifact`, `ArchSpec`, `DockerImage`,
  `SourceMount`) move to a deprecated location inside
  `fxtr_sandbox_types`.
- `SandboxSpawnerImpl.spawn` keeps existing dispatch arms with a
  "deprecated, prefer `NixWasmServiceDefn`" docstring.
- Low-level launchers (`runner.py`, etc.) are **not** moved wholesale —
  the nix path reuses `runner.run_in_factory` and transport plumbing.

v0 lands the wasm path on the git-ref model; Docker / py_container port
in one later cutover.

## Orphaned Stage-1 closure-scan code

Stage 1 built a synthetic closure-scan validation
(`HelloWorldEntity.drv_ref`, `writers.emit_drv_ref_sidecar`, the
`nix-store --query --references` + `nix derivation show` tests) to
de-risk the *old* `.drv`-pointer bundle. The revised direction removes
that mechanic entirely, so this code is dead. **Remove it in Stage 3**
(folded into the bundle/spawner rework). See `stage3_bundle_and_spawner.md`.

## Git-flake-ref resolution: what to validate early

This is the **load-bearing unknown** for Stage 3 (it replaces the old
closure-scan mechanic as "the thing that sinks the stage if found
late"). Retire it before building `mkWasmService` on top. The model is
`nix build "git+file://<repo>?rev=<commit>&dir=<flakePath>#<attr>"`
(`git+ssh://` / `git+https://` for remote). Concretely, confirm:

> **RESOLVED (2026-05-31, Stage 3 chunk 3).** Validated end-to-end by
> building `nix/sandbox_smoke/`'s `fxtr.services.smoke.sitePackages`
> through `prepare_workdir_repo` (see the stage3 plan). Two concrete
> findings that pin the checklist below:
>
> - **Lock-freezing flag = `--no-update-lock-file`.** ("Do not allow any
>   updates to the flake's lock file" — errors rather than silently
>   re-locking, and composes with `?rev=`/`&dir=`.) Note `--frozen` is
>   **not a nix flag** (no subcommand recognizes it; `nix` rejects it as
>   "unrecognised flag") — it was a mistaken assumption, likely cargo/uv
>   terminology. The spawner's `nix.py` wrapper (chunk 4) uses
>   `--no-update-lock-file`.
> - **The pinned lib rev must be *ref-reachable* — for uv, not nix.** The
>   workdir's `uv.lock` carries a `git = "file://<repo>", rev = …` source,
>   and `uv lock` (resolution) fetches via uv's git fetcher, which only
>   retrieves **ref-reachable** commits — an off-branch object (e.g. a jj
>   *working-copy* `@` commit) fails with "failed to find branch, tag, or
>   commit." nix's flake fetcher (build time) resolves off-branch revs
>   fine, so the constraint is purely the uv-lock step. Practical upshot:
>   pin the library to a **committed** rev (a real git HEAD), not a phantom
>   jj working-copy commit. In CI this is automatic (HEAD is the commit
>   under test); locally with jj, `jj commit` first (or point
>   `FXTR_TEST_LIB_REV` at a ref-reachable rev). This is the uv-layer
>   analogue of the `current_commit()` clean-tree guard.

- **`rev=` reachability.** Does nix accept any reachable object, or does
  the commit have to be on a branch/ref? Recent nix wants
  `?rev=<sha>` and may also require `&ref=<branch>` (or `allRefs=1`) to
  *find* the rev on some repos. Pin which incantation works for "an
  arbitrary commit the agent just made," including a commit on no
  branch (detached) and a commit pushed to a bare repo on a disconnected
  history (the multi-history case from "single-repo assumption").
- **Bare vs. worktree repo.** Confirm both resolve: the local common
  case (spawner points at the agent's worktree repo) and the remote
  case (a bare repo the agent pushed to).
- **`rev=` + `dir=` together.** Confirm `&dir=<flakePath>` composes with
  `?rev=` so a subpath flake builds from a pinned commit.
- **Dirty tree is ignored.** Git flakes build from the *committed* tree
  and ignore uncommitted/untracked working-tree changes. This is
  exactly why `current_commit()` (Area 09 / open-concerns #14) guards
  against an agent referencing HEAD while sitting on uncommitted edits —
  validate that the build genuinely reflects the commit, not the dirty
  tree.
- **Force the committed lock; no re-locking (important).** When nix
  evaluates a flake from a git ref, a missing/stale `flake.lock` makes
  it compute an updated lock *in-memory* (fetching inputs) rather than
  erroring — which silently diverges from "what the agent committed"
  and can hit the network. The spawner/dispatcher must run the
  eval/build with **`--no-update-lock-file`** so a missing/out-of-date
  committed lock is a hard **error**, not a silent re-lock — it forbids
  any lock change and composes with `?rev=`/`&dir=`. This makes the
  committed `flake.lock` the single
  source of truth and pairs with `current_commit()` (whose clean-tree
  guard rejects an uncommitted/untracked `flake.lock`). Surfaces as the
  "stale/missing lock" error bucket in `11_failure_and_ux.md`.
- **Cache hit in the shared store.** Locally, confirm that when the
  agent already built `…#…sitePackages`, the spawner's build of the same
  `(commit, attr)` is a store cache hit (no rebuild).
- **`nix eval` of `entryPoint`** over the same git ref returns the
  string without forcing `sitePackages` to build.
- **The workdir's `fxtr` input — don't `--override-input` on the
  lock-frozen path.** `--override-input` is a lock modification, so it
  conflicts with `--no-update-lock-file` (verify the exact failure).
  Resolution: the workdir's committed `flake.lock` is the source of truth
  for `fxtr`, **baked in at scaffold time** — `git+file:///abs/local/fxtr`
  for dev, `github:…` for prod (`09_migration.md`). `--override-input`
  stays an **in-repo-test-only** mechanism, and only on the
  non-lock-frozen `path:` data path (the MCP) — never the
  spawner/dispatcher. Verify in the spike that `--no-update-lock-file` + a
  baked-in local `fxtr` builds against the local checkout.

- **MCP data-path resolution (same spike).** Verify the non-lock-frozen
  bare-path form the MCP uses (Areas 01/04/08): `nix build <bare-path>#…`
  auto-selects `git+file://` inside a repo (inferring `dir=` for a
  subdir flake) and `path:` with no repo; uncommitted *tracked* edits
  are seen; *new untracked* files are invisible (motivating the
  untracked-files notice); and it does **not** require `--no-update-lock-file`.

Centralize all of this in `fxtr_sandboxing/nix.py` (Q12.E) so the
dispatcher's parallel wrapper (Area 06) can mirror whatever works; the
MCP's `fxtr_workstation_mcp/nix.py` mirrors the bare-path/non-frozen
half.

## Open questions

- Q12.A: the specifics above — pin the exact CLI incantation in
  Stage 3 implementation.
- Q12.B: `mkDockerService` output shape. Later.
- Q12.C: final location for deprecated entity types. Cosmetic.
- Q12.E: `fxtr_sandboxing/nix.py` wrapper centralizing the git-ref
  eval/build. Lean: yes (testability + a home for the future
  `.drv`-hash identity helper).
