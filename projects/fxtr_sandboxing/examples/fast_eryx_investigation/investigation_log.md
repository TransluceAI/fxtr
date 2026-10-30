# Fast Eryx Investigation

Goal: drive **per-spawn** WASM sandbox startup cost down to ~tens of ms or
better in the realistic dev-loop case where:

- A handful of pure-Python wheels (`six`, `attrs`, `mashumaro`-like) are
  cached and rarely change → can be **baked** into a persistent factory.
- A handful of **local** Python packages change frequently → must be
  picked up live each spawn, not baked.
- We start from a long-lived host process that holds the factory in
  memory; first sandbox creation is what the user sees as "spawn time."

A second, slightly weaker target: cold-start time across host processes.
`SandboxFactory` exposes `save()` / `load(path)` to persist the
precompiled bytes; a cold start should be `load()` + `create_sandbox()`,
not a fresh build of the factory.

This folder is self-contained and reuses pieces of `fxtr_sandboxing` only
where helpful. Nothing in here is wired into the production stack yet.

---

## Background — what the current path does

`fxtr_sandboxing/runner.py::run` is the WASM launcher today. Per-spawn it:

1. Resolves package refs from the bundle (sync, in-memory).
2. `fetch_wheels(...)` — disk cache hit when warm; cheap.
3. `tempfile.mkdtemp("fxtr_site_packages_")` — fresh dir per spawn.
4. Walks every source-package's `source_files` dict, writes each file to
   the temp dir.
5. For each wheel, opens the zip and `extractall` into the same dir.
6. `eryx.SandboxFactory(site_packages=site_packages_dir)` — **the
   factory is rebuilt every spawn**. Per the eryx docstring this is the
   3–5 s `pre_initialize` + `precompile` step.
7. `factory.create_sandbox(callbacks=registry, ...)`.
8. `await anyio.to_thread.run_sync(lambda: sandbox.execute(runner_code))`.
9. RPC handshake over the JSON callback channel.

Step 6 is the obvious offender. Steps 3–5 also scale with the number
and size of wheels; for a 30-package dependency closure that's not
trivial either.

---

## What eryx actually exposes

(Confirmed by reading `crates/eryx-python/python/eryx/_eryx.pyi`,
`crates/eryx-python/src/preinit.rs`, `crates/eryx-python/src/sandbox.rs`,
`crates/eryx/src/sandbox.rs`.)

### `SandboxFactory(*, site_packages=None, packages=None, imports=None)`

- `packages`: list of `.whl` / `.tar.gz` paths. Each is extracted via
  `ExtractedPackage::from_path`; native `.so` extensions are collected
  and statically linked into the precompiled component (paths
  `/site-packages/<rel>`); pure-Python files end up in a temp dir.
- `site_packages`: existing site-packages dir; merged with the package
  output (extra packages are *copied into* `site_packages` if both are
  given).
- `imports`: module names to **pre-import** into the snapshot. Any
  module pre-imported here becomes available with no per-sandbox import
  cost.
- Internally: `pre_initialize(...)` → `preinit_bytes` →
  `PythonExecutor::precompile(preinit_bytes)`. Both run on a freshly
  built tokio runtime. **This is the 3–5 s step.**
- The factory holds the precompiled bytes plus `extracted_packages`
  (kept alive only to prevent temp-dir cleanup).

### `factory.save(path)` / `SandboxFactory.load(path, *, site_packages=None)`

- `save` just writes `precompiled` bytes to a file.
- `load` reads them back and reuses the embedded stdlib path. Important:
  **`load` does not retain the original wheel temp dir** — if we want
  pure-Python wheel files available, we have to pass a `site_packages`
  that contains them, OR every wheel module the sandbox needs must have
  been pre-imported (`imports=`) at factory build time.

### `factory.create_sandbox(*, site_packages=None, callbacks=..., volumes=..., ...)`

