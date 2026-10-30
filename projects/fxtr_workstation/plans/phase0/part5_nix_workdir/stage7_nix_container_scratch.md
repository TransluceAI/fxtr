# Stage 7 scratch — nix-driven container sandboxing

Working notes behind `stage7_nix_container.md`. Decisions settled in
chat with Daniel (2026-06-04) are marked **[settled]**.

## Decisions from the design discussion

- **[settled] Naming**: "Container"/"OCI", not "Docker".
  `NixContainerServiceDefn`, `lib.fxtr.mkContainerService`,
  `_spawn_runner_nix_container`, `nix_container_runner.py`.
- **[settled] `kind` discriminator leaf** on every `fxtr.services.<name>`
  attr (`"wasm"` / `"container"`), including retrofitting
  `mkWasmService`. The spawner checks it per spawn.
- **[settled] No `entryPoint` on the container attr.** Dangling metadata
  drifts; the entry point lives only in the image's `Cmd` (baked by
  `mkContainerService` as `python -m fxtr_service_runner <ep>`). The
  contract is "any container image that launches into a fxtr service
  runner" — the helper is a convenience, not a constraint on how the
  image/venv is built.
- **[settled] Image transport v0 = minimal, swappable**: tar-pipe
  (skopeo → `oci-archive` → `<runtime> image load` → delete tar) with
  the runtime's own image store as the cache (skip the copy when
  `image inspect` already finds the tag). Podman standardization is the
  likely *next* change — see "Podman follow-up" below; keep the
  nix-side + spawner-side declaration surface stable so only the
  delivery function changes.
- **[settled] skopeo provisioning v0** = pinned
  `github:nlewo/nix2container/<rev>#skopeo-nix2container` built by
  `fxtr_sandboxing/nix.py` for the **host** system (it runs host-side
  and reads the local store — a deliberate exception to
  canonicalSystem, like dev tooling). Eventually a nix-builder
  container inside podman could own this.
- **[settled] Arch**: images are canonicalSystem (x86_64-linux); run via
  Rosetta on Apple Silicon. No `system`/platform metadata leaf on the
  attr. **Spike outcome (2026-06-04):** the original "no arch handling
  at all" stance was falsified — Apple Container resolves a loaded
  image index against the *host* platform and won't fall back to the
  sole variant. So the spawner reads `architecture`/`os` from the image
  JSON it just built (plain `json.load`, no nix eval) and always passes
  `--platform <os>/<arch>`. Separately, a host-env prerequisite
  surfaced: Rosetta on macOS < 15.5 aborts on auxv entries from guest
  kernels ≥ 6.11 (`unhandled auxillary vector type 29`; even stock
  amd64 alpine fails) — fixed by pinning Apple Container's default
  kernel to Kata 3.12.0's 6.1.62 (`container system kernel set`); full
  recipe in the spike's NOTES.md
  (`/Users/daniel/extgit/spikes/2026-06-04-nix-container-derisk/`).
