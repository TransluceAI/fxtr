# Chunk 3 post 1 — fix the scope-discipline root in fxtr_sandboxing

**Status: IMPLEMENTED** (own commit, after chunk 3). The 2026-07-21
audit (below) answered the blast-radius questions; the same day's
review rounds settled the API shape (**option A — always isolated,
not active in body, no new parameters**) and the `_serve_spawn_rpc`
question (**isolate it too** — see the audit item, incl. the contract
analysis and the empirical record; the one in-suite test failure
observed under isolation was attributed to a pre-existing
`test_transport_token_errors.py` suite marginality, not the change).

## Implementation record

- `spawner.py`: the env host scope is
  `FxtrScope(isolated=True, include=[GLOBAL_SCOPE])`, never active in
  the body; `_serve_spawn_rpc` opens its rpc scope `isolated=True`;
  docstrings/comments state the current contracts.
- `sandbox_launcher.py`: `_env_holder` reads `env.host_scope` (no
  active-scope read); the holder-task ACTIVE_SCOPE reset stays as
  defense-in-depth (its comment says so); the construction-scope
  keepalive edge is gone with the env's isolation.
- All ~35 test call sites updated with the joint-CM bridging recipe
  (`async with create_sandbox_environment(...) as env,
  FxtrScope(include=[env.host_scope]):`) and verified standalone.
  One site needed more: `test_sandbox_provenance.py` decodes
  provenance c-nodes from the scope active at resolve time — now the
  named bridging scope, not `env.host_scope`. The benchmark spike got
  the same edit, unverified (no runnable env).
- `test_sandbox_launcher_scopes.py`'s fake env now models a
  worst-case library (docstring updated) and exposes `host_scope`.
- Verified: fxtr_experiment full suite incl. the gated e2e sweep;
  every edited fxtr_sandboxing file standalone; the full serial
  fxtr_sandboxing sweep (144/145 + 3 skips — the one failure is the
  pre-existing roaming per-test-timeout marginality documented above:
  a different test each full run, baseline included, each passing
  standalone in seconds); and
  `fxtr_workstation_mcp/tests/test_mcp_in_memory.py` (the workstation
  consumer path — green after its own unrelated one-line import fix,
  a missed peer_rpc-migration holdover landed as its own commit).

## Context

The chunk-3 e2e deadlock (`chunk3_scope_map.md`) was *enabled* by a
scope-discipline violation in `fxtr_sandboxing`:
`create_sandbox_environment` opens its host scope as
`FxtrScope(name="sandbox_env")` — non-isolated, in library code —
so the long-lived environment scope silently adopts whatever scope is
ambient in the opening task as a structural parent.
`v2/notes/scope_discipline.md` requires library scopes to be
`isolated=True` with explicit `include`s precisely to rule this out.

The landed launcher-side fix (capture `ACTIVE_SCOPE` at
`FxtrSandboxLauncher` construction, reset it at the top of each holder
task) neutralizes the ambient context and is verified by
`tests/test_sandbox_launcher_scopes.py` — but it treats the symptom.
Any *other* caller of `create_sandbox_environment` from an
uncontrolled context (a dispatch, a task started from one) recreates
the hazard.

The non-isolation is only half-deliberate: the CM's docstring promises
"the host scope is the active scope in the body" (callers resolve the
spawner against it), and `isolated=False` bundles that
become-active-in-body behavior together with the unwanted
inherit-ambient-as-parent behavior. The fix is to keep the former and
drop the latter.

## Call-site audit (2026-07-21)

All ~24 files touching `create_sandbox_environment` were audited for
what breaks if the host scope stops inheriting the caller's ambient
scope (i.e. becomes `isolated=True, include=[GLOBAL_SCOPE, *explicit]`
with active-in-body preserved). Results:

- **No site fundamentally relies on ambient inheritance** — every
  dependence reduces to a nameable scope the caller already holds, so
  either an `include=` parameter or caller-side bridging scopes cover
  everything. Nothing blocks the change.
- **Unaffected** (8 files): every demo/main script (their ambient at
  the call is already `GLOBAL_SCOPE`) and the tests whose entities
  are all created *after* entering the env (natively in the host
  scope): the three `*_transport.py` suites, `test_host_death_cleanup`,
  `test_mcp_in_memory`.
