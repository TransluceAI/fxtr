# fxtr_workstation_agent_plugin

The agent-facing client for the fxtr workstation — a Claude Code MCP
plugin that gives an LLM agent a sandboxed Python environment for
driving a persistent workstation session.

New here? Read `projects/fxtr_workstation/CODE_OVERVIEW.md` first to
understand the workstation server (this package is one of its
clients). The plan documents in
`projects/fxtr_workstation/plans/phase0/part2_ws_mcp/` explain the
design decisions in more depth.

## Layout

```
fxtr_workstation_agent_plugin/
├── README.md                 end-user docs (load plugin, .toml, smoke walkthrough)
├── CLAUDE.md                 how to work in this package
├── CODE_OVERVIEW.md          this file
├── plans/                    (empty — plan docs live alongside the workstation's)
├── fxtr_workstation_mcp/     host-side Python package
│   ├── pyproject.toml        console-script `fxtr-workstation-mcp` → `server.main`
│   ├── fxtr_workstation_mcp/
│   │   ├── server.py             FastMCP entry; lifespan; tool callables (incl. `nix_lock` → the backend's `lock()`, to (re)create the workdir's flake.lock without host nix); `logged_tool(name)` verbose-IO decorator
│   │   ├── app_context.py        lifespan-scoped dataclass passed via ctx
│   │   ├── config.py             `.fxtr_workstation.toml` read/write (tomlkit; preserves comments); `WorkstationConfig` (frozen, static fields only) + free `read_active_session` / `write_active_session` helpers
│   │   ├── agent_log_types.py    typed event-log entity definitions (`WsAgentLogEventChain`, per-event entities, placeholders, `ScriptOpRecord`)
│   │   ├── log_walker.py         substitution walker — wire-form payloads → typed `FrozenFxtrCbor` (entity / service / callback substitutions); explicit-scope (no active scope)
│   │   ├── log_builder.py        builds typed log entities (`ScriptExecutionLogEvent` / `InvokeResultLogEvent` / `InvokeErrorLogEvent` / `CallbackNotificationLogEvent`) and extends/pins the chain
│   │   ├── agent_mcp_proto.py    `AgentMcpRunningSessionProto` — workstation-exposed service surface (`get_state` / `subscribe` / `send`)
│   │   ├── agent_mcp_impl.py     `AgentMcpRunningSessionImpl` — chain-head subscriptions via a coalesce-mode fxtr2 `SubscriberHub` (subscribe seeds the retained head as the first event)
│   │   │   …also: bootstrap lives in `ws_connection.py` — `_BootstrapAgentMcpJob`
│   │   │            (cycle-start path) + inline `_do_bootstrap_agent_mcp_inner` (open_session
│   │   │            path); the long-lived `_agent_mcp_session_lifecycle` subtask owns the
│   │   │            impl_scope + impl_tg + the `agent_mcp:{session_id}` registration.
│   │   │            `_do_close_session` cancels the subtask via its saved cancel_scope.
│   │   ├── ws_connection.py      `WorkstationConnection` — supervisor, reader, worker, job queue
│   │   ├── websocket_util.py     `aconnect_ws` wrapper (httpx-ws `__context__` workaround)
│   │   ├── session_state.py      `SessionMirror`, `PendingChange`, `ForgetMissingKey`/`SlotKeyAlreadyExists`
│   │   ├── mutation_builder.py   `build_mutate_request` — derived dict + script ops → wire request
│   │   ├── host_impl.py          `McpHostImpl` — per-script bridge to /rpc + mirror; owns `retrieve_nix_entity` (DATA entity → script) + `current_commit` (HEAD, clean-tree guard)
│   │   ├── channel.py            `notifications/claude/channel` helper + summarizer
│   │   ├── outgoing_buffer.py    `OutgoingBuffer` — lifespan-scoped delivery buffer (in-tool drain / debounced flush / slot watchers)
│   │   ├── exception_log.py      `ExceptionLog` — lifespan-scoped bounded LRU keyed by monotonic `exc_id`; stores full tracebacks for `get_full_exception`
│   │   ├── sandbox_env.py        `SandboxEnvSupervisor` — owns the eryx env's lifecycle
│   │   ├── sandbox_factory.py    eryx bootstrap; walks `<workdir>/packages/` for per-script volume mounts
│   │   └── git_util.py           read-only git wrappers (`head_commit` + clean-tree guard `is_dirty`/`is_git_repo`, `untracked_count`) + `NotAGitRepo`/`WorkingTreeDirty`; back `current_commit` + the untracked-files notice
│   └── tests/                    pytest-anyio; per-test SIGALRM timeout in conftest
├── sandbox/                  loaded into the sandbox, not the host
│   ├── sandbox_runtime/          bootstrap stub for eryx's dep closure
│   └── fxtr_ws_mcp_sandbox_bridge/
│       └── fxtr_ws_mcp_sandbox_bridge/
│           ├── protocols.py      `McpHostProto`, `McpRunnerProto`, `RunnerConfig`, `QueuedOp` union, handle stubs, sandbox-local `WorkstationSessionEventInbox`, sandbox-local `NixWasmServiceDefn` + `NixContainerServiceDefn` (wire-identical dups of the `fxtr_sandbox_types` entities), sandbox-local `NixDispatcherProto` (agent-side `set_preset`-only mirror of the workstation dispatcher proto; `resolve`/`subscribe` omitted — workstation-only types), ref factories, `_ResultTypeSentinel`
│           ├── entry.py          `run(config)` — sandbox-side entry point
│           ├── script_runner.py  exec primitive; installs `_fxtr_mcp_builtin`
│           └── builtins.py       agent-facing closures (`lookup_entity`, `export_entity`, `schedule_invoke`, `retrieve_result`, `create_callback`, callback-notification helpers, `present_to_user`, `retrieve_nix_entity`, `current_commit`, `result_type_of`, …), type/exception re-exports into `_fxtr_mcp_builtin` (`ServiceRef`, `WorkstationSessionEventInbox`, `Subscriber`, `UnsubscribeHandle`, `EventSource`, `UnknownService`, `NixWasmServiceDefn`, `NixContainerServiceDefn`, `NixDispatcherProto`, `NixEntityBuildFailed`, `WorkingTreeDirty`), and the typed-dispatch encode/decode path
├── claude_plugin/            Claude Code plugin packaging — load-bearing
│   ├── .claude-plugin/
│   │   ├── marketplace.json      local marketplace catalog
│   │   └── plugin.json           plugin manifest (declares the channels capability)
│   ├── skills/use_fxtr_workstation/SKILL.md
│   └── .mcp.json                 stdio launcher (uv run fxtr-workstation-mcp)
└── examples/                 manual + automated smoke-test artifacts
    └── basic_test_demo/
        ├── README.md                       top-level setup walkthrough
        ├── run_demo_workstation.py         standalone WorkstationServer with `delayed-echo`, `demo-note` entity, and `callback-tester` service
        └── agent_workdir/                  the workdir the MCP points at
            ├── README.md                   for the agent
            ├── .fxtr_workstation.toml.example
            ├── preamble.py
            ├── .gitignore                  keeps the runtime `.fxtr_workstation.toml` (+ `.tmp_outputs/`, `result*`, venvs) out of the committed tree, so `current_commit`'s clean-tree guard + the untracked-files notice ignore them
            ├── flake.nix                   declares fxtr.services.hello_service = mkWasmService {…} (CODE, git-ref spawn), fxtr.entities.demo_data = mkEntityFromPython {…} (DATA, built on demand by retrieve_nix_entity), fxtr.entities.basic_type = { type="builtin"; idString="!basic_type"; } (a builtin DATA entity — a primitive FxtrID, no build), and fxtr.uiPresets.demo_preset (CODE — type-id-keyed renderer preset: typeEntity via mkEntityFromPython + renderer via esbuildRenderer, pushed to a nix dispatcher by git ref). The fxtr input is a substitution placeholder (filled by prepare_workdir_repo / the scaffold)
            ├── renderers/                  TSX renderer modules for uiPresets
            │   └── demo_data/index.tsx     DemoDataEntity renderer (4 shell dispatch keys); esbuildRenderer-compiled, routed by fxtr.uiPresets.demo_preset. No fxtr_renderer.json (routing is inline in the preset attr)
            └── packages/                   src-layout python projects
                ├── demo_pkg/               Note + scenarios (shared with the server script; sandbox-oriented — its __init__ imports the bridge, so it can't be imported from a host venv)
                ├── demo_data/              host-side pkg whose self-contained `DemoDataEntity` (`DEMO_DATA`) the flake materializes as fxtr.entities.demo_data via mkEntityFromPython. Depends on full host fxtr2 (comment-flip sources, NOT wasm-pinned)
                └── hello_service/          sample sandboxed service; built as fxtr.services.hello_service by the flake. pyproject uses the comment-flip [tool.uv.sources] convention (no committed uv.lock — generated in the workdir copy)
```

`examples/basic_test_demo/` doubles as the fixture for
`tests/test_mcp_in_memory.py` — anything that breaks the manual demo
also breaks the test, and vice versa.

