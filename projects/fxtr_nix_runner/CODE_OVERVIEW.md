# fxtr_nix_runner

The shared **backend** beneath fxtr's three nix wrappers (spawner
`fxtr_sandboxing/nix.py`, MCP `fxtr_workstation_mcp/nix_util.py`, dispatcher
`nix_configurable_dispatch/nix.py`). It owns the host-vs-podman switch,
builder-container lifecycle, ref/path translation, output materialization,
and in-container image delivery. It sits *beneath* the wrappers — transport,
not policy: the wrappers keep their own flake-ref construction and error
conventions.

**Two tiers.** The **transport tier** (`__init__` + `_backend` / `_host` /
`_podman` / `_delivery` / `_lifecycle` / `_config` and the CLI) is deliberately
lightweight — **anyio only, no fxtr2** — so all three wrappers can depend on it;
`import fxtr_nix_runner` stays fxtr2-free. The **manager tier** (`rehost.py`,
`manager.py`, `entities.py`, `namespaces.py`, `renderers.py`, `serve.py`) *is*
fxtr2-aware — it owns repo-store discovery, the build-time rehost, the read
adapters (entities + namespaces + renderer presets), and serving the whole result
over HTTP. fxtr2 is a hard dependency of the package, but its imports are confined
to the manager modules, which you import explicitly (e.g. `from
fxtr_nix_runner.manager import FlakeManager`); they are not re-exported from
`__init__`.

**More than a "runner".** The name is historical: the transport tier just runs
nix, but the manager tier has grown into the central manager of **fxtr project
state declared via nix** — it owns the repo store, builds and reads the project's
entities + namespaces + renderers, and serves the whole result over HTTP. Think of
it as the *fxtr project-repo manager* that happens to use nix as its build
substrate, not merely a nix wrapper. (A rename to drop "runner" is a plausible
future cleanup; kept as-is for now to reduce churn.)

Design + rationale: [`stage9_nix_runner.md`](../fxtr_workstation/plans/phase0/part5_nix_workdir/stage9_nix_runner.md).

## Mental model

A consumer resolves one backend (`resolve_backend()`, from `FXTR_NIX_BACKEND` +
`~/.config/fxtr/nix_runtime.toml`) and, per repo, opens `async with
backend.builder_for(repo) as h:` once around its whole sequence of nix
operations, calling the handle's `flake_ref_root()` / `run_nix()` / `lock()` /
`materialize()` / `deliver_image()`. Backend ownership is explicit (no
process-global): a long-lived consumer holds one backend and reuses it, so
concurrent same-repo borrows refcount-share a builder; a one-shot caller
resolves per operation.

**No implicit default.** The config (`nix_runtime.toml` — spanning the nix
backend *and* the container runtime) does not guess host-vs-podman.
`load_config()` is total (always succeeds, reports set-vs-unset); the
`require_nix_backend()` / `require_container_runtime()` accessors raise a
`ConfigError` pointing at `fxtr_nix runtime setup` when a required field is
unset. Each consumer (spawner, MCP, dispatcher) resolves the backend **lazily**
on first nix use (a cached `_backend()` accessor) and uses a per-call
`builder_for`, so constructing one needs no config — only an actual nix
operation does. (A persistent, env-lifetime builder handle is a deferred
optimization — see `stage9post1_backend_config_wizard.md`.)

- **host backend** — nix runs on the host against the real `/nix/store`;
  `materialize` is identity, `flake_ref_root` is the resolved path.
- **podman backend** — nix runs inside a per-repo builder container; outputs
  are `materialize`d (closure-copied) into a host-readable bind-mount store,
  and images are delivered to podman by skopeo from inside the store.

## File tree

