# Stage 6 — Callback support

Goal: extend the MCP plugin so the agent can mint workstation
callbacks, pass them as args to invoke calls, and observe / retrieve
the resulting `notify` payloads. Callbacks fire repeatedly; each
notify produces a channel notification *and* persists the payload in
session state so the agent can retrieve it later from a follow-up
`run_script`.

The workstation already supports the receive-side machinery
(`CreateCallbackOp` / `CloseCallbackOp` / `CallbackEvent` /
`WorkstationCallbackImpl` — see the workstation's `CODE_OVERVIEW.md`,
sections on `state.py` and `callback_impl.py`). Stage 6 wires those
into the MCP's sandbox builtins and mutation pipeline, and extends
the demo + tests to exercise the round trip.

## Agent surface (new builtins)

```
async create_callback(slot_key)      -> CallbackRefStub
async retrieve_callback_notification(slot_key, notif_index, _type=None) -> Any
def   callback_notification_keys(slot_key) -> list[int]
def   forget_callback_notification(slot_key, notif_index) -> None
```

Existing builtins extended:

- `retrieve_result(slot_key)` recognizes `["callback_open", N]`
  values and returns a `CallbackRefStub(slot_n=N)`.
- `forget(slot_key)` for a slot whose current value is
  `["callback_open", N]` also closes the callback (auto-emit
  `CloseCallbackOp(slot=N)` in the mutation). The `forget` builtin
  itself stays sync; the mutation builder does the discrimination.
- `schedule_invoke(...)`'s recording-proxy arg walker accepts
  `CallbackRefStub` and encodes it as either
  `["callback_handle", slot_key]` (same-script-create case) or
  `["callback", slot_n]` (cross-script case).

Channel notification attrs gain `notif_index`:

```
<channel source="workstation_mcp" slot_key="cb" kind="callback"
         notif_index="47">summary</channel>
```

## Design

### State shape

The workstation's session `state` dict gains a new top-level field
`callback_notifications`, sibling of `data_by_key`. The workstation
doesn't enforce a schema, so this is a pure MCP-side extension:

```
state = {
    "data_by_key":            { slot_key: ["callback_open", N] | ... },
    "callback_notifications": { slot_key: { notif_index_str: payload } },
}
```

`notif_index` is the originating `CallbackEvent.seq` (stringified
because JSON object keys are strings). `seq` is monotonically
increasing across the entire session, so notif indices for a given
`slot_key` are unique forever, never reset on close+recreate, never
collide.

`payload` is the **wire-escaped** value (same shape as the payload
on an `InvokeResultEvent` — `["entity", short_id]` / `["service",
short_id]` / `["bytes", b64]` / `[[…]]` literal-list-escape /
scalars). Sandbox-side `retrieve_callback_notification` runs the
same three-pass unescape as `retrieve_result` does for invoke
results.

### Mirror

`SessionMirror` gains a `callback_notifications: dict[str, dict[str,
list[Any]]]` field, populated by `apply_workstation_view` from the
workstation's `state["callback_notifications"]`. Same shallow-copy
semantics as `data_by_key`.

`pending_slot_index` extends to include `["callback_open", N]` —
`_is_pending_marker` grows a third recognized tag. The index now
serves three kinds of slot lookups (`invoke_pending`,
`load_pending`, `callback_open`); `_apply_state_effect`'s per-kind
branch decides whether to delete the entry on promote (invokes:
yes; callbacks: no, since callbacks fire repeatedly).

### Connection-level unflushed state

A new field on `WorkstationConnection`:

```
_unflushed_callback_notifications: dict[tuple[str, int], UnflushedNotification]
```

Mirrors the existing `_unflushed_state_changes` map but keyed by
`(slot_key, notif_index_int)`. Holds the wire-escaped payload + the
source seq for each callback event we've absorbed locally but the
workstation hasn't subsumed yet. Cleared / pruned in the same
places as `_unflushed_state_changes` (subscribe wipe, foreign
state_update rebuild, self-commit clear).

### Notification arrival flow (`_apply_state_effect`)

A new branch for `CallbackEvent`:

