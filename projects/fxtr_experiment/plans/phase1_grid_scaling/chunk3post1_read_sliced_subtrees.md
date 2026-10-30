# Chunk 3.post1 — read-sliced subtree views: generalize the full-frame rule

Scope: replace the internals-only carve-out in the full-frame invariant
(added during chunk 2; marked ``TODO(chunk3post1)`` in
`view_spec.check_subtree_full_frame`) with the general rule it is a narrow
instance of, and make the surface coherent with it. Deliberately scheduled
*after* the axis-semantics suites (chunks 2–3): the refactor touches every
open path, and the suites are the coverage we want in place first.

## The rule (settled in the chunk 2 design round)

**Full-frame is a property of creation capability, not of subtree-ness.**
The invariant's two rationales — broadcast creation (a subtree key's
config is grid-level, so creating through a partial view asserts it from
a partial vantage) and canonicalization's mapped-relation bijection — are
both about *creating*. Therefore:

- A subtree view that can create (write mode — write on a subtree *is*
  create access) must be full-frame, as today.
- A **read-mode subtree view may be sliced/literal-bound**: it cannot
  create (both open paths independently refuse creates without write
  access), so nothing can broadcast, and reads through the literal prefix
  resolve like any other slice.
- ``canonicalize_open_task``'s *creation path* stays strict regardless of
  mode (it is the child rule's translation frame and must be total);
  sliced read subtrees as *args* already flow (see below).
- The diagonal (duplicate-Param) rejection stays unconditional.

Under this rule ``attach_internals`` needs no special case at all: it
mints an ordinary read-mode sliced subtree view.

## What already works (traced during the chunk 2 round)

- **Forwarding a sliced read subtree as a task arg** needs no new
  machinery: ``translate()`` passes literals through (a code-literal
  slice is batch-invariant trivially), the stored binding carries the
  literal in its slice, and ``spec_from_binding`` reconstructs the
  callee's true spec *with the literal baked in* — so the callee's opens
  work over the existing wire unchanged.

## What has to change

1. `view_spec`: factor the unconditional diagonal check out of
   ``check_subtree_full_frame``; ``open_child`` requires full-frame iff
   ``mode == "write"`` (read-mode gets the diagonal check only); delete
   the internals carve-out + its TODO.
2. `view_spec.bind_keeps`: the subtree rejection becomes write-only
   (read-mode subtree binds are legal).
3. **ViewSel-carrying opens** (the bulk): the six ``open_*`` ops take a
   ``ViewSel`` instead of a bare ``ViewToken`` — the bare token was
   justified exactly by "subtree grants are never sliced", which this
   chunk retires. A holder-local ``at()`` slice lives in the shadow, so
   without this, slice-then-open would silently address the unsliced
   subtree. Registry applies ``sliced()`` before ``open_child``. Layers:
   `runner_api` protos, `view_runtime.ViewBackend` +
   ``RuntimeSubtreeView`` (emit ``sel_partial()``), `ops_engine._open` +
   wrappers, `task_service.RunCapabilityImpl`, `user_api` proto,
   `user_service.UserCapabilityImpl` + ``_UserViewBackend``, test fakes.
   ``attach`` follows (it mints a child view of cells);
   ``list_keys``/``delete_grid`` stay token-shaped (key-space/grid-level
   ops — cell slicing is meaningless for them).
4. `views.SubtreeView.at` / ``RuntimeSubtreeView.at``: legal on read-mode
   views; write-mode keeps raising, with a message suggesting
   ``read_only()`` first when browsing is intended.
5. Docs: ``check_subtree_full_frame`` /  module docstrings state the
   creation-capability rule once; `runner_api`'s wire-model section drops
   the bare-token justification; `design_overview.md`'s "creation sites
   are total" bullet gains the read-mode principle.

## Tests

- Slice → open → read in one holder (the ViewSel-carrying-open proof).
- Forward a sliced read subtree as a task arg; callee opens + reads
  through the literal prefix.
- ``open_task`` *through* a sliced view rejected (creation stays total);
  ``at()`` on a write-mode subtree view rejected.
- Internals browsing (multi-axis cells) rides the general rule — the
  chunk 2 scenario keeps passing with the carve-out deleted.
- The chunk 2–3 suites as regression coverage over all open paths.

Notes/log (implementation, 2026-07-05):

- Landed as planned, with the chunk 2–3 suites passing unchanged through
  the whole wire conversion (their coverage did exactly the job this
  chunk was scheduled after them for); the carve-out and its TODO are
  gone.
- **One refinement over the plan**: the mode-gated ``bind_keeps``
  rejection became "**slicing sheds create authority**" — binding a
  subtree spec downgrades it to read whatever the grant's mode. The plan's
  version broke on the first test: ``read_only()`` is a *local* shadow
  downgrade, so the runner-side true spec of a ``ws.read_only().at(...)``
  still says write. Rather than shipping effective mode on every
  selection, the downgrade-on-slice rule makes crafted selections sound
  by construction and keeps the wire unchanged; the view layer's explicit
  ``read_only()``-before-``at()`` requirement stays as intent-visibility.
- **A second tightening found writing the boundary tests**: a sliced
  window could still attach-open an *existing* child with
  ``mode="write"`` — so ``open_child`` now forces children of sliced
  views read-only ("a window is look-only"). The unsliced version of the
  same escalation (write-mode child opens under read-mode subtree grants
  attach fine) was recorded as **open question 7** in
  `notes/design_overview.md`, and resolved in the followup commit right
  after this chunk: child mode never exceeds the subtree grant's mode,
  sliced or not — the sliced special case collapsed into the general
  rule, and mode-less opens (nested subtrees) inherit the grant's mode
  (see the 2026-07-06 discussion-log round).
- New tests: `tests/test_read_sliced_subtrees.py` (top-level slice → open
  → read + attach + the three rejections; sliced subtrees through rules
  and sliced task-side, with window-shaped staleness), plus the spec-level
  downgrade unit test replacing the old rejection test.
