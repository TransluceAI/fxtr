# Stage 7 — Nix-driven container sandboxing

Add a **container** variant of the git-referenced service path: a
`fxtr.services.<name>` attr whose buildable leaf is a
**nix2container** OCI image instead of a wasm `sitePackages`, a
`NixContainerServiceDefn` spawn reference with the *same shape* as
`NixWasmServiceDefn`, and a spawner arm that builds the image manifest
via nix, delivers it into the container runtime via the
nix2container-patched **skopeo**, and launches it through the existing
UDS/RPC plumbing. This is the follow-up the design notes called
`mkDockerService` (followups.md "Docker service"), renamed to
**Container/OCI** and rebased from `dockerTools` onto
[nix2container](https://github.com/nlewo/nix2container) so the full
image tarball is never materialized in the store.

Design discussion settled 2026-06-04 (see
`stage7_nix_container_scratch.md` for the full decision log):

- Same git-ref spawn shape as wasm (`(git_commit, flake_attr,
  flake_path?)`); naming is Container/OCI, not Docker.
- Every `fxtr.services.<name>` attr (wasm **and** container) gains a
  **`kind` discriminator leaf** (`"wasm"` / `"container"`) that the
  spawner checks per spawn.
- The container attr has **no `entryPoint` leaf** — the entry point
  lives only in the image's baked `Cmd`. The contract is "any
  container image that launches into a fxtr service runner";
  `mkContainerService` is a convenience, not a constraint.
- Image delivery v0 is the **minimal swappable thing**: skopeo →
  `oci-archive` tar → `<runtime> image load` → delete tar, with the
  runtime's image store as the cache. A podman standardization is the
  expected next change; the declaration surfaces (nix-side +
  spawner-side) must not change when delivery is swapped.
- skopeo comes from a **pinned `github:nlewo/nix2container` ref built
  for the host system** by `fxtr_sandboxing/nix.py` (host tooling — a
  deliberate exception to canonicalSystem).
- Images are canonicalSystem (x86_64-linux), run via **Rosetta** on
  Apple Silicon. No platform metadata leaf on the flake attr — the
  image self-describes. **Revised by the chunk-1 spike (2026-06-04):**
  Apple Container does *not* auto-select the only variant of a loaded
  image, so the spawner reads `architecture`/`os` from the **image
  JSON it just built** (plain `json.load`, no nix eval) and always
  passes `--platform <os>/<arch>` to `run`. Also a **host-env
  prerequisite**: Rosetta on macOS < 15.5 can't parse auxv entries
  passed by guest kernels ≥ 6.11 (`unhandled auxillary vector type
  29`); Apple Container's default kernel is 6.18 → pin it to Kata
  3.12.0's 6.1.62 via `container system kernel set` (done on this
  machine; recipe + details in the spike's NOTES.md at
  `/Users/daniel/extgit/spikes/2026-06-04-nix-container-derisk/`).
  Drop the pin on macOS 26.
- Deprecated container paths (`DockerTaskBundle`,
  `PyContainerTaskBundle`) are **left untouched**; removal is a later
  cutover.

## Flake schema addition

```
fxtr.services.<name>
  # wasm (existing, + new kind leaf):
  { kind = "wasm"; sitePackages = <drv>; entryPoint = "module:function"; }
  # container (new):
  { kind = "container"; image = <drv → nix2container JSON manifest>; }
```

- `kind` is a plain metadata leaf, read via
  `nix eval --json <ref>#<attr> --apply 's: s.kind or null'` (lazy —
  selecting it forces no builds; `--apply` keeps a missing leaf from
  erroring uglily). `null` → clear "no kind declared (pre-stage-7 lib
  or hand-built attr)" error; mismatch → "this service is kind X;
  spawn it with <the other defn type>".
- `image` is the buildable leaf: a derivation producing a
  nix2container image description (a small JSON referencing store
  paths as layers — no tarball). Built lazily by the spawner with
  `--no-update-lock-file`, exactly like `sitePackages`.
- Hand-built attrs remain legal (Q5.E analog): any
  `{ kind = "container"; image = <nix2container drv>; }` whose image
  `Cmd`s into a process that speaks the fxtr service-runner UDS
  protocol works.

The spawner's **wasm arm becomes strict** about `kind = "wasm"`:
commits whose flakes were built against a pre-stage-7 fxtr lib (no
`kind`) will error with a message naming the fix. Accepted — nothing
depends on old commits.

## Spawn reference