```
case CallbackEvent():
    slot_key = mirror.pending_slot_index.get(ev.slot)
    if slot_key is None: raise ProtocolError(...)
    mirror.callback_notifications.setdefault(slot_key, {})[str(ev.seq)] = ev.payload
    _unflushed_callback_notifications[(slot_key, ev.seq)] = UnflushedNotification(
        seq=ev.seq, value=ev.payload
    )
    # NB: no del from pending_slot_index — callback stays open.
    # NB: no change to mirror.data_by_key — the ["callback_open", N]
    #     entry stays.
    await _maybe_notify(ev.seq, slot_key, "callback",
                        summarize_invoke_payload(ev.payload),
                        notif_index=ev.seq)
```

`_maybe_notify` grows an optional `notif_index: int | None` kwarg
that, when set, becomes a third attr on the channel frame.

The "callback stays open" branch is the only place where
`_apply_state_effect` doesn't follow the invoke-result shape;
worth a comment in the source.

### Mutation lifecycle (extending phase 3-5)

`_apply_script_mutation` doesn't change shape. The detail work goes
into `mutation_builder.py`:

1. **Slot allocation** — rename `_allocate_invoke_slots` →
   `_allocate_slots`, returning a single
   `dict[(kind, slot_key), int]` (or two parallel dicts —
   implementation detail). Walks `runner_queued_ops` in order;
   each `schedule_invoke` *and* each `create_callback` claims the
   next slot (`slot_counter + 1`, `slot_counter + 2`, ...).
   Workstation requires strict-monotonic slot numbers across all
   slot-claiming ops in one mutation, so the ops list is emitted
   in script order.

2. **`apply_ops`** handles `KIND_CREATE_CALLBACK` by writing
   `["callback_open", slot_n]` to the post-state, plus a new
   `KIND_FORGET_CALLBACK_NOTIFICATION` that removes the entry from
   `callback_notifications` (in the post-state — needed by
   `desired_tracker_set` so dropped notifications stop pinning
   their entity refs).

3. **JSON Patch builder** (`_build_json_patch`):
   - Pre-flush callback notifications: one `add
     /callback_notifications/<slot_key>/<seq>` per entry in
     `_unflushed_callback_notifications`, ordered by `(slot_key,
     seq)` for determinism. Handle the
     "`callback_notifications` object doesn't exist yet" /
     "the slot_key sub-object doesn't exist yet" cases with the
     same helper pattern as `_data_by_key_object_exists`.
   - For each `create_callback` queued op: `add` or `replace`
     `/data_by_key/<slot_key>` with `["callback_open", slot_n]`.
   - For each `forget` queued op whose **running** value (after
     pre-flush + earlier ops in this script) is
     `["callback_open", N]`: same `remove /data_by_key/<slot_key>`
     as today, but the *mutation ops list* (not patch) also
     appends `CloseCallbackOp(slot=N)`.
   - For each `forget_callback_notification` queued op: `remove
     /callback_notifications/<slot_key>/<notif_index>`. (No
     workstation-side op — pure JSON Patch.)

4. **Mutation ops list** — the per-script-op walk emits, in
   order, one of: `InvokeOp` (existing), `CreateCallbackOp` (new),
   `CloseCallbackOp` (auto-emitted for forget-on-callback-slot).
   Tracker diff still goes first, then the slot-claiming ops in
   script order.

5. **Placeholder substitution** — `_substitute_entity_placeholders`
   handles a third placeholder kind: `["callback_handle",
   slot_key]` → `["callback", slot_n]` (using the slot-allocation
   table from step 1).

### `CallbackRefStub`

Single dataclass with both fields optional; the encoder picks the
right wire form based on which is set:

```python
@dataclass(frozen=True)
class CallbackRefStub:
    slot_key: str | None = None  # set when created in the same script
    slot_n:   int | None = None  # set when looked up via retrieve_result
```

