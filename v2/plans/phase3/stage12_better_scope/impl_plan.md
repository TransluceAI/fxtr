# Stage 12 — Better Scope: implementation plan (v2/fxtr2 + v2/tests)

Implements [`new_scope_design.md`](./new_scope_design.md). This plan covers
the `v2/fxtr2` implementation and `v2/tests` migration; cross-repo use-site
changes (other `projects/`) live in a separate doc written after this one.

Read the design doc first — this plan does **not** re-spell the API contract
(see design §7) or the mechanics (§3–§6). It focuses on **sequencing, the
moving parts per chunk, and which tests are green when**.

## Chunking & green/red expectations

The authorization-model flip is the irreducible core; the service layer is
coupled to the exact internals it removes (`_main_lease`, `_key_lease`,
`_with_derived_lifetime`), so services go red mid-refactor. Guarantees:

| After chunk | Green | Red |
|---|---|---|
| 1 (core) | data-layer suites (`test_entity_ref_scope`, `test_memory_arena`, `test_leases`, `test_conversion`, store/namespace suites) | `test_services`, `test_rpc`, `test_cap_ir_arena` |
| 2 (lifespan) | above + new lifespan tests | `test_services`, `test_rpc`, `test_cap_ir_arena` |
| 2.5 (flatten decompose/compose) | above (same set) | `test_services`, `test_rpc`, `test_cap_ir_arena` |
| 3 (service cutover) | **everything** | — |
| 4 (keepalive_via) | everything | — |
| 5 (docs + cleanup) | everything | — |

Transitional decisions:
- **No accepted-no-op for `mountable=`.** The `mountable=`/`autostart=` params
  are removed from the constructor in chunk 1; `mountable=`/`autostart=` call
  sites are stripped **file-by-file in whichever chunk migrates that file**
  (data-layer + their tests in chunk 1; service layer + `test_services` /
  `test_rpc` / `test_cap_ir_arena` in chunk 3). Removing the param early makes
  nothing *newly* red — the files passing `mountable=` are either rewritten in
  chunk 1 anyway (autostart) or already red chunks 1–3 (service layer). This
  avoids touching the same files twice and a lingering deprecated param. Keep
  the purely-mechanical strips as **separate commits within their chunk** for
  review hygiene, but don't add a half-migrated green checkpoint just for them.
- `autostart=` and the auto-start-at-construction behavior are removed in
  chunk 1 (inert construction is incompatible with it); the bare
  `FxtrScope(...)` + `aclose` pattern in data-layer tests is migrated there.
- Run the relevant subset with `uv run pytest -n auto <paths>` at each chunk
  boundary; full suite after chunk 3.

Add the new tests (design §11) and delete superseded tests **within the chunk
that introduces the behavior**, not at the end.

---

## Chunk 1 — Core lifetime rewrite (`data/entity_ref_scope.py`)

**Goal.** Replace the `_main_lease`-attribute + 3-bool model with the
per-context authorization model, inert construction, and the uniform derived
token. Keep a task group on every CM scope (no `attach_lifespan_to_scope`
yet). Data-layer suites green.

**Key moving parts:**

- [x] **Authorization = scope-level base + per-context overrides.** Base: a
  plain `self._scope_auth_lease: Lease[main] | None` attribute (essentially
  today's `_main_lease`, retained) — set at entry, dropped to `None` at
  `aclose`. Overrides: one module-level `_AUTH_OVERRIDES: ContextVar[Mapping[int,
  Lease[main]]]` (default `{}`), COW-inserted **only by `authorize`** (not by
  scope entry). Helper `self._current_auth_lease()` resolves: override entry
  present & active → use it; present but dead → **raise discarded-override
  error (no fallback)**; absent → base if set & active, else raise
  not-authorized. All ops that did `self._main_lease.clone()` now do
  `self._current_auth_lease().clone()`. Root just sets its base attribute → no
  override map default, no special case (design §2, §3.4). (Design §2,
  §3.1–§3.2, §3.5, §8.)
- [x] **Public decompose/compose/clone API.** `clone_authorization()` (clone
  resolved auth lease), `decompose_authorization(lease) ->
  ScopeAuthorizationParts` (consume; `derived.exchange_reverse` → `key_lease`
  + `{parent_scope: Lease[parent.main]}`), `compose_authorization(key_lease,
  parent_leases) -> Lease[main]` (`derived.exchange_forward`; validate parent
  set, take ownership, require open). Add the frozen `ScopeAuthorizationParts`
  dataclass. Scope drives these via the retained **key ULL object** (`_key_ull`,
  `_key_ull.token` is the key token), `_derived`, and the direct-parent set.
  (§3.2, §7.)
- [x] **Inert `__init__`.** Store config only — no token, arena, marker, or
  parent leases. Move token mint / `_init_arena_and_cache` /
  `_populate_ancestor_bridges` / `_apply_context_marker_override` /
  `provenance_context_from_scope` sync copy into the entry path. **Remove the
  `mountable`/`autostart` params.**
- [x] **Uniform derived token + key-into-base.** Entry builds
  `DerivedLocalLifetime(inputs={key_ull.token} ∪ {parent.main…})`, mints the
  first authorization via `exchange_forward`, stores it as `_scope_auth_lease`,
  **discards the bare key root lease** (keep the `_key_ull` *object* as a revoke
  handle — drain still natural since the object pins nothing). `subclone(auth,
  key_token)` recovers a key lease when needed. Remove `_scope_ull` / the
  ULL-only path. (§3.2.)
- [x] **`open_with_explicit_leases(parent_leases=…)`** — the entry primitive
  (consumes supplied parent leases as derived inputs). `__asynccontextmanager__`
  clones each parent's in-context authorization and delegates to it. (§4.2,
  §4.4.) The one path that may derive from a draining-but-open parent.
