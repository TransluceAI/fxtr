# Stage 4 — `schedule_invoke`, `retrieve_result`, invoke result event handling

Goal: agent can dispatch a service-method call into a workstation
slot via `schedule_invoke`, receive the result asynchronously, and
read it back via `retrieve_result` in a subsequent script. This
closes the loop on the v1 agent model.

Scoped to **invoke** events only. The other non-state event kinds
(`LoadResultEvent` / `LoadResultSilentEvent` / `LoadErrorEvent` /
`EntityMetadataResultEvent` / `EntityMetadataErrorEvent` /
`ServiceMetadataResultEvent` / `ServiceMetadataErrorEvent` /
`CallbackEvent`) still raise `NotImplementedError` in
`_apply_state_effect`. Stage 5+ wires them in as the agent
gains the corresponding builtins.

## Scope

- Sandbox builtins:
  - `async schedule_invoke(slot_key, service_stub, action_lambda)`.
  - `async retrieve_result(slot_key, _type=None)`.
- New queued-op kind: `ScheduleInvokeOp(slot_key, service_short_id,
  method_name, args_recorded, copied_entity_handles)`. The
  mutation builder extends to translate it into `InvokeOp` on the
  wire.
- Per-kind dispatch inside `_apply_state_effect` for
  `InvokeResultEvent` and `InvokeErrorEvent`. The
  `_notify_for_event` stub gets a real body for the same two
  kinds.
- A new host RPC method `read_state_slot(slot_key)` for
  `retrieve_result` to read the local mirror sandbox-side.

This is the first stage where the MCP issues operations that
produce non-state events — exercising the
`_replayable_events` / `_unflushed_state_changes` machinery in
anger.

## `schedule_invoke` recording proxy (sandbox side)

The `action_lambda` is invoked with a `_RecordingProxy` instance
that records exactly one method call:

```
class _RecordingProxy:
    _calls: list[tuple[str, tuple, dict]] = []
    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self._calls.append((name, args, kwargs))
            return _Sentinel()
        return _record

result_sentinel = action_lambda(proxy)
assert isinstance(result_sentinel, _Sentinel)
assert len(proxy._calls) == 1
method_name, raw_args, raw_kwargs = proxy._calls[0]
assert not raw_kwargs  # v1: positional-only
```

We then walk `raw_args` recursively to escape it for the wire:

- `FxtrID` / `EntityRef[Any]` → copy out via the host's
  `copy_entity_in` (the same path stage 3 uses for
  `export_entity`); remember the resulting `(slot_key,
  host_entity_handle)` pair under a synthesized internal slot
  name; emit `["entity", <pending_marker>]` with the marker
  resolved post-RPC-registration host-side.
- `ServiceRefStub` → emit `["service", stub.short_id]`.
- `bytes` → emit `["bytes", base64(...)]`.
- list / tuple → emit `[[...recursed...]]`.
- dict → walk values; keys must be strings.
- str / int / float / bool / None → pass through.
- anything else → reject with a descriptive error.

The recorded args + the list of pending entity copies travel back
to the host as part of `RunResult`'s queued-ops list.

## End-of-script flow (host side)

```
for each (export_entity, export_service, schedule_invoke) op:
    collect entity handles from script_scope; de-dup by FxtrID;
    register via expose_entity inside the per-script
        AsyncExitStack (one CM per unique FxtrID)
build MutateSessionRequest:
    patch (in order):
       - pre-flush: one replace per _unflushed_state_changes entry
         (existing 3-post-2 behavior; unchanged)
       - data_by_key[k] = ["exported_entity", short_id]   (export)
       - data_by_key[k] = ["exported_service", short_id]  (export)
       - data_by_key[k] = ["invoke_pending", slot_n]      (invoke)
       - data_by_key.pop(k)                               (forget)
    ops:
       - tracker diff (track_*/untrack_*) against post-patch state
       - InvokeOp(slot_n, service_short_id, method_name,
                  args_post_substitution)
       (slot_n derived as _slot_counter + i for the i-th invoke
        op in this mutation; i starts at 1)
submit and await response.
```

Host-side, the mutation builder substitutes each pending entity
marker (from the arg walk) with `["entity", <real_short_id>]` once
the workstation has assigned the short_id via `expose_entity`.

On `MutationCommittedResponse`:

