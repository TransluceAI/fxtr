# Stage 9 post-1 — unified runtime config + one-stop setup wizard

Follow-on to `stage9_nix_runner.md`. Stage 9 shipped both nix backends
(host, podman) and proved the podman path host-nix-free, but left the
**backend-selection UX** as an open judgment call (its chunk 8). This
post-stage resolves it:

- **No implicit default backend (or runtime).** Instead of silently
  defaulting to `host`/`podman` (and re-litigating the security tradeoff on
  every machine), fxtr **requires an explicit choice**. But the choice is
  enforced *at point of use*, not at load: `load_config` always succeeds and
  reports what's set vs unset; a consumer that needs `backend`/`runtime`
  calls a `require_*` accessor that raises a `ConfigError` **pointing at the
  setup wizard** when it's unset.
- **One config controls both the nix backend and the container runtime**
  (they're related — both are "how fxtr runs nix / containers on this
  machine"), written once and reused, overridable by env (so tests/CI keep
  working).
- **A one-stop setup wizard** detects what the machine has, explains the
  security/convenience tradeoff, nudges the user through any missing setup,
  and writes the config. The choice becomes explicit + informed, made once.

Why no default (recap of the chunk-8 reasoning): the security postures are
*close enough* that the decision mostly comes down to **what the user has
installed / wants to run**, not a globally-correct answer. fxtr's builds are
`x86_64-linux`; the host backend therefore needs a **configured linux
builder** (a remote builder — virby or otherwise; not everyone has one),
while the podman backend is self-contained (`podman machine init`) at the
spike's moderate sandbox rung. Most users won't have a linux builder, so
**podman is the recommended pick** — but making the user confirm it once
(with the tradeoff stated) beats guessing, and gives us a place to state the
security model.

## The security / convenience tradeoff (what the wizard states)

