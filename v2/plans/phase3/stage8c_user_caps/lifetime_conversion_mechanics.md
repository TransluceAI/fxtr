# Lifetime conversion mechanics

How `lazily_attach_from_arena` works under the hood, why there are three
canonical converter shapes, and how to pick between them.

This is the document to read before adding a new foreign-arena attach. After
reading it you should be able to look at any attach in the codebase, identify
which case it belongs to, and explain why an alternate pattern would deadlock.

> **Cross-references.** The recipe-style version of this guide is in
> `fxtr2/leases/LOCAL_CODE_OVERVIEW.md` (with the post-part6post2
> `LocalBridge`-based call shape); the end-to-end worked example is
> `tests/test_entity_ref_scope/test_derived_scope_acceptance.py`. Both
> point back here.
>
> **API note (post part6post2).** The historic
> `LifetimeConverter`/`default_lifetime_converter` shape was deleted in
> the part6post2 memory-store simplification. `lazily_attach_from_arena`
> now takes a `LocalBridge` (`IndependentLocalBridge` or
> `DependentLocalBridge` from `fxtr2.leases.lease_bridges`). The three
> cases below describe the same conceptual relationships; the API
> surface for each maps to:
>   - **Case 1**: `IndependentLocalBridge(lease=Lease[Y])`.
>   - **Case 2**: `DependentLocalBridge(leases={K: key_lease}, converter=...)`
>     where the converter calls `derived.exchange_forward({K: key, X: base})`.
>   - **Case 3**: `DependentLocalBridge(leases={}, converter=...)` whose
>     converter calls `derived.exchange_reverse(base)` and returns the Y via.

---

## 1. The setting

The memory store's `lazily_attach_from_arena(lease, node_id, source, key_lease,
converter)` registers a foreign arena as a lazy source for one node:

- The consumer arena (the one the call is made *on*) is gated by token `X`.
- `source` is some other arena gated by token `Y`.
- A `(stored_key_lease, converter)` pair is recorded in the consumer's
  foreign-lease row at `_token_data[X].foreign_leases[Y]`.
- Every time the consumer needs to load through this attach, the store mints
  a fresh `Lease[Y]` by calling
  `converter(stored_key.clone(), base = Lease[X].clone())`. The minted lease
  is used for one source-arena operation and immediately discarded.

So the converter is invoked *once per load*, with two leases: a stored-key
clone (whose brand is up to the registrant) and a fresh consumer-lease clone.
The converter's job is to produce a `Lease[Y]` from those two inputs. The
question this document answers: **what should the key brand be, and what
should the converter actually compute?**

The choice depends entirely on the structural relationship between `X` and
`Y` — and getting it wrong reliably produces a circular drain dependency.

---

## 2. The via mechanic — the one concept everything else turns on

A `LocalDerivedTokenLease[Y]`, minted by
`DerivedLocalLifetime.exchange_forward(input_leases)`, internally carries
`_vias: dict[LifetimeToken, Lease]` — a clone of the supplied lease on every
declared input. Each via is a real lease, registered in its owner ULL's
`_tracking` table, just like any other clone. Cloning the output lease clones
the vias; discarding it discards the vias; breaking it (via the input-end
cascade) clears the vias too.

Two consequences drive the entire converter design:

1. **A live `Lease[Y]` transitively witnesses every input being alive.** You
   can recover an active lease on any input by calling
   `derived.exchange_reverse(out_lease)`, which returns clones of the vias.
   This is the structural guarantee that case 3 below relies on.

2. **A persistently-held `Lease[Y]` pins every input lifetime.** Each via is
   counted by its input ULL as a live tracked clone — so the input can't
   drain until the via is released, and the via can't be released until the
   `Lease[Y]` holding it is dropped. This is the *cycle hazard* that drives
   case 2 below.

Concretely: if `Y` is a derived output with `t1_token` as an input, and you
store a `Lease[Y]` somewhere that only releases when `t1_token` ends, you
have built a deadlock. `t1_token` waits on the via, the via waits on the
`Lease[Y]`, the `Lease[Y]` waits on `t1_token`. The drain hangs and
something has to revoke to break it.

---

## 3. The three cases

Each case answers: given consumer token `X` and source token `Y`, what's the
relationship between their lifetimes, and what converter shape avoids cycles?

### Case 1 — `Y` is a parent / outer lifetime of `X`

**When.** `Y` is structurally guaranteed to outlive `X`. Typical cause: `Y`
is one of `X`'s memory-store parents (so the store already holds a clone of
`Y`'s lease pinned to `X`'s row), or `Y` is the global scope that the
consumer is a child of, or some other "outer scope" relationship.

**Use.** `default_lifetime_converter` (built-in). Pass a `Lease[Y]` as the
key.

```python
arena.lazily_attach_from_arena(my_lease, node_id, parent_arena, parent_lease.clone())
# converter omitted -> default_lifetime_converter, which is:
#   def default(key: Lease[Y], base: Lease[X]) -> Lease[Y]:
#       base.discard()
#       return key
```

**Why it works.** Holding a `Lease[Y]` is fine because `Y` outlives `X`
anyway. When `X` drains, the consumer's foreign-lease row tears down and
the stored `Lease[Y]` is discarded — but by that point `Y`'s drain ordering
has already been settled by the parent / outer relationship. No cycle.

**Pitfall.** Using this when `Y` does *not* in fact outlive `X` (e.g. they're
peers, or `Y` is derived from `X`). The stored `Lease[Y]` will then pin `Y`
in a way the design didn't anticipate — most likely you'll see the symptom
in case 2 territory (cycle) instead.

---

### Case 2 — `Y` is derived from `X` (and possibly other gating tokens)

**When.** Some other party (the "issuer of `Y`") set up a
`DerivedLocalLifetime` whose inputs include `X` plus a "key" token `K` they
control. They want to hand you a way to access an arena with token `Y`, and
they're willing to give you the converter recipe along with a clone of the
key. You hold leases on `X` already (it's your own brand).

This is the classic "service receiving its consumer's lifetime as an input"
shape. In the acceptance test it's how task 2 sends t1 and t3 a way to
re-derive a `Lease[scope2_token]`.

**Use.** Wrap `exchange_forward`. Pass a key on `K`'s brand (not on `Y`'s).

```python
def converter(key: Lease[K], base: Lease[X]) -> Lease[Y]:
    return derived.exchange_forward({key_token: key, x_token: base})

