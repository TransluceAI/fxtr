# Stage 3b — Auto-reconnect

Goal: a transient drop of the workstation connection doesn't
permanently break the MCP. The supervisor reconnects in the
background (with backoff), re-resolves the workstation proxy,
re-subscribes to the active session if there is one, and resumes
draining the worker queue. In-flight tool calls at disconnect time
get a ``ConnectionLost`` surfaced to the agent; subsequent calls
work without an explicit reconnect.

This stage assumes stage 3a is in: the local state mirror exists,
``SessionSnapshotEvent``-replay populates it, and outgoing
mutations carry the right ``ack_seq``. Without 3a's mirror, a
reconnect would have nothing to refresh.

## Scope

- The supervisor's outer body (``_supervisor_loop``) gains a retry
  loop with exponential backoff. The inner body (the
  ``_open_workstation_rpc`` + ``aconnect_ws`` + ``_run_inner``
  chain) becomes one iteration of the loop.
- On disconnect, in-flight requests fail their futures with
  ``ConnectionLost``. The job queue is preserved across reconnects
  — pending ``_OpenSessionJob`` / ``_CloseSessionJob`` /
  ``_ScriptJob`` / ``_EventJob`` items stay queued, but any that
  were already mid-flight (whose handler raised ``ConnectionLost``)
  surface to their submitters as failures.
- After successful reconnect, the supervisor re-resolves
  ``workstation_proxy`` (the previous one is bound to a now-gone
  ``host_scope``), names the connection, opens the new ``/client``
  websocket, re-subscribes to the active session if one is set,
  and refreshes ``self._mirror`` from the new snapshot. Then signals
  ready and resumes the worker.
- ``connection.wait_ready()`` callers see the *first* successful
  ready event; subsequent reconnect cycles re-set the ready event
  but ``wait_ready`` is one-shot per call — anyone parked on it
  has already proceeded. New tool calls after a disconnect and
  before reconnect-success see the connection as not-ready and
  block on ``wait_ready`` until the supervisor signals again.
  (Open question: do we want to preserve the strict "first ready
  is final" semantics, or expose disconnect/reconnect transitions
  to tools? Stage 3b proposes: keep ``wait_ready`` simple,
  always-await-the-current-cycle; tools that submit jobs to a
  not-currently-connected supervisor block until reconnect, with
  a generous timeout the tool can surface as "still down".)
- Backoff: start at 0.5s, double each retry up to 30s, cap at ~10
  attempts before giving up and exiting the supervisor with the
  error. The exact numbers are arbitrary; we'll tune once we have
  empirical data.
- Hard failures (DNS / TLS / 4xx during websocket handshake) take
  the full retry path, same as transient drops. We don't try to
  classify; the workstation is local-process for now and any
  non-transient failure means the workstation isn't there.

## Reconnect lifecycle

```
supervisor_loop:
  while not shutdown_event.set:
    try:
      async with _open_workstation_rpc(...) as rpc:
        self._rpc = rpc
        await self._name_connection_via_rpc(...)
        async with httpx_ws.aconnect_ws(...) as ws:
          self._ws = ws
          await self._run_inner()  ← reader + worker; subscribes;
                                     parks on shutdown_event
        # ws closed
      # rpc closed
      # _run_inner returned cleanly = shutdown was requested
      break
    except (ConnectionLost, httpx_ws.HTTPXWSException, ...) as e:
      # mark disconnected; fail in-flight pending request streams
      self._fail_pending_requests(ConnectionLost(...))
      # signal not-ready (a fresh _ready event for the next cycle)
      self._ready = anyio.Event()
      backoff += backoff increment
      with anyio.move_on_after(backoff):
        await shutdown_event.wait()
      if shutdown_event.set:
        break
    except BaseException:
      raise  ← cancellation: propagate

  # falling out of the loop = clean shutdown
  finally: drain queue, exit
```

