# Chunk 9 — user-surface followups

Scope: the retrospective items from chunk 8 that we decided to fix while
the context is fresh (one commit per item). Two retrospective items were
resolved *no change*, recorded here so they don't resurface:

- **`ensure_loadable` stays a per-call-site rule** (every wire method
  returning ids re-attaches them). No automatic layer: the namespace
  scope keeps tracking only what was explicitly retrieved, and the
  invariant maintenance stays at the point of use rather than a partial
  papering-over that could miss a case silently.
- **`write_handle` on the wrapper is doctrine, not a wart**: handle
  population is a privileged, top-level-only op, so it lives with
  `overwrite`/`clear` as a wrapper method taking a view; handle *views*
  stay attach-only (the task contract, in both worlds).

Pieces:

- [x] 9.1 — **attach-by-path**: `UserCapabilityProto.attach(subtree, key,
      mode="read")` + `Workflow.attach` — mint a grant for an *existing*
      grid from its stored config (no proposed-config matching, no rule
      reconstruction: the stored config carries kind/axes/schema/outputs,
      which is everything a grant needs, task grids included). Records
      nothing, creates nothing; missing key faults. Kind rules: subtree
      grants stay write (the invariant), task grants are read-shaped,
      checkpoint grids refuse (task internals). This is the "open
      whatever grid is here" affordance the wrapper needs for interactive
      use and view reconstruction after a reopen.
- [x] 9.2 — **typed re-raise at the consuming boundaries**: workflow
      errors crossing a service boundary arrive as transit ``IRException``
      (category = original class name); the view runtime's effectful ops
      and the `Workflow` methods reconstruct the documented typed
      exception (chained from the transit form). Ergonomics only —
      nothing durable is classified from it (runner-side classification
      is unchanged), unknown categories pass through untouched, and the
      raw-boundary tests that deliberately assert the transit form stay
      as they are.
- [x] 9.3 — **explicit user view-backend adapter**: `_UserViewBackend`
      over the capability proxy replaces the structural cast, so the
      wire protocol stops being shaped by the view runtime's seam — the
      ``mode="read"`` default comes off `UserCapabilityProto.open_handle`
      (the adapter supplies it), and the deliberately-absent
      ``open_checkpoint`` / ``run_step`` get intentional errors instead
      of raw unknown-method failures.
- [x] 9.5 — **internals browsing** (`attach_internals`): a read-only
      subtree grant on one task cell's private key-space, sanctioned by
      subtree transparency (the owner may *look*). `list_keys`/`attach`
      recurse over it with everything forced read-mode (no writes, no
      create rights — introspection, not intervention); checkpoint grids
      list but don't attach (step-*entry* browsing is a future
      affordance). `Workflow.internals(task_view, **coords)` is the
      wrapper affordance.
- [x] 9.4 — **auto-release-on-drop for `bind_handle`**: the release impl
      carries a ``_fxtr_mount_lifespan`` whose teardown releases the
      binding, and mounts on the *dispatch call scope* (which the
      framework keeps alive exactly as long as the caller holds the
      returned ref). Intended semantics: drop-detection granularity is
      wherever the caller's bridge lives (in-process through `open_local`
      that is the runner scope; over RPC the client session scope) — the
      guarantee is that a release capability stranded in an
      already-closed scope cannot leave a binding permanently stuck, NOT
      that teardown is prompt when everything shares one scope. Explicit
      ``release()`` stays the immediate path. Verify teardown ordering at
      `open_local` exit; if the call-scope mount fights the drain, fall
      back to the workstation `service_impl.py` end-callback pattern or
      `fxtr2.services.subscription_util`'s shape.

Tests: attach round-trip after a session reopen (including a task grid,
reading its output through the attached view; checkpoint refusal; missing
key); typed exceptions catchable via the view/wrapper surfaces
(`pytest.raises(WriteConflictError)` etc.); adapter behavior for the
absent methods; auto-release (bind in a sub-scope, drop it, binding gone;
bind and close `open_local`, clean exit).

Notes/log:

- **9.1** done. Notes:
  - Checkpoint attach refusal was initially defense-in-depth only (no
    top-level subtree grant reached a task grid's key-space); 9.5 then
    added the deliberate way in (``attach_internals``), making the
    refusal a real surface rule: checkpoints list but don't attach.
  - ``mode`` applies to data/handle only; subtree grants stay write (the
    creation-site invariant) and task grants are read-shaped.
- **9.2** done (`errors.typed_wire_call` + routing at every view-runtime
  effectful op and `Workflow` method; the doctrine bullet in
  `design_overview.md` gained the ergonomic half). This also closes the
  cross-cutting typed-re-raise item first noted while landing the
  task-side step wiring: task code now catches the documented types too.
  Raw-boundary tests that deliberately assert the transit form
  (`test_task_service`) are unchanged; the user-surface tests switched to
  `pytest.raises(<TypedError>)`, which is the point.
- **9.3** done (`_UserViewBackend`: ``__getattr__`` forwarding for the
  shared shapes + the three explicit divergences — attach-only
  ``open_handle`` supplying read mode, intentional ``ViewAxisError``\ s
  for ``open_checkpoint``/``run_step``). The wire proto's ``mode``
  default is gone on both the protocol and the impl.
