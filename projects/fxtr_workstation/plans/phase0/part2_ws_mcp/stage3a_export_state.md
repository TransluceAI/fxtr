# Stage 3a — Export entities/services + persistent session state

Goal: agent can put data into the session via `export_entity` /
`export_service` and remove it via `forget`. The MCP correctly
maintains `data_by_key` on the workstation and the derived
tracker-set for the session. Foreign `state_update` reload path
lands here too.

This stage builds the entire mutation pipeline that stage 4's
`schedule_invoke` will extend; it does not yet handle invoke or
results.

Auto-reconnect is carved out to **stage 3b** (`stage3b_reconnect.md`)
so the mutation-pipeline design can be exercised end-to-end without
the supervisor-loop changes interleaving. Channel notifications
(`notifications/claude/channel`) are punted to **stage 4** since
the only async-to-the-agent thing we have in stage 3 is foreign
mutations, which are rare. Stage 3a silently updates the local
mirror on a foreign reload; the agent finds out via their next
tool call's reflected state.

## Scope

- Three new sandbox builtins (in
  ``fxtr_ws_mcp_sandbox_bridge.builtins``):
  - `async export_entity(slot_key, entity)` — copies the entity out
    to the host's per-script ``script_scope`` via a host RPC method,
    queues an `export_entity` op record.
  - `async export_service(slot_key, stub)` — queues an
    `export_service` op record (services are already
    workstation-tracked; nothing to copy).
  - `def forget(slot_key)` — queues a `forget` op record.
- One new host RPC method (in
  ``fxtr_ws_mcp_sandbox_bridge.protocols.McpHostProto``):
  - `async copy_entity_in(slot_key: str, entity_id: FxtrID) -> None`
    — receives an entity over the sandbox RPC connection (auto-
    bridged into the host's per-call dispatch scope), runs
    `EntityRef(id, Any).copy_to(script_scope)` so the entity
    survives past dispatch-scope teardown, stashes ``(slot_key →
    fxtr_id)`` on the per-script :class:`McpHostImpl`. The host
    rejects (raising over RPC) if `slot_key` is already used in
    this script.
- Local session-state mirror in ``session_state.py``:
  - Plain dataclass ``SessionMirror``: ``last_state:
    StateUpdateEvent | None``, ``data_by_key: dict[str, tuple[str,
    Any]]`` (cached projection of ``last_state.state["data_by_key"]``
    for fast reads). Plus ``ws_tracked_entities`` /
    ``ws_tracked_services`` as ``set[str]`` mirroring the
    workstation's last-reported pin sets (diagnostic only — see
    "State mirror invariants" below).
  - Module-level helpers:
    - `apply_ops(mirror, runner_queued_ops, host_id_stash) →
      new_data_by_key`. Pure function; produces the post-state
      ``data_by_key`` from the current mirror plus the script's
      queued ops + host's stash of copied-out entity ids.
    - `desired_tracker_set(data_by_key) → (entities: set[str],
      services: set[str])`. Walks ``data_by_key`` for
      ``["exported_entity", id]`` / ``["exported_service", id]``
      literals plus recursively walks any ``["invoke_result",
      payload]`` for entity/service tags (stage-4-relevant; safe to
      include now since the mirror may already carry pre-existing
      invoke results from before stage 3a, in which case we want to
      keep them tracked).
- Mutation builder in ``mutation_builder.py``:
  - `build_mutate_request(mirror, runner_queued_ops, host_id_stash,
    *, subscription_id, ack_seq, update_id) → MutateSessionRequest |
    None`. Returns ``None`` when the mutation would be empty
    (empty patch + empty ops). Otherwise produces the request.
  - JSON Patch is generated *directly from the queued ops*, not
    via a generic diff library: each ``export_*`` op becomes
    ``{"op": "add", "path": "/data_by_key/<key>", "value": [tag,
    short_id]}`` (or ``"replace"`` if the key exists in the
    pre-state); each ``forget`` op becomes ``{"op": "remove",
    "path": "/data_by_key/<key>"}`` (validated against the
    pre-state — referencing a missing key surfaces as a tool error
    *before* the request is sent).
  - Tracker diff: compute ``desired_tracker_set(post_data_by_key)``;
    diff against the workstation's last-reported sets
    (``mirror.ws_tracked_entities`` /
    ``ws_tracked_services``); emit ``TrackEntityOp`` /
    ``UntrackEntityOp`` / ``TrackServiceOp`` / ``UntrackServiceOp``
    accordingly.
