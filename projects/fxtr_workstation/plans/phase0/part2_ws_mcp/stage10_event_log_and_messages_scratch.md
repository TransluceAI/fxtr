# Scratch — stage10_event_log_and_messages

Working notes / decisions captured before writing the plan.

## Three-part feature, one stage

1. **Typed persistent log.** Linked list of `WsAgentEventChain`
   (`prev: EntityRef[WsAgentEventChain] | None`, `event:
   EntityRef[WsAgentEvent]`). `WsAgentEvent` is a union of typed
   `DataclassEntity` subtypes. Persisted to a workstation persistence
   cap (`PersistenceProto`) named `agent_mcp_store` (configurable),
   keyed by `session_id`; head is repinned every time the chain grows.

2. **`agent_mcp:{session-id}` service** registered by the MCP over
   `/rpc`. Methods: `get_state` / `watch_state` / `send`.

3. **`present_to_user(...)` sandbox builtin.** Queued op like
   `schedule_invoke`, folded into the script's `ScriptExecution` log
   entry at commit time.

## Substitution walker — host-side

Walks a wire-form `FxtrServiceData` payload, substituting:

- `["entity", short_id]` → resolve via `workstation_proxy.get_entity`,
  build `EntityRef[UnknownEntity](fid)`.
- `["service", short_id]` → resolve as untyped proxy, fetch
  `$fxtr.type` + `$fxtr.ctx` in parallel, build
  `WorkstationServicePlaceholder(short_id, service_type_id,
  service_ctx)` (DataclassEntity), store, emit `EntityRef` to it.
  Cache by short_id within one walk so the same service appearing
  multiple times only round-trips once.
- `["callback", N]` → build `OwnedCallbackPlaceholder(slot_n=N,
  slot_key=<if known via reverse-lookup>)`, store, emit `EntityRef`.
- `["bytes", b64]` → decoded `bytes` (passthrough leaf).
- `[[inner]]` literal-list-escape → recurse into list.
- `dict` → recurse over values.
- scalars / `None` / `bool` / `int` / `float` / `str` → passthrough.

Output tree uses `FrozenFxtrCbor` shape (frozendict / tuple / scalars
/ `RawWeakFxtrID` / `RawStrongFxtrID` leaves). Stored as the
`payload: FrozenFxtrCbor` field of the log entity.

Mirrors the sandbox-provenance walker
(`fxtr_sandbox_rpc_adapter/_provenance.py`) in shape — fan out over
mapping/sequence children via anyio task groups; introspect the two
service framework methods in parallel.

## Entity types (host-only, in `fxtr_workstation_mcp/`)

New module: `fxtr_workstation_mcp/agent_log_types.py`.

```text
WS_AGENT_NS = uuid.UUID(...)   # stable per-module

WorkstationServicePlaceholder(DataclassEntity)
  short_id: str
  service_type_id: FxtrID
  service_ctx: FxtrID

OwnedCallbackPlaceholder(DataclassEntity)
  slot_n: int
  slot_key: str | None   # reverse-looked-up at walk time if known

WsAgentEventChain(DataclassEntity)
  prev: EntityRef[WsAgentEventChain] | None
  event: EntityRef[ScriptExecution | InvokeResultLogged | InvokeErrorLogged
                  | CallbackNotificationLogged]
  timestamp: float   # unix
```

### Event types

```text
ScriptOpRecord(DataclassFxtrSubpart)
  # tagged union via mashumaro discriminator on `kind`
  kind: str   # "export_entity"|"export_service"|"forget"|
              # "schedule_invoke"|"create_callback"|
              # "forget_callback_notification"|"present_to_user"
  slot_key: str | None
  # ... additional per-kind fields (see plan)

ScriptExecution(DataclassEntity)
  script_src: str
  preamble_src: str
  stdout: str
  stderr: str
  error: str | None       # script-raised traceback, or None on success
  rejected: str | None    # workstation rejection reason, or None on commit
  ops: tuple[ScriptOpRecord, ...]
  committed_seq: int | None

InvokeResultLogged(DataclassEntity)
  slot_key: str
  slot_n: int
  seq: int
  payload: FrozenFxtrCbor   # walked through substitution

InvokeErrorLogged(DataclassEntity)
  slot_key: str
  slot_n: int
  seq: int
  message: str

CallbackNotificationLogged(DataclassEntity)
  slot_key: str          # includes "workstation_inbox" for user messages
  slot_n: int
  seq: int
  payload: FrozenFxtrCbor
```

User messages from `agent_mcp.send` flow through the
`workstation_inbox` callback, so they land as
`CallbackNotificationLogged(slot_key="workstation_inbox", ...)`. No
separate `MessageReceived` type — the slot_key is the discriminator
the frontend uses.

## `agent_mcp:{session_id}` service