The job queue (``_job_send`` / ``_job_recv``) is created once in
``connection.start(tg)`` and outlives reconnects. ``_pending``
(per-request streams) is reset on each reconnect cycle since the
request_ids and the streams themselves are bound to the dead
connection's frames.

## In-flight job behavior

- ``_OpenSessionJob`` / ``_CloseSessionJob`` mid-flight: the
  ``await self._expect(receive)`` raises ``ConnectionLost`` when
  the per-request stream closes. The handler propagates; the
  worker catches and forwards via ``_fail_job``. The job's
  submitter sees ``ConnectionLost`` as an exception out of
  ``await fut.wait()`` and surfaces a tool-error string.
- ``_ScriptJob`` mid-flight: the sandbox is opened *under* the
  per-script ``script_scope``, which is a child of the now-gone
  ``host_scope``. We tear down the sandbox + script_scope as part
  of the worker handler's exception propagation (the ``async with``
  unwinds normally). If we'd already submitted the mutation and
  were awaiting the response, ``ConnectionLost`` raises out of
  ``_expect``. If we'd already received ``MutationCommittedResponse``
  and were updating the mirror — the mirror update is sync; it
  completes, but its commit is now in a connection that's gone.
  The next reconnect's snapshot will reload from the workstation's
  authoritative state; if our local commit landed on the wire and
  the workstation kept it, the snapshot agrees; if the connection
  dropped before the workstation processed the mutation, we lose
  the update — but the workstation never committed it, so the
  states stay consistent.
- ``_EventJob`` mid-flight: similar to script — handler may have
  already updated the mirror from the foreign event. Reconnect's
  snapshot replaces the mirror anyway, so any ordering question
  resolves.

The script's exposed-entity registrations are owned by the
``AsyncExitStack`` inside the handler. Disconnect raises out of
the await; the stack unwinds; the registrations are released
locally. The workstation's RPC connection has already gone too,
so the workstation's side cleans up via its own end-callbacks.

## Mirror handling on reconnect

After a successful re-subscribe to the same session UUID, the
workstation sends a fresh ``SessionSnapshotEvent`` with the
current ``last_state`` + the unacked-event suffix. The supervisor
replaces ``self._mirror.last_state`` from the snapshot,
re-derives ``data_by_key`` / ``ws_tracked_*``, and replays the
event suffix (stage 3a: just logs invoke/load events; stage 4
folds invoke results into the mirror).

If the session UUID is gone (rare — workstation crashed, or some
foreign close ran), the re-subscribe errors. The supervisor
treats this as a hard failure: log the error, exit the
supervisor with it, surface to the next ``wait_ready()`` caller.
The user manually clears ``active_session`` from the toml and
calls ``open_session`` again.

## Tests

- `test_reconnect_e2e.py` — boots workstation + MCP + an active
  session. Forces a websocket disconnect (e.g. by stopping +
  restarting the workstation, or by a server-side close from a
  test fixture). Verifies:
  - Tool call mid-disconnect surfaces ``ConnectionLost``.
  - After reconnect-success, a follow-up tool call works.
  - The mirror reflects post-reconnect state (run a script that
    exports a key; assert the workstation sees the new key).
- `test_reconnect_during_script.py` — disconnect *during* a
  ``_ScriptJob``'s ``runner.run()``. Verifies the sandbox tears
  down cleanly, the agent sees an error, and a follow-up
  ``run_script`` after reconnect succeeds.
- (Optional) ``test_reconnect_persistent_failure.py`` — workstation
  stays down through all retry attempts; supervisor exits cleanly
  with the error surfaced. Possibly skipped if it makes the test
  suite slow.

## Implementation steps

- [x] Refactor ``_supervisor_loop`` to a retry loop. The current
      try/except/finally body becomes one iteration via a new
      ``_run_one_cycle`` helper; the outer body wraps it.
