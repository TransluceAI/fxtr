# 2p5 — pyodide-in-deno sandbox backend

A third sandbox backend: pyodide (CPython-in-wasm, emscripten ABI) running
inside a permission-locked deno subprocess. Middle ground between the eryx
WASM backend (in-process, pure-python wheels only) and the podman container
backend (full linux images): real binary wheels via PEP 783
(`pyemscripten_*_wasm32` on PyPI), ~1.6s spawn-to-service with no VM and no
image build, V8-isolate-grade isolation.

**Prior work**: everything risky was spiked in `spikes/deno_py_sandbox/`
(see its README for measured perf, gotchas, and the validated mechanics).
Committed as "fxtr deno spike 1 / 1b / 2". This plan is the productionized
version; the remaining work is pattern-following against the eryx/container
backends.

## Decisions (from spike + discussion)

- Backend name: `kind = "pyodide"`; entity `NixPyodideServiceDefn`
  (wire-shape identical to `NixWasmServiceDefn`).
- Main channel over a per-spawn UDS (stdout stays for user prints);
  transport side-channel over a second multiplexed UDS with the container
  runtime's routing-header protocol. Both reuse `SocketJSONStream` /
  `transport_routing.py` unchanged.
- Pyodide assets delivered via nix; **deno comes from the host PATH** (a
  containerized-nix-built deno wouldn't run on the host). Deno is an
  optional host dependency — only this kind needs it.
- Pyodide version / ABI pin is **flake-controllable**: fxtr's nix lib ships
  a default pin tuple (pyodide version, npm tarball hash, ABI tag —
  they move together), `mkPyodideService` accepts overrides. There is no
  host-side pyodide blob the pin "replaces" — the pin parameterizes the
  bundle's `fetchurl`, so the blob is always a flake output (defaulted by
  whichever fxtr.nix rev the flake pins).
- **Ownership/trust model**: split at the `loadPyodide` line.
  **Host-owned, trusted**: the deno binary, the permission flags, and the
  **driver** (`main.ts` + `deno.json`, shipped as data files of the host
  package — eryx-`_runner.py` style, versions with the host fxtr install).
  **Flake-owned, untrusted**: the pyodide blob, the guest site-packages,
  and the bootstrap — everything that executes after the driver hands
  control to flake-supplied code. Rationale for the trusted driver: (a) it
  enables **grant-then-drop** — dial the main UDS at launch, then
  `Deno.permissions.revoke()` net (entirely when no transport socket is
  configured; down to the transport socket otherwise) *before* importing
  pyodide's JS glue, so all flake-supplied code runs under narrowed
  permissions; (b) host runner ↔ driver plumbing (argv, socket setup,
  permission profile) can evolve freely with no cross-version contract.
  The stable host↔guest seams are then small: the `fxtr_deno_bridge` JS
  API + injected globals + bundle layout (driver ↔ flake-side bootstrap),
  and the RPC wire protocol (host ↔ guest). The interpreter + all guest
  code stay flake-pinned (reproducibility keeps what matters — the driver
  is transport infrastructure, same category as the deno binary itself);
  a user-overridden pyodide has exactly the powers malicious guest python
  already has, since the flags + pre-narrowed permissions bound both.
- Both wheel sources supported as a flake/workspace-level uv concern: PyPI
  `pyemscripten_*` wheels and the pyodide-distribution index
  (`--extra-index-url https://index.pyodide.org/<ver>/` + the
  `unsafe-best-match` strategy). No fxtr code changes — locked wheels are
  locked wheels.
