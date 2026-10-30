# Stage 2 scratch: sandbox-venv design discussion

For Chunk 1 of `stage2_sandbox_runtime.md`. Resolved at the bottom
of the doc; options + analysis above kept as context for later
revisits.

> **Note (2026-05-29):** `mkSandbox` (the combined prelude+venv
> workstation-sandbox helper) was later deleted — the workstation
> sandbox is no longer nix-built (see `stage2_sandbox_runtime.md`
> "Revised direction"). The `_mkGuestSitePackages` decisions on this
> page **still hold**: that internal helper survives and now feeds the
> *bundle* path (`mkWasmPackageBundle`, Stage 3). Read "mkSandbox /
> mkWasmPackageBundle consume it" as "mkWasmPackageBundle consumes
> it."

## Resolved decisions (Chunk 1 output)

| Axis | Decision |
|---|---|
| 1 — build approach | **1A**: build full `mkVenv` internally; extract via `cp -RL` (not symlinks) so the inner venv stays out of the output closure. Followup: move to a leaner uv2nix wiring (1B-ish) if/when needed. |
| 2 — output structure | **2A**: flat at `$out/` — `$out` IS the site-packages directory. mkSandbox does `cp -RL ${guestSitePackages}/. $out/venv/`. |
| 3 — python version | **No `python` knob on the helper.** Inherits mkVenv's default (currently `python312`). Pure-python wheels are interpreter-agnostic; revisit if a real version-sensitive case appears (folded into Axis 1's followup). |
| 4 — validation | **Trust the agent.** No build-time check that wheels are pure-python in v0. Followup tracked in `followups.md` to consider enforcing. |
| 5 — naming / exposure | **Internal helper** at `nix/lib/_mkGuestSitePackages.nix`. Not in `lib.fxtr.*`. Called by `mkSandbox` (Stage 2) and `mkWasmPackageBundle` (Stage 3). Promote to public if a hand-rolled consumer appears. |

Naming note: the helper is `_mkGuestSitePackages` (not `mkSandboxVenv`)
because (a) what it produces is a site-packages directory, not a
full venv, and (b) "guest" reads accurately for both sandbox and
bundle consumers — Stage 3's `mkWasmPackageBundle` builds a
*package* that runs inside an eryx guest, not strictly a "sandbox."

The agent's flake never references `_mkGuestSitePackages` directly.
They write `lib.fxtr.mkSandbox { prelude = ./prelude.py;
workspaceRoot = ./.; }` (or similar — exact mkSandbox arg shape is
settled in Chunk 2) and the helper threads through internally.

Smoke-test implication for Chunk 2: the import-check derivation
exercises mkSandbox end-to-end (bind-mount
`${mkSandbox-output}/venv/` into eryx, `import fxtr2`) rather than
hitting the internal helper in isolation. More faithful since it
covers both helpers together; matches the path the MCP will use.

## Post-Chunk-2 finding: how guest pure-python wheel selection actually works

(Captured here so we don't relitigate. Settled during Chunk 2's
implementation; the contract below is what
`_mkGuestSitePackages` + the sandbox-smoke workspace assume.)

**Goal.** No `.so` files in the venv we hand to eryx. The runtime
would skip them by ABI tag anyway, but they're dead weight in the
nix store and they encode a host platform that has nothing to do
with eryx's WASM target.

**Mechanism — two knobs that cooperate:**

1. **The agent's pyproject sets BOTH `[tool.uv] environments` and
   `required-environments`** to a wasi/wasm32 marker. The
   `environments` (restrict-set) knob is load-bearing: it filters
   the uv lockfile so platform-specific wheels (cbor2's
   `cp312-x86_64-linux-gnu`, etc.) never get included — only
   `py3-none-any` wheels remain candidates. `required-environments`
   (expand-set) on its own is NOT sufficient.
2. **`_mkGuestSitePackages` passes `environ = { sys_platform =
   "wasi"; platform_machine = "wasm32"; }`** through to uv2nix's
   `mkPyprojectOverlay`. Without this, uv2nix's supported-markers
   compat check rejects the wasi-restricted lock when the build
   host is linux/x86_64. `environ` ONLY satisfies that check; it
   does NOT redirect wheel selection.

