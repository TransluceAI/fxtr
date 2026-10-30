# Stage 1 — Package skeleton + persistent workstation connections

Goal: stand up the MCP server as a FastMCP stdio process that owns a
persistent workstation `/rpc` + `/client` connection pair, can read
and write `.fxtr_workstation.toml`, and exposes `connect` /
`open_session` / `close_session` tools. No sandbox yet; no
`run_script`.

## Scope

- `projects/fxtr_workstation_agent_plugin/` directory created with the
  three Python packages (`fxtr_workstation_mcp`,
  `fxtr_ws_mcp_sandbox_bridge`, `claude_plugin/`) — but only
  `fxtr_workstation_mcp/` is fleshed out this stage. The other two
  are placeholders (`pyproject.toml` + empty `__init__.py`) so the
  import topology is in place.
- FastMCP server entry; lifespan that owns the long-lived task group,
  `host_scope`, and connection state. **No** sandbox factory bootstrap
  yet — that lands in stage 2 (saves ~3s startup time for stage-1
  iteration).
- `.fxtr_workstation.toml` schema: `workstation_url: str`,
  `active_session: str (UUID) | absent`. Tomlkit-style read/write so we
  preserve formatting and comments.
- `ws_connection.py`:
  - `ensure_connected(app_ctx)` — idempotent, opens `/rpc` and
    `/client` if not open. Reads `workstation_url` from the toml.
    Errors if `set_working_dir` hasn't been called.
  - `WorkstationRpc` — wraps the `RPCSession` from `open_rpc_scope`
    around an httpx-ws connection (mirror of the test helper). Holds
    the workstation main service ref and a host-side mountable scope
    for transient registrations.
  - `WorkstationClient` — the `/client` connection plus the
    reader + worker tasks.
  - `connection_worker` is a TaskGroup of two tasks: **reader** and
    **worker**. Together they hold:
    - a `job_queue: MemoryObjectStream[Job]` — the worker's drain
      point; tagged-union jobs (MutationJob / SubscribeJob /
      ScriptJob (stage 2+) / EventJob (stage 3+)).
    - `pending_requests: dict[int, AnyioFuture]` keyed by
      `request_id`. Resolved by the reader on
      ack/error/committed/rejected/created/snapshot frames.
    - `active_subscription_id: int | None` so the reader knows
      which subscription's events to enqueue as EventJobs.
    - The reader opens the websocket, parses each frame, and
      dispatches by type. The worker drains the queue, runs each
      job to completion, and uses the pending-requests futures to
      await responses for requests it issues.
    - Stage 1 only wires the request-future plumbing; ScriptJob
      and EventJob are stage 2/3 additions.
- Tools (`server.py`):
  - `set_working_dir(path: str)` — accepts the working dir;
    validates that the directory exists and that
    `.fxtr_workstation.toml` parses. Doesn't yet require
    `preamble.py` (stage 2 adds that). Caches the parsed config.
    **Must be called before `connect`** (no `working_dir` arg on
    `connect`).
  - `connect() -> str` — runs `ensure_connected`. Errors if
    `set_working_dir` hasn't been called. Returns a short summary
    `"connected to <url>; session=<uuid|none>"`.
  - `open_session(metadata: dict[str, Any] | None = None) -> str` —
    errors if `active_session` already in toml; otherwise enqueues
    a SubscribeJob that issues `CreateSessionRequest`, awaits
    `SessionCreatedResponse`, then `SubscribeSessionRequest`,
    awaits `SessionSnapshotEvent`, writes the UUID to the toml,
    returns the UUID.
  - `close_session() -> str` — errors if `active_session` is not
    in the toml (per design decision: agent can manually remove a
    stale one). Otherwise enqueues a MutationJob carrying a
    `MutateSessionRequest` containing only a `CloseOp`, awaits
    commit, removes `active_session` from the toml, returns
    confirmation.

## Module / file responsibilities

- `server.py` — FastMCP setup, lifespan, the four tools listed above.
- `app_context.py` — `AppContext` dataclass: `task_group`,
  `host_scope`, `working_dir: Path | None`, `config: WorkstationConfig`,
  `rpc: WorkstationRpc | None`, `client: WorkstationClient | None`,
  `update_id_counter: itertools.count`.
- `config.py` — `WorkstationConfig` dataclass + `read_config(workdir)` /
  `write_config(workdir, **fields)`. Pure; no IO outside
  read_text/write_text.