- `site_packages`: if provided, **overrides** the factory's
  site-packages. If omitted, uses the factory's stored path.
- `volumes`: `Sequence[tuple[host_path, guest_path, read_only]]` — wires
  through `eryx::VolumeMount`. cap-std-mediated host mount; the guest
  path is preopened via WASI. **Read-only mounts work.**
- All the `Sandbox` knobs come back here (callbacks, network, secrets,
  output handlers, etc.).
- Per the docstring this is the ~10–20 ms call.

### `Sandbox` (no factory)

- Already pre-initialized via the embedded runtime (~1–5 ms claim).
- Exposes the same `volumes=` parameter.
- No way to add custom packages without a factory.

### `Session` / `VfsStorage`

- `Session` keeps Python state across `execute()` calls.
  `snapshot_state` / `restore_state` only handle pickleable user
  variables — module-level state of imported packages does not survive.
- `VfsStorage` is in-memory writable storage mounted at `/data` (or
  custom path). Useful for *writable* per-sandbox files but doesn't help
  with imports.
- Probably not directly useful for our goal — but worth knowing about.

### `with_site_packages` at the lower level (Rust core)

- `eryx::Sandbox::builder().with_site_packages(path)` accumulates into a
  `Vec` — the *executor* supports multiple site-packages dirs (mounted
  at `/site-packages`, `/site-packages-1`, …, all on `sys.path`).
- The Python-facing `SandboxBuilder` wrapper only stores
  `Option<PathBuf>` though, so multiple dirs aren't reachable from
  Python.
- Workaround if we ever want multiple: use `volumes` for the extra
  dirs and add to `sys.path` in the runner.

### Useful upshots

- **Pre-importing matters.** A module that's been pre-imported into the
  snapshot has zero per-sandbox import cost. If the wheel set is
  reasonably stable, we should pre-import as much as we can.
- **Volumes are cap-std-mediated host mounts**, so live local source
  picked up via a volume mount has no copy cost — it's literally the
  host directory at WASI level.
- **Native extensions are baked into precompiled bytes**, so even after
  `save`+`load` we don't need the original wheel files for `.so`s.
- **Pure-Python wheel files are NOT baked into precompiled bytes.**
  They live on disk in the factory's temp site-packages dir, mounted at
  `/site-packages` at sandbox creation time. Across processes (i.e. when
  using `save`+`load`) we have to keep a stable, persistent
  site-packages dir or pre-import everything we'll need.

---

## Architecture sketch — proposed split

The shape I want to evaluate:

1. **Persistent factory**, built once per (set-of-wheels, set-of-imports):
   - `SandboxFactory(packages=[whl1, whl2, ...], imports=["pkg1", "pkg2", ...])`
   - Pure-Python wheel files are extracted to a stable site-packages
     dir on disk (not the per-process temp dir — we want this to
     survive `save`+`load`).
   - Saved to `factory.bin` alongside that site-packages dir.
   - In a fresh process: `SandboxFactory.load(path,
     site_packages=stable_site_packages)`.
2. **Per-spawn**, fast path:
   - `factory.create_sandbox(callbacks=..., volumes=[(local_pkg_root,
     "/local-pkgs", True)])`
   - In the sandbox runner: `sys.path.insert(0, "/local-pkgs/<pkg>")`
     for each local package (or just `"/local-pkgs"` if we mount a
     parent dir that holds all of them).
   - No site_packages override → factory's wheels stay available.

This avoids:
- Rebuilding the factory each spawn (the big win).
- Writing per-spawn temp dirs / unpacking wheels per spawn.
- Copying local source files into a temp dir.

It introduces a new constraint: local packages can no longer be
"projected" through `PySourcePackage.source_files` (in-memory dict);
they need to live on disk somewhere we can mount. For the dev-loop
case (file-on-disk source) that's natural.

---

## Ideas to try, in order

I'll branch off as needed; updating this list as findings come in.

### 1. Stage-by-stage timing of the **current** path