**Why we can't drop `environments` and lean on `environ` alone.**
uv2nix delegates wheel selection to `pyproject-nix`'s
`selectWheels` (lib/pypa.nix). The sort prefers higher
`bestLanguageTag` (cp312 → 312 > py3 → 3), so a host-tagged wheel
beats `py3-none-any` whenever both are in the lock. There is no
documented knob in pyproject-nix or uv2nix to override the
interpreter's reported tags. Alternatives we'd need to consider:

- Patch `pyproject-nix`'s `selectWheels` to prefer broader tags
  (invasive — touches an upstream lib).
- Use a python interpreter whose own `packaging.tags` reports
  wasi-only tags (would need a nixpkgs wasi-python that doesn't
  exist for our purposes).

So `environments` restrict on the lock side is the cleanest lever.
The trade-off: `uv sync` against a workspace with `environments =
["sys_platform == 'wasi' …"]` from a non-wasi host refuses with
"env not supported." For sandbox_smoke / the agent workdir that's
fine — neither is consumed by host `uv sync`; nix builds the venv.
If a dev wants to inspect deps locally, they'd `uv tree` or read
the lockfile, not install it.

**Things that didn't work (record for posterity):**

- `[tool.uv] no-binary-package = ["cbor2"]` (force sdist build).
  uv built cbor2 from sdist as requested, but cbor2's setup.py
  compiled the C extension anyway since gcc was available in the
  nix sandbox. Same `.so` in the output.
- Stripping `*.so` files in `_mkGuestSitePackages`'s `cp -RL`
  step. Considered but rejected — silent stripping is fragile if
  a future dep ships a `.so` it actually depends on.
- `required-environments` alone + `environ` override. Lock retained
  host wheels, and pyproject-nix's sort picked the higher-language-
  tag wheel over `py3-none-any`.

---

(Pre-decision analysis below.)


## What the eryx sandbox actually wants

Cross-checking `eryx/_eryx.pyi`, `fxtr_sandboxing/runner.py`, and
`fxtr_sandboxing/_bundle_materialize.py`:

- `eryx.SandboxFactory(site_packages=<path>)` — the path points at
  a **flat directory of Python packages** (one importable top-level
  name per subdir, `.dist-info/` alongside). Read at factory-
  construction time and snapshotted into the WASM image. ~3-5 s for
  the initial bake, then sandboxes spawn off the factory in ~10-20 ms
  with those packages pre-loaded.
- `factory.create_sandbox(volumes=[…])` — per-spawn bind-mounts:
  `(host_path, guest_path, read_only)`. The runner injects
  `sys.path[:0] = [...]` at the top of the guest script so anything
  in a `sys_path_additions` entry is importable.
- The wheel layout fxtr already extracts into `site_packages=` is
  exactly what wheel extraction produces with `zipfile.extractall` —
  flat `<pkg>/__init__.py` etc. plus `<pkg>-<ver>.dist-info/`.
- No interpreter needed in the host-side venv. Eryx ships its own
  CPython 3.14 inside the WASM blob. Pure-python wheels work
  unchanged in WASM-3.14; C-extension wheels won't (unless the
  extension was specifically built for `wasi`, which is rare).
- Stage 2's plan shifts the bulk of the "baked" path (uses
  `site_packages=` on factory init) to the "bind-mounted" path
  (uses `volumes=` + `sys_path_additions=` per spawn). The factory
  becomes bare CPython.
  - Trade-off worth flagging: cold-import-from-disk per spawn vs.
    "baked into the snapshot" pre-import. Real-world impact unclear
    without measurement; probably fine for agent flows but could
    surprise us at scale.

## Today's MCP flow (the path we're replacing)

1. `sandbox_runtime/pyproject.toml` declares
   `fxtr-sandbox-rpc-adapter[fxtr2-portable]` as its only dep —
   nothing else.
2. MCP startup: `prepare(sandbox_runtime_dir)` → builds a
   `TaskBundle` walking that pyproject's full dep closure.
3. `build_site_packages_for_bundle` materializes the bundle into a
   tempdir of flat wheels + source packages.
4. `eryx.SandboxFactory(site_packages=<that tempdir>)` — fxtr2 etc.
   are baked into the snapshot, paying the ~3-5 s cost once.
5. Per `run_script`: `factory.create_sandbox(volumes=[...])` with
   the bridge stub at `/sandbox-stub` and the agent's `packages/`
   at `/local-pkgs`, plus matching `sys_path_additions`.

So the "deps closure" is **already** materialized as a flat
site-packages dir today — just via `prepare` + `_bundle_materialize`
rather than nix. The new path replaces *how that dir gets
produced* (nix instead of prepare), not what shape it has.