For an operator-facing demo with a live frontend view of the
agent's event log + a text-box to send messages back, see
[`examples/fxtr_workstation_demo/`](../fxtr_workstation_demo/) —
the assembly-stage consumer demo. That one brings the
workstation + the agent MCP plugin + the
`@fxtr-ui/agent-plugin-renderer` frontend together, with a
persistence cap so the agent's event log survives restarts.
`basic_test_demo/` stays as the smaller plugin-only fixture
(and as `test_mcp_subprocess.py`'s backing setup).

## Mental model

### Two long-lived connections, one supervisor

A `WorkstationConnection` owns two persistent WebSockets to the
workstation server: `/rpc` (fxtr-RPC, for `get_entity` /
`expose_entity` / etc.) and `/client` (the session protocol).
Both are managed by **one supervisor task** that lives for the
duration of the FastMCP lifespan. The supervisor is the only
place context managers are entered or exited — no cross-task
`__aexit__` hazards.

```
fxtr_workstation_mcp process (FastMCP / stdio)
└── lifespan task_group
    ├── WorkstationConnection supervisor (one anyio task)
    │   ├── opens /rpc + resolves workstation_proxy into host_scope
    │   ├── opens /client websocket
    │   └── inner task group:
    │        ├── reader task   demultiplexes inbound frames
    │        └── worker task   drains the single typed-job queue
    └── SandboxEnvSupervisor (started lazily by run_script)
         ├── builds the eryx factory from sandbox_runtime's deps
         └── owns the DirectSandboxEnvironment for the lifespan
```

The supervisor is a single-state-machine task driven by one enum
(`SupervisorState`) and two events (`_state_changed`,
`_retry_requested`). It starts in **PAUSED** with reason
`"(not started)"` and only begins work when a tool calls
`wake_and_wait_ready` (`set_working_dir` constructs +
`start(tg)`s the connection; the supervisor parks until a tool
wakes it).

**Per-cycle toml refresh.** At the very top of `_run_one_cycle`,
the supervisor re-reads `.fxtr_workstation.toml` from the
connection's `working_dir` via `config.read_active_session` and
refreshes `_active_session_id`. This is what lets the agent
recover from a stale session ID by manually editing the toml —
the next cycle picks up the change without an MCP restart. If
the toml fails to parse / validate, `_refresh_config_from_disk`
raises `ConfigInvalid` (a `WorkstationRejected` subclass) and
the supervisor pauses with the underlying error as
`pause_reason`. Only `active_session` is refreshed; URLs and
`agent_mcp_store` are baked in at construction (changing them
warrants a restart).

**Single source of truth for the active session.**
`active_session` lives in exactly two places: the toml on disk
(persisted form) and `connection.active_session_id` (runtime
view, refreshed per cycle + updated by open/close paths). It is
**not** a field on `WorkstationConfig` — that dataclass holds
static config only. Tools read `connection.active_session_id`
for "is there an active session?" checks, and call
`config.write_active_session(working_dir, ...)` to persist
changes. The force-close path (which bypasses the supervisor)
calls `config.read_active_session` directly to pick up external
toml edits.

Per-cycle failure handling:

- **Wire disconnect after the cycle reached READY, free-retry
  budget > 0**: the supervisor immediately retries (no PAUSED
  transition, no banner, no backoff). `_free_retry_budget`
  decrements by one. The budget is refreshed to
  `MAX_FREE_RETRY_BUDGET` (= 3) on every successful
  `wake_and_wait_ready` return — i.e. every time the agent
  actually got useful work done through this connection.
- **Agent-initiated disconnect** (`disconnect()` tool fired
  `_disconnect_requested`): the supervisor transitions to
  **PAUSED** with `pause_reason = "disconnected by request"`
  and **does not** emit a `supervisor_paused` banner — the
  agent asked for this state, re-notifying would be noise.
  Recognized via `_find_disconnect_leaf` walking the exception
  group for the `_DisconnectRequested` sentinel raised by
  `_run_inner` (this leaf wins over any other cycle-end cause
  in the same group).
- **Anything else** (non-wire crash, pre-ready failure, or wire
  failure with the budget exhausted): the supervisor transitions
  to **PAUSED** with a formatted `pause_reason` string (via
  `_format_pause_reason`) and emits one `supervisor_paused`
  channel banner. It then awaits the next
  `_retry_requested.set()` from a tool's `wake_and_wait_ready`
  (or shutdown). No non-cancellation exception ever escapes the
  supervisor task.

The single-bit `_reached_ready_this_cycle` flag (reset at the top
of every cycle, set alongside `_set_state(READY)` in `_run_inner`)
gates the wire-error free-retry path; subscribe-time failures land
before it flips and so route through PAUSED.

`wake_and_wait_ready` is a condition-variable-style loop that
captures `_state_changed` before reading `_state`. If state is
PAUSED, it fires `_retry_requested` *once*, waits for the next
transition, and re-checks. If state goes PAUSED a second time
within the same call, it raises `SupervisorNotReady` with the new
`pause_reason` rather than looping forever — the agent's tool
gets a clean error and decides whether to call again.

If the same poisoned `subsequent_events` keeps re-pausing the
supervisor on every wake (the canonical "stuck session"
scenario), the agent recovers via `close_session(force=True)` —
see **Force-close recovery** below.

Tools never touch scopes or context managers. They submit
**typed jobs** onto the worker queue and await a completion
future. The five jobs:

- `_OpenSessionJob` / `_CloseSessionJob` / `_ScriptJob` —
  submitted by tool calls; each has a `completion: Future`.
- `_EventJob` — pushed by the reader when a `SessionEventFrame`
  arrives on the active subscription.
- `_AckCommitJob` — pushed by the worker itself after a
  self-commit succeeds (see "mutation lifecycle" below).

### Reader / worker split

```
/client wire frame
        │
   reader task ──── per-request frame? ─→ pending-request stream (worker `_expect` reads it)
        │
        └─── SessionEventFrame for active subscription? ─→ worker queue (as `_EventJob`)

worker queue ──→ worker task ──→ _dispatch_job ──→ per-job handler
```

The split is mandatory: an RX-driven follow-up (e.g. a foreign
state_update triggering local work) can't go through the worker if
the worker is the thing that reads it. The job queue is the **only
serialization point** for state changes — exactly one job runs at a
time. Reader work that doesn't enqueue (per-request frame routing)
is GIL-atomic dict touches that the worker never contends with.

### Session state — mirror, replayable events, derived dict

The connection holds these fields together representing its view of
the active session:

```
_mirror                  the local view (data_by_key + callback_notifications +
                         callback_subscription_states + tracker pin sets +
                         seqs + pending_slot_index)
_replayable_events       non-state events we've absorbed since the workstation's
                         last ack horizon (StateUpdateEvent excluded)
_pending_changes         ordered log of mutations the workstation hasn't yet
                         committed; the mutation builder walks this to produce
                         the next outgoing JSON Patch
_last_processed_seq      highest event seq we've fully processed (= ack_seq we
                         send on outgoing mutations)
_slot_counter            workstation's slot_counter, copied on each state_update
_notified_seqs           seqs we've delivered as channel notifications (dedupe
                         for the agent surface)
```

`_replayable_events` is the **authoritative** record of what events
we've processed since the last workstation-ack horizon.
`_pending_changes` is a derived view, recomputed from it whenever a
state_update arrives (plus a synthetic `"initialize"` prologue if
the workstation's state lacks the top-level fields the MCP writes
to). The mutation builder walks this log in order to produce the
next outgoing JSON Patch.

Each entry is one of `PendingChange(op="initialize")` (creates
`/data_by_key {}` + `/callback_notifications {}`),
`PendingChange(op="set", path, value, seq)`, or
`PendingChange(op="remove", path, seq)`. The `"set"` translator
lazy-creates any missing intermediate sub-container objects on the
path (e.g. `/callback_notifications/<slot_key>` when the first
notification for a slot_key flushes), so the connection doesn't need
to track per-slot-key existence flags.

The mirror's `data_by_key` is the **local view** — workstation's
`state["data_by_key"]` (carried by the most-recent state_update)
plus the replayed effects of every event currently in
`_replayable_events`. The mirror's `callback_notifications` is the
parallel local view of the workstation's
`state["callback_notifications"]` sibling field — a
`dict[slot_key, dict[notif_index_str, wire_escaped_payload]]` that
persists `CallbackEvent` payloads across reconnects and lets the
agent retrieve them via `retrieve_callback_notification`.

`mirror.pending_slot_index` is a derived reverse index:
`slot → slot_key` for `["invoke_pending", N]` /
`["callback_open", N]` entries — lets the per-kind dispatch in
`_apply_state_effect` find slot_keys in O(1) when result / callback
events arrive. The invoke branch drops the index entry on promote;
the callback branch keeps it (callbacks fire repeatedly).

**Two helpers maintain these fields.** `_apply_state_effect` (single
place that absorbs one non-state event: appends to
`_replayable_events`, runs per-kind dispatch, writes to
`_unflushed_state_changes` + the mirror) and `_handle_state_update`
(authoritative reset: applies the new workstation view to the
mirror, trims `_replayable_events` to events past the new ack
horizon, then re-runs `_apply_state_effect` on each survivor). They
compose cleanly because their concerns are orthogonal:
state_update is authoritative-reset; event apply is
incremental-update. Foreign state_update goes through both (reset,
then replay surviving events on top). Subscribe-time snapshot
replay is the same shape (wipe everything, apply last_state, enqueue
subsequent_events). Self-commits go through `_apply_local_commit`
directly (see below). Neither helper touches `_last_processed_seq`
— the caller advances it (the timing is caller-specific).

**Self-commits don't synthesize an event.** The workstation
self-suppresses broadcasting its state_update back to us; we know
everything we need from the `MutationCommittedResponse` +
`BuildResult` directly. `_apply_local_commit` updates the mirror
via `apply_workstation_view` and **clears** `_replayable_events` +
`_unflushed_state_changes` directly — no rebuild loop is needed
because the new `ack_seq` we sent covers every event in the list
(see invariant 6).

**Notifications are deduped by seq.** Each per-kind branch in
`_apply_state_effect` calls `_maybe_notify`, which checks
`_notified_seqs` and skips if the event's seq is already there;
otherwise records + delivers through `_notify_channel`.
`_notify_channel` is wired (via a thin async adapter in
`server._ensure_connected`) to `OutgoingBuffer.add`, so the
buffer's tool-window / debounced-flush routing decides whether the
message folds into a tool result or goes out as a standalone
channel frame. The dedupe persists across `_do_subscribe`'s wipe
(it's reset only on session close), so the standard reconnect path
naturally suppresses re-notification for events the agent already
saw. `_handle_state_update` and `_apply_local_commit` both prune
`_notified_seqs` for seqs past the new ack horizon — bounded growth,
and "always notify, let dedupe filter" replaces what would otherwise
be an explicit arrival-vs-replay branch.

**`_last_processed_seq` is tracked separately.** It tracks "highest
event seq we've fully processed locally" and is the value we send as
`ack_seq` on outgoing mutations. `_handle_state_update` deliberately
does not touch it — the caller does, because the timing of the seq
advance is caller-specific (see mutation lifecycle below).

### Outgoing message buffer

`OutgoingBuffer` (in `outgoing_buffer.py`) is a lifespan-scoped
abstraction that owns *all* agent-facing notification delivery. The
connection's `_notify_channel` callable pushes to
`buffer.add(content, attrs)`, and the buffer decides where the
message ends up:

```
add(content, attrs)
   │
   ├── if a tool's tool_window() is open (depth > 0)
   │     → message stays in _pending until the tool drains
   │       on exit. Drained text is appended to the tool's
   │       primary result string.
   │
   └── if no tool is open
         → first add schedules a debounced flush task on the
           lifespan TG. After debounce_seconds (default 0.1)
           the task drains _pending into one channel frame
           with meta={"kind": "events"} and the rendered
           <event …> blocks newline-joined as content. If a
           tool opens during the sleep, the flush bails and
           the tool drains instead.
```

Each tool that talks to the workstation (`connect`, `open_session`,
`close_session`, `run_script`, `wait_for_slot_notification`) wraps
its body in `buffer.tool_window()`; `connect` additionally
`anyio.sleep(0.1)` before draining so just-in-time events make it
into the same response. `run_script` either does the same 0.1s
trail-delay (default `wait_seconds=0.0` path) or runs the
wait-for-invokes branch (`wait_seconds > 0.0`; see "Inline-wait on
scheduled invokes" below). `set_working_dir` is not wrapped (no
connection exists during it; buffer is unconditionally empty).

Render format: `<event slot_key="r" kind="invoke_result">repr(payload)</event>`
with attributes alphabetized for determinism and no escaping.
Drained tool output: raw `<event …>` blocks inline. Debounced
channel frame: same blocks wrapped in
`<channel source="workstation_mcp" kind="events">…</channel>` by
Claude Code's renderer.

**Slot watchers.** `OutgoingBuffer.watch_slot(slot_key)` returns a
one-shot `anyio.Event` that fires the next time an `add(content,
attrs)` arrives with matching `attrs["slot_key"]`. Used by
`wait_for_slot_notification` to block inline waiting for a specific
slot's next event. `peek_slot_key(slot_key)` on
`WorkstationConnection` is the sync precondition check — it returns
the current `mirror.data_by_key.get(slot_key)` so the tool can
short-circuit on absent or terminal slots (terminal = the slot's tag
is not in `{"invoke_pending", "callback_open"}`).

**Self-commit notification.** `_do_ack_commit_job` emits a
`kind="self_commit"` event with the committed seq alongside
advancing `_last_processed_seq`. Lands at the right spot in the
buffer relative to any foreign events queued before our response so
the agent can tell which slot promotions came before vs. after their
own mutation.

### Inline-wait on scheduled invokes

`run_script(script, wait_seconds=0.0)` takes an optional
`wait_seconds: float` parameter. With `wait_seconds > 0`, after the
mutation commits, `run_script` blocks until every invoke this script
just scheduled has promoted out of `["invoke_pending", N]` (to
`invoke_result` / `invoke_error`) or `wait_seconds` elapses. The
waited-on result events flow through the same `OutgoingBuffer` path,
so they end up folded into the tool's response string inline. On
timeout the response gets a one-line tail naming the still-pending
slots — the tool never raises on timeout.

Scoping is per-script: the wait set is built from
`runner_queued_ops` entries with `kind == KIND_SCHEDULE_INVOKE`,
plumbed up through `ScriptOutcome` (the dataclass return value of
`_apply_script_mutation` / `_do_script_job` /
`WorkstationConnection.run_script`). A long-running invoke scheduled
in an earlier `run_script` call doesn't delay a later short one's
`wait_seconds`.

`wait_seconds == 0.0` (the default) keeps the simple behavior:
`anyio.sleep(0.1)` trail-delay, then drain. The wait-branch
machinery is never entered and no "Timed out…" tail is ever emitted.

Implementation: `server._wait_for_scheduled_invokes` peeks each slot
via `connection.peek_slot_key` (filters out anything already
terminal at peek time — handles the race where the result lands
during script send/commit), registers an
`OutgoingBuffer.watch_slot` event for each remaining slot, and waits
for all of them inside `anyio.move_on_after(wait_seconds)`. The peek
+ register window has no awaits, same race-safe contract as
`wait_for_slot_notification`. `unwatch_slot` in `finally` releases
any unfired watchers.

### Mutation lifecycle (the self-commit flow)

A `_ScriptJob` runs a sandboxed Python script, then turns its
queued ops into one wire mutation:

```
_handle_script_job
    └─ _do_script_job
        ├─ open script_scope; mount McpHostImpl; run sandbox script
        ├─ collect runner's queued_ops (export_entity / forget / …)
        └─ _apply_script_mutation
            ├─ register each export entity over /rpc (held on AsyncExitStack)
            ├─ build_mutate_request(mirror, _unflushed_state_changes,
            │                       queued_ops, …) → BuildResult or None
            ├─ if None: return early ("no queued ops")
            ├─ send the mutation; await MutationCommittedResponse
            ├─ on rejection: return tool-error string
            ├─ on commit:
            │     ├─ _apply_local_commit: mirror.apply_workstation_view(
            │     │     post_state, response.seq, sent_ack_seq, …) +
            │     │     clear _replayable_events + _unflushed_state_changes +
            │     │     prune _notified_seqs (no synthesized event)
            │     └─ submit _AckCommitJob(seq=committed_seq) to the queue
            └─ AsyncExitStack releases entity registrations. The
              workstation's invoke worker session-pins each entity
              arg when it processes the InvokeOp, so the args stay
              alive across dispatch even after we drop our /rpc reg;
              no in-flight registration bookkeeping is needed on our
              side. Exported-entity refs are independently kept
              alive by the tracker diff this same mutation emitted.
```

**Why `_AckCommitJob` is separate** — `_last_processed_seq` must
not advance past foreign events that were queued before our
response. By submitting an `_AckCommitJob` onto the back of the
queue rather than advancing the seq inside `_do_script_job`, we
guarantee any foreign `_EventJob`s the reader queued during the
mutation's await drain first; `_last_processed_seq` only catches
up to the committed seq after every prior event has been applied.
Foreign state_updates (which arrive *as* `_EventJob`s) advance
`_last_processed_seq` inline at the call site, since by definition
all earlier events have already been processed.

### Hard-error boundaries

Per-job handlers split exceptions into two buckets:

- **Tool errors**: precondition failures (`SessionAlreadyActive`,
  `NoActiveSession`), workstation rejections (`WorkstationRejected`).
  Caught by the per-tool-call handler, routed to the completion
  future, supervisor continues.
- **Tear-down errors**: protocol violations (`ProtocolError`),
  unimplemented handlers (`NotImplementedError`), cancellation,
  unexpected exceptions. Propagate out of `_dispatch_job` → inner
  task group exits → supervisor cycle ends. The supervisor's outer
  except splits by cause: wire disconnects retry with exponential
  backoff (silent — no agent notification); non-wire crashes
  **park** (emit one `supervisor_paused` channel frame and wait for
  the next agent tool call to wake — see "supervisor / parking"
  above). Only `BaseException` (cancellation) and pre-ready non-wire
  errors propagate further up.

`_dispatch_job` is a bare match — it has no exception handling.
Per-tool-call wrappers (`_handle_open_session_job`,
`_handle_close_session_job`, `_handle_script_job`) catch their
specific tool-error types; everything else propagates. Non-tool-call
jobs (`_EventJob`, `_AckCommitJob`) run their bodies directly
through `_dispatch_job` — any exception means state may be
inconsistent and we tear down.

### Scope chain

```
GLOBAL_SCOPE  (fxtr2 process-wide root)
  │
  ├── workstation /rpc connect-scope (per cycle)
  │       │
  │       └── host_scope                 isolated + mountable
  │               (workstation_proxy resolved here once per cycle)
  │               │
  │               └── script_scope       isolated + mountable, child of host_scope
  │                       (per-script: McpHostImpl mounted here)
  │                       │
  │                       └── sandbox FxtrScope (per open_sandbox call)
  │
  └── sandbox env transient bootstrap scope (lifespan-long after first run_script)
```

`host_scope` lives for the supervisor cycle. `script_scope` lives
for one `_ScriptJob`. The sandbox scope is opened inside
`script_scope.as_active_scope()` so the per-call host_ref encodes
cleanly across the chain.

### Sandbox interaction

The agent writes scripts that run inside an eryx sandbox. Inside the
sandbox, a synthetic `_fxtr_mcp_builtin` module exposes closures
(`lookup_entity`, `export_entity`, `forget`, `schedule_invoke`,
`retrieve_result`, …). These closures either RPC back to the host
(reads / state queries) or record `QueuedOp`s on a list (writes);
the runner returns the list at end-of-script, the host drives the
mutation pipeline from it.

`script_runner.run_preamble_and_script` installs `_fxtr_mcp_builtin`
*before* the preamble exec, so preamble authors can `from
_fxtr_mcp_builtin import …` at the top level and have the names live
in the per-script globals for free. (The preamble itself is `exec`'d
without top-level await, so async builtins can be imported but only
*called* from the per-script body.) The wrapper captures both
`sys.stdout` and `sys.stderr` via parallel
`contextlib.redirect_stdout` / `redirect_stderr`, and catches any
`Exception` raised by the preamble or script into a formatted
traceback string returned on `RunResult.error`. `BaseException`
(cancellation, `SystemExit`) still propagates so
structured-concurrency teardown stays correct.

On error the sandbox zeros out `queued_ops` on the wire and the
host's `_do_script_job` routes through `_format_script_error`
(skipping `_apply_script_mutation` entirely — script semantics are
atomic, so a partial side-effect is never applied). Both success and
error paths share the same `--- name ---` / `--- end name ---`
fence-marker format via `_fenced`; empty sections are omitted so the
no-op case stays terse.

Bridge → host RPC dispatches run on RPC-owned tasks during
`runner.run()` — they are structured-concurrency children of
`script_scope` and fully drain before the worker resumes, which is
why no cross-task synchronization is needed.

### Script exception compression

Script tracebacks can run to dozens of lines (user code → library
calls → typed-service dispatch → fxtr internals); shipping the
whole thing on every script-raise burns the agent's context window
on frames they rarely need. `_format_script_error` reduces the
traceback to a two-frame summary by default and stashes the full
text in a lifespan-scoped `ExceptionLog`:

- **`_compress_script_traceback(full)`** parses the
  `traceback.format_exception` output, picks two anchor frames,
  and renders them under the original `script_runner` headline:
  - `[user]`: the deepest frame whose filename is in
    `_USER_CODE_FILENAMES` (`<mcp_script>` / `preamble.py`) — the
    agent's own code.
  - `[raise]`: the very last frame, unfiltered — even if it's
    deep in library code. This is where the exception actually
    came out.
  - If the two are the same frame (raise happened directly in
    user code), they collapse to a single `[user]` line.
  - If no user-code frame is found (defensive — `script_runner`
    always runs user code), only the `[raise]` line is emitted.
- **`ExceptionLog`** is a bounded LRU (default 20) keyed by
  monotonic `exc_id` (`exc_1`, `exc_2`, …). `store(full)` appends
  and evicts the oldest on overflow; ids never recycle within a
  process. Lives on `AppContext` and threaded into the
  `WorkstationConnection` constructor so `_format_script_error`
  can write to it.
- **`get_full_exception(exc_id)`** sandbox builtin proxies to
  `McpHostImpl.get_full_exception` which reads from the log.
  Raises `LookupError` to the agent's script when the id is
  unknown (never stored or evicted). The agent calls it in a
  subsequent `run_script` to fetch the full text when the
  compressed two-frame summary isn't enough.

### Handle namespaces

Three distinct namespaces share the per-script lifecycle and
deliberately do **not** alias:

- **Agent slot_key** — the string the agent picks (e.g. `"r"`).
  Lives in `data_by_key` and on the wire ops.
- **Entity-arg handle** — the host-side key for an entity copied
  into `script_scope`. Derived: `slot:{slot_key}` (export) /
  `invoke_arg:{uuid4}` (schedule_invoke arg). Helpers `slot_handle`
  / `fresh_invoke_arg_handle` in
  `fxtr_ws_mcp_sandbox_bridge.protocols`. Used as key in
  `McpHostImpl.copied_entities` and in the worker-built
  `entity_short_ids` map.
- **Workstation `external_name`** —
  `.mcp-tmp:{session_id}:{uuid4}:{handle}` passed to
  `expose_entity`. Leading dot marks it internal; the embedded
  session_id + uuid4 + handle make it traceable in workstation-side
  logs.

Sandbox-side `ServiceRef[T]` handles are real fxtr2 `ServiceRef`s
backed by two `IRHandle` subclasses (both in
`sandbox_bridge.protocols`, neither ever dispatched — they only
carry sandbox-side metadata, with `token = STATIC_LIFETIME.token`
and `request` / `dispatch` raising `NotImplementedError`):

- `_RemoteServiceHandleStub(short_id: str)` — for a workstation-known
  service. Wire walker emits `["service", short_id]` for invoke args;
  the `exported_service` slot tag round-trips through this shape.
- `_OwnedCallbackHandleStub(slot_key: str | None, slot_n: int | None)`
  — for a callback this session owns. Two same-/cross-script shapes:
  pre-commit (only `slot_key` set, emits `["callback_handle",
  slot_key]`; mutation builder rewrites to `["callback", N]`) and
  post-commit (only `slot_n` set, emits `["callback", N]` directly).

Both stubs populate `ServiceRef.service_type_id` with a sandbox
sentinel (`WS_MCP_REMOTE_SERVICE_TYPE_ID` /
`WS_MCP_OWNED_CALLBACK_TYPE_ID`) — never serialized, just makes refs
Eq-comparable.

### Invoke flow (typed and untyped dispatch)

`schedule_invoke(slot_key, service, action)` captures a single
method call on a recording proxy passed to the user's lambda. The
captured `(method_name, raw_args, raw_kwargs)` then splits on
`service.target_type`:

- **Untyped path** (default — `service.target_type is UnknownService`):
  raw args + kwargs go straight into the wire walker.
- **Typed path** (`service.target_type` is a
  `@service_protocol`-decorated Protocol): looks up the protocol's
  `_fxtr_service_protocol_metadata`, builds the parameterized
  `args_wrapper_cls` for the named method, encodes via
  `get_basic_encoder(...)` against the protocol's dialect (each
  `EntityRef[T]` → `FxtrID`, each `ServiceRef[T]` →
  `ServiceRef[UnknownService]`), strips `USE_REMOTE_DEFAULT` entries,
  splits via `inspect.BoundArguments(sig_no_self, encoded)`, then
  runs the resulting args/kwargs through the same wire walker.
  Errors: `NotImplementedError` if `target_type` isn't
  `@service_protocol`-decorated; `IRException` (category
  `UnknownMethod`) for missing method names. See
  `_typed_encode_invoke_args` in `sandbox_bridge.builtins`.

The wire walker (`_walk_for_wire`) handles the union of raw types +
dialect-output vocabulary:

- `FxtrID` / `EntityRef` → host's `copy_entity_in(<entity_arg_handle>,
  fxtr_id)` (handle synthesized via `fresh_invoke_arg_handle()`) +
  emit `["entity_handle", <handle>]` placeholder (distinct tag from
  the wire-final `["entity", short_id]` so pre- and post-substitution
  forms are unambiguous).
- `ServiceRef` dispatched on `handle` subtype:
  `_RemoteServiceHandleStub` → `["service", short_id]`;
  `_OwnedCallbackHandleStub(slot_key)` → `["callback_handle",
  slot_key]`; `_OwnedCallbackHandleStub(slot_n)` → `["callback", N]`.
- `bytes` → `["bytes", <b64>]`; lists / tuples → `[[…]]`; scalars
  pass through.

Each `ScheduleInvokeOp` records the list of entity-arg handles plus
the keyword-arg dict. End-of-script, the worker walks queued ops and
registers each entity-bearing handle — `slot_handle(op.slot_key)`
for each `ExportEntityOp`, plus every entry in
`ScheduleInvokeOp.entity_arg_handles` — over `/rpc` via
`expose_entity` (on the per-script `AsyncExitStack`). The mutation
builder allocates slot numbers from `_slot_counter` (one per invoke,
in script order), rewrites `["entity_handle", handle]` placeholders
to `["entity", short_id]` and `["callback_handle", slot_key]`
placeholders to `["callback", N]`, emits one `InvokeOp` per invoke
(with `args=` and `kwargs=`) after the tracker diff. On commit the
stack releases all registrations — workstation's invoke worker
session-pins each entity arg, so they stay alive independent of our
/rpc reg lifetime. (The tracker diff on the same mutation covers
`exported_entity` refs only; invoke args aren't in `data_by_key` so
they're not in the tracker diff's scope.)

Result delivery: workstation's `invoke_worker` emits an
`InvokeResultEvent` (or `InvokeErrorEvent`), the MCP's
`_apply_state_effect` per-kind branch promotes the slot in
`mirror.data_by_key`, dedupe via `_notified_seqs`, OutgoingBuffer
delivery.

`retrieve_result(slot_key, result_type=None)` splits into two
stages (`_resolve_slot_to_fxtr_service_data` →
`_apply_result_type`):

- **Stage 1** is tag-keyed. `invoke_pending` raises `LookupError`;
  `invoke_error` raises `RuntimeError`; `exported_entity` → bare
  `FxtrID` (resolved via `lookup_entity`); `exported_service` →
  bare `ServiceRef[UnknownService]` with
  `_RemoteServiceHandleStub`; `callback_open` → bare
  `ServiceRef[UnknownService]` with
  `_OwnedCallbackHandleStub(slot_n=N)`; `invoke_result` →
  unescaped FxtrServiceData tree.
- **Stage 2** applies `result_type` uniformly. `None` → return as-is
  (with a slot-tag default for `callback_open` re-typing to
  `ServiceRef[WorkstationSessionEventInbox]` so the agent-visible
  shape matches what `create_callback` returned). Otherwise:
  `_ResultTypeSentinel` (from `result_type_of`) → decode via the
  layer-2 single-field return wrapper; any other annotation →
  `decode_from_service_data`.

`retrieve_callback_notification` runs the same two-stage pipeline
without the per-tag dispatch (payload is always
`invoke_result`-shaped; no slot-tag default).

### Callback flow

`create_callback(slot_key)` records a `CreateCallbackOp(slot_key)`
queued op and returns a `ServiceRef[WorkstationSessionEventInbox]`
whose handle is `_OwnedCallbackHandleStub(slot_key=slot_key)`. Passing
this ref to a `schedule_invoke` arg in the same script emits a
`["callback_handle", slot_key]` placeholder; the mutation builder
allocates a workstation slot for the create op (unified with
`schedule_invoke` slot allocation, monotonic in script order) and
rewrites the placeholder to `["callback", slot_n]` so the
workstation's op-validation pass resolves it against the newly-
created callback (workstation's async pre-commit phase brings the
callback up before commit, so it's visible to the same mutation's op
validation).

The ref is also a valid `schedule_invoke` *target*: the sandbox
queues a `ScheduleCallbackInvokeOp` (the target riding the same
placeholder grammar — `["callback_handle", k]` same-script /
`["callback", N]` cross-script) and the mutation builder emits a
workstation `CallbackInvokeOp(slot, callback_slot, ...)`. This is how
the agent invokes `subscribe_to` / `notify` / `listening` on its own
inboxes — including same-script create+subscribe in one mutation.

On commit, the mutation puts `["callback_open", N]` into
`data_by_key[slot_key]`. `apply_workstation_view` rebuilds the
`pending_slot_index` to include callback slots; subsequent
`CallbackEvent`s arriving on the wire are dispatched by
`_apply_state_effect`'s callback branch, which:
- looks up `slot_key` via `pending_slot_index[ev.slot]`,
- stores the wire-escaped payload in
  `mirror.callback_notifications[slot_key][str(ev.seq)]` + the
  parallel `_unflushed_callback_notifications[(slot_key, ev.seq)]`,
- **does not** modify `data_by_key` and **does not** drop the
  pending_slot_index entry (callbacks fire repeatedly),
- emits a notification with `kind="callback"` and
  `notif_index="<seq>"` (deduped via `_notified_seqs` the same way
  invoke results are). Routed through the `OutgoingBuffer` along
  with all other agent-facing notifications.

**Subscription connection state.** A callback inbox's `subscribe_to`
feed can break (its upstream source dies); the workstation reports
this as a seq'd `CallbackSubscriptionEvent(slot, connected, reason)`.
`_apply_state_effect`'s branch resolves `slot → slot_key` via
`pending_slot_index` (an open inbox keeps its entry), updates
`mirror.callback_subscription_states[slot_key]` to `"connected"` /
`"broken"` (latest-wins; absent = "idle"), and — **only** on the break
direction — fires a `kind="subscription_broken"` channel notice
(`_render_subscription_broken_notif`, naming the slot_key + reason),
deduped by seq via `_notified_seqs`. `connected=True` updates state
silently. An event whose slot is no longer open (closed/forgotten) is
dropped, not an error. `callback_subscription_states` is in-memory
only (not written to the workstation's persistent state): reset by
`apply_workstation_view`, rebuilt by replaying surviving events.

**`workstation_inbox` special case.** The reserved inbox slot is
the channel operators use to send messages into the agent's
session via `AgentMcpRunningSessionProto.send`. The dispatch
above is identical (same `pending_slot_index` lookup, same
`callback_notifications` storage, same `notif_index`), but the
emitted channel frame uses `kind="workstation_message"` and the
**full** payload as content — not the truncated `repr` summary
that ordinary callbacks get. `_render_workstation_inbox_notif`
formats it: string payloads are emitted verbatim (the common
case — operator typed plain text); non-string payloads are
rendered as the unabbreviated `repr` plus a brief hint at
`retrieve_callback_notification('workstation_inbox', <seq>)` so
the agent can fetch the typed object if they need to walk its
structure. The `CallbackNotificationLogEvent(slot_key=
"workstation_inbox", ...)` shape stored on the log chain is
unchanged — only the agent-channel rendering differs.

`callback_notification_keys(slot_key)` reads the keys via
`host.list_callback_notification_indices`;
`retrieve_callback_notification(slot_key, notif_index)` reads the
payload via `host.read_callback_notification` and unescapes it the
same way `retrieve_result` does for `invoke_result`.
`forget_callback_notification(slot_key, notif_index)` records a
queued op; the mutation builder emits a JSON Patch
`remove /callback_notifications/<slot_key>/<notif_index>`.

`forget(slot_key)` on a slot whose running value is
`["callback_open", N]` keeps its existing semantics
(`remove /data_by_key/<slot_key>`) **and** the mutation builder
appends a `CloseCallbackOp(slot=N)` to the wire ops list — the agent
gets a uniform "I'm done with this slot" API. The slot's stored
notifications are deliberately left in `callback_notifications` so
the agent can still retrieve them after the callback's send-side is
closed.

`callback_notifications` is a top-level field in the session state,
sibling of `data_by_key`. The workstation persists it transparently
(no schema); pre-flush emits one
`add /callback_notifications/<slot_key>/<notif_index>` per unflushed
entry. `desired_tracker_set` walks `callback_notifications` payloads
alongside `data_by_key` so entity refs surfaced in callback payloads
stay session-pinned past the workstation's event-log retention
horizon for the originating `CallbackEvent`.

### Slot-key overwrite check

`_apply_script_op` rejects any queued op that writes to a
`data_by_key` slot that's already populated — `export_entity`,
`export_service`, `schedule_invoke`, `create_callback` all require
the slot to be absent before the write. Raises
`SlotKeyAlreadyExists(slot_key, kind, running_value)`;
`_apply_script_mutation` catches it and surfaces a tool-error string
telling the agent to call `forget(slot_key)` in a prior script
first.

The check exists for callback safety specifically: a second
`create_callback("foo")` against an existing `["callback_open", N]`
would silently overwrite the `data_by_key` value with
`["callback_open", M]` and lose the old slot's
`pending_slot_index` entry. The orphaned callback is still alive
workstation-side and still firing; when its `CallbackEvent` arrives,
the MCP can't reverse-map slot N to "foo" and raises
`ProtocolError`. The check stops that at the source. Same rule
applies uniformly to other kinds — "explicit cleanup before reuse"
beats per-kind special cases.

The sandbox's `used_slot_keys` set blocks reuse within one script;
this host-side check covers the cross-script case where a prior
committed script's value (or a foreign mutator's write) still
occupies the slot.

### Race-safe `wait_for_slot_notification`

The tool peeks the local mirror at call time and short-circuits when
there's nothing to actually wait for. Logic per slot kind:

- `invoke_pending` → park on `watch_slot`. `after_notif_index` is a
  misuse here; rejected with a clear error.
- `callback_open` → consult `mirror.callback_notifications[slot_key]`:
  - No `after_notif_index`: if the inbox has any entry, return
    immediately with a hint pointing at the latest seq + the
    `retrieve_callback_notification` call.
  - With `after_notif_index=K`: if K isn't in the inbox, error (so
    the agent can't ask to wait "after" a seq they never actually
    received). If the inbox has seq > K, return immediately. If K
    is the latest, park for the next event.
  - The watch_slot mechanism is sync between the inbox peek and the
    watcher registration — anyio's cooperative scheduling means no
    notification can land between them.
- Terminal (`invoke_result` / `invoke_error` / etc.) → return success
  immediately ("slot was already resolved"). If the agent passed
  `after_notif_index`, a hint notes it's callback-only and was
  ignored.
- Absent → error.

`peek_callback_notifications` on `WorkstationConnection` exposes the
per-slot inbox as a shallow-copy dict for the tool's snapshot read;
same single-task-mutation safety property as `peek_slot_key`.

### Force-close recovery

`close_session(force=True)` is an emergency recovery tool that
bypasses the supervisor entirely. Implementation lives in
`force_close_session_one_shot` (module-level in `ws_connection.py`):

1. Open a fresh `fxtr_workstation_mcp.websocket_util.aconnect_ws(client_url)`.
2. Send `SubscribeSessionRequest`; receive ack + snapshot. The
   snapshot is discarded — we don't process any events.
3. Send `MutateSessionRequest` with `ops=[CloseOp()]`, `ack_seq=0`.
   The workstation prunes its event log against the cumulative ack
   across mutators, and we're tearing the session down anyway, so
   `ack_seq=0` is safe.
4. Await `MutationCommittedResponse` or `MutationRejectedResponse`.
   Either is success — rejection typically means the session was
   already closed.
5. Close the websocket.

The whole flow runs under `anyio.fail_after(timeout_seconds)`
(default 10s). If the workstation accepts the websocket
handshake but never sends the response frames we're awaiting
(server hung, network partition with TCP still half-open, etc.),
the budget elapses and we raise `ForceCloseTimedOut`. The tool
layer's generic `except Exception` branch surfaces the message
and **leaves the toml's `active_session` untouched** — we did
not successfully close, so we don't pretend the session is gone.
The agent retries once they believe the workstation is reachable
again. The `timeout_seconds` parameter is kw-only so tests can
shorten it.

On success the tool clears the toml's `active_session` and calls
`connection.notify_session_externally_closed()` to reset the
supervisor's `_active_session_id`. If the supervisor was subscribed
to the same session, its subscription will get `session_gone` on the
next operation and naturally tear down → reconnect → no active
session.

Use case: the supervisor is parked because subsequent_events replay
keeps crashing the worker before any agent tool drains. A normal
`close_session()` would queue behind the bad `_EventJob` and never
run. The one-shot websocket bypass works regardless of supervisor
state (parked, normal, crashing).

### `disconnect()` tool

`disconnect()` ends the current supervisor cycle and parks it in
**PAUSED** without closing the workstation-side session. The next
tool call wakes a fresh cycle that re-reads
`.fxtr_workstation.toml` (per the per-cycle toml refresh
described above) — so manual toml edits (notably swapping
`active_session`) take effect on the very next call. Does NOT
close the workstation-side session; that's `close_session` /
`close_session(force=True)` territory.

Mechanism:

- `WorkstationConnection.disconnect()` checks state. If PAUSED
  or EXITED, returns `"Already disconnected."` immediately. If
  READY or STARTING, fires `_disconnect_requested` and waits
  (condition-variable style) for the supervisor to leave
  READY/STARTING.
- `_run_inner` races `_shutdown` against `_disconnect_requested`
  via a wait task group. When disconnect wins it replaces the
  event with a fresh one and raises `_DisconnectRequested`.
- The supervisor's pause path catches the sentinel via
  `_find_disconnect_leaf` (walks the exception group), routes
  to PAUSED with the message verbatim as `pause_reason`, and
  **skips** the `supervisor_paused` channel banner.
- In-flight tool jobs get torn down through the same inner-TG
  cancel as a wire blip; their futures see `ConnectionLost`-
  shaped errors via `_handle_*_job`'s `BaseException` catch
  that sets the future before re-raising the cancellation.
  Caveat: when `disconnect()` fires *while a long-running
  `run_script` is mid-execution*, the harness shutdown after
  the test hangs forever — the disconnect mechanism itself
  reaches PAUSED correctly and the future fires, but something
  in the cancel-mid-script teardown chain doesn't terminate.
  The exact layer hasn't been isolated. Disconnect-during-flight
  is rare in practice; the relevant supervisor-state test is
  present but `pytest.mark.skip`'d as a marker for follow-up.

### Agent event log + user-message channel

Per session, the MCP maintains a typed, persistent record of
everything the agent does (and everything inbound it cares about),
plus a workstation-side service surface that lets a human-driven
frontend watch the log live and send messages back into the
session.

The pieces:

- **Typed entities** in `agent_log_types.py`:
  - `WsAgentLogEventChain(prev, event, timestamp)` — the head-only
    linked-list spine. Persistence cap holds *only the current
    head's id*; ancestors stay reachable via stored CBOR.
  - Per-kind event entities — `ScriptExecutionLogEvent`,
    `InvokeResultLogEvent`, `InvokeErrorLogEvent`,
    `CallbackNotificationLogEvent`. The `Log` infix avoids name
    collisions with `fxtr_workstation.client_protocol`'s wire-event
    types (`InvokeResultEvent`, etc.).
  - `ScriptOpRecord` (flat-shape `DataclassFxtrSubpart` with a
    string `kind` discriminator) — one per queued op on the
    committed script. Service refs in invoke args /
    `present_to_user` payloads land as
    `EntityRef[WorkstationServicePlaceholder]` (capturing
    `short_id` + `service_type_id` + `service_ctx`); callback refs
    land as `EntityRef[OwnedCallbackPlaceholder]`. Entity refs
    land as `EntityRef[UnknownEntity]` resolved against the
    workstation.
  - `ScriptPreamble(source)` — content-addressed via mashumaro so
    identical preambles share storage.

- **`AgentMcpRunningSessionImpl`** in `agent_mcp_impl.py` — the
  workstation-exposed service surface
  (`AgentMcpRunningSessionProto.get_state` / `subscribe` /
  `send`). One impl per session lives on a per-cycle `impl_scope`
  owned by `_agent_mcp_session_lifecycle` in `ws_connection.py`.
  Chain-head subscriptions are a coalesce-mode fxtr2
  `SubscriberHub` (`task_group=impl_tg`; the hub owns no scope —
  events ride per-event origin leases on `impl_scope`, where heads
  are stored): the proto is a genuine
  `EventSource[EntityRef[WsAgentLogEventChain]]` subclass (since
  fxtr2 stage 13 part 0), and the impl's `subscribe` delegates to
  `hub.subscribe(..., seed=[head], seed_origin=impl_scope)` (seed
  omitted while no head exists) — the impl retains the latest head
  itself and it arrives as the subscription's first event
  (replacing the old `watch_state` snapshot-return); `update_head`
  publishes through the hub with `origin_scope=impl_scope`
  (skip-ahead: multiple updates during one delivery collapse to
  the latest). Subscriptions don't pin `impl_scope`.

- **Log writer + intent stream.** Worker handlers
  (`_do_event_job` / `_do_script_job` / `_apply_script_mutation`)
  push `_LogIntent` records (sync, no awaits) onto an
  `anyio.MemoryObjectStream` owned by the lifecycle subtask. A
  separate `_log_writer_loop` task drains the stream: walks each
  payload via `log_walker.walk_with_context` into a typed
  `FrozenFxtrCbor` tree, builds the matching event entity into
  `impl_scope`, extends the chain with
  `log_builder.extend_chain`, pins to the persistence cap (if
  configured), and calls `impl.update_head` to wake watchers.

  The walker takes a `ServiceRef[WorkstationServiceProto]` (not
  a pre-resolved proxy bound to `impl_scope`) and re-resolves it
  into a fresh `FxtrScope(isolated=True, mountable=True,
  include=[impl_scope])` for each entity / service lookup, then
  copies the resulting FxtrID up into `impl_scope` via
  `copy_from_scope` before the temp scope drains. Two reasons:
  the lifted workstation registration's lifetime is bounded to a
  single lookup (no accumulation on the cycle-long `impl_scope`);
  and the resolve uses an explicit mountable scope rather than
  the active scope, which is `GLOBAL_SCOPE` here (the writer
  task was started via `impl_tg.start_soon` with no scope
  activation) and not mountable.

- **Bootstrap.** `_BootstrapAgentMcpJob` (cycle-start path) +
  `_do_bootstrap_agent_mcp_inner` (open_session inline path)
  share a helper:
  1. Look up the persistence cap by `agent_mcp_store` config
     (default `"agent_mcp_store"`; explicit `false` disables —
     log extends but doesn't pin).
  2. Create or reuse `workstation_inbox` callback (reserved
     slot_key, allowed only for this bootstrap via
     `allow_reserved_slot_keys=True` in
     `mutation_builder.build_mutate_request`).
  3. Fetch the inbox callback's `service_short_id` via
     `GetCallbackServiceRequest` (a dedicated `/client` request
     in the workstation, see *Client protocol* in
     `projects/fxtr_workstation/CODE_OVERVIEW.md`).
  4. If persistence is configured, `await persistence_proxy
     .try_retrieve(session_id)` to load the prior chain head.
  5. `inner_tg.start(self._agent_mcp_session_lifecycle, ...)` —
     the lifecycle opens `impl_scope` (`include=[host_scope,
     GLOBAL_SCOPE]` — `GLOBAL_SCOPE` is required because the log
     builder stores typed entities whose mashumaro decode chases
     type-descriptor entities tracked in `GLOBAL_SCOPE`), spawns
     the writer task, resolves the persistence cap as a proxy
     local to `impl_scope` (so pin's wire-encode of head
     `FxtrID`s succeeds), then `expose_service`s
     `agent_mcp:{session_id}`.

- **Graceful drain on close.** `_do_close_session` pushes a
  `_LogIntent_Shutdown` sentinel and waits on
  `_log_writer_done` before cancelling the lifecycle subtask, so
  any in-flight log intents (script executions, callback
  notifications received during the close-mutation's await) land
  on the chain before teardown.

- **`present_to_user(payload)`** is an MCP-side-only sandbox
  builtin (in `fxtr_ws_mcp_sandbox_bridge/builtins.py`): the
  payload is walked through `_walk_for_wire` (same vocabulary as
  `schedule_invoke` args — scalars, lists, dicts, `EntityRef`,
  `ServiceRef`, `bytes`), enqueued as a `PresentToUserOp` with
  the entity-arg handle list. Host-side: `_apply_script_op`
  returns early on `KIND_PRESENT_TO_USER`,
  `_register_exported_entities` registers the entity handles
  alongside `schedule_invoke` arg handles, and
  `BuildResult.substituted_runner_queued_ops` carries the
  post-substitution form for the log writer. When a script's
  only side effect is `present_to_user`, `build_mutate_request`
  returns `None` (no wire op needed) — the no-op path
  pre-substitutes via `mutation_builder.substitute_op_for_log`
  and pushes the log intent with the substituted ops so the
  present_to_user record reaches the chain.

### `packages/` layout convention

The agent's `<workdir>/packages/` dir is the single source of truth
for *both* run-script-importable code and code that gets bundled for
a remote sandbox spawner. Both paths agree on **src layout** as the
convention:

```
packages/<name>/
  pyproject.toml
  src/<name>/
    __init__.py
    ...
