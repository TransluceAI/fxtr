# Stage 9 — Stage 5: Auto-fallback provenance + scope name inference

## Goal

Today a `FxtrScope` is binary: either it was constructed with
`provenance_context=…` (everything stored / loaded round-trips through
provenance form) or it wasn't (every layered child ID is stripped to
its bare data form on store). The strip behavior is lossy: if a
prov-bearing FxtrID reaches a non-provenanced scope, its provenance
trace disappears the first time the scope stores something that
references it.

This stage makes the strip behavior opt-in by introducing a new
per-scope mode:

```python
store_provenance: Literal["auto", "if_marked", "strip"] = "auto"
```

- **`"strip"`** — always strip, even when children carry provenance.
  Equivalent to the old "non-provenanced" behavior. Cannot be combined
  with `provenance_context=…`.
- **`"if_marked"`** — the *previous* default: store-through-provenance
  only when the cbor's `$fxtr.ctx` marker itself carries a provenance
  parent; otherwise strip.
- **`"auto"`** (new default) — store-through-provenance when the
  marker is provenanced *or* when any child reference in the cbor is
  provenanced. In the second case we substitute the marker with a
  prov-bearing one: the scope's own `provenance_context` c-node if it
  has one, otherwise a lazily-minted **fallback c-node** built from a
  new `FallbackContextMarker` system entity tied to the scope's name.

The motivation is that provenance information should flow naturally
through the system when it's there, without forcing every intermediate
scope to opt in. Scopes that *want* the strip behavior (e.g. a sandbox
that intentionally discards traces) still get it via `store_provenance="strip"`.

Two minor pieces ride along:

- Every `FxtrScope` gets a non-optional `name: str` after construction.
  When the caller doesn't supply one, we infer it from
  `inspect.currentframe().f_back.f_code.co_name` (the function the
  caller is in). The name surfaces in `repr(scope)` for debugging and
  becomes the `scope_name` payload of the fallback c-node.
- `ContextEntity` and `PathRootEntity` in `system_entities.py` are
  unused and get deleted.

## Background — what already exists

- **Marker layering.** A scope's `$fxtr.ctx` marker is either bare
  `FxtrID(".")` or `FxtrID(".", _provenance_parent=c_node)`. The
  marker is sync-copied into the scope's arena at construction (see
  `_apply_context_marker_override` and
  `_track_provenance_context_from`).
- **`store_cbor` dispatch.** The current implementation in
  `entity_ref_scope.py` branches on `ctx.inner_id._provenance_parent
  is not None`:
  - bare → `_bare_project` strips any child layering before handing
    to `arena.store`.
  - prov → `_project` lifts every child, u-wraps bare data IDs, and
    re-stores into the arena's n-node path.
- **`load_cbor` rewrap.** `_load_arena_cbor` re-attaches the prov
  parent to each child after the n-node load. This stage does **not**
  change load behavior — under `"strip"` mode, if a layered ID
  somehow ends up loaded (e.g. via lift from a parent), the rewrap
  still happens; only stores discard prov.
- **System entities.** `system_entities.py` defines `ContextEntity`,
  `PathRootEntity`, `FxtrBasicType`, plus the helper `define_type`.
  Grep confirms `ContextEntity` and `PathRootEntity` are not
  referenced anywhere except their own definitions.
- **`FxtrScope.create_c_node` / `FxtrScope.create_u_node`** —
  scope-level wrappers we use to mint the fallback c-node.

## Design

### `FallbackContextMarker` system entity

New `DataclassEntity` in `fxtr2/data/system_entities.py`:

```
FallbackContextMarker
    scope_name: str
    value: FxtrID = "."  (default_factory, hardcoded to the ground node)
```

Acts as the d-node payload of the lazily-minted fallback c-node. The
`value` field is required because `MemoryArena.create_c_node` validates
the cbor has one; we hardcode it to `.` so the resulting c-node
unwraps to `.` (which is what the marker-override path requires).