arena.lazily_attach_from_arena(my_lease, node_id, source, key_clone_on_K, converter)
```

**Why it works.** Each load mints a *fresh* `Lease[Y]` from a freshly-cloned
`base = Lease[X]`. The minted lease lives only for the duration of one
source-arena call, then is discarded — so its vias on `X` and `K` are
released right away. The store's persistent foreign-lease row holds only a
`Lease[K]`, which lives on a brand independent of `X`, so `X`'s drain is
not pinned by anything in the consumer's row.

**Pitfall — the cycle.** Don't store a `Lease[Y]` directly (e.g. by minting
one at attach time and using *that* as the key). The stored output lease
would carry a via on `X`, registered on `X`'s ULL. `X` then can't drain
until that via is released; the via won't release until the consumer's
foreign-lease row tears down at `_on_token_end`; that callback only fires
when `X`'s token ends. Hang.

This is the hazard that case 2 is structurally designed to avoid. If you
find yourself wanting to store a `Lease[Y]` "for performance" — don't. The
per-load mint is cheap, and storing the output is the deadlock.

---

### Case 3 — `X` is derived from `Y` (we are downstream of `Y`)

**When.** `X` is your *own* derived lifetime, and `Y` is one of its inputs.
Any live `Lease[X]` you hold therefore already carries a via on `Y` — the
mechanic from §2 above. You don't need to be given anything by an external
party; the structural requirement that `Y` is alive is baked into your
own scope.

In the acceptance test it's how task 2 brings task 1's arena into scope2:
`scope2_token` is derived from `t1_token`, so any `Lease[scope2_token]`
already witnesses `t1_token` being alive.

**Use.** Wrap `exchange_reverse`. Pass `STATIC_LIFETIME.lease()` as the key.

```python
def converter(key: Lease[Static], base: Lease[X]) -> Lease[Y]:
    key.discard()
    inputs = derived.exchange_reverse(base)  # consumes base
    for tok, ls in inputs.items():
        if tok is not y_token:
            ls.discard()
    return inputs[y_token]

arena.lazily_attach_from_arena(my_lease, node_id, source,
                               STATIC_LIFETIME.lease(), converter)
