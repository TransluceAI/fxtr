# Stage 12 — Better Scope: new `FxtrScope` lifetime design

This document settles the redesign of `FxtrScope`'s fundamental lifetime
patterns. It is a **design spec**, not an implementation plan — the
chunked implementation plan and the use-site migration plan live in
sibling files (`impl_plan.md`, `usage_migration.md`, to be written next).

The starting point was [`sketch.md`](./sketch.md); this doc expands it into
a concrete conceptual model, a full inventory of the post-change public
API, and the abstract migration story. Where the conclusions diverge from
the sketch, the text here wins (the sketch is left as the historical
seed).

---

## 1. Motivation

Today a scope is shaped by three near-orthogonal axes (`isolated`,
`mountable`, ULL-vs-`_with_derived_lifetime`) plus a three-boolean
lifecycle (`_started` / `_draining` / `_closed`). The scope owns a single
`_main_lease` **instance attribute** that every operation clones, which
means the scope is usable from any task as long as it is alive, and there
are several confusingly-overlapping entry points (auto-start, CM entry,
the internal derived constructor, bare `aclose`).

The concrete pain that motivates the change: **once a scope drains, code
that was mounted into it loses all access to it.** In RPC flows a service
is mounted into a scope that then drains while the service is still
expected to handle calls; the dispatch machinery works around this with a
bespoke "derive a child from the draining parent using the outer dispatch
lease" path that is hard to reason about and impossible to reproduce from
outside the framework. There is no public way for a long-running service to
hold resources whose lifetime is tied to its mount scope, nor to re-enter
its mount scope during a call.

**Goal:** a single, well-defined public lifetime model in which (a) holding
a lease lets you (re)authorize and use a scope from any context while its
token is alive, independent of the owning context's drain state, and (b)
services can attach lifespan-managed resources to their mount scope and be
re-authorized against it during calls.

---

## 2. Conceptual model: four lifespan states

A scope is described by four states. Three are **per-scope** (single facts
about the scope object — `open`, `within-lifespan`, and the *base*
authorization); one has a **per-context** overlay (`active`, plus per-context
authorization *overrides* layered over the base).

- **open** — *per-scope.* The scope's token is alive; its resources can be
  used by anyone holding a lease. A scope starts **not** open, becomes open
  exactly once (on entry), and closes exactly once, never to reopen.

- **within-lifespan** — *per-scope.* The scope object owns an `anyio` task
  group plus a "closing" event, and can enter *new* lifetime context
  managers on its own behalf (`attach_lifespan_to_scope`); attached CMs are
  guaranteed to either (a) tear down *after* the token ends, or (b) revoke
  the token if an exception forces an early teardown. CM-entered scopes are
  within-lifespan; the lowlevel lifespanless route (§4.5, used by the root
  scope) is not. A single *controlling* context — the one that entered the
  CM — owns teardown, but the property "this scope has a lifespan" is a fact
  about the scope, not about the calling context.

