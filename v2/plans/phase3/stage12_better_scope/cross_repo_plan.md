# Stage 12 — Better Scope: cross-repo use-site migration

Covers consumers of `v2/fxtr2` **outside `v2/`** (other `projects/`, `spikes/`,
`examples/`). Companion to [`impl_plan.md`](./impl_plan.md) (the v2
implementation) and [`new_scope_design.md`](./new_scope_design.md) (the design).

## Approach: one post-v2 phase, per-project, all-mechanical

Decision: **do the entire cross-repo migration after all v2 chunks land.** No
interleaving. Consumers are red until v2 is complete, which is unavoidable
anyway — nearly every consumer pins `fxtr2 = { path = "../../v2", editable =
true }` (a **live editable path dep**, not a version pin), so they break in
lockstep with the v2 API on the branch and can't be green until v2 chunk 3
(async `mount`) has landed.

The good news from the audit: the consumer surface is **almost entirely
mechanical** — no consumer overrides service-impl internals, calls
`bridge_service_from_scope`, or touches `is_alive` / `_with_derived_lifetime` /
`autostart` / `_fxtr_mount_lifespan`. So nothing about the new design
(lifespans, auth model, keepalive_via) needs consumer-side rethinking. Each
project is a self-contained sweep that ends green; do `mountable=` removal in
the **same** sweep as `await scope.mount` (they touch the same files).

## Blast-radius audit (consumer code, build/cache copies excluded)

