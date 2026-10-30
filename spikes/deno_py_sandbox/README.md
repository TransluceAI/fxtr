# deno_py_sandbox spike

> **Status: productionized.** The real implementation lives in
> `nix/lib/{_mkLockedSitePackages,_mkPyodideRuntimeBundle,mkPyodideService}.nix`,
> `projects/fxtr_sandboxing/fxtr_pyodide_runner/` (driver + bootstrap), and
> `fxtr_sandboxing`'s `pyodide_runner.py` + `_spawn_runner_nix_pyodide`;
> plan + implementation notes in
> `projects/fxtr_sandboxing/plans/stage2/2p5_deno_py_sandbox/plan.md`.
> This spike is kept as reference for the raw mechanics + measurements.

Feasibility spike for a third sandbox backend: **pyodide (CPython-in-wasm,
emscripten) running inside deno**, as a middle ground between the eryx WASM
backend (in-process, idiosyncratic wheel support) and the podman container
backend (full linux images, heavyweight). Motivated by PEP 783 (accepted
2026-04): `pyemscripten_*_wasm32` binary wheels can now live on PyPI, so the
pyodide ABI is a real distribution target.

Status: **the end-to-end demo works.** Host spawns a locked-down deno
subprocess, deno boots pyodide, the guest runs the unmodified
`fxtr_sandbox_rpc_adapter` + `fxtr2-portable` stack over a UDS, and the host
drives the same spawn→connect→typed-service flow the eryx/container backends
use (the `rpc_task` greeter demo). ~1.6s wall from spawn to torn-down,
~10-20ms per service call.

## Files