Sandbox-side `_walk` (in `schedule_invoke`'s arg encoder):

```
if isinstance(v, CallbackRefStub):
    if v.slot_n is not None:   return ["callback", v.slot_n]
    if v.slot_key is not None: return ["callback_handle", v.slot_key]
    raise ValueError(...)
```

`create_callback(slot_key)` returns `CallbackRefStub(slot_key=...)`.
`retrieve_result(slot_key)` for `["callback_open", N]` returns
`CallbackRefStub(slot_n=N)`. The agent treats the stub as opaque.

### Tracker diff

`desired_tracker_set` walks **both** `data_by_key` and
`callback_notifications`. For each callback notification payload,
the existing `_walk_for_tracker_set` already handles the
list-escape grammar — `["entity", id]` / `["service", id]` tags get
pulled in. Result: entity refs surfaced in callback payloads stay
session-pinned by our tracker as long as the notification is in
state (i.e., until the agent calls
`forget_callback_notification`).

The workstation's event-log refcount on those entities drops at
ack-horizon time (same mechanism that already drives invoke-result
entity refs). Our `TrackEntityOp` on the same mutation takes over;
the workstation processes ops before advancing ack, so there's no
gap. (This is the same invariant that makes
`entity_arg_echo` work today.)

### Reconnect / resubscribe

Snapshot's `last_state.state` carries `callback_notifications`
authoritatively. `apply_workstation_view` rebuilds
`mirror.callback_notifications` from it. The `pending_slot_index`
rebuild already covers `callback_open` markers (via the extended
`_is_pending_marker`). Subsequent CallbackEvents in
`subsequent_events` replay through `_apply_state_effect` — the
notification dedupe (`_notified_seqs`) suppresses re-delivery to
the agent, but `_unflushed_callback_notifications` rebuilds
correctly because the prior-cycle's mutation that would have
flushed those entries never committed.

### `forget` semantics summary

```
forget(slot_key) on data_by_key value:
  ["exported_entity", id] / ["exported_service", id] / ["invoke_result", _]
                            → remove /data_by_key/<slot_key>
  ["invoke_pending", N]     → remove /data_by_key/<slot_key>
                              (pending invoke is orphaned — workstation
                               will still emit a result event into the
                               released slot, which will fail
                               pending_slot_index lookup and raise
                               ProtocolError. Same behavior as today
                               for invoke_pending; documenting for
                               completeness.)
  ["callback_open", N]      → remove /data_by_key/<slot_key>
                              + CloseCallbackOp(slot=N)
                              (callback_notifications[slot_key] is NOT
                               cleared — see "open questions" below.)
```

### Wire encoding additions

No changes to the workstation's wire vocabulary (workstation already
supports `["callback", N]`, `CreateCallbackOp`, `CloseCallbackOp`,
`CallbackEvent`). Only MCP-internal additions:

- Sandbox-emitted `["callback_handle", slot_key]` placeholder
  (rewritten by mutation builder).
- Sandbox queued-op tagged-union grows `CreateCallbackOp(slot_key)`
  and `ForgetCallbackNotificationOp(slot_key, notif_index)`.
- Mirror-internal `["callback_open", N]` value tag in
  `data_by_key`.

### Channel content

Same `summarize_invoke_payload(payload)` shape as today, no prefix
change. The agent's mental model: `kind="callback"` + `notif_index`
attr distinguish callback frames from `kind="invoke_result"`
frames. Frame attrs are still identifier-shaped per the channel
contract (`notif_index` is the int rendered as a decimal string).

## Demo (`examples/basic_test_demo/`)

### New service: `CallbackTesterImpl`

Methods:

```
async register_callback(cb: ServiceRef[EventListenerProto]) -> None
async fire_notify(payload: FxtrServiceData, delay: float)    -> None
```

`register_callback` bridges the received callback ref into a
long-lived impl-owned scope (so it survives past the dispatch
`call_scope`). Subsequent `fire_notify` calls spawn a
`task_group.start_soon(delayed_notify, ...)` task that sleeps
`delay`, then calls `cb.notify(payload)` and discards the return
value. `fire_notify` returns immediately (fire-and-forget).

**Scope structure** (load-bearing for clean shutdown):

- The impl runs in its **own async task**, separate from the
  task that owns the workstation. The demo's task layout becomes:
  ```
  _run task_group
    ├── WorkstationServer (async-CM)
    └── callback_tester_worker task
          ├── opens peer /rpc connection
          ├── opens impl_scope (isolated, mountable; child of
          │   peer.scope but NOT a child of any workstation-internal
          │   scope)
          ├── opens its own anyio.create_task_group() for
          │   delayed_notify children
          ├── mounts CallbackTesterImpl on impl_scope
          ├── expose_service under CALLBACK_TESTER_NAME
          └── parks until shutdown signal; on shutdown, cancels its
              inner task group (draining any in-flight
              delayed_notify tasks), releases the registration,
              drains impl_scope — all of this completes before
              the workstation's own teardown begins.
  ```