- **authorized** — base is *per-scope*; overrides are *per-context.*
  Authorization comes in two layers:

  - **Base (scope-level).** While open, the scope holds its own `Lease[main]`
    as a plain instance attribute (`_scope_auth_lease` — essentially today's
    `_main_lease`, retained). Anyone in any task/context can use the scope
    while this is set; it is set at entry and dropped (→ `None`) at `aclose`.
    There is **no per-context bookkeeping and no copy on ordinary scope
    entry/exit** — just an attribute write. This is the common path and is
    deliberately close to today's "usable from any task while alive."
  - **Override (per-context).** A `with scope.authorize(lease) as auth:` block
    binds a context-local override in a single process-wide `ContextVar` holding
    a copy-on-write `{scope: Lease[main]}` map. The override **unions** with the
    base, it does not replace it: it is what lets you (re)authorize a scope from
    a context that holds a lease **when the base is gone** (a draining scope; a
    cross-task / opaque-lease caller; the dispatch re-auth of a mount scope,
    §5.3). While the base is live the override is a no-op.

  `_current_auth_lease()` resolves **base-first**:

  ```
  if token is None:                   raise not-started
  if base set and active:             return base       # ← floor: scope open ⇒ authorized in ANY context
  if ctx override present and active: return override    # base gone (draining) + a live held lease
  if ctx override present and dead:   raise discarded-override   # base gone + you let your lease die
  raise not-authorized                                   # base gone, no override
  ```

  A live **base is the floor**: it authorizes any context, so a stale/dead
  override inherited by a spawned subtask via context-copy **cannot** revoke
  access the base still grants. The override is consulted **only when the base
  is `None`** (a draining-but-open scope) — the one case it is load-bearing
  (in-flight dispatch keeps working via its held lease; new ambient use is
  rejected). The *discarded-override* error therefore fires in exactly one
  situation: **base gone AND this context held an override AND let its lease
  die** — a genuine "used a draining scope without keeping it alive" bug.
  `is_authorized()` / `check_authorized()` follow the same base-first resolution.

  The flip side: you cannot force "use my override lease even though the base is
  live" via ambient resolution — `clone_authorization()` in a dispatch context
  returns a *base-gated* clone (lives with the scope), not a *call-gated* one
  (which is what a task outliving the call wants). Where lifetime-gating must be
  exact, use the explicit-lease paths (`open_with_explicit_leases`, the dispatch
  handle's call lease).

  (Design note: overrides use **one** shared module-level cv holding a small
  map — *not* one `ContextVar` per scope instance (stdlib "create ContextVars
  at module level" guidance; avoids churning a fresh cv on every per-call
  `call_scope`). The map is COW-updated **only by `authorize`** — ordinary
  nested scope entry/exit does *not* touch it — so the map holds an entry only
  per *active override* in this task, normally zero. The COW update copies a
  tiny dict of **lease references** (no `.clone()`). Because resolution is
  base-first, the hot read path is just the base attribute read — the cv-map is
  consulted **only when the base is `None`** (draining). If deep-nested
  *overrides* at volume ever show up in a perf sweep, swap the COW `dict` for an
  `immutables.Map` with no call-site changes.)

- **active** — *per-context, global.* The "current" scope in the task,
  stored in the single `ACTIVE_SCOPE` contextvar (≤1 at a time). The active
  scope is almost always authorized. Activation is a *pure contextvar set*
  (`as_active_scope`) — it does **not** authorize and does **not** extend
  lifetime.

### Relationship to today

| New state        | Closest current notion |
|------------------|------------------------|
| open             | `_started and not _closed and not token.ended()` |
| authorized (base)| `_main_lease` attribute, usable from any task (**kept**, renamed `_scope_auth_lease`) |
| authorized (override) | *new* — per-context re-authorization, unioned with the base (consulted only when base gone) |
| within-lifespan  | generalizes `mountable=True` (task group) to **every** CM-entered scope |
| active           | `ACTIVE_SCOPE` (unchanged) |

---

## 3. Lifetime structure

### 3.1 Uniform derived token

Every scope's token is a `DerivedLocalLifetime` whose inputs are
`{private key ULL token} ∪ {parent.main_token for each parent}`. When there
are no parents it is derived from just the key — we keep the structure
uniform rather than special-casing a bare `UnconditionalLocalLifetime`.
This retires the ULL-vs-derived split: `_with_derived_lifetime` and the
`_scope_ull` path both go away.

Consequences:

- **Child-keeps-parent-alive is the derived-via mechanism, uniformly.** A
  live `Lease[child.main]` carries a via on every `parent.main`; the
  ancestor `IndependentLocalBridge` clones that used to back default scopes
  are no longer needed for *parent* keepalive. (Ancestor bridge entries
  still exist for the **lift machinery** and for **external arena owners**
  registered via `_register_external_arena_bridge` — those are unchanged.)
- **A parent revoke cascades to children.** `parent.main` ending ends every
  `child.main` derived from it. This matches structured-concurrency intent.

### 3.2 The key is consumed into authorization

The scope does **not** retain a standalone key/mint handle. At entry the
scope mints its key ULL, builds its first authorization lease
(`Lease[main]`) via `derived.exchange_forward({key, <parent leases>})`,
**retains that lease as its base scope-level authorization**
(`_scope_auth_lease`, §2), and **discards its own hold on the bare key root
lease** — the key now lives only as a via inside authorization leases (the
base one and any clones / overrides).

Therefore:

- Holding an authorization lease == holding the bundled key + parent vias.
- You can split a key lease back out of any authorization lease with
  `derived.subclone(auth, key_token)` (non-consuming).
- To re-mint a *fresh independent* authorization you need both an existing
  authorization (to split the key out of) **and** the parents (to clone
  fresh parent leases) — i.e. you can grab a key lease from any authorized
  scope. The scope **retains the key ULL object** (`_key_ull`) so
  `force_close` can revoke the key — breaking any externally-split key leases
  (§7) — but **discards its standing root lease** at entry. So `_key_ull` is a
  *revoke* handle, not a *mint* handle (you split from an existing auth, never
  mint fresh from it), and `_key_ull.token` serves as the key token for
  splitting / identification. Storing the ULL object does not pin the token
  alive (only outstanding leases do), so drain stays natural.
- A **parentless** scope closes once its base authorization is dropped (at
  `aclose`) and no other lease clones remain — there's no dangling key
  keeping the derived output idle forever.

**Public decompose / compose / clone API.** The split-and-recombine machinery
above is exposed as a public surface so callers can build or take apart scope
authorization by hand — e.g. ship the component leases to another task and
reconstruct authorization there, or split the key out to gate teardown
independently. The decomposed form is a single **`{token: Lease}` map** keyed by
lifetime token (deliberately *not* segregated into key / parents / extras) — a
caller that needs a particular via looks it up by token, using the public
`scope.scope_token` (the main token), `scope.key_token` (the private key token),
or `parent.scope_token` accessors. Low-level call sites (dispatch, the
`concurrent_child_bridge`) manipulate vias at the token level anyway, so the flat
map is what they want.

- `scope.clone_authorization() -> Lease[main]` — a fresh clone of *this
  context's* current authorization lease (requires authorized). Hand it to
  `scope.authorize(...)` in another context, stash it, or pass it across a
  task boundary.
- `scope.decompose_authorization(lease) -> {token: Lease}` — consume a
  `Lease[main]` (via `derived.exchange_reverse`) and return **all** its
  constituent vias keyed by token: the key via (at `scope.key_token`), one
  per-parent via (at `parent.scope_token`), and one per non-parent derived input
  (§4.4). (Takes ownership of `lease`; a live `Lease[main]` always implies every
  via is live, so reverse is safe.)
- `scope.compose_authorization(leases: {token: Lease}) -> Lease[main]` — the
  inverse: recombine a `{token: Lease}` map back into a `Lease[main]` via
  `derived.exchange_forward`. The keys must match the scope's full derived-input
  set exactly — `key_token` + every parent's `scope_token` + every extra
  derived-input token — which `exchange_forward` enforces. Takes ownership of the
  supplied leases; requires the scope open.

To drive these, the scope uses `key_token` (from the retained key ULL object —
see above), `_derived`, its direct-parent set, and the non-parent derived-input
tokens recorded at entry. It holds no standing key *lease* (the bare key root is
discarded at entry, §3.2). These are pure plumbing over the uniform derived token
(§3.1): the same `exchange_forward` / `exchange_reverse` the entry path (§4.2) and
dispatch (§5.3) use, surfaced for external use.

### 3.3 Re-minting from parents requires authorized parents

Building a child's first authorization clones each parent's *in-context*
authorization lease (`parent._current_auth_lease()` — the parent's override
if one is set in this context, else the parent's base). So **every `include`d
parent (and `provenance_context_from_scope`) must be authorized in the context
where the child is entered.** Because base authorization is scope-level (§2),
this is automatic for any **open** parent; an explicit `authorize` /
`open_with_explicit_leases` is needed only when the parent's base is gone
(draining) or you hold only a lease:

- Non-isolated child: parent == active and open → base-authorized ✓
  (automatic).
- `include=[GLOBAL_SCOPE]` (or any open parent): the parent's base lease makes
  it authorized everywhere ✓.
- Child entered in a task spawned from the parent: the parent's base is
  scope-level (visible in any task), so an open parent is authorized there
  with no context copy needed ✓.
- Opaquely-passed / draining parent whose base is gone and you only hold a
  lease: `with parent.authorize(lease):` first (§4.3), or use
  `open_with_explicit_leases` (§4.4).

If the parent has *drained* (base dropped to `None`) before the child opens —
or an authorizing override was discarded — the child sees no authorization and
the entry raises.

### 3.4 The root / global scope

`GLOBAL_SCOPE` is started via `lowlevel_start_lifespanless()` (no task
group, no lifespan) wrapped by `_start_as_root_scope()`. Because base
authorization is just a scope-level attribute (§2), root needs **no special
casing**: `_start_as_root_scope()` mints the root token + a never-discarded
`Lease[main]` and stores it as root's `_scope_auth_lease`. Since the base
lease is a plain attribute (not a contextvar), it is visible in every
task/context, so root reads authorized everywhere with zero per-context setup
— preserving bare `entity.store()` from any task, exactly as today's
`_main_lease` did. There is no auth-map default to seed and no override entry
for root. The root token never drains until the `atexit` `force_close` hook
revokes it (via root's retained lifetime owner). The current one-liner
`GLOBAL_SCOPE = FxtrScope(...)._start_as_root_scope()` keeps working unchanged
(`_start_as_root_scope` still returns `self`).

### 3.5 Drain state from the base lease + token liveness

The persistent lifecycle facts a scope tracks are "has the token been created
yet?" (not-started errors, `aclose` short-circuit), "has the token ended?"
(closed errors, idempotency), and "is the base `_scope_auth_lease` still set?"
— all read directly. A scope is **draining** while its
token is still alive but the base lease has been dropped to `None`: `aclose`
drops the base **first** (a global attribute write, §2), then awaits the
token's drain.

`authorize(lease)` succeeds whenever the token is still alive (not ended),
**regardless of whether the base has been dropped**. "I hold a valid lease +
the token hasn't ended" is sufficient to operate. The drain-rejection behavior
falls out of this directly:

- Rejecting operations after the owner starts closing: `aclose` set the base
  to `None`, so `check_authorized` (token alive + this context resolves an
  active lease) fails for any context without an override — while in-flight
  calls / the spawner, which hold per-context overrides (§5.3), keep working.
  That is the point: base-gone rejects *new* ambient use everywhere, overrides
  keep the chosen contexts alive.
- Rejecting a draining parent at child construction: cloning the parent's
  in-context authorization lease (`parent._current_auth_lease()`) raises
  because the parent's base is `None` and there is no override.

So the redesign uses `check_authorized` (token alive + this context resolves
an active authorization), and the bespoke draining-parent branch in dispatch is
deleted (§7).

---

## 4. Lifecycle walkthrough

### 4.1 Construction is inert

`FxtrScope(...)` only records configuration. It does **not** create a
token, take parent leases, build an arena, or run marker setup. A
constructed-but-unentered scope can be GC'd with no effect and no warning;
no scope method works on it. (This is the big internal reshuffle: nearly
everything currently in `__init__` + autostart moves to entry — see §8.)

### 4.2 Normal route — `async with scope:`

To keep a single entry implementation, `__asynccontextmanager__` clones
each parent's in-context authorization lease and then **delegates to
`open_with_explicit_leases(parent_leases=…)`** (§4.4). The shared entry body
is:
1. Open the task group + closing event (every CM scope is within-lifespan).
2. Create the token (`DerivedLocalLifetime` over key + parent tokens) using
   the supplied parent leases as the derived inputs; build the main arena.
3. Mint the first authorization lease and store it as the base
   `_scope_auth_lease` (→ authorized, scope-level, §2); discard the bare key
   *root lease*, keeping the key ULL object (`_key_ull`, a revoke handle) on
   `self` (§3.2). (No contextvar / map write — base authorization is a plain
   attribute.)
4. Run marker setup / `provenance_context_from_scope` sync copy.
5. If not isolated, set `ACTIVE_SCOPE = self` (→ active).
6. `yield self`.

So "normal" entry is just "grab parent leases from in-context authorization,
then explicit-lease entry"; cross-task / draining-parent callers supply the
leases themselves.

On exit (either path):
- **Happy path** (`aclose`): drop the base `_scope_auth_lease` (→ `None`; new
  ambient use is rejected everywhere) → await token drain (waits for all
  external lease holders, incl. in-flight calls re-authorized via overrides, to
  release) → set the closing event (every attached lifespan begins teardown,
  concurrently) → **close the arena** (safe now: drain guaranteed every
  `Lease[main]` holder released, and teardown is unauthorized so it can't touch
  the arena) → **wait for the lifespan latch** so `aclose` returns only once the
  lifespan tasks have actually torn down. `aclose` *can't* await the task group
  directly (it runs inside it), so it waits on a `_lifespans_closed_event` that
  the last finishing lifespan sets (tracked by a `_lifespan_count`); the CM
  stack's later `TG.__aexit__` is then a no-op. (See §4.6.)
- **Exception path** (`force_close`): synchronously revoke the token (breaks
  outstanding leases) + set the closing event + close the arena; the CM stack's
  task-group unwind then cancels / awaits the parked lifespan tasks (teardown
  runs under cancellation). `force_close` is sync, so it does not itself wait on
  the latch.

### 4.3 `authorize` / `as_active_scope` / discard surface

- `with scope.authorize(lease) as auth:` — bind `lease` (ownership taken; pass
  `.clone()` to retain) as this context's **override** for `scope` (COW-insert
  into the override map, restore the prior map on exit), and yield an
  `AuthorizationHandle`. The override *unions* with the base — it is what you
  need when the scope's base is gone (draining) or you hold only a lease in a
  context with no live base; while the base is live it's a no-op (§2). Works on
  any open scope whose token is alive (§3.5).
- `with scope.as_active_scope():` — pure `ACTIVE_SCOPE` set, no args, no
  lease, no lifetime effect. Common pattern: an authorized-but-not-active
  scope made active for a block. (Activating a scope whose base is gone means
  doing both: `with scope.authorize(lease), scope.as_active_scope():`.)
- `auth.discard()` (on the `AuthorizationHandle` from `authorize`) — sync,
  idempotent; discard *this override's* lease, leaving the map entry to the
  enclosing block (it then reads as a dead override per §2). Used to drop a
  lifespan task's override after `started()` (§4.6).
- `scope.discard_base_authorization()` — sync, idempotent; discard *only* the
  scope-level base `_scope_auth_lease` (→ `None`), the owner relinquishing the
  scope's authorization (→ drain). The lowlevel cleanup partner of
  `lowlevel_start_lifespanless` (§4.5). (Distinct from `auth.discard()`, which
  drops a per-context override.)

### 4.4 `open_with_explicit_leases` — the entry primitive

`async with child.open_with_explicit_leases(parent_leases=…):` is the
underlying entry implementation: it consumes the supplied per-parent leases
as the derived-token inputs instead of sourcing them from in-context
authorization. The regular `__asynccontextmanager__` *is*
`open_with_explicit_leases`, called with leases cloned from each parent's
in-context authorization (§4.2). The explicit form exists for callers that
have a lease but not in-context authorization — cross-task, opaquely-passed,
or draining-but-open parents (the `_dispatch_body` case). It is the one path
allowed to derive from a draining-but-open parent.

(Note: this is *not* the same as nesting `with parent.authorize(lease):`
around the child's `async with` — that would keep the parent authorized for
the child's whole body. Here the supplied leases are needed only long enough
to mint the child's token at entry; nothing keeps the parent authorized
afterward.)

**Non-parent derived inputs (`extra_derived_leases`).** Beyond parent scopes,
the primitive also accepts `extra_derived_leases: {LifetimeToken: Lease}` —
derived-token inputs keyed by **token** for which there is no `FxtrScope`
object. Each becomes a derived input (so the child's lifetime is gated on it)
and gets a case-3 `DependentLocalBridge` ancestor entry (so handles on that
token can `attach_bridged_lowlevel_cap(..., bridge=None)`), but it is *not* a
parent scope: no transitive ancestor walk, not in the scope-ancestry DAG, and
its token is recorded so decompose/compose round-trip it as one more
`{token: lease}` entry.

**Duplicates are tolerated.** A token already supplied as a parent (or as the
key) may *also* appear in `extra_derived_leases` — the duplicate is accepted and
the redundant extra lease is discarded. A derived input is a *set* of tokens, so
a second lease on the same token is redundant; the token keeps its parent/key
treatment and is **not** recorded as an extra. This lets callers pass the full
set of candidate gating tokens without pre-deduping against the parents. This is
what the per-call dispatch `call_scope` uses to gate on its **arg-ServiceRef**
tokens (§5.3): `open_with_explicit_leases(parent_leases={mount_scope:
outer_lease}, extra_derived_leases={arg tokens})` — an arg token that happens to
ride on the mount scope's own token is simply tolerated and discarded.
(`lowlevel_start_lifespanless` takes the same param.)

### 4.5 Non-mountable / lifespanless route (root scope, lowlevel)

- `scope.lowlevel_start_lifespanless(parent_leases=…, extra_derived_leases=…)`
  — create the token, mint the authorization lease and **store it as the base
  `_scope_auth_lease`** (no override-map write — base is a plain attribute),
  **no task group, no lifespan**, return `self`. Parent / extra leases are
  supplied **explicitly** (ownership-taken), the synchronous counterpart of
  `open_with_explicit_leases` — not sourced from in-context authorization,
  since this is a low-level primitive. Used for the root scope (no parents) and
  any lowlevel caller that wants a scope without CM structure. Such a scope
  cannot mount/resolve services (no TG).
- Teardown for lowlevel users: `discard_base_authorization()` when done
  (drops the base → the scope then closes whenever its last lease is released —
  nobody waits), or `force_close()` to revoke now.

### 4.6 Lifespans

- `await scope.attach_lifespan_to_scope(cm)` — spawn a task in the scope's
  task group that enters `cm`, signals readiness, awaits the closing event,
  then exits `cm`; returns `cm`'s yielded value. Uses `tg.start(...)` so the
  CM is fully entered before the call returns.

  **The lifespan task manages its own authorization explicitly** rather than
  leaning on whatever authorization the spawn site ambiently copied in. The
  attaching caller (which must itself be authorized) mints a setup lease and
  passes it to the runner. The runner enters `cm` with a **proper
  `async with`** — never a manual `__aenter__`/`__aexit__` split across an
  `await`, which corrupts anyio cancel scopes / task groups — and keeps the
  `authorize` block spanning the whole task, but **discards the auth lease
  right after `started()`** so the parked task holds no `Lease[main]`:

  ```python
  async def _run_lifespan(self, cm, setup_lease, *, task_status):
      with self.authorize(setup_lease) as auth, self.as_active_scope():  # active set explicitly
          async with cm as value:             # proper async with — setup runs authorized + active
              task_status.started(value)
              auth.discard()                  # drop THIS override's lease; parked task holds NO Lease[main]
              await self._closing_event.wait() # parked
          # cm teardown runs here, unauthorized (e.g. dispatch TG closes, then user teardown)
      # authorize block exits — idempotent re-discard of the already-dead lease
  ```

  (The runner sets the mount scope **active** explicitly rather than relying
  on the spawn site's inherited active scope, so `_fxtr_mount_lifespan` setup
  can use scope-relative APIs and mount sub-services against the mount scope.
  Active has no lifetime effect, so leaving it set through the parked wait /
  teardown is harmless.)

  `auth.discard()` here discards this override's lease (idempotent) while
  leaving the contextvar bookkeeping to the enclosing `authorize` block; a
  discarded lease holds no `Lease[main]`. The parked task still reads as
  authorized via the live base — harmless, since reading the base attribute pins
  nothing; it only loses authorization once `aclose` drops the base, which is
  exactly when teardown should run unauthorized.

  This is **load-bearing for drain**: the closing event only fires after the
  token drains, and the token can only drain once nothing holds a standing
  `Lease[main]`. A parked lifespan that retained an authorization lease would
  pin the token forever (deadlock). Discarding it after `started()` makes the
  parked task hold no `Lease[main]`, so drain proceeds → event fires →
  teardown runs. It also avoids relying on the subtle "the spawn site's lease
  object gets discarded out from under us at the right moment" argument, which
  breaks when the lifespan is attached from a context other than the draining
  one.

  So: setup runs authorized, teardown runs **after token drain** and is
  therefore **unauthorized** (external-resource cleanup only — tracked
  entities are gone). The user-facing rule is the usual one: don't retain a
  standing `Lease[main]` past your yield. Multiple lifespans tear down
  **concurrently** as parallel, mutually-isolated lifetimes (not
  LIFO/ExitStack).

  **Structured teardown latch.** Each `_run_lifespan` bumps a `_lifespan_count`
  at task start and, in a `finally`, decrements it — the last one to finish
  sets a `_lifespans_closed_event`. `aclose` awaits that latch (after closing
  the arena) so it returns only once the lifespan tasks have actually exited,
  *regardless of how `aclose` was invoked* — it can't await the task group
  itself (it runs inside it). A single non-re-armable event suffices: a
  lifespan only finishes at close (it parks until the closing event fires), so
  the count is monotone up during the scope's life and down at close, reaching
  0 once; and once `aclose` drops the base no new lifespan can attach
  (`attach_lifespan_to_scope` → `check_authorized` fails). On the
  `force_close`/exception path the CM stack's task-group unwind cancels the
  parked tasks instead.

---

## 5. Service-impl surface

### 5.1 `_fxtr_mount_lifespan` hook

`CommonServiceImpl` (the helper base in `services/service_cap_common.py`, not
the bare `FxtrServiceImpl` ABC) gains a single-yield async-CM hook that
**yields a state value**. (`FxtrServiceImpl` stays bare — just the abstract
`_mount_as_fxtr_service`; all the lifespan / dispatch machinery lives in the
services layer.)

```python
@contextlib.asynccontextmanager
async def _fxtr_mount_lifespan(self, scope: FxtrScope) -> AsyncIterator[LifespanState]:
    # setup — runs authorized AND with `scope` set as the active scope (§5.2),
    #         so bare `Entity().store()` / sub-service mounts target `scope`.
    yield state            # arbitrary per-mount value; retrievable in methods
    # teardown — runs after token drain, all calls complete, unauthorized
```

`scope` is passed **explicitly** *and* is the active scope during setup. The
explicit arg is the canonical, unambiguous handle (used to register, to stash
into lifespan state); active-during-setup is the ergonomic affordance for
scope-relative APIs. They are guaranteed to be the same scope — but note the
deliberate asymmetry with *call* time, where the active scope is the per-call
child `call_scope`, **not** the mount scope (§5.3); the explicit arg is what
lets setup avoid relying on "active == mount scope here," and call methods use
`_fxtr_get_mount_scope()` instead.

The base classes define a **no-op default** that yields `None`, so impls
that don't need it ignore it. **The recommended convention is to stash
per-mount state nowhere on `self`** — neither the yielded state nor the
mount scope — and instead carry it as the yielded lifespan state, which the
framework puts on the per-mount `CommonServiceImplHandle`. Inside a call,
methods reach their mount scope and lifespan state via accessors that read
the current dispatch's handle through a single framework-owned contextvar
(§5.3): `self._fxtr_get_mount_scope()` and `self._fxtr_get_lifespan_state()`.

This is a convention, not an enforced rule: Python can't stop a concrete
impl from doing `self.x = ...`. An impl is free to, but then **owns the
safety guarantee** — it's only sound if that impl instance is mounted at
most once (or the impl keys its own per-mount state). The lifespan-state
path is the footgun-free default precisely because mounting the same impl
instance into two scopes produces two independent handles with independent
state, whereas `self`-attributes would collide.

### 5.2 Mount flow and the dispatch task group

`await scope.mount(impl)` → `await impl._mount_as_fxtr_service(scope)`
(**now async; no `task_group=` parameter anymore**) →
`await scope.attach_lifespan_to_scope(<framework lifespan>)`. `mount` sets the
mount scope as the **active** scope for the duration (as the old `mount` did),
and the lifespan runner re-asserts it active for the lifespan task (§4.6), so
both `_mount_as_fxtr_service` and `_fxtr_mount_lifespan` setup run with `scope`
active *and* receive it explicitly. `mount` /
`_mount_as_fxtr_service` are async precisely because attaching the lifespan
must be awaited (its async setup brings up the dispatch TG and registers the
handle before the `ServiceRef` is returned). The framework lifespan is
structured outer→inner:

```
async with impl._fxtr_mount_lifespan(scope) as lifespan_state:  # user setup (authorized); yields state
    async with create_task_group() as dispatch_tg:  # impl's OWN dispatch TG, inside the user lifespan
        handle = CommonServiceImplHandle(impl, scope, dispatch_tg, lifespan_state)  # state on the handle
        ref = scope.mount_lowlevel_cap_as_service(handle, target_type, service_type_id)
        yield ref                                    # surfaced out via attach_lifespan_to_scope
    # dispatch_tg closes: in-flight calls finish / unwind here…
# …then user _fxtr_mount_lifespan teardown runs, with all calls structurally complete
```

Key points:
- The dispatch task group is the **impl's own**, nested *inside* the user
  lifespan, so every call is guaranteed to complete (incl. exception/finally
  unwinding) before the user teardown runs.
- `CommonServiceImplHandle` holds `(impl, scope, dispatch_tg, lifespan_state)`
  — the per-mount state, including the yielded lifespan state, lives **on the
  handle, not the impl**. It no longer borrows the mount scope's task group,
  and there is no captured context. The mount scope's TG hosts only lifespan
  tasks. Mounting the same impl instance into two scopes yields two
  independent handles with independent state.
- The untyped and typed impl base classes are each responsible for invoking
  `_fxtr_mount_lifespan` (just as they invoke `mount_lowlevel_cap_as_service`
  today). This wiring is part of this change and is tested even though there
  are initially no real-world overriders.

### 5.3 Per-call dispatch (`_dispatch_body`)

*(Revised by `input_scope_plan.md` — stage 12 post 1: the call's wire
inputs move out of the call scope into a per-call **input scope**.)*

Per call, the handle spawns **two** scopes:

- the **input scope** — lifespanless
  (`lowlevel_start_lifespanless(extra_derived_leases={arg tokens})`,
  §4.5): it owns the call packet's arg leases, hosts the
  `lazily_attach_from_arena` of the incoming `fxtr_arena`, and carries
  the case-3 Lease[scope] → Lease[arg-token] hook (§4.4). It has **no
  mount parentage**. When the call ends, `_dispatch_body` calls
  `discard_base_authorization()` — the scope closes the moment the
  call token's parent lease on it is released, *unless* the impl
  retained `clone_authorization()`, in which case it sits
  drained-but-open (no task waits on it — that's why it must be
  lifespanless, not a CM in the dispatch task) pinning only the
  **callers' arg-export tokens** until the retention is released.
- the derived child `call_scope` (with a task group, for sub-mounts +
  result tracking; drains when the dispatch ends), built via
  `open_with_explicit_leases(parent_leases={mount_scope: outer_lease,
  input_scope: <auth clone>})` — the outer dispatch lease gates the
  call scope on its mount scope (drain waits for in-flight calls), and
  the input-scope parent gates it on its arguments (args reach it
  through the parent entry).

**Consequence worth knowing** (document at the user surface): anything
mounted on / inherited from the *call* scope holds `Lease[mount.main]`
via the call token, so the impl's mount cannot finish draining until
it is released — retaining a call-scope lease past the dispatch
deadlocks teardown unless you own a matching release path. When what
you need to keep is the call's *data*, retain the **input scope**
instead (`_fxtr_get_input_scope()`): non-pinning for the mount, at the
price of pinning the data's senders drained-but-open. The input scope
is lifespanless ⇒ no task group ⇒ it cannot mount or resolve services;
entity residency only.

**Additionally**, `_dispatch_body` makes the handle's per-mount context
available to the user method for the call's duration:

- It binds a single framework-owned contextvar — call it
  `_CURRENT_DISPATCH_CONTEXT` (née `_CURRENT_DISPATCH_HANDLE`, renamed
  when its value grew beyond the handle) — to a small per-call record
  `(handle, input_scope)`, around the `_handle_invoke`.
  The base-class accessors read it: `_fxtr_get_mount_scope()` →
  `handle.scope`, `_fxtr_get_lifespan_state()` →
  `handle.lifespan_state`, `_fxtr_get_input_scope()` → the call's
  input scope (each raises a clear error if called outside a
  dispatch). This is *one explicit framework contextvar*, not a replay
  of the captured context, so there is no `_dispatch_body_factory`
  context-override step.
- It re-authorizes the mount scope for the call's duration from the
  **per-call dispatch lease** (`with handle.scope.authorize(call_lease.clone()):`
  around the `_handle_invoke`), so `_fxtr_get_mount_scope()` returns an
  *authorized* scope. This authorization is *not* a standing lease held by
  the lifespan — a standing `Lease[main]` would wedge the token-drain wait
  forever (§4.2 / §4.6).

This unifies away the current draining-parent special-casing in
`_dispatch_body` and the "reach into a possibly-draining scope's
`_task_group`" reasoning in `_run_request_async` (§7).

---

## 6. Cross-tree bridging — explicit `keepalive_via`

`bridge_service_from_scope` gains a **required, no-default** `keepalive_via`
argument so the caller is forced to declare (and the reviewer to think
about) how the source is kept alive:

- `keepalive_via="independent_lease"` — **today's behavior.** Dest holds a
  standing clone of `Lease[ref.handle.token]` until dest drains, so
  `source.aclose` blocks on dest → **source closes after dest**. The contract is
  therefore on **block nesting** (async `async with` task nesting, *not* the
  scope-parent relationship): source must **not** be block-nested *inside* dest
  (`async with dest: async with source:` in one task self-deadlocks — source,
  inner, must close first but can't until dest drains). Source block-nested
  *outside* dest, or concurrent, is fine. We don't statically verify block
  nesting (it's a runtime/task property); TODO consider a stricter mode.

- `keepalive_via="concurrent_child_bridge"` — **new, scoped narrow.**
  Requires **dest is a direct parent of source**, that `ref` is **mounted on
  `source_scope` itself** — i.e. `ref.handle.token is source_scope.main` (the
  converter produces a `Lease[source.main]`, so the bridged token must *be*
  `source.main`; a ref reachable only via source's *ancestors* must use
  `independent_lease`) — and that **source has no extra (non-parent) derived
  inputs** (its `extra_derived_leases` set is empty). The last restriction keeps
  the safety check statically decidable: source's other inputs are then all
  *parent `FxtrScope`s*, whose descendant-of-dest status is checkable;
  bare extra-input tokens are not. Install on dest a `DependentLocalBridge` for
  `source.main` whose stored inputs are *source's other inputs* (its key plus
  every parent except dest) and whose converter is
  `source._derived.exchange_forward({dest.main: base, <stored others>})`, with
  `base = Lease[dest.main]` minted from dest per call. Because dest never holds a
  *standing* `Lease[source.main]` it holds no standing `dest.main` via, so dest
  can drain; when it does, `dest.main` ends → `source.main` cascades closed →
  **both end together**. Note dest holds source's *key* via at rest, so source
  cannot self-drain via its key — its `main` only ends via the dest→source
  cascade, so **block nesting must be concurrent**: source is a scope-parent
  descendant of dest, so it can't be block-nested *outside* dest (can't enter
  before its parent) and must not be block-nested *inside* dest (self-deadlock) —
  the two scopes must drain in **concurrent tasks**, closing simultaneously.
  - **TODO (generalization).** The mechanism composes cleanly to refs reachable
    on source via a `DependentLocalBridge` (e.g. bubbling a service child→parent
    →grandparent): compose this `dest.main → source.main` step with source's
    existing bridge converter (mounted-on-source is the identity-converter
    special case). Deferred because the composed form makes dest *inherit*
    source's bridge's stored vias — bare tokens whose descendant-of-dest status
    we can't statically check without new leases-layer infra (a token→transitive-
    input graph). The two restrictions above (mounted-on-source, no extra derived
    inputs) keep the current form fully verifiable.
  - **Only requires source authorized at bridge time.** Source's
    authorization lease is a `Lease[source.main]` derived-output lease that
    already carries a via on *every* input of source; we `exchange_reverse`
    / `subclone` it to recover clones of the key and every parent via. The
    other parents do **not** need to be independently authorized — their
    leases come out of source's authorization. (The dest via is recovered
    but discarded; dest supplies `base` per call.)
  - Safety restriction: every *other* input of source must **not** be a
    descendant of dest (else its recovered via would itself carry a
    `dest.main` via and reintroduce the cycle). Checked at bridge time.
  - This is the shape that implements the RPC service-return pattern (a
    service from a child that pulled in a separate mount scope, bridged back
    into the caller, introducing a caller→mount-scope dependency without a
    caller→caller deadlock).

`resolve_service`'s first-contact bridge-lift is unchanged in spirit (it
installs a local entry so subsequent resolves short-circuit); it is not
given a `keepalive_via` — only the explicit cross-tree `bridge_*` entry
points require the declaration.