Build a tiny bench harness (`bench_harness.py`) that drives the existing
`fxtr_sandboxing.runner.run` against a fixed bundle and reports
timestamps for: factory creation, sandbox.execute startup, first RPC
round-trip, full task completion.

Need this **before** any optimization to know what we're actually
saving. Want absolute numbers and a per-stage breakdown.

### 2. Bare `SandboxFactory()` + no packages baseline

Time `eryx.SandboxFactory()` with no packages, no imports — to see how
much of the 3-5s is the embedded preinit-vs-pre_initialize-with-packages
overhead. This sets the "best possible" floor for any factory build.

### 3. Factory with wheels baked in, no per-spawn rebuild

Build factory once with `packages=[<list of cached wheels>]` and
`imports=[<top-level names>]`. Time many `create_sandbox()` calls in a
row plus full RPC round-trip on each. This should be the headline
number: **how fast is a "warm" spawn?**

### 4. `factory.save()` + `SandboxFactory.load()` cold path

Save factory to disk, then in a fresh `python` process load it back and
time `create_sandbox()`. Two questions:
- How fast is `load()`?
- Do pure-Python wheel modules still resolve when we pass the same
  `site_packages` path that was used at factory build time?

### 5. Volume-mount path for local packages

Without rebuilding the factory: mount a host dir at `/local-pkgs` via
`volumes=`, add it to `sys.path` in the runner, import a package from
there. Time: mount + import + first call.

Followups:
- Is read-only `volumes` cheaper than read-write?
- Does adding multiple volumes (one per package) cost more than one
  parent-dir mount?
- Does `import` from `/local-pkgs` defeat the precompiled-snapshot
  optimizations? (i.e. does importing **fresh** modules at runtime
  cost similar to importing baked ones?)

### 6. What happens if pre-imported modules are also importable from local?

Edge case: factory pre-imports `mypkg` from a wheel; user mounts a
local `mypkg` at `/local-pkgs`. Which wins? Likely the snapshot wins
because it's already in `sys.modules`. Want to confirm so we have
clear semantics.

### 7. Python import-cost comparison: snapshot vs cold

Time `import attrs` (or whatever) inside the sandbox — once when it's
been pre-imported into the factory snapshot, once when it hasn't. The
delta tells us how aggressive `imports=` should be.

### 8. Factory-rebuild cost as a function of N wheels / N pre-imports

Does adding 30 pre-imports vs 3 substantially change build time? Helps
us decide whether to be picky or aggressive about what we bake.

### 9. Reuse strategies / nice-to-haves to evaluate later

- **In-memory shared factory** across sandboxes: factory is already
  reusable across `create_sandbox` calls — this is the dev-loop case.
- **On-disk factory cache** keyed on wheel set hash + pre-import
  set: lets us reuse across processes without rebuilding.
- **Symlink / overlay trick** if we ever need both the factory's
  site-packages and an extra site-packages: build a per-spawn dir of
  symlinks to the factory's wheels + the live local source, pass
  that as the `site_packages` override. Avoids file copies; not sure
  if WASI cap-std follows symlinks though — needs checking.

---

## Findings

(Filled in as experiments run. Each entry: date, what I ran, what I
saw, conclusion.)

### 2026-05-05 — first numbers, eryx 0.4.6, macOS

Wheel set: `six`, `attrs`, `click` (3 pure-Python wheels, ~170 KB total).
Local-pkg set: two trivial packages under `local_pkgs/`.