- `main.ts` — the deno driver: dials the host's per-spawn UDS, boots
  pyodide, NODEFS-mounts the site-packages dir read-only, exposes byte-level
  connection handles to Python as a JS module (`fxtr_deno_bridge`: the
  already-dialed `main` handle + `connect_transport()` which dials one fresh
  connection to the host's multiplexed transport UDS per call), runs the
  bootstrap.
- `guest_bootstrap.py` — runs inside pyodide: wraps the bridge as an anyio
  `ByteStream`, wraps that in `SocketJSONStream` (same framing as the
  container runtime), builds a `PyodideServiceContext`
  (`SandboxServiceContext` impl), and hands off to
  `fxtr_sandbox_rpc_adapter.main(ctx, entry_point=...)`. No eryx-style
  socket/loop shims needed — but note the *top-level await* entry style:
  `asyncio.run` / `anyio.run` need JSPI in pyodide; running on the live
  WebLoop via `runPythonAsync` is the supported path (same shape as eryx's
  `_runner.py`). `open_transport_stream(token)` mirrors the container
  runtime's: bridge-dial the transport socket, `write_routing_header` /
  `read_routing_response` (reused from `transport_routing.py`), return the
  byte stream.
- `build_guest_site_packages.sh` — builds the guest site-packages dir with
  uv: repo-local pure-python packages (`fxtr2-portable`, sandbox types,
  adapter, `rpc_task`) installed `--no-deps`, third-party deps resolved
  wheel-only against `--python-platform wasm32-pyodide2024` so a
  host-platform compiled wheel can never sneak in.
- `host_common.py` — the host-side launcher (`open_deno_sandbox`), the
  spike's stand-in for a future `_spawn_runner_nix_pyodide` arm. Mirrors
  `fxtr_sandboxing.direct_launcher.open_sandbox`'s body over a subprocess:
  UDS listener(s) → `deno run` (see permission profile below) →
  `accept_or_exit` → `SocketJSONStream` → `wrap_json_stream` →
  `open_rpc_scope` → bridge → `_SpawnConnectWrapperImpl` (sentinel bundle)
  → yields the connect ref → `teardown_container_proc`. With
  `transport_server=` it also serves the multiplexed transport UDS via
  `transport_accept_loop` (all reused from `_container_common.py`).
- `host_demo.py` — greeter demo: `connect(EntityRef[GreetingConfig])` →
  `greet`/`add`.
- `host_demo_transport.py` — transport demo: `transport_task.raw_echo`
  minting + redeeming tokens against a host-side echo handler
  (`TransportConnectionServer` + `NetworkHandleAuthorizerImpl`, no
  env/spawner).
- `lockfetch_spike/` — the nix locking spike (see below): builds a guest
  site-packages dir straight from `uv.lock`, including a `pyemscripten_*`
  binary wheel, bypassing uv2nix's `mkVirtualEnv`.
- `deno.json` — **load-bearing**: without a config file here, deno's config
  discovery walks up to the repo root, finds `pnpm-workspace.yaml`, and
  *rewrites the repo's `package.json`* trying to migrate it (then exits 1).

## Run

```sh
./build_guest_site_packages.sh   # once (uses host uv; all pure wheels)
# warm deno's npm cache for pyodide once (the sandboxed subprocess has no net):
deno run -A --config deno.json main.ts /dev/null x x 2>/dev/null || true
cd ../../projects/fxtr_sandboxing/fxtr_sandboxing
uv run python ../../../spikes/deno_py_sandbox/host_demo.py
```

## Deno permission profile (the sandbox boundary)

```
--no-prompt
--allow-read=<deno cache>,<spike dir>,<site-packages dir>,<socket path>
--allow-write=<socket path>
--allow-net=unix:<socket path>
```

No other net, no env, no run, no ffi. Note: pyodide guest code can call
arbitrary JS, so deno's permission layer (a V8 isolate + op checks) is the
real perimeter — roughly eryx-class isolation, below container-class.
Memory caps via `--v8-flags=--max-heap-size=N` (not wired up here yet).

## Measured (this machine: M-series mac, deno 2.9.1, pyodide 314.0.2)

- `loadPyodide` from warm deno cache: ~950ms
- spawn → typed service connected (incl. pyodide boot, portable-fxtr2
  import, RPC handshake, connect provenance walk): ~1.6s
- per-call round trip after connect: ~7ms
- guest process RSS: ~225MB
- (context: eryx warm spawn ~10-20ms but ~3s per-spawn factory build on the
  spawner path; podman container warm spawn ~4s)

## Verified separately (scratchpad, not in this dir)

- **Binary PEP 783 wheel**: `pydantic-core` `cp314-...-pyemscripten_2026_0_wasm32`
  from PyPI imports and runs from a NODEFS-mounted dir — plain `import`,
  no micropip/loadPackage machinery. The dlopen-from-mounted-FS path works
  under deno.
- anyio task groups / memory streams / cancel scopes / events under
  pyodide's WebLoop.
- `uv lock` (universal) records `pyemscripten_2026_0` wheels in `uv.lock`
  even though `--python-platform` has no 2026 value yet (closed enum:
  `wasm32-pyodide2024`/`2025` as of uv 0.11.26) — so uv2nix-style wheel
  selection from the lock is not blocked by the enum.

## Gotchas found

- **deno config discovery** rewrites the repo root `package.json` if it
  finds a pnpm workspace and no nearer config — always ship a `deno.json`
  next to the driver (or run with an explicit `--config`).
- **JS `null` → `JsNull`** in pyodide 314 (not Python `None`); the bridge
  signals EOF as an empty `Uint8Array` instead.
- **macOS `SUN_LEN`**: UDS paths must stay short; the host uses
  `tempfile.mkdtemp(prefix="fxtr-deno-")` (i.e. `$TMPDIR`), same as the
  container runner.
- Deno gates unix sockets behind `--allow-net=unix:<path>` *plus*
  `--allow-read`/`--allow-write` on the socket path.
- Deno NODEFS **writes** are broken (deno bug #30972); reads (including
  `.so` dlopen) work. Mount site-packages read-only; guest writes go to
  MEMFS.

## The nix locking story (`lockfetch_spike/`)

**Finding:** the existing `mkVenv` / `_mkGuestSitePackages` path can never
select a PEP 783 binary wheel. pyproject-nix's wheel selection
(`pypa.selectWheels` → `isPlatformTagCompatible`, both at the repo-pinned
rev and current master) has no arm for `pyemscripten_*_wasm32` platform
tags — an emscripten wheel is always "incompatible", so uv2nix silently
falls back to compiling the sdist for linux (the wrong artifact). No
upstream issue/PR exists for this.

**Design that works** (proven end-to-end in `lockfetch_spike/flake.nix`):
reuse uv2nix's *lock parsing + marker resolution* only —
`lock1.parseLock` + `lock1.resolveDependencies` with a
`pep508.setEnviron`-retargeted environ (`sys_platform = "emscripten"`,
`platform_machine = "wasm32"`, python 3.14 for `python_version` markers) —
then do our own per-package wheel pick: exact-ABI `pyemscripten_<year>_wasm32`
wheel if the lock has one, else the pure `py3-none-any` wheel, else fail
loudly. Every wheel has url+sha256 in `uv.lock`, so `fetchurl` is pure (no
FOD relocking), and the wheels unzip into a flat dir. Verified: the
resulting store path mounts into pyodide-in-deno and the pydantic-core
binary wheel imports and runs (`check_lockfetch.ts`).

**Local packages** (second + third rounds of the spike): built through the
ordinary pyproject-nix `pythonSet`. The overlay must carry the
**emscripten environ** (guest workspaces constrain `[tool.uv] environments`
to emscripten, and uv2nix asserts the lock's supported-markers are
satisfiable by the overlay environ — a host-environ overlay fails that
assert). That in turn would force each plucked local's propagated dep
closure through emscripten wheel selection — where a binary dep has no
compatible wheel and sdist-builds for linux — so plucked local drvs get
`propagatedBuildInputs = [ ]` (building a pure-python local's installed
tree doesn't need its runtime deps present; verified with localpkg
depending directly on pydantic-core). Then each drv's installed
`lib/python*/site-packages` subtree is copied
(the drv `out` is an installed tree — no wheel `dist` output at this
pyproject-nix rev) and `__pycache__` stripped. Verified end-to-end by
`check_local.ts` (localpkg with hatch force-include + the pydantic-core
binary wheel importing together). Limitation found: uv2nix stages only the
member dir when building a *local* package, so out-of-dir force-include
(fxtr2-portable's `../fxtr2`) fails for local members; git-sourced deps
build from the full fetched repo, so the fxtr2-portable pattern works
there — which is how it actually reaches guest workspaces (the existing
eryx mkVenv path already exercises this). Local members must be
self-contained.

Notes for the real `mkPyodideService`:

- The ABI tag (`pyemscripten_2026_0_wasm32`) should be a flake-level
  parameter next to the pinned pyodide version — they move together.
- This also subsumes the "leaner uv2nix wiring that skips mkVirtualEnv"
  followup already noted in `_mkGuestSitePackages.nix` — the same
  lock-driven builder with the emscripten arm removed works for the eryx
  wasm path too.
- Built here with host nix on darwin; content is platform-independent
  (fetch + unzip), so it runs identically through the fxtr_nix_runner
  podman backend.
- The pyodide-distribution index (`index.pyodide.org`) wheels enter this
  design as ordinary locked wheels *if* uv resolves against that index
  (`--extra-index-url` + `unsafe-best-match` per pyodide's docs) — a
  flake/workspace-level uv config concern, no fxtr changes.

## Not covered yet (for the real implementation / next spikes)

- The `[http]` extra path (`get_client` / `http_get`) — httpx/httpcore are
  not installed in the spike guest. Raw transport works; httpx over the
  authorizing backend should follow, but pyodide's stubbed `ssl` module is
  a possible wrinkle (TLS terminates host-side, so likely import-only).
- Nix: the real `mkPyodideService` (`kind = "pyodide"`) around the
  lock-driven builder proven in `lockfetch_spike/` (see above — the uv2nix
  question is answered), plus delivering the deno+pyodide runtime assets
  via nix (pyodide npm tarball as an FOD; deno itself expected on the host
  since a containerized-nix build of deno wouldn't run on the host).
- Spawner integration: `NixPyodideServiceDefn` entity + a
  `_spawn_runner_nix_pyodide` arm.
- Resource limits (`--v8-flags=--max-heap-size`), stdout/stderr capture
  policy, host-death cleanup story, pyodide/deno version pinning
  (flake-controllable rather than baked into fxtr).
- Python-version skew: pyodide 314.x is CPython 3.14 ↔ `pyemscripten_2026_0`
  wheels; uv's `--python-platform` enum will presumably grow a 2026 value.

## Design decisions taken (from discussion, 2026-07)

- Pyodide assets delivered via nix; deno itself comes from the host (a
  nix-in-podman-built deno wouldn't be executable on the host).
- Pin the pyodide version / ABI, but flake-controllable, not baked into
  fxtr.
- Support both PyPI (`pyemscripten_*`) wheels and the pyodide-distribution
  index (`index.pyodide.org`) as wheel sources, ideally as a flake-level
  concern (uv resolution config), with at most small ergonomic helpers in
  fxtr's nix lib.
- Main channel over UDS (not stdio) — keep stdout for user prints.
- Isolation tier: same trust tier as eryx (V8-isolate-grade), documented.
- Backend name: `kind = "pyodide"`.