```
src/fxtr_nix_runner/
  _backend.py    The Backend / Handle interface + NixResult, Materialized, DeliveredImage, ExposedEndpoint + the impure_env_nix_flags helper.
  _config.py     NixRuntimeConfig / PodmanConfig / ContainerRuntime + load_config (env + toml) + require_*.
  perf.py        TRANSPORT TIER (stdlib only, no fxtr2): opt-in perf timing (perf_span / perf_span_sync / record / collect_perf / proc_label + FXTR_PERF_LOG stderr logging). Inert by default; used to instrument the nix + sandbox-spawn phases so a spawn isn't a silent wait.
  testing.py     Test support: skip_if_runtime_unconfigured (public; lazy-imports pytest).
  _lifecycle.py  RefcountedResourceRegistry: keyed, refcounted async resources.
  _host.py       HostBackend / HostHandle — host-nix passthrough.
  _podman.py     PodmanBackend / PodmanHandle — per-repo builder + lazy deliverer containers.
  _delivery.py   Store-side image delivery: skopeo pin, socket discovery, ref/platform, host load.
  _clear.py      `clear()` — sweep containers + volumes + scratch.
  _mask_nix.py   `run-with-masked-nix` — run a command with host nix masked + tripwired.
  _wizard.py     `fxtr_nix runtime` setup wizard: detect → recommend → write nix_runtime.toml; init-podman.
  _fod_lock.py   TRANSPORT TIER (no fxtr2): fod_lock.toml read/write (tomllib read, tomlkit write, a single sorted [hashes] table — exactly what lib.fxtr.fodLockReader reads) + per-slot sentinel hashes + `hash mismatch` stderr parsing. The pure, unit-testable pieces of the FOD lock.
  __main__.py    `fxtr_nix {lock [path],update-hashes [path],serve [path],clear,runtime {setup,init-podman}}` CLI (also `python -m fxtr_nix_runner`). `update-hashes` + `serve` lazy-import the manager-tier engine so the other subcommands stay fxtr2-free.
  rehost.py      MANAGER TIER (fxtr2): rehost_context + RehostInfo — token-authed ephemeral HTTP view of a RepoEntityStore for build-time FOD fetches.
  manager.py     MANAGER TIER (fxtr2): FlakeManager (async-CM: `async with FlakeManager(repo) as mgr` → discover/open repo store + long-lived manager scope) + scope/repo_store exposure + mount_pin (mount a built layer's pin into the manager scope, lazily loadable for the manager's lifetime) + build_span (stand up rehost, wire FXTR_REMOTE_* into run_nix) + entities(span) + namespace(span, name) + list_namespace_names(span) + list_preset_names(span) (enumerate declared fxtr.namespaces.* / renderer presets, cheap eval).
  entities.py    MANAGER TIER (fxtr2): EntitiesReadAdapter — FxtrReadNamespace-shaped view over fxtr.entities.* (eval metadata → build .layer → materialize → mgr.mount_pin into the manager scope). retrieve/try_retrieve/list_entity_names take an optional `nix_location` (default "fxtr.entities", any attr under fxtr) — the workstation MCP's retrieve_nix_entity delegates here with its caller-supplied location, getting remote-pointer resolution + scope-copy for free.
  update_hashes.py MANAGER TIER (fxtr2): the runner-owned `fxtr_nix update-hashes` relock engine — discovers `fxtr.preloads` slot keys (attrNames), decides the target set (pure `plan_relock`: default = new slots + drop-stale; `--all`; explicit `-k`; `--dry-run`), writes sentinels, stages an untracked lock (git add, like flake.lock), drives a `--keep-going` build, parses the `hash mismatch` hashes, and rewrites the `[hashes]` table. In-place content edits aren't auto-detected (use `--all`/`-k`).
  namespaces.py  MANAGER TIER (fxtr2): the namespace read view over fxtr.namespaces.<name> — eval_namespace_names (cheap eval of the declared names; backs mgr.list_namespace_names) + build_namespace_manifest (nix: eval+build+materialize+read the export JSON) + NamespaceReadAdapter (a real FxtrReadNamespace over the manifest + the repo store as deep-fetch source). mgr.namespace(span, name) composes them.
  serve.py       MANAGER TIER (fxtr2): published_serving_context — drive FlakeManager to build a flake's entities (layered) + namespaces (bare "<ns>:<key>") + renderer preset, then assemble + serve the 7a stateless HTTP backend (create_fxtr_app + ServiceRegistry, both provenanced=True, + a flake-driven `dispatcher` ws service) in a background task; yields a PublishedServer handle. Optional `web_dist` mounts a built static SPA (the shared viewer) at / after the API routes (same-origin UI). run_published_server backs the `fxtr_nix serve` CLI (`--web-dist`).
  renderers.py   MANAGER TIER (fxtr2): build a flake's renderer preset (the fxtr.uiPresets.<name> convention) into a servable table. Low-level (Handle-based, fxtr2-free) eval_preset_table (nix eval → renderer/typeEntity drv paths, no realization) + build_drv_outputs (one batched nix build → {drv: Materialized}) — shared with fxtr_workstation's nix/git dispatcher. fxtr2-aware adapter: eval_preset_names (backs mgr.list_preset_names) + build_preset_table (eval → batched build → validate typeEntity ids → mint a RendererEntity per renderer into a scope → RendererTable of RendererEntry{subtype_key, renderer ref}).
tests/
  conftest.py            The parameterized `nix_backend` sweep fixture (host + podman). For podman it mounts the harness lib source (`resolve_lib_ref().repo_path` — the bare mirror in a jj workspace, the repo in plain git) into the builder via `mountable_paths`, so a workdir flake whose `mkVenv` pulls fxtr2 from that git ref can fetch it at build time.
  test_backend.py        Pure backend-neutral helpers (impure_env_nix_flags).
  test_config.py         Config resolution.
  test_lifecycle.py      Refcounted registry behavior (borrow sharing/teardown/failure + pin: no-instantiate, warm-across-borrows, borrow-outliving-pin, failure-retry-under-pin + invalidate: tears-down-and-rebuilds-under-pin, no-op-if-replaced).
  test_perf.py           perf helper: inert by default, span nesting + start-order, task-independent collection (spans on a child task still captured), sink cleanup.
  test_host_backend.py   Host backend (incl. endpoint exposure + run_nix pure passthrough) + the shared-matrix smoke test.
  test_podman_backend.py Podman ref/path logic + mount args + `_exec` transport-failure detection (rc 125 → recycle+raise; non-125 → NixResult) + `_recycle_deliverer` bookkeeping + `LivenessBeacon` (alive→dial-ok, closed→refused) + `_beacon_health_args` shape + live builder/lock/deliver/materialize + live warm-builder-recycled-after-death.
  test_delivery.py       Image-ref/platform helpers + host skopeo-load branches.
  test_mask_nix.py       run-with-masked-nix: passthrough exit codes + tripwire on a leaked nix call.
  test_wizard.py         Wizard: recommendation + nudges + --detect JSON + init-podman argv + write (probes mocked).
  test_rehost.py         Rehost (manager tier, fxtr2): /data round-trip + bearer-token accept/reject + ephemeral bind (in-process RepoEntityStore, no nix).
  test_manager.py        FlakeManager (manager tier, fxtr2, host backend): repo_store by-id resolution + create-if-absent + build_span rehost reachable via its env + BuildSpan.run_nix prepends the rehost env flags (no nix).
  test_entities.py       Manager mount path (manager tier, fxtr2, no nix): a layer with a local entity A referencing a remote-only B, mounted via mgr.mount_pin inside a build span — after the span closes, A loads (local) and A→B resolves from the repo store (lazy-load-after-span). Full eval/build/materialize E2E deferred to the integration step.
  test_fod_lock.py       Pure FOD-lock helpers (no nix/fxtr2): fod_lock.toml round-trip (sorted, quoted special keys, missing-file → empty), sentinel determinism/distinctness/valid-SRI, multi-block + label-order `hash mismatch` parsing.
  test_update_hashes_plan.py  Pure relock-target decision (`plan_relock`, no nix): new-only + drop-stale default (never touches already-locked slots), `--all`, `-k` subset, undeclared `-k` key rejected.
  test_namespaces.py     Namespace read view (manager tier, fxtr2, no nix): NamespaceReadAdapter over an in-process RepoEntityStore + a hand-built manifest — list_pins mirrors the manifest, retrieve resolves a pin's bytes + transitive closure through the store, and a caller scope only gains the requested entity (the store stays on the view's own scope). The nix half (build_namespace_manifest) rides the part-2 nix E2E.
  test_nix_e2e.py        Nix-marked E2E over the backend sweep: FOD fetch through build_span (env delivery + networking + auth) + entities-adapter builtin retrieve + the entityFromTsvPin remote-pointer path (lock populated by `update_hashes`, read back through mgr.entities, remote dep resolved through the repo store) + the `update_hashes` lifecycle (new slot → lock; re-run is a default no-op; repoint the TSV pin → default leaves the locked slot, `-k thePin` re-locks it to the new hash) + the **FOD-builder** path (a `lib.mkFxtrFnBuilder` FOD whose entrypoint builds `ctx.remote` from the delivered `FXTR_REMOTE_*` and fetches/pins via the rehost — the net-new seam being `nix_builder.amain`'s env→`http_deep_fetchable` inside a real nix FOD; locked via `update_hashes`, read back through `mgr.entities`). + the **provenanced-builder** path (a pure `lib.mkFxtrFnBuilder` with `provenanced = true`, read back through `mgr.entities` as a layered id — proving the flag threads nix scaffold → `builderSpec.provenanced` → writer → `mount_pin` embed). Uses the _workdir_templates fixtures via fxtr_workdir_harness. (FOD/preload tests skip on the macOS host backend — see below.)
  test_serve_e2e.py      Nix-marked E2E for serve.py over the sweep: builds the publishing_demo fixture (provenanced entity + tsv namespace + a uiPresets renderer) and serves it via published_serving_context. Asserts the provenanced entity round-trips **layered** + a namespace pin stays **bare** (read back via HttpFxtrReadNamespace), list_namespace_names/list_preset_names == ["demo"], build_preset_table builds the flake renderer into a loadable RendererEntity, and /discovery lists the `dispatcher` ws service. Pure build/export/esbuild (no rehost fetch), so it runs on the macOS host backend. (The dispatcher's ws resolve/subscribe round-trip is not exercised — no Python ws client; build_preset_table + /discovery cover it.)
  _workdir_templates/rehost_fod/   Fixture flake for test_nix_e2e: a fetch FOD (impureEnvVars FXTR_REMOTE_*) + a builtin fxtr.entities.demo.
  _workdir_templates/builder_fod/  Fixture for the FOD-builder E2E: a host fxtr2 workdir venv (pyproject comment-flip sources, no committed uv.lock) + src/builder_fod (the `build` entrypoint: fetch @ENTITY_ID@ via the rehost + pin it) + flake declaring `fxtr.entities.thePin`/`fxtr.preloads.thePin` via `lib.mkFxtrFnBuilder` (outputHash from fod_lock.toml) + a committed empty fod_lock.toml.
  _workdir_templates/provenanced_builder/  Fixture for the provenanced-builder E2E: a host fxtr2 workdir venv + src/provenanced_builder (the `build` entrypoint: construct an entity from spec + pin it) + flake declaring `fxtr.entities.thePin` via a pure `lib.mkFxtrFnBuilder { provenanced = true; }`.
  _workdir_templates/tsv_pin_entity/  Fixture for the entityFromTsvPin / fod_lock E2E: a tsv namespace (fxtr_namespace.toml + a templated TSV pin) + a committed empty fod_lock.toml; flake declares `fxtr.entities.thePin` + `fxtr.preloads.thePin` with `outputHash = (lib.fodLockReader { lockFile = ./fod_lock.toml; }) "thePin"`.
  _workdir_templates/publishing_demo/  Fixture for test_serve_e2e: a host fxtr2 workdir venv + src/publishing_demo (the `build` entrypoint: construct a provenanced entity + pin it) + flake declaring a provenanced `fxtr.entities.report` (pure `lib.mkFxtrFnBuilder { provenanced = true; }`), `fxtr.namespaces.demo` (`lib.tsvNamespaceExport`, a templated `@ENTITY_ID@` TSV pin), and `fxtr.uiPresets.demo` (`lib.mkUiPreset` over renderers/report — a minimal `index.tsx` keyed to a builtin type, so no typeEntity layer build).
```

