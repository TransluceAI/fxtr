# Stage 10 — Persistent event log + user-message channel

## Goal

Give the MCP plugin a typed, persistent record of everything it
does, and a way for a human-driven frontend to:

- watch that record live as it grows,
- send messages into the agent's session.

End-user surface:

- A linked list of typed entities (`WsAgentLogEventChain`) recording
  every script execution, invoke result, callback firing, etc.,
  persisted to a workstation persistence cap on every chain
  extension. Reloaded on reconnect.
- A workstation-mounted service `agent_mcp:{session_id}` (registered
  by the MCP over `/rpc`) with `get_state` / `watch_state` / `send`
  methods.
- A sandbox-side `present_to_user(payload)` builtin so script code
  can put arbitrary data into the log explicitly.

## High-level design

```
                          MCP plugin (host)
  ┌───────────────────────────────────────────────────────────────┐
  │                                                               │
  │  workstation/rpc connection                                   │
  │    │                                                          │
  │    ├── workstation_proxy (existing)                           │
  │    │                                                          │
  │    ├── agent_mcp_store ref (PersistenceProto)  ◄──┐           │
  │    │     looked up by external_name on subscribe │           │
  │    │                                              │           │
  │    └── registers `agent_mcp:{session_id}` ───┐    │           │
  │                                              │    │ pin head  │
  │  AgentMcpImpl                                │    │ on extend │
  │    ├── current chain_head: EntityRef[...]  ──┼────┘           │
  │    ├── inbox_short_id: str                   │                │
  │    └── watchers: list[ServiceRef[EventListenerProto]]         │
  │                                              │                │
  │  workstation/client subscription (existing)  │                │
  │    │                                         │                │
  │    └── _do_event_job                         │                │
  │          + builds + pins log entries ────────┘                │
  │  _do_script_job                                               │
  │    + builds + pins ScriptExecution entries                    │
  │                                                               │
  └───────────────────────────────────────────────────────────────┘
                              ▲
                              │ agent_mcp.send / watch_state / get_state
                              │
                       frontend (separate /rpc peer)
```

Three orthogonal pieces, each described below: the **typed log
entities + walker**, the **agent_mcp service**, and the
**present_to_user builtin**. Tying them together: the supervisor's
`_do_subscribe` learns how to create `workstation_inbox` and mount
the service; the worker's `_do_event_job` / `_do_script_job` paths
gain a log-extension step before they return.

## Background

- **Sandbox-provenance walker** (in
  `projects/fxtr_sandboxing/fxtr_sandbox_rpc_adapter/_provenance.py`)
  walks a `FxtrServiceData` tree, substituting `ServiceRef` leaves
  with stored `ServiceConfigReplacementEntity` instances. We mirror
  that shape (almost identically) for the agent log substitution
  walker — replacing wire-form entity / service / callback
  short_ids with typed `EntityRef` / placeholder entities. See
  *Substitution walker* below.

- **Workstation persistence cap** (`PersistenceProto`, see
  `projects/fxtr_workstation/CODE_OVERVIEW.md`) — `pin` / `unpin` /
  `retrieve` / `try_retrieve` / `list_pins`. Two modes
  (`no_provenance` / `preserve_provenance`); both accept bare d-tag
  data. The MCP looks up the cap by external_name at subscribe time
  and uses it transparently — whichever mode the workstation
  operator chose.

- **Workstation callback short_ids.** Every workstation-side
  callback record has a `service_short_id` (allocated when the
  `CallbackRecord` is created, dropped when the slot closes).
  Currently exposed only on `SessionDebugSnapshotEvent` (`open_callbacks:
  dict[int, str]`). For this stage we add a dedicated request /
  response so the MCP can fetch one short_id without subscribing to
  the chatty debug stream.

## New entity types

All in a new host-only module
`fxtr_workstation_mcp/agent_log_types.py`. Stable namespace UUID
`WS_AGENT_NS` defined alongside.

### Substitution-target entities

```text
WorkstationServicePlaceholder(DataclassEntity)
  service_type_id: FxtrID
  service_ctx:     FxtrID
  short_id:        str

OwnedCallbackPlaceholder(DataclassEntity)
  slot_n:   int
  slot_key: str    # "<unknown>" if reverse-lookup fails (pathological)
```

These replace `["service", short_id]` / `["callback", N]` leaves in
walked payloads. Content-addressed; an identical service appearing
multiple times in the same payload (or across payloads) maps to one
stored entity.

`slot_key` is reliably available for owned callbacks via
`mirror.pending_slot_index[N]` (which keeps callback entries past
promote, only dropping invoke entries on result). The
`"<unknown>"` sentinel only fires if a payload references a slot
that's already been closed and forgotten — pathological;
warning-logged + sentinel-stored rather than raising.

### Preamble — content-addressed for storage reuse

```text
ScriptPreamble(DataclassEntity)
  source: str
```

`ScriptExecution.preamble` references this rather than carrying the
source inline, so multiple script runs with identical preambles
share one preamble entity (content-addressed via mashumaro).

### Log spine

```text
WsAgentLogEventChain(DataclassEntity)
  prev:      EntityRef[WsAgentLogEventChain] | None
  event:     EntityRef[ScriptExecutionLogEvent | InvokeResultLogEvent
                       | InvokeErrorLogEvent
                       | CallbackNotificationLogEvent]
  timestamp: float    # unix seconds, host clock at the time of extension
```

Linked-list, head-only — older entries are reachable only by
following `prev`. The agent_mcp persistence cap holds *only* the
current head's id under key `session_id`; transitive ancestors
remain in the workstation's tracked entity registry because the
chain head's stored cbor references them.

### Event payload entities

Each is a `DataclassEntity` whose `payload` field is `FrozenFxtrCbor`
populated by the substitution walker.

```text
ScriptExecutionLogEvent(DataclassEntity)
  script_src:    str
  preamble:      EntityRef[ScriptPreamble]
  stdout:        str
  stderr:        str
  error:         str | None      # script-raised traceback (atomic abort)
  rejected:      str | None      # workstation rejection reason
  ops:           tuple[ScriptOpRecord, ...]   # empty on error / rejected
  committed_seq: int | None      # None on rejection or script-raised

InvokeResultLogEvent(DataclassEntity)
  slot_key: str
  slot_n:   int
  seq:      int                  # workstation event seq
  payload:  FrozenFxtrCbor

InvokeErrorLogEvent(DataclassEntity)
  slot_key: str
  slot_n:   int
  seq:      int
  message:  str

CallbackNotificationLogEvent(DataclassEntity)
  slot_key: str
  slot_n:   int
  seq:      int
  payload:  FrozenFxtrCbor
```

Naming note: each event entity uses the `LogEvent` suffix
specifically to avoid name collisions with the workstation's
wire-protocol types (`InvokeResultEvent` / `InvokeErrorEvent` /
`CallbackEvent` in `fxtr_workstation.client_protocol`). The
`Log` infix makes the agent-side persistent-log distinction
explicit at the import site.

User messages (`agent_mcp.send`) flow through the
`workstation_inbox` callback, so they land here as
`CallbackNotificationLogEvent(slot_key="workstation_inbox", ...)`.
The frontend distinguishes by inspecting `slot_key`. No separate
`MessageReceived` type.

**Failed scripts.** When `error is not None` or `rejected is not
None`, `ops` is the empty tuple — the script didn't commit any
ops, so we drop them rather than carrying half-effects. All
informative detail lives in `stderr` / `error` / `rejected`.

