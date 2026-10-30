# Stage 12 post 1 — the per-call input scope

Split the service-dispatch call scope in two: a lifespanless **input
scope** that owns the call's wire inputs (arg leases + the attached
input arena), and the call scope proper, which now derives from
`{mount, input}` instead of carrying the inputs itself. Impls gain
`_fxtr_get_input_scope()` — a per-call scope they can retain, store
into, or bridge through **without pinning the mount token**.

Scoped after the ws part6 stage-3 demo port hit the retained-lease
deadlock (see
`projects/fxtr_workstation/plans/phase0/part6_adjustments/stage3_demo_refactor.md`,
Risks): any live `Lease[call_scope.main]` carries `Lease[mount.main]`
as a via — that is what makes mount drain wait for in-flight calls —
so retaining one past the dispatch pins the mount against the very
drain that runs the teardown that would release it. Today the only
escape is copying into a separately-owned scope. The input scope is
the sanctioned non-pinning residence.

Design discussion record: conversation with Daniel, 2026-06-10
(option "two scopes" chosen over "call scopes don't derive from mount
at all", which would lose drain-waits-for-in-flight-calls and break
the common case).

## Design

### D1 — two scopes per dispatch

`_dispatch_body` currently opens one scope:

```python
call_scope = FxtrScope(isolated=True, include=[mount_scope])
async with call_scope.open_with_explicit_leases(
        parent_leases={mount_scope: parent_clone},
        extra_derived_leases=input_leases):   # the packet's arg leases
    ...
```

New shape — the arg leases move to a lifespanless input scope, and the
call scope gains it as a second parent:

```python
input_scope = FxtrScope(isolated=True, include=[...], name=".../input")
input_scope.lowlevel_start_lifespanless(extra_derived_leases=input_leases)
try:
    call_scope = FxtrScope(isolated=True, include=[mount_scope, input_scope])
    async with call_scope.open_with_explicit_leases(
            parent_leases={mount_scope: parent_clone,
                           input_scope: input_scope.clone_authorization()}):
        ...  # arena attach + decode now target input_scope; user method
             # runs with call_scope active, exactly as today
finally:
    input_scope.discard_base_authorization()
```

- **Lifespanless is load-bearing**: the input scope must not be a CM
  in the dispatch task — a retained lease would park the task in the
  drain wait, and those tasks live on the framework lifespan's
  dispatch TG, whose close is part of mount teardown (the same
  deadlock one level up). `lowlevel_start_lifespanless` +
  `discard_base_authorization` is the "closes when the last lease
  releases — nobody waits" shape (§4.5 of the design doc).
- The incoming `fxtr_arena` is `lazily_attach_from_arena`d against the
  **input scope's** arena, and `input_scope._derived` provides the
  case-3 Lease[scope] → Lease[arg-token] hook. The call scope reaches
  args through its parent entry on the input scope.
- Lifetime equivalence for the common case: today the call token holds
  the arg leases directly; now it holds `Lease[input.main]`, which
  holds them — args stay valid for exactly the call's duration, and an
  unretained input scope closes the moment the call token ends.
- The mount-side structure is untouched: the call scope still holds
  `Lease[mount.main]` (drain still waits for in-flight calls), the
  per-call mount re-auth and the dispatch contextvar binding are
  unchanged in mechanism (the var is renamed — see D2).

### D2 — `_fxtr_get_input_scope()`

New accessor on `CommonServiceImpl`, beside `_fxtr_get_mount_scope` /
`_fxtr_get_lifespan_state`. The input scope is **per-call** while the
handle is per-mount, so the framework contextvar's bound value becomes
a small per-call record `(handle, input_scope)` — and the var is
renamed `_CURRENT_DISPATCH_HANDLE` → `_CURRENT_DISPATCH_CONTEXT` to
match (still one framework contextvar; the existing accessors read the
handle field). No re-auth
machinery needed: the input scope's base is live for the whole call,
so any context is authorized for it; a caller that wants it past the
call retains `clone_authorization()` (and re-authorizes later via
`authorize`, the standard stage-2 pattern).

What an impl can do with it:

- `clone_authorization()` and stash → the call's args (and anything
  the impl `.store()`s / `copy_from_scope`s into it during the call)
  stay loadable after the dispatch, drained-but-open, **without
  pinning the mount**. Publish-origin / seed-origin use cases plug in
  directly. **Caveat (found by the attempted scheduler migration,
  ws part6 stage 3 follow-up)**: the retention pins the call's
  *sender*, and if that same caller also holds a *dependent return*
  from this service across teardown (its chain bridge captures an
  at-rest `Lease[mount.main]`, released at caller close), the two
  pins form a teardown cycle — caller-drain → retention →
  impl-teardown → mount-end → caller-close. A service whose callers
  routinely hold its returns should retain via *copies into an
  impl-owned scope* instead (which pins nobody); input-scope
  retention fits when the retained data flows only to third parties
  (e.g. subscribers) rather than back to the arg-sending caller.