| scenario                                            | time    |
| --------------------------------------------------- | ------- |
| `eryx.SandboxFactory()` (no packages, no imports)   | 3090 ms |
| `eryx.SandboxFactory(packages=[3 wheels], imports=[])` | 2703 ms |
| `eryx.SandboxFactory(packages=[3 wheels], imports=[3])` | 2837 ms |
| **warm `factory.create_sandbox()` (median, n=5)**    | **10.9 ms** |
| **warm `execute("import six,attrs,click")` (median)** | **5.1 ms** |
| warm `create_sandbox(volumes=[(local_pkgs, ...)])`   | 10.8 ms |
| warm `execute("import local_pkg_b; .combined()")`    | 7.4 ms  |
| `factory.save(...)` (56.9 MB on disk)               | 6 ms    |
| `SandboxFactory.load(...)` (cold, fresh process)    | 113 ms  |
| `create_sandbox` after cold load                    | 21 ms   |
| `execute("import six,attrs,click")` after cold load | 7 ms    |

Headline conclusions:

1. **Factory creation cost is roughly fixed** at ~3 s — it's almost
   entirely the embedded preinit + WASM precompile, not the work to
   pull in 3 small wheels (3 wheels added 0.0–0.2 s, pre-importing them
   added a similar small delta on top).
2. **Warm spawn is already cheap** if we don't rebuild the factory:
   ~11 ms create_sandbox + ~5–7 ms first import. That's ~250× the
   current production path's per-spawn cost (which rebuilds the factory
   every time, ~3 s).
3. **`save` + `load` works as a cold-start shortcut**: ~120 ms for
   load and ~140 ms total to first ready sandbox in a fresh process.
   The on-disk file is non-trivial (~57 MB precompiled bytes) but
   that's the headline price for skipping the 3 s rebuild.
4. **Volume mount + sys.path insert works as the live-source path,
   with no measurable overhead vs no-volume**: median spawn was
   identical (10.8 vs 10.9 ms). Importing a fresh module from the
   mounted dir cost ~7 ms vs ~5 ms for the snapshot-baked imports;
   that 2 ms delta is the on-the-fly compile / import work.
5. **Cold-load did not need `site_packages=`** to make the
   pre-imported wheels work — they were already in the snapshot's
   `sys.modules`. (Open question: would a wheel module that's *not*
   pre-imported still resolve after save+load? Need to check next.)

### 2026-05-05 — bare `Sandbox()` + volumes

Question: can we skip `SandboxFactory` entirely by using bare
`eryx.Sandbox()` and mounting wheels + local source via `volumes=`?
That would dodge the 3 s factory build cost.

Setup: pre-extracted wheels into `.cache/wheels_extracted/` once
(`zipfile.extractall`), then per spawn: `eryx.Sandbox(volumes=[(extracted,
"/wheels", True), (local_pkgs, "/local-pkgs", True)])` and
`execute("sys.path.insert(0,'/wheels'); sys.path.insert(0,'/local-pkgs');
import six,attrs,click,local_pkg_b; ...")`.

| step                                 | time |
| ------------------------------------ | ---- |
| `eryx.Sandbox(volumes=...)` (first) | 294 ms |
| `eryx.Sandbox(volumes=...)` (subsequent, n=4) | ~0.3 ms |
| `sandbox.execute(...)` (each call, n=5) | **250–290 ms** |

So **constructor is ~free, but every `execute()` pays a fresh Python
WASM init** — no pre-compiled snapshot to start from. ~25× slower per
spawn than the factory path (250 ms vs 10–20 ms for create_sandbox +
execute combined).

Why: the factory path pays the heavy Python interpreter init *once*
(into the precompiled WASM component) and `create_sandbox` snapshots
that pre-initialized state. Bare `Sandbox` just hands you the embedded
raw runtime and `execute()` does cold Python boot every time. The
"1–5 ms" Sandbox-creation claim in the eryx docstring is real, but
misleading — the work shifts into `execute`.

Conclusion: bare-Sandbox-with-volumes is **not** the fast path we want.
The factory has to be in the picture; volumes are a useful add-on for
live local source on top of a factory, not a replacement for the
factory.

### 2026-05-05 — empty factory + volumes; isolating what the factory actually does

Question: does `SandboxFactory` need to bake the wheels in, or is its
value just "Python is pre-initialized into a precompiled snapshot"?