Two scopes constructed from a function with the same `co_name` (and
no explicit `name=`) will produce the same `FallbackContextMarker`
d-node, hence the same c-node. This is intentional dedup — fallback
provenance is supposed to be low-noise; collisions just mean "these
all came through the same code path."

Registration: `track_builtin_id("fallback_ctx")` (mirrors the existing
`track_builtin_id("ctx")` pattern).

### Delete `ContextEntity` and `PathRootEntity`

Both are unused. Remove the class definitions and their
`track_builtin_id("ctx")` / `track_builtin_id("pathroot")` calls. No
shim or alias. We keep `FxtrBasicType` and `define_type` as-is.

(The `"ctx"` builtin name is *also* used by test helpers via
`FxtrID.builtin("ctx")` to build c-node `$fxtr.type` fields, so the
builtin string itself isn't going anywhere — just the dataclass.)

### Scope name inference

`FxtrScope.__init__` adds:

```
if name is None:
    frame = inspect.currentframe()
    if frame is not None and frame.f_back is not None:
        name = frame.f_back.f_code.co_name
    else:
        name = "<unnamed>"
```

Same block in `FxtrScope._with_derived_lifetime`. Internal call sites
that don't want the inferred name (it would be e.g. `_dispatch_body`)
should pass `name="..."` explicitly. Out of scope: walking past
fxtr2-internal frames automatically — keep the rule simple, fix
specific ugly cases by passing `name=` explicitly when they show up.

`self._name` becomes non-optional. `__repr__` no longer needs the
`if self._name:` branch (it always has one).

### The three `store_provenance` modes

Add `store_provenance: Literal["auto", "if_marked", "strip"] =
"auto"` to both constructor surfaces. Stored as `self._store_provenance`.

**Validation in `__init__` / `_with_derived_lifetime`:**

- `store_provenance="strip"` plus `provenance_context is not None` →
  `ValueError("strip mode cannot be combined with provenance_context")`.
  (No separate `provenance_context_from_scope` check —
  `_apply_context_marker_override` already errors when
  `_from_scope` is supplied without a `provenance_context`.)

**Inheritance:** `store_provenance` always defaults to `"auto"` — it
does *not* inherit from the parent. Each scope makes its own choice.

**Marker setup under `"strip"`:** `_apply_context_marker_override`
gets a short-circuit at the top — if mode is `"strip"`, leave the
bare `.` marker alone, do *not* walk parents for an inherited
provenance marker. `assert provenance_context is None` belongs here
too: by validation it should already have been rejected, so reaching
this branch with a non-None context is a bug. The scope behaves as
a "provenance-free island" from the marker's perspective.

(Loads through a `"strip"` scope still rewrap children with their
prov parents per `_load_arena_cbor` — only stores discard. This
matches the user's preference: "lets just strip on store. loads can
preserve their provenance.")

### `store_cbor` decision table

The cbor's `$fxtr.ctx` marker is `ctx_id`. Walk the cbor once
(visit_ids) to compute:

- `marker_has_prov = ctx_id._provenance_parent is not None`
- `any_child_has_prov` = any *non-marker* FxtrID reference in the
  cbor has `_provenance_parent is not None`

```
mode = self._store_provenance

if mode == "strip":
    bare path  (current _bare_project)

elif mode == "if_marked":
    if marker_has_prov:
        prov path  (current _project)
    else:
        bare path  (current _bare_project)

elif mode == "auto":
    if marker_has_prov:
        prov path  (current _project; unchanged)
    elif not any_child_has_prov:
        bare path  (current _bare_project; no marker rewrite)
    else:
        # rewrite the marker, then take the prov path
        new_marker_id = self._get_or_mint_fallback_or_own_marker()
        rewritten_cbor = replace $fxtr.ctx in the cbor with
            RawFxtrEntityMarkerID(inner_id=new_marker_id)
        prov path on rewritten_cbor
```

The "marker_has_prov" check and the "scan children for prov" walk can
share a single cbor traversal. The marker rewrite is a shallow dict
copy (top-level field replacement).

### `_get_or_mint_fallback_or_own_marker`

Returns a full layered `FxtrID(".", _provenance_parent=<c_node>)`
ready to drop into `$fxtr.ctx` directly — the caller doesn't do
any wrapping. Pseudocode:

```
def _get_or_mint_fallback_or_own_marker(self) -> FxtrID:
    if self._context_marker._provenance_parent is not None:
        # Scope itself has a provenance_context; its own marker is
        # already the layered ".", prov=c_node" form.
        return self._context_marker

    if self._fallback_marker is None:
        # Bypass the MISSING_FXTR_CTX_SENTINEL replacement in
        # _fxtr_convert_to_cbor by supplying the marker explicitly;
        # avoids any dependency on the active scope. The c-node body
        # only needs a bare "." marker because it's a d-node payload,
        # not a provenanced entity in its own right.
        fallback_cbor = FallbackContextMarker(
            scope_name=self._name,
            fxtr_ctx=RawFxtrEntityMarkerID(inner_id=GROUND),
        )._fxtr_convert_to_cbor()
        c_node = self._main_arena.create_c_node(
            self._main_lease.clone(), fallback_cbor
        )
        self._fallback_marker = FxtrID.from_tag_and_payload(
            ".", b"", provenance_parent=c_node
        )

    return self._fallback_marker
```

`self._fallback_marker: FxtrID | None = None` is initialized in
`_init_arena_and_cache` and lives for the scope's lifetime. It's
keyed implicitly on `self._name` (since the `FallbackContextMarker`
content is `(scope_name=self._name, value=GROUND)`); two scopes with
the same name produce the same `FallbackContextMarker` d-node, hence
the same c-node, hence the same fallback marker.

We use `DataclassEntity._fxtr_convert_to_cbor()` (the existing
"build cbor, don't store" helper) rather than manually constructing
the dict — keeps the FallbackContextMarker honest as a normal
DataclassEntity. Passing `fxtr_ctx=` explicitly side-steps the
helper's active-scope read so the lazy-mint path doesn't need to
touch `ACTIVE_SCOPE`.

### Interaction with `_load_arena_cbor` and downstream paths

No changes. `load_cbor` continues to load whatever was stored:

- Bare-stored entity → bare cbor on load.
- Prov-stored entity (whether from explicit `provenance_context` or
  from auto-fallback) → layered cbor on load, with the fallback
  c-node visible as the child's `_provenance_parent`.

The cache key shape `(data_id, provenance_parent)` already
distinguishes the two; nothing else to do.

### Note on bypassing `_fxtr_store_impl`

If a caller bypasses `DataclassEntity._fxtr_store_impl` and hands
`store_cbor` a dict whose `$fxtr.ctx` is a bare `FxtrID(".")` (not
the scope's marker), the same "auto" rules apply — the scope sees a
bare marker and scans the children. This is intentional behavioral
change for the bypass case, not just the `_fxtr_store_impl` case;
worth a sentence in the docstring.

## Implementation steps

- [x] Add `FallbackContextMarker` to `fxtr2/data/system_entities.py`.
  Field shape: `scope_name: str`, `value: FxtrID = default_factory →
  FxtrID.from_tag_and_payload(".", b"")`. Register via
  `track_builtin_id("fallback_ctx")`.
- [x] Delete `ContextEntity` and `PathRootEntity` from
  `fxtr2/data/system_entities.py` (and their builtins).
- [x] In `FxtrScope.__init__` and `FxtrScope._with_derived_lifetime`,
  add name inference via `inspect.currentframe().f_back.f_code.co_name`
  when `name is None`. Drop the `if self._name:` branch in
  `__repr__`. Pass `name="GLOBAL_SCOPE"` at the global construction
  site.
- [x] Add the `store_provenance` parameter to both constructors,
  store as `self._store_provenance`. Add validation: `"strip"` +
  `provenance_context is not None` is a `ValueError`.
- [x] In `_apply_context_marker_override`, short-circuit at the top
  when `self._store_provenance == "strip"`: assert
  `provenance_context is None` (constructor validation should already
  have caught it), skip the parent-inherit branch, leave the bare `.`
  marker.
- [x] In `_init_arena_and_cache`, initialize `self._fallback_marker:
  FxtrID | None = None`.
- [x] Add `_get_or_mint_fallback_or_own_marker(self) -> FxtrID` on
  `FxtrScope` per the design above (returns a layered
  `FxtrID(".", _provenance_parent=…)` directly; uses
  `DataclassEntity._fxtr_convert_to_cbor` to build the cbor).
- [x] Rewrite `FxtrScope.store_cbor` to dispatch on
  `self._store_provenance` per the decision table. Share one
  cbor traversal across "marker has prov?" and "any child has prov?"
  checks. In the "auto + override" branch, shallow-copy the cbor with
  `$fxtr.ctx` replaced by
  `RawFxtrEntityMarkerID(inner_id=self._get_or_mint_fallback_or_own_marker())`
  and delegate to the existing prov path.
- [x] Update CODE_OVERVIEW.md: add a short section describing
  `store_provenance` modes; update the `system_entities.py` summary
  to mention `FallbackContextMarker` and drop `ContextEntity` /
  `PathRootEntity`.
- [x] Adapt the existing provenance tests
  (`tests/test_entity_ref_scope/test_provenance_flow.py`,
  `test_provenance_context.py`) for the renamed semantics. The
  `TestBareModeStripsLayeredChildren` class' behavior moves to
  `store_provenance="strip"`; equivalent tests for `"if_marked"` cover
  the rest of the old bare-mode strip path.
- [x] Add new tests in
  `tests/test_entity_ref_scope/test_provenance_auto_fallback.py`
  covering:
  - `store_provenance="auto"` with no children prov → bare store
    (no marker rewrite).
  - `store_provenance="auto"` with a layered child + scope has
    `provenance_context` → marker substituted with scope's c-node,
    prov-stored.
  - `store_provenance="auto"` with a layered child + scope has *no*
    `provenance_context` → marker substituted with a fallback c-node;
    second call reuses the same fallback c-node.
  - Two scopes with the same inferred name share the same fallback
    c-node ID.
  - `store_provenance="strip"` + layered child → strips on store.
  - `store_provenance="strip"` + `provenance_context=` → ValueError
    at construction.
  - `store_provenance="strip"` + non-isolated parent with
    `provenance_context=` → child scope still has bare `.` marker
    (no inheritance).
  - `store_provenance="if_marked"` matches current behavior on the
    existing test cases.
  - Name inference: a `FxtrScope()` constructed inside a test
    function has `scope._name == "<that test's co_name>"`.
- [ ] Update CODE_OVERVIEW.md a second time once tests are passing
  (in case the implementation reveals naming / surface tweaks).

## Out of scope

- Walking past fxtr2-internal frames in name inference. Internal
  call sites pass `name=` explicitly when the inferred name would be
  ugly; we add explicit names only where they actually show up.
- Changing `load_cbor` / `_load_arena_cbor` behavior. Loads continue
  to preserve provenance regardless of mode; only stores discard
  under `"strip"`.
- Persisting fallback c-nodes to namespaces. The d-node + c-node live
  in the scope's main arena; if a caller wants the fallback c-node to
  travel across scope boundaries, they `lift` / `copy_to` it like any
  other tracked node.
- Surfacing the chosen mode in `repr(scope)` or logs beyond what
  already exists. The `scope_name` is in the fallback c-node payload
  for after-the-fact inspection; that's enough.