- **Need a mechanical `include=` edit** (13 files, ~30 call sites,
  mostly `fxtr_sandboxing/tests/`): one repeated idiom — a
  bundle/config entity is `.store()`d in a bare outer `FxtrScope()`
  *before* entering the env, then passed to
  `spawner.spawn(...)`/`sandbox.connect(...)` whose proxy is homed on
  the host scope, so arg reachability rides the host scope's
  ambient-parent walk today. Fix per site: a bridging scope
  (`include=[<outer>, env.host_scope]`, resolve the proxy there) or
  an env-side `include=[<outer>]` if that param exists. Missing one
  turns the test's intended assertion into a client-side "not
  tracked" arg-encode error — mechanical, but each site matters.
  Also the `sandbox_spawn_benchmark` spike (same idiom).
- **`sandbox_launcher.py`** (fxtr_experiment): its holder/bridging
  scopes are already fully explicit; the only body-level ambient use
  is `get_active_scope()` → the existing `env.host_scope` field. The
  incidental keepalive edge on the construction scope is dropped
  (rationale under option A below).
- **Active-in-body is heavily used** (no-arg `.resolve()` in nearly
  every test/demo body) — but only mechanically: every use is a
  no-arg resolve/store that becomes explicit (`env.host_scope`), plus
  the launcher's `get_active_scope()` (→ the existing
  `env.host_scope` field). Keeping the contract would be pure
  churn-avoidance; see the decision below.
- The guest side (`fxtr_sandbox_rpc_adapter/_main.py`) uses
  `FxtrScope(isolated=True, include=[GLOBAL_SCOPE])` +
  `as_active_scope()` — precedent for the isolation half, though not
  for keeping activation: the guest is a special case (it bootstraps
  its process's ambient and no user enters that CM), whereas
  `create_sandbox_environment` returns a named object whose natural
  contract is explicit field access.
- The host scope's *own* needs are mounts only (the spawner impl,
  transport authorizers) — no stores or lifts from ambient —, so
  `GLOBAL_SCOPE` plus caller includes suffices.

## Scope of the change

1. **`create_sandbox_environment`** (`fxtr_sandboxing/spawner.py`):
   the host scope stops inheriting the ambient scope. DECIDED:
   option A; option B kept below as the rationale trail.
   - **Option A (CHOSEN) — fully isolated, not active in body.**
     Host scope = `FxtrScope(isolated=True, include=[GLOBAL_SCOPE])`;
     no activation around the yield (isolated-but-active is a weird
     unpairing of behaviors fxtr2 deliberately couples, and the CM
     returns a named `env` object whose natural contract is explicit
     access). Callers use `env.host_scope` directly, or — the
     disciplined consumer pattern, already what the launcher does —
     build a bridging scope: `FxtrScope(isolated=True,
     include=[my_scope, env.host_scope])` and resolve proxies there
     (dispatch-arg reachability follows the *proxy home scope*'s
     ancestry, so pre-stored bundles/configs reach the spawner
     through the bridging scope with no env participation needed).
     Consequence (verified): **no `include=` parameter** — no spawner
     method expects anything ambiently present in the env scope
     (spawn touches only its per-call scope; env methods mount on
     `self.host_scope` explicitly; zero stores/lifts host-side), so
     the env scope's full parent requirement is `[GLOBAL_SCOPE]` and
     the signature is unchanged. The one candidate `include=` use —
     the launcher's incidental keepalive edge on its construction
     scope — is dropped: it is unreliable (absent when no scope was
     active at construction, so nothing can depend on it), redundant
     (`open_local`'s exit stack closes the env before any user scope
     can reach its own teardown), and where it would bite it converts
     a misuse into a hang. The chunk-3 bug happened precisely
     because the unsafe behavior was the default and a library
     consumed it; this removes both halves of the magic. Cost: the
     ~30 mechanical test edits (explicit resolves/stores and/or
     bridging scopes — same files as the include= variant, better
     end-state: the suite then models the recommended pattern).
   - **Option B (REJECTED) — `SQLiteFxtrNamespace`-style flag**
     (`isolate_scope: bool = False`), treating the CM as a
     user-boundary API where ambient ergonomics are sanctioned. Zero
     churn (the launcher passes `isolate_scope=True`); but the
     hazard stays default-on, which is how chunk 3 happened.