Two new scenarios:

| scenario                                        | factory build | create_sandbox | execute |
| ----------------------------------------------- | ------------- | -------------- | ------- |
| empty factory + everything via volumes          | 3432 ms       | ~11 ms (median) | **~260 ms** |
| factory(packages=baked, imports=[])             | 3391 ms       | ~12 ms         | **~256 ms** |
| (vs.) factory(packages=baked, imports=[3])     | 2837 ms       | ~11 ms         | ~5 ms |
| (vs.) bare Sandbox + everything via volumes     | (none)        | ~0.3 ms        | ~270 ms |

**The factory's value is almost entirely `imports=[...]`**, not where
the wheel files live. Across all four shapes the per-spawn execute
cost falls into two clear buckets:

- **~5 ms** if the wheel modules were pre-imported into the snapshot
- **~250–270 ms** if the wheel modules are imported "cold" — whether
  they sit in the factory's baked `/site-packages` or in a mounted
  `/wheels` volume

create_sandbox is ~10 ms with a factory and ~0.3 ms without; that's
small enough to ignore relative to the 250 ms cold-import gulf.

So baking wheels into `packages=[...]` without also listing them in
`imports=[...]` buys us almost nothing — the snapshot still doesn't
have them in `sys.modules`. The 50× speedup we saw earlier comes from
the pre-import, full stop.

This changes the architecture story: it doesn't really matter whether
wheels are in `packages=` or on a mounted volume; what matters is
whether we pre-import them. So:

