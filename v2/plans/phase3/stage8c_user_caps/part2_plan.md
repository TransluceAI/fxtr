# Substage 2 — Memory-store token-ownership refactor

Detailed implementation plan for substage 2 of
[stage 8c](overview.md#substage-2--memory-store-token-ownership-refactor).

**Goal.** The `MemoryStore` stops minting `UnconditionalLocalLifetime`s on behalf
of callers. Callers mint their own lifetime (a ULL today, a `DerivedLocalLifetime`
starting in substage 3), take the root lease themselves, and then register the
resulting `(token, grant)` pair with the store as a subscriber — the store
records parent/ancestor bookkeeping and hooks the token's end-callback for GC,
without owning the lifetime.

This is purely an ownership refactor. Lease validation semantics, antichain
bookkeeping, foreign-arena registration, and every other store API stay the
same. Nothing outside the token-creation path changes shape.

---

## API change

### Out

```python
def create_token_from_leases(
    self,
    *,
    parents: Sequence[Lease[Any]] = (),
    name: str = "",
) -> Lease[Any]: ...
```

Deleted. No wrapper, no deprecation alias.

### In

```python
def register_token(
    self,
    token: LifetimeToken[Any],
    *,
    parents: Sequence[Lease[Any]] = (),
    name: str = "",
) -> None:
    """Register a pre-existing token with the store.

    The token is minted by the caller (typically as
    ``UnconditionalLocalLifetime(name).token``). This call:

    - Takes (``.take()``) each parent lease; validates each's subject
      against the store's known tokens; stores the taken lease in
      ``_token_data[token].parent_leases`` keyed by the parent's token.
    - Computes ``ancestor_leases`` (transitive closure over parents),
      cloning each ancestor lease once per child token.
    - Subscribes ``self._on_token_end`` to ``token.register_end_callback``
      so the store's GC logic runs when the token drains.
    - Does **not** hold a lease on ``token`` itself. The caller's own
      root lease (and clones thereof) drive the token's lifetime.

    Args:
        token: The caller-owned lifetime token.
        parents: Lease clones for each direct parent of this token in
            the DAG. Each lease is consumed (``.take()``).
        name: Optional label for this token, used only in the store's
            log messages. The token already carries its own name via
            the caller's lifetime; ``name`` here is a tag for debug
            log lines.
    """
```

**Rationale — no cached grant, no grant checking.** The store is a
*subscriber* to tokens minted elsewhere, not a lifetime owner. Only the
lifetime owner (a `UnconditionalLocalLifetime` / `DerivedLocalLifetime`)
can guarantee "there is exactly one valid grant for this token" — that
invariant is bound up with the `Grant` object's identity and lease-
construction. A subscriber that only knows a token has no way to enforce
grant uniqueness; if it pretends to, it's promising something it can't
back. The store therefore validates only what it *can* know: the token
is one it's seen (`lease.subject in _token_data`) and the lease is
active. Any stricter grant-matching lives with owners — `MemoryArena`
also loses its `_expected_grant` cache for the same reason (it's a
view on a token it doesn't own).

This is a principled relaxation, not a code debt. If a future component
owns a token that the store subscribes to, that component already does
grant-matching at the lifetime boundary before any lease gets to the
store.

### Field shape

`LifetimeTokenData.ull` and `LifetimeTokenData.grant` are both deleted.
`ull` was write-only today; `grant` is dropped per the rationale above.
Everything else (`nodes_in_antichain`, `parents`, `parent_leases`,
`ancestor_leases`, `foreign_leases`) is unchanged.

`MemoryArena._expected_grant` is deleted for the same reason. The
arena keeps `_token_cached` (identity-only; set from the lease it
consumed at construction).

---

## Caller migration

### `FxtrScope.__init__` — `fxtr2/data/entity_ref_scope.py`

Non-`_share_token` path changes from

```python
self._main_lease = self._global_store.create_token_from_leases(
    parents=unique_parent_leases, name=name or repr(self)
)
self._token = self._main_lease.subject
```

to

```python
self._scope_ull = UnconditionalLocalLifetime(name or repr(self))
self._token = self._scope_ull.token
self._global_store.register_token(
    self._token,
    parents=unique_parent_leases,
    name=name or repr(self),
)
self._main_lease = self._scope_ull.root_lease.take()
```

Notes:
- `self._scope_ull` is a new instance attribute. Substage 3 consumes it
  (the `_with_derived_lifetime` constructor wants a separate
  `_scope_ull` / `_key_ull` split). For substage 2 it's just
  "the thing we minted".
- `_share_token=True` path is unchanged — it clones a parent's main
  lease and reuses its token; no `register_token` call. Weak-scope /
  share-token removal is substage 3's job.

### `tests/conftest.py::memory_arena`

```python
@pytest.fixture
def memory_arena(
    memory_store: MemoryStore,
) -> Iterator[tuple[MemoryArena[Any], Lease[Any]]]:
    """...docstring updated to reference register_token..."""
    ull = UnconditionalLocalLifetime("test_arena")
    memory_store.register_token(ull.token, name="test_arena")
    lease = ull.root_lease.take()
    arena = memory_store.new_arena(lease.clone())
    arena.track_builtin(lease.clone(), FxtrID.from_tag_and_payload(".", b""))
    try:
        yield arena, lease
    finally:
        arena.close()
        lease.discard()
```

The yield signature stays `(arena, lease)` — the ULL is kept alive for
fixture duration via `lease`'s owner reference, so the fixture need not
expose it. Docstring updated to reference the new API.

### `tests/test_memory_arena.py::_new_arena`

Same pattern: mint a local ULL, register, take the lease, build the
`_LeasedArena`. No test body changes — every test uses `_new_arena` or
the `memory_arena` fixture, not `create_token_from_leases` directly.

### `tests/test_sqlite_store/test_sqlite_store.py`

Three call sites (lines 2226, 2274, 2325). Each is the same pattern:
```python
mem_lease = mem_store.create_token_from_leases(name="load_flat_d")
```
becomes
```python
_mem_ull = UnconditionalLocalLifetime("load_flat_d")
mem_store.register_token(_mem_ull.token, name="load_flat_d")
mem_lease = _mem_ull.root_lease.take()
```

Local underscore-prefixed name to signal it's just alive-for-teardown
scaffolding; optional, leaving it unprefixed is fine too.

### `tests/test_git_store/*` — five files, ~13 call sites

Same migration. The `memory_arena` fixture in `test_git_namespace.py`
(line 115) uses the same pattern as `conftest.py`'s. The inline call in
`test_git_namespace.py` line 1085 (the depth-benchmarking loop) is one
of several identical inline migrations.

### `fxtr2/data/memory_arena.py` — internal cleanup

- `LifetimeTokenData`'s class docstring references
  `create_token_from_leases`. Update to reference `register_token`.
  Drop the `ull` and `grant` field blocks.
- Module-level docstring (lines 1–31): reframes the store-owns-ULL
  mental model as store-subscribes-to-caller-owned-lifetime.
- `MemoryArena` class docstring (line 2261 block) currently says
  "grant-validated against the store's cached grant for this token"
  — change to "subject-validated" and note grant is not cached.
- `MemoryArena.__init__`: drop the
  `self._expected_grant = store._token_data[arena_tok].grant` line.
- `MemoryArena._assert_lease_matches_arena`: drop the
  `validate_lease_grant(self._expected_grant, lease)` call. The
  remaining check (`lease.subject is not self.token`) keeps raising
  `LeaseGrantMismatchError` with a now-more-accurate "subject does
  not match token" message; rename the exception raised here to
  something clearer if it feels off — currently it reuses
  `LeaseGrantMismatchError` for both checks.
- `_take_and_validate`: drop `validate_lease_grant(row.grant, taken)`.
  Keep the subject-in-`_token_data` check and `ensure_active`.
- Imports: drop `Grant` from the `lease_types` import line and
  `validate_lease_grant` / `LeaseGrantMismatchError` from the
  `abstract_arena` import line if no remaining uses. Verify by
  searching the file after the edits.

---

## Body of `register_token`

Transcribed from today's `create_token_from_leases`, minus the ULL
construction, the grant check, and the taken-root-lease return:

1. Guard: if `token in self._token_data`, raise
   `RuntimeError(f"Token {token!r} already registered on this store")`.
   See duplicate-registration note in edge cases.
2. Take each parent lease (`p.take()`), collect
   `(parent_token, taken_parent_lease)` pairs. On any failure during
   the take loop, the `with taken:` unwind discards what we've got.
3. Validate each parent:
   - `parent_row = self._token_data.get(parent_token)` must be non-None
     → `RuntimeError(...)` if unknown.
   - `parent_lease.ensure_active()` → raises on broken/discarded.
   (No grant check — see rationale above.)
4. Compute `ancestors`: for each parent, add the parent token itself
   plus every entry from `parent_row.ancestor_leases`, cloning each
   lease once per new child. First-writer-wins on duplicates (mirrors
   today).
5. `self._token_data[token] = LifetimeTokenData(
   nodes_in_antichain=set(), parents=..., parent_leases=...,
   ancestor_leases=ancestors, foreign_leases={})`. No `ull`, no
   `grant`.
6. `token.register_end_callback(lambda: self._on_token_end(token))`.
   Register *after* populating `_token_data` so that a synchronously-
   fired callback (caller registered an already-ended token — DOA
   edge case) finds a populated row. This matches today's ordering.
7. Debug-log `"Token {token!r} registered with {n} parents"` (adjust
   today's `created` phrasing).

No return value.

### Edge cases

- **Empty parents** (root scopes, and every test-driven standalone
  ULL). `parent_leases` and `ancestor_leases` end up empty; the token
  has no DAG neighbours. Same behaviour as today.
- **DOA token** (caller somehow passes a token whose lifetime is
  already ended — can only happen if caller `.revoke()`d before
  calling `register_token`). The end-callback fires synchronously
  inside `register_token`; `_on_token_end` pops the row we just
  inserted and tears down. The caller is left with a broken lease.
  This is weird but not worse than today's behaviour for a caller who
  immediately revokes a freshly-minted ULL (though today's
  `create_token_from_leases` is simpler because the caller doesn't
  own the ULL). Not something to guard against specifically.
- **Duplicate registration** (caller calls `register_token` twice for
  the same token). Today this was impossible — each call minted a
  fresh ULL. Now it's possible in principle. Guard: if `token` is
  already in `_token_data`, raise `RuntimeError(f"Token {token!r}
  already registered")`. Cheap insurance against caller bugs.

---

## `_on_token_end` — no change

The callback already assumes it runs *in response to* the token having
ended (not as a proactive action on the store's side). It pops the row,
frees nodes, discards parent/ancestor/foreign leases. The only thing
the store used to do that touched the ULL was keeping a reference in
`row.ull` — that reference was never read, so its removal is invisible
to `_on_token_end`.

Worth a comment on `_on_token_end` noting that it runs on the caller-
owned lifetime's end-callback thread (same as today), not a
store-initiated action.

---

## Validation paths — relaxed to token identity

Three call sites of `validate_lease_grant` inside `memory_arena.py`
all disappear:

| Site | Today | After |
|---|---|---|
| `register_token` parent check | `validate_lease_grant(parent_row.grant, parent_lease)` + `ensure_active()` | `ensure_active()` only |
| `_take_and_validate` | subject-in-`_token_data` + `validate_lease_grant(row.grant, taken)` + `ensure_active()` | subject-in-`_token_data` + `ensure_active()` |
| `MemoryArena._assert_lease_matches_arena` | `validate_lease_grant(self._expected_grant, lease)` + `ensure_active()` + `lease.subject is self.token` | `ensure_active()` + `lease.subject is self.token` |

`_make_base_lease` (line 520) doesn't touch grants at all (it only
clones ancestor-lease entries), so it's unchanged.

`validate_lease_grant` and `LeaseGrantMismatchError` themselves stay
in `abstract_arena.py` — they remain the right tool for genuine
lifetime-owner call sites (HTTP client, SQLite session, etc.). Only
the memory store stops consuming them.

---

## Docs to update

- `fxtr2/CODE_OVERVIEW.md`:
  - Line 53 (overview prose): rewrite "each `FxtrScope` calls
    `MemoryStore.create_token_from_leases(...)`" block to describe the
    mint-then-register pattern.
  - Line 462 (memory-arena section preamble): the "store owns an
    `UnconditionalLocalLifetime` per token" sentence needs to flip —
    the store now *subscribes to* caller-owned lifetimes.
  - Line 475 (`LifetimeTokenData` bullet): drop "the owned ULL" and
    "cached grant"; mention only the bookkeeping fields.
  - Line 480 (`MemoryStore` token lifecycle bullet): replace
    `create_token_from_leases(...)` description with `register_token`.
    Note it holds no lease on the token itself.
  - Line 383 (scope bullet): update the parenthetical mention of
    `create_token_from_leases`.
  - Line 489 (`MemoryArena` bullet): update "grant-validated" to
    "subject-validated" and note no cached grant.
- `fxtr2/data/memory_arena.py` module docstring (line 5) — the
  "owned as `UnconditionalLocalLifetime` objects" phrasing now
  describes caller-owned lifetimes that the store subscribes to.

No `LOCAL_CODE_OVERVIEW.md` exists under `fxtr2/data/`. Overview
updates all live in the top-level `CODE_OVERVIEW.md`.

---

## Implementation order

One sitting; suite runs at the end (same cadence as substage 1).

1. `memory_arena.py`: delete `LifetimeTokenData.ull`; replace
   `create_token_from_leases` with `register_token`. Update the
   module + class docstrings.
2. `entity_ref_scope.py`: migrate the one `__init__` caller. Add
   `_scope_ull` attribute.
3. `tests/conftest.py`: migrate the `memory_arena` fixture; update
   its docstring.
4. `tests/test_memory_arena.py::_new_arena`: migrate.
5. `tests/test_sqlite_store/test_sqlite_store.py` x 3.
6. `tests/test_git_store/*` migrations (5 files, ~13 sites).
7. Run `uv run pytest` from `v2/`. Fix any fallout (most likely:
   a test that still uses the fixture name `memory_arena` but
   happens to also import something that broke, or a straggler call
   site that `grep` missed — re-run the grep after).
8. `fxtr2/CODE_OVERVIEW.md` updates.
9. Sanity run of the full suite.

---

## Checklist

- [x] Delete `LifetimeTokenData.ull` and `.grant`; update docstrings.
- [x] Delete `MemoryArena._expected_grant`; drop
      `validate_lease_grant` call from `_assert_lease_matches_arena`.
- [x] Drop `validate_lease_grant` call from `_take_and_validate`.
- [x] Replace `create_token_from_leases` with `register_token`
      (including duplicate-registration guard; no grant param; no
      grant check on parents).
- [x] Prune unused imports (`Grant`, `validate_lease_grant`,
      `UnconditionalLocalLifetime`) from `memory_arena.py`.
      *`LeaseGrantMismatchError` stays — still used for the
      subject-mismatch branch in `_assert_lease_matches_arena`.*
- [x] Migrate `FxtrScope.__init__` non-`_share_token` path.
- [x] Migrate `tests/conftest.py::memory_arena` fixture.
- [x] Migrate `tests/test_memory_arena.py::_new_arena`.
- [x] Migrate `tests/test_sqlite_store/test_sqlite_store.py` (3 sites).
- [x] Migrate `tests/test_git_store/` (5 files, 13 sites).
- [x] Add `TestRegisterToken` in `tests/test_memory_arena.py`:
      duplicate-registration raises `RuntimeError`, unknown-parent
      raises `RuntimeError`.
- [x] Full-suite green (1738 passed).
- [x] Update `fxtr2/CODE_OVERVIEW.md`.
- [x] Update `fxtr2/data/memory_arena.py` module + class docstrings.

---

## Open / "revisit later" notes

- **Subscriber-grant principle.** Chose to drop grant caching in the
  memory store (and in `MemoryArena`) in line with the rule that
  non-owners of a lifetime can only enforce token identity, not
  grant uniqueness. If later we find a case where a subscriber
  genuinely wants to assert a specific grant (e.g. a debugging
  mode), we can reintroduce it as an optional attach-time
  parameter — but it should be clear at that point what the
  stricter promise is buying.
- **`LeaseGrantMismatchError` reuse for subject mismatch.** After
  this substage the arena's only remaining "grant-flavored" check
  is the `lease.subject is not self.token` identity check, which
  raises `LeaseGrantMismatchError`. The error name is now a bit
  of a misnomer; leave it for now (renaming it means touching
  every raise site and hitting the rpc layer), but flag as a
  cleanup opportunity. Possibly a `LeaseSubjectMismatchError` or
  a retitled shared error class.
- **`_share_token` path** remains in `FxtrScope.__init__` for now;
  substage 3 deletes the weak/share-token mechanism entirely. No
  need to touch it here.
- **Duplicate-register guard** is a new runtime error that didn't
  exist before (because `create_token_from_leases` minted tokens
  from scratch). If this trips anywhere unexpectedly once the
  ServiceRef work is in flight, we can loosen it to a no-op. Low
  risk.