- Keep the call's arg `ServiceRef`s alive past the dispatch (their
  `ServiceKey` registrations live on the input scope, so retained
  seeds/snapshots can re-encode them). *(Installing fresh bridges on
  the input scope remains TG-gated — implemented scope is arg-ref
  registration + entity residency; revisit if a consumer needs
  general bridge-out.)*
- **Limitation (documented, accepted)**: lifespanless ⇒ no task group
  ⇒ the input scope cannot mount or resolve services. Service-valued
  work still happens in the call scope (or an impl-owned scope);
  entity residency is the input scope's job.

**Supported composition (not built here): fully zero-copy retention.**
An impl that wants results born outside the call scope can open its
own TG-bearing scope **parented on the input scope only** (a holder
task on its lifespan tg, `include=[input_scope]` via
`open_with_explicit_leases` with a `clone_authorization()` of the
input scope), resolve arg `ServiceRef`s into it (the
"child-including-both" pattern), and dispatch there — returns then
land in that scope, zero copies, still no mount pin. The price is the
full pin-the-origins semantics: the retained tree pins the callers'
arg exports (via the input scope) *and* the arg services' reply scopes
(chain-inherited return claims — the services can't dispose of their
outputs, and later reads are chain loads against them). Copying
results out instead decouples those lifetimes (the producing service
GCs as soon as its dispatch drains), which is why e.g. the demo
scheduler prefers copy-out for outputs while taking inputs zero-copy.
No extra mechanics beyond this plan are needed for the pattern — only
the transitive-arg-access verify item below covers it.

### D3 — what retention pins instead

A retained input-scope lease vias the **callers' arg-export tokens**
(the wire-input leases are its derived inputs): the senders' export
records stay drained-but-open until the retention is released. That is
the principled trade — "a snapshot pins its origin until superseded",
the same idea as the hub's per-event origin leases, extended in
duration — but it must be documented as the contract: *retaining the
input scope keeps the data's senders pinned; releasing it (or the
senders' tokens breaking, e.g. connection death) ends it.*

### D4 — docs guardrail

Describe the actual consequence rather than a rule, at the places
people will look (the call-scope/§5.3 prose, `clone_authorization`'s
docstring, the `CommonServiceImpl` accessor docstrings): anything
mounted on / inherited from a per-call scope holds `Lease[mount.main]`
via the call token, so the impl's mount cannot finish draining until
it is released — retain past the dispatch only if you own a matching
release path (else you deadlock teardown); `_fxtr_get_input_scope()`
is the non-pinning alternative when what you need is the call's
*data*. Users who genuinely need mount-scope access past the call can
still do it knowingly.

## Out of scope

- Hot-path cost of the second scope open per dispatch: accepted for
  now (uniform "always create"); investigate only if it shows up.
- Consumer migrations. The scheduler-retention migration is planned
  separately in the ws stage-3 doc (follow-up section); the workstation
  parker / hub internals don't need it.

## Implementation chunks

### Chunk 1 — dispatch split + accessor

- [x] `service_cap_common.py`: `_dispatch_body` per D1 (input scope
      lifespanless around the call scope; arena attach + case-3 hook
      move to the input scope; `_run_user_method` learns the input
      scope); contextvar value becomes the per-call record + rename
      `_CURRENT_DISPATCH_HANDLE` → `_CURRENT_DISPATCH_CONTEXT`; add
      `_fxtr_get_input_scope()`. (Plus the chain-converter two-hop
      redesign + base-discard ordering — see Implementation notes;
      and the `bridge=None` guard relaxation in
      `attach_bridged_lowlevel_cap`.)
- [x] D4 docstrings (`_dispatch_body`, the accessors,
      `clone_authorization`).

### Chunk 2 — tests (`tests/test_services/`)

- [x] Common-case equivalence: `tests/test_services/` +
      `tests/test_rpc/` fully green (581) — the spawner-pattern
      `same_scope` layouts were the regression surface that caught
      both chain-converter deadlocks.
- [x] Retention (the headline): an impl retains
      `_fxtr_get_input_scope().clone_authorization()`; after the
      dispatch ends, arg entities load via the retained lease
      (re-authorized), **and the mount drains + runs teardown with the
      retention still outstanding**
      (`tests/test_services/test_input_scope.py`).
- [x] Caller-pin contract: the retained input scope keeps the caller's
      work scope drained-but-open; releasing the retention lets it
      end. Caller force-close (broken-input) path: the input scope
      unwinds via the cascade rather than leaking.
- [x] Unretained input scope closes with the call token (lease-leak
      fixture covers the bookkeeping); `_fxtr_get_input_scope()`
      outside a dispatch raises; active scope ≠ input scope inside a
      call.
- [x] Store-into the input scope during the call; survives the
      dispatch under retention without pinning the mount. (Bridge-out
      *of new resources* via the input scope still requires the
      bridge-installing `attach` path, which stays TG-gated —
      arg-ref re-registration is the supported lifespanless form.)