- ``McpHostImpl`` extension (still per-script):
  - New per-script field ``copied_entities: dict[str, FxtrID]`` —
    the host id-stash. Populated by ``copy_entity_in``; read by the
    worker after ``runner.run()`` returns.
  - ``copy_entity_in`` runs in an RPC dispatch task during
    ``runner.run()``. It uses the per-script ``script_scope`` field
    (constructor arg) as the copy target.
- ``_do_script_job`` body grows to include the end-of-script
  flow described under "End-of-script flow" below. Mirror updates
  happen only here (or in ``_do_event_job``).
- ``_do_event_job`` body grows to handle ``StateUpdateEvent`` for
  our active subscription whose mutator is *not* us — replace
  ``last_state``, recompute ``data_by_key``, refresh
  ``ws_tracked_*``. (Self-mutator state-updates never reach
  ``_do_event_job`` because the workstation suppresses them on the
  mutator side; we update the mirror in ``_do_script_job``'s
  commit branch instead.) ``InvokeResultEvent`` /
  ``InvokeErrorEvent`` log + drop in stage 3a (stage 4 handles).
  ``LoadResultEvent`` / ``LoadErrorEvent`` / ``CallbackEvent``
  log + drop as soft assertions (we don't issue the originating
  ops in v1).

## End-of-script flow

```
runner.run() returns RunResult(queued_ops=[...])
   │
   ▼
build_mutate_request(...)
   │
   ▼
maybe-empty? (no patch, no track/untrack ops)
   ├── yes → return "Script ran. (no queued ops)"
   ▼
   no
   ▼
AsyncExitStack:
   for each export_entity op (in order):
     stack.enter_async_context(
        expose_entity(workstation_ref, external_name=f"mcp-tmp-{uuid4()}",
                      entity=copied_entities[key], scope=script_scope))
   │
   ▼
send mutation, await response
   │
   ▼
match response:
  MutationCommittedResponse:
    update mirror (data_by_key, ws_tracked_*) from the request's
        post-state + the response's seq + the originally-built
        StateUpdateEvent shape (we synthesize it locally — the
        workstation does NOT send us a self-state-update; we ARE
        the mutator)
    exit_stack.aclose()  ← session pin keeps refs alive
    return success summary
  MutationRejectedResponse:
    exit_stack.aclose()  ← release registrations; nothing committed
    return tool-error string with reason + message; mirror unchanged
```

Note on the "synthesize StateUpdateEvent locally" step: when we are
the mutator, the workstation only sends us ``MutationCommittedResponse``
(carrying ``seq``); it does not echo a ``state_update`` event back
on our subscription (self-suppression, per ``client_protocol.py``
docstring on ``MutationCommittedResponse``). So our mirror has to be
the one that knows what state we just committed. The mutation we
just sent fully describes the new state — we can construct a
synthetic ``StateUpdateEvent`` from ``(seq, ack_seq, post_state,
desired_tracker_set)`` and use it as the new ``last_state``. We do
this only because we have the same information the workstation
used; if the workstation diverges from our model, the next foreign
event reload will correct us.

This is a deliberate scoping of the "no synthetic
StateUpdateEvent" rule from the post-1 cleanup: foreign events
land verbatim, but our own commits *have to* be locally
synthesized since we don't get the wire echo.

## State mirror invariants

After every successful mutation (ours *or* foreign):

- ``last_state`` is a :class:`StateUpdateEvent` (workstation-emitted
  for foreign mutators; locally-synthesized for our own commits).
- ``data_by_key = last_state.state.get("data_by_key", {})``.
- ``ws_tracked_entities`` / ``ws_tracked_services`` come from
  ``last_state.tracked_entities`` / ``tracked_services``.

We compute our *own* ``desired_tracker_set`` from
``data_by_key`` to emit track/untrack ops in outgoing mutations. We
do **not** rely on ``ws_tracked_*`` as the source of truth — those
are diagnostic. After each commit (ours or foreign), cross-check
our derived set against the workstation's reported set; a
discrepancy is logged at WARN level (with both sides) but does not
raise. The workstation's stage-4b fields catch bugs in our
derivation logic without coupling us tightly to their wire shape.

## Foreign-mutation reload path

When ``_do_event_job`` receives a ``StateUpdateEvent`` for our
active subscription that came from a different mutator (we know
we didn't issue it because *we'd be in our own ``_ScriptJob``'s
post-commit branch* if we had — the workstation doesn't echo our
own events back):

- Replace ``last_state`` with the new event.
- Replace ``data_by_key`` with ``state["data_by_key"]`` (or ``{}``
  if missing).