### ScriptOpRecord — discriminated subpart

`DataclassFxtrSubpart` (inline only; no separate entity storage)
with a `kind: str` discriminator. One subclass per script-side op
kind:

```text
ScriptOpRecord(DataclassFxtrSubpart)   # base
  kind: Literal["export_entity" | "export_service" | "forget"
                | "schedule_invoke" | "create_callback"
                | "forget_callback_notification" | "present_to_user"]
  slot_key: str | None     # all kinds except forget_callback_notification

ExportEntityOpRecord       — adds entity: EntityRef[UnknownEntity]
ExportServiceOpRecord      — adds service: EntityRef[WorkstationServicePlaceholder]
ForgetOpRecord             — no extra fields
ScheduleInvokeOpRecord     — adds service_short_id, method_name, args, kwargs
                             (args/kwargs walked through substitution)
CreateCallbackOpRecord     — no extra fields
ForgetCallbackNotificationOpRecord — adds notif_index: int (slot_key non-None)
PresentToUserOpRecord      — adds payload: FrozenFxtrCbor (walked)
```

mashumaro tagged-union encoding by `kind` keeps round-trip clean.

## Substitution walker

New host-only module `fxtr_workstation_mcp/log_walker.py`. Async
single-pass walk over a wire-form value (with FxtrID leaves still as
`["entity", short_id]` etc.), producing a `FrozenFxtrCbor` tree:

| Input                              | Output                                                                   |
|------------------------------------|--------------------------------------------------------------------------|
| `["entity", short_id]`             | `RawWeakFxtrID(workstation_proxy.get_entity(short_id))`                  |
| `["service", short_id]`            | `RawStrongFxtrID(stored WorkstationServicePlaceholder.entity_id)`        |
| `["callback", N]`                  | `RawStrongFxtrID(stored OwnedCallbackPlaceholder.entity_id)`             |
| `["bytes", b64]`                   | `bytes` (b64-decoded)                                                    |
| `[[inner]]` literal-list-escape    | `tuple(walk(item) for item in inner)`                                    |
| `dict`                             | `frozendict` with values recursed                                        |
| scalar / `None`                    | passthrough                                                              |

For service / callback substitutions:
- Resolve service refs via `workstation_proxy.get_service(short_id)`
  → introspect `$fxtr.type` + `$fxtr.ctx` in parallel.
- Build placeholder entity, `entity.store(scope)` it into the
  walker's explicit `scope` argument (per scope discipline — see
  `v2/notes/scope_discipline.md`; no implicit active-scope use).
- Cache `short_id → entity_id` per walk; identical leaves don't
  re-introspect.

Owned callbacks (`["callback", N]`) reverse-look-up `slot_key` via
`mirror.pending_slot_index.get(N)` (best-effort; may be `None` if
the slot has already promoted).

Walker dispatches over mapping / sequence children via
`anyio.create_task_group` (same shape as
`fxtr_sandbox_rpc_adapter/_provenance.py:walk_config_for_provenance`).

## Log builder + chain extender

New host module `fxtr_workstation_mcp/log_builder.py`:

- `async build_script_execution_event(scope, ...)` — given a script's
  full context (sources, queued ops, mutation outcome, std streams),
  walk each op's payloads and build a `ScriptExecutionLogEvent`
  entity into the explicit `scope` argument. Empty-`ops` on the
  error/rejected paths.
- `async build_invoke_result_event(scope, ...)` /
  `build_invoke_error_event(scope, ...)` /
  `build_callback_notification_event(scope, ...)` — walk the
  relevant payload, build the corresponding event entity into
  `scope`.
- `async extend_chain(scope, prev_head, event_ref, persistence,
  session_id, update_head, ...) -> EntityRef[WsAgentLogEventChain]`
  — mints the new chain node into `scope`, pins to `persistence`
  (if non-None) under the session_id key, calls `update_head` to
  notify watcher tasks.

