# Part 2 — Workstation MCP plugin (overview)

The agent-facing client for the workstation: a Python MCP server that an
LLM agent loads as a Claude Code plugin. The agent drives a persistent
workstation session by writing Python that runs inside an eryx WASM
sandbox; the MCP brokers between the sandbox and the workstation server,
applying the agent's intent as one ACID-style mutation per script.

This part assumes part 1's workstation server is running. It does not
extend the workstation's wire protocol — every operation surfaces in the
existing `/rpc` and `/client` endpoints.

See also:

- [`../sketch.md`](../sketch.md) — the broader workstation vision
  (defines the agent-MCP role at a sketch level).
- [`./sketch.md`](./sketch.md) — the part-2 starting brief.
- [`../part1_ws_server/`](../part1_ws_server/) — the workstation
  server design these plans build on.
- [`spikes/mcp_plugin_spike/`](../../../../mcp_plugin_spike/CODE_OVERVIEW.md)
  — the prior sandboxed-execution spike whose machinery we adapt.

## Goals

- One Python package that runs as an MCP server (FastMCP, stdio
  transport) and bridges a persistent workstation session to a Claude
  Code agent.
- Sandboxed Python script execution as the primary agent-facing
  affordance; the working-dir contract from the spike is preserved
  (`preamble.py`, optional `packages/`) plus a new
  `.fxtr_workstation.toml` carrying connection + active-session state.
