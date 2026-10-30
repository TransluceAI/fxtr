# Migrating the remaining `_peer_rpc` copies to `fxtr_workstation.peer_rpc`

**Status: complete** (all seven sites + the tests-helper delegation
landed 2026-07-18, one commit per site, each smoked per the table
below). Was the follow-up left open by the renderer consolidation
(`projects/fxtr_experiment/plans/phase4_monitoring_ui/renderer_consolidation.md`).
Execution notes beyond the plan: open question (a) resolved as
"convert normally" (the `UnknownService` typing was incidental — both
connections immediately retyped); (b) resolved as "switch the MCP dep
to `fxtr_workstation[client]`"; (d) boot smokes sufficed, with
`debug_ui_demo` driven through its whole interactive arc via piped
newlines against a stand-in dist (the debug UI bundle itself fails to
build on a pre-existing TS error — JSX namespace — left alone).
Drive-by fix while smoking site 6: `basic_test_demo` never awaited the
now-async `add_passthrough_transport()` (pre-existing bit-rot).

## Context

`fxtr_workstation.peer_rpc.open_workstation_rpc(rpc_url, *, name)` is
the canonical client for a workstation's `/rpc` endpoint: httpx-ws
websocket → `JSONObjectStream` (the same in-band-EOF discipline as the
server-side `_ws_transport.py`) → `open_rpc_scope` typed against
`WorkstationServiceProto`, yielding fxtr2's
`RPCSession[WorkstationServiceProto]` (`.remote_service` + `.scope`).
It also folds in the httpx-ws **self-referential-exception-context
workaround** (an httpx-ws quirk that can make anyio loop forever
walking an exception chain) that only the MCP's copy previously had —
so converting a site is a small robustness upgrade, not just dedup.
`httpx-ws` rides the `client` optional extra
(`fxtr_workstation[client]`); the module is import-on-demand and not
re-exported from `__init__`.

One site is already converted and is the reference diff:
`examples/fxtr_experiment_demo/run_experiment_demo.py` (see the
"fxtr_workstation.peer_rpc" commit). A second consumer,
`examples/fxtr_experiment_demo/reload_renderers.py`, shows the
resolve-by-external-name idiom.

## The mechanical recipe (identical at every site)

Each copy is a local `~40`-line block: a `_ws_stream`-style CM adapting
`httpx_ws.aconnect_ws` → `JSONObjectStream`, then `open_rpc_scope(...)`.
Replace it with:

```python
from fxtr_workstation.peer_rpc import open_workstation_rpc
...
async with open_workstation_rpc(rpc_url, name="<the site's existing name>") as peer:
    ...  # peer.remote_service / peer.scope, exactly as before
```

- **Keep the site's existing `name=`** (it's the diagnostic label).
- Delete the local helper + now-unused imports (`httpx_ws`,
  `JSONObjectStream`, `wrap_json_stream`, `open_rpc_scope`,
  `RPCSession` if only used to annotate the deleted CM,
  `WorkstationServiceProto` if unused elsewhere in the file).
- Every surveyed site passes `our_main_service=None`, which is what
  the helper hardwires — no behavioral deltas to reason about.
- `peer.remote_service` is a `ServiceRef` (resolve it before calling
  methods); every site already does this, so bodies are untouched.

## Site inventory

Surveyed 2026-07-18; all seven use `our_main_service=None`, six type
the remote as `WorkstationServiceProto` (one exception noted).

