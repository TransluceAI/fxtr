# Part 5 follow-ups (post-v0)

Things explicitly *not* part of the v0 vertical slice but
referenced or deferred during the design. Each item points at
the design note(s) where the decision lives. We'll sequence
these in whatever order seems most useful once Stages 1-6 land;
there's no implied dependency ordering between items here.

## Remote entity store (Area 07)

Self-contained-only is the v0 stance. Bringing the
MCP-side localhost rehost + the in-memory `DeepFetchable` for
sandbox-side reads online is its own follow-up sequence:

- Build-time HTTP rehost wired through impureEnvVars
  (`FXTR_REMOTE_BASE_URL`, `FXTR_REMOTE_BEARER_TOKEN`).
- In-memory `DeepFetchable` for sandbox reads from the
  workstation's remote-store service.
- Eventual real hosted remote store (out of scope; flagged in
  the area).

Reference: `design_notes/07_remote_entity_store.md`.

## Docker service (`mkDockerService`) — ✅ LANDED as Stage 7 (2026-06-04)

Landed as **`mkContainerService`** (renamed Container/OCI, rebased from
`dockerTools` onto nix2container so no image tarball is materialized in
the store): `stage7_nix_container.md`. Same git-ref spawn shape
(`NixContainerServiceDefn`), `kind` discriminator on all
`fxtr.services.<name>` attrs, skopeo `nix:` delivery into the container
runtime. The deprecated `DockerTaskBundle` / `PyContainerTaskBundle`
arms are still present (their removal is a separate cutover, below).

## Remove the deprecated in-band-bytes container paths — LANDED (stage 8)

Landed in `stage8_podman.md` chunk 2: `DockerTaskBundle` /
`PyContainerTaskBundle` arms, runners, prepare pipelines, entities,
the registry/buildkit example infra, and their tests are deleted; the
nested-spawn container-inner test was ported to a
`NixContainerServiceDefn` inner. The wasm `TaskBundle` path is
untouched.

## Podman runtime standardization — LANDED (stage 8)

Landed in `stage8_podman.md`: podman is the recommended default
runtime (`FXTR_CONTAINER_RUNTIME` selects an alternative — Apple
Container is a supported non-default backend on the tar-fallback
delivery, swept by the container tests since
`stage8post1_runtime_sweep_tests.md`; docker is a compatibility
escape hatch), delivery is tarless (skopeo → podman's API
socket), and remote-podman socket plumbing rides a per-spawn
sshd-sidecar pod (inetd-mode ssh transport over `podman exec`, nothing
listening anywhere). The bigger version — a nix-builder container
inside podman owning building *and* delivery, so the host needs
neither nix nor skopeo — is `stage9_nix_runner.md`.

Host-env caveat RESOLVED (2026-06-06): the macOS 26 upgrade re-opened
Rosetta. The dev machine's podman machine was recreated as applehv with
Rosetta activated (the machine-os image still gates activation behind a
`/etc/containers/enable-rosetta` marker — see the stage-8 addendum and
the CODE_OVERVIEW Development section for the one-time step); real
amd64 binaries run at near-native speed, full container E2E sweep
green on both runtimes. qemu-user remains the documented fallback for
hosts still on macOS < 26.

## Collapse the alternative runtimes + tar fallback (conditional)

Posture since `stage8post1_runtime_sweep_tests.md`: Apple Container
is a supported non-default backend, kept honest by the container-test
runtime sweep — not a planned removal. Collapsing
`FXTR_CONTAINER_RUNTIME` + the oci-archive tar fallback in
`_load_image_into_runtime` to podman-only is a *deliberate* future
decision, not the default post-stage-9 step; the signal to take it is
the sweep's maintenance cost no longer earning its keep (what we
don't want is the middle state: a backend that's nominally supported
but untested). `stage9_nix_runner.md` making podman load-bearing for
nix itself is a prerequisite, not the trigger.

