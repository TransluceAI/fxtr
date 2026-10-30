# Stage 2 post-1 — Cleanup pass before stage 3

A small structural refactor that lands before any new agent-facing
functionality from stage 3. No behavior change visible to the agent
or the workstation; the existing stage-2 integration test
(`test_run_script_lookup_entity.py`) is the regression gate.

The cleanup has three connected pieces, all motivated by the
"everything the MCP does serializes through one queue" model. Today
stage 1 + 2 already mostly work this way, but a few places drift —
either by spawning subtasks that aren't load-bearing, or by giving
the wrong owner the wrong lifetime.

This doc captures what to change and why. Item ordering is the
suggested implementation order; (1) is pure plumbing and has no
dependency on (2)+(3), (2) and (3) are coupled.

## Background: the worker-queue model

The intended invariant: every operation the MCP performs runs as
exactly one item drained from the worker queue. Tool calls
(`open_session` / `close_session` / `run_script`) submit jobs;
inbound session events from the workstation also become jobs. The
worker drains one at a time, with full ownership of:

- the `/rpc` connection's `host_scope`,
- per-script `script_scope`s opened during a `_ScriptJob`,
- the local state mirror,
- the workstation `/client` send side.

Concurrent tasks exist (the reader; RPC dispatch tasks during a
script's `runner.run()`) but they don't compete with the worker for
shared state. The reader only routes; RPC dispatch tasks during a
script are structured-concurrency children of the per-script scope
and are fully drained before `runner.run()` returns. The worker's
"one thing at a time" property holds at the granularity that
matters.

The cleanup makes this property structural rather than incidental.

## Routing confirmation (no change)

Per the per-request routing table in `ws_connection.py:555-589`,
`MutationCommittedResponse` and `MutationRejectedResponse` are
routed by `frame.request_id` into the per-request stream tracked by
`_track_pending`, not into the session-event path. So when the
worker awaits its mutation's response, the response "jumps ahead" of
any foreign session events that arrive in the meantime — the
foreign events queue up as `_EventJob`s and are processed only after
the current `_ScriptJob` completes. No reader change is needed for
this.

## (1) Collapse the session-event consumer into the reader

Today the supervisor runs three subtasks: reader, worker,
session_event_consumer. The consumer is a no-op (logs + drops); it's
a stage-1 placeholder for stage-3 functionality.

**Change:** drop the `_session_event_send`/`_recv` stream and the
consumer task. The reader's `SessionEventFrame` branch wraps the
frame in an `_EventJob(frame)` and `send_nowait`s it onto the
worker's `_job_send` directly.

**`_Job` union grows:** add `_EventJob(frame: SessionEventFrame,
completion: Future[None] | None = None)`. Completion is None for
reader-pushed events (fire-and-forget) — added later if a job needs
to surface back to a submitter. Stage 2 dispatches `_EventJob` with
a no-op handler (logs at debug level, returns); stage 3 fills in
the foreign-state-update reload + invoke-result promotion.

**Backpressure:** `_job_send` already has `max_buffer_size=64`. If
the reader's `send_nowait` raises `WouldBlock` for a session event,
log + drop (matching today's behavior when the session-event stream
overflows). Stage 3 may revisit if backpressure becomes a real
problem, but with one-mutation-in-flight semantics it shouldn't.

**Subtasks after this change:** reader + worker. That's it.

## (2) Per-script `McpHostImpl` + per-script `script_scope`

Today `McpHostImpl` is mounted once on `host_scope` at supervisor
startup (`ws_connection.py:836-838`) and the resulting `host_ref` is
exposed via `WorkstationRpc.host_ref`. Every `run_script` reuses
the same impl + ref.

This works for stage 2's single read-only method but breaks for
stage 3: `copy_entity_in(slot_key, entity_id)` needs to copy entities
into the *current script's* `script_scope`, and `export_entity` /
`forget` / (stage 4) `read_state_slot` need access to the
per-script queued-ops collector and the local state mirror. None of
those can hang off a connection-lived mount.

**Change:**

- `WorkstationConnection`'s supervisor resolves the workstation
  proxy *once* at startup and exposes it on `WorkstationRpc` as
  `workstation_proxy: WorkstationServiceProto`. Drop `host_ref`
  from the bundle.
- The `_ScriptJob` handler (see (3)) opens a fresh `script_scope`
  per call: `FxtrScope(isolated=True, mountable=True,
  include=[host_scope], name="script_scope")`. Constructs
  `McpHostImpl(workstation_proxy, script_scope, queued_ops)`,
  mounts on `script_scope`, gets a per-script `host_ref`, threads
  it into `RunnerConfig`. On script exit (whether success, error,
  or rejection), `script_scope` drains; the per-script impl, ref,
  and any copied-out entities go with it.

**`McpHostImpl` constructor (stage-2 cleanup shape):**

```
McpHostImpl(
    workstation: WorkstationServiceProto,
    script_scope: FxtrScope,
    queued_ops: list[dict[str, Any]],   # currently unused; stage 3 fills in
)
```

Stage 2 only uses `workstation` (for `lookup_entity`); the other
fields land now so stage 3 just adds methods that read them, with
no further refactor. `queued_ops` matches the `RunResult.queued_ops`
shape (`list[dict[str, Any]]`, tagged-union dicts).

**Scope topology after this change:**

```
workstation /rpc user scope
  └─ host_scope (connection-lived; isolated, mountable,
                 include=[ws/rpc user scope])
        ├─ workstation_proxy resolved once, lives here
        └─ per-_ScriptJob:
             script_scope (isolated, mountable, include=[host_scope])
               ├─ McpHostImpl mount → host_ref
               └─ open_sandbox call_scope
                    └─ sandbox connect dispatch scope
                         └─ _GuestImpl mount
```

`run_script` enters `script_scope.as_active_scope()` (not
`host_scope`) around `open_sandbox` so the per-call sandbox scope
inherits through `script_scope` → `host_scope` → ws/rpc user scope,
which is what makes the per-script `host_ref` encodable.

## (3) Move the `run_script` body into the worker as `_ScriptJob`

Today `run_script` (the FastMCP tool) opens the sandbox, calls
`runner.run()`, and inspects the result, all on the tool's own
task. The connection's worker is only involved for
`open_session` / `close_session`. This means a `run_script` and a
hypothetical foreign-state reload could in principle run
concurrently on different tasks — not a problem in stage 2 (the
reload doesn't exist yet), but the wrong shape going into stage 3.

**Change:**

- Add `_ScriptJob(preamble_src: str, script_src: str, working_dir:
  Path, completion: Future[str])` to the `_Job` union.
- `run_script` becomes a thin tool: read `preamble.py`, ensure
  sandbox env is ready, submit a `_ScriptJob`, await
  `completion.wait()`, return its string result.
- The worker handler `_do_script_job(job)` does:
  1. Open `script_scope` as a child of `host_scope`.
  2. Build `McpHostImpl(workstation_proxy, script_scope,
     queued_ops)`. Mount on `script_scope`. Capture the per-script
     `host_ref`.
  3. With `script_scope.as_active_scope()`, call
     `env.open_sandbox(...)` → `runner.run()` (passing
     `RunnerConfig(preamble_src, script_src, host_ref)`).
  4. After `runner.run()` returns, `queued_ops` is non-empty for
     stage 3+; for stage 2 it's empty, so the worker just builds
     the tool-result string and resolves `completion`.
  5. `script_scope` drains (the `async with` exits at this point).
- The worker holds at most one `_ScriptJob` open at a time —
  serializes naturally with `_OpenSessionJob` / `_CloseSessionJob`
  / `_EventJob`.

**Errors during the script** (sandbox setup, `runner.run()` raised,
etc.): caught, converted to a tool-error string, set on
`completion.set_result(...)`. Worker keeps draining. The two-layer
exception handling in `_dispatch_job` already does the right thing
for cancellation.

**`SandboxEnvSupervisor` access:** The worker needs the env. The
simplest shape: `_ScriptJob` carries a reference to the supervisor
(or the AppContext); the worker calls `await
sandbox_env.get_env()` inside the handler. The env is supervisor-
lived and cheap to fetch.

## (4) Update the stage-3 plan to reflect the simpler model

`stage3_export_state.md` currently sketches:

- A separate `connection_worker` job-queue addition for
  `MutationJob` / `ScriptJob` / `EventJob`. After this cleanup,
  these are just additional `_Job` variants on the existing queue;
  no separate machinery.
- `event_handler.py` as a separate module dispatching off the
  long-lived session-event stream. After cleanup, "event handling"
  is just the `_EventJob` arm of `_dispatch_job`.
- "Set `last_state` to a fresh synthetic `StateUpdateEvent`
  constructed from the carried `state` + post-mutation tracker
  fields." The workstation now carries `tracked_entities` /
  `tracked_services` on every `StateUpdateEvent` (part-1 stage 4b),
  so the synthetic construction is unnecessary — store the actual
  `StateUpdateEvent` we received.
- Worth updating the "State mirror invariants" section: we maintain
  our *own* understanding of the desired tracker set from
  `data_by_key` and emit track/untrack ops accordingly; we do
  **not** rely on the workstation's `tracked_entities` /
  `tracked_services` fields as the source of truth. Instead we
  treat any discrepancy as a warning (log at WARN level with both
  sides) rather than an assertion failure.

Update `stage3_export_state.md` in place to pull these threads
through. No file moves; just adjust the wording + the
implementation-steps list.

## Implementation steps

- [x] Drop `_session_event_send` / `_session_event_recv` stream
      from `WorkstationConnection` and remove
      `_session_event_consumer_loop`. Add `_EventJob` to the
      `_Job` union with a stage-2 no-op dispatch arm. Reader's
      `SessionEventFrame` branch builds `_EventJob` and pushes
      onto `_job_send` via `send_nowait`; backpressure overflow
      logs + drops to match today.
- [x] Reshape `WorkstationRpc`: drop `host_ref`, add
      `workstation_proxy: WorkstationServiceProto` resolved once at
      supervisor startup. Update `_open_workstation_rpc` to do the
      resolve and stop mounting `McpHostImpl` there.
- [x] Update `McpHostImpl.__init__` to take `(workstation,
      script_scope, queued_ops)`. Stage 2 keeps using just
      `workstation` for `lookup_entity`; the other fields are
      stored unused.
- [x] Add `_ScriptJob` to the `_Job` union and a
      `_do_script_job(job)` worker handler that owns the
      script-scope lifecycle + sandbox open + runner invocation.
      `run_script` (the tool) becomes a thin submit-and-await.
- [x] Run `tests/test_run_script_lookup_entity.py` — passes
      unchanged. Stage 1 tests pass too; they don't use the
      sandbox so the change is invisible to them.
- [x] Update `stage3_export_state.md` per item (4) above:
      replace the "separate worker / event handler" framing with
      "additional `_Job` variants on the existing queue"; drop the
      synthetic-StateUpdateEvent language; soften the tracker-set
      invariant to a warning.
- [x] Update `CODE_OVERVIEW.md`: refresh the resource-model
      diagram (two subtasks not three; per-script `script_scope`
      replaces the persistent `McpHostImpl` mount on `host_scope`);
      update the `host_impl.py` and `ws_connection.py` blurbs.

## Out of scope for this cleanup

- Lifting `Future` to a `_util.py` (reflection note 8). Cheap; can
  ride alongside any future stage-3 commit.
- Focused unit tests for `get_service` / `get_id_for_service`
  (reflection note 6). Pick up alongside stage 3 builtins work.
- Auto-reconnect (stage 3 plan). Architecturally local to the
  supervisor's outer body; no cleanup-time prerequisite.
- The two-package-deep sandbox-bridge layout (reflection note 5).
  Leave for now.
