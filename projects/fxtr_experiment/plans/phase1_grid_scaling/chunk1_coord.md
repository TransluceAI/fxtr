# Chunk 1 — CoordView: the path-less coordinate view

Scope: a 1D ephemeral "grid" that is pure metadata — no path, no storage,
no cells. Its value along its single axis *is* the coordinate. Purpose:
bind it as a task input whose axis gets vmap-consumed, so each task cell
reads a scalar and learns its own coordinate — the sanctioned way in,
since vmap-consumed axes are deliberately unobservable ("the task cannot
tell what they were bound to").

Naming: coord-flavored throughout — the settled slogan is "coordinates
are structure, not data", and this view *is* the coordinate: `CoordView`
/ `CoordBinding` / view kind `"coord"`, constructors `int_coord(name)` /
`str_coord(name)`. (The phase-scoping round called this `EnumerateView`;
the rename is a chunk-1 design-round decision.)

Design (phase-scoping round + the chunk 1 design round):

- **Coordinates are structure, not data.** Reading a coord view is
  unrecorded-pure: no observation, no record entry, no staleness edge —
  a cell's coordinate can never change, so observing it creates no
  dependency, and the safety pass needs no special handling (re-minted
  grants are identical by construction).
- **No wire op.** For a consumed axis, the value is resolved runner-side
  at grant-mint time (`mint_rule_grants` runs per-run and gains the
  cell's holder coords) and rides the `ViewGrant` as an extra, like
  `schema`/`outputs`. For a kept axis, ``value(i=5) == 5`` is local
  shadow arithmetic. `CoordView.value()` is a pure **sync** local call
  in every case (async would falsely suggest an op); the view has no
  backend calls at all. The common case is the fully-consumed zero-axis
  received view, read as ``value()`` with no args.
- **Received grants are token-backed; forwarding just works.**
  Rule-input coord grants register a true spec in the run's
  `SpecRegistry` like any grant, so a task forwarding a received coord
  view (consumed or kept) into a child ``open_task`` sends a plain
  ``WiredArg``, and the canonicalizer translates its ``Param``/``Keep``
  template through the mapped relation unchanged — batch-invariance
  rejection included. The token is never *called* (no effectful op
  exists; the engine's ops reject coord tokens); it carries identity so
  forwarding never has to literalize a per-cell value. Only locally
  created coord views cross as the token-less wired-arg variant.
- **Runner-side representation: disjoint spec classes.** Rather than a
  degenerate path (empty tuples collide with the root-subtree spec;
  marker steps are duck-typing hacks), `ViewSpec` splits into a family
  of disjoint frozen dataclasses sharing the discriminator field
  ``kind`` (restricted per-class ``Literal`` types, so kind checks
  narrow for the typechecker) plus ``axes``/``mode``: a stored-grid
  family carrying ``path``/``target_axes`` and the flat-template
  calculus — with ``output`` living only on the task spec and the
  internals flag only on the subtree spec — and ``CoordViewSpec``
  carrying a single template + the kept axis, with no path at all
  (walking a coord spec's path is not a well-defined operation and now
  cannot be expressed). The split lands first as its own
  no-behavior-change commit; docstring invariants touched by "a spec
  always addresses a stored grid" (`view_spec.py`, `runner_api.py`,
  `model.py`) get rechecked and updated with it.
- **Identity-bearing like any input**: a new `CoordBinding` in
  `TaskRule` (coordinate kind + wiring template + received axis name for
  the kept case) and a wired-arg wire variant for local creation. The
  task can observe "I receive an int coord named ``idx``" — that
  participates in config equality per the observability principle. The
  canonicalizer's name-matching and batch-invariance rules apply
  unchanged. The template may be ``Param`` (consumed), ``Keep``
  (identity), or ``LiteralCoord`` — a literal-bound coord view
  (``int_coord("i").at(i=5)`` wired in) is a constant scalar input,
  allowed as the natural consequence of the calculus, with the literal
  identity-bearing.
- **Creation is local**: `views`-level constructors ``int_coord(name)``
  / ``str_coord(name)`` (no wire call — there is nothing to open);
  usable identically task-side and top-level (the top level trivially
  knows its coordinates; uniformity is free).
- **1D only; ``T: CoordValue = str | int``;** read-only trivially
  (``read_only`` is a no-op); the standard pure rewrites
  (``rename_axes``, ``at``) apply. Kept-axis coord views are allowed —
  the identity function, admittedly (a task could construct one
  itself), but the calculus makes allowing it more natural than
  refusing.

Pieces (expected; adjust while implementing):

- Prestep: the `ViewSpec` disjoint-class split (no behavior change;
  existing tests stay green; own commit).
- Model + encoding: `CoordBinding` in the rule union, canonical entity
  encoding (explicitly tagged, like the rest of the union encodings),
  config-equality coverage.
- Wire: the wired-arg variant for locally created coord views
  (`runner_api`; shape-distinct from ``WiredArg`` — union decodability
  covered in the encoding/RPC tests), the grant extra (resolved value
  for consumed/literal axes).
- Canonicalizer: `view_spec` handling — the coord spec through the
  per-template translation (consume/keep/literal; batch-invariance
  unchanged), the coord branch emitting `CoordBinding` instead of
  `SliceBinding`.
- Runtime: `views.CoordView` contract + the runtime class (pure shadow,
  no backend; sync ``value()``); `spec_registry` minting + token
  registration for received grants (incl. the per-run value resolution —
  `mint_rule_grants` gains holder coords; the safety pass re-mints
  identical grants by construction).
- User surface: nothing new on the wire beyond `open_task` accepting the
  variant; `Workflow` needs no method (creation is a `views` call).

Tests: value round-trip through a real run (task learns its coordinate,
uses it in its output; assert per-cell results); consumed-by-literal and
kept-axis cases; a prefix-axis consume (a task learning its enclosing
subtree coordinate); forwarding a received coord view into a child rule
(consumed→Param translation, kept, and the unmapped batch-invariance
rejection); engine ops reject coord tokens; config identity (same wiring
twice attaches, different received name or kind mismatches);
replay/safety-pass transparency (a resumed run and the safety pass see
identical values, no record rows for coord reads); no staleness edge
(overwriting unrelated inputs never implicates the coord view; nothing
can implicate it).

Notes/log (implementation, 2026-07-05; landed as two commits — 1.1 the
spec split, 1.2 the feature):

- The spec split (chunk 1.1) surfaced three latent looseness spots, all
  closed by the types: `canonicalize_open_task` never verified the
  subtree token's *kind* itself (it relied on `open_child` at the very
  end); `attach_internals` re-tagged a task spec across kinds via
  `dataclasses.replace`; and `spec_from_binding` would silently carry an
  output selection onto a non-task target. Also found while making it
  typecheck: pyright widens literal-typed comprehensions/attributes to
  their base type unless declared — two real annotation fixes.
- The canonicalizer refactor fell out cleanly: the per-template rules
  (`mapped_param` / `consumed_param`) are shared between the stored-grid
  walk and the single-template coord branch, so coord translation is the
  same code path, batch invariance included.
- `WiredCoord` decodes fine beside `WiredArg` in the args union (disjoint
  required fields); covered in-process everywhere and over a real wire by
  the extended RPC smoke test (consume/keep/literal variants + a WiredArg
  in one map, no run needed — open_task only canonicalizes).
- The stored `CoordBinding` uses the map-shaped encoding with a disjoint
  required key (``coord`` vs ``slice``) as the discriminator, so existing
  slice-binding fingerprints are unchanged.
- Wrapper-side edge (guarded in a follow-up commit): passing a coord view
  to a privileged `Workflow` method (e.g. ``add_target``) now raises the
  typed ``ViewAxisError("coord views address no stored cells")`` from
  ``Workflow._sel`` / ``_sel_partial`` — previously a local coord view
  fell over with a bare ``AttributeError`` on the ``_shadow`` seam. The
  capability independently rejects coord selections
  (`user_service._resolve_cell`).
- Deliberate scope cut: `Workflow`/user-side coord views are always local
  (the user trivially knows its coordinates — the user registry never
  mints coord tokens), so the user capability's coord handling is pure
  defense.
