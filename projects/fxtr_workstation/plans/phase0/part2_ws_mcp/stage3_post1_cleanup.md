# Stage 3 post-1 — Cleanup before stage 4

A small structural refactor that lands after stage 3a + 3b but
before stage 4's invoke pipeline. Two pieces; no behavior change.

The stage-4 plan picks up two larger-scope notes (channel
notifications scaffolding + subscribe-time event-suffix replay)
that are best designed alongside the invoke work itself.

## (1) Drop the duplicate JSON-Patch implementation

Today's flow:

- ``mutation_builder.build_mutate_request`` calls
  ``apply_ops(pre, queued_ops, entity_short_ids)`` to produce the
  post-``data_by_key``, then ``_build_json_patch`` to build the
  RFC-6902 patch from the same op list. The post-state is
  computed but discarded.
- ``ws_connection._apply_local_commit`` then *re-applies* the
  patch to the prior state via ``_apply_json_patch_to_state`` —
  a partial JSON-Patch interpreter that has to stay in sync with
  the patch shapes ``_build_json_patch`` emits.

Two pieces of code mirror each other for a one-cycle round-trip
through the patch shape. Stage 4 makes ``data_by_key`` values
richer (``["invoke_pending", slot]`` / ``["invoke_result",
payload]``) and adds new patch shapes for invoke commits — the
mirror would only get more brittle.

**Change:** ``build_mutate_request`` returns a small dataclass
carrying the request **plus** the computed post-state and
desired tracker sets. The caller uses those directly to
synthesize the local ``StateUpdateEvent``; the JSON-Patch-replay
helper goes away.

```
@dataclass(frozen=True)
class BuildResult:
    request: MutateSessionRequest
    post_data_by_key: dict[str, list[Any]]
    desired_entities: set[str]
    desired_services: set[str]
```

Both ``_apply_json_patch_to_state`` and ``_unescape_json_pointer``
disappear from ``ws_connection.py``.

## (2) Restructure ``_apply_script_mutation``

Current shape: one ~80-line function with an ``AsyncExitStack``,
a per-op registration loop, a try/except for ``ForgetMissingKey``,
an else clause with a nested if/elif chain, and a
``result: str`` placeholder + ``# pyright: ignore`` to satisfy
the type checker.

Stage 4 grows the registration loop (invoke args carry entity refs
that need ``expose_entity`` too) and grows the build/send phase
(``InvokeOp``s with wire-escaped args, slot-counter management).
The current function would balloon awkwardly.

**Change:** factor the body into clear phases with early returns.
Extract the entity-registration step into a small helper so
stage 4 can extend it (more op kinds contributing entities) without
re-touching ``_apply_script_mutation``:

- ``async _register_export_entities(stack, host_impl, runner_queued_ops,
  workstation_ref, script_scope) → dict[slot_key, short_id]`` —
  walks ``runner_queued_ops`` for ``ExportEntityOp`` records,
  enters an ``expose_entity`` async-CM on ``stack`` for each,
  collects the workstation short_ids. Stage 4 grows this to also
  walk ``ScheduleInvokeOp`` args.
- ``_apply_script_mutation`` is now a straight read-top-to-bottom:
  open stack → register → build → maybe-noop-return → send +
  await → branch on response → return.

The ``result = ""`` placeholder goes away — every path returns
explicitly inside the ``async with`` block.

## Implementation steps

- [x] Add ``BuildResult`` dataclass to ``mutation_builder.py``;
      change ``build_mutate_request`` to return ``BuildResult |
      None``. Update unit tests
      (``test_session_state_diff.py``) to read ``.request`` /
      ``.post_data_by_key`` from the returned object.
- [x] Delete ``_apply_json_patch_to_state`` and
      ``_unescape_json_pointer`` from ``ws_connection.py``. Update
      ``_apply_local_commit`` to take a ``BuildResult`` and
      synthesize the ``StateUpdateEvent.state`` from
      ``{**prev_state, "data_by_key": post_data_by_key}`` directly.
- [x] Extract ``_register_export_entities`` from the body of
      ``_apply_script_mutation``. Restructure
      ``_apply_script_mutation`` with explicit early returns; drop
      the ``result = ""`` placeholder + pyright ignore. (Added a
      trailing ``raise AssertionError("unreachable: …")`` to
      satisfy pyright's return-path analysis without sacrificing
      readability.)
- [x] Run the full test suite to confirm no regressions
      (33 passed).
- [x] Update ``CODE_OVERVIEW.md`` to reflect the new
      ``mutation_builder`` return shape and the
      ``_register_export_entities`` extraction.

## Out of scope (deferred to stage 4 plan)

- Subscribe-time event-suffix replay. Stage 3a skipped this; stage
  4 needs to fold any ``invoke_result`` events from
  ``snapshot.events`` into the mirror.
- ``notifications/claude/channel`` scaffolding. Where it plugs in
  (probably a method on ``WorkstationConnection`` called from
  ``_do_event_job`` after promoting an invoke result into state)
  is a stage-4 design point, not a cleanup.
- A unified ``ScriptOps`` dataclass. The
  ``runner_queued_ops + entity_short_ids dict`` pair is fine for
  stage 3a; stage 4 may justify it once invoke ops add a third
  parallel collection. Revisit then.