## skopeo / nix2container pin drift (stage 7)

The repo flake pins `nix2container` as an input (backs
`lib.fxtr.mkContainerService`); `fxtr_sandboxing/nix.py` separately
pins a rev constant for the host-side `skopeo-nix2container` build.
The two can drift — harmless while the image-JSON format is stable,
but worth collapsing (e.g. derive the skopeo ref from the workdir
flake's locked input) if it ever bites.

## Remote deployment (Area 12)

v0 runs locally (shared nix store → builds are cache hits). Moving the
spawner/dispatcher to a separate machine needs only: git reachability
(`git+ssh`/`git+https`) for the configured repo, and a build
environment on the consumer (build deps or a binary cache). The trust
model does **not** change — it's connection-level (the workstation
trusts authorized clients, like a nix remote builder; **no repo/ref
allowlist**). Hardening is about *who can connect*, not *what they may
build*. Multiple agents already work via a shared bare repo (no extra
machinery).

Reference: `design_notes/12_nix_drv_reference.md` "Trust model &
topology".

## Service-identity `.drv`-hash route (Area 12)

Spawning uses the rich `(commit, attr)` ref; *identity* (equality /
dedup of services) is the resolved `.drv` hash, derived lazily from the
git ref (`nix eval "<git-ref>#…sitePackages.drvPath"`). No v0 route
surfaces it. Add when an equality/dedup question actually arises.

Reference: `design_notes/12_nix_drv_reference.md` "Service identity vs.
spawn reference".

## CI "build everything" rot-check (replaces `mkAll`)

`mkAll` / the `fxtr.workstation.all` linkFarm was dropped for v0 (its
pre-warm rationale evaporated once consumers build lazily from git
refs; a `path:` linkFarm doesn't reliably warm the git-ref builds
anyway — see `design_notes/03_entity_declarations.md`). The one real
residual need — "does every declared entity / service / preset-renderer
still build?" — is a **CI** concern. When wanted, add a CI target (or
`nix flake check` integration) that enumerates and builds the declared
objects, rather than re-introducing an agent-facing flake helper.

Reference: `design_notes/03_entity_declarations.md`,
`design_notes/10_nix_infrastructure.md`.

## Thin MCP commit→spawn helper (Q5.F)

V0's commit→spawn loop is fully manual (shell `git`, hand-written
`NixWasmServiceDefn`). A later helper could capture HEAD / construct the
defn / require a clean tree. Not v0.

Reference: `design_notes/05_sandbox_bundle.md` Q5.F, open-concerns #14.

## `get_full_exception` migration to file-based (Q11.C)

The `.tmp_outputs/` mechanism introduced in Stage 2 for nix
build errors could subsume the in-memory `ExceptionLog` LRU that
backs `get_full_exception` today. Small simplification refactor
once the v0 nix path lands.

Reference: `design_notes/11_failure_and_ux.md` Q11.C.

## Operator-driven preset selection (Q6.A)

In v0 the dispatcher auto-loads a `default` preset on startup
and the agent calls `set_preset(name)` to switch. The remaining
piece — out of scope for v0 — is exposing the *list* of
available uiPresets in the flake to a human operator (e.g. via
the workstation frontend) so they can switch presets without
involving the agent. Implementation sketch: walk
`fxtr.workstation.uiPresets` at workstation-server startup (and
maybe on a refresh trigger), surface the names through a
workstation service the frontend can list against, expose a
"switch to preset X" affordance in the UI.

Reference: `design_notes/06_ui_presets.md` Q6.A.

## CBOR-closure-scanner verification (Q12.D) — ❌ OBSOLETE (2026-05-30)

The mechanic it validated (a `.drv` embedded in a pointer entity, kept
reachable via closure scanning + sidecar symlink) no longer exists —
code travels via git, not as a `.drv`-in-an-entity. The synthetic
check landed in Stage 1 is now orphaned code; it's **removed in
Stage 3** along with the rest of the `.drv`-pointer apparatus. Kept
here only as a pointer to the history.