## Classification of venvs in the system

(For framing. Not committing to naming.)

| Venv | Where it runs | Needs interpreter? | Stage 1 helper |
|---|---|---|---|
| `fxtr_nix_writer` build-time venv | Inside a nix build derivation, on the canonical arch | Yes (`/bin/fxtr-nix-writer` console script) | `mkVenv` |
| `py_testing` build-time venv | Same — inside derivations | Yes | `mkVenv` |
| `mkEntityFromPython` caller venv | Same — inside derivations | Yes (writer script imports user packages) | `mkVenv` |
| **sandbox guest venv (new)** | Inside eryx, bind-mounted; no `/bin/python` consumed | **No** | (this discussion) |
| MCP host venv | Outside nix entirely (uv-managed in-repo) | Yes | n/a |

So the new helper occupies a category that `mkVenv` doesn't fit
cleanly into: "data, not a runnable environment." Whether that
warrants a separate helper is part of the question.

## Option matrix

Five axes; deciding each independently in chunk 1's discussion.

### Axis 1 — Build approach (how the wheels get resolved)

- **1A. Re-use `mkVenv` internally.** Build a full uv2nix
  `mkVirtualEnv`-shaped output against the agent's pyproject/lock
  (under `python = pkgs.python314`), then snapshot/symlink its
  `lib/python3.14/site-packages/` subdir into our output.
  - Pros: minimal new nix; lock-resolution path is identical to
    what mkVenv already does. Spike's `preload`/`augment` worked
    this way (full venv, even though only data was used downstream).
  - Cons: builds a host-runnable venv we discard most of. /bin/python
    etc. ride along in the closure; we'd `cp -RL` only the bits we
    need. Slight wastefulness, minor closure bloat.