- [x] **`authorize(lease)` (sync CM, binds override)**, **`discard_authorization()`
  (sync, idempotent; override-first: drop the override lease leaving the dead
  map entry, else drop the base → `None`)**, **`as_active_scope()`** unchanged
  (lease-free). (§4.3.)
- [x] **`lowlevel_start_lifespanless()`** (token + set base `_scope_auth_lease`,
  no TG) and **`_start_as_root_scope`** layered on it (sets root's base
  attribute → authorized everywhere since base is a plain attribute; still
  returns `self`, so the `GLOBAL_SCOPE = FxtrScope(...)._start_as_root_scope()`
  one-liner is unchanged). (§3.4, §4.5.)
- [x] **`is_open()` / `is_authorized()` / `check_authorized()`** replace
  `is_alive` / `check_alive`; remove `_draining` (drain ⟺ token alive + base
  `None`). Distinct error messages for not-started / closed / unauthorized /
  discarded-override. (§3.5, design §7.)
- [x] **`aclose` / `force_close`** mechanics: `aclose` drops the base
  `_scope_auth_lease` (→ `None`) then awaits token drain (closing-event + TG
  steps land in chunk 2 — for now: drop base → drain → close arena; the CM
  still opens/closes the TG). `force_close` revokes `_derived` **and**
  `_key_ull` (breaks externally-split key leases) → close arena.
- [x] **Re-express `_mint_ancestor_lease` / lift / deep-fetch** ancestor
  lease-minting in terms of the derived/authorization machinery rather than a
  stored `_main_lease`.
- [x] **Remove `_with_derived_lifetime`** (superseded). Service layer calls it
  → runtime failure (expected red until chunk 3).
- [x] **Other in-repo data-layer callers of `_main_lease`** — grep
  `_main_lease` across `fxtr2/` (esp. `fxtr_context_api.py`, namespaces,
  `store_indexing`) and update to the new minting; confirm none are
  module-level.
- [x] **Strip `mountable=`/`autostart=`** from every chunk-1-green file
  (data-layer `fxtr2/` + their tests), as a separate mechanical commit.

**Tests (this chunk):**
- [x] Migrate `test_entity_ref_scope/*` off the bare-`FxtrScope`+`aclose` /
  autostart pattern (→ `async with` or `lowlevel_start_lifespanless()`);
  drop reliance on `_main_lease`/`is_alive`.
- [x] Update `test_derived_*` (the `_with_derived_lifetime` suites) to the
  uniform derived token / `open_with_explicit_leases` (design §10 open issue).
