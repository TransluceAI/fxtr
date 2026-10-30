# Sandbox prelude + venv via nix (combined)

> **SUPERSEDED (2026-05-29).** This entire note describes the
> nix-built workstation sandbox (`fxtr.workstation.sandbox` = combined
> `prelude` + `venv`), which we **reverted**. The workstation sandbox
> stays host-built: the MCP's `run_script` path is unchanged from
> today (the `sandbox_runtime` closure supplies fxtr2 + the adapter,
> `<workdir>/packages/` is walked for local source, `<workdir>/
> preamble.py` is the prelude). fxtr2 does **not** move into a
> nix venv, so the MCP/venv version-coupling concern (Q2.D) does not
> arise. Nix's venv machinery (`mkVenv`, `_mkGuestSitePackages`)
> survives only for *bundle* venvs — see `05_sandbox_bundle.md` and
> `../stage2_sandbox_runtime.md` "Revised direction". The body below
> is retained only as the record of the approach we chose against.

How nix produces the runtime files the MCP's `run_script` path
consumes.

This area is **not** affected by the drv-pointer redesign in
`12_nix_drv_reference.md`: the prelude + venv are read locally by
the MCP from the nix store at script time; they never travel as
entity payloads.

## Two sandbox paths, only one in scope

The codebase has two distinct sandbox-spawn paths. **Only the
direct path** is in scope here. The bundle path (one sandbox
spawning another via the spawner) is handled by Areas 05 + 12.

- **Direct path** — `fxtr_sandboxing.direct_launcher.
  create_direct_sandbox_environment` + `env.open_sandbox(volumes=,
  sys_path_additions=)`. The path the MCP uses for every
  `run_script`. This area redesigns it.
- **Bundle path** — see `12_nix_drv_reference.md` and
  `05_sandbox_bundle.md`.

## Single combined derivation

The flake exposes one attr — `fxtr.workstation.sandbox` — which is
a single derivation. Its output is a directory:

```
out/
  prelude.py   # always present (may be empty)
  venv/        # always present (may be empty)
```

Both `prelude.py` and `venv/` are **required to exist** inside the
derivation output. Empty is fine; missing is a hard error. This
trades a tiny bit of flake-author ergonomics for a much simpler
MCP — no existence checks, no branching on "do I have a prelude
this time?"

The agent's flake assembles `sandbox` via a helper
(`lib.fxtr.mkSandbox`) that takes prelude source + venv-shape
inputs and produces the combined derivation. The venv input
itself is typically produced by `lib.fxtr.mkVenv`, which wraps
uv2nix output into the flat site-packages directory `mkSandbox`
expects.

## `fxtr.workstation.sandbox` is effectively required

`run_script` cannot proceed without the venv (it carries fxtr2 +
sandbox-side adapter runtime — see below). The MCP fails fast at
the top of `run_script` if `fxtr.workstation.sandbox` is missing
from the flake's structure-eval.

Practically: the `lib.fxtr.mkSandbox` helper makes the "empty
prelude + minimal-fxtr2 venv" case a one-liner, so omitting the
attr is never the easier path.

## fxtr2 lives in the venv

Previously: the MCP's eryx factory walked the `sandbox_runtime`
package's dep closure to provide fxtr2 + adapter machinery to the
sandbox. Now: fxtr2 (in its `fxtr2-portable` form) and the
sandbox-RPC adapter live in the venv that nix produces; the MCP's
factory becomes a bare eryx Sandbox constructor (no preloaded
packages).

Consequences:

- **`sandbox_runtime`** (the bootstrap stub package in
  `fxtr_workstation_agent_plugin/sandbox/sandbox_runtime/`) goes
  away.
- **`SandboxEnvSupervisor` + `sandbox_factory.build_runner_volumes`**
  collapse substantially: no per-script walk of `<workdir>/
  packages/`, no per-package sys.path additions, no per-package
  warning collection. The whole per-script venv is one bind-mount
  of `out/venv/` plus one entry in `sys_path_additions` pointing
  into it.
- **The MCP still injects `_fxtr_mcp_builtin`** at runtime — its
  adapter machinery is host-side, not packaged. This forces
  agreement between the MCP's bundled fxtr2 version and the
  agent's venv-provided fxtr2 version. Mismatches at the
  serialization layer will produce confusing errors. We accept
  this coupling for v0; if it bites in practice we may surface
  a version check at the top of `run_script` and refuse
  mismatches with a clear error (Q2.D).
- **Warm-spawn is preserved at a simpler form**: the bare eryx
  factory built once per lifespan is reused across `run_script`
  calls; the venv path is whatever the current flake build
  resolves to (different store path = different bind-mount; the
  factory itself doesn't care). A future optimization could
  pre-import a small set of frequently-used packages at
  factory-build time and recreate the factory only when that set
  changes, but the savings aren't obviously large and v0 doesn't
  need it.

## venv layout (single flat /venv mount)

The venv output is a flat directory of importable packages — one
top-level subdir per import name (matches `python.withPackages` /
uv2nix output shape). The MCP bind-mounts it as a single path and
prepends that one path to `sys_path_additions`. This replaces the
per-package `/local-pkgs/<name>/src` bind-mounts today's
`build_runner_volumes` produces.

Resource files (non-`.py` data files inside packages) ride along
because they're just files in the nix store path; the previous
`build_runner_volumes` already preserved them and this approach
continues to.

## prelude handling

`prelude.py` from the derivation output replaces today's
`<workdir>/preamble.py`. The MCP reads it from the store path and
feeds it to `script_runner.run_preamble_and_script` unchanged.

The prelude can be a single hand-authored file copied verbatim by
nix, or assembled by nix from multiple sources (concatenation,
codegen, etc.). The on-output contract is "exactly one file named
`prelude.py`"; how that file gets there is the flake author's
business.

## Build trigger

Builds happen at the top of `run_script` (see Area 08). The MCP
runs `nix build .#fxtr.workstation.sandbox` and reads the resolved
store path. Build failure aborts `run_script` before the script
body runs, with the nix stderr surfaced to the agent (see Area 11).

## Open questions

- Q2.A (resolved): Single-file prelude convention; multi-file
  concatenation is the flake author's job inside the helper.
- Q2.B: Bind-mount lifecycle across nix rebuilds — when the flake
  rebuilds and the store path changes, the per-script bind-mount
  picks up the new path automatically (resolved per-script). What
  stays open: do we recreate the eryx factory on rebuild, or reuse
  it? Reusing is fine *if* the factory is bare CPython only (which
  is the v0 plan); the venv bind-mount carries fxtr2 fresh each
  script. Likely no special handling needed — confirm during
  implementation.
- Q2.C (resolved): Resource files ride along through the nix store
  path naturally.
- Q2.D (new): MCP/venv fxtr2 version-mismatch handling. **Decision
  (from open-concerns #1):** accept the implicit coupling for v0 —
  the agent is responsible for pinning their flake's fxtr-input to
  a rev compatible with the MCP they're running against; we make no
  attempt to auto-align versions. Eventually, add a runtime version
  check that detects skew and fails with a clear error. Note this
  presumes a queryable fxtr2 version exposed over the adapter
  protocol, which doesn't exist yet — adding that hook is a
  prerequisite for the check and is tracked as a followup.