All run on the worker task (so they serialize with other jobs;
they're synchronous w.r.t. mirror state). Per scope discipline,
callers pass the impl scope explicitly as the `scope` argument;
the log builder never reads the active scope.

## `agent_mcp` service

### Protocol (host-only)

New module `fxtr_workstation_mcp/agent_mcp_proto.py`:

```python
AGENT_MCP_RUNNING_SESSION_TYPE_ID = system_entities.define_type(
    WS_AGENT_NS, "agent_mcp_running_session"
)

@service_protocol
class AgentMcpRunningSessionProto(Protocol):
    async def get_state(self) -> EntityRef[WsAgentLogEventChain] | None: ...
    async def watch_state(self, listener: ServiceRef[EventListenerProto]) -> None: ...
    async def send(self, payload: FxtrServiceData) -> None: ...
```

`EventListenerProto` re-used from `fxtr_workstation.protocols`.

### Impl

Mounted in the **MCP process** (host-side), on a per-cycle
`impl_scope` owned by the supervisor's session subtask.
`register_service` bridges its ServiceRef into the workstation;
incoming method dispatches from frontends route workstation →
MCP-over-`/rpc` → AgentMcpRunningSessionImpl. Each dispatch lands
inside an `open_rpc_scope`-allocated `call_scope` that inherits
from `impl_scope`, so return values (e.g. the chain head EntityRef)
chain-bridge cleanly to the caller.

New module `fxtr_workstation_mcp/agent_mcp_impl.py`:

```text
class AgentMcpRunningSessionImpl(TypedServiceImpl[AgentMcpRunningSessionProto]):
    fields:
        _chain_head: EntityRef[WsAgentLogEventChain] | None
        _inbox_callback_ref: ServiceRef[EventListenerProto]
        _watchers: list[_WatcherTask]
        _impl_scope: FxtrScope
        _impl_tg: anyio.abc.TaskGroup    # child of supervisor's session subtask

    get_state:
        Returns _chain_head directly. The framework chain-bridges
        the tracked EntityRef through call_scope's ancestor chain
        (impl_scope is in the chain), so no explicit copy is
        needed.

    watch_state(listener):
        Resolve listener (a ServiceRef[EventListenerProto]) to a
        proxy in the active call_scope. The proxy's underlying
        handle bridges through impl_scope so it survives past
        call return. Construct a _WatcherTask carrying the proxy,
        a `latest: EntityRef[...] | None` slot, and a fresh
        `has_update: anyio.Event`. Start a watcher worker task
        on `_impl_tg`.

    send(payload):
        proxy = await self._inbox_callback_ref.resolve()
        await proxy.notify(payload)
        # Workstation fires a CallbackEvent on workstation_inbox;
        # the MCP's /client subscription picks it up; standard
        # callback-event log path logs it as
        # CallbackNotificationLogEvent(slot_key="workstation_inbox").
```

### Watcher worker model

Each `watch_state` invocation spawns a dedicated worker task on
`_impl_tg`. The worker:

```text
async def _watcher_worker(state: _WatcherTask):
    while True:
        await state.has_update.wait()
        value = state.latest
        state.has_update = anyio.Event()    # reset before send
        try:
            still_listening = await state.proxy.notify(value)
        except Exception as e:
            log.warning("watcher.notify raised; dropping: %r", e)
            return
        if not still_listening:
            return
```

Update-side (called by the log builder via `update_head`):

```text
def update_head(new_head):
    self._chain_head = new_head
    for state in self._watchers:
        state.latest = new_head
        state.has_update.set()
    self._reap_finished_watchers()
```

Properties:
- **Skip-ahead, never-out-of-order.** A worker mid-`notify` sees
  the slot updated by the time it loops back to `wait()`; it
  always reads the latest. If multiple updates arrive during one
  send, only the latest is sent next.
- **No blocking.** `update_head` doesn't wait for any worker;
  it just writes slots and fires events. Slow watchers are their
  own problem.
- **Self-draining.** Workers exit cleanly on notify-False or
  exception; the next `update_head`'s sweep reaps them out of
  `_watchers`.
- **Recoverable history.** A skipped intermediate head is reachable
  via `prev` walks from any newer head.

When the cycle ends (or the session changes), `_impl_tg` cancels,
all workers exit, `_impl_scope` drains, the workstation drops the
`agent_mcp:{session_id}` registration via `expose_service`'s
release path.

## `present_to_user` sandbox builtin

### Sandbox-bridge addition

In `fxtr_ws_mcp_sandbox_bridge/protocols.py`:

```python
@dataclass(frozen=True)
class PresentToUserOp:
    payload: dict[str, Any] | list[Any]   # wire-form
    entity_arg_handles: list[str]
    kind: str = "present_to_user"

QueuedOp = (... | PresentToUserOp)        # extend the existing union
```

In `fxtr_ws_mcp_sandbox_bridge/builtins.py`:

```python
async def present_to_user(payload: FxtrServiceData) -> None:
    entity_arg_handles = []
    walked = await _walk_for_wire(payload, register_via_host)
    _enqueue(PresentToUserOp(payload=walked, entity_arg_handles=...))
```

Reuses the existing wire walker so EntityRef args round-trip through
`host.copy_entity_in` (same lifetime contract as
`schedule_invoke`'s entity args) and ServiceRef args end up as
`["service", short_id]` / `["callback_handle", slot_key]` /
`["callback", N]` placeholders.

### Host-side wire-form processing

`PresentToUserOp` is **MCP-side only** — *no* JSON Patch, *no*
workstation op emitted. The mutation builder ignores it.

But: `["entity_handle", handle]` and `["callback_handle", slot_key]`
placeholders need post-processing the same way `schedule_invoke`
args do (substitute the workstation short_id / slot_n after
`expose_entity` returns and after slot allocation). Two paths:

1. **Run the mutation-builder placeholder rewrite over present_to_user
   payloads too.** Tidy — the same code substitutes placeholders for
   both invoke args and present-to-user payloads. The rewrite produces
   a fully wire-form payload (`["entity", short_id]` / `["callback", N]`).
2. After the mutation commits, walk the rewritten payload through
   the substitution walker to produce the typed log form.

The substitution rewrite (step 1) lives next to `build_mutate_request`
(in `mutation_builder.py`); the typed-form walk (step 2) lives in the
log builder.

## New `/client` request

In `projects/fxtr_workstation/fxtr_workstation/client_protocol.py`:

```python
class GetCallbackServiceRequest(msgspec.Struct, tag="get_callback_service"):
    request_id: int
    subscription_id: int
    slot: int

class GetCallbackServiceResponse(msgspec.Struct, tag="get_callback_service"):
    request_id: int
    subscription_id: int
    slot: int
    service_short_id: str
```

Workstation handler (`client_app.py`):

- Validates `subscription_id` is owned by the calling connection
  and binds to a session subscription.
- Looks up the session's `open_callbacks[slot]`. Errors with an
  `ErrorResponse` if absent.
- Returns `GetCallbackServiceResponse(service_short_id=cb.service_short_id)`.

No side effects, no refcount changes. Constant-time.

## Toml config addition

`projects/fxtr_workstation_agent_plugin/.../config.py`:

```toml
# Either:
agent_mcp_store = "agent_mcp_store"   # default; lookup at subscribe

# Or:
agent_mcp_store = false               # disable persistence (in-memory log)
```

`WorkstationConfig` gains `agent_mcp_store: str | None`. Default
value used when the toml omits the field entirely: `"agent_mcp_store"`.
Explicit `false` → `None` → disable.

## Bootstrap — as a worker-queued job

The bootstrap runs as a new typed worker job,
`_BootstrapAgentMcpJob`. It's submitted onto the standard worker
queue — never executed inline before the worker starts. Two reasons
for this structure:

- **Seq integrity.** The reader can have queued `_EventJob`s
  (`subsequent_events` from the snapshot, plus anything that lands
  after subscribe) ahead of the bootstrap. Those must be processed
  in seq order so `_last_processed_seq` reflects what we've
  actually seen before our own commit's ack_seq is written. A
  worker-queue ordering makes this automatic — bootstrap's job
  sits behind the subsequent_events; the worker drains them
  first.
- **One commit path.** The bootstrap's `create_callback` mutation
  goes through the same `_apply_script_mutation`-style flow as
  agent scripts: build → send → await commit → `_apply_local_commit`
  → submit `_AckCommitJob` onto the queue. No special-case manual
  seq advance.

```text
_BootstrapAgentMcpJob(session_id, completion: Future[None])
  worker handler:
    1. Look up persistence cap (if configured) via
       workstation_proxy.lookup_short_id_by_external_name + get_service.
    2. Ensure workstation_inbox: send create_callback("workstation_inbox")
       mutation (allow_reserved_slot_keys=True), await commit,
       _apply_local_commit, submit _AckCommitJob.
    3. GetCallbackServiceRequest for the slot → inbox short_id.
    4. Resolve inbox ref via get_service.
    5. Load prior chain head from persistence
       (await persistence_proxy.try_retrieve(session_id)).
    6. await self._inner_tg.start(self._agent_mcp_session_lifecycle, ...)
       — the long-lived subtask opens impl_scope + impl_tg, mounts
       AgentMcpRunningSessionImpl, expose_service the agent_mcp:{session_id}
       external name, parks until cancelled (close_session or cycle end).
       It signals task_status.started() once setup is complete; the
       bootstrap job's start() unblocks.
    7. Stash self._agent_mcp_impl, self._agent_mcp_persistence_ref.
    8. completion.set_result(None).

  on failure:
    - completion.set_exception(exc) so the awaiter sees it.
    - re-raise to tear down the cycle — supervisor routes to PAUSED
      with the formatted reason. Cycle teardown cancels the
      agent_mcp subtask, expose_service unregisters, impl_scope
      drains.
```

### Where the bootstrap runs from

Three entry paths into the bootstrap. The first uses the job-queue
machinery; the others can't (they run *inside* the worker; the
worker can't submit-and-await another job — that deadlocks).

- **Cycle-start (initial_session_id set).** In `_run_inner`, after
  `_do_subscribe` returns, the supervisor submits the
  `_BootstrapAgentMcpJob` onto the queue, then starts the worker.
  The first jobs the worker drains are the `_EventJob`s for
  `subsequent_events` (queued during subscribe), then the
  bootstrap job. The supervisor awaits the bootstrap's completion
  future before transitioning to READY.

- **`_do_open_session`.** After create+subscribe inside the
  open_session worker job, the handler calls
  `_do_bootstrap_agent_mcp_inner` **directly** — the same helper
  the bootstrap job uses, just inline. Submitting a job here
  would deadlock (we'd be awaiting our own queue). Seq ordering
  is still correct because the bootstrap's `_AckCommitJob` lands
  behind any pre-existing `_EventJob`s; when control returns to
  the worker loop after open_session finishes, those events
  drain first and the AckCommitJob advances seq afterwards. The
  outgoing bootstrap-mutation `ack_seq` is the pre-drain
  `_last_processed_seq` (= `snapshot.last_state.seq`), which the
  workstation accepts.

- **`_do_close_session`.** Before sending `CloseOp`, the handler
  cancels the agent_mcp subtask via its saved cancel_scope
  (fire-and-forget). The subtask's async-with chain unwinds at
  its next yield: expose_service drops the workstation
  registration, impl_tg cancels watcher workers, impl_scope
  drains. The cancellation races the close mutation independently
  — the workstation tolerates the agent_mcp registration
  outliving the session by a few event-loop ticks (the close +
  registration drop are independent workstation-side operations).

### Reserved slot_key rejection

Host-side `_apply_script_op` rejects any agent-script queued op
writing to a key in `RESERVED_SLOT_KEYS` (currently just
`workstation_inbox`) with a tool-error string. The bootstrap's
own `create_callback("workstation_inbox")` opts in via
`build_mutate_request(..., allow_reserved_slot_keys=True)`.

## Chain extension — log writer indirection

The chain extension is **decoupled from the worker** via an
in-memory queue of log intents. Reason: the worker's per-event /
per-script handlers stay sync w.r.t. mirror state — they push a
log-intent record onto an `anyio` memory stream and move on. A
separate `_log_writer_loop` task (started by the agent_mcp
lifecycle subtask, alongside the impl mount) drains the stream
and does the async work: walks payloads, stores typed entities,
extends + pins the chain, calls `impl.update_head`.

```text
worker (_do_event_job, _do_script_job):
   ... existing mirror updates ...
   if log_intent_send is not None:
       log_intent_send.send_nowait(LogIntent.from_*(...))

log_writer_loop (started inside _agent_mcp_session_lifecycle,
                 after impl_scope + impl_tg + expose_service):
   async for intent in log_intent_recv:
       if intent is _SHUTDOWN_SENTINEL:
           return  # graceful shutdown — see close_session
       event_ref = await build_*_event(intent, ...)
       new_head = await extend_chain(
           prev_head=impl.chain_head,
           event_ref=event_ref,
           persistence=persistence_ref,
           session_id=session_id,
           update_head=impl.update_head,
       )
```

Properties:

- **Worker stays fast.** Pushing a `LogIntent` is sync, no
  workstation RPC, no persistence cap call.
- **Order preserved.** Single-task drain on the log_writer side
  means intents process in submission order. Within a single
  worker job, intents are pushed in the order events were
  processed.
- **Buffering during bootstrap.** The log_writer task starts as
  part of the agent_mcp lifecycle subtask, *after* the impl is
  mounted. Worker handlers can push intents earlier (e.g. during
  the bootstrap's own commit ack); they sit in the stream's
  buffer until the writer drains.

### Per-cycle stream lifetime

The log intent stream is per-cycle:

- Created at the start of `_agent_mcp_session_lifecycle` (before
  the impl is mounted, so the worker can start pushing as soon
  as the bootstrap commits).
- Drained for the cycle's lifetime by `_log_writer_loop`.
- Closed on cycle teardown (or on `close_session` — see below).

A reconnect / new cycle gets a fresh stream. Buffered-but-not-yet-
flushed entries at cycle teardown are lost; acceptable given the
"pin failures might lose tail" contract and the cycle-level reset
semantic.

### Graceful shutdown on `close_session`

To avoid losing the *script-execution* log entry for the close
mutation itself (and any preceding events), `_do_close_session`
runs the teardown in order:

1. Push a `_LOG_SHUTDOWN_SENTINEL` onto the log intent stream.
2. Wait for the log_writer task to drain the queue (it sees the
   sentinel and returns).
3. Cancel the agent_mcp subtask via its saved cancel_scope; await
   drain (expose_service unregisters, impl_tg cancels, impl_scope
   drains).
4. Send `CloseOp`.

For abnormal close paths (force-close, cycle crash, supervisor
shutdown), the log writer task gets cancelled with the subtask,
buffered-but-unflushed entries are lost. Same contract as pin
failure: agent recovers from the last pinned head on the next
cycle.

## Pin failure handling

If `persistence_proxy.pin(session_id, chain_head)` raises inside
`_log_writer_loop`:

- The log writer task exits with the exception → propagates
  through `_agent_mcp_session_lifecycle`'s task group → the
  bootstrap-time `inner_tg.start(...)` await (if still in flight)
  or the cycle's `inner_tg` (if past READY) surfaces it →
  supervisor pauses with `pause_reason = "agent_mcp_store pin
  failed: <e>"`. Agent recovers via reconnect, which reloads the
  last pinned head from persistence and starts a fresh log
  writer.

Worker-side push (`log_intent_send.send_nowait`) is sync and
won't fail under normal load — the stream is sized for typical
event bursts. Send-failure (overflow) is treated as a structural
error: the supervisor's standard "unhandled error in worker"
path catches it and tears down the cycle.

## Reconnect semantics

Reconnect path (cycle restart) walks the same bootstrap-job flow:

- `workstation_inbox` already in `data_by_key` (from the
  snapshot's `last_state`) → skip the create_callback mutation.
- `inbox_short_id` re-fetched via `GetCallbackServiceRequest`.
- Chain head re-loaded from persistence — captures whatever was
  pinned before the disconnect, drops in-memory-only tail (per
  the pin-failure contract).
- `AgentMcpRunningSessionImpl` mount + `expose_service` re-runs.
  Watchers list starts empty; frontends reconnect + re-register
  their listeners.
- Fresh log intent stream + log_writer_loop.

## CODE_OVERVIEW updates

After each subsection completes (see *Implementation steps*),
update both:

- `projects/fxtr_workstation_agent_plugin/CODE_OVERVIEW.md` — new
  modules in *Layout*, new section *Agent event log + user-message
  channel* under *Mental model*, new builtin in the *Sandbox
  interaction* section's list, toml schema in *Toml*.
- `projects/fxtr_workstation/CODE_OVERVIEW.md` — new
  `GetCallbackServiceRequest` in the *Client protocol* / wire
  protocol section's request list.

## Tests

New file
`projects/fxtr_workstation_agent_plugin/.../tests/test_agent_log_e2e.py`:

1. **Smoke**: open_session, run a script that does
   `export_entity` + `schedule_invoke`, wait for result.
   `agent_mcp.get_state()` returns a chain with two entries
   (ScriptExecution then InvokeResultLogged). Both pinned.
2. **Reconnect**: same as 1, then disconnect + reconnect the MCP.
   `get_state` returns the same chain head (loaded from
   persistence).
3. **watch_state**: register a watcher (mock EventListenerProto
   impl), run a script. Watcher's `notify` fires with the new
   head after every chain extension.
4. **send → callback notification**: register agent_mcp client,
   call `send(payload)`. Verify the MCP's
   `mirror.callback_notifications["workstation_inbox"]` grows
   one entry, and the chain extends with a
   `CallbackNotificationLogged(slot_key="workstation_inbox")`.
5. **present_to_user**: script calls `present_to_user({"hello":
   42, "ref": some_entity_ref, "svc": some_service_ref})`. After
   commit, the chain head's `ScriptExecution.ops` includes a
   `PresentToUserOpRecord` with walked payload — entity ref as
   `EntityRef[UnknownEntity]`, service ref as
   `EntityRef[WorkstationServicePlaceholder]`.
6. **agent_mcp_store missing**: configure toml with a name that
   doesn't resolve. Verify `_ensure_connected` fails with a
   clear `SupervisorNotReady("agent_mcp_store service not
   registered")` error.
7. **agent_mcp_store disabled**: configure `agent_mcp_store = false`.
   Verify chain extensions happen in memory but no pin calls; on
   reconnect the chain is empty.
8. **Workstation_inbox reservation**: script that tries
   `export_entity("workstation_inbox", ...)` fails with a tool
   error before any mutation is sent.
9. **Walker — service-ref substitution**: a callback notification
   payload containing a `ServiceRef` round-trips through the
   walker; the resulting `WorkstationServicePlaceholder` has the
   right `service_type_id` and `service_ctx`.

Plus extending the existing in-memory test in `test_mcp_in_memory.py`
to assert the new boot path doesn't break the existing flows
(workstation_inbox creation should be transparent — but it adds
events to the chain, which other tests should accept).

## Implementation chunks (for pair-programming review)

The plan is broken into six review chunks. Each chunk is
independently verifiable; we pause between for review.

- **Chunk A — Infra prerequisites.** Steps 1–2.  ✅ **done**
- **Chunk B — Log machinery in isolation.** Steps 3–5.  ✅ **done**
- **Chunk C — Agent_mcp service + watcher workers.** Step 6.  ✅ **done**
- **Chunk D — Supervisor bootstrap.** Step 7.  ✅ **done**
- **Chunk E — Wire up event logging + present_to_user.** Steps 8–9.  ✅ **done**
- **Chunk F — Reconnect + e2e tests + doc finalize.** Steps 10–12.  ✅ **done**

## Implementation steps

1. ✅ **Workstation: `GetCallbackServiceRequest`.**
   - Add request / response types in
     `projects/fxtr_workstation/fxtr_workstation/client_protocol.py`.
   - Handler in
     `projects/fxtr_workstation/fxtr_workstation/client_app.py`.
   - Test in `projects/fxtr_workstation/tests/`. Covers ok / unknown-
     slot / unknown-subscription / non-callback-slot cases.
   - Update `projects/fxtr_workstation/CODE_OVERVIEW.md` to list the
     new request / response.

2. ✅ **MCP toml: `agent_mcp_store` config field.**
   - Extend `WorkstationConfig` (default name `"agent_mcp_store"`,
     explicit `false` disables).
   - Round-trip test in the existing config tests.
   - Update agent plugin CODE_OVERVIEW *Toml*.

3. ✅ **Substitution walker.**
   - New `fxtr_workstation_mcp/log_walker.py` with the async
     walker. Mirrors `_provenance.py`'s shape (mapping/sequence
     fan-out via `anyio.create_task_group`, per-walk short_id
     cache).
   - Unit-test against synthetic payloads (mock workstation_proxy
     for `get_entity` / `get_service`).

4. ✅ **Typed log entities.**
   - New `fxtr_workstation_mcp/agent_log_types.py`:
     `WS_AGENT_NS`, `WorkstationServicePlaceholder`,
     `OwnedCallbackPlaceholder`, `ScriptPreamble`,
     `WsAgentLogEventChain`, `ScriptExecutionLogEvent`,
     `InvokeResultLogEvent`, `InvokeErrorLogEvent`,
     `CallbackNotificationLogEvent`, `ScriptOpRecord` subclasses.
   - Round-trip tests via mashumaro (each entity stored + loaded;
     `ScriptOpRecord` tagged-union encoding works).
   - Update agent plugin CODE_OVERVIEW *Layout* + new section.

5. ✅ **Log builder + chain extender.**
   - New `fxtr_workstation_mcp/log_builder.py` with
     `build_script_execution` / `build_invoke_result` /
     `build_invoke_error` / `build_callback_notification` /
     `extend_chain`.
   - Unit tests against synthetic input + mock persistence cap.

6. ✅ **`AgentMcpRunningSessionProto` + impl.**
   - `fxtr_workstation_mcp/agent_mcp_proto.py` (protocol +
     `AGENT_MCP_RUNNING_SESSION_TYPE_ID`).
   - `fxtr_workstation_mcp/agent_mcp_impl.py` (impl with
     `get_state` / `watch_state` / `send`; per-watcher worker
     task model; `update_head` internal API for the log builder).
   - Unit tests against a synthetic mount in isolation (no real
     workstation). Cover the watcher skip-ahead + ordering
     property explicitly.
   - Update CODE_OVERVIEW.

7. ✅ **Workstation_inbox bootstrap + service mount.**
   - Add `_BootstrapAgentMcpJob(session_id, completion: Future[None])`
     to `ws_connection.py`'s job union.
   - Worker handler `_do_bootstrap_agent_mcp_job` delegates to
     `_do_bootstrap_agent_mcp_inner(session_id)` which runs the
     bootstrap steps (persistence lookup → workstation_inbox
     mutation via standard `_apply_local_commit` + `_AckCommitJob`
     path → inbox short_id → inbox ref → load prior head →
     `inner_tg.start(self._agent_mcp_session_lifecycle, ...)`).
     Sets `_agent_mcp_impl` / `_agent_mcp_persistence_ref` on
     success; surfaces failure both via the future and by
     re-raising (so the supervisor's cycle teardown fires).
   - Entry paths:
     - Cycle-start (initial_session_id): supervisor submits the
       `_BootstrapAgentMcpJob` + awaits completion before READY,
       in `_run_inner` between subscribe and worker start.
     - `_do_open_session`: after create+subscribe, calls
       `_do_bootstrap_agent_mcp_inner` **directly** (submitting a
       job here would deadlock — we're already inside the worker).
     - `_do_close_session`: cancels the agent_mcp subtask via its
       saved cancel_scope (fire-and-forget). The cleanup unwind
       races independently with the close mutation; workstation
       tolerates the brief overlap.
   - `_agent_mcp_session_lifecycle` subtask owns: per-session
     `impl_scope`, per-session `impl_tg` (for watcher workers),
     `expose_service` registration. *Chunk E* will extend this
     to also own the log intent stream + `_log_writer_loop`.
   - Reserved-slot_key rejection in `_apply_script_op`
     (`allow_reserved_slot_keys=True` only set by the
     bootstrap's own `create_callback("workstation_inbox")`
     mutation).
   - `_AckCommitJob.silent: bool = False` flag — bootstrap
     mutations set `silent=True` so the `self_commit` channel
     notification is suppressed (it's MCP-internal, not an
     agent action).
   - `force_close_session_one_shot` updated to use
     `snapshot.last_state.seq` as `ack_seq` instead of `0`
     (workstation's stale_ack check rejects ack_seq=0 once
     bootstrap has advanced the session).
   - Integration tests in `test_agent_mcp_bootstrap.py`:
     cycle-start, open_session-driven, close_session teardown,
     persistence cap held, persistence cap missing, reserved-slot
     rejection, registration dropped on shutdown (7 tests total).
   - Bulk-edited 30 toml-write sites across 12 existing test
     files to add `agent_mcp_store = false` (per-test opt-out so
     tests don't need a real persistence cap).

8. ✅ **Chain extension in event/script handlers + log writer.**
   - Per-cycle `MemoryObjectStream[_LogIntent]` (`max_buffer_size=256`)
     created inside `_agent_mcp_session_lifecycle` before
     `expose_service`, after `impl_scope` / `impl_tg` are open.
   - Worker handlers wired:
     - `_do_event_job` (`_handle_arrival_event`) looks up
       `slot_key` from `mirror.pending_slot_index` **before**
       `_apply_state_effect` (the invoke branch drops the index
       entry on promote), then calls
       `_push_event_log_intent(ev, slot_key)` after the seq
       advances. Per-kind branches dispatch `InvokeResultEvent`
       / `InvokeErrorEvent` / `CallbackEvent`; others are dropped
       silently.
     - `_do_script_job` (script-error path) and
       `_apply_script_mutation` (every return path:
       ForgetMissingKey, SlotKeyAlreadyExists, SlotKeyReserved,
       no-op, MutationRejected, ErrorResponse, successful commit)
       all push `_LogIntent_ScriptExecution`.
     - `BuildResult` gained `substituted_runner_queued_ops` (the
       wire-final form, post-placeholder substitution) and
       `slot_allocations`; the script-commit log push reads from
       those rather than re-computing.
   - `_log_writer_loop` task started on `impl_tg` (spawned
     **before** `expose_service` so worker pushes during the
     bootstrap commit's AckCommit don't get dropped). Drains
     intents → `build_*_event` → `extend_chain` →
     `impl.update_head`. Exits on `_LogIntent_Shutdown` /
     stream-closed / cancellation. Sets `_log_writer_done` event
     on exit so `_do_close_session` can synchronize.
   - **`impl_scope` includes GLOBAL_SCOPE.** The log builder's
     mashumaro encode of typed entities (`ScriptPreamble`,
     `WsAgentLogEventChain`, the per-kind event entities)
     chases type-descriptor entities that live in
     `GLOBAL_SCOPE`; same reason `script_scope` does the same.
   - **Sub-key projection for export entities.** `entity_short_ids`
     is keyed by host-side *handle* (e.g. `slot:foo`); the log
     builder wants a slot_key-keyed subset covering only
     `export_entity` ops. The push site re-projects with
     `{op["slot_key"]: entity_short_ids[slot_handle(op["slot_key"])]
       for op in runner_queued_ops if op["kind"] == KIND_EXPORT_ENTITY}`.
   - `_do_close_session` drains the writer before cancelling the
     subtask: push `_LogIntent_Shutdown`, await
     `_log_writer_done`, then `_agent_mcp_cancel_scope.cancel()`,
     then send `CloseOp`. Handles
     `anyio.BrokenResourceError` on the push (writer raced us
     on cycle teardown) so the wait doesn't deadlock.

9. ✅ **`present_to_user` sandbox builtin.**
   - `PresentToUserOp(payload, entity_arg_handles)` added to
     `fxtr_ws_mcp_sandbox_bridge/protocols.py`; appended to the
     `QueuedOp` union.
   - `present_to_user(payload)` builtin in
     `fxtr_ws_mcp_sandbox_bridge/builtins.py` re-uses
     `_walk_for_wire` (with a `_maybe_register_entity` closure
     identical in shape to `schedule_invoke`'s).
   - Host-side: `KIND_PRESENT_TO_USER` added to
     `session_state.py`; `mutation_builder._apply_script_op`
     returns early on this kind (no wire op, no JSON patch);
     `_substitute_op_for_log` walks the payload through
     `substitute_placeholders`;
     `_register_exported_entities` registers each
     `entity_arg_handles` entry the same way as
     `schedule_invoke`.
   - Update CODE_OVERVIEW *Sandbox interaction*.

10. ✅ **Reconnect + persistence load.**
    - The bootstrap already resolved a one-shot ``persistence_proxy``
      against ``rpc.host_scope`` and called ``try_retrieve`` to get
      the prior chain head (chunk D, step 5 of
      ``_do_bootstrap_agent_mcp_inner``); this work continues to
      cover the reconnect case unchanged.
    - **Bug fixed**: `_log_writer_loop` previously passed
      `persistence_ref` (a ServiceRef) to `extend_chain`, which
      expects a resolved `PersistenceProto`. Fixed by resolving the
      persistence cap *inside* `impl_scope` (so chain-head FxtrIDs
      are encodable through the proxy on every pin) and threading
      the resolved proxy into the writer.
    - Tests in `test_event_log_chain.py`:
      `test_reconnect_loads_chain_head_from_persistence` — cycle 1
      runs a script, pins; cycle 2 (fresh app_ctx) reads the pinned
      head from the cap and presents it as the new impl's initial
      head. Also
      `test_disabled_persistence_chain_does_not_survive_reconnect`
      (memory-only mode is gone on reconnect) and
      `test_chain_pinned_after_each_extension` (each chain
      extension writes the latest head to the cap; verified by an
      independent /rpc peer reading the cap directly).

11. ✅ **End-to-end tests** — chunk E covered scenarios 1, 3, 4, 5;
    chunk F adds scenarios 2 (reconnect) + 7 (disabled persistence).
    Scenario 6 (agent_mcp_store missing) is covered by
    `test_agent_mcp_bootstrap.test_bootstrap_fails_when_persistence_cap_missing`;
    scenario 8 (workstation_inbox reservation) is covered by
    `test_agent_mcp_bootstrap.test_reserved_slot_key_rejected_in_script`;
    scenario 9 (service-ref substitution) is covered at the unit
    level by `test_log_builder.test_script_execution_success_with_diverse_ops`.

12. ✅ **Update CODE_OVERVIEW.md** — final pass.

## Sharp edges / open questions

1. **Wire-form vs typed-form for ScriptExecution.ops.** The host
   captures the script's queued ops *after* the mutation builder
   has substituted host-internal placeholders for workstation
   short_ids. The walker runs on that post-substitution form. If
   substitution failed (mutation rejected), we still want the log
   entry for the rejected script — in that case the ops carry
   pre-substitution placeholders. Cleaner: always run the walker
   against the post-substitution form; on rejection, walk the
   *pre*-substitution form with stub placeholders. Details TBD at
   impl time.

2. **AgentMcpImpl scope ownership.** The impl lives one-per-cycle-per-
   session. Cleanest is a per-cycle, per-session subtask on the
   inner TG. Setup-on-subscribe / teardown-on-close-or-reconnect
   are non-trivial in the supervisor body; sketch above will get
   refined at implementation.

3. **`WsAgentLogEvent` union typing.** `EntityRef[ScriptExecution |
   InvokeResultLogged | ...]` needs mashumaro's union support via
   the inner entity's `$fxtr.type` discriminator. Spot-checked
   that v2's `EntityRef` + `decode_from_service_data` support this;
   confirm at implementation time.

4. **`OwnedCallbackPlaceholder.slot_key` reverse-lookup.**
   `mirror.pending_slot_index` is the primary source. Closed slots
   are still in `data_by_key` (until forgotten) — fall back to
   that. Closed *and* forgotten — give up, store `None`.

5. **Watcher backpressure.** A slow watcher blocks the chain
   extension's completion. Acceptable for v1 (frontend should
   process callbacks quickly); revisit if it becomes a problem.
   Watchers that never return (hang) will pause the supervisor;
   user can force-close the session to recover.

6. **Multiple MCP instances on the same session.** The
   `agent_mcp:{session_id}` external_name conflicts if two MCPs
   try to register. By design — only one MCP per session. The
   conflict surfaces as a `register_service` rejection during
   `_bootstrap_agent_mcp` and pauses the second MCP with a clear
   error.

7. **Pin granularity.** Pinning after every chain extension is
   simple; for high-volume callback streams it could be slow. For
   v1, accept the latency. Future option: batch with bounded
   delay.

8. **`SystemSource` vs RPC-peer registration for `agent_mcp:*`.**
   The MCP is a regular `/rpc` peer, so it uses the standard
   `register_service` path; the workstation tags the registration
   with `kind=rpc_connection`. No need for a `SystemSource`
   here.

9. **Last step:** Update CODE_OVERVIEW.md.

---

## Current state (as of chunk F completion)

### What's done

- **Chunk A** — Workstation `GetCallbackServiceRequest` + handler
  + tests; MCP toml `agent_mcp_store` field (default
  `"agent_mcp_store"`, `false` disables).
- **Chunk B** — `agent_log_types.py` (entities + namespace),
  `log_walker.py` (substitution walker, explicit-scope per scope
  discipline), `log_builder.py` (`build_*_event` + `extend_chain`,
  also explicit-scope).
- **Chunk C** — `agent_mcp_proto.py` (`AgentMcpRunningSessionProto`),
  `agent_mcp_impl.py` (`AgentMcpRunningSessionImpl` + per-watcher
  worker model with skip-ahead semantics + per-watcher sub-scope
  for bridge isolation).
- **Chunk D** — `_BootstrapAgentMcpJob` + `_do_bootstrap_agent_mcp_inner`
  helper, `_agent_mcp_session_lifecycle` long-lived subtask,
  open_session/close_session integration, reserved-slot
  rejection, `force_close` fix, `_AckCommitJob.silent` flag,
  test bulk-edits. Full integration tests in
  `test_agent_mcp_bootstrap.py`.
- **Chunk E** — `_LogIntent` union (script execution, invoke
  result, invoke error, callback notification, shutdown sentinel)
  + per-cycle `MemoryObjectStream`. `_log_writer_loop` task
  spawned inside `_agent_mcp_session_lifecycle` before
  `expose_service`. Worker handlers push intents from every
  return path of `_do_script_job` / `_apply_script_mutation` and
  per-kind branches of `_do_event_job` (the `slot_key` lookup
  happens *before* `_apply_state_effect` so it survives the
  invoke-branch's promote-and-drop). `_do_close_session` drains
  via sentinel before cancelling the subtask. `present_to_user`
  sandbox builtin + `PresentToUserOp` queued-op kind + host-side
  routing (no wire op, but logged on
  `ScriptExecutionLogEvent.ops`). `BuildResult` carries
  `substituted_runner_queued_ops` so the writer sees wire-final
  shapes without re-computing. Tests in
  `test_event_log_chain.py` cover script-execution chain
  extension, schedule_invoke + result, present_to_user op
  record, close-session drain, and `agent_mcp.send` → callback
  notification log.
- **Chunk F** — Bug fix: `_log_writer_loop` resolves the
  persistence cap as a proxy local to `impl_scope` (not the
  raw `ServiceRef` previously passed through), so per-extension
  pins succeed. Reconnect tests verify the bootstrap loads the
  prior chain head from the cap (cycle 1 pins; cycle 2 reads).
  Disabled-persistence test verifies the chain extends in
  memory but is gone on reconnect. Pin-each-extension test
  reads the cap via a separate /rpc peer to confirm the
  workstation-side value matches the latest chain head.
  CODE_OVERVIEW.md updated with a new "Agent event log +
  user-message channel" section.

### Design changes that happened during implementation

These differ from the original plan; reflected in the relevant
sections above but worth knowing:

- **Naming.** Event entities are `XLogEvent` (not the original
  `XEvent` shape) to avoid shadowing the workstation's wire-type
  names — `ScriptExecutionLogEvent`, `InvokeResultLogEvent`,
  `InvokeErrorLogEvent`, `CallbackNotificationLogEvent`. The
  chain is `WsAgentLogEventChain` (cascade rename).
- **Walker scope discipline.** Walker takes `scope` explicitly,
  no implicit active-scope use. Same for the log builder + chain
  extender.
- **`OwnedCallbackPlaceholder.slot_key: str`** with `"<unknown>"`
  sentinel (not `str | None`) — simpler API.
- **AgentMcp impl runs in the MCP process.** Mounted on a
  per-cycle `impl_scope`; the `ServiceRef` is bridged to the
  workstation via `register_service`. (Initial plan was unclear;
  resolved during chunk C.)
- **Watcher worker model: per-watcher sub-scope.** Each
  `watch_state` worker opens its own `watcher_scope` (child of
  `impl_scope`), bridges the listener into it, signals
  `task_status.started()`. When the watcher exits, the scope
  drains and the bridge releases — no accumulation of stale
  bridges on `impl_scope` for the cycle's lifetime.
- **Bootstrap as a worker-queued job (cycle-start) + inline
  call (open_session).** Two entry shapes because open_session
  runs inside the worker and can't submit-and-await another job.
  Same helper underneath. close_session cancels the subtask
  fire-and-forget; cleanup races the close mutation
  independently.
- **`_AckCommitJob.silent` flag.** Bootstrap mutations set
  `silent=True` so the user-facing `self_commit` channel
  notification isn't fired for MCP-internal commits.
- **`force_close_session_one_shot`** uses
  `snapshot.last_state.seq` as `ack_seq` (not `0`). Workstation
  rejects ack_seq=0 once bootstrap has advanced the session.

### Things to remember / non-obvious gotchas

- **Tests must be `async def`.** The package's autouse
  `_per_test_timeout` fixture in `conftest.py` is async; sync
  test functions fail under pytest 9. Mark test modules with
  `pytestmark = pytest.mark.anyio`.
- **`-n auto` for parallel test runs.** A few existing tests
  flake under parallel load (notably
  `test_wait_seconds_only_waits_for_this_scripts_invokes`);
  these pass alone. Re-run on flake.
- **`EntityRef[T]` wire-strips to `FxtrID`.** `notify`'s
  `payload: FxtrServiceData` doesn't accept `EntityRef` — the
  framework wire-encodes it as a bare `FxtrID`. Frontends
  reconstruct `EntityRef[WsAgentLogEventChain]` from the
  received `FxtrID`. Tests assert against `FxtrID` equality on
  the receiving side.
- **`bridge_service_from_scope` ordering.** Bridging from
  scope A into scope B pins A's drain on B. If A and B are in
  the same `async with (A, B):` block, B must be the *inner*
  scope (entered last → exited first) so it drains, releases
  the bridge, then A can drain.
- **Per-test toml opt-out.** Existing tests have
  `agent_mcp_store = false` in their toml to disable the
  bootstrap's persistence-cap lookup. New tests that exercise
  agent_mcp explicitly should set `agent_mcp_store =
  "agent_mcp_store"` (and configure a `PersistenceNamespaceConfig`
  on the workstation).
- **The bootstrap creates `workstation_inbox` once per session.**
  Tests that assert `data_by_key` equality after a tool call must
  filter out `workstation_inbox` (4 existing tests already do
  this — search for `.pop("workstation_inbox", None)`).
- **Worker queue serializes everything.** A worker job can't
  submit-and-await another job (deadlock). If you need
  "inline-like" behavior, factor a helper both the worker
  handler and the inline caller can use (see
  `_do_bootstrap_agent_mcp_inner`).
- **Chunk E's log writer needs to start *before* the impl
  mount inside the lifecycle subtask.** The bootstrap commit
  fires an `_AckCommitJob` which (per chunk E) should push a
  log intent — but the impl isn't mounted yet. Resolution: the
  log intent stream is created first thing in
  `_agent_mcp_session_lifecycle`, before the impl mount; the
  writer task spawns alongside the mount. Intents pushed
  during bootstrap-commit's AckCommit sit in the buffer until
  the writer starts and drains them. (Implementation detail
  for chunk E.)
- **Open `_do_close_session` graceful-drain ordering**
  (chunk E): push `_LOG_SHUTDOWN_SENTINEL` → wait for the
  log writer to drain → *then* cancel the agent_mcp subtask
  → *then* send `CloseOp`. Without the sentinel + drain, the
  close-mutation's `ScriptExecutionLogEvent` would be lost.
- **`impl_scope` must include `GLOBAL_SCOPE`.** The log
  builder stores typed entities (`ScriptPreamble`,
  `WsAgentLogEventChain`, the per-kind event entities) into
  `impl_scope`; mashumaro's encode chases type-descriptor
  entities that live in `GLOBAL_SCOPE`. Without it, the very
  first chain extension fails with `Node … was not found in
  any parent scope!`. (Same fix pattern as `script_scope`
  uses.)
- **`export_entity_short_ids` is slot_key-keyed for the log
  builder.** The connection's `entity_short_ids` is keyed by
  host-side *handle* (`slot:foo` for export_entity,
  `invoke_arg:…` for invoke args). The log builder wants a
  slot_key-keyed dict covering only `export_entity` ops;
  the push site re-projects:
  `{op["slot_key"]: entity_short_ids[slot_handle(op["slot_key"])]
    for op in runner_queued_ops if op["kind"] == KIND_EXPORT_ENTITY}`.
- **MCP-side-only ops still produce log entries via the
  "no-op script" path.** A script that only calls
  `present_to_user` has `build_mutate_request → None` (no
  wire activity), but its op list is non-empty. The no-op
  path now runs `mutation_builder.substitute_op_for_log`
  over `runner_queued_ops` (with empty `slot_allocations`)
  so the present_to_user op shows up on the chain's
  `ScriptExecutionLogEvent.ops`.
- **`_walk_for_wire` error messages still mention
  `schedule_invoke`.** The wire walker in
  `fxtr_ws_mcp_sandbox_bridge/builtins.py` was originally
  written for `schedule_invoke`; `present_to_user` reuses
  it. The error strings ("schedule_invoke: cannot encode
  arg of type …") are slightly misleading for present_to_user
  callers. Not a correctness issue; cosmetic cleanup
  candidate.
- **Resolve the persistence proxy in `impl_scope`**
  (chunk F): the bootstrap's one-shot `try_retrieve` resolves
  the persistence cap against `rpc.host_scope` (fine for a
  single FxtrID return that chain-bridges back). But each
  chain extension calls `pin(session_id, chain_head_id)` where
  `chain_head_id` is stored in `impl_scope` — the proxy must
  dispatch *from* `impl_scope` so the FxtrID is wire-encodable.
  `_agent_mcp_session_lifecycle` resolves a *second* proxy
  in `impl_scope` and threads it into `_log_writer_loop`.
  Don't try to share the bootstrap's host_scope-resolved proxy
  here.

### Files modified during chunks A–F

**Workstation (`projects/fxtr_workstation/`):**
- `fxtr_workstation/client_protocol.py` — `GetCallbackServiceRequest` + `GetCallbackServiceResponse`.
- `fxtr_workstation/client_app.py` — `_handle_get_callback_service`.
- `tests/test_get_callback_service_10.py` — 4 unit tests.
- `CODE_OVERVIEW.md` — new request/response described.

**Agent plugin (`projects/fxtr_workstation_agent_plugin/fxtr_workstation_mcp/`):**

New modules:
- `fxtr_workstation_mcp/agent_log_types.py`
- `fxtr_workstation_mcp/log_walker.py`
- `fxtr_workstation_mcp/log_builder.py`
- `fxtr_workstation_mcp/agent_mcp_proto.py`
- `fxtr_workstation_mcp/agent_mcp_impl.py`

Modified modules:
- `fxtr_workstation_mcp/config.py` — `agent_mcp_store` field.
- `fxtr_workstation_mcp/session_state.py` — `RESERVED_SLOT_KEYS`,
  `SlotKeyReserved`, `KIND_PRESENT_TO_USER`.
- `fxtr_workstation_mcp/mutation_builder.py` — `allow_reserved_slot_keys`
  param; `BuildResult.substituted_runner_queued_ops` +
  `slot_allocations`; public `substitute_placeholders` /
  `substitute_op_for_log`; `KIND_PRESENT_TO_USER` handling in
  `_apply_script_op` (no wire op).
- `fxtr_workstation_mcp/ws_connection.py` — bootstrap helpers,
  `_BootstrapAgentMcpJob`, `_agent_mcp_session_lifecycle`
  (incl. `impl_scope` w/ `GLOBAL_SCOPE`, persistence-proxy
  resolution, log-intent stream creation, writer task spawn),
  `_log_writer_loop` + `_build_log_event` dispatcher,
  `_LogIntent_*` records, `_push_event_log_intent` /
  `_push_script_execution_log_intent` helpers, log-intent
  pushes from every return path of `_apply_script_mutation` +
  `_do_script_job` + per-kind branches of `_do_event_job`,
  graceful-drain ordering in `_do_close_session`,
  `_AckCommitJob.silent` flag, `KIND_PRESENT_TO_USER` handling
  in `_register_exported_entities`, etc.
- `fxtr_workstation_mcp/server.py` — threads `agent_mcp_store`
  config to the connection.

**Sandbox bridge (`projects/fxtr_workstation_agent_plugin/sandbox/fxtr_ws_mcp_sandbox_bridge/`):**
- `fxtr_ws_mcp_sandbox_bridge/protocols.py` — `PresentToUserOp`
  dataclass + extension of the `QueuedOp` union.
- `fxtr_ws_mcp_sandbox_bridge/builtins.py` — `present_to_user`
  builtin (re-uses `_walk_for_wire`); export added to the
  closure mapping.

New tests:
- `tests/test_agent_log_types.py` (21 round-trip tests).
- `tests/test_log_walker.py` (16 substitution tests).
- `tests/test_log_builder.py` (11 builder + extender tests).
- `tests/test_agent_mcp_impl.py` (13 impl + watcher tests).
- `tests/test_agent_mcp_bootstrap.py` (7 integration tests).
- `tests/test_event_log_chain.py` (8 chunk E/F integration
  tests — script execution, invoke result, present_to_user op
  record, close-session drain, agent_mcp.send → callback log,
  reconnect persistence load, disabled-persistence behavior,
  pin-each-extension).
- `tests/test_config.py` (5 new field tests).

Bulk-edits (30 sites across 12 files):
- Adds `agent_mcp_store = false` to existing test tomls.

Test totals at end of chunk F:
- MCP plugin: 204 tests passing (one pre-existing flake under
  parallel load — `test_wait_seconds_only_waits_for_this_scripts_invokes`;
  passes alone).
- Workstation: 341 tests passing (unchanged from chunk A).