- **Bake what we'll always need + pre-import it** (`packages=[...],
  imports=[...]`) → ~5 ms first import.
- **Mount the long tail (rarely-used wheels) via a volume** → ~250 ms
  first import, deferred until the code actually imports it.
- **Local source: must go via volume** (changes per spawn). Pays
  ~250 ms per fresh module imported. For tiny modules this is
  much cheaper (`local_pkg_b` → `local_pkg_a` was ~7 ms). For larger
  local trees we'd want to bake-and-pre-import the stable parts and
  reserve the volume for the actively-edited subset.

Open thread: that ~250 ms cold-import cost is almost certainly
WASM-Python's cost for parse+compile+execute of the module body —
not eryx overhead. Worth a one-off check: how does `execute("import
attrs")` time compare to `execute("import json")` (a stdlib module
that's not pre-imported but lives in the embedded stdlib)? If similar,
the cost is fundamental Python-WASM compile cost; if `import json`
is much cheaper, eryx might have something like a stdlib bytecode
cache we could exploit.

### 2026-05-05 — late preload via `Session` (the surprise winner)

Question: instead of baking imports into a factory, can we open a
sandbox empty, run an `execute("import six, attrs, click, ...")`
preload step, and then have subsequent `execute()` calls be cheap?

`Sandbox` says no in the docstring; `Session` says yes. Measured all
three:

| API                                            | preload | next call | next call |
| ---------------------------------------------- | ------- | --------- | --------- |
| `Sandbox.execute()` × 3 (same instance)        | 262 ms  | 246 ms    | 255 ms    |
| `Session.execute()` × 3 (same instance)        | 285 ms  | **1.8 ms** | **0.2 ms** |
| `Session` + `clear_state()` between calls      | 253 ms  | 0.5 ms (clear) → 1.7 ms re-import | — |

`Sandbox` really is fully isolated per the docs — no late-preload
possible. `Session` is the opposite: imports paid once last forever.
And critically:

> **`Session.clear_state()` clears user-defined variables but leaves
> `sys.modules` intact.**

That's the killer combo: pay ~250 ms once when the Session is created
to import everything we want warm; then per "spawn" do `clear_state()`
(~0.5 ms) + a near-empty `execute()` (~1–2 ms) for tasks whose code
only depends on the warm imports. **~2 ms per spawn**, vs ~16 ms for
the factory + pre-imports path and ~270 ms for everything-via-volume.

#### Picking up local-source edits in a long-lived Session

If `clear_state()` keeps `sys.modules`, edits to a volume-mounted
local package won't be picked up automatically. Need to drop the
relevant entries before re-importing. Measured:

| step                                                         | time |
| ------------------------------------------------------------ | ---- |
| `Session.execute(preload + import local_pkg_b)`              | 313 ms |
| `clear_state()`                                              | 0.5 ms |
| `execute("import local_pkg_b")`  (still cached)              | 0.2 ms |
| `execute("del sys.modules[k] for k in local_pkg*; import local_pkg_b")` | **3.2 ms** |
| `execute("import local_pkg_b")`  (re-cached)                 | 0.2 ms |

So the live-source dev-loop path is:

1. Session warmup once: import all the stable wheels (~250 ms one-time).
2. Per spawn: `clear_state()` + execute that drops `local_*` from
   `sys.modules` and runs the task.

Total per-spawn cost = clear_state + cold-import-of-local-source. For
our two-file local tree that's ~3 ms; for a real local package the
cold-import would scale roughly with code size (recall: full wheels
~250 ms cold; tiny modules ~3 ms).

#### Caveats — what `clear_state` does not do

- **Module-level state in imported modules persists.** A wheel's
  module-globals (registries, caches, side-effect counters) carry
  between spawns. Most well-behaved libraries are stateless or
  idempotent, but not all — anything that mutates module globals
  becomes a leak between tasks.
- **VFS state persists** (in-memory storage if `vfs=` was passed to
  the Session). Need to reset / use a fresh VFS per spawn if writable
  files are involved.
- **No way to clear `sys.modules` cheaply.** We can selectively drop
  entries we know about, but a wholesale `sys.modules.clear()` would
  defeat the warm cache and force everything cold next call. Means
  isolation between spawns is *cooperative* — we have to trust the
  user code not to depend on or mutate state that we don't reset.
- **`Session` doesn't go through `SandboxFactory`**, so its volumes,
  callbacks, network config etc. are configured at Session creation
  time. To match the current host-side ergonomics (per-spawn callback
  queues, per-spawn fxtr2 dispatch scope) we'd want one Session per
  "warmed environment" and serialize task spawns through it (or
  multiplex multiple tasks within one Session by routing each on its
  own RPC channel).

#### Strategy menu after this finding

We now have three plausible per-spawn shapes, ordered fastest to most
isolated:

1. **`Session` + `clear_state` per task (~2–10 ms per spawn).**
   Cooperative isolation; module-level state can leak. Best if we trust
   the workload.
2. **`SandboxFactory(packages, imports)` + `create_sandbox()` per task
   (~16 ms per spawn).** Hard isolation between spawns. Pay 3 s up
   front (once per process; or ~140 ms cold via `save`/`load`).
3. **Hybrid: factory for the host process, Session for fast workloads
   that opt in.** Use `(1)` for short-lived dev-loop iterations and
   `(2)` for production tasks that need real isolation.

The current production path (rebuild factory each spawn, ~3 s) is
worse than all three by an order of magnitude or more, so any of
these is a big win.

### 2026-05-05 — pool of warm Sessions, parallel warmup

Question (from user): can we keep a pool of pre-warmed Sessions so the
~250 ms preload happens in the background and the user-visible spawn
is just `execute(real_code)`?

Two sub-questions: (a) does eryx actually release the GIL so warmups
can overlap on a thread pool, and (b) what's the per-spawn checkout
cost on a warm Session?

| step                                   | time |
| -------------------------------------- | ---- |
| First Session (priming, JIT etc.)      | 610 ms |
| Sequential warmup of 4 Sessions        | 1046 ms (~261 ms each) |
| **Parallel warmup of 4 Sessions**      | **258 ms (4.06× speedup)** |
| `execute(real_code)` on a warm Session | ~2 ms |

eryx's `Sandbox.execute` calls `py.detach()` (released the GIL) before
running WASM, so multiple Sessions doing their preload on a
ThreadPoolExecutor really do overlap; we got near-perfect 4× scaling
with 4 workers on this machine.

This **is** the answer to the user's "preemptive pool" idea:

1. Background filler thread/task keeps the pool at N warm Sessions.
   Refilling a slot costs ~260 ms wall-clock but multiple slots refill
   concurrently — with M workers you replenish ~M slots / 250 ms.
2. User spawn = pop a warm Session, `execute(task_code)` (~2 ms),
   discard the Session. No cleanup required between spawns because
   each one was a fresh WASM instance.
3. **Hard per-spawn isolation** — the cooperative-isolation caveat
   from the long-lived-Session pattern only applied *within* a
   single Session (`clear_state` between calls). Between Sessions,
   state is fully independent.
4. **Imports change → drain + refill the pool.** Wall-clock cost is
   one parallel warmup wave (~250 ms with enough workers). No
   factory rebuild, no save/load, no stop-the-world.

Compared to the other strategies:

| strategy                                         | one-time | per spawn (latency) | isolation |
| ------------------------------------------------ | -------- | ------------------- | --------- |
| **Warm-Session pool, parallel warmup**           | 0 (background) | **~2 ms**       | **hard** |
| Long-lived Session + `clear_state` per task      | ~250 ms  | ~2–10 ms           | cooperative |
| Factory(packages, imports) + create_sandbox      | ~3 s (or ~140 ms cold) | ~16 ms | hard |
| Empty factory + cold imports via volumes         | ~3 s     | ~270 ms            | hard |
| Bare Sandbox + cold imports via volumes          | none     | ~270 ms            | hard |
| Current production path (rebuild factory each)   | —        | ~3 s               | hard |

Hard isolation + ~2 ms spawn latency, in exchange for:

- **Memory budget**: N warm WASM instances live in RAM. Each is
  whatever the embedded Python interpreter + preloaded modules costs
  (~tens of MB; haven't measured directly yet — worth doing).
- **Pool sizing**: spawn rate vs warmup throughput. If users spawn
  faster than the pool refills, latency degrades back toward the
  ~260 ms preload time. With M workers and ~250 ms preload, you
  sustain M × 4 spawns/sec without tail latency.
- **Pool invalidation policy**: when the import set changes, every
  warm Session in the pool is stale. Easy to handle (drain + refill)
  but means we want a clean signal for "imports changed" — e.g. a
  wheel-set hash or filesystem mtime watch on the wheel cache.

This is genuinely the most attractive shape so far: it gets the
~2 ms spawn latency of the cooperative-Session approach AND the
hard isolation of the factory approach, by paying with idle
background CPU + RAM rather than per-spawn time.

### Implications for the architecture

The numbers strongly support the split sketched above:

- **Persistent in-process factory + per-spawn `create_sandbox`** —
  ~10–20 ms per spawn, single ~3 s build cost. This alone is a huge
  improvement over the current path.
- **`save`/`load` cache** for cross-process startup — adds ~120 ms
  cold-start cost in exchange for skipping the 3 s build.
- **Volumes for live local source** — no measurable cost beyond the
  baseline spawn, picks up host-side edits with no copy step.

Next experiments worth running:
- Does an un-pre-imported wheel module still resolve after
  `save`+`load`? (Tests whether the precompiled bytes carry the wheel
  file contents or just the snapshot of pre-imported state.)
- Local-shadow semantics: if `mod` is both pre-imported into the
  factory and present in `/local-pkgs`, which wins after a fresh
  spawn? (Affects whether dev-loop edits to a "vendored" package
  get picked up.)
- Bigger wheel set and a wider import set — does the 3 s factory
  build scale linearly, sub-linearly, or hit a wall?
- Plug back into the fxtr2 RPC layer — does running the
  `JSONObjectStream` + `open_rpc_scope` stack on top add more
  per-spawn cost than the bare numbers above?