- **[settled] Templates**: container greeter **and** container
  transporter, plus a wasm transporter template/test on the way
  (transport coverage for the git-ref wasm path didn't exist). Test
  templates only; no `examples/` additions.
- **[settled] Deprecated paths stay** (`DockerTaskBundle` /
  `PyContainerTaskBundle` arms + old examples untouched). Removal is a
  later cutover.
- **[settled] Bridge dup** of `NixContainerServiceDefn` lands; no MCP
  E2E test (spawner-level E2E covers the new mechanics; the MCP path is
  identical to wasm).

## Why nix2container (vs dockerTools)

`dockerTools.buildLayeredImage` materializes the tarball in the store;
`streamLayeredImage` avoids that but still pipes a full tarball per
copy. nix2container builds a small **JSON manifest** referencing store
paths as layers; its patched skopeo (`nix:` transport) reads layer
content straight out of the store at copy time. So:

- `nix build <ref>#<attr>.image` is cheap (manifest + layer closure in
  the store; no tar).
- The tarball exists only transiently for the Apple Container `image
  load` path, and only on image-cache miss. On docker we could copy
  `docker-daemon:` directly (no tar ever); on podman,
  `containers-storage` / the docker-compatible socket. The delivery
  function is the only thing that varies.

## The `kind` check mechanics

`nix eval` of a missing attr path errors uglily, so don't eval
`<attr>.kind` directly. Use `--apply`:

```
nix eval --json "<ref>#<attr>" --apply 's: s.kind or null'
```

Lazy eval means selecting `.kind` doesn't force the buildable leaves.
Returns `"wasm"` / `"container"` / `null`. `null` → clear error ("the
service attr declares no `kind`; rebuild against a current fxtr lib or
add `kind = ...` to a hand-built attr"). Mismatch → "this service is
kind 'container'; spawn it with NixContainerServiceDefn" (and vice
versa).

Note this makes the wasm arm **strict**: spawning a commit whose flake
was built against a pre-stage-7 fxtr lib (no `kind` emitted by the old
`mkWasmService`) now errors. Accepted — nothing production depends on
old commits; the error message names the fix.

## mkContainerService internals

```
mkContainerService { src, name?, system ? canonicalSystem, sourcePreference ? "wheel" }
→ { kind = "container"; image = <nix2container drv>; }
```

1. Read the single `[project.entry-points."fxtr.service"]` entry from
   `src/pyproject.toml` — same `fromTOML` convention + validation as
   `mkWasmService` (factor the little reader into a shared
   `lib/_readServiceEntryPoint.nix` or just duplicate the ~10 lines;
   lean: factor, it's pure).
2. `mkVenv { workspaceRoot = src; inherit system sourcePreference; }` —
   a full-Linux venv, **no** wasi `environ` override (unlike
   `_mkGuestSitePackages`). The full-fxtr2 venv path is already proven
   by `fxtr_nix_writer`'s venv (`fxtr2-import-check`).
3. `nix2container.buildImage` (`inputs.nix2container.packages.${system}.nix2container`)
   with `Cmd = [ "${basePython}/bin/python" "-m" "fxtr_service_runner"
   entryPoint ]` + `Env = [ "PYTHONPATH=${venv}/lib/python3.12/site-packages" ]`.
   **Revised during chunk-2 validation:** the first cut used
   `${venv}/bin/python`, which silently degraded to the bare interpreter
   in-container — pyproject-nix venvs activate via a 15KB exec shim that
   re-execs the base python with argv[0] = the venv path, and argv[0]
   does not survive binfmt_misc-based emulation (translators like
   Rosetta under Apple Container and similar VM-backed runtimes are
   registered without the preserve-argv0 flag). No userspace workaround
   preserves argv0 through that exec, so the helper treats the venv as a
   packaging intermediate and PYTHONPATH as the runtime contract — the
   container-side analog of `_mkGuestSitePackages` ("venv as packaging,
   path as runtime"), pointing in place instead of copying out (keeps
   per-package store paths for layer granularity). Semantic deltas vs a
   real venv (all reviewed + accepted): `sys.prefix`/`sys.executable` =
   base interpreter; isolation-by-construction instead of
   `include-system-site-packages=false` (nixpkgs base python's
   site-packages is empty); PYTHONPATH sorts before the stdlib (venv
   site-packages sorts after); console scripts unused;
   `importlib.metadata` entry-point discovery unaffected (sys.path
   -based). The interpreter is pinned explicitly (`basePython =
   pkgs.python312`, passed to mkVenv) so venv and Cmd can't drift.
   Pinning the entry point explicitly (rather than relying on the
   runner's discovery scan) avoids ambiguity if a dep ever declares its
   own `fxtr.service` entry.
4. Layering: v0 uses default layering. If rebuild churn hurts, add an
   explicit `layers = [ (buildLayer { deps = [ venv ]; }) ]` so app
   edits don't re-create the venv layer — noted as an option, not done.

No base distro (`fromImage`): self-contained nix-only image. The
runner shim needs no shell. (The old `2026_05_15_nix_spike` notes the
fromImage merged-/usr footgun; we sidestep it entirely.)

Repo flake gains the `nix2container` input (`inputs.nixpkgs.follows =
"nixpkgs"`), threaded into `nix/lib/default.nix`.

## Guest pyproject shape (templates)

Full-fxtr2 flavor, mirroring the deprecated Dockerfile's wheel set but
as uv git sources via the comment-flip convention:

- deps: `fxtr-service-runner`, `fxtr-sandbox-rpc-adapter[fxtr2]`
  (+ `[http]` for the transporter).
- `[tool.uv.sources]` comment-flip pairs for `fxtr-service-runner`,
  `fxtr-sandbox-rpc-adapter`, `fxtr-sandbox-types`, `fxtr2`
  (subdirectory `v2`), **`fxtr`** (v1 — fxtr2's transitive dep —
  subdirectory repo root). uv only honors the workspace root's
  sources, so transitive non-PyPI sources must be redeclared (same
  convention as `fxtr_nix_writer`'s pyproject).
- mashumaro git pin + `[tool.uv.extra-build-dependencies]` for the
  sdist-only deps (cbrrr, sqlitedict, ...) — copy from
  `fxtr_nix_writer` / greeter as applicable.
- **No** `[tool.uv] environments` wasi restriction (that's the wasm
  guest's knob); the lock targets linux.

`fxtr_service_runner` is already a plain hatchling package
(`projects/fxtr_sandboxing/fxtr_service_runner`) — git-source
installable as-is; its own `[tool.uv.sources]` entry for the adapter is
irrelevant when consumed as a git dep (the consumer redeclares).

## Image delivery + naming

`_load_image_into_runtime(image_json, skopeo, runtime) -> str`:

- Image ref: `fxtr-nix-service:<storehash>` where `<storehash>` is the
  nix store hash component of the image-JSON path — content-addressed,
  so identical builds collide onto the same tag (cache hit).
- `<runtime> image inspect <ref>` success → return ref (no copy).
- Else `skopeo --insecure-policy copy nix:<image_json>
  oci-archive:<tmp>.tar:<ref>` → `<runtime> image load -i <tmp>.tar` →
  delete tar → return ref.
- `--insecure-policy` because there's no signature policy for the
  `nix:` transport; the trust boundary is the nix store, not sigstore.
- docker's classic `image load` prefers docker-archive; if we ever care
  on docker, switch the transport per runtime (`docker-archive:` /
  straight `docker-daemon:`). v0 targets Apple Container; keep a
  runtime switch only if it falls out naturally.

Run command: `<runtime> run --rm --network none -v task.sock -v
transport.sock <ref>` — **no trailing command** (Cmd baked into image
config), no env, no source mounts. Reuses `accept_or_exit` /
`transport_accept_loop` / `teardown_container_proc` from
`_container_common`. We deliberately do **not** extract the remaining
listener/teardown scaffolding shared with `container_runner` /
`py_container_runner` — those are frozen deprecated code; when they're
deleted, the new runner becomes the only copy.

## Spawner arm

`_spawn_runner_nix_container(defn, teardown_event, *, task_status)`:

1. guard `git_repo_path` (same message as wasm arm)
2. `kind = await nix.eval_service_kind(...)`; require `"container"`
3. `image_json = await nix.build_image(...)` (`<attr>.image`,
   `--no-update-lock-file`)
4. `skopeo = await nix.build_skopeo()` (pinned ref, host system,
   `--no-link --print-out-paths`, returns `<out>/bin/skopeo`)
5. `image_ref = await _load_image_into_runtime(...)`
6. `_run_nix_container_internal(image_ref, transport_server=...)` →
   `SocketJSONStream` → `open_rpc_scope` → `task_status.started` →
   park on teardown_event (same shape as `_spawn_runner_container`).

Wasm arm gains step 2 with `"wasm"` required.

`spawn`'s union + isinstance chain gains `NixContainerServiceDefn`.
Check whether `fxtr_sandbox_types/protocols.py` repeats the bundle
union in `SandboxSpawnerProto` — if so extend there too.

## The load-bearing unknown (retire first)

Stage 3's pattern: validate the scary end-to-end mechanics *before*
building the helpers on top. Here the unknown chain is:

1. nix2container image built for x86_64-linux **via the remote
   builder** — outputs (manifest + layer derivations) come back to the
   local darwin store, and skopeo's `nix:` transport can read them.
2. `skopeo-nix2container` **builds and runs on aarch64-darwin**.
3. Apple Container `image load` accepts the skopeo-written
   `oci-archive` (with the `:<ref>` name annotation surviving).
4. An amd64 image runs under Rosetta on the arm64 VM, mounted UDS
   included. **Spike result:** works, but (a) `--platform linux/amd64`
   is mandatory (no auto-select of the sole variant — spawner reads the
   platform from the image JSON), and (b) only after pinning the
   runtime's guest kernel below 6.11 (Rosetta auxv bug on macOS 15.1;
   Kata 3.12.0 / 6.1.62 pin applied — see the spike's NOTES.md).

Chunk 1 is a manual spike of exactly this chain using a throwaway
`nix2container.buildImage` (even a busybox-ish hello) before any
spawner code.

## Wasm transport template/test (the "on the way" addition)

`tests/_workdir_templates/transporter/` = `examples/transport_task`
ported to the workdir-flake shape (deps
`fxtr-sandbox-rpc-adapter[fxtr2-portable,http]`, comment-flip sources,
wasi `[tool.uv]` restriction like the greeter, `mkWasmService` flake).
Test mirrors `test_transport_http_e2e_wasm.py` but spawns by
`NixWasmServiceDefn` via `prepare_workdir_repo` instead of
`deprecated.prepare`. httpx/httpcore/h11 are pure-python → fine under
the wasi-restricted lock.

## Podman follow-up (thought through, not built)

Target architecture: drop the docker/Apple-Container split and
standardize on **podman** (virtualized on macOS via the same
mechanism). What changes:

- Delivery: skopeo can write to `containers-storage:` directly — but
  only *inside* the podman machine; from the darwin host the practical
  paths are podman's docker-compatible API socket (skopeo
  `docker-daemon:` against it) or `podman load`. nix2container also
  ships `copyToPodman` (we'd keep invoking skopeo ourselves per the
  stage-7 shape).
- Bigger version: a **nix-builder container inside podman** owns
  building AND image delivery — the host then needs neither nix nor
  skopeo; the spawner talks to the builder container. That inverts
  where `nix.py` runs and is the real rearchitecture.
- Stage 7 isolates all of this behind `_load_image_into_runtime` +
  `get_runtime()`, so the swap is local to `nix_container_runner.py`.

## Misc

- Failure surfacing: all new nix/skopeo/load subprocess failures raise
  plain `RuntimeError` with stderr inline (per `11_failure_and_ux.md` +
  the stage5post2 simplification) — they ride back as the `spawn`
  invoke's error.
- The skopeo pin: rev constant in `nix.py` next to a comment; the repo
  flake's `nix2container` input is locked independently in
  `flake.lock`. Two pins that could drift — acceptable for v0 (the
  manifest format is stable); note in followups.
- Skip/marker conventions for the new tests: same as existing container
  tests (skip without runtime) + `deadline(seconds=...)` for builder
  offloads, mirroring `test_nix_spawner.py`.
- `DispatchKey`-style naming nit doesn't apply here; no new dispatch
  keys.