- `ws_connection.py` — `WorkstationRpc`, `WorkstationClient`,
  `connection_worker`, `ensure_connected`. The connection helpers can
  reuse the tests-only `connect_rpc` / `connect_client` shape from
  `projects/fxtr_workstation/tests/_ws_client.py`, but they live in the
  package proper (we're a real client, not a test helper).
- `channel.py` — `send_channel_notification(session, content, meta)`
  ported from the spike. Stage 1 only uses it to confirm `connect`
  worked (smoke channel, optional — can defer to stage 4).

## Lifespan ordering

```
anyio.create_task_group()                       ← outer; deliveries +
  workstation /rpc connection (httpx-ws)            connection_worker
    open_rpc_scope(...)                              outlive any tool
      FxtrScope(isolated=True,                      call
                include=[<rpc user scope>],
                mountable=True, name="mcp_host")
        host_scope = ↑
        # NO McpHostImpl mount yet — added in stage 2 with the sandbox
        workstation /client connection (httpx-ws)
          spawn connection_worker(...) on tg
          yield AppContext(...)
```

`host_scope.include` lists the workstation `/rpc` connection's user
scope so that, when stage 2 mounts `McpHostImpl` here, its method
implementations can resolve the workstation main service into
`host_scope` and call `get_entity` / `get_service`.

## Startup-time session subscribe

If `.fxtr_workstation.toml.active_session` is set on first
`ensure_connected`, the connection_worker auto-subscribes to that
session. If the workstation returns an `ErrorResponse` (session gone),
log a warning and let the next agent-facing tool surface the error
("active_session is stale; remove from .fxtr_workstation.toml").

## Reconnect

Stage 1 does not implement auto-reconnect (sessions don't yet hold
much; the agent can call `connect` again). Stage 3 introduces it
once the session-state pipeline is solid; the design lives in the
overview's "Auto-reconnect on connection drop" section.

## Tests (stage 1)

`fxtr_workstation_mcp/tests/`. We use `pytest-anyio` (per the spike's
rationale). Reuse `fxtr_workstation`'s `WorkstationServer` test
fixture so we drive a real workstation in-process.

- `test_config.py` — read / write / round-trip through the toml.
- `test_connect_open_close_session.py` — boots a workstation, runs
  the MCP's lifespan in-process (without going through MCP stdio),
  drives the tools through their Python entry points (FastMCP
  exposes them; we can also call the underlying functions
  directly). Asserts:
  - `connect` is idempotent.
  - `open_session` writes `active_session` to toml; the workstation
    sees the session in its `sessions` registry.
  - `close_session` removes `active_session` and the workstation
    drops the session.
  - `open_session` errors when `active_session` already set.
  - `close_session` errors when `active_session` not set.
- `test_connection_worker.py` — focused unit test for the
  `connection_worker`'s request-id futures + frame routing. Uses an
  in-memory `MemoryObjectStream` pair as the websocket stand-in,
  asserts that ack/error responses route to the right future and
  unrelated `SessionEventFrame`s are buffered for stage-3 handling.

## Implementation steps

- [x] Create the directory tree per `overview.md` "File layout";
      placeholder `__init__.py` and `pyproject.toml` for each Python
      package; root `README.md` is a one-liner pointing at this
      stage.
- [x] Write `config.py` (toml read/write).
- [x] Write `ws_connection.py`: `WorkstationConnection` —
      single supervisor-task pattern owning both /rpc + /client
      connections and the active session subscription. Tools never
      enter scopes themselves; they submit typed jobs
      (``OpenSessionJob`` / ``CloseSessionJob``) onto the
      worker's queue. Reader / worker / session-event-consumer
      subtasks live under the supervisor's inner task group.
- [x] Write `app_context.py` + lifespan in `server.py`. Lifespan
      owns the long-lived task group; on exit it signals
      ``connection.shutdown()`` and the TG awaits the supervisor's
      drain.
- [x] Implement `connect`, `set_working_dir`, `open_session`,
      `close_session` tools.
- [x] Tests: ``test_config.py`` (6 unit tests) +
      ``test_connect_open_close_session.py`` (3 integration tests
      against a real ``WorkstationServer``). 9 / 9 pass.
- [x] Update `CODE_OVERVIEW.md` (root of `fxtr_workstation_agent_plugin/`).

## Resolved (from design discussion)

- **toml is required.** `connect` does *not* accept a
  `--workstation-url` arg; the agent writes the toml first.
- `connect`'s return is one line; the debug UI is the source of
  truth for connection state.