Reference: `stage1_nix_foundations.md` (orphaned),
`stage3_bundle_and_spawner.md` (removal).

## `.tmp_outputs/` cleanup policy (Q11.B)

V0 doesn't auto-clean nix error dumps. If they bloat in
practice, decide on an LRU / TTL / size-cap mechanism.

Reference: `design_notes/11_failure_and_ux.md` Q11.B.

## Setup-header polish + entity-build progress (Q11 follow-ups)

Possible extensions to the `--- nix ---` setup header: include
mid-script `retrieve_nix_entity` builds in a trailing
`--- nix entity builds ---` block. Maybe expose long-build
progress via the agent-MCP service (so a human watching the
frontend sees "agent stuck in nix build" status).

Reference: `design_notes/11_failure_and_ux.md`.

## MCP / venv fxtr2 version-mismatch handling (Q2.D)

V0 accepts the implicit coupling between MCP-bundled fxtr2 and
agent-venv-supplied fxtr2. The agent is responsible for pinning
their flake's fxtr-input to a rev compatible with the MCP; we do
not auto-align. If serialization-layer mismatches bite in
practice, add a runtime version check that detects skew and fails
fast with a clear error. **Prerequisite:** this needs a queryable
fxtr2 version exposed over the adapter protocol (none exists
today) — adding that hook is part of this followup, not a
pre-existing affordance.

Reference: `design_notes/02_prelude_and_venv.md` Q2.D,
open-concerns #1.

## Shared nix wrapper library — **REVERSED → simplified in place** (2026-06-01)

This was originally "do first after Stage 5": extract the three parallel
nix-CLI wrappers (`fxtr_workstation_mcp/nix_util.py`, `fxtr_sandboxing/nix.py`,
`nix_configurable_dispatch/nix.py`) into a shared `fxtr_nix_util` lib.

**On investigation we decided *not* to.** The duplication turned out small,
mechanical, and split across three different domains (DATA/non-frozen vs
CODE/frozen; MCP vs sandbox vs dispatcher); a shared lib needed a knob to
absorb the frozen/non-frozen variation (a design smell) and would couple three
independent packages to save ~50 lines — premature. Crucially, the whole
typed-error apparatus (`Nix{Service,Preset}{Ref,Eval,Build}Error`, the
`.ref`/`.context`/`.returncode` attrs, the eval-vs-build split) was used by
**nobody but the wrapper tests** — the spawner/dispatcher don't catch their nix
errors (they propagate as `invoke_error` with stderr inline), and only the MCP
catches its base `NixError`.

So instead we **deleted the dead surface and simplified in place**
(`stage5post2_nix_wrapper_simplify.md`, landed):

- `build_flake_ref` → local-only + private (`_build_flake_ref`); the `"://"`
  sniff + remote (`git+ssh`/`git+https`) passthrough was dead code. A remote
  constructor gets written when remote deployment actually lands and its shape
  is real (this is why we *didn't* build a `GitRepoRef` union now — no live
  caller to define it).
- Errors: sandboxing + dispatcher raise plain `RuntimeError` (nothing catches
  them); the MCP keeps one `NixError` (+ `NoFlakeNixError`) since `host_impl`
  genuinely catches it. The eval-vs-build distinction is gone everywhere.
- The two repo-root-coupled wrapper tests (`test_nix_preset_ref.py`,
  `test_nix_git_ref.py`) were **deleted** outright — they tested the nix CLI +
  thin wrapping, and the real git-ref path is covered end-to-end by the spawner
  integration tests + `test_nix_dispatch_preset.py`. This also dissolved the
  "dedicated fixture flake / `prepare_isolated_flake_repo`" sub-task — not
  needed once the low-value tests are gone. (The MCP's `test_nix_util.py` stays
  — it uses its own tmp fixtures and has real good-path coverage; only its
  error references collapsed to `NixError`.)