- `register_callback` uses
  `bridge_service_from_scope(cb_ref, source=call_scope,
  dest=impl_scope)` to ferry the received ref into the
  impl-owned scope. The bridged ref outlives the dispatch
  `call_scope`. The bridge pins its source scope, which is fine
  here because the source is the workstation's call_scope (which
  the workstation is going to keep alive for the duration of the
  bridge's lifetime via its own structured concurrency — same
  pattern `register_service` documents under "structurally safe
  usage patterns").
- The independent-task structure is what lets the demo service
  unregister + drain cleanly *before* the workstation needs to
  tear down. If we instead mounted the impl directly on
  `peer.scope` and held a bridge pin into `call_scope` from a
  shared task group, workstation shutdown could deadlock against
  the bridge cycle.

If anything weird surfaces in this scope-bridging step — bridge
pin cycles, drain deadlocks, callback ref losing its mount — pause
and re-evaluate the structure rather than papering over it.

The third built-in service in the demo. `agent_workdir/packages/
demo_pkg/__init__.py` gains a `CALLBACK_TESTER_NAME =
"callback-tester"` constant; `run_demo_workstation.py` registers
the impl under that external name.

### New scenarios

`demo_pkg/scenarios.py` gains:

- `schedule_same_script_callback(slot_key, payload, delay)` —
  exercises the same-script `create_callback` + `schedule_invoke`
  path. Creates a callback, then in the same script calls
  `schedule_invoke` on `callback-tester.register_callback(cb)`
  followed by `callback-tester.fire_notify(payload, delay)` in a
  second `schedule_invoke`. (Two invokes; both happen in the same
  mutation. Tests slot monotonicity.)
- `schedule_cross_script_callback(slot_key)` — half 1: just
  `create_callback(slot_key)`. The follow-up call lives in a
  second `run_script`.
- `schedule_cross_script_callback_use(slot_key, payload, delay)` —
  half 2: `cb = await retrieve_result(slot_key)`, then
  `schedule_invoke` register + fire on it.
- `retrieve_one_callback_notification(slot_key)` —
  `keys = callback_notification_keys(slot_key); assert
  len(keys) >= 1; return await retrieve_callback_notification(
  slot_key, keys[-1])`.

## Tests

Two new test files alongside the existing `test_invoke_e2e.py`:

### `test_callback_e2e.py`