| Change | ~Sites | Migration |
|---|---|---|
| `scope.mount(impl)` → `await scope.mount(impl)` | **~168** (2 already awaited) | add `await`; make the enclosing fn `async` in the rare sync-helper case |
| Drop `mountable=` (`=True`/`=False`) | ~111 | delete the kwarg |
| bare `FxtrScope(...)` + `aclose` → `async with` / `lowlevel_start_lifespanless()` | ~21 (195 ctors − 174 `async with`) | wrap or lowlevel-start |
| `scope._main_lease.clone()` private access → public provenance helpers | 7 | use `scope.create_c_node(...)` / `create_p_node(...)` (no lease threading) |
| `scope._token` private access (now `\| None` after inert construction) | ≥4 | use the public `scope.scope_token` (non-None, raises if unstarted) — **AUDIT MISS, see below** |
| `bridge_service_from_scope(...)` now requires `keepalive_via` | ≥4 | add `keepalive_via="independent_lease"` (today's behavior) — **AUDIT MISS, see below** |
| `_mount_as_fxtr_service` override / `is_alive` / `_with_derived_lifetime` / `autostart` | **0** | none |

> **Audit corrections (found during migration).** Two rows above were
> originally scored **0** and were wrong:
> - **`bridge_service_from_scope` is NOT absent.** `fxtr_workstation` alone has
>   **4** (`external_services.py`, `service_impl.py`, `callback_impl.py`,
>   `state.py`). `keepalive_via` is now required (no default), so these
>   **hard-fail** without the arg — and many are reachable only through RPC
>   dispatch, so they surface at **runtime/pyright, not test collection**.
>   Every remaining project must be grepped for `bridge_service_from_scope`.
> - **`scope._token` private reach** breaks under inert construction: `_token`
>   is now `LifetimeToken | None`, so pyright-gated projects (`fxtr_workstation`,
>   `fxtr_sandboxing`, `fxtr_nix_runner`, `fxtr_workstation_mcp`) error on
>   `scope._token.foo()`. Swap to the public `scope.scope_token`. Re-grep each
>   project for `._token` (and other private scope attrs that became Optional:
>   `_main_arena` survives as a non-None property, but verify). Tests alone won't
>   catch these — rely on the project's `pyright` precommit hook.
>
> Also note: the public `FxtrScope.create_p_node` wrapper the `_main_lease` row
> assumed did not exist in v2 and was **added** as a prep commit.

Concentration (by project, mount + ctor weight): `fxtr_workstation` (heaviest)
> `fxtr_sandboxing` > `fxtr_workstation_agent_plugin` > `examples/fxtr_workstation_demo`
> `spikes/mcp_plugin_spike` > `fxtr_workflow` / `fxtr_report_builder` /
`fxtr_nix_runner` / `spikes/thingwatcher` / `behaviors_hub_*` (light: mostly
bare-scope / data usage, few or no mounts).

## Per-project sweep (the standard checklist)

Apply to each project, in the concentration order above, each ending with that
project's own test suite green (`uv run pytest -n auto` in the project). Keep
the find/replace-y parts as separate commits for review.

- [ ] `await scope.mount(...)` everywhere `mount(` resolves to an `FxtrScope`
  mount; promote the enclosing function/helper to `async` where it was sync
  (and `await` its callers — usually already in async test bodies).
- [ ] Delete `mountable=True` / `mountable=False` kwargs.
- [ ] Rewrite bare `FxtrScope(...)` + `await scope.aclose()` to `async with
  FxtrScope(...) as scope:`; use `lowlevel_start_lifespanless()` only where a
  CM genuinely doesn't fit.
- [ ] Replace `scope._main_arena.create_c_node(scope._main_lease.clone(), …)` /
  `create_p_node(...)` with the public `scope.create_c_node(...)` /
  `scope.create_p_node(...)`.
- [ ] Run that project's suite green before moving on.

### Per-project notes
- **`fxtr_workstation`** — heaviest (mounts in ~36 files, ~22 ctors, 16
  `mountable=True`, several `_main_lease` in tests/examples incl. one
  `create_p_node`). Largest single sweep.
- **`fxtr_sandboxing`** — spans sub-packages (`fxtr_sandboxing`,
  `fxtr_sandbox_types`, `fxtr_sandbox_rpc_adapter`, `fxtr_service_runner`) plus
  test `_workdir_templates/*` (see Special cases). `_main_lease` in
  `test_sandbox_provenance`.
- **`fxtr_workstation_agent_plugin`** — `fxtr_workstation_mcp`, the sandbox
  bridge, and `examples/basic_test_demo/agent_workdir/packages/*` (templates).
- **`fxtr_workflow` / `fxtr_report_builder` / `fxtr_nix_runner`** — light; mostly
  bare-scope / data-API usage.
- **`spikes/thingwatcher`** — debug UI ("will be used by fxtr2"); small.
- **`spikes/mcp_plugin_spike`, `examples/fxtr_workstation_demo`** — small;
  demo also has `agent_workdir/packages/*` templates.

## Special cases

- **Sandbox workdir templates** (`fxtr_sandboxing/.../tests/_workdir_templates/*`,
  `*/agent_workdir/packages/*`). These carry a dual dependency form:
  `fxtr2 = { path = ".../v2", editable = true }  # fxtr-lib-sub: active-in-template`
  vs a commented `git = "@FXTR_LIB_URL@", rev = "@FXTR_LIB_REV@"  #
  active-in-workdir` that is swapped in when the template is materialized into a
  sandbox workdir. Migrate the **template source** like any other consumer; the
  materialized form pins to a **git rev**, so a **rev bump** is needed for live
  sandboxes to pick up the change. Coordinate that bump after the v2 change is
  committed. Template service impls (e.g. `hello_service`, `delayed_echo`,
  `greeter`) that mount services need the `await scope.mount` fix too.
- **`v2/portable` / `fxtr2-portable`** — a packaging flavor (`pyproject.toml`
  only, no `.py`); no separate scope code, so no code migration — just make sure
  the portable extra resolves against the updated `fxtr2`.

## Final step

- [x] Confirmed via repo-wide grep over `projects/` + `examples/` (excluding
  build/cache **and `spikes/`**, intentionally skipped): no remaining
  `mountable=`, no `_main_lease`, no `scope._token` reach, no un-awaited
  `scope.mount`/`get_active_scope().mount`, and every `bridge_from_scope` /
  `bridge_service_from_scope` call (9 total) carries `keepalive_via`. The only
  `.mount(` / "mountable" string hits left are **prose docstrings** and the
  unrelated `PodmanConfig.mountable_paths`/`_mountable_path_args` in
  `fxtr_nix_runner`.
- [x] Unrelated issues surfaced by the sweep (NOT folded in): `fxtr_workflow`'s
  7 pre-existing `test_fxtr_support` FxtrID-conversion failures (see progress
  log). Flagged for separate investigation.

## Re-running the audit

The find+xargs form is required (plain `grep -r --include` matched nothing on
this machine):

```sh
find projects spikes examples -name '*.py' \
  -not -path '*/.venv/*' -not -path '*/__pycache__/*' -not -path '*/node_modules/*' \
  -not -path '*/.cache/*' -not -path '*/wheel_cache/*' -not -path '*/site*-packages/*' \
  -print0 | xargs -0 grep -nE 'mountable|FxtrScope\(|\.mount\(|_main_lease'
```

## Migration progress

Per-project sweeps (concentration order), each committed separately via jj.

- [x] **v2 prep** — added public `FxtrScope.create_p_node` (+ test, overview).
- [x] **`fxtr_report_builder`** — 1 `create_c_node` site. Green (4 passed, 12
  pre-existing infra skips).
- [x] **`fxtr_workstation`** — 113 `await mount`, 71 `mountable=`, 9
  `lowlevel_start_lifespanless` (record/load/transient/scratch), 4
  `create_{c,p}_node`, 2 `_token`→`scope_token`, 4 `keepalive_via`. Green (375
  passed, 1 infra skip: debug UI not built). pyright clean.
- [x] **`fxtr_sandboxing`** — 22 `await mount` (incl. spawner sync→async cascade +
  14 test callers), 18 `mountable=`, 1 `c_node_to_context_data`, 1
  `_token`→`scope_token`, 2 `ServiceRef.bridge_from_scope` `keepalive_via`.
  pyright clean. Nix/podman + container/wasm suites green serially after the
  lib-mirror `mountable_paths` env fix; 2 tests flaky only under `-n auto`.
- [ ] `fxtr_workstation_agent_plugin`
- [x] **`examples/fxtr_workstation_demo`** — 5 `await mount` (2 chained service
  mounts + 3 `agent_workdir/packages` template `run()` entrypoints), 2
  `mountable=`. No test suite (harness-run demo); py_compile clean; templates
  exercised transitively by the green sandboxing nix e2e suite.
- [~] `spikes/mcp_plugin_spike` — **SKIPPED per user**: spikes are bitrotted
  against an old API, nothing meaningful to migrate, not reliably testable.
- [x] **`fxtr_workflow`** — **NO-OP** (every `FxtrScope` already `async with`; no
  mount/`mountable=`/`_main_lease`/`_token`/bridge/hazard). No edits made.
  ⚠️ 7 **pre-existing, stage-12-unrelated** failures in `tests/test_fxtr_support.py`
  (a step returning an `FxtrID` gets it back as `RawWeakFxtrID` — a
  conversion-layer round-trip issue, not scope lifecycle). NOT fixed (out of
  scope); flagged for separate investigation — confirm whether it's a v2
  regression from outside stage-12 or a known branch red.
- [x] **`fxtr_nix_runner`** — **NO-OP**. No fxtr2-scope usage at all; it was in
  the audit's file list purely by **substring false-positive** (`mountable_paths`
  / `_mountable_path_args` matched the broad `mountable` grep; no `mountable=`,
  no `.mount(` method call). Config-layer tests green (35 passed). The slow nix
  backend is exercised transitively by `fxtr_sandboxing`'s now-green nix suite.