- [x] Add core auth tests (design §11 "Authorization", "Construction /
  lifetime"): per-context isolation, nested-parent-authorized, spawned-task
  inheritance + discarded-lease hazard, `is_open`≠`is_authorized`,
  root-authorized-in-fresh-task, parentless-closes-on-deauthorize,
  inert-construction-no-leak, `authorize` on draining-but-open vs closed.
- [x] Add decompose/compose/clone tests (design §11): `decompose_authorization`
  → `compose_authorization` round-trip; mismatched parent set rejected;
  `clone_authorization` → `authorize` in another context makes the scope usable
  there.
- [x] **Green gate:** data-layer suites pass.
- [x] Update `data/LOCAL_CODE_OVERVIEW.md` for the new lifecycle (and root
  `CODE_OVERVIEW.md` scope section).

### Chunk 1 — bugs found / unexpected changes (running log)

- **Duplicate-parents lease leak.** A non-isolated scope whose `include=` names
  the currently-active (outer) scope listed it twice in `_parents`
  (`(outer, *include)`), so the entry path cloned its authorization twice into a
  scope-keyed `parent_leases` map and **leaked the first clone**. Fix:
  `_init_common` now dedupes `_parents` by identity, preserving order. (Caught
  by `test_include_scope` via the leak fixture.)
- **`_get_or_mint_fallback_or_own_marker` used an inert scope.** It built a
  `tmp_scope = FxtrScope(isolated=True, include=[GLOBAL_SCOPE])` and used it
  immediately — relying on the old auto-start. With inert construction that
  scope had no token. Fix: `.lowlevel_start_lifespanless()` it (sync context).
- **`check_authorized` error ordering.** Reordered to report token-state
  (not-started / closed) **before** the authorization error, so a closed scope
  says "already been closed" rather than "not authorized". (Refinement, keeps
  error messages specific.)
- **Closing a never-opened scope now errors** (see test-migration log) — a
  design refinement enabled by removing autostart.
- **Typing cleanup for inert-construction Optionals.** Making `_token` /
  `_main_arena` / `_derived` / etc. `| None` (for the inert pre-entry state)
  introduced ~50 pyright `reportOptionalMemberAccess`/`reportArgumentType`
  errors at runtime-safe post-entry sites. Resolved with typing-only narrowing
  (no runtime change): `_main_arena` is now a read-only **property** over a
  `_main_arena_obj` backing field (returns non-None, raises if unentered), and
  an `_entered_token` property is used at the ~13 sites that pass a token to
  non-Optional APIs; `get_context_marker_id` / marker-override narrow locally.
  Pyright is back to 0 errors on the file.
- **Ancestor bridge for a parent is now case-3 Dependent, not Independent.**
  Under the uniform-derived token every parent is a derived input, so the
  ancestor entry is a `DependentLocalBridge` (`subtake`) with no stored lease.
  Tests asserting an `IndependentLocalBridge` ancestor entry were updated.

### Derive-from-external-token — RESOLVED in chunk 1 via `extra_derived_leases`

`_with_derived_lifetime` was genuinely used in production to derive a scope's
lifetime from **arbitrary external tokens that are not parent scopes**:
`_dispatch_body` (`services/service_cap_common.py`) builds the per-call
`call_scope` with `derived_from_leases = _take_input_leases(packet)` (one lease
per **arg-ServiceRef** shipped in `packet["leases"]`, keyed by subject token)
**plus** `parent_scope._token`. Only `parent_scope` is in `include`; the arg
tokens have no `FxtrScope` object. So the call scope's lifetime is gated by both
its mount scope and the service-ref arguments, and the arg tokens get case-3
dependent ancestor entries so service handles for them can attach.

**Resolution (option (a), built in chunk 1):** `open_with_explicit_leases` /
`lowlevel_start_lifespanless` / `_do_open` take an `extra_derived_leases:
Mapping[LifetimeToken, Lease]` param — non-parent derived inputs that become
case-3 dependent ancestor entries (`_populate_ancestor_bridges` installs them)
but are not parent scopes. `decompose_authorization` / `compose_authorization`
carry the matching `extra_leases` set (`ScopeAuthorizationParts.extra_leases`).
The ported `test_derived_scope_acceptance.py` exercises it end-to-end. Chunk 3's
`_dispatch_body` rewrite will build the `call_scope` with
`parent_leases={mount_scope: outer_lease}` + `extra_derived_leases={arg tokens}`.
Design §4.4 / §7 updated accordingly (and §5.3 should reference it when the
dispatch is rewritten).

### Chunk 1 test-migration log (non-mechanical changes)

Reasoning per-test about *intent* (not matching the old structure). Mechanical
strips (`mountable=`/`autostart=` removal, `_main_lease.clone()` →
`clone_authorization()`, `is_alive()` → `is_open()`, `check_alive` →
`check_authorized`, internal `_main_lease` → `_scope_auth_lease`) are not
logged individually. Notable / judgement-call changes:

- **Refinement landed in code:** closing a *never-opened* scope (`aclose` /
  `force_close` on a constructed-but-unentered scope) now **raises** instead of
  short-circuiting — with no autostart you can't close what you never opened.
  (Tests asserting the old "never-entered close is a no-op / drains the ULL"
  are rewritten to assert the raise.)
- **`test_scope_lifecycle.py`**: `TestScopeDrain` bare-scope tests rewritten to
  `lowlevel_start_lifespanless()`; `test_force_close/aclose_on_mountable_not_entered`
  → `test_*_on_never_opened_raises`; `test_mutation_raises_during_drain` →
  `test_mutation_raises_after_close`; `test_check_alive_states` →
  `test_check_authorized_states`. `TestMountableScope` (tested the removed
  `mountable` property + autostart) **dropped**, replaced by `TestScopeEntry`
  (every CM scope has a task group / lifespanless has none / never-entered not
  open) with a TODO for chunk-2/3 lifespan coverage. `TestDerivedScope` (tested
  `_with_derived_lifetime`) **dropped**, replaced by `TestUniformDerivedToken`
  (token is derived output; parent-revoke cascades; `open_with_explicit_leases`
  from a supplied lease; store/load in a child).
- **`test_cap_bridges.py`**: `test_*_requires_mountable` → `test_*_requires_task_group`
  (lifespanless scope, "no task group"). `test_resolve_lifts_parent_bridge_into_child`
  assertion updated: parent ancestor entry is now a case-3 `DependentLocalBridge`
  (was `IndependentLocalBridge`).
- **`test_derived_ancestor_bridges.py`**: rewritten off `_with_derived_lifetime`
  to ordinary derived child scopes (`async with FxtrScope(include=[parent])`).
  Dropped `TestUnconditionalGrandchildOfDerived` — it tested the removed
  "unconditional grandchild eager-eval → Independent" branch (no unconditional
  scopes exist now; the transitive-Dependent case is covered by the 3-deep
  chain test).
- **`test_derived_scope_acceptance.py`**: **ported** (not deleted — see the
  `extra_derived_leases` resolution of the gap above). Task 2 now derives
  `scope2` from task 1's external token via
  `open_with_explicit_leases(extra_derived_leases={t1_token: lease})`; the
  converters account for GLOBAL being a derived input now; `_key_lease` is
  replaced by splitting a key clone from the auth (`derived.subclone`). The
  P1–P4 drain predictions are unchanged (scope1's only non-key input and
  scope2's extra input are GLOBAL, which never ends).
- _(entries appended below as files are migrated)_

---

## Chunk 2 — Lifespan machinery (additive)

**Goal.** Add the closing event + `attach_lifespan_to_scope` without touching
the service layer. Data + lifespan tests green.

> **Teardown sequencing (refines the design's §4.2/§4.6).** Two facts about
> the chunk-1 structure shape this:
> 1. The task group lives in `open_with_explicit_leases`'s `AsyncExitStack`, so
>    it closes when the stack unwinds — *after* `aclose` returns. `aclose` runs
>    *inside* that TG context, so it **cannot `await` the TG itself** (would
>    deadlock: the TG won't drain until `aclose` returns).
> 2. `await self._derived.aclose()` waits for the token to **end**, which only
>    happens once the last `Lease[main]` clone drops — including any in-flight
>    dispatch leases (chunk 3). So **the moment the drain `await` returns,
>    every holder that could touch the mount arena has released**, and the
>    arena is safe to close immediately. There is **no need to delay the arena
>    close** until after lifespan teardown (teardown runs *unauthorized* — it
>    can't do arena ops anyway). [This corrects the original "await TG → close
>    arena" wording, which was unnecessary.]
>
> For a **structured-concurrency** `aclose` (it should not return until the
> scope's lifespan tasks have actually exited, regardless of how `aclose` was
> invoked — not only via the `async with` stack unwind), we use a **counter +
> a second event** instead of awaiting the TG:
> - `_lifespan_count: int` and `_lifespans_closed_event: anyio.Event` (created
>   unset at CM entry, alongside `_closing_event`; lifespanless scopes have
>   neither).
> - `_run_lifespan` increments the count at task start and, in a `finally`,
>   decrements it and **sets `_lifespans_closed_event` when it hits 0**
>   (increment+decrement paired *inside the task* so a `tg.start` failure can't
>   leak the count).
> - `aclose`: drop base → `await derived.aclose()` (drain) → set `_closing_event`
>   → **close the arena** → `if _lifespan_count > 0: await _lifespans_closed_event`
>   → mark closed. The stack's `TG.__aexit__` (after `aclose` returns) is then a
>   no-op — the lifespan tasks already finished.
>
> A single, non-re-armable event suffices: a lifespan task only finishes at
> close (it parks on `_closing_event` until `aclose`/`force_close` fires it), so
> `_lifespan_count` is monotonically up during the scope's life and down at
> close; and once `aclose` drops the base, no *new* lifespan can attach
> (`attach_lifespan_to_scope` → `check_authorized` now fails), so the count only
> decreases to 0 once. If `aclose`'s lifespan-wait is cancelled, the stack's
> `TG.__aexit__` cancels the parked/tearing-down tasks on unwind (consistent
> with the drain-wait's cancellation behavior).
>
> `force_close` stays **synchronous**: drop base → revoke derived + key → set
> `_closing_event` → close arena (no wait — it has no async context). On the CM
> exception path the stack's `TG.__aexit__` awaits/cancels the parked lifespans.
> `lowlevel_start_lifespanless` scopes have no TG / events; their
> `aclose`/`force_close` just close the arena (as in chunk 1). `_main_arena_obj`
> (the chunk-1 backing field behind the `_main_arena` property) is the close
> target.

- [x] **Closing event + lifespan-count latch** on the scope, created at CM
  entry (in `open_with_explicit_leases`, alongside the task group);
  `aclose`/`force_close` set `_closing_event`; `aclose` additionally awaits
  `_lifespans_closed_event` so it returns only once lifespans have exited, per
  the sequencing note above. (Harmless to scopes with no lifespans: count 0 →
  no wait.) (§4.2.)
- [x] **`attach_lifespan_to_scope(cm)`** — mint a setup lease from the
  (authorized) caller, `tg.start(_run_lifespan, cm, setup_lease)`. `_run_lifespan`
  bumps `_lifespan_count`, uses **proper `async with cm`** under
  `authorize(setup_lease)` + `as_active_scope()`, calls `discard_authorization()`
  right after `started()`, parks on the closing event, tears down unauthorized,
  and in a `finally` decrements the count (setting `_lifespans_closed_event` at
  0). Returns the CM's yielded value. Raises on a lifespanless scope. (§4.6.)
- [x] Confirm `force_close`/exception path still tears down attached lifespans
  (TG-unwind cancels the parked wait → `async with cm` exits via cancellation).

**Tests (this chunk):** design §11 "Lifespans":
- [x] setup-authorized / teardown-unauthorized; returns yielded value;
  lifespanless raises.
- [x] **no-deadlock drain, `move_on_after`-bounded** (regression that pins a
  standing `Lease[main]` in the parked lifespan fails loudly).
- [x] teardown on exception/force_close path; concurrent (non-LIFO) teardown
  of multiple lifespans.
- [x] **Green gate:** data-layer + lifespan tests pass.
- [x] Update overviews for `attach_lifespan_to_scope`.

---

## Chunk 2.5 — Flatten decompose/compose + tolerate duplicate derived inputs

**Goal.** A small, self-contained simplification of the decompose/compose
surface and the entry primitive, landed *before* the service cutover so chunk 3
(and chunk 4) build on the simpler shape. Pure data-layer change; same green/red
posture as chunk 2 (data-layer + lifespan green; service layer still red).

**Motivation.** The chunk-1 surface segregated a decomposed authorization into
`ScopeAuthorizationParts(key_lease, parent_leases: {FxtrScope: Lease},
extra_leases: {token: Lease})` and made `_do_open` *reject* an
`extra_derived_leases` token that collided with a parent. But every low-level
consumer (dispatch's `call_scope`, chunk 4's `concurrent_child_bridge`)
manipulates vias at the **token** level and would otherwise have to maintain a
scope↔token mapping and pre-dedup arg tokens against the parents. Flattening to a
`{token: Lease}` map and tolerating duplicates pushes that bookkeeping into one
place (entry) and deletes it from the call sites. (Design §3.2, §4.4, §7.)

**Moving parts (`data/entity_ref_scope.py`):**

- [x] **Public token accessors.** Add `scope_token` (→ the main derived token,
  i.e. `_token`) and `key_token` (→ `_key_ull.token`) as read-only properties,
  each raising a clear not-started error if the scope is inert. These are how
  callers index a decomposed `{token: Lease}` map.
- [x] **`decompose_authorization(lease) -> dict[LifetimeToken, Lease]`.** Return
  *all* vias as one flat token-keyed map (key via at `key_token`, each parent
  via at `parent.scope_token`, each extra via at its token) instead of the
  `ScopeAuthorizationParts` triple. Still consumes `lease` via
  `exchange_reverse`; on partial failure discard everything already split out.
- [x] **`compose_authorization(leases: Mapping[LifetimeToken, Lease]) ->
  Lease[main]`.** Take the flat map; validate its key set equals the scope's full
  derived-input set (`key_token` + parent tokens + recorded extra tokens) and
  raise (discarding all supplied leases) on mismatch; `exchange_forward` the lot.
- [x] **Delete the `ScopeAuthorizationParts` dataclass** and its imports/usages.
- [x] **`_do_open` tolerates duplicate derived inputs.** Replace the
  "extra tokens must be disjoint from parent tokens" *raise* with: when an
  `extra_derived_leases` token is already a parent token (or the freshly-minted
  key token), `.discard()` the redundant extra lease and skip it — do **not**
  add it to `_extra_derived_tokens`. The parent/key keeps its existing treatment
  (parents get the transitive ancestor walk; the key stays the key). Keep the
  per-direct-parent coverage check on `parent_leases` strict (still exactly one
  lease per parent). The dedup must happen before `_extra_derived_tokens` is
  recorded so decompose/compose continue to round-trip.
- [x] `open_with_explicit_leases` / `lowlevel_start_lifespanless` signatures are
  unchanged (`parent_leases` stays scope-keyed; only `extra_derived_leases`
  gains the duplicate tolerance via `_do_open`).

**Tests (`v2/tests`):**

- [x] Update the two `test_authorization.py` round-trip tests off
  `ScopeAuthorizationParts` / `.parent_leases` / `.key_lease` / `.extra_leases`
  to the flat `{token: Lease}` map (index via `scope_token` / `key_token` /
  `parent.scope_token`); keep the mismatched-token-set rejection assertion.
- [x] Add a **duplicate-tolerance** test: `open_with_explicit_leases` /
  `lowlevel_start_lifespanless` with an `extra_derived_leases` entry whose token
  is already a parent (and one whose token is the active scope's token) opens
  cleanly, leaks no lease (autouse leak fixture), and the token is not
  double-counted in a subsequent decompose.
- [x] **Green gate:** data-layer + lifespan suites pass (`uv run pytest -n auto`
  over the chunk-1/2 green set).
- [x] Update `data/LOCAL_CODE_OVERVIEW.md` (+ root `CODE_OVERVIEW.md` if the
  decompose/compose shape is described there) for the flat map + new accessors.

---

## Chunk 3 — Service-layer cutover (`services/`) + remove transitional surface

**Goal.** Move service mount/dispatch onto lifespans + the new auth model;
remove `_main_lease`/`_key_lease` references and the `mountable` gating.
Everything green.

> ### Pre-spike: lease-translation + proxy-side authorization (do FIRST)
>
> An audit of `service_cap_common.py` shows the `_main_lease`/`_key_lease`
> removal splits into a mechanical majority and **one real design decision**.
>
> **Mechanical (low risk) — confirmed by reading the code:**
> - `home_scope._main_lease.subject` / `call_scope._main_lease.subject`
>   (~1946, ~2300, ~2523) are pure **token reads** → `._token`. No auth.
> - The **chain-converter core** — `_ChainConverterHandle`,
>   `_make_chain_converter_handle`, `_validate_case3_inputs` — is built
>   entirely on `_derived` / `_key_ull.token` / `_cap_bridges` / `_token`,
>   all of which survive. **No changes needed** (the part I'd feared, and it's
>   fine).
> - Server-side `_emit_call_result_packet` runs in the dispatch task where
>   `call_scope` is authorized: `call_scope._main_lease.clone()` →
>   `call_scope._current_auth_lease().clone()`; `call_scope._key_lease.clone()`
>   → `call_scope._derived.subclone(call_scope._current_auth_lease(),
>   k_call_token)` (k_call_token is still `call_scope._key_ull.token`). Add a
>   `scope._current_key_lease()` helper to centralize the subclone.
>
> **Proxy/client-side authorization — spike RESULT (resolved; also mechanical).**
> `invoke_service_method` → `ServiceProxyContext.mint_lease_for_key` /
> `fork_arena_handle_for_ids` (already migrated in chunk 1: each calls
> `check_authorized()` then `_current_auth_lease()`), and
> `_install_returned_resources`'s arena attach (~2541/2544/2593), all need
> `Lease[home_main]` → `home_scope._current_auth_lease().clone()`. The original
> worry (that this becomes a *new* per-context restriction) is **dissolved by
> the hybrid base model**: `_current_auth_lease()` returns the scope-level
> **base** — a plain attribute visible in any task while the scope is open — so
> a proxy call works from any context while `home_scope` is open, exactly like
> today's `home_scope._main_lease.clone()`. Not per-context. `ServiceProxyContext`
> holds only `(home_scope, proxy_handle, service_type_id)` — **no captured
> lease**, so the rejected "proxy pins home_scope → drain-hang" can't occur; the
> per-op clone is transient. Only behavior change vs today: a proxy call on a
> *draining/closed* home scope raises (correct). So proxy-side is a mechanical
> swap (`._main_lease.clone()` → `._current_auth_lease().clone()`,
> `._main_lease.subject` → `._token`).

- [x] **`CommonServiceImpl._mount_as_fxtr_service`** → `async def (self,
  scope)` (no `task_group`). Implement the framework mount lifespan (design
  §5.2): `await scope.attach_lifespan_to_scope(<framework cm>)` where the cm
  `async with self._fxtr_mount_lifespan(scope) as state: async with
  create_task_group() as dispatch_tg: build CommonServiceImplHandle(self,
  scope, dispatch_tg, state); register; yield ref`. (Composition with chunk 2:
  the `dispatch_tg` is nested inside the framework lifespan, which runs as a
  parked task in the mount scope's TG. On mount-scope `aclose` the token-drain
  step already waits for in-flight calls — they hold `Lease[mount.main]` — so by
  the time the closing event fires and the framework lifespan resumes, the
  `dispatch_tg` closes over already-drained calls, then user teardown runs, then
  the chunk-2 latch releases `aclose`. The drain-waits-for-in-flight-calls
  behavior is exactly the §1 motivation.)
- [x] **`_fxtr_mount_lifespan` (no-op default, yields None)**,
  **`_fxtr_get_lifespan_state()`**, **`_fxtr_get_mount_scope()`** on
  `CommonServiceImpl` (not the bare ABC). Add the single module-level
  `_CURRENT_DISPATCH_HANDLE` contextvar. (§5.1, §5.3.)
- [x] **`CommonServiceImplHandle`** holds `(impl, scope, dispatch_tg,
  lifespan_state)`; spawns dispatch into its own `dispatch_tg` (not the mount
  scope's TG).
- [x] **`_dispatch_body`** rewrite: build the per-call child `call_scope` via
  `open_with_explicit_leases(parent_leases={mount_scope: outer_lease},
  extra_derived_leases=<arg-ServiceRef leases>)` — the old `derived_from_leases`
  splits into the mount-scope *parent* lease plus the arg tokens as
  *extra* derived inputs (chunk 1's `extra_derived_leases`). **The mount-token
  collision is now handled by entry, not the caller** (chunk 2.5): if an arg
  ServiceRef rides on the mount scope's own token, just pass it in
  `extra_derived_leases` anyway — `open_with_explicit_leases` tolerates the
  duplicate and discards the redundant arg lease. The old code's explicit
  `if parent_scope._token in derived_from_leases: discard outer` dedup goes away
  entirely. Around `_handle_invoke`: set the **call_scope
  active** (`as_active_scope` — active during a call is the call_scope, not the
  mount scope, §5.3), re-authorize the mount scope (`with
  mount_scope.authorize(call_lease.clone())`), and bind `_CURRENT_DISPATCH_HANDLE`
  to the handle. Remove the `_with_derived_lifetime` branch and the
  draining-parent special-casing. (§5.3, design §7 "Dispatch reactivation".)
  - **Lease plumbing (confirmed):** clone the incoming `outer_lease`
    (`Lease[mount.main]`) twice — one clone consumed as the `{mount_scope: …}`
    parent input, one for the `mount_scope.authorize(…)` override around
    `_handle_invoke` (discarded after); the owned `outer_lease` is discarded
    once both clones are taken. The "per-call dispatch lease" / `call_lease` in
    §5.3 *is* this `outer_lease`.
  - **No captured-context replay exists to remove.** Design §5.3's "no
    `_dispatch_body_factory` context-override step / not a replay of the
    captured context" refers to an earlier idea that was never built:
    `_dispatch_body_factory` today is *only* a closure-builder for
    `task_group.start` (binds kwargs so `task_status` lands correctly), with no
    `copy_context`/`.run()`. Keep the wrapper as-is and just add the
    `_CURRENT_DISPATCH_HANDLE` bind; there is nothing to delete.
- [x] **Replace `call_scope._main_lease` / `_key_lease` + proxy-side
  `home_scope._main_lease`** sites per the pre-spike notes above (token-read
  swaps + server-side clones are mechanical; the chain-converter core is
  unchanged; proxy-side minting follows the chosen auth option).
- [x] **`FxtrScope.mount`** → `async`. **`_mount_as_fxtr_service` abstract
  signature** drops `task_group`. Update typed/untyped `_mount_as_fxtr_service`
  delegations.
- [x] **`FxtrServiceImpl`** (bare ABC) docstring: only `_mount_as_fxtr_service`;
  no `task_group`.
- [x] **`_check_mountable` → `_check_has_task_group`** (rejects lifespanless
  scopes) on `mount_lowlevel_cap_as_service` / `attach_bridged_lowlevel_cap` /
  `resolve_service`. *(Already done in chunk 1.)*
- [x] **Internal `.mount(` callers → `await`** (`mount` is async now):
  `subscription_util` (mounts unsubscribe handles), `rpc/scope_bridge`, and any
  others in-package; promote enclosing helpers to `async` as needed.
- [x] **`_current_key_lease()` helper** on `FxtrScope` to centralize the
  server-side key-lease recovery (`derived.subclone(_current_auth_lease(),
  _key_ull.token)`) for the two `call_scope._key_lease` sites in
  `_emit_call_result_packet`.
- [x] **Strip `mountable=`/`autostart=`** from the service layer + remaining
  chunk-3 test suites (`test_services`, `test_rpc`, `test_cap_ir_arena`), as a
  separate mechanical commit. After this no `mountable=`/`autostart=` remains
  in `v2/`.

**Tests (this chunk):** design §11 "Service mount + lifespan", "Dispatch
reactivation":
- [x] migrate `test_services/*` to `await scope.mount(...)`; add
  lifespan-state retrieval, `_fxtr_get_mount_scope` use-persists-across-calls,
  accessors-raise-outside-dispatch, **multi-mount isolation**,
  teardown-after-in-flight-calls, active-scope asymmetry, no-`_fxtr_mount_lifespan`
  impl.
- [x] port the resolve-into-mount-scope drain-cycle acceptance test; call
  completes after mount-scope body exits while a caller holds a lease.
- [x] **Green gate:** full suite (`uv run pytest -n auto`).
- [x] Update `services/LOCAL_CODE_OVERVIEW.md` + `CODE_OVERVIEW.md`.

### Chunk 3 — findings / running log

- **Real bug found in chunk-2 `aclose` (fixed here).** The chunk-2 structured-
  teardown wait set `_closing_event` *after* `await derived.aclose()` (the drain),
  so a **cancelled** `aclose` (e.g. `SubscriberHub`'s
  `with move_on_after(grace): await sub_scope.aclose()`) skipped setting the
  event. The parked framework-lifespan task then never unparked, and the
  enclosing `async with scope:`'s task-group exit hung forever awaiting it. Fix:
  set `_closing_event` in `aclose`'s `finally` so a cancelled/forceful close
  still unparks lifespans (mirrors `force_close`). Surfaced by `test_subscription_util`
  (4 tests hung); lifespan suite stays green after the fix.
- **`TypedServiceImpl` also overrides `_mount_as_fxtr_service`** (not just the
  ABC + `CommonServiceImpl`, as the initial audit assumed) — migrated to
  `async def (self, scope)` + `await super()...`.
- **Handle field naming.** `IRHandleLocalImplBase`'s base dispatch path spawns
  into `self._task_group`, so the handle stores its own dispatch TG as
  `_task_group` (not a new `_dispatch_tg` name); both the base `request()` and
  our `_run_request_async` spawn into it.
- **`_dispatch_body_factory` kept** as a pure arg-binder for `task_group.start`
  (no captured-context replay ever existed — see chunk-3 dispatch note above);
  only the `_CURRENT_DISPATCH_HANDLE` bind was added around the invoke.
- **Status at pause:** chunk-3 production code complete and pyright-clean; the
  full data + service + rpc + cap_ir + scope suites are green **except** one
  acceptance test, `test_cap_ir_arena.py::TestThreeTaskAcceptanceCapIR::
  test_three_tasks_drain_in_lockstep` (922 passing, 1 failing). That test's
  internal `_ExchangeForwardConverter` + case-3 attach converters assume scope2's
  derived inputs are `{key, t1_token}`, but under the uniform-derived model
  `GLOBAL_SCOPE` (in `include=`) is now also a derived input. Port mirrors
  `test_derived_scope_acceptance` (chunk 1): thread `GLOBAL_SCOPE.scope_token`
  through the converter's `x_tokens`/forward inputs and discard the GLOBAL via in
  the reverse/attach converters. **Still TODO**, along with the new §11 coverage
  tests (multi-mount isolation, accessors-outside-dispatch, active-scope
  asymmetry, teardown-after-in-flight, no-`_fxtr_mount_lifespan` impl) and the
  overview updates.

---

## Chunk 4 — `keepalive_via` on `bridge_service_from_scope`

- [x] Add required `keepalive_via: Literal["independent_lease",
  "concurrent_child_bridge"]`. `independent_lease` = today's behavior with the
  arg now explicit. (§6.)
- [x] `concurrent_child_bridge` (scoped narrow — restriction confirmed): require
  **dest = direct parent of source**, **`ref` mounted on source itself**
  (`ref.handle.token is source_scope.scope_token`), and **source has no extra
  (non-parent) derived inputs** (`source_scope._extra_derived_tokens` empty —
  their descendant-of-dest safety can't be checked from bare tokens). Build a
  `DependentLocalBridge` on dest for `source.main`: stored leases = source's key
  via + each non-dest parent via, recovered **non-consuming** via
  `source._derived.subclone(source._current_auth_lease(), tok)` (only source need
  be authorized); converter `source._derived.exchange_forward({dest.main: base,
  **stored})`, `base = Lease[dest.main]` supplied per call by the existing
  `_mint_lease_via_ancestor_bridge` machinery. Reject when dest is not a direct
  parent, ref isn't mounted on source, source has extra derived inputs, or any
  non-dest source-parent is a descendant of dest (`dest.included_in_scope(P)`).
  Add a **TODO** on the method to consider generalizing to refs attached via a
  `DependentLocalBridge` (nested ancestor bridging) — needs a token→descendant
  check (leases-layer infra) for the inherited stored vias.
- [x] Update `ServiceRef.bridge_from_scope` to thread `keepalive_via`.

**Tests:** design §11 "`keepalive_via` bridging" — both-close-together
no-deadlock, the two guard rejections, required-arg, only-source-authorized,
`independent_lease` parity. Update existing `test_bridge_service_from_scope`
to pass the new arg.

- [x] Migrated existing bridge callers (`subscription_util`, `scope_bridge`'s
  noop_host is unaffected; `test_*`) to explicit `keepalive_via="independent_lease"`.
- [x] Added `TestConcurrentChildBridge`: closes-together + callable (concurrent
  tasks), separate-mount-parent shape, the four guard rejections, required-arg.
- [x] **Green gate:** full suite (`uv run pytest -n auto`) — 2460 passed.

### Chunk 4 — findings / contract clarification

- **The keepalive contract is about *block nesting* (async-task `async with`
  nesting), not the scope-parent relationship.** The unifying rule: ``source``
  must never be forced to finish closing *before* ``dest``. So:
  - `independent_lease`: dest holds a standing `Lease[source.main]` → source
    closes *after* dest → source must **not** be block-nested *inside* dest
    (outside, or concurrent, is fine). The design's earlier "not structurally
    *outside*" wording was backwards (fixed → "*inside*").
  - `concurrent_child_bridge`: dest holds source's *key* via (no standing
    `Lease[source.main]`), so source can't self-drain — it closes only via the
    dest→source cascade. Source is a scope-parent *descendant* of dest, so it
    can't be nested outside dest and must not be nested inside dest → the two
    **must drain in concurrent tasks**. This (block nesting) is a runtime
    property and is **not** statically checked; only the scope-parent / mounted-
    on-source / no-extra-derived preconditions are. The happy-path tests use a
    concurrent-task helper; nesting them in one task self-deadlocks (by design).
- Derived-output tokens end on **input-end** (or revoke), not on last-output-
  lease-drop — that's why a scope drains via its *key* ending, and why holding
  source's key on dest ties source's drain to the dest cascade.

---

## Chunk 5 — Cleanup + docs

(`mountable=`/`autostart=` are already gone from `v2/` after chunks 1+3 —
nothing left to strip here. Cross-repo handled separately.)

- [x] Final pass over `CODE_OVERVIEW.md`, `data/LOCAL_CODE_OVERVIEW.md`,
  `services/LOCAL_CODE_OVERVIEW.md`, `leases/LOCAL_CODE_OVERVIEW.md` — ensure
  they describe the *current* state (no chunk/stage references in long-lived
  docs).
- [x] Confirm `_assert_no_lease_leaks` clean across the full suite (2460 passed).
- [x] New note `notes/scope_service_lifetimes.md` — usage guide for mounting
  services, lifespans, and moving services between scopes (lifting + the two
  bridging patterns); referenced from the bridge method docstring and the
  data/services overviews.
- [x] Cleaned stale `_main_lease` / `_with_derived_lifetime` references from
  internal docstrings/comments (renamed to `_scope_auth_lease` /
  `open_with_explicit_leases`; the conceptual `our_main_lease` converter term and
  test-lineage docstrings left as-is).

---

## Chunk 6 — Base-floor authorization + explicit discard handles (design Revision R1)

**Motivation (post-cross-repo).** See design **Revision R1** and
[`cross_repo_plan.md`](./cross_repo_plan.md) → "Context-copy hazard (design
§11)". The migration surfaced that making a per-context override *take
precedence* over the base — with a discarded override a hard error and **no
fall-through to the base** — breaks `start_soon`/`tg.start`-from-dispatch: the
spawned task inherits the dispatch's transient mount-scope override via
context-copy and crashes once dispatch discards it, **even though the base is
still live**. It also makes `discard_authorization` ambiguous (and, under the
fix, self-contradictory — discarding an override while the base is live leaves
you authorized). R1 makes the **base a floor** (overrides *union*, consulted
only when the base is gone) and splits discard into an explicit
`AuthorizationHandle.discard()` (override) + `discard_base_authorization()` (base).

**Green/red posture.** Pure data-layer change in `data/entity_ref_scope.py` +
test updates. The service/rpc layers are **API-unaffected** — dispatch still
installs its override (needed for the draining-mid-call case); only the
*resolution preference* flips to base-first. Full suite green at the end.

**Moving parts (`data/entity_ref_scope.py`):**

- [x] **Base-first resolution.** Rewrote `_current_auth_lease()` to: token
  `None` → not-started; **base set & active → return base**; else override
  present & active → return it; override present but dead → raise
  discarded-override; else not-authorized. `is_authorized` / `check_authorized`
  delegate unchanged. Updated the module-level §2 doc comments + the
  `_AUTH_OVERRIDES` comment + method docstrings to "base-first / union."
- [x] **`AuthorizationHandle`** (new module-level class, before `FxtrScope`):
  holds the taken (`_lease`) lease + a `_discarded` flag; `.discard()` is
  idempotent, `is_discarded` property. `authorize` builds one, `yield`s it, and
  its `finally` calls `handle.discard()` (idempotent) then
  `_AUTH_OVERRIDES.reset(token)`. (Double-discard already relied on.)
- [x] **Split discard.** Renamed `discard_authorization` →
  `discard_base_authorization`; body = base-only. Migrated `_run_lifespan` →
  `with self.authorize(setup_lease) as auth: … auth.discard()`; the two
  lowlevel-teardown docstrings → `discard_base_authorization`.
- [x] **Confirmed dispatch (§5.3) untouched** — `service_cap_common.py` uses
  `with mount_scope.authorize(reauth_clone), …:` (no `as`), unaffected.

**Tests (`v2/tests`):**

- [x] **Flipped the context-copy-hazard test**
  (`test_inherited_override_after_origin_discard_uses_live_base`): the spawned
  subtask now **succeeds via the live base**. Added the negative
  (`test_dead_override_raises_only_when_base_also_gone`): base *also* gone
  (draining) → still raises discarded-override.
- [x] Renamed/rewrote `test_override_takes_precedence_then_no_fallback` →
  `test_discarded_override_falls_through_to_live_base` (base-floor). `is_authorized`
  True under a live base in any context.
- [x] Discard tests: `auth.discard()` (handle) vs `discard_base_authorization()`;
  lifespan runner still parks holding no `Lease[main]` and drains (existing
  `move_on_after` no-deadlock regression in `test_service_lifespan` stays green).
- [x] Grepped `discard_authorization(` across `v2/` → none remain.
- [x] **Green gate:** full suite `uv run pytest -n auto` → 2464 passed; pyright
  (v2) + black + isort clean via `jj pre`.
- [x] Updated `data/LOCAL_CODE_OVERVIEW.md` + root `CODE_OVERVIEW.md` auth-model
  description (base-first / handle / `discard_base_authorization`).

**Cross-repo follow-up:**

- [x] Reverted the `fxtr_workstation_agent_plugin` context-copy workaround (the
  `clone_authorization()`/`authorize()` dance in `test_callback_e2e.py` +
  `examples/.../run_demo_workstation.py`) — base-floor makes it unnecessary. The
  16 `test_callback_e2e` + `test_stage7_buffer_e2e` tests pass **without** it,
  confirming base-floor fixes the original breakage. Full mcp suite green
  serially (252 passed, 1 skip; `test_in_memory_e2e_nix_set_preset` is
  flaky-under-`-n auto` nix-build contention — passes serially in 37s, same
  pattern as the sandboxing nix tests).

---

## Notes / risks to watch

- **Lease-leak discipline.** The autouse leak fixture will catch any auth
  lease / key lease / setup lease not discarded on the new paths — expect to
  iterate on discard placement in `authorize` / `_run_lifespan` / dispatch.
- **Context-copy hazard** (design §11): originally a *documented* behavior
  pinned by a test. **Superseded by Chunk 6 / Revision R1** — base-floor
  resolution makes the inherited-override-after-discard case *succeed* via the
  live base, so the hazard no longer fires while the scope is open. The test is
  flipped accordingly in Chunk 6.
- **`provenance_context_from_scope` at entry** now requires the source scope
  authorized at entry — verify the provenance suites still pass post-move.
- **Update CODE_OVERVIEW.md** is the final step (and intermediate overview
  updates land within chunks 1, 2, 3 as noted above).