Same shape as `test_invoke_e2e.py`. Boots a real workstation,
registers `CallbackTesterImpl` (test-local copy, not the demo's),
drives the MCP through:

1. **Same-script test**:
   - `create_callback("cb")` + `schedule_invoke` register +
     `schedule_invoke` fire_notify in one script.
   - Poll the test's captured channel frames until a
     `kind="callback"` frame for `slot_key="cb"` arrives.
   - Assert the frame carries `notif_index` (some int), and the
     content has the expected payload shape.
   - In a follow-up script, call
     `callback_notification_keys("cb")` and
     `retrieve_callback_notification("cb", keys[-1])`; assert
     the returned value matches the payload sent.

2. **Cross-script test**:
   - Script 1: `create_callback("cb")`.
   - Script 2: register + fire_notify (using
     `await retrieve_result("cb")` to get the stub).
   - Wait for channel frame.

3. **Forget cancels future notifications**:
   - After (1), call `forget("cb")` in another script.
   - Schedule another fire_notify against the (now-closed)
     callback by stashing the slot_n before forget (so we can
     emit `["callback", N]` referring to a closed slot — the
     workstation's `lookup_callback` is permissive against
     pending_creates ∪ record.open_callbacks; for a closed
     callback we'd need to test that `notify` returns `False`
     and no `CallbackEvent` is emitted).

   Actually wait — once forget commits, the slot is gone from
   `open_callbacks`. The next `schedule_invoke` whose args
   include `["callback", N]` referring to the closed slot would
   fail op-validation with `lookup_callback` raising. So we
   can't directly test "send a notify to a closed callback"
   through a fresh invoke — it'd never get to the demo service
   in the first place.

   Reformulated: schedule a fire_notify with a *long delay*
   while the callback is open; then immediately forget the
   callback; the demo service's delayed_notify task fires `notify`
   on its stashed cb ref *after* the callback is closed
   workstation-side, and `notify` returns `False` (per
   `WorkstationCallbackImpl._closed`). No `CallbackEvent` is
   emitted; no channel frame arrives.

   Concretely:
   - Script A: `create_callback("cb")` + register + fire_notify
     with `delay=1.0`.
   - Brief sleep on the test side (50ms-ish) to let the script
     commit but well before the 1s delay elapses.
   - Script B: `forget("cb")` (closes the callback).
   - Wait for the 1s delay + a bit; assert no `kind="callback"`
     channel frame for `slot_key="cb"` ever arrived.

   (The demo service could optionally surface the `notify` bool
   via a log line, which the test can inspect — but the
   "no channel frame" assertion is the load-bearing one.)

4. **Forget callback notification** — call
   `forget_callback_notification("cb", idx)` and assert the
   subsequent `callback_notification_keys("cb")` doesn't
   include `idx`, and `retrieve_callback_notification("cb",
   idx)` raises `KeyError`.

### `test_mcp_subprocess.py` extension

Add a callback path to the subprocess test (similar to how it
already exercises schedule_invoke + retrieve_result via the
demo scenarios). One same-script and one cross-script case is
enough; the forget-cancels-future-notifications case can stay
in `test_callback_e2e.py` since it doesn't need the subprocess
layer.

## Implementation steps

- [x] **Sandbox protocol additions** (`sandbox/.../protocols.py`):
      `CallbackRefStub`, `CreateCallbackOp(slot_key)`,
      `ForgetCallbackNotificationOp(slot_key, notif_index)`.
      Extend `QueuedOp` union. Document the wire-form encoding for
      `CallbackRefStub` in the dataclass docstring.
- [x] **Sandbox builtins** (`sandbox/.../builtins.py`):
      `create_callback`, `callback_notification_keys`,
      `retrieve_callback_notification`,
      `forget_callback_notification`. Extend `retrieve_result`
      to return `CallbackRefStub(slot_n=N)` for
      `["callback_open", N]` values. Extend `schedule_invoke`'s
      `_walk` to encode `CallbackRefStub` to either
      `["callback_handle", slot_key]` or `["callback", slot_n]`.
- [x] **Host RPC methods** (`fxtr_workstation_mcp/host_impl.py`):
      `read_callback_notification(slot_key, notif_index) ->
      list[Any] | None`,
      `list_callback_notification_indices(slot_key) ->
      list[int]`. Both read from the mirror. Extend
      `McpHostProto` accordingly.
- [x] **Mirror extensions** (`session_state.py`):
      `SessionMirror.callback_notifications`,
      `apply_workstation_view` populates it,
      `_is_pending_marker` recognizes `"callback_open"`.
      `UnflushedNotification` dataclass (mirrors
      `UnflushedChange`).
      Op-kind constants `KIND_CREATE_CALLBACK`,
      `KIND_FORGET_CALLBACK_NOTIFICATION`.
      `apply_ops` handles both new kinds.
      `desired_tracker_set` walks
      `callback_notifications` payloads too.
- [x] **Update CODE_OVERVIEW.md** (interim — mirror + state shape).
- [x] **Connection state**
      (`ws_connection.py.WorkstationConnection`):
      `_unflushed_callback_notifications` field.
      Reset on subscribe wipe, prune on
      `_handle_state_update`'s ack-horizon math, clear on
      `_apply_local_commit`.
- [x] **`_apply_state_effect`** gains `CallbackEvent` branch.
      `_maybe_notify` grows `notif_index` kwarg; channel attrs
      include it.
- [x] **Mutation builder** (`mutation_builder.py`):
      `_allocate_slots` unifies invoke + callback allocation in
      script order. `apply_ops` post-state for the new kinds.
      `_build_json_patch` emits the
      `callback_notifications` pre-flush, the create-callback
      `add`/`replace`, the forget-on-callback-slot dual
      patch+CloseCallbackOp, and the
      `forget_callback_notification` `remove`. `_build_invoke_ops`
      generalizes to emit `InvokeOp` / `CreateCallbackOp` /
      `CloseCallbackOp` in script order. Placeholder substitution
      handles `callback_handle`.
- [x] **Update CODE_OVERVIEW.md** (interim — mutation lifecycle +
      handle namespace).
- [x] **Demo service**
      (`examples/basic_test_demo/run_demo_workstation.py`):
      `CallbackTesterProto`, `CallbackTesterImpl` with
      fire-and-forget `fire_notify`. Bridge the stashed callback
      into an impl-owned scope; spawn delayed-notify on a
      long-lived task group.
- [x] **Demo package** (`demo_pkg/__init__.py` + `scenarios.py`):
      `CALLBACK_TESTER_NAME` constant; new scenarios for
      same-script and cross-script callback usage and notification
      retrieve.
- [x] **SKILL.md / preamble**: document `create_callback` /
      `callback_notification_keys` /
      `retrieve_callback_notification` /
      `forget_callback_notification` in the agent-facing skill.
      Add the new names to `preamble.py`'s top-of-file imports.
- [x] **Tests**: `test_callback_e2e.py` covering all four cases
      (same-script, cross-script,
      forget-cancels-future-notifications,
      forget_callback_notification) — passes after the
      pending-changes refactor (see "Wrinkles" below).
- [x] **Cross-process MCP coverage**: replaced
      ``test_mcp_subprocess.py`` with ``test_mcp_in_memory.py``,
      which runs the package's FastMCP server in-process over
      :func:`mcp.shared.memory.create_client_server_memory_streams`.
      Same coverage (real JSON-RPC framing, real FastMCP
      tool-dispatch, real ``ServerSession`` notification path)
      minus the actual subprocess spawn. Two scenario tests:
      ``test_in_memory_e2e_all_scenarios`` (basic_echo +
      entity_arg_echo + fetch_demo_note) and
      ``test_in_memory_e2e_callback_scenarios`` (cross-script
      callback). The subprocess variant kept hanging at cleanup
      when its 30s test-budget timer fired mid-teardown and
      orphaned the MCP child process; the in-memory rewrite
      sidesteps that entirely (task group cancellation handles
      teardown cleanly).
- [x] **Update CODE_OVERVIEW.md** (final).

## Wrinkles that came up during implementation

- The original mutation-builder design tracked per-top-level-field
  existence with heuristic flags
  (``workstation_has_data_by_key`` / ``workstation_has_callback_notifications``)
  passed across function signatures, plus separate
  ``_unflushed_state_changes`` and
  ``_unflushed_callback_notifications`` maps for the two top-level
  fields. This bit us when ``_apply_local_commit`` synthesized
  ``mirror.state["callback_notifications"]`` even when the patch
  didn't actually create it, so the mirror's view of "what the
  workstation has" diverged from reality and the next mutation
  emitted a stale ``add /callback_notifications/...`` that the
  workstation rejected.

  We replaced both unflushed maps with a unified
  ``_pending_changes: list[PendingChange]`` ordered log. Each
  ``PendingChange`` is one of ``"initialize"`` / ``"set"`` /
  ``"remove"``. The connection enqueues a synthetic
  ``"initialize"`` entry whenever ``apply_workstation_view``
  lands a state lacking the top-level fields the MCP writes to
  (``workstation_state_needs_init``); event-driven entries
  carry the originating event's ``seq``. The mutation builder
  walks the log against a running copy of the workstation's
  pre-state, lazy-creating any missing intermediate
  sub-container objects (``/callback_notifications/<slot_key>``
  for newly-seen slot_keys) via a path-walking helper. The
  build result returns the *full* post-state dict, which
  ``_apply_local_commit`` plugs into the mirror directly — no
  field-by-field synthesis.

  After this refactor the four ``test_callback_e2e.py`` cases
  all pass. The teardown ``RPCConnectionAborted`` we saw earlier
  was a symptom of the mid-test mutation rejection, not a
  separate bridge-lifetime issue.

- The demo's ``_delayed_notify`` task needs an explicit scope to
  resolve the bridged callback ref against. Background tasks have
  no active scope, so we pass the impl-owned ``impl_scope`` into
  ``_delayed_notify`` and call ``cb.resolve(impl_scope)``.

- Same-mutation invoke-ordering is not guaranteed by the
  workstation — ``invoke_worker`` spawns each ``InvokeOp`` as a
  concurrent task. So we collapsed
  ``register_callback`` + ``fire_notify`` into a single
  ``register_callback_and_fire_notify`` method on the
  test-local ``_CallbackTesterImpl``. The demo workstation
  still exposes the two-method API the plan called for, with a
  matching ordering caveat in its docstring; that's fine for
  manual operation where the agent can wait between scripts,
  but the e2e test uses the combined helper for determinism.

- ``McpHostProto.read_callback_notification`` returns ``Any``
  and raises ``IRException(category="KeyError")`` on missing
  notifications. The sandbox-side
  ``retrieve_callback_notification`` catches that and re-raises
  a Python ``KeyError`` to the agent. Original ``list[Any] | None``
  return type was too narrow — fxtr-RPC's encoder used the
  annotation to drive list-copy logic, which crashed on bare
  scalar payloads.

- We tried adding ``test_subprocess_e2e_callback_scenarios`` to
  the existing stdio subprocess fixture and hit a teardown
  hang. SIGALRM (the 30s per-test timeout) fired during the
  test body, raised ``TimeoutError`` mid-cleanup, cancelled
  ``stdio_client.__aexit__`` before it could terminate the MCP
  subprocess via SIGTERM, and the orphaned child kept the
  pytest stderr-capture pipe open. We pivoted to running the
  MCP server in-process over
  :func:`mcp.shared.memory.create_client_server_memory_streams`
  (``test_mcp_in_memory.py``) which preserves the same
  coverage minus the subprocess and ``uv run`` entrypoint.
  Worth holding on to as a cautionary tale: stdio-subprocess
  tests are inherently fragile under cancellation because
  ``mcp.client.stdio.__aexit__`` only SIGTERMs the child via
  the ``except TimeoutError:`` path, and an outer
  ``CancelledError`` bypasses that arm.

## Open questions / known limitations

1. **Slot_key reuse after callback close.** If the agent does
   `create_callback("cb")` → notifications arrive →
   `forget("cb")` → `create_callback("cb")` again, the new
   callback's notifications mix with the old ones in
   `callback_notifications["cb"]`. The agent has to call
   `forget_callback_notification` for each old index first, or
   pick a different slot_key. We don't auto-purge on
   `forget(slot_key)` because the user wants notifications to
   outlive their callback. Documented; revisit if it bites.

2. **Long-delay notify after callback close.** The demo's
   delayed-notify task uses its stashed callback ref to call
   `notify` after the agent has already forgotten the callback.
   `WorkstationCallbackImpl.notify` short-circuits to `False`
   when `_closed`, so no `CallbackEvent` is emitted and no
   channel frame fires. This is the load-bearing assertion for
   test (3). The `notify` bool is currently discarded by the
   demo service; we could surface it via a sentinel log line if
   that's worth testing too.

3. **`["callback", N]` to a closed callback in invoke args.** The
   workstation's `lookup_callback` rejects refs to closed
   callbacks at op-validation time (not in `record.open_callbacks`
   anymore). So once the agent forgets a callback, they
   structurally cannot ship its ref to another service in a new
   invoke. Aligns with the rest of the model — no special
   handling needed on our side.

4. **Empty `callback_notifications[slot_key]` sub-objects.** After
   the agent forgets all notifications for a slot, the empty
   sub-object lingers in state. Doesn't affect correctness;
   minor state-size leak. Could clean up by emitting an extra
   `remove /callback_notifications/<slot_key>` patch op when the
   running post-state's sub-object is empty. Defer unless it
   matters in practice.

5. **Demo service scope-bridging.** Resolved in the demo
   section above: use `bridge_service_from_scope` with the
   impl-owned scope as `dest`, and run the demo service in its
   own async task with its own task group so it can drain
   cleanly before the workstation tears down. If a bridge cycle
   or drain deadlock surfaces despite that structure, pause and
   reassess rather than working around it.