- [~] `spikes/thingwatcher` — **SKIPPED per user** (same reason).
- [x] **workdir-template sources** — migrated in-place by their owning project
  sweeps: `fxtr_sandboxing/tests/_workdir_templates/*`,
  `fxtr_workstation_agent_plugin/examples/basic_test_demo/agent_workdir/packages/*`,
  and `examples/fxtr_workstation_demo/agent_workdir/packages/*`. The
  `@FXTR_LIB_REV@`-pinned commented dep lines left untouched (materialized-workdir
  rev bump is out of scope, per user — no concern for out-of-repo pinned hashes).

## Findings & surprises (running log)

Chronological log of everything we hit during the cross-repo migration —
especially the non-obvious. Useful for the post-migration review.

### Audit was wrong about "0 bridge sites / purely mechanical"

The intro claimed no consumer calls `bridge_service_from_scope` and the sweep
is "all-mechanical." Both turned out false — there were **three** required-arg /
private-attr categories the audit scored 0:

1. **`FxtrScope.bridge_service_from_scope` — NOT absent.** `fxtr_workstation`
   has 4 (`external_services.py`, `service_impl.py`, `callback_impl.py`,
   `state.py`). `keepalive_via` is now required (no default) → these hard-fail
   without it, and several are only reachable through RPC dispatch, so they
   surface at **runtime / pyright, not test collection**. Fixed with
   `keepalive_via="independent_lease"` (preserves prior behavior).