- Replace ``ws_tracked_entities`` / ``ws_tracked_services`` from
  the event's fields.
- Stage 3a: log a debug-level "session state reloaded" line. No
  channel notification (deferred to stage 4).

Race with our own in-flight script: foreign events that arrive
during a ``_ScriptJob``'s ``runner.run()`` queue behind it as
``_EventJob``s. The script runs to completion against the *pre-
foreign* mirror; we submit the mutation; the workstation rejects
with ``conflict`` (stale ack_seq); the worker surfaces the
rejection to the agent; *then* the worker drains the foreign
``_EventJob`` and updates the mirror. The agent's retry sees the
new state. Trades one wire round-trip for a single source of
truth (the workstation).

## Empty-mutation short-circuit

If the runner's queued-ops list is empty AND no track/untrack ops
result from the (unchanged) tracker set, skip the wire entirely
and return a "no queued ops" tool result. We do not advance
``ack_seq`` — there's no committed-on-our-side state to advance
to. The next outgoing mutation carries the latest ``ack_seq`` we
know about (from the most recent ``last_state.seq``); foreign
events that already landed have already been folded into the
mirror by their own ``_EventJob``s.

## Tests (stage 3a)

Light surface — the design is still being learned, so we focus on
one big integration test plus pure-unit coverage of the diff
logic. Foreign-state coverage is a bonus.

- `test_session_state_diff.py` — pure unit tests against
  ``apply_ops`` + ``desired_tracker_set`` + ``build_mutate_request``:
  - Empty start, single export → patch adds key, track op for
    new short_id.
  - Existing key with ``["exported_entity", x]``, ``forget`` →
    patch removes key, untrack op for ``x`` (assuming nothing
    else references it).
  - Two keys reference the same short_id; ``forget`` one → no
    untrack (still referenced).
  - State has ``["invoke_result", payload]`` referencing entity
    y; payload walked, y appears in desired tracker set.
  - Empty mutation: ``build_mutate_request`` returns ``None``.
  - ``forget`` on missing key surfaces a structured error from
    ``build_mutate_request`` (caller uses this to fail the
    script before sending).
- `test_export_pipeline_e2e.py` — integration:
  - Boots workstation + MCP; another peer pre-registers a
    ``bg-entity``.
  - Agent script: ``await export_entity("foo", await
    lookup_entity("<bg-entity short id>"))``.
  - After ``run_script``, drive a *separate* ``/client`` connection
    (a hand-rolled subscriber, mirrored on the workstation tests'
    helpers) to confirm the session state has
    ``data_by_key = {"foo": ["exported_entity", id]}`` and
    ``tracked_entities = {id}``.
  - Drop the peer's registration; assert the entity stays alive in
    the workstation's ``entities_scope`` (session pin holds it).
  - Run another script: ``forget("foo")``. Workstation's
    ``data_by_key`` is empty; entity is untracked.
- (Bonus) `test_foreign_state_update.py` — boots two ``/client``
  connections (the MCP and a hand-rolled second client). Second
  client mutates the session's ``data_by_key`` via a direct
  ``MutateSessionRequest``. The MCP receives the foreign event,
  reloads, and the next ``run_script``'s end-of-script mutation
  builds against the new state correctly. Stage-3a-skippable but
  cheap to add since the second-client fixture already exists.

## Implementation steps