| # | site | venv / deps action | smoke |
|---|---|---|---|
| 1 | `examples/fxtr_workstation_demo/run_demo_workstation.py` (`name="fxtr-workstation-demo/peer-rpc"`) | own uv project — change the `fxtr_workstation` dep to `fxtr_workstation[client]` + `uv lock` (same as the experiment demo's commit) | boot the orchestrator headless to its banner (it parks like the experiment demo); its pytest suite doesn't drive the orchestrator, so the boot IS the check |
| 2 | `frontend_lib/packages/fxtr-ui-workstation-demo/src/demo_workstation.py` | none — `src/server.ts` spawns `uv run python` from the package dir, which walks up to the **workstation project's own venv** (the package is in it; `httpx-ws` is in its dev group) | `uv run python src/demo_workstation.py` from the package dir → wait for the `READY ws://…` stdout line, Ctrl-C |
| 3 | `frontend_lib/packages/fxtr-ui-demo/src/demo_workstation.py` | none (same venv walk-up) | same `READY` handshake |
| 4 | `frontend_lib/packages/fxtr-ui-dispatch-demo/src/dispatch_demo_workstation.py` | none | same `READY` handshake |
| 5 | `frontend_lib/packages/fxtr-ui-dynamic-dispatch-demo/src/dynamic_dispatch_demo_workstation.py` | none | same `READY` handshake (note: this one imports `open_rpc_scope` *inside* a function — the replacement import can go to module top) |
| 6 | `projects/fxtr_workstation_agent_plugin/examples/basic_test_demo/run_demo_workstation.py` (`name="basic_test_demo_peer"`) | runs in the MCP project venv, which depends on `fxtr_workstation` (path dep) AND declares `httpx-ws` directly — works as-is; see open question (b) for the hygiene call. Uses `fxtr_workstation_mcp.websocket_util.aconnect_ws` today (the patched variant) — the helper subsumes it | this example backs the MCP test suites (`test_mcp_in_memory.py` uses its workdir + scenarios) — run the MCP tests; NOTE they flake under `-n auto`, rerun failures serially before believing them |
| 7 | `projects/fxtr_workstation/examples/debug_ui_demo.py` (two connections, `name="alice-rpc"` / `"bob-rpc"`) | none (workstation venv) | run the example script to its banner; see open question (a) first |

## Out of scope (deliberately — do not convert)

- **`fxtr_workstation_mcp/ws_connection.py` + `websocket_util.py`**:
  the MCP's *persistent* connection layer, not a one-shot peer session
  (different lifecycle/ownership). Its `websocket_util` copy of the
  exception-context patch stays; converging the MCP onto shared
  transport plumbing is a separate design question.
- **`v2/examples/*`** (`rpc_traceback`, `service_spawner`): raw fxtr2
  `open_rpc_scope` demos with no workstation on the other end — the
  helper doesn't apply.
- **`fxtr_sandboxing` / `fxtr_sandbox_rpc_adapter`**: their
  `open_rpc_scope` uses are over sandbox transports, not workstation
  websockets.

## Open questions (settle before / during execution)

- **(a) `debug_ui_demo.py` types its remotes as `UnknownService`**,
  not `WorkstationServiceProto`. Check what alice/bob actually invoke:
  if they call workstation methods, the helper's typed remote is a
  strict improvement (convert normally); if the untyped-ness is the
  *point* of the example (exercising `replace_service_type`), either
  add `.replace_service_type(UnknownService)` after the helper or
  leave this site on raw `open_rpc_scope` with a comment saying why.
- **(b) MCP example dep hygiene**: the MCP venv has `httpx-ws` only
  because the MCP itself declares it — the example would import
  `peer_rpc` without the `[client]` extra being requested anywhere.
  Options: switch the MCP's `fxtr_workstation` dep to
  `fxtr_workstation[client]` (honest, costs a relock of the MCP
  lock + any downstream locks embedding its metadata), or accept the
  incidental coverage with a comment. Leaning: switch the dep — it's
  the same "make the import honest" argument as the original commit.
- **(c) `projects/fxtr_workstation/tests/_ws_client.py` convergence**
  *(settled 2026-07-18)*: `connect_rpc` DELEGATES to
  `open_workstation_rpc`, keeping its untyped signature so no
  downstream test changes — `RPCSession` is a frozen dataclass, so the
  wrapper is `dataclasses.replace(session, remote_service=
  session.remote_service.replace_service_type(UnknownService))`. Its
  docstring says it delegates and recommends `peer_rpc` for non-test
  code, with a TODO to migrate the tests and remove. This deletes the
  file's `_httpx_ws_object_stream` copy; note `tests/_websocket_util.py`
  **stays** — the `/client`-endpoint half (`ClientWS` /
  `connect_client`) still uses its patched `aconnect_ws`, and peer_rpc
  covers only `/rpc`. Do this site LAST (most likely to be hairier
  than expected).
- **(d) smoke depth**: the table's boot-to-banner/`READY` checks cover
  the converted code path (the peer connection is exercised at boot —
  every site registers its services through it). A browser pass per
  frontend_lib demo would be extra credit, not required: nothing
  frontend-facing changes.

## Commit structure (settled 2026-07-18)

One commit per site, each with its smoke, in inventory order —
`examples/fxtr_workstation_demo` first (the dep/lock change), then the
four frontend_lib backends, the agent-plugin example (+ the (b) dep
switch), `debug_ui_demo.py` (after resolving (a)), and the
`tests/_ws_client.py` delegation LAST (per (c)).

Update `projects/fxtr_workstation/CODE_OVERVIEW.md`'s `peer_rpc.py`
section when done (it currently names the demo copies as extant), and
strike the follow-up note in `renderer_consolidation.md`.