2. **`ServiceRef.bridge_from_scope` — a DISTINCT method the audit never grepped
   for — ALSO requires `keepalive_via`.** `fxtr_sandboxing` has 2 (`spawner.py`,
   `direct_launcher.py`), both core RPC guest-ref bridge-back. Missing the arg
   caused **21 of 30 initial sandboxing test failures** (everything routing
   through spawn/connect). **Every project must grep BOTH `bridge_from_scope`
   and `bridge_service_from_scope`.**
3. **`scope._token` private reach breaks under inert construction.** `_token` is
   now `LifetimeToken | None`, so any consumer doing `scope._token.foo()` trips
   pyright `reportOptionalMemberAccess`. Swap to the public **`scope.scope_token`**
   (non-None, raises if unstarted). Seen in `fxtr_workstation/service_impl.py`
   (2× `register_end_callback`) and `fxtr_sandboxing/network_authorizer.py` (1×).
   **Tests do not catch these — the project's `pyright` precommit hook does.**

### v2 gap: `create_p_node` didn't exist

The plan's `_main_lease` row assumed a public `scope.create_p_node`. Only
`scope.create_c_node` existed. Added `FxtrScope.create_p_node` (mirrors
`create_c_node`: `check_authorized` → auto-lift refs → `_main_arena.create_p_node`
with a cloned auth lease) as a prep commit, plus a unit test + overview update.

### Per-project mechanical specifics worth knowing

- **`fxtr_workstation`**: 113 `await mount`, 71 `mountable=`, 4
  `create_{c,p}_node`, 2 `_token`, 4 `keepalive_via`. All `src/` mounts were
  already inside `async` fns → **no sync→async cascade needed**. Chained form
  `(await x.mount(i)).replace_service_type(...)`; nested
  `await dispatcher.subscribe(await scope.mount(...))`. 9 bare
  record/load/transient/scratch scopes (these were `mountable=False`, outlive
  their creating fn, torn down via `force_close`) → migrated to
  **`lowlevel_start_lifespanless(parent_leases={parent: parent.clone_authorization()})`**,
  matching v2's own `_get_or_mint_fallback_or_own_marker` idiom. `clone_authorization()`
  works because the parent (root_scope / record scopes) holds base authorization.
- **`fxtr_sandboxing`**: here a **sync→async cascade WAS needed** —
  `spawner.py`'s `_mount_authorizer` / `add_transport` / `add_passthrough_transport`
  / `add_raw_transport` were sync, became `async`, and **14 test callers** had to
  add `await`. Reminder that "no cascade in workstation" doesn't generalize.

### Tooling gotcha: bare `uv run pyright` is misleading in multi-package projects

In `fxtr_sandboxing` (a multi-package uv workspace), a bare `uv run pyright`
floods with `reportMissingImports` for sibling packages it can't resolve, plus
pre-existing unrelated errors. **The authority is the repo's
`pyright (projects/<x>)` precommit hook** (run via `jj pre`), which resolves the
workspace correctly. Confirm pyright there, not via a bare invocation.