- [x] Extend ``fxtr_ws_mcp_sandbox_bridge.protocols`` with
      ``copy_entity_in`` on ``McpHostProto``. Add a ``QueuedOp``
      tagged-union dataclass family (``ExportEntityOp(slot_key)`` /
      ``ExportServiceOp(slot_key, short_id)`` /
      ``ForgetOp(slot_key)``); the ``RunResult.queued_ops`` shape
      switches from ``list[dict]`` to ``list[QueuedOp]`` (still
      asdict'd for the wire, deserialized on the host side).
- [x] Extend ``McpHostImpl``: ``copy_entity_in`` method (uses
      ``script_scope`` constructor arg as copy target; rejects
      duplicate slot_key with an over-RPC error). Add
      ``copied_entities: dict[str, FxtrID]`` field.
- [x] Add the three sandbox builtins. ``export_entity`` calls
      ``host.copy_entity_in`` then appends an ``ExportEntityOp``;
      ``export_service`` appends an ``ExportServiceOp``; ``forget``
      appends a ``ForgetOp``. Each rejects duplicate slot_keys
      sandbox-side as well, so the agent gets a clean error
      without the RPC roundtrip.
- [x] Write ``session_state.py`` (``SessionMirror`` dataclass +
      ``apply_ops`` + ``desired_tracker_set``). Pure-Python; no
      I/O. Place a ``self._mirror`` field on
      ``WorkstationConnection`` initialized from
      ``SessionSnapshotEvent`` at subscribe-time.
- [x] Write ``mutation_builder.py``
      (``build_mutate_request`` returning ``MutateSessionRequest |
      None``; raises a structured error for missing-forget-key).
      JSON Patch synthesized directly from the op list, not via
      ``jsonpatch``.
- [x] Grow ``_do_script_job`` to include the end-of-script flow:
      build request, ``AsyncExitStack`` of ``expose_entity``
      registrations, send + await response, mirror update on
      commit, surface error on reject. ``copied_entities`` is
      pulled off the per-script ``McpHostImpl``.
- [x] Grow ``_do_event_job`` to handle foreign ``StateUpdateEvent``
      → mirror replace. Other event kinds: log + drop (with the
      right log level per the soft-assertion classification).
- [x] Subscribe-time mirror init: when the supervisor receives the
      ``SessionSnapshotEvent`` in ``_do_subscribe`` (or the initial-
      session subscribe on startup), populate the mirror from
      ``last_state``. (We do not replay the ``events`` suffix in
      stage 3a — it only contains result events we don't issue.
      Stage 4 will fold invoke-result events from the suffix into
      the mirror.)
- [x] Tests: ``test_session_state_diff.py`` (17 unit tests for
      pure helpers) + ``test_export_pipeline_e2e.py`` (full
      export → peer-drop survival → forget integration).
- [x] Update ``CODE_OVERVIEW.md``: list ``session_state.py`` /
      ``mutation_builder.py``; refresh the ``McpHostImpl`` blurb;
      reflect the grown ``_do_script_job`` / ``_do_event_job``
      bodies.

## Surprises landed during implementation

- **``ack_seq`` semantics**: the first version of
  ``build_mutate_request`` used ``mirror.last_state.ack_seq`` as
  the outgoing ``ack_seq``, which produced a ``conflict``
  rejection when the MCP reconnects to a session that already has
  a committed state-update from a previous (now-gone) MCP
  subscription. The workstation's stale-ack rule rejects when
  ``ack_seq < last_state_seq`` AND the last mutator's
  ``(connection_id, subscription_id)`` differs from the new
  mutator's. The right answer turned out to be deeper than just
  switching to ``mirror.last_state.seq``: see the
  ``last_processed_seq`` design below.
- **``last_processed_seq`` + ``_AckCommitJob``**: outgoing
  ``ack_seq`` is now a connection field
  (``_last_processed_seq``) that strictly tracks "highest event
  seq we've actually applied locally". Two paths advance it: an
  ``_EventJob`` for a state_update (advances on the worker just
  before the mirror-reset), and an ``_AckCommitJob`` enqueued
  by ``_apply_script_mutation`` after a successful self-commit.
  The ack-commit job lands behind any foreign events queued
  before our response, so those drain first; we never ack a
  workstation-side commit's seq before locally processing every
  preceding event. Stage 4 will add seq-advancement to the
  invoke-result handler the same way.
- **Self-commit synthesis** is necessary because the workstation
  self-suppresses state-update events on the mutator side
  (per ``MutationCommittedResponse`` docstring). We synthesize a
  ``StateUpdateEvent`` locally from the request's patch + the
  response's ``seq`` and use that to refresh the mirror. Foreign
  events still land verbatim from the wire — the synthesis only
  applies to our own commits. Note: ``_apply_local_commit``
  refreshes the mirror but does **not** touch
  ``_last_processed_seq``; the ack-commit job does, after
  preceding events have drained.

## Resolved (from design discussion)

- ``forget(key)`` waits for ``mutation_committed`` before clearing
  the local state mirror. The whole script's queued ops apply
  atomically; nothing in the mirror moves until the workstation
  acknowledges.
- ``export_entity`` does *not* reject already-workstation-tracked
  entities (e.g. ones the script just got from ``lookup_entity``).
  Re-registering is essentially free, content-addressing
  deduplicates on the workstation, and a single export route keeps
  the agent-facing surface simple.
- Foreign-mutation race: don't proactively check the queue before
  submitting; let the workstation reject with ``conflict``. The
  worker surfaces the rejection to the agent; the foreign
  ``_EventJob`` runs next and updates the mirror; agent retries.
  One source of truth (the workstation), simpler invariant.
- JSON Patch generated directly from the queued ops list, not via
  ``jsonpatch`` — the change set is sparse and fully known.