2. **Audit the package's other implicit-scope sites** and make each
   one deliberate (isolated + include, or a comment justifying the
   ambient use as a user boundary):
   - `spawner.py` `_serve_spawn_rpc` (line ~205) →
     `open_rpc_scope(...)` with the default `isolated=False`: the rpc
     user scope's implicit parent is the spawn dispatch's call scope
     (the runner task is started on `env_tg` from *inside* the
     dispatch — `spawner.py:675` — and inherits its context; the four
     runner arms all funnel through `_serve_spawn_rpc`). DECIDED:
     **isolate it** (`isolated=True`; `open_rpc_scope`'s fxtr2
     *default* stays as-is). The edge was an oversight, established
     two ways:
     - *Contract analysis*: every documented lifetime guarantee
       routes through the explicit bridge or the task group — the
       module's own docstrings attribute guest keepalive/teardown to
       the `raw_guest_ref` chain-bridge (`spawner.py:198–204`,
       `:682–688`) and env-close safety to chain-bridge release plus
       `env_tg` waiting on the runner tasks (`:888–894`); the session
       scope itself never escapes `spawn` (module-private
       `_SpawnStartedValue`; used once, as the bridge source), so no
       caller can hold an expectation about its parentage. What the
       edge adds — call-scope completion ordered after conn-drain
       completion — is observed by nothing documented.
     - *Empirical*: with `isolated=True`, the fxtr_experiment e2e
       sweep (wasm/pyodide/container) + the launcher regression pass,
       and the full fxtr_sandboxing suite passes serially except one
       in-suite failure in `test_transport_token_errors.py` —
       **attributed to a pre-existing suite marginality, not the
       isolation**: the unmodified serial baseline fails the *sibling*
       test in the same file with the identical signature (per-test
       30s timeout + leaked-lease teardown noise; both siblings
       exercise the host-callback-raises redemption path, and both
       pass standalone in ~5s). Known pre-existing flake, worth an
       independent look (candidates: the per-test budget vs the eryx
       callback-failure abandonment path) but NOT a gate for this
       change. (Parallel `-n auto` runs are additionally
       contention-flaky on both baselines — verify serially.)
   - `network_authorizer.py:36` (docstring example + `get_client`'s
     per-connect scope): caller-side short-lived scope — likely fine
     as a user-boundary use; decide and document.
   - `direct_launcher.py` `direct_sandbox_call`: deliberately
     non-isolated (has a comment); confirm and keep or tighten.
   - Guest-side `fxtr_sandbox_rpc_adapter` (`_main.py`, `_http.py`):
     the guest process owns its whole context, so ambient inheritance
     is bounded; confirm and document.
3. **fxtr_experiment cleanup, riding the same commit**: revisit the
   launcher's `_construction_active_scope` capture — with the root
   fixed, holder-task scopes no longer *depend* on ambient context,
   so the capture can either stay as defense-in-depth or be replaced
   by an explicit, documented context (e.g. holder scopes derived
   from the runner scope; teardown ordering already guarantees the
   runner scope outlives `launcher.aclose`). Either way the current
   incidental "whatever was ambient when `open_local` ran" should
   become a stated contract.
   `tests/test_sandbox_launcher_scopes.py`'s fake environment must
   KEEP the hazard ingredient (non-isolated host scope) — after the
   root fix it models a worst-case library, which is exactly what
   makes it a regression test for the launcher's own behavior.

## Open questions

All decided (option A — always isolated, not active in body, no new
parameters; `_serve_spawn_rpc` isolated too; `open_rpc_scope`'s fxtr2
default unchanged; no `include=`; launcher keepalive edge dropped).
Nothing gates implementation. Noted for an independent followup, not
this commit: the pre-existing `test_transport_token_errors.py`
in-suite marginality (the `_serve_spawn_rpc` audit item above).

## Verification

- fxtr_sandboxing's own suites (spawner, blast radius, nix/pyodide/
  container spawners, direct launcher) — under option A the 13 edited
  test files are themselves the coverage for the `include=` edits, so
  the gated (`nix_runtime`) sweep must actually run, not skip.
- fxtr_experiment: core suite incl.
  `test_sandbox_launcher_scopes.py`, plus the gated e2e sweep (wasm,
  pyodide, container).
- A workstation demo smoke (the other production consumer of
  `create_sandbox_environment`).
- Update `fxtr_sandboxing/CODE_OVERVIEW.md` (scope arrangement of the
  environment) and this file's checkboxes.