New module: `fxtr_workstation_mcp/agent_mcp_proto.py` (protocol +
type id constants) and `fxtr_workstation_mcp/agent_mcp_impl.py`
(impl + scope management).

```python
@service_protocol
class AgentMcpProto(Protocol):
    async def get_state(self) -> EntityRef[WsAgentEventChain] | None: ...
    async def watch_state(
        self, listener: ServiceRef[EventListenerProto]
    ) -> None: ...
    async def send(self, payload: FxtrServiceData) -> None: ...
```

### `send` implementation

Looks up the `workstation_inbox` callback's service via its
short_id (stashed at session-setup time) and calls
`notify(payload)`. That fires a workstation `CallbackEvent` →
arrives back at the MCP's `/client` subscription → standard
callback processing path appends a
`CallbackNotificationLogged(slot_key="workstation_inbox", ...)` to
the chain just like any other callback.

### `watch_state` implementation

Stashes the listener `ServiceRef` in a per-impl list (held in a
session-scoped FxtrScope so refs stay alive). On every chain
extension, the host iterates listeners and invokes `notify(new_head)`.
A listener returning `False` (or raising) is dropped.

The session-scoped FxtrScope:
- Lives in the supervisor's per-session sub-scope.
- Drains on session close / supervisor reconnect.
- On reconnect, watchers are lost (the frontend reconnects + resubscribes).

## New `/client` request

`GetCallbackServiceRequest(request_id, subscription_id, slot)` →
`GetCallbackServiceResponse(request_id, subscription_id, slot,
service_short_id)` on success / `ErrorResponse` on failure.

Workstation-side: validates `subscription_id` is owned by the
calling connection, looks up `session.open_callbacks[slot]`, returns
`cb.service_short_id`. Error if slot is missing or refers to a
non-callback.

Alternative considered: subscribe to `SubscribeSessionDebugRequest`
(which already includes `open_callbacks: dict[int, str]`) — rejected
because it's a chatty stream and the MCP only needs the short_id
once per slot creation.

## Workstation_inbox setup flow

**On `_do_subscribe`** (after subscribe completes, before
transitioning to READY):

1. Inspect `mirror.data_by_key.get("workstation_inbox")`.
2. If absent: send a one-op mutation `[CreateCallbackOp(slot=...)]`
   that writes `["callback_open", N]` to `data_by_key["workstation_inbox"]`.
   The mutation builder reuses its existing slot allocator. Wait for
   commit (uses the standard self-commit flow).
3. After commit (or skipping step 2 if already present): the slot
   number N is known. Send `GetCallbackServiceRequest(slot=N)`. Stash
   `inbox_service_short_id`.
4. Load prior chain head: `agent_mcp_store.try_retrieve(session_id)`
   over `/rpc`. Stash result.
5. Mount `AgentMcpImpl(inbox_short_id, chain_head, ...)` on a new
   session_scope. `expose_service` it as `agent_mcp:{session_id}`.

All of this happens during the supervisor cycle, *before* READY,
so any failure pauses the supervisor with a meaningful
`pause_reason`.

## Persistence cap discovery + lifetime

- On `_do_subscribe`: `workstation_proxy.lookup_short_id_by_external_name(<configured_name>)`
  → service short_id. `get_service(short_id)` →
  `ServiceRef[PersistenceProto]`. Store in cycle-scoped state.
- If config says `agent_mcp_store = null`: skip persistence (in-memory
  log only). Don't error.
- If config defaults to `"agent_mcp_store"` and lookup fails: fail loud,
  pause supervisor with `"agent_mcp_store service not registered on
  workstation"`.

The configurable name lives in `.fxtr_workstation.toml`:

```toml
agent_mcp_store = "agent_mcp_store"   # default
# or:
agent_mcp_store = false               # disable persistence
```

## Chain extension — where it happens

**Per script execution** (in `_do_script_job`):
- After `_apply_local_commit`, async-build the `ScriptExecution`
  entity:
  - Walk every payload-bearing op (`schedule_invoke.args/kwargs`,
    `present_to_user.payload`) through the substitution walker.
  - For other ops, capture lightweight metadata (slot_key, op kind).
- Store the entity in a transient build_scope, then copy to a
  persistent "log_scope" owned per-session.
- Extend the chain: new `WsAgentEventChain(prev=head, event=...)`.
- Pin to agent_mcp_store under session_id.
- Update in-memory `chain_head` on `AgentMcpImpl`.
- Notify watchers.
- Finally, return script outcome to the tool.

**Per inbound event** (in `_do_event_job`, for
`InvokeResultEvent` / `InvokeErrorEvent` / `CallbackEvent`):
- After `_apply_state_effect` does its current work, async-build the
  log entity (substitution walker on the payload, scalar fields for
  errors).
- Same extend → pin → notify → done flow.

Pinning happens in the same async function — serialized by the
worker queue.

**Workstation_inbox bootstrap mutation** does *not* create a log
entry; it's MCP-side housekeeping (not agent-driven).