If a 4th nix consumer appears with the *same* shape, revisit extraction then.

Reference: `stage5post2_nix_wrapper_simplify.md`; `fxtr_sandboxing/nix.py`,
`nix_configurable_dispatch/nix.py`, `fxtr_workstation_mcp/nix_util.py`.

## Pure-python validation in `_mkGuestSitePackages` (Stage 2 Axis 4)

**Partial solution landed in Chunk 2.** The sandbox-smoke workspace
declares `[tool.uv] environments = ["sys_platform == 'wasi' and
platform_machine == 'wasm32'"]` + the matching
`required-environments`. Combined with the `environ` override
`_mkGuestSitePackages.nix` passes to uv2nix's `mkPyprojectOverlay`,
uv's resolver can only pick `py3-none-any` wheels for deps like
cbor2. The agent's workdir pyproject (Chunk 4) will need to mirror
this dual-knob setup; the chunk-4 work should include factoring
that boilerplate into a copy-paste-ready template.

What's still missing is a **defensive build-time check** that fails
the derivation if any non-pure wheel somehow slips through (e.g. an
agent that forgets to copy the `[tool.uv] environments` block).
That check would read wheel filenames out of uv2nix's package set
and assert all selected wheels match `py3-none-any` (or `abi3` if
we want to be slightly more permissive). Optional first pass: log
a warning instead of failing.

Reference: `stage2_sandbox_runtime_scratch.md` Axis 4,
`stage2_sandbox_runtime.md` Chunk 2.

## Host-runnable agent / sandbox pyprojects (Stage 2 wheel-selection tradeoff)

V0's `_mkGuestSitePackages` keeps `.so` files out of the guest venv by
setting **both** `[tool.uv] environments` (restrict-set) and
`required-environments` on the agent's workspace, plus a wasi `environ`
override on the nix side. The trade-off: `environments` *restricts* the
lock to wasi/wasm32 markers, so the same pyproject refuses `uv sync` on
a non-wasi host. For sandbox-only workdirs that's fine — but it blocks
useful host-side workflows:

- IDE / language-server setup against the agent's deps
- Local test runs that share the agent's dep set
- Future "publish the agent workdir as a package" flows

**Why we couldn't drop the restriction**: uv2nix delegates wheel
selection to `pyproject-nix`'s `pypa.selectWheels`, which sorts compatible
wheels by `bestLanguageTag` (cp312 → 312 beats py3 → 3). The `environ`
override only changes PEP 508 marker *evaluation*; it doesn't redirect
the tag-priority sort. Restricting the lock is the only knob within
uv2nix's framework that filters cp312 wheels out entirely.

**Options to explore when this matters:**

- **Option 2 — Manual lock-walk + extract**: bypass uv2nix for the
  guest case. `builtins.fromTOML` the lock, pick `py3-none-any` wheels
  directly via `pkgs.fetchurl`, fetch git sources via
  `builtins.fetchGit`, build any hatchling/uv_build sources via a thin
  derivation, unzip everything into `$out/`. Lock can be unrestricted
  (multi-platform), so host `uv sync` works. ~150–200 lines of
  bespoke nix; the source-build step is the chunky bit (need to handle
  build deps for hatchling/uv_build inside our own derivation).
- **Option 3 — Use uv2nix's lock parser, replace wheel-selection
  ourselves**: call `uv2nix.lib.lock1.parseLock` to get a structured
  view, pick `py3-none-any` from each package's `wheels` list
  directly, keep uv2nix's source-building machinery for git/path
  packages. ~80–120 lines, less duplication of uv2nix internals, but
  taps into `lock1.*` which is internal-ish and could shift across
  uv2nix versions.

Lean: when the time comes, try Option 3 first. It preserves the part
of uv2nix we'd hate to reimplement (source builds with build-dep
handling), and replaces only the load-bearing piece — the wheel-tag
priority. Option 2 is a fallback if uv2nix's lock parser turns out to
be too brittle to depend on.