- [x] Add ``_reset_per_cycle_state_on_disconnect`` helper that
      closes leaked ``_pending`` entries and clears
      ``_active_subscription_id``. (We do *not* drain the job
      queue — see "in-flight vs queued" below.)
- [x] Replace ``_ready`` per cycle by swapping in a fresh
      ``anyio.Event`` only when the current one is already set.
      Avoids stranding ``wait_ready()`` callers that captured the
      constructor's event between ``start()`` and the supervisor's
      first iteration.
- [x] Add backoff parameters (``reconnect_backoff_initial``,
      ``reconnect_backoff_max``, ``reconnect_max_attempts``) on
      the connection constructor with sensible defaults
      (0.5s / 30s / 10 attempts). Reset the consecutive-failure
      counter when a cycle becomes ready before disconnecting.
- [x] On reconnect-success: re-resolve ``workstation_proxy`` (the
      ``_open_workstation_rpc`` async-CM does this each cycle),
      re-subscribe to ``self._active_session_id`` if any, refresh
      the mirror from the snapshot. Pre-populate
      ``self._active_session_id`` from the constructor's
      ``initial_session_id`` so first-connect and reconnect both
      consult the same field.
- [x] Tests: ``test_reconnect.py`` — basic forced-disconnect
      reconnect path + queued-call-during-disconnect path. Skipped
      a "drop during in-flight call" test since timing it
      race-free is hard; the existing path-coverage gives us
      enough confidence in the supervisor's retry behavior.
- [x] Update ``CODE_OVERVIEW.md``: refresh the resource-model
      diagram with the retry-loop framing; document the
      reconnect contract on ``WorkstationConnection``.

## Surprises landed during implementation

- **Stranded ``_ready`` event on first cycle**: the original
  design called for a fresh ``_ready`` event each cycle. But
  ``wait_ready()`` callers that captured the constructor's
  ``_ready`` between ``start()`` and the supervisor's first
  iteration would deadlock — the supervisor swaps in a *new*
  event for the first cycle, sets it after subscribe, but the
  caller is parked on the old (never-set) constructor event.
  Fix: only swap when the current event is already set. First
  iteration reuses the constructor's event so existing waiters
  get woken up correctly. (Caught by ``test_full_session_lifecycle``
  hanging — the per-test timeout fixture in conftest.py made
  this catchable rather than a silent hang.)
- **In-flight vs queued semantics**: the original
  ``_fail_in_flight_on_disconnect`` drained queued jobs too,
  which broke the "tool call during reconnect window
  transparently delays" UX. Renamed to
  ``_reset_per_cycle_state_on_disconnect`` and dropped the
  drain. In-flight jobs (whose handler is currently running)
  fail via ``_dispatch_job``'s ``BaseException`` path when the
  inner task group cancels the worker; queued jobs survive,
  and the next cycle's worker drains them after subscribe
  succeeds. Cleaner UX for the common "agent submits a call,
  network blips, call eventually returns" case.
- **Subtask ordering inside ``_run_inner``**: the worker must
  start *after* ``_do_subscribe`` returns, not before. If the
  worker starts first, it can pull a queued job from a prior
  disconnect and run it against a not-yet-subscribed
  connection (``_active_subscription_id`` is None). The fix
  was a one-line move; reader still starts first since
  subscribe needs the per-request stream plumbing.

## Out of scope for stage 3b

- Reconnect-during-mutation-with-uncertain-commit. The current
  design accepts that a mutation we sent but didn't get an ack for
  may or may not have committed; the next snapshot tells us which.
  A more careful design would attach an idempotency key + check
  the snapshot's event log for our ``update_id``; deferred.
- Connection swap (different workstation_url at runtime). Not a
  reconnect — would be a fresh connection altogether.
- Surfacing disconnect/reconnect transitions to the agent via
  notifications. Tied to channel notifications; deferred to
  stage 4 alongside the rest of the channel work.