- Release **all** RPC registrations (exports and invoke args
  alike) via the per-script `AsyncExitStack` exit. The
  workstation's invoke worker session-pins each entity arg when
  it processes the `InvokeOp`, so invoke args stay alive across
  dispatch independent of our /rpc reg lifetime; exported-entity
  refs are covered by the tracker diff on the same mutation. No
  need for the host to retain a separate hold. (No
  `in_flight_invoke_regs` dict; the invoke args are released on
  the same path as exports.)
- The synthesized self-commit `StateUpdateEvent` flows through
  `_handle_state_update` as in stage 3; `_AckCommitJob` is
  submitted as before.

On `MutationRejectedResponse`:

- Release **all** RPC registrations (mutation never committed; no
  workstation-side session-pin exists).
- Surface error to the tool.

Local de-duplication: if the same FxtrID appears as an
`export_entity` op *and* as a `schedule_invoke` arg in the same
script, register it once over `/rpc` and reuse the short_id in
both places. The workstation also dedups by FxtrID, so a
redundant double-export would be safe — we just save the
round-trip.

## Slot allocation

The MCP allocates monotonic slot numbers client-side per the
workstation's strict-monotonicity rule:

- For each mutation we build, derive slots fresh from the
  connection's cached `_slot_counter`:
  `slot_n = _slot_counter + i` for the `i`-th invoke op (`i`
  starts at 1).
- Mutation rejection means the workstation never advanced its
  slot counter; the next mutation re-derives from the same base.
  No persistent client-side counter to roll back.
- A successful commit advances the workstation's slot_counter to
  match the new high-water mark; the synthesized self-commit's
  `StateUpdateEvent` carries the new value, and
  `_handle_state_update` advances `_slot_counter` to match.

## RX-side handling (per-kind dispatch in `_apply_state_effect`)

In `_apply_state_effect`, the inline match adds branches for
`InvokeResultEvent` and `InvokeErrorEvent`. Both follow the same
shape — derive the slot_key from the matching `["invoke_pending",
slot]` entry in `mirror.data_by_key`, compute the new value,
write it to both the derived dict and the mirror:

```python
def _apply_state_effect(self, ev: NonStateSessionEvent) -> None:
    self._replayable_events.append(ev)
    match ev:
        case InvokeResultEvent():
            slot_key = self._slot_key_for_pending(ev.slot)
            value = ["invoke_result", ev.payload]
            self._mirror.data_by_key[slot_key] = value
            self._unflushed_state_changes[slot_key] = UnflushedChange(
                seq=ev.seq, value=value,
            )
        case InvokeErrorEvent():
            slot_key = self._slot_key_for_pending(ev.slot)
            value = ["invoke_error", ev.message]
            self._mirror.data_by_key[slot_key] = value
            self._unflushed_state_changes[slot_key] = UnflushedChange(
                seq=ev.seq, value=value,
            )
        case _:
            raise NotImplementedError(...)


def _slot_key_for_pending(self, slot: int) -> str:
    for key, value in self._mirror.data_by_key.items():
        if (isinstance(value, list) and len(value) == 2
                and value[0] == "invoke_pending" and value[1] == slot):
            return key
    raise ProtocolError(
        f"non-state event for slot {slot} has no matching "
        "invoke_pending entry in mirror.data_by_key"
    )
```

This is the **only** change to the host-side state machinery.
There is no follow-up mutation. The next time the agent issues a
`run_script`, the mutation builder reads
`_unflushed_state_changes` and emits the pre-flush `replace`
patch as part of *that* mutation. The workstation absorbs the
promotion on its own schedule.

**Tracker-set behavior for refs in payload.** The
`desired_tracker_set` helper already walks `data_by_key` for
`["invoke_result", payload]` entries and recurses through the
payload for `["entity", id]` / `["service", id]` tags. So once
the promotion lands in the mirror's data_by_key, any new refs in
the payload appear in the desired tracker set automatically.
The next mutation's tracker diff emits the `track_*` ops to pin
them on the workstation side. Until then, the workstation
already has the refs pinned via the invoke worker's session-pin
from when the InvokeOp was processed — there is no window where
the refs could be released.

**The slot lookup failure case.** If `_slot_key_for_pending`
fails, the workstation has emitted a result event for a slot
that's no longer pending in our mirror. This can happen if a
foreign mutator overwrote our slot's `["invoke_pending", slot]`
entry between issuing the invoke and the result landing —
unexpected under stage 4's usage but possible if multiple
clients share a session. Hard-fail with `ProtocolError`;
supervisor tears down + reconnects. Same behavior whether the
lookup fails at initial arrival or during a rebuild.