## Module details

- **`_backend.py`** — `Backend.builder_for(repo)` is a refcounted async CM
  yielding a `Handle`; `repo` is both the keying basis and the ref target the
  handle is bound to. `Backend.keep_builder_warm(repo)` is a lazy, non-
  instantiating async CM (a `_lifecycle` pin on the podman backend, a no-op on
  host): held around a sequence of `builder_for(repo)` borrows, it keeps the
  builder container alive between them (started on the first borrow, torn down
  when the block exits) instead of paying start/stop per borrow. `Handle` methods: `flake_ref_root()` (the ref-root
  string for the builder's target), `expose_host_endpoint(host_port)` (async CM
  → `ExposedEndpoint` whose `url_for_builds` is the address a build/FOD dials to
  reach a host-bound server — `127.0.0.1` host, `host.containers.internal`
  podman; design/07 option 1, no tunnel), `run_nix(argv)` → `NixResult`
  (non-raising; **pure transport** — runs exactly `nix <argv>`, no
  interpretation/injection. Build-time env delivery is the manager tier's job —
  the `impure_env_nix_flags(env)` helper, here for reuse, is *prepended* as
  global flags to the argv by `manager.BuildSpan.run_nix`, not spliced in by the
  backend), `lock()` (`nix flake lock` → write `flake.lock` back to the host workdir),
  `materialize(store_path, *, closure)` → `Materialized` (host path +
  optional `(old_prefix, new_prefix)` remap for the v2 reader),
  `deliver_image(image_json, *, runtime: ContainerRuntime)` → `DeliveredImage`
  (the `image_ref` + `--platform` to run the loaded image with — the store-side
  export sibling of `materialize`; `runtime.type` gates podman-only delivery).