- **1B. Custom uv2nix wiring, skip `mkVirtualEnv`.** Reach into
  `pythonSet` directly to enumerate per-package store paths, then
  glue them together flat (cp / symlink / linkFarm).
  - Pros: cleanest "data-only" output. No /bin/python in the closure.
  - Cons: more bespoke nix; we'd need to understand uv2nix's
    package-set internals (the spike doesn't exercise this).
    Risk of subtle bugs around how dist-info / data files normally
    get merged by `mkVirtualEnv`.

- **1C. Run `uv pip install` (or equivalent) inside a derivation.**
  Skip uv2nix; have the derivation invoke `uv` directly against
  the pyproject + lock, with wheels fetched at build time.
  - Pros: simplest mental model. uv handles all the resolution
    quirks.
  - Cons: needs network access during the build (fixed-output
    derivation territory). Loses uv2nix's per-wheel content
    addressing and reusable cache. Spike already proved uv2nix
    works for fxtr.

- **1D. Don't introduce mkSandboxVenv at all; have mkSandbox take
  a `venv` arg (full mkVenv output) and reach into its subdir.**
  - Pros: only one helper, fewer names.
  - Cons: agent's pyproject must remember to override
    `python = pkgs.python314`; the "what's the venv for" intent
    is implicit. Couples mkSandbox tightly to mkVenv's exact
    output layout (`lib/python3.X/site-packages`), which could
    bite if mkVenv changes shape later.

### Axis 2 — Output structure

- **2A. Output is flat at `$out/`** (top-level subdirs are
  importable package names). mkSandbox bind-mounts as `out/venv/`.
- **2B. Output is `$out/site-packages/`** (one indirection).
  mkSandbox bind-mounts the subdir.
- **2C. Output mirrors a real venv** (`$out/lib/python3.X/
  site-packages/`). mkSandbox reaches into it.
- (2A and 2B are mostly cosmetic; 2C only matters if we keep the
  full uv2nix output via Option 1A or 1D.)

### Axis 3 — Python version pinning

- **3A. Pin python314.** Matches eryx's runtime interpreter;
  future-proofs against any wheel that emits version-conditional
  bytecode or has `cp314_cp314_*` wheels. Requires nixpkgs to
  carry `python314` (it does on `nixos-unstable`).
- **3B. Pin python312.** Matches Stage 1's mkVenv default; smaller
  diff from the spike. Pure-python wheels (`py3-none-any`) work
  fine in eryx-3.14 regardless. But version-specific ABI wheels
  wouldn't be picked.
- **3C. Default to python314 in mkSandboxVenv, allow override.**
  Clean defaults; agent flakes can still override for experiments.

### Axis 4 — Validation / errors

- **4A. Enforce pure-python at build time.** Fail the derivation
  if any resolved wheel has a platform tag that isn't `any`. Stops
  C-extension dependencies from silently producing a broken venv;
  agent gets a build error instead of a runtime ImportError.
- **4B. No build-time check.** Trust the agent to declare only
  pure-python deps; let import-time errors in eryx surface. Less
  nix code; matches mkVenv's behavior today.
- **4C. Warn (log) but don't fail.** Less hostile than 4A but
  noisier than 4B.

### Axis 5 — Naming / exposure

- **5A. Separate `lib.fxtr.mkSandboxVenv`** sibling of `mkVenv`.
  Distinct file, distinct documentation, explicit intent.
- **5B. Extend `mkVenv`** with a knob (e.g.
  `forSandboxGuest = true;` or `output = "site-packages";`).
  One helper, behavior selected by arg.
- **5C. Hide inside `mkSandbox`** (Axis 1D's implication). No
  exposed helper for "build the sandbox venv" — it's a
  not-otherwise-exposed step inside the sandbox derivation.

## Cross-cutting questions

These don't fit cleanly on any single axis but show up in any
chosen combo.

- **`__pycache__` / `.pyc` handling.** If we go through
  `mkVirtualEnv` (Option 1A), does it emit `__pycache__/*.pyc`
  for the host python (3.14)? Those `.pyc` files would be useless
  in eryx-3.14 anyway (different bytecode header? actually no,
  same major version, same bytecode — they'd work). Likely fine
  to leave alone; cheap to strip if not.
- **`.dist-info` retention.** Some packages depend on
  `importlib.metadata` looking up their version. Keeping
  `.dist-info` is safer. uv2nix's mkVirtualEnv keeps it; manual
  layouts need to remember to copy it.
- **`bin/` scripts inside the venv.** Pure-python packages
  sometimes ship console-script shims as `bin/foo`. Eryx doesn't
  read these from a path; safe to drop. (Wasted space if we ship
  `mkVenv`'s full output.)
- **Closure size.** A full `mkVenv` output drags in nixpkgs's
  python interpreter (~30-50 MB closure) because of the `/bin`
  symlinks. A flattened site-packages-only output skips that.
  For the agent's daily workflow this probably doesn't matter
  (local nix store, copies are cheap); for a future remote
  spawner case it might.
- **uv2nix + python314 status.** Stage 1's mkVenv has worked with
  python312. We haven't yet tried python314. Risk of surprises;
  Chunk 2's smoke test is the forcing function.
- **Bridge package location** (cross-ref Q&A point 5). The plan's
  initial direction is to keep the bridge as a separate
  host-controlled bind-mount, parallel to the venv mount. So
  mkSandboxVenv's contents do NOT include
  `fxtr_ws_mcp_sandbox_bridge`. If we instead fold the bridge into
  the venv (fallback option), the agent's pyproject grows a
  `[tool.uv.sources]` entry pointing at the bridge package's
  source, and mkSandboxVenv resolves it like any other dep.
- **Interaction with `mkSandbox`'s `out/venv/` shape.** Whatever
  mkSandboxVenv emits, mkSandbox's job is to put it under
  `out/venv/` in the combined sandbox derivation. If the upstream
  output is already flat (2A), it's `cp -RL` straight in. If it's
  `lib/python3.14/site-packages` (2C), mkSandbox copies that
  subdir. Either way trivial; just want consistency.

## Options I'd want to think more about during the discussion

(Not preferences yet — just "these are the bits where I'd push back
on a too-fast decision.")

- Whether the wastefulness of Option 1A (full venv built then
  mostly discarded) is actually a problem. Disk and store-path
  closure size, yes; build time, probably no (uv2nix's overlay
  resolution is what's expensive, and that runs either way).
- Whether Option 4A's pure-python validation is worth the nix
  complexity. The error message we'd produce ("can't use this
  wheel in eryx") could be very helpful, but writing the check
  cleanly requires reading wheel filenames at build time.
- Whether to introduce a `lib.fxtr.mkSandboxVenv` *now* or wait
  until we have a second consumer (e.g. a remote spawner) and
  defer the abstraction. The downstream Chunk 4 only needs *some*
  way to produce the venv; a one-off inline expression in the
  demo flake could carry us through Stage 2 with the helper
  introduced later if it turns out to need different ergonomics.