## `_notify_for_event` (channel notification)

Bodies for `InvokeResultEvent` / `InvokeErrorEvent`. The
notification surfaces "slot landed" to the agent via the
FastMCP channel.

```python
def _notify_for_event(self, ev: NonStateSessionEvent) -> None:
    match ev:
        case InvokeResultEvent():
            slot_key = self._slot_key_for_recent(ev)  # see below
            self._send_channel(
                slot_key=slot_key,
                kind="invoke_result",
                summary=_summarize_payload(ev.payload),
            )
        case InvokeErrorEvent():
            slot_key = self._slot_key_for_recent(ev)
            self._send_channel(
                slot_key=slot_key,
                kind="invoke_error",
                summary=ev.message,
            )
        case _:
            pass  # nothing to notify for stage 4
```

Subtlety: `_apply_state_effect` has already promoted the slot
(so `mirror.data_by_key[slot_key]` is now
`["invoke_result", payload]`, not `["invoke_pending", slot]`).
`_notify_for_event` can find the slot_key by looking up the
just-recorded `_unflushed_state_changes` entry that the
preceding `_apply_state_effect` wrote — `_replayable_events[-1]`
is `ev` itself, but `_unflushed_state_changes` maps slot_key →
the just-written entry. Two equivalent approaches:

- Have `_apply_state_effect` stash the derived slot_key on a
  local field (set on call, read by `_notify_for_event`,
  cleared in `_do_event_job` after both helpers return).
- Or just walk `_unflushed_state_changes` looking for the entry
  with `seq == ev.seq` (always exactly one match for
  invoke events).

Pick whichever is cleaner — leaning toward the second; no extra
field, no clear-on-exit dance.

**Suggestion**: a small per-kind helper like
`_slot_key_for_just_applied(ev)` that walks
`_unflushed_state_changes` for `change.seq == ev.seq`. Returns
None if not found (which would be a bug for the kinds we
handle, since `_apply_state_effect` always wrote one).

**Channel delivery plumbing.** The supervisor doesn't directly
send channel notifications — that requires the FastMCP server's
request context. Same pattern as the spike:

- `WorkstationConnection` accepts a `notify_channel:
  Callable[[str, str, str], Awaitable[None]] | None` callback in
  its constructor, or via a setter. Args:
  `(slot_key, kind, summary)`.
- `server.py`'s lifespan wires the callback to call a small
  `send_channel_notification` helper against the FastMCP server.
  Channel source is `"workstation_mcp"`; channel name is
  `"fxtr_workstation"`.
- In tests where there's no FastMCP server, the callback is
  `None` and `_notify_for_event` no-ops.

**Replay re-notification.** Subscribe-time replay enqueues
`subsequent_events` as `_EventJob`s — each one runs
`_apply_state_effect` *and* `_notify_for_event`. This means the
agent gets the channel notification for events promoted during
the prior cycle that the workstation hadn't yet subsumed. This
is correct: from the agent's perspective the slot just resolved
(it didn't have a notification for it in the new conversation
turn). The redelivery-is-bit-identical principle covers this
case naturally.

