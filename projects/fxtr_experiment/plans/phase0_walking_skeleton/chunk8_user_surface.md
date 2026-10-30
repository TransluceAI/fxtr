# Chunk 8 — user surface + full-slice e2e

Scope: the public way in, per `user_api.py`.

- User capability impl (calculus ops with user semantics — no record, no
  observations, exceptions interactive; user-sourced responsibility;
  `overwrite=`), targets (add/remove/list with attachment status), `clear`,
  minimal presence queries (`list_keys`, `list_cells`), `bind_handle`
  forwarding.
- `Workflow` wrapper: local bootstrap constructor (open both files, start
  runner, mount capability), `root`, privileged methods over views,
  `demand(..., target=)`, `run_targets()`.
- The full-slice end-to-end test from the overview, entirely through the
  public surface, including target persistence across sessions
  (close/reopen the Workflow).
- Beyond this, a test of subtree mechanics to prove that tasks can create
  subtasks: user creates and demands a "workflow" task, passing it an empty subtree,
  and the workflow task itself runs through the end-to-end flow from the
  full-slice into that subtree (creating a data grid, writing to it, creating
  a task that reads from it, demanding that task's result, then wrapping and
  returning that). Proves that the idempotent subset of user capabilities can
  be exercised by tasks themselves.
- Stretch: one `open_rpc_scope` smoke test driving the user capability
  over a real wire.

Tests: as above; plus dangling-target statuses after delete/replace.

Notes/log — done (see the discussion log's 2026-07-05 chunk 8 round for
the pre-implementation decisions: fully-bound targets unique per slice,
remove-by-slice-identity, valueless `CellInfo`, atomic `clear_cells`,
plain-CM bootstrap, `inspect_cell`). Implementation notes:

- Landed as `user_service.py` (impl + `Workflow` + `open_local`),
  mirroring the 7.3 layering: `user_api.py` is pure decorated protocols;
  the concrete `Workflow` moved out of it.
- **`demand_target` joined the wire surface** (decided while
  implementing): `run_targets` needs to demand *stored* (absolute,
  root-frame) slices, but wire sels are token-relative and no granted
  token addresses a target record — reconstructing views would need
  re-attach machinery that doesn't exist. The by-record demand is the
  honest primitive (materialization events are already capability-level);
  `run_targets` stays wrapper sugar looping over it, returning the
  dangling records it refused to demand.
- `open_handle(mode="read")` default keeps the shared views'
  three-argument call working over this capability; creation is
  deliberate (`mode="write"`, via `Workflow.open_handle`). Handle cells
  are populated via `Workflow.write_handle` (handle *views* stay
  attach-only — the task contract).
- `Shadow.sel_partial` added (view_runtime): the multi-cell selection
  emitter for `list_cells` / `clear_cells`; single-cell effectful ops
  keep `sel`'s fully-bound check.
- `bind_handle` returns a mounted release capability; the
  auto-release-on-ref-drop half of the workstation convention is
  deferred (explicit `release()` works; bindings stay bounded by the
  connector's lifetime).
- Served-id coverage strikes again: `list_targets` had to
  `ensure_loadable` each record's `expected_config` (any wire method
  returning ids must re-attach them — the rule from the chunk 7.4
  finding, now applied across the user surface).
- RPC smoke test lesson (test-side, not machinery): each side's
  connection drain waits for the peer to release its grant exports at
  the peer's own exit, so both sides must drain concurrently — a server
  gated on the client's *completed* CM exit is a cross-wait cycle
  (`v2/notes/scope_service_lifetimes.md` has the contract). Signal
  before exiting the rpc scope. With that, the capability drives over a
  real wire unchanged — grants, values, presence queries, inspection.
- The sub-workflow test passes: a task builds a data grid + inner task
  inside its received subtree, wires the inner fn from a *granted*
  handle view, and demands the inner output transitively — the
  idempotent user-calculus subset is exercisable by tasks, surviving
  the safety-pass replay.