- **`_config.py`** — config resolution; env read **only here**. Spans the nix
  backend *and* the container runtime (`nix_runtime.toml`). `load_config` is
  total — unset choice fields stay `None`; `NixRuntimeConfig.require_nix_backend()`
  / `require_container_runtime()` raise a wizard-pointing `ConfigError` when
  needed-but-unset. Per-field resolution: `FXTR_NIX_RUNTIME_CONFIG` (full-file
  override, supersedes everything) vs the default-location toml + per-field env
  (`FXTR_NIX_BACKEND` → `nix_backend`, `FXTR_CONTAINER_RUNTIME_CMD` /
  `FXTR_CONTAINER_RUNTIME_TYPE` → the runtime). `ContainerRuntime` (`cmd` +
  concrete `type ∈ {podman, docker_compatible}`) is resolved via
  `resolve_container_runtime` — `"auto"` infers the family from `basename(cmd)`
  (`podman → podman`, else `docker_compatible`). `[nix_backend_podman]` keys
  (was `[podman]`): `image`, `sandbox`, `multi_user`, `filter_syscalls`,
  `materialize_root`, `mountable_paths`. The image default is pinned `>= 2.34.7`
  — the floor for the multi-user daemon (older daemons crash under Rosetta
  emulation; see
  [`stage9_nix_runner.md`](../fxtr_workstation/plans/phase0/part5_nix_workdir/stage9_nix_runner.md)).