### Env / infra issue (NOT a migration problem): podman lib-mirror not mounted

7 `fxtr_sandboxing` nix/podman tests failed with
`PodmanError: Git repository ".../fxtr-workdir-harness/lib-mirror.git" does not
exist`. Root cause: `~/.config/fxtr/nix_runtime.toml` had **lost its entire
`[nix_backend_podman]` table**, so `PodmanConfig.mountable_paths` defaulted to
empty. The workdir harness publishes the fxtr lib to that bare mirror and the
build container fetches it as `git+file://<mirror>?rev=…`; `mountable_paths` is
the whitelist of host dirs bind-mounted RO (`-v <dir>:<dir>:ro`) so those inputs
resolve in-container. Fix: restore the table with
`mountable_paths = ["/Users/daniel/.cache/fxtr-workdir-harness/lib-mirror.git"]`.
Verified `test_nix_spawner` 3/3 green after. (Config is outside the repo, so this
is a machine-setup fix, not a code change — logged here for context only.)

### Flaky under `pytest -n auto`

`test_direct_launcher_factory_reuse_across_sandboxes` and
`test_nested_spawn_wasm_inner` time out under `-n auto` but pass in
isolation/serial — resource contention from many concurrent nix builds, not a
regression. Run heavy nix/sandbox suites serially (or with low `-n`) when
confirming green.

### Context-copy hazard (design §11) — the first genuinely NON-mechanical breakage

`fxtr_workstation_agent_plugin` surfaced this (8 e2e failures after the
mechanical sweep). A test helper / demo impl spawned a background notify task via
`task_group.start_soon(...)` **from inside a dispatch handler**, where the mount
scope is authorized through a *transient per-context override* (§5.3). The
spawned task inherits that override in its copied context; once dispatch returns
and discards the override lease, the task raises `RuntimeError: authorization
override in this context was discarded`. Under the old `_main_lease`-attribute
model the scope was usable from any task, so this "just worked."

**Fix pattern (design §4.3/§11 cross-task):** hand the spawned task a standalone
`scope.clone_authorization()` and have it re-authorize via
`with scope.authorize(auth_lease):`. (`authorize` takes ownership, so the clone
is consumed — no leak.)

**Rule of thumb for the migration:** *any* consumer that spawns a background task
from a dispatch handler (or any override-authorized context) and later uses that
scope hits this. It is **runtime-only** — tests catch it only if they exercise
the spawn path. The base-authorized case (spawning from lifespan/mount **setup**,
or using the always-base-authorized root scope) is safe. Production
`ws_connection.py` log-writer is safe for exactly that reason.

**Action:** already-migrated projects (`fxtr_workstation`, `fxtr_sandboxing`)
re-audited for `start_soon`/`tg.start` reachable from a dispatch handler that
uses an override-authorized scope. **Audit result: all 23 background spawns are
SAFE.** 15 spawn from BASE-authorized contexts (server lifespan / sync WS
handlers / `apply_mutation` on the long-lived `background_tg`). The 8 that spawn
from a dispatch/override context are safe because they either (a) use
`tg.start(task_status)` synchronous-handoff — the spawned task does its critical
scope ops and calls `started()` *before* the dispatch returns and discards the
override (e.g. `callback_impl._inbox_subscription_parker` bridges at spawn time,
then only touches its own base-authorized `sub_scope`; `spawner._spawn_runner_*`
opens a fresh independent RPC scope), (b) re-authorize via a passed
`clone_authorization()` (`direct_load_worker`, `load_worker`), or (c) receive the
scope as an explicit parameter rather than via contextvar inheritance
(`service_record_worker`, `_phase2_attach_entity`). So `agent_plugin`'s test
helper was the only genuine instance, now fixed.

Cheap canary the fork noted: `uv run pytest --co -q` per project before the full
run — a missed sync→async promotion leaves `await` in a sync def = collection-time
`SyntaxError`.