- Two long-lived workstation connections from the MCP process: a single
  `/rpc` connection (workstation main service mounted as remote, used
  for `register_service` / `copy_entity` / `get_entity` / `get_service`)
  and a single `/client` connection (custom session protocol, used for
  the active session's subscribe + mutate pipeline).
- Each `run_script` call is realized as exactly one
  `MutateSessionRequest` carrying the script's queued ops + JSON Patch
  + tracker diff. Atomic on the workstation side.
- Receive-side events (`invoke_result`, foreign `state_update`) are
  always acked into the session's persistent state via a follow-up
  mutation, then surfaced to the agent via the
  `notifications/claude/channel` mechanism.
- Resource hygiene: per-call sandboxes drain on script exit; transient
  RPC registrations release after the workstation has the data
  (immediately after `mutation_committed` for `export_*`; after the
  matching result event lands for `invoke` args).

## Non-goals (for part 2)

- Typed `ServiceRefStub[T]` / typed `retrieve_result(..., type=T)` —
  v1 is `UnknownService`-only on the sandbox side; results come back as
  raw `FxtrServiceData`.
- Callback support (`["callback", N]`).
- Bundling locally-defined Python packages as workstation-shipped
  sandboxable artifacts.
- The workstation-server changes in part 1 (no wire-protocol extensions
  here).

## Resource model

```
fxtr_workstation_mcp process
├── workstation /rpc connection (RPCSession, peer = WorkstationServiceProto)
│   └── transient registrations (export_*, invoke args) per-script
├── workstation /client connection (ClientWS) + connection_worker:
│   ├── reader task: parses ServerFrames, routes by id (resolves
│   │   pending-request futures; enqueues SessionEventFrames as
│   │   worker jobs)
│   ├── worker task: drains a single job queue, runs each job to
│   │   completion before pulling the next
│   ├── job queue: tagged union — MutationJob / SubscribeJob /
│   │   ScriptJob / EventJob
│   ├── pending requests: request_id → AnyioFuture (ack / committed
│   │   / rejected / created / etc.)
│   └── one active SessionRecord (uuid pinned in
│       .fxtr_workstation.toml)
├── eryx SandboxFactory (built once at startup from sandbox-runtime
│                        dep closure, ~3s)
├── DirectSandboxEnvironment (long-lived; one open_sandbox per script)
├── host_scope (FxtrScope, mountable, isolated, includes workstation
│              /rpc connection's user scope so McpHostImpl methods can
│              reach `get_entity` / `get_service`)
│   └── McpHostImpl (mounted; bridged into each sandbox via host_ref)
└── per-script script_scope (FxtrScope, mountable, isolated, child of
    host_scope; opened by sandbox_runner per run_script call; holds
    copied-out entities introduced by export_entity / schedule_invoke;
    drains on script exit)
```

The `connection_worker` is **two** anyio tasks under one TaskGroup:
**reader** (demultiplexes incoming frames) and **worker** (drains a
single job queue). Splitting reader from worker is mandatory — if
the same task did both, an RX-driven follow-up mutation (e.g.
acking an invoke_result into state) would deadlock waiting for a
response the same task is supposed to deliver.

The job queue is the only serialization point. Job kinds:

- **MutationJob** — send a mutation, await its
  `mutation_committed` / `mutation_rejected` future, complete.
- **SubscribeJob** — send a subscribe / unsubscribe, await ack +
  initial snapshot if applicable, complete. (Used by `connect`,
  `open_session`, `close_session`, reconnect.)
- **ScriptJob** — run the per-script sandbox, register copied-out
  entities over `/rpc`, build + send the mutation, await response,
  release transient registrations, fire any channel notifications.
  Tool calls (`run_script`) submit one of these and `await` the
  job's completion future. This is the answer to "two `run_script`
  calls simultaneously": they enqueue and serialize on the worker.
- **EventJob** — handle a single SessionEventFrame: build a
  follow-up mutation if needed (foreign state_update reload,
  invoke_result/error promotion into state), submit it through the
  same worker, fire channel notifications. The reader task
  enqueues these.

Reader-side work that does *not* go through the queue: resolving
ack/error/committed/rejected/snapshot/created/createrejected frame
futures (single-frame demultiplex; no follow-up logic).

Host RPC methods invoked by the sandbox (`lookup_entity`,
`copy_entity_in`, `read_state_slot`) are *not* worker jobs — they
run on their own per-call dispatch tasks driven by the workstation
`/rpc` and the sandbox connection. They interact with `script_scope`
and the local state mirror but never enqueue worker jobs of their
own. (The state mirror is dict-typed with immutable tuple values;
reads are GIL-atomic, writes happen only inside the worker.)

## Resolved design decisions

These were settled in the design discussion before this overview was
written; recorded here so the plan stands alone.

- **Tracker set as derived state.** Every mutation we send recomputes
  the desired tracker set by walking the post-patch `data_by_key` for
  `["entity", id]` / `["service", id]` short_ids. We diff against the
  workstation's current `tracked_entities` / `tracked_services` and
  emit `track_*` / `untrack_*` ops to converge. No imperative
  agent-facing track/untrack API.
- **No auto-track for invoke args.** Args are kept alive by the RPC
  registration we hold before submitting the mutation; the workstation
  dispatcher's leases keep them alive across the worker's call. We
  release the registration only after the matching result event lands.
- **One mutation in flight at a time.** Enforced by the
  `connection_worker` queue. Concurrent tool calls or RX-driven
  follow-ups serialize behind it.
- **Channel-notify the agent only after a result has been promoted into
  state.** `invoke_result` arrival → follow-up mutation that updates
  `data_by_key` + advances `ack_seq` + applies tracker diff →
  `mutation_committed` → only then send the channel notification. The
  agent never sees a result that the session log might still drop.
- **Foreign `state_update` triggers a snapshot reload.** We discard
  cached state, reinitialize from the new `last_state.state`, and
  channel-notify the agent with a one-line summary of `data_by_key`
  (abbreviating `invoke_result` payloads). Under normal use this is
  rare (the MCP is the only writer); it covers connect-time and the
  human-edits-via-debug-UI case.
- **Per-script transient registrations split by op-kind.** For
  `export_entity` / `export_service` we release the RPC registration
  immediately after `mutation_committed` (the session-pin holds the
  ref alive). For `invoke` args we hold the RPC registration until
  the matching result event has been promoted into state (or the
  invoke errored). This matches the workstation's "args alive until
  dispatch" contract via the RPC lease layer.
- **Mutation-rejection rolls back the script.** If our final mutation
  is rejected (`validation_failed`, `stale_ack`, `conflict`, etc.) we
  drop all queued exports + invokes, release any RPC registrations
  we'd opened, and surface the rejection's `message` as the
  `run_script` tool result.
- **Sandbox builtin module name.** Unchanged from the spike:
  `_fxtr_mcp_builtin`. The host can swap which closures it injects
  per call; the sandbox-side import path stays stable.
- **Short IDs are opaque strings to the agent.** No client-side
  prefix/suffix parsing. We faithfully echo the workstation's allocator
  output.
- **Plugin / channel / skill / source names.** Plugin name
  `fxtr_workstation`, channel name `fxtr_workstation`, MCP server
  name `workstation_mcp`, skill name `use_fxtr_workstation`. Channel
  notifications render as `<channel source="workstation_mcp" …>` —
  the channel `source` attribute is the server name, not the
  channel name (matches the spike's behavior). The skill ships as a
  stub for v1; fleshed out later.
- **Per-script `script_scope` for copied-out entities.** Each
  `run_script` opens a fresh isolated mountable scope as a child of
  `host_scope`. Entities the script exports / passes-as-args are
  copied (via `copy_to`) from the sandbox-call dispatch scope up
  into `script_scope` by the host-side `copy_entity_in` RPC method,
  so they survive past the per-RPC-call dispatch scope. The
  workstation `expose_entity` registration is then resolved in
  `script_scope`; on script exit the scope drains, releasing
  whatever's still there.
- **State mirror reads are GIL-atomic; writes serialized.** The
  local mirror is a plain `dict[str, tuple]`. Reads from host RPC
  methods (`read_state_slot`) are GIL-atomic and value contents
  are immutable tuples. Writes only happen inside the
  `connection_worker`'s event_handler / response path — no
  inter-task contention. We don't add a lock.

## Wire-format conventions

All wire shapes the MCP touches are already defined elsewhere; we just
reuse them.

- Workstation `/rpc` JSON-framed `RPCMessage`s — same as the spike, via
  `wrap_json_stream` over a websocket.
- Workstation `/client` JSON frames — `msgspec`-defined in
  `fxtr_workstation/client_protocol.py`. We import the request /
  server-frame types directly.
- `data_by_key` payload values are stored on the workstation in their
  list-based-escape form for entity / service refs (i.e. the same
  shape we receive in `invoke_result`). When the agent's
  `retrieve_result` walks them, the host re-escapes nothing — it just
  recursively unwraps `["entity", short_id]` / `["service", short_id]`
  / `["bytes", b64]` / literal-list-escape forms.

## Server-side session state JSON

The workstation session's `state` (a free-form JSON dict) is the only
place persistent agent-relevant state lives. We adopt a fixed shape:

```json
{
  "data_by_key": {
    "<agent_key>": ["exported_entity",  "<entity_short_id>"],
    "<agent_key>": ["exported_service", "<service_short_id>"],
    "<agent_key>": ["invoke_pending",   <slot:int>],
    "<agent_key>": ["invoke_result",    <wire-escaped-payload>]
  }
}
```

The MCP also caches a *local* mirror of this same shape so the
sandbox's read-only builtins (`retrieve_result`, `get_service`,
`get_id_for_service`) can answer synchronously without round-tripping
through the workstation.

`invoke_result` payloads are stored *as received* (already
list-based-escaped). `retrieve_result` walks them lazily.

## File layout

```
projects/fxtr_workstation_agent_plugin/
  README.md                       end-user docs (loading the plugin,
                                  toml shape, sample workdir, smoke
                                  walkthrough)
  CLAUDE.md                       package-local "how to work in here"
  CODE_OVERVIEW.md                architecture / how things fit together

  fxtr_workstation_mcp/           host-side Python package
    pyproject.toml
    fxtr_workstation_mcp/
      __init__.py
      server.py                   FastMCP entry, lifespan, tools
      app_context.py              AppContext dataclass + lifespan
                                  helpers (state, env, host_scope, …)
      config.py                   .fxtr_workstation.toml read/write
      ws_connection.py            workstation /rpc + /client connection
                                  manager, connection_worker
      session_state.py            local mirror of session state +
                                  derived tracker-set computation
      mutation_builder.py         turn queued sandbox ops into a
                                  MutateSessionRequest + tracker diff
      event_handler.py            RX-side dispatch for SessionEvents
      host_impl.py                McpHostImpl bridged into the sandbox
      sandbox_runner.py           per-script sandbox driver: build
                                  builtins, run script, register
                                  entities, send mutation, release
      sandbox_factory.py          eryx factory bootstrap (adapted from
                                  the spike) + volume builder
      channel.py                  notifications/claude/channel helper
      protocols.py                shared wire protos that import from
                                  fxtr_ws_mcp_sandbox_bridge.protocols
    tests/
      __init__.py
      test_config.py
      test_session_state.py
      test_mutation_builder.py
      test_event_handler.py
      test_sandbox_integration.py
      test_full_session.py        end-to-end: workstation + MCP +
                                  sample scripts

  sandbox/                        only loaded into sandboxes, not host
    fxtr_ws_mcp_sandbox_bridge/
      pyproject.toml              fxtr2-portable + minimal deps
      fxtr_ws_mcp_sandbox_bridge/
        __init__.py
        protocols.py              host service proto + runner proto +
                                  RunnerConfig dataclass
        entry.py                  fxtr.service entry point (`run`);
                                  mounts the per-call _GuestImpl
        script_runner.py          stdlib exec primitive (port from spike)
        builtins.py               the _fxtr_mcp_builtin closures:
                                  lookup_entity, get_service,
                                  export_entity, export_service,
                                  schedule_invoke, retrieve_result,
                                  forget; queue records returned to
                                  host

  claude_plugin/                  what Claude Code loads
    .claude-plugin/
      marketplace.json            local marketplace catalog
      plugin.json                 manifest, declares the channel
    skills/use_fxtr_workstation/SKILL.md   stub instructions for v1
    .mcp.json                     stdio launcher: uv run workstation-mcp
```

`fxtr_workstation_mcp` declares `fxtr-ws-mcp-sandbox-bridge` as a path
dependency so the host can import the shared protocols. The bridge
package is also volume-mounted into each sandbox at run time
(`build_runner_volumes` returns the volume + sys-path additions, same
pattern as `_fxtr_mcp_runner` in the spike).

## Sandbox builtins (v1)

All exposed via the per-call `_fxtr_mcp_builtin` module. Sync ones don't
hit the workstation; async ones round-trip via `host_ref`.

- `async lookup_entity(short_id: str) -> FxtrID` — host method calls
  `workstation_main.get_entity(short_id)`; the FxtrID is bridged across
  both connections back into the sandbox connect-dispatch scope.
- `def get_service(short_id: str) -> ServiceRefStub` — pure-local
  metadata wrapper. v1 stubs are untyped (UnknownService).
- `def get_id_for_service(stub: ServiceRefStub) -> str` — metadata.
- `async export_entity(slot_key: str, entity: FxtrID | EntityRef[Any])`
  — copies the entity into a host-side scope (synchronously over the
  sandbox's RPC channel) and queues an `("export_entity", slot_key,
  copied_entity_handle)` op record. Errors if `slot_key` already in
  use this script.
- `async export_service(slot_key: str, stub: ServiceRefStub)` — queues
  an `("export_service", slot_key, short_id)` record.
- `async schedule_invoke(slot_key, stub, action_lambda)` — runs the
  lambda against a recording proxy that captures
  `(method_name, args)`. Args are walked recursively; `FxtrID`s and
  `EntityRef`s are copied out to the host scope; `ServiceRefStub`s
  reduce to their short_ids; nested lists/dicts/scalars/bytes pass
  through. Queues an `("schedule_invoke", slot_key, stub_id,
  method_name, captured_args, copied_entities)` record. Errors if
  `slot_key` collides; rejects multi-method or zero-method lambdas.
- `async retrieve_result(slot_key: str, _type=None)` — reads the local
  state mirror. If the slot is `("invoke_pending", _)` raises
  `LookupError("result not yet available")`. If `("invoke_result",
  payload)` walks the payload recursively: `["entity", id]` →
  `await lookup_entity(id)`; `["service", id]` →
  `ServiceRefStub(id)`; `["bytes", b64]` → bytes;
  literal-list-escape unwraps. v1 ignores `_type`.
- `def forget(slot_key)` — queues `("forget", slot_key)`. Nothing else.

The script's queued op list is returned to the host along with a
collection of copied-out entities (bound to host-scope-loadable
handles).

## End-of-script mutation flow

When `runner.run()` returns to the host:

1. Walk the queued ops, gather all (entity-handle, op-purpose) pairs
   where `op-purpose ∈ {export, invoke_arg}`.
2. For each entity handle, run `expose_entity(workstation_ref,
   external_name=f"mcp-tmp-{uuid4()}", entity_id, scope=host_scope)`
   to register over the workstation `/rpc` connection. Collect the
   resulting `(short_id, release_cb)` pairs.
   - `expose_entity` is the async-CM helper; we *don't* exit it here.
     We hold each one open and exit explicitly later.
3. Build the `MutateSessionRequest`:
   - `patch`: a JSON Patch sequence over `data_by_key` reflecting all
     `export_*`, `invoke_pending` slot entries to add, and `forget`
     keys to remove.
   - `ops`: tracker diff (computed against the post-patch desired
     tracker set vs the workstation's current `tracked_entities` /
     `tracked_services`, both visible from our cached state and the
     last-known snapshot) followed by `invoke` ops in deterministic
     order.
   - `ack_seq`: current cached `last_state.ack_seq` (no event yet).
   - `update_id`: monotonic counter on `AppContext`.
4. Submit via `connection_worker.send_mutation(request)`. Await the
   future:
   - `MutationCommittedResponse` → success.
   - `MutationRejectedResponse` → bail; release all RPC registrations
     by exiting their async-CMs; surface the `(reason, message)` as
     a tool error string.
5. On success:
   - For each `export_*` registration: exit the async-CM, releasing
     the registration. The session's `track_*` op now keeps the ref
     alive.
   - For each `invoke` op's arg-registrations: stash them in
     `AppContext.in_flight_invoke_regs[slot]` keyed by the workstation
     slot the mutation consumed. They release when the matching
     `invoke_result` / `invoke_error` event has been promoted into
     state by the event handler (steps under "RX-side handling").
6. Update the local state mirror to match what we just committed
   (mutator-side has the post-state already).
7. Return a tool-result string to the agent — a brief description of
   what was applied (per-key declared / scheduled-invoke summary in
   the spike's spirit).

## RX-side handling

The single `connection_worker` reads frames continuously. Dispatch:

- `MutationCommittedResponse` / `MutationRejectedResponse` →
  resolve the matching `request_id` future. (Producer is the active
  mutation submitter.)
- `AckResponse` / `ErrorResponse` → resolve / reject futures for
  non-mutation requests (subscribe / create_session / unsubscribe).
- `SessionSnapshotEvent` → set the local state mirror from
  `last_state.state` + replay any non-state events in `events` (under
  v1 these are only `invoke_result` / `invoke_error`).
- `SessionEventFrame` carrying:
  - `StateUpdateEvent` (foreign mutator) → discard cached state,
    re-initialize from `state`, channel-notify the agent with a
    one-line summary "session state was reloaded; data_by_key has
    keys=[…]". Intentionally minimal — the agent re-orients via its
    next `run_script`.
  - `InvokeResultEvent(seq, slot, payload)` → enqueue a follow-up
    mutation that:
    - patches `data_by_key[key]` (by reverse-lookup from
      `slot → key` in the local mirror) to
      `["invoke_result", payload]`.
    - sets `ack_seq = seq` (advances retention horizon past this
      event).
    - includes the tracker diff (entities/services newly referenced
      by the payload need `track_*` ops; entities/services no longer
      referenced anywhere become candidates for `untrack_*`).
  - `InvokeErrorEvent(seq, slot, message)` → analogous. The slot's
    entry in `data_by_key` is removed (or rewritten to
    `["invoke_error", message]` — TBD during stage 4 design; v1
    leans toward removal + agent surfaces error via channel).
  - `LoadResultEvent` / `LoadErrorEvent` / `CallbackEvent` —
    not produced under v1's MCP usage (we never emit `LoadEntityOp`
    / `CreateCallbackOp`). Treat as a soft assertion: log + ignore.
- After the follow-up mutation commits, the event handler:
  - Releases the in-flight RPC registrations stashed for that slot.
  - Sends a `notifications/claude/channel` notification of the form
    `<channel source="fxtr_workstation" slot="<key>">…summary…</channel>`.

## Connection startup + reconnect

- `connect` (and any tool that needs the workstation) runs through
  `ensure_connected(app_ctx)`:
  - **Requires `set_working_dir` to have been called first.** The
    toml lives in the working dir, so without one we have nothing
    to read. Errors with a clear message if not.
  - If both connections are open and the session subscription is
    active, no-op.
  - Else: open the `/rpc` connection (resolve workstation main service
    into host_scope's parent chain). Open the `/client` connection.
    Send `name_connection("workstation_mcp ...")` over `/rpc` for
    debug-UI labelling.
  - If `.fxtr_workstation.toml` has `active_session`, enqueue a
    `SubscribeSessionJob`. If the workstation rejects (session gone
    / unknown), surface the error to the agent with the suggestion
    to manually clear the toml (no auto-clear).
- `open_session` errors if `active_session` is already set in the
  toml. Else issues `CreateSessionRequest(metadata={...})`, writes
  `active_session = uuid` to the toml, then subscribes. Fresh
  sessions start with `state = {}`; the MCP treats a missing
  `data_by_key` key as the empty dict everywhere — no synthetic
  initialize-state mutation needed.
- `close_session` errors if `active_session` is not set. Else issues
  a `MutateSessionRequest` containing only `CloseOp`, awaits
  `mutation_committed`, removes `active_session` from the toml.

### Auto-reconnect on connection drop

Connection drops are *in scope* for v1 — the workstation's session
protocol is designed to support exactly this case (rejoin a session,
reload state, in-flight invoke workers re-deliver their results
when ready, abandoned RPC registrations clean up automatically).

- The reader task detects WebSocket EOF / error. It signals
  reconnect to the worker.
- If a `ScriptJob` / `MutationJob` was in flight at the time, its
  in-progress request (mutation, subscribe, etc.) errors back to
  the awaiting tool call with a "connection lost during call"
  message. The tool call surfaces the error; the script's
  copied-out entities + transient RPC registrations drain on the
  per-script `script_scope` exit.
- Outside of an in-flight tool call, the worker:
  - Closes the old connections.
  - Reopens `/rpc`, re-resolves the workstation main service.
  - Reopens `/client`, sends a fresh `SubscribeSessionRequest`
    against the same UUID. Receives the `SessionSnapshotEvent`,
    resets the local mirror from `last_state.state`, replays the
    snapshot's `events` suffix.
  - Resumes draining the job queue.
- Net result: the agent sees a tool error if a call was mid-flight
  during the drop, but the next tool call works without an
  explicit `connect` call. In-flight invoke workers we'd lost
  track of will re-deliver their results to the resumed
  subscription; the existing slot-key reverse-lookup machinery
  promotes them into state.

The only failure mode that *isn't* handled is loss of the
workstation server itself (reconnect attempts will fail). We
exponentially back off and surface the error after a handful of
attempts; the agent can `connect` again when the server comes
back.

## Stage breakdown

Each stage adds a vertical slice of agent-facing functionality. Stages
are roughly additive — a later stage assumes the earlier ones are in
place. Each stage has its own plan with a tests section and an
"Update CODE_OVERVIEW.md" tail step.

1. **[stage1_skeleton.md](./stage1_skeleton.md)** — package skeleton,
   FastMCP entry, `.fxtr_workstation.toml` reader/writer, persistent
   `/rpc` + `/client` connections via the `connection_worker`,
   `connect` / `open_session` / `close_session` tools. No sandbox yet;
   end-to-end test creates a session and verifies it appears in the
   workstation.
2. **[stage2_sandbox_lookup.md](./stage2_sandbox_lookup.md)** —
   port the eryx sandbox machinery; introduce `set_working_dir` /
   `run_script` tools; `_fxtr_mcp_builtin` exposes only
   `lookup_entity` + `get_service` + `get_id_for_service`. End-to-end
   test: another `/rpc` peer registers an entity, the agent's script
   looks it up and inspects its CBOR.
   *Post-stage-2 cleanup:*
   **[stage2_post1_cleanup.md](./stage2_post1_cleanup.md)** —
   collapse the supervisor's three subtasks (reader / worker /
   session_event_consumer) down to two; move ``McpHostImpl`` from
   a connection-lived mount on ``host_scope`` to a per-script
   mount on a per-script ``script_scope`` opened inside a new
   ``_ScriptJob`` worker handler. Plumbing-only refactor, no
   behavior change.

3. Stage 3 splits into two sub-stages.

   **[stage3a_export_state.md](./stage3a_export_state.md)** — add
   `export_entity` / `export_service` / `forget`; build the
   end-of-script mutation pipeline (patch + tracker diff + ack);
   local state mirror; foreign `state_update` reload path
   (silent — channel notifications deferred to stage 4).
   End-to-end test: agent exports an entity, sees it in the
   session state via a separate ``/client`` subscriber, then
   forgets and sees it untracked.

   **[stage3b_reconnect.md](./stage3b_reconnect.md)** —
   supervisor outer body grows a retry loop with backoff;
   in-flight requests fail with ``ConnectionLost`` on disconnect;
   reconnect re-resolves ``workstation_proxy``, re-subscribes to
   the active session, refreshes the mirror from the new snapshot.
   Test: forced disconnect mid-tool-call surfaces an error;
   follow-up tool call works post-reconnect.

4. **[stage4_invoke_results.md](./stage4_invoke_results.md)** —
   add `schedule_invoke` + `retrieve_result`; RX handling for
   `InvokeResultEvent` / `InvokeErrorEvent`; channel notification
   wiring (the ``notifications/claude/channel`` machinery from the
   spike, ported here) on slot promotion; in-flight registration
   release. End-to-end test: agent invokes a service registered by
   another peer, retrieves the result (including entity + service
   refs in the payload), and re-uses them in a follow-up script.
5. **[stage5_packaging.md](./stage5_packaging.md)** —
   `claude_plugin/` directory with manifest + marketplace + `.mcp.json`
   + stub `use_fxtr_workstation` skill; uv-based launcher; README +
   smoke walkthrough. Final integration test: subprocess the MCP
   over stdio and drive it through a sample workdir.

## Out of scope for part 2

- Full panel-viewer UI integration (`projects/fxtr_workstation/ts/` is
  not used here).
- Federation / multi-process coordination across MCPs.
- Anything in the part-2 sketch's "things we can implement
  eventually but can skip for v1" list (typed stubs, callbacks,
  package bundling).