- **`perf.py`** — opt-in, dependency-light (stdlib only) perf timing shared
  across the nix + spawn paths. `perf_span(name, **fields)` (async) /
  `perf_span_sync` time a region; `record(name, **fields)` emits a
  zero-duration marker (poll counts, cache hit/miss); `proc_label(argv)`
  gives a compact per-subprocess label. **Inert by default** — a span is a
  `perf_counter()` pair unless a consumer is listening. Two ways to listen:
  `collect_perf()` (a `with` region yielding the ordered `PerfEvent` list — the
  **sink is a module global**, not a contextvar, so it captures spans that run
  on a long-lived `env_tg` task group opened before the region) and
  `FXTR_PERF_LOG=1` / `enable_perf_stderr_logging()` (indented stderr line per
  span). `depth` (indentation) rides a contextvar, so intra-task nesting is
  correct and child tasks inherit their parent's depth. The backends
  (`_host` / `_podman` / `_delivery`) wrap every subprocess + the builder /
  delivery phases; `fxtr_sandboxing` wraps the container-run + sidecar phases.
- **`_lifecycle.py`** — `RefcountedResourceRegistry[K, R]`: first borrower of
  a key runs `factory`; later/concurrent borrowers share; last-out runs
  `teardown`. factory/teardown are awaited outside the bookkeeping lock, so
  one key's slow container start/stop never blocks other keys. Also `pin(key)`
  — a keepalive CM that **does not instantiate** but keeps a key's resource
  warm: the first `borrow` under a pin builds it, borrow cycles between then
  and the pin's release reuse it, and `teardown` runs only once **both** the
  borrow count and pin count hit 0 (a failed build under a pin is retried by
  the next borrow rather than dropped). Also `invalidate(key, resource)` —
  drop a resource believed dead (identity-checked) so the next `borrow`
  rebuilds it, keeping the entry + any pin (a pinned-warm resource re-warms) +
  best-effort teardown of the corpse. The podman backend uses `borrow` for
  "one builder per repo per process" (and the deliverer), `pin` for
  `keep_builder_warm`, and `invalidate` to recycle a warm builder whose
  container died. Podman-free + unit-tested on its own.
- **`_host.py`** — trivial passthrough: `run_nix` shells to host `nix`,
  `flake_ref_root` is the resolved target, `lock` = `nix flake lock <workdir>`
  in place, `materialize` is identity, `builder_for` is a no-op CM.
  `expose_host_endpoint` → identity `http://127.0.0.1:<port>` (host builds reach
  loopback). Per-build env delivery (`--option impure-env`) needs, on a
  multi-user host daemon, a trusted user + `configurable-impure-env` in the
  *daemon's* experimental-features (documented prerequisite; single-user nix
  gets `impureEnvVars` free).
  `deliver_image` builds skopeo on host nix and loads the (host-readable)
  manifest via `_delivery.host_load_into_runtime`.