Within a single cycle, the rebuild path inside
`_handle_state_update` runs `_apply_state_effect` only (the
rebuild loop doesn't call `_notify_for_event`). Otherwise we'd
re-notify on every state_update. This is the one place the
arrival and replay paths diverge — the rebuild is "redo the
state-mutation work", not "redo the surfacing-to-agent work."

## `retrieve_result` (sandbox side)

```
async def retrieve_result(slot_key, _type=None):
    val = host_proxy.read_state_slot(slot_key)
    # val arrives over RPC as a tagged tuple:
    #   ("exported_entity", id), ("exported_service", id),
    #   ("invoke_pending", n), ("invoke_result", payload),
    #   ("invoke_error", msg), or None.
    match val:
        case ("invoke_pending", _):
            raise LookupError(
                f"slot {slot_key!r}: result not yet available"
            )
        case ("invoke_error", msg):
            raise RuntimeError(f"invoke for {slot_key!r} failed: {msg}")
        case ("invoke_result", payload):
            return await _unescape_payload(payload)
        case ("exported_entity", short_id):
            return await lookup_entity(short_id)
        case ("exported_service", short_id):
            return ServiceRefStub(short_id)
        case None:
            raise KeyError(slot_key)
```

`_unescape_payload(payload)` walks recursively. Three-pass design
so all `lookup_entity` round-trips happen in parallel:

- **Pass 1 (collect):** walk `payload`, collect every distinct
  `["entity", id]` short_id into a set; remember positions.
- **Pass 2 (resolve):** `async with anyio.create_task_group() as
  tg:` spawn one `lookup_entity` task per distinct short_id;
  collect into `id → FxtrID`.
- **Pass 3 (assemble):** walk the structure again, substituting
  resolved FxtrIDs at each entity tag, building
  `ServiceRefStub`s for service tags, base64-decoding bytes tags,
  unwrapping literal-list-escape forms (`[inner_list]` length-1 →
  list).

N parallel round-trips interleave freely over the single RPC
channel; the sandbox sees one bundle of awaitable work rather
than N sequential steps.

The host has a new RPC method `read_state_slot(slot_key) ->
FxtrServiceData` that returns the local mirror's value for the
slot. It does *not* round-trip to the workstation — the local
mirror is authoritative (per the post-3-post-2 invariants:
`mirror.data_by_key` is the local view, which is exactly what
the agent should see).

## Tests

- `test_recording_proxy.py` — pure: `_RecordingProxy` accepts one
  method call, rejects multi-call, rejects no-call, rejects
  kwargs (until we decide to support them).
- `test_arg_walk.py` — pure: walking a captured-args structure
  with mixed scalars, lists, dicts, FxtrIDs, ServiceRefStubs,
  bytes; asserts the placeholder substitution shape.
- `test_invoke_result_promotion.py` — focused integration:
  - Boots workstation + MCP; another peer registers a service
    that returns a known `FxtrServiceData`.
  - Agent script: `await schedule_invoke("r", get_service("<id>"),
    lambda s: s.echo(42))`.
  - Wait for the channel notification (test harness mocks
    `notifications/claude/channel`).
  - Run another script: `await retrieve_result("r")` and assert
    its value.
- `test_invoke_with_entity_arg.py` — same shape but the script
  passes an entity arg (constructed in the sandbox via the
  preamble's helpers, copied out via the schedule_invoke arg
  walk). Assert the workstation receives the entity; assert the
  arg-RPC-reg is released right after `MutationCommittedResponse`
  (not after the result lands).
- `test_invoke_error.py` — service raises; assert
  `retrieve_result` raises `RuntimeError` with the message;
  assert `data_by_key` contains `["invoke_error", msg]` after the
  next agent mutation flushes the unflushed entry.
- `test_invoke_result_with_refs_in_payload.py` — service returns
  a payload containing entity / service refs. Assert: payload-
  side refs become workstation-tracked under our session via the
  next agent mutation's tracker diff; later `forget(slot_key)`
  un-pins them.
- `test_invoke_apply_state_effect_unit.py` — pure-ish unit on
  `_apply_state_effect`'s invoke branches: pre-arrange a mirror
  with an `["invoke_pending", N]` entry; call
  `_apply_state_effect(InvokeResultEvent(seq=K, slot=N, payload=...))`;
  assert the mirror + unflushed-dict update; assert
  `_replayable_events` has the event. Re-run for `InvokeErrorEvent`.
  Slot-lookup failure raises `ProtocolError`.
- `test_invoke_rebuild_on_state_update.py` — unit: process an
  invoke result locally (recording it in
  `_replayable_events` + `_unflushed_state_changes`), then apply
  a foreign state_update whose `ack_seq` doesn't subsume our
  event; assert the rebuild re-applies the event and the
  unflushed entry survives. Then apply a state_update whose
  `ack_seq` does subsume; assert the entry drops.

## Implementation steps

- [x] Extend `QueuedOp` union with `ScheduleInvokeOp`.
- [x] Implement `_RecordingProxy` and the args-walk +
      placeholder-substitution machinery in
      `fxtr_ws_mcp_sandbox_bridge/builtins.py`. Add
      `retrieve_result` + `_unescape_payload`.
- [x] Extend `host_impl.py` with `read_state_slot(slot_key)`.
      ``copy_entity_in`` is reused as-is by ``schedule_invoke``
      (the sandbox synthesizes internal slot_keys like
      ``_invoke_arg_<uuid>`` so there's no collision with the
      agent-facing slot_keys used by ``export_entity``).
- [x] Extend `mutation_builder.build_mutate_request` to emit
      `InvokeOp`s after the tracker diff, with placeholder
      substitution. Slot allocation from `_slot_counter`.
      ``BuildResult`` gains ``new_slot_counter`` so
      ``_apply_local_commit`` bumps the connection's
      ``_slot_counter`` to match the workstation's post-mutation
      value.
- [x] Extend `_apply_state_effect`'s inline match with
      `InvokeResultEvent` / `InvokeErrorEvent` branches. Slot
      lookup uses ``mirror.pending_slot_index`` (O(1)) rather
      than walking ``data_by_key``.
- [x] Implement `_notify_for_event` body for the same two
      kinds. Uses ``_slot_key_for_seq`` to find the slot_key
      from the just-written unflushed entry.
- [x] Wire the channel callback: ``notify_channel`` kwarg on
      ``WorkstationConnection.__init__``; ``server.py`` lifespan
      wires ``make_channel_notifier(mcp)``. New ``channel.py``
      module.
- [x] Update `_apply_script_mutation` to release the invoke-arg
      RPC registrations on the same `AsyncExitStack` exit as the
      export registrations. ``_register_export_entities`` now
      walks both ``ExportEntityOp.slot_key`` and
      ``ScheduleInvokeOp.entity_arg_handles``. No
      ``in_flight_invoke_regs`` bookkeeping.
- [x] Tests. Unit-level: per-kind dispatch + slot index drop +
      ``read_state_slot`` (in ``test_event_seq_redesign.py``).
      E2E: ``test_invoke_e2e.py`` with happy-path echo + error
      path.
- [x] Update `CODE_OVERVIEW.md` (per-kind dispatch documented
      in the mental-model body; ``read_state_slot`` mentioned in
      ``host_impl`` line; new ``channel.py`` listed; new
      "Invoke flow" subsection added; outstanding TODOs trimmed).

## Design decisions carried in

These are the resolved decisions from earlier discussion;
recorded here for the implementer.

- **No follow-up mutation on result arrival.** The promotion is
  recorded locally in `_unflushed_state_changes`; the next
  agent-issued mutation flushes it. This is what 3-post-2 set
  up — invoke just exercises it.
- **Invoke arg registrations release on mutation-commit.** Once
  `MutationCommittedResponse` lands, the workstation's invoke
  worker has session-pinned each entity arg from the `InvokeOp`,
  so they stay alive for the dispatch regardless of our /rpc reg
  lifetime. We can drop our RPC reg immediately; no
  `in_flight_invoke_regs` bookkeeping needed. (The tracker diff
  on the same mutation covers `exported_entity` refs only; invoke
  args aren't in `data_by_key` so they're outside its scope.)
- **Slot allocation uses `_slot_counter`** (the connection
  field, updated by every state_update). Mutation rejection
  rolls back implicitly (we re-derive from the same base).
- **All `schedule_invoke`s in one script share one mutation;**
  they get distinct slots and the workstation runs them in
  parallel.
- **Subscribe-time replay just works.** The
  `subsequent_events` enqueue in `_do_subscribe` flows through
  `_apply_state_effect` + `_notify_for_event` — same code path
  as live arrival. No separate "replay snapshot suffix" helper.
- **Channel notification source / channel names.** Source
  `"workstation_mcp"`; channel `"fxtr_workstation"`. Channel
  source attribute on the frame is the MCP server name; the
  channel name is the channel name. Matches the spike's
  convention.
- **`retrieve_result`'s payload walk parallelizes
  `lookup_entity`s.** Three-pass design (collect / resolve /
  assemble) lets N round-trips interleave over the single RPC
  channel.

## Out of scope for stage 4

- Other non-state event kinds remain stubs that raise
  `NotImplementedError`. The dispatch shape established here is
  the template for stage 5+ to fill in
  load / metadata / callback handlers.
- Typed `ServiceRefStub[T]` / typed `retrieve_result(...,
  type=T)`. v1 is UnknownService-only.
- Bundling locally-defined Python packages as workstation-
  shipped sandboxable artifacts. Stage 5+.
- A unified `ScriptOps` dataclass aggregating exports / services
  / forgets / invokes. Defer until the parallel collections
  become clunky in `_apply_script_mutation` /
  `build_mutate_request`; for now the explicit fields are
  readable enough.