`NixContainerServiceDefn(git_commit: str, flake_attr: str,
flake_path: str | None = None)` — a `DataclassEntity` with
`define_type(SHARED_SANDBOX_NS, "nix_container_service_defn")`, field-
identical to `NixWasmServiceDefn`. Two copies per the established
pattern:

- canonical in `fxtr_sandbox_types/entities.py`;
- wire-identical dup in the sandbox bridge
  (`fxtr_ws_mcp_sandbox_bridge/protocols.py`), re-exported from
  `_fxtr_mcp_builtin` — with the wire-identity test extended (mirror
  `test_nix_wasm_service_defn.py`: id-string **and** field list).

`spawn`'s `EntityRef[…]` union + isinstance dispatch gain the new type
(also wherever `fxtr_sandbox_types/protocols.py` repeats the union).
No new spawner method; no MCP E2E test (the MCP path is identical to
wasm and already covered there).

## `lib.fxtr.mkContainerService`

```
mkContainerService { src, name?, system ? canonicalSystem, sourcePreference ? "wheel" }
→ { kind = "container"; image = <nix2container drv>; }
```

1. Read the single `[project.entry-points."fxtr.service"]` entry from
   `src/pyproject.toml` (same `fromTOML` + fail-loudly convention as
   `mkWasmService`; factor the shared ~10-line reader).
2. `mkVenv { workspaceRoot = src; … }` — a **full-Linux** venv (no wasi
   `environ` override). The full-fxtr2 venv path is already proven by
   `fxtr_nix_writer`'s venv.
3. `nix2container.buildImage` with (**revised during chunk 2**, see the
   binfmt finding under the chunk):
   `Cmd = [ "${basePython}/bin/python" "-m" "fxtr_service_runner" <entryPoint> ]`
   and `Env = [ "PYTHONPATH=${venv}/lib/python3.12/site-packages" ]` —
   the **base** interpreter + an explicit module path, *not*
   `${venv}/bin/python`. pyproject-nix venvs activate via an
   argv[0]-carrying exec shim, and argv[0] does not reliably survive
   binfmt_misc-based emulation (translators are often registered
   without preserve-argv0 — e.g. Rosetta under Apple Container and
   similar VM-backed runtimes), so the venv python silently degrades to
   the bare interpreter there. The venv is a *packaging intermediate*;
   the runtime contract is PYTHONPATH (identical under emulation and
   native; `importlib.metadata` discovery is sys.path-based so the
   runner still works). Same "venv as packaging, path as runtime"
   pattern as the wasm side's `_mkGuestSitePackages`. The interpreter
   is pinned explicitly in the helper so the venv and Cmd can't drift.
   Entry point pinned explicitly (not left to the runner's discovery
   scan) so a dep declaring its own `fxtr.service` entry can't make it
   ambiguous. Self-contained nix-only image — no `fromImage` base
   distro. Default layering in v0 (an explicit venv `buildLayer` is a
   noted option if rebuild churn hurts).

The repo-root `flake.nix` gains a `nix2container` input
(`nixpkgs.follows`), threaded into `nix/lib/default.nix`;
`mkWasmService` gains `kind = "wasm"` in its returned attr. The old
`mkDockerPackageBundle` `notYet` stub in `default.nix` is replaced by
`mkContainerService`.

Guest pyproject convention (used by the templates): deps
`fxtr-service-runner` + `fxtr-sandbox-rpc-adapter[fxtr2]`; comment-flip
`[tool.uv.sources]` pairs for those plus `fxtr-sandbox-types`, `fxtr2`
(subdir `v2`) and `fxtr` v1 (repo root — uv only honors the workspace
root's sources, so fxtr2's transitive non-PyPI deps are redeclared,
same as `fxtr_nix_writer`); mashumaro git pin +
`extra-build-dependencies` for the sdist-only deps; **no** wasi
`[tool.uv] environments` restriction (linux lock).

## Spawner-side resolution

**`fxtr_sandboxing/nix.py` additions** (same plain-`RuntimeError`,
`--no-update-lock-file`, stderr-inline conventions):

- `eval_service_kind(repo, commit, attr, flake_path) -> str | None` —
  the `--apply 's: s.kind or null'` eval.
- `build_image(repo, commit, attr, flake_path) -> Path` — `nix build`
  of `<attr>.image` (`--no-link --print-out-paths`), returns the image
  JSON store path.