When this lands, the agent workdir pyproject can drop `[tool.uv]
environments` and keep only `required-environments` (so the lock
includes both host and wasi-compatible wheels). The guest-side helper
picks py3-none-any unconditionally; host `uv sync` picks the host
wheels.

Reference: `stage2_sandbox_runtime_scratch.md` "Post-Chunk-2 finding"
section, `stage2_sandbox_runtime.md` Chunk 2 notes.

## Internal `_mkGuestSitePackages` → public `lib.fxtr.*` if needed

V0 keeps the "build a flat site-packages dir from a uv workspace"
helper internal (not in `lib.fxtr.*`). Only `mkSandbox` (Stage 2)
and `mkWasmPackageBundle` (Stage 3) call it. If a hand-rolled
sandbox-or-bundle layer use case (per `05_sandbox_bundle.md` Q5.E)
materializes, or if a third in-repo consumer appears, promote it.

The Axis-1 TODO (move off "build full mkVenv internally + extract"
to a leaner uv2nix wiring that skips `mkVirtualEnv`) lives here
too — if we end up rewriting the helper, that's a natural moment to
reconsider the python-version pinning question (Axis 3) too.

Reference: `stage2_sandbox_runtime_scratch.md` Axes 1 + 3 + 5.

## Service-protocol inheritance in the resolver registry (Stage 5 finding)

> **Resolved (2026-06, fxtr2 stage 13 part 0).** `@service_protocol`
> now supports genuine subclassing via
> `allow_protocol_subtypes=True` on the base (registers the base's
> resolver with `allow_implicit_overrides=True`; inherited methods
> get TypeVar substitution). `WorkstationDispatcherProto` is marked,
> and `NixDispatcherProto` is a real subclass that only adds
> `set_preset`. (`DemoDispatcherProto` no longer exists.) See
> `v2/plans/phase3/stage13_output_channels/part0_protocol_subclassing.md`.

Stage 5 wanted `NixDispatcherProto(WorkstationDispatcherProto)` (extend the
base proto with `set_preset`), as the design notes sketched. But
`@service_protocol` registers a `ServiceResolver` per Protocol type, and the
resolver registry **rejects registering a resolver for a subtype** of an
already-registered Protocol — `find_handler` raises *"Conflicting handlers …
would override … but … does not have allow_implicit_overrides=True"*. So
`NixDispatcherProto` had to be a **standalone** Protocol that re-declares
`resolve` / `subscribe_to_changes` (wire-identical) + adds `set_preset`,
matching the existing `demo_dispatch.DemoDispatcherProto` workaround. Wire
dispatch is by method name so it works, but the method re-declaration is
duplication we'd like to avoid.

Worth revisiting after Stage 5: should `@service_protocol` support protocol
inheritance cleanly? Options to weigh — register the base with
`allow_implicit_overrides=True`; have `@service_protocol` skip re-registering
methods already covered by a registered base; or a registry rule that a
subtype Protocol's resolver legitimately supersedes/extends its base. The fix
lives in `v2/fxtr2/services/typed_service.py` + the resolver registry
(`v2/fxtr2/conversion/base_registry.py`), so it's a v2-core change, not a
workstation one. If solved, both `NixDispatcherProto` and `DemoDispatcherProto`
collapse to real subclasses.

Reference: `stage5_ui_presets.md`; the standalone-proto comment in
`dynamic_dispatch_types.NixDispatcherProto`.

## Spawn-env entry points ride on TOML-only metadata (demo-migration finding)