- **`_podman.py`** — `builder_for(repo)` computes the git repo root, then
  refcount-borrows a builder container for it (lazy `run -d … sleep
  infinity`; last-out `rm -f`). The builder runs at the moderate rung
  (`SYS_ADMIN + label=disable + unmask=/proc/*`) with `sandbox=true` +
  `sandbox-fallback=false` + arch-resolved `filter-syscalls`, mounts the
  shared `fxtr-nix` (`/nix`) + `fxtr-nix-cache` (`XDG_CACHE_HOME`) volumes,
  the repo RO at `/workdir`, and the materialize scratch at `/materialize`;
  on start it writes `/etc/nix/nix.conf` (incl.
  `configurable-impure-env` in `experimental-features` so the manager's
  per-build `--option impure-env` reaches FODs — the builder daemon is root +
  ours, so both daemon-side prerequisites (a trusted invoking user and the
  daemon feature) are satisfied here) and (default
  multi-user) creates the `nixbld` users + launches `nix-daemon`.
  `expose_host_endpoint` → `http://host.containers.internal:<port>` (gvproxy
  gateway; the host server must bind `0.0.0.0`). `/workdir` is mounted **read-write**
  so `lock()` (`nix flake lock` at the flake's `flake_ref_root`, so a subpath
  flake locks the right dir) can write `flake.lock` back to the
  host workdir (the untrusted build runs sandboxed off a store copy, never
  seeing the mount); `mountable_paths` are bind-mounted RO at their abspaths
  for local-path flake inputs. `run_nix` = `podman exec [-e
  NIX_REMOTE=daemon]`; `flake_ref_root` maps the target to
  `/workdir[/relpath]`; `materialize` = `nix copy --to local?root=/materialize`
  + a `/nix/store` → host-store remap. The builder holds no API socket
  (builder/deliverer split). `deliver_image` builds skopeo *in the builder*,
  reads the manifest there (the `/nix` volume isn't host-readable), and lazily
  starts a **deliverer** container — a socket-holding sibling that **shares
  the builder's lifetime** (a field on the `_Builder`, torn down with it) and
  execs the pre-built skopeo against the VM podman socket. `runtime` must be
  podman (else raises). The image is the security boundary, so the deliverer
  is `--security-opt label=disable`, no repo, no build. Builder + deliverer run
  with `--stop-timeout=0` (both are `sleep infinity` PID 1, which ignores
  SIGTERM, so `rm -f` would otherwise wait the full 10s stop timeout).
  **Transport-failure recycle:** every builder/deliverer exec goes through
  `PodmanHandle._exec`, which on rc 125 (the container is gone — distinct from a
  nix nonzero exit) recycles the dead container and raises `BuilderTransportError`
  (exported): a *builder* death calls `_invalidate_builder` → `registry.invalidate`
  (rebuilt on the next borrow, re-warming any pin); a *deliverer* death calls
  `_recycle_deliverer` (clears the cached `deliverer_id`, leaving the builder).
  Callers catch `BuilderTransportError` to retry (the spawner does).
  **Host-death self-teardown:** the backend runs a `LivenessBeacon` (a
  `0.0.0.0` listening socket + accept/close daemon thread, started lazily on
  first builder, living for the process's lifetime) and gives builder +
  deliverer a health check (`--health-cmd` dialing
  `host.containers.internal:<beacon port>` via bash's `/dev/tcp`,
  `--health-on-failure=stop`, `--rm`). When the owning process dies the beacon
  socket closes → the dial fails → podman stops + removes the container ~one
  health window later, so a crashed spawner doesn't leak a running builder.
  (`--stop-timeout 0` makes the stop a prompt SIGKILL.)
- **`_delivery.py`** — anyio-only store-side delivery shared by both backends:
  the skopeo pin (`SKOPEO_FLAKE_REF`), `image_ref_for` (ref from the manifest
  store-path basename; `runtime.type == "podman"` adds the `localhost/`
  prefix), `platform_from_manifest`, `resolve_podman_socket(cmd)` (host-side
  API socket), and `host_load_into_runtime` (host skopeo → tarless podman
  socket for a podman-type runtime, else oci-archive + `image load`). Takes a
  `ContainerRuntime` (no more `is_podman` basename check — behavior keys on
  `.type`, argv on `.cmd`).
- **`testing.py`** — public test-support (lazy-imports pytest):
  `skip_if_runtime_unconfigured()` skips a test when no `nix_backend` is
  configured (the `FXTR_TESTS_REQUIRE_RUNTIME` env flips skip→fail for CI).
  Downstream suites mark genuinely-nix E2E tests `nix_runtime` + call the skip
  helper from an autouse conftest fixture.
- **`_clear.py` / `_wizard.py` / `__main__.py`** — the `fxtr_nix` CLI (a
  console script; `python -m fxtr_nix_runner` also works). `fxtr_nix lock
  [path]` resolves the backend and `lock()`s a flake repo (default cwd) — lets
  a flake repo lock without host nix, e.g. from a pre-commit hook. `fxtr_nix
  clear` runs `_clear.clear()`: label-sweep builder/deliverer containers,
  remove the shared volumes, wipe the materialize scratch (crash-debris
  recovery; normal teardown is the context manager).
- **`_wizard.py`** — the `fxtr_nix runtime` setup wizard. `detect()` probes the
  machine (host nix + can-it-build-x86_64-linux via a trivial test build;
  podman CLI/machine/running/Rosetta; Apple Container) and computes a
  `Recommendation` + `nudges`. `fxtr_nix runtime setup` prints the tradeoff +
  detection + nudges + recommendation and writes `nix_runtime.toml` (`--detect`
  prints the stable JSON and stops, no writes). `fxtr_nix runtime init-podman`
  is the **only** subcommand that changes the system — a thin wrapper over
  `podman machine init` + `start` (+ the Rosetta marker unless `--no-rosetta`);
  `setup` stays detect-and-write only and points at it. Probes are pure-mocked
  in tests (`test_wizard.py`); `init-podman`'s argv is the testable
  `_init_podman_steps`.
- **`rehost.py`** (manager tier — imports fxtr2) — `rehost_context(store, *,
  host="127.0.0.1")`: an async CM that serves a `RepoEntityStore`'s by-id
  entities over an **ephemeral-port** HTTP server with **bearer-token** auth
  (`_BearerAuthMiddleware`, constant-time compare; fresh `secrets.token_hex` per
  rehost). Reuses fxtr2's `create_fxtr_app` over a fresh isolated `FxtrScope`
  with the store registered as the scope's deep-fetch source (an
  `IndependentLocalBridge` over `store.get_lease()` — case 1, the store outlives
  the rehost), so `/data/<id>` resolves any id the store holds. Yields
  `RehostInfo(base_url, token, port)`; `RehostInfo.build_env(base_url=None)`
  produces the `{FXTR_REMOTE_BASE_URL, FXTR_REMOTE_BEARER_TOKEN}` build env —
  the manager passes the backend's build-visible URL (from
  `Handle.expose_host_endpoint(port)`) for non-loopback backends (podman). Binds
  `127.0.0.1` (host) or `0.0.0.0` (podman); the caller picks `host`.
  **Host-backend limitation:** the `127.0.0.1` URL assumes the build runs
  *locally*. When host nix offloads (e.g. macOS building linux derivations via a
  remote builder VM), that builder can't reach the mac host's loopback rehost —
  so FOD-fetch over the host backend only works where builds are local (e.g.
  linux). Podman routes via `host.containers.internal` and works on macOS. (The
  `127.0.0.1` URL works for local/native builds; the offload case is the gap —
  `test_nix_e2e` skips the FOD test on darwin host.)