- `build_skopeo() -> Path` — `nix build
  github:nlewo/nix2container/<PINNED_REV>#skopeo-nix2container` for the
  **host** system; returns `<out>/bin/skopeo`. Rev pinned as a module
  constant (independent of the flake-input pin; acceptable v0 drift,
  noted in followups).

**`fxtr_sandboxing/nix_container_runner.py`** (new):

- `_load_image_into_runtime(image_json, skopeo, runtime) -> str` — the
  swappable delivery step. Image ref `fxtr-nix-service:<storehash>`
  (the image JSON's nix store hash → content-addressed tag). If
  `<runtime> image inspect <ref>` succeeds, return it (cache hit — the
  runtime's image store is the cache). Else
  `skopeo --insecure-policy copy nix:<image_json>
  oci-archive:<tmp>.tar:<ref>`, `<runtime> image load -i <tmp>.tar`,
  delete the tar, return the ref. (Tarball exists only transiently and
  only on cache miss; on a future podman/docker daemon path it
  disappears entirely.)
- `_run_nix_container_internal(image_ref, *, transport_server)` —
  async CM yielding the accepted task-socket `SocketStream`. Same
  UDS scaffolding as `container_runner._run_container_internal` but
  radically simpler: no entity loads, no source mounts, no env, **no
  trailing command** (Cmd is baked into the image config). Reuses
  `accept_or_exit` / `transport_accept_loop` /
  `teardown_container_proc` from `_container_common`. We deliberately
  do **not** extract the remaining listener/teardown scaffolding shared
  with the two deprecated runners — they're frozen; when they're
  deleted this becomes the only copy.

**Spawner arm** `_spawn_runner_nix_container`: guard `git_repo_path` →
`eval_service_kind` must be `"container"` → `build_image` →
`build_skopeo` → `_load_image_into_runtime` →
`_run_nix_container_internal` → `SocketJSONStream` → `open_rpc_scope`
→ `task_status.started` (same shape as `_spawn_runner_container`).
The **wasm arm** gains the matching `kind == "wasm"` check before its
existing eval/build steps.

Arch at run time (**settled by the chunk-1 spike**): the runtime does
*not* auto-select the sole platform variant, so
`_run_nix_container_internal` (or the load step) reads
`architecture`/`os` out of the image JSON the spawner just built and
passes `--platform <os>/<arch>` on `run`. The image JSON is the single
self-describing source — still no arch metadata on the flake attr and
no nix eval for it.

## Templates & tests

All under `projects/fxtr_sandboxing/fxtr_sandboxing/tests/` (test
templates only — no `examples/` additions; the user-facing flow is:
copy a template, commit, construct the defn, spawn):

- `_workdir_templates/container_greeter/` — port of
  `examples/container_rpc_task/task_src` (`GreeterProto` +
  `platform_info()`) as a workdir flake using `mkContainerService`,
  full-fxtr2 guest pyproject per the convention above.
- `_workdir_templates/container_transporter/` — port of
  `examples/container_transport_task/task_src` (`TransporterProto`,
  `[http]` extra).
- `_workdir_templates/transporter/` — **wasm** port of
  `examples/transport_task` (`[fxtr2-portable,http]`, wasi-restricted
  lock like the greeter template) — closes the transport-coverage gap
  for the git-ref wasm path while we're here.

Tests (skip without a container runtime where applicable; `deadline`
markers for cold builder offloads, mirroring `test_nix_spawner.py`):

- `test_nix_container_spawner.py` — prepare_workdir_repo the container
  greeter → spawn `NixContainerServiceDefn` → connect → `greet` /
  `add` / `platform_info` (asserts the call crossed the container
  boundary); spawn-twice exercises the image-cache (inspect-hit) path;
  kind-mismatch test (wasm defn at a container attr and vice versa →
  helpful error); no-`git_repo_path` guard.
- `test_nix_container_transport.py` — container transporter +
  `env.add_transport` route → `http_get` round trip (mirror of
  `test_transport_e2e_container.py` over the new path).
- wasm transporter test (in `test_nix_spawner.py` or a sibling) —
  mirror of `test_transport_http_e2e_wasm.py` spawning by
  `NixWasmServiceDefn` instead of `deprecated.prepare`.

## Implementation chunks (ordered)

1. ✅ **[DONE 2026-06-04] Manual pipeline spike** (at
   `/Users/daniel/extgit/spikes/2026-06-04-nix-container-derisk/`, see
   its NOTES.md). All steps validated: (a) x86_64-linux image manifest
   built via the remote builder, **all layer store paths land in the
   local store** (output closure); (b) `skopeo-nix2container` builds +
   runs natively on aarch64-darwin — note it has **two outputs**
   (`out`/`man`), `nix.py` must select `out`; spiked at nix2container
   rev `76be9608a7f4d6c985d28b0e7be903ae2547df3e` (candidate pin);
   (c) copy → `oci-archive` → `image load` round-trips with the tag
   intact; (d) amd64 + mounted-UDS round-trip works under Rosetta.
   **Two findings folded back into this plan:** the mandatory
   `--platform` flag read from the image JSON (runtime won't
   auto-select the sole variant), and the host-env guest-kernel pin
   (Rosetta auxv bug on macOS < 15.5 with kernels ≥ 6.11 — pinned to
   Kata 3.12.0 / 6.1.62 on this machine; recipe in NOTES.md).
2. ✅ **[DONE 2026-06-04] `mkContainerService` + `kind` leaves.**
   nix2container flake input (locked at `76be960`, matching the spike);
   `nix/lib/mkContainerService.nix`; `kind = "wasm"` added to
   `mkWasmService`; shared `_readServiceEntryPoint.nix` factored;
   `notYet`/`mkDockerPackageBundle` stub removed; wired into
   `default.nix`. The `container_greeter` template (chunk-6 material)
   was pulled forward as the validation vehicle: `prepare_workdir_repo`
   + `uv lock` resolve the full-fxtr2 git-source dep set fine; `nix
   eval --apply 's: s.kind or null'` → `"container"` without forcing a
   build; the image builds via the git ref `--no-update-lock-file`;
   skopeo-copy + load + run executes the service runner (fails only on
   the absent task socket, as expected pre-spawner) and all imports
   work under Rosetta. **Finding (design change):** the image Cmd uses
   the base interpreter + `PYTHONPATH` instead of `${venv}/bin/python`
   — pyproject-nix venv activation rides on argv[0], which binfmt_misc
   emulation drops (no preserve-argv0 in Rosetta-style translator
   registrations), so venv-python Cmds silently degrade to the bare
   interpreter under emulation. The venv is a packaging intermediate;
   PYTHONPATH is the runtime contract (matches the wasm
   `_mkGuestSitePackages` pattern). Discussed + settled in chat.
3. ✅ **[DONE 2026-06-04] `NixContainerServiceDefn`** — canonical entity
   (`fxtr_sandbox_types/entities.py` + package re-export), bridge dup
   (`fxtr_ws_mcp_sandbox_bridge/protocols.py`), `_fxtr_mcp_builtin`
   re-export (`builtins.py`), wire-identity tests
   (`test_nix_wasm_service_defn.py` parametrized over both defns +
   a distinct-ids check; 7 passed). CODE_OVERVIEWs updated.
4. ✅ **[DONE 2026-06-04] nix.py additions** — `eval_service_kind`
   (`--json --apply 's: s.kind or null'`), `build_image`,
   `build_skopeo` (pinned rev constant `76be960…`, `^out` selector for
   the two-output drv; builds skopeo 1.21.0 from the pin's own lock).
   Smoke-tested all three against a prepared container_greeter workdir.
5. ✅ **[DONE 2026-06-04] `nix_container_runner.py` + spawner arm** —
   delivery function (`_load_image_into_runtime`: inspect-hit cache,
   skopeo → transient oci-archive → `image load`), run CM (`--platform`
   from the image JSON, no trailing command),
   `_spawn_runner_nix_container`, spawn-union extension (impl +
   `SandboxSpawnerProto`, which had also never gained
   `NixWasmServiceDefn` — both added), shared `_require_git_repo` +
   `_check_service_kind` helpers (wasm arm now kind-checked too).
   E2E green: `test_nix_container_spawner.py` (greet/add/platform_info
   + image-cache second spawn; kind-mismatch error; no-repo guard) and
   no regressions in `test_nix_spawner.py` / `test_spawner.py`
   (11 passed).
6. ✅ **[DONE 2026-06-04] Templates + tests** — `container_greeter` +
   `test_nix_container_spawner.py` landed early (chunk 2/5 validation);
   `container_transporter` + `test_nix_container_transport.py` and the
   wasm `transporter` template + `test_nix_wasm_transport.py` landed
   here. Each transport test covers both surfaces (raw byte echo via
   `connect_transport` + HTTP via `get_client`) over its git-ref spawn
   path. All green (2 passed, ~1 min warm).
7. ✅ **[DONE 2026-06-04] Docs** — `projects/fxtr_sandboxing/CODE_OVERVIEW.md`
   + `nix/CODE_OVERVIEW.md` updated as features landed (chunks 2–6);
   `overview.md` flake schema / helper list / scope / stage map updated;
   `followups.md`: `mkDockerService` marked landed, added
   deprecated-path-removal, podman-standardization, and
   skopeo-pin-drift follow-ups; "Docker (later)" banners in
   `design_notes/05` / `12` point here.

## Implementation loose ends (post-stage notes)

- `test_nested_spawn_wasm_inner::test_nested_spawn_relay_http_get` is
  **xdist-flaky** (times out at the conftest's default 30s deadline
  under `-n auto` while it builds two eryx sandboxes; passes standalone
  and serially). Pre-existing test, untouched by this stage — it just
  lacks the `deadline(seconds=...)` marker the other heavy tests have.
  Trivial fix when it next annoys someone.
- The built skopeo is 1.21.0 (the pinned nix2container rev's own
  committed lock); the chunk-1 spike's was 1.23.0 (same rev but with a
  `nixpkgs.follows` override). Both carry the `nix:` transport; the
  in-code pin uses upstream's lock for full reproducibility.
- `SandboxSpawnerProto.spawn`'s union annotation had never gained
  `NixWasmServiceDefn` in stage 3 (worked because `EntityRef` decode is
  structural); both defn types were added in chunk 5.
- The greeter image is currently a single ~500MB layer; nix2container's
  explicit `buildLayer` split (deps vs app) remains the noted option if
  rebuild/delivery churn hurts.

## Follow-up: podman standardization (not in this stage)

The expected next change: drop the docker / Apple Container split and
standardize on **podman** (virtualized on macOS like Apple Container).
Delivery then becomes skopeo → podman's docker-compatible API socket
(or `containers-storage:` from inside the machine), with no tarball
ever; the bigger version puts a **nix-builder container inside
podman** that owns building *and* image delivery, so the host needs
neither nix nor skopeo. Stage 7 isolates everything that would change
behind `_load_image_into_runtime` + `get_runtime()` — the nix-side and
spawner-side declaration surfaces are final.

## Relevant design notes & code

- `followups.md` "Docker service (`mkDockerService`)" — the item this
  stage lands (renamed).
- `design_notes/12_nix_drv_reference.md` — spawn-ref model, trust
  model, "Other modes (later)".
- `design_notes/05_sandbox_bundle.md` — service attr shape, "Docker
  (later)".
- `design_notes/11_failure_and_ux.md` — spawn errors raise normally,
  stderr inline.
- `projects/fxtr_sandboxing/`: `spawner.py` (`_spawn_runner_nix_wasm`
  + `_spawn_runner_container` — the two arms this one hybridizes),
  `nix.py`, `container_runner.py` + `_container_common.py` (UDS
  scaffolding + shared helpers), `fxtr_service_runner/` (the
  in-container shim, already git-installable),
  `tests/test_nix_spawner.py` + `tests/_workdir_templates/greeter/`
  (the patterns the new templates/tests mirror),
  `examples/container_rpc_task/` + `examples/container_transport_task/`
  (the deprecated demos being ported).
- `nix/lib/` (`mkWasmService.nix`, `mkVenv.nix`, `default.nix`),
  `nix/fxtr_workdir_harness/` (`prepare_workdir_repo`),
  `nix/fxtr_nix_writer/pyproject.toml` (the full-fxtr2 uv-sources
  redeclaration convention).

## End-of-stage check

- A workdir flake declaring `fxtr.services.<name> =
  mkContainerService { src = ./.; }` builds `…image` from a git ref
  with `--no-update-lock-file`; `nix eval --apply` of `kind` returns
  `"container"` without forcing the build.
- Spawning a `NixContainerServiceDefn` against a prepared workdir repo
  builds the manifest, delivers via skopeo + `image load` (tar deleted
  after), launches under Rosetta, and the greeter responds
  (`platform_info` confirms Linux-in-container). A second spawn of the
  same commit skips the skopeo copy (runtime image-store hit).
- Transport round-trips work through the new path (container
  transporter) **and** through the git-ref wasm path (wasm
  transporter).
- Kind mismatches and missing-`kind` attrs fail with errors that name
  the right defn type / the fix.
- The bridge dup is wire-identical (test) and importable from
  `_fxtr_mcp_builtin`.
- Existing suites stay green (deprecated container paths untouched;
  wasm spawns still work with the new kind check).