The `delayed_echo` split (host-importable marker package +
`sandbox_spawn/` spawn-env declaration, 2026-06-05) puts the
`[project.entry-points."fxtr.service"]` table on a **virtual** uv project
(`package = false`). This works because `mkWasmService` /
`mkContainerService` read the entry point straight out of
`src/pyproject.toml` via `fromTOML` — no installed-dist metadata is ever
consulted — but it means we're using `[project.entry-points]` purely as
inert config for nix on a project that never builds a dist (uv happily
locks it and even mints a stub dist-info). If that ever feels too cute,
the sanctioned alternative is an explicit `entryPoint` argument on
`mkWasmService` / `mkContainerService` for spawn-env-style srcs; today
the TOML is the only place it can live. The packaging conventions
themselves are documented in the demo template's `packages/README.md`.

Reference: `examples/fxtr_workstation_demo/agent_workdir/packages/delayed_echo/sandbox_spawn/pyproject.toml`;
`nix/lib/_readServiceEntryPoint.nix`.

## Transient invoke flake: "increment after resolved_sent" (demo-migration finding)

Seen once during the 2026-06-05 demo-migration e2e: the **first** invoke
against a freshly spawned + connected wasm service
(`delayed_echo.echo("…", 2.0)` through the workstation's invoke worker)
failed with `invoke_error:
AssertionError('increment after resolved_sent: caller must
short-circuit')`; an identical retry on a fresh slot succeeded, and every
subsequent invoke (including against other fresh spawns) was clean.
Smells like a refcount / lifetime race in the fxtr2 RPC layer around a
just-established bridge chain (nothing demo- or migration-specific). Not
reproduced since; if it recurs, the assertion lives at
`v2/fxtr2/rpc/lowering.py:1251` — reason about the
resolved-vs-increment ordering on newly bridged refs.

## Stage 9 podman-backend follow-ups

Surfaced landing `stage9_nix_runner.md` (the chunk-7 host-nix-free E2E sweep):

- **Builder binary-cache substituter.** In-container builds fetch flake
  inputs (e.g. nix2container's nixpkgs pin) from GitHub archive tarballs,
  which intermittently `504` and only cleared on retry. A binary-cache /
  substituter in the builder (or cache-pinned inputs) would cut the
  GitHub-archive dependence and the flakiness.
- **Case-sensitive `materialize_root` volume.** `materialize` (`nix copy`)
  reproduces a closure verbatim, so a closure with case-colliding names
  (classically linux `ncurses` terminfo) fails on a case-insensitive
  `materialize_root` (default macOS APFS). v0 sidesteps it by keeping guest
  outputs dependency-free (`_mkGuestSitePackages` strips `__pycache__`); a
  closure that *genuinely* needs case-colliding paths would need a
  case-sensitive volume. (Noted in `PodmanHandle.materialize`'s docstring.)

The design-time stage-9 deferrals — filtering socket proxy in front of the
deliverer, native-Linux sandbox rung, `lock` for a dirty flake *input*,
persistent deliverer — live in `stage9_nix_runner.md`'s security-posture +
chunk notes. Backend selection (no implicit default + setup wizard) is its
own follow-on: `stage9post1_backend_config_wizard.md`.

## Misc small items

- `nix flake init` template's exact wiring to point at the
  in-repo example workdir (Q9.A).
- Cosmetic location for deprecated entity types
  (`deprecated_entities.py` vs `deprecated/entities.py`)
  (Q12.C).
- Cosmetic subpackage layout for `NixConfigurableDispatchImpl`
  vs `BasicDynamicDispatchImpl` (Q6.C).
- Wrapping `mkEntityFromPython` documentation to its own design
  note if it gets used outside UI presets (Q6.E).
- Renaming the migrated `basic_test_demo` packages (Q9.B).
- Eager-build-everything mode on `set_working_dir` if cold-cache
  latency proves annoying (Area 08 follow-up).
- Structure-eval `--apply` filter expression (Q8.A) — pin during
  Stage 2 implementation.

## Other semi-related TODOs

- Change dispatch key string from a UUID to a `fxtr.transluce.org/id/{uuid}/name` string (will eventually be reservable from here, and eventually basic types can also inherit this)
- Add a Determinate Systems flake schema for user-authored flakes that follow our `fxtr.` namespace