- Guest bootstrap ships **in the runtime bundle** (not injected by the
  host, not a dependency of service workspaces): it is guest code by
  definition (python inside pyodide) and its tight couplings are to
  flake-side code (`fxtr_sandbox_types` / the adapter in the guest
  site-packages, locked at the flake's fxtr rev), so it versions with the
  flake — unlike the driver, which is host-side. It still lives in a real
  package in the fxtr repo for testability; the bundle build copies it in.
- Resource limits: best-effort `--v8-flags` knob only (eryx parity; no
  watchdog). Verify during implementation how wasm linear memory interacts
  with `--max-old-space-size`; document what the knob actually bounds.
- Host-death cleanup: free via UDS EOF (guest exits when the host's socket
  fd closes) + a small driver-side backstop (main.ts hard-exits a grace
  period after the main conn drops if python hasn't returned).
- Same trust tier as eryx, documented: the deno permission layer is the
  perimeter (guest python can call arbitrary JS); V8-isolate-grade, below
  container-class.

## Components

### N1. `nix/lib/_mkLockedSitePackages.nix` — lock-driven guest dir builder

The generalization of `spikes/deno_py_sandbox/lockfetch_spike/flake.nix`.
Builds a flat site-packages dir straight from `uv.lock`, **bypassing
`mkVirtualEnv`** (pyproject-nix's `isPlatformTagCompatible` has no
`pyemscripten_*` arm, so the mkVenv path can never select a PEP 783 wheel —
it silently falls back to a linux sdist build; confirmed at pinned rev and
master, no upstream issue).

- Reuse uv2nix's lock parsing + marker resolution: `lock1.parseLock` +
  `lock1.resolveDependencies` with a `pep508.setEnviron`-retargeted environ
  (`sys_platform = "emscripten"`, `platform_machine = "wasm32"`, python
  version matching the pinned pyodide's CPython).
- Per resolved **registry** package, our own wheel pick: a wheel whose
  platformTags contain the configured binary tag (e.g.
  `pyemscripten_2026_0_wasm32`) → else `py3-none-any` (match on
  abiTags/platformTags — languageTags are parsed structs) → else fail
  loudly naming the package. `fetchurl` by the lock's url+sha256 (pure, no
  FOD relock), unzip flat.
- Per **local / git** package (`lock1.isLocalPackage` + git sources): build
  through the existing pyproject-nix `pythonSet` (same machinery mkVenv
  uses — hatchling semantics for free) and **copy each drv's installed
  `lib/python*/site-packages` subtree** (at the current pyproject-nix rev
  the drv `out` is an installed tree; there is no wheel `dist` output).
  Validated in the spike, with two findings baked into the design: (a) the
  pythonSet's overlay must carry the **guest environ** (guest workspaces
  constrain `[tool.uv] environments` and uv2nix asserts the lock's
  supported-markers are satisfiable by the overlay environ), which would
  force each plucked local's propagated dep closure through emscripten
  wheel selection (a binary dep would sdist-build for linux) — so plucked
  local drvs get `propagatedBuildInputs = [ ]`; building a pure-python
  local's installed tree doesn't need its runtime deps present (verified
  with a local member depending directly on pydantic-core under a fully
  constrained lock); (b) uv2nix stages only the member dir when
  building a *local* package, so out-of-dir hatch force-include
  (fxtr2-portable's `../fxtr2`) is unsupported for local members — it
  works for *git*-sourced deps (full repo fetched, built from subdir),
  which is how fxtr2-portable actually reaches guest workspaces (the
  existing eryx mkVenv path already exercises exactly this). Document
  "local members must be self-contained" on the helper.
- Parameterized for eryx reuse later: `binaryPlatformTags ? []` — the eryx
  path is the same builder with an empty binary-tag list (subsumes the
  "leaner uv2nix wiring" followup in `_mkGuestSitePackages.nix`). Swapping
  eryx over is explicitly out of scope here but must stay easy.
- Strip `__pycache__` the way `_mkGuestSitePackages` does (locally-built
  wheels may carry compiled .pyc closure references).

### N2. `nix/lib/_mkPyodideRuntimeBundle.nix` — runtime assets

One derivation = unpacked pyodide npm tarball (`fetchurl` from
registry.npmjs.org, hash part of the pin tuple) + `guest_bootstrap.py`,
copied from the flake's pinned fxtr rev. (No driver files — the driver is
host-side.) The host driver imports `pyodide.mjs` from the bundle by file
path and calls `loadPyodide({indexURL: <bundle>})` — **no npm resolution at
spawn time**: no network, no deno npm cache, and the deno read grant
shrinks to bundle + site-packages + sockets.

### N3. `nix/lib/mkPyodideService.nix`

`{ src, name?, system?, pyodidePin? }` → `{ kind = "pyodide"; sitePackages;
runtimeBundle; entryPoint; }`. `sitePackages` via N1 with the pin's binary
tag; `runtimeBundle` via N2; `entryPoint` via `_readServiceEntryPoint.nix`.
Register in `nix/lib/default.nix`. Workspace guidance (helper docstring):
use `[tool.uv] required-environments = ["sys_platform == 'emscripten'"]` so
the lock is guaranteed to carry emscripten-compatible wheels (verify
whether an sdist satisfies uv's check — if it does, the guarantee is weak
for binary-only packages and the builder's loud per-package failure is the
real backstop); further constraining `environments` is the flake author's
choice.

### P1. `projects/fxtr_sandboxing/fxtr_pyodide_runner/` — driver + guest package

Sibling package (analog of `fxtr_service_runner`) holding both the
host-trusted driver and the flake-copied guest bootstrap:

- `main.ts` + `deno.json` — the deno driver, **host-owned**: shipped as
  data files of this package and passed to `deno run` by path (never in
  the bundle; versions with the host fxtr install). Spike shape plus:
  file-path pyodide import from the bundle, `--v8-flags` passthrough,
  EOF-watchdog backstop, and **grant-then-drop**: dial the main UDS,
  then `Deno.permissions.revoke()` net down to the transport socket (or
  entirely, when none is configured) *before* importing pyodide's JS
  glue. The `fxtr_deno_bridge` JS module exposes byte-level handles
  (`main` + `connect_transport()`) — this API + the injected globals +
  the bundle layout are the stable driver↔bootstrap seam, so keep them
  minimal and versionable. EOF is signaled as an empty Uint8Array
  (pyodide 314 maps JS null to `JsNull`, not `None`).
- `fxtr_pyodide_runner/guest_bootstrap.py` — `DenoSocketByteStream`
  (anyio ByteStream over a bridge handle), `PyodideServiceContext`
  (`main_protocol_stream` = `SocketJSONStream`; `open_transport_stream` =
  bridge dial + routing-header handshake), top-level-await into
  `fxtr_sandbox_rpc_adapter.main` (pyodide has no `asyncio.run` without
  JSPI — run on the live WebLoop via `runPythonAsync`).

### P2. Host side in `fxtr_sandboxing`

- `fxtr_sandbox_types/entities.py`: `NixPyodideServiceDefn(git_commit,
  flake_attr, flake_path?)` + the wire-identical dup in the sandbox bridge
  (agents construct defns in-script).
- `pyodide_runner.py` (analog of `nix_container_runner.py`): per-spawn
  tmpdir (short path — macOS `SUN_LEN`), task + transport UDS listeners,
  `deno run` with the locked-down permission profile
  (`--no-prompt --allow-read=<bundle>,<site-packages>,<socks>
  --allow-write=<socks> --allow-net=unix:<socks>`), `accept_or_exit`,
  teardown ladder — all reusing `_container_common.py`.
  `spikes/deno_py_sandbox/host_common.py` is the working model.
- `spawner.py`: `_spawn_runner_nix_pyodide` arm — kind check `"pyodide"`,
  `nix.eval_entry_point`, `nix.build_site_packages` (reused; materialize so
  deno can read it) + build/materialize `runtimeBundle` the same way, then
  `pyodide_runner` + `_serve_spawn_rpc`. Rides the existing warm-builder /
  `_build_with_recycle` / per-env build cache machinery like the other two
  nix arms. Deno discovered via PATH (`shutil.which`), error pointing at
  install docs when the kind is used without it.
- Perf spans on the new phases (`pyodide.boot`, spawn arm build/deliver) —
  the existing `fxtr_nix_runner.perf` conventions.

## Implementation steps

Phase A — nix:

- [x] A1. `_mkLockedSitePackages.nix`: registry-wheel arm (port from
  lockfetch spike; binary-tag param, abi/platform tag matching, loud
  failures) + `__pycache__` strip.
- [x] A2. local/git package arm via the pyproject-nix pythonSet
  (guest-environ overlay + propagation strip, installed-tree copy —
  mechanics proven in the spike). The git-sourced `fxtr2-portable`
  force-include-from-full-repo case rides the C2 E2E (same uv2nix build
  path the eryx suite already exercises).
- [x] A3. `_mkPyodideRuntimeBundle.nix` + the default pin tuple in
  `nix/lib/default.nix`.
- [x] A4. `mkPyodideService.nix` + registration; `required-environments`
  guidance (+ verify its sdist semantics); fixture workdir template
  (pyodide greeter with one binary dep, mirroring
  `tests/_workdir_templates/greeter/`).
- [ ] A5. Update nix lib docs / CODE_OVERVIEW for the new helpers.

Phase B — runtime + host:

- [x] B1. `fxtr_pyodide_runner` package: host-owned driver (`main.ts`,
  `deno.json` as package data) + `guest_bootstrap.py`, promoted from the
  spike; file-path pyodide import; v8-flags knob; EOF-watchdog backstop;
  grant-then-drop net narrowing before pyodide import.
- [x] B2. `NixPyodideServiceDefn` entity + sandbox-bridge dup.
- [x] B3. `pyodide_runner.py` host runner + `_spawn_runner_nix_pyodide`
  spawner arm (incl. env build cache + perf spans).
- [x] B4. Transport wiring through the spawn arm (env's
  `TransportConnectionServer` → per-spawn transport UDS, like the container
  arm).
- [ ] B5. Update `projects/fxtr_sandboxing/CODE_OVERVIEW.md` (new mode, new
  package, lifetime nesting diagram, dev setup: deno optional dependency).

Phase C — verification:

- [x] C1. `[http]` extra under pyodide: add httpx/httpcore to a fixture,
  `http_get` through `add_transport`; if pyodide's stubbed `ssl` bites at
  import time, add a minimal shim or pin. (TLS terminates host-side, so
  import-compat is the only requirement.)
- [x] C2. E2E tests, skipping when `deno` is not on PATH:
  `test_nix_pyodide_spawner.py` (greet + binary-wheel import + kind
  mismatch), `test_nix_pyodide_transport.py` (raw echo + http). Deferred
  to follow-ups (see notes): a dedicated host-death self-teardown test
  (the driver's EOF backstop was validated manually in B1 and covers it)
  and a nested-spawn-with-pyodide-inner test (bridging is
  backend-agnostic; the wasm/container nested tests lock in the
  machinery).
- [x] C3. Docs: README.md "Variant 3" under fxtr_sandboxing; trust-tier
  note (deno permission layer is the perimeter; guest python can call
  arbitrary JS).
- [x] C4. Final `CODE_OVERVIEW.md` pass (fxtr_sandboxing + nix lib +
  anything touched).

## Future work (noted, not in scope)

- ~~Swap the eryx wasm path onto `_mkLockedSitePackages` (empty binary-tag
  list) and retire `_mkGuestSitePackages`'s venv-and-extract detour.~~
  **Done** (immediately after Phase C landed): `mkWasmService` now builds
  `sitePackages` via `_mkLockedSitePackages` with the wasi environ +
  `binaryPlatformTags = [ ]`; `_mkGuestSitePackages.nix` deleted. The wasm
  E2E suite (`test_nix_spawner` + `test_nix_wasm_transport`, 4 tests)
  passes over the new builder. One behavior caveat documented on the
  helper: raw-unzipped registry wheels skip installer normalization
  (`*.data/` dirs left as-is) — none of the guest-facing deps use them.
- Warm spawns: pre-booted deno process pool, or pyodide's private
  `_makeSnapshot` memory-snapshot API (Cloudflare-style; churn-prone).
- stdout/stderr capture into structured host logging (v1 inherits).
- Track uv gaining a `wasm32-pyodide2026` / pyemscripten
  `--python-platform` value (irrelevant to the nix path, useful for local
  dev ergonomics), and pyodide/deno upgrade compat (pyodide does not
  officially support deno — the E2E sweep is the regression net).

## Known gotchas (carried from the spike)

- A `deno.json` must sit next to the driver — without one, deno's config
  discovery walks up, finds `pnpm-workspace.yaml`, and rewrites the repo
  root `package.json`.
- Deno NODEFS **writes** are broken (deno#30972); mounts are read-only,
  guest writes go to MEMFS.
- Unix sockets need `--allow-net=unix:<path>` *plus* read/write grants on
  the path; keep socket paths short (macOS `SUN_LEN`).
- One `loadPyodide` per process (re-load leaks); process-per-spawn.

## Implementation notes (surprises / things that didn't work as expected)

Logged during implementation, 2026-07-03:

- **uv2nix's overlay assert vs environment-constrained locks.** Guest
  workspaces constrain `[tool.uv] environments` to emscripten; uv2nix's
  `mkPyprojectOverlay` asserts the lock's supported-markers are satisfiable
  by the overlay environ, so the local-package pythonSet cannot use a
  neutral environ (the original design) — it must carry the guest environ,
  and plucked local drvs get `propagatedBuildInputs = [ ]` so their dep
  closures are never forced through emscripten-unaware wheel selection.
  (Spike 2c; verified with a local member depending directly on
  pydantic-core.)
- **pyproject-nix drvs have no wheel `dist` output** at the pinned rev —
  `out` is an installed tree (`lib/python3.X/site-packages/...`). The local
  arm copies that subtree instead of extracting a wheel. Fine, arguably
  simpler.
- **uv2nix stages only the member dir for local packages**, so out-of-dir
  hatch force-include (fxtr2-portable's `../fxtr2`) fails for *local*
  members. Git-sourced deps build from the full fetched repo, which is how
  fxtr2-portable actually arrives — but "local members must be
  self-contained" is a real constraint, documented on the helper.
- **Abrupt pre-handshake host close wedges the guest.** When the host
  closes the task socket before/without an RPC handshake, the guest adapter
  does not unwind on EndOfStream — `runPythonAsync` never returns. The
  driver's EOF backstop (hard exit 10s after main-conn EOF) is therefore
  load-bearing, not belt-and-suspenders. Worth a separate look at the
  adapter's pre-handshake EOF path someday; the backstop makes it
  non-urgent. (Normal RPC drain teardown unwinds cleanly — E2E-verified.)
- **Deno unix sockets need three grants**: `--allow-net=unix:<path>` (deno
  2.9 moved UDS under net) *plus* `--allow-read`/`--allow-write` on the
  path. Older docs/issues describe read-only gating; not current behavior.
- **Deno config discovery rewrites foreign config**: without a `deno.json`
  next to the entry module, deno walked up, found the repo's
  `pnpm-workspace.yaml`, and *modified the root `package.json`* trying to
  migrate it (then exited asking to be re-run). The driver always ships a
  `deno.json`; reverted the stray edit.
- **`Deno.permissions.revoke({name: "net"})`** revokes the whole net
  capability cleanly with the already-dialed conn staying usable —
  grant-then-drop works. Per-path selective revoke (keep transport, drop
  task socket) was not attempted; when a transport socket is configured the
  static grants stay as-is.
- **Repo-pinned uv 0.11.3 records `pyemscripten_*` wheels** in generated
  locks (no uv bump needed for fixtures), even though its
  `--python-platform` enum predates the tag.
- **`required-environments` sdist semantics unverified**: whether an
  sdist-only package satisfies uv's required-environments check is still
  unconfirmed; the builder's loud per-package failure is the real backstop
  either way.
- **Environment wart**: host nix had a stale GitHub PAT in
  `~/.config/nix/nix.conf` `access-tokens`, 401-ing all github flake
  fetches; scratch validation used `NIX_CONFIG="access-tokens ="`. The
  production path (podman backend) is unaffected.
- Spawn perf, measured by the E2E on this machine: first spawn of a defn
  ~40s cold (workdir prep + uv lock + nix eval/build/materialize through
  the podman builder), of which the deno/pyodide boot itself is ~1.5s
  (`pyodide.run_and_accept` perf span). Repeat spawns hit the per-env cache
  like the other nix arms.
- **anyio's raw `UNIXSocketStream` leaks raw `OSError`s** where its TCP
  `SocketStream` translates/swallows them: `send_eof()` is an unguarded
  `shutdown(SHUT_WR)`, which raises `ENOTCONN` (errno 57) on macOS when the
  peer already fully closed. Surfaced as a `_writer_loop` crash in fxtr2's
  RPC teardown (`lowering._drain_and_eof`) when a guest died mid-connection.
  Fixed in v2 by adding `OSError` to `_drain_and_eof`'s already-documented
  swallow-transport-errors clauses (the docstring always promised this;
  the except list just didn't anticipate untranslated raw-socket errors).
  Worth remembering for any other host-side UDS `send_eof` call sites.
- **A guest that crashes on entrypoint import** (the transport fixture
  briefly had a NameError from a botched rename) surfaced as a
  hang-until-deadline rather than a fast spawn failure: the deno driver
  dials the task socket *before* pyodide boots, so `accept_or_exit` has
  already succeeded, and the subsequent guest death during the RPC
  handshake left the host wedged (with the ENOTCONN crash above as
  wreckage). With the `_drain_and_eof` fix the death should propagate as a
  clean pre-`started` handshake failure, but this exact path (guest dies
  post-accept, pre-handshake) has no dedicated test — candidate for a
  follow-up alongside the existing `test_spawn_build_error_propagation`.
- Per Daniel: prefer `anyio.fail_after` around E2E interaction phases over
  relying on the conftest SIGALRM backstop — cancellation unwinds the
  async CMs (real teardown, readable traceback) where SIGALRM detonates
  mid-anything. Both pyodide E2E tests now do this (the slow nix build in
  `spawn()` stays under SIGALRM; the live interaction gets `fail_after(120)`).
- Deferred follow-ups from C2: (a) a dedicated host-death self-teardown
  E2E (SIGKILL the host mid-session, assert the deno process reaps itself
  via the EOF backstop — manually validated in B1, no automated test);
  (b) a nested-spawn test with a pyodide inner; (c) the
  guest-dies-post-accept-pre-handshake fast-failure test noted above.