---

## 7. API inventory after the change

The block below is the post-change public contract of `FxtrScope` and the
service-impl surface. Significantly-changed members are sketched as code;
unchanged members are abbreviated with a one-line comment noting any
mechanical adjustment; removed members are listed explicitly.

```python
# =====================================================================
# FxtrScope — construction & configuration
#
# (There is no decompose/compose helper dataclass. `decompose_authorization`
#  returns — and `compose_authorization` accepts — a plain `{LifetimeToken:
#  Lease}` map keyed by token: the key via + every parent via + every extra
#  via, flat and uniform. Callers index it with the public `scope_token` /
#  `key_token` accessors.)
# =====================================================================

class FxtrScope(anyio.AsyncContextManagerMixin):
    def __init__(
        self,
        include: Sequence[FxtrScope] = (),
        isolated: bool = False,
        name: str | None = None,
        *,
        provenance_context: FxtrID | None = None,
        provenance_context_from_scope: FxtrScope | None = None,
        store_provenance: StoreProvenance = "auto",
    ):
        """CHANGED: now CONFIG-ONLY and inert.

        Records configuration; does NOT create a token, take parent leases,
        build an arena, or run marker setup (all of that moves to entry).
        REMOVED params: `mountable` (every CM scope is within-lifespan now)
        and `autostart` (the auto-start path is gone; see below).
        `isolated`, `name`, and the three provenance/store kwargs are
        unchanged in meaning.
        """

    # ---- lifecycle: entry / exit -------------------------------------

    @contextlib.asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncGenerator[Self]:
        """CHANGED: now performs the real start (was split across __init__
        + autostart + old __aenter__). Clones each parent's in-context
        authorization lease and DELEGATES to open_with_explicit_leases() so
        there is one entry implementation. That shared body opens the task
        group + closing event, creates the derived token, builds the arena,
        mints the first authorization lease and stores it as the base
        `_scope_auth_lease` (discarding the bare key root lease, keeping the
        `_key_ull` object), runs marker setup, sets ACTIVE_SCOPE (non-isolated
        only), yields. Exit: aclose() on happy path, force_close() on
        exception."""

    async def aclose(self) -> None:
        """CHANGED (mechanics): drop the base `_scope_auth_lease` (→ None) →
        await token drain → set the closing event (lifespans tear down
        concurrently) → close the arena → wait on the lifespan latch
        (`_lifespans_closed_event`) so it returns only once lifespans have
        exited (it can't await the task group — it runs inside it; §4.2/§4.6).
        Public name kept."""

    def force_close(self) -> None:
        """CHANGED (mechanics): synchronously revoke the derived token AND the
        key ULL (`_key_ull`, breaking externally-split key leases), set the
        closing event, close the arena. Sync, so it does not wait on the
        lifespan latch — the CM stack's task-group unwind cancels parked
        lifespans. Public name kept. (lowlevel_force_close is NOT introduced —
        this is the one revoke primitive.)"""

    def lowlevel_start_lifespanless(
        self,
        *,
        parent_leases: Mapping[FxtrScope, Lease[Any]] = {},  # default-empty (immutable)
        extra_derived_leases: Mapping[LifetimeToken[Any], Lease[Any]] = {},  # default-empty (immutable)
    ) -> Self:
        """NEW: start without a task group / lifespan; return self. Creates the
        token, mints the authorization lease and stores it as the base
        `_scope_auth_lease`. Cannot mount/resolve services. The synchronous,
        explicit-lease counterpart of open_with_explicit_leases: `parent_leases`
        (one per direct parent; empty for the root) and `extra_derived_leases`
        are supplied explicitly and ownership-taken — *not* sourced from
        in-context authorization (this is a low-level primitive). For the root
        scope and lowlevel callers. Pair with discard_base_authorization()
        or force_close() to tear down."""

    @contextlib.asynccontextmanager
    async def open_with_explicit_leases(
        self,
        *,
        parent_leases: Mapping[FxtrScope, Lease[Any]],
        extra_derived_leases: Mapping[LifetimeToken[Any], Lease[Any]] = {},  # default-empty (immutable)
    ) -> AsyncGenerator[Self]:
        """NEW: the entry primitive. Consumes `parent_leases` as the
        derived-token inputs instead of sourcing them from in-context
        authorization; __asynccontextmanager__ is this, called with leases
        cloned from each parent's in-context authorization. NOT equivalent to
        nesting `with p.authorize(lease)` around the body (the leases are
        needed only to mint the token at entry, not for the child's whole
        body). Used by _dispatch_body and cross-task callers. The one path
        allowed to derive from a draining-but-open parent.

        `extra_derived_leases` adds derived-token inputs keyed by *token* (not
        parent scope) — for tokens the scope must gate on that have no FxtrScope
        object (e.g. dispatch arg-ServiceRef tokens). Each gets a case-3
        dependent ancestor entry but is not a parent scope (no transitive walk,
        not in the scope-ancestry DAG). A token already supplied as a parent (or
        the key) may also appear here — the duplicate is tolerated and the
        redundant lease discarded (§4.4). `lowlevel_start_lifespanless` takes the
        same param. (§3.2; §5.3 dispatch builds the call_scope with
        `parent_leases={mount_scope: outer_lease}` + arg tokens here.)"""

    # ---- authorization / activation ----------------------------------

    @contextlib.contextmanager
    def authorize(self, lease: Lease[Any]) -> Generator[AuthorizationHandle]:
        """NEW: bind `lease` as this context's OVERRIDE authorization for the
        scope (ownership taken; pass .clone() to retain). COW-inserts
        `self -> lease` into the shared `_AUTH_OVERRIDES` map (§2) and restores
        the prior map on exit; the block's exit discards the held lease
        (idempotent). **Yields an `AuthorizationHandle`** whose `.discard()`
        drops this override's lease early (§4.6). The override is *unioned* with
        the base — while the base is live this is a no-op; it is load-bearing
        only when the base is gone (draining) or you hold only a lease in a
        context with no live base."""

    def discard_base_authorization(self) -> None:
        """NEW (sync, idempotent): discard *only* the scope-level base
        `_scope_auth_lease` (→ None) — the owner relinquishing the scope's
        authorization, beginning drain. The lowlevel cleanup partner of
        `lowlevel_start_lifespanless`. (To drop a per-context OVERRIDE early, use
        the `AuthorizationHandle` from `authorize` — `auth.discard()` — not this
        method.)"""

    @contextlib.contextmanager
    def as_active_scope(self) -> Generator[None]:
        """UNCHANGED in spirit: pure ACTIVE_SCOPE set, no args, no lease, no
        lifetime effect. Does NOT authorize (that needs a lease via
        authorize()). Common pattern: make an authorized-but-not-active
        scope active for a block."""

    def clone_authorization(self) -> Lease[Any]:
        """NEW: return a fresh clone of THIS context's current authorization
        lease (`Lease[main]`). Requires authorized (else not-authorized error).
        Pair with `authorize()` elsewhere, or stash / pass across a task
        boundary. (§3.2.)"""

    def decompose_authorization(
        self, lease: Lease[Any]
    ) -> dict[LifetimeToken[Any], Lease[Any]]:
        """NEW: consume a `Lease[main]` and split it (via
        `derived.exchange_reverse`) into **all** its constituent vias, returned
        as a single `{token: Lease}` map — the key via (at `key_token`), every
        per-parent via (at `parent.scope_token`), and every non-parent
        derived-input via. Takes ownership of `lease`. Inverse of
        `compose_authorization`. (§3.2.)"""

    def compose_authorization(
        self,
        leases: Mapping[LifetimeToken[Any], Lease[Any]],
    ) -> Lease[Any]:
        """NEW: recombine a `{token: Lease}` map back into a `Lease[main]` via
        `derived.exchange_forward`. `leases` keys must match the scope's full
        derived-input set exactly — `key_token` + every parent's `scope_token` +
        every extra derived-input token (`exchange_forward` enforces this). Takes
        ownership of the supplied leases; requires the scope open. Inverse of
        `decompose_authorization`. (§3.2.)"""

    # ---- lifespans ---------------------------------------------------

    async def attach_lifespan_to_scope[T](
        self, cm: contextlib.AbstractAsyncContextManager[T]
    ) -> T:
        """NEW: enter `cm` in the scope's task group (via tg.start, so it's
        fully entered before returning), await the closing event, then exit
        `cm`. Returns cm's yielded value. Requires the calling context to be
        authorized (mints a setup lease from it) and a task group (not a
        lifespanless scope).

        The lifespan task manages authorization EXPLICITLY: it enters `cm`
        with a proper `async with` (never a manual __aenter__/__aexit__ split,
        which corrupts anyio cancel scopes), wraps it in an `authorize` block
        spanning the whole task, and discards the auth lease right after
        started() — so while parked on the closing event it holds NO
        Lease[main] (can't wedge the token-drain wait) and tears down
        unauthorized after the token drains. See §4.6."""

    # ---- properties / queries ----------------------------------------

    @classmethod
    def get_active(cls) -> FxtrScope: ...          # UNCHANGED
    @property
    def isolated(self) -> bool: ...                # UNCHANGED
    @property
    def frozen(self) -> bool: ...                  # UNCHANGED
    @property
    def outer(self) -> FxtrScope: ...              # UNCHANGED
    @property
    def scope_token(self) -> LifetimeToken[Any]:
        """NEW: the scope's main derived token (the subject of `Lease[main]`).
        Used to index a decomposed `{token: Lease}` map. Raises if not started."""
    @property
    def key_token(self) -> LifetimeToken[Any]:
        """NEW: the scope's private key token (`_key_ull.token`). The key via in a
        decomposed map lives here. Raises if not started."""
    def is_open(self) -> bool:
        """REPLACES is_alive (per-scope half): True iff the token has been
        created and not ended."""
    def is_authorized(self) -> bool:
        """REPLACES is_alive (per-context half): True iff this context resolves
        a live authorization for the scope — an active override, or (no
        override) the active scope-level base. False on a present-but-discarded
        override. Non-raising counterpart of check_authorized()."""
    def check_authorized(self) -> None:
        """RENAMED from check_alive + CHANGED: raises unless is_open() AND this
        context resolves a live authorization (§2 resolution order; a draining
        scope — token alive, base dropped — fails for contexts without an
        override). Distinct errors: token-not-yet-created (not-started),
        token-ended (closed), no resolvable auth (not-authorized),
        present-but-discarded override (discarded-override)."""
    def included_in_scope(self, scope: FxtrScope) -> bool: ...        # UNCHANGED
    def included_in_active_scope(self) -> bool: ...                  # UNCHANGED
    def is_tracked(self, node) -> bool:            # UNCHANGED (reads auth lease)
    def freeze_scope(self) -> None: ...            # UNCHANGED (orthogonal)

    # REMOVED property:
    #   mountable  — every CM-entered scope is within-lifespan; lifespanless
    #                scopes are the lowlevel exception. Callers that branched
    #                on `mountable` drop the branch.

    # =================================================================
    # Data operations — UNCHANGED surface; mechanical change only:
    # each clones the lease this context resolves to (override-or-base,
    # clone-per-op) via `_current_auth_lease()` instead of cloning a stored
    # `self._main_lease`, and raises if not authorized. (Common path: no
    # override → the scope-level base, same as today's `_main_lease`.)
    # =================================================================
    def store[T: Entity](self, entity: T) -> EntityRef[T]: ...
    def store_cbor(self, cbor: FxtrCbor) -> FxtrID: ...
    async def load[T: Entity](self, ref: EntityRef[T]) -> T: ...
    async def load_cbor(self, node_id: FxtrID) -> FxtrCbor: ...
    async def load_frozen_cbor(self, node_id: FxtrID) -> FrozenFxtrCbor: ...
    def get_cached_value[T: Entity](self, ref: EntityRef[T]) -> T: ...
    async def deep_load[T](self, thunk: Callable[[], T]) -> T: ...
    def lift(self, node): ...
    async def copy_from_scope[T](self, ref, from_scope) -> ...: ...
    def track_from_scope[T](self, ref, from_scope) -> ...: ...
    def abandon(self, node_id) -> None: ...
    def clear_cache(self, include_parents: bool = False) -> None: ...
    def track_builtin(self, builtin_id: FxtrID) -> FxtrID: ...

    # =================================================================
    # Provenance helpers — UNCHANGED (orthogonal); same per-op auth-lease
    # mechanical change.
    # =================================================================
    def get_context_marker_id(self) -> FxtrID: ...
    def extract_provenance_parent(self, data_with_prov: FxtrID) -> FxtrID: ...
    def embed_as_provenance_parent(self, prov_node: FxtrID) -> FxtrID: ...
    def c_node_to_context_data(self, c_node: FxtrID) -> FxtrID: ...
    def create_c_node(self, context_data: FxtrCbor) -> FxtrID: ...
    def create_u_node(self, wrapped_id: FxtrID) -> FxtrID: ...
    def step_back_p_ctx(self, p_node: FxtrID) -> FxtrID | None: ...

    # =================================================================
    # Service surface
    # =================================================================
    async def mount[T](self, impl: FxtrServiceImpl[T]) -> ServiceRef[T]:
        """CHANGED: now ASYNC (was sync). Delegates to
        await impl._mount_as_fxtr_service(self) with NO task_group argument;
        the impl creates its own dispatch task group inside its lifespan and
        attaches it via `await attach_lifespan_to_scope`, which must be
        awaited so the lifespan's async setup (TG up + handle registered)
        completes before the ServiceRef is returned. Requires a task group
        (lifespanless scopes can't mount). Sets self active for the call as
        today. Call sites become `await scope.mount(impl)`."""

    def mount_lowlevel_cap_as_service[B, T](
        self, handle: IRHandle[B], service_type, service_type_id
    ) -> ServiceRef[T]:
        """UNCHANGED in contract. The "requires mountable" guard becomes
        "requires a task group" (rejects lifespanless scopes)."""

    def attach_bridged_lowlevel_cap[T](
        self, handle, service_type, service_type_id, bridge
    ) -> ServiceRef[T]:
        """UNCHANGED in contract; same task-group guard rename."""

    def forget_service(self, ref: ServiceRef[Any]) -> None: ...     # UNCHANGED
    async def resolve_service[T](self, ref: ServiceRef[T]) -> T:
        """UNCHANGED in contract; "requires mountable" → "requires a task
        group". First-contact bridge lift unchanged (no keepalive_via)."""

    async def bridge_service_from_scope[T](
        self,
        ref: ServiceRef[T],
        source_scope: FxtrScope,
        *,
        keepalive_via: Literal["independent_lease", "concurrent_child_bridge"],
    ) -> None:
        """CHANGED: `keepalive_via` is now REQUIRED (no default). See §6 for
        the two modes and the concurrent_child_bridge restrictions."""

    def register_deep_fetch_source[B](self, source, bridge) -> None: ...  # UNCHANGED


# =====================================================================
# AuthorizationHandle (NEW). Yielded by `authorize`.
# =====================================================================
class AuthorizationHandle:
    """Handle for one `authorize(lease)` override binding.

    def discard(self) -> None:
        '''Discard THIS override's lease (idempotent). Leaves the cv-map entry
        to the enclosing `authorize` block — afterward the entry reads as a dead
        override (only matters once the base is gone, §2). Distinct from
        `scope.discard_base_authorization()`, which drops the scope-level base.'''
    @property
    def is_discarded(self) -> bool: ...
    """

# =====================================================================
# REMOVED from FxtrScope:
#   @classmethod _with_derived_lifetime(...)   — superseded by the uniform
#       derived token + open_with_explicit_leases. Its only caller
#       (_dispatch_body) moves to open_with_explicit_leases.
#   mountable= / autostart= constructor params and the `mountable` property.
#   The auto-start-at-construction behavior for isolated+non-mountable
#       scopes (bare `s = FxtrScope(isolated=True); ... ; await s.aclose()`)
#       — bare usage is now lowlevel_start_lifespanless() or `async with`.
# =====================================================================


# =====================================================================
# Service impl base surface
# =====================================================================
# Layering note: the lifespan hook + dispatch accessors live on
# CommonServiceImpl (services/service_cap_common.py), NOT on the bare
# FxtrServiceImpl ABC in entity_ref_scope.py. FxtrServiceImpl stays bare.

class FxtrServiceImpl[T](abc.ABC):
    # UNCHANGED-bare ABC except for the one signature change below.
    @abc.abstractmethod
    async def _mount_as_fxtr_service(self, scope: FxtrScope) -> ServiceRef[T]:
        """CHANGED: now ASYNC + signature takes `scope` only (NO
        `task_group`). The impl creates its own dispatch task group inside
        its lifespan and returns the ServiceRef via
        `await scope.attach_lifespan_to_scope(...)`. `scope` is also the
        active scope here (set by `mount`), so scope-relative APIs target
        it. (This is the ONLY member on the bare ABC; the lifespan hook +
        accessors below are on CommonServiceImpl.)"""


class CommonServiceImpl[T](FxtrServiceImpl[T]):
    # Concrete helper base in services/service_cap_common.py. Implements
    # _mount_as_fxtr_service (framework lifespan → dispatch TG + handle), and
    # adds the lifespan hook + dispatch accessors.

    @contextlib.asynccontextmanager
    async def _fxtr_mount_lifespan(self, scope: FxtrScope) -> AsyncIterator[Any]:
        """NEW hook on CommonServiceImpl, no-op default (yields None).
        Single-yield async CM: setup before yield (authorized + with `scope`
        ACTIVE, inside the dispatch TG), teardown after (post-drain,
        unauthorized). `scope` is both passed explicitly and active during
        setup (§5.2). The yielded value is the per-mount lifespan state — the
        channel for per-mount resources (sockets, etc.). The impl stashes
        NOTHING on `self`; everything per-mount goes through the yielded
        state, which the framework puts on the handle (§5.2)."""
        yield None

    def _fxtr_get_lifespan_state(self) -> Any:
        """NEW on CommonServiceImpl: return the lifespan state for the
        CURRENTLY-DISPATCHING call, read from the single framework contextvar
        (`_CURRENT_DISPATCH_CONTEXT`, a module-level constant in
        service_cap_common.py) that `_dispatch_body` binds to the call's
        `(handle, input_scope)` record inside the call task. Raises if called outside a dispatch.
        Replaces capturing/replaying the mount-time context."""

    def _fxtr_get_mount_scope(self) -> FxtrScope:
        """NEW on CommonServiceImpl: return the mount scope (`handle.scope`)
        for the currently-dispatching call, from the same
        `_CURRENT_DISPATCH_CONTEXT` contextvar. It is authorized for the call's
        duration (§5.3). Raises if called outside a dispatch."""

    def _fxtr_get_input_scope(self) -> FxtrScope:
        """NEW (stage 12 post 1, `input_scope_plan.md`): return the
        currently-dispatching call's INPUT scope — the lifespanless per-call
        scope owning the call's wire inputs (§5.3). Its base is live for the
        whole call (no re-auth needed); retain `clone_authorization()` to keep
        the call's data loadable past the dispatch WITHOUT pinning the mount
        token (pins the callers' arg exports instead). Lifespanless ⇒ cannot
        mount/resolve services. Raises if called outside a dispatch."""
```

---

## 8. Implementation ripple (flagged, not yet planned)

The single largest mechanical change is **moving everything out of
`__init__`**: token mint, arena build (`_init_arena_and_cache`),
ancestor-bridge population (`_populate_ancestor_bridges`), context-marker
setup (`_apply_context_marker_override`), and the
`provenance_context_from_scope` sync c-node copy all move into entry
(`__asynccontextmanager__` / `lowlevel_start_lifespanless`). This is
intentional — autostart has been a recurring wart.

No spirit-clash found with the orthogonal features, which stay as designed
modulo this mechanical move:
- **provenance** (`provenance_context`, `store_provenance`): unchanged;
  `provenance_context_from_scope` now does its sync copy at entry, which is
  consistent with the new parent-authorization contract (parents are
  authorized at entry).
- **freeze**: unchanged; frozen+authorized reads still work; root cannot be
  frozen (root is lifespanless).
- **deep-fetch sources**: unchanged; torn down in the same token-end
  callback.
- **entity-load / service-resolver registries**: untouched.

Operations now read the lease this context resolves to — override-or-base
via `_current_auth_lease()` (clone per op) — rather than
`self._main_lease.clone()`. In the common path (no override) that *is* the
base scope-level lease, i.e. a near-rename of `_main_lease`; the per-ancestor
lease-minting in `lift` / deep-fetch (`_mint_ancestor_lease`) is re-expressed
against the base / derived machinery.

---

## 9. Migration story (abstract; old → new patterns)

**Bare isolated scope → `async with` (the common case):**
```python
# OLD
scope = FxtrScope(isolated=True)
...
await scope.aclose()
# NEW
async with FxtrScope(isolated=True) as scope:
    ...
```

**Bare isolated scope that must stay bare → lowlevel:**
```python
# OLD
scope = FxtrScope(isolated=True)        # auto-started
... use across calls ...
scope.force_close()
# NEW
scope = FxtrScope(isolated=True).lowlevel_start_lifespanless()
... use across calls ...
scope.discard_base_authorization()      # closes when last lease releases
# (or scope.force_close() to revoke now)
```

**`mountable=True` → drop it (every CM scope hosts services); `mount` is now async:**
```python
# OLD
async with FxtrScope(mountable=True) as scope:
    ref = scope.mount(impl)
# NEW
async with FxtrScope() as scope:
    ref = await scope.mount(impl)        # mount is async now
```

**`is_alive()` → `is_open()` / `is_authorized()` (pick what you mean):**
```python
# OLD
scope.is_alive()
# NEW
scope.is_open()         # token created & not ended (per-scope)
scope.is_authorized()   # this context holds a live auth lease (per-context)
```

**Service impl managing long-lived resources / its mount scope:**
```python
# OLD: no public way; resources mounted into a scope were lost on drain.
# NEW — stash NOTHING on self; per-mount resources go through the yielded
# lifespan state, reached in methods via the accessors.
class MyServiceImpl(SimpleAsyncServiceImpl[UnknownService]):
    @contextlib.asynccontextmanager
    async def _fxtr_mount_lifespan(self, scope):
        async with open_my_socket() as sock:   # per-mount external resource
            yield sock                          # calls run here; state lives on the handle
        # teardown after all calls complete, after token drain

    @service_method
    async def do_thing(self):
        sock = self._fxtr_get_lifespan_state()  # this mount's socket
        mount_scope = self._fxtr_get_mount_scope()  # authorized for this call
        ...
```

**Service impl mount signature (framework-internal impls / subclasses):**
```python
# OLD
def _mount_as_fxtr_service(self, *, task_group): ...
# NEW
async def _mount_as_fxtr_service(self, scope): ...   # async; impl builds its own dispatch TG
```

**Cross-task / draining parent (incl. `_dispatch_body`):**
```python
# OLD (internal)
FxtrScope._with_derived_lifetime(include=[parent],
                                 derived_from_leases={parent.token: outer_lease,
                                                      **arg_token_leases}, ...)
# NEW
async with child.open_with_explicit_leases(
    parent_leases={parent: outer_lease},
    extra_derived_leases=arg_token_leases,   # non-parent derived inputs (e.g. arg-ServiceRef tokens)
) as child:
    ...
# (the explicit-lease entry primitive; the normal `async with child:` is just
#  this with the parent leases taken from in-context authorization, and no extras)
```

**Using a scope you only hold a lease for:**
```python
# NEW
with scope.authorize(my_lease.clone()):
    ref = await some_ref.load(scope)
```

**Cross-tree service bridge → declare keepalive:**
```python
# OLD
await dest.bridge_service_from_scope(ref, source_scope)
# NEW — pick the mode explicitly
await dest.bridge_service_from_scope(ref, source_scope,
                                     keepalive_via="independent_lease")
# or, when dest is a direct parent of source (RPC service-return shape):
await dest.bridge_service_from_scope(ref, source_scope,
                                     keepalive_via="concurrent_child_bridge")
```

---

## 10. Open issues / to revisit

- **Per-mount state plumbing — resolved.** State lives on the per-mount
  `CommonServiceImplHandle` and is reached via a single framework contextvar
  (`_CURRENT_DISPATCH_CONTEXT`) bound per-call to the dispatch record, so
  mounting the same impl instance on multiple scopes is safe (no shared
  `self` state). Just confirm the accessor's "outside a dispatch" error is
  clear.
- **Proxy/client-side authorization — DECIDED: caller-authorized.** §5.3
  covers server-side mount-scope reactivation; the *proxy* side
  (`invoke_service_method` / `mint_lease_for_key` / `_install_returned_resources`)
  needs `Lease[home_main]`, which under the new model comes from the home
  scope's current authorization → **a proxy call requires home_scope
  authorized in the calling task** (the faithful analog of today's per-call
  `_main_lease.clone()`). Proxy-self-authorizing was rejected (a standing
  captured lease pins home_scope open → drain-hang generator). Remaining work
  is the chunk-3 verification spike: confirm internal proxy-invocation paths
  run authorized (see `impl_plan.md`).
- **`concurrent_child_bridge` is deliberately narrow** (dest = direct parent
  of source; other parents not descendants of dest). The
  "independent_lease / structurally-outside" property is not statically
  verified — possible future stricter mode (TODO).
- **`check_authorized` error messages.** Make sure the four distinguishable
  cases (token not yet created / token ended / token alive but no resolvable
  authorization / present-but-discarded override) all produce debuggable
  messages (§3.5).
- Whether any current `_with_derived_lifetime` test (`test_derived_*`)
  encodes behavior we want to preserve as a public-API test of the new
  uniform derived token.

---

## 11. Test coverage to add (new / changed behavior)

High-level checklist of the subtle edge cases the new design introduces, so
the implementation plan can turn each into a concrete test. Existing scope /
service / derived-lifetime suites must also stay green after mechanical
migration (that's regression coverage, not listed here).

**Authorization (base + overrides).**
- **Base is scope-level**: an *open* scope is usable from a sibling task with
  no per-context setup (base authorization, like today). After `aclose` drops
  the base, that same sibling-task use raises (base gone, no override).
- `with scope.authorize(lease) as auth:` binds an **override** that *unions*
  with the base; makes a *base-gone* (draining-but-open) scope usable. Takes
  ownership (discards on exit; `.clone()` retains). Works on a draining-but-open
  scope, raises on a closed one.
- **Base-floor union**: while the base is live, ops succeed regardless of
  override state — including an override snapshotted by an anyio-spawned subtask
  and used after the origin discarded it (**context-copy hazard: succeeds via
  the live base**). The *discarded-override* error fires **only** when the base
  is gone (draining) AND this context held an override AND its lease died. With
  the base gone, a live override authorizes; a dead one raises; none →
  not-authorized.
- `is_open()` vs `is_authorized()` disagree on a draining (base-dropped, token
  still alive) scope: open True, authorized False (until you `authorize`).
  A live base ⇒ `is_authorized()` True in any context.
- Root/global scope is authorized in a fresh task by default (base attribute is
  globally visible; bare `entity.store()` works with no setup).
- `as_active_scope` does not authorize: activating a base-gone scope still
  raises on ops until you also `authorize`.

**Construction / lifetime.**
- Constructed-but-unentered scope: every method raises not-started; GC
  produces no lease-leak warning; entry creates token/arena/marker.
- Parentless scope closes at `aclose` when the base lease is dropped and no
  other clones remain (no idle-key hang — the retained `_key_ull` object pins
  nothing).
- Split a key lease from an auth lease + re-mint a fresh authorization
  round-trips. Public-API form: `decompose_authorization` →
  `compose_authorization` round-trips a `Lease[main]` (returned/accepted as a
  flat `{token: Lease}` map; token-set validated against the full derived-input
  set — `key_token` + parents + extras — and a mismatched set rejected);
  `clone_authorization` in one context → `authorize` in another makes the scope
  usable there. **Duplicate-token tolerance**: `open_with_explicit_leases` (and
  `lowlevel_start_lifespanless`) accept an `extra_derived_leases` entry whose
  token is already a parent (or the key) — the redundant lease is discarded and
  the token is not double-counted. Parent-revoke cascades to children;
  `parent.aclose()` blocks until the child drains.
- `auth.discard()` (override handle) and `discard_base_authorization()` (base)
  are each idempotent; `auth.discard()` composes inside its `authorize` block
  (no contextvar double-reset; the block's exit re-discards the already-dead
  lease harmlessly).
- `lowlevel_start_lifespanless`: no task group (mount/resolve raise);
  `discard_base_authorization()` lets it close when the last lease releases;
  `force_close` revokes cleanly.
- `open_with_explicit_leases`: open a child from a parent authorized only in a
  different task; from a draining-but-open parent (spawner); supplied leases
  don't leave the parent authorized for the child's whole body.

**Lifespans.**
- Setup runs authorized; teardown runs after drain and is unauthorized (op in
  teardown raises). `attach_lifespan_to_scope` returns the CM's yielded value;
  raises on a lifespanless scope.
- **No-deadlock drain** (bound with a timeout so a regression that reintroduces
  a standing `Lease[main]` in the parked lifespan *fails* instead of hanging).
- Teardown still runs on the exception / `force_close` path.
- Multiple lifespans tear down concurrently, not LIFO.

**Service mount + lifespan.**
- `_fxtr_mount_lifespan` yields state → a call reads it via
  `_fxtr_get_lifespan_state()`; `_fxtr_get_mount_scope()` returns an
  authorized scope and storing into it persists across calls.
- Accessors raise outside a dispatch.
- **Multi-mount isolation**: same impl instance mounted into two scopes →
  independent per-call state / mount scope.
- Teardown runs only after all in-flight calls complete (dispatch TG nested in
  the user lifespan).
- Active-scope asymmetry: mount scope active in setup, `call_scope` active
  during a call.
- A no-`_fxtr_mount_lifespan` impl mounts fine; `mount` is awaited.

**`keepalive_via` bridging.**
- `concurrent_child_bridge` (dest = direct parent of source): bridge back, then
  both close together with no deadlock (dest can drain). Only *source* need be
  authorized at bridge time.
- Rejections: dest not a direct parent of source; some other source-parent is a
  descendant of dest. `keepalive_via` omitted → error. `independent_lease`
  matches today's behavior.

**Dispatch reactivation / drain cycle.**
- Keep/port the resolve-into-mount-scope drain-cycle acceptance test under the
  new dispatch.
- A call completes after the mount scope's CM body has exited (token draining)
  while a caller still holds a lease (dispatch on the impl's own TG).