```

`build_runner_volumes` (in `sandbox_factory.py`) walks
`<workdir>/packages/` once per `run_script` call. For each immediate
subdirectory:

- If it has `pyproject.toml` *and* `src/`: add
  `/local-pkgs/<name>/src` to `sys_path_additions` so the package is
  importable from the sandbox script.
- If it has `pyproject.toml` but no `src/`: log a warning, add
  nothing to sys.path, and return the warning string in the third
  return value (a `list[str]`). `_run_script_primary` prepends those
  warnings to the `run_script` response so the agent sees them
  inline.
- Without a `pyproject.toml`: skip silently (so scratch dirs, caches,
  or non-Python assets don't trigger the warning).

The whole `packages/` dir is volume-mounted at `/local-pkgs`
(read-only) regardless — only the sys.path entries differ. The same
src-layout that makes a package importable here is what the flake's
`mkWasmService` builds a `fxtr.services.<name>` from (the git-ref
spawn path), so one layout serves both.

### Nix DATA entities (`retrieve_nix_entity` + `current_commit`)

The MCP's nix-data path bridges a nix-declared **DATA entity** into a
running script and lets the agent self-serve the workdir's HEAD commit.
Both are sandbox builtins backed by `McpHostProto` RPCs on `McpHostImpl`,
using the `fxtr_nix_runner` manager (nix) + `git_util.py` (git). Code (services
/ UI presets) does *not* travel this path — it's git-referenced and built by
the consuming service (see the git-ref spawn flow). Full design:
`projects/fxtr_workstation/plans/phase0/part5_nix_workdir/`
(`stage4_retrieve_nix_entity.md` + design notes 01/04/08/11).

**`retrieve_nix_entity(name, *, nix_location="fxtr.entities") -> FxtrID`.**
`McpHostImpl.retrieve_nix_entity` checks the workdir has a `flake.nix` (→ a
friendly `NixEntityBuildFailed`), then **delegates to the `fxtr_nix_runner`
manager's entities adapter** — the same build/mount path the publishing serve
uses, so the MCP is a normal consumer of the flake. It opens a `FlakeManager`
over the workdir (which discovers/mints the workdir's `.fxtr/` repo store) in a
`build_span` and calls `mgr.entities(span).retrieve(name, nix_location=<loc>,
into_scope=script_scope, copy_into_scope=True)`. The adapter evals
`<nix_location>.<name>`'s metadata; for a **layer entity** (`type ==
"fxtr_layer_entity"`) it builds + materializes the `.layer` and mounts the pin
via `mgr.mount_pin` — branding the layer arena on the repo store, so a
**remote-pointer closure resolves transparently** (what the old hand-rolled
reader couldn't do) — then copies it into `script_scope`. For a **builtin
entity** (`type == "builtin"`, e.g. `!basic_type` via `lib.fxtr.builtinEntity`)
it constructs the primitive `FxtrID` from `idString` directly — no build, no copy
(a primitive carries no content/arena lifetime, so it round-trips the return
bridge bare) — with a `get_tag()` guard rejecting a non-primitive (`!`/`.`) id
smuggled in via a hand-written builtin attr. `nix_location` (default
`fxtr.entities`, any attr under `fxtr`) is the adapter's optional location param;
the MCP is the only caller that passes a non-default value. Returns
`(fxtr_id, untracked_notice)`. The sandbox builtin prints the notice once and
returns the bare `FxtrID`. Resolution uses the **bare workdir path**
(auto-git/`path:`, non-frozen), so uncommitted edits to *tracked* files take
effect immediately while new **untracked files are invisible to nix** — hence the
count-only notice (`git ls-files --others --exclude-standard`). Failures map to
`NixEntityBuildFailed` (a missing name, a non-entity attr, an eval/build failure,
or a missing `layerKey`), carrying the full nix stderr — the compressed
tool-result shows only the first line, but `get_full_exception` retains the rest.

**`current_commit() -> str`.** `git_util.head_commit` returns HEAD's sha
with a clean-tree guard: a dirty tree (any uncommitted change *including*
untracked files) → `WorkingTreeDirty`; non-repo → `RuntimeError`. Used to
build `NixWasmServiceDefn` / preset args correct-by-default. Because the
runtime `.fxtr_workstation.toml` lives in the workdir, the agent_workdir
**`.gitignore`s** it (and `.tmp_outputs/`, `result*`, venvs, and the manager's
`.fxtr/` repo store) so it doesn't trip the guard / the untracked notice.

The deprecated in-band-bytes bundle path (`prepare_package_bundle` +
`fxtr_sandboxing.deprecated.prepare`) has been removed from the MCP
surface; `sandbox_factory.py` still uses `prepare` to assemble the MCP's
*own* sandbox-runtime dep closure, which is unrelated.

## Invariants

1. **Single state-mutation task.** All connection-level state
   (mirror, `_replayable_events`, `_pending_changes`,
   `_last_processed_seq`, `_slot_counter`, `_notified_seqs`,
   `_active_session_id`, `_active_subscription_id`) is mutated only
   by the worker task. The reader pushes onto the queue (GIL-atomic)
   but never touches connection-level state.

2. **Wire frame order is preserved through the queue.** The reader
   pushes `_EventJob`s in arrival order. The worker drains them in
   arrival order. Combined with the workstation's monotonic seq
   numbering, the worker sees events in strictly-increasing seq.

3. **`_replayable_events` is the source of truth.**
   `_pending_changes` (modulo its synthetic `"initialize"` prologue)
   and the effects on `mirror.data_by_key` +
   `mirror.callback_notifications` are all derivable from
   `_replayable_events` + the most-recent state_update.
   `_apply_state_effect` is the only function that mutates any of
   these.

4. **Redelivery is bit-identical to initial delivery.**
   `_apply_state_effect` is called the same way from `_do_event_job`
   (arrival) and from `_handle_state_update`'s rebuild loop
   (replay). Same dispatch, same derivations, same lookups must
   succeed. No "during_rebuild" flag. The dedupe inside
   `_maybe_notify` (driven by `_notified_seqs`) handles "don't
   re-surface to the agent" without forking the dispatch path.

5. **`mirror.data_by_key` is the local view.** Always equals the
   workstation's `state["data_by_key"]` (carried by the last applied
   state_update) with the effects of every event in
   `_replayable_events` layered on top. `mirror.pending_slot_index`
   is a derived reverse-lookup (`slot → slot_key`) maintained
   consistent with `data_by_key`'s pending markers (`invoke_pending`
   / `callback_open`). `mirror.callback_notifications` is the
   parallel local view of the workstation's
   `state["callback_notifications"]` sibling field, with
   `CallbackEvent` payloads layered on top per the same
   `_replayable_events` model.

6. **Self-commit's outgoing ack_seq subsumes every event in
   `_replayable_events`.** We send `ack_seq = _last_processed_seq`,
   and every event in the list has `seq <= _last_processed_seq`
   (since `_apply_state_effect` is the only writer and
   `_do_event_job` advances the seq past every event it processes).
   So `_apply_local_commit` can clear the list and the derived dict
   directly — no rebuild loop needed.

7. **`_handle_state_update` does not touch `_last_processed_seq`.**
   The caller advances inline (foreign-event arm, subscribe
   bootstrap) or defers via `_AckCommitJob` (self-commit, so that
   any foreign events queued during the response await drain first
   and we never ack ahead of locally-processed work).

8. **State_updates are strictly monotonic per session.** Enforced
   by `_handle_state_update`'s and `_apply_local_commit`'s
   regression check: `ev.seq > mirror.last_state_seq` (or the
   no-prior-state branch where `last_state_seq == 0`).

9. **Outgoing mutations carry `ack_seq = _last_processed_seq`.**
   The MCP only acks events it has fully processed locally. The
   workstation prunes its event log against the cumulative `ack_seq`
   it has seen from all mutators.

10. **Channel notifications are deduped by seq for the session's
    lifetime.** `_maybe_notify` (called from inside
    `_apply_state_effect`'s per-kind branches) only delivers if the
    seq isn't already in `_notified_seqs`. The set persists across
    reconnects (the `_do_subscribe` wipe block deliberately doesn't
    touch it) and is pruned alongside the ack horizon. Reset only on
    session close.

11. **Bare-dispatch teardown.** `_dispatch_job` has no exception
    handling. Per-tool-call handlers catch specific tool-error types
    (`SessionAlreadyActive` / `NoActiveSession` /
    `WorkstationRejected`) and route them to the completion future;
    anything else propagates → supervisor cycle exits. The
    supervisor's main except classifies the failure as wire-blip
    free-retry (silent) or PAUSED (banner + `pause_reason`). Queued
    tool-call jobs are only failed with `ConnectionLost` on lifespan
    exit, by `_drain_pending_jobs_and_requests`.

12. **Sandboxed RPC dispatches are children of `script_scope`.** A
    script calling `lookup_entity` runs on an RPC-owned task whose
    parent scope is the per-script scope; it fully drains before the
    worker resumes script-end work.

13. **Supervisor task only exits on shutdown or cancellation.** No
    non-cancellation exception escapes `_supervisor_loop`. Wire
    blips after a cycle reached READY consume one from
    `_free_retry_budget` and retry silently; all other failures
    (pre-ready, non-wire, exhausted budget) route through PAUSED.
    The MCP never dies because of a connection-level error.

14. **`pause_reason` is the single source of truth for "why
    paused."** Written by `_set_state(PAUSED, reason)`. Read by
    `wake_and_wait_ready` (raised as `SupervisorNotReady(reason)`)
    and by `_maybe_notify_supervisor_paused` (channel banner
    content). No exception objects live in the state machine.

## Outstanding TODOs

- **TODO: Per-kind dispatch inside `_apply_state_effect`** today
  handles `InvokeResultEvent` / `InvokeErrorEvent` and
  `CallbackEvent`. The other non-state event kinds
  (`LoadResultEvent` / `LoadResultSilentEvent` / `LoadErrorEvent` /
  `EntityMetadataResultEvent` / `EntityMetadataErrorEvent` /
  `ServiceMetadataResultEvent` / `ServiceMetadataErrorEvent`) still
  raise `NotImplementedError` — an arrival of one of these pauses
  the supervisor with `pause_reason = "unimplemented handler: ..."`
  (the agent recovers via `close_session(force=True)`). Each handler
  is the same shape as the invoke branches — slot_key lookup via
  `mirror.pending_slot_index`, write to data_by_key + unflushed
  dict, drop the index entry on promote. Fill these in as the agent
  gains the corresponding builtins (load-via-session-load, metadata
  queries).

- **TODO: Reconnect-during-mutation-with-uncertain-commit** — a
  mutation we sent but didn't get an ack for may or may not have
  committed. Currently we accept the ambiguity (the next snapshot
  tells us which). A cleaner design would attach an idempotency key
  and check the snapshot's event log for our `update_id`. Surface
  area is small enough that the ambiguity is acceptable for now;
  revisit if it bites.

- **TODO: Duplicate `WorkstationSessionEventInbox`.** A sandbox-local
  copy lives in `fxtr_ws_mcp_sandbox_bridge.protocols` (so the bridge
  can reference it without taking a `fxtr_workstation` dep), and a
  third copy is re-exported into `demo_pkg.typed_protos` for
  cross-side typing in the demo. (The fxtr2 subscription primitives
  are NOT duplicated — fxtr2-portable ships
  `fxtr2.services.subscription_types`.) Operationally harmless (the dialect strips
  `target_type` to `UnknownService` on encode and the owned-callback
  wire form doesn't carry a `service_type_id`); cosmetic dup.
  Cleanup option: extract into a tiny shared package both host and
  sandbox can depend on, then drop the duplicates.

## How tests work

`pytest-anyio` with the asyncio backend. Two flavors of integration
test, distinguished by how the MCP server's tool callables are
reached:

1. **Direct-call**: most files (`test_invoke_e2e.py`,
   `test_callback_e2e.py`, `test_export_pipeline_e2e.py`,
   `test_run_script_lookup_entity.py`, `test_script_error.py`,
   `test_run_script_wait_seconds.py`, `test_stage7_buffer_e2e.py`,
   `test_supervisor_state_machine.py`, `test_typed_service_e2e.py`)
   boot a real `WorkstationServer` in-process, write a
   `.fxtr_workstation.toml` pointing at the bound URL, and call the
   tool functions (`set_working_dir`, `connect`, `open_session`,
   `run_script`, …) directly with a small `_FakeContext` wrapper
   supplying the `ctx.request_context.lifespan_context` shape
   FastMCP tools expect. Skips JSON-RPC framing + FastMCP dispatch;
   everything else is real.
2. **In-memory transport**: `test_mcp_in_memory.py` runs the
   package's FastMCP server in-process over a pair of
   typed-`SessionMessage` streams from
   `mcp.shared.memory.create_client_server_memory_streams`. A raw
   JSON-RPC client (`_MemoryClient`) sends `tools/call` /
   `initialize` over those streams and collects responses +
   notifications by message shape. This adds real FastMCP tool
   dispatch + `SessionMessage` (de)serialization + the
   `ServerSession._write_stream.send(...)` notification path (which
   `notifications/claude/channel` frames ride on) to the coverage
   matrix. No subprocess — task group cancellation handles teardown
   cleanly. `test_in_memory_e2e_nix_spawn` is the git-ref spawn E2E:
   it `prepare_workdir_repo`s the demo workdir, boots a workstation
   whose `sandbox-spawner` external service is a
   `create_sandbox_environment(..., git_repo_path=<that workdir>)`,
   then drives a `run_script` flow that constructs a
   `NixWasmServiceDefn(git_commit=await current_commit(), …)` in-sandbox
   (the agent self-serves the commit), spawns `fxtr.services.hello_service`
   (built from the workdir git ref), connects, and calls `greet()`.
   `test_in_memory_e2e_retrieve_nix_entity` is the DATA-path E2E: against a
   bare workstation + prepared workdir it `retrieve_nix_entity("demo_data")`s
   + loads the entity, `retrieve_nix_entity("basic_type")`s a builtin entity
   (a primitive `!basic_type` FxtrID, no build), reads HEAD via
   `current_commit`, and exercises the `NixEntityBuildFailed` /
   untracked-notice / `WorkingTreeDirty` surfaces.
   `test_in_memory_e2e_nix_set_preset` is the preset-path E2E *through the
   agent wire*: its `_running_workstation_with_nix_dispatcher` fixture mounts
   a `NixConfigurableDispatchImpl(repo=<prepared workdir>)` (no initial
   preset) as the `renderer-dispatcher` external service, then a `run_script`
   looks the dispatcher up, `replace_service_type(NixDispatcherProto)`s it,
   and `schedule_invoke`s `set_preset(await current_commit(),
   "…uiPresets.demo_preset")`; the test asserts `mounted.impl.current_ref`
   (set only after the table swaps) + resolves the demo_data type to real ESM
   bytes through a proxy into the impl's scope. This is the agent-path
   counterpart to the direct test below.
3. **Nix UI-preset integration**: `test_nix_dispatch_preset.py` is the
   preset-path counterpart (no MCP/RPC — drives the dispatcher directly).
   It `prepare_workdir_repo`s the demo workdir, constructs a
   `NixConfigurableDispatchImpl(repo=<workdir>)`, mounts + resolves a
   `NixDispatcherProto` proxy, and `set_preset(<HEAD>,
   "fxtr.uiPresets.demo_preset")` — the real batched `nix build`
   (esbuildRenderer ESM + `typeEntity` layer) + eager key validation + a
   `RendererEntity` minted at serve time + the reload broadcast — then
   `resolve`s the demo_data type to a renderer loading to real ESM bytes.
   Slow + `@pytest.mark.deadline(seconds=900)` (the `demo_data` host venv is
   the heavy cold build). `test_mcp_in_memory.py::test_in_memory_e2e_nix_set_preset`
   (above) exercises the same preset path through the full agent stack.

The `examples/basic_test_demo/agent_workdir` doubles as the workdir
fixture for the in-memory tests; same constraint as before —
anything that breaks the manual demo also breaks the test. (Its
`flake.nix` declares `fxtr.services.hello_service` for the nix-spawn E2E,
`fxtr.entities.demo_data` for the retrieve E2E, and
`fxtr.uiPresets.demo_preset` for the preset integration test.)

`conftest.py` installs a SIGALRM-based per-test timeout (lifted from
the workstation's tests) — catches deadlocks that anyio
cancellation alone wouldn't unstick.

Shared event-parsing helpers (`tests/_events.py`) handle the fact
that notifications can land in *either* a tool's drained result
string or in a debounced `notifications/claude/channel` frame:
`parse_events(text)` pulls `<event …>…</event>` blocks out of any
string, and the test ctx-shims accumulate every tool's return value
so a single `wait_for_…` helper can search both sources.
`test_outgoing_buffer.py` carries the unit tests that exercise the
buffer in isolation (no workstation). `test_tool_logging.py`
similarly exercises the `@logged_tool` decorator in isolation,
patching `server._VERBOSE_IO` per-test and asserting on
`capsys`-captured stderr.