```

**Why it works.** `exchange_reverse(base)` consumes `base` and returns fresh
clones of every via — including a `Lease[Y]`. The minted `Lease[Y]` is
short-lived (used for one source-arena call). The converter itself has no
real lifetime dependency: it works as long as the closure capturing the
`derived` reference is reachable, which is bounded by the consumer scope's
own foreign-lease row, which is torn down at `_on_token_end`.
`STATIC_LIFETIME` is the formal way to express "no gating needed" — its
token never ends, its leases never break, and the foreign-lease row's
break-callback never fires.

**Pitfall — the same cycle, in reverse.** Don't try to use `Lease[Y]` as
the stored key here. That would make the foreign-lease row hold a clone of
`Y`'s lease, pinning `Y`'s drain. Then if anything wants to end `Y` (e.g.
some other party drops the last external clone) the consumer's row would
have to tear down first — but the row only tears down when the consumer
itself drains, which (since `X` is derived from `Y`) only happens when `Y`
ends. Hang.

The rule: if `X` is derived from `Y`, the consumer already has all the
structural witness it needs via the consumer's *own* lease — don't try to
store any `Y`-flavored lease.

---

## 4. The acceptance test as a worked example

`tests/test_entity_ref_scope/test_derived_scope_acceptance.py` performs three
attaches across three tasks. All three cases appear, exactly once each.

### Tokens

| Token | Owner | Notes |
|---|---|---|
| `t1_token` | task 1's `scope1` (Unconditional) | task 1's main lease + clones sent to t2 and t3 |
| `key_ull_token` | task 2's private key (Unconditional, internal) | minted inside `_with_derived_lifetime`; clones sent to t1 and t3 |
| `scope2_token` | task 2's `scope2` (Derived; inputs = `{key_ull_token, t1_token}`) | task 2's main lease |
| `scope3_token` | task 3's `scope3` (Unconditional) | local to task 3 |

### Attach 1: `scope2` brings in `arena_from_t1`

- Consumer X = `scope2_token`; source Y = `t1_token`.
- `scope2_token` is *derived from* `t1_token` (`t1_token` is in the input
  set). **Case 3.**
- Converter: `t2_attach_converter` runs `exchange_reverse(base)` where
  `base = Lease[scope2_token]`, returns `inputs[t1_token]`.
- Stored key: `STATIC_LIFETIME.lease()`.
- Why it matters here: storing a `Lease[t1_token]` directly (e.g. one of
  the spare clones t1 sent over) was the test's first attempt, and produced
  exactly the case-3 cycle described above — `t1_token` couldn't drain
  while scope2's row held a clone, scope2's row only tore down when
  `scope2_token` ended, `scope2_token` only ended when an input cascade
  fired, and `t1_token` ending was supposed to be that input cascade. The
  case-3 pattern fixed it.

### Attach 2: `scope1` (= task 1) brings in `new_arena_t1` from scope2

- Consumer X = `t1_token`; source Y = `scope2_token`.
- `scope2_token` is *derived from* `t1_token` — but from task 1's
  perspective, `Y` is the derived one and `X` is the input. **Case 2.**
- Converter: `t2_forward_converter` (sent over the wire from task 2) runs
  `exchange_forward({key_ull_token: key, t1_token: base})`.
- Stored key: a clone of `scope2._key_lease` (a `Lease[key_ull_token]`)
  that task 2 sent along with the converter.
- Why it matters here: this is the "consumer borrowing access to a
  derived-output lifetime" pattern, mediated by a key on a third brand
  (`key_ull_token`). Task 2 mints `key_ull` privately exactly so that this
  brand exists.

### Attach 3: `scope3` brings in `new_arena_t3` from scope2

- Consumer X = `scope3_token`; source Y = `scope2_token`.
- `scope2_token` has no derivation relationship to `scope3_token`. But task 3
  already holds (or can mint) a `Lease[scope2_token]` directly — by running
  task 2's converter on its own clones of `(key_ull_token, t1_token)` it
  obtained at receive time. **Case 1.**
- Converter: `default_lifetime_converter` (no custom converter passed).
- Stored key: a `Lease[scope2_token]` minted via
  `converter_t3(key_lease_t3.clone(), lease_from_t1.clone())`.
- Why it matters here: case 1 is the "I already have a `Lease[Y]`, just
  hold onto it" pattern. The default converter's `(key, base) → key`
  shape encodes "trust the stored lease, ignore the consumer's lease on
  this attach". Task 3 reduced its case-2 bridge to a case-1 attach by
  evaluating the converter once at receive time and holding the result.

### Reading the test

If any of the three attaches looks confusing in isolation, the procedure is:

1. Identify `X` and `Y` for the attach.
2. Identify which derivation relationship (if any) connects them.
3. Map to one of the three cases.
4. The converter shape and key brand follow mechanically.

The test exists in part to be the canonical reference for "what a
mixed-lifetime data flow actually looks like in practice." Future substages
(particularly substage 5's arena-over-CapIR) reproduce the same shape with
Cap-IR-marshalled equivalents — knowing the three cases first makes that
replay much easier to follow.

---

## 5. Quick decision procedure

When you have a new attach, ask in order:

1. **Will `Y` outlive `X` for sure** (parent of `X`, or some other
   structural reason it has to be alive longer)? → **Case 1**: default
   converter, key = `Lease[Y]`.

2. **Is `Y` derived from `X`** (`X` appears in `Y`'s input set)? → **Case 2**:
   converter wraps `exchange_forward`, key on a separate gating brand
   `K`. Storing a `Lease[Y]` would deadlock.

3. **Is `X` derived from `Y`** (`Y` appears in `X`'s input set)? →
   **Case 3**: converter wraps `exchange_reverse(base)`, key =
   `STATIC_LIFETIME.lease()`. Storing a `Lease[Y]` would deadlock the
   other way.

4. **None of the above.** This usually means you don't actually have a
   structural relationship to lean on. Convert externally first: mint a
   `Lease[Y]` from whatever leases you have, then use case 1 to hold it.
   This is what task 3 does in attach 3 of the acceptance test.

If the decision comes out "case 2 *or* case 3, depending on perspective,"
you're standing at the boundary between two parties' lifetimes and need
to think carefully about whose perspective you're encoding. The test's
attach 1 (scope2 attaching from t1's territory, **case 3**) and attach 2
(scope1 attaching from scope2's territory, **case 2**) are mirror images
of the same `t1_token`–`scope2_token` derivation; the case differs because
the *consumer* differs.