## Pin failures

If `agent_mcp_store.pin(...)` fails:
- The worker job raises.
- For a `_ScriptJob`: tool-error path (job.completion gets the
  exception).
- For an `_EventJob`: the supervisor pauses, agent recovers via
  `close_session(force=True)` if needed.

Acceptable: the chain is in-memory consistent (we've already
extended `chain_head`) but not durably pinned. On reconnect, the
last pinned head is loaded, recent in-memory extensions are lost.
This is the user's accepted contract (pin on every extension; if
it fails, recovery loses tail).

Alternative considered: hold extension in a "pending" state until
pin succeeds. Adds significant complexity; defer.

## sandbox-side `present_to_user`

In `fxtr_ws_mcp_sandbox_bridge/protocols.py`:

```python
@dataclass(frozen=True)
class PresentToUserOp:
    payload: dict[str, Any]   # wire-form (post-walker)
    entity_arg_handles: list[str]
    kind: str = "present_to_user"
```

`QueuedOp` union extended with this.

In `fxtr_ws_mcp_sandbox_bridge/builtins.py`:

```python
async def present_to_user(payload: FxtrServiceData) -> None:
    walked = await _walk_for_wire(payload, _maybe_register_entity)
    _enqueue(PresentToUserOp(payload=walked, entity_arg_handles=...))
```

Reuses the existing `_walk_for_wire` logic (and entity-handle
registration via `host.copy_entity_in`). The op is folded into the
host-side `ScriptExecution.ops` entry; *not* sent over the wire to
the workstation (it's an MCP-side concept).

## Host-side handling of PresentToUserOp

In `_apply_script_op` / wherever scripts ops are dispatched:
- `PresentToUserOp` does *not* contribute to the wire mutation
  (no JSON Patch, no workstation op).
- Captured into the per-script "presented payloads" list.
- After commit, the substitution walker runs on each presented
  payload to produce the `ScriptOpRecord(kind="present_to_user",
  payload=<walked>)` for the log entry.

## CODE_OVERVIEW updates

Both `projects/fxtr_workstation_agent_plugin/CODE_OVERVIEW.md` and
`projects/fxtr_workstation/CODE_OVERVIEW.md` get updates:

- Agent plugin: new modules (`agent_log_types.py`,
  `agent_mcp_proto.py`, `agent_mcp_impl.py`, `log_walker.py`,
  `log_builder.py`), new section in *Mental model* describing
  the event chain + workstation_inbox setup + agent_mcp service
  lifecycle, new `PresentToUserOp` in the sandbox-bridge types,
  new `present_to_user` builtin, toml schema change.
- Workstation server: new `/client` request
  `GetCallbackServiceRequest` + corresponding response. (Tiny.)

## Open / sharp edges

- **Wire-form ops still in ScriptExecution.** The script's wire
  payload (e.g. `schedule_invoke.args` already has wire-encoded
  entity placeholders / callback handles) gets walked through the
  substitution walker. Need to handle `["entity_handle", handle]` /
  `["callback_handle", slot_key]` placeholders — these are
  *pre-substitution* host-internal forms. The mutation builder has
  already substituted to `["entity", short_id]` / `["callback", N]`
  by the time we walk (we walk the post-build wire form, not the
  pre-build form). TODO: confirm this in the implementation.

- **`workstation_inbox` reserved slot_key.** Agent scripts can't
  use that key for their own slots. Need a host-side reject in
  `_apply_script_op` for any op writing to `workstation_inbox`.

- **AgentMcpImpl session-scope ownership.** The impl needs to live
  through session lifetime, get torn down on session-end + on
  cycle-end. Cleanest is a per-cycle, per-session sub-task on the
  inner TG that owns the scope + expose_service CM. Sketch in
  plan; details at implementation time.

- **Chain entity backwards-pointer reachability.** Pinning only the
  latest head means earlier entries need to be loadable from the
  workstation. They are — the persistence cap retains the entity
  subgraph through the chain's transitive refs (preserve_provenance
  or no_provenance, both fine).

- **Storage of in-flight chain on supervisor pause.** If the
  supervisor pauses mid-extension, the next cycle reloads from
  persistence (the last pinned head). Any extensions that didn't
  pin are lost. Same contract as above.

- **`WsAgentEvent` union typing.** `EntityRef[A | B | C | D]` needs
  mashumaro support for the union via the inner entity's
  `$fxtr.type` discriminator. v2 supports this already (the workstation
  shape uses similar patterns). Confirm at implementation time.

- **`OwnedCallbackPlaceholder.slot_key` reverse-lookup at walk
  time.** The walker has access to `mirror.pending_slot_index`
  (slot_n → slot_key). For active callbacks the slot_key is
  available; for already-closed callbacks the index entry is
  removed but data_by_key may still hold the slot_key — fall back
  to a `data_by_key` reverse scan if needed.