- **host nix** — convenient *if you already run nix*; sandbox strength is
  whatever *your* nix is configured for (`sandbox = true`? builder
  isolation?). Needs a **linux builder** for fxtr's `x86_64-linux`
  derivations (macOS can't build them locally) — extra setup, not always
  virby.
- **podman** — no host nix/skopeo, no linux-builder setup. Builds at the
  moderate rung (first-line virby-parity: per-build network denial + RO
  store; second-line = container boundary, with the single-kernel
  co-tenancy caveat on the macOS VM). Same on Linux and macOS.
- **Net:** comparable first line; pick by what you have + the second-line
  posture you want. (On native Linux, podman *adds* a container boundary
  over bare host nix at a likely-lower privilege rung — see the stage-9
  security posture's "native-Linux rung" note.)

## The unified config

### Filename + ownership

Rename the file `~/.config/fxtr/nix_runner.toml` → **`~/.config/fxtr/nix_runtime.toml`**
(XDG): it now spans both the nix backend *and* the container runtime, so the
old `_runner` name under-describes it. The `nix_` prefix stays deliberate —
this is the **runtime for nix builders and nix-built images** (even the podman
container backend runs nix and runs nix-built images), so the file is
nix-domain end-to-end. **`fxtr_nix_runner` keeps owning the loader** —
it's the lean shared dep both `fxtr_nix_runner` consumers and `fxtr_sandboxing`
already use. No back-compat read of the old name (this is pre-release; the
only writers are us + the wizard).

**Env vars are renamed to align 1:1 with the config fields** (below) and are
read in **exactly one place** — `load_config`. No consumer (`get_container_runtime`,
`resolve_backend`) touches `os.environ` directly anymore; centralizing the
parse is half the point of this stage. This churns the stage-8/9 sweeps + the
masked-nix E2E recipe + CI, which is acceptable for the alignment.

Sketch:

```toml
nix_backend            = "podman"   # "host" | "podman"                    — REQUIRED, no default
container_runtime_cmd  = "podman"   # the executable to invoke             — REQUIRED, no default
container_runtime_type = "auto"     # "auto" | "podman" | "docker_compatible" — OPTIONAL, default "auto"
[nix_backend_podman]
# ...existing podman *builder* knobs (image, sandbox, multi_user,
#    materialize_root, mountable_paths, filter_syscalls) — secure code defaults.
#    Namespaced to the nix backend; distinct from the container runtime above.
```

- **No coupling validation at parse.** We do *not* reject `nix_backend = "podman"`
  with a `docker`-family runtime in `load_config`. The only real
  incompatibility — the podman nix backend delivering an image into a
  non-podman runtime — already raises at runtime in `deliver_image` (stage 9),
  a sufficient and more honest place (it names the operation that can't be
  bridged). The loader stays a dumb parser.

### Config object shape (the "no implicit default" mechanism)

The de-anchored design: **load is total; *use* is where a required-but-unset
field fails.**

```
NixRuntimeConfig:                                       # renamed from NixRunnerConfig
    nix_backend:            Literal["host","podman"] | None   # None = not configured
    container_runtime_cmd:  str | None                        # None = not configured
    container_runtime_type: Literal["auto","podman","docker_compatible"] # default "auto"
    podman:                 PodmanConfig                       # always populated (secure defaults)
    source:                 ConfigSource                       # provenance, for messages only

    require_nix_backend()       -> Literal["host","podman"]    # value, or ConfigError naming the fix
    require_container_runtime() -> ContainerRuntime            # resolved (cmd + concrete type), or ConfigError
```

- **Required choice fields with no safe default** — `nix_backend`,
  `container_runtime_cmd`: `… | None`, `None` = "not configured."
- **`container_runtime_type` defaults to `"auto"`** (it's the one runtime field
  you can usually omit; `auto` infers it — see the runtime model).
- **Tuning fields with safe defaults** (the `[nix_backend_podman]` builder
  knobs) are always populated — unchanged from today.
- `ConfigSource` records *what was consulted* (the file path; whether it
  existed; which env vars were in play) purely so `require_*` can emit a
  specific message, e.g.:
  ```
  nix_backend not configured. Looked in ~/.config/fxtr/nix_runtime.toml (no such file).
  Run 'fxtr_nix runtime setup', or set FXTR_NIX_BACKEND=host|podman.
  ```
- Consumers **never read the raw fields directly** — they call `require_*`.
  This puts the "how to fix" message in one place and lets the **wizard
  introspect** (`load_config()` and look at what's set/unset) without catching
  exceptions.

### The container-runtime model (`cmd` + `type`)

Today the runtime is a bare string doing two jobs: the **executable** in every
`[runtime, "run"/"machine"/"image"/…]` argv, *and* a **behavior selector** via
the scattered `_delivery.is_podman(runtime)` basename check (gating tarless
socket delivery, the socket-file mounts, the `podman_is_remote` tunnel, and the
`localhost/` ref prefix). Split those two jobs explicitly — and let the
behavior `type` default to `auto`:

```
ContainerRuntime:                                 # resolved value object, owned by fxtr_nix_runner
    cmd:  str                                     # executable name or path — used in every argv
    type: Literal["podman","docker_compatible"]   # concrete behavior family ("auto" already resolved)
```

- **`cmd` is required and open** — any executable/path (`"podman"`,
  `"container"`, `"docker"`, `/opt/.../podman-remote`). The escape hatch is
  data, not a special case; no `Literal` on `cmd`, no cast.
- **`type` is the closed behavior family** — the genuine fork: `"podman"`
  (tarless socket delivery + socket mounts + `localhost/` ref) vs
  `"docker_compatible"` (the docker-CLI subset + tar `image load`; covers
  Docker, Apple Container, and most others). `is_podman` goes away —
  downstream reads `runtime.type == "podman"`.
- **`auto` inference** (config `container_runtime_type = "auto"`, the default):
  resolve the family from `basename(cmd)` — `podman → podman`; **everything
  else → `docker_compatible`** (nearly every runtime speaks the docker CLI, so
  that's the safe default for "not podman" — `container`, `docker`, … all
  infer cleanly). The door stays open: if we ever recognize a *known-
  incompatible* runtime, `auto` errors for it and demands an explicit `type` —
  but there are none in v0, so `auto` always resolves. An explicit `type`
  skips inference (a podman wrapper not named `podman` is `cmd =
  "/opt/.../mypodman"`, `type = "podman"`).
- `deliver_image(runtime: ContainerRuntime)`; the podman-nix-backend gate
  becomes `if runtime.type != "podman": raise`.
- `fxtr_sandboxing.get_container_runtime()` (renamed from `get_runtime`) =
  `load_config().require_container_runtime()` — the resolution (require `cmd`,
  resolve `auto`) lives in `require_container_runtime`.

### Resolution precedence (per field, no implicit default)

Env vars map **1:1 to fields**; `load_config` is the only reader:

| field | env override |
|---|---|
| `nix_backend` | `FXTR_NIX_BACKEND` |
| `container_runtime_cmd` | `FXTR_CONTAINER_RUNTIME_CMD` |
| `container_runtime_type` | `FXTR_CONTAINER_RUNTIME_TYPE` |
| *(full-file override)* | `FXTR_NIX_RUNTIME_CONFIG` (renamed from `FXTR_NIX_RUNNER_CONFIG`) |

`load_config()`:

1. **Full-file override** — `FXTR_NIX_RUNTIME_CONFIG` (a complete config file path)
   *is* the config: it supersedes the default-location file **and** every
   per-field env var. (The hermetic test/CI override; the only way to set
   `[nix_backend_podman] mountable_paths` for a run.)
2. Otherwise, the **default-location file** (`nix_runtime.toml` if present)
   sets all fields + `[nix_backend_podman]`, then the **per-field env
   overrides** win for their field (`FXTR_NIX_BACKEND` → `nix_backend`, etc.).
   Env wins per field; the file wins for the `[nix_backend_podman]` builder
   knobs.
3. A field left unset stays `None` (or `"auto"` for the type). `load_config`
   **succeeds**; `require_*` is what raises `ConfigError` — and only for the
   field a given consumer actually needs.

Consequence to handle: bare `pytest` / CLI on an unconfigured machine errors
*when it first needs a backend/runtime* (clear message → wizard), instead of
silently assuming `host`/`podman`. Tests are unaffected — the sweep fixtures
set the env per field; the E2E runs pass `FXTR_NIX_RUNTIME_CONFIG`; CI sets the
env. Document that a one-time `fxtr_nix runtime setup` (or the env) is required.

## Phase 1 — fold the runtime into the config (do this first; **separate commit**)

**Status: implemented 2026-06-09** (steps 1–10). Pyright-clean across
`fxtr_nix_runner` / `fxtr_sandboxing` / `fxtr_workstation` /
`fxtr_workstation_mcp`; `fxtr_nix_runner` unit suite green; lazy resolution
smoke-tested with zero config (constructing an env / doing non-nix work needs
no backend config).

1. **Rename + reshape the config object** in `fxtr_nix_runner._config`:
   `NixRunnerConfig` → `NixRuntimeConfig`; `backend` → `nix_backend:
   Literal["host","podman"] | None`; add `container_runtime_cmd: str | None`
   and `container_runtime_type: Literal["auto","podman","docker_compatible"]`
   (default `"auto"`); add the `ConfigSource` provenance; add
   `require_nix_backend()` and `require_container_runtime()` (the latter
   requires `cmd` + resolves `auto`, returning a `ContainerRuntime`) with
   wizard-pointing `ConfigError` messages. The podman *builder* knobs keep
   their content but move to a `[nix_backend_podman]` table (was `[podman]`) —
   namespacing them to the nix backend, distinct from the container runtime.
2. **Add the `ContainerRuntime` value object + the `auto` inference helper** to
   `fxtr_nix_runner` (the runtime model above): `cmd` + concrete
   `type ∈ {podman, docker_compatible}`; basename inference (`podman→podman`,
   everything else `→ docker_compatible`).
3. **Rewrite `load_config`** for the per-field resolution + renamed env vars
   (above): total load, no implicit default, `FXTR_NIX_RUNTIME_CONFIG` full-file
   override vs default-file + per-field env (`FXTR_NIX_BACKEND`,
   `FXTR_CONTAINER_RUNTIME_CMD`, `FXTR_CONTAINER_RUNTIME_TYPE`). Env read
   **only here**. No coupling check.
4. **Rename the config file** the loader reads/writes →
   `default_config_path()` returns `…/fxtr/nix_runtime.toml`.
5. **`resolve_backend()`** now calls `load_config().require_nix_backend()`
   (was: a defaulted field). Update the `nix_backend` sweep fixture for the
   field rename (it passes the backend explicitly, so it keeps working — just
   the new field name + setting `container_runtime_*` where it builds configs).
6. **Thread `ContainerRuntime` through delivery**: **delete `is_podman`**;
   `deliver_image` signature becomes `runtime: ContainerRuntime`; the
   podman-backend gate becomes `if runtime.type != "podman"`. Replace every
   `_delivery.is_podman(…)` / bare-string use in `_delivery.py` with
   `.cmd` / `.type`.
7. **Rename + rewire `fxtr_sandboxing.get_runtime` → `get_container_runtime`**,
   returning a `ContainerRuntime` via `load_config().require_container_runtime()`.
   `_DEFAULT_RUNTIME = "podman"` and the direct `os.environ` read are
   **removed** — env now resolves in `load_config`. Update callers
   (`spawner.py`, `nix_container_runner.py`) to use `.cmd` / `.type` (grep
   `get_runtime` first to update mechanically).
8. **Update the E2E podman wiring + the renamed env vars everywhere they're
   set**: the temp config toml the podman E2E variant writes now uses
   `nix_backend` / `container_runtime_cmd = "podman"`; the env var that points
   at it is `FXTR_NIX_RUNTIME_CONFIG`. Update the stage-8 `container_runtime` sweep
   + stage-9 `nix_backend` sweep fixtures and the masked-nix recipe to the new
   env var names.
9. Update CODE_OVERVIEW.md (`fxtr_nix_runner`: the file + env-var rename, the
   `NixRuntimeConfig` shape + `require_*`, the `ContainerRuntime` model + `auto`
   inference; `fxtr_sandboxing`: `get_container_runtime` + the `.cmd`/`.type`
   split, no direct env reads).

10. **Lazy backend resolution in the consumers (decided 2026-06-09).** The
    "no implicit default" hard-fail fires at point of use — but the three
    consumers (`Spawner`, `McpHostImpl`, `NixConfigurableDispatchImpl`) used to
    resolve eagerly in `__init__`, so under the new rule *constructing* any of
    them would fail on an unconfigured machine, hitting the whole non-nix E2E
    surface (≈17/31 MCP modules, most spawner tests) — not just nix tests. Fix:
    - **Lazy resolution.** Each consumer resolves the backend on **first nix
      use** via a cached, lock-free `_backend()` accessor
      (`resolve_backend()` is sync), not in `__init__`, and uses a **per-call
      `builder_for`**. A test that constructs a consumer but never does nix work
      never resolves a backend → needs no config. This shrinks the blast radius
      back to genuinely-nix tests (zero per-test changes for the rest), and is
      all this step does — deliberately small.
    - **Deferred: persistent builder handle + backend injection.** An earlier
      cut of this step added an injected `nix_backend` param + an env-lifetime
      persistent builder handle (spawner via the env `exit_stack`; dispatcher
      via an injected `builder_stack`, per-op otherwise) + a `testing.StubBackend`.
      It was **rolled back as too complicated** (three inconsistent shapes across
      the consumers, plus a test-only per-op branch). The persistent-builder
      optimization — opened once by the owning scope, reused across spawns /
      set_presets, no per-op container churn — is worth doing **as its own
      deliberate change** with one consistent mechanism (owning scope passes a
      handle in; per-call otherwise). Not done here.
    - **Test wiring.** Real-nix E2E modules (sandboxing `test_nix_*` /
      `test_nested_spawn_*`, MCP `test_nix_dispatch_preset`) carry
      `pytest.mark.nix_runtime`; an autouse fixture in each package's conftest
      calls `skip_if_runtime_unconfigured()` for marked tests (skip when
      unconfigured; `FXTR_TESTS_REQUIRE_RUNTIME=1` flips skip→fail for CI).
      Mocked-nix tests (`test_nix_configurable_dispatch`) instead monkeypatch
      `resolve_backend` → `HostBackend` (hermetic; no marker — works because the
      lazy accessor goes through the module-level `resolve_backend`).
      `test_nix_util` already constructs `HostBackend` directly, so it's
      untouched.
    - **Note (pre-existing, now fixed in a prior commit):**
      `test_nix_configurable_dispatch` was **red on the base** — its
      `fake_build` was stale vs the stage-9 handle-passing
      `build_drv_outputs(h, drvs)` (signature *and* `Path`→`Materialized` return
      shape). Fixed in the commit *before* this one; the hermetic fixture keeps
      it from spinning up a real builder under a podman-configured env.

## Phase 2 — the one-stop setup wizard (**separate commit**)

**Status: implemented 2026-06-09.** `fxtr_nix_runner._wizard` + the `runtime`
subcommand group on the `fxtr_nix` CLI (`setup`, `setup --detect`,
`init-podman`). `detect()` probes host nix (+ a trivial `x86_64-linux`
test build), podman (CLI/machine/running/Rosetta-marker), and Apple Container;
the neither-podman-nor-Apple-Container nudge surfaces the manual
`container_runtime_cmd` hint (docker untested). Probes pure-mocked in
`test_wizard.py` (18 tests); `--detect` JSON shape is stable; `init-podman`
argv is the testable `_init_podman_steps`. Live `--detect` smoke verified.

A new **`runtime` subcommand group on the existing `fxtr_nix` CLI** (alongside
the repo-level `fxtr_nix lock|clear`, but namespaced under `runtime` to keep
machine-setup separate from per-repo ops), living in `fxtr_nix_runner` (it
already knows podman socket/machine discovery). Subcommands:

- **`fxtr_nix runtime setup`** (interactive): detect → explain → nudge →
  ask → **write `nix_runtime.toml`** (`nix_backend` + `container_runtime_cmd`;
  it leaves `container_runtime_type` at `auto` for podman/apple-container,
  which infer cleanly). *Detect and write
  only* — it does **not** run `podman machine init` or any system setup
  itself; it points you at `fxtr_nix runtime init-podman` when that's the gap.
- **`fxtr_nix runtime setup --detect`** (pure autodetect): print findings as JSON
  and stop. No prompts, no writes — the machine-readable view of what's
  installed + missing + the recommendation. (Manual config-editing is always
  available, so a non-interactive *writer* isn't required; deferred.)
- **`fxtr_nix runtime init-podman`** (the separate automation, Q3): automates
  `podman machine init` for the user. **Rosetta is a choice, not forced** — it
  informs (near-native amd64 builds vs. plain emulation), offers a sensible
  default (Rosetta on, matching stage 8's recommended config), and lets the
  user opt out. Run this *first* if the nudge says podman is missing/uncreated,
  then re-run `setup`. Keeping it a distinct subcommand keeps `setup` itself
  side-effect-free (detect + write a toml).

### Detection

- **host nix:** is `nix` on PATH? **Can it build `x86_64-linux`?** — a tiny
  **test build** of a trivial linux derivation, run **every time** `--detect`
  is invoked (simplicity over caching). Use a minimal `builtins.derivation`
  with `system = "x86_64-linux"` and a `/bin/sh` builder (no nixpkgs / no
  channel), so the only thing it exercises is "is there a working linux
  builder" — it succeeds locally on linux, via a remote builder on macOS, and
  fails when no linux builder is configured. Distinguishes "nix present +
  builds linux" from "nix present but no linux builder."
- **podman:** CLI present? machine created? machine running? **Rosetta
  enabled** on the machine (the recommended amd64-under-Rosetta config — see
  stage 8's `enable-rosetta` marker)?
- **Apple Container:** present? (the runtime **fallback** when there's no
  podman, for the host backend).

### Nudges (detected-state → guidance the wizard prints)

- *nix present but can't build linux* → how to set up a linux builder
  (remote builder / virby), since the host backend needs it.
- *podman present, no machine* → run `fxtr_nix runtime init-podman` (which wraps
  `podman machine init …` the recommended way, with the Rosetta choice).
- *podman machine up but Rosetta off* → how to re-create with Rosetta for
  near-native amd64 builds (or accept slower emulation).
- *no podman* → check Apple Container as the runtime fallback (host backend).
- *neither podman nor Apple Container found* → tell the user they can still set
  `container_runtime_cmd` **manually** in `nix_runtime.toml` to any
  docker-CLI-compatible runtime if they want one we don't auto-detect, and note
  that **plain `docker` may work but is untested** (it infers
  `container_runtime_type = docker_compatible` via `auto`). The wizard *states*
  this escape hatch but does not write a `docker` cmd itself.
- **never** auto-configure `docker` (untested runtime) — it stays a documented,
  user-typed escape hatch only.

### Recommendation logic (sketch)

```
if podman machine usable:        nix_backend=podman, container_runtime_cmd=podman
elif host nix builds linux:      nix_backend=host    (cmd: podman if usable, else container)
else:                            no usable setup → print the nudges, recommend installing podman
```

Always print the tradeoff blurb so the recommendation is an *informed* pick,
not a black box. Whatever the user picks, `nix_backend` + `container_runtime_cmd`
are written explicitly to `nix_runtime.toml` (type stays `auto`).

## Testing

- Config: per-field env-override precedence, full-file override
  (`FXTR_NIX_RUNTIME_CONFIG`), the `require_*` `ConfigError` on an unset field
  (message naming the file + the wizard), runtime-from-config +
  runtime-from-env, the file rename. No coupling-validation tests (there is no
  coupling validation).
- Runtime model: `require_container_runtime` / the `auto` inference —
  `cmd=podman` (incl. abspath `/…/podman`) → `type=podman`;
  `cmd=container`/`cmd=docker`/other → `type=docker_compatible`; explicit
  `type` overrides inference (e.g. `cmd=mypodman` + `type=podman`); missing
  `cmd` → `ConfigError`; `.cmd` round-trips the input.
- Wizard: detection logic unit-tested with the probes (nix test-build,
  `podman info`, `container`) **mocked** — assert the JSON shape + the
  recommendation + which nudges fire for representative machine states.
  Don't shell out to real podman/nix in unit tests. `init-podman` is the only
  thing that touches the real system; keep it out of the unit tests (its body
  is a thin wrapper over `podman machine init` — test argv construction +
  the Rosetta-choice branching with the subprocess mocked).
- `--detect` JSON: stable, documented shape.
- **Test-skip on an unconfigured machine (decided).** Production `require_*`
  hard-fails (point-of-use), but the *downstream* test suites that need a
  backend/runtime **skip** (not fail) when nothing is configured at all — via a
  shared `skip_if_runtime_unconfigured()` helper that calls `load_config()` and
  skips with an actionable reason (`set FXTR_NIX_BACKEND /
  FXTR_CONTAINER_RUNTIME_CMD, or run 'fxtr_nix runtime setup'`). This is the
  limit case of the existing sweep's skip-when-*unavailable* (no host nix / no
  podman machine), so it's consistent, not a special exception; the sweep
  fixtures still set env explicitly and are unaffected. The one accepted hazard
  — a CI lane that *means* to run these but forgets to set the env would skip →
  green silently (same hazard the existing skip-when-unavailable already has) —
  is closed by an opt-in **`FXTR_TESTS_REQUIRE_RUNTIME=1`** that flips
  skip→fail; CI sets it so a misconfigured CI fails loudly while local cold
  `pytest` stays friendly.

## Open questions (resolved)

- ~~Final command name + home.~~ **A `runtime` subcommand group on the existing
  `fxtr_nix` CLI** — `fxtr_nix runtime setup` / `setup --detect` /
  `init-podman` — namespaced under `runtime` to separate machine-setup from the
  repo-level `fxtr_nix lock|clear` (which stay top-level). No second console
  script.
- ~~Config filename + env var names.~~ **Renamed to `nix_runtime.toml`**
  (spans backend + runtime); **env vars renamed to align 1:1 with fields**
  (`FXTR_NIX_BACKEND`, `FXTR_CONTAINER_RUNTIME_CMD`,
  `FXTR_CONTAINER_RUNTIME_TYPE`, `FXTR_NIX_RUNTIME_CONFIG`), parsed only in
  `load_config`; no back-compat read of the old name/vars.
- ~~Whether `--detect` JSON should drive a non-interactive *writer*.~~
  Deferred until a real need (env overrides cover CI today).

## Update CODE_OVERVIEW.md as the final step (of each phase that touches structure).