- **`manager.py`** (manager tier — imports fxtr2) — `FlakeManager`, the single
  fxtr2-aware object consumers talk to. Construction
  (`FlakeManager(repo, *, rev=None, backend=None, config=None,
  isolate_scope=False)`) is inert; it's an `anyio.AsyncContextManagerMixin`, so
  `async with FlakeManager(...) as mgr:` (the `fxtr_context_api` namespace
  pattern) resolves the backend (or takes one), opens the repo store
  `create=True`, and opens a long-lived **manager `FxtrScope`** alongside it
  (`isolate_scope` follows the namespace pattern — default non-isolated, so it
  inherits + becomes the active scope for the block) — both kept for the
  manager's lifetime, scope entered *after* the store so it closes *before* it
  (releasing the store leases held by mounted entities). `mgr.scope` is the home
  for entities read through the
  adapters: lazily loadable for the manager's lifetime (local bytes from
  materialized layers on disk, remote-pointer closures from the store).
  `mgr.repo_store` → a `(DeepFetchable, Lease)` pair (fresh lease clone per
  access) for consumers to mount on their own scope. `mgr.mount_pin(layer_dir,
  pin, *, remap)` → resolves the pin against a built layer via the fxtr2 `mgr.scope` is the home for entities read through the
  adapters: lazily loadable for the manager's lifetime (local bytes from
  materialized layers on disk, remote-pointer closures from the store).
  `mgr.repo_store` → a `(DeepFetchable, Lease)` pair (fresh lease clone per
  access) for consumers to mount on their own scope. `mgr.mount_pin(layer_dir,
  pin, *, remap)` → `resolve_pin`s the pin against a built layer, opens an
  `open_seed_arena` (branded on `store.grant`, source = the store) on the bare
  node it needs loadable, and attaches it into the manager scope with
  `IndependentLocalBridge(store.get_lease())`, closing the transient arena — the
  mounted node survives because the scope holds the bridge. A **provenanced** pin
  resolves to a layered `(data, prov)` id (its `pins.json` value is a `[data,
  prov]` pair); `mount_pin` seeds/attaches on the bare prov node and reconstructs
  the layered id via `embed_as_provenance_parent` (`EntitiesReadAdapter` passes it
  through unchanged); a bare pin attaches on itself and is returned unchanged.
  `mgr.build_span()` async CM borrows a builder, stands up
  `rehost_context` (bound per backend via `_rehost_bind_host` — `0.0.0.0` podman /
  `127.0.0.1` host), resolves the build-visible URL through
  `Handle.expose_host_endpoint`, and yields a `BuildSpan` whose `run_nix`
  **prepends** the `FXTR_REMOTE_*` impure-env flags (global, via
  `impure_env_nix_flags`) to every nix invocation (unconditional) — env delivery
  lives here, the transport stays pure. `mgr.entities(span)` → the entities read
  adapter below; `mgr.namespace(span, name)` async CM → the namespace read view
  (`namespaces.py`): builds the export manifest once on the span, yields a
  `NamespaceReadAdapter` over it backed by the repo store.
- **`entities.py`** (manager tier — imports fxtr2) — `EntitiesReadAdapter`, a
  `FxtrReadNamespace`-*shaped* (not subclass) read view over a flake's
  `fxtr.entities.*`, bound to a `BuildSpan` + its `FlakeManager`. `retrieve` /
  `try_retrieve` mirror the MCP's `retrieve_nix_entity` (eval one entity's
  metadata via `_ENTITY_METADATA_APPLY` → builtin short-circuit, else build
  `<attr>.layer` + `materialize`), then hand the built layer to
  `mgr.mount_pin(...)`, which mounts the pin into the **manager scope** (so the
  entity outlives the build span and resolves remote-pointer closures through the
  store). `into_scope` is optional sugar: `copy_into_scope=True` does an eager
  `copy_from_scope` into it, `=False` lifts (requires it to inherit the manager
  scope). `list_entity_names` is a cheap eval (no id-valued `list_pins` —
  resolving a name to a `FxtrID` needs a build). Reads at the manager's `rev`
  (committed `git+file://…?rev=`) or live workdir (bare path). The runner can't
  import the MCP (cycle), so the eval/build logic is reimplemented.
- **`namespaces.py`** (manager tier — imports fxtr2) — the namespace read view
  over `fxtr.namespaces.<name>`. `build_namespace_manifest(span, rev, name)` is
  the nix half: eval the attr's `.type` (validate it is `fxtr_namespace_export`),
  build + (non-closure) `materialize` `.export`, read `fxtr_namespace_export.json`,
  return its `key → FxtrID` map. `NamespaceReadAdapter(manifest, store, lease)` is
  the pure read half — a **real `FxtrReadNamespace`** (unlike the entities
  adapter; the manifest gives an id-valued `list_pins` for free). On enter it
  registers the repo store as a deep-fetch source on its **own** isolated scope;
  `retrieve` looks the pin's id up via `store.try_lookup`, attaches it into that
  scope (`lazily_attach_from_arena`, store-leased bridge — the
  `HttpFxtrReadNamespace` dance with the store as the source), then resolves into
  `into_scope` (no layer; the export is metadata, the store supplies bytes). The
  manifest-format constants mirror the `namespace_export` writer in
  `nix/fxtr_nix_writer` (kept in sync by hand — separate packages).