- [x] Provenance/marker: covered by the green `test_provenance_flow`
      / typed+untyped suites (decode now targets the
      GLOBAL-parented input scope; no marker-inherit regressions).

### Chunk 3 — docs + ripple

- [x] `new_scope_design.md` §5.3 + §7 updated in place (done together
      with this plan; re-verified post-implementation — matches the
      built shape incl. the two-hop converter and the GLOBAL parent).
- [x] Cross-repo suites re-run (the dispatch path is shared):
      fxtr_workstation (376 ✓ — after the lift-at-decode fix its
      persistence suite was the canary), fxtr_sandboxing (118 ✓; the
      one podman container failure passes in isolation — `-n auto`
      load flakiness), demo tests (8 ✓), agent plugin
      (244 ✓ / 3 fail + 10 errors under `-n auto`, with the failing
      files passing serially — the documented load sensitivity, plus
      a pre-existing post-test cleanup deadlock Daniel has seen
      before wedged one verification rerun entirely; not a
      regression).
- [x] Update `v2/CODE_OVERVIEW.md` +
      `v2/fxtr2/services/LOCAL_CODE_OVERVIEW.md` (dispatch pair,
      accessors/contextvar, wire-shapes receiver path).

## Implementation notes (running log)

- **Minimal `include=` resolved: `[GLOBAL_SCOPE]`** (the stage-2
  answer, for the same reason): a parentless input scope can't lift
  type descriptors, so `store()`-into fails. GLOBAL is eternal, so the
  parent lease pins nothing that can drain; the chain handle holds an
  at-rest GLOBAL authorization beside the key (same harmlessness) and
  both stay out of the shipped `x_tokens`.
- **`attach_bridged_lowlevel_cap` guard relaxed for `bridge=None`**:
  registering an arg `ServiceKey` on an existing case-3 ancestor entry
  is pure bookkeeping, so it is now allowed on started lifespanless
  scopes (the input scope registers its arg refs this way); the
  task-group guard stays for the bridge-installing branch, and
  mount/resolve remain TG-gated.
- **Chain-converter returns needed a real redesign, twice.**
  - First attempt (handle holds an at-rest `Lease[input.main]`)
    deadlocked the `same_scope` layouts: with the caller == mount, the
    at-rest lease's vias transitively pinned the scope draining at CM
    exit. Replaced with the **two-hop re-mint**: the handle holds the
    input scope's *key* lease (keys pin nothing) and re-mints
    `Lease[input.main]` per convert from key + the caller's arg
    leases, then feeds it into the call-scope exchange. Net effect:
    the shipped `x_tokens` set and every standing pin land exactly
    where the pre-input-scope design put them (caller-side, where
    `_UseBaseSpec` dodges own-token self-pins).
  - Second deadlock, also `same_scope`: the input scope's **base**
    discard originally sat after the call-scope CM exit — but that
    exit legitimately parks while the caller holds returned dependent
    refs (their wire `K_call` leases; pre-existing behavior), and the
    base's vias on the callers' tokens then blocked the caller's own
    drain. Moved `discard_base_authorization()` *inside* the call
    body's `finally` (right after the response send). General lesson,
    twice over: **derived tokens hold their inputs only through vias
    inside live output leases** — audit every standing lease's via
    chain, not just its subject.

- **Args must still be *directly* tracked in the call scope** (found
  by `fxtr_workstation`'s persistence suite): impls legitimately hand
  arg ids to strict same-scope ops (`create_u_node`,
  `extract_provenance_parent`), which check the scope's own arena
  rather than walking parents. The receiver-side decode now passes
  `lift_into=call_scope` to `_decode_fxtr_service_data`: every decoded
  FxtrID (data half + provenance parent, for layered ids) gets a sync
  `lift` into the call scope — lazy inheritance entries through the
  input-scope parent, no data copies. (The decoder's `fxtr_ids`
  out-set alone was not enough: for layered leaves it collects the
  *prov node* only, by the encoder-symmetric rule.)

## Risks / things to verify

- **Transitive arg access**: loads/encodes from the call scope reach
  arg tokens via call → (parent) input scope → (case-3) arg token —
  two exchange hops where today there is one. The uniform derived
  token design says this composes; verify under the RPC suites, not
  just locally.
- **Marker-inherit on the markerless input scope**: the
  `track_from_memory_arena` incident class — make sure decoding into
  a scope with no mount parentage doesn't re-trip provenance-closure
  walks (chunk-2 test).
- **Minimal `include=` for the input scope**: parentless vs
  `[GLOBAL_SCOPE]` (type descriptors) — same verification stage 2 did
  for the hub's sub-scopes.
- **Return-path encode**: results that reference args must still
  encode from the call scope (reachability via the parent entry);
  covered by existing suites if the hop works.
- **Force-close sweep**: the cancelled-RPC-connection teardown must
  cover started-lifespanless input scopes (no CM/task owns them).