- **`renderers.py`** (manager tier — imports fxtr2) — builds a flake's renderer
  **preset** (the `fxtr.uiPresets.<name>` convention, produced by
  `nix/lib/mkUiPreset.nix`) into a servable table. Two layers. The **low-level**
  half (`eval_preset_table` / `build_drv_outputs` + the private `_PRESET_APPLY`
  nix reshape + `_build_flake_ref`) takes a transport `Handle`, is fxtr2-free, and
  is **shared** with `fxtr_workstation`'s `nix_configurable_dispatch` (which imports
  it from here): `eval_preset_table` evals the preset to `{type_id: {renderer +
  typeEntity drv paths, metadata}}` (no realization), `build_drv_outputs` realizes
  the drvs in one batched `nix build`. `_build_flake_ref`/`eval_preset_table` take
  `rev: str | None` — a committed rev (frozen git ref) or `None` for the live
  workdir (bare path ref), so the runner can serve either. The **adapter** half
  (fxtr2-aware) adds `eval_preset_names` (cheap names eval; backs
  `mgr.list_preset_names`) and `build_preset_table(span, scope, rev, preset_name)`
  — eval → batched build → validate each typeEntity's id against its preset key
  (`RendererPresetError` on mismatch, via `LayeredFileStoreReader`) → mint a
  `RendererEntity` per renderer into `scope` (must include `GLOBAL_SCOPE`; the
  blobs serve via `/data`) → return a `RendererTable` (`type_id → key_uuid →
  RendererEntry{subtype_key, renderer ref}`). Mirrors `fxtr_workstation`'s
  `NixConfigurableDispatchImpl.set_preset` build→validate→mint, bound to a manager
  build span.
- **`serve.py`** (manager tier — imports fxtr2 + the http substrate) —
  `published_serving_context(repo, *, port, rev=None, host=…, preset=None, …)`, the
  flake-agnostic glue from the manager read adapters to `fxtr2.experimental.http`.
  Opens a serving `FxtrScope(include=[mgr.scope, GLOBAL_SCOPE])` (the repo store
  registered as a deep-fetch source) **around** the build span, so during the span
  it retrieves every `fxtr.entities.*` (layered ids, mounted into `mgr.scope`),
  builds every `fxtr.namespaces.*` manifest, and `build_preset_table`s the chosen
  renderer preset (minting `RendererEntity` blobs into the serving scope). After
  the span it assembles `create_fxtr_app(provenanced=True)` +
  `ServiceRegistry(provenanced=True)` (entities registered for `/discovery`; pins =
  entities layered + `"<ns>:<key>"` bare) plus — when a preset built — a
  flake-driven `dispatcher` ws service (`_register_dispatcher`: `resolve` is a
  `RendererTable` lookup → `{subtype_key, renderer}`, `subscribe` emits a
  `ReloadEverythingNotification` + a transient unsubscribe). Runs hypercorn on a
  task group, yields a `PublishedServer` once listening, graceful shutdown on exit
  (mirrors `fxtr2.experimental.http.server.serving_context`). `_pick_preset` selects
  the preset (explicit `--preset`, else sole / `default`, else none → no
  dispatcher). An optional `web_dist` directory is mounted as a `StaticFiles`
  SPA at `/` **after** the API routes (so it only catches unmatched paths) — a
  bare `fxtr_nix serve --web-dist DIR` brings up a same-origin UI; flake-agnostic
  (serves any built SPA). `run_published_server` enters the CM and parks on
  SIGINT/SIGTERM — the `fxtr_nix serve` CLI (`--web-dist`).
- **`_mask_nix.py`** — `run-with-masked-nix <cmd…>` (console script): runs a
  command with a temp dir of failing `nix` / `nix-*` shims prepended to
  `PATH`, each logging its argv to a tripwire file. A host-nix call fails
  loudly *and* is recorded, so the run fails (naming the leak) even if the
  child swallows the exit code; the shim dir is cleaned up in a `finally`. The
  ad-hoc proof that a podman-backed run is genuinely host-nix-free — used to
  verify the stage-9 E2E sweep.

## Tests

`conftest.py`'s `nix_backend` fixture is parameterized over `host` + `podman`;
each variant skips when unavailable (no host nix; no podman machine), so a box
with both runs the full matrix and neither backend rots silently. The podman
container tests need a podman machine and share global podman state (the fixed
`fxtr-nix` volumes + builder labels), so **run podman tests serially** (`-n0`);
the materialize roundtrip is marked `slow` (it builds in-container).

Run: `uv run pytest` (add `-n0` when including the podman container tests).
Type-check: `uv run pyright`.
